# plaps — On-Disk Semantic Photo Gallery

*   **Author:** Project owner
*   **Status:** Draft
*   **Date:** 2026-10-30

--------------------------------------------------------------------------------

## Overview

plaps is a single-user, self-hosted photo gallery web app that provides a Google-Photos-style experience over an existing library of ~150K JPEG photos on local disk. Unlike hosted galleries, its source of truth is the filesystem: the server indexes the library in place, never copies, moves, or renames originals, and derives only cached WebP thumbnails and screen-fit images stored outside the library.

The stack is deliberately small: a Python service (Flask) with SQLite as the only store (metadata, vectors, job queue), pyvips for image work, piexif for EXIF, served behind a lighttpd reverse proxy that also provides basic authentication. No Docker, no NodeJS, no build toolchain. Semantic search runs against Google's EmbeddingGemma 2 (joint text+image embeddings), served by llama.cpp on a separate AI box through a single adapter module. The frontend is no-build ES modules rendering client-side against a JSON API — the same API a future Android client would consume.

## The Problem

The owner keeps a large, growing, and loosely organized photo library directly on disk — files added by camera imports and card dumps, with no rigid directory structure, and no intention to reorganize it or upload it anywhere. Hosted and self-hosted galleries each fail this setup in a different way:

* Google Photos requires uploading everything out of the owner's control.
* PhotoPrism expects a specific directory layout and offers label/keyword search rather than true semantic (joint text+image embedding) search.
* Immich supports in-place external libraries but ships as a Docker + TypeScript stack the owner does not want to operate or own.
* PiGallery2 scans in place but is a NodeJS application without semantic search.
* LibrePhotos is Python with semantic search but carries Django + Postgres + worker machinery far beyond a single-user need.

The need: fast, date-organized browsing (flat timeline and Picasa-style folder view) plus genuine semantic search, over photos that live on a local disk in arbitrary structure — with originals untouched, no duplication beyond regenerable caches, and a system one person can install, understand, and maintain alone on a home server.

The hard problems this forces, and which the design must answer directly:

* **Identity and change detection.** Files are added mostly, but occasionally renamed, moved, deleted, or edited in place. The index must track all of that without ever touching the files, survive scanner crashes, and never interpret a configuration mistake as a mass deletion.
* **Search over a fragile dependency.** Embeddings come from a separate machine. Browsing must not depend on it, model changes must not black out search, and the owner must always be able to tell "nothing matched" from "nothing embedded."
* **Scale without machinery.** 150K photos must be browsable without pagination UI, searchable without a vector database, and indexable without a message queue — on one home server.

## Goals

*   **In-place indexing.** Index ~150K local JPEGs by path; originals are never modified, moved, or duplicated (verifiable: file bytes and mtimes unchanged after indexing). Change detection is stat-based (path + mtime + size) with a daily full stat sweep and a roughly monthly re-hash pass.
*   **Date timeline.** A flat, virtualized timeline of all photos sorted by EXIF `DateTimeOriginal` (mtime fallback), served over a cursor-paged JSON API with no pagination UI; scrolling is windowed and stable while the scanner writes.
*   **Folder view.** A tree of arbitrarily nested folders derived from indexed paths; folders sorted by the newest date-taken of contained photos (recursive); photos within a folder sorted by date.
*   **Semantic search.** Text queries rank photos by cosine similarity of joint text+image embeddings; any active date/folder filter applies before ranking; a coverage count line ("search covers N of M photos") always distinguishes "nothing matched" from "nothing embedded"; query latency in tens of milliseconds.
*   **Derived images.** WebP thumbnails (~512 px) and screen-fit images (~2048 px) cached outside the library, keyed by content hash and variant; both grid styles — uniform square (center-cropped) and justified rows (aspect-preserving) — available as a user option; originals downloadable but never the default view.
*   **Operational simplicity.** Installs and runs as a plain service (venv, systemd unit, libvips OS package, lighttpd config with basic auth); zero Docker, zero NodeJS; SQLite single-file backup with a documented restore path; `/status` exposes scan progress, queue state, errors, cache size, and embedding-endpoint health.
*   **Graceful degradation.** With the AI box down: browsing, folder view, and original download are unaffected; search serves whatever is embedded; the embed queue resumes on recovery.
*   **Verification.** An API-level pytest suite (Flask in-process test client, temp library trees, fake embedding adapter) runs locally with one command.

## Non-Goals

*   Photo editing, live photos, RAW, and video support.
*   Multi-user accounts or permissions; app-level authentication in the MVP (lighttpd basic auth covers it; a cookie login page is backlog for share links or a second user).
*   Upload as a primary ingestion path.
*   Remote/network-mounted filesystems (local disks only).
*   Symlink support (symlinks are skipped entirely).
*   ANN vector indexes (brute force suffices below ~1M photos).
*   Message-queue or container infrastructure of any kind.
*   Node/npm tooling of any kind, including at build time.
*   A duplicate-handling workflow (duplicates are detected and reported; acting on them is deferred).

## Open Questions

*None currently. The two questions inherited from the concepts brief were resolved during drafting: the config surface is specified in Technical Plan → Configuration (single TOML file via stdlib `tomllib`; cold start refuses to run without `library_root`), and the tuning values are pinned in the defaults table of the Detailed Implementation Plan.*

## Critical User Journeys (CUJs)

### CUJ 1: First run against an existing library

