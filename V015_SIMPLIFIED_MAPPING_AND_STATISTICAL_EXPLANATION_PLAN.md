# Auto-Zettelkasten v0.15 Simplified Mapping and Statistical Explanation Plan

**Status:** Implemented

**Date:** 2026-07-28

**Foundation:** Engine `0.14.0`, artifact schema `1.13`, relationship registry
schema `5`

**Evidence:** The private 195-source Mediation and Conflict Relapse evaluation,
the v0.14 Obsidian vault inspection, the post-evaluation relationship-context
audit, and the subsequent design review

## 1. Summary

v0.15 should preserve the strongest v0.14 results:

- accurate and detailed atomic notes;
- substantially improved full-note cluster synthesis;
- reciprocal Obsidian links;
- one global graph and cluster registry;
- broad source and cluster concurrency;
- additive Markdown projection; and
- zero-call unchanged replay.

It should make four focused improvements:

1. Improve the one-shot source prompt so statistically complex findings are
   explained accurately to non-specialists without removing the original
   statistics.
2. Carry those explanations into cluster synthesis.
3. Simplify normal-size literature mapping into one cached relationship-
   candidate call, bounded relationship adjudication, one cached cluster-plan
   call, and one concurrent writing call per cluster.
4. Treat an unclustered source as a temporary property of the current map, not
   a rejection, failure, or permanent `not_a_fit` classification.

The release must not add a statistical verifier, arithmetic checker, re-homing
workflow, or another model pass.

## 2. Decisions from the review

The following decisions are settled.

### 2.1 No cluster-coverage quota

Remove the 90% cluster-coverage acceptance threshold.

Some sources are legitimately isolated in the current library. A source may
become clusterable later as Zotero gains related material. Cluster coverage is
therefore descriptive, not an acceptance criterion.

Acceptance should instead require:

- complete accounting of substantive sources;
- coherent membership for sources that are clustered;
- no decorative or keyword-only membership;
- strong representation of obvious foundational studies in the debates they
  directly address;
- reciprocal atomic-to-cluster projection; and
- no source silently lost between planning, writing, and projection.

### 2.2 No permanent negative cluster state

Do not create `not_a_fit`, cluster-rejection memory, or any equivalent
source-level semantic status.

`unclustered_sources` is a computed snapshot:

```text
currently eligible substantive sources
minus
sources in at least one active cluster
```

It records only that a source has no active cluster membership in the current
map revision. It does not claim that the source cannot fit a future cluster.
Every future cluster-planning call may assign it normally.

Where the existing registry requires a reason code for compatibility, use the
neutral computed value `currently_unclustered`. Continue reading older reason
values, but do not treat them as rejection memory or feed them back as a
constraint on later planning.

### 2.3 No re-homing workflow

Do not add a second planning pass for sources omitted by cluster writers.

When a writer drops a proposed member:

- retain the completed cluster;
- recompute the current unclustered snapshot;
- preserve the source in the compact catalogue and collection indexes; and
- allow the next ordinary cluster-planning call to reconsider it.

This avoids extra API calls, cluster rewrites, and another failure-prone
workflow.

### 2.4 Model-led statistical interpretation

Improve statistical explanation in the same source-generation and
cluster-writing calls.

Do not:

- recalculate statistics in local code;
- add a deterministic arithmetic validator;
- trigger a retry because local code dislikes an interpretation;
- add a separate statistical-verification call; or
- switch the whole pipeline to a more expensive reasoning mode by default.

The model must preserve the original statistic, explain it, and silently
self-review its interpretation before returning.

## 3. Target architecture

The normal path for a catalogue that fits the planning context is:

```text
frozen source content
    ↓
parallel one-shot source bundles and atomic notes
    ↓
deterministic global and collection indexes
    ↓
one global relationship-candidate call
    ↓
parallel relationship adjudication with complete endpoint notes
    ↓
one global cluster-planning call using accepted relationships
    ├── cluster proposals and current memberships
    └── neighboring-cluster proposals
    ↓
parallel one-call cluster writing with complete member notes
    ↓
separate relationship and cluster registry commits
    ↓
additive reciprocal Markdown projection
```

