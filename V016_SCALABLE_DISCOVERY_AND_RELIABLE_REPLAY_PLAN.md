# Auto-Zettelkasten v0.16 Scalable Discovery and Reliable Replay Plan

**Status:** Implemented; comparative evaluation pending

**Date:** 2026-07-29

**Foundation:** Engine `0.15.0`, artifact schema `1.13`, source catalogue
schema `3`, relationship registry schema `5`

**Evidence:** The frozen 195-source v0.15 Mediation and Conflict Relapse
evaluation, the v0.14→v0.15 comparison, the exported Obsidian graph, and the
subsequent architecture review

## 1. Objective

v0.16 should improve the existing standalone Python workflow rather than add a
native Codex or Claude orchestration requirement.

The release has five objectives:

1. Preserve detailed one-shot atomic notes while making evidence capture
   method-sensitive and statistical interpretation more reliable.
2. Scale relationship discovery through the existing Zotero collection tree and
   compact indexes without comparing every collection pair.
3. Preserve useful broad connections while distinguishing direct intellectual
   relationships from contextual navigation.
4. Reduce relationship calls and repeated input tokens through deduplicated,
   token-bounded packets of up to 30 pair jobs.
5. Make an unchanged replay perform zero provider calls and zero file writes.

The release must remain local-first, Zotero-read-only, provider-portable, and
compatible with ordinary Markdown and Obsidian.

## 2. Evidence from v0.15

### 2.1 What must be preserved

The frozen v0.15 run established:

- 195 sources processed and globally mapped in approximately 55 minutes;
- atomic critical-fact recall of 119/120 (99.17%);
- 18/18 available statistical notes preserved their headline raw values;
- 14 statistical explanations improved over v0.14 and four worsened;
- every retained cluster member received a specific contribution;
- cluster membership relevance reached 51/53 (96.23%);
- three genuine mixed-literature clusters were produced;
- 93 substantive relationships projected as 186/186 reciprocal atomic links;
- explicit cross-folder citations were recalled 2/2;
- all 53 atomic/cluster memberships projected reciprocally; and
- unchanged replay made zero provider calls and did not change visible note or
  cluster Markdown.

These are foundations. v0.16 must not redesign additive Markdown ownership,
replace the registry as the source of truth, or introduce a model that edits
notes directly.

### 2.2 What must improve

The same evaluation found:

- 15 sources were parked, mostly because otherwise useful one-shot responses
  were invalid JSON;
- strict atomic claim support was 176/192 (91.67%);
- four of 18 available statistical explanations made material scale,
  reference, or estimand mistakes;
- 13 clusters contained six inaccurate statistical presentations and several
  overstated debates;
- strict cross-folder relationship precision was 13/35 (37.14%);
- another five links were useful but too broad or misclassified;
- curated candidate-bridge recall was 6/40 (15%);
- `complements` was used for both direct intellectual complementarity and broad
  contextual adjacency;
- relationship packets repeated complete atomic-note bodies across pair jobs;
  and
- replay rewrote 11 machine-state files by adding false revision events,
  duplicating history, and refreshing timestamps.

The relationship result should not be interpreted as proof that broad links are
useless. It shows that the graph needs visible relationship strength and more
specific types.

## 3. Settled design decisions

The following decisions are part of this plan.

1. The standalone Python pipeline remains the production orchestrator.
2. Codex, Claude Code, and other native agents remain optional development,
   audit, and interactive-research tools.
3. DeepSeek `deepseek-v4-flash` remains the default model.
4. No second atomic-note verification call is added.
5. No deterministic statistical or intellectual validator is added.
6. No model-quality retry loop is added.
7. Local warnings remain advisory and do not veto a useful note.
8. Complete atomic notes remain the evidence supplied for final relationship
   and cluster judgments.
9. The master index never contains every full atomic note.
10. Hierarchical routing is used only when the selected compact catalogue does
    not fit safely in one call.
11. Unclustered sources remain neutral and eligible for future clustering.
12. Cluster coverage remains descriptive, not an acceptance threshold.
13. Zotero remains read-only.
14. Desktop, billing, account, and synchronization work remains outside this
    release.

