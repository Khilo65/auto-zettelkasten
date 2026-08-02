# Auto-Zettelkasten v0.26 Relationship Grounding and Evaluation Calibration Plan

**Status:** Implemented and evaluated

**Date:** 2026-08-02

**Foundation:** Engine `0.25.0`, artifact schema `1.18`, source catalogue
schema `7`, relationship registry schema `7`, relationship decision contract
`relationship-decision-v8`, relationship discovery prompt `15`, relationship
adjudication prompt `14`, literature-family prompt `8`, cluster prompt `33`,
source prompt `5`, and commits `b936285` and `e89b83d`.

**Primary evidence:**

- `V025_GRAPH_COMPLETION_AND_EVALUATION_CALIBRATION_PLAN.md`;
- `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v025-targeted-completion-evaluation-20260802/evaluation/v025-targeted-completion-comparison.md`;
- the frozen 87 v0.25 relationship pair jobs and their complete atomic-note
  inputs;
- the v0.16–v0.25 plans and comparative evaluations; and
- independent reviews of the current code, the v0.25 relationship audit, and
  the historical remediation sequence.

## 1. Summary

V0.26 is a narrow relationship-grounding and evaluation-calibration release.
It is not another graph architecture rewrite.

V0.25 already established that the following layers work and should remain
frozen:

- 193/193 atomic profiles were reused with zero source calls and zero semantic
  drift;
- bridge discovery covered 14/14 literature families and produced a blind
  candidate usefulness score of 25/30;
- candidate, pair-job, registry, and reciprocal-projection accounting were
  complete;
- all 18 planned clusters published, including 13 mixed-literature clusters;
- audited cluster memberships, contributions, claims, debates, boundaries,
  and reciprocal links all passed;
- acquisition accounting and identity projection now have local deterministic
  corrections; and
- provider-empty responses are observable and recoverable with one exact
  bounded retry.

The remaining issue is narrower. DeepSeek usually describes both endpoint
notes accurately, but sometimes creates a direct shared proposition by
widening, recasting, or conflating the two sources. The v0.25 evaluation then
overstated part of that problem by treating two reciprocal `complements` links
as direction reversals. Those links correctly described Svensson's finding in
their endpoint basis but blurred the roles inside the shared mechanism. They
are mechanism-role ambiguities, not reversed directed edges.

V0.26 will therefore:

1. replace the relationship prompt's accumulated rules with a shorter,
   domain-neutral grounding workflow;
2. clearly separate source-owned findings from system-generated joint-reading
   inferences;
3. make direct, contextual, symmetric, and directional relationships
   internally coherent without deterministic intellectual classification;
4. decouple discovery fingerprints from adjudication fingerprints so a prompt
   change reuses the frozen candidate pool rather than paying for discovery
   again;
5. stop requesting optional anchors from DeepSeek while adding
   backward-compatible parsing for the short v14 field names;
6. preserve precise parking for genuinely incomplete provider rows without
   adding another retry workflow;
7. correct the evaluation rubric without erasing the original v0.25 audit; and
8. run a six-call relationship-only comparison using the frozen 87 pair jobs,
   followed by a pristine zero-call, zero-write replay.

Release identities:

- Engine: `0.26.0`
- Artifact schema: unchanged at `1.18`
- Source catalogue schema: unchanged at `7`
- Relationship registry schema: unchanged at `7`
- Relationship selection-state schema: `4`
- Relationship decision contract: unchanged at `relationship-decision-v8`
- Relationship decision normalization: unchanged at `3`
- Relationship discovery prompt: unchanged at `15`
- Relationship adjudication prompt: `15`
- Literature-family prompt: unchanged at `8`
- Cluster prompt: unchanged at `33`
- Source prompt: unchanged at `5`

No new dependency, public API, CLI option, source/profile generation, index or
routing redesign, discovery call, cluster call, acquisition call, verifier,
semantic retry, web search, or Zotero mutation will be added.

## 2. Corrected diagnosis of v0.25

### 2.1 What succeeded

The source-first v0.25 relationship audit found:

- 64/69 audited relationships useful for joint navigation;
- 68/69 endpoint bases faithful to their respective atomic notes;
- 37/39 direct links useful;
- 27/30 sampled contextual links useful;
- 76/76 accepted relationships reciprocally projected;
- zero duplicate-active canonical source pairs; and
- 87/87 selected pair jobs either answered or precisely parked.

These results show that atomic-note quality, candidate discovery, full-note
transport, graph persistence, and projection are not the current bottlenecks.

### 2.2 What the original report overstated

The two Svensson comparisons used `complements`, which is symmetric in the
registry and projects reciprocally. They cannot meaningfully fail because
canonical endpoint order differs. In both rows, the source-owned Svensson basis
correctly stated that a government-biased mediator helps rebels signal
credibility. The imprecision occurred only when the shared proposition treated
the two papers as addressing the same party's version of the commitment
problem.

V0.26 will preserve the original audit for traceability. A new independent
re-audit will test the preliminary interpretation that these should be
classified as:

- accurate endpoint bases;
- a useful intellectual connection;
- an overextended direct comparison or mechanism-role ambiguity; and
- candidates for contextual wording, not reversed directional edges.

### 2.3 The remaining substantive pattern

Four other v0.25 comparisons illustrate the actual defect:

- a finding about rebel-group number and strength was recast as evidence about
  deliberate engagement or inclusion;
- political inclusion and mediation-process inclusivity were treated as the
  same construct;
- negotiation occurrence and sequencing were converted into a claim about
  post-conflict durability; and
- a between-termination-type comparison was presented as refuting all
  within-settlement design improvements.

The endpoint summaries were mostly accurate. The error was the bridge sentence
that made two adjacent contributions appear to establish one direct
proposition. The correct output is usually a carefully bounded
`contextual_connection`, occasionally a narrower direct relationship, or
`no_relationship` when the overlap is superficial.

## 3. Design principles

### 3.1 Probabilistic scholarship, deterministic safety

DeepSeek remains responsible for:

- whether a useful intellectual relationship exists;
- what each source establishes;
- whether the works share a sufficiently specific proposition;
- whether the connection is direct or contextual;
- relation type and applicable intellectual direction;
- the relationship rationale and boundary; and
- whether a pair should be rejected.

Ordinary code remains responsible only for:

- supplied source IDs and pair ownership;
- response shape and required fields;
- recognized relationship vocabulary;
- symmetric versus directional storage rules;
- optional anchor-ID validation;
- batching, budgets, caching, persistence, reciprocity, and projection; and
- complete accounting of answered, rejected, and parked jobs.

Code must not decide that two sources support, undermine, qualify, extend, or
otherwise intellectually relate.

### 3.2 Do not hardcode domain examples into the prompt

The production prompt must remain useful across disciplines, source genres,
methods, mechanisms, and literatures. It must not mention particular authors,
peace processes, signaling, guarantees, parties to a conflict, or any other
domain-specific example.

The prompt may instruct the model generally to preserve:

- the entities and roles used by each source;
- the direction of any mechanism or intellectual action;
- construct definitions;
- unit and level of analysis;
- process or temporal position;
- outcome and evidentiary status;
- scope, population, period, and method; and
- the difference between an author's finding and the system's synthesis.

Domain-specific cases belong in regression fixtures and evaluation, not the
production instruction set.

### 3.3 Replace prompt instructions rather than accumulate them

Prompt v14 already contains many overlapping taxonomy rules and a generic
self-check. V0.26 must rewrite and consolidate those instructions rather than
append another layer. The objective is a clearer decision sequence, not more
instructions.

## 4. Implementation changes

### 4.1 Rewrite relationship adjudication prompt v15

Update `_relationship_adjudication_system_prompt` in
`src/auto_zettelkasten/readers.py` while preserving the existing complete-note
transport and JSON response contract.

The new prompt will use this compact workflow:

1. **Describe each endpoint independently.** Write `source_a_basis` only from
   the left note and `source_b_basis` only from the right note. Each basis must
   preserve the source's actual construct, entities, roles, outcome, scope,
   causal strength, and evidentiary status.
2. **Test direct comparability.** Ask whether both bases support one
   sufficiently specific proposition, question, mechanism, outcome, or explicit
   intellectual engagement without widening or recasting either source.
