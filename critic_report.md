# Stage 7 — Critic Review (FeatureRange, lines 1–380)

* **Reviewers:** egm-design, egm-structural, egm-style (each fresh context, dispatched serially)
* **Standard:** feature-spec-standard.md (design, structural); style persona mandate (style)
* **Date:** 2026-10-30
* **Note:** In consolidated mode, findings are recorded here without the interactive resolve loop; they feed the cross-stage synthesis in `consolidated_review_report.md`.

## egm-design findings

### 🔴 Blocker

**D1. The AI-box-down degradation guarantee is logically impossible as written. (lines 41, 96; framed at 31)** — "Search serves whatever is embedded" / "search continues over already-embedded photos" cannot hold with the endpoint down: text queries require the remote text encoder, so search returns nothing, not "whatever is embedded." No local fallback encoder exists. Three sections assert behavior that cannot be built. Must restate: with the endpoint down, browsing/folders/originals are unaffected and **search is unavailable**, with a distinct visible error state (which also adds a third state to the "nothing matched vs. nothing embedded" distinction).

### 🟡 Major

**D2. Search latency claim ignores the mandatory remote round trip and is unmeasured. (38, 84, 205)** — "Tens of milliseconds" covers only local ranking; every query first round-trips to the AI box for text embedding. The spike measured only image latency; standalone text-embed latency was never measured. Restate as end-to-end with a measurement, or scope the target to the ranking phase.

**D3. "Open Questions: None" is false — the model-cutover threshold exists nowhere. (58, 108, 205, 226)** — Cutover is conditional on a coverage threshold that has no value, config key, or default anywhere. Also unspecified: how a model change is initiated. Load-bearing mechanism for "search never goes dark" with an undefined trigger.

**D4. The spike-flagged embedding-input decision (original vs. screen-fit bytes) is unclaimed. (205 vs. spike)** — Affects ~600 GB vs. ~60 GB of LAN transfer across the full pass, retrieval fidelity, and pipeline ordering (screen-fit is generated on demand, so an embed queue running ahead of views finds it missing unless generation is reordered). The design is silent; "Open Questions: None" overstates resolution.

**D5. User settings are promised but have no home. (39, 72 vs. 209–218, 226)** — Grid style and timeline order are "user options/settings"; no settings endpoint, no config keys, no persistence mechanism anywhere.

**D6. The post-spike static-serving alternative was never weighed. (201, 218, 320–325)** — lighttpd serving `/media/` directly from the cache directory (immutable hash-keyed files) removes Python from the byte path without FastCGI. Real cons likely justify rejection for originals (bypasses resolve-by-id, exposes layout, cache-miss generation still needs the app), but the Alternatives Rule requires it documented.

**D7. Model cutover breaks the "<1 GB resident" NFR or the "search never goes dark" promise. (205 vs. 363)** — During reload both matrices (~920 MB plus arrays and working set) coexist if search stays up; if freed first, search pauses. Both claims are stated absolutely and conflict.

**D8. The embedding channel ships the entire library in cleartext, unexamined. (205, 222, 226, 362)** — The threat model demands TLS for serving photos while silently streaming every original unencrypted to the AI box over LAN. Require TLS for `embedding.base_url` or explicitly accept LAN plaintext.

**D9. Folder view: heaviest read path, no performance target and no approach beyond an assertion. (37, 212, 318, 363)** — Recursive newest-date aggregation over 150K paths per request; the brief's "cacheable per view" did not survive into the design. Give it a target and an approach, or state the cache.

### 🟢 Nit