## 4. Target workflow

```text
Zotero collection tree and frozen source content
    ↓
parallel one-shot source bundles
    ↓
deterministic master, collection, subcollection, and shard indexes
    ↓
compact collection routing only when needed
    ↓
one bounded source-pair discovery call for selected scope
    ↓
parallel deduplicated full-note relationship packets
    ↓
one cluster plan over the selected eligible catalogue
    ↓
parallel full-note cluster writers
    ↓
single registry commit
    ↓
additive reciprocal Markdown projection
```

Semantic dependencies remain one-way:

```text
frozen source
    → source bundle
    → compact profile and indexes
    → relationship candidates
    → relationship decisions
    → cluster plan
    → cluster syntheses
    → registries
    → Markdown views
```

Nothing below a layer may invalidate an unchanged layer above it.

## 5. Compact, method-sensitive source generation

### 5.1 Consolidate the prompt

Do not append an independent rule for every observed failure. Rewrite the
source prompt around eight governing requirements:

1. Capture the thesis, method or knowledge basis, evidence, findings,
   limitations, literature position, and contribution.
2. Capture all important evidence needed to understand and evaluate the
   argument, not merely a generic summary.
3. Preserve the source's distinctions among reported observations, modeled
   estimates, author interpretations, and system explanations.
4. Preserve statistical scale, estimand, baseline, reference condition,
   uncertainty, and observed range.
5. Preserve the source's degree of causal and comparative certainty.
6. Keep every claim within the recovered-document scope.
7. Explain the most important technical findings in plain language without
   inventing numbers, comparisons, or analogies.
8. Return the required source-bundle schema after a same-call self-review.

Formatting and method-specific detail belong in the schema and field
descriptions rather than a growing list of prose prohibitions.

### 5.2 Method-sensitive evidence contract

Use the existing source bundle and evidence-anchor structures. Do not add a
second source result.

Require the model to populate evidence according to the source's knowledge
basis.

#### Quantitative research

Capture, where important:

- dataset, population, period, sample, and unit of analysis;
- dependent and independent variables;
- treatment, comparison, baseline, and reference groups;
- headline estimates on their original scale;
- predicted probabilities and marginal effects when reported;
- uncertainty, confidence intervals, p-values, and null findings;
- interaction conditions;
- robustness and sensitivity results;
- missing-data and measurement limitations; and
- whether the design supports association, prediction, or causal inference.

#### Qualitative, comparative, and case research

Capture:

- case selection and comparison logic;
- interviews, archives, documents, observations, or other evidence sources;
- chronology and process evidence;
- mechanisms;
- decisive examples and counterexamples;
- within-case and cross-case variation;
- alternative explanations; and
- limits on generalization.

#### Historical research

Capture:

- relevant chronology;
- primary and secondary evidence;
- historical examples and analogies;
- the inference drawn from each analogy;
- differences that limit the analogy; and
- competing interpretations.

#### Theoretical and normative work

Capture:

- assumptions;
- causal or logical sequence;
- mechanisms;
- propositions and predictions;
- thought experiments, examples, and analogies;
- rival theories;
- normative premises; and
- scope conditions.

#### Practitioner guidance and institutional reports

Capture:

- institutional or practitioner knowledge basis;
- consultations, cases, datasets, and commissioned research;
- recommendations;
- implementation examples;
- operational constraints;
- acknowledged uncertainties; and
- the difference between evidence and institutional prescription.

#### Literature reviews and synthetic works

Capture:

- the organizing debate;
- the most important cited positions;
- agreement, disagreement, and unresolved questions;
- the evidence bases being compared;
- the author's characterization of cited work; and
- the author's distinct contribution.

The model selects the details that are consequential to the source. It is not
required to fill irrelevant categories.

### 5.3 Statistical interpretation

Retain v0.15's useful direct explanations such as `11.09% versus 50.03%`.
Do not require a “per 100” paraphrase when percentages are already clear.

The technical and plain-English sections must preserve the same:

