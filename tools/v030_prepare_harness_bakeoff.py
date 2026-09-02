#!/usr/bin/env python3
"""Prepare deterministic, provider-neutral v0.30 harness-bakeoff slices."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.notes import semantic_note_hash
from auto_zettelkasten.workspace import validate_opaque_id
from v030_prepare_graph_benchmark import stable_key
from v030_prepare_mapping_sample import (
    copy_clean_source,
    validate_target as validate_mapping_target,
    write_phase_registries,
)


MANIFEST_PATH = Path("11_state/harness_bakeoff_manifest.yml")


def _selection_hash(selection: Mapping[str, Any]) -> str:
    payload = dict(selection)
    payload.pop("selection_sha256", None)
    return stable_key(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _safe_artifact_path(value: Any, *, root: str, field: str) -> str:
    relative = Path(str(value or ""))
    expected = Path(root)
    if (
        not str(value or "")
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.is_relative_to(expected)
    ):
        raise ValueError(f"invalid {field}: {value}")
    return str(relative)


def validate_selection(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(selection, Mapping):
        raise ValueError("frozen selection must be a mapping")
    if str(selection.get("schema_version") or "") != "1":
        raise ValueError("unsupported frozen selection schema")
    expected_hash = str(selection.get("selection_sha256") or "")
    if not expected_hash or _selection_hash(selection) != expected_hash:
        raise ValueError("frozen selection hash mismatch")
    rows = [dict(row) for row in selection.get("sources", []) or []]
    quotas = [dict(row) for row in selection.get("quotas", []) or []]
    if not rows or not quotas:
        raise ValueError("frozen selection has no sources or quotas")
    source_ids = [str(row.get("source_id") or "") for row in rows]
    if "" in source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("frozen selection source identities are invalid")
    if any(str(row.get("canonical_source_id") or "") != row["source_id"] for row in rows):
        raise ValueError("frozen selection contains a canonical alias")
    if any(str(row.get("phase") or "") not in {"baseline", "delta"} for row in rows):
        raise ValueError("frozen selection contains an invalid phase")
    note_ids = [str(row.get("note_id") or "") for row in rows]
    note_paths = [str(row.get("note_path") or "") for row in rows]
    if len(note_ids) != len(set(note_ids)) or len(note_paths) != len(set(note_paths)):
        raise ValueError("frozen selection contains a note identity collision")

    quota_by_id = {str(row.get("stratum_id") or ""): row for row in quotas}
    if "" in quota_by_id or len(quota_by_id) != len(quotas):
        raise ValueError("frozen selection stratum quotas are invalid")
    if set(str(row.get("primary_stratum_id") or "") for row in rows) != set(quota_by_id):
        raise ValueError("frozen selection sources and quotas disagree")
    for stratum_id, quota in quota_by_id.items():
        owned = [row for row in rows if row["primary_stratum_id"] == stratum_id]
        phases = Counter(str(row["phase"]) for row in owned)
        expected = (
            int(quota["combined_count"]),
            int(quota["baseline_count"]),
            int(quota["delta_count"]),
        )
        if (len(owned), phases["baseline"], phases["delta"]) != expected:
            raise ValueError(f"frozen selection quota mismatch: {stratum_id}")
    if sum(int(row["combined_count"]) for row in quotas) != len(rows):
        raise ValueError("frozen selection total quota mismatch")

    for row in rows:
        validate_opaque_id(str(row.get("note_id") or ""), field="note_id")
        _safe_artifact_path(row.get("note_path"), root="02_source_memory/notes", field="note_path")
        _safe_artifact_path(row.get("profile_path"), root="02_source_memory/profiles", field="profile_path")
        if row.get("bundle_path"):
            _safe_artifact_path(row["bundle_path"], root="02_source_memory/bundles", field="bundle_path")
    return rows


def selected_rows(
    selection: Mapping[str, Any], stratum_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = validate_selection(selection)
    requested = sorted({value.strip() for value in stratum_ids if value.strip()})
    if not requested:
        raise ValueError("at least one primary stratum ID is required")
    known = {str(row["stratum_id"]) for row in selection["quotas"]}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise ValueError(f"unknown primary strata: {', '.join(unknown)}")
    chosen = [row for row in rows if str(row["primary_stratum_id"]) in requested]
    phases = Counter(str(row["phase"]) for row in chosen)
    if phases["delta"] == 0 or any(
        not any(row["phase"] == "delta" and row["primary_stratum_id"] == stratum_id for row in chosen)
        for stratum_id in requested
    ):
        raise ValueError("selected slice omits required delta sources")
    return sorted(chosen, key=lambda row: str(row["source_id"])), requested


def selected_complete_packet_rows(
    selection: Mapping[str, Any], *, target_count: int, tolerance: int, seed: str
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    if target_count <= 0 or tolerance < 0 or not seed.strip():
        raise ValueError("packet sample target, tolerance, and seed are invalid")
    rows = validate_selection(selection)
    complete_keys = {
        str(value)
        for value in (selection.get("cohesion", {}) or {}).get(
            "globally_complete_packet_keys", []
        )
        if str(value)
    }
    if not complete_keys:
        raise ValueError("frozen selection has no globally complete packets")
    packets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        packet_key = str(row.get("deepest_leaf_packet_key") or "")
        if packet_key in complete_keys:
            packets[packet_key].append(row)
    if set(packets) != complete_keys:
        raise ValueError("globally complete packet inventory is inconsistent")

    packet_strata: dict[str, str] = {}
    by_stratum: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for packet_key, packet_rows in packets.items():
        owners = {str(row["primary_stratum_id"]) for row in packet_rows}
        if len(owners) != 1:
            raise ValueError(f"complete packet crosses primary strata: {packet_key}")
        stratum_id = owners.pop()
        packet_strata[packet_key] = stratum_id
        by_stratum[stratum_id].append((packet_key, len(packet_rows)))

    quotas = [dict(row) for row in selection["quotas"]]
    stratum_ids = [str(row["stratum_id"]) for row in quotas]
    missing = [stratum_id for stratum_id in stratum_ids if not by_stratum[stratum_id]]
    if missing:
        raise ValueError(f"globally complete packets omit strata: {', '.join(missing)}")

    total_rows = len(rows)
    states: dict[int, tuple[int, tuple[str, ...], tuple[str, ...]]] = {0: (0, (), ())}
    for quota in quotas:
        stratum_id = str(quota["stratum_id"])
        options = []
        owned = sorted(by_stratum[stratum_id])
        for size in range(1, len(owned) + 1):
            for subset in combinations(owned, size):
                count = sum(value for _, value in subset)
                keys = tuple(key for key, _ in subset)
                deviation = (
                    count * total_rows - target_count * int(quota["combined_count"])
                ) ** 2
                options.append(
                    (count, deviation, stable_key(seed, stratum_id, ",".join(keys)), keys)
                )
        next_states: dict[int, tuple[int, tuple[str, ...], tuple[str, ...]]] = {}
        for current_count, (score, tie_breaks, keys) in states.items():
            for count, deviation, tie_break, option_keys in options:
                new_count = current_count + count
                candidate = (score + deviation, tie_breaks + (tie_break,), keys + option_keys)
                if new_count not in next_states or candidate[:2] < next_states[new_count][:2]:
                    next_states[new_count] = candidate
        states = next_states

    allowed = [
        (abs(count - target_count), score, tie_breaks, count, keys)
        for count, (score, tie_breaks, keys) in states.items()
        if abs(count - target_count) <= tolerance
    ]
    if not allowed:
        raise ValueError("no complete-packet sample satisfies the requested size tolerance")
    _, _, _, actual_count, chosen_keys = min(allowed)
    chosen = set(chosen_keys)
    chosen_rows = sorted(
        (row for key in chosen for row in packets[key]),
        key=lambda row: str(row["source_id"]),
    )
    per_stratum = Counter(packet_strata[key] for key in chosen for _ in packets[key])
    sampling = {
        "method": "seeded_whole_globally_complete_leaf_packets_v1",
        "scope": "frozen_notes_relationships_and_clusters",
        "provider_calls_authorized": False,
        "seed": seed,
        "target_count": target_count,
        "tolerance": tolerance,
        "actual_count": actual_count,
        "selected_packet_count": len(chosen),
        "selected_packet_keys": sorted(chosen),
        "per_stratum_counts": {key: per_stratum[key] for key in stratum_ids},
    }
    return chosen_rows, stratum_ids, sampling


def _snapshot_source_ids(payload: Mapping[str, Any]) -> set[str]:
    return {
        f"source-zotero-{str(row.get('key') or '').casefold()}"
        for row in payload.get("items", []) or []
    }


def validate_activated_origin(
    origin: Path, selection: Mapping[str, Any], rows: list[Mapping[str, Any]]
) -> None:
    selection_hash = str(selection["selection_sha256"])
    receipt_path = origin / "evaluation/v030-delta-activation.yml"
    materialization_path = origin / "evaluation/v030-materialization.yml"
    receipt = read_yaml(receipt_path, {}) or {}
    materialization = read_yaml(materialization_path, {}) or {}
    if (
        receipt.get("activated") is not True
        or materialization.get("activated") is not True
        or str(receipt.get("selection_sha256") or "") != selection_hash
        or str(materialization.get("selection_sha256") or "") != selection_hash
    ):
        raise ValueError("origin is not the activated frozen selection")
    all_rows = list(selection.get("sources", []) or [])
    if int(receipt.get("delta_count") or -1) != sum(row["phase"] == "delta" for row in all_rows):
        raise ValueError("activation receipt delta count mismatch")
    if _snapshot_source_ids(
        read_yaml(origin / "01_custody/zotero/collection_snapshot.yml", {}) or {}
    ) != {str(row["source_id"]) for row in all_rows}:
        raise ValueError("activated origin snapshot is incomplete")
    for row in rows:
        note = origin / str(row["note_path"])
        profile = origin / str(row["profile_path"])
        bundle = origin / str(row.get("bundle_path") or "") if row.get("bundle_path") else None
        if not note.is_file() or not profile.is_file() or (bundle and not bundle.is_file()):
            raise ValueError(f"activated origin source is incomplete: {row['source_id']}")
        if (
            semantic_note_hash(note.read_text(encoding="utf-8"))
            != str(row["semantic_note_sha256"])
            or sha256_file(profile) != str(row["profile_sha256"])
            or (bundle and sha256_file(bundle) != str(row["bundle_sha256"]))
        ):
            raise ValueError(f"activated origin source hash mismatch: {row['source_id']}")


def build_manifest(
    origin: Path,
    selection_path: Path,
    selection: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    stratum_ids: list[str],
    sampling: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_rows = []
    for row in rows:
        note = origin / str(row["note_path"])
        source_row = {
                key: row.get(key, "")
                for key in (
                    "source_id",
                    "note_id",
                    "phase",
                    "primary_stratum_id",
                    "note_path",
                    "semantic_note_sha256",
                    "profile_path",
                    "profile_sha256",
                    "bundle_path",
                    "bundle_sha256",
                )
            } | {"origin_note_sha256": sha256_file(note)}
        if sampling is not None:
            source_row["deepest_leaf_packet_key"] = row["deepest_leaf_packet_key"]
        source_rows.append(source_row)
    payload = {
        "schema_version": "1",
        "status": "frozen_provider_neutral_slice",
        "never_production_prompt_input": True,
        "selection_sha256": selection["selection_sha256"],
        "primary_stratum_ids": stratum_ids,
        "source_count": len(rows),
        "baseline_count": sum(row["phase"] == "baseline" for row in rows),
        "delta_count": sum(row["phase"] == "delta" for row in rows),
        "input_hashes": {
            "selection_file_sha256": sha256_file(selection_path),
            "activation_receipt_sha256": sha256_file(origin / "evaluation/v030-delta-activation.yml"),
            "materialization_receipt_sha256": sha256_file(origin / "evaluation/v030-materialization.yml"),
            "collection_snapshot_sha256": sha256_file(origin / "01_custody/zotero/collection_snapshot.yml"),
            "literature_positions_sha256": sha256_file(origin / "02_source_memory/indexes/literature_positions.yml"),
            "missing_sources_sha256": sha256_file(origin / "02_source_memory/indexes/missing_sources.yml"),
        },
        "sources": source_rows,
    }
    if sampling is not None:
        payload["sampling"] = dict(sampling)
    payload["manifest_sha256"] = stable_key(
        json.dumps(payload, sort_keys=True, ensure_ascii=False)
    )
    return payload


def _configure_provider_neutral_workspace(target: Path) -> None:
    initialize_workspace(target)
    config_path = target / "auto-zettelkasten.yml"
    config = read_yaml(config_path, {}) or {}
    for key in (
        "provider",
        "model",
        "literature_model",
        "reasoning_effort",
        "max_provider_spend_usd",
    ):
        config.pop(key, None)
    config["privacy"] = {"allow_cloud": False}
    config["literature_mapping"] = {
        **dict(config.get("literature_mapping", {})),
        "synthesis_enabled": False,
        "external_discovery": "disabled",
        "max_profile_calls": 0,
        "max_synthesis_calls": 0,
    }
    write_yaml(config_path, config)
    workspace_manifest = read_yaml(target / "11_state/workspace_manifest.yml", {}) or {}
    workspace_manifest["created_at"] = "1970-01-01T00:00:00+00:00"
    workspace_manifest["workspace"] = "."
    write_yaml(target / "11_state/workspace_manifest.yml", workspace_manifest)


def _expected_files(rows: list[Mapping[str, Any]]) -> set[str]:
    expected = {
        "auto-zettelkasten.yml",
        "01_custody/zotero/collection_snapshot.yml",
        "02_source_memory/indexes/literature_positions.yml",
        "02_source_memory/indexes/missing_sources.yml",
        "11_state/workspace_manifest.yml",
        str(MANIFEST_PATH),
    }
    for row in rows:
        expected.update(
            {
                str(row["note_path"]),
                str(row["profile_path"]),
                f"11_state/note_metadata/{row['note_id']}.yml",
            }
        )
        if row.get("bundle_path"):
            expected.add(str(row["bundle_path"]))
    return expected


def validate_target(
    origin: Path,
    target: Path,
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    validate_mapping_target(origin, target, {"sources": rows}, activated=True)
    stored = read_yaml(target / MANIFEST_PATH, {}) or {}
    if dict(stored) != dict(manifest):
        raise ValueError("stored harness-bakeoff manifest differs from frozen inputs")
    config = read_yaml(target / "auto-zettelkasten.yml", {}) or {}
    if (
        set(config) & {"provider", "model", "literature_model", "reasoning_effort", "max_provider_spend_usd"}
        or (config.get("privacy", {}) or {}).get("allow_cloud") is not False
    ):
        raise ValueError("slice workspace is not provider-neutral and cloud-disabled")
    actual = {
        str(path.relative_to(target))
        for path in target.rglob("*")
        if path.is_file()
    }
    expected = _expected_files(rows)
    if actual != expected:
        raise ValueError(
            f"slice file inventory mismatch: missing={sorted(expected - actual)} "
            f"unexpected={sorted(actual - expected)}"
        )


def _materialize(
    origin: Path,
    target: Path,
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"target must be absent or empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.name}.", dir=target.parent) as temporary:
        staging = Path(temporary)
        _configure_provider_neutral_workspace(staging)
        for row in rows:
            copy_clean_source(origin, staging, row)
        write_phase_registries(origin, staging, rows)
        write_yaml(staging / MANIFEST_PATH, manifest)
        validate_target(origin, staging, rows, manifest)
        if target.exists():
            target.rmdir()
        staging.replace(target)
    validate_target(origin, target, rows, manifest)


def prepare(
    origin: Path, selection_path: Path, target: Path, stratum_ids: list[str]
) -> dict[str, Any]:
    selection = read_yaml(selection_path, {}) or {}
    rows, requested = selected_rows(selection, stratum_ids)
    validate_activated_origin(origin, selection, rows)
    manifest = build_manifest(origin, selection_path, selection, rows, requested)
    _materialize(origin, target, rows, manifest)
    return manifest


def validate(
    origin: Path, selection_path: Path, target: Path, stratum_ids: list[str]
) -> dict[str, Any]:
    selection = read_yaml(selection_path, {}) or {}
    rows, requested = selected_rows(selection, stratum_ids)
    validate_activated_origin(origin, selection, rows)
    manifest = build_manifest(origin, selection_path, selection, rows, requested)
    validate_target(origin, target, rows, manifest)
    return manifest


def prepare_packet_sample(
    origin: Path,
    selection_path: Path,
    target: Path,
    *,
    target_count: int,
    tolerance: int,
    seed: str,
) -> dict[str, Any]:
    selection = read_yaml(selection_path, {}) or {}
    rows, strata, sampling = selected_complete_packet_rows(
        selection, target_count=target_count, tolerance=tolerance, seed=seed
    )
    validate_activated_origin(origin, selection, rows)
    manifest = build_manifest(
        origin, selection_path, selection, rows, strata, sampling=sampling
    )
    _materialize(origin, target, rows, manifest)
    return manifest


def validate_packet_sample(
    origin: Path,
    selection_path: Path,
    target: Path,
    *,
    target_count: int,
    tolerance: int,
    seed: str,
) -> dict[str, Any]:
    selection = read_yaml(selection_path, {}) or {}
    rows, strata, sampling = selected_complete_packet_rows(
        selection, target_count=target_count, tolerance=tolerance, seed=seed
    )
    validate_activated_origin(origin, selection, rows)
    manifest = build_manifest(
        origin, selection_path, selection, rows, strata, sampling=sampling
    )
    validate_target(origin, target, rows, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "validate", "prepare-packet-sample", "validate-packet-sample"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--origin", type=Path, required=True)
        subparser.add_argument("--selection", type=Path, required=True)
        subparser.add_argument("--target", type=Path, required=True)
        if "packet-sample" in command:
            subparser.add_argument("--target-count", type=int, default=500)
            subparser.add_argument("--tolerance", type=int, default=25)
            subparser.add_argument("--seed", required=True)
        else:
            subparser.add_argument("--stratum-id", action="append", required=True)
    args = parser.parse_args()
    if "packet-sample" in args.command:
        operation = prepare_packet_sample if args.command == "prepare-packet-sample" else validate_packet_sample
        manifest = operation(
            args.origin.resolve(),
            args.selection.resolve(),
            args.target.resolve(),
            target_count=args.target_count,
            tolerance=args.tolerance,
            seed=args.seed,
        )
    else:
        operation = prepare if args.command == "prepare" else validate
        manifest = operation(
            args.origin.resolve(),
            args.selection.resolve(),
            args.target.resolve(),
            args.stratum_id,
        )
    print(
        json.dumps(
            {
                "status": "prepared" if args.command.startswith("prepare") else "valid",
                "source_count": manifest["source_count"],
                "primary_stratum_ids": manifest["primary_stratum_ids"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
