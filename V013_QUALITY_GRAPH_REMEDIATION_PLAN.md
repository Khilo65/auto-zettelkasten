# Auto-Zettelkasten v0.13 Quality, Graph, and Scale Remediation Plan

**Status:** Proposed; not yet implemented

**Date:** 2026-07-27

**Foundation:** Engine `0.12.0`, artifact schema `1.11`, relationship registry schema `3`

**Evidence:** The private 195-source Mediation and Conflict Relapse evaluation

**Primary objective:** Preserve the excellent source coverage of v0.12 while making publication permissive, relationships accurate and discoverable, clusters reliable, indexes scalable, and every Markdown update mechanically safe.

## 1. Executive decision

The next release should simplify the pipeline rather than add more verification
layers.

The v0.12 evaluation established that DeepSeek usually understands the sources
well:

- critical-fact recall was 100%;
- substantive-claim support was 96.9%;
- numeric support was 99.5%; and
- generated cluster claims, when clusters completed, were well supported.

The largest failures came from workflow design:

- an expensive fidelity gate parked 42 sources even though most drafts were
  useful;
- near-complete and excerpted documents were excluded from synthesis;
- relationship discovery found only 30% of curated bridge candidates and only
  5% survived into the final graph;
- a separate relationship verifier could produce a direction or type that did
  not match its rationale;
- repeated cluster proposal, reconciliation, and repair stages timed out or
  exhausted output limits; and
- collection indexes did not yet mirror the complete Zotero hierarchy.

The v0.13 design therefore adopts these rules:

1. **Generate each atomic analysis once.** One DeepSeek source-reading call
   returns the atomic analysis, compact profile, evidence anchors, important
   literature positions, and missing-source recommendations together.
2. **Do not use a second model to edit the note.** Later calls may read compact
   information or bounded passages, but they write only separate relationship
   or cluster records.
3. **Trust strong first-pass generation.** Improve the prompt upstream and use
   same-call self-review. Local fidelity checks become advisory diagnostics.
4. **Use available evidence.** Partial documents and excerpts may participate
   in relationships and clusters when their claims remain bounded to recovered
   content.
5. **Let models make intellectual judgments.** Local code may retrieve,
   constrain, store, and project. It must not decide intellectual relevance,
   relation type, direction, causality, debate, contradiction, or consensus.
6. **Make the index hierarchical and deterministic.** Mirror every Zotero
   collection and subcollection without asking a model to rewrite an index.
7. **Use one complete relationship decision.** Remove the independent
   relationship rewrite/verification stage. A correction replaces a whole
   relationship record.
8. **Use Obsidian wikilinks.** The registry is canonical; local projection adds
   reciprocal wikilinks inside managed blocks without changing atomic prose.
9. **Simplify clustering.** When the compact corpus fits safely below half of
   DeepSeek's context window, use one global cluster-planning call followed by
   one synthesis call per admitted cluster.
10. **Do not hide retries or costs.** There are no automatic semantic retries.
    Every model attempt is counted, and an unchanged replay makes zero calls.

Target release versions:

- engine `0.13.0`;
- artifact schema `1.12`;
- relationship registry schema `4`;
- source catalogue schema `3`; and
- run-ledger schema remains `2` unless implementation reveals a genuinely
  incompatible event shape.

## 2. Required outcomes

### 2.1 Source notes

- Preserve or improve the current critical-fact and substantive-claim results.
- Publish useful notes when only advisory locator, numeric, metadata, or causal
  wording warnings remain.
- Prevent only grossly unusable outputs from publication.
- Add a compact account of the most important literature each source engages.
- Never regenerate atomic prose merely to add a link, cluster, or later citation
  match.

### 2.2 Relationship graph

- Recover important within- and cross-literature relationships.
- Keep citation or engagement distinct from substantive agreement.
- Make relation type, direction, rationale, and evidence one indivisible model
  decision.
- Add readable reciprocal links to both atomic notes.
- Preserve human-authored prose and relationships.
- Grow the graph incrementally when Zotero gains new sources.

### 2.3 Clusters

- Cover the main debates and findings of the analytical corpus.
- Admit evidence-bounded partial documents.
- Produce mixed-literature clusters when the evidence supports them.
- Avoid repeated proposal/reconciliation loops for corpora that fit in one
  context.
- Isolate a failed cluster synthesis to that cluster.

### 2.4 Indexes and operations

- Give every Zotero collection and subcollection an agent- and human-friendly
  index.
- Keep every index deterministic and incrementally projected.
- Navigate a massive library without placing the complete catalogue in one
  model prompt.
- Preserve cumulative budgets, checkpoint history, registry history, and
  zero-call replay.
- Keep Zotero read-only and record recommended metadata corrections separately.

## 3. Explicit non-goals

This remediation will not:

- make Codex, Claude Code, or any other coding harness mandatory;
- ask a model to edit Markdown files;
- compare every possible source pair;
- use deterministic similarity as proof of an intellectual relationship;
- add a vector database, graph database, daemon, Obsidian plugin, or new
  third-party dependency;
- automatically edit Zotero items, collections, or attachments;
- automatically retrieve missing documents;
- implement the weekly scheduler or Research OS orchestration yet;
- regenerate old atomic notes merely to adopt the new template; or
- preserve v0.12 complexity solely for compatibility when a simpler local
  migration suffices.

## 4. Target architecture

```text
Zotero read-only inventory and collection tree
    ↓
extraction and scope assessment
    ↓
one source-reading call
    ├── atomic analysis
    ├── compact profile
    ├── evidence anchors
    ├── Position in the Literature records
    └── missing-source recommendations
    ↓
local advisory diagnostics
    ↓
atomic-note and profile commit
    ↓
deterministic collection indexes and catalogue
    ↓
model-led relationship candidate discovery
    ↓
bounded relationship job packets
    ↓
one complete adjudication per candidate pair
    ↓
canonical registry commit
    ↓
local reciprocal Obsidian wikilink projection
    ↓
one global cluster plan when context permits
    ↓
one independent synthesis per admitted cluster
    ↓
local atomic-note ↔ cluster projection
```