3. **Choose direct, contextual, or none.** Use a direct relationship only when
   the shared proposition is supported by both endpoint bases. Use
   `contextual_connection` when joint reading is useful but requires an
   additional interpretive step. Use `no_relationship` when the overlap is only
   topical, lexical, or generic.
4. **Identify system inference.** When a contextual link contains synthesis not
   stated by either author, frame it explicitly as a joint-reading or
   system-level interpretation rather than as either source's finding.
5. **Apply type and direction.** Choose the narrowest defensible type. Preserve
   the intellectual action and mechanism direction expressed by the sources.
6. **Self-review once.** Confirm that neither endpoint basis nor the rationale
   exchanges roles, constructs, outcomes, stages, scopes, causal strength, or
   source ownership merely to make the pair fit.

The replacement prompt must not exceed the rendered instruction length of
prompt v14. The prompt must continue to require one decision for every supplied
`pair_job_id`, normally one connection per pair, and no invented IDs,
locators, Markdown, or provenance.

### 4.2 Make relationship-type semantics coherent

Clarify the taxonomy without expanding it.

Directional types:

- `supports`: the actor work supplies evidence or argument bearing on a
  sufficiently specific proposition advanced by the reference work;
- `undermines`: the actor supplies materially incompatible evidence or argument
  against the reference proposition;
- `qualifies`: the actor establishes a meaningful condition, exception, or
  boundary on the reference proposition;
- `extends`: the actor explicitly builds on, applies, tests, refines, or
  generalizes the reference work;
- `rival_explanation`: the actor advances a competing explanation for the
  same explanandum addressed by the reference work; and
- `sequential_relationship`: actor/reference order expresses the intellectual
  or process sequence returned by the model.

Independent studies that reach compatible conclusions without one bearing on a
proposition advanced by the other should use `complements`, not directional
`supports`.

Symmetric types remain:

- `complements`;
- `contrasts`;
- `boundary_contrast`;
- `methodological_fault_line`;
- `interpretive_or_normative_disagreement`; and
- `contextual_connection`.

For symmetric types, local code may continue assigning canonical left/right
storage endpoints when actor/reference values are absent. This is already the
existing normalization behavior and does not constitute an intellectual
judgment. Symmetric links are reciprocal and must be marked direction
`not_applicable` during evaluation.

For directional types, missing, identical, unknown, or out-of-pair
actor/reference IDs remain precisely parked. Code must not infer direction from
pair order, chronology, citation, wording, or source metadata.

### 4.3 Keep optional anchors out of the critical path

Optional relationship anchors are not the active quality defect. Prompt v14
already labels them optional; prompt v15 will stop requesting them from
DeepSeek altogether. The v8 parser will continue accepting the canonical
evidence-anchor fields and will add backward-compatible aliases for the short
v14 names `source_a_anchor_ids` and `source_b_anchor_ids`. It will retain only
real supplied IDs and drop invalid optional IDs with an advisory warning.

Empty anchors must never park, retry, downgrade, or fail an otherwise sound
relationship. No normalization-version or schema change is justified solely to
populate advisory metadata.

### 4.4 Decouple discovery and adjudication fingerprints

The current `selection_identity` includes the adjudication prompt version,
decision contract, and decision-normalization version. Consequently, a
relationship-prompt improvement makes unchanged candidate discovery appear
stale and can trigger unnecessary discovery calls.

Relationship selection-state schema `4` will separate two change decisions and
persist the selected pool needed to move between them.

**Discovery identity**

- provider and model used for discovery;
- relationship discovery prompt identity;
- candidate-selection policy and caps;
- compact profile, catalogue, citation, literature-position, and collection
  inputs already used by discovery; and
- discovery algorithm identity.

**Adjudication identity**

- provider and model used for adjudication;
- relationship adjudication prompt identity;
- relationship decision contract;
- decision-normalization identity;
- complete endpoint-note semantic hashes;
- immutable pair-job context; and
- a relationship-semantic policy identity containing only semantic rules.

Operational settings—including deadlines, concurrency, worker count, call
ceilings, retry reserve, and cluster-only policy—must not enter either semantic
identity.

