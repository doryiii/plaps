# Design Review 1 — Resolution Record

* **Target:** /mnt/data/workspace/plaps/docs/designs/plaps.md
* **Review:** `design_review_1.md` (2026-11-01)
* **Date resolved:** 2026-11-01
* **Disposition:** every finding fixed in the document; one owner decision taken (search relevance thresholds). New decisions are in the design's Decision Register, dated 2026-11-01.

## Owner decision

**Search relevance: `embedding.min_score` + `embedding.min_matches`.** The reviewer proved `"no_matches"` unreachable without a similarity floor and noted a fixed floor is fragile (spike: matches 0.62–0.75, runner-ups 0.53–0.68 overlap any threshold). The owner chose two thresholds: results reaching `min_score` are matches; when fewer than `min_matches` (default 10) reach the floor, the list is topped up with the next-closest photos anyway. `"no_matches"` therefore means "zero photos reached the floor while things were searchable" and still carries the fallback results, which the UI labels as closest matches rather than strong ones. A wrong floor degrades labeling, never visibility. `min_score` defaults to 0.70 and is recorded as model-specific (re-judge at every model change).

## Logic & contract bugs — all fixed

* **B1 (`no_matches` unreachable):** fixed by the owner decision above; contract, CUJ 4, Goals, pipeline, `search.py`, `api/search.py`, `search.js`, `test_search.py`, and the pinned-defaults table all state the floor + top-up; search cards carry `score`.
* **B2 (outage drains jobs to `failed_perm`; FIFO starves thumbnails):** endpoint-level failures (unreachable or wrong model) now trip a worker-level circuit backoff (5 s ×2, cap 5 min, cleared on probe success) — no attempts charged, jobs stay pending, thumb jobs keep flowing. Per-job attempts (30 s ×2, cap 1 h, 5 → `failed_perm`) are reserved for per-file failures. The worker pops `thumb` first. CUJ 6 and `test_jobs.py` updated.
* **B3 (phase-1 insert vs phase-2 re-link):** files whose hash matches a live row now stage in a new `scan_staged` table until phase two resolves vanishes — no `UNIQUE(path)` collision, no transient double-display, no wasted re-embeds; a duplicate's staged row is inserted after its live twin survives; a re-link whose target path is occupied tombstones instead. Directory mtimes are recorded only after a directory's files are processed, vanish checks apply only to walked-or-gone directories (pruned dirs are assumed intact), and an interrupted pass restarts pruned and idempotent — phase two never sees a partial seen set, so a crash cannot trip the guard.
* **B4 (`width`/`height` had no producer):** the scanner reads dimensions and EXIF `Orientation` from the already-read bytes and stores the pair swapped for orientations 5–8 (display dimensions), pinned in the schema comment, scanner entry, response contract, and `test_scanner.py`.
* **B5 (vector-index lifecycle):** replace cascades a vector-index mask of the old id; startup load joins `photos` so tombstoned ids start masked; `dim` comes from the first loaded vector or lazily from the first embed on an empty table (never hardcoded); startup prunes pending embed jobs whose photo already has a vector for the configured model (aborted model changes no longer re-embed for days); coverage counts come from `vectors`, never `embedded_at`.

## Scale, schema & operational gaps — all fixed

* **M1 (per-query ~460 MB fancy-index copy):** ranking is one full-matrix product (`matrix[:n] @ query`, BLAS `sgemv`) with the tombstone mask and date/folder filter applied to the 1-D score vector; no SQL candidate fetch when no filter is active.
* **M2 (dequeue index):** `jobs` gains `(kind, next_attempt_at, job_id) WHERE state='pending'`; successful jobs are deleted (per-stage columns record completion), so the state enum is `pending | failed_perm`.
* **M3:** `photos.path`/`dirs.path` pinned library-relative; subtree scoping upgraded from the `substr` form to the sargable range `path >= folder || '/' AND path < folder || '0'` (register row updated); `photo_count` pinned recursive; direct-only folder view deferred to Future Improvements with a trigger.
* **M4 (backup path / FHS):** `database_path` defaults to `/var/lib/plaps/plaps.db`, `cache_dir` to `/var/cache/plaps`, backups to `backup_dir` (default: beside the database) — nothing writes under `/etc`.
* **M5 (two-decade navigation):** `GET /api/photos` gains optional day-inclusive `after`/`before` (the contract's half-open raw-string range); `timeline.js` states DOM virtualization with measured spacers and date-header jumps.

## Minor guardrails — all fixed

* **N1:** the writer lock is never held across disk reads, hashing, pyvips, or HTTP; the scanner commits per directory.
* **N2:** cache-size increments only on creating a previously absent target (checked under the writer lock), cleanup subtracts actually-unlinked sizes, and the counter resets at startup on an empty/missing cache dir; residual drift after partial manual deletion is recorded in Risks & Limits.
* **N3:** `pyvips.cache_set_max(0)` at import.
* **N4:** `mtime` stores `st_mtime_ns`; the mtime fallback is formatted `%Y-%m-%dT%H:%M:%S` after the `zoneinfo` conversion; `date_source='unknown'` is pinned to `st_mtime <= 0` or an out-of-range epoch.
* **N5:** blake2b pinned to `digest_size=32` (64 hex chars), validated by `paths.py` on the `/media/*` routes.
* **N6:** `mask`/`unmask` publish copied boolean arrays.
* **N7:** component map lists `/original/*`; the disk estimate corrected to ~8–14 GB for the two thumbnail variants, with screen-fits adding only as photos are viewed.

The document stands at 723 lines; the Decision Register carries five new rows from this review.