There are four ownership boundaries:

| Artifact | Semantic owner | Writer |
|---|---|---|
| Atomic analysis | Source-reading model | Initial local renderer only |
| Compact profile and literature positions | Source-reading model | Structured artifact writer |
| Relationship and cluster decisions | Relationship or cluster reasoner | Canonical registries |
| Markdown links and managed blocks | Canonical registries | Deterministic local projector |

No later stage is allowed to pass model-generated prose through the immutable
atomic-analysis sections.

## 5. Workstream A — One-shot atomic generation

### 5.1 One model call, one structured result

Replace the separate atomic-note, profile, and fidelity-verifier path with one
source-reading contract. For an ordinary source, one DeepSeek call returns:

```json
{
  "source_identity": {},
  "scope_assessment": {},
  "analysis_sections": {},
  "compact_profile": {},
  "evidence_anchors": [],
  "literature_positions": [],
  "missing_source_recommendations": [],
  "self_review": {}
}
```

The local pipeline validates the envelope, stores the structured components,
and renders the note. It does not ask another model to reproduce or patch the
note.

The compact profile must include:

- stable Zotero and source IDs;
- title, authors, year, and collections;
- one bounded thesis;
- one bounded method or knowledge-basis statement;
- source genre and inferential design;
- evidence scope and coverage;
- a few discriminating mechanism, outcome, case, population, period, or
  dataset facets; and
- profile and source fingerprints.

The master index is not included in this source-reading prompt. Atomic
generation must interpret the supplied source on its own terms. Library
context enters later relationship discovery, where provenance remains clear.

### 5.2 Genre- and design-aware prompt

Before drafting claims, the model must classify the supplied content as one of
the relevant forms:

- quantitative observational;
- experimental or quasi-experimental;
- qualitative or process tracing;
- mixed method;
- theoretical or conceptual;
- systematic or narrative review;
- policy or institutional report;
- legal, normative, or practitioner guidance;
- book, chapter, thesis, or excerpt;
- archival, speech, meeting, or web material; or
- another explicitly described knowledge basis.

The prompt must adapt thesis, method, evidence, findings, and limitations to
that form rather than force every source into an empirical article template.
Fields that genuinely do not apply may be empty or explicitly not applicable;
the model must not fabricate content merely because a schema previously
required every string to be non-empty.

Remove all source-specific instructions from the global prompt. In particular,
the current Kuperman-specific mediation instruction belongs in a regression
fixture, not in every source's context. General rules and diverse short
examples should replace one-off source names.

### 5.3 Upstream causal-language policy

The same prompt must select language based on research design and the source's
own claims:

| Evidence or design | Default wording |
|---|---|
| Observational quantitative | associated with, predicts, estimates, finds |
| Descriptive statistics | reports, records, describes |
| Qualitative/process tracing | the author argues, traces, interprets |
| Theoretical or normative | proposes, contends, reasons |
| Experimental/quasi-experimental | causal wording only within the identified estimand and stated assumptions |
| Practitioner guidance | recommends, advises, proposes |

The model must distinguish:

- what the source observes;
- what the author infers or argues;
- what the design can establish;
- reported mechanisms from tested mechanisms; and
- descriptive arithmetic from an identified causal effect.

Before returning, DeepSeek silently reviews the complete result for unsupported
attribution, numbers, scope, and causal wording. The review occurs inside the
same call and does not add a visible note section.

### 5.4 Context policy

Use the full recovered source directly when the complete request remains
safely below the context target.

- Never plan to use more than 50% of the one-million-token model context.
- Reserve room for instructions and output.
- For global 64,000-token outputs, target no more than approximately 430,000
  input tokens.
- For ordinary atomic outputs, use the same conservative total-context rule
  with the actual smaller output allowance.

Only genuinely oversized works use hierarchical reading:

1. divide the source into the fewest substantive chunks that fit;
2. produce one evidence-preserving memo per chunk;
3. synthesize the final unified atomic result from those memos; and
4. account for every chunk and synthesis call.

Do not hierarchically split an ordinary article or report merely because the
existing pipeline has a partitioning mechanism.

### 5.5 Position in the Literature

The same source-reading call identifies approximately three to eight works
that the author substantively engages. It must not reproduce the bibliography.

The human-facing form is intentionally compact:

```markdown
- **Walter (1997)** — Builds on Walter's commitment-problem account but argues
  that third-party guarantees alter the implementation problem. (Approx. p. 12)
```

Each machine record should store:

- raw citation text;
- normalized author, year, and title when recoverable;
- DOI, ISBN, URL, or other identifier when printed;
- one merged account of how the current source engages the cited work and what
  it contributes in relation to it;
- a compact author-facing relation label such as `builds_on`, `challenges`,
  `qualifies`, `applies`, or `contrasts`;
- an approximate locator;
- the current source ID; and
- a later `matched_source_id` when the cited work exists in the library.

Machine provenance may record whether the engagement is directly stated or a
careful source-level interpretation, but that field should not clutter the
human note.

This section is source-local evidence. It records what the current work says
about another work. It does not by itself prove that the two complete works
substantively support or undermine one another.

### 5.6 Publication policy

Rename the visible `exhausted` outcome to `parked_for_review`.

Only these gross failures prevent publication:

- empty output;
- an unusable or locally unrecoverable core envelope;
- output clearly about the wrong source; or
- absence of enough source content to produce even a bounded note.

Locator uncertainty, a missing numeric token, metadata ambiguity, a possible
causal verb, or another soft warning must not automatically:

- trigger a paid retry;
- downgrade an analytical note;
- exclude the note from the graph or clusters; or
- erase a useful draft.

There are no automatic semantic retries. A user may explicitly retry a gross
failure after inspecting the cause. A transport retry is allowed only when no
usable provider result was produced, the retry policy is explicitly enabled,
and the attempt remains visible in cumulative accounting.

## 6. Workstream B — Advisory quality diagnostics

