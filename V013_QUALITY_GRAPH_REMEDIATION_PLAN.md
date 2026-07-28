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
- evidence-profile schema `1.3`;
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
    ↓
canonical SourceAnalysisBundle
    ├── atomic analysis
    ├── compact profile
    ├── evidence anchors
    ├── Position in the Literature records
    └── missing-source recommendations
    ↓
local advisory diagnostics
    ↓
source-owned bundle commit and deterministic projections
    ↓
deterministic collection indexes and catalogue
    ↓
model-led relationship candidate discovery
    ↓
bounded relationship job packets
    ↓
provider batches containing independent pair jobs
    ↓
one complete decision record per candidate pair
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
| SourceAnalysisBundle | Source-reading model | Source-owned structured artifact writer |
| Atomic note, profile, and literature positions | SourceAnalysisBundle | Deterministic projectors |
| Relationship and cluster decisions | Relationship or cluster reasoner | Canonical registries |
| Markdown links and managed blocks | Canonical registries | Deterministic local projector |

No later stage is allowed to pass model-generated prose through the immutable
atomic-analysis sections.

## 5. Workstream A — One-shot atomic generation

### 5.1 One model call, one source-owned bundle

Replace the separate atomic-note, profile, and fidelity-verifier path with one
source-reading contract. For an ordinary source, one DeepSeek call returns a
canonical `SourceAnalysisBundle`:

```json
{
  "bundle_schema_version": "1",
  "source_identity": {},
  "observed_bibliographic_identity": {},
  "scope_assessment": {},
  "analysis_sections": {},
  "compact_profile": {},
  "evidence_anchors": [],
  "literature_positions": [],
  "missing_source_recommendations": [],
  "self_review": {}
}
```

The bundle belongs to the source, not to a collection, source set, map, or
cluster run. Its semantic fingerprint contains:

- extracted source-content fingerprint;
- extraction and scope-policy revision;
- source-reading prompt and bundle-contract version;
- provider and model identity; and
- source-reading settings that can affect meaning.

Collection membership, source-set ID, graph revision, cluster revision, and
Markdown link rendering are excluded. Canonical display title, creators, year,
and item type are also projection metadata rather than source-analysis
dependencies. Moving the same source between Zotero collections or correcting
its Zotero metadata therefore cannot invalidate paid source-reading work.

`source_identity` contains stable Zotero/source/attachment IDs. The model may
return `observed_bibliographic_identity` from the document body as diagnostic
evidence, but it is not allowed to overwrite the canonical record. Current
Zotero parent-item metadata is the default bibliographic authority, with
source-body discrepancies recorded in the metadata-remediation ledger for
human correction.

The local pipeline validates the envelope, stores the bundle once, and
deterministically projects the atomic note, evidence profile, literature
positions, and acquisition recommendations. It does not ask another model to
reproduce or patch the note.

Validation is component-isolated:

- a valid atomic analysis publishes even if an optional literature-position or
  acquisition-recommendation row is malformed;
- valid anchors and profile fields survive a malformed optional sibling row;
- malformed optional rows are parked separately with diagnostics; and
- only an unusable source identity or atomic analysis can park the complete
  source.

The compact profile must include:

- stable Zotero and source IDs;
- canonical title, authors, and year joined deterministically from the current
  Zotero/parent metadata projection;
- one bounded thesis;
- one bounded method or knowledge-basis statement;
- source genre and inferential design;
- evidence scope and coverage;
- a few discriminating mechanism, outcome, case, population, period, or
  dataset facets; and
- bundle and source fingerprints.

Collection membership is joined deterministically from the current Zotero
snapshot when building indexes or routing context. It is not embedded as
semantic content in the source-owned bundle.

Every evidence anchor returned by the source-reading model includes:

- source-local anchor ID;
- compact proposition;
- approximate locator and support boundary;
- one or more planning roles such as `thesis`, `method`, `major_finding`,
  `mechanism`, `limitation`, or `literature_position`; and
- model-assigned salience priority.

Downstream relationship discovery and cluster planning select the anchor IDs
relevant to their intellectual task. Local code only loads, caps, and validates
the selected records; it does not decide which evidence is intellectually
important.

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

- Maintain a provider/model capability record containing maximum context,
  supported output limits, configured timeout, and any endpoint-specific
  restrictions.
