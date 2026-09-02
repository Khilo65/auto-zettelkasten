# V0.30 Mapping Benchmark Protocol

The benchmark is a provider-neutral 1,000-work snapshot selected from the frozen v0.28 library. It tests mapping without repeatedly regenerating source notes or processing the full library.

## Frozen design

- 900 active baseline works and 100 sealed incremental works.
- 20 declared literature strata; each contributes exactly five delta works.
- 920 analytical/full-document notes and 80 context-only records.
- 120 graph-retrieval anchors remain in the baseline.
- Selection uses Zotero collections, bibliographic metadata, exact source-derived citation routes, and the independent pre-graph bridge benchmark. It does not use accepted semantic links, graph ranks, families, clusters, gaps, or run receipts.
- The private workspace contains source material and benchmark manifests. The repository contains only this protocol and the deterministic tool.

The sample is strategically balanced, not yet reference-grade. Relationship, cluster, acquisition, and gap judgments must be completed blind before their scores can be treated as gold.

## Commands

```bash
PYTHONPATH=src uv run python tools/v030_prepare_mapping_sample.py prepare \
  --origin /path/to/frozen-v028-workspace \
  --spec /path/to/private/evaluation/v030/mapping-sample-spec.yml \
  --target /path/to/private/v030-mapping-benchmark

PYTHONPATH=src uv run python tools/v030_prepare_mapping_sample.py validate \
  --origin /path/to/frozen-v028-workspace \
  --spec /path/to/private/evaluation/v030/mapping-sample-spec.yml \
  --target /path/to/private/v030-mapping-benchmark

PYTHONPATH=src uv run python tools/v030_prepare_mapping_sample.py activate-delta \
  --target /path/to/private/v030-mapping-benchmark

PYTHONPATH=src uv run python tools/v030_prepare_mapping_sample.py validate \
  --origin /path/to/frozen-v028-workspace \
  --spec /path/to/private/evaluation/v030/mapping-sample-spec.yml \
  --target /path/to/private/v030-mapping-benchmark \
  --activated
```

`prepare` computes and validates the complete selection before creating the target. It makes no provider calls. `activate-delta` is idempotent.

## Two test tracks

Routine mapping tests reuse the frozen notes, profiles, and bundles. They evaluate relationship retrieval and adjudication, family/cluster mapping, gaps, locality, cost, and replay.

Source-generation tests use the separate private 80-source canary: 40 standard articles, 12 books/chapters/long documents, 8 reports/policy/legal works, 8 OCR cases, 8 abstract/partial cases, and 4 metadata/identity controls. These runs occur in an isolated workspace and are not part of routine mapping tests.

## Acceptance

Before a paid mapping run:

1. Run `prepare`, then `validate` twice and require an identical selection hash.
2. Confirm 900 active and 100 sealed works, exact stratum/status quotas, and no active delta IDs.
3. Activate the delta twice; the second activation must make no change.
4. Run activated validation and require 1,000 active works.
5. Record provider call, token, retry, and quota ceilings separately for the evaluation run.

Production prompts must never load files under `evaluation/`.
