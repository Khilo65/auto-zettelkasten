# Local discovery and recall

Local discovery prepares relationship candidates from existing source profiles,
references, tags, and Zotero Related records. It makes no provider calls and
does not write to Zotero. Candidates require a separate substantive decision
before becoming research links.

## Inspect a bounded selection

```sh
auto-zettelkasten local-discovery --workspace /path/to/workspace \
  --instance-id my-zotero-desktop --collection COLLECTION_KEY --limit 32
```

Use `--start` for another collection page, or repeat `--item ITEM_KEY` for an
explicit selection. The instance ID identifies this Zotero installation and must
remain stable across scans. The current client supports user libraries.

The scanner reuses complete cached text when coverage is established. Otherwise
it attempts a bounded local attachment extraction with OCR disabled. It skips
Zotero notes and linked URLs as file inputs. Partial, scanned, unavailable, and
failed documents retain their limitations; no references found does not prove
that a document has no bibliography.

Compact machine records live in `.auto-zettelkasten/discovery.sqlite3` inside the
workspace. They contain identities, coverage, references, and fingerprints,
without full source text or bibliography Markdown files. Keep this directory
out of version control. Cache identities include instance, library, and item;
changing a parser or input invalidates exact scan reuse.

The built-in parser conservatively retains lines under bibliography headings
and recognizes identifiers. Its bibliography coverage remains unknown. The
callable scanner accepts a separately versioned local structured parser for
title/author/year matching. AnyStyle and GROBID are not installed or adopted by
these commands.

Matching uses the provided scoped metadata index, including available cached
records and current catalogue identifiers. An unmatched reference does not prove
that its target is absent elsewhere in Zotero. Verified catalogue aliases can
resolve to their existing source note; ambiguous records remain unresolved.

## Prepare candidates or research context

### Combine collection and library discovery from saved embeddings

```sh
auto-zettelkasten refresh-discovery --workspace /path/to/workspace \
  --neighbors 8 --preview-decisions --max-pairs 256 --per-source-cap 8
```

This refresh uses checksummed saved vectors and current catalogue collection
keys. It does not load an embedding model, acquire sources, regenerate notes,
or contact Zotero. Missing or stale embedding inputs stop publication rather
than trigger automatic regeneration. Its library scope is the sources in the
saved map, not every item that may currently exist in Zotero.

The neighbor allocation starts half within shared collections and half across
the library, prioritizing external neighbors in the library lane. Unused slots
can be filled from the remaining eligible neighbors. Overlapping collection
memberships do not multiply the allocation, and unfiled notes remain eligible.
Duplicate pairs are merged; similarity scores remain discovery suggestions.
The wider candidate pool and comparison queue retain work for later passes.
Endpoint and job caps allocate one pass, rather than limiting all future links.

Leiden runs on the combined network, so communities may span collections. The
graph's **Discovery scope** control filters suggestions by the search that
found them. It leaves accepted relationships and citations intact. A pair found
by both searches can appear in either filtered view. Collection folders still
filter which notes are visible.

`--preview-decisions` prepares the existing isolated-pair Jev packets with a
zero spending allowance. The output separates a byte-based planning estimate
from the pinned billing policy's conservative model-usage ceiling. Neither is
a guaranteed invoice cap. This command has no paid-dispatch option. Explicit
approval of the exact requests and a separate spending limit remain necessary
before live decisions. Existing evidence-bound decisions can be reused when
only discovery ranks or collection memberships change.

For a regular mapping run, `--discovery-scope collection_and_library` enables
the same scoped discovery policy. The historical `library` behavior remains
the default. Reviewed calibration from a different discovery policy is not
silently applied to the new route.

```sh
auto-zettelkasten candidates --workspace /path/to/workspace \
  --instance-id my-zotero-desktop --library-id 0 --max-pairs 128 > /path/to/shortlist.json

auto-zettelkasten recall --workspace /path/to/workspace \
  --instance-id my-zotero-desktop --library-id 0 \
  --query "institutional change" --max-notes 8 --max-chars 12000
```

Candidate ranking loads compact profiles. Recall searches the complete
substantive Markdown notes, including saved personal edits. It combines local
word search and compatible cached MiniLM vectors, taking 32 candidates per
route and combining ranks with reciprocal-rank fusion (constant 60). The
default evidence packet contains at most eight notes and 12,000 serialized
characters. It returns literal excerpts, source identities, note paths, hashes
and coverage. Similarity ranks do not establish a research relationship.

Quick-open in the desktop app uses the same search and shows short excerpts.
Python integrations can call `auto_zettelkasten.api.recall(workspace, query)`.
An existing local model can be selected with `recall --model-path /path/to/model`
or the app's embedding-model setting. Search never downloads models, generates
notes or invokes a provider. If compatible vectors or the local encoder are
unavailable, full-note word search remains available and the response reports
the semantic limitation. Changed notes cannot reuse stale semantic vectors.
Use `recall --profile-only` for the previous compact-profile route.

If no compatible note vectors exist, choose **Build local search index** in
quick-open, or run `auto-zettelkasten index-recall --workspace /path/to/workspace
--model-path /path/to/model`. This explicitly embeds the existing note text
into a separate, resumable local cache. It does not regenerate notes or replace
the mapping graph. Python integrations can call `api.prepare_recall(workspace,
model_path="/path/to/model")`. Source-document vectors are reused only when
they match the current complete note representation and encoder contract.

Jev is separate optional enrichment, off by default. Search neither invokes it
nor requires its approval to retrieve a note or preserve a discovery link.

Reading the candidate cache does not reacquire sources or prove current
freshness. Cached reference coverage is labeled separately from source-note
coverage. Omitting the instance ID excludes cached references; candidate and
profile-only routes default the user-library ID to `0`. Full-note recall
searches all visible workspace notes unless a source scope is explicitly
selected. Missing reference-cache records remain not examined.

Popular postings, candidate fanout, pairs per source, total pairs, and recall
characters are bounded. These limits can omit useful candidates or return fewer
pairs than requested. Scores rank candidates and are not probabilities. Sharing
a reference or tag does not establish support, disagreement, or influence.

## Use a reviewed shortlist in the mapper

```sh
auto-zettelkasten build-map --workspace /path/to/workspace \
  --local-candidates /path/to/shortlist.json --no-clusters
```

This explicitly replaces relationship discovery for that build with the local
shortlist. Existing provider consent, model selection, and spending controls
still apply. The mapper batches up to eight pairs, shrinking packets to fit the
context budget, and requests the final relationship decision and explanation
together. Code projects the result into the existing managed relationship
section. It does not call another model to write Markdown.

Prepared pair evidence is compact and fingerprinted; full reference metadata
stays in the cache. The shortlist cannot refill itself through bibliography or family all-pairs
expansion. Valid completed decisions are reused; changing rank alone does not
invalidate a decision. Changed endpoint hashes are rejected for review. Failed
or missing selected decisions remain retryable without resubmitting completed
siblings. An explicit empty shortlist differs from the historical discovery
route, which remains the default when `--local-candidates` is absent.

Selected comparison completion does not mean every possible source pair was
examined. Cluster membership remains separate: unclustered does not mean
unrelated. Historical recovery requires its own reviewed execution identity and
budget; these commands do not start a recovery job.
