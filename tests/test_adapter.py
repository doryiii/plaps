"""Unit — the real adapter against a mocked HTTP transport (httpx.MockTransport):
payload construction (image parts nested inside `content`; the flat shape and
data-URI-as-string traps never hit the wire), defensive normalization, the
typed error split, and the served-model check that doubles as the reachability
probe, cached 60 s. Only the contract test needs the live endpoint."""

import base64
import json

import httpx
import numpy as np
import pytest

from plaps import config
from plaps.embed.adapter import (
    EmbeddingAdapter,
    EndpointUnreachable,
    ItemRejected,
    ModelMismatch,
)


class MockEndpoint:
    """Records requests; answers with scripted responses."""

    def __init__(self, model_id="ggmlorg-test-model-BF16", status=200, vectors=None, count=None):
        self.requests = []
        self.model_id = model_id
        self.status = status
        self.vectors = vectors if vectors is not None else [[0.6] * 768]
        self.count = count  # override the number of returned vectors

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/models":
            # alternate between the two shapes seen in the wild
            if len(self.requests) % 2:
                return httpx.Response(200, json={"data": [{"id": self.model_id}]})
            return httpx.Response(200, json={"models": [{"name": self.model_id, "model": self.model_id}]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        data = [{"embedding": v} for v in (self.vectors[: self.count] if self.count else self.vectors)]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 1}})


def make_adapter(endpoint: MockEndpoint) -> EmbeddingAdapter:
    return EmbeddingAdapter(
        "http://aibox:8095", "test-model", transport=httpx.MockTransport(endpoint.handler)
    )


def test_image_payload_nests_parts_inside_content():
    image = b"\xff\xd8fakejpeg"
    endpoint = MockEndpoint(vectors=[[0.1] * 768, [0.2] * 768])
    adapter = make_adapter(endpoint)
    adapter.embed_images([image, image])

    sent = json.loads(endpoint.requests[-1].read())
    assert "input" in sent
    for item in sent["input"]:
        # the only working shape: typed parts one level deeper, inside content
        assert set(item) == {"content"}
        part = item["content"][0]
        assert part["type"] == "image_url"
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == image
    # and never the flat shape, never a data-URI as a plain string
    assert all(not isinstance(item, str) for item in sent["input"])
    assert all("type" not in item or "content" in item for item in sent["input"])


def test_text_query_carries_the_search_instruction():
    """The query side of EmbeddingGemma's asymmetric search task: the model
    card's instruction must prefix the raw query (image docs need none)."""
    endpoint = MockEndpoint()
    make_adapter(endpoint).embed_text("a dog on the beach")
    sent = json.loads(endpoint.requests[-1].read())
    assert sent == {"input": "task: search result | query: a dog on the beach"}


def test_defensive_normalization_of_unnormalized_responses():
    endpoint = MockEndpoint(vectors=[[3.0, 4.0]])
    vector = make_adapter(endpoint).embed_text("q")
    assert vector.dtype == np.float32
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6


def test_error_split_unreachable_vs_rejected():
    endpoint = MockEndpoint(status=500)
    with pytest.raises(EndpointUnreachable):
        make_adapter(endpoint).embed_text("q")
    endpoint = MockEndpoint(status=400)
    with pytest.raises(ItemRejected):
        make_adapter(endpoint).embed_text("q")
    # a transport-level failure is unreachable, not a rejection
    adapter = EmbeddingAdapter(
        "http://aibox:8095",
        "test-model",
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("no route"))),
    )
    with pytest.raises(EndpointUnreachable):
        adapter.embed_text("q")


def test_short_or_malformed_responses_are_unreachable():
    endpoint = MockEndpoint(vectors=[[0.1] * 3])
    with pytest.raises(EndpointUnreachable):  # fewer vectors than inputs
        make_adapter(endpoint).embed_images([b"a", b"b"])
    # a 200 with a non-JSON body is unreachable, never a partial result
    adapter = EmbeddingAdapter(
        "http://aibox:8095",
        "test-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
    )
    with pytest.raises(EndpointUnreachable):
        adapter.embed_text("q")


def test_served_model_check_and_probe_caching():
    endpoint = MockEndpoint(model_id="ggmlorg-test-model-BF16")
    adapter = make_adapter(endpoint)
    # config names the model; the server reports a qualified name — substring
    info = adapter.probe()
    assert info == {"reachable": True, "served_model": "ggmlorg-test-model-BF16", "matches": True}
    adapter.embed_text("q")
    models_calls = [r for r in endpoint.requests if r.url.path == "/v1/models"]
    assert len(models_calls) == 1  # the embed path shares the cached probe

    # a different served model trips the retryable mismatch, before any embed
    other = make_adapter(MockEndpoint(model_id="qwen3-embedding-8b"))
    with pytest.raises(ModelMismatch):
        other.embed_text("q")


def test_probe_unreachable_endpoint():
    adapter = EmbeddingAdapter(
        "http://aibox:8095",
        "test-model",
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("no route"))),
    )
    assert adapter.probe() == {"reachable": False, "served_model": None, "matches": False}
    with pytest.raises(EndpointUnreachable):
        adapter.embed_text("q")


def test_probe_result_cached_within_ttl(monkeypatch):
    endpoint = MockEndpoint()
    adapter = make_adapter(endpoint)
    clock = {"t": 1000.0}
    monkeypatch.setattr("plaps.embed.adapter.time.time", lambda: clock["t"])
    adapter.probe()
    adapter.probe()
    adapter.embed_text("q")  # the embed path shares the cached probe
    models_calls = [r for r in endpoint.requests if r.url.path == "/v1/models"]
    assert len(models_calls) == 1  # cached 60 s
    clock["t"] += config.PROBE_TTL + 1
    adapter.probe()
    assert len([r for r in endpoint.requests if r.url.path == "/v1/models"]) == 2
