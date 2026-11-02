# Stage 9 — Implementation Readiness Test (DetailedRange, lines 381–601)

* **Reviewers:** egm-implementation-readiness (IR), egm-codebase-details (CD), egm-style (ST) — each fresh context, dispatched serially
* **Standard:** implementation-readiness-standard.md (IR, CD); style persona mandate (ST)
* **Date:** 2026-10-30
* **Reviewer status:** all three ran and reported; none "not run."

## Consolidated findings

### 🔴 Blockers

**R1. No executor is assigned to thumbnail jobs.** (IR-B1, CD-B1, ST-2; plan lines 434/439/479 vs. design line 201) — The scanner enqueues `thumb` jobs and the jobs table carries `kind='thumb'`, but the only background consumer in the file list is the embed worker; the design says thumbnails are produced "at scan time," which no plan file implements. Three buildable variants (scanner-inline, third worker thread, on-demand-only) have different GIL, lock-holding, and crash-resume behavior. Fails the Zero-Decision Rule outright.

**R2. Duplicate detect-and-report is a design requirement with no implementing file, endpoint, or test.** (IR-M7, ST-1; design line 54, brief Key Decision 3) — The hash is computed; nothing reports pairs; no test mentions duplicates; `/api/status` has no dedup field. A cold implementer following the plan ships neither the report nor its test. Owner decision required: give it an owner or move it out of the document's claims.

### 🟡 Major

**R3. Embedding input bytes undecided** (IR-M2, CD-M15, design-critic D4) — original vs. cached ~2048 px screen-fit image; ~10x LAN-transfer difference (≈750 GB vs ≈75 GB of base64 across the first full pass); the screen-fit option creates an ordering dependency with lazy generation. The spike deferred this decision to implementation; the plan claims closure without making it.

**R4. Model-cutover coverage threshold referenced three times, pinned nowhere; not even a config key.** (IR-M3, CD-M3, ST-5; design D3) — `tests/test_search.py` is specified against a value that does not exist. *(Superseded at Stage 8: recommendation is owner-initiated cutover, which deletes the threshold entirely — see consolidated report.)*

**R5. Per-item failure attribution inside a batch is assumed but never designed, and the spike never demonstrated it.** (CD-M1) — The endpoint returns whole-request failures; no item-level signal inside a successful batch was ever reproduced. "Marking only the failed items" needs a stated strategy (serial fallback, bisection, or one-request-per-item).

**R6. The pinned-defaults table does not pin what it claims** (IR-M4, CD-M2, ST-4, ST-10) — `timezone`, `cache_dir`, `database_path`, `embedding.batch_size` have no defaults anywhere; `timezone` is load-bearing (a UTC server mis-sorts the library); required-vs-optional matrix and validation ranges are unstated; "every key with its pinned default" is wrong for required keys.

**R7. `failed_perm` attempt count and backoff base/cap unpinned** (ST-6, CD-M2) — "repeated failures" hides the decision; `tests/test_jobs.py` tests behavior without numbers.

**R8. No JSON response schema anywhere** (IR-M5, CD-M12, ST-9) — Photo card, folder node, status object, and the "same error shape" are all undefined. The justified-rows grid cannot lay out without per-photo width/height (or aspect) in the card before images load; nothing states this.

**R9. `sq` crop: "smart-crop" contradicts the pinned `crop=Interesting.CENTRE`** (IR-M6, CD-M8, ST-3) — In libvips vocabulary "smart crop" is ATTENTION, the alternative the spike left draft-level; the plan points at the option the design rejected; the test asserts only 512×512, which both crops satisfy.

**R10. Folder id and prefix semantics underspecified; folder-tree derivation has no algorithm or performance target** (IR-M8, CD-M14, design D9) — relative-vs-absolute prefix, trailing slash, root node, separator-safe prefix match, and the derivation approach (recursive CTE vs. app-side rollup vs. memoized tree) are all open; `/api/folders` is the one endpoint doing O(library) work per request and has no latency budget.

**R11. NULL/unknown `date_taken` breaks the cursor and ordering as specified** (IR-M9, CD-N3, design D13) — The cursor string cannot encode NULL; NULLS-first/last placement undefined; the timezone application point for the mtime fallback has no owning module.

**R12. Stat-pruning requires persisted directory mtimes and gives them no home** (CD-M4) — No `dirs` table, no schema location for ~10–20K directory mtimes; without it the prune degrades to a full sweep after every restart.

**R13. Revival-at-a-new-path and re-link-under-duplicate-hashes semantics undecided** (CD-M5, design D11) — Path is UNIQUE; does a revived record keep `photo_id` and rewrite path? Which of two same-hash tombstones revives? Does revival re-enqueue jobs? The brief promised a re-link decision table; the plan ships one line.