- estimand;
- event or outcome;
- unit and scale;
- baseline and reference group;
- model or observational status;
- uncertainty; and
- observed range.

The plain-English section may simplify wording but may not change:

- a predicted probability into an observed frequency;
- a one-standard-deviation effect into a one-unit effect;
- an odds or hazard ratio into a probability;
- an interaction into an unconditional main effect;
- a median into an average;
- an association into a causal effect; or
- a source-reported range into an invented teaching example.

These invariants belong in the same-call self-review. They do not create a
retry or publication gate.

### 5.4 One-shot output reliability

Invalid JSON should not discard an otherwise complete source analysis.

Before parking a source:

1. Parse the response normally.
2. Remove transport wrappers such as Markdown fences and leading commentary.
3. Recover one unambiguous top-level JSON object when braces and strings are
   mechanically complete.
4. Normalize locally repairable field aliases and scalar/list shape mistakes
   already supported by the schema.
5. Treat missing noncritical presentation sections, including locators, as
   advisory contract warnings when the core thesis, method or knowledge basis,
   evidence, and findings are substantively present elsewhere in the returned
   bundle.
6. Render a neutral “not separately returned” placeholder rather than asking
   local code to invent the missing prose.
7. Validate the repaired object once.

Do not infer missing intellectual content locally. A response remains parked
only for a gross failure: it is unrecoverably truncated or invalid, concerns the
wrong source, lacks the core thesis/method/evidence/findings, or contains
ambiguous competing objects. Preserve the raw response and warning privately for
diagnosis.

No provider retry is added for an unchanged semantic or contract failure.

## 6. Stage-specific DeepSeek capabilities

Update the built-in DeepSeek capability record to the provider's current
1,000,000-token context and 384,000-token maximum output. Keep lower internal
stage allocations:

| Stage | Model | Reasoning | Internal maximum output |
|---|---|---:|---:|
| Source bundle | V4 Flash | high | 64,000 |
| Hierarchical chunk | V4 Flash | high | existing compact limit |
| Source synthesis | V4 Flash | high | 16,000 |
| Collection/shard routing | V4 Flash | high | 8,000 |
| Relationship candidates | V4 Flash | high | 32,000 |
| Relationship adjudication | V4 Flash | max | 128,000 |
| Cluster plan | V4 Flash | max | 64,000 |
| Cluster synthesis | V4 Flash | max | 128,000 |

These are internal ceilings, not required output lengths. The provider bills
actual tokens generated.

Do not add public per-stage model settings in v0.16. Continue exposing the
existing provider, model, and concurrency settings. Capability overrides for
custom providers remain supported.

Do not introduce V4 Pro into the default path. A separate benchmark may compare
Flash and Pro on the frozen relationship and statistical samples, but a
multi-model production architecture requires demonstrated quality gain.

## 7. Agent-friendly scalable indexes

### 7.1 Reuse the current index system

Keep the existing deterministic:

- master `INDEX.md`;
- `source_catalogue.yml`;
- Zotero collection and subcollection tree;
- per-collection indexes;
- direct-member source shards;
- literature shards;
- cluster catalogue; and
- routing cards.

Index construction makes no provider calls and rewrites only changed files.

### 7.2 Master index

The master index contains:

- catalogue revision;
- top-level collection tree;
- compact collection routing cards;
- source counts and processing status;
- cluster index link;
- currently-unclustered view link; and
- missing-important-source ledger link.

It does not contain every source entry.

### 7.3 Collection and subcollection routing cards

Add one bounded routing card to each collection catalogue record and index:

- collection key and name;
- parent and child keys;
- direct and descendant source counts;
- short scope derived from existing compact profiles;
- dominant controlled facets;
- method and evidence-base mix;
- at most five representative thesis snippets;
- important cases, periods, and datasets when dominant;
- active cluster IDs;
- strongest existing cross-collection connection counts; and
- revision hash.

Selection and rendering are deterministic. Thesis and method text comes from
the existing probabilistically generated compact profiles; the index builder
does not ask a model to rewrite the collection index.

Parent indexes list direct members through shards and link to children. They do
not duplicate every descendant entry.

