#!/usr/bin/env python3
"""Prepare a deterministic, graph-only v0.29 sample from the frozen v0.28 corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Mapping

from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml


ANALYTICAL_STATUSES = {"analytical_atomic_note", "verified_atomic_note"}
EXPLICIT_COLLECTIONS = {"B887A4Q8", "D2XT9ZU9"}
MANAGED_BLOCK = re.compile(
    r"\n?<!-- auto-zettelkasten:(?:graph|literature):start -->.*?"
    r"<!-- auto-zettelkasten:(?:graph|literature):end -->\n?",
    re.DOTALL,
)


def stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def semantic_note_hash(path: Path) -> str:
    text = MANAGED_BLOCK.sub("\n", path.read_text(encoding="utf-8"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def profile_path(root: Path, note_id: str) -> Path:
    return root / "02_source_memory" / "profiles" / f"{note_id}.yml"


def metadata_path(root: Path, note_id: str) -> Path:
    return root / "11_state" / "note_metadata" / f"{note_id}.yml"


def note_path(root: Path, note_id: str) -> Path:
    metadata = read_yaml(metadata_path(root, note_id), {}) or {}
    relative = str(metadata.get("note_path") or "")
    if not relative:
        raise ValueError(f"missing note_path for {note_id}")
    return root / relative


def rows_for_sources(
    source_ids: Iterable[str], source_by_id: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [dict(source_by_id[source_id]) for source_id in sorted(source_ids)]


def collection_ancestors(
    collection_keys: set[str], collections: list[Mapping[str, Any]]
) -> set[str]:
    by_key = {str(row.get("key") or ""): row for row in collections}
    pending = list(collection_keys)
    while pending:
        key = pending.pop()
        parent = str(by_key.get(key, {}).get("parent_key") or "")
        if parent and parent not in collection_keys:
            collection_keys.add(parent)
            pending.append(parent)
    return collection_keys


def filtered_snapshot(
    snapshot: Mapping[str, Any], rows: list[Mapping[str, Any]]
) -> dict[str, Any]:
    zotero_keys = {str(row.get("zotero_key") or "").upper() for row in rows}
    items = [
        dict(item)
        for item in snapshot.get("items", []) or []
        if str(item.get("key") or "").upper() in zotero_keys
    ]
    collection_keys = {
        str(key)
        for item in items
        for key in item.get("collection_keys", []) or []
        if str(key)
    }
    collections = [dict(row) for row in snapshot.get("collections", []) or []]
    collection_keys = collection_ancestors(collection_keys, collections)
    kept_collections = [
        row for row in collections if str(row.get("key") or "") in collection_keys
    ]
    payload = {
        "schema_version": str(snapshot.get("schema_version") or "1"),
        "items": sorted(items, key=lambda row: str(row.get("key") or "")),
        "collections": sorted(
            kept_collections, key=lambda row: str(row.get("key") or "")
        ),
    }
    payload["fingerprint"] = stable_key(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )
    return payload


def filtered_positions(
    positions: Iterable[Mapping[str, Any]], source_ids: set[str]
) -> dict[str, Any]:
    rows = []
    for raw in positions:
        current = str(raw.get("current_source_id") or "")
        matched = str(raw.get("matched_source_id") or "")
        if current not in source_ids or (matched and matched not in source_ids):
            continue
        rows.append(dict(raw))
    rows.sort(key=lambda row: str(row.get("literature_position_id") or ""))
    return {
        "literature_position_registry_schema_version": "2",
        "positions": rows,
        "projection_errors": [],
        "revision_hash": stable_key(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        ),
    }


def choose_routes(
    catalogue: Mapping[str, Any], eligible: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    topics = []
    for row in catalogue.get("virtual_topics", []) or []:
        members = sorted(eligible & set(row.get("source_ids", []) or []))
        if 4 <= len(members) <= 80:
            topics.append({**dict(row), "eligible_source_ids": members})
    topics.sort(
        key=lambda row: (
            min(len(row["eligible_source_ids"]) // 10, 7),
            stable_key(str(row.get("topic_id") or "")),
        )
    )
    selected_topics: list[dict[str, Any]] = []
    for bucket in range(8):
        selected_topics.extend(
            [
                row
                for row in topics
                if min(len(row["eligible_source_ids"]) // 10, 7) == bucket
            ][:2]
        )
    if len(selected_topics) < 16:
        chosen = {str(row.get("topic_id") or "") for row in selected_topics}
        selected_topics.extend(
            row
            for row in topics
            if str(row.get("topic_id") or "") not in chosen
        )
    selected_topics = selected_topics[:16]

    collections = []
    for row in catalogue.get("collections", []) or []:
        key = str(row.get("key") or "")
        members = sorted(eligible & set(row.get("direct_source_ids", []) or []))
        if key not in EXPLICIT_COLLECTIONS and 5 <= len(members) <= 60:
            collections.append({**dict(row), "eligible_source_ids": members})
    collections.sort(key=lambda row: stable_key(str(row.get("key") or "")))
    return selected_topics, collections[:8]


def select_sample(
    catalogue: Mapping[str, Any], positions: list[Mapping[str, Any]]
) -> tuple[set[str], set[str], dict[str, Any]]:
    source_by_id = {
        str(row.get("source_id") or ""): dict(row)
        for row in catalogue.get("sources", []) or []
        if row.get("source_id")
    }
    eligible = {
        source_id
        for source_id, row in source_by_id.items()
        if str(row.get("note_status") or "") in ANALYTICAL_STATUSES
        and str(row.get("canonical_source_id") or source_id) == source_id
    }
    selected = {
        source_id
        for source_id in eligible
        if set(source_by_id[source_id].get("collection_keys", []) or [])
        & EXPLICIT_COLLECTIONS
    }
    protected = set(selected)
    selected_topics, selected_collections = choose_routes(catalogue, eligible)
    for route in [*selected_topics, *selected_collections]:
        selected.update(route["eligible_source_ids"][:12])
    unfiled = sorted(
        (
            source_id
            for source_id in eligible
            if not source_by_id[source_id].get("collection_keys")
        ),
        key=stable_key,
    )
    selected.update(unfiled[:20])

    exact_neighbors = {
        str(row.get("matched_source_id") or "")
        for row in positions
        if str(row.get("current_source_id") or "") in selected
        and str(row.get("matched_source_id") or "") in eligible
    }
    for source_id in sorted(exact_neighbors - selected, key=stable_key)[:40]:
        selected.add(source_id)
        protected.add(source_id)

    for source_id in sorted(eligible - selected, key=stable_key):
        if len(selected) >= 450:
            break
        selected.add(source_id)
    if not 300 <= len(selected) <= 500:
        raise ValueError(f"sample size outside plan: {len(selected)}")

    delta_candidates = sorted(
        (
            source_id
            for source_id in selected - protected
            if not (
                set(source_by_id[source_id].get("collection_keys", []) or [])
                & EXPLICIT_COLLECTIONS
            )
        ),
        key=lambda source_id: stable_key(f"delta:{source_id}"),
    )
    delta = set(delta_candidates[:25])
    if len(delta) != 25:
        raise ValueError("could not reserve 25 incremental sources")
    baseline = selected - delta
    route_manifest = {
        "selected_topic_routes": [
            {
                "topic_id": row.get("topic_id"),
                "label": row.get("label"),
                "eligible_source_count": len(row["eligible_source_ids"]),
            }
            for row in selected_topics
        ],
        "selected_collection_routes": [
            {
                "collection_key": row.get("key"),
                "name": row.get("name"),
                "eligible_source_count": len(row["eligible_source_ids"]),
            }
            for row in selected_collections
        ],
        "explicit_collection_keys": sorted(EXPLICIT_COLLECTIONS),
        "unfiled_source_count": sum(
            1
            for source_id in baseline
            if not source_by_id[source_id].get("collection_keys")
        ),
    }
    return baseline, delta, route_manifest


def copy_sources(
    origin: Path,
    target: Path,
    rows: list[Mapping[str, Any]],
    *,
    relative_root: Path | None = None,
) -> list[dict[str, Any]]:
    destination = target if relative_root is None else target / relative_root
    manifest = []
    for row in rows:
        note_id = str(row["note_id"])
        source_note = note_path(origin, note_id)
        relative_note = source_note.relative_to(origin)
        source_profile = profile_path(origin, note_id)
        profile_payload = read_yaml(source_profile, {}) or {}
        profile = profile_payload.get("profile", profile_payload)
        context = profile.get("context", {}) if isinstance(profile, Mapping) else {}
        bundle_relative = str(context.get("source_analysis_bundle_path") or "")
        files = [
            (source_note, destination / relative_note),
            (
                source_profile,
                destination / "02_source_memory" / "profiles" / f"{note_id}.yml",
            ),
            (
                metadata_path(origin, note_id),
                destination / "11_state" / "note_metadata" / f"{note_id}.yml",
            ),
        ]
        if bundle_relative:
            files.append((origin / bundle_relative, destination / bundle_relative))
        for source, dest in files:
            if not source.is_file():
                raise FileNotFoundError(source)
            copy_file(source, dest)
        manifest.append(
            {
                "source_id": row["source_id"],
                "note_id": note_id,
                "zotero_key": row.get("zotero_key", ""),
                "title": row.get("title", ""),
                "collection_keys": list(row.get("collection_keys", []) or []),
                "virtual_topic_ids": list(row.get("virtual_topic_ids", []) or []),
                "note_path": str(relative_note),
                "note_sha256": sha256_file(source_note),
                "semantic_note_sha256": semantic_note_hash(source_note),
                "profile_sha256": sha256_file(source_profile),
                "bundle_path": bundle_relative,
                "bundle_sha256": (
                    sha256_file(origin / bundle_relative) if bundle_relative else ""
                ),
            }
        )
    return manifest


def filtered_missing_sources(
    payload: Mapping[str, Any], source_ids: set[str]
) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in payload.get("sources", []) or []
        if set(str(value) for value in row.get("discussed_by_source_ids", []) or [])
        & source_ids
    ]
    rows.sort(key=lambda row: str(row.get("external_source_id") or ""))
    return {
        "missing_source_registry_schema_version": str(
            payload.get("missing_source_registry_schema_version") or "1"
        ),
        "sources": rows,
        "revision_hash": stable_key(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        ),
    }


def blind_manifest(
    baseline: set[str],
    source_by_id: Mapping[str, Mapping[str, Any]],
    positions: list[Mapping[str, Any]],
    typed_registry: Mapping[str, Any],
    historical_benchmark: Mapping[str, Any],
) -> dict[str, Any]:
    def crosses(left: str, right: str) -> bool:
        return bool(
            set(source_by_id[left].get("collection_keys", []) or [])
            != set(source_by_id[right].get("collection_keys", []) or [])
        )

    exact = []
    for row in positions:
        left = str(row.get("current_source_id") or "")
        right = str(row.get("matched_source_id") or "")
        if left in baseline and right in baseline and left != right and crosses(left, right):
            exact.append(
                {
                    "left_source_id": left,
                    "right_source_id": right,
                    "route": "resolved_literature_position",
                    "literature_position_id": row.get("literature_position_id", ""),
                }
            )
    exact.sort(key=lambda row: stable_key(json.dumps(row, sort_keys=True)))

    prior = []
    for decision in typed_registry.get("current_pair_decisions", []) or []:
        endpoints = [str(value) for value in decision.get("source_ids", []) or []]
        if (
            len(endpoints) == 2
            and all(value in baseline for value in endpoints)
            and bool(decision.get("active"))
            and str(decision.get("status") or "") == "accepted"
            and crosses(*endpoints)
        ):
            prior.append(
                {
                    "source_ids": sorted(endpoints),
                    "prior_pair_job_id": decision.get("pair_job_id", ""),
                    "prior_connections": list(decision.get("connections", []) or []),
                }
            )
    prior.sort(key=lambda row: stable_key(json.dumps(row["source_ids"])))

    historical = []
    for candidate in historical_benchmark.get("candidates", []) or []:
        left = str((candidate.get("left") or {}).get("source_id") or "")
        right = str((candidate.get("right") or {}).get("source_id") or "")
        if left in baseline and right in baseline:
            historical.append(dict(candidate))
    return {
        "schema_version": "1",
        "frozen_before_v029_graph_generation": True,
        "never_production_input": True,
        "resolved_cross_boundary_routes": exact,
        "prior_cross_boundary_relationships": prior[:60],
        "historical_mediation_relapse_pairs": historical,
    }


def prepare(origin: Path, target: Path) -> None:
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"target must be absent or empty: {target}")
    catalogue = json.loads(
        (origin / "02_source_memory" / "indexes" / "source_catalogue.yml").read_text(
            encoding="utf-8"
        )
    )
    source_by_id = {
        str(row.get("source_id") or ""): dict(row)
        for row in catalogue.get("sources", []) or []
        if row.get("source_id")
    }
    positions_payload = read_yaml(
        origin / "02_source_memory" / "indexes" / "literature_positions.yml", {}
    ) or {}
    positions = list(positions_payload.get("positions", []) or [])
    missing_sources = read_yaml(
        origin / "02_source_memory" / "indexes" / "missing_sources.yml", {}
    ) or {}
    baseline, delta, routes = select_sample(catalogue, positions)

    initialize_workspace(target)
    config = read_yaml(target / "auto-zettelkasten.yml", {}) or {}
    original_config = read_yaml(origin / "auto-zettelkasten.yml", {}) or {}
    for key in ("processing", "literature_mapping", "navigation"):
        if isinstance(original_config.get(key), Mapping):
            config[key] = dict(original_config[key])
    config.update(provider="deepseek", model="deepseek-v4-flash", scope="library")
    config["privacy"] = {"allow_cloud": False}
    write_yaml(target / "auto-zettelkasten.yml", config)

    baseline_rows = rows_for_sources(baseline, source_by_id)
    delta_rows = rows_for_sources(delta, source_by_id)
    baseline_files = copy_sources(origin, target, baseline_rows)
    delta_files = copy_sources(
        origin,
        target,
        delta_rows,
        relative_root=Path("evaluation/delta_payload"),
    )
    snapshot = read_yaml(origin / "01_custody/zotero/collection_snapshot.yml", {}) or {}
    baseline_snapshot = filtered_snapshot(snapshot, baseline_rows)
    combined_rows = [*baseline_rows, *delta_rows]
    combined_snapshot = filtered_snapshot(snapshot, combined_rows)
    write_yaml(target / "01_custody/zotero/collection_snapshot.yml", baseline_snapshot)
    write_yaml(
        target / "02_source_memory/indexes/literature_positions.yml",
        filtered_positions(positions, baseline),
    )
    write_yaml(
        target / "02_source_memory/indexes/missing_sources.yml",
        filtered_missing_sources(missing_sources, baseline),
    )
    write_yaml(
        target / "evaluation/delta_payload/collection_snapshot.yml", combined_snapshot
    )
    write_yaml(
        target / "evaluation/delta_payload/literature_positions.yml",
        filtered_positions(positions, baseline | delta),
    )
    write_yaml(
        target / "evaluation/delta_payload/missing_sources.yml",
        filtered_missing_sources(missing_sources, baseline | delta),
    )

    typed_registry = read_yaml(
        origin / "02_source_memory/indexes/typed_links.yml", {}
    ) or {}
    historical = read_yaml(
        Path(
            "/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/"
            "mediation-relapse-v026-relationship-evaluation-20260802/"
            "evaluation/curated-bridge-benchmark.yml"
        ),
        {},
    ) or {}
    evaluation = target / "evaluation"
    write_yaml(
        evaluation / "v029-sample-manifest.yml",
        {
            "schema_version": "1",
            "origin": str(origin),
            "baseline_source_count": len(baseline),
            "incremental_source_count": len(delta),
            "combined_source_count": len(baseline | delta),
            "routes": routes,
            "sources": baseline_files,
        },
    )
    write_yaml(
        evaluation / "v029-incremental-delta.yml",
        {
            "schema_version": "1",
            "active_before_incremental_test": False,
            "source_count": len(delta),
            "sources": delta_files,
        },
    )
    write_yaml(
        evaluation / "v029-blind-cross-boundary-manifest.yml",
        blind_manifest(baseline, source_by_id, positions, typed_registry, historical),
    )

    active_notes = list((target / "02_source_memory/notes").glob("*.md"))
    active_profiles = list((target / "02_source_memory/profiles").glob("*.yml"))
    if len(active_notes) != len(baseline) or len(active_profiles) != len(baseline):
        raise AssertionError("sample copy count mismatch")
    if any(
        (target / "02_source_memory/notes" / Path(row["note_path"]).name).exists()
        for row in delta_files
    ):
        raise AssertionError("incremental delta leaked into active sample")
    print(
        json.dumps(
            {
                "baseline_sources": len(baseline),
                "incremental_sources": len(delta),
                "combined_sources": len(baseline | delta),
                "topic_routes": len(routes["selected_topic_routes"]),
                "collection_routes": len(routes["selected_collection_routes"]),
                "unfiled_sources": routes["unfiled_source_count"],
            },
            indent=2,
        )
    )


def activate_delta(target: Path) -> None:
    delta_root = target / "evaluation/delta_payload"
    manifest = read_yaml(target / "evaluation/v029-incremental-delta.yml", {}) or {}
    for row in manifest.get("sources", []) or []:
        note_id = str(row["note_id"])
        relative_note = Path(str(row["note_path"]))
        for relative in (
            relative_note,
            Path("02_source_memory/profiles") / f"{note_id}.yml",
            Path("11_state/note_metadata") / f"{note_id}.yml",
        ):
            copy_file(delta_root / relative, target / relative)
    copy_file(
        delta_root / "collection_snapshot.yml",
        target / "01_custody/zotero/collection_snapshot.yml",
    )
    copy_file(
        delta_root / "literature_positions.yml",
        target / "02_source_memory/indexes/literature_positions.yml",
    )
    copy_file(
        delta_root / "missing_sources.yml",
        target / "02_source_memory/indexes/missing_sources.yml",
    )
    write_yaml(
        target / "evaluation/v029-incremental-activation.yml",
        {
            "schema_version": "1",
            "activated": True,
            "source_count": len(manifest.get("sources", []) or []),
        },
    )
    print(json.dumps({"activated_sources": len(manifest.get("sources", []) or [])}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--activate-delta", action="store_true")
    args = parser.parse_args()
    if args.activate_delta:
        activate_delta(args.target.expanduser().resolve())
    else:
        if args.origin is None:
            parser.error("--origin is required unless --activate-delta is used")
        prepare(args.origin.expanduser().resolve(), args.target.expanduser().resolve())


if __name__ == "__main__":
    main()
