# Auto-Zettelkasten

Auto-Zettelkasten turns a Zotero Desktop library or collection into validated
atomic Markdown notes, typed links, source-backed literature clusters,
candidate-gap records, and an Obsidian-ready vault projection.

It is a standalone, file-first Python package. It does not require Research OS,
does not read `zotero.sqlite`, and never writes to Zotero.

> **Release status:** v0.5 is an alpha-quality CLI and Python API using artifact
> schema 1.4. Mapped gaps are claims about the frozen collection only, never
> literature-wide novelty claims.

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
│       ├── tag_proposals.yml
│       ├── tag_registry.yml
│       └── typed_links.yml
├── 03_literature_synthesis/
│   ├── maps/MAP_ID/
│   ├── clusters/
│   ├── gaps/
│   ├── closest_prior_work/
│   └── packets/
└── 11_state/
    ├── runs/RUN_ID/
    │   ├── progress.yml
    │   ├── literature/
    │   └── items/ITEM_KEY/
    ├── fingerprints/
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

Original Zotero tags are preserved exactly. Normalized tags remain proposals
until a controller accepts, parks, or rejects them, and tags are only weak
relation signals. Clusters are formed from versioned evidence profiles,
structured findings, and explicit Zotero/citation relations. Coherent
two-family groupings are `emerging_cluster`; `source_backed_cluster` requires
at least three independent study families. Membership may overlap, up to three
clusters per analytical note.

Accepted normalized tags are also projected into Obsidian's native `tags`
property. A cluster inherits a canonical tag only when at least two independent
study families in that cluster carry it; a one-off tag cannot label or create a
cluster. Tags support grouping and filtering, while actual graph edges use
reciprocal wikilinks between source notes, cluster notes, and evidence-backed
gap records. Cluster and gap indexes and records are generated as Markdown with
native YAML properties, not as YAML-only registries.

Generated filenames combine a readable label with the stable machine ID, for
example `Cluster - Negotiated settlement [cluster-negotiated-settlement-…].md`
and `Gap - Peace duration [gap-author_stated_gap-…].md`. The same stable ID is
retained in frontmatter and aliases, so filenames are scannable by humans
without weakening deterministic agent references.

Debates require at least two independently located positions. Otherwise the
mapper records `mapped_consensus`, `mixed_evidence`, or `no_debate`. Gaps are
generated only by declared rules, searched against every analytical profile in
the frozen collection, and promoted only when the evidence rule, non-obviousness
gate, worth assessment, and executable-design gate all pass. Promotion still
requires at least two independent sources and complete locators. Zero gaps is a
valid result. Every promoted gap records
`scope: collection_only`, `automation_status: promoted`, and
`novelty_claimed: false`.

Cluster-first synthesis runs after deterministic cluster admission. One
checkpointed reasoning call per cluster reads the complete atomic notes,
profiles, and evidence matrix, then explains the central findings, technical
figures and plain-English meaning, agreements, debate positions,
contradictions, boundary conditions, methodological fault lines, neighboring
clusters, source roles, and specific gap hypotheses. Deterministic claim,
locator, study-family, and membership gates remain authoritative.

Independent gap notes are canonical. Each visible gap explains how cluster
analysis generated it, the exact missing relationship or evidence-matrix cell,
supporting and countervailing sources with locators, collection-wide internal
search results, closest prior evidence, its strongest obvious answer, why that
answer is inadequate, what resolving the puzzle changes, and an executable
study design. The design names the estimand, unit, population, exposure,
comparator, outcomes, mechanism measures, inference strategy, data route,
rivals, falsification tests, feasibility, ethics, and validity risks.

Gap opportunities appear inside the exact cluster finding, debate,
contradiction, boundary condition, method fault line, or neighboring-cluster
relationship that generated them. The compact Obsidian callout links to the
canonical gap note without repeating its evidence record. Near-duplicates merge
under a stable gap ID and remain traceable in `gap_merge_ledger.yml`.
Underspecified, obvious, low-value, or non-executable candidates remain audit
records and receive no Markdown or cluster mention.

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
and gap Markdown files retain their native tags and reciprocal wikilinks.

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

Public integration contracts include `EvidenceProfile`, `ClusterProposal`,
`ClusterSynthesis`, `GapRationale`, `LiteratureMapRequest`,
`LiteratureMapReport`, `LiteratureReasoner`, and the optional
`ClusterSynthesisReasoner` extension plus the unused
`ExternalDiscoveryProvider` compatibility seam. v0.5 is strictly
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
profile and unclustered counts, clusters, debates, gaps, packet checkpoints,
active cluster and gap packet, rejected underspecified and quality-gated gaps,
merged gaps, provider calls, failures, and internal-falsification counts. `resume` uses the
run's frozen inventory and frozen acquired representations, reuses completed
direct, chunk, profile, and packet checkpoints, and continues only missing
work. Zotero changes require a new run. A partial CLI run exits with code 3.

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

Schema 1.0-1.3 workspaces remain readable. The idempotent schema-1.4 migration
archives changed bytes, removes legacy review-status material mechanically,
records semantic-hash aliases for paid profile reuse, and makes no model or
Zotero call.

## Scope deliberately deferred from v0.5

- direct `zotero.sqlite` ingestion;
- Zotero writes or collection synchronization;
- group libraries;
- a graphical interface or background daemon;
- a database, vector store, or ontology engine; and
- built-in external scholarly discovery (the injection protocol exists); or
- claims that a generated gap is publication-grade novelty.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