**D10.** Basic vs. digest auth stated inconsistently (13, 40, 47, 222 vs. 362).
**D11.** Re-link ambiguous when duplicate content hashes exist — which record re-links? (90, 197, 356)
**D12.** Guard threshold undefined on first pass and after downtime; `guard/resume` re-evaluation behavior unstated. (101–102, 197)
**D13.** `date_taken` sentinel for `date_source='unknown'` undefined; affects cursor sort and folder newest. (172–174, 211)
**D14.** A derived folder tree has no ids; what `<id>` is remains unstated. (213)
**D15.** CUJ 3 overstates freshness ("reflects the on-disk structure at query time" — it reflects the index). (78)
**D16.** CUJ 1 states the unverified ~4–42 h window as fact. (66)
**D17.** Purge cleanup specified for cache entries but not vector rows. (201 vs. 182–190)
**D18.** Line 90 "drops from view" is wrong (record stays alive until scan); CSRF on the sole mutating endpoint unaddressed; line 230 undercounts draft-stage alternatives (three, not two — X-Sendfile reclassified from adopted design); line 201 CENTRE vs. line 591 "smart-crop" disagree.

## egm-structural findings

### 🔴 Blocker

**S1. The document contradicts itself about whether Open Question 2 is resolved. (58 vs. 226)** — Line 58: tuning values "are pinned in the defaults table." Line 226: "tuning values remain Open Question 2." Both cannot be true; the spec's open-items state is not single-source. (Line 58 is the stale one — the defaults table exists and pins them — but the contradiction itself must go.)

**S2. The standard's Risks & Limits requirement has no auditable section, and the gap has already cost content. (no locus; standard requirement)** — Risk substance is scattered across CUJs 5–8, Security, NFR, Rollout. Demonstrable losses from the brief: the accepted bulk-copy dating failure ("the most common 'my timeline is wrong' report") appears nowhere in lines 1–380; the metadata-only-edit re-embed consequence survives only as an oblique hint; the "re-measure on the production AI box" prerequisite is buried in a parenthetical at 205. A fresh reader cannot confirm "all major risks addressed" without reconstructing from five sections.

### 🟡 Major

**S3. "Additional Resources" and "Future Improvements" are nested as H3 under "Non-Functional Requirements." (366, 373)** — Neither contains NFRs; both vanish from H2 scans; the load-bearing "Future Improvements" cross-reference (line 302) points at a section hidden under the wrong parent.

**S4. The auth story is duplicated in four places and has already drifted. (13, 121, 222 vs. 362)** — "Basic auth" in three places; only the NFR records the digest/htdigest reality matching the target server. Two near-verbatim copies of the security claims (222, 362) are guaranteed to diverge. Pick one normative location.

**S5. The component map attaches the cache directory to the wrong component. (152–157)** — The diagram's only reading is that the cache hangs off `plaps.db`; in reality the service reads/writes it. The edge is also unlabeled.

**S6. The status endpoint has two names. (40, 96, 102 vs. 197, 215)** — `/status` in Goals/CUJs vs. `GET /api/status` in the normative surface. Carryovers from the brief.

**S7. A performance claim is "covered by the concurrency stress test," a test the document never introduces. (363 vs. 42)** — The stress-test tier exists only in the concepts brief; within this spec the mitigation rests on an undefined antecedent.

### 🟢 Nit

**S8.** Alternatives provenance note undercounts (three draft-stage items, not two) and misses that X-Sendfile was the brief's adopted design, reclassified post-spike — the one place a reader learns the design changed after brainstorm. (230)
**S9.** Resource paths do not resolve from the document's location (`docs/designs/plaps.md` citing root-relative `concepts_brief.md`, `docs/spikes/*.md`). (201, 368, 371)
**S10.** Horizontal rules used asymmetrically (7, 358, 379). Decoration, not delimitation.

## egm-style findings

### 🔴 Blocker

**Y1. Lines 58 vs. 226 — same contradiction as S1, from the writing side.** A cold reader cannot tell whether tuning values are decided. Also: "documented with defaults:" at 226 introduces a key list with no values — give defaults inline or point at the table.

### 🟡 Major

