# Auto-Zettelkasten Relationship Graph Improvement Plan

**Status:** Implemented in v0.11; large-folder graph projection qualified, overall acceptance failed
**Date:** 2026-07-26
**Scope:** Atomic-note relationships, graph memory, indexes, incremental refresh, cluster integration, and Obsidian projection
**Foundation:** Auto-Zettelkasten v0.10

## 1. Executive decision

The current Auto-Zettelkasten should remain the foundation. It has the strongest
source-reading, evidence-profile, locator, coverage, checkpoint, and
cluster-validation machinery of the systems reviewed. The relationship layer
should be improved by combining:

- the current system's source fidelity and structured evidence profiles;
- the older Obsidian system's readable typed relationships, cross-literature
  navigation, and maps of content;
- Research OS's compact thesis-and-method source catalogue, structured
  registries, and reciprocal projections; and
- a new probabilistic relationship-selection stage that uses the catalogue and
  existing graph as memory.

The target architecture is deliberately hybrid:

- **Reasoning models make intellectual judgments.** They decide which works
  matter to one another and whether a relationship supports, undermines,
  qualifies, extends, complements, or contrasts.
- **Local deterministic code manages integrity.** It gathers bounded context,
  checks identifiers and evidence references, checkpoints results, prevents
  duplicate work, and projects accepted relationships into Markdown.
- **The relationship registry is canonical.** Models return structured
  relationship records; they never edit atomic-note Markdown.
- **Graph projection is additive and mechanically isolated.** The source
  analysis remains unchanged while a managed graph block and graph-only
  metadata are updated.
- **Indexes use progressive disclosure.** Agents and humans move from a small
  master index to a relevant literature index and only then to selected
  profiles, atomic notes, or frozen source passages.
- **Graph construction completes before cluster synthesis.** A failed cluster
  call cannot erase or prevent atomic-note relationships.

This plan does not add a database, vector service, Obsidian plugin, background
daemon, or filesystem hook. Existing YAML registries, evidence profiles,
subject assignments, checkpoint conventions, and Markdown projection helpers
are sufficient.

## 2. What the system must achieve

### 2.1 Required outcomes

1. Preserve the current atomic-note source fidelity.
2. Connect atomic notes with meaningful, explained, typed relationships.
3. Identify connections both within a literature and across literatures.
4. Link atomic notes to clusters and clusters back to atomic notes.
5. Revisit relevant older notes when a new source enters the library.
6. Avoid loading the whole library into a model context.
7. Avoid one API call per possible source pair.
8. Avoid rewriting atomic-note prose when only relationships change.
9. Preserve valid graph state when a later model call fails.
10. Make unchanged replays provider-call-free and hash-stable.

### 2.2 Non-goals

- Do not compare every source with every other source using a model.
- Do not treat keyword overlap, shared tags, or a shared method as proof of an
  intellectual relationship.
- Do not force every source to have a relationship.
- Do not ask a model to produce or patch Markdown.
- Do not put complete profiles or findings into one master index.
- Do not let one malformed proposed relationship fail the complete graph run.
- Do not silently convert an ambiguous relationship into `supports` or
  `undermines`.
- Do not replace human-curated relationships.

## 3. Evidence from the three system generations

### 3.1 Older and active Obsidian Zettelkasten lineage

The active Obsidian Zettelkasten is an expanded descendant of the older
Literature Review Atomic Notes library. It demonstrates the value of readable
relationship statements and cross-literature links:

- 1,321 ordinary atomic notes;
- 5,085 relationship-section links;
- 1,160 resolved relationship edges crossing top-level literatures;
- useful verbs such as `Builds on`, `Complements`, `Contrasts`, `Supports`,
  and `Foundations for`; and
- folder indexes and 15 cross-theme maps of content.

Its limitations should not be reproduced:

- 216 uncontrolled relation labels;
- only 32.5% directed-edge reciprocity;
- 223 unresolved relationship-section links;
- 2,398 map-to-note links but no explicit atomic-note-to-map links;
- indexes that omit methods and are too large for reliable automated
  comparison; and
- manual batch backfilling rather than a persistent incremental mechanism.

### 3.2 Research OS

Research OS demonstrates useful structured surfaces:

- a source index containing thesis, method, concepts, and possible use;
- a structured relationship registry;
- reciprocal projection into source notes; and
- clusters that discuss findings, disagreements, and methodological fault
  lines.

Its actual graph is too mechanically broad. In the inspected 68-note export,
201 of 363 relationships were `same_method` and 119 were `same_case`. Those
relations are useful retrieval signals, but they do not establish substantive
support, contradiction, or extension.

### 3.3 Current Auto-Zettelkasten

The large-folder evaluation showed strong atomic-note performance:

- critical-fact recall: 120/120;
- sampled substantive-claim support: 185/192;
- numerical accuracy: 210/210; and
- exact locator accuracy: 251/266.

The same audit found eight unsupported causal upgrades and one incorrectly
scoped limited note. Relationship improvements must therefore preserve the
current strengths without treating existing profiles or notes as infallible;
two-sided source anchors and the existing causal-language safeguards remain
necessary.

The system also already stores source-level concepts, theories, mechanisms,
methods, cases, datasets, outcomes, findings, limitations, boundaries, and
evidence anchors in `EvidenceProfile`. It already has typed navigation,
proposition mapping, cluster-family relations, graph hashes, and an idempotent
`update_note_graph` projection helper.

The central defect is sequencing. Navigation is currently constructed only
after cluster synthesis. When cluster synthesis fails, the pipeline returns an
empty graph. A read-only invocation of the existing navigation builder over the
evaluation profiles produced 246 possible navigation relations, including 24
crossing the Mediation and Conflict Relapse collections, but none were
committed.

The implementation should therefore reuse current machinery while changing
which component is allowed to make which decision and when graph state is
committed.

## 4. Target architecture

```text
source extraction
    ↓
atomic note + evidence profile                    provider call, checkpointed
    ↓
catalogue and graph-memory refresh                local, no provider
    ↓
relevant-index selection                          reasoning call when needed
    ↓
relationship-candidate selection                  reasoning call, checkpointed
    ↓
selective evidence-packet assembly                local, no provider
    ↓
relationship adjudication                         reasoning call, checkpointed
    ↓
tolerant record validation                        local, row-level
    ↓
canonical relationship-registry commit            local, atomic
    ↓
reciprocal graph projection                        local, additive
    ↓
cluster proposal and synthesis                     separate provider stages
    ↓
reciprocal atomic-note ↔ cluster projection        local, additive
```

Graph-memory construction and atomic projection must succeed independently of
cluster synthesis.

## 5. Layered index and graph memory

The index is a context-routing system, not a substitute for the notes.

### 5.1 Human master index

`02_source_memory/indexes/INDEX.md` should become a small directory of
literatures rather than a list of every complete profile. It should contain:

- literature or collection name;
- source count;
- one-sentence scope;
- link to the corresponding literature index; and
- links to the most relevant neighboring literatures or clusters.

Target size: approximately 2,000–4,000 tokens for a large library. If the
library grows, the master remains approximately the same size because it lists
shards, not sources.

### 5.2 Literature or topic indexes

Generate bounded Markdown indexes under a stable subdirectory such as:

```text
02_source_memory/indexes/by_literature/
```

Each source entry must contain at least:

- stable source or note ID;
- note wikilink;
- author and year;
- title;
- one-sentence thesis;
- compact method or knowledge-basis label; and
- up to three discriminating facets when useful.

Example:

> **Fortna 2004 — Peace Time.** Thesis: ceasefire provisions can improve peace
> durability by altering incentives and reducing uncertainty. Method:
> cross-national duration analysis. Facets: ceasefire design, monitoring,
> recurrence.

