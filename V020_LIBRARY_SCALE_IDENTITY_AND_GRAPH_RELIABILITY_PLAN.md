# Auto-Zettelkasten v0.20 Library-Scale Identity and Graph Reliability Plan

**Status:** Proposed

**Date:** 2026-07-30

**Foundation:** Engine `0.19.0`, artifact schema `1.14`, source bundle prompt
`5`, relationship prompt `9`, relationship decision contract
`relationship-decision-v6`, relationship registry schema `6`, cluster
synthesis prompt `28`, source catalogue schema `4`, and note metadata schema
`1`

**Primary evidence:**

- `V019_LEAN_GRAPH_AND_FULL_COMPARATIVE_EVALUATION_PLAN.md`;
- the v0.19 evaluation at
  `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v019-evaluation-20260730/evaluation/v019-full-comparison.md`;
- manual inspection of the Berg atomic note and the Fortna peacekeeping
  cluster; and
- the architectural discussion about whole-library, collection-incremental,
  flat-library, and duplicate-record operation.

## 1. Objective

v0.20 will fix the three remaining material release blockers:

1. valid source analyses are lost because provider envelopes are malformed;
2. cross-literature discovery misses too many valuable bridge candidates; and
3. accepted relationships often cite endpoint evidence anchors that do not
   establish the stated connecting proposition.

It will also fix two concrete graph defects revealed by manual review:

- Berg's explicit citations to already-mapped Walter and Hegre works were not
  converted into Obsidian links; and
- Fortna's in-corpus peacekeeping book was removed entirely from a
  peacekeeping cluster because the recovered material could support only a
  bounded theoretical and methodological contribution, not the previewed
  empirical effect.

These fixes will be implemented as the smallest coherent step toward the
intended production architecture:

- one canonical source and graph workspace per Zotero library;
- collection and subcollection indexes as filtered navigation views;
- system-generated virtual indexes when Zotero organization is flat or too
  broad;
- deterministic bibliographic identity and citation reconciliation;
- probabilistic intellectual relationship and cluster judgment;
- incremental updates that reconsider only new or affected neighborhoods; and
- exact zero-call, zero-write replay.

This is an incremental remediation, not a rewrite. Existing atomic-note
content quality is treated as mature and must not be traded away to add graph
features.

## 2. Evidence and diagnosis

### 2.1 What v0.19 established

Preserve these results:

- published atomic notes achieved 100% sampled critical-fact recall;
- substantive-claim support reached 96.51%;
- no material unsupported causal upgrade was found;
- literature-position accuracy reached 98.28%;
- direct relationship type and direction reached 88.89%;
- cluster membership, contribution, claim-support, and boundary thresholds
  passed;
- every accepted relationship and cluster membership projected reciprocally;
- the complete production run took about 35 minutes; and
- identical replay made zero provider calls and changed no bytes or mtimes.

The atomic-note prompt does not need another semantic redesign. Statistical
baseline wording and the two cluster-wide overclaims remain quality
improvements worth preserving in prompt self-review, but they are not v0.20
release blockers and do not justify new verifier stages.

### 2.2 Source-envelope failure

Seventeen historically readable sources were parked:

- sixteen long DeepSeek responses were not valid JSON despite completing
  normally; and
- one response retained the correct source and useful analysis but contained
  optional component-shape defects such as numeric literature years, malformed
  evidence-envelope fields, and a mistyped model-generated anchor ID.

This is primarily an output-contract problem, not a document-understanding
problem. The current source call asks DeepSeek to return a large nested
artifact containing content, stable IDs, ownership fields, duplicate
recommendations, support envelopes, and self-review bookkeeping. Much of that
structure is already known or can be derived locally.

The correct fix is a smaller provider-owned envelope, local generation of
stable IDs and ownership, and isolation of malformed optional rows. It is not
a semantic retry loop or a permissive parser that guesses ambiguous JSON.

### 2.3 Bridge discovery failure

v0.19 made two opposite collection-oriented discovery calls and produced 61
unique bridge-origin pairs, but:

- 22 rows were actually same-folder pairs;
- the production merge did not enforce the planned bridge cap correctly;
- only 34/62 available Mediation endpoints and 36/88 available Relapse
  endpoints appeared;
- eligible benchmark candidate recall fell to 7/38; and
- useful final recall fell to 6/38.

The calls produced many plausible alternative comparisons, so the model was
not simply incapable of reasoning about the literature. The discovery packet
asked for globally ranked pairs across broad, weakly structured "other
topics" shards. Prominent sources and obvious themes consumed attention while
many bridge families were never traversed.

The fix is coverage-oriented hierarchical routing through collection and
virtual index cards, followed by bounded source-pair discovery within selected
shard combinations. A candidate remains only a request for full-note
comparison; discovery should favor recall and coverage while adjudication
remains selective.

### 2.4 Relationship evidence-anchor failure

Direct relationship proposition grounding was strong at 25/27 and
type/direction passed, but only 17/27 direct links selected adequate endpoint
anchors. Ten links cited valid anchors belonging to the correct notes that did
not establish the claims used in the rationale.

The current response asks for arrays of anchor IDs without requiring the model
to state the exact endpoint claim each selected anchor supports. The fix is to
pair one concise claim from each source with its selected source-owned anchor
inside the same adjudication call. No second verifier call is needed.

### 2.5 Berg citation failure

Berg's atomic note correctly recorded important literature positions, but its
Walter 2002 and Hegre 2015 entries remained unlinked even though matching
atomic notes existed in the evaluated workspace.

The current fallback requires exact normalized author-string equality:

- `Walter, Barbara F.` does not equal `Walter`; and
- `Hegre, Håvard` does not equal `Hegre`.

The title and year matched, but the author filter removed both candidates.
Other Berg citations were outside the two evaluated collections, so their lack
of atomic-note links was expected for this test. In production they should be
distinguished as:

- mapped in this workspace;
- known in Zotero but not yet mapped; or
- absent from the available Zotero identity snapshot.

### 2.6 Fortna cluster failure

The exact 2004 article *Does Peacekeeping Keep Peace?* was outside the tested
collections. Fortna's 2007 book *Does Peacekeeping Work?* was inside the
Conflict Relapse collection.

The cluster planner selected the book as central to "Peacekeeping and
Post-Conflict Stability." The cluster writer then correctly recognized that
the recovered introductory chapter did not establish the previewed empirical
effect, but it removed the work entirely.

The evidence boundary was correct; the binary membership behavior was not.
Partial sources may contribute theory, method, framing, definitions,
historical context, or explicitly available findings without being counted as
full-document empirical support.

### 2.7 Scale and incremental-growth gap

The existing code already contains:

- a complete Zotero collection-tree snapshot;
- collection and subcollection index projections;
- compact source profiles and routing cards;
- literature-position and missing-source ledgers;
- a reconciliation pass after source processing;
- focused relationship discovery support;
- a global relationship registry and cluster catalogue; and
- acyclic replay fingerprints.

The missing piece is not another indexing system. It is a more complete use of
these structures so that:

- duplicate Zotero records do not produce duplicate provider calls or notes;
- flat root libraries receive bounded virtual topic shards;
- newly mapped sources resolve old citations in both directions;
- collection-scoped runs converge into one global graph; and
- only affected relationships and clusters are reconsidered.

## 3. Settled architecture and non-goals

### 3.1 One logical source registry, several projections

Each mapped work has one canonical logical record assembled from existing
Zotero snapshot, note metadata, and compact profile data.

It has two logical sections:

```yaml
identity:
  canonical_zotero_key: ...
  zotero_item_keys: [...]
  doi: ...
  isbn: ...
  url: ...
  normalized_title: ...
  normalized_author_surnames: [...]
  year: ...
  zotero_relations: {...}

navigation:
  title: ...
  author: ...
  year: ...
  thesis: ...
  method: ...
  source_scope: ...
  evidence_eligibility: ...
  facets_by_type: {...}
  collections: [...]
```

This is a logical schema, not a requirement to create a second database.
Extend the existing note metadata and source catalogue projections.

Operations read only the fields they need:

- local citation and duplicate resolution reads `identity`;
- collection and virtual routing reads `navigation`;
- relationship adjudication retrieves complete atomic notes;
- cluster synthesis retrieves complete notes for proposed members; and
- human-facing indexes render compact navigation fields and links.

### 3.2 Formats

Keep the current format split:

- atomic notes: Markdown with concise YAML frontmatter;
- per-source machine metadata and profiles: YAML;
- machine catalogues and registries: YAML;
- agent and human navigation indexes: Markdown;
- evaluation exports: Markdown and YAML, with CSV only when a tabular export is
  useful.

Do not introduce SQLite in v0.20. The current YAML system is adequate until a
measured large-library benchmark demonstrates that parsing or rewriting
sharded YAML is a material bottleneck.

### 3.3 One workspace and one graph per library

The production default is one canonical Auto-Zettelkasten workspace for a
Zotero library or explicitly bounded Zotero project.

- A Zotero item in several collections produces one atomic note.
- Collections are filtered navigation and reporting views.
- A collection-scoped run adds sources to the same canonical registry.
- A later collection run reuses already mapped sources.
- A source can appear in multiple collection and virtual indexes without
  duplicating its note.
- Separate workspaces remain useful for private evaluations and deliberate
  isolated projects, but they are not the default incremental architecture.

### 3.4 Deterministic versus probabilistic responsibilities

Deterministic code may:

- normalize and match bibliographic identity;
- consolidate high-confidence duplicate Zotero records;
- build and shard indexes;
- select context-bounded packets after probabilistic routing;
- validate source and endpoint IDs;
- validate evidence-anchor ownership;
- deduplicate and cap candidates;
- persist registries and checkpoints;
- project managed Markdown blocks; and
- calculate invalidation and replay fingerprints.

DeepSeek decides:

- which routed sources are intellectually worth comparing;
- whether a substantive or contextual relationship exists;
- relationship type and intellectual direction;
- the endpoint claims establishing that relationship;
- cluster coherence and membership roles;
- the synthesis of findings, debates, and boundaries; and
- whether a new source changes an existing cluster's interpretation.

### 3.5 Explicit non-goals

Do not add:

- a second graph registry;
- a parallel virtual-index database;
- a harness-native agent dependency;
- a relationship or source verifier call;
- semantic retry loops;
- automatic Zotero edits or duplicate deletion;
- a scheduler, heartbeat daemon, or desktop application;
- embeddings or a new third-party dependency;
- sentence-level deterministic truth adjudication;
- a cluster-coverage acceptance threshold; or
- a rewrite of successful atomic-note prose.

The existing Python pipeline remains independently deployable and suitable for
future CLI, desktop, or hosted products.

## 4. Release identities and compatibility

Release as:

- engine `0.20.0`;
- artifact schema `1.15`;
- source semantic prompt `5`;
- built-in source provider-envelope contract `source-bundle-envelope-v2`;
- internal normalized source bundle schema `1`;
- source catalogue schema `5`;
- note metadata schema `2`;
- Zotero collection snapshot schema `2`;
- literature-position registry schema `2`;
- relationship prompt `10`;
- relationship provider-decision contract `relationship-decision-v7`;
- relationship registry schema `6`; and
- cluster-synthesis prompt `29`.

Keep public APIs and existing CLI options backward-compatible.

The source semantic prompt identity remains `5` because its intellectual
instructions and atomic-note template remain unchanged. The new provider
envelope receives a separate identity and checkpoint component.

Migration is local, lazy, and idempotent:

- accept existing v0.19/schema-1.14 workspaces;
- derive canonical identity aliases from existing note metadata and Zotero
  snapshots;
- retain validated existing source bundles and atomic notes without provider
  calls;
- first reparse saved raw responses for old parked sources through the new
  conservative local recovery path, at zero provider cost;
- retry only still-parked sources, and only when explicitly resumed under the
  new source-envelope contract;
- rebuild catalogue and literature-position projections deterministically;
- do not rewrite atomic-note prose;
- do not activate old ambiguous citation matches; and
- do not rewrite unchanged files.

## 5. Implementation

### 5.1 Simplify the provider-owned source envelope

Keep the existing source reading and atomic-note content instructions, but
replace the oversized provider return contract rather than appending more
rules.

The v2 provider envelope contains:

```yaml
analysis_sections:
  thesis: ...
  method_and_research_design: ...
  evidence_and_data: ...
  detailed_findings: ...
  plain_english_interpretation: ...
  strengths_and_contributions: ...
  methodological_critique: ...
  limitations: ...
  what_this_source_can_support: ...
  what_this_source_cannot_support: ...
  locators: ...

compact_profile:
  thesis: ...
  method_or_knowledge_basis: ...
  source_genre: ...
  inferential_design: ...
  mechanisms: [...]
  outcomes: [...]
  cases: [...]
  populations: [...]
  periods: [...]
  datasets: [...]

evidence_anchors:
  - claim: ...
    locator: ...
    planning_roles: [...]
    salience_priority: 0
    evidence_role: ...
    support_boundary: ...
    plain_english_meaning: ...
    uncertainty: ...
    quantitative_result: {...}  # optional

literature_positions:
  - raw_citation: ...
    author: ...
    year: ...
    title: ...
    identifiers: {...}
    engagement: ...
    relation_label: ...
    locator: ...

observed_bibliographic_identity:
  title: ...
  creators: [...]
  date: ...
```

Remove provider responsibility for:

- `bundle_schema_version`;
- stable source, Zotero, note, literature-position, or anchor IDs;
- source ownership fields repeated on every row;
- authoritative source scope and evidence eligibility;
- nested support-envelope bookkeeping;
- duplicate missing-source recommendations;
- match status and retrieval status;
- self-review output; and
- fields deterministically known from the request.

The prompt still asks the model to perform an internal same-call self-review,
but it does not return a self-review object.

Local normalization:

- supplies stable source identity from the request;
- supplies authoritative extraction scope;
- generates stable anchor and literature-position IDs;
- assigns row ownership;
- normalizes years and scalar enums to strings;
- derives missing-source recommendations from unmatched important literature
  positions;
