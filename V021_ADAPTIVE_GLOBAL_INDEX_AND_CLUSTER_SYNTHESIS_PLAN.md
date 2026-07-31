# Auto-Zettelkasten v0.21 Adaptive Global Index and Cluster Synthesis Plan

**Status:** Proposed

**Date:** 2026-07-30

**Foundation:** Engine `0.20.0`, artifact schema `1.15`, source semantic
prompt `5`, source provider envelope `source-bundle-envelope-v2`, source
catalogue schema `5`, note metadata schema `2`, relationship prompt `10`,
relationship decision contract `relationship-decision-v7`, relationship
registry schema `6`, cluster plan prompt `5`, cluster synthesis prompt `29`,
and the v0.20 implementation through commit `8114a4f`.

**Primary evidence:**

- `V020_LIBRARY_SCALE_IDENTITY_AND_GRAPH_RELIABILITY_PLAN.md`;
- the v0.20 evaluation at
  `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v020-evaluation-20260730/evaluation/v020-full-comparison.md`;
- manual inspection of the v0.20 Berg atomic note;
- manual inspection of the v0.20 cluster
  *Ceasefire Design and Peacekeeping Effectiveness: Context and Mechanisms*;
- the frozen 40-pair cross-literature bridge benchmark; and
- the full discussion following v0.20 about index scale, chunking,
  directional citations, relationship evidence, registry state, cluster
  organization, overlapping membership, and cited-but-unmapped literature.

## 1. Objective

v0.21 will make the graph and cluster workflow simpler, more complete, and
more readable without regenerating or redesigning successful atomic notes.

The release has five objectives:

1. replace hierarchy-first discovery with an adaptive complete-index-first
   workflow;
2. recover cross-literature bridge recall without weakening full-note
   adjudication;
3. remove mandatory relationship evidence-anchor IDs as a publication failure
   point while retaining source-grounded explanations;
4. ensure each source pair has one coherent current effective machine
   adjudication, including intentional primary and secondary relationship
   types; and
5. improve cluster membership, use the literature-position graph inside atomic
   notes, and render concise source-grouped cluster syntheses.

This is a graph-and-cluster release. The source layer is frozen:

- no source API calls;
- no atomic-note regeneration;
- no compact-profile regeneration;
- no extraction or scope reclassification;
- no source-prompt change; and
- no Zotero mutation.

The two parked v0.20 sources remain deferred. The v0.20 atomic-note result is
accepted as the release baseline.

## 2. v0.20 results to preserve

v0.21 must not trade away the following:

- 175/177 historically readable sources represented;
- 120/120 sampled critical facts;
- 380/380 sampled source-grounded claim units under the v0.20 rubric;
- 141/142 headline numeric values accurately represented;
- zero material unsupported causal upgrades;
- 142 mapped citations with reciprocal `cites`/`cited_by` projections;
- correct Berg links to Walter 2002 and Hegre 2015;
- 17 clusters, including eight mixed-literature clusters;
- 100% audited cluster membership relevance;
- 98.16% audited cluster claim support;
- complete reciprocal atomic-note, relationship, and cluster projections;
- a clean global build below 30 literature calls; and
- exact zero-call, zero-write replay.

The strongest existing architecture remains:

- one canonical source record and atomic note per work;
- deterministic bibliographic identity and citation reconciliation;
- probabilistic intellectual judgment;
- complete atomic notes for final relationship and cluster synthesis;
- a registry as machine source of truth;
- additive managed Markdown projection; and
- affected-neighborhood incremental updates.

## 3. Diagnosis

### 3.1 Hierarchy was used before it was needed

The v0.20 global map routed 193 sources through collection and virtual-shard
cards. It selected 12 shard combinations but did not select the direct
Mediation-versus-Conflict-Relapse collection pair requested by the evaluation.

Fresh bridge performance regressed:

| Version | Candidate recall | Useful/final recall |
|---|---:|---:|
| v0.18 | 12/40 | 9/40 |
| v0.19 | 7/40 | 6/40 |
| v0.20 clean | 5/40 after mandatory injection | 2/40 |

The bridge call returned 27 rows but only 18 unique pairs and concentrated on
several prominent themes. The failure was primarily probabilistic routing and
bridge-family coverage, not deterministic filtering.

### 3.2 The master catalogue and the model-facing index were conflated

The v0.20 `source_catalogue.yml` is correctly rich machine state. For 193
sources it occupies about 1.97 MB because it also includes:

- 493 Zotero collections;
- 85 virtual topics and 86 virtual shards;
- identity and navigation fields;
- cluster and relationship IDs;
- collection membership; and
- routing and revision metadata.

That is not the representation that discovery should receive.

A measured uncapped model-facing projection containing only source identity,
title, author, year, the existing complete compact thesis, and the existing
complete compact method would use roughly:

- 18,000–29,000 tokens for the 193-source evaluation corpus; and
- approximately 690,000–790,000 tokens if all 5,324 Zotero snapshot records
  had comparable mapped profiles.

The evaluation corpus easily fits in one call. The entire current Zotero
library may also fit inside a one-million-token context, but it may exceed the
preferred safety target once prompt instructions, reasoning, and output are
reserved.

The correct decision is made from the serialized prompt size. Do not ask a
model to count characters. Do not truncate a thesis or method at an arbitrary
character boundary.

### 3.3 Independent chunks cannot see cross-chunk pairs

Two independent calls over the first and second halves can discover
relationships within each half. They cannot discover a relationship whose
endpoints are divided between the halves because neither call sees both
entries.

Chunking remains a valid overflow strategy when combined with:

- a compact global spine or family summary;
- explicit cross-chunk family selection;
- bounded calls over the selected chunk pairs; and
- one deterministic candidate merge.

This fallback should run only when the complete lean index does not fit.

### 3.4 Citations and inferred intellectual relationships were conflated

A mapped citation is directional and can be established from the citing work:

```text
Kennedy --cites--> Hartzell and Hoddie
```

The inverse `cited_by` projection is a navigation view. It does not claim that
Hartzell and Hoddie cite Kennedy.

Citation projection does not require evidence from both atomic notes. A
separate claim that Kennedy supports, qualifies, extends, or is contextually
connected to Hartzell and Hoddie requires cross-source judgment.

v0.21 must preserve every strong mapped citation even when an additional
inferred relationship is absent or incomplete.

### 3.5 Mandatory relationship anchor IDs were too brittle

The v0.20 diagnostic produced useful reasons but parked 13 responses for
missing endpoint anchor IDs.

Examples included:

- Kennedy and Hartzell/Hoddie, where a directional citation already exists;
- Suhrke/Samset and Walter, where the atomic notes correctly map a debate over
  the widely repeated 50% recurrence estimate;
- Bercovitch et al. and Bercovitch/Houston, where the later study was described
  as extending the earlier directive-strategy findings; and
- several contextual comparisons where both full notes had been supplied but
  the model returned empty anchor arrays.

An anchor ID is internal bookkeeping. Failure to reproduce it does not mean
that the atomic note lacks evidence or that the intellectual relationship is
wrong.

The Suhrke/Samset–Walter–Collier mapping is a representative success:

- Collier and Hoeffler supplied an influential high recurrence estimate;
- Suhrke and Samset criticized its construction and policy circulation;
- later Collier estimates were lower;
- Walter obtained a lower estimate under a narrower definition; and
- the graph captured citation, methodological criticism, and corroborating
  evidence.

v0.21 should preserve this reasoning and remove machine anchor copying as a
mandatory publication gate.

### 3.6 Pair-decision history and current state were not separated

After the v0.20 diagnostic:

- 259 accepted decision rows represented 170 canonical source pairs;
- 89 pairs therefore had more than one active decision record;
- 35 duplicated the same relation ID; and
- 54 carried several active relation IDs, sometimes with related but different
  labels.

Some label overlap is intellectually legitimate:

- `undermines` commonly also implies `contrasts`;
- `qualifies` may contain a bounded contrast; and
- two works may support one proposition while differing on another.

The defect is not the existence of nuance. The defect is that separate runs
left several independent decisions marked current without recording whether
they were:

- intentional concurrent relationships;
- primary and secondary descriptions of one relationship; or
- an older decision superseded by a newer one.

### 3.7 Cluster prose was repetitive

The representative ceasefire/peacekeeping cluster repeated:

- the Fortna 2018 source link three times;
- the Fortna 2007 link four times;
- the Gromes link three times; and
- the Gilligan/Stedman link three times.

It also generated a separate “In plain English” statement for ordinary
qualitative findings that were already understandable.

Plain-English statistical interpretation was intended only for technical
results whose practical meaning is not apparent to a non-specialist. It was
not intended to paraphrase every sentence.

### 3.8 Cluster membership was accurate but underinclusive

The ceasefire/peacekeeping cluster retained five relevant works, but several
other mapped notes materially inform the same literature:

- Quinn and Mason, *Sustaining the Peace*;
- Doyle and Sambanis, *Making War and Building Peace*;
- Almuslem, *Post Conflict Justice, Peacekeeping, and Civil Conflict
  Recurrence*;
- Mross and Fiedler, *Identifying Pathways to Peace*;
- Levin and Miodownik on disarmament, peacemaking, and recurrence; and
- the Brahimi Report on mission design and implementation capacity.

These works need not all be core members. They should at least be considered
for core, supporting, mechanism, boundary, or practitioner roles.

The planner behaved too much like an exclusive partitioner. A source assigned
to a justice, governance, or general recurrence cluster was less likely to
enter the peacekeeping cluster even when it had a distinct relevant
contribution.

### 3.9 Atomic notes contain a second-order literature graph

Each atomic note's structured `Position in the Literature` records contain:

- an important cited work;
- the current author's characterization of it;
- how the current author uses, extends, criticizes, or contrasts it;
- identifiers and match status; and
- an approximate locator.

Cluster synthesis currently underuses this graph.

A mapped cited work should become a high-priority membership or relationship
candidate. A cited-but-unmapped work can still explain how mapped authors
position their contribution and can become an acquisition recommendation.
The cluster must not treat that second-hand characterization as independently
verified evidence.

## 4. Settled design decisions

### 4.1 Flat complete index first

Use one complete lean index whenever its serialized prompt fits the existing
provider context budget with reserved reasoning and output headroom.

Hierarchy and chunking are overflow fallbacks, not the normal path.

### 4.2 No arbitrary thesis or method truncation

The index uses the complete compact thesis and method already generated during
atomic-note creation.

