"""/api/status: the owner's entire debugging surface — scan_stalled logic,
guard-state surfacing and the resume transition, cache-size accounting
(including a file created by the request thread), error counts by stage, and
the ignored-file count."""

import json
import time

from conftest import jpeg_bytes


def status(harness):
    response = harness.client.get("/api/status")
    assert response.status_code == 200
    return response.get_json()


def test_status_shape(harness, library):
    library.add("a.jpg")
    harness.scan()
    info = status(harness)
    assert set(info) == {"scan", "queue", "errors", "ignored_files", "cache", "guard"}
    assert info["scan"]["stats"]["added"] == 1
    assert info["scan"]["checkpoint"]["phase"] == "phase2"
    assert info["queue"]["thumb"]["pending"] == 1
    assert info["errors"] == {"exif": 0, "thumb": 0, "embed": 0}
    assert info["ignored_files"] == 0
    assert info["guard"]["tripped"] is False


def test_ignored_files_counted(harness, library):
    library.add("a.jpg")
    library.add("notes.txt", content=b"not a photo")
    library.add("raw.cr2", content=b"\x00")
    harness.scan()
    assert status(harness)["ignored_files"] == 2


def test_scan_stalled_logic(harness, library):
    library.add("a.jpg")
    harness.scan()
    harness.db.set_meta("scanner_idle", "0")
    info = status(harness)
    assert info["scan"]["scan_stalled"] is False  # fresh checkpoint
    # age the heartbeat past the 15-minute threshold while not idle
    checkpoint = json.loads(harness.db.get_meta("scan_checkpoint"))
    checkpoint["ts"] = time.time() - 901
    harness.db.set_meta("scan_checkpoint", json.dumps(checkpoint))
    assert status(harness)["scan"]["scan_stalled"] is True
    # idle backoff exempts the scanner from the stalled flag
    harness.db.set_meta("scanner_idle", "1")
    assert status(harness)["scan"]["scan_stalled"] is False


def test_error_counts_by_stage(harness, library, monkeypatch):
    library.add("bad-exif.jpg", content=b"\xff\xd8\xff\xe0not-really-exif" + b"\x00" * 64)
    harness.scan()
    info = status(harness)
    assert info["errors"]["exif"] == 1  # unreadable EXIF is recorded per stage


def test_cache_size_accounting_includes_request_thread_files(harness, library):
    library.add("viewed.jpg", width=800, height=600)
    harness.scan()
    before = status(harness)["cache"]["size_bytes"]
    assert before == 0  # nothing derived yet
    # a first view generates the screen-fit file in the request thread
    card = harness.client.get("/api/photos").get_json()["photos"][0]
    response = harness.client.get(f"/media/large/{card['content_hash']}.webp")
    assert response.status_code == 200
    after = status(harness)["cache"]["size_bytes"]
    assert after > before


def test_cache_size_survives_duplicate_hits_without_double_count(harness, library):
    library.add("dup1.jpg", content=jpeg_bytes(width=40, height=40))
    library.add("dup2.jpg", content=jpeg_bytes(width=40, height=40))
    harness.scan()
    card = harness.client.get("/api/photos?limit=1").get_json()["photos"][0]
    for _ in range(3):
        assert harness.client.get(f"/media/large/{card['content_hash']}.webp").status_code == 200
    info = status(harness)
    assert info["cache"]["size_bytes"] > 0
    # one derived file counted once, no matter how many requests hit it
    import pathlib

    cache_file = (
        pathlib.Path(harness.settings.cache_dir)
        / "large"
        / card["content_hash"][:2]
        / f"{card['content_hash']}.webp"
    )
    assert info["cache"]["size_bytes"] == cache_file.stat().st_size


def test_guard_state_surfaced_and_resume_transition(make_harness, library):
    harness = make_harness(**{"scanner.guard_threshold": 10})
    for i in range(10):
        library.add(f"g{i}.jpg", content=jpeg_bytes(width=8 + i, height=8))
    harness.scan()
    for i in range(10):
        library.delete(f"g{i}.jpg")
    harness.scan()
    info = status(harness)
    assert info["guard"]["tripped"] is True
    assert "vanishes" in info["guard"]["reason"]
    response = harness.client.post("/api/admin/guard/resume")
    assert response.status_code == 200
    assert response.get_json()["guard"]["tripped"] is False
    assert status(harness)["guard"]["tripped"] is False


def test_backup_writes_timestamped_snapshot(tmp_path, library, harness):
    harness.scan()
    target = harness.db.backup(harness.settings.backup_dir)
    assert target.exists() and target.parent == harness.settings.backup_dir
    assert target.name.startswith("plaps-")
    # and a second run produces a new timestamped file, keeping both
    time.sleep(1.1)  # the timestamp has second granularity
    second = harness.db.backup(harness.settings.backup_dir)
    assert second != target and second.exists() and target.exists()
