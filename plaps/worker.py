"""The background job worker thread — the single consumer of the jobs table.

One queue, one consumer: thumb and embed jobs share the worker, the writer
connection, and the retry machinery. Thumb jobs are popped first, so a slow or
backoff-idle embed queue never delays eager thumbnailing. Successful jobs are
deleted — the per-stage columns on photos record completion — and per-file
failures retry at 30 s doubled (capped at 1 h) with `failed_perm` after 5 attempts.
"""

from __future__ import annotations

import threading
import time

from plaps import config


def now() -> float:
    return time.time()


def backoff_delay(attempts: int) -> float:
    """Exponential backoff for per-file failures: base doubling, capped."""
    return min(config.EMBED_RETRY_BASE * 2 ** max(0, attempts - 1), config.EMBED_RETRY_CAP)


class Worker:
    """One worker thread; run_once/drain are synchronous so tests drive them."""

    def __init__(self, db, cache, library_root):
        self.db = db
        self.cache = cache  # ImageCache
        self.library_root = library_root
        self._stop = threading.Event()
        self._embed_backoff_until = 0.0  # the endpoint circuit (search increment)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """Pop and process one due job. Returns False when the queue is idle."""
        job = self._pop()
        if job is None:
            return False
        if job["kind"] == "thumb":
            self._run_thumb(job)
        # 'embed' handling arrives with the search increment
        return True

    def drain(self) -> None:
        """Process every due job until nothing is runnable (tests use this)."""
        while self.run_once():
            pass

    # -- queue -------------------------------------------------------------

    def _pop(self):
        """Oldest due pending job, thumb kind first (via the dequeue index)."""
        reader = self.db.reader()
        return reader.execute(
            "SELECT * FROM jobs WHERE state='pending' AND next_attempt_at <= ? "
            "ORDER BY kind, next_attempt_at, job_id LIMIT 1",
            (now(),),
        ).fetchone()

    def _finish(self, job, error: str | None) -> None:
        """Record the outcome: success deletes the row, failure schedules a retry
        or marks failed_perm after the attempt cap."""
        with self.db.writer() as conn:
            if error is None:
                conn.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))
                return
            attempts = job["attempts"] + 1
            if attempts >= config.EMBED_RETRY_MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE jobs SET state='failed_perm', attempts=?, error_text=? WHERE job_id=?",
                    (attempts, error, job["job_id"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET attempts=?, next_attempt_at=?, error_text=? WHERE job_id=?",
                    (attempts, now() + backoff_delay(attempts), error, job["job_id"]),
                )

    # -- jobs --------------------------------------------------------------

    def _run_thumb(self, job) -> None:
        reader = self.db.reader()
        photo = reader.execute("SELECT * FROM photos WHERE photo_id=?", (job["photo_id"],)).fetchone()
        if photo is None or photo["deleted_at"] is not None:
            self._finish(job, None)  # the photo is gone; nothing to do
            return
        try:
            # the source path comes from the database record, never from the job
            source = self.library_root / photo["path"]
            self.cache.ensure(photo["content_hash"], "thumb", source)
        except Exception as exc:  # per-file failure: corrupt file, unreadable, pyvips error
            with self.db.writer() as conn:
                conn.execute(
                    "UPDATE photos SET thumb_error=? WHERE photo_id=?",
                    (str(exc), photo["photo_id"]),
                )
            self._finish(job, str(exc))
            return
        with self.db.writer() as conn:
            conn.execute(
                "UPDATE photos SET thumbnailed_at=?, thumb_error=NULL WHERE photo_id=?",
                (now(), photo["photo_id"]),
            )
        self._finish(job, None)
