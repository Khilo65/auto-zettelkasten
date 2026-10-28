# Local semantic maps

New mapping requests use local `sentence-transformers/all-MiniLM-L6-v2`
embeddings and Leiden communities. This mapping route needs no provider key.
Source-note generation still uses its separately configured source reader and
model; selecting local mapping does not change that role or regenerate notes.

Install the local mapping dependencies in the application's Python environment:

```sh
python -m pip install 'auto-zettelkasten[local-mapping]'
```

Model loading is offline. Supply an existing MiniLM model directory with
`--embedding-model-path` or `local_mapping.model_path`. An existing Hugging Face
cache snapshot directory is suitable; model discovery is not automatic.
In the app's mapper, use **MiniLM model folder → Browse…** to select the cached
directory. The selected folder is checked before an approval ticket is created
and remembered locally after a successful estimate. An explicit selection takes
precedence over the workspace setting; otherwise the workspace setting is kept.
The remembered folder is a default only for workspaces without a model setting.
The estimate review shows the effective model folder before approval.
Missing model files or dependencies produce an
actionable error; the mapper never downloads weights or falls back to paid
embedding or LLM clustering calls.

Build a derived map from existing validated workspace material:

```sh
auto-zettelkasten build-map --workspace /path/to/workspace \
  --embedding-model-path /path/to/cached/minilm
```

The same mapping flags are available on `map`, `sync`, and `estimate`. `map` and
`sync` retain their normal source-reading and note-generation behavior, so use
`build-map` when only refreshing derived maps. Existing notes and editable
overrides remain protected by that route.

Workspace configuration selects the default route independently of source
reader settings:

```yaml
mapping_backend: local
local_mapping:
  encoder: minilm
  clustering: leiden
  model_path: /path/to/cached/minilm
```

The derived cache is under `.auto-zettelkasten/mapping/` in the selected
workspace and is excluded by the repository's standard ignore rules. It
records model, preprocessing, text, ownership, dimension, and
library identity. Atomic checkpoints reuse unchanged source embeddings; changed
sources invalidate their affected representations, then neighbors and
communities refresh. Full source material remains available for auditing and
citation extraction. Cleaning applies to the separate embedding representation,
with excluded and unresolved text coverage reported rather than silently sampled.

The semantic preparation removes generic note-template headings and invisible
link destinations while retaining visible labels, meaningful headings, findings,
negation, conditions and limitations. A narrowly recognized publisher-navigation
footer is excluded; this is a bounded Markdown preparation step, not a complete
HTML or Markdown rendering engine. Original text is unchanged for citation use.

Retained fragments are joined within their original section and ownership group
before tokenization, so a linked word does not become a separately weighted
embedding. Chunk `source_spans` identify the exact retained slices of the original
text; `start` and `end` bound those slices and may also enclose removed markup.
The cleaning and chunking versions are part of the vector identity, so older
representations are not silently mixed with the new representation. A markup-only
change can reuse compatible chunk vectors when visible text and ownership stay
the same, while source hashes and offset mappings update.

Defaults bound the job to 10,000 sources, 100,000 chunks, 64 MiB of input text,
a 1 GiB cache and 600 seconds, with two CPU threads and batches of eight.
Resource checks run between native operations; they do not impose a hard
process-memory limit or interrupt a native call at the wall-time boundary.
Committed chunks survive a stop and can be reused by an explicit refresh.

Semantic neighbors and community memberships are suggestions for navigation.
They do not establish an evidence-backed relationship, accepted link, citation,
consensus, or research gap. Membership documents are not extra source nodes or
semantic hubs.

HDBSCAN is an optional, separately selected density/noise view:

```sh
python -m pip install 'auto-zettelkasten[local-mapping,local-mapping-hdbscan]'
auto-zettelkasten build-map --workspace /path/to/workspace \
  --local-clustering hdbscan --embedding-model-path /path/to/cached/minilm
```

HDBSCAN is not combined with Leiden automatically. Its absence does not disable
the default Leiden route. Qwen is not currently a supported production encoder;
unsupported encoder values fail explicitly instead of selecting a substitute.

To use the existing provider-based mapping compatibility route, explicitly pass
`--mapping-backend llm` or set `mapping_backend: llm`. Provider/privacy admission,
literature model selection, and spending controls still apply on that route.
Saved requests from before backend selection existed resume with their
historical LLM route; new requests and workspace configuration default to local.