Do not include detailed findings, complete evidence, limitations, all tags, or
long relationship lists. Those belong in the evidence profile and atomic note.

Each shard should remain within an approximate 8,000–12,000-token ceiling. A
shard that exceeds the ceiling should split by a meaningful subliterature, not
by arbitrary alphabetical pages. The model may select multiple shards when a
new source bridges literatures.

### 5.3 Machine-readable catalogue

The machine catalogue should be produced from existing evidence profiles. It
is not a second analytical database. It is a compact projection containing the
same fields exposed in the literature indexes:

- source and note IDs;
- author, year, and title;
- thesis summary;
- method or knowledge-basis summary;
- compact facets;
- current cluster IDs;
- current high-confidence relationship IDs; and
- profile hash.

Existing `02_source_memory/profiles/*.yml` files remain the full structured
records. Existing `subject_tag_assignments.yml` and graph relations remain
retrieval signals. The compact catalogue prevents the candidate selector from
loading all full profiles.

### 5.4 Cluster catalogue

Maintain a compact machine and Markdown catalogue of admitted clusters:

- cluster ID and title;
- shared question;
- bounded scope;
- central proposition or debate;
- core source IDs;
- neighboring cluster IDs; and
- refresh status.

This lets a candidate-selection model notice that a new article belongs near an
existing debate before it reads every member note.

### 5.5 Graph memory available to candidate selection

For a new or changed source, the candidate selector should receive:

- the new source's catalogue entry and evidence-profile summary;
- the master literature directory;
- selected literature-index entries;
- explicit citations and Zotero relations;
- relevant cluster summaries;
- the current neighbors of likely candidate sources; and
- a short list of unresolved or previously parked relationship candidates.

This graph memory lets the model reason from the library's existing structure
without receiving the whole graph or whole library.

## 6. Probabilistic candidate discovery

Meaningful candidate identification should be model-led. Local retrieval is
only a high-recall context router.

### 6.1 Local pre-routing

Local code may use the following to assemble a broad set of index shards and
catalogue entries:

- explicit citations and Zotero relations;
- collection membership;
- subject assignments;
- existing cluster adjacency;
- shared proposition IDs;
- normalized title, author, case, outcome, or method fields; and
- the current navigation similarity score.

These signals do not create a visible intellectual relationship. They only
limit which catalogue entries are offered to the reasoning model.

The pre-router should prefer recall over precision and include an adjacent
literature when the source has multiple plausible domains. It should never
label two works as supporting or contradicting one another.

### 6.2 Index-shard selection

For a small corpus, the candidate selector may receive all compact catalogue
entries in one call.

For a large corpus:

1. Give the reasoning model the new source summary and master literature
   directory.
2. Ask it to select the most relevant literature shards and explain the
   selection.
3. Include at least one plausible neighboring literature when the source may
   bridge domains.
4. Load only the selected shard entries.

This is normally one additional call for a large library and can be skipped
when the complete compact catalogue fits comfortably.

### 6.3 Candidate-selection call

The model then receives:

- the new source summary;
- the selected compact entries;
- relevant cluster summaries; and
- existing one-hop graph neighbors.

It returns a ranked structured list containing:

- candidate source or cluster ID;
- why the work may matter;
- likely comparison unit, such as proposition, mechanism, outcome, method,
  case, or boundary;
- likely relationship family;
- requested evidence depth; and
- confidence that adjudication is worth the cost.

Returning no candidates is valid.

The selector should normally nominate no more than 8–12 candidates for one
source. This bounds later context and cost; it is not a lifetime cap on the
source's relationships.

### 6.4 Candidate-selection checkpoint

Checkpoint selection using:

- new source profile hash;
- catalogue revision hash;
- cluster-catalogue revision hash;
- candidate-selection prompt version; and
- model/provider identity.

