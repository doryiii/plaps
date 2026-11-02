"""The background job worker thread — the single consumer of the jobs table.

One queue, one consumer: thumb and embed jobs share the worker, the writer
connection, and the retry machinery. Thumb jobs are popped first, so a slow or
backoff-idle embed queue never delays eager thumbnailing. Successful jobs are
deleted — the per-stage columns on photos record completion — and per-file
failures retry at 30 s doubled (capped at 1 h) with `failed_perm` after 5
attempts.

Endpoint-level failures never touch per-job attempts: an unreachable endpoint,
or one serving a model other than the configured one, trips a worker-level
circuit backoff (5 s doubled, capped at 5 min, cleared by the next success)
with the popped jobs returned to `pending` untouched — an AI-box outage never
manufactures `failed_perm` jobs. A failed batch is retried one item per request
to attribute per-item failures, because the endpoint has no item-level signal.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from plaps import config
from plaps.embed.adapter import EndpointUnreachable, ItemRejected, ModelMismatch


def now() -> float:
    return time.time()


def backoff_delay(attempts: int) -> float:
    """Exponential backoff for per-file failures: base doubling, capped."""
    return min(config.EMBED_RETRY_BASE * 2 ** max(0, attempts - 1), config.EMBED_RETRY_CAP)


class Worker:
    """One worker thread; run_once/drain are synchronous so tests drive them."""

    def __init__(self, db, cache, library_root, adapter=None, index=None):
        self.db = db
        self.cache = cache  # ImageCache
        self.library_root = library_root
        self.adapter = adapter
        self.index = index  # VectorIndex; upserts are skipped without it
        self._stop = threading.Event()
        self._embed_backoff_until = 0.0  # the endpoint circuit
        self._embed_circuit_trips = 0

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
        elif job["kind"] == "embed":
            self._run_embed(job)
        return True

    def drain(self) -> None:
        """Process every due job until nothing is runnable (tests use this)."""
        while self.run_once():
            pass

    # -- queue -------------------------------------------------------------

    def _pop(self):
        """Oldest due pending job, thumb kind first (via the dequeue index).

        While the endpoint circuit is open, only thumb jobs are popped — the
        pending embed jobs wait out the backoff untouched.
        """
        reader = self.db.reader()
        if self._embed_backoff_until > now():
            return reader.execute(
                "SELECT * FROM jobs WHERE state='pending' AND kind='thumb' "
                "AND next_attempt_at <= ? ORDER BY next_attempt_at, job_id LIMIT 1",
                (now(),),
            ).fetchone()
        return reader.execute(
            "SELECT * FROM jobs WHERE state='pending' AND next_attempt_at <= ? "
            "ORDER BY CASE kind WHEN 'thumb' THEN 0 ELSE 1 END, "
            "next_attempt_at, job_id LIMIT 1",
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

    # -- endpoint circuit ----------------------------------------------------

    def _trip_embed_circuit(self, reason: str) -> None:
        self._embed_circuit_trips += 1
        delay = min(
            config.EMBED_CIRCUIT_BASE * 2 ** (self._embed_circuit_trips - 1),
            config.EMBED_CIRCUIT_CAP,
        )
        self._embed_backoff_until = now() + delay
        self.db.set_meta("embed_circuit", f"open until {self._embed_backoff_until:.0f}: {reason}")

    def _reset_embed_circuit(self) -> None:
        self._embed_circuit_trips = 0
        self._embed_backoff_until = 0.0
        self.db.set_meta("embed_circuit", "closed")

    # -- jobs ----------------------------------------------------------------

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

    def _run_embed(self, first_job) -> None:
        """Embed one batch: endpoint failures trip the circuit and charge no
        attempts; a rejected batch falls back to serial per-item requests."""
        reader = self.db.reader()
        batch = [
            first_job,
            *reader.execute(
                "SELECT * FROM jobs WHERE state='pending' AND kind='embed' "
                "AND next_attempt_at <= ? AND job_id != ? "
                "ORDER BY next_attempt_at, job_id LIMIT ?",
                (now(), first_job["job_id"], config.EMBED_BATCH_SIZE - 1),
            ).fetchall(),
        ]
        work: list[tuple[dict, dict]] = []  # (job, live photo row)
        for job in batch:
            photo = reader.execute("SELECT * FROM photos WHERE photo_id=?", (job["photo_id"],)).fetchone()
            if photo is None or photo["deleted_at"] is not None:
                self._finish(job, None)  # vanished while queued; nothing to embed
                continue
            work.append((job, photo))

        try:
            vectors = self.adapter.embed_images(
                [(self.library_root / photo["path"]).read_bytes() for _, photo in work]
            )
        except (EndpointUnreachable, ModelMismatch) as exc:
            # endpoint-level: the jobs stay pending, no attempts charged
            self._trip_embed_circuit(str(exc))
            return
        except ItemRejected:
            # the endpoint rejected something in the batch: attribute per item
            self._embed_serially(work)
            return
        except OSError:
            # an unreadable original is a per-file failure for the whole batch;
            # fall through to serial attribution to find which ones
            self._embed_serially(work)
            return

        for (job, photo), vector in zip(work, vectors, strict=True):
            self._write_vector(photo, vector)
            self._finish(job, None)
        self.db.set_meta("last_embed_success", str(now()))
        self._reset_embed_circuit()

    def _embed_serially(self, work: list[tuple[dict, dict]]) -> None:
        """One item per request: successes are written, per-item rejections get
        the per-file retry, and an endpoint failure mid-way stops the batch."""
        for job, photo in work:
            try:
                vector = self.adapter.embed_images([(self.library_root / photo["path"]).read_bytes()])[0]
            except (EndpointUnreachable, ModelMismatch) as exc:
                self._trip_embed_circuit(str(exc))
                return  # the remaining jobs stay pending, untouched
            except (ItemRejected, OSError) as exc:
                with self.db.writer() as conn:
                    conn.execute(
                        "UPDATE photos SET embed_error=? WHERE photo_id=?",
                        (str(exc), photo["photo_id"]),
                    )
                self._finish(job, str(exc))
                continue
            self._write_vector(photo, vector)
            self._finish(job, None)
            self.db.set_meta("last_embed_success", str(now()))
            self._reset_embed_circuit()

    def _write_vector(self, photo, vector) -> None:
        """Persist the vector stamped with the configured model and upsert it
        into the resident index (which always carries the configured model)."""
        array = np.asarray(vector, dtype=np.float32)
        with self.db.writer() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vectors(photo_id, model_id, dim, vector) VALUES(?, ?, ?, ?)",
                (photo["photo_id"], self.adapter.model_id, len(array), array.tobytes()),
            )
            conn.execute(
                "UPDATE photos SET embedded_at=?, embed_error=NULL WHERE photo_id=?",
                (now(), photo["photo_id"]),
            )
        if self.index is not None:
            self.index.add(photo["photo_id"], array)