The semantic dependency graph remains one-way:

```text
source content
    ↓
source bundle
    ↓
compact catalogue
    ↓
relationship-candidate plan
    ↓
relationship decisions
    ↓
cluster plan
    ↓
cluster syntheses
    ↓
registries
    ↓
Markdown and collection views
```

Generated graph, cluster, catalogue, or Markdown state must never invalidate a
source bundle or catalogue-level planning call that did not semantically depend
on it.

## 4. One-shot source generation

### 4.1 Reuse the existing source bundle

Do not introduce a new source-result schema.

The current code already provides:

- `SourceAnalysisBundle.analysis_sections`;
- `SourceAnalysisBundle.self_review`;
- `EvidenceAnchor.plain_english_meaning`;
- `EvidenceAnchor.quantitative_result`;
- typed `QuantitativeResult` fields for statistic, estimand, outcome, estimate,
  unit, scale, baseline, reference group, comparison group, denominator,
  sample, uncertainty, population, period, model, and provenance; and
- `source_reported`, `system_derived`, and `unknown` provenance.

v0.15 should populate and use these existing fields more consistently.

### 4.2 Correct the prompt conflict

The current prompt both asks for statistical explanation and broadly prohibits
calculating or converting any number. Replace that blanket prohibition with a
bounded interpretation rule:

- always preserve the source-reported statistic and its original scale;
- allow a simple derived explanation only when all required inputs are explicit
  in the source;
- label the explanation as derived rather than source-reported;
- never replace the source statistic with the derived explanation;
- do not invent a missing baseline, denominator, reference group, model
  quantity, or uncertainty measure; and
- state plainly when the available statistic cannot support an intuitive
  percentage conversion.

### 4.3 Statistical interpretation rules

The prompt must distinguish the following.

#### Percentages and percentage points

When a source reports two comparable proportions, explain both only when useful:

```text
40% to 31%
= 9 percentage points lower
= 22.5% lower relative to the 40% baseline
```

Never call the ending value the reduction. Always name the baseline.

#### Probabilities and odds

- An odds ratio describes odds, not probability.
- A logit coefficient is on a log-odds scale.
- Do not convert a coefficient into a probability change unless the source
  reports a marginal effect, predicted probability, or all information needed
  for the interpretation.
- If an odds ratio is source-reported, explain its direction and reference
  group without calling it a probability increase.

#### Interaction terms

- Do not interpret an interaction coefficient as an independent percentage
  change.
- Explain which combination of conditions the interaction concerns.
- Prefer source-reported predicted probabilities or marginal effects.
- If those are absent, explain direction and uncertainty and say that the
  coefficient alone does not yield an intuitive percentage.

#### Hazard ratios and risks

- A hazard ratio concerns the instantaneous event rate, not cumulative
  probability.
- Keep hazard, risk, odds, and probability distinct.
- Explain the event, time scale, reference group, and direction.

#### Regression coefficients

- Preserve the dependent variable, unit, model scale, and comparison.
- Explain the substantive direction and conditional nature of the estimate.
- Do not call a coefficient large or small without a meaningful reference.

#### P-values and confidence intervals

- A p-value is not the probability that a hypothesis is true.
- Statistical significance is not practical importance.
- Explain uncertainty in ordinary language without promising certainty.
- Preserve confidence-interval endpoints and the parameter they describe.

#### Association and causation

- Observational associations remain associations.
- Model predictions remain model predictions.
- Descriptive before-and-after comparisons remain descriptive.
- Author causal interpretations are attributed unless the design identifies
  the effect.

### 4.4 Plain-English Interpretation

The section must not be a second abstract or a fixed checklist.

For the two to four findings most important to understanding the source, it
should:

- retain the technical result;
- explain what a reader would observe in the relevant cases or population;
- identify the comparison and reference point;
- translate the scale when a defensible translation is possible;
- use an everyday analogy or concrete example only when it preserves the
  source's denominator and inferential scope;
