# Release notes

## 0.30.0 — relationship-first release candidate

- New workspaces generate reciprocal typed relationships with clusters and gaps
  off. Existing cluster/gap artifacts and exact membership rows are preserved;
  legacy workspaces without the setting keep their previous clustering behavior.
- `map`, `sync`, `build-map`, and `estimate` accept `--clusters` and
  `--no-clusters`, and estimates make disabled cluster/gap work explicit zeroes.
- Codex CLI 0.145.0 uses separate Luna source and Terra relationship roles,
  fail-closed tools and credentials, resumable quota/timeout/interruption
  handling, a four-call automatic concurrency limit, a 1–8 explicit boundary,
  and a fail-fast lock against overlapping local Auto-Zettelkasten Codex runs.
- `ocr=auto` can send at most 16 deterministic, bounded PNG page images only to
  the Codex source-bundle contract after custody, capability, and token checks.
  Direct PDF input remains unsupported.
- Source prompt 14 and bundle prompt 9 add conditional definitions and source
  structure while retaining exact completed-checkpoint reuse for 12/7 and 13/8.

Artifact schema 1.20 and evidence-profile schema 1.3 are unchanged. This release
does not add direct-PDF transport, extended context, API spending, external
discovery, tags, pushes, or upstream changes.
