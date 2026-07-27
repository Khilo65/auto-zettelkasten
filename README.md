# Auto-Zettelkasten

Auto-Zettelkasten turns a Zotero Desktop library or collection into validated
atomic Markdown notes, typed links, source-backed literature clusters,
candidate-gap records, and an Obsidian-ready vault projection.

It is a standalone, file-first Python package. It does not require Research OS,
does not read `zotero.sqlite`, and never writes to Zotero.

> **Release status:** v0.11 is an alpha-quality CLI and Python API using artifact
> schema 1.10 and evidence-profile schema 1.2. Mapped gaps are claims about the
> frozen collection only, never literature-wide novelty claims.

## What it produces

```text
workspace/
├── auto-zettelkasten.yml
├── 01_custody/
│   ├── zotero/inventory/
│   ├── files/
│   └── read_attempts/
├── 02_source_memory/
│   ├── notes/
│   ├── profiles/
│   └── indexes/
│       ├── source_sets/
│       ├── by_literature/
│       ├── source_catalogue.yml
│       ├── cluster_catalogue.yml
│       ├── tag_proposals.yml
│       ├── subject_tag_registry.yml
│       ├── subject_tag_assignments.yml
│       └── typed_links.yml
├── 03_literature_synthesis/
│   ├── maps/MAP_ID/Literature Map - COLLECTION [MAP_ID].md
│   ├── clusters/
│   ├── gaps/
│   ├── propositions.yml
│   ├── topic_neighborhoods.yml
│   ├── subject_tag_registry.yml
│   ├── typed_source_relations.yml
│   ├── study_lineage_registry.yml
│   ├── independence_assessments.yml
│   ├── cluster_source_contributions.yml
│   ├── quantitative_comparisons.yml
│   ├── locator_audit.yml
│   ├── coverage_register.yml
│   ├── tag_concept_registry.yml
│   ├── closest_prior_work/
│   └── packets/
└── 11_state/
    ├── runs/RUN_ID/
    │   ├── progress.yml
    │   ├── literature/
    │   └── items/ITEM_KEY/
    ├── fingerprints/
    ├── legacy_navigation/
    ├── legacy_maps/
    └── exports/
```

Full-document analytical notes contain:

- thesis;
- method and research design;
- evidence and data;
- detailed findings retaining exact estimates, units, samples, and uncertainty;
- a separate plain-English interpretation covering direction, magnitude,
  reference point, uncertainty, and practical meaning;
- strengths and contributions;
- methodological critique;
- limitations;
- what the source can and cannot support;
- locators; and
- structural validation, source coverage, model/provider, and source-lineage metadata.

Passing the structural and full-document coverage gates commits an
`analytical_atomic_note`. Generated notes contain no review-status field or
boilerplate validation section.

Abstracts, paywall snapshots, and metadata-only records use compact limited
notes instead of empty analytical templates. Their statuses are
`abstract_only_atomic_note`, `metadata_only_source_note`, or
`fulltext_available`. They remain searchable and linkable, but cannot form
canonical clusters or support candidate gaps.

For PDF sources, the mapper prefers the actual primary Zotero attachment and
preserves page markers throughout extraction. Born-digital pages use `pypdf`.
A lightweight text sniff sends only suspicious pages to PDFium rendering plus
Tesseract OCR, with one orientation-aware retry; good embedded-text pages are
never discarded. Poppler is a fallback when PDFium is unavailable. Extraction
provenance records the page count, embedded-text pages, OCR pages, unresolved
pages, and route in machine metadata. A visibly textual page that remains
unreadable produces a limited note rather than a falsely complete analysis.
Tables may be flattened when their labels, values, and surrounding explanation
remain readable; the note prompt asks the reader to use that context without
inventing row-column relationships.

Original Zotero tags and their normalized forms remain provenance. The graph
projection derives conservative, typed subject tags from existing profile
fields, such as `mechanism/mediator-legitimacy`, `outcome/mediation-success`,
and `case/syria`. Only mechanical variants are reconciled automatically;
uncertain synonyms remain audit proposals. Structural values such as source
status or note type remain YAML properties and are not written as subject tags.