- converts flat evidence role and support-boundary fields into the existing
  internal support envelope; and
- validates the existing internal `SourceAnalysisBundle` schema before
  publication.

Keep `response_format: {"type": "json_object"}`. If the provider later
advertises a stronger compatible schema facility, use it through existing
capability detection; do not make it a requirement for custom readers.

Use two local parse paths:

1. strict JSON parsing remains the default;
2. when strict JSON fails, use the already-installed safe YAML parser only as
   a conservative JSON-superset recovery path.

The fallback must:

- reject duplicate mapping keys;
- reject YAML tags, aliases, merge keys, and multiple documents;
- return exactly one top-level mapping;
- pass source-ownership and required-core-field validation;
- preserve the original raw response and record that local recovery occurred;
  and
- cost zero provider calls.

This path addresses the observed long responses with unquoted mixed
text-and-number scalar values. It is not permission to repair truncation,
combine objects, infer missing core analysis, or publish ambiguous output.

### 5.2 Isolate optional component defects

A source publishes an analytical note when all of the following are valid:

- exactly one parseable top-level source response;
- correct source ownership;
- usable thesis;
- usable method or knowledge basis;
- usable evidence/data description;
- usable detailed findings or explicitly stated absence of findings; and
- correct recovered-document scope.

Malformed optional rows must not park an otherwise valid note:

- invalid evidence-anchor rows are dropped individually with private
  diagnostics;
- invalid literature-position rows are dropped individually;
- missing optional sections receive the existing neutral placeholder;
- invalid model-generated IDs are ignored because IDs are generated locally;
- numeric years become strings;
- scalar versus list presentation differences are normalized only where the
  intended value is unambiguous; and
- an optional component failure is reported in the item checkpoint and
  evaluation ledger.

Never reinsert a rejected optional row or its malformed fields into the
normalized bundle merely to preserve diagnostics. Diagnostics remain beside
the artifact in private machine state.

Do not combine several candidate objects or publish a wrong-source response.
Preserve raw response text and provider completion metadata for every
unrecoverable result.

There is no semantic retry. The normal single transport retry remains
available and is accounted for.

### 5.3 Correct remaining source-scope and anchor-integrity defects

Keep source scope determined primarily from extraction and attachment
identity.

- A fully recovered report or thesis does not become partial because
  `Introduction`, `Partial Fulfillment`, or similar text appears in a title,
  table of contents, or boilerplate.
- Explicit attachment identity such as a chapter, excerpt, appendix, or
  introduction remains authoritative.
- Missing isolated pages produce `partial_document` only when the unresolved
  content is potentially substantive.
- Parent metadata continues to override generic attachment filenames.

Fix split anchor referential integrity locally:

- when anchor collisions create suffixed canonical IDs, rewrite every nested
  source locator to the final owning anchor ID in the same normalization
  function;
- validate that all nested locator anchor IDs resolve before saving a profile;
  and
- treat unresolved nested IDs as a local profile-generation error, not a
  provider retry.

### 5.4 Canonicalize duplicate Zotero records before provider work

Extend the existing Zotero snapshot and inventory preflight. Distinguish:

1. one Zotero item belonging to several collections; and
2. several Zotero item keys representing the same work.

Create high-confidence duplicate groups using this order:

1. explicit Zotero same-item or same-work relation;
2. exact normalized DOI;
3. exact normalized ISBN plus compatible edition/title;
4. exact normalized title, complete compatible author-surname sequence, and
   publication year, when the match is unique.

Near-title matches, title fragments, missing-author matches, preprint versus
published versions, translations, and revised editions remain separate or
enter an advisory duplicate-review ledger. Do not merge them automatically.

For a confirmed duplicate group:

- retain an already processed canonical Zotero key when one exists;
- otherwise choose the item with the best usable attachment, then the richest
  strong metadata, then the stable lexical Zotero key;
- derive the existing `source-zotero-<canonical-key>` source ID;
- retain every duplicate Zotero key as an identity alias;
- union collection memberships, Zotero relations, and provenance;
- make one source-generation call;
- create one atomic note and compact profile; and
- record every noncanonical inventory row as a terminal `duplicate_alias`
  pointing to the canonical source.

Do not migrate an existing source ID merely because a DOI or another alias is
later discovered. Preserve the established canonical source ID and add the
new identity as an alias so existing links and filenames remain stable.

If a later duplicate supplies materially better content, treat that content
change as a canonical source update rather than generating a second note.

Zotero remains read-only. Do not merge, delete, retag, or edit Zotero items.

### 5.5 Complete deterministic citation identity and autolinking

Extend the existing `_source_match_index`,
`_match_literature_position`, `_reconcile_literature_memory`, and managed
literature projection. Do not introduce a second autolinker.

Build the identity lookup from canonical note metadata and the Zotero identity
snapshot rather than depending only on rendered Markdown frontmatter.

Matching order:

1. exact mapped Zotero item key or alias;
2. exact DOI;
3. exact ISBN with compatible work identity;
4. exact normalized URL where it is a stable publication identifier;
5. exact normalized title and year with compatible first-author surname;
6. exact normalized title and compatible complete author sequence when year is
   missing;
7. high title similarity with compatible year and author, only when one
   candidate is clearly separated from the next-best result.

Author normalization must treat forms such as:

- `Walter`;
- `Walter, Barbara F.`; and
- `Barbara F. Walter`

as compatible first-author identities.

Title fragments, common thematic words, and author-only matches never produce
automatic links.

Every match records:

- `match_status`: `mapped`, `known_zotero_unmapped`, `ambiguous`, or
  `not_in_snapshot`;
- `match_basis`;
- `match_confidence`;
- canonical source ID when mapped;
- Zotero key when known but unmapped; and
- candidate identities when ambiguous.

For a strong mapped match:

- render an additive Obsidian wikilink in the managed `Position in the
  Literature` block;
- record a deterministic `cites` edge and inverse `cited_by` projection;
- retain the author's engagement description;
- add the pair as a high-priority substantive relationship candidate; and
- keep citation distinct from support, qualification, extension, or
  disagreement.

For `known_zotero_unmapped`, retain the citation and retrieval recommendation
without creating a dangling atomic-note link. When that Zotero item later
receives a canonical atomic note, the reconciliation pass upgrades the entry
to `mapped` and adds the link without rereading the old source.

The Berg fixture must resolve Walter 2002 and Hegre 2015 while leaving
out-of-corpus works unresolved or known-unmapped according to the full Zotero
identity snapshot.

### 5.6 Extend the existing catalogue with machine identity and navigation views

Keep `source_catalogue.yml` as the generated machine catalogue. Advance its
schema and represent each source with:

- a machine-only identity section; and
- a compact navigation section.

The relationship and cluster prompt renderers must continue selecting only
the bounded navigation fields they need. Strong identifiers and duplicate
aliases are never included in model prompts unless a specific pair job needs
citation direction.

Keep:

- `INDEX.md` as the compact human entry point;
- per-collection `INDEX.md` files;
- context-bounded Markdown source shards;
- collection routing cards; and
- byte-change-aware deterministic projection.