- explain why an apparently large coefficient may not be an intuitive
  percentage;
- distinguish statistical from practical importance; and
- state the most important uncertainty or boundary.

For example, a logit interaction reported as `β=-15.962, p<.001` should not be
turned into a fabricated percentage decline. A faithful explanation should say
that the combination is estimated to be strongly negatively associated with
implementation, that the result is statistically distinguishable under the
model, and that predicted probabilities are required for an intuitive
percentage effect.

### 4.5 Same-call self-review

Use the existing `self_review` object. Require the source reasoner to silently
check, in the same call:

- source-reported numbers were preserved;
- percentage points and relative percentages were not confused;
- odds, hazards, risks, and probabilities were not conflated;
- coefficients and interactions were not converted without the required
  information;
- p-values were not presented as effect sizes or truth probabilities;
- statistical significance was not substituted for practical importance; and
- causal language matches the research design.

The self-review is provenance and prompt discipline. It is not a second model
call and is not a local publication gate.

## 5. Compact catalogue and indexes

Keep the v0.14 deterministic catalogue and collection/subcollection indexes.

Each compact source entry remains bounded to:

- source ID and Zotero key;
- title, author, and year;
- thesis;
- method or knowledge basis;
- scope;
- a few controlled facets;
- important matched literature positions; and
- current cluster IDs when already assigned.

Do not place full atomic notes in the catalogue.

The human-facing index should provide:

- collection and subcollection navigation;
- cluster navigation;
- a neutral `Currently unclustered` view; and
- unresolved important cited sources for future library expansion.

The unclustered view is generated from current membership. It is not negative
memory and has no effect on future eligibility.

## 6. Two catalogue-level planning calls

### 6.1 Normal-size corpus

When the compact catalogue fits the measured planning context, use:

1. one global relationship-candidate selection call;
2. bounded relationship adjudication packets; and
3. one global cluster-planning call that receives the accepted relationships.

Keep these as two planning calls. The current pipeline already caches both
stages, and cluster planning benefits from seeing adjudicated relationships.
Combining them would save only one provider call while either depriving cluster
planning of accepted relationships or requiring a new precomputed-plan handoff
and cache design.

The relationship-candidate call returns:

- inferred candidate pairs;
- cross-collection bridge candidates; and
- a bounded reason for examining each pair.

The cluster-plan call returns:

- proposed cluster families;
- proposed memberships;
- organizing mode and organizing problem;
- optional guiding question or central tension;
- cluster-neighbor proposals;
- and source-specific reasons for proposed membership.

The planner may leave any number of sources unclustered. It must not create
weak clusters to maximize coverage.

Stop requiring the planner to enumerate or explain unclustered sources. Local
code derives the neutral current snapshot from eligible sources minus active
memberships. Keep accepting the legacy response field from custom reasoners,
but do not require it from built-in reasoners or treat its explanation as a
durable semantic judgment.

### 6.2 Large-library fallback

Reuse the existing deterministic collection indexes, routing cards, measured
context packing, shard planning, and family-card reconciliation only when the
global catalogue does not fit.

Do not invoke shard selection for a catalogue that fits.

For an oversized library:

1. Route through collection and subcollection indexes.
2. Split only oversized indexes into bounded packets.
3. Plan independent packets concurrently.
4. Return compact candidate sets or cluster-family cards for the relevant
   stage.
5. Reconcile only compact results, not full source profiles.
6. Preserve unassigned sources in their collection indexes for future plans.

This fallback may use multiple calls because the context limit requires it. It
must not complicate the ordinary one-call path.

### 6.3 Plan identity and replay

The relationship-candidate fingerprint contains only:

- sorted compact source-profile hashes;
- explicit Zotero relations;
- relationship-candidate prompt, model, provider, and policy identity; and
- the selected catalogue scope or shard identities.

The cluster-plan fingerprint contains only:

- sorted compact source-profile hashes;
- hashes of accepted relationships supplied to planning;
- human-authored constraints;
- cluster-planning prompt, model, provider, and policy identity; and
- the selected planning scope or shard identities.