The source catalogue provides compact title, author, thesis, method, and facet
entries for model-led relationship discovery without loading every full note.
Substantive links are adjudicated from two-sided evidence anchors and stored in
the canonical typed-link registry. Models return relationship records only;
local code projects reciprocal, explained links into explicit managed graph
blocks in both atomic notes. Graph projection is committed before cluster
synthesis, leaves the source-analysis semantic hash unchanged, and is
provider-call-free on an unchanged replay.

A `topic_neighborhood` is promoted only when at least two effective analytical
evidence bases share a discriminative typed subject tag. Single-source facets stay
as source-local search metadata and do not become native graph tags or neighborhoods. Neighborhoods remain machine-sidecar
retrieval and candidate-discovery signals; they do not receive a competing researcher-facing Markdown map and never establish
a cluster, debate, or gap. Analytical clusters are coherent research conversations or evidence bases. They may connect the same proposition,
rival explanations, complementary mechanisms, boundary contrasts,
methodological fault lines, sequential relationships, or interpretive and
normative disagreements. A cluster does not require consensus or disagreement to exist, and shared vocabulary alone is insufficient.
`emerging_cluster` requires two effective evidence bases and
`source_backed_cluster` requires at least three. Connected publications that
reuse one evidence base remain visible as an `evidence_concentrated_cluster`;
they cannot establish independent consensus. Context and bridge sources do not
count toward qualification. Analytical membership may overlap, up to three
core cluster roles per source.

Subject tags are projected into Obsidian's native `tags` property. A cluster
inherits a tag only after analytical admission, when at least two independent
core sources use it while supporting the same admitted proposition. Gap tags
come only from the originating proposition and finalized related clusters.
Navigation changes therefore alter a separate `graph_projection_hash`; they do
not change cluster, proposition, gap, evidence-anchor, or source semantic IDs.

Typed graph relations distinguish `cites`, `cited_by`, `zotero_related`,
`same_proposition`, `shared_concept`, `same_case`, `same_method`,
`same_outcome`, and `semantic_similarity`. Inferred related-note links are
bounded and include a plain-language reason. Broad shared tags do not create an
all-pairs link graph.

Generated filenames combine a readable label with the stable machine ID, for
example `Cluster - Negotiated settlement [cluster-negotiated-settlement-…].md`
and `Gap - Peace duration [gap-author_stated_gap-…].md`. The same stable ID is
retained in frontmatter and aliases, so filenames are scannable by humans
without weakening deterministic agent references.

Debates require at least two independently located, comparable positions. Publication
count is kept separate from effective evidence-base count so reprints, overlapping
samples, shared datasets, and within-program reports do not inflate support. The
mapper distinguishes `mapped_debate`, `mapped_consensus`, `emerging_convergence`,
`aligned_institutional_guidance`, `within_program_consistency`, `mixed_evidence`,
`conditional_relationship`, `complementary_positions`, `parallel_literatures`,
`single_position`, and `no_debate`. Gaps are
generated only by declared rules, searched against every analytical profile in
the frozen collection, and promoted only when the evidence rule, non-obviousness
gate, worth assessment, and feasible-resolution gate all pass. Promotion still
requires at least two independent sources and complete locators. Zero gaps is a
valid result. Every promoted gap records
`scope: collection_only`, `automation_status: promoted`, and
`novelty_claimed: false`.

The generated **Literature Map** is the main human entry point. It reports frozen-collection coverage, explains specific reasons for
unclustered analytical sources, catalogs admitted clusters and their verdicts, links collection-relative gaps, and points to source,
cluster, and gap indexes. Topic neighborhoods and complete audit matrices remain machine-readable sidecars.

