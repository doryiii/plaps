"""Composition root: one file owns the process's lifecycle so the threading
model is visible in a single place.

Wires config → db → scanner + worker (thumb jobs) → Flask app → waitress. The
app listens on 127.0.0.1:8096 — constants matching the lighttpd sample, not
config. The vector index, the embed pipeline, and model-change detection arrive
with the search increment.
"""

from __future__ import annotations

import logging
import signal

from waitress import serve as waitress_serve

from plaps import config
from plaps.api import create_app
from plaps.db import Database
from plaps.images import ImageCache
from plaps.scanner import Scanner
from plaps.worker import Worker

log = logging.getLogger("plaps")

HOST = "127.0.0.1"
PORT = 8096
THREADS = 16


class Plaps:
    """The whole process: database, background threads, and the WSGI app."""

    def __init__(self, settings: config.Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.cache = ImageCache(self.db, settings.cache_dir)
        self.cache.init_counter()
        self.scanner = Scanner(settings, self.db, cache=self.cache)
        self.worker = Worker(self.db, self.cache, settings.library_root)
        self.app = create_app(self.db, settings, self.cache, self.scanner, self.worker)
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
            self.db.set_meta("shutdown_at", str(__import__("time").time()))
        finally:
            self.db.close()


def build_app(settings: config.Settings) -> Plaps:
    return Plaps(settings)


def serve(settings: config.Settings) -> None:
    plaps = build_app(settings)
    plaps.start_background()

    def handle_signal(signum, frame):
        log.info("signal %s: shutting down", signum)
        plaps.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.info("plaps serving on http://%s:%d", HOST, PORT)
    waitress_serve(plaps.app, host=HOST, port=PORT, threads=THREADS)
