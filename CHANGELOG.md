# Release notes

## Unreleased

- Book notes request a concise whole-book analysis followed by chapter-by-chapter
  theses, arguments, evidence/data and qualifications in the existing structure
  section. Edited-volume authors and partial coverage stay explicit. Shared
  subscription/API prompts and chunk memos retain chapter attribution without
  adding model calls or changing article structure. Prompt identities advance to
  atomic 16 / source bundle 42; historical artifacts remain readable.

## 0.30.0 — relationship-first release candidate

- New workspaces generate reciprocal typed relationships with clusters and gaps
  off. Existing cluster/gap artifacts and exact membership rows are preserved;
  legacy workspaces without the setting keep their previous clustering behavior.
- `map`, `sync`, `build-map`, and `estimate` accept `--clusters` and
  `--no-clusters`, and estimates make disabled cluster/gap work explicit zeroes.
- Supported Codex CLI profiles 0.145.0 and 0.152.1 use separate Luna source
  and Terra relationship roles,
  fail-closed tools and credentials, resumable quota/timeout/interruption
  handling, automatic concurrency of up to four source calls and one literature
  call, a 1–8 explicit boundary,
  and a fail-fast lock against overlapping local Auto-Zettelkasten Codex runs.
- With `ocr=auto`, the verified macOS arm64 Codex 0.152.1 companion can send
  the selected original PDF in one source request. Existing file, custody and
  provider limits remain; extraction/OCR fallbacks remain available.
- Atomic notes request author-date/page citations across subscription and
  API-key routes. Code renders source-PDF links using the selected attachment,
  independently of model-written locators.
- Current source and profile generation no longer builds an anchor inventory
  or appends duplicate findings. Full-note cluster synthesis retains structured
  source contributions without generating legacy evidence matrices.
- Verified-companion graph calls use HTTP streaming without automatic transport
  retries. Cluster planning and synthesis honor the configured request deadline.
- Relationship work denied by a local call ceiling remains pending instead of
  being mislabeled as a permanent provider failure; completed work is retained.

Artifact schema 1.20 is retained; current profiles use schema 1.4. Historical
formats remain readable through explicit compatibility paths. The existing
family planner remains the mapping approach. Incremental retrieval, larger
scaling comparisons and batching adoption remain deferred.