Reuse the useful local scanners in `fidelity.py`, but stop using them as a
publication gate.

Write advisory diagnostics to a sidecar rather than into atomic prose. The
sidecar may record:

- unresolved or approximate locators;
- a number in the note not found verbatim in nearby extracted text;
- possible causal wording;
- conflicting source and parent metadata;
- low extraction coverage;
- ambiguous tables or multi-column extraction; and
- suggested human review priority.

Local checks remain authoritative only for mechanical safety:

- the structured result is readable;
- source and note IDs exist;
- managed-block markers are unambiguous;
- a relationship endpoint resolves;
- a referenced evidence-anchor ID exists;
- registry IDs are unique;
- reciprocal projections share an ID; and
- an update does not alter content outside an owned block.

They must not decide whether a source proves causality, whether two works
qualify one another, or whether a debate exists.

Exact numeric-token comparison remains an optional typo warning. Locator
checks distinguish PDF ordinal, printed page, section, table, and figure, but
page precision is a navigational aid rather than a publication requirement.

Quality assurance shifts to deterministic sampling after a run:

- draw a reproducible, source-stratified sample;
- compare the frozen source to the visible note;
- report recall, claim support, numeric support, locator usefulness, and
  material causal overstatement; and
- improve the upstream prompt when a recurring pattern appears.

## 7. Workstream C — Extraction, partial documents, and metadata

### 7.1 Separate coverage from analytical eligibility

Keep `partial_document` as a source-coverage description, not an automatic
synthesis exclusion.

A partial-document note is analytically eligible when:

- the recovered content is substantive;
- every claim is grounded in recovered content;
- missing pages or absent parent scope are disclosed; and
- the note does not claim to represent unseen sections or the complete parent
  work.

Examples:

- 39 of 40 recovered pages remain analytically usable;
- 100 of 101 or 105 of 106 recovered pages remain analytically usable;
- an 18-page book excerpt may support claims about that excerpt;
- an abstract-only record remains context-only because its evidence is
  genuinely limited to the abstract.

`partial_document_atomic_note` remains a terminal completed status, but gains
an explicit `synthesis_eligible` value derived from substantive recovered
content. Terminal means processing is complete; it does not mean excluded.

### 7.2 Classification fixes

Add regression-backed scope rules for the evaluation failures:

- A long report with extensive substantive text before its references cannot
  become `bibliography_only_attachment`.
- A chapter, introduction, appendix, executive summary, or excerpt cannot be
  labeled as the complete parent book or report merely because every attachment
  page was recovered.
- An unrelated appended page cannot establish complete-source coverage.
- A complete institutional webpage is full content when the main article body
  is present; incidental access language does not override it.
- Valid parent Zotero title, authors, editors, date, and item type take
  precedence over generic filenames such as `download_file.pdf`.
- Editors remain editors and institutional bylines retain their actual role.

Content classification and Zotero bibliographic type remain separate. A
pipeline may correctly recognize a report even when Zotero's item type is
wrong, while still recording that Zotero should be repaired.

### 7.3 Remediation ledgers

Maintain two read-only remediation surfaces:

1. `zotero_metadata_issues.yml` for probable Zotero corrections.
2. `pipeline_classification_issues.yml` for extraction or classifier defects.

A Zotero metadata issue should contain:

- Zotero item and attachment keys;
- current title, creators, date, and item type;
- recommended correction;
- evidence for the recommendation;
- confidence and ambiguity;
- status such as `open`, `confirmed`, `fixed_in_zotero`, or `dismissed`; and
- last observed Zotero version.

No remediation command may mutate Zotero in v0.13.

## 8. Workstream D — Missing-source and acquisition memory

Important cited works that are not in Zotero or lack an atomic note enter a
durable recommendation ledger. Do not invent an atomic note for them.

Each record should include:

- stable external-source ID;
- raw and normalized citation;
- DOI, ISBN, URL, and other identifiers;
- every source that cited or substantively discussed it;
- why it appears important;
- relevant collection, topic, or cluster;
- acquisition priority;
- match and retrieval status;
- ambiguity notes; and
- eventual Zotero key, source ID, and note ID.

Matching order:

1. DOI or another strong identifier;
2. normalized exact title plus compatible author/year;
3. strong title/author/year fuzzy evidence; and
4. unresolved review when multiple candidates remain.

When a later Zotero addition matches this ledger, older literature-position
records immediately become candidate links. The system does not need to rescan
every old source.

The schema should be compatible with later Research OS retrieval workflows,
but v0.13 implements only recording, matching, and status transitions.

## 9. Workstream E — Hierarchical, agent-friendly indexes

### 9.1 Mirror the Zotero collection tree

Use the existing read-only collections endpoint and its `parentCollection`
field to snapshot the complete collection hierarchy.

Generate:

```text
02_source_memory/indexes/
├── INDEX.md
├── source_catalogue.yml
├── collections/
│   ├── COLLECTION_KEY_A/
│   │   ├── INDEX.md
│   │   └── sources-001.md
│   └── COLLECTION_KEY_B/
│       ├── INDEX.md
│       └── sources-001.md
└── source_sets/
```

Stable Zotero keys, not mutable collection names, determine canonical
directories. The root index renders the parent-child tree and links to each
collection page. Moving or renaming a Zotero collection updates navigation
without changing source identities.

Every collection and subcollection index includes:

- collection name and stable key;
- parent and child collection links;
- direct source count;
- descendant source count;
- direct versus inherited membership;
- links to bounded source shards;
- compact source entries;
- relevant cluster links;
- processed, context-only, partial, parked, and missing-source counts; and
- catalogue revision.

A source has one canonical atomic note and profile. Multiple collection indexes
may reference it without duplicating source artifacts. A source that belongs to
multiple Zotero collections appears in each relevant index with the same IDs.

### 9.2 Compact source entry

Each source entry contains only:

- Zotero key and canonical note wikilink;
- title, author, and year;
- one compact thesis;
- one compact method or knowledge-basis statement;
- source scope and evidence coverage; and
- at most a few discriminating facets.