### 7.4 Source shards

Each compact entry remains bounded to:

- source and Zotero IDs;
- title, author, and year;
- one compact thesis;
- one compact method or knowledge-basis statement;
- document scope and evidence coverage;
- controlled mechanism, outcome, case, population, period, and dataset facets;
- matched literature positions and unresolved important citations;
- active clusters; and
- active graph neighbours.

Full atomic notes never enter the master or collection routing cards.

### 7.5 Obsidian compatibility

Indexes remain ordinary Markdown notes using Obsidian-compatible wikilinks,
properties, tags, backlinks, and graph projection. Obsidian's search and
backlinks supplement the generated semantic indexes; they do not replace them.

Fix apostrophe and punctuation normalization in exported wikilinks so the
Paris and Fortna failures reproduce as passing fixtures.

## 8. Scalable probabilistic discovery

### 8.1 Normal-size catalogue

When the complete selected compact catalogue fits within the measured
relationship-discovery budget:

- make no routing call;
- make one relationship-candidate call; and
- include collection membership so cross-collection bridges are not crowded
  out by same-collection candidates.

The 195-source evaluation remains on this path.

### 8.2 Oversized catalogue

Do not compare every collection pair.

When the compact catalogue is too large:

1. Give DeepSeek the bounded collection routing cards.
2. Select relevant top-level collections.
3. If needed, inspect only the selected collections' child cards.
4. Select relevant source shards.
5. Load compact profiles only from the selected shards.
6. Propose candidate pairs from that bounded set.

Batch all cards at the same tree level when they fit. Typical routing cost is
one call per traversed level, not one call per collection pair.

If the collection-card level itself is oversized, partition it by parent branch
and route the independent branches concurrently. Reconcile selected collection
and shard IDs deterministically by union and cap; do not ask a second model to
rewrite routing results.

### 8.3 Incremental growth

For new or changed sources:

- insert compact profiles into their collection and literature shards;
- route only those sources against collection cards and graph neighbourhoods;
- generate a bounded candidate neighbourhood;
- adjudicate only new or invalidated pairs; and
- refresh only affected clusters.

Do not rescan every old pair. Reuse accepted, rejected, contextual, and
no-relationship memory while endpoint and prompt identities remain unchanged.

### 8.4 Candidate sources

Candidate discovery combines:

- exact DOI, Zotero, title, author, and year matches;
- fuzzy and probabilistically resolved literature-position matches;
- unresolved important citations;
- compact theses and methods;
- shared or opposed mechanisms and outcomes;
- cases, populations, periods, and datasets;
- collection routing;
- existing graph neighbourhoods;
- cluster membership; and
- prior negative-pair memory.

Exact deterministic matches are useful high-priority signals, not a discovery
gate. Failure to match a citation cannot prevent probabilistic discovery.

The candidate call returns:

- endpoint IDs;
- a precise proposition or navigational question connecting them;
- candidate class: `direct_intellectual`, `contextual`, or `citation`;
- evidence for why full-note adjudication is worthwhile; and
- confidence.

Candidate classes are retrieval hints, not final relationship decisions.

## 9. Direct and contextual relationship graph

### 9.1 Preserve useful broad links

Do not reject every broad cross-stage or cross-method connection. Instead,
separate intellectual claims from contextual navigation.

#### Direct intellectual relationships

- `supports`
- `undermines`
- `qualifies`
- `extends`
- `complements`
- `contrasts`
- `rival_explanation`
- `boundary_contrast`
- `methodological_fault_line`
- `sequential_relationship`
- `interpretive_or_normative_disagreement`

`complements` requires a bounded shared proposition, question, or mechanism.

#### Contextual relationships

Add:

- `contextual_connection`

This means the works illuminate different dimensions, stages, cases, or
evidence bases that are useful to inspect together without claiming agreement
or direct evidentiary support.

A contextual relationship must state:

- the exact dimension contributed by each source;
- why joint reading is useful;
- the boundary preventing a stronger direct label; and
- evidence from both notes.

“Together they provide a fuller picture” is insufficient unless the rationale
names the distinct contributions.

