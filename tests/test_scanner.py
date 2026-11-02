"""API-level scanner behavior via temp trees: adds, ignored files, staged
re-links (never displayed twice mid-scan), duplicates, vanish/revival/purge,
the replace cascade, display dimensions, pruning, the guard, symlinks, crash
restarts, and the originals-untouched assertion."""

import hashlib
import os
import time
from pathlib import Path

from conftest import jpeg_bytes, set_mtime_ns


def live_paths(harness):
    rows = (
        harness.db.reader()
        .execute("SELECT path FROM photos WHERE deleted_at IS NULL ORDER BY path")
        .fetchall()
    )
    return [row["path"] for row in rows]


def row_by_path(harness, path):
    return harness.db.reader().execute("SELECT * FROM photos WHERE path=?", (path,)).fetchone()


def test_add_and_ignored_count(harness, library):
    library.add("a.jpg", exif_date="2021:01:01 00:00:00")
    library.add("b.txt", content=b"not a photo")
    library.add("sub/c.jpeg")
    stats = harness.scan()
    assert live_paths(harness) == ["a.jpg", "sub/c.jpeg"]
    assert stats["added"] == 2
    assert stats["ignored"] == 1


def test_exif_date_and_mtime_fallback(harness, library):
    library.add("exif.jpg", exif_date="2021:07:03 10:20:30")
    library.add("plain.jpg")
    set_mtime_ns(library.root / "plain.jpg", 1_600_000_000_000_000_000)  # 2020-09-13 UTC
    harness.scan()
    exif_row = row_by_path(harness, "exif.jpg")
    plain_row = row_by_path(harness, "plain.jpg")
    assert exif_row["date_taken"] == "2021-07-03T10:20:30"
    assert exif_row["date_source"] == "exif"
    # Europe/Berlin in September 2020 is UTC+2: local wall-clock, not UTC
    assert plain_row["date_taken"] == "2020-09-13T14:26:40"
    assert plain_row["date_source"] == "mtime"


def test_unknown_date_sentinel(harness, library):
    library.add("weird.jpg")
    set_mtime_ns(library.root / "weird.jpg", 0)  # st_mtime <= 0 → unknown
    harness.scan()
    row = row_by_path(harness, "weird.jpg")
    assert row["date_taken"] == "0001-01-01T00:00:00"
    assert row["date_source"] == "unknown"


def test_portrait_dimensions_swapped_for_orientation(harness, library):
    library.add("portrait.jpg", width=480, height=640, orientation=6)
    library.add("landscape.jpg", width=640, height=480)
    harness.scan()
    assert (
        row_by_path(harness, "portrait.jpg")["width"],
        row_by_path(harness, "portrait.jpg")["height"],
    ) == (640, 480)
    assert (
        row_by_path(harness, "landscape.jpg")["width"],
        row_by_path(harness, "landscape.jpg")["height"],
    ) == (640, 480)


def test_rename_relinks_without_double_display(harness, library):
    content = jpeg_bytes(exif_date="2021:01:01 00:00:00")
    library.add("old/trip.jpg", content=content)
    harness.scan()
    old_id = row_by_path(harness, "old/trip.jpg")["photo_id"]
    # same-pass move: the old path vanishes and the new path appears before scan
    (library.root / "new").mkdir()
    os.rename(library.root / "old/trip.jpg", library.root / "new/trip.jpg")
    stats = harness.scan()
    assert live_paths(harness) == ["new/trip.jpg"]
    assert row_by_path(harness, "new/trip.jpg")["photo_id"] == old_id  # metadata survives
    assert stats["relinked"] == 1
    assert stats["added"] == 0  # no transient duplicate, no wasted jobs


def test_duplicate_at_second_path_inserts_after_pass(harness, library):
    content = jpeg_bytes()
    library.add("one.jpg", content=content)
    harness.scan()
    library.add("two.jpg", content=content)
    harness.scan()
    assert live_paths(harness) == ["one.jpg", "two.jpg"]  # duplicates display twice (O4)
    ids = {row_by_path(harness, p)["photo_id"] for p in ("one.jpg", "two.jpg")}
    assert len(ids) == 2