Detailed findings, all evidence anchors, full relationship rationales, and
complete literature reviews stay out of the index.

The existing deterministic catalogue behavior should be preserved:

- build entries from stored profiles and Zotero snapshots;
- sort stably;
- render without provider calls;
- write only byte-changed files;
- keep unchanged replays byte-identical; and
- update only affected collection shards plus necessary aggregate navigation.

Regenerating an aggregate catalogue locally is safe because no model rewrites
it. A database should be considered only if measured catalogue size or update
time becomes a real bottleneck.

### 9.3 Progressive context routing

The master index is a navigation surface, not a model prompt.

For relationship or cluster work:

1. If the selected compact catalogue fits within the safe context target, send
   it directly.
2. Otherwise show the reasoner bounded collection and shard routing cards.
3. Let the reasoner select relevant shards.
4. Load compact profiles only from selected shards.
5. Load bounded atomic passages only for selected candidate pairs.

If routing cards themselves eventually exceed the context target, use broad
local retrieval over title, author, thesis, method, facets, citations, graph
neighbors, and collection membership. This local step retrieves possible
context; it does not declare an intellectual relationship. The reasoning model
still selects and adjudicates candidates.

This hierarchy must support libraries containing tens of thousands of sources
without requiring a whole-library prompt.

## 10. Workstream F — Relationship discovery and adjudication

### 10.1 Candidate discovery

Candidate discovery should use:

- compact profiles;
- literature-position records;
- exact and probable citation matches;
- existing graph neighbors;
- cluster membership and compact cluster summaries;
- prior accepted and rejected pair memory; and
- collection boundaries without treating them as intellectual boundaries.

For a corpus whose complete compact catalogue fits the safe context target,
use one global discovery call. Its response has separate capacity for:

- within-literature candidates; and
- cross-literature bridge candidates.

Same-collection candidates cannot consume the reserved bridge capacity.

For an incremental batch, the discovery call receives the new profiles,
matched citations, relevant graph memory, and the selected catalogue context.
After parallel atomic generation completes, one catch-up discovery pass covers
new-to-new pairs that could not have been considered earlier.

Deterministic code may:

- resolve explicit identifiers;
- assemble and cap context;
- retrieve an intentionally broad pool at very large scale;
- deduplicate canonical pairs;
- exclude metadata-only sources;
- remember unchanged negative decisions; and
- enforce call and context budgets.

It must not decide that two works support, undermine, qualify, extend, or
otherwise relate.

### 10.2 Relationship job packet

Every candidate pair becomes a bounded, immutable job packet. The same packet
can be sent to DeepSeek or inspected by an optional coding-harness agent.

```json
{
  "job_id": "relationship-job-...",
  "catalogue_revision": "...",
  "pair": {
    "left_source_id": "...",
    "right_source_id": "..."
  },
  "profiles": {
    "left": {},
    "right": {}
  },
  "literature_positions": [],
  "selected_evidence": {
    "left": [],
    "right": []
  },
  "graph_context": {},
  "candidate_basis": [],
  "prior_pair_memory": {},
  "output_contract": "relationship-decision-v3"
}
```

The packet normally contains compact profiles plus selected evidence anchors,
not two complete atomic notes. When the bounded evidence is genuinely
insufficient, the initial adjudicator may return `needs_more_context`. The item
is parked for explicit follow-up; the system does not automatically spend
another call.

Packets and results live under the run state so an agent can audit them without
reconstructing hidden prompt context.

### 10.3 One complete adjudication

Remove the separate v0.12 relationship verifier. One adjudication response is
the final probabilistic judgment:

```json
{
  "decision": "relationship",
  "pair": {
    "left_source_id": "...",
    "right_source_id": "..."
  },
  "relation_type": "extends",
  "actor_source_id": "...",
  "reference_source_id": "...",
  "forward_label": "extends",
  "inverse_label": "extended by",
  "comparison_proposition": "...",
  "reason": "...",
  "left_evidence_anchor_ids": [],
  "right_evidence_anchor_ids": [],
  "boundary_or_qualification": "...",
  "confidence": "high"
}
```

The alternative is a complete `no_relationship` decision with a bounded
reason. The model silently checks before returning that:

- actor and reference direction match the natural-language proposition;
- the relation type matches the reason;
- both works are characterized accurately;
- citation chronology is not confused with intellectual direction;
- a shared topic, method, case, or dataset is not mistaken for a substantive
  relationship; and
- causal, lineage, support, and qualification claims are grounded.

A correction replaces this entire record. It may not modify only the type,
direction, or label while preserving an old rationale.

Local validation checks IDs, pair membership, anchor existence, schema,
registry uniqueness, and projection safety. It does not semantically reinterpret
the relationship. An invalid row is parked without a retry and without
discarding valid sibling rows.

### 10.4 Citation is not agreement

The graph distinguishes:

- `cites` or `engages`, established from the current source;
- substantive `supports`, `undermines`, `qualifies`, `extends`,
  `complements`, or `contrasts`, requiring both works; and
- navigation-only similarity, which remains an invisible retrieval signal
  unless a human asks to expose it.

When an important literature-position citation matches an existing note, the
source-local `Position in the Literature` projection may link to that note
immediately. A substantive typed relationship appears only after adjudication.

### 10.5 Registry behavior

Preserve the strongest v0.12 registry features:

- stable event IDs and complete history;
- accepted, rejected, parked, and no-relationship memory;
- retirement lineage;
- human-authored relationships kept separate and active;
- profile, prompt, model, and catalogue fingerprints;
- cumulative call accounting;
- registry commit before Markdown projection; and
- zero-call unchanged replay.

A successful `no_relationship` decision retires an older machine relationship
for that pair under the same evidence revision. Provider or parsing failure
never retires an existing edge.

## 11. Practical Obsidian linking

### 11.1 Link format

Use Obsidian wikilinks for internal note-to-note and note-to-cluster links.
Do not duplicate them with native Markdown links. Wikilinks integrate directly
with Obsidian's graph, backlinks, aliases, and rename behavior.

