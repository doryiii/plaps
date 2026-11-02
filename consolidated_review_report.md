# Consolidated Review Report — plaps design document

* **Target:** /mnt/data/workspace/plaps/docs/designs/plaps.md (601 lines; FeatureRange 1–380, DetailedRange 381–601)
* **Mode:** Consolidated report, all review rubrics, no checkpoints
* **Date:** 2026-10-30
* **Stages executed:** 5 (Initial Zoom Out), 6 (Comprehension), 7 (Critic: design/structural/style), 8 (Intermediate Zoom Out), 9 (Implementation Readiness: readiness/codebase-details/style)
* **Reviewer status:** all 8 reviewer dispatches ran and reported; none "not run." All dispatched serially per the single-slot inference-server constraint; each had a fresh context.
* **Stage reports:** `zoom_out_report.md`, `comprehension_report.md`, `critic_report.md`, `intermediate_zoom_out_report.md`, `implementation_readiness_report.md`

## Overall verdict

**The architecture survives; the document is not ready for implementation.** No finding at any stage pushes toward any of the rejected machinery — every architectural recommendation points toward *less* machinery than the document promises. The load-bearing decisions (stat-pruned in-place scanning, hash identity with one record per path, one process/one writer, SQLite-only storage, content-hash-keyed cache, brute-force resident-matrix ranking, proxy-terminated auth, no watcher/queue/Docker) are untouched. The plan's spike-backed claims verified accurate, and its file/test inventories are near-complete. But eight reviewer dispatches found one genuinely missing subsystem, one mechanism that should be re-architected rather than patched, and enough unpinned load-bearing decisions that a cold implementer would be designing, not implementing, in at least a dozen places. The document is strongest where it reasoned from verified results and stated hard problems (identity, media serving, the inotify rejection) and weakest in the subsystem attached to the one external dependency — availability, cutover, index freshness, and measurement all cluster on the embedding path — and in the seams it declared "contracts" (the API, the pipeline stages) without specifying.

## Consolidated action items

### 🔴 Blockers — must be resolved before implementation

**B1. Assign the thumbnail-job executor.** Scanner enqueues `thumb` jobs; no file consumes them; the design says "at scan time." Three buildable variants with different threading and crash-resume behavior. One sentence closes it — or delete the `thumb` kind and state all derived images generate on demand.
*Traceability:* IR-B1, CD-B1, ST-2; design line 201.

**B2. Split "delete" into two operations with one explicit cascade.** *Vanish* (tombstone, revival window, guard, purge) vs. *content-change-at-same-path* (record replacement that must bypass the tombstone path because `path` is UNIQUE). As written the most safety-relevant event in the system is unimplementable: an in-place edit can't tombstone (blocks the insert), and nothing states what cascades to `photo_id`, the vector row, cache entries, and jobs. Also fixes: purge must not delete cache entries shared with a live duplicate; revival-at-a-new-path and which-of-two-same-hash-tombstones-revives need rules; the guard's scope is vanish-only.
*Traceability:* Z-8, C-edit, D-11, D-17, IZO-4, CD-M5, CD-M16, R-13, R-19.

**B3. Give the resident vector index a refresh path.** The design loads the matrix at startup and cutover only — but the steady state is continuous: search would rank against a startup snapshot (typically zero photos) for the entire first embed, and never see new photos or tombstones afterward. Append-and-swap on worker write, mask on tombstone; the same immutable-swap primitive serves cutover. The only missing subsystem the review found.
*Traceability:* IZO-2 (sole source — missed by Stages 5–7).

**B4. Re-architect model cutover from automated threshold to owner-initiated.** The threshold mechanism generated seven unresolved decisions across four stages (unpinned value, `failed_perm` accounting, job dedup, `model_id` authority, old-model query embedding, ~920 MB double-residency vs. the <1 GB NFR, "search never goes dark" contradiction). For a single operator, the simpler architecture deletes five specifications and one impossibility: re-embed through the existing queue, report coverage on `/api/status`, owner flips the model (config or one admin action) accepting a brief announced search pause. **Owner decision required** (it rewrites CUJ 8 and the NFR).
*Traceability:* Z-4, Z-9, D-3, D-7, C-threshold, C-model_id, Y-6, IZO-1, R-4, CD-M3, ST-5.

