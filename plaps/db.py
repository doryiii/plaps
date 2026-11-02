"""SQLite access layer: the entire concurrency story in one module.

WAL mode; one locked writer connection shared by the scanner, the job worker,
and admin actions; per-thread reader connections. The writer lock is never held
across disk reads, hashing, pyvips work, or HTTP calls — callers commit in small
batches. No other module opens a connection, so no connection ever carries the
wrong pragmas.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# Ordered one-way migration steps, gated by PRAGMA user_version.
SCHEMA_STEPS = [
    """
    CREATE TABLE photos(
      photo_id      INTEGER PRIMARY KEY,
      path          TEXT UNIQUE NOT NULL,
      content_hash  TEXT,
      size          INTEGER,
      mtime         INTEGER,
      date_taken    TEXT NOT NULL,
      date_source   TEXT,
      width         INTEGER,
      height        INTEGER,
      thumbnailed_at REAL,
      embedded_at   REAL,
      deleted_at    REAL,
      exif_error    TEXT,
      thumb_error   TEXT,
      embed_error   TEXT
    )
    """,
    "CREATE INDEX idx_photos_date ON photos(date_taken, photo_id)",
    "CREATE INDEX idx_photos_hash ON photos(content_hash)",
    "CREATE TABLE dirs(path TEXT PRIMARY KEY, mtime INTEGER NOT NULL)",
    """
    CREATE TABLE scan_staged(
      path         TEXT PRIMARY KEY,
      size         INTEGER,
      mtime        INTEGER,
      content_hash TEXT,
      date_taken   TEXT NOT NULL,
      date_source  TEXT,
      width        INTEGER,
      height       INTEGER
    )
    """,
    """
    CREATE TABLE vectors(
      photo_id  INTEGER NOT NULL,
      model_id  TEXT NOT NULL,
      dim       INTEGER NOT NULL,
      vector    BLOB NOT NULL,
      PRIMARY KEY (photo_id, model_id)
    )
    """,
    """
    CREATE TABLE jobs(
      job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
      photo_id        INTEGER NOT NULL,
      kind            TEXT NOT NULL,
      state           TEXT NOT NULL,
      attempts        INTEGER NOT NULL DEFAULT 0,
      next_attempt_at REAL NOT NULL,
      error_text      TEXT,
      created_at      REAL NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX idx_jobs_pending_unique ON jobs(photo_id, kind) WHERE state='pending'",
    "CREATE INDEX idx_jobs_dequeue ON jobs(kind, next_attempt_at, job_id) WHERE state='pending'",
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)",
]

BUSY_TIMEOUT_MS = 5_000


class Database:
    """One writer (locked), per-thread readers (WAL), versioned migrations."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._writer_lock = threading.RLock()
        self._writer = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._writer.isolation_level = None  # autocommit; explicit transactions
        self._writer.row_factory = sqlite3.Row
        self._writer.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # -- connections -------------------------------------------------------

    def reader(self) -> sqlite3.Connection:
        """Per-thread reader connection in WAL mode."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.isolation_level = None
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def writer(self):
        """The single writer connection, serialized by its lock.

        Callers must not perform disk reads, hashing, image work, or HTTP calls
        while holding it — commit in small batches instead.
        """
        with self._writer_lock:
            yield self._writer

    def close(self) -> None:
        with self._writer_lock:
            self._writer.close()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- migrations --------------------------------------------------------

    def _migrate(self) -> None:
        with self._writer_lock:
            current = self._writer.execute("PRAGMA user_version").fetchone()[0]
            for version, step in enumerate(SCHEMA_STEPS, start=1):
                if version <= current:
                    continue
                self._writer.execute("BEGIN")
                try:
                    self._writer.execute(step)
                    self._writer.execute(f"PRAGMA user_version={version}")
                    self._writer.execute("COMMIT")
                except Exception:
                    self._writer.execute("ROLLBACK")
                    raise

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default=None):
        row = self.reader().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_meta(self, key: str, value) -> None:
        with self.writer() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # -- backup ------------------------------------------------------------

    def backup(self, backup_dir: Path, keep: int = 8) -> Path:
        """A consistent snapshot via VACUUM INTO — no writer pause, no IPC.

        VACUUM INTO errors when the target exists, so every run writes a fresh
        timestamped file; older snapshots beyond `keep` are pruned.
        """
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"plaps-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        with self._writer_lock:
            self._writer.execute("VACUUM INTO ?", (str(target),))
        snapshots = sorted(backup_dir.glob("plaps-*.db"))
        for old in snapshots[:-keep] if len(snapshots) > keep else []:
            old.unlink(missing_ok=True)
        return target