Do not parse Markdown to establish machine identity.

### 5.7 Add virtual navigation shards by extending existing topic navigation

Virtual indexes are generated catalogue shards, not a new semantic registry.

Reuse `navigation.promote_topic_neighborhoods`,
`literature.map_topic_neighborhoods`, `_meaningful_catalogue_chunks`,
subject-facet normalization, and the existing routing-card renderers. Extend
their shared projections so that a broad or flat collection can be navigated
by controlled source-level facets. Do not add a second topic-neighborhood
engine.

Inputs are existing upstream data only:

- accepted normalized subject tags;
- compact profile mechanisms, outcomes, cases, populations, periods, and
  datasets;
- thesis and method for representative snippets; and
- collection membership.

Rules:

- routing cards expose up to three primary virtual memberships per source by
  default, while the catalogue retains all controlled facets for later
  routing and search;
- a source may appear in several virtual indexes without duplicating its
  atomic note;
- generic values such as `None`, `study`, `conflict`, `research`, and
  `other topics` cannot be primary virtual labels;
- each shard is capped by measured rendered context size, not source count
  alone;
- oversized topics split into stable numbered parts;
- sources without usable facets enter bounded catch-all shards;
- virtual shard identity depends only on source profiles, controlled facets,
  and collection membership; and
- graph edges, clusters, projections, and timestamps never enter the routing
  revision.

Render virtual indexes under the existing index tree, for example:

```text
02_source_memory/indexes/by_topic/
  peacekeeping.md
  conflict-recurrence.md
  security-sector-governance.md
  quantitative-survival-analysis-part-01.md
```

These files answer "where should an agent look?" They make no literature-wide
claims and are not clusters.

### 5.8 Use collection and virtual indexes to route discovery and clustering

The dependency flow remains acyclic:

```text
source content
  → source bundle
  → canonical identity + compact profile
  → collection and virtual routing indexes
  → relationship candidates
  → relationship decisions
  → cluster plan
  → cluster synthesis
  → Markdown projection
```

Indexes inform cluster construction:

- overlapping virtual memberships identify promising source neighborhoods;
- cross-index intersections identify bridge opportunities;
- verified relationship components strengthen cluster proposals;
- cluster planning reads selected compact profiles and relationship summaries;
  and
- cluster writing reads the complete atomic notes of every proposed member.

Clusters link back to relevant collection and virtual indexes for navigation,
but cluster prose and membership do not alter source facets or routing
revisions. This provides two-way navigation with one-way semantic
computation.

### 5.9 Replace global bridge ranking with coverage-oriented shard-pair jobs

Keep one general discovery path. Replace the two broad v0.19 bridge
orientations with:

1. one probabilistic routing call over compact collection and virtual shard
   cards;
2. context-bounded bridge discovery packets over the selected shard pairs; and
3. adaptive full-note adjudication packets.

The router returns:

```yaml
selected_shard_pairs:
  - left_shard_id: ...
    right_shard_id: ...
    bridge_family: ...
    why_examine: ...
    target_candidate_count: ...
```

It may select collection, subcollection, or virtual shards. It must explore
several bridge families rather than return only the globally most obvious
topic.

Discovery packets:

- contain compact profile records, not full notes;
- contain each source once per packet;
- retain collection and virtual memberships;
- include resolved cross-source literature positions;
- ask for concrete shared propositions, mechanisms, outcomes, sequences,
  debates, applications, or boundaries;
- allocate candidate targets per selected shard-pair job so one family cannot
  consume the whole response; and
- optimize recall and useful navigation, not publication certainty.

Local code:

- rejects unknown endpoints;
- validates each bridge candidate against the selected left and right shard
  contexts, including a distinct collection context when the job is
  collection-routed;
- rejects a bridge-only candidate when its endpoints are the same canonical
  source or cannot be assigned to the two selected sides;
- rejects duplicates and excluded pairs;
- preserves valid candidates up to the current build capacity;
- records losses by routing, provider omission, filtering, adjudication,
  persistence, and projection; and
- never invents a substitute candidate.

Repeat the selected-side and distinct-collection validation when constructing
final pair jobs, after the general and bridge pools have been merged. This
prevents a valid bridge-origin row from being mislabeled or replaced by a
same-folder pair later in the pipeline.

For the 195-source comparative run, allow:

- up to 64 general candidates;
- up to 96 cross-collection bridge candidates; and
- up to 160 inferred candidates total.

This is a per-build-round capacity, not a promise to scan every pair in a
large library. For incremental runs, new or changed sources receive a bounded
neighborhood target of approximately 8–16 candidates each before
deduplication. Existing `max_synthesis_calls` remains the hard call governor.

For larger libraries:

- route top-level collection or virtual cards first;
- descend only into selected child collections and shards;
- pack several selected shard-pair jobs per discovery request;
- continue in checkpointed rounds when the configured synthesis budget cannot
  cover every routed job; and
- never perform an all-pairs collection or source scan.

### 5.10 Simplify relationship evidence selection while preserving judgment

Continue supplying:

- both complete atomic notes;
- a de-duplicated `source_documents` mapping so each note appears once per
  request;
- pair jobs referring to source IDs;
- compact bibliographic and citation-direction context; and
- every validated endpoint anchor once per request as bounded compact cards,
  rather than a planner-selected top-five subset.

Each anchor card contains:

- anchor ID;
- source ID;
- claim;
- locator;
- evidence role;
- support boundary; and
- concise plain-English meaning.

The v7 decision row replaces anchor arrays with one required endpoint claim
and primary anchor per side:

```yaml
decision: relationship
relation_type: qualifies
actor_source_id: ...
reference_source_id: ...
comparison_proposition: ...
left_endpoint_claim: ...
left_evidence_anchor_id: ...
right_endpoint_claim: ...
right_evidence_anchor_id: ...
reason: ...
boundary_or_qualification: ...
confidence: 0.82
```

The model may return one optional additional anchor per endpoint only when the
relationship genuinely requires it.

Packet construction remains token-aware: reduce the number of pair jobs
before omitting endpoint anchors. If one unusually large pair still cannot fit
inside the literature context target, place that pair in its own packet.

The prompt keeps the existing successful v0.19 type-and-direction ladder and
adds no new relation categories. Replace overlapping evidence instructions
with one requirement:

> State the exact claim used from each endpoint, then select the source-owned
> anchor that establishes that claim. The rationale, relationship type, and
> direction must connect those two claims.

Before returning, DeepSeek performs one same-call consistency check across
endpoint claims, anchor IDs, proposition, type, actor/reference, and
rationale.

Local code validates:

- job ID and supplied endpoints;
- actor/reference membership;
- relation vocabulary;
- anchor existence and ownership;
- reciprocal label derivation; and
- structural completeness.

It does not decide whether an anchor semantically proves a claim.

Normalize the v0.19 shorthand
`decision: contextual_connection` into
`decision: relationship, relation_type: contextual_connection` only when the
row is otherwise complete and unambiguous. Record the normalization. Do not
infer missing types, direction, claims, or rationale.