- Do not ask DeepSeek to count characters.
- Do not cut prose at a local character boundary.
- Do not make another model call to shorten an entry.
- Measure the complete serialized packet.
- Split between complete source records when the packet is too large.

### 4.3 One canonical catalogue, several deterministic projections

Keep `source_catalogue.yml` as the rich source of truth.

Render, on demand:

- a lean model-facing discovery projection;
- human-facing Markdown collection and topic indexes;
- machine identity lookups; and
- complete-note retrieval packets.

Do not add a second canonical index database.

### 4.4 Directly compare requested collections

If a run explicitly names two or more collections, every requested
collection pair must appear in the planning context or receive a direct
family-discovery route.

Probabilistic routing may add other useful collection combinations. It may not
replace the requested comparison.

Represent this intent explicitly. Add optional
`comparison_collection_keys` to `LiteratureMapRequest`/`build_map` and a
repeatable CLI `--compare-collection KEY`. When omitted, global planning may
choose collection combinations probabilistically and must not pretend that all
possible pairs were user-requested.

### 4.5 Probabilistic discovery, deterministic transport

DeepSeek decides:

- which sources are worth comparing;
- which literature families matter;
- which bridge candidates are plausible;
- whether a relationship exists;
- relationship type and direction;
- cluster membership and role; and
- the intellectual synthesis.

Local code only:

- renders and sizes complete source records;
- splits packets at source boundaries;
- maps IDs;
- validates requested collection coverage;
- deduplicates candidate pairs;
- applies call and candidate budgets;
- persists decisions;
- retires stale current state; and
- projects Markdown.

### 4.6 Citations remain distinct from agreement

A strong mapped citation always creates a directional `cites` relation and an
inverse `cited_by` navigation projection.

Citation does not automatically become `supports`, `extends`, `qualifies`, or
another intellectual relationship.

### 4.7 Complete atomic notes remain the final evidence context

Discovery sees the lean index.

Final relationship adjudication sees both complete atomic notes.

Final cluster synthesis sees every complete atomic note proposed for that
cluster, subject only to the measured provider context boundary.

### 4.8 Evidence explanations replace mandatory anchor IDs

The relationship model must explain what **each** work contributes. It does
not have to reproduce internal evidence-anchor identifiers.

Existing atomic-note anchors remain useful for source auditing, cluster
planning, and optional diagnostics. They stop being a mandatory relationship
publication gate. Removing anchor-ID matching does not remove two-note
grounding: every inferred direct or contextual connection must still contain
one non-empty, source-specific basis for each endpoint.

### 4.9 One current pair adjudication, preserved history

Each canonical source pair has one current **effective** machine adjudication
record: an accepted relationship decision or a structurally valid
`no_relationship`.
Malformed, incomplete, or otherwise parked refresh attempts remain pending
history and do not replace the last valid effective decision.

That record may contain:

- one primary relationship;
- optional secondary relationship types deliberately returned with the
  primary decision; and
- at most two propositions, and only when each proposition has its own
  source-specific bases and explanation.

Older machine decisions remain inactive history. Human-authored links remain
untouched.

### 4.10 Cluster membership is overlapping

A source may belong to several clusters when it makes a different material
contribution to each.

Clusters are not an exclusive partition. Unclustered sources remain neutral
and eligible for future clusters.

### 4.11 Cited-only literature is attributed, not laundered

A cluster may say:

> Fortna characterizes Smith as arguing X and uses that argument to motivate
> Y.

Until Smith's mapped note or source is inspected, it may not say:

> Smith proves X.

### 4.12 Advisory quality checks remain advisory

Missing optional evidence detail, a locator warning, or a model formatting
imperfection must not trigger a semantic retry or erase otherwise usable graph
work.

Only gross failures prevent publication of the affected new connection:

- wrong source IDs;
- unknown endpoints;
- unusable or ambiguous response structure;
- no relationship decision;
- a missing source-specific basis for either endpoint;
- no intelligible relationship reason; or
- an internally impossible actor/reference direction.

A failed refresh never erases the last valid relationship or cluster. It marks
that artifact `refresh_pending` while retaining the last valid visible
version.

## 5. Release identities and compatibility

Release as:

- engine `0.21.0`;
- artifact schema `1.16`;
- source semantic prompt `5`;
- source provider envelope `source-bundle-envelope-v2`;
- source catalogue schema `6`;
- note metadata schema `2`;
- Zotero collection snapshot schema `2`;
- literature-position registry schema `2`;
- relationship prompt `11`;
- relationship decision contract `relationship-decision-v8`;
- relationship registry schema `7`;
- cluster plan prompt `6`; and
- cluster synthesis prompt `30`.

Keep public Python APIs and CLI options backward-compatible.

Do not make custom reasoners implement a stricter protocol. Use capability and
response-shape detection:

- legacy v7 relationship responses continue to parse;
- v7 anchor fields may be preserved as optional provenance;
- legacy one-relation decisions normalize into one v8 connection;
- a custom reasoner without secondary types returns only a primary type; and
- existing cluster responses remain readable during migration.

No provider, Zotero, or cloud call occurs during migration.

## 6. Implementation

### 6.1 Freeze the source layer

Do not modify:

- source extraction;
- the v2 source provider envelope;
- atomic-note headings or content prompt;
- profile semantic fields;
- scope classification; or
- source call checkpoints.

The v0.21 graph build consumes the exact frozen v0.20 notes and profiles.

Changing v0.21 relationship, index, or cluster prompt identities must not
invalidate source bundles or notes.

### 6.2 Add a lean on-demand discovery projection

Extend the existing catalogue code in `indexes.py`; do not introduce a new
index subsystem.

For each eligible source, render one record containing:

```yaml
source_id: source-zotero-...
zotero_key: ABC12345
title: Complete title
author: Compact author display
year: "2020"
thesis: Complete existing compact thesis
method: Complete existing compact method or knowledge basis
source_scope: full_document
evidence_eligibility: substantive_bounded
collection_keys: [...]
```

For model transport, use compact JSON objects or JSONL generated in memory.
The representation is not a new canonical file.

Do not include:

- evidence anchors;
- full note text;
- relationship IDs;
- graph neighbors;
- cluster IDs or summaries;
- virtual-topic assignments;
- catalogue revision counters;
- provider histories;
- note frontmatter unrelated to discovery; or
- duplicated collection objects.

Each source appears exactly once. Collection membership uses compact keys
rather than repeated collection prose.

Build records only after the existing v0.20 identity reconciliation has
collapsed duplicate Zotero entries by strong identifiers (Zotero relations,
DOI, ISBN, and normalized bibliographic identity). One canonical work receives
one note and one lean-index row; alias Zotero keys and all collection
memberships remain attached to that canonical record.

The discovery projection is rendered deterministically from the current
catalogue. An unchanged catalogue produces identical bytes.

Do not build the lean projection from `_catalogue_entry`'s presently shortened
display fields. Read the complete probabilistically generated compact thesis
and method from the frozen profile/note record and normalize whitespace only.
Remove the hidden 360-character thesis and 220-character method shortening
from this model-facing path. Existing human navigation cards may remain
bounded because they are routing summaries, not the complete discovery index.

### 6.3 Measure context instead of truncating content

For built-in DeepSeek reasoners, serialize the actual system and user prompts
and pass that exact payload through the reader's existing `_prompt_fits`
admission logic. Do not create a second approximate budget formula. Custom
reasoners receive the existing conservative fallback without a stricter public
protocol.

Calculate:

- system and task instructions;
- complete serialized discovery entries;
- explicit requested-collection metadata;
- required reasoning/output reserve; and
- provider-specific context safety margin.

Take the flat path when the complete packet fits. Otherwise split only between
complete source records.

Record:

- serialized characters;
- estimated or provider-tokenized input tokens;
- configured context maximum;
- reserved output/reasoning allowance;
- chosen path: `flat_complete_index` or `chunked_index_reconciliation`; and
- the source IDs in each packet.

Do not expose a public character-cap option.

### 6.4 Use one shared global planning call on the flat path

Replace the default v0.20 collection-router and virtual-shard-router sequence
with one global planning call when the complete index fits.

Add one optional built-in reasoner capability,
`plan_literature_families`. Invoke it after the catalogue and lean projection
are stable, but before `_run_relationship_reasoning` starts candidate
discovery. Persist its validated result and provider receipt under a
checkpoint keyed only by:

- complete lean-index content hash;
- explicitly requested collection membership hashes;
- resolved mapped-citation and literature-position identity hashes;
- human-authored relationship-pair hashes;
- provider and model identity;
- literature-family prompt identity; and
- planning policy identity.

The result is a shared upstream artifact. `_run_relationship_reasoning`
consumes its `discovery_jobs`; `build_literature_report` consumes its
`literature_families` and `neighboring_families`. The latter must not make a
second global cluster-planning call when a valid shared plan is present.

Keep custom reasoners backward-compatible through capability detection. When
`plan_literature_families` is unavailable, retain the current relationship
selection and `plan_clusters` path; unverified custom planner output must not
be forced into the built-in shared schema. Record the fallback path in the run
ledger and budget its existing calls explicitly.

The call receives:

- the complete lean index;
- requested collection keys and names;
- compact collection membership;
- mapped citation/literature-position pairs;
- human-authored relationship pairs;
- clear output responsibilities.

It returns:

```yaml
literature_families:
  - family_id: ...
    label: ...
    organizing_problem: ...
    source_ids: [...]
    proposed_roles: {...}
    candidate_cluster: true

discovery_jobs:
  - job_id: ...
    family: ...
    left_source_ids: [...]
    right_source_ids: [...]
    requested_collection_pair: [...]
    discovery_goal: ...
    candidate_quota: ...

neighboring_families:
  - left_family_id: ...
    right_family_id: ...
    reason: ...
```

The planning call does not adjudicate relationships and does not write
clusters. It identifies broad, overlapping candidate families and bounded
discovery jobs.

It must:

- inspect the full source inventory;
- include every explicitly requested collection pair;
- seek within-literature and cross-literature families;
- allow a source to appear in several families;
- avoid treating every source as requiring a cluster;
- avoid using citation as proof of agreement; and
- return fewer families rather than inventing incoherent ones.

The returned literature families also improve optional agent/human virtual
navigation. They do not become clusters until complete-note synthesis accepts
them, and they are not relationship evidence.

Apply machine-generated negative-pair memory locally **after** cached
candidate discovery. It must not enter this planning fingerprint or prompt:
`no_relationship` is an output of adjudication and allowing it to invalidate
its own upstream discovery would create a replay cycle.

