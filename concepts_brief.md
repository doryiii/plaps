# plaps — On-Disk Semantic Photo Gallery

* **Author:** Project owner (brainstormed with EGM Goldfish review)
* **Date:** 2026-10-29 · Revised 2026-10-30 after a second Goldfish review (six lenses: design, prior-art accuracy, scope, comprehension, implementation readiness, structure)

## Overview

A single-user, self-hosted photo gallery web app — a Google Photos-style experience whose photo source is an existing library of ~150K JPEG files on local disk, not uploads. The server indexes the library in place: originals are never copied, moved, or renamed; only derived caches (WebP thumbnails and screen-fit "large" images) are stored separately. It runs as a plain Python service (Flask behind a lighttpd reverse proxy) with SQLite on a home server — no Docker, no NodeJS anywhere, no build toolchain.

**Simplicity and ease of maintenance and installation are explicit design goals.** Standing rule carried into the draft: any feature must be small enough for one person to own indefinitely; if it is not, it does not go in.

MVP features: (1) a flat timeline of all photos sorted by date taken, (2) a folder view with arbitrarily nested folders, each folder's photos sorted by date and the folder list itself sorted by date, and (3) semantic text-to-photo search ranked by similarity with any active date/folder filter applied before ranking, over a verified embedding model (Google's EmbeddingGemma 2, 2026, joint text+image embeddings; served via llama.cpp GGUF on a separate AI box through one adapter module). Password authentication is handled entirely by basic auth at the lighttpd reverse proxy; the app itself ships no authentication code in the MVP. From first run the UI serves whatever is already indexed, with an optional progress indicator during the initial scan.

Later phases: content-similarity auto-grouping, face detection and grouping, map view, read-only share links, Android app with directory auto-backup, an app-level cookie login page (backlog until share links or a second user make it necessary), and (as a novelty, not a need) browser-side query encoding.

## Analysis

The library is add-mostly: additions dominate, and deletes, renames, and in-place edits are rare but must be handled as first-class (if uncommon) events — the change-detection and deletion machinery below exists for that reason, not because deletes are frequent.

### Scanning

The background scanner walks the full directory tree on a schedule and:

* **Prunes file stats, not traversal.** It descends the whole tree but skips `stat`-ing files inside directories whose own mtime is unchanged. (Pruning traversal itself would be wrong: a file added to `2019/Italy/beach/` changes `beach/`'s mtime but no ancestor's, so a scanner that stops at unchanged ancestors would never reach it.)
* **Runs a daily full stat sweep** as a backstop. In-place edits change the file's mtime but never the parent directory's, so mtime-pruned scans cannot see them; the daily sweep bounds how long an edit goes unnoticed.
* **Runs a low-frequency (roughly monthly) re-hash pass** to catch mtime-preserving copies (`rsync -a` restores, same-size rewrites with coarse mtime granularity). Cost is background I/O over 150K files — hours, tolerated.
* **Detects deletes, never at read time.** Deletion marks the record with a `deleted_at` timestamp (a column, not a separate lifecycle). A record revives if the same content hash reappears within the purge window — preserving its embedding and any future favorites/albums. Before tombstoning a vanished path, the scanner checks whether that content hash reappeared at another path in the same scan pass and treats that as a move: re-link, not delete.
* **Guards against mass-delete accidents.** If the library root is unstat-able, or the unre-linked file-count drop exceeds a threshold, the deletion pass halts, logs, and surfaces on `/status`; resuming requires manual action. This protects against config mistakes (root pointed at the wrong directory) at the cost of one stateless check.
* **Skips symlinks entirely.** Not followed for files or directories. This removes double-counting, realpath ambiguity (which path gets recorded), and jail-escape concerns in one rule. Accepted limitation; the owner does not curate via symlinks today.

### Identity

Decided: **one record per path; the content hash is a non-unique indexed column.** Two byte-identical files at different paths are two records — the timeline shows both until a dupe workflow exists, and the dedup detector reports them as a pair. Re-linking on move is a hash match. This resolution also settles the prerequisite for the future Android backup phase, which will manufacture duplicates under live data.