Cluster-first synthesis runs after thematic-cluster admission and strict proposition-level comparison.
One checkpointed reasoning call per cluster reads the complete atomic notes,
profiles, and proposition-evidence matrix, then explains the central findings, technical
figures and plain-English meaning, agreements, debate positions,
contradictions, boundary conditions, methodological fault lines, neighboring
clusters, source roles, and specific proposition-linked gap hypotheses. It also retains
the most important cluster-relevant findings from every core study in the
"What the studies find" section, even when no other source reports the same
finding. Those source-specific contributions are never mislabeled as agreement. Every
exact comparative assertion resolves to a map-local proposition and one or
more source-local evidence anchors; broader family assertions resolve to a
typed located family relation and cannot be presented as consensus or
contradiction. Descriptive or associational anchors cannot
support causal wording. Generated atomic-note headings cannot serve as strong source
locators, and quantitative prose must pass arithmetic and estimand checks. Anchor IDs remain hidden from human Markdown unless a
machine-readable link requires them.

Atomic-note generation uses the complete page-preserving source text and asks
the reader to adapt to the actual source type, including academic studies,
books, reports, legal or policy documents, archival records, conference notes,
meeting records, speeches, practitioner guidance, and web publications. The
prompt requires source-specific context, method or knowledge basis, technical
detail plus plain-English meaning, and a distinction between observation,
author interpretation, and what the source can establish. DeepSeek runs atomic
notes with high reasoning and cluster synthesis with maximum reasoning. Paid
calls are checkpointed; replaying an unchanged completed run reuses them.

Independent gap notes are canonical. Each visible gap explains how cluster
analysis generated it, the exact missing relationship or evidence-matrix cell,
supporting and countervailing sources with locators, collection-wide internal
search results, closest collection evidence, its strongest obvious answer, why
that answer is inadequate, what resolving the puzzle changes, and a concise
type-sensitive `ResolutionPath`. Quantitative paths specify estimands,
comparisons, identification, and measurement; qualitative, historical,
theoretical, normative, methodological, and practitioner paths instead state
the discriminating evidence appropriate to those forms of inquiry. A resolution
path is not a finalized project study design.

Gap opportunities appear inside the exact cluster finding, debate,
contradiction, boundary condition, method fault line, or neighboring-cluster
relationship that generated them. The compact Obsidian callout links to the
canonical gap note without repeating its evidence record. Near-duplicates merge
under a stable gap ID and remain traceable in `gap_merge_ledger.yml`.
Underspecified, obvious, low-value, or non-executable candidates remain audit
records and receive no Markdown or cluster mention.

Every cluster Markdown note also includes deterministic strict-claim checks for
consensus and contradiction. A failed threshold is explained rather than
silently omitted: the note states which requirement failed, why it failed in
the collection, and what evidence could change the judgment. Every visible gap
or lead similarly explains whether it meets the stronger gap threshold. These
checks are embedded in the existing cluster and gap records; they do not create
new claim files or controller objects.

## Install

Auto-Zettelkasten requires Python 3.11 or newer and a running Zotero Desktop.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install auto-zettelkasten
```

For local development:

```bash
python -m pip install -e '.[test]'
python -m pytest
```

## Quick start

```bash
auto-zettelkasten init ~/Research/my-map
auto-zettelkasten doctor --workspace ~/Research/my-map
auto-zettelkasten zotero collections
```

A fresh workspace deliberately reports a blocked DeepSeek route until a key is
available and cloud use is explicitly enabled for a run, or the workspace is
configured for the local Ollama provider.

Map the complete local user library with an explicitly local Ollama reader:

```bash
auto-zettelkasten map \
  --workspace ~/Research/my-map \
  --scope library \
  --provider ollama \
  --parallel 4
```

Map one collection with the default DeepSeek route:

```bash
export DEEPSEEK_API_KEY='...'
auto-zettelkasten map \
  --workspace ~/Research/my-map \
  --scope collection \
  --collection COLLECTION_KEY \
  --provider deepseek \
  --model deepseek-v4-flash \
  --allow-cloud
```

Use the collection currently selected in Zotero:

```bash
auto-zettelkasten map \
  --workspace ~/Research/my-map \
  --scope selected \
  --provider ollama \
  --model llama3.2