Machine-generated `no_relationship` memory must not make discovery react to its
own downstream output. It remains prompt- and endpoint-hash-scoped pair memory
used when deciding whether an already selected pair needs adjudication. Human
exclusions and upstream citation/catalogue inputs may still affect discovery.

Schema `4` must persist, in addition to the discovery identity:

- every selected canonical source pair;
- its discovery proposition or candidate rationale;
- rank and contributing family/job provenance;
- explicit/citation basis when present;
- selected, already-visible, rejected, and deferred disposition;
- the upstream hashes used to construct the pool; and
- a stable selected-candidate-pool hash.

Implement this by extending and reusing the existing candidate disposition and
selected-pair payload in relationship selection state. Do not introduce a
second candidate-store subsystem.

The orchestration flow must distinguish:

1. neither discovery nor adjudication changed: return the existing no-op result;
2. discovery changed: run discovery, persist the new selected pool, then
   adjudicate work requiring a current decision;
3. only adjudication changed: load the persisted selected pool, reconstruct the
   immutable pair jobs from current complete notes and upstream pair context,
   and run adjudication without discovery; and
4. both changed: run both stages in dependency order.

This explicitly replaces the current early-return behavior that would otherwise
skip both discovery and adjudication when the discovery identity is unchanged.

Changing only adjudication prompt `14` to `15` must:

- reuse the unchanged selected candidate pool;
- make zero family-planning, routing, or candidate-discovery calls;
- invalidate only the affected pair decisions;
- retain prompt-14 machine links as `reconciliation_pending` until their pair
  receives a current decision; and
- refresh only relationship registry/projection state after adjudication.

Changing the discovery prompt, discovery model, candidate policy, source
profile, catalogue membership, citation/literature-position evidence, or a
human-authored exclusion must continue to invalidate the relevant discovery
work. A machine `no_relationship` decision invalidates only its pair's
adjudication eligibility when its own prompt or endpoint scope changes; it must
not recursively invalidate candidate discovery.

Migration from selection-state schemas `1` through `3` must be local and
idempotent. When a legacy state contains sufficient selected-pair dispositions
and its catalogue and profile hashes establish equivalence, carry those pairs
into the schema-4 pool. Do not add semantic reconstruction logic for incomplete
legacy state. If the required data is absent, one normal production rediscovery
is safer than assuming equivalence. The targeted evaluation does not depend on
migration inference because it explicitly seeds a schema-4 state from the 87
frozen pair jobs.

### 4.5 Preserve precise provider imperfection handling

Keep the current maximum of fifteen pair jobs per adaptive packet. V0.25
returned 86 of 87 requested rows; one omission does not justify smaller
packets, another verifier, or a new retry workflow.

Continue to:

- commit every valid returned row once;
- preserve the provider packet and response;
- park a missing requested row as `provider_batch_missing_pair_row`;
- park a structurally incomplete directional row with its exact reason;
- make no semantic or omission retry; and
- never reprocess completed rows because another row in the packet failed.

A precisely parked provider imperfection satisfies accounting. It is not a
semantic release failure and does not justify silently manufacturing a result.
If repeated omission becomes a measured pattern in a later release, a single
omission-only completion packet may be reconsidered then; it is explicitly
out of scope for v0.26.

### 4.6 Preserve additive, reciprocal projection

The registry remains the source of truth. Models still never edit Markdown.

The targeted evaluation must use a private shadow registry and projection
root. It must not activate prompt-15 decisions inside the frozen v0.25
workspace. After prompt-15 adjudication in that isolated root:

- commit all current accepted, contextual, `no_relationship`, and parked
  decisions before projection;
- retain one active current machine decision per canonical pair;
- retire prompt-14 machine decisions only when the same pair receives a current
  prompt-15 accepted or `no_relationship` decision;
- preserve human, citation, Zotero, and alias relations;
- derive forward/inverse display labels locally;
- project symmetric links reciprocally without scoring endpoint order; and
- preserve user content outside managed graph blocks byte-for-byte.

No cluster refresh is performed for the targeted relationship evaluation.
Inherited v0.25 clusters are frozen historical artifacts and must be labeled as
such in the private Obsidian export; they are not evidence that v0.26 cluster
semantics were refreshed. In a normal full production build, changed accepted
relationships may invalidate only clusters whose actual semantic inputs depend
on those relationships, using the existing cluster refresh path.