The relationship registry stores stable source and note IDs. The local
projector resolves each target to its canonical vault-relative note path or
unique note stem and renders it inside the existing managed block.

For example, if Work A extends Work B, Work A receives:

```markdown
## Graph Links

<!-- auto-zettelkasten:graph:start -->
### Relationships

- extends: [[canonical/path/to/work-b|Work B]] — Work A extends Work B's
  commitment account by identifying a monitoring mechanism.
  <!-- relation_id: substantive-relation-123 -->
<!-- auto-zettelkasten:graph:end -->
```

Work B receives the reciprocal projection:

```markdown
<!-- auto-zettelkasten:graph:start -->
### Relationships

- extended by: [[canonical/path/to/work-a|Work A]] — Work A extends Work B's
  commitment account by identifying a monitoring mechanism.
  <!-- relation_id: substantive-relation-123 -->
<!-- auto-zettelkasten:graph:end -->
```

Both entries come from the same registry record and share the same relation
ID. The model never writes either line.

### 11.2 Literature-position links

`Position in the Literature` is rendered from stable structured engagement
records. When the cited work is already matched, its label becomes a wikilink.
When it is missing, the same prose remains plain text and points to the
missing-source ledger through machine metadata.

Use a separate owned region so later identifier resolution changes only the
link target:

```markdown
## Position in the Literature

<!-- auto-zettelkasten:literature:start -->
- **[[canonical/path/to/walter-1997|Walter (1997)]]** — Builds on Walter's
  commitment-problem account but argues that third-party guarantees alter the
  implementation problem. (Approx. p. 12)
<!-- auto-zettelkasten:literature:end -->
```

If the work is added later, local projection changes only the target rendering
inside the managed literature-position region. It does not ask a model to
rewrite the engagement statement.

### 11.3 Atomic-to-cluster reciprocity

An admitted atomic member receives:

```markdown
### Clusters

- member of: [[canonical/path/to/cluster|Commitment Problems]]
```

The cluster's managed member block links back:

```markdown
## Members

<!-- auto-zettelkasten:members:start -->
- [[canonical/path/to/atomic-note|Walter (1997)]] — foundational commitment
  problem account
<!-- auto-zettelkasten:members:end -->
```

Neighboring clusters use the same reciprocal-ID rule as atomic relationships.

### 11.4 File safety

Projection must:

- update only explicit managed blocks;
- preserve all bytes outside those blocks when practical;
- preserve semantic hashes unconditionally;
- leave human-authored relationships untouched;
- park malformed or ambiguous managed blocks for review;
- write registry state before note projections;
- update only affected notes;
- repair an interrupted projection from the registry on resume; and
- produce no writes on unchanged replay.

## 12. Optional coding-harness agents

A Codex or Claude Code agent is another probabilistic reasoner with filesystem
tools; it does not inherently possess the complete project context. Its value
is that it can navigate the hierarchical indexes, inspect multiple artifacts,
and investigate ambiguous or failed work.

The ordinary product must still work with DeepSeek alone. v0.13 should lay the
minimum backend-neutral groundwork:

- persist complete relationship job packets and result schemas;
- make pending and parked packets discoverable from run state;
- accept a schema-valid externally produced result through the same registry
  ingestion path; and
- record `reasoner_backend`, model identity, and provenance.

The practical exchange layout is:

```text
11_state/runs/RUN_ID/relationship_jobs/JOB_ID/
├── input.json
├── result.json
└── status.yml
```

The built-in DeepSeek backend reads `input.json` and writes `result.json`. An
optional harness agent may read the same immutable input and write only that
job's isolated result. On `resume`, the normal ingestion path validates any
completed result, commits registry events, and projects links. The agent never
writes a note or canonical registry directly, and ingesting an existing result
costs no provider call.

Suitable harness-agent work includes:

- processing a bounded set of ambiguous relationship packets;
- auditing a deterministic sample;
- reviewing metadata-remediation recommendations;
- diagnosing provider failures; and
- coordinating CLI runs.

Agents must not directly edit atomic notes, indexes, registries, or clusters.
They return structured results, and the same local commit/projection path
applies.

Parallelism is safe only across isolated artifacts:

- DeepSeek generates separate source records with the existing worker pool.
- Relationship workers or agents write separate job results.
- A single deterministic merge commits registry events.
- Read-only audits may run concurrently.
- Cluster refresh begins after the relevant relationship revision is committed.

Direct Codex/Claude orchestration, scheduler integration, and automatic agent
spawning remain later features. The persisted packet protocol prevents that
later work from requiring another graph redesign.

## 13. Workstream G — Simplified clustering

### 13.1 One global plan when the corpus fits

For the current 122-profile analytical corpus, replace repeated partition
proposals and reconciliation with one cluster-planning call.

Input:

- all eligible compact profiles;
- accepted substantive relationships;
- important matched literature-position records;
- collection identities;
- prior compact cluster summaries when refreshing; and
- explicit instructions to find mixed-literature debates.

Output:

- cluster ID and concise title;
- shared question;
- member source IDs;
- core source IDs;
- short membership rationale;
- neighboring cluster IDs and relationship;
- unclustered analytical source IDs; and
- a concise reason for sources intentionally left unclustered.

Operational settings:

- conservative input target of approximately 430,000 tokens;
- no more than half of the one-million-token total context;
- output allowance up to 64,000 tokens when the configured DeepSeek endpoint
  supports it;
- 600-second request timeout;
- existing two-hour overall literature deadline; and
- one concise structured response without repeated source summaries.

Provider capability and configured output limit must be checked during
preflight. Do not silently request 16,000 tokens for a contract known to need
more output.

### 13.2 Independent cluster synthesis

After the plan:

1. perform only mechanical ID, duplicate-membership, and context checks;
2. admit the model-selected cluster families;
3. make one synthesis call per admitted cluster using only member evidence and
   relevant relationships; and
4. project membership and neighboring-cluster links locally.