Recover provider envelopes locally before declaring the shared plan malformed.
Reuse the source-envelope principle already proven in v0.20:

- accept fenced JSON, prose-wrapped JSON, or a single-object list only when
  exactly one complete plan object passes schema and source-ownership checks;
- reject ambiguous, truncated, or incomplete output;
- preserve the raw provider response and completion metadata;
- make no repair call; and
- on refresh, retain the last valid family plan, effective relationship graph,
  and cluster map if a plan cannot be recovered.

A failed shared-plan refresh may commit deterministic identity and citation
changes only. Mark `planning_refresh_pending`; do not retire machine
relationships or invoke another automatic semantic fallback call. On a first
build with no prior valid family plan, return a clearly terminal partial
planning state while leaving registries empty and accounted—not a misleading
successful empty graph.

### 6.5 Use chunk-and-reconcile only on measured overflow

When the complete index does not fit:

1. split the lean index into stable, context-bounded packets at source-record
   boundaries;
2. give each packet the requested collection metadata and a compact global
   spine or previously generated family cards;
3. ask each packet for local literature families and possible external family
   connections;
4. reconcile the compact family summaries in one call;
5. create explicit cross-chunk discovery jobs only for promising family
   combinations; and
6. merge all jobs deterministically.

Reconciliation selects a bounded set of cross-chunk family combinations. It
must never expand into every possible chunk pair or family pair. An explicitly
requested collection comparison remains mandatory even when its sources span
several chunks.

The global spine contains only enough information to route:

- source ID;
- title;
- author;
- year; and
- a short existing topic/family label when one already exists.

Do not generate a second thesis or method summary for the spine.

A source may appear in a routing spine and one full chunk without creating a
duplicate catalogue entry or atomic note.

For exactly two chunks, the reconciliation must consider:

- within chunk A;
- within chunk B; and
- selected relationships between A and B.

Do not assume that merging independent A and B results recovers cross-chunk
pairs.

### 6.6 Run broad, disjoint discovery jobs

Use the planning result to create a small number of independent family
discovery calls.

For the 193-source evaluation corpus:

- target two to four calls;
- run them concurrently;
- assign different literature or bridge families;
- give each a unique-pair quota;
- permit both direct and contextual candidates;
- require a concrete shared proposition, mechanism, outcome, debate,
  sequence, institutional problem, or boundary; and
- optimize candidate-stage recall rather than final precision.

Retain the existing 120-unique-pair adjudication ceiling for this corpus.
Family quotas divide that capacity; duplicates and deterministic citation-only
edges do not consume it. Five adaptive full-note packets of at most 30 jobs
provide sufficient transport headroom without raising the semantic pair
ceiling.

One job must directly compare Mediation and Conflict Relapse.

Candidate discovery receives complete lean index records for the selected
families. It does not receive full atomic notes.

The same source may enter several jobs, but each canonical pair is adjudicated
once after deterministic merge.

Local candidate handling may:

- map and validate source IDs;
- ensure bridge-only pairs cross the intended collections;
- canonicalize pair ordering;
- deduplicate;
- merge discovery reasons and family provenance;
- consult negative-pair memory;
- rank by model confidence and family diversity; and
- enforce the run's pair ceiling.

It may not invent a candidate the model or deterministic citation layer did
not propose.

Mapped citations are already preserved as useful deterministic links. Do not
send every citation through full-note intellectual adjudication. Promote a
citation pair into the bounded adjudication pool only when selected by the
shared literature plan, an important literature-position record, or a family
discovery job.

### 6.7 Preserve deterministic one-way citation autolinking

Keep the v0.20 identity matcher and literature-position reconciliation.

For every strong mapped match:

- project `cites` from the citing note;
- project `cited_by` in the cited note as an inverse navigation label;
- preserve the author's engagement description;
- keep the citation visible even if inferred adjudication returns
  `no_relationship`, a contextual relation, or an unusable response.

Submit the pair for a stronger inferred relationship only when the shared
planner, an important literature-position engagement, or family discovery
selects it within the pair budget.

No two-sided evidence is required for the citation edge.

For `known_zotero_unmapped`:

- retain bibliographic identity and why the mapped author invokes it;
- add or update the existing retrieval/generation recommendation;
- do not create a dangling atomic-note wikilink; and
- permit attributed mention in a cluster's unmapped-literature section.

For `not_in_snapshot`, preserve an acquisition recommendation only when the
citation is important to an atomic note or cluster. Routine citations remain
unlisted.

For `ambiguous`, do not auto-link or select one candidate.

### 6.8 Simplify the relationship provider contract

Continue to batch adaptive pair jobs and include both complete atomic notes.
Deduplicate source documents inside a request so one atomic note appears once
even when it participates in several pair jobs.

Rewrite the built-in relationship prompt as v11 instead of appending more
rules to the accumulated v10 text. Remove obsolete mandatory anchor-ID,
model-written projection-label, and duplicate direction instructions. Keep
the contract focused on the two-note intellectual judgment below.

Replace v7's mandatory endpoint anchor arrays with v8:

```yaml
pair_job_id: relationship-job-...
decision: relationship | no_relationship
pair:
  source_a_id: ...
  source_b_id: ...

connections:
  - proposition: ...
    primary_relation_type: supports
    secondary_relation_types:
      - contrasts
    actor_source_id: ...
    reference_source_id: ...
    source_a_basis: What source A establishes or argues
    source_b_basis: What source B establishes or argues
    reason: Why these bases create this relationship
    boundary: Important scope or method limitation
    confidence: high | medium | low
```

Most decisions contain one connection. Permit a second connection only when
the pair is related through a genuinely different proposition.

Projection semantics are deliberately narrow:

- one pair adjudication may contain one or two proposition connections;
- each proposition receives one stable connection ID;
- `primary_relation_type` supplies the visible edge label;
- secondary types appear as metadata in that connection's explanation rather
  than as duplicate parallel edges; and
- a second visible edge exists only for the genuinely separate second
  proposition.

The prompt must distinguish:

- citation from substantive support;
- `contrasts` from evidence that actually `undermines`;
- `qualifies` from general topical difference;
- explicit `extends` lineage from chronology or similarity;
- contextual usefulness from generic “fuller picture” wording; and
- primary from secondary relationship labels.

The same call performs a concise self-review:

- both source IDs are correct;
- actor/reference direction matches the explanation;
- primary and secondary labels describe the stated proposition;
- each source-specific basis accurately reflects its atomic note; and
- no citation direction, chronology, or dataset reuse is reversed.

Do not ask for machine anchor IDs, relation IDs, timestamps, note IDs, or
projection labels. Local code derives them.

Normalize live v8 batch responses at the per-job boundary before parking
anything. Reuse the conservative local recovery already proven on v0.19 and
v0.20 saved responses:

- accept fenced or prose-wrapped objects, singleton object lists, recognized
  `decision: <relation_type>` shorthand, and harmless scalar-versus-list
  variants only when the recovered job is unambiguous and complete;
- validate and retain valid sibling jobs when another row in the same batch is
  malformed;
- preserve the complete raw response, completion metadata, and row-level
  failure reason;
- reject ambiguous, truncated, wrong-pair, or incomplete jobs; and
- make no repair or semantic retry call.

Represent the normalized result internally as a pair outcome rather than
stretching the existing singular relationship row:

- `RelationshipPairOutcome` owns the supplied pair, final status, provider
  provenance, and zero to two connections;
- `RelationshipConnection` owns one proposition, primary/secondary types,
  actor/reference direction, both endpoint bases, reason, boundary,
  confidence, and optional legacy anchor provenance; and
- the existing v7 `RelationshipDecision` remains a compatibility input that
  normalizes into a one-connection pair outcome.

Do not replace the public custom-reasoner protocol with these internal types.
The adapter accepts legacy responses and the built-in reader emits v8.

Give every validated connection a stable ID derived from:

- the canonical pair;
- normalized proposition;
- primary relationship type; and
- actor/reference direction.

Do not derive the ID from pair and relationship type alone. Two genuinely
different propositions may have the same primary type and must not collide.

The v8 provider packet continues to deduplicate complete atomic-note
documents, but it no longer sends the model a list of internal anchor IDs to
copy. Existing selected evidence may remain private local provenance and
legacy v7 transport may retain its old fields for compatibility.

Persist raw and normalized provider material separately:

- `provider_result.json` contains the exact raw job row or recoverable raw
  envelope plus completion metadata;
- `result.json` contains only the normalized, validated pair outcome; and
- the job/global cache is marked `completed` only after local normalization
  and validation succeed.

If one row is invalid, preserve its raw output and mark only that job parked.
Valid sibling rows become completed immediately. A cache hit must pass through
the current local normalizer before it can be trusted; a parser improvement may
therefore recover saved raw output with zero provider calls.

### 6.9 Make relationship validation structural and non-dictatorial

Local validation requires:

- a known pair job;
- the two supplied source endpoints;
- an allowed primary and secondary relation type;
- valid actor/reference IDs when direction is required;
- one non-empty source-specific basis for source A;
- one non-empty source-specific basis for source B;
- an intelligible reason;
- no self-link; and
- no third source inserted into the decision.

Local validation may record advisory warnings for:

- an unusually generic contextual reason;
- missing boundary text;
- an optional locator or legacy anchor mismatch; or
- an unsupported secondary-label shape.

These warnings do not trigger a provider retry.

Only the unusable connection is parked. A missing endpoint basis is a
publication failure for that connection, but causes no paid retry and does not
erase a prior valid decision. Do not downgrade, rewrite, or reinterpret the
scholarship deterministically.

Legacy v7 decisions:

- normalize recognized shorthand locally;
- retain valid reasons and directions;
- preserve optional source evidence and anchor IDs when available;
- convert one v7 relation into one v8 connection; and
- do not park solely because an endpoint anchor array is empty.

The same normalizer handles live v8 and saved v7/v10 envelopes through
response-shape detection. Do not maintain separate repair pipelines for each
version.

When optional legacy anchors are present, validate and preserve recognized
ones as provenance. An unknown or missing anchor creates an advisory warning,
not a publication failure. Normalize the mandatory endpoint bases into the
existing source/target evidence envelope used by cluster context so cluster
synthesis can consume grounded relationships even when locator or anchor
fields are empty.

### 6.10 Reconcile one current adjudication per canonical pair

Fix `persist_relationship_registry` rather than adding another registry.

Separate:

- immutable or append-only decision history; and
- the current **effective** decision index keyed by canonical unordered source
  pair.

The effective index is keyed by canonical pair, not by catalogue revision,
profile fingerprint, provider attempt, prompt version, or decision key. Those
values describe the current outcome and its history; they do not create
several simultaneously current machine states for one pair.

When a current-fingerprint accepted or `no_relationship` decision is
committed:

1. canonicalize the endpoint pair;
2. validate and group every new proposition connection for that pair;
3. write exactly one new effective pair-decision record;
4. retire every prior effective machine adjudication record for that pair in
   the same atomic commit;
5. project every validated connection deliberately contained in that record;
6. retain prior decisions as inactive history with retirement lineage; and
7. commit all changes atomically before cluster planning.

Do not persist connections row by row: two connections from the same decision
must not supersede each other.

A malformed, incomplete, or otherwise parked refresh is recorded as an
attempt in history with precise diagnostics. It does **not** become the
effective decision and does not retire the last valid accepted or
`no_relationship` record. Mark the pair `refresh_pending` until a later valid
decision supersedes it.

Write the canonical history and run-scoped ledger from the same stable
decision/attempt event IDs. Every scheduled pair job must appear in both as
accepted, `no_relationship`, parked, transport-failed, or unscheduled. A
resume merges events by ID instead of replacing the run ledger. This preserves
the v0.19 lesson that a complete canonical registry is not enough when the run
ledger silently omits decisions.

The current pair record contains:

```yaml
pair_key: source-a--source-b
decision_key: ...
status: accepted | no_relationship
connections: [...]
provider: ...
model: ...
prompt_version: "11"
input_profile_hashes: {...}
active: true
supersedes: [...]
refresh_pending: false
```

If a pair contains several intentional propositions, those propositions live
inside the one current record. They are not separate stale adjudications.

Human-authored relationships:

- remain independently active;
- are never retired by a machine `no_relationship`;
- may coexist with the current machine adjudication; and
- remain outside machine-pair migration.

The relationship stage currently commits probabilistic state before cluster
planning and later commits structural relationships after cluster work. Both
calls must use the same effective-pair reconciliation helper. The later
structural-only commit may add or preserve structural links, but it must not
reactivate retired probabilistic history or replace the current machine pair
outcome.

Projection reads only:

- active human relationships;
- deterministic citation/structural links; and
- connections belonging to the one effective accepted machine outcome for
  each pair.

Projection deduplicates by stable connection ID, not merely by target note and
primary relationship type. This permits two separate propositions of the same
type while still collapsing exact duplicates. Retiring a connection removes
only its managed Markdown line; decision history remains in the registry.

### 6.11 Migrate the v0.20 registry locally

On first read of schema 6:

- group machine pair decisions by canonical pair;
- identify identical duplicate rows;
- retain a structurally valid current-fingerprint decision when saved state
  unambiguously identifies one;
- otherwise use trustworthy saved ordering metadata when available, or a
  documented stable fallback when it is not, to select one structurally valid
  prompt-10 decision as the **provisional current effective** record and mark
  it `reconciliation_pending`;
- collapse identical repeated relation IDs;
- mark all other superseded machine decisions inactive history;
- preserve every event and raw diagnostic;
- rebuild active projections from the current-pair index; and
- make the migration idempotent.

Use one pure registry-payload migration/reconciliation helper from both normal
registry loading and workspace migration. Do not maintain separate selection
rules in `migration.py` and `relationships.py`.

The current metadata migration hard-codes registry schema 6. Replace that
behavior so a migrated schema-7 registry can never be downgraded on the next
workspace open.

Do not decide whether `contrasts` is intellectually better than `undermines`
during migration.

When several old active labels conflict and no current v0.21 decision exists,
prefer a structurally valid decision only when the saved state provides a
trustworthy current fingerprint or actual ordering metadata. Schema 6 does not
always contain reliable timestamps. When “newest” cannot be established,
choose one structurally valid record by a documented stable fallback, mark it
`reconciliation_pending`, retain every alternative as inactive migration
history, and queue the pair for v0.21 readjudication. Do not imply that the
fallback is the intellectually superior judgment.

Replace a provisional record only after a successful prompt-11 accepted or
`no_relationship` decision. A planner, provider, contract, or budget failure
must never leave a previously valid linked pair with no effective machine
state.

Migration and local reparse cost zero provider calls.

### 6.12 Reuse literature positions during cluster planning

For every proposed cluster family, collect the important structured
literature-position records from its candidate atomic notes.

Separate:

1. **mapped cited works** — existing atomic notes;
2. **known Zotero but unmapped works**;
3. **works absent from the Zotero snapshot**; and
4. **ambiguous matches**.

Mapped cited works:

- are supplied to the probabilistic planner as bounded, high-priority
  literature-position options;
- become cluster-membership candidates only when the planner selects them and
  explains their cluster relevance;
- may join relationship discovery only within the normal pair budget;
- must have their complete atomic note loaded before the cluster writer uses
  their findings independently; and
- remain eligible even when they already belong to another cluster.

Known-unmapped or absent works:

- enter cluster context only when the citing note marks them important;
- include which mapped member invokes them and why;
- update the existing missing-source/retrieval ledger;
- do not become cluster members; and
- cannot establish a finding, statistic, debate, contradiction, or consensus.

Ambiguous matches remain unlinked and are not promoted.

Do not send complete bibliographies. Send only the bounded important
literature positions already selected during atomic-note generation.
Deterministic code resolves identities and loads selected notes; it does not
infer that a citation belongs in a cluster.

### 6.13 Make cluster candidate planning broad and overlapping

The global planning call proposes a deliberately broad membership pool for
each candidate cluster.

Each proposed member includes:

```yaml
source_id: ...
proposed_role: core | supporting | mechanism | boundary | practitioner | partial
relevance_reason: ...
```

The roles guide synthesis but do not predetermine the final verdict.

Remove the existing instruction limiting a cluster to four context or bridge
members. Remove exclusive or near-exclusive assignment pressure. Do not impose
an arbitrary final membership count.

The planner must:

- consider sources already assigned to other clusters;
- consider mapped important citations from candidate members;
- include directly relevant methodological and practitioner sources;
- include partial notes when their available content makes a bounded
  contribution;
- distinguish candidate breadth from final cluster membership; and
- leave unrelated sources unclustered without a permanent `not_a_fit` status.

For the ceasefire/peacekeeping regression fixture, planning must consider:

- Fortna 2018;
- Fortna 2007;
- UN DPPA–MSU ceasefire guidance;
- Gromes 2019;
- Gilligan and Stedman 2003;
- Quinn and Mason 2007;
- Doyle and Sambanis;
- Almuslem 2020;
- Mross and Fiedler 2022;
- Levin and Miodownik; and
- the Brahimi Report.

Acceptance does not require retaining all eleven. It requires that the
immutable planner/writer input receipt proves each was considered and that
every retained member receives a source-specific contribution.

### 6.14 Give the cluster writer every candidate atomic note

For a normally sized cluster, send:

- the proposed organizing problem;
- every complete candidate atomic note;
- current accepted relationships among candidates;
- mapped literature positions among candidates;
- important known-unmapped/absent literature positions;
- existing neighboring-cluster cards; and
- partial-document boundaries.

Do not restrict the writer to planning anchors or selected extracts.

Do not impose the old:

- one contribution per core source;
- three central findings;
- two entries per section; or
- approximately 4,500-token synthesis instruction.

Use the provider's measured context and output capacities.

If candidate atomic notes exceed the safe context budget:

- ask the probabilistic planner to divide the proposed family into coherent
  subproblems;
- synthesize those subclusters separately; and
- link them as neighbors.

Do not silently omit members or synthesize a purported whole cluster from an
unreported subset.

The cluster writer contract must return:

- `retained_members`, each with a specific contribution; and
- optional `material_exclusions` for candidates whose rejection establishes an
  important intellectual boundary.

The pipeline already knows the proposed candidate IDs from the immutable input
receipt and computes unretained IDs locally. Do not require the model to emit
one rejection row or explanation for every unretained candidate. Missing
exclusion prose must not warn, park, retry, or fail the cluster.

Apply the same conservative local envelope recovery used for the shared plan
to cluster-writer output. Publish exactly one recoverable, complete object;
reject ambiguous or truncated output; preserve the raw response; make no
semantic repair call; and retain the last valid cluster with
`refresh_pending` if recovery fails.

### 6.15 Narrow statistical plain-English interpretation

Advance cluster synthesis prompt 30 by replacing, not appending to, the
overbroad instruction.

Change normalization as well as the prompt. In
`validate_streamlined_cluster_synthesis`, remove the fallback that copies an
ordinary finding into `plain_english_meaning`, and remove the equivalent
evidence-thread fallback that copies its synthesis. In
`_streamlined_cluster_markdown`, render an interpretation only when the model
supplied one for a genuinely technical statistical result.

Rules:

- ordinary prose receives no second plain-English restatement;
- qualitative findings, theories, examples, historical analogies, and
  practitioner recommendations are written clearly once;
- an intuitive percentage such as `11% versus 50%` needs no automatic
  “per 100” translation;
- technical interpretation is optional and appears only when a coefficient,
  odds ratio, hazard ratio, interaction, marginal effect, QCA score,
  confidence interval, p-value, or model-derived quantity would otherwise be
  difficult to understand;
- preserve the raw result and scale when relevant;
- explain practical direction, comparison, and uncertainty;
- distinguish percentage points from relative change;
- distinguish odds, hazards, risks, and probabilities;
- do not convert a coefficient or interaction into a percentage without the
  required reported quantities;
- do not describe a p-value as effect size or hypothesis probability; and
- if no defensible intuitive conversion exists, explain only the direction,
  comparison, and uncertainty.

Keep the successful cluster-wide self-review from v0.18–v0.20. In the same
writer call, check every use of:

- `all`, `none`, `every`, `most`, and other universal or majority claims;
- consensus and disagreement;
- `includes`, `excludes`, and literature-boundary claims;
- quantitative denominators and comparison baselines; and
- claims drawing on partial-document members.

Use named-source wording when evidence is heterogeneous. Do not claim “every”
or “no” case unless the retained cases establishing that denominator are
enumerated. This remains model self-review, not a local semantic gate or
second call.

Use one integrated finding, for example:

> Restrained peacekeeping appeared in a QCA path with consistency 1.00. Every
> case matching that combination maintained peace in this sample, although
> this does not establish that the result will generalize.

Do not add:

> In plain English: restrained peacekeeping worked in these cases.

### 6.16 Group cluster findings by source

Keep the structured synthesis model source-aware, then change the Markdown
renderer so it groups findings by `source_id` within each line of inquiry.

Target structure:

```markdown
# Cluster title

## Organizing problem

## Bottom line

## Main lines of inquiry

### Ceasefire agreement design

#### [[Fortna2018 ...|Fortna 2018 — Peace Time]]

*Method: Duration analysis of 48 interstate ceasefires, with case studies.*

- Stronger agreements were associated with more durable peace.
- Particularly useful provisions included demilitarized zones, monitoring,
  guarantees, and dispute-resolution commissions.
- Arms-control provisions alone were not associated with greater durability.

#### [[UNDPPA-MSU2022 ...|UN DPPA–MSU 2022 — Guidance]]

*Knowledge basis: Practitioner guidance from UN mediation experience.*

- Recommends clear objectives, force-separation arrangements, civilian
  protections, monitoring, DDR, and communication mechanisms.
- These are recommendations, not causal findings.

## How the findings relate

## Important cited works not yet mapped

## Limits and boundaries

## Members
```

Rendering rules:

- default each retained source to one primary source block in the cluster;
- several findings become bullets beneath that source;
- method or knowledge basis appears once for that source in that section;
- locators appear on the relevant finding without repeating the source link;
- if a source materially informs another line of inquiry, use a compact
  cross-reference to its primary block rather than repeating its full heading,
  method, link, and findings;
- a technical result and its interpretation appear together;
- the line-of-inquiry synthesis explains cross-source meaning rather than
  repeating every bullet;
- `How the findings relate` contains only genuine support, qualification,
  disagreement, sequence, or boundary analysis;
- keep one compact member list for navigation and reciprocity; and
- frontmatter remains machine-readable without affecting prose length.

Every rendered source, relationship, cluster, and cross-reference wikilink
must resolve after filename normalization. Preserve the local apostrophe and
punctuation normalization that repaired the v0.15–v0.18 projection failures.

### 6.17 Add an attributed unmapped-literature section

Add the optional cluster section `Important cited works not yet mapped`.

Include only a cited work that materially affects:

- the origin of a central argument;
- a claimed debate;
- a method or dataset lineage;
- a cluster boundary;
- an unresolved mechanism; or
- the cluster's acquisition priorities.

Each entry contains:

- cited work identity;
- the mapped member that invokes it;
- the mapped author's characterization;
- why it matters to this cluster; and
- status: `known_zotero_unmapped` or `not_in_snapshot`.

Do not:

- list routine citations;
- repeat the entire missing-source ledger;
- describe the cited work as independently verified;
- include statistics from a work that has not been inspected; or
- count it toward cluster membership, consensus, or evidence-base totals.

### 6.18 Harmonize virtual navigation with literature families

Keep Zotero collection indexes and human Markdown indexes.

When flat complete-index planning succeeds:

- use its broad literature-family assignments as optional virtual navigation
  families;
- persist membership deterministically from the returned family plan;
- allow overlapping family membership;
- give each source one canonical note;
- distinguish a broad navigation family from an accepted cluster; and
- keep unassigned sources in a neutral list rather than inventing a generic
  topic.

Do not use the current 85 fragmented virtual-topic labels and 86 shards as the
default discovery route when the complete index fits.

Existing facet-derived shards remain available only for:

- overflow routing;
- deterministic local browsing;
- recovery when global family planning is unavailable; and
- backward compatibility.

Filter empty Zotero collection cards from the mapped-source planning view.
Do not delete or alter Zotero collections.

### 6.19 Incremental library growth

Use the same `plan_literature_families` capability in two modes:

- `initial_global` receives the complete lean index and establishes the
  accepted family plan; and
- `incremental_patch` receives only new/changed lean entries, stable existing
  family cards, resolved citation changes, and a bounded set of neighboring
  entries selected from the complete index.

The accepted family plan is the frozen baseline for an incremental update.
The patch result contains family additions, removals, membership changes, and
affected discovery jobs; it does not regenerate, rename, or reshuffle
unaffected families. Run a full global replan only when explicitly requested
or when the family-planner prompt, policy, or schema changes.

When new sources or a new collection are mapped later:

1. insert their canonical records into the existing catalogue;
2. reconcile identity aliases and duplicates;
3. resolve old literature positions that now match the new notes;
4. add deterministic citation links in both navigation directions;
5. route the new lean entries against the existing family cards and complete
   existing lean index when that incremental packet fits;
6. otherwise route them against context-bounded existing chunks;
7. adjudicate only the selected new-to-old and new-to-new pairs using complete
   notes;
8. apply only the returned family patch and reconsider clusters touched by new
   sources, resolved citations, or changed current relationships; and
9. update or remove satisfied acquisition recommendations.

Do not rescan or regenerate every existing atomic note.

A newly mapped cited work triggers reconsideration of:

- the older notes that cited it;
- current relationships involving those notes; and
- clusters whose unmapped-literature section referenced it.

The incremental receipt must prove union equivalence for the affected
neighborhood: applying a patch to the old state must match a fresh build of the
same fixture for new/changed sources, while unaffected family IDs, cluster
hashes, and mtimes remain stable.

### 6.20 Preserve acyclic fingerprints and exact replay

Discovery fingerprints depend only on upstream semantic inputs:

- complete lean index records;
- requested collection-pair membership;
- mapped citation/literature-position pairs;
- human relationships;
- provider/model/reasoning identity;
- prompt identity;
- discovery policy; and
- context-budget path.

The initial-global plan fingerprint contains the complete lean-index hash. An
incremental-patch fingerprint instead contains the accepted baseline family
plan hash, new/changed entry hashes, resolved citation changes, selected
neighbor-entry hashes, comparison-collection inputs, and the same
provider/prompt/policy identities. Merely adding one source must not force a
new initial-global plan.

They do not depend on:

- generated cluster prose;
- managed Markdown;
- graph-neighbor projections;
- active cluster summaries;
- timestamps;
- ledger order; or
- global revision counters unrelated to the selected records.

Relationship-decision fingerprints depend on:

- the two atomic-note/profile hashes;
- pair job and discovery provenance;
- provider/model/reasoning identity;
- relationship prompt and contract; and
- human relationship state for that pair.

The relationship normalization version is a local parser identity, not a
reason to buy the same provider response again. When it changes, reparse the
preserved raw provider result, validate it under the current contract, and
rewrite only normalized machine state whose bytes changed. Provider, model,
prompt, source-note, pair-job, or policy changes remain semantic invalidators.

Cluster fingerprints depend on:

- candidate member note hashes;
- current accepted relationship connection hashes among those members;
- relevant literature-position records;
- partial-source boundaries;
- cluster organizing problem;
- cluster prompt/model identity; and
- stable neighboring family cards from the shared global plan.

Machine-generated negative-pair memory is a downstream adjudication output.
Apply it as a local post-discovery filter and exclude it from provider
selection fingerprints.

Remove machine `no_relationship`/negative-pair state from the top-level
`_build_map_semantic_fingerprint` and every upstream build receipt as well.
Keep only genuine upstream inputs there: source/profile hashes, citations,
human relationships, explicit comparison collections, provider/model,
prompts, policy, and source identities. A first build that creates a new
`no_relationship` must therefore remain an exact no-op on immediate replay.

A source moving into or out of an explicitly compared collection changes
bridge eligibility and invalidates the affected discovery plan. A collection
rename, hierarchy-only presentation edit, or equivalent view-only change does
not. Tests must distinguish these cases rather than promising zero calls for
every collection-membership change.

Projection fingerprints never invalidate semantic work.

An identical replay must return before any provider call or file write.

Use one relationship-event identity function for both canonical registry
history and the run-scoped ledger.

For adjudication outcomes, the stable event identity is based primarily on:

- pair job ID;
- decision/input identity; and
- canonical pair.

Mutable status text, parking reason, completion timestamp, or locally
normalized payload must not create a second event for the same attempt.
Instead, semantic payload changes update that event's payload history. Separate
retirement/supersession events use the stable connection ID and retirement
lineage.

The run ledger contains only jobs scheduled or resolved for that run. It must
not import every unrelated retired relation or global `no_relationship`
decision from the canonical registry. Both stores must nevertheless record
the same stable event ID for every job that belongs to the run.

### 6.21 Concurrency, context, and call accounting

Use existing `provider_concurrency=auto`.

Independent work runs concurrently:

- family discovery jobs;
- adaptive relationship adjudication packets; and
- cluster writers.

Sequential boundaries remain:

1. catalogue and citation reconciliation;
2. global or chunked planning;
3. candidate discovery;
4. relationship adjudication and one registry commit;
5. cluster synthesis;
6. projection; and
7. replay receipt.

Two jobs must not mutate the registry simultaneously. Provider calls return
immutable results; one local commit merges them.

For the frozen 193-note evaluation, target:

| Stage | Expected maximum |
|---|---:|
| Complete-index global planning | 1 |
| Disjoint family discovery | 3 |
| Full-note relationship adjudication | 5 |
| Cluster writers | 18 |
| Shared transport/overflow reserve | 3 |
| **Hard ceiling** | **30** |

The normal single transport retry consumes the same hard ceiling. Semantic,
contract, and advisory failures receive no paid retry.

The planner may propose more than 18 valid clusters. If the ceiling cannot
schedule every writer, publish completed independent clusters, preserve the
last valid versions of affected existing clusters, and mark only unscheduled
clusters pending. Do not turn the entire map partial. Report the measured
tradeoff rather than silently raising the ceiling. For the frozen acceptance
run, any planned cluster left unscheduled because the ceiling was reached
produces a qualified cluster/pipeline verdict even though completed artifacts
remain valid and visible.

Use:

- high reasoning for global planning and family discovery;
- max reasoning for relationship adjudication and cluster synthesis when the
  selected provider supports it;
- output ceilings appropriate to each stage rather than one global maximum;
- adaptive relationship packets that may contain up to 30 pair jobs but
  shrink based on complete-note context size; and
- stable source-document ordering to support provider prefix caching.

### 6.22 Smallest root-cause implementation path

Integrate into the current modules rather than creating parallel v0.21
subsystems:

1. **Models and prompt**
   - extend `models.py` around `RelationshipPairJob` and
     `RelationshipDecision` with the internal pair-outcome/connection types;
   - advance the built-in relationship contract identity; and
   - replace `_relationship_adjudication_system_prompt` in `readers.py` with
     the concise v11 contract.