## 5. Evaluation calibration

### 5.1 Preserve and amend v0.25 rather than rewrite history

Do not delete, edit, or silently overwrite the frozen v0.25 workspace or its
metrics. Write two new historical-calibration artifacts inside the v0.26
evaluation workspace:

- `evaluation/v025-relationship-reaudit.yml`; and
- `evaluation/v025-relationship-calibration-addendum.md`.

The addendum will record:

- four overextended bridge inferences;
- two mechanism-role ambiguities in symmetric `complements` links;
- no established direction reversal in those symmetric links;
- one separately observed endpoint-basis attribution swap;
- the original evaluator's classifications and aggregate counts preserved as
  historical evidence; and
- a recalibrated baseline under the dimensions below.

### 5.2 Score separate intellectual dimensions

Every audited relationship receives independent scores for:

1. **Endpoint attribution**
   - faithful;
   - minor imprecision;
   - materially unsupported or conflated; or
   - contradicted/invented hard fact.
2. **Bridge status**
   - directly supported;
   - transparent contextual synthesis;
   - overextended direct inference; or
   - superficial/no useful relationship.
3. **Taxonomy**
   - exact;
   - defensible alternative; or
   - materially wrong polarity.
4. **Direction applicability and accuracy**
   - not applicable for symmetric types;
   - correct;
   - ambiguous wording; or
   - genuinely reversed for an asymmetric type.
5. **Inference transparency**
   - source-owned finding;
   - clearly marked joint-reading/system inference; or
   - system inference incorrectly attributed to a source.
6. **Navigation value**
   - useful;
   - marginal; or
   - not useful.

Calculate the release metrics with explicit denominators:

- **endpoint fidelity** = audited published connections whose two endpoint
  bases are materially faithful / all audited published connections;
- **direct precision** = source-grounded and defensible direct relationships /
  all audited direct relationships;
- **contextual usefulness** = useful contextual relationships / all audited
  contextual relationships;
- **inference transparency** = correctly framed system inferences / all audited
  relationships that contain a system inference;
- **direction accuracy** = correct asymmetric relationships / all audited
  asymmetric relationships;
- **pair-decision utility** = useful accepted relationships plus correct
  `no_relationship` decisions / all audited pair decisions; and
- **valid-current-decision rate** = pair jobs with a current valid accepted or
  `no_relationship` decision / all 87 frozen pair jobs.

Symmetric relationships are excluded only from the direction denominator.
They remain fully auditable for endpoint attribution, bridge status, inference
transparency, polarity when applicable, and navigation value.

The audit manifest must first deduplicate the direct, changed, disputed,
previously parked, `no_relationship`, and contextual-sample groups into one
canonical pair set. Each pair contributes at most once to any denominator.

### 5.3 Calibrated release gates

Use rate-based semantic gates rather than automatic failure from one ordinary
interpretive imperfection:

- endpoint bases materially faithful: at least 95%;
- valid current decisions: at least 95% of the 87 frozen pair jobs;
- pair-decision utility: at least 90%;
- source-grounded, defensible direct precision: at least 90%;
- contextual-link usefulness: at least 85%;
- transparent inference framing where system synthesis is used: at least 95%;
- correct actor/reference among genuinely directional relationships: at least
  95%;
- reciprocal accepted-link projection: 100%;
- duplicate-active canonical pairs: zero; and
- selected pair-job accounting: 100%.

When the direction denominator is below twenty, report the exact rate but use a
small-sample rule of no more than one material error rather than mechanically
requiring 95%. This prevents one ordinary interpretive error from becoming a
disguised zero-tolerance gate while still exposing the small sample clearly.

Material polarity remains a mandatory diagnostic. A reversed
support/undermine or comparable orientation makes a direct relationship
indefensible and therefore lowers direct precision; when the relation is
asymmetric it also lowers direction accuracy. It does not create a redundant
third pass/fail gate.

Exact label agreement and optional anchor completeness remain advisory.
Symmetric relationships are excluded from direction denominators.

