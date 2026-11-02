"""pyvips image derivation: the derived-artifact cache.

Every derived file is keyed by (content_hash, variant) in a two-level shard, so
an edited photo — a new hash — can never hit a stale entry, and keys are
immutable, so responses carry long-lived immutable cache headers. Signatures
take bytes, not paths: generating every variant for a photo costs one disk read.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pyvips

from plaps import config

# The on-disk cache makes libvips's in-memory operation cache redundant, and its
# default retention grows RSS by hundreds of MB in a long-running threaded server.
pyvips.cache_set_max(0)


def cache_path(cache_dir: Path, variant: str, content_hash: str) -> Path:
    """Sharded cache location for a (hash, variant) pair."""
    stem = "large" if variant == "large" else "thumb"
    name = f"{content_hash}.webp" if variant == "large" else f"{content_hash}.{variant}.webp"
    return Path(cache_dir) / stem / content_hash[:2] / name


def _render(data: bytes, content_hash: str, variant: str, cache_dir: Path) -> tuple[Path, Path]:
    """Render one variant to a temp file beside its target. Returns (tmp, target)."""
    size = config.SCREENFIT_SIZE if variant == "large" else config.THUMB_SIZE
    options = {"height": size}
    if variant == "sq":
        options["crop"] = pyvips.enums.Interesting.ATTENTION
    image = pyvips.Image.thumbnail_buffer(data, size, **options)
    target = cache_path(cache_dir, variant, content_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{uuid.uuid4().hex}"
    image.webpsave(str(tmp), Q=config.WEBP_QUALITY)
    return tmp, target


def derive(data: bytes, content_hash: str, variant: str, cache_dir: Path) -> Path:
    """One thumbnail variant from bytes: sq 512x512 (ATTENTION crop) or as <=512 px."""
    tmp, target = _render(data, content_hash, variant, cache_dir)
    os.replace(tmp, target)
    return target


def screenfit(data: bytes, content_hash: str, cache_dir: Path) -> Path:
    """The ~2048 px aspect-preserving screen-fit image, from bytes."""
    tmp, target = _render(data, content_hash, "large", cache_dir)
    os.replace(tmp, target)
    return target


class ImageCache:
    """Cache lookups, generation, size accounting, and cleanup.

    Size accounting is exact: an increment is taken only by the thread that
    actually created the target (the existence check happens under the writer
    lock, via os.link, so two threads racing one miss never double-count),
    cleanup subtracts the st_size of files it really unlinks, and the counter
    resets at startup when the cache directory is empty or missing.
    """

    def __init__(self, db, cache_dir: Path):
        self.db = db
        self.cache_dir = Path(cache_dir)

    # -- accounting --------------------------------------------------------

    def init_counter(self) -> None:
        """Reset the size counter when the cache is empty or missing."""
        if not self.cache_dir.exists() or not any(self.cache_dir.rglob("*")):
            self.db.set_meta("cache_size", 0)

    def size(self) -> int:
        return int(self.db.get_meta("cache_size", 0))

    @staticmethod
    def _add(conn, amount: int) -> None:
        conn.execute("INSERT INTO meta(key, value) VALUES('cache_size', '0') ON CONFLICT(key) DO NOTHING")
        row = conn.execute("SELECT value FROM meta WHERE key='cache_size'").fetchone()
        conn.execute(
            "UPDATE meta SET value=? WHERE key='cache_size'",
            (str(max(0, int(row[0]) + amount)),),
        )

    # -- generation --------------------------------------------------------

    def ensure(self, content_hash: str, kind: str, source_path: Path) -> list[Path]:
        """Generate any missing variants on miss. `kind` is 'thumb' (both
        variants) or 'large'. Returns every target path for the kind."""
        variants = ("sq", "as") if kind == "thumb" else ("large",)
        targets = [cache_path(self.cache_dir, v, content_hash) for v in variants]
        if all(t.exists() for t in targets):
            return targets
        data = source_path.read_bytes()  # one disk read per file, not three
        for variant, target in zip(variants, targets, strict=True):
            if target.exists():
                continue
            tmp, _ = _render(data, content_hash, variant, self.cache_dir)
            with self.db.writer() as conn:
                try:
                    os.link(tmp, target)  # fails if another thread won the race
                except FileExistsError:
                    tmp.unlink(missing_ok=True)
                    continue
                self._add(conn, target.stat().st_size)
            tmp.unlink(missing_ok=True)
        return targets

    # -- cleanup -----------------------------------------------------------

    def remove_entries(self, content_hash: str) -> None:
        """Drop every cache entry for a hash, subtracting what is really unlinked."""
        targets = [cache_path(self.cache_dir, v, content_hash) for v in ("sq", "as", "large")]
        with self.db.writer() as conn:
            freed = 0
            for target in targets:
                try:
                    freed += target.stat().st_size
                    target.unlink()
                except FileNotFoundError:
                    pass
            if freed:
                self._add(conn, -freed)