- Measure or conservatively estimate the complete serialized request before
  provider submission.
- Record the effective input estimate, output allowance, timeout, and capability
  identity in the checkpoint fingerprint.
- Never plan to use more than 50% of the configured model context.
- Reserve room for instructions and output.
- Use a compact 16,000–32,000 output target when measurement shows that it is
  sufficient.
- Allow 64,000 output tokens only when the endpoint supports it and the
  estimated structured response genuinely needs it.
- For a one-million-token model with a 64,000-token output, target no more than
  approximately 430,000 input tokens.
- For ordinary atomic outputs, use the same conservative total-context rule
  with the actual smaller output allowance.
- Fail locally before a paid call when the serialized prompt and required
  output cannot fit the effective capability.

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

New v0.13 artifacts write `parked_for_review`. Readers and migrations continue
accepting legacy `exhausted` and normalize it to the new reporting category.
CLI output, API responses, source-set counts, coverage registers, progress
reports, and acceptance denominators must switch together; do not leave a
partial rename with contradictory counts.

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

Use one authoritative `evidence_eligibility` enum throughout notes, profiles,
source sets, relationship routing, and clusters:

- `substantive_bounded` — recovered evidence may support analysis,
  relationships, and clusters within its declared scope;
- `context_only` — metadata, abstract, or other limited material may support
  navigation but not substantive cross-source claims; and
- `unavailable` — no usable content is currently available.

Do not add a second `synthesis_eligible` boolean beside the existing
`excluded_from_synthesis` state. Migrate old state into the enum and remove the
contradictory field from new artifacts.

`partial_document_atomic_note` remains a terminal completed status and may use
`substantive_bounded`. Terminal means processing is complete; it does not mean
excluded.

An evidence-bounded partial note uses the analytical structure—thesis, method
or knowledge basis, findings, limitations, literature position, and evidence
anchors—plus a prominent coverage boundary stating what was recovered and
what remains absent. It does not use the metadata-only limited-note template.

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

One canonical collection-membership snapshot contains:

- every collection key, display name, and parent key;
- every source's direct collection keys;
- canonical parent-item metadata for standalone attachment records;
- item and collection revisions needed to detect rename, move, removal, and
  multi-collection membership; and
- a stable snapshot fingerprint.

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
Collection source shards contain direct members. Parent pages navigate to child
collections and report descendant counts without repeatedly copying every
descendant source into the parent's shards.

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

For the 195-source acceptance corpus, the discovery contract returns at most
120 model-ranked unresolved pairs, matching twenty adjudication batches of
approximately six pairs before mandatory-pair budget adjustment. At least 40%
of the effective inferred-pair slots are reserved for cross-literature bridges.
Same-collection candidates cannot consume the reserved bridge capacity.

Every candidate includes model-supplied priority, intellectual relevance
rationale, discovery route, and the compact propositions that should be
compared. Candidate discovery also names the source-local anchor IDs it wants
loaded for each side. Local code resolves those IDs into the pair job. For a
structurally explicit pair that bypasses model discovery, the job starts with
the source model's highest-priority thesis, method, finding, and
literature-position anchors; the adjudicator may return `needs_more_context`
rather than receiving an automatic second call.

Exact matched citations and explicit Zotero relations are mandatory candidates.
They consume adjudication capacity first. The inferred-pair quota is then:

`min(120, remaining planned adjudication capacity)`

Preflight may reduce the inferred quota or borrow from the shared call reserve
while remaining below 100 total calls. It never drops a mandatory explicit
pair or exceeds the hard ceiling. If mandatory pairs alone cannot fit the
remaining stage plus reserve capacity, the run stops before provider submission
and reports the unresolved budget conflict.

When capping is required, local code follows the model's rank within the
within-literature and bridge quotas. It must not rerank candidates using local
semantic similarity.

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

Before schema-4 graph migration, the frozen discovery benchmark must achieve
at least 85% candidate recall on the independently curated 40 bridge pairs.
Prompt changes, capacity changes, and routing changes rerun this benchmark
without publishing graph edges.

### 10.2 Pair decision job

Every candidate pair becomes a bounded, immutable pair decision job. This is
the canonical unit of state, fingerprinting, retry, result storage, and
registry history. It is not necessarily one provider call. The same job can be
included in a DeepSeek batch or answered individually by an optional
coding-harness agent.

