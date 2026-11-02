"""Cursor windows — ordering (including the unknown-date sentinel), bounds,
limit/order validation, day-inclusive date bounds, and stability under
concurrent inserts."""

from conftest import set_mtime_ns


def seed(harness, library, count=6):
    """count photos with distinct EXIF dates, one of them without EXIF."""
    for i in range(count - 1):
        library.add(f"p{i}.jpg", exif_date=f"2021:01:0{i + 1} 10:00:00")
    library.add("no-date.jpg")  # mtime fallback — a real date
    set_mtime_ns(library.root / "no-date.jpg", 1_600_000_000_000_000_000)
    harness.scan()


def test_ordering_desc_by_default(harness, library):
    seed(harness, library)
    body = harness.client.get("/api/photos").get_json()
    dates = [card["date_taken"] for card in body["photos"]]
    assert dates == sorted(dates, reverse=True)
    assert body["photos"][0]["photo_id"] != body["photos"][-1]["photo_id"]


def test_ordering_asc_and_card_fields(harness, library):
    seed(harness, library)
    body = harness.client.get("/api/photos?order=asc").get_json()
    dates = [card["date_taken"] for card in body["photos"]]
    assert dates == sorted(dates)
    card = body["photos"][0]
    assert set(card) == {
        "photo_id",
        "content_hash",
        "width",
        "height",
        "date_taken",
        "date_source",
        "thumb_sq",
        "thumb_as",
        "large",
        "original",
    }
    assert card["thumb_sq"].startswith("/media/thumb/")
    assert card["original"] == f"/original/{card['photo_id']}"


def test_unknown_date_sentinel_sorts_oldest(harness, library):
    library.add("known.jpg", exif_date="2021:06:01 00:00:00")
    library.add("weird.jpg")
    set_mtime_ns(library.root / "weird.jpg", 0)
    harness.scan()
    body = harness.client.get("/api/photos?order=asc").get_json()
    assert body["photos"][0]["date_taken"] == "0001-01-01T00:00:00"
    assert body["photos"][0]["date_source"] == "unknown"


def test_pagination_windows_are_contiguous(harness, library):
    seed(harness, library, count=6)
    seen = []
    cursor = None
    while True:
        url = "/api/photos?order=asc&limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = harness.client.get(url).get_json()
        seen.extend(card["photo_id"] for card in body["photos"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 6
    assert len(set(seen)) == 6  # no skipped or repeated entries


def test_windows_stable_under_concurrent_inserts(harness, library):
    seed(harness, library, count=4)
    first = harness.client.get("/api/photos?order=asc&limit=2").get_json()
    cursor = first["next_cursor"]
    # the scanner inserts a photo that sorts before the second window
    library.add("newer.jpg", exif_date="2021:01:01 00:00:00")
    harness.scan()
    second = harness.client.get(f"/api/photos?order=asc&limit=2&cursor={cursor}").get_json()
    ids = [card["photo_id"] for card in first["photos"] + second["photos"]]
    assert len(ids) == 4
    assert len(set(ids)) == 4  # keyset pagination: the insert skipped nothing


def test_limit_and_order_validation(harness, library):
    seed(harness, library)
    assert harness.client.get("/api/photos?limit=0").status_code == 400
    assert harness.client.get("/api/photos?limit=501").status_code == 400
    assert harness.client.get("/api/photos?limit=abc").status_code == 400
    assert harness.client.get("/api/photos?order=sideways").status_code == 400
    assert harness.client.get("/api/photos?cursor=@@@bad@@@").status_code == 400


def test_day_inclusive_date_bounds(harness, library):
    seed(harness, library)
    # everything on 2021-01-02..2021-01-03 inclusive, oldest-first
    body = harness.client.get("/api/photos?order=asc&after=2021-01-02&before=2021-01-03").get_json()
    dates = {card["date_taken"][:10] for card in body["photos"]}
    assert dates == {"2021-01-02", "2021-01-03"}


def test_date_bounds_validation(harness, library):
    seed(harness, library)
    assert harness.client.get("/api/photos?after=notadate").status_code == 400
    assert harness.client.get("/api/photos?before=2021-13-99").status_code == 400


def test_tombstoned_photos_leave_the_timeline(harness, library):
    seed(harness, library, count=3)
    library.delete("p0.jpg")
    harness.scan()
    body = harness.client.get("/api/photos?order=asc&limit=100").get_json()
    paths = [card["photo_id"] for card in body["photos"]]
    assert len(paths) == 2


def test_error_shape_on_404(harness, library):
    response = harness.client.get("/api/nonexistent")
    assert response.status_code == 404
    body = response.get_json()
    assert body["error"]["code"] == "not_found"
