"""Queue order, per-file backoff (30 s doubling, cap 1 h) and failed_perm after 5
attempts for thumb jobs, successful jobs deleted from the table, and pending-job
dedup across re-hash and revival."""

import time

from conftest import jpeg_bytes, set_mtime_ns

from plaps import config
from plaps.worker import backoff_delay


def pending_jobs(harness, kind="thumb"):
    return (
        harness.db.reader()
        .execute(
            "SELECT * FROM jobs WHERE kind=? AND state='pending' ORDER BY job_id",
            (kind,),
        )
        .fetchall()
    )


def test_backoff_schedule():
    assert backoff_delay(1) == 30
    assert backoff_delay(2) == 60
    assert backoff_delay(3) == 120
    assert backoff_delay(4) == 240
    assert backoff_delay(5) == 480
    assert backoff_delay(20) == config.EMBED_RETRY_CAP  # capped at 1 h


def test_successful_jobs_are_deleted(harness, library):
    library.add("ok.jpg")
    harness.scan()
    assert len(pending_jobs(harness)) == 1
    harness.drain()
    assert len(pending_jobs(harness)) == 0
    # nothing lingers as 'done' — the per-stage column records completion
    total = harness.db.reader().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert total == 0
    row = harness.db.reader().execute("SELECT thumbnailed_at FROM photos WHERE path='ok.jpg'").fetchone()
    assert row["thumbnailed_at"] is not None


def test_per_file_failure_backs_off_then_failed_perm(harness, library, monkeypatch):
    library.add("broken.jpg")
    harness.scan()
    job = pending_jobs(harness)[0]

    real_ensure = harness.cache.ensure
    calls = {"n": 0}

    def failing_ensure(content_hash, kind, source):
        calls["n"] += 1
        raise RuntimeError("pyvips cannot read this")

    monkeypatch.setattr(harness.cache, "ensure", failing_ensure)
    harness.drain()
    assert calls["n"] == 1  # one attempt, then the job waits out its backoff
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
    assert job["state"] == "pending"
    assert job["attempts"] == 1
    assert job["next_attempt_at"] > time.time()  # scheduled, not due now

    # make it due immediately and retry until the attempt cap
    for expected_attempts in (2, 3, 4):
        with harness.db.writer() as conn:
            conn.execute("UPDATE jobs SET next_attempt_at=? WHERE job_id=?", (0.0, job["job_id"]))
        harness.drain()
        job = harness.db.reader().execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert job["attempts"] == expected_attempts
    with harness.db.writer() as conn:
        conn.execute("UPDATE jobs SET next_attempt_at=? WHERE job_id=?", (0.0, job["job_id"]))
    harness.drain()
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
    assert job["state"] == "failed_perm"  # after 5 attempts
    assert job["error_text"] is not None
    photo = harness.db.reader().execute("SELECT thumb_error FROM photos WHERE path='broken.jpg'").fetchone()
    assert photo["thumb_error"] is not None
    monkeypatch.setattr(harness.cache, "ensure", real_ensure)


def test_queue_order_oldest_first(harness, library):
    library.add("first.jpg")
    harness.scan()
    library.add("second.jpg", content=jpeg_bytes(width=17, height=17))
    harness.scan()
    jobs = pending_jobs(harness)
    assert [job["photo_id"] for job in jobs] == sorted([job["photo_id"] for job in jobs])
    # oldest-first: the first-scanned photo's job precedes the later one
    first_id = (
        harness.db.reader()
        .execute("SELECT photo_id FROM photos WHERE path='first.jpg'")
        .fetchone()["photo_id"]
    )
    assert jobs[0]["photo_id"] == first_id


def test_dedup_across_rehash_touch(harness, library):
    library.add("touched.jpg")
    harness.scan()
    # a pure mtime touch re-scans the file but must not stack a second job
    set_mtime_ns(library.root / "touched.jpg", library.mtime("touched.jpg") + 1_000_000)
    harness.scan()
    assert len(pending_jobs(harness)) == 1


def test_dedup_across_revival(harness, library):
    content = jpeg_bytes()
    library.add("revive.jpg", content=content)
    harness.scan()
    harness.drain()
    library.delete("revive.jpg")
    harness.scan()
    library.add("back.jpg", content=content)
    harness.scan()  # revival re-enqueues missing jobs — exactly one pending job
    assert len(pending_jobs(harness)) == 1
    harness.drain()
    row = harness.db.reader().execute("SELECT thumbnailed_at FROM photos WHERE path='back.jpg'").fetchone()
    assert row["thumbnailed_at"] is not None


def test_thumb_jobs_not_starved_behind_backoff(harness, library, monkeypatch):
    # the walking skeleton has only thumb jobs; the ordering contract
    # (thumb before embed) is exercised fully by the search increment's tests
    library.add("a.jpg")
    harness.scan()
    harness.drain()
    assert len(pending_jobs(harness)) == 0