An unchanged library and unchanged source must reuse the selection result.

## 7. Selective reading and relationship adjudication

Candidate selection and relationship classification are separate decisions.

### 7.1 Evidence-depth ladder

Use progressive evidence loading:

1. **Catalogue entry:** enough to decide whether a source may be relevant.
2. **Evidence profile:** default material for relationship adjudication. It
   contains findings, methods, outcomes, boundaries, and source locators in a
   structured form.
3. **Atomic-note sections:** load when the profiles leave the direction,
   qualification, or intellectual contribution ambiguous.
4. **Frozen source passages:** load when an important relationship cannot be
   established from the profile and note, or when a locator or causal claim is
   disputed.

The candidate selector does not need complete atomic notes. The relationship
adjudicator should normally receive the source profile and evidence anchors for
both sides, including the selected anchor text and locator rather than only an
anchor ID. Full atomic notes are escalation material, not default context.

### 7.2 Batched adjudication call

Avoid one call per pair. For each new or changed source, send one bounded
adjudication packet containing the source and approximately 4–8 candidate
profiles. The model returns one structured decision per candidate.

If a candidate requires more context, it may return
`needs_more_context` plus the requested fields or source passages. The pipeline
may perform one focused escalation call for the unresolved subset. Valid
decisions from the first call are retained; they are not regenerated.

### 7.3 Relationship ontology

Keep structural/navigation relations separate from substantive relations.

Structural relations:

- `cites` / `cited_by`;
- `zotero_related`;
- `cluster_member` / `has_member`; and
- optionally machine-side retrieval signals such as shared case, method,
  outcome, or semantic neighborhood.

Substantive relationships:

- `supports` / `supported_by`;
- `undermines` / `undermined_by`;
- `qualifies` / `qualified_by`;
- `extends` / `extended_by`;
- `complements`;
- `rival_explanation`;
- `boundary_contrast`;
- `methodological_fault_line`;
- `sequential_relationship`; and
- `interpretive_or_normative_disagreement`.

A shared method, topic, or case alone cannot become `supports`,
`undermines`, or `extends`.

Limited notes may participate in explicit citation, Zotero, bibliographic, and
navigation relationships. They cannot support a substantive stance relation
that implies full-document findings unless the required claim and two-sided
source evidence are actually present in their limited coverage.

### 7.4 Required substantive-relation record

Each proposed substantive relation should contain:

- stable relation ID;
- source and target IDs;
- relationship type and reciprocal type;
- comparison unit;
- proposition IDs when available;
- plain-language reason;
- source-side evidence-anchor ID and locator;
- target-side evidence-anchor ID and locator;
- important qualifiers or boundary conditions;
- confidence;
- provenance and model;
- source and target profile hashes; and
- decision status.

The reason should explain the intellectual relationship, not merely repeat the
relation label.

### 7.5 Pair ownership and duplicate prevention

Canonicalize each unordered source pair for checkpointing. A pair already
adjudicated under the same profile hashes and prompt version must not be sent
again merely because both sources appear in a later batch.

Directional results remain directional even though the checkpoint identity is
pair-stable.

## 8. Tolerant deterministic validation

Deterministic checks should protect data, not punish harmless response
variation.

### 8.1 Hard integrity checks

Use hard failure only when committing the row would corrupt or misrepresent the
graph:

- response cannot be parsed after one bounded repair attempt;
- source or target ID does not exist;
- source equals target;
- the target was not present in the context supplied to the model;
- relationship type is unsupported;
- reciprocal direction is internally inconsistent;
- referenced evidence anchor or locator does not exist for a substantive
  relation;
- profile hashes do not match the profiles adjudicated; or
- duplicate relation IDs describe conflicting pairs.

Hard failure applies to the affected row, not to the complete response, source,
or run. Valid sibling rows continue.

### 8.2 Soft quality checks

Treat the following as warnings or parked proposals:

- low confidence;
- a vague reason;
- incomplete qualifier text;
- uncertainty between two compatible relation families;
- missing evidence for a low-stakes navigation relation; or
- a relationship that needs fuller source context.

Do not automatically reinterpret an ambiguous substantive relation as another
substantive type. Park it for retry or review. Existing valid relations remain
unchanged.

### 8.3 Response repair

Accept harmless provider variations such as:

- a relationship list without a redundant outer wrapper;
- extra explanatory prose surrounding a recoverable JSON object; or
- one malformed row alongside valid rows.

Normalize, validate row by row, and retain valid work. Allow at most one repair
call for materially incomplete structured output.

### 8.4 Failure isolation

No relationship-stage failure may:

- erase the existing relationship registry;
- remove existing atomic-note graph links;
- prevent source-index generation;
- prevent unrelated sources from completing; or
- force cluster synthesis to restart from scratch.

## 9. Canonical registry and strictly additive note projection

### 9.1 Models never edit atomic notes

Every relationship model call returns data only. It does not receive an
instruction to rewrite, insert into, or reproduce a Markdown note.

The canonical source of graph truth remains a YAML registry, extending the
existing `02_source_memory/indexes/typed_links.yml`. Run-scoped proposals,
rejections, parked rows, and checkpoints remain under `11_state/runs/RUN_ID/`.

### 9.2 Managed graph block

Reuse and harden the existing `update_note_graph` approach. Atomic notes should
have a clearly machine-owned region:

```markdown
## Graph Links

<!-- auto-zettelkasten:graph:start -->
- supports: [[Target note]] — concise reason
- cluster: [[Cluster note]]
<!-- auto-zettelkasten:graph:end -->
```

Only content between the markers and graph-only metadata fields may change
during graph projection. Content outside the managed region is user- or
source-analysis-owned.

Existing legacy `## Graph Links` sections can migrate once to explicit markers.
Because this changes a public artifact contract, implementation must increment
the artifact schema version and document compatibility.

### 9.3 Semantic immutability guard

Before and after every graph update:

1. compute the existing `semantic_note_hash`, which excludes generated graph
   projections;
2. render the managed graph block mechanically;
3. verify that the semantic hash is identical; and
4. abort that note's projection if the semantic hash changes.

This converts “purely additive” from a prompt request into an enforceable file
invariant.

### 9.4 Minimal writes

- Update only notes whose desired graph projection changed.
- Preserve byte-identical files on an unchanged replay.
- Use atomic file replacement.
- Update reciprocal source and target projections from the same committed
  registry state.
- Never regenerate the atomic note or evidence profile merely because its graph
  links changed.
- Keep human-curated relationships in a separate, unmanaged section and never
  delete them.

“Additive” applies to source-analysis content and to successful registry
merges; it does not mean stale machine-generated links must remain forever. A
previously accepted edge may be retired only after the relevant profile
changed and that exact pair was successfully re-adjudicated as
`no_relationship`, or through an explicit human decision. A provider failure,
parse failure, missing response, or soft warning can never retire an existing
edge.

Although the renderer may mechanically serialize frontmatter, no LLM-generated
prose is allowed to pass through the source-analysis sections. A later
implementation may preserve the exact frontmatter byte layout, but semantic
immutability is the mandatory safety property.

### 9.5 Resume safety

Commit the canonical registry before projecting Markdown. Record the desired
graph projection hash. If a run stops between note writes, resume recomputes
the desired projection from the registry and repairs only missing or stale
managed blocks.

Registry merge and projection should use the existing run-ownership and atomic
write conventions so two concurrent runs cannot interleave different graph
revisions. A later run may resume or supersede a completed registry revision;
it must not partially merge against a projection still being written.

## 10. Pipeline sequencing and provider-call budget

The graph should be an ordinary pipeline stage, not a hook.

