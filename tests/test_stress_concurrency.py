"""Threaded browsing during a live scanner pass and a large original download:
no lock errors, consistent windows, and timeline requests under ~100 ms while
the download streams. The one place nondeterminism is accepted."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from conftest import jpeg_bytes

TIMELINE_TARGET_MS = 100.0


def seed(library, count=60):
    for i in range(count):
        library.add(f"s{i // 10:02d}/p{i:03d}.jpg", exif_date=f"2021:01:01 00:{i // 60:02d}:{i % 60:02d}")


def test_browse_during_scan_and_download(harness, library):
    seed(library, count=30)
    harness.scan()

    # a large original to stream while browsing (a few MB keeps the test fast
    # but long enough to overlap several timeline requests)
    big = jpeg_bytes(width=2400, height=1800)
    library.add("big/download.jpg", content=big)
    harness.scan()
    big_id = harness.db.reader().execute(
        "SELECT photo_id FROM photos WHERE path='big/download.jpg'"
    ).fetchone()["photo_id"]

    errors: list[Exception] = []
    latencies: list[float] = []
    stop = threading.Event()

    def browse():
        client = harness.app.test_client()
        cursor = None
        seen = []
        while not stop.is_set():
            start = time.perf_counter()
            try:
                url = "/api/photos?order=asc&limit=10"
                if cursor:
                    url += f"&cursor={cursor}"
                body = client.get(url).get_json()
                ids = [card["photo_id"] for card in body["photos"]]
                assert len(set(ids)) == len(ids), "repeated entries in one window"
                seen.extend(ids)
                cursor = body["next_cursor"]
            except Exception as exc:
                errors.append(exc)
                return
            latencies.append((time.perf_counter() - start) * 1000)

    def scan_passes():
        while not stop.is_set():
            try:
                harness.scanner.run_pass("pruned")
            except Exception as exc:
                errors.append(exc)
                return
            time.sleep(0.01)

    def download():
        client = harness.app.test_client()
        while not stop.is_set():
            try:
                response = client.get(f"/original/{big_id}")
                assert response.status_code == 200
                assert len(response.data) == len(big)
            except Exception as exc:
                errors.append(exc)
                return

    with ThreadPoolExecutor(max_workers=7) as pool:
        pool.submit(scan_passes)
        pool.submit(download)
        for _ in range(5):
            pool.submit(browse)
        time.sleep(5.0)
        stop.set()

    assert not errors, errors
    assert len(latencies) >= 20, f"too few samples: {len(latencies)}"
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 < TIMELINE_TARGET_MS, f"timeline p95 {p95:.0f} ms exceeded the target"


def test_concurrent_media_generation_is_safe(harness, library):
    """Two threads racing the same cache miss: one file, one accounting entry,
    both requests succeed."""
    content = jpeg_bytes(width=800, height=600)
    library.add("race.jpg", content=content)
    harness.scan()
    card = harness.client.get("/api/photos?limit=1").get_json()["photos"][0]

    results = []
    barrier = threading.Barrier(4)

    def hit():
        client = harness.app.test_client()
        barrier.wait()
        response = client.get(f"/media/large/{card['content_hash']}.webp")
        results.append(response.status_code)

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [200, 200, 200, 200]
    import pathlib

    target = (
        pathlib.Path(harness.settings.cache_dir)
        / "large"
        / card["content_hash"][:2]
        / f"{card['content_hash']}.webp"
    )
    assert target.exists()
    # and the counter counted the file exactly once
    assert harness.cache.size() == target.stat().st_size
