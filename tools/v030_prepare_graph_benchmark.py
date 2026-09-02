#!/usr/bin/env python3
"""Freeze a diverse anchor benchmark over an existing full-library graph."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.notes import semantic_note_hash


ELIGIBLE_STATUSES = {"analytical_atomic_note", "verified_atomic_note"}
COHORTS = ("ordinary", "sparse_non_bridge", "bridge")
PARTITIONS = ("development", "locked_test")
SPEC_KEYS = {"schema_version", "benchmark_id", "seed", "strata"}
STRATUM_KEYS = {
    "stratum_id",
    "label",
    "collection_keys",
    "include_descendants",
    "predeclared_bridge_source_ids",
    "rationale",
}


def stable_key(*values: str) -> str:
    return hashlib.sha256(":".join(values).encode()).hexdigest()


def load_catalogue(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        value = read_yaml(path, {}) or {}
    if not isinstance(value, Mapping):
        raise ValueError(f"catalogue must be a mapping: {path}")
    return dict(value)


def validate_spec(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(spec) != SPEC_KEYS or str(spec.get("schema_version")) != "1":
        raise ValueError("benchmark spec has unexpected fields or schema version")
    strata = [dict(row) for row in spec.get("strata", []) or []]
    if len(strata) != 20:
        raise ValueError(f"benchmark requires exactly 20 strata, found {len(strata)}")
    identifiers = []
    for row in strata:
        if set(row) != STRATUM_KEYS:
            raise ValueError(f"invalid stratum fields: {row.get('stratum_id', '')}")
        if not row.get("collection_keys"):
            raise ValueError(f"stratum has no collection keys: {row['stratum_id']}")
        identifiers.append(str(row["stratum_id"]))
    if len(set(identifiers)) != 20:
        raise ValueError("stratum IDs must be unique")
    return strata


def descendant_keys(
    roots: Iterable[str], collections: Iterable[Mapping[str, Any]]
) -> set[str]:
    children: dict[str, set[str]] = defaultdict(set)
    known = set()
    for row in collections:
        key = str(row.get("key") or "")
        known.add(key)
        children[str(row.get("parent_key") or "")].add(key)
    result = {str(value) for value in roots}
    missing = result - known
    if missing:
        raise ValueError(f"unknown collection keys: {sorted(missing)}")
    pending = list(result)
    while pending:
        for child in children.get(pending.pop(), set()):
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def note_path(origin: Path, note_id: str) -> Path:
    metadata = read_yaml(
        origin / "11_state" / "note_metadata" / f"{note_id}.yml", {}
    ) or {}
    relative = str(metadata.get("note_path") or "")
    if not relative:
        raise ValueError(f"missing note path for {note_id}")
    return origin / relative


def canonical_group(row: Mapping[str, Any]) -> str:
    identity = row.get("identity", {})
    identity = identity if isinstance(identity, Mapping) else {}
    for field in ("doi", "isbn"):
        value = str(identity.get(field) or "").strip().casefold()
        if value:
            return f"{field}:{value}"
    title = str(identity.get("normalized_title") or row.get("title") or "").casefold()
    author = ",".join(identity.get("normalized_author_surnames", []) or [])
    year = str(identity.get("year") or row.get("year") or "")
    return f"work:{title}|{author}|{year}"


def structural_adjacency(
    positions: Iterable[Mapping[str, Any]], eligible: set[str]
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for row in positions:
        left = str(row.get("current_source_id") or "")
        right = str(row.get("matched_source_id") or "")
        if left in eligible and right in eligible and left != right:
            graph[left].add(right)
            graph[right].add(left)
    return graph


def collection_adjacency(
    source_by_id: Mapping[str, Mapping[str, Any]], eligible: set[str]
) -> dict[str, set[str]]:
    members: dict[str, set[str]] = defaultdict(set)
    for source_id in eligible:
        for key in source_by_id[source_id].get("collection_keys", []) or []:
            members[str(key)].add(source_id)
    graph: dict[str, set[str]] = defaultdict(set)
    for source_ids in members.values():
        for source_id in source_ids:
            graph[source_id].update(source_ids - {source_id})
    return graph


def bridge_candidates(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        for endpoint, counterpart in (("left", "right"), ("right", "left")):
            source_id = str(row.get(endpoint, {}).get("source_id") or "")
            candidate_id = str(row.get(counterpart, {}).get("source_id") or "")
            if source_id and candidate_id:
                result[source_id].append(
                    {
                        "candidate_source_id": candidate_id,
                        "pair_id": str(row.get("pair_id") or ""),
                        "basis": "pregraph_full_library_bridge_benchmark",
                    }
                )
    return result


def choose_two(
    candidates: Iterable[str], *, seed: str, stratum_id: str, cohort: str,
    source_by_id: Mapping[str, Mapping[str, Any]], used_sources: set[str],
    used_groups: set[str],
) -> list[str]:
    ordered = sorted(
        set(candidates),
        key=lambda source_id: stable_key(seed, stratum_id, cohort, source_id),
    )
    selected = []
    for source_id in ordered:
        group = canonical_group(source_by_id[source_id])
        if source_id in used_sources or group in used_groups:
            continue
        selected.append(source_id)
        used_sources.add(source_id)
        used_groups.add(group)
        if len(selected) == 2:
            return selected
    raise ValueError(
        f"insufficient unique {cohort} anchors for {stratum_id}: "
        f"candidates={len(ordered)} selected={len(selected)}"
    )


def prepare(origin: Path, spec_path: Path, output_dir: Path) -> dict[str, Any]:
    catalogue_path = origin / "02_source_memory/indexes/source_catalogue.yml"
    positions_path = origin / "02_source_memory/indexes/literature_positions.yml"
    snapshot_path = origin / "01_custody/zotero/collection_snapshot.yml"
    bridge_path = origin / "evaluation/pregraph-full-library-bridge-benchmark.yml"
    catalogue = load_catalogue(catalogue_path)
    positions = read_yaml(positions_path, {}) or {}
    snapshot = read_yaml(snapshot_path, {}) or {}
    bridge_rows = read_yaml(bridge_path, {}) or {}
    bridge_by_source = bridge_candidates(bridge_rows.get("pairs", []) or [])
    item_type_by_key = {
        str(row.get("key") or "").casefold(): str(row.get("item_type") or "")
        for row in snapshot.get("items", []) or []
    }
    spec = read_yaml(spec_path, {}) or {}
    strata = validate_spec(spec)
    source_by_id = {
        str(row.get("source_id") or ""): dict(row)
        for row in catalogue.get("sources", []) or []
        if row.get("source_id")
    }
    eligible = {
        source_id
        for source_id, row in source_by_id.items()
        if str(row.get("note_status") or "") in ELIGIBLE_STATUSES
        and str(row.get("canonical_source_id") or source_id) == source_id
        and str(row.get("evidence_eligibility") or "") == "substantive_bounded"
    }
    exact_graph = structural_adjacency(positions.get("positions", []) or [], eligible)
    collection_graph = collection_adjacency(source_by_id, eligible)
    seed = str(spec["seed"])
    used_sources: set[str] = set()
    used_groups: set[str] = set()
    anchors: list[dict[str, Any]] = []
    feasibility = []

    for stratum in strata:
        roots = [str(value) for value in stratum["collection_keys"]]
        keys = (
            descendant_keys(roots, catalogue.get("collections", []) or [])
            if bool(stratum["include_descendants"])
            else set(roots)
        )
        pool = {
            source_id
            for source_id in eligible
            if set(source_by_id[source_id].get("collection_keys", []) or []) & keys
        }
        bridge = set(stratum["predeclared_bridge_source_ids"])
        if not bridge <= pool:
            raise ValueError(
                f"bridge seeds outside {stratum['stratum_id']}: {sorted(bridge - pool)}"
            )
        missing_bridge_evidence = bridge - set(bridge_by_source)
        if missing_bridge_evidence:
            raise ValueError(
                "bridge seeds absent from frozen pregraph benchmark: "
                f"{sorted(missing_bridge_evidence)}"
            )
        remaining = pool - bridge
        ranked = sorted(
            remaining,
            key=lambda source_id: (
                len(exact_graph[source_id]),
                len(collection_graph[source_id] & pool),
                stable_key(seed, str(stratum["stratum_id"]), source_id),
            ),
        )
        sparse_count = max(2, len(ranked) // 3)
        sparse = set(ranked[:sparse_count])
        ordinary = remaining - sparse
        cohorts = {
            "ordinary": ordinary,
            "sparse_non_bridge": sparse,
            "bridge": bridge,
        }
        feasibility.append(
            {
                "stratum_id": stratum["stratum_id"],
                "eligible_count": len(pool),
                "ordinary_candidate_count": len(ordinary),
                "sparse_candidate_count": len(sparse),
                "bridge_candidate_count": len(bridge),
                "sparse_rank_count": sparse_count,
            }
        )
        for cohort in COHORTS:
            selected = choose_two(
                cohorts[cohort],
                seed=seed,
                stratum_id=str(stratum["stratum_id"]),
                cohort=cohort,
                source_by_id=source_by_id,
                used_sources=used_sources,
                used_groups=used_groups,
            )
            selected.sort(
                key=lambda source_id: stable_key(
                    seed, str(stratum["stratum_id"]), cohort, source_id
                )
            )
            for partition, source_id in zip(PARTITIONS, selected, strict=True):
                row = source_by_id[source_id]
                note = note_path(origin, str(row["note_id"]))
                profile = (
                    origin / "02_source_memory/profiles" / f"{row['note_id']}.yml"
                )
                cross_reference = sorted(exact_graph[source_id] - pool)
                anchors.append(
                    {
                        "anchor_id": f"anchor-{stable_key(seed, source_id)[:16]}",
                        "source_id": source_id,
                        "canonical_group": canonical_group(row),
                        "stratum_id": str(stratum["stratum_id"]),
                        "cohort": cohort,
                        "partition": partition,
                        "title": str(row.get("title") or ""),
                        "year": str(row.get("year") or ""),
                        "item_type": item_type_by_key.get(
                            source_id.removeprefix("source-zotero-").casefold(), ""
                        ),
                        "source_scope": str(row.get("source_scope") or ""),
                        "evidence_coverage": str(row.get("evidence_coverage") or ""),
                        "note_id": str(row["note_id"]),
                        "note_path": str(note.relative_to(origin)),
                        "semantic_note_sha256": semantic_note_hash(
                            note.read_text(encoding="utf-8")
                        ),
                        "profile_path": str(profile.relative_to(origin)),
                        "profile_sha256": sha256_file(profile),
                        "selection_evidence": {
                            "exact_citation_degree": len(exact_graph[source_id]),
                            "within_stratum_exact_citation_degree": len(
                                exact_graph[source_id] & pool
                            ),
                            "within_stratum_collection_degree": len(
                                collection_graph[source_id] & pool
                            ),
                            "cross_stratum_exact_citation_source_ids": cross_reference,
                            "predeclared_bridge": source_id in bridge,
                            "pre_rank_bridge_candidates": (
                                bridge_by_source[source_id]
                                if source_id in bridge
                                else []
                            ),
                        },
                    }
                )

    anchors.sort(key=lambda row: str(row["anchor_id"]))
    development = [row for row in anchors if row["partition"] == "development"]
    canary = []
    for index, stratum in enumerate(strata):
        cohort = COHORTS[index % len(COHORTS)]
        row = next(
            row
            for row in development
            if row["stratum_id"] == stratum["stratum_id"]
            and row["cohort"] == cohort
        )
        canary.append(row["anchor_id"])
    # Fill the remaining four slots to exactly eight anchors per cohort.
    while len(canary) < 24:
        counts = Counter(
            next(row["cohort"] for row in development if row["anchor_id"] == value)
            for value in canary
        )
        cohort = min(COHORTS, key=lambda value: (counts[value], value))
        candidate = next(
            row["anchor_id"]
            for row in development
            if row["cohort"] == cohort and row["anchor_id"] not in canary
        )
        canary.append(candidate)

    manifest = {
        "schema_version": "1",
        "benchmark_id": str(spec["benchmark_id"]),
        "seed": seed,
        "status": "anchor_selection_complete_gold_pending",
        "never_production_input": True,
        "frozen_inputs": {
            "source_catalogue_sha256": sha256_file(catalogue_path),
            "literature_positions_sha256": sha256_file(positions_path),
            "collection_snapshot_sha256": sha256_file(snapshot_path),
            "pregraph_bridge_benchmark_sha256": sha256_file(bridge_path),
            "strata_spec_sha256": sha256_file(spec_path),
        },
        "corpus": {
            "source_record_count": len(source_by_id),
            "canonical_work_count": sum(
                str(row.get("canonical_source_id") or source_id) == source_id
                for source_id, row in source_by_id.items()
            ),
            "eligible_anchor_count": len(eligible),
            "retrieval_scope": "full_frozen_source_catalogue",
        },
        "selection_policy": {
            "anchor_count": 120,
            "stratum_count": 20,
            "cohorts": list(COHORTS),
            "anchors_per_cohort_per_stratum": 2,
            "partition_policy": "one anchor per stratum and cohort per partition",
            "sparse_policy": "bottom non-bridge third by exact-citation then collection degree",
            "bridge_policy": "predeclared independent candidate; manual confirmation required",
        },
        "feasibility": feasibility,
        "diversity": {
            "item_types": dict(Counter(row["item_type"] for row in anchors)),
            "source_scopes": dict(Counter(row["source_scope"] for row in anchors)),
            "evidence_coverage": dict(
                Counter(row["evidence_coverage"] for row in anchors)
            ),
        },
        "anchors": anchors,
        "phase_zero_canary_anchor_ids": sorted(canary),
    }
    manifest["selection_sha256"] = stable_key(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    )
    judgments = {
        "schema_version": "1",
        "benchmark_id": manifest["benchmark_id"],
        "selection_sha256": manifest["selection_sha256"],
        "status": "pending_manual_reference_graph",
        "minimum_independent_positives_per_anchor": 2,
        "minimum_hard_negatives_per_anchor": 2,
        "anchors": [
            {
                "anchor_id": row["anchor_id"],
                "status": "pending",
                "manual_independent_positive_source_ids": [],
                "hard_negative_source_ids": [],
                "pre_rank_bridge_candidates": row["selection_evidence"][
                    "pre_rank_bridge_candidates"
                ],
                "judgments": [],
            }
            for row in anchors
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "anchors.yml", manifest)
    write_yaml(output_dir / "judgments-template.yml", judgments)
    return manifest


def validate(origin: Path, spec_path: Path, selection_path: Path) -> dict[str, Any]:
    manifest = read_yaml(selection_path, {}) or {}
    expected = prepare(origin, spec_path, selection_path.parent / ".validation")
    try:
        if manifest != expected:
            raise ValueError("selection does not match deterministic regeneration")
    finally:
        for path in (selection_path.parent / ".validation").glob("*"):
            path.unlink()
        (selection_path.parent / ".validation").rmdir()
    anchors = manifest.get("anchors", []) or []
    counts = Counter((row["stratum_id"], row["cohort"]) for row in anchors)
    partitions = Counter(row["partition"] for row in anchors)
    if len(anchors) != 120 or set(counts.values()) != {2}:
        raise ValueError("anchor quota mismatch")
    if partitions != Counter({"development": 60, "locked_test": 60}):
        raise ValueError(f"partition mismatch: {dict(partitions)}")
    if len({row["source_id"] for row in anchors}) != 120:
        raise ValueError("duplicate anchor source")
    if len({row["canonical_group"] for row in anchors}) != 120:
        raise ValueError("canonical work crosses anchor slots")
    return {
        "status": "valid",
        "anchor_count": 120,
        "development_count": 60,
        "locked_test_count": 60,
        "phase_zero_canary_count": len(manifest["phase_zero_canary_anchor_ids"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "validate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--origin", type=Path, required=True)
        subparser.add_argument("--spec", type=Path, required=True)
        if command == "prepare":
            subparser.add_argument("--output-dir", type=Path, required=True)
        else:
            subparser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()
    result = (
        prepare(args.origin, args.spec, args.output_dir)
        if args.command == "prepare"
        else validate(args.origin, args.spec, args.selection)
    )
    summary = (
        {
            "status": result["status"],
            "anchor_count": len(result["anchors"]),
            "corpus": result["corpus"],
            "phase_zero_canary_count": len(result["phase_zero_canary_anchor_ids"]),
        }
        if args.command == "prepare"
        else result
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