Explicit `cites` and `cited_by` remain structurally separate from intellectual
agreement.

### 9.2 Relationship decision

The adjudicator receives:

- both complete semantic atomic notes;
- both compact profiles;
- source-owned anchors and quantitative results;
- relevant literature-position records;
- citation or Zotero context;
- candidate rationale and class;
- bounded existing neighbours; and
- prior pair memory.

It returns one of:

- direct relationship;
- contextual relationship;
- no meaningful relationship; or
- needs more context.

It must decide the proposition or navigational question, type, direction,
boundary, evidence, and visible rationale. Local code validates IDs and
projection only.

No independent verification call is added.

## 10. Deduplicated adaptive relationship packets

### 10.1 Packet shape

Keep the persistent `RelationshipPairJob` record and its cache identity
unchanged. Replace repeated full notes only in the provider transport envelope:

```yaml
source_documents:
  source-A:
    semantic_hash: ...
    atomic_note: ...
  source-B:
    semantic_hash: ...
    atomic_note: ...
source_profiles:
  source-A: ...
  source-B: ...
pair_jobs:
  - pair_job_id: ...
    left_source_id: source-A
    right_source_id: source-B
    candidate_basis: ...
```

Each unique note and profile appears once per request. Pair jobs refer to source
IDs. Stable source ordering places shared instructions and source documents in
repeatable prefixes for provider cache reuse.

The semantic identity of each pair job remains based on its endpoints, endpoint
semantic hashes, candidate evidence, prompt, model, provider, and policy. It
does not depend on transport packet membership.

### 10.2 Token-bounded packing

Pack:

- at most 30 pair jobs;
- at most the measured relationship input budget;
- only complete jobs; and
- as many jobs sharing source documents as practical without changing semantic
  order.

Use a target input ceiling of 65% of the declared context window, reserving the
remainder for system instructions, reasoning, and output. Measure the serialized
packet rather than estimating from pair count alone.

Oversized individual notes reduce the job count automatically. A single pair
that cannot fit is parked with an explicit context reason.

### 10.3 Batch benchmark

Before selecting the release default, run the frozen 35-link audit set through
maximum packet sizes 8, 16, 24, and 30.

Measure:

- strict direct-link precision;
- contextual-link usefulness;
- direction and evidence ownership;
- missing decisions;
- JSON validity;
- input, reasoning, and output tokens;
- cache hits;
- latency; and
- provider cost.

Select the largest maximum that does not materially worsen intellectual or
contract quality. The implementation still supports up to 30 even if the
measured release default is lower.

### 10.4 Concurrency

All independent relationship packets run concurrently under
`provider_concurrency=auto`. Do not preserve an artificial four-worker cloud
limit.

Keep local commits single-owner:

1. workers write job-owned results;
2. the coordinator merges validated decisions;
3. the registry commits once; and
4. Markdown projection begins afterward.

The provider's account concurrency is a ceiling, not a target. Launch only the
ready independent packets.

## 11. Cluster planning and writing

### 11.1 Planning

Keep one global cluster-plan call when the compact eligible catalogue fits.
For oversized libraries, reuse the same collection hierarchy and source shards
used for relationship discovery, then reconcile compact family cards.

The planner may organize clusters around:

- a question;
- debate;
- mechanism;
- outcome;
- method;
- case family;
- historical sequence; or
- practice problem.

A forced question is not required.

### 11.2 Writing

Each cluster writer continues to receive all complete semantic atomic notes for
all proposed members. It may omit a member only when it cannot provide a
specific cluster-relevant contribution.

The writer must:

- give every retained member a specific contribution;
- preserve source-reported statistics and nulls;
- explain only the central quantitative results;
- distinguish source arithmetic from its own interpretation;
- preserve denominators, baselines, interaction regions, and controlled nulls;
- identify disagreements rather than smoothing them into consensus;
- distinguish direct contradiction from tension across different constructs;
- state evidence boundaries; and
- create reciprocal membership and neighboring-cluster proposals.

Do not add a cluster verifier or repair call. Improve the single writer contract
and audit statistically.