* Move/rename with unchanged content re-links to the existing record; metadata survives.
* Same-path content change = delete the old record, add a new one (owner decision). **Stated consequence:** this keys on file bytes, not pixels — a lossless EXIF rotation or metadata strip discards the old record's derived images and queues a re-embed, and a bulk metadata edit over the library means a day-scale re-embed. Accepted for now; a pixel-level or perceptual hash could refine this later (noted in Deferred, not adopted).

### Vectors and search

* 150K × 768-dim is a small vector problem. Vectors live as blobs in SQLite (single-file backup covers the whole index), and at startup the server loads them into a resident in-memory float32 matrix (~460 MB at full scale) for brute-force cosine search via numpy — tens of milliseconds per query. sqlite-vec is dropped: it does not accept float16, and once the matrix is resident it adds nothing. No ANN before ~1M photos.
* The adapter normalizes vectors at write time, defensively — llama.cpp embedding servers do not guarantee normalized output. No per-vector "normalized" flag; there is one legal behavior.
* `model_id` is stamped per vector.
* Search applies any active date/folder filter before ranking (a WHERE clause, not a subsystem), then ranks by similarity. Photos without a current-model embedding are silently excluded from ranking — and the results view carries one count line, "search covers N of M photos," sourced from the `/status` counters, so "nothing matched" is distinguishable from "nothing embedded" without a banner subsystem.
* **Model migration must not create a search outage.** On a model change, old-model vectors stay searchable while the re-embed runs through the *existing* job queue (no separate migration pipeline — a second embedding path would rot between uses); the active `model_id` cuts over when new-model coverage crosses a threshold, and old vectors are deleted after cutover. Note: old-model text and image vectors are mutually consistent, which is what makes the overlap window valid.

### Embedding pipeline

A SQLite job table drives embedding: one row per file, oldest-first, with per-file error rows carrying an error kind (thumbnail / hash / embed) and text, and a pending-vs-permanently-failed distinction. Retries back off; a mid-batch adapter failure marks only the failed items. First full embed at the observed 0.1–1 s/photo is roughly 4–42 hours — stated here so the expectation exists before the first run. When the AI box is down, browsing is unaffected and search covers only what is embedded; `/status` exposes adapter reachability and the last-successful-embed timestamp so a dead AI box is distinguishable from "no results" at a glance.

### Serving

* Derived images are **keyed by content hash plus variant** — an edited photo is a new hash, so its cache entry can never be stale. The thumbnail cache holds two variants per photo — square-cropped (for the uniform grid) and aspect-preserving (for justified rows); screen-fit images are shared between styles. Thumbnails target ~512 px max edge, screen-fit images ~2048 px max edge (exact pixel and WebP quality numbers are draft-level). Because keys never change, derived images are served with long-lived immutable cache headers.
* Originals are served via lighttpd **X-Sendfile**: Flask emits the header, lighttpd does the byte streaming, range handling, and `sendfile()`. Python never streams multi-megabyte files through a WSGI worker (each pinned worker during a large download would degrade the whole app).
* The cache lives outside the library, in a configurable location, unbounded by policy. Expected magnitude stated so disk provisioning is a decision, not a surprise: roughly 35–70 GB at 150K photos with both thumbnail variants (two thumbnails ~30–50 KB each plus one screen-fit image ~200–400 KB per photo). Orphaned entries (hashes whose records are gone) are removed when their records purge.

### Frontend

No-build ES modules (vanilla or a lightweight vendored library; no bundler, no npm) rendering client-side against a JSON API. The client-rendered choice stands on its own: a virtualized timeline over 150K items wants client rendering, and the JSON API happens to also serve the future Android client. The API is cursor-paged with an opaque composite cursor — (date_taken, photo_id) — so ties and mid-scroll insertions are stable while the scanner writes. "No pagination" means no pagination UI, not no pagination API: shipping all metadata (~20+ MB JSON) is not viable.

