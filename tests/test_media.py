"""Variant dimensions and formats, cache hit/miss, immutable cache headers,
Range request handling, originals served by id only, tombstoned originals 404,
and unknown hashes never generated."""

import pyvips
from conftest import jpeg_bytes

pyvips.cache_set_max(0)


def first_card(harness):
    return harness.client.get("/api/photos?limit=1").get_json()["photos"][0]


def test_square_variant_is_exactly_512(harness, library):
    library.add("wide.jpg", width=1600, height=900)
    harness.scan()
    card = first_card(harness)
    response = harness.client.get(f"/media/thumb/{card['content_hash']}.sq.webp")
    assert response.status_code == 200
    image = pyvips.Image.new_from_buffer(response.data, "")
    assert image.width == 512 and image.height == 512  # exact square via ATTENTION crop
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_aspect_variant_max_edge_512(harness, library):
    library.add("pano.jpg", width=2048, height=512)
    harness.scan()
    card = first_card(harness)
    response = harness.client.get(f"/media/thumb/{card['content_hash']}.as.webp")
    image = pyvips.Image.new_from_buffer(response.data, "")
    assert max(image.width, image.height) == 512
    assert image.width / image.height == 4  # aspect preserved: 2048x512 → 512x128


def test_auto_rotation_applied(harness, library):
    # 480x640 pixels with Orientation 6: the derived image is auto-rotated
    library.add("portrait.jpg", width=480, height=640, orientation=6)
    harness.scan()
    card = first_card(harness)
    assert (card["width"], card["height"]) == (640, 480)  # stored display dimensions
    response = harness.client.get(f"/media/thumb/{card['content_hash']}.as.webp")
    image = pyvips.Image.new_from_buffer(response.data, "")
    assert image.width > image.height  # rotated to landscape


def test_screenfit_lazy_then_cached(harness, library):
    library.add("big.jpg", width=3000, height=2000)
    harness.scan()
    card = first_card(harness)
    # no worker drain: the large variant exists only after first view
    import pathlib

    cache_file = (
        pathlib.Path(harness.settings.cache_dir)
        / "large"
        / card["content_hash"][:2]
        / f"{card['content_hash']}.webp"
    )
    assert not cache_file.exists()  # lazy by decision (O3)
    response = harness.client.get(f"/media/large/{card['content_hash']}.webp")
    assert response.status_code == 200
    image = pyvips.Image.new_from_buffer(response.data, "")
    assert max(image.width, image.height) == 2048
    assert cache_file.exists()  # generated on miss, in the request thread


def test_cache_hit_reuses_file(harness, library):
    library.add("hit.jpg")
    harness.scan()
    harness.drain()  # eager thumbs via the worker
    card = first_card(harness)
    first = harness.client.get(f"/media/thumb/{card['content_hash']}.sq.webp")
    second = harness.client.get(f"/media/thumb/{card['content_hash']}.sq.webp")
    assert first.data == second.data
    assert harness.cache.size() > 0


def test_unknown_hash_never_generated(harness, library):
    library.add("one.jpg")
    harness.scan()
    fake = "0" * 64
    assert harness.client.get(f"/media/thumb/{fake}.sq.webp").status_code == 404
    assert harness.client.get(f"/media/large/{fake}.webp").status_code == 404
    assert harness.client.get("/media/thumb/nothex.sq.webp").status_code == 404
    assert not (harness.settings.cache_dir / "thumb" / "00").exists() or (
        not (harness.settings.cache_dir / "thumb" / "00" / f"{fake}.sq.webp").exists()
    )


def test_original_served_by_id_with_range(harness, library):
    content = jpeg_bytes(width=32, height=32)
    library.add("orig.jpg", content=content)
    harness.scan()
    card = first_card(harness)
    response = harness.client.get(f"/original/{card['photo_id']}")
    assert response.status_code == 200
    assert response.data == content
    # Range/206 handled by Werkzeug
    ranged = harness.client.get(
        f"/original/{card['photo_id']}",
        headers={"Range": "bytes=0-9"},
    )
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"].startswith("bytes 0-9/")
    assert len(ranged.data) == 10


def test_tombstoned_original_404(harness, library):
    library.add("bye.jpg")
    harness.scan()
    photo_id = first_card(harness)["photo_id"]
    library.delete("bye.jpg")
    harness.scan()
    assert harness.client.get(f"/original/{photo_id}").status_code == 404


def test_original_for_missing_id_404(harness, library):
    library.add("here.jpg")
    harness.scan()
    assert harness.client.get("/original/99999").status_code == 404
