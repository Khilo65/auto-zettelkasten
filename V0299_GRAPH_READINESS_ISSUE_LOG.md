# v0.29.9 Graph Readiness Issue Log

This log tracks only failures that block the frozen-sample, incremental, or full-library graph gates. Atomic notes and profiles remain frozen.

| ID | Issue | Root cause | Minimal correction | Validation | Status |
|---|---|---|---|---|---|
| G-01 | Partition bridges became duplicate primary members | Child writers could change the planner's structural bridge/primary class independently | Preserve bridge versus primary class; allow only `core`/`context` refinement within a primary child; recheck the partition invariant before commit | Focused partition test, cached zero-call reprojection, 77/77 unique primaries | Passed local gate |
| G-02 | Partition child state omitted or polluted fields | Child normalization did not close `source_backed` and `missing_member_ids` over the admitted source set | Derive `source_backed` from qualification and intersect missing members with proposed source IDs | Focused validator and replay tests | Passed local gate |
| G-03 | Eight reciprocal child-cluster links were absent | Legacy target aliases and reciprocal rows were normalized inconsistently | Resolve the existing target alias and preserve the same evidence-backed pair identity in both directions | Reciprocal projection test; zero missing reverse links | Passed local gate |
| G-04 | Visible acquisition actions contained duplicates | Projection did not consistently collapse strong canonical identities | Merge only strong identifiers or exact title, author, and year; keep ambiguous aliases separate | Positive and negative identity tests; sampled duplicate rate | Passed local gate |
| G-05 | One citation identity matched an undated attachment to a dated work | Title matching treated a missing candidate year as compatible with a supplied year | Require a nonempty equal candidate year for title-only matching | Citation-identity regression | Focused regression passed |
| G-06 | Two relationship decisions had inverted endpoint evidence | Provider prose swapped bases; current contract has no structured ownership proof | Correct only exact, human-audited rows; do not add semantic guessing or a verifier | Targeted registry audit | Pending local correction |
| G-07 | One relationship was materially wrong across onset and recurrence | Candidate and adjudication promoted an unsupported cross-outcome direct claim | Retire or targeted re-adjudicate that pair only | Source-first pair audit | Pending targeted correction |
| G-08 | Blind candidate usefulness fell to 19/30 | Discovery treated family goals as evidence and invented missing bridges | State that route goals are hypotheses; require both endpoint profiles to supply a bounded comparison; permit `no_more_candidates` below target | Fresh frozen 30-pair audit: at least 24 useful; retain at least 155 endpoints and 219/219 exact routes | Pending after local gate |
| G-09 | One report chapter and containing report counted as independent works | Explicit container lineage is absent | Preserve explicit parent/container identity when supplied; do not infer from prose, author, or year | Positive explicit-lineage and negative same-author/year tests | Deferred until explicit metadata repair |
| G-10 | Projection remains CPU-heavy | Local projection took 6m15s for 425 notes | Measure after correctness fixes; optimize only if the unchanged replay path or full-corpus estimate remains material | Before/after wall time and write count | Monitoring |
| G-11 | A zero-call repair run missed the family-plan checkpoint | The family-plan identity included nonsemantic call limits | Exclude call limits and other accounting controls with the existing semantic-policy projection | Planning-key regression and capped cached rebuild | Passed local gate |

## Release gate

The release is ready for the reserved incremental batch only when:

- frozen source/profile drift and source/profile calls remain zero;
- candidate usefulness is at least 24/30 without losing exact identity routes;
- every partition source has exactly one primary child and optional bridge memberships only;
- relationship endpoint fidelity is 100% in the deterministic audit;
- reciprocal generated cluster links and replay are exact;
- no unresolved paid or local cluster failure remains.

The full-library graph is estimated and authorized only after the incremental batch and its replay pass.
