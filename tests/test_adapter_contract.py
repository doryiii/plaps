"""Opt-in contract test: the real adapter against the real llama.cpp endpoint.

Skipped unless PLAPS_ADAPTER_URL is set (for example
PLAPS_ADAPTER_URL=http://127.0.0.1:8095). The fake adapter covers logic; only
this test exercises the real endpoint's behavior, and only on demand.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from plaps.embed.adapter import EmbeddingAdapter

pytestmark = pytest.mark.skipif(
    not os.environ.get("PLAPS_ADAPTER_URL"),
    reason="PLAPS_ADAPTER_URL not set — opt-in contract test",
)

MODEL_ID = os.environ.get("PLAPS_ADAPTER_MODEL", "embeddinggemma-2")
DIM = int(os.environ.get("PLAPS_ADAPTER_DIM", "768"))


@pytest.fixture
def adapter():
    return EmbeddingAdapter(os.environ["PLAPS_ADAPTER_URL"], MODEL_ID)


def test_served_model_matches_configuration(adapter):
    info = adapter.probe(force=True)
    assert info["reachable"], f"endpoint unreachable: {info}"
    assert info["matches"], (
        f"endpoint serves {info['served_model']!r}, which does not match the "
        f"configured {MODEL_ID!r} — set PLAPS_ADAPTER_MODEL accordingly"
    )


def test_image_embedding_dimensions_and_normalization(adapter, tmp_path):
    import pyvips

    pyvips.cache_set_max(0)
    data = pyvips.Image.black(64, 48).cast("uchar").jpegsave_buffer()
    vector = adapter.embed_images([data])[0]
    assert vector.shape == (DIM,)
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-3


def test_text_and_image_share_one_space(adapter, tmp_path):
    """Cross-modal joint space: a beach photo and 'a beach' must be closer than
    the photo and an unrelated string."""
    import pyvips

    pyvips.cache_set_max(0)
    data = (
        pyvips.Image.black(128, 64)
        .cast("uchar")
        .draw_rect([230, 180, 120], 0, 32, 128, 32)  # a sandy stripe
        .jpegsave_buffer()
    )
    image_vector = adapter.embed_images([data])[0]
    beach = adapter.embed_text("a sandy beach")
    tax = adapter.embed_text("tax paperwork")
    assert float(image_vector @ beach) > float(image_vector @ tax)


def test_batch_order_is_preserved(adapter, tmp_path):
    import pyvips

    pyvips.cache_set_max(0)
    images = [pyvips.Image.black(32 + 8 * i, 48).cast("uchar").jpegsave_buffer() for i in range(3)]
    batched = adapter.embed_images(images)
    serial = [adapter.embed_images([image])[0] for image in images]
    for batch_vector, serial_vector in zip(batched, serial, strict=True):
        assert float(batch_vector @ serial_vector) > 0.99999  # float drift only