Material invented quotations, numbers, source identities, or findings count as
the most severe endpoint-attribution errors. They must always be reported with
source-linked evidence, but the verdict follows the calibrated fidelity rate
and the stated denominators rather than an undifferentiated zero-error rule.
Repeated error patterns and whether more matched decisions improved than
worsened remain mandatory diagnostics, not hidden pass/fail gates.

## 6. Tests

### 6.1 Prompt and semantic-contract tests

Add a small set of focused tests proving the prompt:

- is domain-neutral and contains no source-specific example;
- requires each endpoint basis to remain source-owned;
- distinguishes a directly supported shared proposition from a joint-reading
  inference;
- requires contextual framing when an additional interpretive step is needed;
- defines directional `supports` consistently with stored
  `supports`/`supported_by` projection;
- performs one same-call consistency review without asking for another model
  pass.

Use only the minimum varied fixtures needed to demonstrate that the instruction
is general rather than optimized for civil-war literature. Do not turn every
sentence in the prompt into a brittle substring test.

### 6.2 Normalization and persistence tests

Add tests proving:

- missing actor/reference is canonicalized for every existing symmetric type;
- symmetric normalization does not change the intellectual label or rationale;
- missing actor/reference remains parked for `supports`, `undermines`,
  `qualifies`, `extends`, `rival_explanation`, and
  `sequential_relationship`;
- `sequential_relationship` is recognized and is not misreported as an
  unsupported type;
- both canonical and v14 alias anchor field names are accepted;
- invalid optional anchor IDs are dropped with an advisory warning;
- empty anchors do not park a valid decision;
- completed rows survive a sibling omission;
- omitted rows are precisely parked without retry;
- prompt-14 links remain visible during reconciliation;
- prompt-15 accepted or negative decisions replace only the same pair's older
  machine decision;
- duplicate-active canonical pairs remain impossible; and
- reciprocal projection remains byte-stable.

### 6.3 Acyclic fingerprint and replay tests

Add tests proving:

- changing only the adjudication prompt causes zero discovery calls and reruns
  only pair adjudication from the persisted selected pool;
- selection-state schema `4` stores the complete canonical selected pool,
  provenance, dispositions, upstream identities, and a stable pool hash;
- an unchanged discovery identity plus changed adjudication identity does not
  take the whole-operation early return;
- neither identity changing produces a true no-op;
- changing only decision normalization does not rerun discovery;
- changing the discovery prompt or candidate policy reruns discovery;
- operational changes to deadlines, workers, concurrency, call ceilings, retry
  reserve, or cluster settings do not invalidate semantic relationship work;
- unchanged candidates are reusable across run IDs;
- selection-state schemas 1–3 migrate locally when their upstream identity is
  provably unchanged;
- ambiguous legacy state safely falls back to rediscovery;
- an unchanged prompt-15 relationship operation makes zero calls and writes;
  and
- frontmatter or projection-only edits never invalidate discovery or
  adjudication.

### 6.4 Full local verification

Run the complete existing pytest suite, Ruff, bytecode compilation, package
build, migration tests, and replay tests with no regressions.

## 7. Limited paid comparative evaluation

