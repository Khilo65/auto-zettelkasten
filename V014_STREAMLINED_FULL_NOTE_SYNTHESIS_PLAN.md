# Auto-Zettelkasten v0.14 Streamlined Full-Note Synthesis Plan

**Status:** Implemented and evaluated in engine `0.14.0`

**Date:** 2026-07-28

**Foundation:** Engine `0.13.0`, artifact schema `1.12`, relationship registry
schema `4`

**Evidence:** The private 195-source Mediation and Conflict Relapse evaluation,
the v0.13 Obsidian inspection vault, and the v0.13 replay and integration audits

**Primary objective:** Preserve the strong atomic notes and reciprocal Obsidian
links from v0.13 while replacing the restrictive cluster workflow, eliminating
self-invalidating checkpoints, improving cross-literature discovery, and
reducing a clean 195-source run to no more than four hours.

## 1. Executive decision

v0.14 will simplify the semantic pipeline.

The release will use one global graph, one global cluster registry, and
deterministic collection views. It will generate every eligible atomic note
once, in maximum safe parallelism, before planning the literature map. A
cluster-writing call will receive the full analytical bodies of all proposed
member notes. Local code will validate identifiers and projections but will not
select which intellectual findings the model is allowed to use.

The target flow is:

```text
Zotero inventory and local extraction
    ↓
all independent atomic-note calls run concurrently
    ↓
one immutable source bundle per source
    ↓
deterministic global catalogue and collection indexes
    ↓
one global semantic plan when the catalogue fits
    ↓
parallel relationship adjudication using complete endpoint notes
    ↓
parallel cluster writing using complete member notes
    ↓
one registry commit
    ↓
one additive Obsidian projection
    ↓
deterministic collection and subcollection views
```

The release must remove obsolete stages instead of layering another verifier or
repair loop on top of v0.13.

Target versions:

- engine `0.14.0`;
- artifact schema `1.13`;
- relationship registry schema `5`;
- source-analysis bundle schema remains unchanged unless implementation proves
  that an incompatible field is necessary;
- run-ledger schema remains `2`; and
- no new third-party dependency.

## 2. Evidence from v0.13

### 2.1 What must be preserved

The v0.13 acceptance run established that:

- critical-fact recall was 100%;
- substantive-claim support was 96.35%;
- numeric accuracy was 100%;
- all 195 sources reached terminal note states;
- all 128 active substantive source relationships projected reciprocally;
- Obsidian wikilinks, inverse labels, relation IDs, and managed atomic blocks
  worked correctly;
- one source-reading call can produce a useful atomic note, compact profile,
  evidence anchors, literature positions, and missing-source recommendations;
  and
- large-library indexes can be written deterministically.

These are stable foundations. v0.14 must not redesign atomic Markdown ownership
or reciprocal relationship projection.

### 2.2 What must change

The evaluation also found:

- source generation made 363 provider calls for 167 analytical sources because
  fingerprints changed across runs and implementation revisions;
- the three literature maps made 266 provider calls;
- the Combined map alone used 20 cluster-planning calls and 72
  cluster-synthesis calls;
- an unchanged replay made three new relationship calls and changed artifacts;
- cluster coverage reached only 120 of 163 analytical notes;
- no mixed-literature cluster was produced;
- cross-folder inferred relationship precision was 71.88%;
- curated cross-folder bridge recall was 25%;
- 23 of 32 cross-folder relationships contained at least one stale final
  evidence anchor;
- cluster, frontmatter, compatibility, and typed-registry projections retained
  stale machine state; and
- the generated cluster notes were often generic aggregations rather than
  specific explanations of what the member studies found.

The Civil War Duration and Termination cluster exposed the cluster root cause:

- seven studies were admitted, but only two were allowed to shape the central
  verdict;
- `context` and `bridge` labels were treated as evidentiary exclusion rules;
- the synthesis call received only anchors selected during planning rather than
  the complete member analyses;
- clusters with five or more members were restricted to one contribution per
  core source, three central findings, two optional items, and approximately
  4,500 output tokens; and
- deterministic validation replaced missing or rejected material with generic
  fallback prose.

## 3. Product rules

v0.14 adopts the following rules.

1. **One source, one semantic bundle.** An unchanged source is generated once
   and reused across run IDs, collections, maps, and replays.
2. **Atomic notes are a phase dependency.** Final relationship and cluster
   planning begins only after every in-scope source has reached a terminal
   source-note state.
3. **Use provider concurrency.** Independent source calls may all run
   concurrently up to the configured provider limit.
4. **Use one global semantic graph.** Zotero collections are deterministic
   views, not separate semantic databases.
5. **Plan compactly, synthesize fully.** The global planner reads compact
   catalogue entries. A relationship adjudicator or cluster writer reads the
   complete relevant atomic-note analyses.
6. **Do not gate findings by source role.** Every admitted analytical member
   may contribute to the cluster. Roles may describe emphasis but never
   prohibit use.
7. **Let the model organize the literature.** A cluster may be organized around
   a question, debate, mechanism, outcome, method, case, or practice problem.
