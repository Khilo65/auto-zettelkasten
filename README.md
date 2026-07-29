# Auto-Zettelkasten

Auto-Zettelkasten turns a Zotero Desktop library or collection into atomic
Markdown notes, typed links, full-note literature syntheses, optional
candidate-gap records, and an Obsidian-ready vault projection.

It is a standalone, file-first Python package. It does not require Research OS,
does not read `zotero.sqlite`, and never writes to Zotero.

> **Release status:** v0.15 is an alpha-quality CLI and Python API using artifact
> schema 1.13 and evidence-profile schema 1.3. Mapped gaps are claims about the
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
│   ├── bundles/
│   ├── profiles/
│   └── indexes/
│       ├── collections/
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
    │   ├── relationship_jobs/
    │   ├── relationship_batches/
    │   ├── literature/
    │   └── items/ITEM_KEY/
    ├── fingerprints/
    ├── legacy_navigation/
    ├── legacy_maps/
    └── exports/
```

One source-reading call produces a source-owned bundle containing the atomic
analysis, compact profile, evidence anchors, important literature positions,
and missing-source recommendations. Deterministic projectors then write the
note, profile, indexes, and managed links without asking a model to rewrite
atomic prose.

Analytical notes contain:

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

Useful full and evidence-bounded partial documents commit analytical notes.
Metadata- and abstract-only records remain context-only. Advisory locator,
numeric, metadata, and causal-language diagnostics do not trigger hidden retry
calls; only grossly unusable output is parked for review.

Abstracts, paywall snapshots, and metadata-only records use compact limited
notes instead of empty analytical templates. Their statuses are
`abstract_only_atomic_note`, `metadata_only_source_note`, or
`fulltext_available`. They remain searchable and linkable, but cannot support
substantive cluster findings. Evidence-bounded partial documents can participate
in relationships and clusters, with their recovered scope carried into the
synthesis.

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

A `topic_neighborhood` remains a machine-sidecar retrieval signal rather than a
competing researcher-facing map. Analytical clusters are model-planned,
coherent research conversations organized around a question, debate,
mechanism, outcome, method, case, historical problem, or practice problem.
Shared vocabulary alone is insufficient. Every retained analytical member must
have a specific cluster-relevant finding; descriptive source roles never
exclude a member from the synthesis.

Subject tags are projected into Obsidian's native `tags` property. Gap tags
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

Debate, agreement, qualification, and contradiction are model judgments made
from the complete member notes and must remain traceable to those members.
Publication count remains distinct from effective evidence-base count so
reprints, overlapping samples, shared datasets, and within-program reports do
not inflate support. Gap discovery is no longer part of the default map build;
existing gap memory remains readable for an explicit downstream workflow.

The generated **Literature Map** is the main human entry point. It reports frozen-collection coverage, explains specific reasons for
unclustered analytical sources, catalogs admitted clusters and their verdicts, links collection-relative gaps, and points to source,
cluster, and gap indexes. Topic neighborhoods and complete audit matrices remain machine-readable sidecars.

The planner reads compact catalogue entries. Each cluster then receives every
complete, projection-free atomic note for its proposed members in one
checkpointed call. The writer may refine the organizing problem, drop a
decorative member, and arrange specific findings into lines of inquiry. Local
code validates schemas, source IDs, evidence ownership, and reciprocal
projection; it does not replace rejected model prose with generic verdicts.
Independent cluster jobs run concurrently and an unchanged semantic cluster is
reused across run IDs.

Atomic-note generation uses the complete page-preserving source text and asks
the reader to adapt to the actual source type, including academic studies,
books, reports, legal or policy documents, archival records, conference notes,
meeting records, speeches, practitioner guidance, and web publications. The
prompt requires source-specific context, method or knowledge basis, technical
detail plus plain-English meaning, and a distinction between observation,
author interpretation, and what the source can establish. DeepSeek runs atomic
notes with high reasoning and cluster synthesis with maximum reasoning. Paid
calls are checkpointed; replaying an unchanged completed run reuses them.

Existing gap notes and ledgers remain canonical and readable. A later explicit
gap workflow may use the completed graph and clusters; gap failure cannot make
the default graph or cluster map partial.

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
  --provider-concurrency auto \
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
        provider_concurrency="auto",
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
- `parked_for_review`, with a route-level attempt record and reason.

During resumable work, an item can instead be `partial` or `pending`.

The run invariant is therefore:

```text
inventory_count == validated_note_count + limited_note_count
                 + parked_for_review_count + partial_count + pending_count
```

Notes and checkpoints are fingerprinted by Zotero item key, inspected-content
hash, source scope, source-shaping document type, classifier and chunking
versions, prompt version, and effective provider and model. Display metadata,
run IDs, timeouts, registry revisions, and Markdown projections do not enter
semantic identities. Harmless Zotero metadata corrections therefore update the
projection without another source call.
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
`sync` compares the read-only Zotero inventory with the last processed snapshot
and updates only changed work. A partial CLI run exits with code 3.

The run source set records exactly what source generation attempted. The
canonical literature graph and cluster registry span all eligible workspace
notes; Zotero collections and subcollections are deterministic, provider-free
views of that global state. An explicit source-set remains available when a
separate semantic resynthesis is deliberately requested. Limited
`fulltext_available` or metadata/abstract notes remain searchable and can
participate in structural links, but cannot support substantive cluster
findings.

`MapRequest.question` is an optional projection lens. It does not change source
notes, evidence profiles, cluster/gap identities, or the underlying collection
map. Research OS may use the lens for downstream ranking without mutating the
base map.

Artifact schemas 1.0-1.13 and evidence-profile schemas 1.0-1.3 remain readable.
The idempotent schema-1.9 migration retires the standalone Literature Neighborhoods Markdown projection, archives superseded current cluster and gap
projections, preserves historical maps, profiles, analytical identities, and
atomic-note bytes, and makes no model or Zotero call. Existing schema-1.5
proposition anchors remain valid; unsupported legacy anchors cannot establish
strong synthesis until they are lazily reprofiled.
The schema-1.12 migration creates provider-free legacy source bundles where
safe, retains conflicting variants for review, and moves old machine
relationships into schema-4 review state without rewriting notes.
The schema-1.13 migration preserves atomic notes and human content, marks
pre-v0.14 cluster syntheses as legacy projections, selects the broadest legacy
cluster registry as the global baseline, consolidates relationship registry
copies, and retires stale machine cluster memberships locally without provider
or Zotero calls.
The v0.15 metadata migration keeps schema 1.13, advances the source prompt to
v10, and makes no provider, Zotero, source-read, or note-rewrite call.
Legacy unmarked `## Graph Links` sections are converted to bounded
`auto-zettelkasten:graph` markers on their next graph projection; source prose,
profiles, and human-authored sections are not rewritten.

## Scope deliberately deferred from v0.15

- direct `zotero.sqlite` ingestion;
- Zotero writes or collection synchronization;
- group libraries;
- a graphical interface or background daemon;
- a database, vector store, or ontology engine; and
- built-in external scholarly discovery (the injection protocol exists); or
- claims that a generated gap is publication-grade novelty.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