**R14. No job dedup rule; schema cannot enforce one** (CD-M6) — No UNIQUE(photo_id, kind); stacked pending jobs (mtime touch, revival, model change over in-flight queue) silently double embed work at ~2.5 s/photo.

**R15. Search latency target likely unmeasurable as architected** (IR-M11, design D2, Stage 8 #7) — "p95 < 50 ms warm" must include the live text-embed round trip; standalone text latency was never measured; no query-embedding cache decision. Re-scope to ranking-only or instrument.

**R16. Backup cadence has no owner, trigger, or test** (IR-M10, CD-N6-adjacent) — `VACUUM INTO` helper exists; nothing schedules or invokes it; no backup/restore test; the coverage gate excludes `db.py`.

**R17. The systemd unit as pinned cannot start the service; static-file deployment unspecified** (CD-M13, ST-19) — Bare `python` resolves system Python, not the venv; no config path passed; the config env var is unnamed; no step gets `static/` to `/srv/http/plaps/`.

**R18. Increment labels contradict the brief's increment order; Create-only phasing is structurally impossible** (IR-N13, CD-M10, CD-M11, ST-8, ST-23, CD-N7) — Walking-skeleton files depend on search-increment modules; the spike docs exist but are marked "Create"; either the phasing or the labels must be rewritten with explicit modify steps.

**R19. Purge rule deletes cache entries still shared by live duplicates** (CD-M16) — Cache is keyed by content hash and shared between duplicate paths; "purge drops … cache orphans" needs "unless another live record shares the hash." Related unstated cost: duplicates also embed twice.

### 🟢 Nit (summary)

Missing `plaps/embed/__init__.py` (IR-12); `test_paths.py` tests logic with no owning module — `plaps/paths.py` implied (IR-15, CD-N10); counter ownership unassigned (IR-16, CD-N2); "checkpoint" and "stalled scan" undefined (IR-17, ST-7, CD-N5); `images.py` signatures are shorthand (IR-18); perf-smoke generator has no owning file (IR-20, ST-21); coverage gate skips `images.py` and `db.py` (IR-21); `adapter.model_id` source unstated (IR-22, CD-N1); dead vectors accumulate (IR-23); probe timeout/cache unpinned (CD-N5, ST-16); re-hash pass cost omitted from its rationale (CD-N8); concurrent cache-file write strategy unstated (CD-N9); config env var unnamed (ST-17); `order`/`limit` values unpinned (ST-18, Stage 6); install check not executable as written (ST-19); "responsive" unmeasurable (ST-20); grid-style preference persistence unstated (ST-24); style items: "correctly," "poisoning," "classic hole," "quiesces," anthropomorphism (ST-11–14), compressed cadence shorthand (ST-15), "thin unit layer" undefined (ST-22). **Reuse:** `hashlib.file_digest()` for blake2b streaming; `pyvips.thumbnail_buffer()` on already-read bytes (one disk read per file instead of three); consider a vendored virtualization library instead of hand-rolled `timeline.js` (CD reuse notes).

## Claims verified as accurate (CD)

pyvips auto-rotation and square-crop behavior match the spike; piexif-over-pyvips validated; X-Sendfile cut, Range/206 passthrough, port 8096, digest auth, static doc-root all match the lighttpd spike; the embedding endpoint contract, failure modes, 768-dim normalized vectors, batching behavior, and latency caveats match the embedding spike; arithmetic checks out (matrix, cache, DB sizes); the keyset cursor mechanism is genuinely skip-free; the concurrency model makes writer-writer `SQLITE_BUSY` structurally impossible; the dependency surface is complete for everything named (no missing third-party package — the gaps are plan components, not dependencies); the guard threshold is internally consistent; all referenced artifacts exist and say what the plan claims.

## Verdict (IR, concurred by CD and ST)

**Not implementable as written.** A competent engineer would produce something that runs, but would be making load-bearing decisions in at least a dozen places. The plan's structure is otherwise sound: the file list is near-complete, the test list maps to real behavior, the pinned-defaults table is the right idea, and the verification plan is concrete — and its spike-backed claims are accurate. The failures are specific and fixable.

Resolve before implementation begins, in order: (1) thumbnail executor (R1); (2) the embedding pipeline decisions — input bytes, batch-failure attribution, job dedup, retry pins (R3, R5, R7, R14); (3) response schemas and error shape (R8); (4) the plan-vs-design conflicts for the owner — `sq` crop, dedup in/out (R9, R2); (5) folder-id rules, NULL-date handling, dir-mtime storage, revival semantics, backup trigger, static deployment, systemd unit, phasing rewrite (R10–R13, R16–R19); (6) re-scope or instrument the search latency target (R15).
