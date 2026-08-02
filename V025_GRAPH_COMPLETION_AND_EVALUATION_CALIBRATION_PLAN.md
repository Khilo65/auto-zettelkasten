# Auto-Zettelkasten v0.25 Graph Completion and Evaluation Calibration Plan

**Status:** Implemented and evaluated; material relationship semantic gates failed

**Date:** 2026-08-02

**Foundation:** Engine `0.24.0`, artifact schema `1.17`, source prompt `5`,
source catalogue schema `6`, relationship discovery prompt `15`, relationship
adjudication prompt `13`, relationship registry schema `7`, relationship
decision contract `relationship-decision-v8`, literature-family prompt `8`,
cluster synthesis prompt `32`, cluster contract `streamlined-full-note-v2`, and
the implementation through commit `b9a5cb8`.

**Primary evidence:**

- the v0.24 evaluation at
  `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v024-targeted-graph-acquisition-evaluation-20260801/evaluation/v024-graph-acquisition-comparison.md`;
- the post-fix v0.24 graph workspace at
  `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v024-postfix-graph-evaluation-20260802`;
- the v0.16–v0.24 comparative evaluations;
- manual review of v0.24 relationship and cluster outputs; and
- the decisions reached after v0.24 about relationship-label severity,
  acquisition visibility, omitted jobs, call reservations, duplicate Zotero
  works, and advisory evidence anchors.

## 1. Summary

V0.25 is a completion and calibration release, not another architecture
rewrite. Atomic notes, profile generation, global-index discovery, family
planning, relationship persistence, full-note cluster synthesis, and
acquisition accounting already work and will be preserved.

The release will:

1. make relationship classification rules clearer in the existing
   adjudication call without adding a verifier;
2. reduce relationship packets to at most fifteen jobs so omitted rows become
   less likely without adding another retry workflow;
3. replace the coarse cluster-call reservation with an exact four-call reserve
   that is released when no retry needs it;
4. render both priority and relevant-secondary cited works from the existing
   acquisition ledger;
5. reuse existing identity reconciliation to suppress duplicate works before
   discovery and cluster synthesis while preserving Zotero aliases;
6. treat relationship anchor IDs as optional traceability metadata, never as a
   publication or release gate;
7. repair the local coverage, manifest, cluster-accounting, and revision-join
   inconsistencies found in v0.24; and
8. recalibrate evaluation so useful links with debatable labels, approximate
   locators, and missing optional anchors are advisory issues rather than
   reasons to call the release a qualified failure.

Release identities:

- Engine: `0.25.0`
- Artifact schema: `1.18`
- Source catalogue schema: `7`
- Relationship registry schema: unchanged at `7`
- Source prompt: unchanged at `5`
- Relationship discovery prompt: unchanged at `15`
- Relationship adjudication prompt: `14`
- Literature-family planning prompt: unchanged at `8`
- Cluster synthesis prompt: `33`
- Relationship decision contract: unchanged at `relationship-decision-v8`
- Cluster contract: unchanged at `streamlined-full-note-v2`
- Acquisition ledger schema: unchanged at `1`

No new dependency, public API, CLI option, source-generation call, profile
regeneration, verifier call, Zotero mutation, web-search workflow, or model
editing of Markdown will be added.

## 2. Results to preserve

V0.25 must preserve the successful v0.24 behavior:

- 193 frozen notes with zero atomic-note semantic changes;
- 14/14 bridge-family coverage;
- 26/30 blind discovery candidates worth full-note examination;
- 120/120 candidate dispositions with zero deterministic loss;
- one active substantive decision per source pair;
- reciprocal projection of every visible relationship;
- last-good relationship visibility during prompt reconciliation;
- 178/178 relevant audited cluster memberships;
- 345/345 supported source-specific cluster contributions;
- 127/127 accurate audited debate and boundary statements;
- 1,053/1,053 acquisition candidates accounted;
- 212/212 precise and correctly attributed visible acquisition
  recommendations;
- isolated provider and cluster failures rather than whole-map collapse;
- a cumulative literature-call ceiling; and
- zero-call, zero-write identical replay.

The v0.24 graph-retention correction in `b9a5cb8` is part of the baseline. A
prompt change may mark a last-good relationship `reconciliation_pending`, but
must not hide it until the same pair receives a newer accepted or
`no_relationship` decision.

## 3. Evaluation policy: material failures versus advisory imperfections

