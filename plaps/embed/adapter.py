"""The embedding endpoint client — the quarantine boundary for llama.cpp quirks.

The spike (docs/spikes/embedding-endpoint.md) pins the contract: image parts
must be nested inside a ``content`` array (the flat OpenAI shape 400s), a
data-URI sent as a plain string is silently tokenized as text (garbage, never
do it), vectors come back 768-dim and server-normalized (normalized defensively
anyway), and batch order is preserved. The served-model check doubles as the
reachability probe: one cached ``GET /v1/models`` answers both "is the endpoint
up" and "is it serving the model the config claims" — a mismatch backs off
instead of mis-stamping vectors.
"""

from __future__ import annotations

import base64
import time

import httpx
import numpy as np

from plaps import config

EMBED_PATH = "/v1/embeddings"
MODELS_PATH = "/v1/models"
REQUEST_TIMEOUT = 300.0  # one image is ~2.5 s on slow hardware; a batch of 8 much longer
# EmbeddingGemma's search-task instruction for the *query* side only; image
# documents are sent with no prefix (the model card's asymmetry rule).
QUERY_PREFIX = "task: search result | query: "


class AdapterError(Exception):
    """Base class for every typed adapter failure."""


class EndpointUnreachable(AdapterError):
    """Retryable: connection errors, 5xx, malformed or short responses."""


class ModelMismatch(AdapterError):
    """Retryable: the endpoint serves a model other than the configured one."""


class ItemRejected(AdapterError):
    """Per-file failure: the endpoint refused this payload (4xx)."""


class EmbeddingAdapter:
    """The only module that talks to the embedding endpoint."""

    def __init__(self, base_url: str, model_id: str, transport=None):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id  # from config — the single source of truth
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=config.PROBE_TIMEOUT),
        )
        self._probe: tuple[float, dict] | None = None

    # -- probe -------------------------------------------------------------

    def probe(self, force: bool = False) -> dict:
        """One GET /v1/models: reachability + served-model check, cached 60 s.

        Tolerant of both response shapes seen in the wild: OpenAI's
        ``{"data": [{"id": ...}]}`` and llama.cpp's ``{"models": [{"name": ...,
        "model": ...}]}`` — the spike's build and the deployed one differ."""
        now = time.time()
        if not force and self._probe and now - self._probe[0] < config.PROBE_TTL:
            return dict(self._probe[1])
        try:
            response = self._client.get(self.base_url + MODELS_PATH, timeout=config.PROBE_TIMEOUT)
            response.raise_for_status()
            served = self._served_model(response.json())
            result = {
                "reachable": True,
                "served_model": served,
                "matches": self._model_matches(served),
            }
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            result = {"reachable": False, "served_model": None, "matches": False}
        self._probe = (time.time(), result)
        return dict(result)

    @staticmethod
    def _served_model(payload: dict) -> str:
        entries = payload.get("data") or payload.get("models") or []
        first = entries[0]
        return first.get("id") or first.get("model") or first.get("name")

    def _model_matches(self, served: str) -> bool:
        """The config names the model; the server reports a qualified name
        (e.g. 'embeddinggemma-2' vs 'ggmlorg-embeddinggemma-2-BF16')."""
        return self.model_id.lower() in served.lower()

    def _verify_model(self) -> None:
        info = self.probe()
        if not info["reachable"]:
            raise EndpointUnreachable("embedding endpoint unreachable")
        if not info["matches"]:
            raise ModelMismatch(f"endpoint serves {info['served_model']!r}, config says {self.model_id!r}")

    # -- embedding ---------------------------------------------------------

    def embed_images(self, images: list[bytes]) -> list[np.ndarray]:
        """Embed original file bytes — image parts nested inside `content`."""
        self._verify_model()
        payload = {
            "input": [
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64," + base64.b64encode(image).decode()
                            },
                        }
                    ]
                }
                for image in images
            ]
        }
        return [self._normalize(vector) for vector in self._post(payload, len(images))]

    def embed_text(self, text: str) -> np.ndarray:
        """Embed a search query with the model's asymmetric search instruction.

        EmbeddingGemma's retrieval task is asymmetric: the *query* side carries
        the ``task: search result | query:`` instruction, while image documents
        take no prefix (embed_images sends them bare, as the model card
        directs). Measured on the live endpoint, the prefix lifts top-1
        similarity by ~0.02 to 0.05 without changing ranking order — which is
        what makes the configured similarity floor meaningful.
        """
        self._verify_model()
        return self._normalize(
            self._post({"input": QUERY_PREFIX + text}, 1)[0]
        )

    def _post(self, payload: dict, expected: int) -> list:
        try:
            response = self._client.post(self.base_url + EMBED_PATH, json=payload)
        except httpx.HTTPError as exc:
            raise EndpointUnreachable(str(exc)) from exc
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise EndpointUnreachable(f"endpoint returned {response.status_code}")
        if response.status_code >= 400:
            raise ItemRejected(f"endpoint rejected the request: {response.status_code}")
        try:
            vectors = [item["embedding"] for item in response.json()["data"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise EndpointUnreachable("malformed embedding response") from exc
        if len(vectors) != expected:
            raise EndpointUnreachable(f"expected {expected} vectors, got {len(vectors)}")
        return vectors

    @staticmethod
    def _normalize(vector) -> np.ndarray:
        """Defensive L2 normalization — never assume the server normalized."""
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        return array / norm if norm > 0 else array