**UI design is deliberately staged after the server.** Visual and interaction design happens against a working backend serving real photos — real aspect ratios, date density, and folder shapes are inputs the UI needs and doesn't exist on screen yet, and the single user can iterate cheaply. The JSON API is the seam that makes deferral safe. One UI decision was resolved up front because it is baked into the thumbnail cache: **both grid styles ship as a user option** — a uniform square grid (center-cropped thumbnails, Google-Photos-style) and justified/masonry rows (aspect-preserving thumbnails). The cache therefore carries two thumbnail variants per photo, keyed by (content hash, variant), roughly doubling thumbnail storage — which the owner accepts. Everything else (scrubber, viewer gestures, folder-tree presentation, map and grouping UI) is additive endpoints plus frontend work.

### Dates

Sort key is EXIF `DateTimeOriginal` (that specific field), read by an explicit EXIF reader — piexif, a single pure-Python file that fits the no-toolchain constraint — not left as an unstated gap in the dependency list. Malformed EXIF routes to the per-file error path and falls back to file mtime. Timestamps are treated as local wall-clock in the server's configured timezone; a server set to UTC will mis-sort the whole library, so the timezone is an explicit config item. UTC-normalized storage was considered and rejected: a single-user, effectively single-timezone library does not justify timezone-inference machinery, and the later map view can revisit per-photo GPS-derived zones. Known accepted failure: files without EXIF that were bulk-copied date to the copy event (the most common "my timeline is wrong" report in self-hosted galleries).

### State model

There is no single lifecycle enum. Scanning, thumbnailing, and embedding are independent pipeline stages with independent failure modes, so each record carries per-stage nullable columns (`thumbnailed_at`, `embedded_at`, error kind/text, `deleted_at`) and derived predicates. "Changed" is an event (delete + add), not a state; a one-enum-fits-all model would need transition rules that cannot be written consistently.

### Folders

The folder tree is derived from indexed paths at query time (GROUP BY, cacheable per view), not maintained as a separate table — a maintained tree drifts (ghost folders after renames, stale empty dirs) and 150K rows is well within SQLite's reach. A folder's date is the newest date-taken among its photos, computed recursively over subfolders; folders with no photos anywhere beneath them are hidden.

### Security and operations

* Library-root jail for all file serving plus the no-symlinks rule; basic auth at the lighttpd proxy covers the entire MVP threat model — the app ships no authentication code, and basic auth also serves a future Android client. An app-level cookie login page is backlog; share links or any second user are its trigger. Remote exposure requires TLS at the proxy.
* SQLite runs in WAL mode with a busy timeout and a single writer connection shared by scanner and embedder; `VACUUM INTO` backups run with the writer quiesced. Losing the database costs a rescan plus a multi-day re-embed, which is why a stated backup cadence matters. The restore path is: stop service, restore the DB file, restart, scanner reconciles against disk; the cache directory is regenerable and may be deleted freely. Schema evolution uses SQLite `user_version` with one-way migrations.
* `/status` reports: scan progress, embedding queue depth and error counts by kind, cache size, active `model_id`, adapter reachability, last-successful-embed timestamp, lock-wait counters.
* Packaging: venv, systemd unit, libvips as a documented OS package, a lighttpd config sample (including the basic-auth block). No Docker, ever.
* First run: the UI serves what is indexed from the start, with an optional progress indicator during the initial metadata scan; search coverage grows as embedding proceeds, carried by the results-view count line. The scanner must survive crashes and resume, and a stall or crash is a visible error state — never a hung or bricked UI.
* Dependency surface: Flask, piexif, pyvips, sqlite3, httpx, and one WSGI server (named in the draft). The embedding adapter is one module — `embed_images()`, `embed_text()`, `model_id` — chosen in code, not config; llama.cpp-specific quirks (endpoint shape, image payload encoding, batching, mmproj flags) live only there.

### Testing

The suite is API-level by default: pytest drives Flask's in-process test client (no network, no server process, no Docker) against a temp library tree, asserting on JSON responses, served image bytes, and — where it matters — that the library files themselves are unchanged. Around that core:

* **Scanner behavior is tested through real temp directory fixtures:** create, rename, delete files and set mtimes (`os.utime`), run a scan pass, assert through the API. Non-portable filesystem edge cases (sub-second mtime granularity, coarse-mtime filesystems) are documented as covered by the monthly re-hash backstop rather than faked in tests.
* **The embedding adapter is faked with controlled vectors**, which makes ranking order deterministically assertable (impossible against a real model). One opt-in contract test, skipped by default, exercises the real llama.cpp endpoint; the spike already staged captures its request/response shapes.
* **A thin unit layer covers the pure logic:** EXIF `DateTimeOriginal` parsing against malformed inputs, cursor encoding, the re-link decision table, the path jail.
* **A few concurrency stress tests** exercise browsing during a scanner pass to expose SQLite locking bugs; everything else stays serial and deterministic.
* **The frontend is manually tested:** no Node means no headless-browser tooling without breaking the no-toolchain constraint, and the frontend is a thin renderer over the tested API.
* **Local-run only:** `pytest` (a `make test` alias only if commands accumulate), no CI infrastructure, no hosted runners.

### Corrections to the prior-art record

Two rejection reasons stated during ideation were factually stale, and the build decision rests on corrected grounds:

* **Immich** has supported *external libraries* (in-place registration of on-disk paths, originals untouched) since late 2023. "Requires upload/import" was outdated. The rejection stands on Docker + TypeScript grounds — and Immich's external-library, read-only design is the best existing blueprint for this project's ingestion model.
* **PiGallery2** is not Docker-only; it is an npm-installable Node app. The rejection stands on NodeJS grounds and its lack of semantic search.

## Goals vs. Non-Goals

* **In-Scope Goals (MVP):**
  * In-place indexing of ~150K local JPEGs; stat-based change detection (path + mtime + size) with a daily full-sweep backstop; one record per path with a non-unique content-hash column; hashes for re-link and detect-only dedup
  * Flat date-sorted timeline, virtualized (no pagination UI; opaque composite cursor API), serving what is indexed from first run with an optional progress indicator
  * Folder view derived from indexed paths, arbitrary nesting, folders sorted by newest contained photo date
  * Semantic search: active date/folder filter applied before similarity ranking, via the EmbeddingGemma 2 adapter; server-side query encoding; silent exclusion of un-embedded photos with a coverage count line in the results view; model cutover without search outage
  * Cached WebP thumbnails and screen-fit images keyed by content hash in a configurable cache directory outside the library; originals served via lighttpd X-Sendfile, download available but not the default view
  * SQLite metadata + vector store in WAL mode; Flask behind lighttpd with proxy basic auth and no app-level auth; no Docker, no NodeJS, no build toolchain