The previous evaluation rubric treated every missed numerical target as a
qualified failure. That no longer matches the intended use of the system.
V0.25 will report three levels.

### 3.1 Release-blocking failures

The following remain failures:

- invented or materially false endpoint claims;
- a rationale that attributes a finding to the wrong source;
- a reversed direction that changes the intellectual meaning;
- a completely superficial relationship presented as substantive;
- fabricated cluster findings, debates, consensus, or quantitative
  comparisons;
- missing or nonreciprocal visible graph projections;
- duplicate active machine decisions for the same canonical pair;
- silent candidate, pair-job, cluster, or acquisition-candidate loss;
- unfinished eligible work while provider-call capacity and deadline remain;
- an undiagnosable provider failure;
- duplicate canonical works being treated as independent evidence; or
- a replay that makes calls or rewrites unchanged artifacts.

### 3.2 Qualified but usable results

A section may be qualified without failing the release when a bounded minority
of links are useful and source-grounded but their exact taxonomy is debatable,
or when the hard call ceiling is genuinely exhausted before every optional
small cluster can run. The report must state the practical effect and show
examples.

### 3.3 Advisory findings

The following are advisory and cannot independently produce a qualified-fail
verdict:

- `supports` versus `complements` versus `contextual_connection` when the
  proposition and rationale remain useful and accurate;
- missing optional relationship evidence-anchor IDs;
- approximate or mildly inaccurate locators;
- stylistic imperfections;
- a relevant work appearing under priority rather than secondary acquisition,
  or vice versa;
- one localized causal verb when surrounding prose accurately states the
  observational limitation; and
- exact-pair benchmark misses when family coverage and blind candidate quality
  pass.

Exact relationship-label accuracy, exact-40 bridge overlap, anchor completeness,
and locator precision remain reported diagnostics. They are not release gates.

## 4. Implementation changes

### 4.1 Clarify relationship judgment in the existing full-note call

Advance only the relationship adjudication prompt to `14`. Continue giving
DeepSeek both complete atomic notes, compact profiles, citation direction,
publication years, selected source evidence, and existing pair memory.

Replace overlapping relationship instructions with one compact decision
hierarchy:

1. `supports`: both works address the same sufficiently specific proposition
   and reach compatible conclusions.
2. `undermines`: both address a comparable proposition and one supplies a
   materially incompatible result or argument.
3. `qualifies`: one establishes a meaningful condition, scope boundary, or
   exception to the other's proposition.
4. `extends`: one explicitly builds on, applies, tests, refines, or generalizes
   the other.
5. `complements`: both make distinct contributions to the same specific
   question, proposition, mechanism, or outcome.
6. `contextual_connection`: joint reading is useful, but the works concern
   adjacent mechanisms, outcomes, stages, cases, methods, or scopes.
7. `no_relationship`: the overlap is only a broad subject or generic outcome.

Retain the specialized types only when the notes clearly justify them:

- `contrasts`: comparable propositions differ, but neither directly refutes
  the other;
- `rival_explanation`: the same explanandum receives genuinely competing
  explanations;
- `boundary_contrast`: the same proposition changes across a meaningful case,
  population, period, or scope boundary;
- `methodological_fault_line`: different designs or measurements materially
  change how the same question is answered;
- `sequential_relationship`: the works explain different stages of the same
  process; and
- `interpretive_or_normative_disagreement`: the sources explicitly disagree
  about interpretation or prescription rather than empirical effect.

Add four concise operating rules:

- Shared words, citations, chronology, datasets, methods, or broad outcomes do
  not alone establish a direct intellectual relationship.
- When two accurate source bases do not support one direct proposition, prefer
  `contextual_connection` rather than inventing a bridge mechanism.
- Direction follows the intellectual action, not pair order. Citation alone
  remains a separate `cites`/`cited_by` edge.
- Before returning, check that proposition, type, actor, reference, source A
  basis, source B basis, rationale, and boundary describe one coherent
  relationship.

Use DeepSeek's maximum reasoning setting for this existing call. Do not add an
independent verifier, correction call, local semantic classifier, or model
rewrite of either atomic note.

The existing `source_a_basis` and `source_b_basis` fields remain required and
must faithfully describe the corresponding atomic note. Evidence-anchor ID
arrays remain optional. When DeepSeek supplies valid IDs, retain them. Empty or
invalid optional IDs are dropped with an advisory warning; they cannot park or
downgrade an otherwise valid relationship.

### 4.2 Make relationship packets smaller without another retry workflow