**B5. Restate the AI-box-down degradation story and redesign the coverage line together.** "Search serves whatever is embedded" is unimplementable — text queries need the same dead endpoint. Honest form: endpoint down → browsing/folders/originals unaffected, search unavailable with a distinct visible state. The coverage line must then distinguish three states (endpoint down / nothing embedded in this filter / nothing matched) and be **filter-scoped** — global counters alongside filtered results mislead in exactly the window the feature exists for. These two fixes interact; landing them separately costs three review cycles on one line of UI.
*Traceability:* Z-1, D-1, D-2, C-coverage, IZO-3, CD-M9; also re-scope the search latency target (ranking-only, or measure standalone text-embed latency first) — D-2, R-15, IZO-7.

**B6. Resolve the Open-Questions contradiction and make closure auditable.** Line 58 ("none currently; tuning values pinned") vs. line 226's stale parenthetical ("tuning values remain Open Question 2") — three stages falsified the closure independently, and at least eight decisions were genuinely open (cutover threshold, embed input, edit mechanics, guard arithmetic, job uniqueness, screen-fit ownership, settings home, response schemas). Fix the stale line, add the missing **Risks & Limits** section (the standard requires it; its absence already dropped the brief's accepted bulk-copy dating failure and buried the re-measure-on-AI-box prerequisite), and replace scattered "resolved" claims with a decision register covering exactly the items below.
*Traceability:* S-1, S-2, Y-1, IZO-9.

**B7. Decide duplicate detect-and-report: in or out.** The design says "detected and reported," the brief says "detect-only is in scope," and the plan contains no implementing file, endpoint, status field, or test. Either give it an owner and a test or remove the claim. **Owner decision required.**
*Traceability:* IR-M7, ST-1, C-dup-destination.

### 🟡 Major — resolve in the revision pass

**M1. One decision for the derived-artifact pipeline, not four patches.** Which artifacts are eager, which lazy, and therefore what the jobs table and per-stage columns contain. The four individual fixes conflict: screen-fit has no owner/job kind/column; embed-input-as-screen-fit forces screen-fit eager; eager-vs-lazy for the two thumbnail variants reopens under the style toggle; the lighttpd static-alias option for `/media/` works cleanly only against a complete cache (and must be weighed as an alternative either way). **Owner decision required** (see O2/O3/O7).
*Traceability:* IZO-5, Z-7, C-screenfit, D-4, D-6, R-3.

**M2. Pin the embedding pipeline mechanics.** Per-item batch-failure attribution strategy (never demonstrated by the spike — serial fallback, bisection, or one-per-item); job dedup rule (UNIQUE(photo_id, kind) or upsert-on-pending); `failed_perm` attempt count and backoff base/cap; `adapter.model_id` authority (config vs. endpoint probe, and what happens on disagreement).
*Traceability:* CD-M1, CD-M6, R-5, R-7, R-14, Z-5, C-model_id, CD-N1, IR-22.

**M3. Specify the client contract.** Photo card fields (must include `photo_id`, `content_hash`, `width`, `height`/aspect, `date_taken`, `date_source` — the justified-rows grid cannot lay out without aspect before images load), the uniform error shape, folder-id format (relative prefix, separator-safe matching, root node), NULL-date cursor encoding and sort placement, `limit`/`order` legal values, date-filter formats and inclusivity. Put grid style and sort order in client-side localStorage (the YAGNI-minimal settings home for one user with one browser) and retire the four near-verbatim Android-client justifications to one.
*Traceability:* R-8, R-10, R-11, C-schemas, C-cursor, C-folder-ids, D-5, S-6, Y-2, Y-31, IZO-6.

**M4. Complete the config and operations surface.** Required-vs-default matrix and validation ranges (`timezone` is load-bearing — a UTC server mis-sorts the library; unset `timezone` should refuse like `library_root`); the timezone application point for the mtime fallback (owning module, `zoneinfo`); persisted directory mtimes for stat-pruning (schema home, or pruning degrades to full sweeps after restart); backup trigger and test (nothing schedules `VACUUM INTO`); systemd unit as written cannot start the service (venv absolute path, config path, named env var); static-file deployment step; "stall" definition; counter ownership; probe timeout/TTL.
*Traceability:* R-6, R-12, R-16, R-17, CD-M2, CD-M4, CD-M7, CD-N5, ST-7, ST-10, ST-17, IR-16/17, ST-16.

