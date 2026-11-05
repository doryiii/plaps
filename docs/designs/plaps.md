# plaps — On-Disk Semantic Photo Gallery

*   **Author:** Project owner
*   **Status:** Proposed (revised after review)
*   **Date:** 2026-10-30 · Revised 2026-10-31 after the consolidated review (eight Goldfish dispatches) and owner decisions O1–O7 · Revised 2026-11-01 after the Stage 9 re-run and external design review 1 (`design_review_1.md`); see Decision Register

--------------------------------------------------------------------------------

## Overview

plaps is a single-user, self-hosted photo gallery web app that provides a Google-Photos-style experience over an existing library of ~150K JPEG photos on local disk. Unlike hosted galleries, its source of truth is the filesystem: the server indexes the library in place, never copies, moves, or renames originals, and derives only cached WebP thumbnails and screen-fit images stored outside the library.

The stack is deliberately small: a Python service (Flask) with SQLite as the only store (metadata, vectors, job queue), pyvips for image work, piexif for EXIF, served behind a lighttpd reverse proxy that also provides password authentication (digest, matching the target server's existing setup). No Docker, no NodeJS, no build toolchain. Semantic search runs against Google's EmbeddingGemma 2 (joint text+image embeddings), served by llama.cpp on a separate AI box through a single adapter module. The frontend is no-build ES modules rendering client-side against a JSON API — the same API a future Android client would consume.

## The Problem

The owner keeps a large, growing, and loosely organized photo library directly on disk — files added by camera imports and card dumps, with no rigid directory structure, and no intention to reorganize it or upload it anywhere. Hosted and self-hosted galleries each fail this setup in a different way:

* Google Photos requires uploading everything out of the owner's control.
* PhotoPrism expects a specific directory layout and offers label/keyword search rather than semantic (joint text+image embedding) search.
* Immich supports in-place external libraries but ships as a Docker + TypeScript stack the owner does not want to operate or own.
* PiGallery2 scans in place but is a NodeJS application without semantic search.
* LibrePhotos is Python with semantic search but carries Django + Postgres + worker machinery far beyond a single-user need.

The need: fast, date-organized browsing (flat timeline and Picasa-style folder view) plus semantic search, over photos that live on a local disk in arbitrary structure — with originals untouched, no duplication beyond regenerable caches, and a system one person can install, understand, and maintain alone on a home server.

The hard problems this forces, and which the design must answer directly:

* **Identity and change detection.** Files are added mostly, but occasionally renamed, moved, deleted, or edited in place. The index must track all of that without ever touching the files, survive scanner crashes, and never interpret a configuration mistake as a mass deletion.
* **Search over a fragile dependency.** Embeddings come from a separate machine. Browsing must not depend on it; when the endpoint is down, the app must say so rather than imply "no results"; model changes are owner-initiated and must not silently break search; and the owner must always be able to tell "nothing matched well" from "nothing embedded."
* **Scale without machinery.** 150K photos must be browsable without pagination UI, searchable without a vector database, and indexable without a message queue — on one home server.

## Goals

*   **In-place indexing.** Index ~150K local JPEGs by path; originals are never modified, moved, or duplicated (verifiable: file bytes and mtimes unchanged after indexing). The scanner indexes files matching a fixed, case-insensitive extension allowlist (`.jpg`, `.jpeg`); everything else is ignored and counted on `/api/status`. Change detection is stat-based (path + mtime + size) with a daily full stat sweep and a roughly monthly re-hash pass.
*   **Date timeline.** A flat, virtualized timeline of all photos sorted by EXIF `DateTimeOriginal` (mtime fallback), served over a cursor-paged JSON API with no pagination UI; windows are stable against concurrent scanner writes — no skipped or repeated entries in the fetch stream.
*   **Folder view.** A tree of arbitrarily nested folders derived from indexed paths; folders sorted by the newest date-taken of contained photos (recursive); photos within a folder sorted by date.
*   **Semantic search.** Text queries rank photos by cosine similarity of joint text+image embeddings; any active date/folder filter applies before ranking; results that reach a similarity floor (`embedding.min_score`) are matches, and when fewer than `embedding.min_matches` reach the floor the view is topped up with the next-closest photos anyway; the results view carries a filter-scoped coverage count ("search covers N of M photos in this view") and distinguishes three states — endpoint down, nothing embedded in this filter, nothing reached the floor; ranking completes in tens of milliseconds (target; end-to-end latency adds the query-embed round trip — see Risks & Limits).
*   **Derived images.** WebP thumbnails (512 px, both variants generated eagerly) and screen-fit images (~2048 px, generated lazily on first view) cached outside the library, keyed by content hash and variant; both grid styles — uniform square (salience-cropped) and justified rows (aspect-preserving) — available as a client-side option; originals downloadable but never the default view.
*   **Operational simplicity.** Installs and runs as a plain service (venv, systemd unit, libvips OS package, lighttpd config with proxy password auth); zero Docker, zero NodeJS; SQLite single-file backup with a scheduled trigger and a documented restore path; `/api/status` exposes scan progress, queue depth, errors by kind, ignored-file count, cache size, and embedding-endpoint health.
*   **Graceful degradation.** With the AI box down: browsing, folder view, and original download are unaffected; search is unavailable and reports a distinct endpoint-down state (query encoding needs the same endpoint); the embed queue backs off and resumes on recovery.
*   **Verification.** An API-level pytest suite (Flask in-process test client, temp library trees, fake embedding adapter) plus a thin unit layer for pure logic and a few concurrency stress tests, run locally with one command.

## Non-Goals

*   Photo editing, live photos, RAW, and video support.
*   Multi-user accounts or permissions; app-level authentication in the MVP (proxy password auth covers it; a cookie login page is backlog for share links or a second user).
*   Server-side user preferences — grid style and timeline order persist in the browser's localStorage, not in config or a settings table.
*   Upload as a primary ingestion path.
*   Remote/network-mounted filesystems (local disks only).
*   Symlink support (symlinks are skipped entirely).
*   ANN vector indexes (brute force suffices below ~1M photos).
*   Message-queue or container infrastructure of any kind.
*   Node/npm tooling of any kind, including at build time.
*   Any duplicate handling in the MVP — duplicates display twice and cost double embeddings (accepted; see Risks & Limits). Detection, reporting, and any workflow are backlog.

## Open Questions

*None currently. The two questions inherited from the concepts brief were resolved during drafting; the consolidated review of 2026-10-31 raised further items, all resolved by owner decisions O1–O7 and the drafting fixes in that revision, and the Stage 9 re-run of 2026-11-01 (which surfaced the cutover flip-at-start decision) is likewise closed — see the Decision Register under Risks & Limits.*

## Critical User Journeys (CUJs)

### CUJ 1: First run against an existing library

*   **Given**: A home server with a local disk holding ~150K JPEGs in arbitrary nested folders; plaps installed (venv + systemd + lighttpd) and configured with the library root and cache directory; the AI box reachable.
*   **When**: The service starts for the first time against an unindexed library.
*   **Then**: The scanner begins a full metadata pass; the UI serves what is already indexed from the start, with a progress indicator the user can dismiss; thumbnails and embeddings accrue in the background (the full-embed window is hours to days — it depends on AI-box throughput, which is unmeasured; the dev box ran ~2.5 s/photo, so re-measure on the AI box before expecting a number); search results carry the coverage line until embedding completes; a scanner crash never corrupts the index — the interrupted pass restarts pruned and idempotent, and vanish resolution never runs against a partially-walked library — and a stall or crash is a visible error state.

### CUJ 2: Browse the timeline

*   **Given**: The library is indexed (or indexing in progress).
*   **When**: The user opens the app and scrolls the flat timeline across two decades of photos.
*   **Then**: Photos appear sorted by date taken (newest or oldest first — a client-side setting persisted in localStorage); the virtualized view fetches windows through the cursor API without a pagination widget, and windows are stable against concurrent inserts — no skipped or repeated entries in the fetch stream; date headers jump to a year in one date-bounded request, so reaching a decade back never means paging through hundreds of windows; photos newly indexed above the viewport appear after a client refresh; clicking a photo shows the screen-fit image (generated on first view, then cached); an explicit action downloads the original, untouched, via the proxy.

### CUJ 3: Browse by folder

*   **Given**: The same index.
*   **When**: The user opens the folder view.
*   **Then**: Folders appear sorted by the newest date-taken among their contained photos (computed recursively); folders with no photos anywhere beneath them are hidden; entering a folder shows its photos sorted by date; the tree reflects the indexed structure at query time (it lags the disk by at most the scan interval) with no separately maintained index to drift.

### CUJ 4: Semantic search

*   **Given**: Embeddings exist for at least part of the library; the AI box is reachable.
*   **When**: The user types a text query such as "dog on the beach," optionally while a date range or folder filter is active.
*   **Then**: The query is encoded server-side; any active filter is applied before ranking; results are ranked by cosine similarity; photos without a current-model embedding are silently excluded from ranking; the results view distinguishes three states — endpoint down (a distinct error state), nothing embedded in this filter, and nothing reached the similarity floor (the view still shows the closest photos, topped up to `min_matches`) — with the coverage count computed inside the active filter; ranking completes in tens of milliseconds after the query embed returns.

### CUJ 5: Library changes underneath the index

*   **Given**: The library is indexed and the service is running.
*   **When**: The user adds files to any folder, renames or moves files or folders, deletes files, or edits a file in place.
*   **Then**: The scanner picks up additions at its next pass (stat-pruned, with the daily sweep as backstop for in-place edits). A **vanish** (path gone, content nowhere) tombstones the record (`deleted_at`); renames and moves re-link to existing records via content-hash match so metadata survives — staged until vanish resolution, so a moved photo never displays twice mid-scan or re-embeds content that already has vectors; a tombstone revives if the same content hash reappears within the purge window — the revived record keeps its `photo_id`, rewrites its path to the new location, and re-enqueues any missing thumbnail/embed jobs. A **replace** (content changed at the same path) hard-deletes the old record and inserts a new one, cascading: the old record's vectors are deleted, its pending jobs are dropped, and its cache entries are removed unless another live record shares the hash. A file that vanishes between scans fails its read but its record stays listed until the next scan.

### CUJ 6: The AI box goes down

*   **Given**: Embeddings are partially complete, or a re-embed is running.
*   **When**: The AI box becomes unreachable for hours or days.
*   **Then**: Browsing, folder view, and original download are unaffected; search is unavailable and returns a distinct endpoint-down state rather than an empty result set; the embed queue backs off (a worker-level circuit, charging no attempts) and resumes from where it stopped when the box returns — an outage never marks a job `failed_perm`; thumbnail jobs keep flowing throughout; per-file errors are recorded per stage, distinguishable from pending work; `/api/status` shows endpoint reachability and the last successful embed.

### CUJ 7: A configuration mistake threatens the index

*   **Given**: The deletion guard is armed with a file-count-drop threshold (10 % of the previous completed pass's live file count).
*   **When**: The library root is unstat-able, or a scan pass sees an unre-linked vanish count beyond that threshold (e.g., root pointed at the wrong directory).
*   **Then**: The tombstone pass halts without tombstoning anything; the condition is logged and surfaced on `/api/status`; the rest of the scan (adds, re-links, replaces) continues — the guard covers vanishes only; deletion processing resumes only after manual acknowledgment, and the next pass re-evaluates the condition before re-arming. The first pass against a fresh database establishes the baseline; the guard arms from the second pass onward.

### CUJ 8: The embedding model changes

*   **Given**: Vectors are stamped with `model_id`; the owner wants to move to a new embedding model. Cutover is owner-initiated — there is no automatic threshold. One endpoint serves one model, so the change flips at the start, not at the end (Decision Register).
*   **When**: The owner starts the new model on the AI box, edits `embedding.model_id` in the config (and `base_url` if the port moved), restarts, watches re-embed coverage on `/api/status`, and calls `POST /api/admin/model/cutover` when satisfied with the coverage.
*   **Then**: On startup with a `model_id` that differs from `meta.active_model_id`, the system queues re-embed jobs for every photo lacking a vector for the configured model — a restart mid-change re-queues only the remainder. The vector index serves the configured model from that moment: coverage starts near zero and the results-view coverage line shows it growing, exactly as during first ingest; queries and vectors share a single embedding space, so results are valid over the re-embedded subset throughout, and browsing is unaffected. Calling cutover rewrites `active_model_id` and deletes the old-model vectors; until then, aborting means reverting the config and restarting (the old vectors are still in the database, and startup prunes the aborted change's leftover pending jobs). The owner decides when.

## Technical Plan (Architecture)

### Component map

```text
                    browser (no-build ES modules)   future Android client
                              │  JSON over HTTP                 │
                              ▼                                 ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ lighttpd                                                      │
        │  · password auth, digest (the only auth layer)                │
        │  · serves static frontend files directly from disk            │
        │  · reverse-proxies /api/* and /media/* and /original/*        │
        │  · streams proxied responses; Range/206 passthrough verified  │
        │  · TLS termination if ever exposed past localhost/LAN         │
        └───────────────┬───────────────────────────────────────────────┘
                        │ HTTP (proxy)
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ plaps service — ONE process (systemd unit), waitress WSGI     │
        │                                                               │
        │  Flask app (request threads)          background threads      │
        │   /api/photos      timeline windows    scanner: walk library, │
        │   /api/folders     folder tree               stat-prune,      │
        │   /api/search      ranked results            hash, EXIF,      │
        │   /api/status      health/progress           tombstone, guard │
        │   /media/*         derived images      job worker: thumb +    │
        │   /original/*      originals,           embed jobs → adapter  │
        │                     both streamed          → normalized vecs  │
        │                                                               │
        │  SQLite: one writer connection (locked, shared by scanner +   │
        │  job worker, and used by admin actions too); per-thread       │
        │  reader connections in WAL mode                               │
        └──────┬──────────────┬───────────────┬────────────┬────────────┘
               │              │ reads/writes  │ read-only  │ HTTP (embeddings)
               ▼              ▼               ▼            ▼
        ┌─────────────┐ ┌──────────────┐ ┌───────────┐ ┌────────────────┐
        │ plaps.db    │ │ cache dir    │ │ library   │ │ AI box:        │
        │ (SQLite,WAL)│ │ (outside the │ │ root      │ │ llama.cpp      │
        │ photos,dirs,│ │ library)     │ │ (originals│ │ EmbeddingGemma │
        │ vectors,    │ │ thumb/<aa>/… │ │ never     │ │ 2 GGUF+mmproj; │
        │ jobs, meta  │ │ large/<aa>/… │ │ written)  │ │ /v1/embeddings │
        └─────────────┘ └──────────────┘ └───────────┘ └────────────────┘
```

### Process model

One process, one systemd unit. waitress (pure-Python, no C toolchain, one dependency) serves WSGI with a 16-thread pool; the scanner and the job worker run as daemon threads in the same process. This follows from the brief's single-writer decision: the scanner, the job worker, and every mutating request (guard resume, model cutover) share one SQLite writer connection guarded by a lock, which makes "database is locked" structurally impossible between writers; readers open per-thread connections in WAL mode. The GIL is not a bottleneck here — pyvips, hashing, and numpy all release it during native work, and file streaming is socket I/O that releases the GIL on every write. A crash kills all three roles at once, which is acceptable by design: the jobs table and a scan checkpoint in `meta` make every background pass resumable, never restartable-from-zero.

### Data model

```sql
photos(
  photo_id      INTEGER PRIMARY KEY,
  path          TEXT UNIQUE NOT NULL,      -- library-relative (POSIX '/', no leading slash);
                                           -- no realpath canonicalization (symlinks are skipped)
  content_hash  TEXT,                      -- blake2b-32 hex (64 chars); NON-unique: identity for re-link/revival
  size          INTEGER, mtime INTEGER,    -- last seen stat (mtime = st_mtime_ns); change = re-hash
  date_taken    TEXT NOT NULL,             -- ISO 8601 local wall-clock 'YYYY-MM-DDTHH:MM:SS': EXIF DateTimeOriginal,
                                           -- mtime fallback, sentinel '0001-01-01T00:00:00' if unknown
  date_source   TEXT,                      -- 'exif' | 'mtime' | 'unknown' (sentinel sorts oldest)
  width, height INTEGER,                   -- display dimensions: swapped when EXIF Orientation is 5–8
  thumbnailed_at, embedded_at DATETIME,    -- per-stage status columns, no lifecycle enum
  deleted_at    DATETIME,                  -- tombstone (vanish only); NULL = alive
  exif_error TEXT, thumb_error TEXT, embed_error TEXT   -- one error slot per stage
)
-- indexes: (date_taken, photo_id) for the timeline cursor;
--           (content_hash) for re-link/revival; (path) UNIQUE for the scanner

dirs(path TEXT PRIMARY KEY, mtime INTEGER)   -- directory mtimes (st_mtime_ns) from the last pass; drives stat-pruning

scan_staged(path, size, mtime, content_hash, date_taken, date_source, width, height)
-- same-pass rename/move staging: a walked file whose hash matches a live row waits here
-- until phase two resolves vanishes, so re-linking never collides with UNIQUE(path)

vectors(photo_id, model_id, dim, vector BLOB,   -- float32 little-endian, L2-normalized
        PRIMARY KEY (photo_id, model_id))

jobs(job_id, photo_id, kind,              -- 'thumb' | 'embed'
     state,                               -- 'pending' | 'failed_perm' (successful jobs are deleted;
                                           --  thumbnailed_at / embedded_at record completion)
     attempts, next_attempt_at, error_text, created_at)
-- partial unique index UNIQUE(photo_id, kind) WHERE state='pending'  -- job dedup
-- dequeue index (kind, next_attempt_at, job_id) WHERE state='pending'

meta(key, value)    -- active_model_id, scan checkpoint, guard state, coverage counters
-- schema version via PRAGMA user_version; one-way migrations
```

Key points: one record per path (decided); the content hash is a non-unique identity for re-linking and revival (duplicates display twice and embed twice — accepted, see Risks & Limits); per-stage nullable columns with one error slot per stage replace a lifecycle enum. **Deletion is two operations, not one:** a *vanish* (path gone) writes a tombstone and participates in revival and the mass-delete guard; a *replace* (content changed at the same path) hard-deletes the old row — freeing the UNIQUE path — and inserts a new one, cascading: the old row's vectors are deleted, its id is masked in the vector index, its pending jobs are dropped, and its cache entries are removed unless another live record shares the same hash. The guard never trips on replace.

### Scanning and change detection

The scanner thread loops, each pass in **two phases**: phase one walks the library root with **stat-pruning** (descend every directory, but skip stat-ing files inside directories whose own mtime is unchanged since the last pass, using the `dirs` table — entries for vanished directories are dropped each pass), and for each new or stat-changed file that matches the extension allowlist (`.jpg`, `.jpeg`, case-insensitive; everything else is ignored and counted): reads the file **once**, hashes it (blake2b via `hashlib.file_digest`, `digest_size=32`), reads EXIF `DateTimeOriginal`, `Orientation`, and the JPEG dimensions from the same bytes (piexif; malformed EXIF → `exif_error` + mtime fallback, converted from epoch using the configured timezone via `zoneinfo` and formatted `%Y-%m-%dT%H:%M:%S`; `(width, height)` stored swapped when `Orientation` is 5–8, so the card carries post-rotation display dimensions), and then **inserts immediately only when the content hash matches no live row** — first ingest and normal imports — upserting the `photos` row and enqueuing thumbnail and embed jobs (insert-or-ignore against the pending-jobs unique index — no stacked duplicates). When the hash **does** match a live row (a same-pass rename/move, or a duplicate), the file is staged in `scan_staged` instead: inserting at the new path now would collide with `UNIQUE(path)` when phase two re-links, display the photo twice mid-scan, and enqueue embed jobs for content that already has vectors. A directory's mtime is recorded only after every file in it has been processed, and every content hash seen is recorded. Phase two resolves vanishes against that collected seen set — a live photo vanishes only if it was not seen **and** its directory was either walked this pass or no longer exists (photos in pruned, unchanged-mtime directories are assumed present). A vanished path whose content hash appeared at another path in the same pass re-links (move, keep `photo_id` — `UPDATE` the old row to the staged path; if another live row already occupies that path, the vanished row tombstones instead, its content surviving at the duplicate); staged entries whose live twin never vanished are inserted as new duplicates with jobs enqueued, and `scan_staged` is cleared at pass end. A vanish with no re-link writes `deleted_at` and masks the photo in the vector index. A tombstone revives if the same content hash reappears anywhere within the purge window: the record keeps its `photo_id`, rewrites its path to the new location, unmasks in the vector index, and re-enqueues missing jobs; if two tombstones share the hash, the most recently tombstoned one revives. A stat-changed file whose new hash differs from its old one is a **replace**: hard-delete the old row and cascade (vectors, a vector-index mask of the old id, pending jobs, cache entries unless a live record shares the hash), then insert the new row. Two backstops: a daily full stat sweep (in-place edits never touch the parent directory's mtime) and a roughly monthly re-hash pass (mtime-preserving copies; a full library read — hours on spinning disks, accepted). The mass-delete guard covers **vanishes only** and runs before the tombstone write: if the library root is unstat-able, or the pass's unre-linked vanish count exceeds the configured threshold of the previous completed pass's live file count (armed from the second pass onward — the first pass establishes the baseline; a threshold of 0 disables the guard), tombstone writes halt, the state lands in `meta` and on `/api/status`, and resuming requires the explicit admin action, after which the next pass re-evaluates the condition before re-arming. **Crash safety:** an interrupted pass restarts rather than resuming mid-phase — phase one is idempotent (re-running re-upserts and re-stages) and stat-pruned, so the re-walk is cheap, and phase two never runs against a partial seen set, so a crash can never masquerade as a mass vanish and trip the guard. The `meta` checkpoint records pass and phase boundaries for progress reporting and stall detection.

### Thumbnail and image serving

Thumbnail **jobs** (executed by the job worker, not the scanner) produce two WebP variants per photo eagerly: square 512×512 (uniform grid; `crop=Interesting.ATTENTION`, the salience crop — owner decision O5; the spike verified it yields an exact square) and aspect-preserving 512 px max edge (justified rows), both auto-rotated via pyvips `thumbnail()` (auto-rotation verified on real files; `resize()` must never be used). Screen-fit images (~2048 px max edge, aspect-preserving, auto-rotated) are generated lazily on first view, in the request thread. All derived files are keyed by `(content_hash, variant)` in a two-level shard (`thumb/<first-2-hex-chars>/`, `large/<first-2-hex-chars>/`), so an edited photo — a new hash — can never hit a stale entry, and keys are immutable, so responses carry long-lived immutable cache headers. Concurrent generation of the same entry (request thread racing the worker) writes to a temp file and atomically renames into place. `/media/*` requests verify the hash exists in a live record (never generate for unknown hashes), check the cache, generate on miss, and stream the file via Werkzeug's conditional `send_file`; `/original/<photo_id>` resolves the path *from the database by id* — the client never supplies a path — and streams the original the same way, with full Range/206 support through the proxy (passthrough verified on lighttpd 1.4.85); tombstoned records return 404. X-Sendfile was cut after live verification: this lighttpd build's `mod_proxy` silently ignores the header — a proxied app emitting it serves 200 responses with zero bytes — and the compiled-in `mod_fastcgi` that does implement it is unreachable from a WSGI HTTP backend. At single-user LAN scale, streaming through waitress costs milliseconds per request (wire time dominates); the lighttpd static-alias alternative was weighed and rejected (owner decision O7 — see Alternatives). See `docs/spikes/lighttpd-sendfile.md` and `docs/spikes/pyvips-exif.md`. Cache cleanup (purge, replace) removes a hash's entries only when no live record shares that hash.

### Embedding pipeline and search

The **job worker** (one background thread, the scanner being the other) pops pending `thumb` and `embed` jobs with `thumb` first — a slow or backoff-idle embed queue never delays eager thumbnailing — oldest-first within each kind. Embed jobs are batched to the adapter (a fixed batch of 8 — the spike showed batching gains no throughput, so it is a transport convenience, not a knob), and the worker writes L2-normalized float32 vectors stamped with `model_id`. The embed input is the **original file bytes** (owner decision O2 — LAN transfer is accepted; the alternative of feeding the cached screen-fit image was rejected because it would force eager generation of images nothing otherwise needs). The adapter is one module with a three-function surface — `embed_images()`, `embed_text()`, `model_id` — and is the only place that knows the llama.cpp specifics (endpoint shape, image payload encoding, mmproj flags); it normalizes defensively rather than assuming the server does. `model_id` comes from **config** — the single source of truth — and the adapter **verifies the endpoint against it**: it reads the served model from `GET /v1/models` (verified in the spike; cached 60 s — this check is the reachability probe) and, on a mismatch, raises the retryable endpoint error instead of embedding — a not-yet-switched AI box surfaces as a visible endpoint state on `/api/status` that clears on recovery, never as silently mis-stamped vectors. Failure handling splits endpoint outages from per-file failures. An unreachable endpoint — or one serving a model other than the configured one — trips a **worker-level circuit backoff** (5 s × 2, capped at 5 min, cleared by the next successful probe): the popped jobs return to `pending` untouched, no `attempts` are charged, and the worker keeps processing `thumb` jobs meanwhile — an outage never manufactures `failed_perm` jobs, and thumbnails never queue behind a dead AI box. Per-file failures — thumbnail errors and per-item embed rejections — use per-job retry (30 s × 2, capped at 1 h; after 5 attempts the job becomes `failed_perm`, distinguishable from pending); a failed *batch* is retried **one item per request** to attribute per-item failures — the endpoint returns whole-request errors with no item-level signal, so per-item attribution is achieved by falling back to serial requests for that batch. (Spike-measured on the dev box: ~2.5 s/photo on the iGPU — see `docs/spikes/embedding-endpoint.md`; the full-embed window depends on AI-box throughput, which is unmeasured — re-measure there before the first run.)

The **vector index** keeps search current without restarts: at startup all vectors for the **configured** `model_id` load into a resident float32 numpy matrix (~460 MB at 150K × 768) with a parallel `photo_id` array, **preallocated with capacity for 200,000 rows** (a constant) — an embed writes into spare capacity and publishes a new immutable view, so readers never see a torn matrix and no per-embed copy of ~460 MB ever happens; the only reallocation is a doubling if the library outgrows the capacity (the one transient ~2× residency — see Risks & Limits). The load joins `photos` so the tombstone mask starts correct (`deleted_at IS NOT NULL` → masked — tombstoned photos keep their vectors until purge), and `dim` is never hardcoded: it comes from the first loaded vector, or is allocated lazily on the first embed when the table is empty at boot (first run, or a restart onto a new model while the AI box is down), asserted constant thereafter — swap models need not share EmbeddingGemma 2's 768. The index carries exactly the configured model, no exceptions: a model change swaps spaces at the restart, not mid-queue, so there is no old/new mixing to guard against. A tombstone **masks** its photo from ranking; a revival **unmasks** it (its vector still exists — vectors are not deleted by a tombstone); `mask`/`unmask` publish a copied boolean array rather than mutating the live one, so an in-flight reader never observes a half-updated mask. A search embeds the query text once, computes scores as one full-matrix product (`matrix[:n] @ query` — a zero-copy BLAS `sgemv`, never a fancy-index copy of the matrix), and applies the tombstone mask and any date/folder filter to the resulting 1-D score vector (the filter's candidate ids become a boolean mask over the resident id array; with no date/folder filter active, the SQL candidate fetch is skipped entirely) — ranking in tens of milliseconds (target; the query-embed round trip is extra and unmeasured) — and computes the coverage counts (`embedded`, `total`) **inside the same filter** (`embedded` counts vectors for the configured model — never `embedded_at`), so the results view's three states — endpoint down, nothing embedded in this filter, nothing reached the similarity floor — are always accurate. Photos without a current-model embedding are silently excluded from ranking.

**Model change is owner-initiated and flips at the start** (owner decision O1; the flip-at-start shape is in the Decision Register): one endpoint serves one model, so the owner starts the new model on the AI box and restarts with the new `embedding.model_id` — from that restart the index, the queue, and query embedding all use the new model. Startup queues re-embed jobs for every photo lacking a vector for the configured model (deduplicated by the pending-jobs index; a restart mid-change re-queues only the remainder). `/api/status` reports re-embed coverage; the owner calls `POST /api/admin/model/cutover` when satisfied — it rewrites `active_model_id` and deletes the old-model vectors, and until it is called, reverting the config and restarting restores old-model search. No threshold, no automatic switch, no dual endpoints, never two resident matrices.

### API surface (JSON)

| Endpoint | Purpose |
|---|---|
| `GET /api/photos?cursor&limit&order&after&before` | One timeline window; cursor is an opaque base64url of `"<date_taken>|<photo_id>"`; `limit` default 100, max 500; `order` is `asc` or `desc` (default `desc` = newest first); optional day-inclusive `after`/`before` bounds let the client jump to a date in one request |
| `GET /api/folders` | Folder tree derived at query time (memoized in-process, invalidated on scan commit): `GROUP BY` path prefixes over live photos; each node carries its recursive newest `date_taken`; folders with no photos anywhere beneath them are hidden; target < 200 ms warm |
| `GET /api/folders/<id>/photos?cursor&limit&order` | Timeline window scoped to one folder subtree (`order` as on `/api/photos`) |
| `GET /api/search?q&folder&after&before&limit` | Filtered, similarity-ranked results. Endpoint down → `503` with the error shape and `state: "endpoint_down"` (never an empty result set). Otherwise returns ranked cards (each with `score`) plus filter-scoped `coverage: {embedded, total}` and `state: "ok" \| "no_matches" \| "empty_filter"` (semantics in the response contract, including the `min_score`/`min_matches` top-up). `after`/`before` are day-inclusive ISO dates; `folder` is a folder id |
| `GET /api/status` | Scan progress/checkpoint, queue depth, error counts by stage, ignored-file count, cache size, active and configured `model_id`, re-embed coverage during a model change, adapter reachability and served-vs-configured model, last successful embed, `scan_stalled` flag, guard state |
| `POST /api/admin/guard/resume` | Manual acknowledgment after a mass-delete guard trip |
| `POST /api/admin/model/cutover` | Owner-initiated model cutover (see Embedding pipeline) |
| `GET /media/thumb/<hash>.<variant>.webp` · `GET /media/large/<hash>.webp` | Derived images, streamed with immutable cache headers, generated on miss |
| `GET /original/<photo_id>` | Original file by database id, streamed with Range/206 support; tombstoned → 404 |

**Response contract.** Every JSON response uses one error shape, `{"error": {"code": "...", "message": "..."}}`, for all failures including 404 and 500. A photo card is `{"photo_id", "content_hash", "width", "height", "date_taken", "date_source", "thumb_sq", "thumb_as", "large"}` — the three media fields are prebuilt URLs, and `width`/`height` are required by the justified-rows layout before images load (stored post-EXIF-orientation, so a portrait photo's box matches its rotated thumbnail). A folder node is `{"id", "name", "newest_date_taken", "photo_count", "children"}`; a folder id is the base64url of the library-relative directory path with no trailing slash, **except the library root, whose reserved id is the literal string `"root"`** (an empty path segment cannot appear in the route) — `GET /api/folders` includes the root node, and subtree scoping matches `path >= folder || '/' AND path < folder || '0'` — an index-friendly range over `UNIQUE(path)` (`'0'` immediately follows `'/'` in ASCII, so the range is exactly the subtree): case-sensitive, separator-safe (`/foo` does not match `/foobar` or `/FOO/`), no wildcard metacharacters to escape (a folder named `2021_Italy` matches its `_` literally; `LIKE` would treat it as a wildcard and fold case, and `substr()` would wrap the indexed column and force a scan). The root matches everything; a folder node's `photo_count` is recursive, matching the subtree scoping. Search `state` means: `"ok"` — at least one photo scored ≥ `embedding.min_score` (a match); `"no_matches"` — zero photos reached `min_score` while `coverage.embedded > 0` (things were searchable, nothing matched well — the response still carries the top `min_matches` fallback results, and the UI says so); `"empty_filter"` — `coverage.embedded == 0` (nothing embedded inside the active filter). When fewer than `embedding.min_matches` photos reach the floor, the result list is topped up with the next-closest photos up to `min_matches` either way, so a floor set too high never hides the library behind an empty page. Search result cards carry `score` in addition to the photo-card fields. Day-inclusive date filters compare on the raw ISO string, half-open — `date_taken >= after AND date_taken < before + 1 day`, the upper bound computed before the query — so no function wraps the indexed column. A cursor encodes the sentinel date as its literal ISO string. Grid style and timeline order are **client-side settings persisted in localStorage** — no server keys, no settings endpoint (owner decision: single user, single browser).

### Security

Proxy password authentication at lighttpd — digest on the target server, reusing its existing htdigest setup; basic would be equivalent — is the only authentication in the MVP. The app ships no authentication code, and the JSON API is consumable by a future Android client with the same credentials. All file serving resolves paths from the database (by id or by content hash), never from client input, which makes path traversal structurally impossible; the library root and cache root are the only directories the app ever touches, and symlinks are skipped at scan time, closing the escape routes. Any exposure past the LAN requires TLS at the proxy, documented in the lighttpd config sample. The embedding channel's transport security follows the configured URL scheme (owner decision O6): an `https://` `embedding.base_url` uses TLS; an `http://` URL is accepted plaintext for LAN deployment, recorded as an accepted risk below. The two admin endpoints accept no request body and sit behind the proxy's digest auth, whose nonces mitigate cross-site replay; there is nothing else to forge.

### Configuration

A single TOML file (stdlib `tomllib`, Python ≥ 3.11), read from `--config` or the `PLAPS_CONFIG` environment variable. **Required — the service refuses to start without them:** `library_root`, `embedding.base_url` (scheme must be `http` or `https`), `embedding.model_id`, and `timezone` (an IANA name; required because a UTC-defaulted server silently mis-sorts the whole library). **Defaulted when unset:** `database_path` (`/var/lib/plaps/plaps.db`), `cache_dir` (`/var/cache/plaps`), `backup_dir` (the database file's parent directory + `/backups`), `embedding.min_score` (0–1 cosine floor; default 0.70, calibrated for EmbeddingGemma 2 from the spike — re-calibrate at a model change), `embedding.min_matches` (≥ 0; default 10 — the top-up count), `scanner.poll_interval` (seconds; ≥ 1; the loop backs off when idle), `scanner.full_sweep_interval`, `scanner.rehash_interval`, `scanner.guard_threshold` (0–100 %), `tombstone.purge_after_days` (≥ 1). The embed batch size is a constant in the adapter, not a config key. Cold start with no file: the service prints the path it expects and exits — no silent defaults for the settings that point at 150K irreplaceable files or that define what the vectors mean.

## Alternatives Considered

*Carried from the brainstorm's `Alternatives Weighed` (the only source for this section), plus three alternatives weighed during drafting and review: gunicorn, separate worker processes, and lighttpd static-serving of the media cache. X-Sendfile appears below as a rejected alternative too — it was the brief's adopted serving design, reclassified after the live-verified negative spike, and is recorded here because that is where a reader learns the design changed after brainstorming.*

### Existing solutions

*   **Immich (external libraries, read-only mode)**
    *   **Pros**: Closest existing feature set including true semantic search; registers on-disk paths without touching originals.
    *   **Cons**: Docker + TypeScript stack; heavy for one user; the owner does not own or want to maintain the code.
    *   **Reason for Rejection**: Stack and ownership. Its external-library, read-only ingestion model is the blueprint this design copies.
*   **LibrePhotos**
    *   **Pros**: Python, in-place scanning, semantic search already implemented.
    *   **Cons**: Django + Postgres + worker fleet, Docker-first operations, multi-user machinery.
    *   **Reason for Rejection**: The surrounding stack exceeds the cost of greenfield for a single user. Its embedding indexer is the most relevant reference implementation.
*   **PhotoPrism**
    *   **Pros**: Mature, decent UI, in-place import.
    *   **Cons**: Label/keyword search rather than joint text+image embeddings; expects a specific directory layout.
    *   **Reason for Rejection**: No true semantic search and rigid structure expectations.
*   **PiGallery2**
    *   **Pros**: In-place scanning with a directory-mtime scan cache; npm-installable (not Docker-only).
    *   **Cons**: NodeJS; no semantic search.
    *   **Reason for Rejection**: NodeJS and the missing centerpiece. Its stat-pruning scanner design is borrowed.
*   **Photoview**
    *   **Pros**: Go single binary, in-place scan, face grouping.
    *   **Cons**: Go; no semantic search.
    *   **Reason for Rejection**: Language and missing centerpiece; cited as evidence the niche is viable.
*   **Photofield**
    *   **Pros**: Very fast zoomable timeline over a raw filesystem.
    *   **Cons**: Go; no semantic search.
    *   **Reason for Rejection**: Language and missing centerpiece; reference for virtualized-timeline UX.

### Platform and language

*   **Go instead of Python**
    *   **Pros**: Single binary, better raw concurrency.
    *   **Cons**: Maintainability preference dominates at this scale; heavy lifting (libvips, hashing) happens in C from Python anyway.
    *   **Reason for Rejection**: Owner readability/maintainability preference. Kept as a profiling-gated fallback via clean module boundaries.
*   **FastAPI/Starlette instead of Flask**
    *   **Pros**: Async, validation, modern ecosystem.
    *   **Cons**: A single-user, read-mostly JSON + file-serving workload does not need async; more conceptual surface than Flask.
    *   **Reason for Rejection**: Simplicity; the owner prefers the simplest framework.
*   **gunicorn instead of waitress** *(weighed during drafting)*
    *   **Pros**: The de-facto standard, extensive docs.
    *   **Cons**: Requires a C toolchain on some installs; more process-model knobs (worker classes, reload behavior) than this design needs.
    *   **Reason for Rejection**: waitress is pure Python, one dependency, thread-based — matching the one-process threading model exactly.
*   **Separate worker processes (web + scanner/embedder as two systemd units)** *(weighed during drafting)*
    *   **Pros**: A web crash does not kill background work and vice versa.
    *   **Cons**: Cross-process coordination for the single SQLite writer; two units to operate; contradicts the brief's shared-writer-connection decision.
    *   **Reason for Rejection**: Crash-resume via the jobs table and scan checkpoint achieves the same recovery with half the operational surface.

### Storage and search

*   **FAISS/hnswlib ANN index**
    *   **Pros**: Sub-linear search at very large scale.
    *   **Cons**: Brute force over 150K vectors is already tens of milliseconds; an ANN index is a compiled dependency plus tuning surface.
    *   **Reason for Rejection**: No payoff below ~1M photos. Revisit at that scale.
*   **sqlite-vec**
    *   **Pros**: Vector search inside SQLite.
    *   **Cons**: Does not accept float16; once vectors are resident in a float32 numpy matrix at startup, it adds nothing over brute force.
    *   **Reason for Rejection**: Redundant with the resident-matrix design.
*   **Separate vector store / sidecar files**
    *   **Pros**: Purpose-built storage.
    *   **Cons**: Breaks the single-file backup story; one more dependency to own.
    *   **Reason for Rejection**: SQLite blobs keep backup and restore to one file.
*   **Celery/Redis job queue**
    *   **Pros**: Battle-tested workers, retries, dashboards.
    *   **Cons**: A broker process and a result backend to operate forever, for one queue with two job kinds.
    *   **Reason for Rejection**: A SQLite job table plus a worker loop does the same job with no additional moving parts.

### Pipeline and watching

*   **inotify/fsnotify file watching**
    *   **Pros**: Event-driven, instant detection of the exact file changed, zero idle cost.
    *   **Cons**: Watches are per-directory (~10–20K needed here) against a `max_user_watches` sysctl that commonly defaults lower — exceeding it produces `IN_Q_OVERFLOW`, an event that says "you missed things" without saying what, and the remedy is a full rescan. No reliable events on FUSE filesystems (NTFS/exFAT external disks — plausible photo-archive media). Large imports emit event storms that need coalescing. MOVED_FROM/MOVED_TO cookie-pairing duplicates the scanner's same-pass hash re-link. Most decisively: a full reconcile pass is mandatory regardless (startup, overflow recovery, the daily sweep backstop), so a watcher is a second, parallel change-detection path that must implement identical identity semantics — not a replacement for the scanner.
    *   **Reason for Rejection**: A watcher would be a second change-detection path to maintain, with the same identity semantics as the scanner; when it fails, the daily sweep hides the failure; and the faster pickup it buys serves no single-user workflow. Polling with stat-pruning plus the daily full sweep detects everything inotify would, plus what it would not (FUSE disks, changes while the service is down). Revisitable as an optional accelerator (see Future Improvements).
*   **Pillow for imaging**
    *   **Pros**: Ubiquitous, pure install path.
    *   **Cons**: Slower than pyvips, holds the GIL during decode, no streaming decode.
    *   **Reason for Rejection**: pyvips is faster, releases the GIL, and streams — all three matter for 150K files and a threaded server.
*   **Tombstone-free immediate deletes**
    *   **Pros**: One fewer record state; simplest scanner.
    *   **Cons**: An accidental mass delete costs a full rescan plus a multi-day re-embed.
    *   **Reason for Rejection**: A `deleted_at` column with a purge delay is cheap insurance for the most expensive failure mode.
*   **A single file-lifecycle state enum**
    *   **Pros**: One column tells the whole story.
    *   **Cons**: Conflates pipeline stages with events; cannot express hash re-linking; transition rules cannot be written consistently.
    *   **Reason for Rejection**: Per-stage nullable columns with derived predicates.
*   **A scanner-maintained folders table**
    *   **Pros**: Folder queries are one indexed read.
    *   **Cons**: Denormalization that drifts (ghost folders after renames, stale empty dirs); reconciliation logic in the scanner forever.
    *   **Reason for Rejection**: Derive the tree from indexed paths at query time — 150K rows is well within SQLite's reach, and a derived tree cannot drift.

### Media serving

*   **X-Sendfile/X-Sendfile2 for media and originals** *(weighed and cut during the spike, 2026-10-30)*
    *   **Pros**: Removes Python from the byte path entirely; the web server streams from disk.
    *   **Cons**: On the target lighttpd (1.4.85), `mod_proxy` silently ignores the header — a proxied backend emitting X-Sendfile serves 200 responses with zero bytes (verified live). `mod_fastcgi` implements X-Sendfile/X-Sendfile2 (compiled into this server's binary), but using it would require the app to speak FastCGI, which no maintained Python WSGI server does.
    *   **Reason for Rejection**: Verified broken on the target platform for this stack. Werkzeug conditional streaming through a 16-thread waitress pool handles the load, and at single-user LAN scale the byte path through Python costs milliseconds per request.
*   **lighttpd static-alias serving of `/media/` from the cache directory** *(weighed during review, 2026-10-31)*
    *   **Pros**: Removes Python from the thumbnail byte path entirely — the cache is immutable, hash-keyed, nearly ideal static content.
    *   **Cons**: Works cleanly only against a complete cache, and screen-fit images are lazy by decision (owner decision O3), so first views would 404 or need an app fallback; cache-miss generation still requires the app; extending it to originals would bypass resolve-by-id and expose the library's directory layout in URLs.
    *   **Reason for Rejection**: Owner decision O7 — the streaming cost through waitress is accepted; lazy screen-fit conflicts with a complete static cache.

### Product and interface decisions

*   **Remote/network mount support**
    *   **Pros**: NAS-hosted libraries.
    *   **Cons**: Mount-health machinery, inotify alternatives, latency variance.
    *   **Reason for Rejection**: Forced complexity for little owner benefit. Local disks only.
*   **Server-rendered HTML (htmx-style)**
    *   **Pros**: Less client code for three views.
    *   **Cons**: A virtualized timeline over 150K items wants client rendering; a JSON API also serves the future Android client.
    *   **Reason for Rejection**: Client-side rendering against a JSON API stands on its own.
*   **UI lock until the first index completes**
    *   **Pros**: No one sees a partial library.
    *   **Cons**: The two candidate unlock conditions (metadata scan vs. embedding pass) differ by ~40 hours, and a scanner crash mid-scan would leave the app locked with no way in.
    *   **Reason for Rejection**: Serve what is indexed, with a dismissible progress indicator and visible error states.
*   **App-level login page (cookie sessions) in the MVP**
    *   **Pros**: A custom login page; groundwork for per-user features.
    *   **Cons**: Two auth mechanisms for one user; session handling to maintain forever; password auth at the proxy already covers the threat model and serves a future Android client.
    *   **Reason for Rejection**: Proxy password auth only in the MVP. The login page moves to the backlog; share links or a second user are its trigger.
*   **Coverage banner as a subsystem**
    *   **Pros**: Constant visibility of embedding coverage.
    *   **Cons**: A permanent UI element for a transient state.
    *   **Reason for Rejection**: One count line in the results view (adopted) carries the signal.
*   **UTC-normalized timestamp storage**
    *   **Pros**: Correct cross-timezone ordering.
    *   **Cons**: Timezone-inference machinery for a single-user, effectively single-timezone library; a full re-index to change later either way.
    *   **Reason for Rejection**: Local wall-clock in a configured server timezone. The map-view phase can revisit per-photo GPS-derived zones.
*   **One photo per content with a locations table (vs. one record per path)**
    *   **Pros**: Exact duplicates display once.
    *   **Cons**: More schema; split delete semantics (deleting one of two copies touches shared identity).
    *   **Reason for Rejection**: The owner accepts duplicates displaying twice (any dedup is backlog); one record per path with a non-unique hash is simpler and keeps re-linking trivial.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Privacy.** The library is irreplaceable personal data. The app treats the library as read-only (verified by tests asserting bytes and mtimes unchanged); the only outbound network call is to the configured embedding endpoint; no telemetry. Auth is proxy password authentication at lighttpd — digest on the target server's existing htdigest setup; basic would be equivalent — and the JSON API is consumable by a future Android client with the same credentials. The app itself listens on localhost only; any LAN/WAN exposure requires TLS at the proxy, stated in the config sample. The embedding channel follows the configured URL scheme (O6): `https://` uses TLS, `http://` is accepted plaintext for LAN — an accepted risk, below. Path traversal is structurally prevented (all file serving resolves paths from the database, never client input); symlinks are never followed.
*   **Performance & Latency.** Targets on the reference hardware (Ryzen 5 5700G, 32 GB): timeline window requests under ~100 ms warm; folder tree under ~200 ms warm (memoized, invalidated on scan commit); search *ranking* in tens of milliseconds warm (resident float32 matrix — end-to-end adds the query-embed round trip, unmeasured); thumbnail-cache hits streamed by waitress in milliseconds (LAN wire time dominates); cache-miss generation is the only slow path; the 16-thread waitress pool keeps timeline requests under ~100 ms during a large original download (covered by the concurrency stress test). First full ingest is a one-time cost the owner accepts: hours for scan + thumbnails, and an embed window to be measured on the AI box (dev-box worst case ~4 days at 2.5 s/photo). Resident memory stays under ~1 GB in steady state (the vector matrix is preallocated for 200K rows, ~586 MB, plus pyvips working set); a model change never holds two matrices — the index loads the new model's vectors only — and the one planned excursion, a transient ~2× matrix when capacity doubles, happens only if the library outgrows 200K photos (see Risks & Limits). Disk: database roughly 1–2 GB including vectors; derived cache roughly 8–14 GB for the two 512 px thumbnail variants at 150K photos, with lazy 2048 px screen-fits adding more only as photos are viewed (tens of GB if most of the library is eventually opened).
*   **Rollout & Rollback.** Single-user service, no feature flags: rollout is a systemd unit restart; rollback is restoring the previous unit and, if needed, the last database backup. Schema evolution is one-way migrations gated by `PRAGMA user_version`. Database loss costs a rescan plus a multi-day re-embed — the stated reason for a scheduled `VACUUM INTO` backup (`plaps backup` subcommand; a systemd timer sample ships with the unit) and a documented restore path (stop service, restore file, restart, scanner reconciles; the cache is regenerable and freely deletable).

## Risks & Limits

Accepted limitations of this design, stated so a reader never has to reconstruct them:

*   **Bulk-copied files without EXIF date to the copy event.** The mtime fallback uses the file's modification time; files restored from backup or copied between disks land at the copy date. This is the most common "my timeline is wrong" report in self-hosted galleries, accepted for a single-user library.
*   **Metadata-only edits are full replaces.** A lossless EXIF rotation or metadata strip changes file bytes, so it hard-deletes and re-adds the record and re-embeds the photo; a bulk metadata edit over the library means a day-scale re-embed. A pixel-level hash could refine this (backlog).
*   **Endpoint down = search unavailable.** Query encoding needs the same endpoint as image embedding; there is no local fallback encoder by decision. The app reports a distinct endpoint-down state.
*   **During a model change, search covers only the re-embedded subset.** The change flips at the start (one endpoint serves one model), so coverage drops to near zero at the restart and grows back over the re-embed window; the results-view coverage line shows it, exactly as during first ingest. Accepted in exchange for no dual model servers, no extra config keys, and never two resident matrices.
*   **A capacity doubling transiently doubles the matrix.** The index preallocates 200,000 rows (~586 MB); crossing that copies into a doubled buffer (~1.2 GB transient). Accepted — the floor leaves 50K photos of headroom over the current library, so it should never fire.
*   **`http://` embedding is plaintext on the LAN.** Every original is base64-shipped to the AI box unencrypted (O6). Accepted for a trusted LAN; use `https://` if that stops being true.
*   **Duplicates display twice and embed twice.** One record per path with a non-unique hash; identical files both appear and both cost an embed (~2.5 s each on dev hardware). Any dedup is backlog (O4).
*   **The monthly re-hash pass reads the whole library.** Hundreds of GB of reads, hours on spinning disks, forever. Accepted as the bound on mtime-preserving-copy blindness.
*   **The first-embed window is unverified on the AI box.** The design quotes no number as fact until it is measured there (dev box: ~2.5 s/photo ≈ 4.3 days for 150K).
*   **Search latency targets cover ranking only.** End-to-end latency includes the query-embed round trip, which no spike measured; the target is restated once measured.
*   **`min_score` is model-specific.** The similarity floor is calibrated for one model (spike: matches 0.62–0.75, runner-ups 0.53–0.68) and must be re-judged at every model change. The `min_matches` top-up bounds the damage of a wrong value: results are shown either way, only the `"ok"` vs `"no_matches"` labeling shifts.
*   **Cache-size accounting drifts if the cache is deleted by hand mid-run.** The counter is maintained incrementally and reset at startup only when the cache directory is empty or missing; a partial manual deletion leaves the reported size inflated until the next empty-cache startup. Accepted — the number provisions disk, it does not bill anything.

### Decision Register

Decisions taken after the brainstorm, each with its date and reason:

| Decision | Date | Rationale |
|---|---|---|
| O1 — Model cutover is owner-initiated (`POST /api/admin/model/cutover`); no automated threshold | 2026-10-31 | One subsystem generated seven open decisions; owner-initiated deletes five specs and one NFR contradiction. Runs once every few years — no state machine earned |
| O2 — Embed input is original file bytes | 2026-10-31 | LAN transfer accepted; screen-fit input would force eager generation of images nothing else needs |
| O3 — Thumbnails (`sq`, `as`) eager via the job worker; screen-fit lazy on first view | 2026-10-31 | One user exercises one grid style, but eager thumbs keep `/media/` a pure cache-hit path; lazy screen-fit avoids ~35 GB of unneeded artifacts |
| O4 — Duplicate detection/reporting is backlog, not MVP | 2026-10-31 | No workflow exists to act on a report; duplicates display twice (accepted) until it does |
| O5 — `sq` variant uses `crop=Interesting.ATTENTION` (salience crop) | 2026-10-31 | Owner preference; spike verified ATTENTION yields exact 512×512 |
| O6 — Embedding transport follows the configured URL scheme | 2026-10-31 | `https://` → TLS; `http://` → accepted LAN plaintext, recorded above |
| O7 — lighttpd static-alias for `/media/` rejected; waitress streams all media | 2026-10-31 | Lazy screen-fit conflicts with a complete static cache; resolve-by-id and layout hiding are worth the streaming cost |
| Job worker (one thread) executes both `thumb` and `embed` jobs | 2026-10-31 | The plan had a queue with no consumer; a single worker matches the one-writer model and pyvips releases the GIL |
| Two deletion operations (vanish vs. replace) with explicit cascades | 2026-10-31 | `UNIQUE(path)` makes a tombstoned replace unbuildable; the guard covers vanishes only |
| Vector index: append-and-swap on embed, mask on tombstone | 2026-10-31 | Search otherwise ranks a startup snapshot; immutable swap also solves reader tearing and serves cutover |
| Coverage counts computed inside the active filter; three result states | 2026-10-31 | Global counts mislead exactly when the line matters (first embed, filtered folders) |
| Grid style and timeline order persist in localStorage | 2026-10-31 | Single user, single browser; no server settings surface to maintain |
| `model_id` authority is config | 2026-10-31 | Endpoint-side swaps surface as per-item failures rather than silently mis-stamped vectors |
| Batch failure → serial per-item retry for attribution | 2026-10-31 | The endpoint has no item-level error signal; per-item truth requires per-item requests on failure |
| Backup relies on `VACUUM INTO`'s WAL snapshot; no writer pause, no cross-process coordination | 2026-10-31 | `plaps backup` runs in its own process (systemd timer); a pause would require IPC nobody wants, and the snapshot needs none |
| Timeline virtualization is hand-rolled; no vendored library | 2026-10-31 | One less dependency to own; a vendored substitute remains a future option if the hand-rolled one proves costly |
| The library root's folder id is the reserved string `"root"` | 2026-10-31 | An empty path segment cannot appear in the `/api/folders/<id>/photos` route |
| A model change flips at the start: the endpoint serves the new model from the restart, and search covers the re-embedded subset until cutover | 2026-11-01 | One endpoint serves one model, so "old-model results throughout the overlap" needs two live servers and two config keys for a once-every-few-years event (Stage 9 re-run blocker); the coverage line already communicates partial coverage — it was built for first ingest |
| Subtree scoping is the range `path >= folder \|\| '/' AND path < folder \|\| '0'` (updated 2026-11-01 from the `substr` form after design review 1) | 2026-11-01 | `LIKE` folds ASCII case and treats `_`/`%` as wildcards; `substr()` additionally wraps the indexed column and defeats the `UNIQUE(path)` B-tree — the range is exactly the subtree because `'0'` immediately follows `'/'` in ASCII |
| Search relevance: `embedding.min_score` floor + `embedding.min_matches` top-up (owner decision) | 2026-11-01 | Without a floor, top-N ranking always returns results, making the `"no_matches"` state unreachable; a floor alone is fragile (spike: matches 0.62–0.75, runner-ups 0.53–0.68 overlap any threshold), so the top-up displays the closest photos anyway — a wrong floor degrades labeling, never visibility |
| Endpoint outages trip a worker-level circuit backoff; per-job `attempts` are charged only on per-file failures | 2026-11-01 | An hours-long AI-box outage otherwise drains healthy jobs into `failed_perm` batch by batch; and thumbnails must not queue behind a dead embed endpoint |
| Same-pass renames/moves stage in `scan_staged` until vanish resolution | 2026-11-01 | Immediate insertion at the new path collides with `UNIQUE(path)` at re-link, displays the photo twice mid-scan, and re-enqueues embeds for content that already has vectors |
| FHS paths: database `/var/lib/plaps/plaps.db`, cache `/var/cache/plaps`, backups beside the database (`backup_dir`) | 2026-11-01 | A dedicated service user cannot write under `/etc/plaps`, and 8 × ~1.5 GB of snapshots do not belong in `/etc` |

## Additional Resources

*   `../../concepts_brief.md` — the brainstorm artifact this design is based on (decisions, prior-art corrections, testing strategy).
*   EmbeddingGemma 2: `huggingface.co/google/embeddinggemma-2`; llama.cpp-ready weights: `huggingface.co/ggml-org/embeddinggemma-2-GGUF`.
*   Reference implementations studied: Immich external libraries (ingestion blueprint), LibrePhotos (embedding indexer), PiGallery2 (stat-pruned scanner), Photoview and Photofield (in-place gallery UX).
*   Spike results (all run 2026-10-30): `../spikes/embedding-endpoint.md` (verified EmbeddingGemma 2 HTTP contract, failure modes, batching, 8/8 retrieval on real photos), `../spikes/lighttpd-sendfile.md` (X-Sendfile negative result; Range passthrough; target lighttpd config), `../spikes/pyvips-exif.md` (EXIF sub-IFD naming, auto-rotation, square-crop, stripped-EXIF behavior).
*   Review record (2026-10-31): `consolidated_review_report.md` and its five stage reports in the workspace root; Stage 9 re-run record (2026-11-01): `stage9_rerun_report.md`, same root; external design review (2026-11-01): `design_review_1.md`, same root — all findings resolved, see Decision Register.

## Future Improvements

*   **Deferred decisions with triggers** (from the brief): browser-side query encoding via transformers.js (wanted as a novelty, not a need; requires model-version coupling); pixel/perceptual hash to survive metadata-only edits (trigger: the first real lossless-rotation re-embed); app-level cookie login page (trigger: share links or a second user); duplicate detection, reporting, and any dupe-handling workflow (trigger: displayed duplicates becoming an annoyance in practice; O4); Android auto-backup (the settled one-record-per-path model defines how its duplicates are handled); Go rewrite (only if profiling shows Python is too slow — the module boundaries would let it replace individual components).
*   **inotify as an optional change-detection accelerator** (trigger: change-pickup latency ever mattering to the single user): add a watcher in front of the scanner with the polling sweep remaining the source of truth; the scanner's interface does not change, so the addition is isolated.
*   **Later-phase features** (in scope, sequenced after the MVP): content-similarity auto-grouping, face detection and grouping, map view, read-only share links, the Android client. A direct-only folder view (`?recursive=false`, showing just the photos loose in one directory rather than its whole subtree) is deferred with a trigger: the first time needing to see only a parent folder's loose photos becomes a real annoyance — subtree scoping merges descendants by design.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

Greenfield: files marked **(Create)** are new; where a later increment changes an earlier file, the entry carries an explicit **(Modify)** step — a Create-only list cannot express cross-increment changes, so modified files carry the step instead. Increments: **walking skeleton → folder view → search → hardening** (the three spikes already ran; their records are listed at the end). No production code appears in this document; signatures and schemas only.

### Target Files to Create/Modify

#### 1. `pyproject.toml` (Create — walking skeleton)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**: Declares the `plaps` package, Python ≥ 3.11, and the complete dependency surface: `flask`, `waitress`, `pyvips`, `piexif`, `httpx`, `numpy`. Dev extras `pytest`, `pytest-cov`, `ruff` behind a named `dev` extra (`pip install -e ".[dev]"`). Rationale: one manifest is the whole install story (`pip install -e ".[dev]"`); listing every dependency here is what keeps the manifest complete.

#### 2. `README.md` (Create — hardening)

*   **Type / Scope**: Documentation; the product's front door.
*   **Proposed Changes**: The front-door runbook, written as a scannable list:
    *   Install: checkout at `/opt/plaps`; OS packages `libvips` and `lighttpd`; stdlib `python -m venv` at `/opt/plaps/venv`; `pip install -e ".[dev]"`.
    *   Config walkthrough.
    *   systemd and lighttpd setup, including the `/srv/http/plaps → static/` symlink the lighttpd docroot expects, and the dev-time variant: point the same block's docroot at a repo checkout.
    *   The one-line venv recreation for after a distro Python major upgrade (config, database, and cache are unaffected — the venv is disposable by design).
    *   Backup/restore: `plaps backup` on the systemd timer — a timestamped snapshot under `backup_dir` (default: beside the database), the 8 most recent kept; restore = stop → restore file → restart → scanner reconciles; the cache is regenerable.
    *   How to run tests.

    Rationale: ease of installation is a stated design goal, and the restore path only counts if it is written down.

#### 3. `config.example.toml` (Create — walking skeleton)

*   **Type / Scope**: Configuration template.
*   **Proposed Changes**: Every key from Technical Plan → Configuration — required keys marked "no default — required", defaulted keys with their pinned value — each with a one-line comment. Rationale: the config surface is documented by example, and the service's validation errors can point at this file.

#### 4. `systemd/plaps.service` (Create — hardening)

*   **Type / Scope**: Process definition.
*   **Proposed Changes**: `ExecStart=/opt/plaps/venv/bin/python -m plaps --config /etc/plaps/plaps.toml` (absolute venv path — bare `python` resolves system Python where the package is not installed; the env var alternative is `PLAPS_CONFIG`), dedicated `User=`, `Restart=on-failure`. Rationale: crash-resume makes automatic restart safe — a killed scanner resumes from its checkpoint rather than corrupting state.

#### 5. `systemd/plaps-backup.timer` (Create — hardening)

*   **Type / Scope**: Timer definition (backup cadence).
*   **Proposed Changes**: `OnCalendar=weekly`, `Persistent=true`, `Unit=plaps-backup.service`. Rationale: the backup cadence needs an explicit trigger; `Persistent=true` runs a missed backup after downtime instead of skipping the week.

#### 6. `systemd/plaps-backup.service` (Create — hardening)

*   **Type / Scope**: Process definition (backup); the unit the timer activates.
*   **Proposed Changes**: `Type=oneshot`, `ExecStart=/opt/plaps/venv/bin/python -m plaps backup --config /etc/plaps/plaps.toml` — same absolute-venv and config-path rules as the service unit, so the backup reaches the same database the service writes. Rationale: a systemd timer cannot execute anything by itself; it activates this same-named `.service` unit.

#### 7. `lighttpd/plaps-lighttpd.conf` (Create — hardening)

*   **Type / Scope**: Reverse proxy configuration sample.
*   **Proposed Changes**: Proxy-auth block (the only auth layer; digest, reusing the server's existing htdigest userfile), `mod_proxy` to the app on `127.0.0.1:8096`, `static/` served directly from disk, commented TLS block for any exposure past the LAN. Rationale: the security model lives in this file; shipping it as a reviewed sample prevents a misconfigured proxy from becoming the deployed configuration. Shape verified against the target server — see `docs/spikes/lighttpd-sendfile.md`.

#### 8. `plaps/__init__.py` (Create — walking skeleton)

*   **Type / Scope**: Package marker; version string only.

#### 9. `plaps/config.py` (Create — walking skeleton)

*   **Type / Scope**: Configuration loading and validation.
*   **Proposed Changes**: Loads the TOML file with stdlib `tomllib`, applies defaults for unset optional keys, validates types and ranges (`poll_interval ≥ 1 s`; intervals ≥ 1; `guard_threshold` 0–100 %; `purge_after_days ≥ 1`; `min_score` 0–1; `min_matches ≥ 0`; `base_url` scheme `http`/`https`; `timezone` a valid IANA name), and **exits with a clear message if any required key is absent** — `library_root`, `embedding.base_url`, `embedding.model_id`, `timezone`. Exposes a frozen settings object. Rationale: no silent defaults for the settings that point at irreplaceable files or define what the vectors mean.

#### 10. `plaps/db.py` (Create — walking skeleton)

*   **Type / Scope**: SQLite access layer.
*   **Proposed Changes**: Connection factory (per-thread reader connections in WAL mode), the single writer connection guarded by a lock — used by scanner, worker, and admin actions alike — `PRAGMA user_version`-gated one-way migrations (an ordered list of schema steps, including the `dirs` and `scan_staged` tables and both jobs partial indexes), and a `VACUUM INTO` backup helper. **The writer lock is never held across disk reads, hashing, pyvips work, or HTTP calls** — the scanner commits in small per-directory batches, so a request thread's cache-size update or an admin action never waits behind a scan's I/O. The backup needs **no writer pause and no cross-process coordination**: `plaps backup` runs in its own process (invoked by the systemd timer) and `VACUUM INTO` reads a consistent WAL snapshot; a `busy_timeout` handles write contention. `VACUUM INTO` errors if the target file already exists, so the helper writes a **timestamped** target — `<backup_dir>/plaps-<YYYYMMDD-HHMMSS>.db`, `backup_dir` defaulting to the database file's parent + `/backups` — and prunes to the 8 most recent snapshots (a constant, like the batch size). Rationale: WAL + one writer is the entire concurrency story; centralizing it here means no other module ever opens a connection with the wrong pragmas — and the backup mechanism is specified without an impossible writer pause.

#### 11. `plaps/exif.py` (Create — walking skeleton)

*   **Type / Scope**: Pure EXIF-date functions.
*   **Proposed Changes**: `date_taken(data: bytes) -> (datetime | None, error | None)` — reads `DateTimeOriginal` via piexif, parses `YYYY:MM:DD HH:MM:SS`, treats malformed blocks as a returned error, never raises. The mtime fallback and its timezone conversion (epoch → local wall-clock via `zoneinfo` with the configured timezone) live in the scanner, not here — this module is pure bytes-in, value-out. Rationale: the most malformed-input-heavy logic in the system is isolated as pure functions so the unit tests can hit bad inputs without files or a database.

#### 12. `plaps/scanner.py` (Create — walking skeleton)

*   **Type / Scope**: The background scanner thread.
*   **Proposed Changes**: One pass function with three modes — stat-pruned (default cycle), full sweep (daily), re-hash (monthly) — selected by comparing each interval's `last_run` timestamp in `meta`; when more than one is due, the re-hash runs in preference to the full sweep, and the full sweep in preference to the pruned pass. **Each pass runs in two phases** (the checkpoint respects the boundary): phase one walks `library_root` (skipping symlinks), applies the extension allowlist, skips `stat` in unchanged-mtime directories using the `dirs` table (pruned mode only; refresh `dirs` every pass — a directory's mtime is recorded only after all of its files are processed), and for new/stat-changed files reads once, hashes via `hashlib.file_digest` (`digest_size=32`), reads EXIF date/orientation and dimensions from the same bytes via `exif.py` (swapping `width`/`height` for orientation 5–8), and **inserts immediately only when the hash matches no live row** — upserting the `photos` row and enqueuing `thumb` + `embed` jobs (insert-or-ignore) — else stages the file in `scan_staged` (same-pass rename/move or duplicate); phase two resolves vanishes against the seen set (a live photo vanishes only if unseen **and** its directory was walked or no longer exists): re-link via staged path (`UPDATE`, keep `photo_id`; tombstone instead if another live row occupies the path), insert staged duplicates whose live twin survived, clear `scan_staged`, then handle remaining vanishes. Implements the two deletion operations exactly as specified in the data model: **vanish** (same-pass re-link check → tombstone + vector-index mask; revival within the purge window keeps `photo_id`, rewrites path, unmasks, and re-enqueues missing jobs; most-recently-tombstoned wins on hash ties; purge drops expired tombstones plus their cache entries, only when no live record shares the hash) and **replace** (hard-delete + cascade — including the vector-index mask of the old id — + insert). An interrupted pass restarts (phase one is idempotent and pruned; phase two never sees a partial seen set). The mtime fallback is formatted `%Y-%m-%dT%H:%M:%S` after the `zoneinfo` conversion; `date_source = 'unknown'` (sentinel date) is assigned when `st_mtime <= 0` or the epoch is out of range. The mass-delete guard covers vanishes only, is armed from the second pass (first pass sets the baseline; threshold 0 disables it), and latches a `meta` state only `POST /api/admin/guard/resume` clears, after which the next pass re-evaluates. Rationale: this module owns every identity rule in the design; all of it is exercised through `tests/test_scanner.py`.

#### 13. `plaps/images.py` (Create — walking skeleton)

*   **Type / Scope**: pyvips image derivation.
*   **Proposed Changes**: `derive(data: bytes, content_hash, variant) -> cache path` for `sq` (512×512, `crop=Interesting.ATTENTION`) and `as` (512 px max edge), and `screenfit(data: bytes, content_hash) -> cache path` (2048 px max edge), all via pyvips `thumbnail_buffer()` on bytes the caller already holds, auto-rotated (never `resize()`), encoded WebP at quality 80, written to a temp file and atomically renamed into `thumb/<2-hex>/<hash>.<variant>.webp` / `large/<2-hex>/<hash>.webp`; `ensure(content_hash, kind)` resolves a live record's path from the database, **reads the file once**, and passes those bytes to the variant functions. The libvips operation cache is disabled at import (`pyvips.cache_set_max(0)`) — the on-disk cache makes it redundant, and its default retention grows RSS by hundreds of MB in a long-running threaded server. Rationale: keyed by content hash + variant, so staleness is impossible by construction; every key is immutable; the atomic rename makes concurrent generation safe; and the signatures take bytes, not paths, so generating every variant for a photo costs one disk read, not one per variant.

#### 14. `plaps/embed/__init__.py` (Create — search increment)

*   **Type / Scope**: Package marker.

#### 15. `plaps/embed/adapter.py` (Create — search increment)

*   **Type / Scope**: The embedding endpoint client; the only module that knows llama.cpp specifics.
*   **Proposed Changes**: `embed_images(list[bytes]) -> list[vector]`, `embed_text(str) -> vector`, `model_id` (read from config — the single source of truth); payload encoding, fixed batch of 8, and endpoint path exactly as recorded in the embedding spike (image parts nested inside a `content` array — the flat shape 400s, a data-URI string is silently tokenized as text); defensive L2 normalization of every returned vector; typed errors distinguishing "endpoint unreachable" (retryable) from "endpoint rejected item" (per-file failure); and a **served-model check** — `GET /v1/models` (verified in the spike), result cached 60 s, compared against the configured `model_id`, with a mismatch raising the retryable endpoint error so a not-yet-switched AI box backs off and resumes instead of yielding mis-stamped vectors. Rationale: the adapter is the quarantine boundary for every llama.cpp quirk; the typed error split is what lets the worker retry the right failures without marking rejected files as retryable.

#### 16. `plaps/worker.py` (Create — walking skeleton; Modify — search increment)

*   **Type / Scope**: The background job worker thread — the single consumer of the jobs table.
*   **Proposed Changes**: **Walking skeleton:** pops `pending` `thumb` jobs oldest-first via the dequeue index (`thumb` first once embed jobs exist — a slow or backoff-idle embed queue never delays eager thumbnailing), generates via `images.ensure()`, records `thumbnailed_at` or `thumb_error`, retries per-file failures with exponential backoff (30 s × 2, capped at 1 h; 5 attempts → `failed_perm`), and **deletes successful jobs** — the per-stage columns on `photos` record completion, so `state='done'` rows have nothing to say. **Search increment (Modify):** adds `embed` handling — batches of 8 to the adapter, writes normalized vectors stamped with the configured `model_id`, and upserts each into the vector index (the index always carries the configured model — a model change flips the space at the restart, so there is no cross-model case for the worker to guard against); records `embedded_at` or `embed_error`; on a failed batch, retries the batch's items one request per item to attribute failures. Endpoint-level failures — unreachable, or serving a model other than the configured one — trip a **worker-level circuit backoff** (5 s × 2, capped at 5 min, cleared on the next successful probe): the popped jobs return to `pending` with no `attempts` charged, and `thumb` jobs keep flowing meanwhile. Model-change *detection* belongs to `app.py` at startup (the single owner — config is read once); the worker processes the re-embed jobs that startup queued, alongside initial embeds, through the same table. Rationale: one queue, one consumer — thumb and embed jobs share the worker, the writer connection, and the backoff machinery; no second pipeline to rot, and an AI-box outage never manufactures `failed_perm` jobs.

#### 17. `plaps/search.py` (Create — search increment)

*   **Type / Scope**: Vector index and ranking.
*   **Proposed Changes**: `VectorIndex` loads all vectors for the **configured** `model_id` into a float32 numpy matrix plus a parallel `photo_id` array at startup (joining `photos` so tombstoned ids start masked), **preallocating capacity for 200,000 rows** (a constant; a doubling on overflow is the only reallocation, so an embed never copies the matrix — it writes into spare capacity and publishes a new immutable view); `dim` comes from the first loaded vector or is allocated lazily on the first embed when the table is empty at boot, asserted constant thereafter — never hardcoded; `add(photo_id, vector)` **upserts** (an id may already be present, masked — revival re-uses its existing vector); `mask(photo_id)` / `unmask(photo_id)` hide and reveal a photo, publishing a copied boolean array rather than mutating the live one. There is no `reload`: the index never changes models while running — a model change is a restart. `query(text, folder, after, before, limit)` encodes the text once via the adapter, scores with one full-matrix product (`matrix[:n] @ query` — a zero-copy BLAS `sgemv`, never a fancy-index copy), applies the tombstone mask and the date/folder filter (candidate ids become a boolean mask over the resident id array; no SQL candidate fetch when no filter is active), applies the `min_score` floor with the `min_matches` top-up per the response contract, and returns ranked ids with scores plus coverage counts computed inside the same filter (`embedded` counts vectors for the configured model — never `embedded_at`). Rationale: filters-before-ranking is a mask over a 1-D score vector; brute force over a resident matrix is the whole search engine — and upsert with mask/unmask is what keeps it current between restarts, through tombstones and revivals; the configured model is the only one it ever holds.

#### 18. `plaps/api/__init__.py` (Create — walking skeleton)

*   **Type / Scope**: Flask app factory and the response contract's home.
*   **Proposed Changes**: Builds the app, registers the blueprints below, installs JSON error handlers so every failure — including 404 and 500 — returns `{"error": {"code", "message"}}`, and defines the photo-card and folder-node schemas from the API surface section in one place. Rationale: the JSON API is the client contract; uniform errors and one schema definition are part of it.

#### 19. `plaps/api/photos.py` (Create — walking skeleton)

*   **Type / Scope**: Timeline endpoints.
*   **Proposed Changes**: `GET /api/photos?cursor&limit&order&after&before` — one window over live (`deleted_at IS NULL`) photos ordered by `(date_taken, photo_id)`; `limit` default 100 max 500; `order` `asc`/`desc` default `desc`; optional `after`/`before` day-inclusive ISO dates applied as the response contract's half-open raw-string range, so jumping to a year is one bounded request instead of hundreds of cursor pages; the cursor is base64url of `"<date_taken>|<photo_id>"` (the sentinel date encodes as its literal ISO string) and is opaque to clients. Rationale: the composite cursor is what makes windows stable while the scanner inserts, and the date bounds keep two decades of timeline navigable without pagination UI.

#### 20. `plaps/api/folders.py` (Create — folder view increment)

*   **Type / Scope**: Folder tree endpoints.
*   **Proposed Changes**: `GET /api/folders` derives the tree by grouping live-photo path prefixes (memoized in-process; the memo is invalidated when the scanner commits a pass), computes each node's recursive newest `date_taken`, hides branches with no photos anywhere beneath them, and includes the root node (reserved id `"root"` — see the response contract); a folder id is the base64url of the library-relative directory path with no trailing slash, and subtree scoping matches `path >= folder || '/' AND path < folder || '0'` — an index-friendly range over `UNIQUE(path)` (`'0'` immediately follows `'/'` in ASCII, so the range is exactly the subtree): case-sensitive, separator-safe, no wildcard metacharacters (a folder named `2021_Italy` must not have its `_` match any character, and `/foo` must not match `/FOO/`; the rule lives in `paths.py`). `GET /api/folders/<id>/photos?cursor&limit&order` is the timeline window scoped to the subtree. Rationale: a derived tree cannot drift from disk, and the memo keeps the heaviest read path off the request path.

#### 21. `plaps/api/search.py` (Create — search increment)

*   **Type / Scope**: Search endpoint.
*   **Proposed Changes**: `GET /api/search?q&folder&after&before&limit` — delegates to `search.py`; when the adapter is unreachable (or serving a model other than the configured one), returns `503` with the error shape and `state: "endpoint_down"` (never an empty result set); otherwise returns ranked photo cards — each carrying `score` — plus filter-scoped `coverage: {embedded, total}` and `state: "ok" | "no_matches" | "empty_filter"` (semantics in the response contract, including the `min_score`/`min_matches` top-up); `after`/`before` apply as the contract's half-open raw-string range. Rationale: the three result states and the coverage counts are part of the response contract, not a UI afterthought.

#### 22. `plaps/api/media.py` (Create — walking skeleton)

*   **Type / Scope**: Image and original serving.
*   **Proposed Changes**: `GET /media/thumb/<hash>.<variant>.webp` and `GET /media/large/<hash>.webp` — verify the hash exists in a live record (never generate for unknown hashes), `ensure()` on miss, emit immutable `Cache-Control`, stream via Werkzeug `send_file` (conditional requests; Range/206 handled by Werkzeug, passthrough through the proxy verified); whenever generation creates a cache file, the creating thread — request thread or worker — records its size through the writer connection, so cache-size accounting stays exact even for lazily generated files; the increment is taken only when the target was previously absent (checked under the writer lock), so two threads racing the same miss do not double-count, cleanup subtracts the `st_size` of files it actually unlinks (`0` on `FileNotFoundError`), and the counter resets to 0 at startup when the cache directory is empty or missing. `GET /original/<photo_id>` resolves the path **from the database by id** and streams it the same way; tombstoned or missing records return 404. Rationale: clients never supply paths, so traversal is structurally impossible.

#### 23. `plaps/api/status.py` (Create — walking skeleton; Modify — search increment)

*   **Type / Scope**: Health and administration.
*   **Proposed Changes**: **Walking skeleton:** `GET /api/status` assembles scan progress/checkpoint, queue depth, error counts by stage, ignored-file count (non-allowlist files seen), cache size (maintained incrementally — every thread that creates a cache file records its size through the writer connection; nothing is walked per request), `scan_stalled` (no checkpoint progress for 15 minutes while idle backoff is not active), and guard state; `POST /api/admin/guard/resume` clears the latched guard. **Search increment (Modify):** adds active and configured `model_id`, re-embed coverage during a model change (photos with vectors for the configured model over live photos), adapter reachability and served-vs-configured model (the served-model check is the reachability probe: one `GET /v1/models`, 2 s timeout, result cached 60 s), last successful embed, and `POST /api/admin/model/cutover`. Rationale: `/api/status` is the owner's entire debugging surface.

#### 24. `plaps/app.py` (Create — walking skeleton; Modify — search increment)

*   **Type / Scope**: Composition root.
*   **Proposed Changes**: **Walking skeleton:** wires config → db → scanner + worker (thumb jobs) → Flask app → waitress (16-thread pool, so a large original download never starves timeline requests; listens on `server.listen`, default `127.0.0.1:8096` matching the lighttpd sample — owner-approved 2026-11-04; the thread count stays a constant, not config); handles startup ordering and graceful shutdown (stop accepting, checkpoint scanner, close writer). **Search increment (Modify):** adds the vector index to the wiring (loaded before serving) and model-change detection at startup — the single owner: a configured `embedding.model_id` differing from `meta.active_model_id` queues re-embed jobs for every photo lacking a vector for the configured model, so a restart mid-change re-queues only the remainder, not the whole library. Startup also **prunes `pending` embed jobs whose photo already has a vector for the configured model** — the leftover re-embed jobs of an aborted model change would otherwise spend days re-embedding photos that already have valid vectors. Rationale: one file owns the process's lifecycle so the threading model is visible in a single place.

#### 25. `plaps/__main__.py` (Create — walking skeleton)

*   **Type / Scope**: Entrypoint (`python -m plaps`).
*   **Proposed Changes**: Resolves the config path (`--config` or `PLAPS_CONFIG`), and dispatches subcommands: `serve` (default; calls into `app.py`) and `backup` (calls the `VACUUM INTO` helper — a timestamped snapshot pruned to the 8 most recent, per `db.py`; the trigger the backup cadence needs). Rationale: keeps the systemd unit and manual runs identical, and gives the backup timer something to invoke.

#### 26. `static/index.html` (Create — walking skeleton)

*   **Type / Scope**: Single-page shell.
*   **Proposed Changes**: One HTML file loading the ES modules below; no bundler, no build step, served directly by lighttpd. Rationale: the no-Node constraint made structural.

#### 27. `static/css/plaps.css` (Create — walking skeleton)

*   **Type / Scope**: Styles for both grid styles (`sq` uniform square, `as` justified rows), the viewer, and the progress indicator.

#### 28. `static/js/api.js` (Create — walking skeleton)

*   **Type / Scope**: Fetch wrapper; cursor handling mirroring the server format; photo-card consumption; error-shape surfacing to the UI; localStorage read/write for grid style and timeline order.

#### 29. `static/js/timeline.js` (Create — walking skeleton)

*   **Type / Scope**: Virtualized timeline grid — windowed fetching via IntersectionObserver, layout switching between the two grid styles (persisted via `api.js`), date headers. Virtualization covers the DOM too: off-screen row blocks are unmounted and replaced with measured-height spacers, so tens of thousands of `<img>` elements are never live at once. Date headers double as jump-to-date controls — one `after`/`before`-bounded request reaches a year decades back, where sequential cursor paging would need hundreds of windows. Hand-rolled — no vendored virtualization library (Decision Register).

#### 30. `static/js/status.js` (Create — walking skeleton)

*   **Type / Scope**: First-run progress indicator and stalled-scan error banner, polling `/api/status` and keying the banner on its `scan_stalled` field.

#### 31. `static/js/folders.js` (Create — folder view increment)

*   **Type / Scope**: Folder tree navigation; entering a folder scopes the timeline.

#### 32. `static/js/viewer.js` (Create — walking skeleton)

*   **Type / Scope**: Photo viewer — screen-fit display (lazy, cached after first view), explicit original-download action, keyboard navigation.

#### 33. `static/js/search.js` (Create — search increment)

*   **Type / Scope**: Query box, ranked results grid, the three result states (endpoint down / nothing embedded in this filter / nothing reached the similarity floor — the `"no_matches"` view labels its fallback results as closest matches rather than strong ones) with filter-scoped coverage and per-card scores, active date/folder filter state.

#### 34. `static/js/main.js` (Create — walking skeleton)

*   **Type / Scope**: Hash-based router wiring the views together.

#### 35. `tests/conftest.py` (Create — walking skeleton)

*   **Type / Scope**: Shared fixtures.
*   **Proposed Changes**: A temp-library factory that writes real JPEGs (pixel data generated with pyvips, since piexif can only insert EXIF into existing JPEG bytes; EXIF then added via piexif; plus decoy non-JPEG files) and manipulates mtimes; an app fixture (in-process Flask test client pointed at temp config/DB/cache); a fake embedding adapter with injectable vectors for deterministic ranking. Rationale: every scanner and search test stands on these three fixtures.

#### 36. `tests/test_exif.py` (Create — walking skeleton)

*   **Type / Scope**: Unit — valid, malformed, and missing EXIF, pure bytes-in/value-out; `date_source` tagging, the mtime fallback, and its timezone conversion live in the scanner tests (`test_scanner.py`).

#### 37. `tests/test_config.py` (Create — walking skeleton)

*   **Type / Scope**: Unit — cold-start refusal for each required key; range validation; default application. Rationale: this file guards the settings that point at 150K irreplaceable files.

#### 38. `tests/test_scanner.py` (Create — walking skeleton)

*   **Type / Scope**: API-level scanner behavior via temp trees: add; non-allowlist files ignored and counted; rename/move re-link — staged until vanish resolution, so a moved photo never appears twice mid-scan and never re-enqueues embeds for content that already has vectors; duplicate appearance at a second path (staged, then inserted after its live twin survives); vanish → tombstone; revival within the purge window (new path, kept `photo_id`, re-enqueued jobs; most-recently-tombstoned wins on hash ties); purge after the window (cache entries removed only when no live record shares the hash); in-place edit = replace with the full cascade (old vectors gone, old id unrankable, old jobs dropped, old cache removed-or-shared, new `photo_id`); portrait photos store swapped display dimensions (EXIF orientation 5–8); nested-add discovery under stat-pruning; guard trip (vanish-only; baseline armed from pass two) and manual resume with re-evaluation; symlink skipping; crash mid-pass → the pass restarts pruned and idempotent with no false vanishes and no guard trip; and the originals-untouched assertion (bytes and mtimes unchanged after a full pass).

#### 39. `tests/test_api_photos.py` (Create — walking skeleton)

*   **Type / Scope**: Cursor windows — ordering (including the unknown-date sentinel), bounds, `limit`/`order` validation, day-inclusive `after`/`before` bounds, and stability under concurrent inserts.

#### 40. `tests/test_api_folders.py` (Create — folder view increment)

*   **Type / Scope**: Tree derivation, recursive newest-date computation, empty-branch hiding, subtree scoping (separator-safe: `/foo` does not match `/foobar`; case-sensitive: `/foo` does not match `/FOO/`; literal: `_` in a folder name matches only itself), memo invalidation on scan commit.

#### 41. `tests/test_search.py` (Create — search increment)

*   **Type / Scope**: Synthetic-vector ranking order; filter-before-rank; filter-scoped coverage counts; silent exclusion of un-embedded photos; the three result states (endpoint down → 503), including `"no_matches"` reachability — a query scoring below `min_score` on an embedded filter returns the fallback results and the state — and the `min_matches` top-up (fewer photos above the floor than the count still yields a full list); **index freshness** — a photo embedded after startup is searchable without restart, and a tombstoned photo stops ranking without restart (a replaced photo's old id stops ranking too); model change: with a changed `embedding.model_id`, re-embedded photos become searchable without a further restart while not-yet-re-embedded photos are silently excluded, `POST /api/admin/model/cutover` rewrites `active_model_id` and deletes the old vectors, and reverting the config before cutover restores old-model search with the aborted change's pending jobs pruned.

#### 42. `tests/test_media.py` (Create — walking skeleton)

*   **Type / Scope**: Variant dimensions and formats (square variant exactly 512×512 via ATTENTION crop, aspect variant ≤ 512 px max edge, auto-rotated); cache hit/miss; immutable cache headers; Range request handling (206, `Content-Range`); originals served by id only; tombstoned originals 404; unknown hashes never generated.

#### 43. `tests/test_jobs.py` (Create — walking skeleton; Modify — search increment)

*   **Type / Scope**: **Walking skeleton:** queue order (`thumb` first); per-file backoff (30 s ×2, cap 1 h) and `failed_perm` after 5 attempts for thumb jobs; successful jobs deleted from the table; pending-job dedup across re-hash and revival. **Search increment (Modify):** per-file retry for rejected embed items; **endpoint outage → circuit backoff** — no `attempts` charged, jobs stay `pending`, `thumb` jobs keep flowing, and the queue resumes untouched when the probe succeeds (a multi-outage never manufactures a `failed_perm`); dedup across a model change; mid-batch failure retried per-item, marking only genuinely failed items.

#### 44. `tests/test_status.py` (Create — walking skeleton; Modify — search increment)

*   **Type / Scope**: **Walking skeleton:** `scan_stalled` logic (15-minute window, idle-backoff exemption), guard-state surfacing and the resume transition, cache-size accounting (including a file created by the request thread), error counts by stage, ignored-file count. **Search increment (Modify):** probe caching (2 s timeout, 60 s TTL) including the served-vs-configured model mismatch state, re-embed coverage reporting during a model change, and the cutover endpoint's before/after behavior. Rationale: the entry for `api/status.py` calls this "the owner's entire debugging surface" — it needs its own test file.

#### 45. `tests/test_paths.py` (Create — walking skeleton)

*   **Type / Scope**: Unit — the path jail, symlink skip decisions, folder prefix logic (including the reserved root id), all against `plaps/paths.py`.

#### 46. `plaps/paths.py` (Create — walking skeleton)

*   **Type / Scope**: Pure path functions — jail checks, symlink skip decisions, folder prefix construction and matching, folder-id encode/decode (with the reserved `"root"` id), and content-hash format validation for the `/media/*` routes (exactly 64 hex chars — blake2b-32). Rationale: `tests/test_paths.py` needs one owning module, and the scanner, media, and folders modules all reuse these rules.

#### 47. `tests/test_stress_concurrency.py` (Create — hardening)

*   **Type / Scope**: Threaded browsing requests during a live scanner pass and a large original download; asserts no lock errors, consistent windows, and timeline requests under ~100 ms during the download. The one place nondeterminism is accepted.

#### 48. `tests/test_adapter_contract.py` (Create — search increment)

*   **Type / Scope**: Opt-in contract test, skipped unless `PLAPS_ADAPTER_URL` is set; asserts the real endpoint matches the adapter's assumptions (payload shape, dimensions, normalization). Rationale: the fake adapter covers logic; only this test exercises the real endpoint's behavior, and only on demand.

#### 49. `tests/test_adapter.py` (Create — search increment)

*   **Type / Scope**: Unit — the real adapter against a mocked HTTP transport (`httpx.MockTransport`): payload construction (image parts nested inside `content`; the flat shape never sent), defensive L2 normalization of unnormalized responses, the typed error split (unreachable vs. rejected item), and the served-model mismatch raising the retryable error. Rationale: `plaps/embed/` is in the coverage gate, but the fake adapter leaves the real module's coverage near zero in the default run; only the contract test needs the live endpoint.

#### 50. `tests/tools/make_synthetic_db.py` (Create — hardening)

*   **Type / Scope**: Generates the synthetic 150K-row database (with vectors) used by the performance smoke; no production imports beyond `plaps.db`.

#### 51. `docs/spikes/embedding-endpoint.md`, `docs/spikes/lighttpd-sendfile.md`, `docs/spikes/pyvips-exif.md` (Record — already written, 2026-10-30)

*   **Type / Scope**: Spike records, listed because the adapter and media modules build directly on them: the exact embedding endpoint contract (path, image payload encoding, batch behavior, mmproj flags, normalization, observed latency, llama.cpp version/commit); X-Sendfile availability (verified absent from `mod_proxy`; the compiled-in `mod_fastcgi` implementation is unreachable from a WSGI HTTP backend) and Range passthrough through lighttpd ↔ waitress; pyvips EXIF read and orientation behavior on real library photos. These files exist; nothing here re-runs them.

### Pinned defaults

| Setting | Default / Value | Rationale |
|---|---|---|
| `library_root`, `embedding.base_url`, `embedding.model_id`, `timezone` | **required — no default** | The settings pointing at irreplaceable files and defining what vectors mean; the service refuses to start without them |
| `database_path` | `/var/lib/plaps/plaps.db` | FHS home for stateful data; writable by the dedicated service user (never under `/etc`) |
| `cache_dir` | `/var/cache/plaps` | FHS home for regenerable data; freely deletable |
| `backup_dir` | `<database dir>/backups` | Beside the database it snapshots; a dedicated service user can write there, and ~12 GB of snapshots never lands in `/etc` |
| `embedding.min_score` | 0.70 (cosine) | Calibrated for EmbeddingGemma 2 from the spike (matches 0.62–0.75, runner-ups 0.53–0.68); re-judge at every model change — the top-up bounds a wrong value to a labeling shift, never an empty page |
| `embedding.min_matches` | 10 | When fewer photos reach `min_score`, the list is topped up with the next-closest anyway |
| `scanner.poll_interval` | 30 s, backing off ×2 to 10 min when idle | Warm pickup stays at seconds-level; idle backoff keeps a quiet server cheap |
| `scanner.full_sweep_interval` | 24 h | Bounds in-place-edit blindness to one day |
| `scanner.rehash_interval` | 30 d | Bounds mtime-preserving-copy blindness to a month (cost: a full library read, accepted — see Risks & Limits) |
| `scanner.guard_threshold` | 10 % of the previous completed pass's live count | An intentional reorganization is re-linked (not counted); only unre-linked vanishes trip it; armed from pass two |
| `tombstone.purge_after_days` | 30 | Long enough to survive an unnoticed mistake; short enough to not accumulate stale tombstones |
| Embed batch size | constant 8 (in the adapter, not config) | The spike proved batching gains no throughput — a knob with one legal behavior is a support question |
| Embed retry (per-file: rejected items, thumb errors) | 30 s ×2 backoff, cap 1 h, 5 attempts → `failed_perm` | Long enough to survive transient per-file trouble; `failed_perm` after 5 attempts keeps permanently failing jobs from retrying forever |
| Embed circuit (endpoint-level: unreachable or wrong model) | 5 s ×2, capped at 5 min, cleared on probe success; no `attempts` charged, jobs stay `pending` | An hours-long AI-box outage must never drain healthy jobs into `failed_perm` batch by batch |
| Adapter probe (reachability + served-model check) | one `GET /v1/models`, 2 s timeout, cached 60 s, compared against the configured `model_id` | Cheap enough to run often and stable enough to trust; one request answers both "is the endpoint up" and "is it serving the model the config claims" — a mismatch backs off instead of mis-stamping vectors |
| Vector index capacity | 200,000 rows (~586 MB), doubling on overflow | An embed writes into spare capacity instead of copying ~460 MB per photo; the doubling is the only transient |
| Backup snapshots | timestamped `<backup_dir>/plaps-<YYYYMMDD-HHMMSS>.db`; the 8 most recent kept | `VACUUM INTO` refuses to overwrite an existing file; two months of weekly snapshots is the recovery window |
| `scan_stalled` threshold | no checkpoint progress for 15 min outside idle backoff | Distinguishes a slow large directory from a deadlocked scanner |
| Thumbnails | `sq` 512×512 `crop=Interesting.ATTENTION`; `as` 512 px max edge; screen-fit 2048 px max edge; WebP q80 | Matches the two grid styles (O5) and a ~2×-retina desktop viewport |
| API limits | `limit` default 100, max 500; `order` `asc`/`desc` default `desc` | One screen of a virtualized grid, generous headroom |

### Verification Plan

*   **Install check**: on a fresh machine with only the documented OS packages (`libvips`, `lighttpd`): checkout at `/opt/plaps`, `python -m venv /opt/plaps/venv && /opt/plaps/venv/bin/pip install -e ".[dev]"` from the checkout, then serving the frontend via the documented symlink (`/srv/http/plaps` → the repo's `static/`) — the README runbook must succeed verbatim. One install location everywhere: `/opt/plaps`, matching the systemd units.
*   **Static analysis & linting**: `ruff check plaps tests` and `ruff format --check` — zero findings.
*   **Unit + API tests**: `pytest` (contract test auto-skipped); `PLAPS_ADAPTER_URL=... pytest tests/test_adapter_contract.py` on demand against the real AI box.
*   **Coverage requirement**: ≥ 85 % branch coverage on `plaps/scanner.py`, `plaps/worker.py`, `plaps/exif.py`, `plaps/search.py`, `plaps/paths.py`, `plaps/config.py`, `plaps/db.py`, `plaps/images.py`, `plaps/api/`, and `plaps/embed/` — the identity, config, pipeline, and search logic. No global gate; the frontend is manually tested by decision.
*   **Manual end-to-end**: against a 1K-photo copy of the real library — originals byte-identical before/after (sampled hash comparison); first-run progress visible; both grid styles render and the choice persists (localStorage); originals and media download through the lighttpd → waitress chain with working Range scrubbing; a `plaps backup` run restores cleanly (stop, restore, restart, reconcile); search works on the embedded subset with the coverage line and correct three-state behavior (including with the AI box stopped); `kill -9` mid-scan resumes from checkpoint on restart; a model change (AI box switched to the new model, config edit, restart) ranks in the new space over the re-embedded subset with the coverage line growing, and `POST /api/admin/model/cutover` deletes the old vectors.
*   **Performance smoke**: on a synthetic 150K-row database (`tests/tools/make_synthetic_db.py`) — timeline window p95 < 100 ms warm; folder tree < 200 ms warm; search *ranking* p95 < 50 ms warm (query-embed round trip excluded and measured separately) on the reference hardware.

