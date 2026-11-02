# Goldfish Review Report — Resolution Record

* **Target:** /mnt/data/workspace/plaps/docs/designs/plaps.md
* **Date:** 2026-10-31
* **Inputs:** consolidated_review_report.md (B1–B7, M1–M9, nits) and owner decisions O1–O7 (2026-10-31)
* **Disposition:** every finding categorized `PROPOSE FIX` or `ACCEPTED RISK` (documented, not fixed). No finding was contested as invalid.

## Owner decisions applied (O1–O7)

| Decision | Applied as |
|---|---|
| O1 — owner-initiated cutover | CUJ 8 rewritten; pipeline section rewritten; `POST /api/admin/model/cutover`; threshold, accounting, job-dedup-for-cutover, and double-residency questions deleted; NFR memory claim corrected (no excursion) |
| O2 — embed original bytes | Pipeline section + Decision Register; screen-fit stays lazy |
| O3 — thumbs eager via worker, screen-fit lazy | B1 resolved: `plaps/worker.py` (one job worker) consumes both `thumb` and `embed` jobs; explicit Modify step for the search increment |
| O4 — dedup to backlog | Non-Goals rewritten (no duplicate handling at all in MVP); Risks & Limits records the double-display/double-embed cost; Future Improvements carries detection + workflow |
| O5 — ATTENTION crop | Serving section, `images.py` entry, defaults table, test entry all pinned to `crop=Interesting.ATTENTION` |
| O6 — TLS per URL scheme | Security section + Configuration validation (scheme must be http/https) + Risks & Limits acceptance |
| O7 — static-alias rejected | New Alternatives entry with pros/cons/reason; serving section references it |

## Blockers — all fixed

* **B1 (thumbnail executor):** fixed via O3 — `plaps/worker.py`, one consumer for both job kinds, explicit Modify step.
* **B2 (delete split):** fixed — data model "Key points" paragraph, CUJ 5, and `scanner.py` entry now define *vanish* (tombstone/revival/guard, most-recently-tombstoned wins, cache removal only when no live record shares the hash) vs. *replace* (hard-delete + cascade over vectors, pending jobs, cache; guard never trips). Fixes Z-8, C-edit, D-11, D-17, CD-M5, CD-M16, R-13, R-19.
* **B3 (index refresh):** fixed — `VectorIndex.add/mask/reload` (append-and-swap), pipeline section, `search.py` entry, and a dedicated test (`test_search.py`: freshness without restart).
* **B4 (cutover re-architecture):** fixed via O1 (above). Fixes Z-4, Z-9, D-3, D-7, C-threshold, C-model_id, Y-6, R-4, CD-M3, ST-5.
* **B5 (degradation + coverage line):** fixed — Goals, CUJ 4/6, pipeline, and `api/search.py` now specify three states (endpoint down → 503 `endpoint_down`; `empty_filter`; `ok`) with filter-scoped coverage; search latency re-scoped to ranking (end-to-end unmeasured, recorded in Risks & Limits). Fixes Z-1, D-1, D-2, C-coverage, IZO-3, CD-M9, R-15.
* **B6 (closure auditable):** fixed — line-58/226 contradiction resolved (Configuration rewritten, no stale parenthetical); new **Risks & Limits** section carries the previously lost content (bulk-copy dating, metadata-edit re-embed, re-hash cost, first-embed unverified, latency scope) plus the new accepted risks; new **Decision Register** lists O1–O7 and the six drafting decisions with dates and reasons. Fixes S-1, S-2, Y-1, IZO-9.
* **B7 (dedup):** fixed via O4 (above). Fixes IR-M7, ST-1, C-dup-destination.

## Majors — all fixed

* **M1 (artifact pipeline):** fixed via O2/O3/O7 — one pipeline decision stated in the serving section; static-alias weighed and rejected in Alternatives.
* **M2 (embedding mechanics):** fixed — batch failure → serial per-item retry (pipeline + worker + test); job dedup via partial unique index (schema + scanner + test); retry pins (×2, cap 1 h, 5 attempts) in defaults table and test; `model_id` authority = config (pipeline + adapter + register).
* **M3 (client contract):** fixed — response-contract paragraph in the API surface (photo card incl. width/height, error shape, folder-node shape, folder-id spec with separator-safe matching, cursor sentinel encoding, `limit`/`order` values, inclusive dates); settings → localStorage (Goals, Non-Goals, register); Android justification reduced to two mentions.
* **M4 (config & ops):** fixed — required-vs-default matrix with validation ranges; `timezone` required with owning module (scanner, `zoneinfo`); `dirs` table for stat-pruning; backup trigger (`plaps backup` + systemd timer + restore test); systemd unit fixed (venv path, `--config`, `PLAPS_CONFIG`); static deployment step (symlink) in README scope and install check; stall defined (`scan_stalled`, 15 min) and keyed by `status.js`; counter ownership (writer maintains); probe pinned (2 s / 60 s).
* **M5 (folder view):** fixed — memoized in-process tree invalidated on scan commit (`api/folders.py`), < 200 ms target in API table and NFR and perf smoke.
* **M6 (sq crop):** fixed via O5.
* **M7 (TLS/CSRF):** fixed via O6 + a CSRF sentence for the bodyless admin endpoints.
* **M8 (structure/drift):** fixed — "proxy password authentication (digest on the target server)" terminology throughout; `/api/status` everywhere; "queue depth"; Additional Resources and Future Improvements promoted to H2; component map redrawn (cache attached to the service, labeled edges, `dirs` in the DB box); concurrency stress test added to the verification Goal (S-7 antecedent); phasing rewritten with explicit Modify steps (worker, status, folders); spikes reworded as records; relative paths fixed (`../../concepts_brief.md`, `../spikes/…`); provenance note corrected (three draft/review alternatives + the X-Sendfile reclassification remark).
* **M9 (compressed passages):** fixed — guard baseline at first mention (CUJ 7); old-model query-embedding mechanism stated in CUJ 8 and the pipeline; "a progress indicator the user can dismiss"; inotify summary reworded; "correct" removed; latency framed as target everywhere it appears.

## Nits — fixed except where noted

Fixed: per-stage error slots (`exif_error`/`thumb_error`/`embed_error`); `date_taken NOT NULL` with sentinel + sort placement; `plaps/embed/__init__.py` and `plaps/paths.py` added; `images.py` signatures take source path + hash; `make_synthetic_db.py` added; coverage gate extended (`config.py`, `db.py`, `images.py`, `paths.py`); install check executable; "responsive" → the NFR number; grid-style persistence clause; concurrent cache writes (temp + atomic rename); dead vectors removed at replace/purge/cutover; `batch_size` knob removed (constant); CUJ 1 window stated as unmeasured; CUJ 3 freshness wording; duplicate embed cost recorded; `hashlib.file_digest` and `thumbnail_buffer()` reuse adopted; style items (banned words, "bricks," "Branded," "felt need," telegraphic phrasings, "machinery"/"surface" trimmed where they appeared most).

Accepted, not changed (recorded here): "machinery"/"surface" retain a few legitimate uses; the timeline virtualization stays hand-rolled with a vendored substitute noted as acceptable; the monthly re-hash cost is accepted (Risks & Limits) rather than made configurable-beyond-interval.

## Verification of the pass

Line 58 vs. 226 contradiction: resolved (Configuration section rewritten; Open Questions points at the Decision Register). All seven owner decisions traceable in the text. The document grew by the Risks & Limits and Decision Register sections and shrank by the deleted cutover-threshold machinery. Recommended next step, per the consolidated report: re-run Stage 9 (implementation readiness) on the revised DetailedRange.