**M5. Folder view needs a target and an approach.** The heaviest read path (recursive newest-date over 150K paths per request) has no latency budget and no algorithm; the brief's "cacheable per view" was dropped in drafting. One sentence — in-process memoized tree, invalidated on scan commit — plus a target.
*Traceability:* D-9, CD-M14, R-10, IZO-7.

**M6. Reconcile the `sq` crop conflict.** Design pins `crop=Interesting.CENTRE` (verified); the plan says "smart-crop" twice, which in libvips vocabulary is ATTENTION — the option the spike left draft-level and the design rejected; the test cannot detect the divergence. **Owner decision required** (CENTRE per the design is the default answer).
*Traceability:* R-9, D-18, Z-10, C-crop, ST-3.

**M7. TLS for the embedding channel.** The threat model demands TLS for serving photos while streaming every original, base64 and unencrypted, to the AI box as ordinary operation. Require `https://` for `embedding.base_url` or write the LAN-plaintext acceptance down. **Owner decision required.** Also one sentence on CSRF for the sole mutating endpoint.
*Traceability:* D-8, IZO-8, D-18.

**M8. Fix the document's structure and drift.** Auth terminology (say "proxy password authentication — digest on the target server" once, reference it); `/status` vs `/api/status` naming; "queue state" vs "queue depth"; Additional Resources and Future Improvements nested as H3 under NFR (Future Improvements is load-bearing — cross-referenced at line 302); component map attaches the cache to the DB box with an unlabeled edge; the concurrency-stress-test antecedent missing from the verification goal; phasing — increment labels contradict the brief's order and a Create-only list cannot express the cross-increment modifications the plan itself requires (explicit modify steps needed); spike entries marked "Create" though they exist; relative resource paths dead from the document's location; Alternatives provenance undercount and the missing X-Sendfile reclassification remark.
*Traceability:* S-3, S-4, S-5, S-6, S-7, S-8, S-9, D-10, Y-3, C-relative-paths, R-18, IR-13/14, CD-M10/M11, ST-8, ST-23, CD-N7.

**M9. Rewrite the compressed passages that hide mechanisms.** The guard threshold's baseline at first mention (CUJ 7); the old-model query-embedding mechanism ("mutually consistent" hides the rule an implementer could get wrong); "an optional progress indicator" (not actionable in a Then-clause); the inotify rejection summary; "correct" (banned); search latency stated as fact in four places where only the NFR frames it as a target.
*Traceability:* Y-4, Y-5, Y-6, Y-7, Y-8, Y-9, D-2, D-16, Z-6.

### 🟢 Nit — fold into the same editing pass

Error slots: one column pair per stage, not a single `error_kind`/`error_text` (Z/IZO-10); `date_source='unknown'` reachability; missing `plaps/embed/__init__.py` and `plaps/paths.py` (implied by `test_paths.py`); `images.py` signatures need source-path/hash arguments; perf-smoke generator needs an owning file; coverage gate should include `images.py` and `db.py`; install check not executable as written; "responsive" → reuse the NFR number; grid-style persistence clause; concurrent cache-file write strategy (temp + atomic rename); dead vectors accumulate; re-hash pass cost in its rationale; dead knob `embedding.batch_size` (spike proved no throughput gain — pin to 1 or remove); style items (banned/fluffy vocabulary, "machinery" ×6, "surface" ×4, "bricks," "Branded," "felt need," telegraphic phrasings); horizontal-rule asymmetry; CUJ freshness overclaim; CUJ 1 window stated as fact; duplicate embed cost unstated. **Reuse:** `hashlib.file_digest()`; `pyvips.thumbnail_buffer()` on already-read bytes (one disk read per file, not three); consider a vendored virtualization library for `timeline.js`.

## Owner decisions required (the review cannot make these)

