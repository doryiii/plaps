# Stage 9 Re-Run Report — Resolution Record

* **Target:** /mnt/data/workspace/plaps/docs/designs/plaps.md
* **Date:** 2026-11-01
* **Reviewers (serial dispatches, fresh contexts):** egm-implementation-readiness, egm-codebase-details, egm-style
* **Disposition:** every finding fixed in the document. One new owner decision (model cutover flips at the start). The document now stands at 707 lines / 51 file entries.

## Owner decision taken during the re-run

**Model cutover flips at the start.** The codebase-details review blocked the revised cutover design: "old-model vectors keep serving while re-embed jobs produce new-model vectors" is unimplementable against a single llama.cpp endpoint (one server, one model) — whichever order the AI box is switched, one path silently produces garbage. The owner chose flip-at-start over running both models on two ports: start the new model on the AI box, restart with the new `embedding.model_id`, and search ranks in the new space over the re-embedded subset — the results-view coverage line shows progress exactly as during first ingest. Cutover (`POST /api/admin/model/cutover`) rewrites `active_model_id` and deletes old vectors; reverting the config before cutover aborts cleanly. Recorded in the Decision Register (2026-11-01). Consequences: the vector index loses `reload()` and the worker's model-comparison guard (the index always carries the configured model); never two resident matrices; no extra config keys.

## Implementation-readiness findings — all fixed

1. **Model-change window index corruption** — fixed by the flip-at-start decision (above); the worker upserts unconditionally, the index holds only the configured model.
2. **VectorIndex surface** — `add` upserts (masked ids handled), `unmask` added, `reload` deleted as no longer needed.
3. **Search-state enum pinned** — `"ok" | "no_matches" | "empty_filter"` in the response contract and the `/api/search` entry.
4. **Root folder addressable** — reserved id `"root"`; registered in the Decision Register.
5. **Backup mechanism** — `VACUUM INTO`'s WAL snapshot needs no writer pause and no cross-process coordination; the impossible "pause the writer from another process" instruction deleted.
6. **Increment tagging** — explicit Create/Modify splits on `app.py`, `api/status.py`, `test_jobs.py`, `test_status.py`.
7. **`tests/test_status.py` added** — the owner's debugging surface has its own test file.
8. **`timeline.js` decided** — hand-rolled, no vendored substitute; registered.

## Codebase-details findings — all fixed

* **Blocker (cutover overlap)** — owner decision above. Additionally: the adapter gained a **served-model check** (`GET /v1/models`, spike-verified, cached 60 s — it is also the reachability probe) so a not-yet-switched AI box surfaces as a visible retryable endpoint state instead of silently mis-stamped vectors; and re-embed jobs are queued only for photos lacking a vector for the configured model, so a restart mid-change re-queues only the remainder.
* **`/api/search` state enum** — completed to three states in the endpoint entry.
* **Subtree scoping** — `LIKE` (case-folding, `_`/`%` wildcards) replaced by the case-sensitive `substr` comparison; registered; tests cover case, `_`, and separator safety.
* **`systemd/plaps-backup.service` added** (a timer cannot execute anything by itself); the timer got its own entry too (`OnCalendar=weekly`, `Persistent=true`).
* **Backup rotation pinned** — timestamped target under `<config dir>/backups/` (`VACUUM INTO` refuses to overwrite), 8 most recent kept; stated in `db.py`, `__main__.py`, README, and the pinned-defaults table.
* **Cache-size accounting** — every thread that creates a cache file (worker or request thread) records its size through the writer connection.
* **Vector-index capacity** — preallocated 200,000 rows (~586 MB); an embed writes into spare capacity instead of copying ~460 MB per photo; the doubling-on-overflow transient recorded in Risks & Limits; NFR memory claim corrected accordingly.
* **`tests/test_adapter.py` added** — real adapter against `httpx.MockTransport` (payload shape, normalization, error typing, served-model mismatch), so `plaps/embed/` can meet the coverage gate in the default run.
* **Nits** — `/opt/plaps` install location everywhere (unit, README, install check); `derive()`/`screenfit()` take bytes (one disk read for all variants); the `dev` extra named; `date_source` tagging moved to scanner tests; conftest states pyvips generates JPEG pixels (piexif only inserts EXIF); date filters re-pinned as a sargable half-open range on the raw ISO string.

## Style findings — all fixed

* Banned vocabulary reintroductions removed ("poison jobs", "classic … hole").
* Pinned-defaults rationales rewritten as plain claims ("dies with nothing" → "kept beside the config so one directory holds all stateful data", etc.).
* README entry restructured from one ~120-word sentence into a scannable list.
* Coverage surfaces disambiguated: the **results-view coverage line** vs. the **`/api/status` re-embed coverage field**, named consistently in CUJ 8 and Risks & Limits.
* `plaps-backup.timer` given its own numbered entry (structural drift with the list's convention).
* Eighteen nits: anthropomorphism ("speak", "truthful", "trusting", "self-healing", "pretend"), filler ("simply"), register ("legit", "the llama.cpp truth"), compressed shorthand ("most thorough due first" → explicit re-hash > full sweep > pruned precedence; cutover-condition wording aligned on "when satisfied"; probe wording standardized on "the served-model check is the reachability probe"), the double-nested conftest parenthetical, the substr-clause chain in the response contract, "grid layouts" → "grid styles", "without restart" → "without a further restart" in the model-change test, and the merged adapter-probe row in the pinned-defaults table.

## Verdicts

* **Implementation-readiness (first dispatch):** the prior round's headline defects genuinely fixed; eight majors and nine nits remained (all resolved above).
* **Codebase-details:** "The plan is close to implementable — the file inventory, defaults, and spike-grounded details are solid — but the model-cutover mechanism (Blocker) must be resolved" — resolved via the owner decision.
* **Style:** "Stylistically ready for implementation, with one cleanup pass required first" — the cleanup pass is applied; its claims-verified section confirmed the flip-at-start phrasing, backup retention, substr rule, three result states, served-model check, and increment labels are consistent across the document.

The design document is ready for implementation.
