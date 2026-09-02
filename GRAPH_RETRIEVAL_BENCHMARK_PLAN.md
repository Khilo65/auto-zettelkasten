# Graph Retrieval Benchmark Plan

## Objective

Measure whether the existing exact, deterministic-affinity, and adjudicated-semantic graph layers retrieve useful literature relationships. New graph methods are out of scope until this baseline is scored.

The benchmark keeps the complete frozen v0.28 library as the retrieval corpus. Copying a smaller note subset would change degrees, hubs, bridges, and isolated-node behavior—the properties being tested.

## Frozen design

- Retrieval corpus: 4,933 source records representing 4,901 canonical works.
- Anchor eligibility: canonical, substantive, full-document analytical notes with frozen profiles.
- Strata: 20 declared literature domains.
- Anchors: 120 total; six per stratum.
- Cohorts per stratum: two ordinary, two sparse non-bridge, two bridge.
- Partitions: 60 development and 60 locked test, with one anchor from every cohort and stratum in each partition.
- Identity control: aliases and duplicate canonical works cannot occupy separate anchor slots or cross partitions.
- Bridge control: every bridge anchor must occur in the independent pre-global-graph benchmark. These rows are candidates for manual confirmation, not automatic positives.
- Phase-zero process canary: 24 development anchors covering all 20 strata and exactly eight anchors per cohort. Its results are descriptive only.

The selector records only IDs, relative paths, hashes, and selection evidence. It does not copy source text and never feeds evaluation data into production prompts.

## Reference graph

Before ranking any method, reviewers must add for every anchor:

- at least two independently identified useful positives;
- at least two hard negatives;
- endpoint, proposition, relationship type, direction, and evidence judgments where a semantic relationship is claimed.

Existing pre-graph pairs may nominate a candidate but must be reread and confirmed. Unjudged and abstained retrieved rows count as misses. The development partition may be used to lock thresholds; the locked partition is scored once.

## Baseline arms

1. Exact structural links only: citations and explicit identity/custody relations.
2. Exact plus current deterministic affinity links.
3. Exact and affinity plus current accepted semantic relations.

Freeze ranking, fusion, top-k, canonical deduplication, and tie-breaking before pooling candidates. Reviewers must not see the producing arm, rank, route, or score.

Primary metrics are precision at five and independently authored positive recall at five. Secondary metrics are sparse-anchor rescue, cross-stratum bridge retrieval, isolated-anchor rate, and two-hop reachability. Guardrails are false thematic adjacency, identity errors, hub concentration, and loss of existing accepted semantic edges.

## Commands

```bash
uv run python tools/v030_prepare_graph_benchmark.py prepare \
  --origin /path/to/frozen-v028-workspace \
  --spec /path/to/private/evaluation/v030/graph-benchmark-strata.yml \
  --output-dir /path/to/private/evaluation/v030

uv run python tools/v030_prepare_graph_benchmark.py validate \
  --origin /path/to/frozen-v028-workspace \
  --spec /path/to/private/evaluation/v030/graph-benchmark-strata.yml \
  --selection /path/to/private/evaluation/v030/anchors.yml
```

The strata, anchor manifest, judgments, and review packets remain private because they describe the user's library. The repository contains only this protocol and the deterministic prepare/validate tool.

## Release gates

Selection is ready when prepare and validate are byte-stable and all structural quotas pass. The benchmark is reference-grade only after all 120 anchors meet the positive/negative judgment minimums, independent review disagreements are adjudicated, and the locked partition remains unopened until final scoring.

No provider calls, clustering, new graph methods, or full-library rebuild are required to prepare or validate the benchmark.