8. **Local code does mechanical work.** It checks schemas, IDs, ownership,
   budgets, cache keys, reciprocal links, and projection parity. It does not
   decide intellectual relevance, findings, relationship type, debate, or
   consensus.
9. **No routine semantic retries.** Improve the first prompt and use same-call
   self-review. Retry only a genuinely interrupted transport request.
10. **Commit once.** Workers produce source-owned or job-owned results. One
    coordinator commits registries and projections after the semantic phase.
11. **Warnings are not vetoes.** Locator uncertainty, isolated metadata
    ambiguity, and minor wording strength remain visible but do not block an
    otherwise useful note.
12. **Evaluation time is separate.** Runtime acceptance measures the tool
    itself, not the subsequent human or agent audit.

## 4. Explicit non-goals

v0.14 will not:

- add a graph database, vector database, event bus, daemon, queue service, or
  Obsidian plugin;
- require Codex, Claude Code, or another coding harness in production;
- ask a model to rewrite existing Markdown files;
- add an independent relationship-verification call;
- add a post-generation atomic-note fidelity call;
- force every cluster into a research-question format;
- force a mixed-literature cluster when the sources do not support one;
- automatically edit Zotero;
- implement weekly Zotero monitoring or automatic document acquisition yet;
- run gap discovery in the critical map-building path;
- preserve obsolete v0.13 fallback machinery merely for internal
  compatibility; or
- raise provider-call ceilings to hide repeated work.

## 5. Canonical ownership and one-way dependencies

Semantic dependencies must be acyclic:

```text
frozen source content
    ↓
source-analysis bundle
    ↓
compact catalogue
    ↓
global semantic plan
    ↓
relationship decisions
    ↓
cluster syntheses
    ↓
global registries
    ↓
Markdown and collection-view projections
```

Nothing below a layer may invalidate a layer above it.

### 5.1 Source fingerprint

The source semantic fingerprint contains only:

- frozen extracted-content hash;
- the semantic subset of source metadata;
- extraction-scope classification;
- source prompt identity;
- model and provider identity;
- source-analysis schema identity; and
- source policy identity.

It excludes:

- run ID;
- collection or subcollection membership;
- catalogue revision;
- relationship or cluster state;
- timestamps;
- note path;
- frontmatter;
- generated Markdown;
- projection hashes; and
- provider-usage state.

The completed bundle is stored by semantic fingerprint and may be referenced
from any run.

### 5.2 Global planning fingerprint

The global registry records the exact deterministic
`derived_from_source_state_hash` from which it was produced. When that hash and
the planning prompt, model, and policy identities still match the current
workspace, the coordinator skips planning before constructing any provider
job. This is the primary unchanged-replay check.

The global planning fingerprint contains:

- the deterministic sorted compact-profile hashes for the in-scope corpus;
- global-planning prompt identity;
- model and provider identity;
- planning policy identity;
- human-authored relationship constraints, when present; and
- manually curated exclusions or required pairs, when present.

Machine-generated graph, cluster, catalogue-projection, or Markdown revision
counters are not independent semantic inputs and must not enter the fingerprint.

A fresh full-corpus plan does not use its own prior machine graph as semantic
evidence. An incremental plan may receive the previous stable graph as bounded
context, but its job identity contains the previous registry semantic hash and
the changed-source delta. After commit, the new registry is marked as derived
from the new source-state hash. Replaying that state therefore skips the
incremental job rather than treating the newly written graph as another change.

### 5.3 Relationship fingerprint

Each relationship job fingerprint contains:

- sorted endpoint source IDs;
- endpoint source-bundle hashes;
- relationship prompt identity;
- model and provider identity; and
- relationship policy identity.

It excludes the relationship-registry revision, cluster state, graph
projection, catalogue revision, and run ID.

### 5.4 Cluster fingerprint

Each cluster-writing fingerprint contains:

- stable cluster ID;
- sorted retained member source IDs;
- member source-bundle hashes;
- accepted relationship-decision hashes whose endpoints are both members;
- proposed organizing-mode and organizing-problem card;
- cluster-writing prompt identity;
- model and provider identity; and
- cluster policy identity.

It excludes unrelated relationships, other cluster revisions, collection-view
projections, frontmatter, timestamps, and global registry revision counters.

### 5.5 Projection fingerprint

Projection fingerprints contain only the active canonical source, relationship,
and cluster registry records they project. Projection changes never trigger a
provider call.

## 6. Phase 1 — Extraction and source scope

### 6.1 Local extraction

Keep extraction local and read-only against Zotero.

- Use a CPU-appropriate local worker pool for PDF reading, OCR, HTML recovery,
  and attachment inspection.
- As soon as a source reaches a stable extracted state, enqueue its source call.
- Do not wait for every extraction to finish before beginning independent
  source calls.
- Write frozen text and extraction diagnostics before submitting the provider
  job.

### 6.2 Scope categories

Retain:

- `full_document`;
- `partial_document`;
- `abstract_only`;
- `metadata_only`; and
- complete institutional webpage or equivalent full-content classifications.

Improve classification so that:

- one unresolved page does not reduce an otherwise substantive PDF to
  metadata-only;
- excerpts and chapters identify the actual recovered scope;
- complete institutional webpages are substantive when their main body
  contains coherent institutional claims;
- attachment filenames cannot replace valid parent metadata;
- editor, author, and institutional creator roles remain distinct; and
- extraction failures are recorded as pipeline failures rather than semantic
  claims that the source contains only metadata.

### 6.3 Partial-document use

A partial or excerpted document may participate in relationships and clusters
when:

- the recovered content is substantive;
- every projected claim is supported by recovered text;
- the note states its recovered scope;
- no claim is made about absent sections; and
- the cluster writer receives the same scope warning.

Metadata-only sources remain contextual and cannot support substantive findings.

## 7. Phase 2 — Maximum-parallel source generation

### 7.1 Phase barrier

The final global semantic plan begins only after every in-scope source has one
of these terminal outcomes:

- analytical atomic note;
- evidence-bounded partial-document note;
- abstract-only note;
- metadata-only note; or
- terminal gross provider or extraction failure.

No source remains `processing` when planning starts.

### 7.2 Concurrency

Add an `auto` provider-concurrency mode.

For source generation:

```text
active calls = min(ready eligible jobs, configured provider concurrency)
```

For the 195-source acceptance corpus, all ready eligible source calls may run
concurrently. The DeepSeek account limit is far above the corpus size.

Implementation must:

- reuse the existing provider client and standard-library concurrency where
  practical;
- add no dependency solely for concurrency;
- give every worker a source-owned output location;
- prohibit workers from rewriting shared indexes or registries;
- collect provider-usage events centrally or through job-owned immutable
  records;
- count every provider attempt exactly once;
- preserve successful results if another worker fails; and
- allow a user override below the provider maximum.

For libraries larger than a safe local thread count, the coordinator may use
waves or an already-installed asynchronous client. That local implementation
detail must not impose an arbitrary four- or twelve-call semantic limit.

### 7.3 One-shot source contract

Keep the v0.13 source-analysis bundle, with a strengthened instruction to:

- distinguish author recommendation from demonstrated empirical result;
- preserve observational, descriptive, preliminary, model-based, normative, and
  practitioner language;
- avoid `best`, `only`, `causes`, `works`, `helps`, and `more effective` unless
  the source establishes that wording;
- keep modeled quantities distinct from observed quantities;
- identify the recovered document scope;
- retain the most important literature positions;
- record missing important cited sources; and
- self-check conspicuous numbers and source identity before returning.

No second model call edits the bundle.

### 7.4 Failure behavior

- Empty, wrong-source, or unusable structured output becomes
  `parked_for_review`.
- A malformed but locally recoverable envelope is normalized locally.
- Advisory causal, locator, numeric, or metadata warnings are recorded without
  retry or downgrade.
- A transport interruption receives at most one retry.
- A repeated unchanged replay never retries a terminal semantic failure unless
  the user explicitly requests it.

## 8. Phase 3 — Deterministic catalogue and collection indexes

After the source phase completes:

- render one compact global catalogue;
- render an index for every Zotero collection and subcollection;
- append or replace only affected deterministic entries;
- retain title, author, year, thesis, method or knowledge basis, scope, and
  bounded facets;
- include literature-position identities and unresolved cited-source IDs in
  machine-readable companion records;
- keep the human index concise; and
- make zero provider calls.

The index is a routing and planning surface. Full atomic-note bodies do not
belong in it.

## 9. Phase 4 — Global semantic planning

### 9.1 One-call path

Measure the actual prompt size.

When the full compact catalogue plus planning instructions fit safely, send one
global planning call. Planning may use approximately 70–75% of the model context
provided that measured output and system reserves keep the request within the
model limit.

The call receives:

- compact profiles;
- collection and subcollection membership;
- important literature positions and matched citations;
- explicit Zotero relations;
- human-authored links;
- existing accepted machine-neighbor summaries only for a genuine incremental
  run whose source-state delta has not already been incorporated;
- rejected-pair memory;
- active cluster-family cards for incremental runs; and
- any curated required or excluded bridge pairs.

The planner returns:

- relationship candidate pairs;
- proposed clusters and members;
- an organizing mode for each cluster;
- an organizing problem;
- an optional guiding question;
- an optional central tension;
- provisional membership reasons;
- cross-literature family candidates;
- sources not yet placed; and
- compact neighboring-family proposals.

The planner does not select the only evidence that later writers may read.

### 9.2 Organizing modes

A cluster may use:

- `question`;
- `debate`;
- `mechanism`;
- `outcome`;
- `method`;
- `case`;
- `historical_problem`; or
- `practice_problem`.

The cluster writer may refine the label, organizing problem, guiding question,
or central tension after reading complete notes. These wording changes do not
change the stable cluster ID when membership and semantic identity remain
equivalent.

### 9.3 Large-library path

When the compact catalogue does not fit:

1. Use deterministic Zotero collection and subcollection indexes as routing
   nodes.
2. Split only oversized collection indexes into measured token-bounded shards.
3. Run local semantic planning for independent shards concurrently.
4. Return compact family cards and relationship candidates, not free-form
   rolling summaries.
