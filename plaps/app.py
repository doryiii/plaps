"""Composition root: one file owns the process's lifecycle so the threading
model is visible in a single place.

Wires config → db → adapter → vector index (loaded before serving) → scanner +
worker → Flask app → waitress. The app listens on 127.0.0.1:8096 — constants
matching the lighttpd sample, not config. Model-change detection happens here
at startup, the single owner: a configured ``embedding.model_id`` differing from
``meta.active_model_id`` queues re-embed jobs for every photo lacking a vector
for the configured model, and stale pending embed jobs (an aborted change) are
pruned so they cannot spend days re-embedding photos with valid vectors.
"""

from __future__ import annotations

import logging
import signal
import time

from waitress import serve as waitress_serve

from plaps import config
from plaps.api import create_app
from plaps.db import Database
from plaps.embed.adapter import EmbeddingAdapter
from plaps.images import ImageCache
from plaps.scanner import Scanner
from plaps.search import VectorIndex
from plaps.worker import Worker

log = logging.getLogger("plaps")

HOST = "127.0.0.1"
PORT = 8096
THREADS = 16


def startup_model_check(db, settings) -> None:
    """Model-change detection — the single owner (config is read once).

    First run: record the configured model as active, no re-embed. A changed
    model: queue re-embeds only for photos lacking a vector for it. Either
    way, prune pending embed jobs whose photo already has one.
    """
    configured = settings.embedding.model_id
    active = db.get_meta("active_model_id")
    if active is None:
        db.set_meta("active_model_id", configured)
        active = configured
    stamp = time.time()
    with db.writer() as conn:
        if active != configured:
            # the flip happened here, at the restart: re-embed the remainder
            conn.execute(
                "INSERT OR IGNORE INTO jobs(photo_id, kind, state, attempts, "
                "next_attempt_at, created_at) "
                "SELECT photo_id, 'embed', 'pending', 0, ?, ? FROM photos p "
                "WHERE p.deleted_at IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM vectors v WHERE v.photo_id = p.photo_id AND v.model_id = ?)",
                (stamp, stamp, configured),
            )
        # an aborted change leaves pending jobs for photos that already have
        # vectors for the configured model — prune them
        conn.execute(
            "DELETE FROM jobs WHERE kind='embed' AND state='pending' AND photo_id IN ("
            "SELECT photo_id FROM vectors WHERE model_id = ?)",
            (configured,),
        )


class Plaps:
    """The whole process: database, background threads, and the WSGI app."""

    def __init__(self, settings: config.Settings, adapter: EmbeddingAdapter | None = None):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.cache = ImageCache(self.db, settings.cache_dir)
        self.cache.init_counter()
        self.adapter = adapter or EmbeddingAdapter(settings.embedding.base_url, settings.embedding.model_id)
        self.index = VectorIndex(self.db, settings, self.adapter)  # loaded before serving
        self.scanner = Scanner(settings, self.db, cache=self.cache, index=self.index)
        self.worker = Worker(
            self.db,
            self.cache,
            settings.library_root,
            adapter=self.adapter,
            index=self.index,
        )
        startup_model_check(self.db, settings)
        self.app = create_app(
            self.db,
            settings,
            self.cache,
            self.scanner,
            self.worker,
            self.adapter,
            self.index,
        )
        self._threads = []

    def start_background(self) -> None:
        import threading

        for target, name in (
            (self.scanner.run_forever, "plaps-scanner"),
            (self.worker.run_forever, "plaps-worker"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self) -> None:
        """Stop accepting work, checkpoint the scanner, close the writer."""
        self.scanner.stop()
        self.worker.stop()
        try:
            self.db.set_meta("shutdown_at", str(time.time()))
        finally:
            self.db.close()


def build_app(settings: config.Settings, adapter: EmbeddingAdapter | None = None) -> Plaps:
    return Plaps(settings, adapter)


def serve(settings: config.Settings, adapter: EmbeddingAdapter | None = None) -> None:
    plaps = build_app(settings, adapter)
    plaps.start_background()

    def handle_signal(signum, frame):
        log.info("signal %s: shutting down", signum)
        plaps.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.info("plaps serving on http://%s:%d", HOST, PORT)
    waitress_serve(plaps.app, host=HOST, port=PORT, threads=THREADS)