Reuse `_relationship_transport_context` so every atomic note appears only once
per request and pair jobs refer to source IDs.

Change the existing `_RELATIONSHIP_BATCH_MAX_JOBS` from 30 to 15 and retain the
existing context-measured packer:

- maximum: fifteen pair jobs;
- reduce automatically for unusually long shared notes;
- preserve stable pair ordering and context hashing; and
- run independent packets concurrently under the existing provider worker
  limit.

After validating a batch, compare returned decision keys with the supplied
`pair_job_id` set. Completed rows are committed immediately. Any omitted job is
precisely parked as `provider_batch_missing_pair_row`, with the original packet
and provider response preserved. Do not resubmit completed or omitted jobs in
the same run. Malformed, wrong-source, omitted, or substantively invalid
decisions receive no paid repair call.

### 4.3 Finish every feasible cluster and release unused retry capacity

Replace the current percentage-based `_schedule_cluster_writers` reserve with
two bounded waves.

Initial wave:

- reserve exactly four provider attempts for `ProviderEmptyResponse` retries;
- launch at most fifteen highest-ranked eligible cluster writers;
- run independent writers concurrently; and
- keep every unscheduled proposal in explicit pending state.

Completion wave:

- wait for the initial writers and any immediate empty-response retries;
- recompute the actual remaining call capacity and literature deadline;
- release every unused retry slot;
- schedule the highest-ranked remaining eligible clusters through the same
  writer one at a time, so an empty-response retry always gets the next
  available slot; and
- continue until no eligible cluster remains, the cumulative ceiling is
  reached, or the deadline is genuinely exhausted.

The scheduler must never finish with `cluster_synthesis_deferred_budget` while
unused provider calls and sufficient deadline remain. A remaining deferral is
valid only when the run has actually reached its hard call ceiling, reached its
deadline, or the individual packet cannot fit the model context. Record these
reasons separately.

Provider-empty responses retain the v0.24 rule: one exact immutable retry, at
most once per job. Non-empty semantic or contract failures receive no retry.
One failed writer remains isolated from every other cluster.

### 4.4 Render both priority and secondary cited works

Do not add another provider call or a second recommendation-generation
contract. Reuse the existing complete
`important_unmapped_literature` input, cluster-writer dispositions, and
`cluster_acquisition_ledger.yml`.

Clarify cluster prompt `33`:

- `recommend` means the cited work is a priority addition that would materially
  improve the cluster's central synthesis, evidence base, debate, or boundary;
- `relevant_secondary` means it is genuinely useful for understanding or
  expanding the cluster but is not among the first works to map; and
- `not_relevant_to_cluster` means it should remain machine-only for this
  cluster.

Project two source-grouped sections locally:

```markdown
## Priority works to map

### Author, year — Title
- Why it matters to this cluster.
- Cited by: [[Mapped atomic note]].
- Action: Already in Zotero—map an atomic note.  # or Acquire or add

## Additional cited works worth mapping

### Author, year — Title
- Relevant because: the existing member's literature-position characterization.
- Cited by: [[Mapped atomic note]].
- Action: Already in Zotero—map an atomic note.  # or Acquire or add
```

Use the writer's `why_it_matters` for priority recommendations. For secondary
works, render the existing member-owned attribution and characterization; do
not ask DeepSeek for redundant prose. Group identical works once per cluster
and list all citing members beneath them.

Keep invalid, omitted, conflicting, dropped-member, and writer-failure states
machine-only. Preserve last-good visible recommendations when refresh fails.
Retire either visible tier when identity reconciliation shows that the work has
since been mapped.

### 4.5 Reconcile duplicate works before graph and cluster reasoning

Reuse the existing `_inventory_work_identity`, `_source_match_index`,
`_canonical_inventory_plan`, and duplicate-alias rules. Do not create a second
identity engine.

Extend catalogue construction to reconcile already-mapped legacy notes before
model-facing graph inputs are assembled. A duplicate group may be formed only
from strong deterministic evidence:

- the same Zotero key;
- the same normalized DOI with compatible title;
- the same normalized ISBN with compatible title;
- the same exact normalized stable URL;
- the same explicit `owl:sameAs`, ORA UUID, or equivalent strong external
  identifier;
- the same exact document hash when available;
- an explicit same-work relation; or
- an exact normalized title, complete author-surname set, year, and compatible
  item type when the match is unique.

