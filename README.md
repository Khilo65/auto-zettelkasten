# Auto-Zettelkasten

Auto-Zettelkasten turns a Zotero Desktop library or collection into validated
atomic Markdown notes, typed links, source-backed literature clusters,
candidate-gap records, and an Obsidian-ready vault projection.

It is a standalone, file-first Python package. It does not require Research OS,
does not read `zotero.sqlite`, and never writes to Zotero.

> **Release status:** v0.2 is an alpha-quality CLI and Python API. Generated gap
> records are research candidates, not verified novelty claims. Source-note
> analysis should still be reviewed against the original document before being
> cited or promoted into an argument.

## What it produces

```text
workspace/
├── auto-zettelkasten.yml
├── 01_custody/
│   ├── zotero/inventory/
│   ├── files/
│   ├── read_attempts/
│   └── citation_leads/
├── 02_source_memory/
│   ├── notes/
│   └── indexes/
│       ├── source_sets/
│       ├── tag_proposals.yml
│       ├── tag_registry.yml
│       └── typed_links.yml
├── 03_literature_synthesis/
│   ├── clusters/
│   ├── gaps/
│   ├── closest_prior_work/
│   ├── expansion/
│   │   ├── candidates.yml
│   │   ├── decisions.yml
│   │   └── candidates/
│   └── packets/
└── 11_state/
    ├── runs/
    ├── fingerprints/
    └── exports/
```

Atomic notes contain:

- thesis;
- method and research design;
- evidence and data;
- detailed findings;
- strengths and contributions;
- methodological critique;
- limitations;
- what the source can and cannot support;
- locators; and
- a deterministic source-lineage and structure review.

Passing that deterministic gate commits an `analytical_atomic_note`; it does
not label the note `verified_atomic_note`. Verification requires a separate
source-aware human or reviewing-controller pass that v0.2 does not pretend to
perform.

Original Zotero tags are preserved exactly. Normalized tags remain proposals
until a controller accepts, parks, or rejects them. Canonical clusters require
at least two validated analytical notes. Gap outputs stay at `candidate` status
and retain closest-prior provenance.

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
auto-zettelkasten resume --workspace ~/Research/my-map --run-id RUN_ID --allow-cloud
auto-zettelkasten status --workspace ~/Research/my-map --run-id RUN_ID --json
```

Export generated Markdown into a new Obsidian vault:

```bash
auto-zettelkasten export obsidian \
  --workspace ~/Research/my-map \
  --vault ~/Documents/MyVault \
  --new-vault
```

The export is a generated projection. Canonical YAML registries remain in the
workspace and are never edited through Obsidian.

## Bounded graph expansion

Graph expansion turns local citation relations into reviewable suggestions. It
does not silently add items to Zotero, read an external paper, or promote a
suggestion into evidence, a cluster, or a gap claim.

Local expansion is the default and makes no external request:

```bash
auto-zettelkasten expansion run \
  --workspace ~/Research/my-map \
  --scope cluster \
  --id cluster-institutions \
  --provider internal \
  --depth 1 \
  --budget 100
```

Optional Semantic Scholar expansion sends only publication identifiers and
bibliographic metadata. It requires distinct, invocation-scoped consent:

```bash
auto-zettelkasten expansion run \
  --workspace ~/Research/my-map \
  --scope source-set \
  --id source-set-example \
  --provider semantic-scholar \
  --allow-network
```

Review and act explicitly:

```bash
auto-zettelkasten expansion list --workspace ~/Research/my-map
auto-zettelkasten expansion decide --workspace ~/Research/my-map \
  --suggestion SUGGESTION_ID --decision accept \
  --expected-version 0 --reason "Directly relevant comparison"
auto-zettelkasten expansion map-accepted --workspace ~/Research/my-map \
  --suggestion SUGGESTION_ID --provider ollama
auto-zettelkasten expansion export --workspace ~/Research/my-map \
  --state accepted --format bibtex --output accepted-expansion.bib