```

Resume and inspect a run:

```bash
auto-zettelkasten resume --workspace ~/Research/my-map --run-id RUN_ID
auto-zettelkasten status --workspace ~/Research/my-map --run-id RUN_ID --json
```

Rebuild a systematic map from existing notes, or preview the idempotent v0.4
schema migration and legacy-map archive:

```bash
auto-zettelkasten migrate --workspace ~/Research/my-map --dry-run
auto-zettelkasten build-map \
  --workspace ~/Research/my-map \
  --provider deepseek \
  --model deepseek-v4-flash \
  --allow-cloud
```

Export generated Markdown into a new Obsidian vault:

```bash
auto-zettelkasten export obsidian \
  --workspace ~/Research/my-map \
  --vault ~/Documents/MyVault \
  --new-vault
```

The export is a generated projection. Canonical YAML registries remain in the
workspace and are never edited through Obsidian. The exported source, cluster,
and gap Markdown files retain their native subject tags and reciprocal
wikilinks. The exported literature map keeps its human-readable collection name.

## Privacy and provider routes

Cloud providers are blocked unless `--allow-cloud` is present. The guard is
checked before inventory, text-reader, and document-vision calls. A saved
configuration value or an available API key never substitutes for per-run CLI
consent.

| Provider | Environment variable | Cloud | Intended route |
|---|---|---:|---|
| DeepSeek | `DEEPSEEK_API_KEY` | yes | default text reader |
| OpenRouter | `OPENROUTER_API_KEY` | yes | alternate/model experiment |
| Gemini | `GEMINI_API_KEY` | yes | text and document vision |
| Ollama | none | no | local text reader |

Built-in Zotero and Ollama endpoints are restricted to loopback hosts. The
local extraction ladder ranks complete PDF text above clean full-article HTML,
abstract-only snapshots, and metadata. `indexedChars == totalChars` proves only
that Zotero indexed an attachment completely; it does not prove that a paywall
page contains the publication. PDF extraction inserts page markers.

Document routing is provider-aware. DeepSeek V4 Flash is configured with its
one-million-token context window, so documents such as ordinary books are read
directly when they fit safely. Smaller-context models use coarse page-aware
evidence chunks followed by one synthesis call. Successful direct reads and
chunk calls are checkpointed before note commit.

API keys are read only from the process environment. They are not written to
`auto-zettelkasten.yml`, run manifests, attempts, notes, or export files.

Zotero access uses the local HTTP API at `http://127.0.0.1:23119`. The client
implements only status, collection inventory, item/child/full-text reads, and
attachment-file reads. It never invokes a Zotero mutation endpoint.

## Python API

```python
from auto_zettelkasten.api import (
    LiteratureMappingPolicy,
    MapRequest,
    ProcessingPolicy,
    run_map,
)

report = run_map(
    MapRequest(
        workspace="~/Research/my-map",
        scope="collection",
        collection_key="COLLECTION_KEY",
        provider="ollama",
        model="llama3.2",
        processing=ProcessingPolicy(
            max_calls_per_document_run=24,
            request_deadline_seconds=120,
            document_deadline_seconds=900,
        ),
        literature_policy=LiteratureMappingPolicy(
            source_backed_threshold=3,
            max_memberships=3,
            external_discovery="disabled",
        ),
    )
)
print(report.to_dict())
```

Stable v0.4 entry points live in `auto_zettelkasten.api`:

- `initialize_workspace`
- `doctor`
- `list_collections`
- `inventory`
- `run_map`
- `run_literature_map`
- `resume_map`
- `get_status`
- `build_map`
- `export_to_obsidian`

Providers, Zotero, and proposal controllers are injectable through protocols in
`auto_zettelkasten.ports`. This is the supported integration boundary for
Research OS and other controllers.