Use DeepSeek max reasoning for built-in relationship adjudication when the
provider supports the existing reasoning-effort capability. Keep routing and
candidate discovery at high reasoning. Custom reasoners remain compatible
through capability detection.

### 5.11 Allow evidence-bounded partial sources in relationships and clusters

`partial_document` means incomplete evidence, not intellectual irrelevance.

Relationship adjudication may use a partial source when:

- the proposed endpoint claim is explicit in recovered content;
- the rationale identifies the available-content boundary; and
- the relationship does not attribute unseen findings to the complete work.

Cluster planning and writing use the existing member `role` field with these
values:

- `empirical`;
- `theoretical`;
- `methodological`;
- `contextual`; and
- `boundary`.

The writer may change the proposed role based on the full atomic note. It may
remove an irrelevant source, but it must not remove a central work solely
because its available contribution is theoretical, methodological, or
contextual.

For every retained partial source:

- state the specific available-content contribution;
- state the coverage boundary;
- do not count previewed or unavailable findings as empirical support; and
- avoid whole-document claims.

If a planned central work cannot support the cluster's empirical conclusion
but remains important to the literature, retain it in a concise
`Foundational, methodological, or limited-evidence contributions` section.

The Fortna fixture must:

- retain the 2007 book in the peacekeeping cluster as a bounded theoretical
  and methodological contribution;
- distinguish its previewed conclusion from available empirical evidence; and
- not treat the missing 2004 article as present in the two-folder corpus.

Keep the successful full-note cluster writer, concurrent per-cluster calls,
specific member contributions, malformed-cluster isolation, and no cluster
coverage threshold.

Use max reasoning for the built-in cluster planner and writer when supported.
Do not add a cluster verifier.

### 5.12 Incremental library convergence

Reuse the existing Zotero collection snapshot and diff machinery.

Extend the full read-only library snapshot with compact top-level identity
fields:

- item key;
- title;
- creators;
- date/year;
- DOI;
- ISBN;
- stable URL;
- item type;
- Zotero relations; and
- collection keys.

Do not include attachment text in the global identity snapshot.

For an initial whole-library run:

1. snapshot library identities and collection hierarchy;
2. form canonical duplicate groups;
3. generate one atomic note and compact profile per canonical work;
4. build collection and virtual indexes;
5. reconcile all explicit citations;
6. route and adjudicate graph candidates in budgeted rounds;
7. plan and synthesize clusters from complete notes; and
8. project one global Obsidian graph with collection views.

For later additions:

1. diff Zotero identities and memberships;
2. process only new or content-changed canonical works;
3. update only affected source, collection, and virtual index shards;
4. resolve citations from the new note to old notes;
5. resolve old unresolved citations that point to the new note;
6. route the new profile to relevant collection and virtual neighborhoods;
7. adjudicate only new candidate pairs;
8. reconsider clusters directly connected through membership, accepted
   relationships, or routed cluster cards; and
9. project only changed managed blocks.

A source that does not yet fit a cluster remains neutrally
`currently_unclustered`. It may be reconsidered when new sources or
relationships appear.

If a cluster membership changes, regenerate that machine-owned cluster
synthesis from all retained complete atomic notes. Preserve user-authored
content outside managed cluster sections. Do not append an isolated sentence
that leaves the synthesis internally inconsistent.

Membership-only collection moves update indexes and views with zero provider
calls.

### 5.13 Preserve exact replay and bounded invalidation

Retain and extend the acyclic fingerprint chain.

- Source provider-envelope identity affects only new or explicitly retried
  source calls.
- Validated existing source bundles remain reusable.
- Duplicate aliases and Zotero membership changes affect identity and index
  projections, not atomic semantics.
- Citation reconciliation changes citation edges and downstream focused graph
  work, not source bundles.
- Virtual routing revisions depend only on upstream profiles, facets, and
  collection membership.
- Relationship decisions depend only on endpoint note/profile evidence,
  citation context, prompt, model, and policy.
- Cluster decisions depend only on retained member notes, relevant accepted
  relationships, prompt, model, and policy.
- Projection changes never invalidate semantic work.

Persist all completed, `no_relationship`, contextual, parked, and structural
decisions before cluster work.

Write files only when serialized bytes change. An unchanged replay must return
before provider initialization and produce no ledger, timestamp, history, or
projection writes.

## 6. Interfaces and compatibility

Keep existing public mapping and build interfaces. No new scheduler or daemon
is introduced.

Minimum externally visible changes:

- new terminal alias status `duplicate_alias`;
- source catalogue schema `5`;
- note metadata schema `2`;
- Zotero collection snapshot schema `2`;
- literature-position match statuses and provenance;
- source provider-envelope contract identity in private ledgers; and
- virtual index paths in catalogue outputs.

Custom readers returning the existing internal source bundle remain accepted.
The simplified provider envelope is a built-in reader capability, not a
stricter public protocol.

Custom relationship reasoners returning valid v6 decisions remain accepted
and normalized under the existing v6 anchor-array validation. They do not need
to support the new v7 endpoint-claim fields. Only new built-in v7 decisions
must satisfy the v7 endpoint-claim contract; migration does not retroactively
park an otherwise valid custom v6 relationship.

Existing v0.19 workspaces migrate without Zotero, provider, or cloud calls.

## 7. Regression and integration tests

### 7.1 Source-envelope tests

- A complete v2 provider envelope normalizes into the existing internal source
  bundle.
- The conservative fallback recovers an unquoted mixed scalar into one valid
  source-owned mapping without a provider call.
- Duplicate keys, multiple YAML documents, tags, aliases, merge keys,
  truncation, and ambiguous objects remain terminal.
- Stable IDs and ownership are generated locally.
- Numeric literature years become strings.
- A malformed optional literature row is isolated while the note publishes.
- A malformed optional anchor row is isolated while the note publishes.
- Wrong-source identity remains terminal.
- Ambiguous multiple objects remain terminal.
- Truncated top-level JSON remains terminal.
- Raw response and completion metadata are preserved.
- No syntactic or semantic repair call occurs.
- Existing validated v0.19 notes remain reusable.
- Parser-contract changes locally reconsider only parked raw responses and do
  not invalidate successful-note checkpoints.

### 7.2 Scope and anchor-integrity tests

- *Pathways for Peace* with 337/337 pages is full document.
- `Partial Fulfillment` in a thesis title does not imply partial content.
- A genuine recovered introduction remains partial.
- One missing substantive page remains partial.
- Final split anchor IDs propagate into all nested source locators.
- Every nested locator resolves to its owning anchor.

### 7.3 Duplicate tests

- The same Zotero key in several collections produces one note and all
  memberships.
- Two items with the same DOI produce one provider call and one canonical
  note.
- Duplicate aliases resolve citations to the canonical note.
- An existing processed key remains canonical when a duplicate is added.
- A later better attachment updates the canonical source rather than creating
  a second note.
- Similar titles with different authors do not merge.
- Preprint and published versions remain separate unless explicitly linked.
- Ambiguous duplicate candidates enter an advisory ledger.
- Every duplicate inventory row receives complete terminal accounting.

### 7.4 Citation reconciliation tests

