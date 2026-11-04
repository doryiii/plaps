"""The background scanner thread: every identity rule in the design.

Each pass runs in two phases. Phase one walks the library (stat-pruned by
default) and inserts immediately only when a file's content hash matches no live
row; a hash matching a live row stages in scan_staged until phase two resolves
vanishes — so a same-pass move never collides with UNIQUE(path), never displays
twice, and never re-enqueues work. Phase two resolves vanishes (re-link staged
moves, tombstone the rest under the mass-delete guard) and purges expired
tombstones. Revival happens in phase one: a walked file whose hash matches a
tombstone revives it (keep photo_id, rewrite path).

An interrupted pass restarts rather than resuming mid-phase: phase one is
idempotent and stat-pruned, and phase two never runs against a partial seen set,
so a crash can never masquerade as a mass vanish and trip the guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from plaps import config, exif, paths

ALLOWED_EXTENSIONS = {".jpg", ".jpeg"}
SENTINEL_DATE = "0001-01-01T00:00:00"
SELF_COMMIT = "SELF_COMMIT"  # op marker: the operation already committed itself
MODES = ("rehash", "sweep", "pruned")  # precedence when more than one interval is due
MODE_META_KEYS = {
    "rehash": "scan_rehash_run",
    "sweep": "scan_sweep_run",
    "pruned": "scan_pruned_run",
}


def now() -> float:
    return time.time()


class Scanner:
    """One scanner thread; run_pass is synchronous so tests drive it directly."""

    def __init__(self, settings: config.Settings, db, cache=None, index=None):
        self.settings = settings
        self.db = db
        self.cache = cache  # ImageCache, for purge/replace cache cleanup
        self.index = index  # VectorIndex (search increment); masking is a no-op without it
        self._stop = threading.Event()
        self._idle_cycles = 0
        self._pass_id = 0

    # -- thread loop -------------------------------------------------------

    def run_forever(self) -> None:
        poll = self.settings.scanner.poll_interval
        while not self._stop.is_set():
            changed = self.run_pass()
            self._idle_cycles = 0 if changed else self._idle_cycles + 1
            self.db.set_meta("scanner_idle", "1" if self._idle_cycles >= 2 else "0")
            self._stop.wait(min(poll * 2**self._idle_cycles, 600))

    def stop(self) -> None:
        self._stop.set()

    # -- pass structure ----------------------------------------------------

    def _select_mode(self) -> str:
        intervals = {
            "rehash": self.settings.scanner.rehash_interval,
            "sweep": self.settings.scanner.full_sweep_interval,
            "pruned": self.settings.scanner.poll_interval,
        }
        for mode in MODES:
            last = self.db.get_meta(MODE_META_KEYS[mode])
            if last is None or now() - float(last) >= intervals[mode]:
                return mode
        return "pruned"

    def run_pass(self, mode: str | None = None) -> bool:
        """One two-phase pass. Returns True when anything changed.

        The mode is selected by due-ness unless a test pins one explicitly."""
        mode = mode or self._select_mode()
        self._pass_id += 1
        self.db.set_meta(MODE_META_KEYS[mode], str(now()))
        stats = {
            "mode": mode,
            "added": 0,
            "relinked": 0,
            "revived": 0,
            "replaced": 0,
            "vanished": 0,
            "staged": 0,
            "ignored": 0,
            "scanned": 0,
        }
        self._checkpoint("phase1", stats)
        seen, walked, changed = self._phase_one(mode, stats)
        self._checkpoint("phase2", stats)
        changed |= self._phase_two(seen, walked, stats)
        self.record_baseline()
        self.db.set_meta("scan_stats", json.dumps(stats))
        self.db.set_meta("scan_last_pass", str(now()))
        if changed:
            # The change stamp read-derivations (folder tree, search candidate
            # sets) memoize on: bumped only when a pass actually changed
            # something, so an idle poll loop never churns their caches.
            self.db.set_meta("scan_change", str(int(self.db.get_meta("scan_change", "0") or 0) + 1))
        return changed

    def _checkpoint(self, phase: str, stats: dict) -> None:
        self.db.set_meta(
            "scan_checkpoint", json.dumps(dict(stats, phase=phase, ts=now(), pass_id=self._pass_id))
        )

    # -- phase one ---------------------------------------------------------

    def _phase_one(self, mode: str, stats: dict) -> tuple[set, set, bool]:
        """Walk the library. Returns (seen paths, walked dirs, changed).

        `walked` holds only the directories actually scanned this pass (root
        included); unchanged-mtime directories pruned from the walk are tracked
        separately as intact and never feed the phase-two vanish check — their
        files were not stat-ed, so absence from `seen` means nothing there.
        """
        root = self.settings.library_root
        seen: set[str] = set()
        walked: set[str] = set()
        intact: set[str] = set()
        changed = False
        if not (root.is_dir() and os.access(root, os.R_OK | os.X_OK)):
            # An unstat-able root is a guard condition, not a mass vanish:
            # halt the pass without touching any record.
            self._trip_guard("library root is not readable", 0)
            return seen, walked, False

        dir_mtimes = self._load_dirs()
        for dirpath in self._walk_dirs(root, mode, dir_mtimes, walked, intact):
            rel_dir = paths.relative_to_library(root, dirpath)
            walked.add(rel_dir)  # scanned this pass: its files are in `seen`
            changed |= self._scan_dir(dirpath, rel_dir, mode, seen, stats)
            mtime_ns = dirpath.stat().st_mtime_ns
            dir_mtimes[rel_dir] = mtime_ns
            self._record_dir(rel_dir, mtime_ns)  # recorded only after its files
            self._checkpoint("phase1", stats)
        self._drop_vanished_dirs(walked | intact)
        return seen, walked, changed

    def _load_dirs(self) -> dict[str, int]:
        rows = self.db.reader().execute("SELECT path, mtime FROM dirs").fetchall()
        return {row["path"]: row["mtime"] for row in rows}

    def _walk_dirs(self, root: Path, mode: str, dir_mtimes: dict[str, int], walked: set, intact: set):
        """Yield directories to walk; unchanged-mtime dirs are pruned, not yielded,
        and land in `intact` — they are assumed present but their files are not
        vanish-checked this pass."""
        walked.add("")
        yield root
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                child = Path(entry.path)
                rel = paths.relative_to_library(root, child)
                try:
                    unchanged = mode == "pruned" and dir_mtimes.get(rel) == child.stat().st_mtime_ns
                except OSError:
                    unchanged = False
                if unchanged:
                    intact.add(rel)  # intact: its files are neither stat-ed nor vanish-checked
                    continue
                yield child
                stack.append(child)

    def _scan_dir(self, dirpath: Path, rel_dir: str, mode: str, seen: set, stats: dict) -> bool:
        """Scan one directory's file dispositions, committing once at the end."""
        root = self.settings.library_root
        changed = False
        ops = []
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError:
            return False
        for entry in entries:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if Path(entry.name).suffix.lower() not in ALLOWED_EXTENSIONS:
                stats["ignored"] += 1
                continue
            rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            stats["scanned"] += 1
            op = self._process_file(root / rel_path, rel_path, stat, mode, seen, stats)
            if op is not None:
                if op[0] == SELF_COMMIT:  # committed inside (revival, replace)
                    changed = True
                else:
                    ops.append(op)
                    changed = True
        if ops:
            with self.db.writer() as conn:
                conn.execute("BEGIN")
                try:
                    for sql, params in ops:
                        cursor = conn.execute(sql, params)
                        if sql.startswith("INSERT INTO photos"):
                            self._enqueue_jobs(conn, cursor.lastrowid)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return changed

    def _process_file(
        self, abs_path: Path, rel_path: str, stat: os.stat_result, mode: str, seen: set, stats: dict
    ):
        """Disposition of one file: a batched writer-op tuple, None, or a
        self-committing operation (replace, revival)."""
        reader = self.db.reader()
        row = reader.execute("SELECT * FROM photos WHERE path=?", (rel_path,)).fetchone()
        if row is not None and row["deleted_at"] is None:
            unchanged = row["size"] == stat.st_size and row["mtime"] == stat.st_mtime_ns
            if mode != "rehash" and unchanged:
                seen.add(rel_path)
                return None  # no read, no hash — the record already carries identity
        try:
            data = abs_path.read_bytes()  # the one disk read
        except OSError as exc:
            if row is not None and row["deleted_at"] is None:
                seen.add(rel_path)  # vanished between scans: record stays listed
                return ("UPDATE photos SET exif_error=? WHERE photo_id=?", (str(exc), row["photo_id"]))
            return None
        content_hash = hashlib.blake2b(data, digest_size=32).hexdigest()
        taken, exif_err = exif.date_taken(data)
        width, height = exif.dimensions(data)
        if exif.orientation(data) in (5, 6, 7, 8):
            width, height = height, width  # store post-rotation display dimensions
        if taken is not None:
            date_taken, date_source = taken.strftime("%Y-%m-%dT%H:%M:%S"), "exif"
        else:
            date_taken, date_source = self._mtime_fallback(stat)
        seen.add(rel_path)

        if row is not None and row["deleted_at"] is None:
            if content_hash == row["content_hash"]:
                # same content, new stat (a touch, or a re-hash confirming it)
                return (
                    "UPDATE photos SET size=?, mtime=?, exif_error=? WHERE photo_id=?",
                    (stat.st_size, stat.st_mtime_ns, exif_err, row["photo_id"]),
                )
            self._replace(row, content_hash, stat, date_taken, date_source, width, height, stats)
            return (SELF_COMMIT, ())
        return self._disposition_new(
            rel_path,
            content_hash,
            stat,
            date_taken,
            date_source,
            width,
            height,
            exif_err,
            stats,
        )

    def _mtime_fallback(self, stat: os.stat_result) -> tuple[str, str]:
        if stat.st_mtime <= 0:
            return SENTINEL_DATE, "unknown"
        local = datetime.fromtimestamp(stat.st_mtime, tz=self.settings.timezone)
        return local.strftime("%Y-%m-%dT%H:%M:%S"), "mtime"

    # -- dispositions ------------------------------------------------------

    @staticmethod
    def _enqueue_jobs(conn, photo_id: int, kinds=("thumb", "embed")) -> None:
        stamp = now()
        for kind in kinds:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(photo_id, kind, state, attempts, "
                "next_attempt_at, created_at) VALUES(?, ?, 'pending', 0, ?, ?)",
                (photo_id, kind, stamp, stamp),
            )

    def _disposition_new(
        self, rel_path, content_hash, stat, date_taken, date_source, width, height, exif_err, stats
    ):
        """No live row at this path: revive a matching tombstone, stage against a
        live twin, or insert new."""
        reader = self.db.reader()
        tombstone = reader.execute(
            "SELECT photo_id FROM photos WHERE content_hash=? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC, photo_id DESC LIMIT 1",
            (content_hash,),
        ).fetchone()
        if tombstone is not None:
            with self.db.writer() as conn:
                conn.execute(
                    "UPDATE photos SET path=?, size=?, mtime=?, date_taken=?, date_source=?, "
                    "width=?, height=?, exif_error=?, deleted_at=NULL WHERE photo_id=?",
                    (
                        rel_path,
                        stat.st_size,
                        stat.st_mtime_ns,
                        date_taken,
                        date_source,
                        width,
                        height,
                        exif_err,
                        tombstone["photo_id"],
                    ),
                )
                self._enqueue_jobs(conn, tombstone["photo_id"])
            self._mask_index(tombstone["photo_id"], unmask=True)
            stats["revived"] += 1
            return (SELF_COMMIT, ())
        live_twin = reader.execute(
            "SELECT 1 FROM photos WHERE content_hash=? AND deleted_at IS NULL LIMIT 1",
            (content_hash,),
        ).fetchone()
        if live_twin is not None:
            # same-pass rename/move, or a duplicate: stage until phase two decides
            stats["staged"] += 1
            return (
                "INSERT OR REPLACE INTO scan_staged(path, size, mtime, content_hash, "
                "date_taken, date_source, width, height) VALUES(?,?,?,?,?,?,?,?)",
                (
                    rel_path,
                    stat.st_size,
                    stat.st_mtime_ns,
                    content_hash,
                    date_taken,
                    date_source,
                    width,
                    height,
                ),
            )
        stats["added"] += 1
        return (
            "INSERT INTO photos(path, content_hash, size, mtime, date_taken, date_source, "
            "width, height, exif_error) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rel_path,
                content_hash,
                stat.st_size,
                stat.st_mtime_ns,
                date_taken,
                date_source,
                width,
                height,
                exif_err,
            ),
        )

    def _replace(self, row, content_hash, stat, date_taken, date_source, width, height, stats) -> None:
        """Content changed at the same path: hard-delete, cascade, insert new."""
        with self.db.writer() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM vectors WHERE photo_id=?", (row["photo_id"],))
                conn.execute("DELETE FROM jobs WHERE photo_id=? AND state='pending'", (row["photo_id"],))
                conn.execute("DELETE FROM photos WHERE photo_id=?", (row["photo_id"],))
                conn.execute(
                    "INSERT INTO photos(path, content_hash, size, mtime, date_taken, "
                    "date_source, width, height) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        row["path"],
                        content_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        date_taken,
                        date_source,
                        width,
                        height,
                    ),
                )
                new_id = conn.execute("SELECT photo_id FROM photos WHERE path=?", (row["path"],)).fetchone()[
                    "photo_id"
                ]
                self._enqueue_jobs(conn, new_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._mask_index(row["photo_id"])
        if self.cache is not None and not self._hash_live_elsewhere(row["content_hash"]):
            self.cache.remove_entries(row["content_hash"])  # only when no live record shares it
        stats["replaced"] += 1

    def _hash_live_elsewhere(self, content_hash: str, exclude_photo_id: int | None = None) -> bool:
        """Some OTHER live record carries this hash (a candidate vanish does not
        count — it is still live while being resolved)."""
        if exclude_photo_id is None:
            row = (
                self.db.reader()
                .execute(
                    "SELECT 1 FROM photos WHERE content_hash=? AND deleted_at IS NULL LIMIT 1",
                    (content_hash,),
                )
                .fetchone()
            )
        else:
            row = (
                self.db.reader()
                .execute(
                    "SELECT 1 FROM photos WHERE content_hash=? AND deleted_at IS NULL "
                    "AND photo_id != ? LIMIT 1",
                    (content_hash, exclude_photo_id),
                )
                .fetchone()
            )
        return row is not None

    # -- phase two ---------------------------------------------------------

    def _phase_two(self, seen: set, walked: set, stats: dict) -> bool:
        reader = self.db.reader()
        live = reader.execute("SELECT * FROM photos WHERE deleted_at IS NULL").fetchall()
        candidates = [
            row
            for row in live
            if row["path"] not in seen and self._dir_walked_or_gone(row["path"], walked)
        ]
        changed = bool(candidates)
        self._resolve_vanishes(candidates, stats)
        changed |= self._insert_remaining_staged(stats)
        self._purge_expired(stats)
        return changed

    def _dir_walked_or_gone(self, photo_path: str, walked: set) -> bool:
        rel_dir = photo_path.rpartition("/")[0]
        if rel_dir in walked:
            return True
        # A directory not walked this pass: still on disk (pruned as intact, or
        # a new sibling) — its photos are assumed present, never vanish-checked.
        return not (self.settings.library_root / rel_dir).is_dir()

    def _resolve_vanishes(self, candidates: list, stats: dict) -> None:
        if not candidates:
            return
        re_links, safe_vanishes, unre_linked = [], [], []
        for row in candidates:
            staged = (
                self.db.reader()
                .execute(
                    "SELECT * FROM scan_staged WHERE content_hash=? ORDER BY path LIMIT 1",
                    (row["content_hash"],),
                )
                .fetchone()
            )
            if staged is not None:
                re_links.append((row, staged))  # same-pass move: keep photo_id
            elif self._hash_live_elsewhere(row["content_hash"], exclude_photo_id=row["photo_id"]):
                safe_vanishes.append(row)  # content survives at a duplicate path
            else:
                unre_linked.append(row)

        # The guard covers vanishes only, is armed from the second pass (the
        # first pass establishes the baseline), and a threshold of 0 disables it.
        tripped = self.guard_state().get("tripped", False)
        if not tripped:
            baseline = int(self.db.get_meta("scan_baseline_live_count", 0))
            threshold = self.settings.scanner.guard_threshold
            if baseline > 0 and threshold > 0 and len(unre_linked) > baseline * threshold / 100:
                self._trip_guard(
                    f"{len(unre_linked)} unre-linked vanishes exceed "
                    f"{threshold:g}% of the previous pass's {baseline} live photos",
                    len(unre_linked),
                )
                tripped = True

        stamp = now()
        tombstoned = []
        with self.db.writer() as conn:
            conn.execute("BEGIN")
            try:
                for row, staged in re_links:
                    # re-links continue even while the guard is tripped
                    conn.execute(
                        "UPDATE photos SET path=?, size=?, mtime=? WHERE photo_id=?",
                        (staged["path"], staged["size"], staged["mtime"], row["photo_id"]),
                    )
                    conn.execute("DELETE FROM scan_staged WHERE path=?", (staged["path"],))
                    stats["relinked"] += 1
                if not tripped:
                    for row in [*safe_vanishes, *unre_linked]:
                        conn.execute(
                            "UPDATE photos SET deleted_at=? WHERE photo_id=?",
                            (stamp, row["photo_id"]),
                        )
                        tombstoned.append(row["photo_id"])
                        stats["vanished"] += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        for photo_id in tombstoned:
            self._mask_index(photo_id)

    def _insert_remaining_staged(self, stats: dict) -> bool:
        """Staged files unclaimed by a re-link: their live twin survived, so they
        are real duplicates — insert with jobs, then clear the staging table."""
        staged = self.db.reader().execute("SELECT * FROM scan_staged ORDER BY path").fetchall()
        if not staged:
            return False
        with self.db.writer() as conn:
            conn.execute("BEGIN")
            try:
                for entry in staged:
                    conn.execute(
                        "INSERT INTO photos(path, content_hash, size, mtime, date_taken, "
                        "date_source, width, height) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            entry["path"],
                            entry["content_hash"],
                            entry["size"],
                            entry["mtime"],
                            entry["date_taken"],
                            entry["date_source"],
                            entry["width"],
                            entry["height"],
                        ),
                    )
                    new_id = conn.execute(
                        "SELECT photo_id FROM photos WHERE path=?", (entry["path"],)
                    ).fetchone()["photo_id"]
                    self._enqueue_jobs(conn, new_id)
                    stats["added"] += 1
                conn.execute("DELETE FROM scan_staged")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return True

    def _purge_expired(self, stats: dict) -> None:
        cutoff = now() - self.settings.purge_after_days * 86_400
        expired = (
            self.db.reader()
            .execute(
                "SELECT * FROM photos WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
            .fetchall()
        )
        if not expired:
            return
        with self.db.writer() as conn:
            conn.execute("BEGIN")
            try:
                for row in expired:
                    conn.execute("DELETE FROM vectors WHERE photo_id=?", (row["photo_id"],))
                    conn.execute("DELETE FROM jobs WHERE photo_id=?", (row["photo_id"],))
                    conn.execute("DELETE FROM photos WHERE photo_id=?", (row["photo_id"],))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if self.cache is not None:
            for row in expired:
                if not self._hash_live_elsewhere(row["content_hash"]):
                    self.cache.remove_entries(row["content_hash"])

    # -- guard -------------------------------------------------------------

    def _trip_guard(self, reason: str, count: int) -> None:
        self.db.set_meta(
            "guard_state", json.dumps({"tripped": True, "reason": reason, "count": count, "ts": now()})
        )

    def guard_state(self) -> dict:
        raw = self.db.get_meta("guard_state")
        if raw is None:
            return {"tripped": False}
        state = json.loads(raw)
        state["tripped"] = bool(state.get("tripped"))
        return state

    def resume_guard(self) -> None:
        self.db.set_meta("guard_state", json.dumps({"tripped": False, "resumed": now()}))

    # -- bookkeeping -------------------------------------------------------

    def _record_dir(self, rel_dir: str, mtime_ns: int) -> None:
        with self.db.writer() as conn:
            conn.execute(
                "INSERT INTO dirs(path, mtime) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime",
                (rel_dir, mtime_ns),
            )

    def _drop_vanished_dirs(self, confirmed: set) -> None:
        rows = self.db.reader().execute("SELECT path FROM dirs").fetchall()
        gone = [row["path"] for row in rows if row["path"] not in confirmed]
        if gone:
            with self.db.writer() as conn:
                conn.executemany("DELETE FROM dirs WHERE path=?", [(p,) for p in gone])

    def _mask_index(self, photo_id: int, unmask: bool = False) -> None:
        if self.index is None:
            return
        self.index.unmask(photo_id) if unmask else self.index.mask(photo_id)

    def record_baseline(self) -> None:
        """Record the live count after a completed pass — the guard's baseline."""
        count = self.db.reader().execute("SELECT COUNT(*) FROM photos WHERE deleted_at IS NULL").fetchone()[0]
        self.db.set_meta("scan_baseline_live_count", str(count))