Do not merge on partial title, author alone, year alone, topical similarity,
embedding similarity, or probabilistic model judgment. Ambiguous matches remain
separate and enter the existing
`01_custody/zotero/zotero_metadata_issues.yml` ledger.

Source catalogue schema `7` adds a small identity projection:

- `canonical_source_id` on every entry;
- `alias_source_ids` on the canonical entry;
- `canonical_work_count` and `duplicate_alias_count`; and
- the strong identifier and rule that established each alias.

For a confirmed duplicate:

- retain both Zotero keys and both existing atomic-note files;
- select one canonical source using the existing deterministic richness/rank
  rule;
- merge collection membership and citation aliases onto the canonical source;
- expose only the canonical source to discovery, relationship adjudication,
  cluster planning, and cluster synthesis;
- never count both aliases as independent evidence or cluster members;
- project an `alias_of` navigation relation between existing notes when both
  remain present; and
- append a reviewable duplicate issue to the existing
  `01_custody/zotero/zotero_metadata_issues.yml` without modifying Zotero.

Identity-only reconciliation makes no provider call and must not rewrite
atomic prose. A future Zotero merge or deletion should converge onto the same
canonical work without breaking graph links.

### 4.6 Repair local accounting from one authoritative source-status projection

Create no new ledger. Derive all counts and memberships from the same existing
terminal note-status and canonical-identity projection.

Repair the v0.24 inconsistencies so that:

- when a normalized profile exists, validated versus limited status comes from
  the profile's analytical state; terminal source-set rows are used only for
  missing, parked, pending, or legacy records without a profile;
- stored source records, canonical works, analytical works, limited notes, and
  duplicate aliases are reported separately;
- `coverage_register.yml` agrees with the authoritative run progress and note
  statuses;
- every canonical analytical work appears in exactly one of clustered,
  currently unclustered, pending-cluster, or retired state;
- every normalized non-analytical source, including the Brahimi Report when its
  bibliographic year conflicts with the dated evidence, appears in the
  excluded/limited accounting with its precise reason rather than being forced
  into the analytical unclustered list;
- family-plan source IDs are validated, and a mistyped or unknown ID is warned
  and omitted rather than silently replacing a real source;
- `attempted_route` is populated from the existing run route/checkpoint
  evidence when available and otherwise explicitly says `not_recorded_legacy`;
- workspace manifests always identify the current workspace rather than a
  copied historical path; and
- acquisition-ledger cluster revision IDs use the same final semantic revision
  key as active cluster-registry rows. Candidate-input hashes remain tied to
  the pre-call input, while failed and pending attempts retain their attempted
  revision.

Write files only when their serialized semantic bytes change. These repairs
must be local, idempotent, and provider-free.

### 4.7 Preserve provider observability without expanding the contract

Keep all v0.24 empty-response diagnostics and raw-response preservation.

When the existing DeepSeek response object already carries completion metadata,
copy its response ID, usage, finish reason, model, output ceiling, fragment
counts, byte counts, and stable hashes into the successful provider-attempt
ledger row as well. Do not preserve private reasoning text. Do not add another
request merely to obtain usage metadata.

## 5. Compatibility and migration

Migration is local, lazy, and idempotent:

- accept v0.24/schema-1.17 workspaces;
- update the source catalogue identity projection when the catalogue is next
  built;
- preserve existing atomic notes, relationships, clusters, acquisition
  dispositions, and human-authored content;
- keep prompt-13 machine relationships visible as
  `reconciliation_pending` until their pair is actually adjudicated under
  prompt 14;
- accept legacy custom reasoners through the existing contract and capability
  detection;
- render legacy priority recommendations as before and expose secondary rows
  only when the ledger contains a valid `relevant_secondary` disposition; and
- make no Zotero, provider, cloud, or web call during migration.

No migration will automatically delete duplicate note files or rewrite user
Markdown outside managed blocks.

## 6. Tests

### 6.1 Relationship prompt and packet completion

Add focused tests proving:

- same specific proposition plus compatible findings may remain `supports` or
  `complements`;
- same broad outcome with different mechanisms defaults to contextual;
- compatible mechanisms are not forced into `rival_explanation`;
- different stages or objects do not become direct complements;
- explicit lineage still permits `extends`;
- citation direction remains independent from intellectual direction;
- actor/reference and reciprocal labels remain correct;
- missing optional evidence-anchor IDs do not park a valid relationship;
- invalid supplied anchor IDs are dropped with an advisory warning;
- 31 short jobs pack as 15, 15, and 1;
- long shared notes reduce packet size automatically;
- each source document appears once per request;
- completed rows from partial batches are committed once;
- omitted jobs are precisely parked without another call;
- completed jobs are never reprocessed because another row was omitted; and
- replay does not rerun completed or terminally parked jobs.