- Berg's `Walter, Barbara F. (2002)` matches canonical author `Walter`.
- Berg's `Hegre, Håvard (2015)` matches Hegre and Nygård.
- Exact DOI, ISBN, URL, Zotero key, and Zotero relation matching work.
- Title/year/first-surname matching requires a unique candidate.
- Title fragments alone never autolink.
- A known but unmapped Zotero item receives
  `known_zotero_unmapped`.
- Adding its atomic note later upgrades the old citation without rereading the
  citing source.
- Citation links project additively and resolve in Obsidian.
- Citation direction and inverse `cited_by` are correct.
- Citation alone does not publish a `supports` or `extends` relationship.

### 7.5 Catalogue and virtual-index tests

- Identity fields remain absent from model-facing compact navigation packets.
- Collection indexes remain deterministic.
- A flat 10,000-profile synthetic library produces bounded virtual shards.
- Generic facets do not become primary shard names.
- A source can appear in multiple virtual indexes but only one atomic note.
- Shard size is capped by measured rendered context.
- One changed source rewrites only its affected source/collection/topic shards
  plus the machine catalogue.
- Cluster or relationship projection changes do not change routing revisions.
- Empty or weakly faceted sources remain discoverable through bounded
  catch-all shards.
- Existing topic-neighborhood helpers, catalogue shards, and routing cards are
  extended rather than duplicated by a second index subsystem.

### 7.6 Discovery tests

- Collection and virtual routing cards enter the router.
- The router can select top-level, child, and virtual shard pairs.
- Selected shard pairs pack by context size.
- Candidate targets are allocated per shard-pair job.
- Same-collection candidates are rejected from bridge-only jobs.
- Distinct collection-side validation is repeated when final pair jobs are
  built.
- General candidates remain allowed within collections.
- Valid candidates survive merge, deduplication, and capacity enforcement.
- Up to 96 bridge and 160 total inferred candidates are retained for the
  acceptance corpus.
- Candidate-stage losses are attributed to an exact pipeline stage.
- New-source focused discovery does not rescan the full library.
- No deterministic code invents intellectual candidates.

### 7.7 Relationship tests

- Complete notes appear once per request.
- Every validated endpoint anchor appears once per request before pair-packet
  size is reduced.
- Pair jobs refer to source document IDs.
- Endpoint claims and primary anchors are required for accepted direct links.
- Selected anchor IDs belong to the correct endpoint.
- Unknown or stale anchor IDs park only the affected job.
- v0.19 contextual shorthand normalizes only when unambiguous.
- Directional actor/reference logic and local reciprocal labels remain
  correct.
- Existing type/direction fixtures continue to pass.
- A citation or dataset reuse does not automatically become support.
- An unrelated but broadly thematic pair returns `no_relationship`.
- Partial sources can support only claims established in recovered content.
- Every completed and parked decision appears in canonical and run ledgers.

### 7.8 Cluster tests

- Virtual indexes inform cluster candidate routing.
- Cluster output cannot alter virtual routing revisions.
- A partial source may remain as theoretical or methodological context.
- A previewed result is not counted as empirical evidence.
- Fortna remains visible in the peacekeeping cluster with a bounded role.
- A genuinely irrelevant proposed member may still be dropped.
- Every retained member has a specific contribution.
- Removing one malformed member does not fail the cluster.
- Cluster-wide quantifiers remain bounded to retained members.
- Reciprocal source, cluster, and related-cluster links resolve.

### 7.9 Incremental and replay tests

- Mapping collection A and then collection B produces the same canonical
  source identities as mapping their union.
- Already processed sources receive zero new source calls.
- New collection membership alone produces zero provider calls.
- A new mapped source resolves prior citations in both directions.
- Only its candidate neighborhood is adjudicated.
- Only affected clusters are reconsidered.
- An unrelated existing cluster remains byte-identical.
- An identical global replay makes zero calls and zero writes.
- The full existing suite passes without regression.

## 8. Staged validation before the full evaluation

### 8.1 Local gate

Before any paid call:

1. inspect the implementation diff for duplicate systems or unnecessary new
   abstractions;
2. run focused tests;
3. run the full local suite;
4. build the package;
5. run `doctor`;
6. run the flat-library and duplicate synthetic benchmarks;
7. commit the implementation with `.DS_Store` excluded; and
8. require a clean tracked tree.

### 8.2 Bounded source-contract smoke test

Use five deterministic, previously problematic frozen source texts, including
one long Relapse source and the Mediation `2DRID8G3` case.

First run the saved v0.19 raw responses through local recovery. Make a v2
source call only for a case that remains parked and is intentionally included
to exercise the new provider contract. Existing hierarchical document reading
may still use its normal bounded calls for a long source; no repair call is
allowed.

Require:

- five parseable source-owned normalized bundles;
- five publishable notes;
- zero semantic or contract retries;
- correct optional-row isolation; and
- preserved raw diagnostics; and
- an explicit split between zero-call local recoveries and new provider calls.

If this gate fails, stop. Do not start the full corpus run.

### 8.3 Bounded graph-contract smoke test

Use twelve deterministic non-benchmark pairs from the frozen v0.19 corpus.
Require:

- every job returned or explicitly parked;
- valid endpoint claims;
- source-owned primary anchors on both endpoints for every accepted link;
- valid actor/reference direction; and
- no model-visible benchmark data.

Make one bounded Fortna cluster-writer call using the frozen peacekeeping
members and require evidence-bounded retention.

If either gate fails, stop before the full evaluation.

## 9. Fresh full comparative evaluation