Public integration contracts include `EvidenceProfile`, `EvidenceAnchor`,
`SupportEnvelope`, `LiteratureProposition`, `SynthesisAssertion`,
`SubjectTag`, `SubjectTagAssignment`, `TypedSourceRelation`,
`TopicNeighborhood`, `NavigationPolicy`, `ResolutionPath`, `ClusterProposal`, `ClusterSynthesis`,
`GapRationale`, `LiteratureMapRequest`,
`LiteratureMapReport`, `LiteratureReasoner`, and the optional
`ClusterSynthesisReasoner` extension plus the unused
`ExternalDiscoveryProvider` compatibility seam. v0.8 is strictly
collection-native: non-disabled external discovery and injected discovery
providers fail before inventory.

Processing defaults can be overridden through `auto-zettelkasten.yml`,
`ProcessingPolicy`, or the matching `map` flags. The defaults use a 120,000
character fallback only for unknown model contexts, a 64-chunk hard coverage
limit, 24 provider calls per document invocation, a 120-second request
deadline, and a 900-second document deadline.

## Terminal accounting and resume

Every inventoried item ends a mapping run as either:

- `validated_note`; or
- `limited_note`; or
- `exhausted`, with a route-level attempt record and reason.

During resumable work, an item can instead be `partial` or `pending`.

The run invariant is therefore:

```text
inventory_count == validated_note_count + limited_note_count
                 + exhausted_count + partial_count + pending_count
```

Notes and checkpoints are fingerprinted by Zotero item key, inspected-content
hash, source scope, Zotero metadata, source-classifier and
chunking versions, processing policy, prompt version, and effective provider
and model. Each completed item and attempt record is committed immediately.
Prompt version 2 keeps technical figures in `Detailed Findings` and requires a
separate statistical interpretation for non-specialists. Remapping an older
prompt-version note invalidates its old fingerprint and replaces it in place.
`status` reads live `progress.yml`; it exposes the active literature stage,
profile, proposition, subject-tag, typed-relation, topic-neighborhood, singleton-facet, and unclustered counts, clusters,
debates, gaps, packet checkpoints,
active cluster and gap packet, rejected underspecified and quality-gated gaps,
merged gaps, provider calls, failures, and internal-falsification counts. `resume` uses the
run's frozen inventory and frozen acquired representations, reuses completed
direct, chunk, profile, and packet checkpoints, and continues only missing
work. A completed replay with unchanged source, model, policy, prompt, and
algorithm fingerprints reuses those checkpoints and makes no paid model calls.
Zotero changes require a new run. A partial CLI run exits with code 3.

The run source set records exactly what that run attempted. Automatic mapping
uses that frozen run collection—not unrelated notes elsewhere in the
workspace. `build-map` can instead target an explicit source-set snapshot or
all existing workspace notes. Source-set snapshots are dependency-hashed and
immutable; stable compatibility files point to the latest snapshot. Limited
`fulltext_available` or metadata/abstract notes remain searchable and can
participate in typed links, but cannot form analytical clusters or answer gaps.

`MapRequest.question` is an optional projection lens. It does not change source
notes, evidence profiles, cluster/gap identities, or the underlying collection
map. Research OS may use the lens for downstream ranking without mutating the
base map.

Artifact schemas 1.0-1.10 and evidence-profile schemas 1.0-1.2 remain readable.
The idempotent schema-1.9 migration retires the standalone Literature Neighborhoods Markdown projection, archives superseded current cluster and gap
projections, preserves historical maps, profiles, analytical identities, and
atomic-note bytes, and makes no model or Zotero call. Existing schema-1.5
proposition anchors remain valid; unsupported legacy anchors cannot establish
strong synthesis until they are lazily reprofiled.
The schema-1.10 migration updates managed artifact version markers only.
Legacy unmarked `## Graph Links` sections are converted to bounded
`auto-zettelkasten:graph` markers on their next graph projection; source prose,
profiles, and human-authored sections are not rewritten.

## Scope deliberately deferred from v0.11

- direct `zotero.sqlite` ingestion;
- Zotero writes or collection synchronization;
- group libraries;
- a graphical interface or background daemon;
- a database, vector store, or ontology engine; and
- built-in external scholarly discovery (the injection protocol exists); or
- claims that a generated gap is publication-grade novelty.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