The synthesis prompt performs its own same-call review. Remove automatic
cluster quality-repair calls. An unusable synthesis parks only that cluster.
Valid sibling clusters remain visible.

If an existing cluster refresh fails, retain the last valid note and display
`Update pending`. The registry may retain the compatibility field
`refresh_pending`, but the human meaning must be explicit: a newer version
could not be completed; the prior valid cluster was not erased.

### 13.3 Large-library fallback

Introduce sharded planning only after measured compact input exceeds the safe
context target:

1. plan clusters within selected collection or literature shards;
2. produce compact local cluster summaries;
3. make one cross-shard bridge-planning call over those summaries; and
4. synthesize admitted clusters independently.

Do not restore repeated proposal, reconciliation, coverage-repair, and retry
loops. The fallback adds only the minimum hierarchy required by measured
context size.

### 13.4 Cluster quality rules

- Partial documents may be members when their available evidence is relevant.
- Metadata-only and abstract-only records remain context, not evidence-bearing
  core members.
- Shared vocabulary, geography, method, or collection is not enough.
- Every substantive cluster claim cites member evidence.
- Debate, contradiction, qualification, and consensus remain probabilistic
  judgments.
- A contradiction requires genuinely comparable propositions.
- Absence of evidence is not automatically a research gap.
- Atomic↔cluster and neighboring-cluster projections are reciprocal.

## 14. Cost, call, and failure policy

### 14.1 Expected call shape

For an ordinary source:

| Stage | Calls |
|---|---:|
| Atomic analysis + profile + literature position | 1 |
| Fidelity verifier | 0 |
| Separate profile generation | 0 |
| Local diagnostics and projection | 0 |

An oversized source uses the minimum necessary chunk calls plus one synthesis,
all reported.

For a 195-source combined literature run expected to fit the global context:

| Literature stage | Expected maximum |
|---|---:|
| Global relationship candidate discovery | 1 |
| Batched relationship adjudication | 20 |
| Global cluster planning | 1 |
| Independent cluster synthesis | 15 |
| Collection-wide gap assessment, if retained | 1 |
| Shared manual/transport reserve | 13 |
| **Expected ceiling** | **51** |

The public hard literature-synthesis ceiling remains 100. The lower internal
target makes the ceiling a safety boundary rather than a spending goal.

Atomic generation remains under the existing 250-call source/profile ceiling,
but the ledger must clearly count the actual source-reading and hierarchical
calls. There must be no hidden profile or verifier totals.

### 14.2 Failure behavior

- No automatic semantic retries.
- Locally recover harmless JSON framing without a model call.
- A complete but warning-bearing response is published.
- A grossly unusable response is parked with its source, prompt, attempt, and
  provider evidence preserved.
- A transport retry occurs only under explicit retry policy and is counted.
- A timeout or truncation in one relationship batch parks that batch without
  deleting earlier graph state.
- A failed cluster affects only that cluster or, for the global planning call,
  leaves the last valid cluster map visible.
- An unchanged replay never retries a terminal unchanged failure.
- Changing source, prompt, model, context, or explicit retry intent creates a
  new fingerprint eligible for another attempt.

## 15. Incremental Zotero growth groundwork

Implement the groundwork for later weekly automation:

- stable Zotero inventory snapshots;
- complete collection-tree snapshots;
- source, attachment, and collection fingerprints;
- deterministic library diffs;
- durable `last_processed` state;
- idempotent processing of new or changed sources;
- missing-source ledger matching;
- affected relationship and cluster IDs; and
- a resumable incremental command.

Incremental batch flow:

1. detect new and changed Zotero records read-only;
2. generate new atomic records in parallel;
3. commit one stable catalogue revision;
4. discover new-to-existing and new-to-new candidates;
5. adjudicate bounded packets;
6. project reciprocal links;
7. refresh affected clusters; and
8. update the processed inventory only after successful local commits.

The weekly heartbeat, notifications, document retrieval, and Research OS
workflow invocation are deferred. Later automation should call the same
idempotent command rather than introduce a second sync implementation.

## 16. Interfaces and compatibility

### 16.1 Model and status changes

- Keep `SourceScope.partial_document`.
- Make partial-document synthesis eligibility independent of source scope.
- Replace visible `exhausted` with `parked_for_review`.
- Add structured literature-position and missing-source records.
- Replace preliminary-plus-verified relationship output with one complete
  relationship-decision record.
- Preserve custom reasoners through capability detection.

A custom reasoner that cannot return the v0.13 complete relationship record may
still produce candidates, but those candidates remain parked and invisible.

### 16.2 Migration

Migration is local, lazy, idempotent, and provider-free.

- Accept v0.11 and v0.12 workspaces.
- Preserve all old atomic prose.
- Preserve human-authored links.
- Keep v0.12 relationship history.
- Do not remove a visible v0.12 machine edge until its exact pair is
  successfully re-adjudicated.
- Mark old machine decisions with their original prompt/model provenance.
- Recover v0.12 fidelity-parked drafts locally when the stored output is
  structurally usable and about the correct source; retain warnings.
- Require an explicit new generation only for the two truncated or otherwise
  grossly unusable outputs.
- Make existing partial-document notes synthesis-eligible when their stored
  evidence supports it; do not regenerate them.
- Do not backfill `Position in the Literature` for old notes automatically.
  It appears on newly generated or explicitly reprocessed sources.
- Build the collection hierarchy on the next explicit Zotero inventory or sync,
  not during schema migration.
- Make no Zotero or cloud calls during migration itself.

## 17. Implementation sequence

### Phase 0 — Preserve the baseline

- Commit the current v0.12 implementation and evaluation fixtures separately
  from v0.13 changes.
- Freeze the current 195-source evaluation evidence and known-failure fixtures.
- Record current call counts, hashes, and representative bad relationships.

Success gate: v0.12 tests remain reproducible before remediation begins.

### Phase 1 — Replace the atomic publication path

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/profiles.py`
- `src/auto_zettelkasten/fidelity.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- implement the unified source-reading contract;
- remove the separate paid profile and fidelity calls from new runs;
- add genre/design and same-call self-review instructions;
- remove source-specific global-prompt content;
- make diagnostics advisory;
- add `parked_for_review`; and
- recover usable v0.12 drafts without provider work.