### 11.3 Membership accounting

After writers finish, local code computes:

```text
currently_unclustered
= eligible analytical sources
- sources in at least one active cluster
```

No coverage threshold, permanent rejection, or re-homing call is introduced.

## 12. Strict unchanged replay

Fix replay at the shared state writers rather than special-casing the evaluation
command.

### 12.1 Relationship state

- Give every ledger and payload-history event a stable semantic event ID.
- Do not append an event already present.
- Do not duplicate `no_relationship` payload history.
- Do not change status or timestamps on a checkpoint hit.

### 12.2 Cluster state

- Compare membership, prose, relationship, and evidence hashes before appending
  a cluster revision.
- Do not change `new` to `revision` when no semantic field changed.
- Do not duplicate writer-membership revision events.
- Preserve the last semantic `updated_at` on unchanged replay.

### 12.3 Manifests and packets

- Separate semantic timestamps from observation timestamps.
- Do not render a new `updated_at` into a semantic artifact on replay.
- Use write-if-bytes-changed for every generated YAML and Markdown projection.
- Provider ledgers may record an explicitly requested operational replay only
  outside the immutable generated-artifact snapshot; a checkpoint-only build
  records no new semantic event.

An unchanged replay must make zero provider calls and leave bytes and mtimes
unchanged across the full generated-artifact manifest.

## 13. Call and runtime targets

For a normal-size corpus with 120 candidate pairs and 13 clusters:

```text
1 relationship-candidate call
+ 4 relationship packets at 30 jobs each
+ 1 cluster-plan call
+ 13 concurrent cluster-writer calls
= 19 literature calls
```

If one collection-routing call is required, the total becomes 20. A three-level
oversized hierarchy would typically add no more than three routing calls before
source discovery.

Keep the existing cumulative 100-call literature ceiling. The clean 195-source
target is no more than 25 literature calls, unless the corpus legitimately
produces more than 18 clusters.

Source generation remains one paid call per source on the normal direct path.
Local JSON normalization costs no provider call.

Runtime targets:

- no regression beyond two hours for the frozen 195-source production run;
- target no more than 90 minutes;
- record extraction/OCR, source-provider, routing, candidate, relationship,
  cluster-plan, cluster-writer, projection, and replay time separately; and
- record peak local workers separately from peak provider concurrency.

## 14. Versioning and compatibility

Planned identities:

- engine `0.16.0`;
- artifact schema `1.14` because relationship tier is a new visible persisted
  semantic field;
- source catalogue schema `4` for collection routing cards;
- relationship registry schema `6` for relationship tier and
  `contextual_connection`;
- source prompt `11`;
- source-bundle prompt `5`;
- relationship prompt `6`;
- relationship decision contract `5`;
- cluster-plan prompt remains `5` unless its response contract changes; and
- cluster-synthesis prompt `26`.

Migration is local, lazy, and idempotent:

- no provider calls;
- no source rereads;
- no automatic atomic or cluster prose rewrite;
- preserve human-authored links;
- preserve existing machine relationship labels;
- classify legacy non-`complements` types into the corresponding direct tier;
- mark legacy machine `complements` as `legacy_unclassified` until the pair is
  naturally reconsidered; and
- keep its existing visible projection rather than silently deleting it.

Custom reasoners remain compatible through capability detection. A reasoner
using the older pair-job protocol may receive legacy packets capped at eight
jobs. Built-in reasoners use the deduplicated decision-contract-v5 packet.

## 15. Likely code touchpoints

- `src/auto_zettelkasten/readers.py`
  - consolidated source contract;
  - method-sensitive evidence guidance;
  - stage-specific output ceilings;
  - updated DeepSeek capability;
  - contextual relationship prompt; and
  - tolerant local response normalization.
- `src/auto_zettelkasten/indexes.py`
  - bounded collection routing cards;
  - deterministic collection hierarchy payload; and
  - catalogue schema `4`.
- `src/auto_zettelkasten/pipeline.py`
  - oversized-catalogue routing;
  - incremental routing;
  - deduplicated provider envelopes;
  - adaptive 30-job packing; and
  - stable selection-state writes.