| Stage | Provider call | Typical unit | Replay behavior |
|---|---|---|---|
| Atomic note and profile | Yes | One source | Existing profile checkpoint |
| Catalogue refresh | No | Changed profiles | Hash-stable |
| Shard selection | Sometimes | One changed source | Skip for small catalogue; checkpoint |
| Candidate selection | Yes | One changed source | Checkpoint by profile and catalogue hashes |
| Relationship adjudication | Yes | One source plus 4–8 candidates | Pair/profile checkpoint |
| Focused evidence escalation | Sometimes | Unresolved subset | Maximum one bounded escalation |
| Registry commit | No | Valid relation rows | Atomic and idempotent |
| Note projection | No | Affected notes | Semantic-hash guarded |
| Cluster refresh | Yes | Affected admitted cluster | Separate checkpoint |

For a typical new source in a large established library, the relationship
stage should require:

- one candidate-selection call;
- one adjudication call; and
- only occasionally one shard-selection or evidence-escalation call.

For a smaller library, shard selection should be skipped. An unchanged replay
must make zero calls.

## 11. Incremental expansion behavior

### 11.1 New source

1. Generate and validate the atomic note and evidence profile.
2. Refresh its compact catalogue entry.
3. Use graph memory and indexes to select candidates.
4. Adjudicate bounded candidate relationships.
5. Commit accepted relations.
6. Project links into the new note and affected older notes.
7. Identify affected clusters.
8. Refresh those clusters separately.
9. Rebuild only affected literature indexes and cluster catalogues.

Older notes do not need new profile calls merely to receive backlinks.

### 11.2 Changed source

Use semantic profile hashes to determine whether intellectual content changed.
If only the graph projection changed, do nothing. If the evidence profile
changed:

- reconsider its current relations;
- include former neighbors as candidates so stale links can be reviewed;
- preserve relations that remain supported;
- revise only affected projections and clusters; and
- checkpoint decisions under the new pair hashes.

### 11.3 Removed or unavailable source

Do not silently delete graph history. Mark affected relationships inactive or
orphaned in the registry, remove broken visible projections on the next
successful graph commit, and retain provenance for audit.

### 11.4 Batch import

Profile all new sources first, then build one catalogue revision. Candidate
selection may consider new-to-new and new-to-existing relationships. Canonical
pair ownership prevents duplicate adjudication when several new sources select
one another.

### 11.5 Periodic reconciliation

Ordinary mapping should remain incremental. Initial migration of an older
library and occasional recall audits may run an explicit, resumable
reconciliation over catalogue shards and cluster neighborhoods. This process
asks the model to find missing relationships among already-existing works
without rereading every source or regenerating atomic notes. Existing
pair/profile checkpoints remain authoritative, so only newly proposed or
changed pairs require adjudication.

## 12. Cluster integration

### 12.1 Graph before clusters

Persist indexes, candidates, accepted atomic relationships, and reciprocal
projections before any cluster proposal call. The atomic graph must remain
usable if clustering fails.

### 12.2 Cluster candidate context

Cluster proposal should use:

- evidence profiles;
- accepted substantive relations;
- propositions;
- compact cluster catalogue;
- structural citation relationships; and
- graph neighborhoods selected by the reasoning model.

Shared vocabulary or deterministic similarity alone is insufficient for
cluster membership.

### 12.3 Large-library hierarchy

For large collections:

1. propose bounded local debate families;
2. reconcile overlapping families across index shards;
3. admit clusters using existing evidence and independence rules;
4. synthesize each admitted cluster separately; and
5. reason about neighboring clusters using compact cluster summaries.

This avoids sending the full library through one cluster-proposal context.

### 12.4 Reciprocal cluster projection

Every admitted cluster links to its atomic members. Every atomic member
explicitly links back to the cluster. Neighboring clusters link reciprocally
when the relationship is committed.

Thematic maps of content should be projections of the canonical graph and
cluster catalogue, not independently authored relationship systems.