```

Suggestions are scoped to one source, source set, cluster, or candidate gap.
Their lifecycle is `proposed`, `accepted`, `parked`, or `rejected`; fulfillment
is tracked separately. Decisions are append-only and protected by an expected
version. Default depth is one, maximum depth is two, and the hard candidate
budget is 500.

Workspaces created with artifact schema 1.0 remain readable. Expansion writes
require the explicit additive migration:

```bash
auto-zettelkasten migrate --workspace ~/Research/my-map --to 1.1 --dry-run
auto-zettelkasten migrate --workspace ~/Research/my-map --to 1.1
```

The migration creates empty expansion registries and citation-sidecar storage;
it does not rewrite source notes, clusters, gaps, links, or packets.

## Privacy and provider routes

Cloud providers are blocked unless `--allow-cloud` is present. The guard is
checked before inventory, text-reader, and document-vision calls. A saved
configuration value or an available API key never substitutes for per-run CLI
consent.

`--allow-network` is a separate consent gate for Semantic Scholar and Crossref
identifier/metadata lookup. It never authorizes source text, notes, abstracts,
PDFs, or page images to leave the machine. Saved run requests do not authorize
resumed network or cloud activity; consent must be supplied again. Semantic
Scholar API keys, when used, are read from `SEMANTIC_SCHOLAR_API_KEY` or
`S2_API_KEY` and remain environment-only. Crossref is used only as a DOI
identity/metadata fallback.

| Provider | Environment variable | Cloud | Intended route |
|---|---|---:|---|
| DeepSeek | `DEEPSEEK_API_KEY` | yes | default text reader |
| OpenRouter | `OPENROUTER_API_KEY` | yes | alternate/model experiment |
| Gemini | `GEMINI_API_KEY` | yes | text and document vision |
| Ollama | none | no | local text reader |

Built-in Zotero and Ollama endpoints are restricted to loopback hosts. The
local extraction ladder uses indexed Zotero full text only when its coverage
metadata is complete, then tries file extraction, embedded-image OCR with
Tesseract, and configured document vision. PDF extraction inserts page markers.
Long documents use a versioned bounded chunk route when a full-document call is
too large or rejected for context length.

API keys are read only from the process environment. They are not written to
`auto-zettelkasten.yml`, run manifests, attempts, notes, or export files.

Zotero access uses the local HTTP API at `http://127.0.0.1:23119`. The client
implements only status, collection inventory, item/child/full-text reads, and
attachment-file reads. It never invokes a Zotero mutation endpoint.

## Python API

```python
from auto_zettelkasten.api import MapRequest, run_map

report = run_map(
    MapRequest(
        workspace="~/Research/my-map",
        scope="collection",
        collection_key="COLLECTION_KEY",
        provider="ollama",
        model="llama3.2",
    )
)
print(report.to_dict())
```

Stable v0.2 entry points live in `auto_zettelkasten.api`:

- `initialize_workspace`
- `doctor`
- `list_collections`
- `inventory`
- `run_map`
- `resume_map`
- `get_status`
- `build_map`
- `export_to_obsidian`
- `migrate_workspace`
- `run_expansion`
- `resume_expansion`
- `list_expansion_candidates`
- `decide_expansion`
- `map_accepted_candidates`
- `export_expansion_candidates`

Providers, Zotero, and proposal controllers are injectable through protocols in
`auto_zettelkasten.ports`. This is the supported integration boundary for
Research OS and other controllers.

## Terminal accounting and resume

Every inventoried item ends a mapping run as either:

- `validated_note`; or
- `exhausted`, with a route-level attempt record and reason.

The run invariant is therefore:

```text
inventory_count == validated_note_count + exhausted_count
```

Validated notes are fingerprinted by Zotero item key, inspected-content hash,
Zotero metadata, question lens, extraction/chunking version, prompt version,
and the effective provider and model. Matching reruns reuse the existing note.
Resume reacquires content and validates that full fingerprint before reuse, so
item reordering or changed source content cannot attach the wrong note.

The run source set records exactly what that run attempted. Literature maps use
a separate coherent workspace source set containing all indexed notes; limited
`fulltext_available` or metadata/abstract notes can participate in typed-link
reconstruction but cannot form canonical clusters or support candidate gaps.

## Scope deliberately deferred from v0.2

- direct `zotero.sqlite` ingestion;
- Zotero writes or collection synchronization;
- group libraries;
- a graphical interface or background daemon;
- a database, vector store, or ontology engine; and
- bundled Zotero or Obsidian companion plugins; and
- claims that a generated gap is publication-grade novelty.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