```json
{
  "pair_job_id": "relationship-job-...",
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
  "output_contract": "relationship-decision-v4"
}
```

The packet normally contains compact profiles plus selected evidence anchors,
not two complete atomic notes. When the bounded evidence is genuinely
insufficient, the initial adjudicator may return `needs_more_context`. The item
is parked for explicit follow-up; the system does not automatically spend
another call.

Packets and results live under the run state so an agent can audit them without
reconstructing hidden prompt context.

### 10.3 Provider batch packet

The automatic DeepSeek backend groups approximately four to eight unresolved
pair jobs into one provider request, bounded by measured context size. The
provider batch is only a transport optimization; every returned row is split
back into its canonical pair job.

Batch checkpoints store the ordered pair-job IDs, effective provider
capabilities, serialized-context fingerprint, and returned row IDs under a
separate `relationship_batches/BATCH_ID/` run-state path. Repacking does not
change pair-job identity.

- One malformed row parks only that pair.
- Valid sibling rows commit normally.
- A failed or truncated provider batch parks its unresolved pair jobs without
  altering already committed jobs.
- Resume may repack still-unresolved pair jobs only under the explicit retry
  policy and cumulative call ceiling.
- A harness agent may answer one exported pair job without adopting the
  DeepSeek batching format.

### 10.4 One complete adjudication

Remove the separate v0.12 relationship verifier. One adjudication response is
may contain multiple pair rows, but each row is one complete final
probabilistic judgment:

```json
{
  "pair_job_id": "relationship-job-...",
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

The new single-call adjudication prompt must pass the Schelling–Smith/Stam,
Carnegie–Hartzell, and McAuliffe–Hampson direction/type regression fixtures
before any schema-4 machine relationship becomes visible.

### 10.5 Citation is not agreement

The graph distinguishes:

- `cites` or `engages`, established from the current source;
- substantive `supports`, `undermines`, `qualifies`, `extends`,
  `complements`, or `contrasts`, requiring both works; and
- navigation-only similarity, which remains an invisible retrieval signal
  unless a human asks to expose it.

When an important literature-position citation matches an existing note, the
source-local `Position in the Literature` projection may link to that note
immediately. A substantive typed relationship appears only after adjudication.

### 10.6 Registry behavior

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

The structured engagement record is canonical. Bundle and profile fingerprints
include its engagement prose and source evidence, but exclude
`matched_source_id`, target path, and wikilink rendering. Note-preservation
hashing must normalize or exclude the managed literature block just as it
already excludes the graph block. Resolving a missing citation therefore
changes a link projection without invalidating the source analysis or paid
profile work.

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
- expose explicit job export and result import commands if a public harness
  workflow is included; and
- record `reasoner_backend`, model identity, and provenance.

The practical exchange layout is:

```text
11_state/runs/RUN_ID/relationship_jobs/JOB_ID/
├── input.json
├── result.json
└── status.yml
```

The built-in DeepSeek backend reads the same immutable job data through the
normal pipeline. An optional harness agent receives an exported `input.json`
and returns an isolated `result.json`. An explicit import or `resume` ingestion
path validates the result, commits registry events, and projects links. The
agent never writes a note or canonical registry directly, and ingesting an
existing result costs no provider call.

Suitable harness-agent work includes:

- processing a bounded set of ambiguous relationship packets;
- auditing a deterministic sample;
- reviewing metadata-remediation recommendations;
- diagnosing provider failures; and
- coordinating CLI runs.

Agents must not directly edit atomic notes, indexes, registries, or clusters.
They return structured results, and the same local commit/projection path
applies.

DeepSeek source generation keeps the existing worker pool, and read-only
harness audits may run concurrently. v0.13 does not implement automatic job
claiming, agent scheduling, competing result writers, or concurrent registry
commits. Imported results pass through one deterministic commit path, and
cluster refresh begins only after the relevant relationship revision is
committed.

Direct Codex/Claude orchestration, scheduler integration, automatic agent
spawning, and parallel harness claiming remain later features. The persisted
packet protocol prevents that later work from requiring another graph redesign.

## 13. Workstream G — Simplified clustering

### 13.1 One global plan when the corpus fits

For the current 122-profile analytical corpus, replace repeated partition
proposals and reconciliation with one cluster-planning call.

Input:

- all eligible compact cluster-planning cards;
- accepted substantive relationships;
- important matched literature-position records;
- collection identities;
- prior compact cluster summaries when refreshing; and
- explicit instructions to find mixed-literature debates.

A cluster-planning card is the compact profile plus three to five
synthesis-relevant, source-specific evidence references. Each reference
contains an existing `evidence_anchor_id`, a one-sentence proposition, a rough
locator, and its support boundary. It does not contain the complete atomic note
or every evidence passage.

The source-reading model has already assigned anchor roles and salience. The
card builder selects a role-diverse top three to five using those
model-supplied priorities and a size cap. The cluster planner then names the
anchor IDs that actually justify each membership. Local code neither invents
the priorities nor makes the membership judgment.

There are no “global evidence anchors.” The plan is global because one call
sees the eligible corpus. Every evidence anchor remains owned by one source.
The planner simply cites those source-local IDs to explain why it placed a work
in a cluster.

Output:

- cluster ID and concise title;
- shared question;
- members, each containing `source_id`, `role` (`core`, `context`, or
  `bridge`), `evidence_anchor_ids`, and a concise membership reason;
- neighboring relationships containing both cluster IDs, the relationship,
  basis source IDs, and evidence-anchor IDs;
- unclustered analytical source IDs; and
- a concise reason for sources intentionally left unclustered.

Example:

```yaml
members:
  - source_id: source-a
    role: core
    evidence_anchor_ids: [anchor-a-thesis, anchor-a-finding-2]
    membership_reason: Connects commitment problems to third-party guarantees.