### 12.5 Failed cluster refresh

When a changed source affects an existing valid cluster:

- retain the last successful cluster note;
- record `refresh_pending` in the cluster registry;
- list the changed source IDs not yet incorporated;
- display a concise staleness warning;
- retry only that cluster on resume; and
- atomically replace the cluster only after the new synthesis validates.

If no prior valid cluster exists, record a partial cluster attempt without
creating a misleading synthesis.

## 13. Implementation workstreams

### Workstream 1 — Decouple graph persistence from clustering

Primary files:

- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/literature.py`
- `src/auto_zettelkasten/navigation.py`

Changes:

- create one reusable internal graph-refresh path;
- run it after profile construction;
- persist graph state before cluster synthesis;
- preserve graph results on cluster exceptions;
- make normal mapping refresh the workspace-wide graph; and
- keep explicit structural links available even when probabilistic
  relationship work is disabled.

### Workstream 2 — Build the layered catalogue

Primary files:

- `src/auto_zettelkasten/indexes.py`
- `src/auto_zettelkasten/pipeline.py`

Changes:

- replace the flat title/ID/status index with a master directory and bounded
  literature indexes;
- derive entries from existing profiles and note summaries;
- persist a compact machine catalogue;
- create stable shard and catalogue revision hashes; and
- regenerate only affected shards.

### Workstream 3 — Add candidate selection and relation adjudication

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/literature.py`

Changes:

- define structured candidate and relationship response records;
- add checkpointed shard-selection, candidate-selection, adjudication, and
  focused-escalation stages;
- reuse the existing provider consent, provider routing, timeout, and
  checkpoint infrastructure;
- canonicalize pair identities; and
- project existing validated family relations into atomic relationships where
  their evidence and direction are sufficient.

### Workstream 4 — Harden additive projection

Primary files:

- `src/auto_zettelkasten/notes.py`
- `src/auto_zettelkasten/models.py`
- migration code for the artifact-schema update

Changes:

- add explicit managed graph markers;
- enforce semantic-hash equality around graph writes;
- preserve human-curated relationship sections;
- update only affected files;
- add row-level projection diagnostics; and
- retain idempotent atomic writes.

### Workstream 5 — Repair incremental cluster synthesis

Primary files:

- `src/auto_zettelkasten/literature.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/readers.py`

Changes:

- introduce bounded hierarchical cluster proposal;
- use the accepted relationship graph as one clustering input;
- refresh only affected clusters;
- preserve last-good clusters with `refresh_pending`;
- project reciprocal atomic-note and cluster links; and
- prevent cluster failures from altering committed atomic graph state.

No new third-party dependency is expected in any workstream.

## 14. Testing requirements

### 14.1 Source-content protection

- A graph update leaves `semantic_note_hash` unchanged.
- Thesis, method, evidence, findings, limitations, and locator bytes remain
  semantically unchanged.
- A model response never becomes atomic-note body text outside the managed
  graph block.
- A human-curated relationship section survives projection.

### 14.2 Additive and reciprocal projection

- Adding a relationship updates both source and target graph blocks.
- Removing or superseding a committed relationship repairs both projections.
- An unchanged graph produces byte-identical note files.
- A crash during projection is repaired on resume from the canonical registry.
- All rendered wikilinks resolve through filenames or aliases.

### 14.3 Tolerant failure behavior

- One malformed relation row does not discard valid sibling rows.
- A missing response wrapper is normalized when the relationship list is
  otherwise recoverable.
- An ungrounded substantive relation is parked without erasing prior graph
  state.
- Cluster-provider failure leaves indexes and atomic relationships committed.
- A failed cluster refresh preserves the last valid cluster with a warning.

### 14.4 Context and call efficiency

- The complete master index stays within its token budget.
- Oversized literature indexes split by meaningful subliterature.
- Candidate selection never receives all full profiles in a large library.
- Typical new-source relationship mapping uses one candidate call and one
  adjudication call.