- `src/auto_zettelkasten/models.py`
  - engine and artifact identities;
  - decision-contract-v5 transport models; and
  - 1–30 provider-batch validation.
- `src/auto_zettelkasten/relationships.py`
  - direct/contextual tier;
  - `contextual_connection`;
  - reciprocal labels; and
  - registry schema `6`.
- `src/auto_zettelkasten/literature.py`
  - stage allocations;
  - stable cluster event identities;
  - semantic write-if-changed persistence; and
  - reciprocal neighboring-cluster projection.
- `src/auto_zettelkasten/migration.py`
  - local, lazy, idempotent v0.16 migration.
- `src/auto_zettelkasten/obsidian.py`
  - apostrophe and punctuation-safe wikilink export.

## 16. Implementation order

### Phase 1 — Reproduce and protect

- Add exact regression fixtures for the 11 replay rewrites.
- Add invalid-JSON source fixtures based on v0.15 parked responses.
- Freeze the 35 audited cross-folder links, 40 candidate bridges, 20 statistical
  sources, and 13 cluster audit cases.

### Phase 2 — Replay and local reliability

- Deduplicate relationship and cluster event writes.
- Suppress timestamp-only manifest writes.
- Add local source-envelope normalization.
- Fix apostrophe-containing Obsidian links.
- Require a zero-call, zero-write replay before semantic prompt changes.

### Phase 3 — Source prompt and capabilities

- Consolidate the source prompt.
- Add method-sensitive field guidance.
- Strengthen same-call statistical invariants.
- Update DeepSeek capability and internal stage ceilings.
- Regenerate only focused fixtures and review differences.

### Phase 4 — Index and routing

- Add deterministic collection routing cards.
- Keep the ordinary no-routing path for the 195-source corpus.
- Add hierarchical oversized-catalogue routing and incremental-source routing.
- Prove that no collection-pair Cartesian product is constructed.

### Phase 5 — Relationship graph

- Add direct/contextual relationship tiers.
- Add `contextual_connection`.
- Introduce deduplicated source-document packets.
- Implement token-bounded packing up to 30 jobs.
- Run batch-size benchmarks.
- Keep all independent packets concurrent.

### Phase 6 — Cluster prompt

- Apply the compact statistical and debate-boundary invariants.
- Preserve full-member-note input and concurrent single-call writers.
- Fix reciprocal related-cluster projection.

### Phase 7 — Full validation

- Run focused tests.
- Run the complete existing suite.
- Build the package.
- Run a fresh frozen 195-source comparison only after code and prompts are
  frozen.

## 17. Test plan

### Source generation

- Quantitative article preserves estimand, baseline, scale, nulls, interactions,
  and observed range.
- Predicted probability never becomes an observed sample frequency.
- One-standard-deviation effect never becomes a per-unit effect.
- Qualitative source captures cases, mechanism, counterevidence, and process
  evidence.
- Historical source preserves an analogy and its boundary.
- Theoretical source preserves assumptions and thought experiment.
- Practitioner report distinguishes evidence from recommendation.
- Literature review captures important cited positions and distinct
  contribution.
- Fenced or prefaced valid JSON repairs locally.
- Missing noncritical presentation sections produce an advisory warning and a
  usable note when core semantic content exists elsewhere in the bundle.
- Ambiguous, truncated, wrong-source, and semantically incomplete outputs park
  without a paid retry.

### Indexes and routing

- Every Zotero collection and subcollection receives a bounded index and routing
  card.
- Parent indexes link to children without duplicating descendant source entries.
- A changed source rewrites only its compact catalogue, relevant shards, and
  collection ancestors.
- Unchanged index replay preserves bytes and mtimes.
- A synthetic 10,000-source, 500-collection catalogue routes without placing
  every compact profile into one prompt.
- Normal-size catalogues skip routing entirely.
- Hierarchical routing never constructs all collection pairs.

### Relationships

