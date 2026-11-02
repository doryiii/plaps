# Design Review 1 — `docs/designs/plaps.md`

* **Target:** `docs/designs/plaps.md` (707 lines, post-Stage-9 revision)
* **Spikes cross-checked:** `docs/spikes/embedding-endpoint.md`, `docs/spikes/lighttpd-sendfile.md`, `docs/spikes/pyvips-exif.md`
* **Date:** 2026-11-01

## Overall Verdict

**Strong architecture; one short revision pass needed before coding.**

The load-bearing choices in `docs/designs/plaps.md` are coherent, minimalist, and unusually well-grounded in live platform verification: one process, one locked SQLite WAL writer, resident `float32` NumPy ranking, content-hash-keyed WebP caches, proxy-terminated digest auth, no-build ES modules, and flip-at-start model swaps. Running the three spikes up front (catching `mod_proxy` silently dropping `X-Sendfile` and llama.cpp's nested `content` image payload shape) eliminated major integration surprises early.

None of the findings below push toward rejected machinery. Every issue comes from tracing the document's own SQL, NumPy, worker-queue, and filesystem invariants end-to-end—uncovering five logic/contract bugs that would fail tests or corrupt state if implemented verbatim, five 150K-scale performance/operational traps, and a handful of small implementation guardrails.

---

## 1. Logic & Contract Bugs (Fix Before Coding)

### B1. `state: "no_matches"` in search is mathematically unreachable without a similarity floor
* **Locations:** Lines 38, 85 (CUJ 4), 219–226 (API surface & Response contract), 527–531 (`plaps/search.py`), 547–551 (`plaps/api/search.py`), 634–637 (`tests/test_search.py`), 678–698 (Pinned defaults).
* **Problem:** The response contract defines `"no_matches"` as *"zero results with `coverage.embedded > 0` (things were searchable, nothing matched)"*, and both `tests/test_search.py` and the manual E2E plan require verifying all three states. However, `search.py` performs pure cosine-similarity ranking with **no score cutoff (`min_score`)** anywhere in configuration, `search.py`, or Pinned defaults. Every embedded photo produces a real cosine score in `[-1, 1]` (in `docs/spikes/embedding-endpoint.md` line 64, even non-matching runner-ups scored `0.53–0.68`), so whenever `coverage.embedded > 0`, top-K ranking **always** returns `min(coverage.embedded, limit) >= 1` results. `"no_matches"` is dead code as written.
* **Proposed Fix:** Either:
  1. Add an `embedding.min_score` threshold under `[embedding]` alongside `model_id` (cosine distributions are model-specific—a `~0.55` cutoff calibrated for EmbeddingGemma 2 would zero out a future swap model whose matches sit around `0.25–0.35`), **or**
  2. Drop `"no_matches"` on the server (leaving `"ok"` vs `"empty_filter"` plus `503 "endpoint_down"`) and include `score` on search result cards if client-side thresholding is ever desired. *(Owner decision if a threshold is introduced.)*

### B2. AI-box downtime (`CUJ 6`) drains healthy jobs into `failed_perm` and head-of-line blocks thumbnails
* **Locations:** Lines 93–98 (CUJ 6), 206 (Embedding pipeline), 522–526 (`plaps/worker.py`), 691 (Pinned defaults).
* **Problem (two interacting defects):**
  1. **Outage bricks pending jobs permanently:** Line 206 states that *"endpoint unreachable retries with exponential backoff (×2, capped at 1 h; after 5 attempts the job becomes `failed_perm`)"*. First, no base retry delay is pinned—and for any base `< 450 s`, 5 attempts at `×2` cannot mathematically reach the 1 h cap (e.g., a 30 s base exhausts 5 attempts in 7.5 minutes; a 60 s base in 15 minutes). Worse: when the AI box is unreachable *"for hours or days"* (CUJ 6) or serving the wrong model before switchover, batch 1 hits 5 attempts and marks 8 healthy photos `failed_perm`, then the worker pops batch 2 and marks the next 8 `failed_perm`, continuously draining the queue into permanent failure with no recovery path when the AI box returns.
  2. **FIFO queue (`oldest-first`) throttles thumbnails to AI-box speed:** The single worker pops `thumb` and `embed` jobs *"oldest-first"* (line 206). Generating two 512 px WebP thumbnails in libvips takes ~20 ms/photo (~1 hour for 150K), whereas embedding takes ~0.5–2.5 s/photo (up to ~4.3 days). Strict FIFO throttles eager thumbnailing to AI-box throughput during first ingest—and after a model-change restart (which queues 150K `embed` jobs at `T_startup`), newly imported photos sit behind 150K re-embed jobs for days without eager thumbnails. Likewise, if the worker sleeps during embed backoff, `thumb` jobs stall during AI-box downtime (violating CUJ 6).
* **Proposed Fix:**
  * Separate **endpoint-level circuit backoff** from **per-job `attempts`**: `EndpointUnreachableError` (including served-model mismatch) advances a worker `embed_backoff_until` timer (e.g., `5 s × 2` capped at `5 min`, cleared on probe success) **without** incrementing `jobs.attempts` or transitioning jobs to `failed_perm`. Reserve `attempts → failed_perm` (with a pinned base delay, e.g., `30 s × 2`, 5 attempts) strictly for per-file failures (`thumb` errors and serial `ItemRejectedError`s).
  * Always prioritize ready `thumb` jobs ahead of `embed` jobs in `worker.py`, and continue popping `thumb` jobs while `now < embed_backoff_until`.

### B3. Scanner Phase 1 immediate upsert conflicts with Phase 2 move re-linking (`UNIQUE(path)`) and `kill -9` resume
* **Locations:** Lines 91 (CUJ 5), 165 (`photos` schema), 198 (Scanning and change detection), 503–507 (`plaps/scanner.py`), 705 (Verification Plan).
* **Problem:**
  * Phase 1 states that for every new/changed file it immediately **upserts the `photos` row** and **enqueues `thumb` + `embed` jobs**, while Phase 2 says a vanished path whose content hash appeared at another path in the same pass **re-links (move, keep `photo_id`)**.
  * Trace renaming a 5,000-photo folder (`2021_trip/` → `2021_Hawaii/`): Phase 1 walks `2021_Hawaii/`, inserts 5,000 *new* `photo_id`s at the new paths (exposing 10,000 duplicate photos to timeline readers mid-scan and waking `worker.py` to re-embed already-embedded photos!). When Phase 2 discovers `2021_trip/*` vanished and tries to rewrite the *old* `photo_id`s (which own the `vectors` rows) to `2021_Hawaii/*`, `photos.path` (`UNIQUE NOT NULL`) collides with the rows Phase 1 just inserted.
  * Additionally, if `"every content hash seen"` and seen-path tracking for Phase 2 live only in Python memory, a `kill -9` crash mid-scan (line 705) that resumes from the `meta` checkpoint loses pre-crash seen state and will falsely treat pre-checkpoint directories as vanished, tripping the 10% mass-delete guard.
* **Proposed Fix:**
  * During Phase 1, insert immediately into `photos` (and enqueue jobs) when `content_hash` matches **no live row** (99.9% of first ingest and normal camera imports, preserving live UI streaming during CUJ 1). When a newly discovered path's `content_hash` matches an existing *live* row, stage `(path, size, mtime, content_hash, date_taken, ...)` in a SQLite `scan_staged` table until Phase 2 checks vanishes: if a live row with that hash vanished, `UPDATE photos SET path = new_path, ... WHERE photo_id = old_id` (zero transient duplicates, zero wasted embed jobs, zero `UNIQUE` collision); if all existing live rows with that hash remained on disk, insert the staged row as a new duplicate and enqueue jobs.
  * Persist pass-seen tracking in SQLite (e.g., `seen_pass_id` or deterministic directory cursor + SQLite staging) and update `dirs.mtime` only after all files in that directory have been processed.

### B4. `photos.width` and `photos.height` have no producer—and must reflect post-EXIF-orientation display dimensions
* **Locations:** Lines 172 (`photos` schema), 226 (Response contract), 498–502 (`plaps/exif.py`), 503–507 (`plaps/scanner.py`), 508–512 (`plaps/images.py`).
* **Problem:** Line 226 states that `width` and `height` on every photo card are *"required by the justified-rows layout before images load"*, and CUJ 1 serves the timeline immediately after metadata scan before background thumbnail jobs run. Yet neither `exif.py`, `scanner.py`, nor `images.py` mentions reading or writing `width` and `height`. Furthermore, because `images.py` auto-rotates `thumb_as` via `pyvips`, storing raw unrotated JPEG dimensions would give every portrait phone/camera photo (stored as landscape pixels with EXIF `Orientation` 6 or 8) an inverted 4:3 box in justified rows while loading a 3:4 rotated WebP.
* **Proposed Fix:** Have `scanner.py` (or `exif.py` + `pyvips.Image.new_from_buffer(data, "")`, which parses only JPEG headers without decoding pixels) read `(width, height)` and EXIF `Orientation` from the already-read file bytes, swapping `width` and `height` when `Orientation` is in `{5, 6, 7, 8}`.

### B5. `VectorIndex` & model-change lifecycle gaps (`replace` unmasking, startup tombstones, cold-start `dim`, abort cleanup)
* **Locations:** Lines 107–110 (CUJ 8), 198, 208–210, 506 (`plaps/scanner.py`), 527–531 (`plaps/search.py`), 562–566 (`plaps/app.py`), 634–637 (`tests/test_search.py`).
* **Four gaps:**
  1. **`replace` forgets `VectorIndex.mask(old_photo_id)`:** Vanish masks `photo_id` in `VectorIndex`, but **replace** only cascades to the SQLite `vectors` table (lines 198, 506) without masking `old_photo_id` in the resident `VectorIndex`—leaving deleted `photo_id`s rankable in memory until restart.
  2. **Startup load must respect existing tombstones:** Because tombstoned photos retain their `vectors` rows until the 30-day purge, `VectorIndex` startup must join `photos` and initialize `masked = (deleted_at IS NOT NULL)`.
  3. **Cold-start / model-swap vector `dim` is unknown at boot:** On first run (CUJ 1) or immediately after restarting onto a new `embedding.model_id` (CUJ 8), SQLite has 0 rows in `vectors` for the configured model (and the AI box may be down at boot, CUJ 6). Preallocating `(200_000, dim)` cannot hardcode `dim = 768` (candidate swap models like Qwen3 or SigLIP use different dimensions); allocate `(200_000, len(vec))` lazily on the first vector when startup loads 0 rows, and assert `len(vec) == dim` thereafter.
  4. **Aborting a model change leaves ~140K redundant `pending` embed jobs:** CUJ 8 (line 109) and `tests/test_search.py` (line 636) specify that reverting `config.toml` to the old `model_id` and restarting before cutover aborts cleanly. However, `jobs(photo_id, kind, state)` is model-agnostic: the `pending` re-embed jobs queued for the new model remain in `jobs`, so restarting on the old model causes `worker.py` to spend days re-embedding photos that already have old-model vectors. Startup should prune `pending` `embed` jobs whose `photo_id` already has a `vectors` row for the configured `model_id` (and `search.py` must compute `coverage.embedded` from `vectors` / `VectorIndex` for `configured_model_id`, never `photos.embedded_at IS NOT NULL`).

---

## 2. Scale, Schema & Operational Gaps (150K Photos)

### M1. Avoid a ~460 MB per-query NumPy fancy-index copy in `search.py`
* **Locations:** Lines 208, 375 (Non-Functional Requirements), 530 (`plaps/search.py`), 706 (Verification Plan).
* **Problem:** Resolving candidate IDs in SQL first and then fancy-indexing `matrix[candidate_rows] @ query_vec` causes NumPy to allocate and copy a **~460 MB temporary float32 matrix** on unfiltered or broad queries—violating the `< 1 GB` steady-state RAM claim (especially under concurrent requests) and eating most of the `< 50 ms` p95 ranking budget (plus Python `sqlite3` `fetchall()` overhead for 150K IDs).
* **Proposed Fix:** Always multiply the contiguous active slice first (`scores = matrix[:size] @ query_vec` — zero-copy BLAS `sgemv`, ~12 ms, yielding a 600 KB 1D `float32` vector) and apply the live boolean mask (and optional SQL filter mask) to the 1D `scores` array. When no folder/date filter is active, skip the 150K-row SQL candidate-ID fetch altogether.

### M2. Missing dequeue partial index on `jobs`
* **Locations:** Lines 185–188 (Data model), 524–525 (`plaps/worker.py`).
* **Problem:** `jobs` only defines `UNIQUE(photo_id, kind) WHERE state='pending'`, ordered by `photo_id`. During initial ingest (300K pending jobs, ~169K worker pops), querying `WHERE state='pending' AND next_attempt_at <= :now ORDER BY kind, job_id LIMIT ...` scans remaining pending entries linearly on every pop ($O(N^2)$ across ingest).
* **Proposed Fix:** Add `CREATE INDEX idx_jobs_dequeue ON jobs(kind, next_attempt_at, job_id) WHERE state='pending'` (and optionally `DELETE` completed jobs instead of keeping hundreds of thousands of `state='done'` rows, since `photos.thumbnailed_at` and `embedded_at` already record completion).

### M3. `photos.path` relativity, sargable folder prefix queries, and folder `photo_count` / direct-photo semantics
* **Locations:** Lines 25, 79 (CUJ 3), 165 (`photos` schema), 218, 226 (Response contract), 417 (Decision Register), 545 (`plaps/api/folders.py`).
* **Three items:**
  1. **Pin library-relative paths:** Line 165 says `photos.path` is *"stored as walked"*, while line 226 compares `path` directly against the library-relative `folder` string decoded from `folder_id`. Explicitly specify that `photos.path` and `dirs.path` are stored **library-relative** (POSIX `/`, no leading slash)—which also makes moving `library_root` to a new mountpoint free.
  2. **Sargable folder prefix range:** Line 226 carefully avoids wrapping `date_taken` in a SQL function so its index works, yet `substr(path, 1, length(folder) + 1) = folder || '/'` wraps `path` in `substr()`, defeating the `UNIQUE(path)` B-tree index and forcing a 150K-row scan. Because `'0'` (`0x30`) immediately follows `'/'` (`0x2F`) in ASCII, `path >= (:folder || '/') AND path < (:folder || '0')` preserves exact case-sensitive, wildcard-free, separator-safe semantics while using the `UNIQUE(path)` index directly.
  3. **Folder `photo_count` & loose photos in parent folders:** Pin whether `photo_count` on folder nodes is recursive (matching subtree-scoped `GET /api/folders/<id>/photos`) or direct. Also note that pure subtree scoping means clicking `"root"` (or any parent folder containing both loose JPEGs and subfolders) merges all descendants, leaving no way to view *only* loose photos sitting directly in that directory (unlike Picasa's folder view cited on line 25). Consider supporting `?recursive=false` (or direct-only folder view).

### M4. `/etc/plaps` backup path permissions & FHS clash
* **Locations:** Lines 234 (Configuration), 454 (`README.md`), 464–478 (`systemd/*.service`), 496 (`plaps/db.py`), 683–684, 694 (Pinned defaults).
* **Problem:** The systemd units run as a dedicated unprivileged `User=` with `--config /etc/plaps/plaps.toml`. Even if the owner overrides `database_path` and `cache_dir` out of `/etc/plaps`, backups are hardcoded to `<config dir>/backups/` (`/etc/plaps/backups/` — 8 × ~1.5 GB ≈ 12 GB inside `/etc`), which will fail with `PermissionError` for the non-root service user and fill the root partition.
* **Proposed Fix:** Default backup snapshots to **`<database_path parent>/backups/`** (with an optional `backup_dir` config key), and use `/var/lib/plaps/plaps.db` and `/var/cache/plaps` in `config.example.toml`.

### M5. Navigating across two decades in `timeline.js`
* **Locations:** Lines 73 (CUJ 2), 216, 539–540 (`plaps/api/photos.py`), 585–588 (`static/js/timeline.js`).
* **Problem:** Pure sequential `IntersectionObserver` cursor paging from the top requires fetching 150–750 consecutive API pages to reach photos from 10–15 years ago (no way to jump to 2010), and `timeline.js` must genuinely unmount off-screen row blocks into measured height spacers rather than infinite-appending tens of thousands of `<img>` elements.
* **Proposed Fix:** State explicitly that `timeline.js` virtualizes DOM sections (replacing off-screen blocks with measured-height spacers), and consider accepting optional `before` / `after` date bounds on `GET /api/photos` (reusing `/api/search`'s exact half-open ISO string logic on the `(date_taken, photo_id)` index) so jumping to a year/date is one `< 10 ms` request.

---

## 3. Minor Guardrails & Doc Nits

* **N1. Writer-lock granularity (`plaps/db.py`, line 496):** Explicitly forbid holding the single writer lock across disk reads, hashing, `pyvips` encoding, or HTTP calls (commit scanner work in small batches per directory / ~100 files) so lazy screen-fit `cache_size` updates on request threads and admin actions never stall behind a scan pass.
* **N2. Cache-size drift on concurrent miss or cache deletion (`plaps/api/media.py`, lines 376, 555):** Two threads racing `os.replace()` on the same cache miss both succeed and double-increment `meta.cache_size`; and deleting `cache/` (*"regenerable and freely deletable"*, line 376) leaves `meta.cache_size` permanently inflated by gigabytes. Only add bytes when creating a previously absent target (e.g., `os.link` + `unlink` temp, or check under writer lock), subtract `st_size` of actually unlinked files (`0` on `FileNotFoundError`), and reset `cache_size = 0` at startup if `cache_dir` is empty.
* **N3. Disable libvips operation cache (`plaps/images.py`, line 510):** Call `pyvips.cache_set_max(0)` at startup. libvips's default 100-operation in-memory cache adds no value behind an on-disk WebP cache and causes multi-hundred-MB RSS growth in long-running multi-threaded servers.
* **N4. `mtime` precision & `date_taken` string formatting (`photos` / `dirs`, lines 168–171, 180, 506):**
  * Store `st_mtime_ns` (nanoseconds, fits in signed 64-bit SQLite `INTEGER` until year 2262) in `photos.mtime` and `dirs.mtime` to avoid 1-second mtime race blindness in scripts and `pytest`.
  * Format mtime-fallback `date_taken` strictly via `strftime("%Y-%m-%dT%H:%M:%S")` after `zoneinfo` conversion (stripping timezone offset and fractional seconds so raw ISO string comparisons match EXIF timestamps).
  * Specify what triggers `date_source = 'unknown'` (`'0001-01-01T00:00:00'`), e.g., `st_mtime <= 0` or out-of-range epoch, since `test_api_photos.py` (line 628) tests it.
* **N5. Pin `blake2b` digest size (`plaps/scanner.py` & `plaps/paths.py`, lines 167, 506, 656):** `hashlib.blake2b()` defaults to 64 bytes (128 hex chars); `digest_size=32` (64 hex chars) halves hash index and JSON card URL sizes—pin whichever length `paths.py` validates on `/media/*` routes.
* **N6. Copy-on-write boolean mask in `VectorIndex` (`plaps/search.py`, lines 208, 530):** Appending into spare capacity of the 200K-row matrix buffer is tear-free for readers holding a slice `[:N]`, provided `mask()` / `unmask()` publishes a copied 200 KB boolean mask array rather than mutating the active mask slice in place.
* **N7. Two small doc nits:**
  * Component map (line 123) lists `reverse-proxies /api/* and /media/* to the app`, omitting `/original/*` (present in the Flask box on line 138 and in `docs/spikes/lighttpd-sendfile.md` line 44).
  * NFR disk estimate (line 375: *"derived cache roughly 35–70 GB at 150K photos with both thumbnail variants"*) reflects pre-O3 eager screen-fits; two 512 px WebP q80 variants average ~50–90 KB/photo (~8–14 GB at 150K photos), reaching 35–70 GB only if lazy 2048 px screen-fits are eventually viewed across most of the library.