Both exclude run ID, Markdown, registry counters, provider-usage ledgers,
cluster projections, and their own generated output.

Unchanged candidate selection and cluster planning replay with zero provider
calls.

## 7. Relationship adjudication

### 7.1 Candidate selection

The existing global relationship-candidate call proposes inferred candidates.
Explicit citations, Zotero relations, and matched literature positions remain
mandatory candidates when their endpoints are available.

Candidate generation uses the compact catalogue. It does not load every atomic
note.

### 7.2 Full-note decision packets

Keep the post-v0.14 fix that resolves atomic notes through the canonical
workspace note index.

Every inferred pair job receives:

- both complete semantic atomic-note bodies;
- both compact profiles;
- relevant evidence anchors;
- relevant literature-position records;
- explicit citation or Zotero-relation context;
- bounded existing-neighbor context; and
- the planner's candidate rationale.

Managed graph and literature projection blocks are stripped from the Markdown.
Literature-position records are supplied separately.

The model decides:

- relationship or no relationship;
- proposition being compared;
- type;
- direction;
- boundary or qualification;
- source-grounded rationale; and
- confidence.

The model never edits Markdown. Local code validates identifiers and projects
reciprocal links.

### 7.3 Adaptive batching

Replace the fixed six-pair transport assumption with measured context packing
using the existing context estimator.

- Pack as many complete pair jobs as safely fit.
- Preserve each immutable pair-job identity.
- Cap packet size conservatively during initial benchmarking.
- A transport batch never changes semantic cache identity.
- A failed packet does not invalidate successful packets.

The current provider contract permits at most eight pair jobs per request.
Benchmark six, eight, and an adaptive measured cap no greater than eight.
Select the smallest call count that does not reduce relationship precision.

## 8. Cluster writing

### 8.1 One call per cluster

Each planned cluster receives exactly one semantic writing call unless the
transport request is genuinely interrupted.

The writer receives:

- every proposed member's complete semantic atomic note;
- source-owned evidence anchors and quantitative results;
- accepted relationships among proposed members;
- the compact organizing card; and
- scope warnings for partial documents.

All independent cluster calls run concurrently up to provider concurrency.

### 8.2 Statistical explanations in clusters

Update the cluster-writing prompt so the cluster explains the important
statistics it chooses to display.

For each central quantitative finding:

- preserve the source statistic;
- explain the comparison and scale;
- distinguish percentage points from relative change;
- distinguish odds, hazards, risks, and probabilities;
- explain interactions only to the level supported by the source;
- include null and uncertain results;
- avoid treating statistical significance as practical importance; and
- use predicted probabilities or marginal effects when the atomic note reports
  them.

The cluster should not reproduce every coefficient. It should select the
statistics needed to understand the literature's substantive conclusions and
explain those clearly.

### 8.3 Writer omissions

A writer may omit a proposed source when it cannot give that source a specific,
cluster-relevant contribution.

Do not ask it to label the source `not_a_fit`.

After all writers complete, local code recomputes:

- active memberships;
- currently unclustered source IDs; and
- complete membership accounting.

No re-homing call follows.

## 9. Deterministic responsibilities

Local code remains responsible for:

- schemas and required fields;
- real source, note, anchor, relationship, and cluster IDs;
- immutable fingerprints;
- context measurement and packet packing;
- cumulative provider-call accounting;
- concurrency safety;
- additive Markdown ownership;
- reciprocal graph projection;
- current cluster-membership accounting;
- checkpoint reuse; and
- byte-stable unchanged replay.

Local code does not:

- interpret a coefficient;
- recalculate a percentage;
- determine intellectual relevance;
- choose a relationship type;
- force a source into a cluster;
- reject a model explanation because of a heuristic warning; or
- trigger a semantic retry for a statistical interpretation.

## 10. Calls, concurrency, and retries

### 10.1 Clean normal-path formula

For a catalogue that fits one planning call:

```text
literature calls
= 1 relationship-candidate call
+ relationship transport packets
+ 1 cluster-plan call
+ accepted cluster count
```

For the previous 15-cluster, 96-relationship corpus, adaptive relationship
packing should make a clean run approximately 29-33 literature calls, subject
to quality benchmarking.

This is a performance target, not a reason to combine unrelated work into
oversized prompts.

### 10.2 Retry policy

- No model-quality retries.
- One transport retry only when a request was genuinely interrupted.
- Locally recoverable JSON envelopes are normalized without a provider call.
- Completed source, relationship, and cluster jobs are reused across run IDs.
- A prompt or semantic input change invalidates only the jobs that depend on it.

### 10.3 Call ceiling

Keep one cumulative literature-call ceiling.

For the eventual 195-source acceptance run:

- retain a hard maximum of 100 literature calls;
- report clean-run calls separately from implementation and audit calls;
- never raise the ceiling automatically; and
- stop before rerunning changed relationship jobs if the remaining allowance is
  insufficient.

Source-generation calls remain separately accounted because a new source prompt
may require regenerating atomic notes.

## 11. Versioning and compatibility

Release as engine `0.15.0`.

Do not change the artifact schema or relationship registry schema unless
implementation reveals a real persisted-shape incompatibility. The existing
schema already supports the required quantitative and plain-English fields.

Planned prompt identities:

- atomic-note prompt `v10`;
- source-bundle prompt `v4`;
- cluster-plan prompt `v5`;
- relationship prompt retains its current decision contract unless the prompt
  shape changes; and
- cluster-synthesis prompt `v25`.

Migration is local and idempotent:

- no provider calls;
- no source rereads;
- no automatic rewriting of existing atomic or cluster notes;
- no permanent conversion of old unclustered records into negative memory; and
- existing v0.14 registries remain readable.

Changing the source prompt deliberately changes source semantic identity.
Implement and test the prompt on a bounded quantitative sample before
authorizing a full 195-source refresh.

## 12. Implementation sequence

### Phase A — Prompt and source-bundle improvements

1. Update the atomic-note and source-bundle prompts.
2. Remove the conflicting blanket ban on all derived explanations.
3. Add the bounded statistical-interpretation rules.
4. Require same-call statistical self-review.
5. Populate existing `plain_english_meaning` and `QuantitativeResult` fields.
6. Keep legacy custom readers backward-compatible.

Success: a one-shot source call preserves technical statistics and explains
their practical meaning without an extra call.

### Phase B — Cluster statistical explanation

1. Update the full-note cluster-writing prompt.
2. Require clear explanations for selected central statistics.
3. Keep one call per cluster.
4. Retain complete member atomic notes and evidence anchors.

Success: cluster findings are statistically intelligible without becoming
longer lists of unexplained coefficients.

### Phase C — Lean two-stage planning

1. Retain the existing relationship-candidate call before adjudication.
2. Retain the existing cluster-plan call after accepted relationships exist.
3. Use one whole-catalogue call for each stage when its context fits.
4. Skip shard planning when the catalogue fits.
5. Preserve the existing measured large-library fallback.
6. Reuse the existing semantic and pair-job caches rather than adding a new
   plan-handoff cache.

Success: the 195-source corpus uses one candidate-selection call and one
cluster-planning call, with no redundant planning pass.

### Phase D — Dynamic unclustered projection

1. Remove any acceptance logic that treats cluster percentage as success.
2. Derive current unclustered sources from active membership.
3. Replace the permanent-sounding default reason
   `no_admitted_thematic_cluster` with `currently_unclustered`.
4. Keep older reason values readable without using them as rejection memory.
5. Do not persist `not_a_fit` or rejection memory.
6. Stop requiring a model-generated reason for non-membership.
7. Ensure a later cluster-planning call may assign any previously unclustered
   source.
8. Keep accounting exact after writer omissions.

Success: unclustered sources are visible and recoverable without another
workflow.

### Phase E — Adaptive relationship packets