5. Reconcile family cards at the parent collection level.
6. Reconcile top collection families at the global level.
7. Run an explicit cross-folder bridge pass using citations, matched literature
   positions, and proposed family cards.
8. Recursively reconcile only when the parent family-card packet itself exceeds
   context.

Reconciliation determines final membership and family identity. Final cluster
writing still receives complete member atomic notes.

### 9.4 Planning failure

- A failed shard does not invalidate successful sibling shards.
- A failed reconciliation retains the latest complete global plan and records
  the new work as pending.
- Planning makes no Markdown changes.
- No deterministic code invents a replacement cluster.

## 10. Phase 5 — Relationship adjudication

### 10.1 Candidate discovery

Candidate discovery must use:

- compact profiles;
- literature-position matches;
- explicit citations;
- existing graph neighbors;
- cluster-family membership;
- collection membership;
- rejected-pair memory; and
- cross-folder capacity that cannot be consumed by same-folder candidates.

Deterministic code may deduplicate, cap, route, and validate candidates. It does
not decide whether a relationship is intellectually real.

### 10.2 Full-note adjudication

For every selected pair, DeepSeek receives the complete substantive analytical
bodies of both atomic notes, excluding machine projection blocks.

The relationship response contains:

- pair IDs;
- `relationship` or `no_relationship`;
- relation type;
- intellectual direction;
- one shared, opposing, qualifying, sequential, or otherwise connecting
  proposition;
- source-grounded rationale;
- evidence references from both notes;
- confidence; and
- same-call self-review confirmation.

### 10.3 Publication standard

A substantive relationship requires at least one:

- shared or opposing proposition;
- mechanism-to-outcome connection;
- explicit citation and intellectual extension;
- genuine qualification;
- comparable method or measurement relationship;
- theory-to-evidence connection; or
- clearly explained sequential contribution.

Generic topic overlap is insufficient for `complements`.

### 10.4 Batching and concurrency

- Pack multiple independent pairs into bounded provider packets.
- Include complete endpoint notes only once per packet when a source appears in
  several pairs.
- Run independent packets concurrently.
- Validate each returned pair independently.
- Never regenerate an entire packet because one pair is invalid.
- Commit accepted decisions only after all packets finish.

### 10.5 Anchor reconciliation

Evidence references remain subordinate to the note content, but the registry
must preserve integrity.

- Resolve returned evidence references against the current bundle.
- If an anchor identifier changes while its source text and claim remain
  semantically identical, rebind it locally.
- If the claim no longer exists, retire the machine relationship.
- Human-authored links remain untouched.

## 11. Phase 6 — Full-note cluster writing

### 11.1 Remove obsolete restrictions

Delete from the cluster-writing path:

- `core`, `context`, and `bridge` evidentiary exclusion rules;
- the planner-selected-anchor-only profile projection;
- the 4,500-token large-cluster budget;
- one contribution per core source;
- three-central-finding and two-optional-item caps;
- deterministic semantic fallback paragraphs;
- automatic cluster-repair calls;
- mandatory consensus and contradiction prose;
- gap-generation requirements; and
- provider requirements for administrative diagnostics that are used only by
  local ledgers.

Roles may remain as optional descriptive metadata, but no role may determine
which source findings the writer can use.

### 11.2 Writer input

Each cluster writer receives:

- the complete substantive Markdown body of every proposed member atomic note;
- each member's source ID, title, author, year, and scope;
- accepted relationships among members;
- the proposed organizing-mode card;
- compact cards for neighboring clusters;
- collection membership for each source; and
- an instruction to read all members before drafting.

It does not receive:

- machine-generated atomic graph blocks;
- mutable projection frontmatter;
- run-ledger history;
- unrelated clusters' full notes; or
- obsolete validation traces.

### 11.3 Writer authority

After reading complete notes, the writer may:

- refine the title and organizing problem;
- retain a member and state its exact contribution;
- identify a member as peripheral while explaining why it remains useful;
- drop a member that does not substantively belong;
- split an incoherent proposal into narrower proposed clusters;
- reject the cluster; and
- identify a missing member by source ID from the compact catalogue.

A newly suggested member cannot be silently added because its full note was not
in context. It returns to planning or a bounded follow-up job.

### 11.4 Provider contract

Replace the current broad cluster schema with:

```yaml
cluster_id: stable ID
title: human title
organizing_mode: question | debate | mechanism | outcome | method | case | historical_problem | practice_problem
organizing_problem: concise description
guiding_question: optional
central_tension: optional
bottom_line: connected evidence-grounded synthesis
lines_of_inquiry:
  - title: line of inquiry
    synthesis: what the literature establishes
    study_findings:
      - source_id: source ID
        finding: exact cluster-relevant finding
        method_scope: method, case, period, or evidence basis needed to interpret it
        relation_to_line: supports | qualifies | contrasts | extends | applies | contextualizes
        evidence: source-owned references
differences:
  - specific disagreement, boundary, method, or measurement difference
limits:
  - cluster-level inferential boundary
related_clusters:
  - target_cluster_id: existing cluster ID
    relation_type: relation type
    explanation: substantive relationship
retained_member_ids:
  - source ID
dropped_members:
  - source_id: source ID
    reason: specific reason
```