*   **Given**: A home server with a local disk holding ~150K JPEGs in arbitrary nested folders; plaps installed (venv + systemd + lighttpd) and configured with the library root and cache directory; the AI box reachable.
*   **When**: The service starts for the first time against an unindexed library.
*   **Then**: The scanner begins a full metadata pass; the UI serves what is already indexed from the start, with an optional progress indicator; thumbnails and embeddings accrue in the background (~4–42 hours for the full embed pass); search results carry the coverage count line until embedding completes; a scanner crash resumes rather than restarts, and a stall or crash is a visible error state.

### CUJ 2: Browse the timeline

*   **Given**: The library is indexed (or indexing in progress).
*   **When**: The user opens the app and scrolls the flat timeline across two decades of photos.
*   **Then**: Photos appear sorted by date taken (newest or oldest first, per setting); the virtualized view fetches windows through the cursor API without a pagination widget and without skipping or repeating entries while the scanner writes; clicking a photo shows the cached screen-fit image; an explicit action downloads the original, untouched, via the proxy.

### CUJ 3: Browse by folder

*   **Given**: The same index.
*   **When**: The user opens the folder view.
*   **Then**: Folders appear sorted by the newest date-taken among their contained photos (computed recursively); folders with no photos beneath them are hidden; entering a folder shows its photos sorted by date; the tree reflects the on-disk structure at query time with no separately maintained index to drift.

### CUJ 4: Semantic search

*   **Given**: Embeddings exist for at least part of the library; the AI box is reachable.
*   **When**: The user types a text query such as "dog on the beach," optionally while a date range or folder filter is active.
*   **Then**: The query is encoded server-side; any active filter is applied before ranking; results are ranked by cosine similarity; the results view shows the coverage count line; photos without a current-model embedding are silently excluded from ranking; latency stays in tens of milliseconds.

### CUJ 5: Library changes underneath the index

*   **Given**: The library is indexed and the service is running.
*   **When**: The user adds files to any folder, renames or moves files or folders, deletes files, or edits a file in place.
*   **Then**: The scanner picks up additions at its next pass (stat-pruned, with the daily sweep as backstop for in-place edits); renames and moves re-link to existing records via content-hash match so metadata survives; deletes tombstone records (`deleted_at`) which revive if the same content reappears within the purge window; an in-place edit is treated as delete-plus-add, regenerating derived images and the embedding; a file that vanishes between scans simply fails its read and drops from view until the next scan.

### CUJ 6: The AI box goes down

*   **Given**: Embeddings are partially complete, or a re-embed is running.
*   **When**: The AI box becomes unreachable for hours or days.
*   **Then**: Browsing, folder view, and original download are unaffected; search continues over already-embedded photos with the coverage line visible; the embed queue backs off and resumes from where it stopped when the box returns; per-file errors are recorded with kind and text, distinguishable from pending work; `/status` shows endpoint reachability and the last successful embed.

### CUJ 7: A configuration mistake threatens the index

*   **Given**: The deletion guard is armed with a file-count-drop threshold.
*   **When**: The library root is unstat-able, or a scan pass sees an unre-linked file-count drop beyond the threshold (e.g., root pointed at the wrong directory).
*   **Then**: The deletion pass halts without tombstoning anything; the condition is logged and surfaced on `/status`; the rest of the scan continues; deletion processing resumes only after manual acknowledgment.

### CUJ 8: The embedding model changes

*   **Given**: Vectors are stamped with `model_id`; the configured endpoint's model is replaced.
*   **When**: A re-embed of the library runs under the new model.
*   **Then**: Old-model vectors remain searchable throughout (old-model text and image vectors are mutually consistent); the re-embed runs through the existing job queue and survives interruption; the active `model_id` cuts over when new-model coverage crosses a threshold; old vectors are deleted after cutover — search never goes dark.

## Technical Plan (Architecture)

### Component map

```text
                    browser (no-build ES modules)   future Android client
                              │  JSON over HTTP                 │
                              ▼                                 ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ lighttpd                                                      │
        │  · basic auth (the only auth layer)                           │
        │  · serves static frontend files directly from disk            │
        │  · reverse-proxies /api/* and /media/* to the app             │
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
        │   /media/*         derived images      embed worker: job      │
        │   /original/*      originals,           queue → adapter →     │
        │                     both streamed          normalized vector  │
        │                                                               │
        │  SQLite: one writer connection (locked, shared by scanner +   │
        │  embed worker); per-thread reader connections in WAL mode     │
        └──────┬──────────────────────────────┬───────────┬─────────────┘
               │                              │           │ HTTP (embeddings)
               ▼                              ▼           ▼
        ┌─────────────┐          ┌──────────────────┐  ┌──────────────────┐
        │ plaps.db    │          │ library root     │  │ AI box: llama.cpp│
        │ (SQLite,WAL)│          │ (read-only walk; │  │ EmbeddingGemma 2 │
        │ photos,     │          │ originals never  │  │ GGUF, mmproj;    │
        │ vectors,    │          │ written)         │  │ OpenAI-shaped    │
        │ jobs, meta  │          └──────────────────┘  │ /v1/embeddings   │
        └─────────────┘                                └──────────────────┘
               ▲
        ┌──────┴──────────────────────────────┐
        │ cache dir (outside the library)     │
        │  thumb/<aa>/<hash>.<variant>.webp   │
        │  large/<aa>/<hash>.webp             │
        └─────────────────────────────────────┘
```

### Process model