def test_job_for_tombstoned_photo_completes_as_noop(harness, library):
    library.add("bye.jpg")
    harness.scan()
    library.delete("bye.jpg")
    harness.scan()  # tombstones while its thumb job is still pending
    harness.drain()
    assert harness.db.reader().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_run_forever_processes_until_stop(harness, library):
    import threading

    library.add("one.jpg")
    harness.scan()
    thread = threading.Thread(target=harness.worker.run_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if harness.db.reader().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0:
            break
        time.sleep(0.05)
    harness.worker.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_endpoint_outage_trips_circuit_and_never_fails_jobs(harness, library):
    from plaps.embed.adapter import EndpointUnreachable

    library.add("outage.jpg")
    harness.scan()
    harness.adapter.fail_images_with = EndpointUnreachable("ai box down")
    harness.drain()

    # thumbnails kept flowing: the thumb job completed
    row = (
        harness.db.reader()
        .execute("SELECT thumbnailed_at, embedded_at FROM photos WHERE path='outage.jpg'")
        .fetchone()
    )
    assert row["thumbnailed_at"] is not None
    # the embed job survived the outage untouched: pending, no attempts charged
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE kind='embed'").fetchone()
    assert job["state"] == "pending"
    assert job["attempts"] == 0
    # and the circuit is open, so run_once has nothing embed-shaped to do
    assert harness.worker._embed_backoff_until > time.time()

    # the box returns: the circuit closes and the queue resumes untouched
    harness.adapter.fail_images_with = None
    harness.worker._embed_backoff_until = 0.0  # wait out the backoff instantly
    harness.drain()
    row = harness.db.reader().execute("SELECT embedded_at FROM photos WHERE path='outage.jpg'").fetchone()
    assert row["embedded_at"] is not None
    assert harness.worker._embed_backoff_until == 0.0  # reset on success
    assert float(harness.db.get_meta("last_embed_success", 0)) > 0


def test_circuit_backoff_doubles_and_caps(harness, monkeypatch):
    clock = 1000.0
    monkeypatch.setattr("plaps.worker.now", lambda: clock)
    delays = []
    for _ in range(9):
        harness.worker._trip_embed_circuit("test trip")
        delays.append(harness.worker._embed_backoff_until - clock)
    # the schedule: 5 s, 10 s, 20 s, 40 s, 80 s, 160 s, then capped at 5 min
    assert delays[:6] == [5, 10, 20, 40, 80, 160]
    assert delays[6:] == [config.EMBED_CIRCUIT_CAP] * 3


def test_wrong_model_is_circuit_not_failed_perm(harness, library):
    from plaps.embed.adapter import ModelMismatch

    library.add("wm.jpg")
    harness.scan()
    harness.adapter.fail_images_with = ModelMismatch("serves the old model")
    for _ in range(10):  # far beyond the per-file attempt cap
        harness.worker.run_once()
        harness.worker._embed_backoff_until = 0.0  # force the next attempt due
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE kind='embed'").fetchone()
    assert job["state"] == "pending"  # an outage never manufactures failed_perm
    assert job["attempts"] == 0
    harness.adapter.fail_images_with = None


def test_rejected_item_gets_per_file_retry_then_failed_perm(harness, library):
    from plaps.embed.adapter import ItemRejected

    library.add("broken.jpg")
    harness.scan()
    harness.adapter.fail_images_with = ItemRejected("endpoint rejected the request")
    harness.drain()  # the batch fails, the serial fallback attributes per item
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE kind='embed'").fetchone()
    assert job["state"] == "pending"
    assert job["attempts"] == 1
    assert harness.worker._embed_backoff_until == 0.0  # per-file, not the circuit

    for expected in (2, 3, 4):
        with harness.db.writer() as conn:
            conn.execute("UPDATE jobs SET next_attempt_at=0 WHERE job_id=?", (job["job_id"],))
        harness.drain()
        job = harness.db.reader().execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert job["attempts"] == expected
    with harness.db.writer() as conn:
        conn.execute("UPDATE jobs SET next_attempt_at=0 WHERE job_id=?", (job["job_id"],))
    harness.drain()
    job = harness.db.reader().execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
    assert job["state"] == "failed_perm"  # after 5 attempts
    photo = harness.db.reader().execute("SELECT embed_error FROM photos WHERE path='broken.jpg'").fetchone()
    assert photo["embed_error"] is not None


def test_partial_batch_failure_attributes_per_item(harness, library):
    from plaps.embed.adapter import ItemRejected

    for i in range(2):
        library.add(f"pair{i}.jpg", content=jpeg_bytes(width=20 + i, height=20))
    harness.scan()
    original = harness.adapter.embed_images

    def embed_images(images):
        if len(images) > 1:
            raise ItemRejected("batch rejected")
        if images == [(harness.settings.library_root / "pair1.jpg").read_bytes()]:
            raise ItemRejected("this one is broken")
        return original(images)

    harness.adapter.embed_images = embed_images
    harness.drain()
    ok_row = harness.db.reader().execute("SELECT embedded_at FROM photos WHERE path='pair0.jpg'").fetchone()
    bad_job = (
        harness.db.reader()
        .execute(
            "SELECT * FROM jobs j JOIN photos p ON p.photo_id = j.photo_id "
            "WHERE p.path='pair1.jpg' AND j.kind='embed'"
        )
        .fetchone()
    )
    assert ok_row["embedded_at"] is not None  # the good item was written
    assert bad_job["attempts"] == 1  # the bad item was attributed, alone
    harness.adapter.embed_images = original


def test_startup_reembed_queue_does_not_stack(make_harness, library):
    from plaps.app import startup_model_check

    library.add("once.jpg")
    harness = make_harness()
    harness.scan()
    harness.db.set_meta("active_model_id", "old-model")
    for _ in range(3):  # restarts with a changed model: never stack duplicates
        startup_model_check(harness.db, harness.settings)
    pending = (
        harness.db.reader()
        .execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'")
        .fetchone()[0]
    )
    assert pending == 1  # deduplicated by the pending-jobs unique index