- A repeated source appears once in `source_documents`.
- Pair-job identity is independent of transport packet membership.
- Packets obey both the token ceiling and 30-job ceiling.
- Independent packets run concurrently.
- Direct complementarity requires a bounded shared proposition.
- Broad but useful adjacency becomes a source-grounded
  `contextual_connection`.
- Generic “fuller picture” rationales without distinct contributions fail
  adjudication.
- Citation matching failure does not prevent probabilistic candidate discovery.
- Citation never implies agreement.
- Forward/inverse labels and evidence ownership remain reciprocal.
- Batch sizes 8, 16, 24, and 30 produce complete decisions or explicit
  contract failures without silently dropping jobs.

### Clusters

- Every retained member receives a source-specific contribution.
- Full member notes are present in the writer packet.
- Controlled nulls and disagreements remain visible.
- Different statistical calculations are not conflated.
- Observational tension is not automatically labeled contradiction.
- Membership and related-cluster links reciprocate.
- Unclustered accounting is neutral and complete.

### Replay

- A second identical build makes zero provider calls.
- No duplicate relationship, `no_relationship`, membership, or cluster events.
- No timestamp-only changes.
- Generated bytes and mtimes remain identical.
- A new run ID reuses unchanged semantic jobs.
- Editing user-owned Markdown outside managed blocks causes zero provider calls.
- Changing one source invalidates only its source bundle, incident candidate
  work, incident relationship jobs, and affected clusters.

## 18. Acceptance evaluation

Run the same private Mediation and Conflict Relapse collections after freezing
v0.16.

### Source reliability and atomic notes

- At least 98% of readable sources produce a usable note from the one-shot call
  plus local envelope normalization.
- Critical-fact recall remains at least 95%.
- Substantive-claim support reaches at least 95%.
- Headline source-reported numbers and scales are preserved.
- Every analytical note contains source-specific evidence appropriate to its
  method.
- No gross wrong-source, empty, or unusable note is published.
- Isolated statistical or wording imperfections are reported but do not alone
  fail the entire release.

### Statistical explanation

- More paired explanations improve than worsen relative to v0.15.
- Raw-statistic accuracy does not regress.
- No systematic confusion among predicted and observed quantities, odds,
  hazards, risks, probabilities, or interaction regions.
- Isolated interpretation mistakes qualify the statistical section rather than
  blocking an otherwise useful mapping run.

### Relationships

- Explicit cross-folder citation recall remains 100%.
- Candidate-stage recall of the frozen 40 plausible bridge set reaches at least
  70%; these remain discovery candidates, not presumed final relationships.
- Final useful-bridge recall reaches at least 70%, counting correctly bounded
  contextual connections as useful rather than forcing them into direct types.
- Direct intellectual relationships reach at least 85% strict precision.
- Contextual relationships reach at least 80% navigational usefulness with a
  correct boundary against stronger claims.
- Every published relationship uses evidence from both atomic notes.
- Every visible link projects reciprocally.

### Clusters

- Retained membership relevance remains at least 90%.
- Every retained member has a specific contribution.
- Audited source claims reach at least 95% support.
- No systematic statistical or debate-boundary regression occurs.
- No cluster-coverage threshold is applied.

### Pipeline

- Zero pending or processing-partial items.
- Complete source, call, and projection accounting.
- No more than 100 literature calls; target no more than 25 for this corpus.
- Production runtime no more than two hours; target no more than 90 minutes.
- Unchanged replay makes zero provider calls and changes zero generated bytes or
  mtimes.

Any missed metric receives a source-linked qualified or failed section. The
evaluation must not weaken relationship-tier definitions, raise call ceilings,
or change prompts during the frozen run.

## 19. Explicit non-goals

v0.16 will not:

- create a desktop application;
- add accounts, billing, synchronization, or managed hosting;
- require a native coding agent;
- add a vector or graph database;
- add a second atomic, statistical, relationship, or cluster verification call;
- make deterministic code decide intellectual relationships;
- require every source to belong to a cluster;
- delete useful legacy broad links during migration;
- compare every Zotero collection pair;
- automatically acquire missing documents;
- edit Zotero metadata; or
- raise provider-call ceilings to conceal repeated work.