- Pair checkpoints prevent duplicate calls across overlapping batches.
- An unchanged full replay makes zero provider calls.

### 14.5 Relationship quality

- Every visible substantive relationship has a grounded reason.
- Every `supports`, `undermines`, or `qualifies` relation references evidence
  on both sides.
- Shared method, case, tag, or vocabulary alone never becomes a substantive
  relationship.
- Directional relations have correct reciprocal types.
- Returning no relationship remains valid.

### 14.6 Large-folder acceptance evaluation

Rerun the Mediation and Conflict Relapse evaluation with the original frozen
source sets and retain the existing atomic-note thresholds:

- critical-fact recall at least 85%;
- substantive-claim support at least 95%;
- numeric and locator accuracy at least 95%;
- zero unsupported causal upgrades;
- correct limited-note scope;
- cluster membership relevance and core-source coverage at least 90%;
- cluster claim support at least 95%;
- zero fabricated debate, contradiction, or consensus claims;
- explicit-link recall 100%;
- inferred substantive-link precision at least 85%;
- curated cross-literature bridge recall at least 70%;
- 100% reciprocal visible graph edges;
- zero unresolved generated wikilinks;
- zero pending source items;
- less than 5% exhausted sources;
- stable projection hashes; and
- zero provider calls on an unchanged replay.

Additionally compare a deterministic sample of generated relationship decisions
against the older Obsidian graph. The old graph is a candidate source and
recall benchmark, not ground truth.

## 15. Rollout order

### Milestone 1 — Graph safety and sequencing

- Decouple graph state from cluster synthesis.
- Preserve existing navigation and explicit relations on cluster failure.
- Enforce additive, semantic-hash-guarded projection.
- Add reciprocal atomic-note and cluster projection tests.

This milestone fixes the current graph-loss defect before introducing more
provider work.

### Milestone 2 — Layered indexes and graph memory

- Build the compact master, literature indexes, machine catalogue, and cluster
  catalogue.
- Add revision hashes and incremental shard refresh.
- Validate context budgets on the 195-source evaluation and the 1,321-note
  Obsidian library inventory.

### Milestone 3 — Probabilistic candidate selection

- Add model-led shard and candidate selection.
- Retain local similarity only as invisible context routing.
- Checkpoint selections and verify cross-literature candidate recall.

### Milestone 4 — Evidence-backed relationship adjudication

- Add batched adjudication and one-step evidence escalation.
- Commit directional substantive relations with two-sided evidence.
- Backfill reciprocal links into affected older notes.

### Milestone 5 — Incremental and hierarchical clusters

- Use graph memory during cluster proposal.
- Add hierarchical large-library proposal and reconciliation.
- Preserve last-good clusters and refresh only affected clusters.

### Milestone 6 — Full comparative evaluation

- Rerun the large-folder evaluation.
- Audit cross-literature bridges, relationship precision, reciprocity,
  idempotence, API-call counts, and additive-write guarantees.
- Compare against the best readable relationships from the older Obsidian
  system and the best structured index behavior from Research OS.

Do not proceed from one milestone to the next when its source-protection,
failure-isolation, or idempotence tests fail.

## 16. Definition of done

The improvement is complete when adding one new source to a large workspace
causes the system to:

1. generate one source-faithful atomic note and evidence profile;
2. navigate a compact index and existing graph without loading the full
   library;
3. use a reasoning model to identify a bounded set of genuinely relevant old
   sources and clusters;
4. inspect only the evidence needed to classify those relationships;
5. commit grounded typed relationships with explicit reasons;
6. add reciprocal links to the new and affected old notes without changing
   their source-analysis prose;
7. refresh only affected cluster syntheses;
8. preserve all prior valid graph and cluster state if any later call fails;
   and
9. make zero provider calls when the same operation is replayed unchanged.