One process, one systemd unit. waitress (pure-Python, no C toolchain, one dependency) serves WSGI with a small thread pool; the scanner and the embed worker run as daemon threads in the same process. This follows from the brief's single-writer decision: scanner and embed worker share one SQLite writer connection guarded by a lock, which makes "database is locked" structurally impossible between writers; readers open per-thread connections in WAL mode. The GIL is not a bottleneck here — pyvips, hashing, and numpy all release it during native work, and file streaming is socket I/O that releases the GIL on every write. A crash kills all three roles at once, which is acceptable by design: the jobs table and a scan checkpoint in `meta` make every background pass resumable, never restartable-from-zero.

### Data model

```sql
photos(
  photo_id      INTEGER PRIMARY KEY,
  path          TEXT UNIQUE NOT NULL,      -- scanner's key, realpath-free; symlinks skipped
  content_hash  TEXT,                       -- blake2b, hex; NON-unique: identity for re-link + dedup
  size          INTEGER, mtime INTEGER,     -- last seen stat; change = re-hash
  date_taken    TEXT,                       -- ISO 8601 local wall-clock, from EXIF DateTimeOriginal
  date_source   TEXT,                       -- 'exif' | 'mtime' | 'unknown'
  width, height INTEGER,
  thumbnailed_at, embedded_at DATETIME,     -- per-stage status columns, no lifecycle enum
  deleted_at    DATETIME,                   -- tombstone; NULL = alive
  error_kind    TEXT, error_text TEXT       -- 'thumb' | 'hash' | 'embed' | 'exif'
)
-- indexes: (date_taken, photo_id) for the timeline cursor;
--           (content_hash) for re-link/dedup; (path) UNIQUE for the scanner

vectors(photo_id, model_id, dim, vector BLOB,   -- float32 little-endian, L2-normalized
        PRIMARY KEY (photo_id, model_id))

jobs(job_id, photo_id, kind,              -- 'thumb' | 'embed'
     state,                               -- 'pending' | 'done' | 'failed_perm'
     attempts, next_attempt_at, error_text, created_at)

meta(key, value)    -- active_model_id, scan checkpoint, guard state, coverage counters
-- schema version via PRAGMA user_version; one-way migrations
```

Key points: one record per path (decided); content hash is a non-unique identity for re-linking and dedup; per-stage nullable columns replace a lifecycle enum; "changed" is not a state — an in-place edit deletes the old record and inserts a new one.

### Scanning and change detection

The scanner thread loops: walk the library root, **stat-prune** (descend every directory, but skip stat-ing files inside directories whose own mtime is unchanged since the last pass), and for each new or stat-changed file: hash it (blake2b), read EXIF `DateTimeOriginal` (piexif; malformed EXIF → error row + mtime fallback), upsert the `photos` row, and enqueue thumbnail and embed jobs. Vanished paths are *not* immediately tombstoned: the scanner first checks whether the vanished record's content hash appeared at another path in the same pass (move → re-link, keep `photo_id`); otherwise it writes `deleted_at`. A record whose content hash reappears anywhere within the purge window revives (tombstone cleared, embedding and future metadata intact). Two backstops: a daily full stat sweep (in-place edits never touch the parent directory's mtime) and a roughly monthly re-hash pass (mtime-preserving copies). The mass-delete guard runs before the tombstone write: if the library root is unstat-able, or the pass's unre-linked disappearance count exceeds the configured threshold of the previous pass's file count, tombstone writes halt, the state lands in `meta` and on `/api/status`, and resuming requires an explicit admin action.

### Thumbnail and image serving

Thumbnail jobs produce two WebP variants per photo at scan time: square-cropped 512×512 (uniform grid; `crop=Interesting.CENTRE` — verified) and aspect-preserving 512 px max edge (justified rows), both auto-rotated via pyvips `thumbnail()` (auto-rotation verified on real files; `resize()` must never be used). Screen-fit images (~2048 px max edge, aspect-preserving, auto-rotated) are generated on demand on first view. All derived files are keyed by `(content_hash, variant)` in a two-level shard (`thumb/<first-2-hex-chars>/`), so an edited photo — a new hash — can never hit a stale entry, and keys are immutable, so responses carry long-lived immutable cache headers. `/media/*` requests check the cache, generate on miss, and stream the file via Werkzeug's conditional `send_file`; `/original/<photo_id>` resolves the path *from the database by id* — the client never supplies a path — and streams the original the same way, with full Range/206 support through the proxy (passthrough verified on lighttpd 1.4.85). X-Sendfile was cut after live verification: this lighttpd build's `mod_proxy` silently ignores the header — a proxied app emitting it serves 200 responses with zero bytes — and the compiled-in `mod_fastcgi` that does implement it is unreachable from a WSGI HTTP backend. At single-user LAN scale, streaming through waitress costs milliseconds per request (wire time dominates). See `docs/spikes/lighttpd-sendfile.md` and `docs/spikes/pyvips-exif.md`. Orphaned cache entries are deleted when their records purge.

### Embedding pipeline and search