Include the representative v0.24 failure fixtures: Mattes/Savun–Duursma,
Mukherjee–Walter, Nilsson–Rothchild/Groth, governance–Quinn/Mason, and
justice–Walter.

### 6.2 Cluster scheduling

Add tests proving:

- the initial wave reserves exactly four calls;
- the initial writer wave never exceeds fifteen jobs;
- empty retries consume the reserve before optional completion writers;
- unused reserve is released after the initial wave;
- remaining proposals run until the ceiling or deadline is reached;
- no budget deferral remains while a call is unused;
- genuine ceiling, deadline, and context deferrals have distinct reasons;
- concurrent writers cannot overspend the cumulative ceiling; and
- one empty or malformed cluster does not suppress other clusters.

### 6.3 Acquisition visibility

Add tests proving:

- every valid `recommend` row renders under Priority works to map;
- every valid `relevant_secondary` row renders under Additional cited works
  worth mapping;
- the same work is rendered once with all citing members;
- member attribution and characterization remain source-owned;
- `map_existing` and `acquire` actions remain distinct;
- rejected and unassessed candidates remain machine-only;
- dropped-member candidates do not render;
- last-good rows survive a failed refresh;
- newly mapped works retire both visible tiers; and
- acquisition rendering never parks valid cluster prose.

### 6.4 Duplicate identity and accounting

Add tests proving:

- two Zotero records sharing an ORA UUID become one canonical analytical work;
- identical DOI/ISBN records with compatible titles become aliases;
- exact unique title/author/year/type duplicates reconcile;
- ambiguous bibliographic matches do not merge;
- partial titles and author-only matches do not merge;
- canonical collection membership is the union of aliases;
- only the canonical work reaches relationship and cluster packets;
- aliases do not double-count cluster evidence;
- existing notes remain on disk and receive `alias_of` navigation;
- `zotero_metadata_issues.yml` identifies the Zotero duplicates;
- coverage totals distinguish records, canonical works, limited notes, and
  aliases;
- every canonical analytical work receives one cluster-accounting state;
- unknown family-plan IDs are rejected with an advisory warning;
- current workspace paths replace stale copied paths; and
- acquisition and cluster revisions join on the same semantic key.

### 6.5 Full local verification

Run the full existing suite, static checks, package build, bytecode compilation,
migration tests, and replay tests with no regressions.

## 7. Targeted comparative evaluation