| # | Decision | Context |
|---|---|---|
| O1 | **Cutover: owner-initiated vs. automated threshold** | B4 — recommended: owner-initiated; deletes five specs and one NFR contradiction |
| O2 | **Embed input: original bytes vs. cached 2048 px screen-fit** | M1/R3 — ~10x LAN transfer difference; interacts with O3 |
| O3 | **Eager vs. lazy per artifact (thumbs sq/as, screen-fit)** | M1 — one pipeline decision, not four patches |
| O4 | **Dedup detect-and-report: in MVP (with owner + test) or drop the claim** | B7 |
| O5 | **`sq` crop: CENTRE (design-pinned) vs. ATTENTION** | M6 |
| O6 | **Embedding channel: require TLS or accept LAN plaintext in writing** | M7 |
| O7 | **lighttpd static-alias for `/media/`: weigh and adopt or document rejection** | M1/D6 |

Everything else in this report is a drafting fix with a stated direction — no further owner input needed.

## Recommended resolution order

1. **Owner decisions O1–O7** — they reshape the text; take them first.
2. **The two subsystem changes:** vector-index refresh path (B3) and the delete-semantics split (B2) — genuine design additions.
3. **The interaction-bound pair:** degradation restatement + three-state filter-scoped coverage line (B5), including the latency-target re-scope.
4. **The thumbnail executor** (B1) and the embedding pipeline mechanics (M2) — both become trivial once O2/O3 land.
5. **The contract and config passes** (M3, M4, M5) — mechanical once decisions exist.
6. **Structure, drift, and style** (M8, M9, nits) — one editing pass, including the Risks & Limits section and the decision register (B6).
7. **Re-run Stage 9** (implementation readiness) on the revised plan before implementation begins; Stages 5–8 need no full re-run if the fixes follow this order, since the architecture is confirmed stable.

## Traceability matrix (consolidated item → raw findings)

| Consolidated | Stage 5 (Z) | Stage 6 (C) | Stage 7 (D/S/Y) | Stage 8 (IZO) | Stage 9 (R/CD/ST) |
|---|---|---|---|---|---|
| B1 | — | — | — | — | IR-B1, CD-B1, ST-2 |
| B2 | Z-8 | C-edit | D-11, D-17 | IZO-4 | R-13, R-19, CD-M5, CD-M16 |
| B3 | — | — | — | IZO-2 | — |
| B4 | Z-4, Z-9 | C-threshold, C-model_id | D-3, D-7, Y-6 | IZO-1 | R-4, CD-M3, ST-5 |
| B5 | Z-1 | C-coverage | D-1, D-2 | IZO-3 | CD-M9, R-15 |
| B6 | — | — | S-1, S-2, Y-1 | IZO-9 | (falsifiers throughout) |
| B7 | — | C-dup | — | — | IR-M7, ST-1 |
| M1 | Z-7 | C-screenfit | D-4, D-6 | IZO-5 | R-3, R-9 |
| M2 | Z-5 | C-model_id, C-jobs | D-3 | — | R-5, R-7, R-14, CD-M1, CD-M6, CD-N1 |
| M3 | — | C-schemas, C-cursor, C-folder-ids, C-settings | D-5, S-6, Y-2, Y-31 | IZO-6 | R-8, R-10, R-11 |
| M4 | Z-10 | C-config-defaults | D-12, D-13 | — | R-6, R-12, R-16, R-17, CD-M2, CD-M4, CD-M7, ST-7, ST-10, ST-17 |
| M5 | — | — | D-9 | IZO-7 | R-10, CD-M14 |
| M6 | Z-10 | C-crop | D-18 | — | R-9, ST-3, CD-M8 |
| M7 | — | — | D-8, D-18 | IZO-8 | — |
| M8 | — | C-relative-paths | S-3–S-10, Y-3 | — | R-18, IR-13/14, CD-M10/M11, ST-8, ST-23, CD-N7 |
| M9 | Z-6 | — | Y-4–Y-9, D-2, D-16 | — | — |
| Nits | Z-10 | C-nits | D-10/13/14/15, Y-10–Y-31, S-8–S-10 | IZO-10 | R-nits, CD-N1–N10, ST-11–ST-24 |