**Y2.** Two names for the status endpoint (as S6); plus "queue state" vs. "queue depth" drift. (40, 96, 102 vs. 197, 215)
**Y3.** "Basic auth" five times vs. digest reality once (as S4, writing side). Suggest "proxy password authentication" throughout. (13, 40, 120, 222, 343 vs. 362)
**Y4.** Search latency stated as fact in four places; only 363 frames it as a target; "already" at 282 overclaims. (38, 84, 205, 282 vs. 363)
**Y5.** The guard threshold's baseline (previous pass's file count) appears only at line 197, not at first mention in CUJ 7 — the baseline is what makes the mechanism safe rather than arbitrary. (101)
**Y6.** "Mutually consistent" hides the mechanism a reader must not miss: during the overlap, queries are embedded with the **old** model; old-model text and image vectors share one space. Without this, an implementer could embed queries with the new model mid-migration and silently break search. (108)
**Y7.** "An optional progress indicator" is not actionable — optional to whom? A CUJ Then-clause is a requirement. (66, 340)
**Y8.** The inotify rejection summary (302) is over-compressed to obstruction — three idioms in one sentence; the preceding bullet is clear and this one undoes it.
**Y9.** Banned adjective "correct" at 325 ("waitress pool is correct") — replace with the reason it's adequate, which the sentence already contains.

### 🟢 Nit

**Y10.** "true" vs. "genuine" semantic search (20, 25) — both fluff; the parenthetical already defines the term.
**Y11.** "black out search" vs. "never goes dark" (30, 108) — pick one; the brief's "search outage" is plainer.
**Y12.** "~4–42 hours" vs. "multi-day re-embed" used inconsistently (66/205/363 vs. 309/364).
**Y13.** "the missing centerpiece" three times as shorthand (249, 253, 257); riddle at first use.
**Y14.** "cited as evidence the niche is viable" is ungrounded (253).
**Y15.** "deletes tombstone records" mis-parses (90).
**Y16.** "simply fails its read" — filler + odd construction (90).
**Y17.** "realpath-free" is compressed jargon recoverable only from the brief (169).
**Y18.** "empty-leaf folders hidden" (212) vs. CUJ 3's recursive rule (78) — equivalent after bottom-up pruning, but the reader does the inference.
**Y19.** "the entire auth story" is colloquial (222).
**Y20.** "when their records purge" — telegraphic (201).
**Y21.** Alternatives provenance note is process narration a cold implementer doesn't need (230).
**Y22.** "profiling-gated" twice without expansion (264, 375).
**Y23.** "bricks the whole app" slang; "ambiguous by ~40 hours" takes two reads (339).
**Y24.** "Branded login UX" — the app has no brand (343).
**Y25.** "either way" has no grabbable antecedent (351).
**Y26.** "explicitly tolerated at hours" is clunky (363).
**Y27.** Documents aren't "ingested" (368).
**Y28.** "trigger: living with displayed duplicates" colloquial; "(novelty; ...)" compressed (375).
**Y29.** "a felt need" awkward (376).
**Y30.** Word-frequency tics: "machinery" ×6, "surface" ×4 as complexity measure, "structurally impossible/prevented" ×3.
**Y31.** The Android-client justification appears four times near-verbatim (13, 222, 343, 362).

## What the critics confirm holds (summary)

- Alternatives Rule exceeded: all 24 brief alternatives carried with reasons, plus three draft-stage additions; the X-Sendfile live-verified rejection is the strongest pattern in the document.
- The three self-declared hard problems each have a worked answer; arithmetic checks out everywhere it's stated (~460 MB matrix, 35–70 GB cache, 1–2 GB DB).
- Read-only guarantee verifiable as stated; path resolution from the database structurally closes traversal.
- Standard coverage otherwise complete and ordered; heading hierarchy clean; formatting sound; CUJs consistent.
- Writing is close to the plain-English standard: one banned adjective, claims usually traceable to a spike or stated assumption, honest negative results, grounded derivations a reader can check.