The embed worker pops pending jobs oldest-first, batches them to the adapter, and writes L2-normalized float32 vectors stamped with `model_id`. The adapter is one module with a three-function surface — `embed_images()`, `embed_text()`, `model_id` — and is the only place that knows the llama.cpp specifics (endpoint shape, image payload encoding, mmproj flags); it normalizes defensively rather than trusting the server. Endpoint failures back off exponentially and mark only the failed items; permanently failed files are distinguishable from pending ones. (Spike-measured on the dev box: ~2.5 s/photo on the iGPU, batching gives no throughput gain — see `docs/spikes/embedding-endpoint.md`; the ~4–42 h full-embed estimate assumes the production AI box's observed 0.1–1 s/photo, so re-measure there before the first run.) At startup (and at model cutover) all vectors for the active `model_id` load into a resident float32 numpy matrix (~460 MB at 150K × 768) with a parallel `photo_id` array; a search embeds the query text once, applies the active date/folder filter as a `WHERE` clause to get the candidate id set, and brute-force cosine-ranks against the matrix — tens of milliseconds. Un-embedded photos are silently excluded from ranking; the response's coverage count line comes from the same counters `/api/status` publishes. A configured model change queues re-embed jobs through the *existing* queue while old-model vectors keep serving; cutover (rewrite `active_model_id`, reload the matrix, delete old vectors) happens when new-model coverage crosses the threshold.

### API surface (JSON)

| Endpoint | Purpose |
|---|---|
| `GET /api/photos?cursor&limit&order` | One timeline window; cursor is an opaque base64 of `(date_taken, photo_id)` — stable under concurrent inserts |
| `GET /api/folders` | Folder tree derived at query time: `GROUP BY` path prefixes over live photos; each node carries its recursive newest `date_taken`; empty-leaf folders hidden |
| `GET /api/folders/<id>/photos?cursor&limit` | Timeline window scoped to one folder subtree |
| `GET /api/search?q&folder&after&before&limit` | Filtered, similarity-ranked results + coverage count line |
| `GET /api/status` | Scan progress/checkpoint, queue depth, error counts by kind, cache size, active `model_id`, adapter reachability, last successful embed, guard state |
| `POST /api/admin/guard/resume` | Manual acknowledgment after a mass-delete guard trip |
| `GET /media/thumb/<hash>.<variant>.webp` · `GET /media/large/<hash>.webp` | Derived images, streamed with immutable cache headers, generated on miss |
| `GET /original/<photo_id>` | Original file by database id, streamed with Range/206 support |

### Security

Basic auth at lighttpd is the entire auth story in the MVP — the app ships no authentication code and the JSON API is consumable by a future Android client with the same credentials. All file serving resolves paths from the database (by id or by content hash), never from client input, which makes path traversal structurally impossible; the library root and cache root are the only directories the app ever touches, and symlinks are skipped at scan time, closing the escape routes. Any exposure past the LAN requires TLS at the proxy, documented in the lighttpd config sample.

### Configuration

A single TOML file (stdlib `tomllib`, Python ≥ 3.11) next to the service, documented with defaults: `library_root`, `cache_dir`, `database_path`, `timezone`, `embedding.base_url`, `embedding.model_id`, `embedding.batch_size`, `scanner.poll_interval` (seconds between stat-pruned passes; the loop backs off when idle), `scanner.full_sweep_interval`, `scanner.rehash_interval`, `scanner.guard_threshold`, `tombstone.purge_after_days`. Cold start with no file: the service refuses to run without `library_root` and prints the path it expects — no silent defaults for the one setting that points at 150K irreplaceable files. *(This resolves Open Question 1's shape; tuning values remain Open Question 2.)*

## Alternatives Considered

*Carried from the brainstorm's `Alternatives Weighed` (the only source for this section), plus two alternatives weighed during drafting itself.*

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
    *   **Reason for Rejection**: Strictly additive machinery whose failure modes the sweep silently papers over, in exchange for latency that no single-user flow consumes. Polling with stat-pruning plus the daily full sweep detects everything inotify would, plus what it would not (FUSE disks, changes while the service is down). Revisitable as an optional accelerator (see Future Improvements).
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
    *   **Reason for Rejection**: Verified broken on the target platform for this stack. Werkzeug conditional streaming through a 16-thread waitress pool is correct, and at single-user LAN scale the byte path through Python costs milliseconds per request.

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
    *   **Cons**: The unlock condition was ambiguous by ~40 hours (metadata scan vs. embedding pass); a scanner crash mid-scan bricks the whole app.
    *   **Reason for Rejection**: Serve what is indexed, with an optional progress indicator and visible error states.
*   **App-level login page (cookie sessions) in the MVP**
    *   **Pros**: Branded login UX; groundwork for per-user features.
    *   **Cons**: Two auth mechanisms for one user; session handling to maintain forever; basic auth at the proxy already covers the threat model and serves a future Android client.
    *   **Reason for Rejection**: Proxy basic auth only in the MVP. The login page moves to the backlog; share links or a second user are its trigger.
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
    *   **Reason for Rejection**: The owner accepts duplicates displaying twice until a dupe workflow exists; one record per path with a non-unique hash is simpler and keeps re-linking trivial.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Privacy.** The library is irreplaceable personal data. The app treats the library as read-only (verified by tests asserting bytes and mtimes unchanged); the only outbound network call is to the configured embedding endpoint; no telemetry. Auth is proxy password authentication at lighttpd — basic and digest are interchangeable here because auth terminates at the proxy; the target server's existing htdigest setup satisfies the requirement as-is — and the JSON API is consumable by a future Android client with the same credentials. The app itself listens on localhost only; any LAN/WAN exposure requires TLS at the proxy, stated in the config sample. Path traversal is structurally prevented (all file serving resolves paths from the database, never client input); symlinks are never followed.
*   **Performance & Latency.** Targets on the reference hardware (Ryzen 5 5700G, 32 GB): timeline window requests under ~100 ms warm; semantic search in tens of milliseconds warm (resident float32 matrix); thumbnail-cache hits streamed by waitress in milliseconds (LAN wire time dominates); cache-miss generation is the only slow path; the 16-thread waitress pool keeps timeline requests responsive during large original downloads (covered by the concurrency stress test). First full ingest is explicitly tolerated at hours (scan + thumbnails) to ~4–42 hours (embeddings). Resident memory stays under ~1 GB (vector matrix ~460 MB plus pyvips working set). Disk: database roughly 1–2 GB including vectors; derived cache roughly 35–70 GB at 150K photos with both thumbnail variants.
*   **Rollout & Rollback.** Single-user service, no feature flags: rollout is a systemd unit restart; rollback is restoring the previous unit and, if needed, the last database backup. Schema evolution is one-way migrations gated by `PRAGMA user_version`. Database loss costs a rescan plus a multi-day re-embed — the stated reason for a weekly (or post-large-change) `VACUUM INTO` backup cadence and a documented restore path (stop service, restore file, restart, scanner reconciles; the cache is regenerable and freely deletable).

### Additional Resources

*   `concepts_brief.md` — the brainstorm artifact this design ingests (decisions, prior-art corrections, testing strategy).
*   EmbeddingGemma 2: `huggingface.co/google/embeddinggemma-2`; llama.cpp-ready weights: `huggingface.co/ggml-org/embeddinggemma-2-GGUF`.
*   Reference implementations studied: Immich external libraries (ingestion blueprint), LibrePhotos (embedding indexer), PiGallery2 (stat-pruned scanner), Photoview and Photofield (in-place gallery UX).
*   Spike results (all run 2026-10-30): `docs/spikes/embedding-endpoint.md` (verified EmbeddingGemma 2 HTTP contract, failure modes, batching, 8/8 retrieval on real photos), `docs/spikes/lighttpd-sendfile.md` (X-Sendfile negative result; Range passthrough; target lighttpd config), `docs/spikes/pyvips-exif.md` (EXIF sub-IFD naming, auto-rotation, square-crop, stripped-EXIF behavior).

### Future Improvements

*   **Deferred decisions with triggers** (from the brief): browser-side query encoding via transformers.js (novelty; requires model-version coupling); pixel/perceptual hash to survive metadata-only edits (trigger: first real lossless-rotation re-embed); app-level cookie login page (trigger: share links or a second user); dupe-handling workflow (trigger: living with displayed duplicates); Android auto-backup (the settled one-record-per-path model defines how its duplicates are handled); Go rewrite (profiling-gated only).
*   **inotify as an optional change-detection accelerator** (trigger: change-pickup latency ever becomes a felt need): add a watcher in front of the scanner with the polling sweep remaining the source of truth; the scanner's interface does not change, so the addition is isolated.
*   **Later-phase features** (in scope, sequenced after the MVP): content-similarity auto-grouping, face detection and grouping, map view, read-only share links, the Android client.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

Greenfield: every file below is created; nothing is modified or deleted. Work proceeds in the increment order from the concepts brief — spikes, walking skeleton, folder view, search, hardening — and each file lists the increment it belongs to. No production code appears in this document; signatures and schemas only.

### Target Files to Create/Modify

#### 1. `pyproject.toml` (Create — walking skeleton)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**: Declares the `plaps` package, Python ≥ 3.11, and the complete dependency surface: `flask`, `waitress`, `pyvips`, `piexif`, `httpx`, `numpy`. Dev extras: `pytest`, `pytest-cov`, `ruff`. Rationale: one manifest is the whole install story (`pip install -e .`); listing every dependency here is what keeps the surface honest.

#### 2. `README.md` (Create — hardening)

*   **Type / Scope**: Documentation; the product's front door.
*   **Proposed Changes**: Install runbook (OS packages `libvips` and `lighttpd`, stdlib `python -m venv`, `pip install -e .`), config walkthrough, systemd and lighttpd setup, the documented one-line venv recreation for after a distro Python major upgrade (config, database, and cache are unaffected — the venv is disposable by design), the backup/restore procedure (`VACUUM INTO` cadence; stop → restore file → restart → scanner reconciles; cache is regenerable), and how to run tests. Rationale: ease of installation is a stated design goal, and the restore path only counts if it is written down.

#### 3. `config.example.toml` (Create — walking skeleton)

*   **Type / Scope**: Configuration template.
*   **Proposed Changes**: Every key from Technical Plan → Configuration with its pinned default and a one-line comment. Rationale: the config surface is documented by example, and the service's validation errors can point at this file.

#### 4. `systemd/plaps.service` (Create — hardening)

*   **Type / Scope**: Process definition.
*   **Proposed Changes**: `ExecStart=python -m plaps`, dedicated `User=`, `Restart=on-failure`. Rationale: crash-resume makes automatic restart safe — a killed scanner resumes from its checkpoint rather than corrupting state.

#### 5. `lighttpd/plaps-lighttpd.conf` (Create — hardening)

*   **Type / Scope**: Reverse proxy configuration sample.
*   **Proposed Changes**: Proxy-auth block (the only auth layer; digest, reusing the server's existing htdigest userfile), `mod_proxy` to the app on `127.0.0.1:8096`, `static/` served directly from disk, commented TLS block for any exposure past the LAN. Rationale: the security model lives in this file; shipping it as a reviewed sample prevents the classic misconfigured-proxy hole. Shape verified against the target server — see `docs/spikes/lighttpd-sendfile.md`.

#### 6. `plaps/__init__.py` (Create — walking skeleton)

*   **Type / Scope**: Package marker; version string only.

#### 7. `plaps/config.py` (Create — walking skeleton)

*   **Type / Scope**: Configuration loading and validation.
*   **Proposed Changes**: Loads the TOML file with stdlib `tomllib`, validates types and ranges, applies the pinned defaults for anything unset, and **exits with a clear message if `library_root` is absent**. Exposes a frozen settings object. Rationale: no silent defaults for the one setting pointing at irreplaceable files; everything else defaults safely.

#### 8. `plaps/db.py` (Create — walking skeleton)

*   **Type / Scope**: SQLite access layer.
*   **Proposed Changes**: Connection factory (per-thread reader connections in WAL mode), the single writer connection guarded by a lock, `PRAGMA user_version`-gated one-way migrations (an ordered list of schema steps), and a `VACUUM INTO` backup helper that quiesces the writer first. Rationale: WAL + one writer is the entire concurrency story; centralizing it here means no other module ever opens a connection with the wrong pragmas.

#### 9. `plaps/exif.py` (Create — walking skeleton)

*   **Type / Scope**: Pure EXIF-date functions.
*   **Proposed Changes**: `date_taken(bytes) -> (datetime | None, error | None)` — reads `DateTimeOriginal` via piexif, parses `YYYY:MM:DD HH:MM:SS`, treats malformed blocks as a returned error, never raises. Rationale: the most malformed-input-heavy logic in the system is isolated as pure functions for the thin unit layer.

#### 10. `plaps/scanner.py` (Create — walking skeleton)

*   **Type / Scope**: The background scanner thread.
*   **Proposed Changes**: One pass function with three modes — stat-pruned (default cycle), full sweep (daily), re-hash (monthly) — selected by cadence from `meta`. Per pass: walk `library_root` (skipping symlinks), skip `stat` in unchanged-mtime directories (pruned mode only), and for new/stat-changed files compute the blake2b hash, read EXIF via `exif.py`, upsert the `photos` row, enqueue `thumb` + `embed` jobs. Vanished paths: same-pass content-hash re-link check first, else `deleted_at` tombstone; revival clears a tombstone when the hash reappears within the purge window; purge drops expired tombstones and their cache orphans. The mass-delete guard runs before any tombstone write (root stat check + unre-linked drop threshold) and latches a `meta` state only `POST /api/admin/guard/resume` clears. A pass checkpoint in `meta` makes crashes resume mid-tree. Rationale: this module owns every identity rule in the design; all of it is exercised through `tests/test_scanner.py`.

#### 11. `plaps/images.py` (Create — walking skeleton)

*   **Type / Scope**: pyvips image derivation.
*   **Proposed Changes**: `thumbnail(variant) -> cache path` for `sq` (512×512 smart-crop) and `as` (512 px max edge), `screenfit() -> cache path` (2048 px max edge), all auto-rotated via pyvips `thumbnail` semantics, encoded WebP at quality 80, written to `thumb/<2-hex>/<hash>.<variant>.webp` / `large/<2-hex>/<hash>.webp`; `ensure(hash, kind)` generates on cache miss. Rationale: keyed by content hash + variant, so staleness is impossible by construction and every key is immutable.

#### 12. `plaps/embed/adapter.py` (Create — search increment)

*   **Type / Scope**: The embedding endpoint client; the only module that knows llama.cpp specifics.
*   **Proposed Changes**: `embed_images(list[bytes]) -> list[vector]`, `embed_text(str) -> vector`, `model_id` property; payload encoding, batching size, and endpoint path exactly as recorded in the embedding spike; defensive L2 normalization of every returned vector; typed errors distinguishing "endpoint unreachable" (retryable) from "endpoint rejected item" (per-file failure). Rationale: the adapter is the quarantine boundary for every llama.cpp quirk; the typed error split is what lets the worker back off correctly without poisoning jobs.

#### 13. `plaps/embed/worker.py` (Create — search increment)

*   **Type / Scope**: Background embed worker thread.
*   **Proposed Changes**: Pops `pending` jobs oldest-first in batches, calls the adapter, writes normalized vectors stamped `model_id`, records per-file errors with kind, retries unreachable-endpoint failures with exponential backoff, marks repeated failures `failed_perm`. Detects a configured-model change by comparing `adapter.model_id` to `meta.active_model_id`: queues re-embed jobs through the same table, keeps old-model vectors serving, and cuts over (rewrite `active_model_id`, drop old vectors, signal the search index to reload) when new-model coverage crosses the threshold. Rationale: one queue does initial ingest, retry, and migration — no second pipeline to rot.

#### 14. `plaps/search.py` (Create — search increment)

*   **Type / Scope**: Vector index and ranking.
*   **Proposed Changes**: `VectorIndex` loads all vectors for `meta.active_model_id` into a float32 numpy matrix plus a parallel `photo_id` array at startup and at cutover; `query(text, folder, after, before, limit)` encodes the text once via the adapter, resolves candidate ids with a plain SQL `WHERE` (date/folder filters), cosine-ranks the candidate rows against the matrix, and returns ranked ids plus the coverage counts. Rationale: filters-before-ranking is a WHERE clause; brute force over a resident matrix is the whole search engine, and keeping it in one class makes the ~1M-photo ANN revisit a single-file change.

#### 15. `plaps/api/__init__.py` (Create — walking skeleton)

*   **Type / Scope**: Flask app factory.
*   **Proposed Changes**: Builds the app, registers the blueprints below, installs JSON error handlers so every failure — including 404 and 500 — returns the same error shape. Rationale: the JSON API is the client contract; uniform errors are part of it.

#### 16. `plaps/api/photos.py` (Create — walking skeleton)

*   **Type / Scope**: Timeline endpoints.
*   **Proposed Changes**: `GET /api/photos?cursor&limit&order` — one window over live (`deleted_at IS NULL`) photos ordered by `(date_taken, photo_id)`; the cursor is base64url of `"<date_taken>|<photo_id>"` and is opaque to clients. Rationale: the composite cursor is what makes windows stable while the scanner inserts.

#### 17. `plaps/api/folders.py` (Create — folder view increment)

*   **Type / Scope**: Folder tree endpoints.
*   **Proposed Changes**: `GET /api/folders` derives the tree by grouping live-photo path prefixes at query time (no maintained table), computes each node's recursive newest `date_taken`, hides photoless branches; a folder id is the base64url of its directory prefix. `GET /api/folders/<id>/photos?cursor&limit` is the timeline window scoped to the subtree. Rationale: a derived tree cannot drift from disk.

#### 18. `plaps/api/search.py` (Create — search increment)

*   **Type / Scope**: Search endpoint.
*   **Proposed Changes**: `GET /api/search?q&folder&after&before&limit` — delegates to `search.py`, returns ranked photo cards plus the coverage line (`embedded`, `total`). Rationale: the coverage count is part of the response contract, not a UI afterthought.

#### 19. `plaps/api/media.py` (Create — walking skeleton)

*   **Type / Scope**: Image and original serving.
*   **Proposed Changes**: `GET /media/thumb/<hash>.<variant>.webp` and `GET /media/large/<hash>.webp` — verify the hash exists in a live record (never generate for unknown hashes), `ensure()` on miss, emit immutable `Cache-Control`, stream via Werkzeug `send_file` (conditional requests; Range/206 handled by Werkzeug, passthrough through the proxy verified). `GET /original/<photo_id>` resolves the path **from the database by id** and streams it the same way; tombstoned or missing records return 404. Rationale: clients never supply paths, so traversal is structurally impossible.

#### 20. `plaps/api/status.py` (Create — walking skeleton)

*   **Type / Scope**: Health and administration.
*   **Proposed Changes**: `GET /api/status` assembles scan progress/checkpoint, queue depth, error counts by kind, cache size (maintained incrementally, not walked per request), active `model_id`, adapter reachability (probed with a cheap text embed with a short timeout, cached), last successful embed, and guard state. `POST /api/admin/guard/resume` clears the latched guard. Rationale: `/api/status` is the owner's entire debugging surface.

#### 21. `plaps/app.py` (Create — walking skeleton)

*   **Type / Scope**: Composition root.
*   **Proposed Changes**: Wires config → db → scanner/worker threads → vector index → Flask app → waitress (16-thread pool, so a large original download never starves timeline requests); handles startup ordering (index loads before serving; scanner starts after) and graceful shutdown (stop accepting, checkpoint scanner, close writer). Rationale: one file owns the process's lifecycle so the threading model is visible in a single place.

#### 22. `plaps/__main__.py` (Create — walking skeleton)

*   **Type / Scope**: Entrypoint (`python -m plaps`).
*   **Proposed Changes**: Resolves the config path (argument or environment), calls into `app.py`. Rationale: keeps the systemd unit and manual runs identical.

#### 23. `static/index.html` (Create — walking skeleton)

*   **Type / Scope**: Single-page shell.
*   **Proposed Changes**: One HTML file loading the ES modules below; no bundler, no build step, served directly by lighttpd. Rationale: the no-Node constraint made structural.

#### 24. `static/css/plaps.css` (Create — walking skeleton)

*   **Type / Scope**: Styles for both grid layouts (`sq` uniform square, `as` justified rows), the viewer, and the progress indicator.

#### 25. `static/js/api.js` (Create — walking skeleton)

*   **Type / Scope**: Fetch wrapper; cursor handling mirroring the server format; error surfacing to the UI.

#### 26. `static/js/timeline.js` (Create — walking skeleton)

*   **Type / Scope**: Virtualized timeline grid — windowed fetching via IntersectionObserver, layout switching between the two grid styles, date headers.

#### 27. `static/js/status.js` (Create — walking skeleton)

*   **Type / Scope**: First-run progress indicator and stalled-scan error banner, polling `/api/status`.

#### 28. `static/js/folders.js` (Create — folder view increment)

*   **Type / Scope**: Folder tree navigation; entering a folder scopes the timeline.

#### 29. `static/js/viewer.js` (Create — walking skeleton)

*   **Type / Scope**: Photo viewer — screen-fit display, explicit original-download action, keyboard navigation.

#### 30. `static/js/search.js` (Create — search increment)

*   **Type / Scope**: Query box, ranked results grid, coverage-line display, active date/folder filter state.

#### 31. `static/js/main.js` (Create — walking skeleton)

*   **Type / Scope**: Hash-based router wiring the views together.

#### 32. `tests/conftest.py` (Create — walking skeleton)

*   **Type / Scope**: Shared fixtures.
*   **Proposed Changes**: A temp-library factory that writes real JPEGs with controlled EXIF via piexif and manipulates mtimes; an app fixture (in-process Flask test client pointed at temp config/DB/cache); a fake embedding adapter with injectable vectors for deterministic ranking. Rationale: every scanner and search test stands on these three fixtures.

#### 33. `tests/test_exif.py` (Create — walking skeleton)

*   **Type / Scope**: Unit — valid, malformed, and missing EXIF; `date_source` tagging; mtime fallback behavior.

#### 34. `tests/test_scanner.py` (Create — walking skeleton)

*   **Type / Scope**: API-level scanner behavior via temp trees: add; rename/move re-link; delete → tombstone; revival within the purge window; purge after it; in-place edit = delete + add with fresh derived artifacts; nested-add discovery under stat-pruning; guard trip and manual resume; symlink skipping; crash-resume from checkpoint; and the originals-untouched assertion (bytes and mtimes unchanged after a full pass).

#### 35. `tests/test_api_photos.py` (Create — walking skeleton)

*   **Type / Scope**: Cursor windows — ordering, bounds, and stability under concurrent inserts.

#### 36. `tests/test_api_folders.py` (Create — folder view increment)

*   **Type / Scope**: Tree derivation, recursive newest-date computation, empty-branch hiding, subtree scoping.

#### 37. `tests/test_search.py` (Create — search increment)

*   **Type / Scope**: Synthetic-vector ranking order; filter-before-rank; coverage line; silent exclusion of un-embedded photos; model cutover keeps old-model results until the coverage threshold.

#### 38. `tests/test_media.py` (Create — walking skeleton)

*   **Type / Scope**: Variant dimensions and formats (square variant exactly 512×512, aspect variant ≤ 512 px max edge, auto-rotated); cache hit/miss; immutable cache headers; Range request handling (206, `Content-Range`); originals served by id only; tombstoned originals 404.

#### 39. `tests/test_jobs.py` (Create — search increment)

*   **Type / Scope**: Queue order, backoff, `failed_perm` vs `pending`, mid-batch failure marking only failed items.

#### 40. `tests/test_paths.py` (Create — walking skeleton)

*   **Type / Scope**: Unit — path jail checks, symlink skip decisions, folder prefix logic.

#### 41. `tests/test_stress_concurrency.py` (Create — hardening)

*   **Type / Scope**: Threaded browsing requests during a live scanner pass; asserts no lock errors and consistent windows. The one place nondeterminism is accepted.

#### 42. `tests/test_adapter_contract.py` (Create — search increment)

*   **Type / Scope**: Opt-in contract test, skipped unless `PLAPS_ADAPTER_URL` is set; asserts the real endpoint matches the adapter's assumptions (payload, dimensions, normalization). Rationale: the fake adapter covers logic; only this test covers the llama.cpp truth, and only on demand.

#### 43. `docs/spikes/embedding-endpoint.md`, `docs/spikes/lighttpd-sendfile.md`, `docs/spikes/pyvips-exif.md` (Create — spikes, before implementation)

*   **Type / Scope**: Spike outputs recorded as documents: the exact embedding endpoint contract (path, image payload encoding, batch behavior, mmproj flags, normalization, observed latency, llama.cpp version/commit); X-Sendfile availability (verified absent from `mod_proxy`; the compiled-in `mod_fastcgi` implementation is unreachable from a WSGI HTTP backend) and Range passthrough through lighttpd ↔ waitress; pyvips EXIF read and orientation behavior on real library photos. **All three spikes were run on 2026-10-30** — results in `docs/spikes/embedding-endpoint.md`, `docs/spikes/lighttpd-sendfile.md`, and `docs/spikes/pyvips-exif.md`; the design incorporates their findings.

### Pinned defaults (resolves the brief's tuning open question)

| Setting | Default | Rationale |
|---|---|---|
| `scanner.poll_interval` | 30 s, backing off ×2 to 10 min when idle | Seconds-level pickup warm; idle backoff keeps a quiet server quiet |
| `scanner.full_sweep_interval` | 24 h | Bounds in-place-edit blindness to one day |
| `scanner.rehash_interval` | 30 d | Bounds mtime-preserving-copy blindness to a month |
| `scanner.guard_threshold` | 10 % | A legit reorganization is re-linked (not counted); only unre-linked drops trip it |
| `tombstone.purge_after_days` | 30 | Long enough to survive an unnoticed mistake; short enough to not hoard |
| Thumbnails | `sq` 512×512 smart-crop; `as` 512 px max edge; screen-fit 2048 px max edge; WebP q80 | Matches the two grid styles and a ~2×-retina desktop viewport |

### Verification Plan

*   **Install check**: on a clean user account, `python -m venv && pip install -e ".[dev]"` after installing only the documented OS packages (`libvips`, `lighttpd`) — the README runbook must succeed verbatim.
*   **Static analysis & linting**: `ruff check plaps tests` and `ruff format --check` — zero findings.
*   **Unit + API tests**: `pytest` (contract test auto-skipped); `PLAPS_ADAPTER_URL=... pytest tests/test_adapter_contract.py` on demand against the real AI box.
*   **Coverage requirement**: ≥ 85 % branch coverage on `plaps/scanner.py`, `plaps/exif.py`, `plaps/search.py`, `plaps/api/`, and `plaps/embed/` — the identity and search logic. No global gate; the frontend is manually tested by decision.
*   **Manual end-to-end**: against a 1K-photo copy of the real library — originals byte-identical before/after (sampled hash comparison); first-run progress visible; both grid styles render; originals and media download through the lighttpd → waitress chain with working Range scrubbing, and timeline browsing stays responsive during a large original download; search works on the embedded subset with the coverage line; `kill -9` mid-scan resumes from checkpoint on restart.
*   **Performance smoke**: on a synthetic 150K-row database — timeline window p95 < 100 ms warm; search p95 < 50 ms warm on the reference hardware.