2. **One shared normalizer**
   - extend `_normalize_provider_decision_row`,
     `ingest_relationship_decision_batch`, and the reader response adapter;
   - make the same normalizer accept live v8 and saved v7 rows; and
   - remove all current-code assumptions that endpoint anchor arrays contain
     an element at index zero.
3. **Per-job orchestration**
   - change `_run_relationship_reasoning` so provider rows validate before
     run/global cache completion;
   - change `_relationship_transport_context` so v8 carries deduplicated
     complete notes without anchor-copy tasks; and
   - preserve raw and normalized results separately.
4. **Registry and projection**
   - refactor `persist_relationship_registry` around a canonical-pair effective
     index and an append-only attempt/history stream;
   - make `projected_related_links` consume stable connection IDs; and
   - retain the existing managed-block projection path in
     `_project_atomic_graph`/`update_note_graph`.
5. **Migration and replay**
   - call one registry-payload migration helper from `migrate_workspace` and
     normal registry reads;
   - remove the schema-6 hard-code from the general metadata migration;
   - remove machine negative-pair state from
     `_build_map_semantic_fingerprint`; and
   - retain `_reusable_build_map_manifest` as the zero-call, zero-write
     top-level no-op boundary.
6. **Ledger**
   - replace the two divergent relationship-event ID implementations used by
     the registry and `_write_relationship_run_ledger` with one helper; and
   - keep the run ledger run-scoped while the registry preserves global
     history.
7. **Index and cluster integration**
   - extend the existing catalogue/index renderers in `indexes.py`;
   - feed the validated shared family plan into the existing relationship and
     literature-report paths; and
   - update the current cluster normalizer and Markdown renderer rather than
     adding a second cluster format pipeline.

Complete these boundaries in that order. Prompt changes before cache-state
repair would reproduce false completed rows; projection changes before
registry singularity would merely display inconsistent state more neatly.

## 7. Interfaces and compatibility

Minimum public change:

- optional `comparison_collection_keys` on `LiteratureMapRequest` and
  `build_map`;
- repeatable optional CLI `--compare-collection KEY`;
- no source or mapping API break;
- no new third-party dependency; and
- no new provider requirement.

Internal additions:

- optional built-in `plan_literature_families` capability with custom-reasoner
  fallback;
- `relationship-decision-v8`;
- relationship registry schema `7`;
- source catalogue schema `6`;
- a compact in-memory discovery projection;
- flat-versus-chunked planning receipts;
- one current-pair decision index;
- optional secondary relationship types and multiple propositions inside one
  pair decision; and
- attributed cluster cited-literature context.

Existing configuration controls remain:

- model/provider;
- reasoning effort;
- context fraction;
- provider concurrency;
- literature deadline;
- synthesis-call ceiling; and
- explicit terminal retry.

The new comparison field defaults to empty and is fingerprinted only when
used. Existing callers and global builds retain their current behavior.

Do not add:

- per-stage public budgets;
- a public character-cap setting;
- a required tokenizer dependency;
- a new graph store;
- a new missing-source database; or
- a harness-native orchestration dependency.

## 8. Migration

Migration from v0.20/schema 1.15 is local, lazy, and idempotent.

It must:

- accept existing atomic notes, profiles, catalogue records, and citations;
- preserve source and note IDs;
- preserve every human-authored link;
- retain v0.20 active cluster notes as the last valid visible versions until
  v0.21 replacements succeed;
- normalize saved relationship shorthand locally;
- convert legacy accepted relationship rows into historical pair decisions;
- create one current-pair index without deciding scholarship
  deterministically;
- retire ambiguous conflicting machine current state pending prompt-11
  readjudication;
- preserve raw provider outputs and events;
- rebuild managed projections only after a successful semantic commit; and
- write nothing when the migrated bytes are already current.

Do not automatically rewrite successful atomic-note prose or add the new
cluster format to a cluster that has not been regenerated.

## 9. Tests

### 9.1 Lean index and context tests

Add tests proving:

- every eligible source appears exactly once in the discovery projection;
- duplicate Zotero aliases collapse into one row while retaining all keys and
  collection memberships;
- the existing thesis and method strings are preserved byte-for-byte;
- a fixture longer than the old 360/220-character display limits remains
  complete;
- no local character slicing is applied to either field;
- relationship, cluster, anchor, and revision state are absent;
- requested collection membership is compact and correct;
- the 193-source fixture chooses `flat_complete_index`;
- a packet just inside the provider budget remains flat;
- a packet just outside the budget selects chunked reconciliation;
- a single oversized source record is isolated rather than truncated;
- unchanged input produces byte-identical model context; and
- output/reserved-token headroom is included in admission.

For synthetic 5,324- and 10,000-profile libraries, seed known bridge families
at the beginning, middle, and end. Require the flat or overflow planner to
recover all three positions, and record planning output length,
`finish_reason`, and truncation status. “The request fit” is not sufficient if
the response silently loses the middle of the library.

### 9.2 Chunking and reconciliation tests

Add tests proving:

- chunks split only between complete source records;
- every source appears in exactly one full chunk;
- the global spine does not regenerate thesis or method text;
- within-A, within-B, and selected A–B families remain discoverable;
- cross-chunk jobs can recover a known bridge whose endpoints are separated;
- reconciliation schedules a bounded family subset rather than all chunk
  pairs;
- an explicitly requested collection pair remains represented across chunks;
- family summaries reconcile without duplicating source records;
- empty collection cards are excluded from model context;
- chunk order is stable; and
- chunking is not called when the complete index fits.

### 9.3 Requested-collection and discovery tests

Add tests proving:

- repeatable `--compare-collection` values reach
  `comparison_collection_keys`;
- two explicitly requested collections always create a direct comparison job;
- no comparison keys leaves collection-pair selection probabilistic;
- routed auxiliary collections cannot replace that job;
- discovery families are distinct and respect their pair quotas;
- the same candidate returned by several jobs is adjudicated once;
- discovery reasons and provenance merge deterministically;
- bridge-only jobs reject same-collection pairs;
- broad contextual candidates remain permitted;
- purely superficial pairs may be omitted;
- mapped citations always remain visible links but enter adjudication only
  when probabilistically selected within budget;
- negative-pair memory prevents unchanged reconsideration; and
- negative-pair memory changes do not invalidate cached provider discovery;
- benchmark fixtures never enter model-visible prompts.

Add shared-planner integration tests proving:

- one valid plan receipt feeds both relationship discovery and cluster
  candidate work;
- `build_literature_report` makes no second global planning call;
- an unchanged plan checkpoint is reused;
- changing the lean index or an explicit collection-pair membership
  invalidates the plan;
- a collection rename or hierarchy-only view edit does not; and
- a custom reasoner without the optional capability follows the legacy path.

### 9.4 Citation tests

Add tests proving:

- a one-way mapped citation publishes without two endpoint anchors;
- the cited note receives `cited_by`, not a false reciprocal `cites`;
- Obsidian projection links both notes for navigation;
- `no_relationship` does not erase a deterministic citation edge;
- mapped, known-unmapped, absent, and ambiguous statuses remain distinct;
- Berg retains Walter and Hegre links;
- a later mapped source resolves an older unresolved citation; and
- ambiguous or title-fragment matches never auto-link.

### 9.5 Relationship contract tests

Add fixtures for:

- one primary relationship;
- a primary `undermines` relationship with secondary `contrasts`;
- a primary `qualifies` relationship with a bounded secondary contrast;
- two distinct propositions connecting one pair;
- explicit citation with no additional intellectual relationship;
- generic contextual wording;
- missing source-A basis;
- missing source-B basis;
- correct and reversed actor/reference directions;
- unsupported `extends` lineage;
- dataset reuse without automatic `supports`;
- valid saved v7 shorthand;
- v7 empty anchor arrays;
- a live v8 fenced batch;
- a live v8 batch with one valid and one malformed sibling;
- recognized live decision shorthand and harmless scalar/list variants;
- an ambiguous live envelope;
- malformed or unknown endpoints; and
- no-relationship decisions.

Require:

- valid v8 decisions publish without anchor IDs;
- every published inferred connection has both endpoint bases;
- a missing basis parks only that connection without retry;
- valid sibling jobs survive a malformed row in the same batch;
- unambiguous live envelopes recover locally without a provider call;
- ambiguous live envelopes preserve raw output and park only affected jobs;
- optional anchor provenance does not control publication;
- structurally impossible decisions park without retry;
- advisory warnings do not change the model's intellectual type;
- local code derives inverse labels; and
- atomic-note semantic prose, user content, and non-owned frontmatter remain
  byte-identical; only declared managed relationship/cluster blocks and
  machine-owned frontmatter keys may change.

Add orchestration fixtures proving:

- a raw provider row is not marked completed before it validates;
- valid shorthand is normalized before the run and global caches become
  completed;
- a missing endpoint basis leaves preserved raw output and parked run/global
  cache state;
- a valid sibling completes while a malformed sibling parks;
- cache replay reparses preserved raw output under the current normalizer with
  zero provider calls;
- local reparse never converts an unvalidated cached row into a false
  completed result; and
- normalization-only recovery does not invalidate source, discovery, or
  provider checkpoints.

Snapshot semantic-body hashes separately from managed-projection hashes so a
legitimate graph projection does not look like atomic-note meaning drift.

### 9.6 Registry tests

Reproduce the v0.20 failures:

- identical accepted relation ID recorded under two catalogue revisions;
- one pair with `contrasts` and `undermines` active from separate runs;
- one pair with `qualifies` and `contrasts` active;
- a later `no_relationship`;
- human and machine links on one pair; and
- a current record containing an intentional primary and secondary label;
- a current decision containing two genuinely separate proposition
  connections;
- a malformed refresh after a valid accepted decision; and
- a malformed refresh after a valid `no_relationship`.

Require:

- exactly one current effective machine pair decision;
- all intentional connections project;
- secondary labels remain explanation metadata rather than duplicate edges;
- two proposition connections do not supersede one another;
- two distinct propositions with the same primary relationship type retain
  different stable connection IDs and both project;
- parked refresh attempts remain history and set `refresh_pending`;
- parked refresh attempts do not retire the last effective decision;
- zero-call schema-6 migration leaves every previously valid linked pair with
  one visible provisional or current effective decision;
- prior machine decisions become inactive history;
- exact duplicates collapse;
- human links remain active;
- canonical history and the run ledger contain the same stable event IDs for
  every scheduled job;