Every retained analytical member must appear in at least one specific
`study_findings` record. A writer that cannot state a member's contribution must
drop it rather than retain it as decorative context.

### 11.5 Prompt

The cluster-writing instruction is:

> Read every supplied atomic note. Identify the organizing problem that best
> explains why these works belong together. Write the most useful account of
> what the literature finds. For every retained source, state the finding,
> argument, method, case, or boundary that matters to this cluster. Organize the
> studies into meaningful lines of inquiry. Explain support, qualification,
> disagreement, complementarity, and differences only when the notes establish
> them. Do not replace source-specific findings with generic thematic prose.
> Preserve inferential limits and source scope. Review attribution, direction,
> numbers, and membership before returning.

The model chooses the appropriate depth within the available context and output
capacity.

### 11.6 Context policy

For a normal cluster, include every complete member note in one call.

If the measured request exceeds the safe context budget:

1. Treat excessive size as evidence that the proposed cluster may be too broad.
2. Ask planning to split it into coherent subclusters.
3. Only if a genuinely coherent umbrella cluster remains too large, synthesize
   evidence-rich subclusters from full notes.
4. Give the umbrella writer the complete subcluster syntheses, citations, and
   the most central original notes.

The umbrella path is exceptional and must not become the default for ordinary
clusters.

### 11.7 Concurrency

- Use one provider job per final cluster for maximum quality and failure
  isolation.
- Run every independent cluster job concurrently.
- Do not batch multiple clusters merely to reduce call count unless a benchmark
  proves equal quality.
- Each worker writes a job-owned result.
- One coordinator commits accepted clusters after all workers finish.

Thirty-six clusters should require no more than thirty-six first-pass calls, not
seventy-two refresh and repair calls.

## 12. Human-facing cluster Markdown

Render only useful sections:

```markdown
# Cluster title

## Organizing problem

The question, debate, mechanism, outcome, method, case, historical problem,
or practice problem organizing the literature.

## Bottom line

The connected, evidence-grounded state of the literature.

## Main lines of inquiry

### First line

- Specific finding from Study A, including the method and qualification needed
  to interpret it.
- Specific finding from Study B and how it relates to Study A.

### Second line

- Specific member findings.

## How the findings relate

Only genuine agreements, disagreements, qualifications, complementary
mechanisms, or differences in method, period, population, and measurement.

## Limits and boundaries

What this cluster does and does not establish.

## Related clusters

Managed substantive cluster links.

## Members

Managed reciprocal atomic-note links.
```

Rendering rules:

- omit empty sections;
- omit generic statements that consensus or contradiction is not established;
- do not repeat the same finding in several sections;
- keep administrative validation diagnostics in YAML;
- use native Obsidian wikilinks;
- preserve stable relation and cluster IDs in managed comments or frontmatter;
  and
- never rewrite user-authored prose outside managed blocks.

## 13. Phase 7 — Single registry commit and projection

After relationship and cluster workers finish:

1. Validate all job-owned records mechanically.
2. Build one desired global relationship registry.
3. Build one desired global cluster registry.
4. Commit registries atomically.
5. Project reciprocal atomic relationships.
6. Project reciprocal atomic-to-cluster memberships.
7. Project cluster-to-cluster links.
8. Update only machine-owned frontmatter keys and managed blocks.
9. Remove obsolete machine-owned entries and files.
10. Write deterministic collection and subcollection views.

### 13.1 Projection cleanup

The desired-state projection pass must:

- remove typed memberships to retired clusters;
- remove retired relationships from managed blocks;
- update stale machine-owned frontmatter;
- remove orphan machine cluster files only when their ownership marker proves
  they are generated;
- retain registry history separately;
- preserve human-authored links and prose byte-for-byte; and
- verify registry-to-projection parity before reporting completion.

### 13.2 No semantic feedback

Projection writes never update source, relationship, planning, or cluster
semantic fingerprints.

## 14. Collection and subcollection views

The global graph and cluster registry are canonical.

A collection view is derived locally from:

- source collection membership;
- global relationships whose endpoints intersect the view;
- global clusters containing view members;
- cross-view links; and
- the global compact catalogue.

By default, building a Mediation view or Relapse view makes zero provider calls.

An explicitly requested collection-specific synthesis may make model calls when
the user wants a different intellectual organization from the global map. That
is an optional operation, not the normal mapping path.

## 15. Incremental library growth

When Zotero gains a source:

1. Detect the new or changed Zotero key.
2. Extract it locally.
3. Make one source-analysis call.
4. Add its compact entry deterministically.
5. Give the planner the new profile plus relevant hierarchical indexes,
   citations, existing neighbors, and family cards.
6. Adjudicate only new candidate pairs.
7. Refresh only clusters whose proposed membership or substantive evidence
   changes.
8. Project the updated global graph once.

Adding one source must not:

- regenerate unchanged atomic notes;
- reconsider every possible pair;
- rewrite unrelated clusters;
- rebuild collection-specific semantic maps; or
- change user-authored Markdown.

The source and index state should remain suitable for a future weekly Zotero
heartbeat, but v0.14 does not implement the scheduler.

## 16. Gap research and acquisition memory

Keep:

- literature-position records;
- citation matches;
- missing-source recommendations;
- the future-acquisition ledger; and
- existing valid gap history.

Remove gap adjudication from default cluster-writing prompts and from the
critical map-building path.

A later explicit `detect-gaps` workflow may read completed clusters and the
global graph. Failure of optional gap analysis must never make the graph or
cluster map partial.

## 17. Zotero remediation

Zotero remains read-only.

Extend the advisory metadata ledger to record:

- probable document-type mismatch;
- probable creator-role mismatch;
- attachment-title contamination;
- chapter or excerpt represented as a full book;
- institutional report represented as a book;
- complete webpage classified too narrowly; and
- extraction failure incorrectly represented as metadata-only scope.

Include Pathways for Peace as a regression fixture: the pipeline should
recommend review as a joint institutional report without editing Zotero.

## 18. Compatibility and migration

### 18.1 Existing workspaces

- Read v0.13 workspaces without provider calls.
- Preserve v0.13 atomic notes and human content.
- Preserve active reciprocal source links until v0.14 replaces or retires them.
- Mark v0.13 cluster syntheses as legacy projections until explicitly refreshed.
- Do not regenerate atomic notes merely because the engine upgraded.
- Rebuild global registries lazily when the user runs the v0.14 map workflow.

### 18.2 Registry migration

- Consolidate collection-specific machine relationships into one global
  registry.
- Deduplicate by stable endpoint pair and active semantic decision.
- Preserve retirement lineage.
- Reconcile evidence anchors against current bundles.
- Consolidate clusters by stable semantic identity and member set.
- Retire stale typed memberships.
- Make migration local, idempotent, and provider-free.

### 18.3 Custom reasoners

Keep capability detection.

- Existing custom source reasoners may continue returning v0.13 bundles.
- Existing cluster reasoners that return the old schema may project through a
  compatibility adapter, but the built-in reasoner uses the v0.14 schema.
- Compatibility adapters must not restore core/context exclusions or generic
  fallback prose.

## 19. Interfaces

Prefer reuse over new public settings.

Required behavior:

- accept `provider_concurrency=auto` in configuration and API requests;
- retain explicit numeric concurrency values;
- expose actual peak concurrency and per-stage wall time in reports;
- make the default `build-map` operate on the global registry;
- add an explicit opt-in for collection-specific semantic resynthesis if no
  equivalent interface already exists;
- preserve current cloud authorization and cumulative call ceilings; and
- preserve explicit retry controls.

Do not add per-stage public call budgets unless implementation proves the single
cumulative ceiling cannot safely govern the simplified pipeline.

## 20. Implementation sequence

### Phase A — State and cache foundations

1. Add workspace-wide source-bundle lookup by semantic fingerprint.
2. Remove run ID, catalogue revision, graph revision, projection state, and
   timestamps from semantic cache keys.
3. Add the acyclic global-plan, relationship, cluster, and projection
   fingerprints.
4. Make provider usage and job results safe under concurrent completion.
5. Add zero-call replay and cross-run reuse tests.

Success condition: the same source mapped under two collections and three run
IDs produces one source call total.

### Phase B — Global registry and projection cleanup

1. Make one global relationship registry canonical.
2. Make one global cluster registry canonical.
3. Derive collection views locally.
4. Implement desired-state cleanup for machine-owned frontmatter, managed
   blocks, typed memberships, and orphan cluster files.
5. Preserve all human content.

Success condition: registry and Obsidian projections agree exactly.

### Phase C — Parallel source pipeline

1. Add `auto` provider concurrency.
2. Enqueue source calls as extraction completes.
3. Keep source outputs job-owned.
4. Consolidate bundles and indexes after terminal accounting.
5. Record peak concurrency and stage wall time.

Success condition: all eligible sources in the frozen corpus may be in flight
concurrently without duplicate calls or shared-state corruption.

### Phase D — Global planning and relationship discovery

1. Replace repeated local planning for a fitting corpus with one global call.
2. Add measured 70–75% planning context use.
3. Implement hierarchical collection planning only for oversized catalogues.
4. Include citations, literature positions, graph neighbors, cluster cards, and
   rejected-pair memory.
5. Keep one full-note relationship adjudication decision per pair.

Success condition: candidate recall improves without publishing generic topical
links.

### Phase E — Full-note cluster writing

1. Remove role-based evidentiary exclusions.
2. Remove selected-anchor-only synthesis input.
3. Supply full substantive member notes.
4. Replace the provider schema and human Markdown template.
5. Remove semantic fallbacks, automatic repairs, and default gap generation.
6. Run cluster jobs independently and concurrently.

Success condition: every retained member has a specific cluster-relevant
contribution and the cluster bottom line aggregates the actual findings.

### Phase F — Scope, prompt, and Zotero diagnostics