### 7.1 Workspace and frozen inputs

Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v026-relationship-evaluation-20260802`

Use a fresh isolated clone containing:

- the exact 193 v0.25 source records and 192 canonical works;
- the unchanged 193 atomic notes and profiles;
- the exact 87 v0.25 selected pair jobs;
- both complete atomic notes already frozen for every pair;
- the v0.25 relationship registry as the comparison baseline; and
- the original v0.25 source-first audit evidence used to construct a new
  calibrated re-audit manifest in this workspace.

Treat every copied v0.25 artifact as immutable historical input. The entire
isolated v0.26 clone is the shadow workspace root used by the existing
workspace-relative registry and projection functions; preserve comparison
copies of the v0.25 files under a historical baseline directory inside that
clone. Store prompt-15 active decisions, events, receipts, and projections only
in the clone's normal v0.26 machine paths.

Do not copy the contaminated v0.25 run progress or provider-attempt ledger as
the active v0.26 run state. Preserve them only as historical evidence.

Run ID:

`eval-global-v026-relationships-20260802`

### 7.2 Execution

Exercise the production relationship invalidation path rather than calling the
model directly. The bounded evaluation driver must enter
`_run_relationship_reasoning` with a schema-4 state containing the frozen
selected pool and an unchanged discovery identity but changed adjudication
identity. It may use a small private helper extracted from that production path
only if both production and evaluation call the same helper.

The driver must reuse the existing immutable packet construction,
checkpointing, response parser, validator, `_write_relationship_run_ledger`,
`persist_relationship_registry`, and `_project_atomic_graph` functions. It must
not reproduce their orchestration independently. Do not invoke full
`build-map`, because that would unnecessarily enter cluster work.

Configuration:

- DeepSeek `deepseek-v4-flash`;
- the same maximum reasoning setting used in v0.25;
- `provider_concurrency=auto`;
- six concurrent adaptive packets of at most fifteen pair jobs;
- no semantic retry or verifier;
- one existing exact retry only for a genuine `ProviderEmptyResponse`;
- hard cumulative ceiling: seven provider attempts; and
- relationship deadline: 1,800 seconds.

Expected calls:

| Stage | Maximum |
|---|---:|
| Relationship adjudication | 6 |
| Genuine provider-empty retry reserve | 1 |
| Source/profile generation | 0 |
| Family planning/routing/discovery | 0 |
| Cluster/acquisition synthesis | 0 |
| **Hard ceiling** | **7** |

The existing provider wrapper—not the evaluation driver—owns the one permitted
empty-response retry. The driver must not add a second retry layer. Provider
ledgers must prove zero family-planning, routing, or discovery calls and no more
than six ordinary adjudication attempts plus one genuine empty-response retry.

The evaluation driver is private test infrastructure, not a new public API,
CLI mode, or parallel relationship orchestrator.

### 7.3 Source-first relationship audit

Before examining prompt-15 rationales, read both complete notes for:

- every prompt-15 direct relationship;
- every decision that changed from v0.25;
- all seven disputed v0.25 comparisons: the six bridge/mechanism cases and the
  separately observed endpoint-attribution swap;
- the three previously superficial contextual comparisons;
- the three previously parked pair jobs if prompt 15 returns decisions; and
- every prompt-15 `no_relationship` decision, including every pair changed from
  an accepted v0.25 relationship to `no_relationship`; and
- a deterministic sample of at least 30 prompt-15 contextual relationships.

For each relationship, record the six calibrated dimensions from section 5.2.
Compare matched pairs directly with v0.25 rather than comparing unstable
generated relation IDs.

Acceptance:

- endpoint bases materially faithful at least 95%;
- valid current decisions at least 95% of all 87 pair jobs;
- pair-decision utility at least 90%;
- direct precision at least 90%;
- contextual usefulness at least 85%;
- transparent inference framing at least 95%;
- directional actor/reference accuracy at least 95%, excluding symmetric
  types;
- reciprocal projection 100%;
- duplicate-active canonical pairs zero;
- all 87 jobs answered or precisely parked; and
- no completed pair reprocessed.

Report exact taxonomy agreement, anchor completeness, repeated semantic
patterns, and whether more matched relationships improved than worsened
descriptively only.

Also compare the number of direct, contextual, and `no_relationship` decisions
with v0.25. A smaller visible graph is acceptable only when the source-first
audit shows that the rejected or downgraded links were not useful direct
relationships. Correct rejection counts positively in pair-decision utility;
discarding a valuable relationship counts as a miss.

### 7.4 Known-case comparison

The disputed v0.25 cases remain evaluation fixtures, never prompt examples.
Their preliminary classifications in this plan are hypotheses to test, not
predetermined evaluator answers.

The audit should determine whether prompt 15:

- preserves the accurate endpoint bases in both mechanism-role ambiguity
  cases;
- avoids treating symmetric links as directional;
- either narrows the shared proposition, transparently marks a contextual
  inference, or returns no relationship;
- distinguishes similarly named but substantively different constructs;
- avoids converting one process stage or outcome into another; and
- avoids treating compatible levels of analysis as direct refutation unless the
  notes establish incompatibility.

The evaluator should not require one predetermined label. A source-grounded
`complements`, `contextual_connection`, `qualifies`, `contrasts`, or
`no_relationship` decision may all be defensible depending on the returned
proposition and rationale.

### 7.5 Pristine replay

Immediately after the evaluated relationship projection:

1. snapshot the relationship provider ledger, pair-job statuses, semantic
   registry, relationship event history, atomic managed graph blocks, hashes,
   mtimes, and file membership;
2. invoke the exact same relationship-only operation with the same run ID and
   normal receipt path;
3. do not bypass, remove, or rename any receipt or checkpoint; and
4. compare the snapshots independently.

Require:

- zero new provider calls;
- zero semantic or projection changes;
- byte-identical files;
- unchanged mtimes;
- no additions or removals; and
- stable pair-job, decision, registry, event, and projection identities.

Do not patch or diagnostically rebuild the evaluated workspace after the
snapshot. If a deterministic defect is discovered, preserve the failed
workspace, fix and test the repository separately, and use a new clone for any
subsequent evaluation.

## 8. Deliverables

Write inside the v0.26 evaluation workspace:

- `evaluation/v026-relationship-comparison.md`;
- `evaluation/v025-relationship-reaudit.yml`;
- `evaluation/v025-relationship-calibration-addendum.md`;
- `evaluation/v026-relationship-metrics.yml`;
- `evaluation/v026-pair-comparison.yml`;
- `evaluation/v026-packet-completion-metrics.yml`;
- `evaluation/v026-provider-metrics.yml`;
- `evaluation/v026-runtime-metrics.yml`;
- `evaluation/v026-replay-metrics.yml`;
- the deterministic contextual sampling manifest;
- the list of every changed, parked, omitted, and structurally invalid pair;
- representative successes, residual material errors, and advisory label
  disagreements; and
- a private Obsidian export clearly labeled as a relationship-only v0.26
  projection, containing the updated reciprocal graph links and frozen
  historical v0.25 clusters.

The comparison report must clearly distinguish:

- corrected v0.25 baseline metrics;
- prompt-15 v0.26 results;
- source-attribution quality;
- direct-proposition quality;
- transparent contextual synthesis;
- genuinely directional accuracy;
- exact labels and anchors as advisory diagnostics; and
- production defects from evaluation-artifact defects.

Its verdict is scoped to the v0.26 relationship-adjudication subsystem. It may
cite the previously accepted atomic, discovery, cluster, and acquisition
results as inherited evidence, but it must not present this limited run as a
new full-system evaluation.

## 9. Historical safeguards and explicit non-goals

V0.26 must not repeat earlier remediation patterns that increased complexity or
cost without addressing the active defect.

Do not add:

- another relationship verifier or correction call;
- sentence-level deterministic semantic checks;
- mandatory evidence anchors;
- domain-specific examples in the production prompt;
- exact-40 bridge recall as a release gate;
- new collection hierarchy, virtual-index, shard-routing, or discovery logic;
- source or profile prompt changes;
- cluster planning, synthesis, scheduling, or acquisition changes;
- a model A/B test or model switch during this prompt diagnosis;
- a public relationship-only CLI/API;
- a full 193/195-source regeneration;
- a full combined-map paid evaluation;
- model-written Markdown;
- live web search; or
- Zotero writes or duplicate cleanup.

The existing indexing, discovery, cluster, acquisition, identity, provider,
and additive-projection systems remain in place. V0.26 changes only what is
required to distinguish accurate source findings from useful but explicitly
system-inferred relationships, reuse unchanged discovery work, and evaluate
that distinction fairly.

## 10. Final acceptance and next decision

V0.26 passes when:

- the local suite and build are clean;
- changing only adjudication prompt identity makes zero discovery calls;
- the seven-attempt ceiling is respected;
- the calibrated relationship gates pass;
- pair accounting and reciprocal projection remain complete;
- the pristine replay is zero-call and zero-write; and
- no source/profile work, discovery provider work, candidate content, cluster,
  acquisition, or Zotero state is regenerated or mutated outside the stated
  relationship scope.

If the prompt-15 relationship audit passes, accept the relationship architecture
and defer the next full end-to-end two-folder run until another release-level
change actually affects multiple layers.

If endpoint fidelity or direct-proposition quality still fails, stop and report
the exact pattern. Do not add a verifier automatically. A model change or a
more substantial relationship-contract redesign should be considered only
after prompt 15 has been isolated and measured against the same frozen pairs.
