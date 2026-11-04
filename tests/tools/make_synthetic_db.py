"""Generate the synthetic database used by the performance smoke.

Fills a fresh database with N photos (default 150,000) spread over a realistic
nested folder tree, each with a plausibly-distributed date_taken and a random
768-dim L2-normalized vector — the shape the resident matrix and the timeline
index see at full scale. No production imports beyond ``plaps.db``; the vectors
are random (ranking order is irrelevant to the latency targets).
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from plaps.db import Database

DIM = 768
BATCH = 5_000


def generate(db_path: Path, count: int, seed: int = 42, model_id: str = "synthetic") -> None:
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)
    db = Database(db_path)
    base = datetime(2005, 1, 1)
    for start in range(0, count, BATCH):
        n = min(BATCH, count - start)
        photos = []
        vectors = []
        for offset in range(n):
            i = start + offset
            # a realistic tree: year/event/ subfolders, ~8 photos per leaf
            event = f"event{i // 8:05d}"
            year = 2005 + (i % 20)
            path = f"{year}/{event}/p{i % 8:03d}.jpg"
            taken = base + timedelta(
                days=pyrng.randrange(20 * 365), seconds=pyrng.randrange(86_400)
            )
            content_hash = hashlib.blake2b(f"{i}".encode(), digest_size=32).hexdigest()
            photos.append(
                (i + 1, path, content_hash, 1_000_000 + i, i,
                 taken.strftime("%Y-%m-%dT%H:%M:%S"), "exif", 1600, 900)
            )
            vector = rng.standard_normal(DIM, dtype=np.float32)
            vector /= np.linalg.norm(vector)
            vectors.append((i + 1, model_id, DIM, vector.tobytes()))
        dirs = sorted(
            {photo[1].rpartition("/")[0] for photo in photos} | {".".join("")}
        )
        with db.writer() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO photos(photo_id, path, content_hash, size, "
                "mtime, date_taken, date_source, width, height) VALUES(?,?,?,?,?,?,?,?,?)",
                photos,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO vectors(photo_id, model_id, dim, vector) "
                "VALUES(?,?,?,?)",
                vectors,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO dirs(path, mtime) VALUES(?, ?)",
                [(d, 0) for d in dirs if d],
            )
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('active_model_id', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (model_id,),
        )
    db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the synthetic 150K-row database")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--count", type=int, default=150_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model-id", default="synthetic")
    args = parser.parse_args(argv)
    if args.db_path.exists():
        print(f"refusing to overwrite {args.db_path}", file=sys.stderr)
        return 1
    generate(args.db_path, args.count, args.seed, args.model_id)
    with sqlite3.connect(args.db_path) as conn:
        photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        vectors = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"{args.db_path}: {photos} photos, {vectors} vectors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