No source regeneration is needed. Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v025-targeted-completion-evaluation-20260802`

Use an isolated copy of the v0.24 post-fix workspace and its frozen source
content:

- 193 stored notes;
- 169 analytical source records before duplicate reconciliation;
- 24 limited notes;
- one known duplicate analytical work to be reconciled locally;
- Mediation `B887A4Q8`; and
- Conflict Relapse `D2XT9ZU9`.

Commit implementation before paid evaluation. Make zero source/profile calls
and verify that every atomic-note semantic hash remains unchanged.

Run ID:

`eval-global-v025-targeted-20260802`

Configuration:

- DeepSeek `deepseek-v4-flash`;
- maximum reasoning for relationship and cluster calls;
- `provider_concurrency=auto`;
- normal single transport retry;
- one automatic exact retry only for `ProviderEmptyResponse`;
- no semantic correction or verifier call;
- literature deadline 7,200 seconds; and
- cumulative literature ceiling 32 calls.

The scheduler is dynamic rather than a set of independently spendable stage
quotas. A representative no-failure run is expected to use:

| Stage | Expected calls |
|---|---:|
| Family planning | 2 |
| Broad and complementary discovery | 3 |
| Relationship adjudication at no more than 15 jobs per packet | 7–9 |
| Cluster writers | 15 initial, then remaining feasible writers |
| Empty-response retries | 0 normally; up to 4 reserved initially |

The cumulative 32-call ceiling remains absolute. Unused retry capacity must be
released rather than stranded.

### 7.1 Relationship evaluation

Audit every fresh direct relationship and a deterministic sample of at least
30 contextual relationships. Read both complete atomic notes.

Release gates:

- at least 90% of visible relationships are genuinely useful for joint
  navigation;
- at least 95% of rationales faithfully state both endpoint bases;
- zero materially fabricated endpoint findings;
- zero materially reversed intellectual direction;
- reciprocal projection 100%;
- duplicate-active canonical pairs zero;
- every selected pair job answered or precisely and visibly parked without a
  paid omission retry; and
- no completed job is reprocessed.

Report exact type/direction accuracy and anchor completeness descriptively.
Neither a debatable label nor a missing optional anchor can independently fail
or qualify the release when the proposition and rationale remain accurate.

Retain the historical exact-40 bridge benchmark for descriptive continuity
only. Reuse the frozen 14-family benchmark and deterministic 30-pair blind
sample as the discovery gates:

- at least 12/14 families covered; and
- at least 80% blind candidate usefulness.

### 7.2 Cluster completion and quality

Audit every newly published cluster, every mixed cluster, and the four largest
remaining clusters.

Release gates:

- no eligible cluster remains budget-deferred while calls and deadline remain;
- every scheduled writer publishes or has an isolated diagnosable failure;
- membership relevance at least 90%;
- a specific contribution for every retained member;
- audited claim support at least 95%;
- debate and boundary accuracy at least 90%;
- reciprocal membership links 100%;
- zero duplicate canonical works inside a cluster; and
- zero whole-map partial state caused by one writer.

Minor wording, exact locators, and one debatable cluster boundary remain
advisory unless they materially misstate a source.

### 7.3 Acquisition evaluation

Reuse the frozen 20-work acquisition benchmark without exposing it to the
model.

Measure priority and secondary visibility separately:

- machine accounting for every eligible candidate: 100%;
- priority recommendation precision: at least 90%;
- combined visible relevance precision: at least 90%;
- attribution, identity, and action accuracy: 100%;
- every valid `relevant_secondary` disposition rendered in the secondary
  section; and
- at least 70% of eligible benchmark works visible in either priority or
  secondary form.

Priority-only benchmark recall remains descriptive. A work correctly shown as
secondary rather than priority is not a failure.

### 7.4 Identity and state evaluation

Require:

- the two *Peace and Conflict 2010* records resolve to one canonical work;
- no unrelated pair is merged;
- both Zotero aliases remain traceable;
- the work appears at most once in every relationship packet and cluster;
- stored-record, canonical-work, analytical, limited, and alias counts balance;
- every canonical analytical work has one cluster-accounting state;
- the Brahimi Report is explicitly accounted as non-analytical with its
  metadata/evidence identity-conflict reason unless corrected source metadata
  changes that classification;
- no invalid family-plan source ID survives;
- workspace manifests point to the v0.25 workspace; and
- acquisition and cluster revision joins are exact.

### 7.5 Replay

Snapshot provider ledgers, semantic files, projections, hashes, mtimes, and
file membership. Replay the identical build and require:

- zero new provider calls;
- no semantic or projection changes;
- byte-identical generated files;
- unchanged mtimes;
- no additions or removals; and
- stable relationship, cluster, acquisition, identity, and event revisions.

## 8. Deliverables

Write:

- `evaluation/v025-targeted-completion-comparison.md`;
- machine-readable relationship, packet-completion, cluster-scheduling,
  acquisition, identity, provider, runtime, and replay metrics;
- an updated v0.16–v0.25 historical trend table whose exact-pair and exact-label
  columns are explicitly descriptive;
- representative successes, material failures, and advisory imperfections;
- a list of every provider response that omitted a requested job; and
- a private Obsidian export with zero missing wikilinks.

The final verdict must use the calibrated rubric in section 3. It must not
produce a qualified-fail result solely because of a useful relationship's
debatable label, a missing optional evidence-anchor ID, minor locator drift,
priority-versus-secondary acquisition placement, stylistic imperfections, or
an exact benchmark-pair miss.

## 9. Assumptions and non-goals

- Atomic-note quality is accepted and remains out of scope.
- The source and profile prompts remain frozen.
- Existing complete atomic notes remain the evidence supplied to relationship
  and cluster calls.
- DeepSeek remains responsible for intellectual judgments; deterministic code
  handles identity, structure, accounting, budgets, persistence, and
  projection only.
- Zotero remains read-only. Duplicate cleanup is recommended, never performed.
- Web enrichment remains a later acquisition workflow and cannot alter
  synthesis evidence.
- No full 195-source regeneration is required for v0.25.
- If the targeted evaluation reveals a material source-grounding or state-loss
  defect, report it directly. Do not weaken the calibrated gates, raise the
  call ceiling, or begin a full regeneration without approval.