1. Retain full semantic atomic notes in every pair job.
2. Pack pair jobs by measured context rather than a fixed count.
3. Benchmark packet sizes against relationship precision.
4. Preserve pair-level checkpoints and call accounting.

Success: fewer transport calls without reducing semantic quality.

### Phase F — Evaluation

1. Run prompt fixtures locally.
2. Run a bounded paid quantitative-source comparison.
3. Audit the corrected full-note relationship pathway.
4. Only then authorize a complete 195-source refresh.

## 13. Test plan

### 13.1 Atomic statistical fixtures

Add fixtures covering:

- `40%` to `31%` as nine percentage points and 22.5% relative decline;
- percentage increase versus percentage-point increase;
- a logit coefficient with no reported marginal effect;
- a source-reported odds ratio;
- a hazard ratio that must not become cumulative probability;
- an interaction coefficient without predicted probabilities;
- a p-value that must not become the probability a hypothesis is true;
- a confidence interval;
- a statistically significant but substantively small estimate;
- a null result;
- an observational association;
- a source-reported predicted probability;
- a descriptive before-and-after comparison; and
- a qualitative or normative source where statistical interpretation is not
  applicable.

Local tests should verify the prompt contract, response parsing, field
preservation, and rendering for fixture responses. They do not prove that a
live probabilistic model understands the statistics; the bounded paid
evaluation in Section 14 does that.

Fixture responses must demonstrate:

- preservation of the raw statistic;
- a non-boilerplate plain-English explanation;
- no unsupported percentage;
- correct scale terminology;
- correct reference group or baseline when reported;
- preserved uncertainty; and
- no causal upgrade.

### 13.2 Cluster statistical fixtures

Require the cluster writer to:

- explain a central percentage comparison correctly;
- distinguish odds from probability;
- explain why an interaction cannot be converted from its coefficient alone;
- preserve null findings;
- compare only commensurable estimates;
- retain every displayed source statistic in its original scale; and
- avoid flooding the cluster with peripheral coefficients.

### 13.3 Planning and unclustered tests

- A fitting catalogue makes one relationship-candidate call.
- A fitting catalogue makes one cluster-planning call after adjudication.
- The candidate response returns relationship candidates.
- The cluster plan receives accepted relationships and returns clusters.
- An oversized catalogue uses the existing hierarchical fallback.
- No cluster-coverage threshold affects verdict or status.
- A source without membership appears in the current unclustered snapshot.
- No `not_a_fit` or equivalent negative decision is persisted.
- A later plan may assign a previously unclustered source.
- Writer omissions reconcile exactly with final membership accounting.
- No re-homing provider call occurs.

### 13.4 Relationship tests

- Every pair job contains both complete semantic atomic-note bodies.
- Managed graph and literature blocks are absent from pair Markdown.
- Literature positions are supplied separately.
- Adaptive batches remain within measured context.
- Pair identity is independent of transport batch.
- Six-pair and eight-pair safe packets preserve identical pair-job inputs and
  parse to equivalent fixture decisions.
- Reciprocal projections remain exact and additive.

### 13.5 Replay and call tests

- One relationship-candidate call and one cluster-plan call for the
  195-source-size fixture.
- One successful writing call per accepted cluster.
- No semantic retry after a valid response.
- A new run ID reuses unchanged jobs.
- Editing frontmatter or graph projection causes zero provider calls.
- Changing one source invalidates only that source, the catalogue-level
  candidate and cluster plans, incident relationship decisions, and affected
  cluster syntheses.
- An unchanged replay makes zero calls and no generated-file writes.

Run the complete existing suite and build the package.

## 14. Evaluation plan

### 14.1 Bounded statistical gate

Before refreshing the full corpus, select a deterministic sample of at least 20
quantitatively substantive sources spanning:

- percentages and proportions;
- logit or probit models;
- odds and hazards;
- interactions;
- predicted probabilities or marginal effects;
- confidence intervals;
- null results; and
- observational causal-language risk.

Compare v0.14 and v0.15 atomic notes directly against frozen source text.

Pass when:

- all audited headline statistics preserve their source-reported values;
- no percentage-point/relative-percentage confusion remains;
- no odds, hazard, risk, or probability conflation remains;
- no unsupported conversion is invented;
- plain-English explanations materially clarify the important statistics; and
- no new causal upgrades are introduced.

If this bounded gate fails, improve the prompt before paying to refresh all 195
sources.

### 14.2 Cluster comparison

Audit:

- every mixed-literature cluster;
- the eight largest remaining clusters;
- Civil War Duration and Termination material; and
- clusters containing the statistical fixtures.

Evaluate:

- membership relevance;
- representation of obvious core studies;
- source-specific findings;
- claim support;
- plain-English statistical explanation;
- debate and boundary accuracy;
- reciprocal links; and
- current unclustered accounting.

Report cluster coverage descriptively. Do not use it as a pass/fail threshold.

### 14.3 Relationship comparison

Run the corrected full-note adjudication path with a fresh approved allowance.

Retain:

- 100% explicit-link recall;
- at least 85% inferred-link precision;
- at least 70% curated-bridge recall;
- source-grounded visible reasons; and
- exact reciprocal projection.

The relationship evaluation must state that v0.14's measured relationship
quality was produced before the full-note routing fix.

### 14.4 Runtime and calls

Measure separately:

- Zotero inventory and extraction;
- source-generation wall time and peak concurrency;
- relationship-candidate and cluster-planning calls and wall time;
- relationship packet calls and wall time;
- cluster-writing calls and wall time;
- projection time;
- evaluation/audit time; and
- unchanged replay.

Do not mix implementation-time superseded calls into the clean production
estimate.

## 15. Acceptance criteria

### Atomic notes

- Strong thesis, method or knowledge basis, findings, limitations, and
  literature-position coverage are preserved.
- All audited headline statistics retain their original values and scales.
- No audited percentage-point, relative-percentage, odds, hazard, risk, or
  probability confusion.
- No invented percentage conversion.
- No unsupported causal upgrade.
- Plain-English interpretation explains rather than merely repeats.

### Clusters

- At least 90% relevance among retained memberships.
- At least 95% support for audited substantive claims.
- Every retained member has a specific cluster-relevant contribution.
- No fabricated debate, contradiction, consensus, or statistical comparison.
- Important statistical findings are understandable to a non-specialist.
- No cluster-coverage percentage requirement.
- Every substantive source is accounted for as clustered or currently
  unclustered.

### Relationships

- 100% explicit-link recall.
- At least 85% inferred-link precision.
- At least 70% curated-bridge recall.
- Every visible rationale is grounded in both endpoint notes.
- Reciprocal projection is complete.

### Pipeline

- No pending source or literature jobs.
- One relationship-candidate call and one cluster-planning call when the
  compact catalogue fits.
- One successful writing call per accepted cluster.
- No re-homing workflow.
- No statistical verifier or arithmetic-check call.
- No semantic-quality retries.
- Complete cumulative call accounting.
- Zero-call, byte-stable unchanged replay.

## 16. Non-goals

v0.15 will not:

- force every source into a cluster;
- classify a source permanently as unsuitable for clustering;
- add a source re-homing stage;
- add local statistical arithmetic or interpretation rules;
- add a statistical verifier;
- rewrite Markdown with a model;
- make locators a publication gate;
- replace DeepSeek solely because of one arithmetic wording error;
- add a graph or vector database;
- require a coding harness in production;
- implement weekly Zotero monitoring yet;
- edit Zotero metadata; or
- weaken relationship precision to increase link count.

## 17. Deliverable

After implementation and approved evaluation, write:

`evaluation/v015-comparison.md`

The report must distinguish:

- atomic-note quality;
- statistical-explanation quality;
- cluster coherence;
- currently unclustered sources;
- relationship precision and recall;
- clean production calls;
- implementation and audit calls;
- provider wall time;
- audit wall time; and
- measured results from architectural fixes that were implemented after the
  previous frozen evaluation.

The final verdict must not fail merely because some sources are currently
unclustered.