neighbor_relationships:
  - left_cluster_id: commitment-problems
    right_cluster_id: mediation-design
    relationship: institutional response to the same implementation problem
    basis_source_ids: [source-a, source-b]
    evidence_anchor_ids: [anchor-a-finding-2, anchor-b-thesis]
```

Operational settings:

- conservative input target derived from the configured context capability
  (approximately 430,000 tokens when context is one million and output is
  64,000);
- no more than half of the configured total context;
- output estimated from requested cluster and member counts, normally
  16,000–32,000 and up to 64,000 only when supported and needed;
- a configured literature-request deadline, with 600 seconds as the preferred
  value for this acceptance run when the endpoint and client support it;
- existing two-hour overall literature deadline; and
- one concise structured response without repeated source summaries.

Provider capability, effective output limit, serialized input size, response
size estimate, and configured deadline must be checked during preflight and
included in the checkpoint fingerprint. Do not silently request 16,000 tokens
for a contract known to need more output, and do not assume 64,000 or 600
seconds when the endpoint cannot honor them.

### 13.2 Independent cluster synthesis

After the plan:

1. verify mechanically that member IDs exist, cited anchors belong to those
   members, roles are valid, and neighboring records are reciprocal;
2. admit the model-selected cluster families;
3. load the full text of the selected member anchors and other directly
   relevant member evidence only after membership is selected;
4. make one synthesis call per admitted cluster using that bounded evidence
   and the relevant relationships; and
5. project membership and neighboring-cluster links locally.

Local code does not decide whether the cited evidence makes the family
intellectually coherent. It verifies identity and provenance so the later
synthesis has actual source evidence rather than membership based only on a
title or keyword.

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

The twenty planned adjudication calls carry approximately six independent pair
jobs each. Packing may vary between four and eight jobs according to measured
context size. Mandatory explicit pairs consume this capacity first; the
inferred quota shrinks from its maximum of 120, or the stage borrows available
shared reserve calls, rather than dropping mandatory pairs or exceeding the
hard ceiling.

The public hard literature-synthesis ceiling remains 100. The lower internal
target makes the ceiling a safety boundary rather than a spending goal.

Atomic generation remains under the existing 250-call source/profile ceiling,
but the ledger must clearly count the actual source-reading and hierarchical
calls. There must be no hidden profile or verifier totals.
Externally supplied harness results record backend provenance but consume no
DeepSeek call; any model usage by that harness remains separately identifiable
rather than being reported as free reasoning.

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
- idempotent processing of new, changed, removed, moved, and multi-collection
  sources;
- missing-source ledger matching;
- affected relationship and cluster IDs; and
- a resumable incremental command.

Incremental batch flow:

1. detect new, changed, removed, moved, and membership-changed Zotero records
   read-only;
2. generate new atomic records in parallel;
3. commit one stable catalogue revision;
4. discover new-to-existing and new-to-new candidates;
5. adjudicate bounded packets;
6. project reciprocal links;
7. refresh affected clusters; and
8. update the processed inventory only after successful local commits.

A collection rename or move updates only hierarchy and index projections. A
source removed from Zotero or made unavailable is not silently deleted:

- preserve its canonical note and registry history;
- mark machine relationships orphaned or inactive through a committed registry
  event;
- remove broken managed projections only after that successful commit;
- retain human-authored content and links; and
- require an explicit user deletion request before removing source artifacts.

Incremental cluster refresh uses changed source bundles plus compact existing
cluster context. It does not rerun a whole-library global cluster plan for every
new Zotero item; a full global plan is reserved for fresh maps, explicit
reconciliation, or changes affecting a large share of the corpus.

The weekly heartbeat, notifications, document retrieval, and Research OS
workflow invocation are deferred. Later automation should call the same
idempotent command rather than introduce a second sync implementation.

## 16. Interfaces and compatibility

### 16.1 Model and status changes

- Keep `SourceScope.partial_document`.
- Add the single `evidence_eligibility` enum and remove contradictory new
  eligibility booleans.
- Replace visible `exhausted` with `parked_for_review`.
- Add `SourceAnalysisBundle` schema `1`.
- Bump the evidence-profile schema from `1.2` to `1.3`.
- Add structured literature-position and missing-source records.
- Replace preliminary-plus-verified relationship output with one complete
  relationship-decision record.
- Preserve custom reasoners through capability detection.

A legacy source reader that returns only atomic analysis remains usable through
capability detection and a conservative local profile projection; it does not
silently trigger the removed paid profile or fidelity calls. A custom
relationship reasoner that cannot return the v0.13 complete relationship
record may still produce candidates, but those candidates remain parked and
invisible.

### 16.2 Migration

Migration is local, lazy, idempotent, and provider-free.

- Accept v0.11 and v0.12 workspaces.
- Preserve all old atomic prose.
- Wrap each compatible v0.12 note/profile pair in a local
  `legacy_source_analysis_bundle` without a provider call. Its canonical source
  identity and content/profile hashes replace source-set-dependent ownership.
- Read profile schema `1.2` through a compatibility adapter and write schema
  `1.3` only when a new bundle or explicit local migration artifact is
  committed.
- If the same source has conflicting legacy profiles across source sets, retain
  the variants and park bundle unification for review rather than choosing one
  silently.
- Preserve human-authored links.
- Keep v0.12 relationship history.
- Do not remove a visible v0.12 machine edge until its exact pair is
  successfully re-adjudicated.
- Mark old machine decisions with their original prompt/model provenance and
  `legacy_review_pending`.
- Legacy v0.12 machine edges may remain visibly projected for continuity, but
  they are excluded from new cluster evidence and schema-4 substantive
  reasoning until their pair is successfully re-adjudicated. Their audited
  precision is too low to treat them as trusted cluster support.
- Recover v0.12 fidelity-parked drafts locally when the stored output is
  structurally usable and about the correct source; retain warnings.
- Require an explicit new generation only for the two truncated or otherwise
  grossly unusable outputs.
- Map existing partial-document notes to `substantive_bounded` when their stored
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

### Phase 1 — Define contracts before changing orchestration

Primary files:

- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/ports.py`
- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/relationships.py`

Define and fixture-test:

- `SourceAnalysisBundle` and evidence-profile schema `1.3`;
- canonical Zotero metadata projection versus model-observed identity;
- evidence-anchor roles and salience;
- the single evidence-eligibility enum;
- pair decision jobs versus provider batch packets;
- the complete schema-4 relationship decision;
- cluster-planning cards and evidence-referenced member records;
- provider capability, context, timeout, and output settings; and
- cumulative call-ledger stage identities.

Success gate: every contract round-trips, legacy capability detection is
defined, fingerprints exclude source-set and link-rendering state, and no
provider orchestration changes are merged before these fixtures pass.

### Phase 2 — Replace the atomic publication path

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/profiles.py`
- `src/auto_zettelkasten/fidelity.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- implement the source-owned bundle writer and deterministic projections;
- remove the separate paid profile and fidelity calls from new runs;
- add genre/design and same-call self-review instructions;
- remove source-specific global-prompt content;
- isolate malformed optional bundle components from valid atomic analysis;
- make diagnostics advisory;
- add `parked_for_review`; and
- recover usable v0.12 drafts without provider work.

Success gate: ordinary sources use one call, soft warnings publish, atomic prose
is never rewritten by a later model stage, and an unchanged replay is call-free.

### Phase 3 — Repair extraction and metadata accounting

Primary files:

- `src/auto_zettelkasten/extraction.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/indexes.py`

Changes:

- migrate coverage and synthesis state to `evidence_eligibility`;
- allow evidence-bounded partials and excerpts into synthesis;
- fix report, chapter, appended-page, HTML, and parent-metadata cases; and
- write separate Zotero and pipeline remediation ledgers.

Success gate: all four partial-document fixtures are analytically usable where
appropriate, and every known classification failure is correctly represented.

### Phase 4 — Add literature positions, missing-source memory, and collection indexes

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/indexes.py`
- `src/auto_zettelkasten/zotero.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- store and render compact literature-position records;
- join canonical Zotero/parent metadata without invalidating source bundles;
- match cited works to canonical sources;
- create the missing-source recommendation ledger;
- snapshot complete collection hierarchy, direct membership, parent metadata,
  moves, removals, and multi-collection membership;
- exclude link-only literature rendering from source semantic hashes; and
- generate a deterministic index for every collection and subcollection.

Success gate: adding one source changes only its canonical artifacts, relevant
collection shards, root navigation, and any matched literature-position links.

### Phase 5 — Simplify relationship reasoning

Primary files:

- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/relationships.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/models.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- improve global and incremental candidate discovery;
- gate discovery on the 40-pair recall benchmark;
- persist pair decision jobs and batch four to eight jobs per provider call;
- add one complete direction-safe decision record;
- gate schema-4 visibility on the three known direction/type regressions;
- remove the separate verifier from the active path only after the replacement
  prompt passes both benchmark gates;
- ingest DeepSeek or externally produced schema-valid results through one path;
- preserve negative memory and retirement lineage; and
- project reciprocal Obsidian wikilinks.

Success gate: direction, type, reason, and evidence cannot be updated
independently; all visible relationships are reciprocal and source-grounded.

### Phase 6 — Simplify clustering

Primary files:

- `src/auto_zettelkasten/literature.py`
- `src/auto_zettelkasten/readers.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/notes.py`

Changes:

- use one global plan below the context threshold;
- include three to five compact source-local evidence references per planning
  card;
- require evidence-referenced member and neighbor records;
- configure output and deadlines from measured provider capabilities, allowing
  64,000 tokens and 600 seconds only when supported and needed;
- remove unnecessary proposal/reconciliation/repair loops;
- synthesize each admitted cluster independently;
- include partial-document evidence;
- isolate failures; and
- enforce reciprocal atomic and neighboring-cluster projections.

Success gate: the 122-profile corpus reaches synthesis without partition
proposal loops, produces fresh combined clusters, and preserves valid siblings
when one synthesis fails.

### Phase 7 — Incremental sync groundwork

Primary files:

- `src/auto_zettelkasten/zotero.py`
- `src/auto_zettelkasten/api.py`
- `src/auto_zettelkasten/cli.py`
- `src/auto_zettelkasten/pipeline.py`
- `src/auto_zettelkasten/migration.py`

Changes:

- add stable inventory and collection diffs for new, changed, removed, moved,
  and multi-collection sources;
- persist last-processed state;
- resume new-source processing idempotently;
- connect missing-source matches to candidate discovery; and
- expose affected graph/cluster refresh state.

Success gate: adding a Zotero source and rerunning the incremental command
creates exactly one canonical note and only affected graph/index/cluster writes.
Removing or moving a source preserves history and changes only the appropriate
availability, projection, and index state.

## 18. Test plan

### 18.1 Atomic generation and publication

- One ordinary source produces analysis, profile, anchors, literature positions,
  and recommendations in one call.
- The source bundle fingerprint is unchanged when only collection or source-set
  membership changes.
- Correcting canonical Zotero/parent title, creator, year, or item type updates
  projections without another source-reading call.
- Model-observed bibliographic identity is diagnostic and cannot overwrite the
  canonical Zotero record.
- A malformed optional literature or recommendation row does not suppress a
  valid atomic analysis.
- A legacy analysis-only reader uses the documented local profile fallback
  without a hidden paid call.
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

- 39/40, 100/101, and 105/106 page records become
  `partial_document + substantive_bounded`.
- New artifacts contain one evidence-eligibility enum and no contradictory
  inclusion/exclusion booleans.
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
- Resolving only `matched_source_id` changes the managed literature projection
  without changing the source bundle or semantic profile hash.
- A missing work enters the acquisition ledger.
- Adding that work later resolves the older record and creates a relationship
  candidate without rescanning every source.
- Citation does not automatically become support or extension.

### 18.4 Index hierarchy and scaling

- Every Zotero collection and subcollection receives an index.
- Parent-child links reflect `parentCollection`.
- Direct and descendant memberships remain distinct.
- Parent source shards contain direct members rather than duplicated descendant
  inventories.
- Multi-collection sources retain one canonical note/profile.
- Collection renames and moves change navigation without invalidating source
  analysis.
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
- The frozen discovery prompt reaches at least 85% candidate recall on the 40
  curated bridge pairs before graph migration.
- The 195-source discovery result respects the 120 inferred-pair ceiling and
  40% bridge reservation while retaining every exact citation/Zotero relation.
- Mandatory explicit pairs consume adjudication capacity first; inferred pairs
  shrink or use the reserve without exceeding 100 calls.
- Local capping follows model priority rather than local semantic similarity.
- Candidate discovery names selected anchor IDs and local code only
  loads/validates them.
- Four to eight pair jobs may share one provider batch while retaining
  independent fingerprints, outcomes, and failures.
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
- Every planning card exposes only three to five compact, source-owned evidence
  references.
- Planning-card evidence is selected from source-model roles and salience, not
  local semantic judgment.
- Every proposed member cites anchors belonging to that member and states its
  `core`, `context`, or `bridge` role.
- Every neighboring-cluster record cites basis sources and their anchors.
- Preflight allows the intended 64,000-token output when supported.
- Preflight selects a smaller measured output when sufficient.
- The call receives a 600-second deadline only when the configured client and
  endpoint support it.
- Effective context, output, timeout, and capability identity participate in
  checkpoint fingerprints.
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
- v0.13 harness support is limited to immutable export, validated import, and
  provenance; it does not implement automatic claiming or scheduling.
- Imported results cannot interleave registry projection.
- Removing a Zotero source preserves its note and registry history, marks
  machine edges orphaned/inactive, and repairs managed projections.
- Legacy v0.12 machine edges remain historical/optionally visible but cannot
  support new cluster claims before schema-4 re-adjudication.
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
- source-bundle uniqueness and source-owned fingerprints;
- profile and anchor integrity;
- evidence-eligibility consistency;
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
- 100% of planned member and neighboring-cluster evidence references resolve
  to the declared source;
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
   source-owned bundle that projects a source-faithful atomic note, compact
   profile, evidence set, literature position, and missing-source
   recommendations.
2. Soft diagnostics are recorded without a retry or publication veto.
3. Substantive recovered content remains usable even when one page or the
   complete parent work is absent, through one consistent evidence-eligibility
   state.
4. The canonical source appears in every relevant Zotero collection and
   subcollection index without duplication.
5. Relationship discovery navigates the appropriate compact catalogue rather
   than loading the complete library blindly.
6. Each intellectual relationship has one complete probabilistic pair
   decision, even when several pair jobs share one provider batch.
7. The local projector adds reciprocal Obsidian wikilinks to the relevant
   atomic notes without changing their analytical prose.
8. Important cited works missing from the library become durable acquisition
   recommendations and link automatically when later added.
9. A corpus fitting the safe context target receives one global cluster plan
   whose membership cites compact source-local evidence references, followed
   by independent syntheses using the selected full evidence.
10. One failed relationship packet or cluster cannot erase valid graph,
    index, note, or cluster state.
11. An optional coding-harness agent can inspect or answer bounded job packets
    without becoming a required runtime dependency or editing Markdown.
12. The frozen 195-source evaluation passes the hard atomic, relationship,
    cluster, accounting, and replay criteria within the existing call ceilings.