Success gate: ordinary sources use one call, soft warnings publish, atomic prose
is never rewritten by a later model stage, and an unchanged replay is call-free.

### Phase 2 — Repair extraction and metadata accounting

Primary files:

- `src/auto_zettelkasten/extraction.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/indexes.py`

Changes:

- separate coverage from synthesis eligibility;
- allow evidence-bounded partials and excerpts into synthesis;
- fix report, chapter, appended-page, HTML, and parent-metadata cases; and
- write separate Zotero and pipeline remediation ledgers.

Success gate: all four partial-document fixtures are analytically usable where
appropriate, and every known classification failure is correctly represented.

### Phase 3 — Add literature positions, missing-source memory, and collection indexes

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/indexes.py`
- `src/auto_zettelkasten/zotero.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- store and render compact literature-position records;
- match cited works to canonical sources;
- create the missing-source recommendation ledger;
- snapshot the complete Zotero collection hierarchy; and
- generate a deterministic index for every collection and subcollection.

Success gate: adding one source changes only its canonical artifacts, relevant
collection shards, root navigation, and any matched literature-position links.

### Phase 4 — Simplify relationship reasoning

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/relationships.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- improve global and incremental candidate discovery;
- persist agent-friendly job packets;
- remove the separate verifier;
- add one complete direction-safe decision record;
- ingest DeepSeek or externally produced schema-valid results through one path;
- preserve negative memory and retirement lineage; and
- project reciprocal Obsidian wikilinks.

Success gate: direction, type, reason, and evidence cannot be updated
independently; all visible relationships are reciprocal and source-grounded.

### Phase 5 — Simplify clustering

Primary files:

- `src/auto_zettelkasten/literature.py`
- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- use one global plan below the context threshold;
- allow a 64,000-token output and 600-second request deadline;
- remove unnecessary proposal/reconciliation/repair loops;
- synthesize each admitted cluster independently;
- include partial-document evidence;
- isolate failures; and
- enforce reciprocal atomic and neighboring-cluster projections.

Success gate: the 122-profile corpus reaches synthesis without partition
proposal loops, produces fresh combined clusters, and preserves valid siblings
when one synthesis fails.

### Phase 6 — Incremental sync groundwork

Primary files:

- `src/auto_zettelkasten/zotero.py`
- `src/auto_zettelkasten/api.py`
- `src/auto_zettelkasten/cli.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/migration.py`

Changes:

- add stable inventory and collection diffs;
- persist last-processed state;
- resume new-source processing idempotently;
- connect missing-source matches to candidate discovery; and
- expose affected graph/cluster refresh state.

Success gate: adding a Zotero source and rerunning the incremental command
creates exactly one canonical note and only affected graph/index/cluster writes.

## 18. Test plan

### 18.1 Atomic generation and publication

- One ordinary source produces analysis, profile, anchors, literature positions,
  and recommendations in one call.
- No separate fidelity or profile call occurs.
- The global prompt contains no source-specific research case.
- Quantitative observational fixtures use associational wording.
- Qualitative, theoretical, policy, and normative fixtures use appropriate
  knowledge-basis language.
- Non-applicable fields do not force fabricated prose.
- Same-call self-review corrects supplied causal, numeric, and attribution
  traps.
- Locator, numeric, and causal warnings do not trigger retries or suppress a
  usable note.
- Empty, wrong-source, and unrecoverable outputs become `parked_for_review`.
- A usable v0.12 fidelity-parked draft is recovered locally.

### 18.2 Extraction and metadata

- 39/40, 100/101, and 105/106 page records remain synthesis-eligible.
- A book excerpt is analyzed as an excerpt.
- `Pathways for Peace` is recognized as a substantive UN report.
- A bibliography-only attachment remains context-only.
- An unrelated appended page does not define source scope.
- Complete institutional HTML is recognized.
- Parent metadata overrides a generic attachment filename.
- Editors and institutional authors retain correct roles.
- Zotero and pipeline issues enter separate ledgers without Zotero writes.

### 18.3 Literature positions and missing sources

- Only the most important three to eight engaged works are retained.
- DOI matching takes precedence.
- Exact title/author/year matching works.
- Ambiguous fuzzy matches remain unresolved.
- An existing source produces a wikilink without changing engagement prose.
- A missing work enters the acquisition ledger.
- Adding that work later resolves the older record and creates a relationship
  candidate without rescanning every source.
- Citation does not automatically become support or extension.

### 18.4 Index hierarchy and scaling

- Every Zotero collection and subcollection receives an index.
- Parent-child links reflect `parentCollection`.
- Direct and descendant memberships remain distinct.
- Multi-collection sources retain one canonical note/profile.
- Unchanged index replay is byte-identical.
- Updating one profile does not rewrite unrelated collection shards.
- A catalogue under the safe threshold is supplied directly.
- A larger catalogue routes through collection/shard cards.
- A synthetic tens-of-thousands-source catalogue never enters one model
  context.
- Local retrieval never emits a substantive relation by itself.

### 18.5 Relationship semantics and projection

- Genuine support, undermining, qualification, extension, complementarity, and
  contrast fixtures pass.
- Adjacent but non-equivalent constructs return no relationship.
- Pooled evidence is not attributed to a subgroup.
- Intellectual lineage is not invented.
- A relation does not strawman another work.
- Shared method, case, dataset, or vocabulary alone stays navigation-only.
- Actor, reference, type, inverse label, proposition, reason, and evidence are
  returned together.
- A correction replaces the complete record.
- There is no independent verifier capable of changing only direction or type.
- Schelling–Smith/Stam, Carnegie–Hartzell, and McAuliffe–Hampson regressions
  produce directionally coherent outcomes.
- One malformed row is parked without a retry or sibling loss.
- `no_relationship` retires an older machine edge but not a human link.
- Obsidian wikilinks resolve.
- Reciprocal projections share one relation ID.
- Atomic prose and human-authored sections remain unchanged.