def test_vanish_tombstones_and_masks(harness, library):
    library.add("gone.jpg")
    harness.scan()
    photo_id = row_by_path(harness, "gone.jpg")["photo_id"]
    library.delete("gone.jpg")
    harness.scan()
    row = harness.db.reader().execute("SELECT * FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
    assert row["deleted_at"] is not None
    assert live_paths(harness) == []


def test_unreadable_file_keeps_record_listed(harness, library, monkeypatch):
    library.add("locked.jpg")
    harness.scan()
    # force a re-read by touching the file, then make the read fail
    set_mtime_ns(library.root / "locked.jpg", library.mtime("locked.jpg") + 1_000_000_000)

    real_read = Path.read_bytes

    def failing_read(self):
        if self.name == "locked.jpg":
            raise PermissionError("locked")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    harness.scan()
    row = row_by_path(harness, "locked.jpg")
    assert row is not None and row["deleted_at"] is None  # stays listed
    assert row["exif_error"] is not None
    monkeypatch.undo()
    harness.scan()
    row = row_by_path(harness, "locked.jpg")
    assert row is not None and row["deleted_at"] is None
    assert row["exif_error"] is None


def test_revival_within_purge_window(harness, library):
    content = jpeg_bytes(exif_date="2021:01:01 00:00:00")
    library.add("here.jpg", content=content)
    harness.scan()
    photo_id = row_by_path(harness, "here.jpg")["photo_id"]
    library.delete("here.jpg")
    harness.scan()
    library.add("back-elsewhere.jpg", content=content)
    stats = harness.scan()
    row = row_by_path(harness, "back-elsewhere.jpg")
    assert row["photo_id"] == photo_id  # revival keeps photo_id, rewrites path
    assert row["deleted_at"] is None
    assert stats["revived"] == 1


def test_purge_after_window_removes_record_and_shared_cache_guard(harness, library):
    content = jpeg_bytes()
    content_hash = hashlib.blake2b(content, digest_size=32).hexdigest()
    library.add("a.jpg", content=content)
    library.add("b.jpg", content=content)  # duplicate shares the hash
    harness.scan()
    harness.drain()
    shared_cache = harness.settings.cache_dir / "thumb" / content_hash[:2] / f"{content_hash}.sq.webp"
    assert shared_cache.exists()
    library.delete("a.jpg")
    harness.scan()  # tombstones a.jpg; b.jpg keeps the content live
    # age the tombstone beyond the 30-day purge window
    with harness.db.writer() as conn:
        conn.execute(
            "UPDATE photos SET deleted_at=? WHERE path='a.jpg'",
            (time.time() - 40 * 86_400,),
        )
    harness.scan()
    assert (
        harness.db.reader().execute("SELECT 1 FROM photos WHERE path='a.jpg'").fetchone() is None
    )  # purged: record, vectors, jobs
    # b.jpg still lives and shares the hash: the cache entry must survive
    assert shared_cache.exists()


def test_in_place_edit_is_replace_with_full_cascade(harness, library):
    library.add("edit.jpg", content=jpeg_bytes(exif_date="2021:01:01 00:00:00"))
    harness.scan()
    old_row = row_by_path(harness, "edit.jpg")
    harness.drain()
    old_hash = old_row["content_hash"]
    # edit in place: same path, new content — parent dir mtime untouched
    set_mtime_ns(library.root / "edit.jpg", library.mtime("edit.jpg") + 5_000_000)
    (library.root / "edit.jpg").write_bytes(jpeg_bytes(exif_date="2022:02:02 00:00:00"))
    stats = harness.scan()
    new_row = row_by_path(harness, "edit.jpg")
    # a replace is a hard-delete + insert (SQLite may reuse the rowid, so the
    # identity check is on content and per-stage state, not on photo_id)
    assert new_row["content_hash"] != old_hash
    assert new_row["date_taken"] == "2022-02-02T00:00:00"
    assert stats["replaced"] == 1
    assert stats["added"] == 0
    # cascade: old vectors gone (none yet), old jobs dropped, old cache removed
    old_cache = harness.settings.cache_dir / "thumb" / old_hash[:2] / f"{old_hash}.sq.webp"
    assert not old_cache.exists()
    # and a fresh thumb job exists for the new content
    job = (
        harness.db.reader()
        .execute(
            "SELECT 1 FROM jobs WHERE photo_id=? AND kind='thumb' AND state='pending'",
            (new_row["photo_id"],),
        )
        .fetchone()
    )
    assert job is not None


def test_replace_keeps_shared_cache_when_duplicate_lives(harness, library):
    content = jpeg_bytes()
    content_hash = hashlib.blake2b(content, digest_size=32).hexdigest()
    library.add("dup.jpg", content=content)
    library.add("orig.jpg", content=content)
    harness.scan()
    harness.drain()
    shared = harness.settings.cache_dir / "thumb" / content_hash[:2] / f"{content_hash}.sq.webp"
    # edit orig.jpg in place: its old hash is still live at dup.jpg
    set_mtime_ns(library.root / "orig.jpg", library.mtime("orig.jpg") + 5_000_000)
    (library.root / "orig.jpg").write_bytes(jpeg_bytes(width=32, height=32))
    harness.scan()
    assert shared.exists()  # no live-record-shares check failed


def test_nested_add_discovered_under_stat_pruning(harness, library):
    library.add("top.jpg")
    harness.scan(mode="pruned")
    # a new file inside an *unchanged* directory is the daily sweep's job;
    # a new directory changes its parent's mtime, so pruning finds it
    library.add("newdir/deep.jpg")
    harness.scan(mode="pruned")
    assert "newdir/deep.jpg" in live_paths(harness)


def test_pruned_unchanged_directory_not_restat_ed(harness, library):
    library.add("top.jpg")
    library.add("stable/sub/a.jpg")
    first = harness.scan(mode="pruned")
    assert first["scanned"] == 2
    second = harness.scan(mode="pruned")  # unchanged directory mtimes prune the subtree
    assert second["scanned"] == 1  # only the root's file is stat-ed again


def test_guard_trips_on_mass_vanish_and_requires_resume(make_harness, library):
    harness = make_harness(**{"scanner.guard_threshold": 10})
    paths_20 = [f"batch{i:02d}.jpg" for i in range(20)]
    for i, rel in enumerate(paths_20):
        library.add(rel, content=jpeg_bytes(width=8 + i, height=8))
    harness.scan()  # pass one establishes the baseline (armed from pass two)
    for rel in paths_20:
        library.delete(rel)
    harness.scan()
    assert harness.scanner.guard_state()["tripped"] is True
    # nothing was tombstoned: the guard halted the writes
    assert live_paths(harness) == paths_20
    # while latched, a further pass still refuses
    harness.scan()
    assert harness.scanner.guard_state()["tripped"] is True
    assert live_paths(harness) == paths_20
    # resume acknowledges, but the next pass re-evaluates before re-arming:
    # the mass-vanish condition still holds, so it halts again
    assert harness.client.post("/api/admin/guard/resume").status_code == 200
    harness.scan()
    assert harness.scanner.guard_state()["tripped"] is True
    assert live_paths(harness) == paths_20
    # with the files restored and the guard resumed, processing proceeds
    for i, rel in enumerate(paths_20):
        library.add(rel, content=jpeg_bytes(width=8 + i, height=8))
    assert harness.client.post("/api/admin/guard/resume").status_code == 200
    harness.scan()
    assert harness.scanner.guard_state()["tripped"] is False
    assert sorted(live_paths(harness)) == sorted(paths_20)


def test_guard_never_trips_on_replace(harness, library):
    library.add("one.jpg")
    harness.scan()  # baseline
    set_mtime_ns(library.root / "one.jpg", library.mtime("one.jpg") + 5_000_000)
    (library.root / "one.jpg").write_bytes(jpeg_bytes(width=16, height=16))
    harness.scan()
    assert harness.scanner.guard_state()["tripped"] is False


def test_symlinks_skipped(harness, library):
    library.add("real.jpg")
    (library.root / "linked.jpg").symlink_to(library.root / "real.jpg")
    harness.scan()
    assert live_paths(harness) == ["real.jpg"]


def test_interrupted_pass_restarts_without_false_vanishes(harness, library):
    for i in range(10):
        library.add(f"keep{i}.jpg")
    harness.scan()
    before = live_paths(harness)
    # simulate a crash mid-phase-one: an exception during the walk
    original = harness.scanner._scan_dir

    def crashing(dirpath, rel_dir, mode, seen, stats):
        if rel_dir == "half":
            raise RuntimeError("simulated crash")
        return original(dirpath, rel_dir, mode, seen, stats)

    library.add("half/x.jpg")
    harness.scanner._scan_dir = crashing
    try:
        harness.scan()
    except RuntimeError:
        pass
    # no tombstones were written, no guard tripped, nothing lost
    assert harness.scanner.guard_state().get("tripped") is not True
    assert set(before) <= set(live_paths(harness))
    # the restarted pass completes cleanly (phase one is idempotent + pruned)
    harness.scanner._scan_dir = original
    harness.scan()
    assert "half/x.jpg" in live_paths(harness)
    assert set(before) <= set(live_paths(harness))


def test_originals_untouched_after_full_pass(harness, library):
    library.add("keep1.jpg", exif_date="2020:01:01 00:00:00")
    library.add("sub/keep2.jpg")
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_atime_ns) for p in library.root.rglob("*.jpg")
    }
    harness.scan()
    harness.drain()
    for path, (content, mtime, _atime) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == mtime