### 9.1 Workspace and corpus

Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v020-evaluation-20260730`

Use the unchanged Zotero collections:

- Mediation `B887A4Q8`: expected 75 inventory records;
- Conflict Relapse `D2XT9ZU9`: expected 120 inventory records; and
- Combined: expected 195 inventory records before duplicate alias accounting.

Record:

- evaluated commit;
- engine, artifact, catalogue, registry, prompt, and contract identities;
- provider model and reasoning settings;
- complete read-only Zotero identity and collection snapshot;
- canonical duplicate groups;
- inventory and source-content hashes;
- configuration;
- timestamps and timezone; and
- drift relative to v0.19.

Keep all source text, evaluations, and benchmarks private. Keep the 40-pair
bridge benchmark outside every model-visible input until generation ends.

### 9.2 Source mapping

Map sequentially into the same workspace:

- `eval-mediation-v020-20260730`;
- `eval-relapse-v020-20260730`.

Use:

- DeepSeek `deepseek-v4-flash`;
- explicit cloud permission;
- `provider_concurrency=auto`;
- source reasoning `high`;
- the existing four-worker interface with provider auto-concurrency;
- OCR auto, English;
- the established v0.19 context, chunking, request, and document deadlines;
- no collection-level synthesis; and
- no semantic or contract retry.

The Relapse run must reuse any canonical source already mapped through
Mediation or an alias. Report canonical source calls separately from duplicate
aliases and limited notes.

Freeze source artifacts before graph generation.

### 9.3 One global map

Build one graph without `--source-set`:

- run ID `eval-global-v020-20260730`;
- no source regeneration;
- DeepSeek high reasoning for routing and discovery;
- DeepSeek max reasoning for relationship and cluster judgment when supported;
- `provider_concurrency=auto`;
- 7,200-second literature deadline; and
- 30-call cumulative literature ceiling.

Expected allocation:

| Stage | Expected calls |
|---|---:|
| Collection/virtual shard routing | 1 |
| General discovery | 1 |
| Routed bridge discovery | 2–4 |
| Full-note relationship adjudication | 4–6 |
| Global cluster planning | 1 |
| Concurrent cluster writers | 8–12 |
| Shared transport/overflow reserve | 5 |
| **Expected before reserve** | **17–25** |
| **Hard ceiling** | **30** |

Do not raise the ceiling or add semantic retries during the frozen run.

### 9.4 Incremental convergence exercise

After the main graph is frozen, add a deterministic local fixture representing
a later mapped Zotero collection or use an isolated clone with a small frozen
set of already available Zotero items not included in the two evaluation
collections.

Verify:

- existing atomic notes receive zero source calls;
- duplicate aliases receive zero source calls;
- old unresolved citations matching the added notes resolve;
- new citations to old notes resolve;
- focused routing considers only bounded neighborhoods;
- unrelated clusters remain byte-identical; and
- the resulting canonical identity and graph state matches an equivalent
  union build for the fixture.

Do not expose the bridge benchmark or change the main evaluation corpus.

## 10. Evaluation

### 10.1 Mechanical source and identity audit

Audit all 195 inventory records:

- terminal accounting;
- canonical source versus duplicate alias;
- collection membership union;
- source and attachment identity;
- extraction route and recovered scope;
- analytical, partial, limited, parked, and duplicate status;
- source bundle and profile structure;
- anchor and nested-locator integrity;
- catalogue and index inclusion;
- Zotero-known-unmapped citations;
- provider calls, failures, and checkpoint hits; and
- generated-file integrity.

Acceptance:

- zero pending or processing-partial records;
- at least 175/177 historically readable canonical sources represented;
- target 177/177;
- no wrong-source note;
- no duplicate provider processing;
- no invented complete-document finding;
- every provider failure preserves its raw response and exact reason;
- *Pathways for Peace* and the full thesis receive correct scope; and
- complete alias and call accounting.

### 10.2 Atomic-note audit

Reuse the frozen 30-note sample and source-first method from v0.19.

Acceptance remains:

- critical-fact recall at least 95%;
- substantive-claim support at least 95%;
- headline numeric accuracy at least 95%;
- no material invented statistic;
- no material unsupported causal upgrade;
- accurate limited and partial boundaries; and
- zero invented complete-document findings.

The source-envelope change passes only if content quality remains equivalent or
better. Locator accuracy remains advisory.

### 10.3 Citation and identity audit

Audit:

- every strong explicit citation match in the 30-note sample;
- every mapped literature-position match involving Berg;
- a deterministic 50-match sample when the full matched set is larger;
- every automatically consolidated duplicate group; and
- a deterministic sample of ambiguous and known-unmapped records.

Acceptance:

- 100% recall for exact DOI, ISBN, Zotero-key, and Zotero-relation matches;
- at least 95% recall for uniquely resolvable title/author/year matches;
- at least 99% precision across all auto-links, with zero knowingly ambiguous
  auto-link;
- Berg links to mapped Walter 2002 and Hegre 2015 notes;
- known-unmapped and absent sources are distinguished correctly;
- 100% reciprocal citation registry projection; and
- zero duplicate atomic notes for confirmed canonical work groups.

### 10.4 Virtual-index and routing audit

Audit:

- collection tree completeness;
- virtual shard labels and membership;
- shard context sizes;
- generic-label suppression;
- multi-index source references;
- routing-card usefulness;
- flat-library synthetic behavior; and
- acyclic routing revisions.

Acceptance:

- every mapped source appears in at least one collection or bounded fallback
  navigation shard;
- no virtual shard exceeds the configured context target;
- no atomic note is duplicated;
- at least 90% of a deterministic 50-source sample appears in a useful
  navigation location;
- no graph or cluster output changes an upstream routing revision; and
- a flat synthetic library remains fully routable.

### 10.5 Bridge audit

Reuse the frozen 40-pair benchmark without exposing it to generation.

Report:

- raw and eligible candidate recall;
- useful final-link recall;
- direct-only recall descriptively;
- routing-card and shard-pair coverage;
- source-endpoint coverage;
- bridge-family diversity;
- candidate capacity use;
- same-folder rejections;
- loss by pipeline stage; and
- a deterministic 20-pair non-benchmark plausibility sample.

Acceptance:

- eligible benchmark candidate recall at least 70%;
- eligible useful final-link recall at least 70%;
- non-benchmark candidate plausibility at least 70%;
- zero same-folder admission through bridge-only jobs;
- zero valid candidate lost through deterministic filtering or persistence;
- at least 80% coverage of eligible endpoints that participate in the frozen
  benchmark; and
- 100% explicit cross-folder citation/Zotero-relation recall.

### 10.6 Relationship audit

Audit every direct link when there are at most 120; otherwise use a
deterministic 100-link sample. Audit every cross-folder direct and contextual
link, every final benchmark bridge, every structurally parked row, and every
locally normalized shorthand row.

Read both complete notes before scoring:

- exact type and direction;
- proposition grounding;
- left endpoint claim accuracy;
- right endpoint claim accuracy;
- anchor adequacy and ownership;
- rationale and boundary consistency;
- contextual usefulness; and
- reciprocal projection.

Acceptance:

- all-direct type and direction at least 85%;
- cross-folder direct type and direction at least 85%;
- fully grounded direct relationships at least 85%;
- endpoint-anchor adequacy at least 85%;
- contextual usefulness at least 80%;
- structurally valid rows at least 98%;
- 100% completed-or-parked accounting in canonical and run ledgers;
- 100% explicit cross-folder recall; and
- 100% reciprocal accepted projection.

### 10.7 Cluster audit

Audit:

- every mixed-literature cluster;
- every cluster containing a partial source;
- the peacekeeping cluster;
- the eight largest remaining clusters, or every cluster when there are at
  most 20;
- every cluster containing a statistical-sample source; and
- related-cluster projections.

Score:

- membership relevance;
- member role accuracy;
- specific contribution coverage;
- source-specific claim support;
- partial-source evidence boundaries;
- statistical interpretation;
- debate and boundary accuracy; and
- reciprocal links.

Acceptance:

- membership relevance at least 90%;
- a specific contribution for every retained member;
- source-specific claim support at least 95%;
- debate and boundary accuracy at least 90%;
- every partial-source contribution remains within recovered content;
- Fortna is represented accurately in the peacekeeping literature;
- every planned cluster publishes or parks independently;
- 100% reciprocal membership links; and
- zero fabricated consensus, disagreement, or cluster-wide quantitative
  comparison.

Report unclustered sources descriptively. Do not impose a cluster-coverage
threshold.

### 10.8 Replay, incremental behavior, runtime, and cost

Snapshot before replay:

- all generated paths, hashes, sizes, and nanosecond mtimes;
- provider ledger counts;
- source, identity, catalogue, graph, and cluster semantic digests;
- registry and history event counts; and
- unresolved citation counts by status.

Replay the identical global build and require:

- zero provider calls;
- zero file additions or removals;
- zero byte changes;
- zero mtime changes;
- zero new ledger or history events; and
- zero semantic identity, graph, or cluster changes.

Report source generation, graph generation, incremental exercise, evaluation,
and replay time separately.

Acceptance:

- full production generation below four hours, target below 90 minutes;
- global literature calls at or below 25 normally and never above 30;
- no repeated completed semantic work;
- duplicate aliases cost zero source calls;
- membership-only updates cost zero provider calls;
- unchanged replay is zero-call and zero-write; and
- incremental additions do not regenerate unrelated notes or clusters.

## 11. Comparison over time

Compare v0.20 with every available evaluation while keeping incompatible
metrics separate.

### 11.1 Source and atomic quality

Extend the v0.10–v0.19 table with:

- readable canonical source success;
- duplicate aliases;
- parked count and failure stage;
- critical facts;
- claim and numeric support;
- causal upgrades;
- limited/partial scope; and
- source-contract structural validity.

### 11.2 Bridge benchmark

Preserve:

| Version | Candidate recall | Useful/final recall |
|---|---:|---:|
| v0.10 | N/A | 0/40 |
| v0.11 | 4/40 | 2/40 |
| v0.12 | 12/40 | 2/40 |
| v0.13 | N/A | 10/40 |
| v0.14 | N/A | 2/40 |
| v0.15 | N/A | 6/40 |
| v0.16 | 5/40 | 5/40 |
| v0.17 | 7/40 | 4/40 |
| v0.18 | 12/40 | 9/40 |
| v0.19 | 7/40 raw; 7/38 eligible | 6/40 raw; 6/38 eligible |
| v0.20 | Measure fresh | Measure fresh |

Report raw and eligible denominators, corpus differences, source-endpoint
coverage, and bridge-family coverage.

### 11.3 Modern relationship quality

Extend:

| Version | Cross-folder type/direction | All-direct type/direction | Fully grounded direct | Contextual usefulness |
|---|---:|---:|---:|---:|
| v0.16 | 4/6 | N/A | N/A | 5/5 |
| v0.17 | 7/8 | 59/71 | 54/71 | 3/4 |
| v0.18 | 16/20 | 53/63 | 49/63 | 13/15 |
| v0.19 | 3/4 | 24/27 | 16/27 | 18/23 |
| v0.20 | Measure fresh | Measure fresh | Measure fresh | Measure fresh |

Add endpoint-anchor adequacy for v0.19 and v0.20 where comparable.

### 11.4 Identity, indexes, incremental behavior, clusters, and efficiency

Introduce a v0.20 baseline for:

- explicit citation auto-link recall and precision;
- known-unmapped reconciliation;
- duplicate suppression;
- collection and virtual index routing quality;
- phased-run convergence;
- affected-neighborhood update behavior; and
- membership-only zero-call updates.

Extend the cluster, replay, call, and runtime table from v0.13 onward.

The narrative must distinguish:

- real semantic improvement;
- availability improvement;
- deterministic identity improvement;
- navigation improvement;
- efficiency improvement;
- changed denominators;
- equivalent quality; and
- regression.

## 12. Deliverables

Write inside the v0.20 evaluation workspace:

- `evaluation/v020-full-comparison.md`;
- `evaluation/metrics.yml`;
- `evaluation/source-metrics.yml`;
- `evaluation/atomic-metrics.yml`;
- `evaluation/identity-and-citation-metrics.yml`;
- `evaluation/duplicate-groups.yml`;
- `evaluation/index-routing-metrics.yml`;
- `evaluation/bridge-metrics.yml`;
- `evaluation/relationship-metrics.yml`;
- `evaluation/cluster-metrics.yml`;
- `evaluation/runtime-metrics.yml`;
- `evaluation/replay-metrics.yml`;
- `evaluation/incremental-convergence.yml`;
- `evaluation/atomic-sample.yml`;
- `evaluation/statistical-sample.yml`;
- `evaluation/curated-bridge-benchmark.yml`;
- `evaluation/nonbenchmark-candidate-sample.yml`;
- `evaluation/source-drift.yml`;
- `evaluation/zotero-metadata-remediation.yml`;
- pre-replay and post-replay snapshots; and
- a machine-readable replay diff.

Export a separate private Obsidian vault after replay. Its home note must link
to:

- atomic notes;
- the Zotero collection tree;
- virtual topic indexes;
- clusters;
- relationship views;
- unresolved and known-unmapped literature;
- duplicate aliases; and
- the evaluation report.

## 13. Implementation order and stop conditions

Implement in this order:

1. source provider-envelope simplification and optional-row isolation;
2. scope and split-anchor referential integrity;
3. canonical Zotero identity and duplicate aliases;
4. citation matching and later reconciliation;
5. catalogue identity/navigation split and virtual shards;
6. coverage-oriented bridge routing;
7. relationship endpoint-claim and anchor contract;
8. evidence-bounded partial-source cluster roles;
9. focused incremental invalidation and replay;
10. local tests and synthetic scale checks;
11. bounded paid smoke gates;
12. fresh full evaluation; and
13. comparative report and Obsidian export.

Stop before the full paid run if:

- the full local suite fails;
- the package build or `doctor` fails;
- any smoke source loses correct identity or core content;
- the five-source envelope smoke gate is not structurally complete;
- the twelve-pair graph smoke gate loses jobs or anchors;
- the Fortna smoke cluster invents empirical support; or
- migration changes existing atomic prose or makes provider calls.

During the frozen evaluation, do not:

- patch code or prompts;
- expose benchmark pairs;
- add semantic retries;
- raise call ceilings;
- weaken thresholds; or
- begin a second full run after a failure.

Complete the planned audit and report the exact failed stage.

## 14. Final acceptance

v0.20 is release-ready when:

- source-envelope reliability restores at least 175/177 historically readable
  sources without semantic retries;
- atomic-note quality remains at or above established thresholds;
- high-confidence duplicate Zotero entries cost one source call and produce
  one canonical note;
- explicit citations autolink accurately and reconcile when later sources are
  mapped;
- the collection and virtual index system routes both organized and flat
  libraries without whole-library prompts;
- bridge candidate and useful final recall reach 70% on the frozen eligible
  benchmark;
- relationship type, direction, grounding, and anchor adequacy reach their
  thresholds;
- partial sources contribute within explicit evidence boundaries;
- clusters remain specific and full-note-based;
- collection-scoped additions converge into one global graph;
- unrelated work is not regenerated during incremental updates;
- projections remain additive and reciprocal; and
- identical replay is byte- and mtime-stable with zero provider calls.

Minor locator drift, already-clear percentage wording, unclustered sources,
and isolated nonmaterial cluster phrasing do not fail the release. Wrong-source
notes, duplicate provider processing, ambiguous citation auto-links, invented
complete-document findings, materially unsupported relationships, lost graph
jobs, stale projections, repeated semantic work, and non-idempotent replay do.