1. Make the small source-prompt improvements.
2. Repair partial/excerpt and complete-webpage classification.
3. Permit evidence-bounded partial sources in synthesis.
4. Expand metadata-remediation recommendations.

Success condition: no complete-source findings are invented and useful recovered
content is not unnecessarily excluded.

### Phase G — Migration, documentation, and full verification

1. Add provider-free v0.13 migration.
2. Update API, CLI, README, and schema documentation.
3. Run focused tests.
4. Run the complete existing suite.
5. Build the package.
6. Run the fresh 195-source comparison.

## 21. Test plan

### 21.1 Fingerprint and replay tests

- One source in two collections makes one source call.
- A different run ID reuses the same source bundle.
- Editing frontmatter causes zero semantic calls.
- Editing a managed graph block causes zero semantic calls.
- A source-content change invalidates only that source.
- A source-prompt change invalidates source bundles deliberately.
- A relationship change invalidates only incident cluster jobs.
- A cluster projection change invalidates no semantic jobs.
- An unchanged replay makes zero calls and no writes.
- Machine output cannot become its own semantic invalidation input.

### 21.2 Concurrency tests

- All independent ready source jobs may enter the executor concurrently.
- Peak concurrency is recorded.
- Every attempt is counted once.
- Concurrent jobs cannot overwrite one another.
- One failed source does not cancel completed siblings.
- One interrupted transport request receives at most one retry.
- Shared registries are committed only after worker completion.
- Independent relationship packets run concurrently.
- Independent cluster jobs run concurrently.
- Two workers cannot commit the same relationship or cluster entry.

### 21.3 Global-planning tests

- A 195-profile compact catalogue uses one planning call when it fits.
- Planning may use more than 50% but not exceed its measured safe context.
- Oversized collection indexes split by measured tokens.
- Shard family cards reconcile without full atomic notes.
- Cross-folder family cards are explicitly considered.
- Collection views require zero planning calls.
- The planner may choose every supported organizing mode.
- A cluster ID remains stable when only title or question wording changes.

### 21.4 Relationship tests

- Candidate packets include literature positions, citations, graph neighbors,
  cluster cards, and rejected-pair memory.
- Adjudication receives both complete substantive atomic-note bodies.
- Generic topic overlap cannot publish `complements`.
- Genuine support, qualification, contrast, extension, application, and
  complementarity remain publishable.
- Direction and rationale remain internally aligned.
- Citation remains distinct from substantive agreement.
- A stale but semantically identical anchor rebinds locally.
- A removed claim retires its machine relationship.
- Reciprocal Obsidian links remain byte-safe.

### 21.5 Cluster tests

- The Civil War Duration and Termination fixture supplies all seven complete
  member notes.
- Cunningham 2006, Cunningham 2010, Bapat, Elbadawi, Balch-Lindsay, Findley, and
  Akcinaroglu each receive a specific cluster-relevant contribution or are
  explicitly dropped.
- Context or bridge labels cannot exclude findings.
- No large-cluster 4,500-token instruction remains.
- Empty consensus or contradiction sections are omitted.
- No deterministic semantic boilerplate replaces a failed synthesis.
- The writer may refine the organizing mode and problem.
- A retained member must appear in a study finding.
- An irrelevant proposed member can be dropped.
- An incoherent cluster can be rejected or split.
- Normal clusters receive all complete member notes in one call.
- Oversized coherent clusters use the exceptional umbrella path.
- One failed cluster leaves valid sibling clusters publishable.
- Atomic-to-cluster and cluster-to-atomic membership remains reciprocal.

### 21.6 Scope and metadata tests

- A 39/40 readable PDF remains substantive.
- A 100/101 readable PDF remains substantive.
- A partial excerpt is labeled partial and may contribute bounded findings.
- A chapter is not labeled as the full book.
- A complete institutional page remains substantive.
- A bibliography-only attachment remains metadata-only.
- Parent title and creators override attachment filenames.
- Pathways for Peace produces a report-type and creator-role review
  recommendation.
- Limited notes invent no complete-document findings.

### 21.7 Projection tests

- Desired active registries equal projected managed blocks.
- Stale typed memberships are removed.
- Retired cluster files with machine ownership markers are removed.
- Human cluster files are never removed.
- Human frontmatter and prose remain byte-identical.
- Collection views contain correct cross-view links.
- Reprojection makes zero provider calls.

## 22. Fresh comparison run