- changing a parking explanation or normalizing a formerly parked response
  updates one attempt's payload history instead of appending a duplicate
  attempt event;
- the run ledger contains no unrelated global relationship history;
- resume merges rather than overwrites run-ledger events;
- migration is idempotent;
- opening a schema-7 workspace through the general metadata migration cannot
  downgrade it to schema 6;
- replay adds no event; and
- clusters see only current accepted machine connections.

### 9.7 Cluster candidate tests

Add tests proving:

- one source may appear in several proposed clusters;
- sources already assigned elsewhere remain eligible;
- no four-context-source ceiling exists;
- candidate roles do not dictate final membership;
- partial notes can contribute bounded theory, method, or context;
- unmapped cited works cannot become members;
- an unclustered source remains future-eligible; and
- a malformed member row affects only that cluster;
- the immutable input receipt accounts for every proposed candidate;
- optional material exclusions preserve meaningful cluster boundaries; and
- absent per-candidate rejection prose causes no warning, retry, or failure.

The ceasefire fixture must show that all eleven named candidate works are
considered, while permitting evidence-grounded retention or rejection.

### 9.8 Cited-literature cluster tests

Add fixtures for:

- an important mapped cited work;
- an important Zotero-known unmapped work;
- an important absent work;
- an ambiguous citation;
- a routine citation that should not appear;
- two members citing the same missing work; and
- a previously missing work mapped later.

Require:

- mapped citations are available to the planner but only selected ones add a
  full-note candidate;
- known-unmapped and absent works appear once with attribution and status;
- ambiguous and routine citations are omitted;
- cited-only claims are attributed to the mapped author;
- cited-only works do not count toward evidence, consensus, or membership;
- duplicate recommendations merge in the existing ledger; and
- later mapping refreshes only affected notes, relationships, and clusters.

### 9.9 Cluster synthesis and rendering tests

Add golden Markdown fixtures proving:

- one primary full source heading/link per source per cluster;
- several findings render beneath that source;
- method/knowledge basis appears once per source per section;
- secondary lines of inquiry use compact cross-references rather than repeated
  full source blocks;
- locators do not repeat the source wikilink;
- qualitative findings receive no redundant plain-English line;
- intuitive percentages receive no automatic “per 100” restatement;
- a coefficient, hazard ratio, interaction, QCA result, or p-value receives an
  interpretation only when needed;
- the raw statistic and scale are preserved;
- cluster normalization leaves absent plain-English fields absent rather than
  copying the ordinary finding or synthesis into them;
- percentage points, relative change, odds, hazards, risks, and probabilities
  remain distinct;
- line-of-inquiry synthesis does not duplicate every bullet;
- `How the findings relate` contains actual cross-source analysis;
- the compact member list remains reciprocal; and
- the frontmatter source list remains valid.

Add semantic fixtures reproducing the prior Gromes/Burundi and Svensson
universal/bias overclaims. Require named-source, denominator-aware wording.
Add Paris, Suhrke/Samset, and Fortna apostrophe/punctuation projection fixtures
and require every generated wikilink target to resolve.

### 9.10 Incremental and replay tests

Require:

- a new source compares against the existing complete index when it fits;
- an overflow library compares the new source against bounded chunks;
- an incremental source uses `incremental_patch` rather than invalidating the
  initial-global plan;
- unaffected family IDs remain stable;
- applying the patch equals a fresh union build for the affected fixture;
- old citations resolve without rescanning old source text;
- only incident relationships are reconsidered;
- only affected clusters refresh;
- an unrelated cluster remains byte-identical;
- a collection rename or hierarchy-only presentation change makes zero
  provider calls;
- moving a source into or out of an explicitly compared collection
  invalidates only affected discovery;
- creating a new machine `no_relationship` does not change the top-level
  semantic receipt or trigger calls on immediate replay;
- a new run ID reuses unchanged semantic work;
- an identical replay makes zero calls and zero writes; and
- prompt/model/policy changes invalidate only their downstream layers.

Add global-plan and cluster-writer envelope fixtures for fenced,
prose-wrapped, single-object-list, ambiguous, and truncated responses.
Require unique valid envelopes to recover locally, raw responses to be
preserved, ambiguous/truncated responses to make no repair call, and the last
valid family plan, effective relationship graph, and cluster map to remain
visible. A first-build planner failure must return an accounted terminal
partial state rather than a successful empty graph.

Add prompt snapshot tests proving the rewritten relationship and cluster
prompts contain no obsolete mandatory anchor-ID requirement and no universal
plain-English-restatement instruction.

Run the full existing suite after all focused tests.

## 10. Staged implementation and validation

### Stage 1 — Registry singularity and local migration

Implement and test:

- schema-7 pair state;
- v0.20 migration;
- shorthand normalization;
- stable connection and pair-attempt event identities;
- retirement lineage; and
- effective-current-only projection with parked refresh history.

Validate on an isolated v0.20 workspace clone with zero provider calls.

Stop if any human link is removed or any pair retains accidental multiple
current decisions.

### Stage 2 — Lean complete-index planning

Implement:

- in-memory discovery projection;
- measured flat-path admission;
- requested-collection coverage;
- global family plan; and
- chunk-and-reconcile overflow fallback.

Run synthetic:

- 193-source;
- complete current Zotero-size projection;
- 10,000-profile flat-library; and
- deliberately overflowed cross-chunk fixtures.

Stop if entries are truncated, duplicated, or cross-chunk fixtures become
undiscoverable.

### Stage 3 — Relationship contract and discovery

Implement:

- disjoint family jobs;
- v8 minimal relationship output;
- live per-job normalization before cache completion;
- raw-provider preservation and zero-call cache reparsing;
- optional secondary types/propositions;
- mandatory two-source bases with optional anchor IDs; and
- one relationship-stage registry commit.

Run a zero-source-call graph smoke test over frozen v0.20 notes.

Stop if explicit citation links disappear, current-pair singularity breaks,
or complete atomic notes are not supplied.

### Stage 4 — Cluster planning, cited literature, and rendering

Implement:

- overlapping broad candidate pools;
- probabilistically selected mapped cited-work expansion;
- attributed missing cited literature;
- full-note cluster context;
- narrowed statistical interpretation; and
- source-grouped Markdown.

Run the ceasefire/peacekeeping fixture before any full graph call.

Stop if:

- ordinary prose receives redundant interpretations;
- source links repeat per finding;
- cited-only works become independent evidence;
- Fortna's available-content boundary is lost; or
- directly relevant mapped candidates are not considered.

### Stage 5 — Full local gate

Before paid testing:

- inspect the diff for duplicated systems and obsolete hierarchy-first paths;
- remove code made obsolete by the new default;
- run focused tests;
- run the full test suite;
- build wheel and source distribution;
- run `doctor`;
- require a clean tracked tree;
- exclude `.DS_Store`; and
- commit the implementation.

## 11. Targeted v0.21 evaluation

