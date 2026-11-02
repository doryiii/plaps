"""Vector index and ranking: brute force over a resident matrix.

The index carries exactly the configured model — a model change flips the
space at the restart, never mid-queue. The load joins ``photos`` so tombstoned
ids start masked (tombstones keep their vectors until purge), ``dim`` comes
from the first vector seen and is never hardcoded, and ``mask``/``unmask``
publish copied boolean arrays so an in-flight reader never observes a
half-updated mask. Ranking is one full-matrix BLAS product; the tombstone mask
and the date/folder filter apply to the 1-D score vector, never as a
fancy-index copy of the matrix.
"""

from __future__ import annotations

import threading

import numpy as np

from plaps import config


class VectorIndex:
    """Resident float32 matrix + parallel id array + tombstone mask."""

    def __init__(self, db, settings, adapter, capacity: int = config.INDEX_CAPACITY):
        self.db = db
        self.settings = settings
        self.adapter = adapter
        self.model_id = settings.embedding.model_id
        self.capacity = capacity
        self._lock = threading.Lock()
        self._dim: int | None = None
        self._matrix: np.ndarray | None = None  # (capacity, dim) float32
        self._ids = np.zeros(capacity, dtype=np.int64)
        self._row_of: dict[int, int] = {}
        self._size = 0
        self._masked = np.zeros(capacity, dtype=bool)
        self.load()

    # -- loading and mutation ----------------------------------------------

    @property
    def size(self) -> int:
        """Number of vectors resident for the configured model."""
        with self._lock:
            return self._size

    def load(self) -> None:
        """All vectors for the configured model; tombstoned ids start masked."""
        rows = (
            self.db.reader()
            .execute(
                "SELECT v.photo_id, v.vector, p.deleted_at IS NOT NULL AS masked "
                "FROM vectors v JOIN photos p ON p.photo_id = v.photo_id "
                "WHERE v.model_id = ?",
                (self.model_id,),
            )
            .fetchall()
        )
        for row in rows:
            vector = np.frombuffer(row["vector"], dtype="<f4")
            self._append(photo_id=row["photo_id"], vector=vector, masked=row["masked"])

    def _ensure_dim(self, vector: np.ndarray) -> None:
        if self._matrix is None:
            self._dim = len(vector)
            self._matrix = np.zeros((self.capacity, self._dim), dtype=np.float32)

    def _append(self, photo_id: int, vector: np.ndarray, masked: bool) -> None:
        with self._lock:
            self._ensure_dim(vector)
            assert self._dim == len(vector), "vector dim changed mid-model"
            if self._size >= self.capacity:
                self._grow()
            row = self._size
            self._ids[row] = photo_id
            self._matrix[row, :] = vector
            self._masked[row] = masked
            self._row_of[photo_id] = row
            self._size += 1

    def _grow(self) -> None:
        """The only reallocation: double capacity (the one transient ~2x)."""
        self.capacity *= 2
        matrix = np.zeros((self.capacity, self._dim), dtype=np.float32)
        matrix[: self._size] = self._matrix[: self._size]
        self._matrix = matrix
        ids = np.zeros(self.capacity, dtype=np.int64)
        ids[: self._size] = self._ids[: self._size]
        self._ids = ids
        masked = np.zeros(self.capacity, dtype=bool)
        masked[: self._size] = self._masked[: self._size]
        self._masked = masked

    def add(self, photo_id: int, vector: np.ndarray) -> None:
        """Upsert: an id may already be present (masked — revival re-uses its
        vector). A fresh id writes into spare capacity; an existing id rewrites
        its row with a practically identical vector, which in-flight readers
        tolerate."""
        with self._lock:
            self._ensure_dim(vector)
            row = self._row_of.get(photo_id)
            if row is None:
                if self._size >= self.capacity:
                    self._grow()
                    row = self._row_of.get(photo_id)
                row = self._size
                self._ids[row] = photo_id
                self._row_of[photo_id] = row
                self._size += 1
            self._matrix[row, :] = vector

    def mask(self, photo_id: int) -> None:
        self._set_mask(photo_id, True)

    def unmask(self, photo_id: int) -> None:
        self._set_mask(photo_id, False)

    def _set_mask(self, photo_id: int, value: bool) -> None:
        with self._lock:
            row = self._row_of.get(photo_id)
            if row is None:
                return  # no vector for this id (yet) — nothing to hide
            # publish a copied array rather than mutating the live one
            masked = self._masked.copy()
            masked[row] = value
            self._masked = masked

    # -- ranking -----------------------------------------------------------

    def _candidate_ids(self, folder: str | None, after: str | None, before: str | None) -> list[int]:
        """Live photos inside the filter — the coverage denominator and the
        ranking candidate set (photos without a current-model vector are
        silently excluded from ranking but still counted in `total`)."""
        clauses = ["deleted_at IS NULL"]
        params: list = []
        if after:
            clauses.append("date_taken >= ?")
            params.append(after)
        if before:
            clauses.append("date_taken < ?")
            params.append(before)
        if folder is not None:
            from plaps.paths import subtree_bounds

            low, high = subtree_bounds(folder)
            clauses += ["path >= ?", "path < ?"]
            params += [low, high]
        rows = (
            self.db.reader()
            .execute("SELECT photo_id FROM photos WHERE " + " AND ".join(clauses), params)
            .fetchall()
        )
        return [row["photo_id"] for row in rows]

    def query(
        self,
        text: str,
        folder: str | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Rank live photos in the filter by cosine similarity to the query.

        Applies the min_score floor with the min_matches top-up and computes
        coverage inside the same filter. Raises EndpointUnreachable /
        ModelMismatch from the adapter — the endpoint reports those, never an
        empty result set.
        """
        query_vector = self.adapter.embed_text(text)  # one round trip
        candidates = self._candidate_ids(folder, after, before)
        total = len(candidates)
        with_vectors = [pid for pid in candidates if pid in self._row_of]
        embedded = len(with_vectors)
        coverage = {"embedded": embedded, "total": total}
        if embedded == 0:
            return {"photos": [], "state": "empty_filter", "coverage": coverage}

        # snapshot under the lock; rows >= size are scratch, so the buffer
        # reference is stable while ranking runs lock-free
        with self._lock:
            size = self._size
            matrix, masked = self._matrix, self._masked
        scores = matrix[:size] @ query_vector  # BLAS sgemv — no fancy-index copy

        eligible = np.zeros(size, dtype=bool)
        for pid in with_vectors:
            eligible[self._row_of[pid]] = True
        eligible &= ~masked[:size]
        rows_in_order = np.nonzero(eligible)[0]
        rows_in_order = rows_in_order[np.argsort(-scores[rows_in_order], kind="stable")]

        min_score = self.settings.embedding.min_score
        min_matches = self.settings.embedding.min_matches
        ranked: list[dict] = []
        floor_reached = False
        for row in rows_in_order:  # descending score: matches first
            if len(ranked) >= limit:
                break
            score = float(scores[row])
            if score >= min_score:
                floor_reached = True
                ranked.append({"photo_id": int(self._ids[row]), "score": score})
            elif len(ranked) < min_matches:
                # top-up: when fewer than min_matches reach the floor, the
                # list fills with the next-closest photos anyway
                ranked.append({"photo_id": int(self._ids[row]), "score": score})
            else:
                break  # everything below the floor is less interesting still
        state = "ok" if floor_reached else "no_matches"
        return {"photos": ranked, "state": state, "coverage": coverage}