Create a new private workspace:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v014-evaluation-20260728`

Use the same frozen Zotero collections:

- Mediation: `B887A4Q8`;
- Conflict Relapse: `D2XT9ZU9`; and
- 195 unique sources total.

Use:

- DeepSeek `deepseek-v4-flash`;
- explicit cloud permission;
- the same frozen source corpus where possible;
- `provider_concurrency=auto`;
- the existing source and literature call ceilings as hard maximums;
- one global semantic map;
- deterministic Mediation and Relapse collection views; and
- no provider-backed duplicate collection maps.

Do not modify Zotero or publish artifacts.

### 22.1 Runtime measurement

Record separately:

- Zotero inventory time;
- extraction/OCR wall time;
- source-generation wall time;
- peak source-call concurrency;
- global-planning wall time;
- relationship wall time and calls;
- cluster-writing wall time and calls;
- registry/projection wall time;
- total tool wall time; and
- evaluation/audit wall time.

Tool runtime excludes evaluation/audit time.

### 22.2 Reused evaluation manifests

Reuse:

- the deterministic 30-source atomic sample;
- the frozen 40 curated bridge pairs;
- the same explicit cross-folder citation audit;
- every generated cross-folder relationship;
- the eight largest single-literature clusters;
- every mixed-literature cluster; and
- the Civil War Duration and Termination cluster as a mandatory qualitative
  regression case.

### 22.3 Cluster source-first audit

For audited clusters:

1. Read every member atomic note before reading the cluster.
2. Record the most important cluster-relevant contribution of each member.
3. Inspect the cluster.
4. Score whether each retained member's contribution is specific and accurate.
5. Score bottom-line support.
6. Score whether relationships among findings are real rather than thematic
   boilerplate.
7. Record omitted core studies and decorative members.
8. Check every displayed link.

## 23. Acceptance criteria

### 23.1 Atomic notes

- Critical-fact recall: at least 85%.
- Supported substantive claims: at least 95%.
- Numeric support: at least 95%.
- Zero invented major findings.
- Zero material thesis reversals.
- Zero invented complete-document findings from limited sources.
- Locator accuracy is reported as an advisory navigation metric, with 80% exact
  or approximately resolving locators considered useful.
- Minor causal or comparative wording warnings are reported but block release
  only when they materially change the source's argument or evidence.

### 23.2 Relationships

- Explicit cross-folder link recall: 100%.
- Inferred cross-folder precision: at least 85%.
- Frozen curated-bridge recall: at least 70%.
- Every visible reason identifies a real intellectual connection.
- Generic topic-only complementarity: zero published examples in the audit.
- Active relationship evidence resolves against current bundles: 100%.
- Reciprocal Obsidian projection: 100%.

### 23.3 Clusters

- Analytical-source cluster coverage: at least 90%.
- Membership relevance: at least 90%.
- Core-source coverage: at least 90%.
- Supported substantive cluster claims: at least 95%.
- Every retained analytical member has a specific cluster-relevant
  contribution: 100% mechanically, at least 90% accurate in deep audit.
- Every multi-source bottom line is supported by at least two cited members
  unless it is explicitly a single-position cluster.
- Zero fabricated debates, contradictions, consensus, or findings.
- Zero generic deterministic fallback verdicts.
- Mixed-literature families are tested against curated plausible families but
  are not forced when evidence does not support them.
- The Civil War Duration and Termination cluster specifically explains the
  findings and scopes of its relevant veto-player, intervention, negotiation,
  and adaptation studies.

### 23.4 Pipeline and runtime

- Every source reaches a terminal state.
- Unchanged semantic sources make no duplicate source calls.
- One global map is canonical.
- Collection views make zero provider calls.
- A clean 195-source tool run completes within four hours, excluding audit.
- Source-call count does not exceed one first-pass call per eligible source plus
  explicitly counted transport retries.
- The fitting 195-profile corpus uses one global planning call.
- Every final cluster uses at most one first-pass cluster-writing call.
- No hidden semantic retries.
- Registry and projection state agree exactly.
- No stale typed memberships, frontmatter, or orphan machine clusters remain.
- An unchanged replay makes zero provider calls.
- An unchanged replay performs no generated-artifact rewrites.
- Semantic and projection hashes remain stable.

### 23.5 Incremental behavior

Adding one source:

- makes one source call;
- updates its deterministic index entries;
- adjudicates only bounded new candidate pairs;
- refreshes only substantively affected clusters;
- leaves unrelated notes and clusters byte-identical; and
- completes without a full-library semantic rebuild.

## 24. Comparison report

Write:

`evaluation/v014-comparison.md`

The report must compare v0.13 and v0.14 on:

- atomic-note accuracy;
- source-scope safety;
- relationship precision and recall;
- evidence-anchor integrity;
- cluster membership and source coverage;
- cluster-specific finding coverage;
- Civil War Duration and Termination quality;
- mixed-literature synthesis;
- reciprocal Obsidian links;
- stale projection state;
- source, relationship, planning, and cluster call counts;
- peak concurrency;
- tool wall time;
- audit wall time; and
- unchanged replay.

The report must separate:

- release-blocking defects;
- advisory note-quality warnings;
- evaluation-only time;
- provider-generation time; and
- likely failure stage.

It must include representative source-linked successes and failures and end with
`PASS`, `QUALIFIED`, or `FAIL`.

## 25. Final release decision

v0.14 succeeds only if it preserves the strong atomic notes and reciprocal
linking while materially improving cluster usefulness, relationship discovery,
replay stability, and runtime.

The central qualitative test is simple:

> When a researcher opens a cluster, can they quickly learn what the important
> studies actually found, how those findings relate, and where the evidence
> differs—without reopening every atomic note?

If the answer is no, a mechanically valid cluster is not accepted.