### 11.1 Workspace

Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v021-graph-evaluation-20260730`

Use an isolated clone of the clean frozen v0.20 source layer:

- 193 published notes;
- 175 analytical/partial notes;
- 18 limited notes;
- unchanged profiles and source hashes; and
- no source-generation capability enabled.

All 193 appear in the lean index for identity, navigation, citation
resolution, and planning context. Only the 175 analytical/partial notes are
eligible for substantive inferred relationships and cluster findings; limited
notes may supply explicitly bounded bibliographic or contextual information
but never unsupported full-document claims.

Run ID:

`eval-global-v021-graph-20260730`

### 11.2 Provider configuration

Use:

- DeepSeek `deepseek-v4-flash` for direct comparison with v0.20;
- high reasoning for global planning and discovery;
- max reasoning for relationships and clusters when supported;
- `--compare-collection B887A4Q8`;
- `--compare-collection D2XT9ZU9`;
- `provider_concurrency=auto`;
- no semantic retry;
- normal single transport retry;
- 7,200-second literature deadline; and
- 30-call cumulative ceiling.

Do not expose benchmark pairs to the model.

### 11.3 Index-path evaluation

Record:

- full rich catalogue size;
- lean discovery projection size;
- estimated/provider-tokenized context size;
- flat-path admission calculation;
- prompt and output reserve;
- source count and one-record-per-source integrity;
- selected literature families;
- explicitly requested collection coverage; and
- whether any overflow fallback ran.

Acceptance:

- all 193 published records appear exactly once in the navigation/planning
  index, with evidence eligibility preserved;
- only the 175 analytical/partial notes enter substantive adjudication or
  independent cluster evidence;
- thesis and method are complete and untruncated;
- the 193-source run uses one global flat-index planning call;
- Mediation and Relapse receive a direct comparison route;
- no generated graph or cluster state appears in upstream context; and
- the complete current Zotero-size and 10,000-profile simulations select a
  safe, fully accounted flat or chunked path.

### 11.4 Bridge evaluation

Reuse the frozen 40-pair benchmark.

Measure:

- raw candidate recall;
- candidate recall after mapped-citation injection;
- useful final recall;
- direct-only final recall;
- family diversity;
- endpoint coverage;
- unique candidate count;
- duplicate rate;
- requested collection-pair coverage;
- losses through local filtering;
- losses through adjudication; and
- a deterministic 20-pair non-benchmark plausibility sample.

Acceptance:

- at least 28/40 benchmark pairs reach candidate adjudication;
- at least 28/40 become useful direct or contextual links;
- at least 70% of non-benchmark candidates are worth full-note examination;
- no same-folder pair enters through a bridge-only job;
- no valid benchmark pair is lost through deterministic filtering,
  contract normalization, or persistence;
- requested Mediation–Relapse comparison is present; and
- candidate jobs cover several distinct bridge families.

Report benchmark recall as a controlled diagnostic, not an estimate of all
possible library relationships.

### 11.5 Relationship evaluation

Audit:

- every cross-folder relationship;
- every final benchmark relationship;
- every decision with secondary types;
- every decision containing two propositions;
- every migrated v0.20 duplicate pair;
- every advisory warning;
- every parked decision; and
- a deterministic same-folder sample sufficient to reach 100 direct
  relationships when available.

Read both complete atomic notes before scoring.

Measure:

- primary type and direction;
- secondary-type usefulness;
- proposition accuracy;
- source-A basis;
- source-B basis;
- visible rationale;
- contextual usefulness;
- explicit citation recall;
- current-pair singularity;
- history preservation; and
- reciprocal projection.

Acceptance:

- at least 85% exact primary type/direction;
- at least 85% fully source-grounded direct relationships;
- 100% of published inferred connections contain both source-specific bases;
- at least 98% of current-fingerprint pair jobs receiving a provider response
  normalize into valid `relationship` or `no_relationship` decisions;
- at least 80% contextual usefulness;
- 100% explicit mapped citation recall;
- 100% of current machine pairs represented by exactly one current effective
  adjudication record;
- 100% of intentional connections from that record projected;
- zero relationship parked solely for missing anchor IDs;
- zero valid prior relationship erased by a parked refresh;
- 100% completed-or-parked pair accounting;
- 100% agreement between canonical pair-attempt event IDs and run-ledger event
  IDs;
- 100% reciprocal navigational projection; and
- zero human relationship retired.

The Suhrke/Samset–Walter–Collier debate must remain linked and should be
reported as a representative success.

### 11.6 Cluster evaluation

Audit:

- every mixed-literature cluster;
- the ceasefire/peacekeeping cluster;
- every cluster containing a partial note;
- the eight largest remaining clusters, or every cluster when there are at
  most 20;
- every cluster using an unmapped cited work; and
- every cluster containing a statistical-sample source.

Score:

- organizing-problem coherence;
- candidate consideration breadth;
- final membership relevance;
- overlapping membership where warranted;
- member-role accuracy;
- source-specific contribution coverage;
- claim support;
- use of mapped cited works;
- attribution of unmapped cited works;
- statistical interpretation;
- debate and boundary accuracy;
- prose repetition; and
- reciprocal links.

Acceptance:

- at least 90% membership relevance;
- a specific contribution for every retained member;
- at least 95% source-specific claim support;
- at least 90% debate and boundary accuracy;
- every proposed candidate is present in the immutable writer-input receipt;
- every mapped cited work used as evidence has its complete note supplied;
- zero cited-only work presented as independently verified;
- zero ordinary qualitative finding given a redundant plain-English
  restatement in the audited clusters;
- no repeated full source heading, method, or findings elsewhere in the same
  cluster;
- secondary uses of a source employ compact cross-references;
- all important technical statistical interpretations accurate in scale and
  direction;
- zero unsupported universal, consensus, denominator, or partial-source
  boundary claim in the audited clusters;
- 100% generated source, relationship, cluster, and cross-reference wikilink
  targets resolve;
- 100% reciprocal membership links;
- no whole-map failure caused by one malformed cluster; and
- zero planned cluster left unscheduled because of budget exhaustion.

Do not impose:

- a cluster-coverage threshold;
- a minimum number of clusters;
- a minimum membership count beyond two coherent sources; or
- a requirement that every considered ceasefire candidate be retained.

For the ceasefire/peacekeeping cluster specifically require:

- all eleven named mapped candidates were considered;
- Fortna 2007's available-content boundary is explicit;
- Quinn/Mason and Doyle/Sambanis are either retained or named as material
  exclusions when rejecting them defines the cluster boundary;
- disarmament literature is represented as evidence, boundary, or an
  attributed acquisition need as appropriate;
- findings are grouped under source headings; and
- statistical explanation appears only where needed.

### 11.7 Incremental convergence exercise

Use an isolated fixture representing a later mapped work that an existing
atomic note currently lists as `known_zotero_unmapped`.

Verify:

- zero existing source calls;
- identity reconciliation;
- old citation resolution;
- new-to-old full-note adjudication only;
- affected-cluster refresh only;
- the old cluster's unmapped-literature entry is removed or converted;
- unrelated cluster hashes and mtimes remain unchanged; and
- the resulting graph matches an equivalent union build for the fixture.

### 11.8 Replay evaluation

Snapshot:

- every generated path;
- content hash;
- nanosecond mtime;
- provider-ledger count;
- planning receipt;
- candidate state;
- current pair index;
- relationship history event count;
- cluster revisions; and
- projection digest.

Replay the identical build.

Require:

- zero provider calls;
- zero file additions or removals;
- zero byte changes;
- zero mtime changes;
- zero new pair or history events;
- zero graph or cluster semantic changes; and
- no source artifact access that changes state.

## 12. Comparison with previous evaluations

Write a v0.11–v0.21 longitudinal table wherever definitions remain
comparable. Use v0.16–v0.21 as the primary relationship-quality comparison
because earlier rubrics and denominators differ.

Keep denominators and incompatible rubrics separate.

### 12.1 Bridge trend

Include:

| Version | Candidate recall | Useful/final recall |
|---|---:|---:|
| v0.11 | N/A | 2/40 |
| v0.12 | 12/40 | 2/40 |
| v0.13 | N/A | 10/40 |
| v0.14 | N/A | 2/40 |
| v0.15 | N/A | 6/40 |
| v0.16 | 5/40 | 5/40 |
| v0.17 | 7/40 | 4/40 |
| v0.18 | 12/40 | 9/40 |
| v0.19 | 7/40 raw; 7/38 eligible | 6/40 raw; 6/38 eligible |
| v0.20 clean | 5/40 after mandatory injection | 2/40 |
| v0.21 | Measure fresh | Measure fresh |

### 12.2 Relationship trend

Include:

| Version | Cross-folder type/direction | All-direct type/direction | Fully grounded direct | Contextual usefulness |
|---|---:|---:|---:|---:|
| v0.16 | 4/6 | N/A | N/A | 5/5 |
| v0.17 | 7/8 | 59/71 | 54/71 | 3/4 |
| v0.18 | 16/20 | 53/63 | 49/63 | 13/15 |
| v0.19 | 3/4 | 24/27 | 16/27 | 18/23 |
| v0.20 clean | 6/6 | 14/16 sampled | 14/16 sampled | 9/9 |
| v0.21 | Measure fresh | Measure fresh | Measure fresh | Measure fresh |

Add:

- structural completion;
- anchor-gate parking;
- current pair singularity;
- explicit citation recall;
- primary/secondary relationship agreement; and
- raw versus projected active row counts.

Summarize v0.11–v0.15 relationship results separately without merging their
older precision rubric into the v0.16–v0.21 table.

### 12.3 Cluster and efficiency trend

Compare:

- cluster count descriptively;
- mixed-cluster count;
- membership relevance;
- claim support;
- debate/boundary accuracy;
- overlapping membership;
- cited-literature use;
- prose repetition;
- statistical interpretation;
- literature calls;
- runtime;
- replay; and
- sources currently unclustered, descriptively only.

Report whether v0.21:

- improves bridge recall;
- preserves direct relationship quality;
- eliminates accidental duplicate current state;
- broadens relevant cluster membership;
- reduces cluster repetition;
- retains exact replay; and
- reduces or increases call complexity.

## 13. Deliverables

Write inside the v0.21 evaluation workspace:

- `evaluation/v021-graph-cluster-comparison.md`;
- `evaluation/metrics.yml`;
- `evaluation/index-context-metrics.yml`;
- `evaluation/planning-families.yml`;
- `evaluation/bridge-metrics.yml`;
- `evaluation/nonbenchmark-candidate-sample.yml`;
- `evaluation/relationship-metrics.yml`;
- `evaluation/current-pair-audit.yml`;
- `evaluation/relationship-history-audit.yml`;
- `evaluation/cluster-metrics.yml`;
- `evaluation/ceasefire-cluster-audit.yml`;
- `evaluation/cited-literature-audit.yml`;
- `evaluation/incremental-convergence.yml`;
- `evaluation/runtime-metrics.yml`;
- `evaluation/replay-metrics.yml`;
- `evaluation/curated-bridge-benchmark.yml`;
- pre- and post-replay snapshots;
- a machine-readable replay diff; and
- a private Obsidian vault exported from the completed, replay-stable map.

Include representative:

- mapped citation;
- direct support or qualification;
- useful contextual relationship;
- intentional primary-plus-secondary relationship;
- mapped cited-work cluster expansion;
- known-unmapped cited-work cluster entry;
- concise statistical interpretation;
- source-grouped cluster section;
- legitimate unclustered source; and
- remaining failure.

## 14. Non-goals

Do not add:

- source regeneration;
- another source or relationship verifier call;
- semantic retries;
- sentence-by-sentence deterministic truth adjudication;
- mandatory relationship evidence-anchor IDs;
- a new graph registry;
- a new acquisition database;
- embeddings or a vector database;
- SQLite without a measured need;
- a desktop app or scheduler;
- automatic Zotero edits;
- a permanent `not_a_fit` source status;
- exclusive cluster membership;
- a cluster-coverage target;
- blind thesis or method truncation;
- model-managed Markdown editing;
- harness-specific orchestration; or
- a new third-party dependency.

## 15. Final acceptance

v0.21 passes only if:

- the frozen source layer is unchanged;
- the complete 193-source lean index is used in one shared planning call that
  feeds both relationships and clusters;
- explicit requested collections are directly compared;
- bridge candidate and useful-final recall both reach at least 70%;
- direct relationship type/direction and grounding remain at least 85%;
- live relationship contract completion reaches at least 98%;
- contextual usefulness reaches at least 80%;
- no relationship is parked solely for missing anchor IDs;
- every published inferred relationship has two source-specific bases;
- every canonical source pair has one coherent current effective machine
  adjudication;
- a parked refresh never erases the last valid decision;
- intentional secondary relationships remain expressible;
- every strong mapped citation is linked directionally;
- cluster membership relevance reaches at least 90%;
- cluster claim support reaches at least 95%;
- cited-but-unmapped works are attributed and never laundered as evidence;
- cluster findings are grouped beneath source links;
- retained sources have one primary full block per cluster, with compact
  cross-references elsewhere;
- ordinary prose is not redundantly translated into plain English;
- statistical explanations preserve scale, direction, and uncertainty;
- overlapping cluster membership is supported;
- the ceasefire cluster considers the identified relevant literature;
- every generated wikilink target resolves;
- no planned cluster remains unscheduled in the acceptance run;
- incremental addition refreshes only affected neighborhoods;
- the literature run remains at or below 30 calls;
- runtime remains comfortably below four hours;
- identical replay makes zero calls and zero writes; and
- the full existing test suite passes.

If bridge recall remains poor after the complete-index path, stop and inspect
the global planning and family-discovery outputs. Do not respond by adding
more deterministic semantic filters, a verifier call, or an automatic source
rerun.

If cluster quality improves but the 30-call ceiling prevents all valid cluster
writers from running, report the measured tradeoff and request explicit
approval before changing the ceiling.

If the complete current Zotero library does not fit the flat context budget,
use the tested chunk-and-reconcile fallback. Do not silently truncate index
content.