* **In-Scope Goals (later phases — a deliberate third bullet beyond the template's two):** content-similarity grouping, face grouping, map view, read-only share links, Android app with auto-backup, app-level cookie login page (backlog; trigger is share links or a second user), browser-side query encoding (novelty; model-version coupling must be handled if built)
* **Out-of-Scope Non-Goals:** photo editing, live photos, RAW, video, multi-user accounts/permissions, app-level authentication in the MVP (proxy basic auth covers it), upload as a primary ingestion path, remote/network-mounted filesystems, symlinks in the library, ANN vector indexes, message-queue infrastructure, any Node/npm tooling, dupe-handling workflow (detect-only for now)

## Alternatives Weighed

*(Rejected options with reasons. Deferred items live under Key Decisions, not here. `/egm-draft` copies this section as its only source for Alternatives Considered.)*

* **Immich (external libraries, read-only mode)** — closest full feature set including true semantic search; registers on-disk paths without touching originals. Rejected: Docker + TypeScript stack, heavy for one user, and the owner wants code ownership. Its external-library model is the blueprint to copy.
* **LibrePhotos** — Python, in-place scanning, semantic search already implemented. Rejected: Django + Postgres + workers, Docker-first operations, multi-user machinery. Best reference implementation for the embedding indexer.
* **PhotoPrism** — label/keyword search rather than joint text+image embeddings; expects a specific directory layout. Rejected on both grounds.
* **PiGallery2** — npm-installable, not Docker-only. Rejected on NodeJS grounds and lack of semantic search. Its directory-mtime scan cache (stat-pruning variant) is worth borrowing.
* **Photoview** — Go single binary, in-place scan, face grouping, no semantic search. Rejected on language and missing centerpiece; evidence the niche is viable.
* **Photofield** — Go, very fast zoomable timeline over a raw filesystem. Rejected: Go, no semantic search; useful reference for virtualized-timeline UX.
* **Remote/network mount support** — dropped after the first review: it forced mount-health machinery for little owner benefit. Local disks only.
* **Go instead of Python** — rejected as primary: maintainability dominates at this scale and heavy lifting (libvips, hashing) happens in C anyway. Kept as a profiling-gated fallback via clean module boundaries.
* **FastAPI/Starlette instead of Flask** — rejected: the owner prefers the simplest framework; a single-user, read-mostly JSON + file-serving workload does not need async.
* **FAISS/hnswlib ANN** — rejected for MVP: brute force over 150K vectors is tens of milliseconds. Revisit near ~1M photos.
* **sqlite-vec** — rejected: it does not accept float16, and once vectors are resident in a float32 numpy matrix at startup it adds nothing over brute force.
* **Separate vector store / sidecar files** — rejected: blobs in SQLite keep single-file backup and one fewer dependency.
* **Celery/Redis job queue** — rejected: a SQLite job table plus a worker loop is sufficient with no extra moving parts.
* **inotify/fsnotify file watching** — rejected: watch-lifetime and overflow edge cases; polling with stat-pruning plus a daily full sweep is simpler and catches in-place edits reliably.
* **Pillow** — rejected in favor of pyvips for thumbnailing (faster, releases the GIL, streaming decode).
* **Server-rendered HTML (htmx-style)** — rejected: a virtualized timeline over 150K items wants client rendering; the JSON API also serves the future Android client.
* **UI lock until the first index completes** — rejected after the second review: the unlock condition was ambiguous by ~40 hours (metadata scan vs. embedding pass), and a scanner crash mid-scan would leave a hung lock screen. The app serves what is indexed, with an optional progress indicator and visible error states, instead.
* **App-level login page (cookie sessions) in the MVP** — rejected: lighttpd basic auth covers the single-user threat model, serves the future Android client, and costs the app zero authentication code. The login page moves to the backlog; share links or a second user are its trigger.
* **Coverage banner as a subsystem** — rejected as machinery; one count line in the results view (adopted) carries the signal.
* **Tombstone-free immediate deletes** — rejected: an accidental mass-delete would cost a full rescan plus a multi-day re-embed; a `deleted_at` column with a purge delay is the cheap insurance.
* **A single file-lifecycle state enum** — rejected: it conflates pipeline stages with events and cannot express hash re-linking; per-stage columns with derived predicates replace it.
* **A scanner-maintained folders table** — rejected: denormalization with drift risk (ghost folders, stale empties); derive the tree from indexed paths at query time.
* **UTC-normalized timestamp storage** — rejected: single-user, effectively single-timezone library; timezone-inference machinery costs more than it returns. The map-view phase can revisit per-photo GPS-derived zones.
* **One photo per content with a locations table (vs. one record per path)** — rejected: more schema and split delete semantics to avoid displaying exact duplicates twice, which the owner accepts until a dupe workflow exists.

## Key Decisions & Staging for Next Steps

Anchored decisions:

1. Python + Flask + SQLite + pyvips + piexif, behind lighttpd; no Docker; no NodeJS or build toolchain; simplicity and installability are design goals; every feature must be ownable by one person indefinitely.
2. Stat-based scanning (path + mtime + size) with stat-pruning (never traversal-pruning), a daily full stat sweep, and a roughly monthly re-hash pass; deletes detected by the scanner; `deleted_at` tombstones with purge delay, revival on hash reappearance, same-pass move re-linking; mass-delete guard halting deletions until manual resume; local filesystems only; symlinks skipped.
3. Identity: one record per path; content hash as a non-unique indexed column; move/rename re-links via hash match; same-path content change = delete old + add new (byte-keyed — stated consequence for metadata-only edits accepted); exact duplicates display twice until a dupe workflow exists; dedup is detect-only.
4. WebP thumbnails and screen-fit images keyed by content hash and variant, cached in a configurable directory outside the library (unbounded, expected 35–70 GB with both thumbnail variants); both grid styles — uniform square (center-cropped) and justified rows (aspect-preserving) — ship as a user option; immutable cache headers; originals via lighttpd X-Sendfile with range support.
5. EmbeddingGemma 2 via a single adapter module (server-side text encoding, defensive normalization, `model_id` stamped per vector); search applies the active date/folder filter before similarity ranking; silent exclusion of un-embedded photos with a coverage count line in the results view; resident in-memory float32 matrix for brute-force search; model cutover waits for coverage threshold — no search outage, no separate migration pipeline.
6. EXIF `DateTimeOriginal` as local wall-clock in a configured server timezone, mtime fallback with per-file error rows for malformed EXIF; virtualized pagination-free UI over an opaque (date_taken, photo_id) cursor API; the UI serves what is indexed from first run with an optional progress indicator (crash-resume, stall, and error states required); basic auth at the lighttpd proxy only — no app-level auth in the MVP.
7. SQLite in WAL mode, single writer for scanner+embedder, `VACUUM INTO` backups with a stated cadence and a documented restore path; `user_version` one-way schema migrations; `/status` exposes scan progress, queue depth, error counts by kind, cache size, model state, adapter reachability.
8. Testing is API-level via Flask's in-process test client against temp library trees; fake embedding adapter with synthetic vectors plus one opt-in real-endpoint contract test; a thin unit layer for EXIF parsing, cursor encoding, re-link decisions, and the path jail; a few concurrency stress tests; frontend manual; local-run only (`pytest`), no CI.

Deferred (not rejected — revisit when the trigger arrives):

* Browser-side query encoding (transformers.js): novelty only; requires model-version coupling with server embeddings.
* Pixel-level or perceptual hash to survive metadata-only edits: trigger is the first real lossless-rotation or bulk-EXIF-strip re-embed.
* App-level cookie login page: trigger is share links or any second user.
* Dupe-handling workflow: trigger is living with displayed duplicates in practice.
* Android auto-backup: the duplicate decision (settled as one-record-per-path) defines how backup-manufactured duplicates are handled; revisit before that phase ships.
* Go rewrite: profiling-gated only.

Spikes to run before or during early drafting (each hours, not days):

1. llama.cpp image+text embedding HTTP contract on the AI box: exact endpoint (`/embedding` vs `/v1/embeddings`), image payload encoding, batching support, mmproj flags, normalization behavior, observed latency; record the llama.cpp version/commit. Capture the request/response shapes in the adapter spec.
2. One large-file HTTP range request through Flask + lighttpd, and X-Sendfile behavior through the same stack.
3. pyvips EXIF read and orientation behavior on real photos from the library.

Suggested increment ordering for the draft: (1) spikes; (2) walking skeleton — schema, config loading, scanner, `/status`, timeline cursor API, on-demand WebP thumbnails, minimal ES-modules grid serving what is indexed; (3) folder view; (4) search — adapter, job worker, search API + UI with the coverage count line, model cutover; (5) hardening — root jail, originals via X-Sendfile, tombstone purge + guard, packaging, backup/restore documentation, lighttpd basic-auth config sample.

Open questions:

1. **Config surface.** File format and location, cold-start behavior with no config, defaults for purge delay, guard threshold, timezone, and scan intervals — draft-level, but the walking skeleton needs it first.
2. **Tuning values.** Tombstone purge delay (suggest 30 days), unre-linked drop threshold for the mass-delete guard (suggest 10%), scan interval (suggest continuous with backoff, daily full sweep, monthly re-hash).