### 18.6 Clusters

- The 122-profile fixture uses one global planning call.
- Preflight allows the intended 64,000-token output when supported.
- The call receives the configured 600-second deadline.
- No partition/reconciliation loop occurs below the threshold.
- Mixed-literature cluster fixtures emerge.
- Partial-document members contribute only recovered evidence.
- Every claim resolves to member evidence.
- Membership relevance fixtures reject generic topical adjacency.
- One failed synthesis does not affect siblings.
- A last-valid cluster displays `Update pending` after a failed refresh.
- Atomic membership and neighboring-cluster IDs are reciprocal.

### 18.7 Calls, replay, and agents

- All provider attempts appear in one cumulative ledger.
- There are no hidden profile, verifier, repair, or retry calls.
- The 100-call literature ceiling remains hard across resumes.
- An unchanged terminal item makes zero calls.
- Explicit retry affects only requested gross failures.
- DeepSeek and external-agent results use the same ingestion path.
- Parallel workers cannot interleave registry projection.
- An unchanged combined replay makes zero calls and no generated-artifact
  writes.

Run focused tests first, then `ruff check src tests`, then the complete existing
test suite. No acceptance run begins until all regression tests pass.

## 19. Final private acceptance run

Run a fresh evaluation against the same frozen, non-overlapping Zotero
collections:

- Mediation `B887A4Q8`;
- Conflict Relapse `D2XT9ZU9`; and
- the combined 195-source workspace.

Use the same privacy, DeepSeek authorization, four-worker source generation,
250-call source ceiling, 100-call literature ceiling, and two-hour literature
deadline. Do not weaken limits or increase spending during the run.

### 19.1 Mechanical audit

Validate all 195 records for:

- complete terminal accounting;
- extraction route and coverage;
- note structure;
- profile and anchor integrity;
- collection and subcollection indexing;
- missing-source and metadata ledgers;
- relationship endpoint and anchor resolution;
- reciprocal wikilinks;
- cluster membership reciprocity;
- call accounting; and
- unchanged replay.

### 19.2 Deep atomic audit

Repeat the deterministic 30-note, 15-per-collection audit stratified by source
type, extraction route, full/partial scope, and analytical/context status.

Hard release criteria:

- critical-fact recall at least 85%;
- substantive-claim support at least 95%;
- numeric factual support at least 95%;
- zero material unsupported causal upgrades;
- correct full, partial, excerpt, abstract-only, and metadata-only scope;
- zero invented complete-document findings; and
- gross `parked_for_review` sources below 5%.

Exact page-number accuracy becomes a reported diagnostic rather than a hard
publication or release gate. The hard locator requirement is that important
claims provide a useful, resolvable approximate page, section, table, figure,
or text anchor. Minor ordinal-versus-printed-page discrepancies do not fail an
otherwise accurate knowledge note.

Minor causal phrasing remains reported, but only an upgrade that changes the
substantive interpretation of the evidence is a hard causal failure. No
per-note verifier or retry is allowed to manufacture a passing result.

### 19.3 Relationship audit

- Check every explicit Zotero and matched important-citation link.
- Independently curate 40 plausible bridge pairs before inspecting output.
- Require candidate-stage curated bridge recall of at least 85%.
- Require final curated bridge recall of at least 70%.
- Require inferred substantive-link precision of at least 85%.
- Require explicit-link recall of 100% when a testable in-corpus denominator
  exists.
- Require every visible reason to be grounded in both source records.
- Require 100% reciprocal visible relationships and zero unresolved generated
  wikilinks.
- Manually inspect relation direction, type, and rationale together.

### 19.4 Cluster audit

Audit every mixed-literature cluster and neighboring relationship, plus the
largest single-literature clusters.

Require:

- membership relevance at least 90%;
- core-source coverage at least 90%;
- cluster-claim support at least 95%;
- zero fabricated debates, contradictions, or consensus;
- meaningful coverage of the analytical corpus;
- at least one mixed-literature cluster when supported by the frozen bridge
  ground truth; and
- no stale all-library cluster map after a successful run.

### 19.5 Pipeline audit

Require:

- zero pending or processing-partial items;
- completed evidence-bounded partial documents do not count as pending;
- gross parked items below 5%;
- complete provider and harness-reasoner accounting;
- literature calls at or below 100;
- no automatic semantic retries;
- stable semantic, registry, catalogue, and projection hashes; and
- zero provider calls and zero generated-artifact rewrites on unchanged replay.

Any missed hard criterion produces a failed or qualified verdict with
source-linked examples, the likely pipeline stage, and a prioritized defect
list. Advisory locator and minor wording diagnostics remain visible but do not
retroactively suppress useful atomic notes.

## 20. Definition of done

The remediation is complete when:

1. An ordinary new Zotero source is read once by DeepSeek and produces one
   source-faithful atomic note, compact profile, evidence set, literature
   position, and missing-source recommendations.
2. Soft diagnostics are recorded without a retry or publication veto.
3. Substantive recovered content remains usable even when one page or the
   complete parent work is absent.
4. The canonical source appears in every relevant Zotero collection and
   subcollection index without duplication.
5. Relationship discovery navigates the appropriate compact catalogue rather
   than loading the complete library blindly.
6. One complete probabilistic decision defines each intellectual relationship.
7. The local projector adds reciprocal Obsidian wikilinks to the relevant
   atomic notes without changing their analytical prose.
8. Important cited works missing from the library become durable acquisition
   recommendations and link automatically when later added.
9. A corpus fitting the safe context target receives one global cluster plan
   and independent evidence-grounded cluster syntheses.
10. One failed relationship packet or cluster cannot erase valid graph,
    index, note, or cluster state.
11. An optional coding-harness agent can inspect or answer bounded job packets
    without becoming a required runtime dependency or editing Markdown.
12. The frozen 195-source evaluation passes the hard atomic, relationship,
    cluster, accounting, and replay criteria within the existing call ceilings.
