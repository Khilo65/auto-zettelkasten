#!/usr/bin/env python3
"""Build and validate the frozen v0.30 900+100 mapping benchmark."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.files import (
    atomic_write_text,
    read_yaml,
    sha256_file,
    write_yaml,
)
from auto_zettelkasten.notes import (
    NON_SOURCE_FRONTMATTER_FIELDS,
    _public_note_text,
    _write_note_metadata,
    canonical_source_note_text,
    internal_note_text,
    read_note,
    semantic_note_hash,
    source_note_preservation_hash,
    validate_note,
)
from v029_prepare_frozen_sample import filtered_snapshot
from v030_prepare_graph_benchmark import (
    bridge_candidates,
    descendant_keys,
    load_catalogue,
    stable_key,
    structural_adjacency,
    validate_spec as validate_strata_spec,
)


ANALYTICAL_STATUSES = {"analytical_atomic_note", "verified_atomic_note"}
CONTEXT_STATUSES = {
    "abstract_only_atomic_note",
    "metadata_only_source_note",
    "partial_document_atomic_note",
}
SPEC_KEYS = {
    "schema_version",
    "benchmark_id",
    "seed",
    "strata_spec",
    "anchor_manifest",
    "independent_delta_controls",
    "combined_analytical_count",
    "combined_context_count",
    "strata",
    "source_canary",
}
QUOTA_KEYS = {
    "stratum_id",
    "combined_count",
    "baseline_count",
    "delta_count",
    "combined_context_count",
}
TYPE_MINIMUMS = {"books": 180, "reports": 70, "web": 60, "other": 20}
YEAR_MINIMUMS = {"pre_1990": 50, "1990_2009": 250, "2010_2019": 250, "2020_plus": 150}


def source_note_path(origin: Path, note_id: str) -> Path:
    metadata = read_yaml(origin / "11_state/note_metadata" / f"{note_id}.yml", {}) or {}
    relative = str(metadata.get("note_path") or "")
    if not relative:
        raise ValueError(f"missing note path: {note_id}")
    return origin / relative


def profile_path(origin: Path, note_id: str) -> Path:
    return origin / "02_source_memory/profiles" / f"{note_id}.yml"


def bundle_path(origin: Path, note_id: str) -> Path | None:
    payload = read_yaml(profile_path(origin, note_id), {}) or {}
    profile = payload.get("profile", payload)
    context = profile.get("context", {}) if isinstance(profile, Mapping) else {}
    relative = str(context.get("source_analysis_bundle_path") or "")
    return origin / relative if relative else None


def item_category(item_type: str) -> str:
    if item_type == "journalArticle":
        return "journal"
    if item_type in {"book", "bookSection"}:
        return "books"
    if item_type in {"report", "document", "statute", "hearing", "bill"}:
        return "reports"
    if item_type in {"webpage", "newspaperArticle", "blogPost", "magazineArticle", "forumPost"}:
        return "web"
    if item_type in {
        "preprint",
        "thesis",
        "conferencePaper",
        "dataset",
        "presentation",
        "encyclopediaArticle",
    }:
        return "other"
    return "residual"


def year_bucket(value: Any) -> str:
    text = str(value or "")
    try:
        year = int(text[:4])
    except ValueError:
        return "n_d"
    if year < 1990:
        return "pre_1990"
    if year <= 2009:
        return "1990_2009"
    if year <= 2019:
        return "2010_2019"
    return "2020_plus"


def validate_mapping_spec(spec: Mapping[str, Any], stratum_ids: list[str]) -> list[dict[str, Any]]:
    if set(spec) != SPEC_KEYS or str(spec.get("schema_version")) != "1":
        raise ValueError("mapping sample spec has unexpected fields or schema version")
    quotas = [dict(row) for row in spec.get("strata", []) or []]
    if len(quotas) != 20 or any(set(row) != QUOTA_KEYS for row in quotas):
        raise ValueError("mapping sample requires twenty exact stratum quotas")
    if [str(row["stratum_id"]) for row in quotas] != stratum_ids:
        raise ValueError("mapping sample stratum order differs from strata spec")
    if sum(int(row["combined_count"]) for row in quotas) != 1000:
        raise ValueError("combined quota must equal 1000")
    if sum(int(row["baseline_count"]) for row in quotas) != 900:
        raise ValueError("baseline quota must equal 900")
    if sum(int(row["delta_count"]) for row in quotas) != 100:
        raise ValueError("delta quota must equal 100")
    for row in quotas:
        if int(row["combined_count"]) != int(row["baseline_count"]) + int(row["delta_count"]):
            raise ValueError(f"invalid phase quota: {row['stratum_id']}")
        if int(row["delta_count"]) != 5:
            raise ValueError(f"delta quota must be five: {row['stratum_id']}")
    canary = spec.get("source_canary", {}) or {}
    if sum(int(value) for value in canary.values()) != 80:
        raise ValueError("source canary quota must equal 80")
    return quotas


def eligible_sources(
    origin: Path, source_by_id: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    result = set()
    for source_id, row in source_by_id.items():
        status = str(row.get("note_status") or "")
        note_id = str(row.get("note_id") or "")
        if (
            status not in ANALYTICAL_STATUSES | CONTEXT_STATUSES
            or str(row.get("canonical_source_id") or source_id) != source_id
            or not source_note_path(origin, note_id).is_file()
            or not profile_path(origin, note_id).is_file()
        ):
            continue
        result.add(source_id)
    return result


def diversity_counts(
    source_ids: Iterable[str], info: Mapping[str, Mapping[str, Any]]
) -> tuple[Counter[str], Counter[str]]:
    source_ids = list(source_ids)
    return (
        Counter(str(info[source_id]["item_category"]) for source_id in source_ids),
        Counter(str(info[source_id]["year_bucket"]) for source_id in source_ids),
    )


def collection_depths(collections: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    parents = {str(row.get("key") or ""): str(row.get("parent_key") or "") for row in collections}
    depths: dict[str, int] = {}

    def depth(key: str) -> int:
        if key not in depths:
            parent = parents.get(key, "")
            depths[key] = 0 if not parent or parent == key else depth(parent) + 1
        return depths[key]

    for key in parents:
        depth(key)
    return depths


def fill_analytical_cell(
    *,
    seed: str,
    stratum_id: str,
    target: int,
    baseline: dict[str, tuple[str, str]],
    selected: set[str],
    pool: set[str],
    packet_key: Mapping[str, str],
    global_packet_sizes: Mapping[str, int],
    info: Mapping[str, Mapping[str, Any]],
) -> None:
    current = sum(
        owner == stratum_id and info[source_id]["scope_class"] == "analytical"
        for source_id, (owner, _) in baseline.items()
    )
    remaining = target - current
    if remaining < 0:
        raise ValueError(f"analytical quota exceeded: {stratum_id}")
    groups: dict[str, list[str]] = defaultdict(list)
    for source_id in pool - selected:
        if info[source_id]["scope_class"] == "analytical":
            groups[packet_key[source_id]].append(source_id)
    groups = {
        key: sorted(values, key=lambda value: stable_key(seed, stratum_id, key, value))
        for key, values in groups.items()
        if values
    }
    whole = {
        key: values
        for key, values in groups.items()
        if 3 <= global_packet_sizes.get(key, 0) <= 25
    }
    states: dict[int, tuple[str, ...]] = {0: ()}
    for key in sorted(whole, key=lambda value: stable_key(seed, stratum_id, "packet", value)):
        size = len(whole[key])
        for count, keys in sorted(states.copy().items(), reverse=True):
            if count + size <= remaining and count + size not in states:
                states[count + size] = keys + (key,)
    choice = None
    for count in sorted(states, reverse=True):
        residual = remaining - count
        used_keys = set(states[count])
        boundary = ""
        if residual:
            candidates = [key for key, values in groups.items() if key not in used_keys and len(values) >= residual]
            if not candidates:
                continue
            boundary = min(candidates, key=lambda value: stable_key(seed, stratum_id, "boundary", value))
        choice = states[count], boundary, residual
        break
    if choice is None:
        raise ValueError(f"one-boundary analytical fill infeasible: {stratum_id}")
    whole_keys, boundary, residual = choice
    for key in whole_keys:
        for source_id in whole[key]:
            baseline[source_id] = (stratum_id, f"complete_packet:{key}")
            selected.add(source_id)
    for source_id in groups.get(boundary, [])[:residual]:
        baseline[source_id] = (stratum_id, f"boundary_packet:{boundary}")
        selected.add(source_id)


def select_sample(origin: Path, spec_path: Path) -> dict[str, Any]:
    spec = read_yaml(spec_path, {}) or {}
    strata_path = spec_path.parent / str(spec.get("strata_spec") or "")
    anchors_path = spec_path.parent / str(spec.get("anchor_manifest") or "")
    controls_path = spec_path.parent / str(spec.get("independent_delta_controls") or "")
    strata_spec = read_yaml(strata_path, {}) or {}
    strata = validate_strata_spec(strata_spec)
    quotas = validate_mapping_spec(spec, [str(row["stratum_id"]) for row in strata])
    anchors_payload = read_yaml(anchors_path, {}) or {}
    anchors = list(anchors_payload.get("anchors", []) or [])
    if len(anchors) != 120:
        raise ValueError("anchor manifest must contain 120 rows")
    controls = read_yaml(controls_path, {}) or {}

    catalogue_path = origin / "02_source_memory/indexes/source_catalogue.yml"
    positions_path = origin / "02_source_memory/indexes/literature_positions.yml"
    snapshot_path = origin / "01_custody/zotero/collection_snapshot.yml"
    bridge_path = origin / "evaluation/pregraph-full-library-bridge-benchmark.yml"
    structural_path = origin / "evaluation/pregraph-structural-benchmarks.yml"
    missing_path = origin / "02_source_memory/indexes/missing_sources.yml"
    catalogue = load_catalogue(catalogue_path)
    positions = read_yaml(positions_path, {}) or {}
    snapshot = read_yaml(snapshot_path, {}) or {}
    bridge_payload = read_yaml(bridge_path, {}) or {}
    structural_payload = read_yaml(structural_path, {}) or {}
    source_by_id = {
        str(row.get("source_id") or ""): dict(row)
        for row in catalogue.get("sources", []) or []
        if row.get("source_id")
    }
    eligible = eligible_sources(origin, source_by_id)
    exact_graph = structural_adjacency(positions.get("positions", []) or [], eligible)
    item_by_key = {
        str(row.get("key") or "").casefold(): dict(row)
        for row in snapshot.get("items", []) or []
    }
    info: dict[str, dict[str, Any]] = {}
    for source_id in eligible:
        row = source_by_id[source_id]
        item = item_by_key.get(source_id.removeprefix("source-zotero-").casefold(), {})
        item_type = str(item.get("item_type") or "")
        status = str(row.get("note_status") or "")
        info[source_id] = {
            "scope_class": "analytical" if status in ANALYTICAL_STATUSES else "context",
            "item_type": item_type,
            "item_category": item_category(item_type),
            "year_bucket": year_bucket(row.get("year") or (item.get("identity") or {}).get("year")),
        }

    quota_by_id = {str(row["stratum_id"]): row for row in quotas}
    route_keys: dict[str, set[str]] = {}
    candidate_pools: dict[str, set[str]] = {}
    for row in strata:
        stratum_id = str(row["stratum_id"])
        keys = descendant_keys(row["collection_keys"], catalogue.get("collections", []) or [])
        route_keys[stratum_id] = keys
        candidate_pools[stratum_id] = {
            source_id
            for source_id in eligible
            if set(source_by_id[source_id].get("collection_keys", []) or []) & keys
        }

    anchor_owner = {str(row["source_id"]): str(row["stratum_id"]) for row in anchors}
    if len(anchor_owner) != 120:
        raise ValueError("anchor source IDs must be unique")
    for source_id, stratum_id in anchor_owner.items():
        if source_id not in candidate_pools[stratum_id] or info[source_id]["scope_class"] != "analytical":
            raise ValueError(f"anchor outside analytical stratum pool: {source_id}")

    depths = collection_depths(catalogue.get("collections", []) or [])
    stratum_order = {str(row["stratum_id"]): index for index, row in enumerate(strata)}
    owner_counts = Counter(anchor_owner.values())
    owner_by_source = dict(anchor_owner)
    packet_key_by_source: dict[str, str] = {}
    packet_members: dict[str, set[str]] = defaultdict(set)
    for source_id in eligible:
        actual_keys = {
            str(key) for key in source_by_id[source_id].get("collection_keys", []) or []
            if any(str(key) in keys for keys in route_keys.values())
        }
        if not actual_keys:
            continue
        key = min(actual_keys, key=lambda value: (-depths.get(value, 0), value))
        packet_key_by_source[source_id] = key
        packet_members[key].add(source_id)
    for key in sorted(packet_members, key=lambda value: stable_key(str(spec["seed"]), "owner-packet", value)):
        members = packet_members[key]
        candidates = [stratum_id for stratum_id, keys in route_keys.items() if key in keys]
        anchor_strata = {anchor_owner[source_id] for source_id in members if source_id in anchor_owner}
        packet_owner = min(
            anchor_strata or set(candidates),
            key=lambda stratum_id: (
                owner_counts[stratum_id] / int(quota_by_id[stratum_id]["combined_count"]),
                stratum_order[stratum_id],
            ),
        )
        for source_id in members:
            owner = anchor_owner.get(source_id, packet_owner)
            owner_by_source[source_id] = owner
            owner_counts[owner] += source_id not in anchor_owner
    global_analytical_packet_sizes = {
        key: sum(info[source_id]["scope_class"] == "analytical" for source_id in members)
        for key, members in packet_members.items()
    }
    pools = {
        stratum_id: {source_id for source_id, owner in owner_by_source.items() if owner == stratum_id}
        for stratum_id in route_keys
    }
    packets: dict[str, dict[str, set[str]]] = {}
    for stratum_id, pool in pools.items():
        groups: dict[str, set[str]] = defaultdict(set)
        for source_id in pool:
            groups[packet_key_by_source[source_id]].add(source_id)
        packets[stratum_id] = dict(groups)

    required_baseline: dict[str, tuple[str, str]] = {
        source_id: (stratum_id, "benchmark_anchor")
        for source_id, stratum_id in anchor_owner.items()
    }
    identity_graph: dict[str, set[str]] = defaultdict(set)
    for row in structural_payload.get("explicit_zotero_relations", []) or []:
        left = str(row.get("source_id") or "")
        target_key = str(row.get("target") or "").rstrip("/").rsplit("/", 1)[-1]
        right = f"source-zotero-{target_key.casefold()}" if target_key else ""
        if left in eligible and right in eligible:
            identity_graph[left].add(right)
            identity_graph[right].add(left)
    pending = list(anchor_owner)
    seen = set(pending)
    while pending:
        source_id = pending.pop()
        for companion in identity_graph[source_id] - seen:
            seen.add(companion)
            pending.append(companion)
            required_baseline.setdefault(
                companion, (owner_by_source[companion], "identity_lineage_companion")
            )
    bridge_by_source = bridge_candidates(bridge_payload.get("pairs", []) or [])
    for anchor in anchors:
        if str(anchor.get("cohort") or "") != "bridge":
            continue
        source_id = str(anchor["source_id"])
        candidates = sorted(
            bridge_by_source.get(source_id, []),
            key=lambda row: (str(row["pair_id"]), str(row["candidate_source_id"])),
        )
        if not candidates:
            raise ValueError(f"bridge anchor lacks independent control: {source_id}")
        counterpart = str(candidates[0]["candidate_source_id"])
        if counterpart in eligible and counterpart not in required_baseline:
            owner = owner_by_source.get(counterpart, str(anchor["stratum_id"]))
            owner_by_source[counterpart] = owner
            packet_key_by_source.setdefault(counterpart, "external-bridge-control")
            pools[owner].add(counterpart)
            required_baseline[counterpart] = (owner, "bridge_control")

    manual_controls = {
        str(row["stratum_id"]): dict(row)
        for row in controls.get("controls", []) or []
    }
    delta: dict[str, tuple[str, str]] = {}
    delta_control_rows: list[dict[str, Any]] = []
    used = set(required_baseline)
    scarce_order = sorted(
        pools,
        key=lambda stratum_id: (
            len(pools[stratum_id]) / int(quota_by_id[stratum_id]["combined_count"]),
            list(pools).index(stratum_id),
        ),
    )

    for stratum_id in scarce_order:
        pool = pools[stratum_id]
        control = manual_controls.get(stratum_id)
        if control:
            linked = [(
                str(control["delta_source_id"]),
                str(control["baseline_source_id"]),
                "independent_source_first_audit",
            )]
        else:
            linked = []
            for delta_source in sorted(pool - used):
                if info[delta_source]["scope_class"] != "analytical":
                    continue
                for baseline_source in exact_graph[delta_source] - used - {delta_source}:
                    linked.append((delta_source, baseline_source, "resolved_literature_position"))
        linked.sort(key=lambda row: stable_key(str(spec["seed"]), stratum_id, "delta-control", *row[:2]))
        bundle = None
        for delta_source, baseline_source, basis in linked:
            if (
                delta_source not in pool
                or delta_source in used
                or baseline_source not in eligible
                or info[delta_source]["scope_class"] != "analytical"
            ):
                continue
            first_key = packet_key_by_source[delta_source]
            for keys in [(first_key,), *[(first_key, key) for key in sorted(packets[stratum_id]) if key != first_key]]:
                members = set().union(*(packets[stratum_id][key] for key in keys)) - used - {baseline_source}
                analytical = sorted(
                    (source_id for source_id in members if info[source_id]["scope_class"] == "analytical" and source_id != delta_source),
                    key=lambda source_id: stable_key(str(spec["seed"]), stratum_id, "delta-analytical", source_id),
                )
                context = sorted(
                    (source_id for source_id in members if info[source_id]["scope_class"] == "context"),
                    key=lambda source_id: stable_key(str(spec["seed"]), stratum_id, "delta-context", source_id),
                )
                if len(analytical) >= 3 and context:
                    bundle = (delta_source, baseline_source, basis, keys, analytical[:3], context[0])
                    break
            if bundle:
                break
        if not bundle:
            raise ValueError(f"coherent linked delta bundle unavailable: {stratum_id}")
        delta_source, baseline_source, basis, keys, analytical, context_source = bundle
        baseline_owner = owner_by_source.get(baseline_source, stratum_id)
        required_baseline.setdefault(baseline_source, (baseline_owner, "delta_control"))
        owner_by_source.setdefault(baseline_source, baseline_owner)
        packet_key_by_source.setdefault(baseline_source, "external-delta-control")
        pools[baseline_owner].add(baseline_source)
        for source_id, reason in [
            (delta_source, "delta_linked_analytical"),
            *[(source_id, "delta_coherent_packet") for source_id in analytical],
            (context_source, "delta_context"),
        ]:
            delta[source_id] = (stratum_id, reason)
            used.add(source_id)
        used.add(baseline_source)
        delta_control_rows.append(
            {
                "stratum_id": stratum_id,
                "delta_source_id": delta_source,
                "baseline_source_id": baseline_source,
                "basis": basis,
                "leaf_packet_keys": list(keys),
            }
        )

    old_count = sum(
        info[source_id]["year_bucket"] == "pre_1990"
        for source_id in set(required_baseline) | set(delta)
    )
    old_candidates = sorted(
        (
            source_id for source_id in eligible - used
            if source_id in owner_by_source
            and info[source_id]["scope_class"] == "analytical"
            and info[source_id]["year_bucket"] == "pre_1990"
        ),
        key=lambda source_id: stable_key(str(spec["seed"]), "pre-1990", source_id),
    )
    if old_count + len(old_candidates) < 50:
        raise ValueError("pre-1990 diversity quota infeasible")
    for source_id in old_candidates[: max(0, 50 - old_count)]:
        owner = owner_by_source[source_id]
        required_baseline[source_id] = (owner, "diversity_control")
        used.add(source_id)

    other_count = sum(
        info[source_id]["item_category"] == "other"
        for source_id in set(required_baseline) | set(delta)
    )
    other_candidates = sorted(
        (
            source_id for source_id in eligible - used
            if source_id in owner_by_source
            and info[source_id]["scope_class"] == "analytical"
            and info[source_id]["item_category"] == "other"
        ),
        key=lambda source_id: stable_key(str(spec["seed"]), "scholarly-other", source_id),
    )
    if other_count + len(other_candidates) < 20:
        raise ValueError("scholarly-other diversity quota infeasible")
    for source_id in other_candidates[: max(0, 20 - other_count)]:
        owner = owner_by_source[source_id]
        required_baseline[source_id] = (owner, "diversity_control")
        used.add(source_id)

    for stratum_id in pools:
        journals = [
            source_id for source_id, (owner, _) in required_baseline.items()
            if owner == stratum_id and info[source_id]["item_category"] == "journal"
        ]
        candidates = sorted(
            (
                source_id for source_id in pools[stratum_id] - used
                if info[source_id]["scope_class"] == "analytical"
                and info[source_id]["item_category"] == "journal"
            ),
            key=lambda source_id: stable_key(str(spec["seed"]), stratum_id, "canary-journal", source_id),
        )
        if len(journals) + len(candidates) < 2:
            raise ValueError(f"two-journal source-canary gate infeasible: {stratum_id}")
        for source_id in candidates[: max(0, 2 - len(journals))]:
            required_baseline[source_id] = (stratum_id, "source_canary_journal")
            used.add(source_id)

    baseline: dict[str, tuple[str, str]] = dict(required_baseline)
    if set(baseline) & set(delta):
        raise ValueError("baseline and delta overlap")
    selected = set(baseline) | set(delta)

    for stratum_id in scarce_order:
        quota = quota_by_id[stratum_id]
        baseline_target = int(quota["baseline_count"])
        baseline_context_target = int(quota["combined_context_count"]) - 1
        owned = {source_id for source_id, value in baseline.items() if value[0] == stratum_id}
        context_needed = baseline_context_target - sum(
            info[source_id]["scope_class"] == "context" for source_id in owned
        )
        context_candidates = sorted(
            (
                source_id
                for source_id in pools[stratum_id] - selected
                if info[source_id]["scope_class"] == "context"
            ),
            key=lambda source_id: stable_key(str(spec["seed"]), stratum_id, "baseline-context", source_id),
        )
        if context_needed < 0 or len(context_candidates) < context_needed:
            raise ValueError(f"baseline context quota infeasible: {stratum_id}")
        for source_id in context_candidates[:context_needed]:
            baseline[source_id] = (stratum_id, "context_quota")
            selected.add(source_id)
        fill_analytical_cell(
            seed=str(spec["seed"]),
            stratum_id=stratum_id,
            target=baseline_target - baseline_context_target,
            baseline=baseline,
            selected=selected,
            pool=pools[stratum_id],
            packet_key=packet_key_by_source,
            global_packet_sizes=global_analytical_packet_sizes,
            info=info,
        )

    rows = []
    membership_by_source = {
        source_id: sorted(stratum_id for stratum_id, pool in candidate_pools.items() if source_id in pool)
        for source_id in selected
    }
    for phase, mapping in (("baseline", baseline), ("delta", delta)):
        for source_id, (owner, reason) in mapping.items():
            row = source_by_id[source_id]
            note_id = str(row["note_id"])
            note = source_note_path(origin, note_id)
            profile = profile_path(origin, note_id)
            bundle = bundle_path(origin, note_id)
            rows.append(
                {
                    "source_id": source_id,
                    "canonical_source_id": str(row.get("canonical_source_id") or source_id),
                    "zotero_key": str(row.get("zotero_key") or source_id.removeprefix("source-zotero-").upper()),
                    "note_id": note_id,
                    "phase": phase,
                    "primary_stratum_id": owner,
                    "secondary_stratum_ids": [
                        value for value in membership_by_source[source_id] if value != owner
                    ],
                    "selection_reason": reason,
                    "deepest_leaf_packet_key": packet_key_by_source[source_id],
                    **info[source_id],
                    "note_path": str(note.relative_to(origin)),
                    "semantic_note_sha256": semantic_note_hash(note.read_text(encoding="utf-8")),
                    "profile_path": str(profile.relative_to(origin)),
                    "profile_sha256": sha256_file(profile),
                    "bundle_path": str(bundle.relative_to(origin)) if bundle else "",
                    "bundle_sha256": sha256_file(bundle) if bundle else "",
                }
            )
    rows.sort(key=lambda row: (str(row["phase"]), str(row["source_id"])))
    full_packet_keys = {
        key
        for key, members in packet_members.items()
        if 3 <= global_analytical_packet_sizes.get(key, 0) <= 25
        and {
            source_id for source_id in members
            if info[source_id]["scope_class"] == "analytical"
        } <= selected
    }
    selected_packet_sizes = Counter(
        (owner, packet_key_by_source[source_id])
        for source_id, (owner, _) in {**baseline, **delta}.items()
    )
    coherent_packets = {
        stratum_id: sorted(
            key for (owner, key), count in selected_packet_sizes.items()
            if owner == stratum_id and count >= 4
        )
        for stratum_id in pools
    }
    required_ids = set(required_baseline) | {
        source_id for source_id, (_, reason) in baseline.items() if reason == "context_quota"
    }
    non_required = {
        source_id for source_id in selected - required_ids
        if info[source_id]["scope_class"] == "analytical"
    }
    packet_selected = set().union(
        *(
            {
                source_id for source_id in packet_members[key]
                if info[source_id]["scope_class"] == "analytical"
            }
            for key in full_packet_keys
        )
    ) if full_packet_keys else set()
    whole_packet_ratio = len(non_required & packet_selected) / len(non_required)
    if any(len(keys) < 2 for keys in coherent_packets.values()) or whole_packet_ratio < 0.8:
        raise ValueError(
            f"collection cohesion gate failed: ratio={whole_packet_ratio:.4f} "
            f"packet_counts={{{', '.join(f'{key}:{len(value)}' for key, value in coherent_packets.items())}}}"
        )
    manifest = {
        "schema_version": "1",
        "benchmark_id": str(spec["benchmark_id"]),
        "seed": str(spec["seed"]),
        "status": "selected_gold_pending",
        "never_production_input": True,
        "frozen_inputs": {
            "source_catalogue_sha256": sha256_file(catalogue_path),
            "literature_positions_sha256": sha256_file(positions_path),
            "collection_snapshot_sha256": sha256_file(snapshot_path),
            "pregraph_bridge_benchmark_sha256": sha256_file(bridge_path),
            "pregraph_structural_benchmarks_sha256": sha256_file(structural_path),
            "missing_sources_sha256": sha256_file(missing_path),
            "strata_spec_sha256": sha256_file(strata_path),
            "anchor_manifest_sha256": sha256_file(anchors_path),
            "mapping_spec_sha256": sha256_file(spec_path),
            "independent_delta_controls_sha256": sha256_file(controls_path),
        },
        "quotas": quotas,
        "sources": rows,
        "delta_controls": sorted(delta_control_rows, key=lambda row: row["stratum_id"]),
        "cohesion": {
            "whole_packet_ratio": round(whole_packet_ratio, 6),
            "coherent_packet_keys_by_stratum": coherent_packets,
            "globally_complete_packet_keys": sorted(full_packet_keys),
        },
    }
    manifest["selection_sha256"] = stable_key(json.dumps(manifest, sort_keys=True, ensure_ascii=False))
    validate_selection(manifest, anchors)
    return manifest


def validate_selection(manifest: Mapping[str, Any], anchors: list[Mapping[str, Any]]) -> None:
    rows = list(manifest.get("sources", []) or [])
    baseline = [row for row in rows if row["phase"] == "baseline"]
    delta = [row for row in rows if row["phase"] == "delta"]
    if (len(rows), len(baseline), len(delta)) != (1000, 900, 100):
        raise ValueError("selection count mismatch")
    if len({row["source_id"] for row in rows}) != 1000:
        raise ValueError("selection contains duplicate canonical sources")
    if any(row["canonical_source_id"] != row["source_id"] for row in rows):
        raise ValueError("selection contains a canonical alias")
    anchor_ids = {str(row["source_id"]) for row in anchors}
    if not anchor_ids <= {str(row["source_id"]) for row in baseline}:
        raise ValueError("all anchors must remain in baseline")
    scope = Counter(str(row["scope_class"]) for row in rows)
    if scope != Counter({"analytical": 920, "context": 80}):
        raise ValueError(f"scope quota mismatch: {dict(scope)}")
    quota_by_id = {str(row["stratum_id"]): row for row in manifest["quotas"]}
    for stratum_id, quota in quota_by_id.items():
        owned = [row for row in rows if row["primary_stratum_id"] == stratum_id]
        owned_baseline = [row for row in owned if row["phase"] == "baseline"]
        owned_delta = [row for row in owned if row["phase"] == "delta"]
        if (
            len(owned) != int(quota["combined_count"])
            or len(owned_baseline) != int(quota["baseline_count"])
            or len(owned_delta) != 5
            or sum(row["scope_class"] == "context" for row in owned)
            != int(quota["combined_context_count"])
            or Counter(row["scope_class"] for row in owned_delta)
            != Counter({"analytical": 4, "context": 1})
        ):
            raise ValueError(f"stratum quota mismatch: {stratum_id}")
        boundary_keys = {
            str(row["selection_reason"]).split(":", 1)[1]
            for row in owned_baseline
            if str(row["selection_reason"]).startswith("boundary_packet:")
        }
        delta_packet_keys = {str(row["deepest_leaf_packet_key"]) for row in owned_delta}
        if len(boundary_keys) > 1 or len(delta_packet_keys) > 2:
            raise ValueError(f"packet-boundary gate failed: {stratum_id}")
    types, years = diversity_counts(
        (str(row["source_id"]) for row in rows),
        {str(row["source_id"]): row for row in rows},
    )
    if not 350 <= types["journal"] <= 550:
        raise ValueError(f"journal diversity gate failed: {types['journal']}")
    for category, minimum in TYPE_MINIMUMS.items():
        if types[category] < minimum:
            raise ValueError(f"item-type diversity gate failed: {category}={types[category]}")
    for period, minimum in YEAR_MINIMUMS.items():
        if years[period] < minimum:
            raise ValueError(f"year diversity gate failed: {period}={years[period]} all={dict(years)}")
    if years["n_d"] > 200:
        raise ValueError(f"undated diversity gate failed: {years['n_d']}")


def sanitize_positions(payload: Mapping[str, Any], active: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = []
    hidden = []
    for raw in payload.get("positions", []) or []:
        row = dict(raw)
        if str(row.get("current_source_id") or "") not in active:
            continue
        target = str(row.get("matched_source_id") or "")
        if target and target not in active:
            hidden.append({"literature_position_id": row.get("literature_position_id"), "matched_source_id": target})
            row.update(
                matched_source_id="",
                matched_zotero_key="",
                match_basis="",
                match_confidence="",
                match_candidates=[],
                match_status="not_in_snapshot",
            )
        else:
            row["match_candidates"] = [
                value for value in row.get("match_candidates", []) or [] if str(value) in active
            ]
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("literature_position_id") or ""))
    return (
        {
            "literature_position_registry_schema_version": str(
                payload.get("literature_position_registry_schema_version") or "1"
            ),
            "positions": rows,
            "projection_errors": [],
            "revision_hash": stable_key(json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)),
        },
        hidden,
    )


def sanitize_missing(payload: Mapping[str, Any], active: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = []
    hidden = []
    for raw in payload.get("sources", []) or []:
        discussers = sorted(active & {str(value) for value in raw.get("discussed_by_source_ids", []) or []})
        if not discussers:
            continue
        row = dict(raw)
        row["discussed_by_source_ids"] = discussers
        row["relevant_collections"] = []
        row["relevant_topics"] = []
        row["relevant_clusters"] = []
        mapped = str(row.get("source_id") or "")
        if mapped and mapped not in active:
            hidden.append({"external_source_id": row.get("external_source_id"), "source_id": mapped})
            row.update(source_id="", note_id="", zotero_key="", match_status="not_in_snapshot")
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("external_source_id") or ""))
    return (
        {
            "missing_source_registry_schema_version": str(
                payload.get("missing_source_registry_schema_version") or "1"
            ),
            "sources": rows,
            "revision_hash": stable_key(json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)),
        },
        hidden,
    )


def copy_clean_source(origin: Path, destination: Path, row: Mapping[str, Any]) -> None:
    source_note = origin / str(row["note_path"])
    target_note = destination / str(row["note_path"])
    raw = source_note.read_text(encoding="utf-8")
    merged = dict(read_note(source_note)["frontmatter"])
    frontmatter = {key: value for key, value in merged.items() if key not in NON_SOURCE_FRONTMATTER_FIELDS}
    frontmatter["related_notes"] = []
    clean = _public_note_text(canonical_source_note_text(raw), frontmatter)
    if semantic_note_hash(raw) != semantic_note_hash(clean):
        raise ValueError(f"semantic note changed while cleaning: {row['source_id']}")
    target_note.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_note, clean)
    _write_note_metadata(destination, target_note, frontmatter, machine_text=clean)
    result = validate_note(internal_note_text(target_note))
    if not result.passed:
        raise ValueError(f"clean note invalid: {row['source_id']}: {result.errors}")
    for key in ("profile_path", "bundle_path"):
        relative = str(row.get(key) or "")
        if not relative:
            continue
        source = origin / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_phase_registries(
    origin: Path, destination: Path, rows: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    active = {str(row["source_id"]) for row in rows}
    snapshot = read_yaml(origin / "01_custody/zotero/collection_snapshot.yml", {}) or {}
    catalogue_rows = [
        {"zotero_key": source_id.removeprefix("source-zotero-").upper()}
        for source_id in active
    ]
    write_yaml(destination / "01_custody/zotero/collection_snapshot.yml", filtered_snapshot(snapshot, catalogue_rows))
    positions, hidden_positions = sanitize_positions(
        read_yaml(origin / "02_source_memory/indexes/literature_positions.yml", {}) or {}, active
    )
    missing, hidden_missing = sanitize_missing(
        read_yaml(origin / "02_source_memory/indexes/missing_sources.yml", {}) or {}, active
    )
    write_yaml(destination / "02_source_memory/indexes/literature_positions.yml", positions)
    write_yaml(destination / "02_source_memory/indexes/missing_sources.yml", missing)
    return hidden_positions + hidden_missing


def source_canary(origin: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    rows = [dict(row) for row in manifest["sources"] if row["phase"] == "baseline"]
    metadata = {}
    for row in rows:
        payload = read_yaml(origin / "11_state/note_metadata" / f"{row['note_id']}.yml", {}) or {}
        metadata[str(row["source_id"])] = dict(payload.get("frontmatter", {}) or {})
    chosen: dict[str, str] = {}

    def take(category: str, count: int, predicate: Any) -> None:
        candidates = [
            row for row in rows
            if row["source_id"] not in chosen and predicate(row, metadata[row["source_id"]])
        ]
        candidates.sort(key=lambda row: stable_key(str(manifest["seed"]), "source-canary", category, str(row["source_id"])))
        if len(candidates) < count:
            raise ValueError(f"source canary category infeasible: {category}={len(candidates)}/{count}")
        chosen.update({str(row["source_id"]): category for row in candidates[:count]})

    for stratum_id in [str(row["stratum_id"]) for row in manifest["quotas"]]:
        take(
            f"standard_full_text:{stratum_id}",
            2,
            lambda row, meta, stratum_id=stratum_id: row["primary_stratum_id"] == stratum_id
            and row["scope_class"] == "analytical"
            and row["item_category"] == "journal",
        )
    take("metadata_identity", 4, lambda row, meta: str(meta.get("note_status") or "") == "metadata_only_source_note")
    take("abstract_partial", 8, lambda row, meta: str(meta.get("note_status") or "") in {"abstract_only_atomic_note", "partial_document_atomic_note"})
    take("ocr_suspicious", 8, lambda row, meta: row["scope_class"] == "analytical" and "tesseract" in str(meta.get("content_route") or ""))
    take("report_policy_legal", 8, lambda row, meta: row["scope_class"] == "analytical" and row["item_category"] == "reports")
    take("book_chapter_long", 12, lambda row, meta: row["scope_class"] == "analytical" and row["item_category"] == "books")
    if len(chosen) != 80:
        raise ValueError(f"source canary count mismatch: {len(chosen)}")
    cases = []
    for row in rows:
        source_id = str(row["source_id"])
        if source_id not in chosen:
            continue
        meta = metadata[source_id]
        source_file = Path(str(meta.get("source_file") or ""))
        if source_file and not source_file.is_absolute():
            source_file = origin / source_file
        custody_hash = sha256_file(source_file) if source_file.is_file() else str(meta.get("inspected_content_hash") or "")
        if not custody_hash:
            raise ValueError(f"source canary lacks frozen custody input: {source_id}")
        cases.append({
            "source_id": source_id,
            "note_id": row["note_id"],
            "category": chosen[source_id],
            "stratum_id": row["primary_stratum_id"],
            "content_route": str(meta.get("content_route") or ""),
            "custody_file": str(source_file.relative_to(origin)) if source_file.is_file() and source_file.is_relative_to(origin) else "",
            "custody_kind": "file" if source_file.is_file() else "frozen_content_identity",
            "custody_sha256": custody_hash,
            "expected_semantic_note_sha256": row["semantic_note_sha256"],
        })
    return {"schema_version": "1", "status": "frozen", "never_production_input": True, "cases": sorted(cases, key=lambda row: str(row["source_id"]))}


def materialize(origin: Path, target: Path, manifest: Mapping[str, Any]) -> None:
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"target must be absent or empty: {target}")
    initialize_workspace(target)
    config = read_yaml(target / "auto-zettelkasten.yml", {}) or {}
    config.pop("provider", None)
    config.pop("model", None)
    config["privacy"] = {"allow_cloud": False}
    config["max_provider_spend_usd"] = 0.0
    config["literature_mapping"] = {**dict(config.get("literature_mapping", {})), "synthesis_enabled": False, "max_profile_calls": 0, "max_synthesis_calls": 0}
    write_yaml(target / "auto-zettelkasten.yml", config)
    rows = list(manifest["sources"])
    baseline = [row for row in rows if row["phase"] == "baseline"]
    delta = [row for row in rows if row["phase"] == "delta"]
    delta_root = target / "evaluation/delta_payload"
    for row in baseline:
        copy_clean_source(origin, target, row)
    for row in delta:
        copy_clean_source(origin, delta_root, row)
    hidden = write_phase_registries(origin, target, baseline)
    hidden += write_phase_registries(origin, delta_root, rows)
    write_yaml(target / "evaluation/v030-selection.yml", dict(manifest))
    write_yaml(target / "evaluation/v030-source-canary.yml", source_canary(origin, manifest))
    write_yaml(target / "evaluation/v030-gold-status.yml", {
        "schema_version": "1",
        "status": "judgments_pending",
        "never_production_input": True,
        "relationship_anchors_required": 120,
        "relationship_anchors_complete": 0,
        "cluster_challenges_required": 40,
        "cluster_challenges_complete": 0,
        "external_acquisition_controls_required": 40,
        "external_acquisition_controls_complete": 0,
    })
    write_yaml(target / "evaluation/v030-outside-sample-matches.yml", {"never_production_input": True, "rows": hidden})
    write_yaml(target / "evaluation/v030-materialization.yml", {
        "schema_version": "1",
        "selection_sha256": manifest["selection_sha256"],
        "baseline_count": 900,
        "delta_count": 100,
        "activated": False,
    })


def validate_target(origin: Path, target: Path, manifest: Mapping[str, Any], *, activated: bool) -> None:
    active_rows = list(manifest["sources"]) if activated else [row for row in manifest["sources"] if row["phase"] == "baseline"]
    delta_rows = [] if activated else [row for row in manifest["sources"] if row["phase"] == "delta"]
    phases = [(target, active_rows)]
    if not activated:
        phases.append((target / "evaluation/delta_payload", delta_rows))
    for destination, rows in phases:
        expected_notes = {str(row["note_path"]) for row in rows}
        actual_notes = {
            str(path.relative_to(destination))
            for path in (destination / "02_source_memory/notes").glob("**/*.md")
        }
        if actual_notes != expected_notes:
            raise ValueError(f"note inventory mismatch: {destination}")
        for row in rows:
            note = destination / str(row["note_path"])
            profile = destination / str(row["profile_path"])
            bundle = destination / str(row.get("bundle_path") or "") if row.get("bundle_path") else None
            if not note.is_file() or semantic_note_hash(note.read_text(encoding="utf-8")) != row["semantic_note_sha256"]:
                raise ValueError(f"note validation failed: {row['source_id']}")
            text = note.read_text(encoding="utf-8")
            if "<!-- auto-zettelkasten:graph:start -->" in text or "<!-- auto-zettelkasten:literature:start -->" in text:
                raise ValueError(f"managed projection leaked into note: {row['source_id']}")
            metadata = read_yaml(
                destination / "11_state/note_metadata" / f"{row['note_id']}.yml", {}
            ) or {}
            if (
                metadata.get("note_path") != row["note_path"]
                or metadata.get("machine_preservation_hash") != source_note_preservation_hash(text)
            ):
                raise ValueError(f"note metadata integrity failed: {row['source_id']}")
            frontmatter = metadata.get("frontmatter", {}) or {}
            if frontmatter.get("note_id") != row["note_id"] or frontmatter.get("source_id") != row["source_id"]:
                raise ValueError(f"note metadata identity failed: {row['source_id']}")
            leaked = (set(frontmatter) & NON_SOURCE_FRONTMATTER_FIELDS) - {"related_notes"}
            if leaked or frontmatter.get("related_notes"):
                raise ValueError(f"graph frontmatter leaked into note: {row['source_id']}")
            if sha256_file(profile) != row["profile_sha256"] or (bundle and sha256_file(bundle) != row["bundle_sha256"]):
                raise ValueError(f"profile/bundle validation failed: {row['source_id']}")
    active = {str(row["source_id"]) for row in active_rows}
    snapshot = read_yaml(target / "01_custody/zotero/collection_snapshot.yml", {}) or {}
    snapshot_ids = {
        f"source-zotero-{str(row.get('key') or '').casefold()}"
        for row in snapshot.get("items", []) or []
    }
    if snapshot_ids != active:
        raise ValueError("active Zotero snapshot differs from selected sources")
    positions = read_yaml(target / "02_source_memory/indexes/literature_positions.yml", {}) or {}
    if any(str(row.get("current_source_id") or "") not in active or (row.get("matched_source_id") and str(row["matched_source_id"]) not in active) for row in positions.get("positions", []) or []):
        raise ValueError("active literature positions leak outside the snapshot")
    missing = read_yaml(target / "02_source_memory/indexes/missing_sources.yml", {}) or {}
    if any(
        not set(str(value) for value in row.get("discussed_by_source_ids", []) or []) <= active
        or row.get("relevant_clusters")
        or (row.get("source_id") and str(row["source_id"]) not in active)
        for row in missing.get("sources", []) or []
    ):
        raise ValueError("active missing-source registry leaks prior graph state")
    index_files = {
        path.name for path in (target / "02_source_memory/indexes").glob("**/*") if path.is_file()
    }
    if index_files != {"literature_positions.yml", "missing_sources.yml"}:
        raise ValueError(f"forbidden prior graph indexes: {sorted(index_files)}")
    if any(path.is_file() for path in (target / "03_literature_synthesis").glob("**/*")):
        raise ValueError("prior literature synthesis artifacts are present")
    if any(path.is_file() for path in (target / "11_state/runs").glob("**/*")):
        raise ValueError("prior run receipts are present")


def activate_delta(target: Path) -> None:
    manifest = read_yaml(target / "evaluation/v030-selection.yml", {}) or {}
    receipt_path = target / "evaluation/v030-delta-activation.yml"
    delta_rows = [row for row in manifest.get("sources", []) or [] if row.get("phase") == "delta"]
    if len(delta_rows) != 100:
        raise ValueError(f"activation requires exactly 100 delta rows: {len(delta_rows)}")
    if receipt_path.exists():
        receipt = read_yaml(receipt_path, {}) or {}
        if receipt.get("selection_sha256") != manifest.get("selection_sha256"):
            raise ValueError("delta activation receipt belongs to another selection")
        for row in delta_rows:
            note = target / str(row["note_path"])
            profile = target / str(row["profile_path"])
            if not note.is_file() or semantic_note_hash(note.read_text(encoding="utf-8")) != row["semantic_note_sha256"] or sha256_file(profile) != row["profile_sha256"]:
                raise ValueError(f"activated delta is incomplete: {row['source_id']}")
        return
    delta_root = target / "evaluation/delta_payload"
    for row in delta_rows:
        note = delta_root / str(row["note_path"])
        profile = delta_root / str(row["profile_path"])
        bundle = delta_root / str(row.get("bundle_path") or "") if row.get("bundle_path") else None
        if semantic_note_hash(note.read_text(encoding="utf-8")) != row["semantic_note_sha256"] or sha256_file(profile) != row["profile_sha256"] or (bundle and sha256_file(bundle) != row["bundle_sha256"]):
            raise ValueError(f"delta hash validation failed: {row['source_id']}")
        for key in ("note_path", "profile_path", "bundle_path"):
            relative = str(row.get(key) or "")
            if not relative:
                continue
            source = delta_root / relative
            target_path = target / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_path)
        note_id = str(row["note_id"])
        metadata = delta_root / "11_state/note_metadata" / f"{note_id}.yml"
        metadata_payload = read_yaml(metadata, {}) or {}
        if metadata_payload.get("note_path") != row["note_path"]:
            raise ValueError(f"delta metadata validation failed: {row['source_id']}")
        target_metadata = target / "11_state/note_metadata" / metadata.name
        shutil.copy2(metadata, target_metadata)
    for relative in (
        "01_custody/zotero/collection_snapshot.yml",
        "02_source_memory/indexes/literature_positions.yml",
        "02_source_memory/indexes/missing_sources.yml",
    ):
        atomic_write_text(target / relative, (delta_root / relative).read_text(encoding="utf-8"))
    materialization_path = target / "evaluation/v030-materialization.yml"
    materialization = read_yaml(materialization_path, {}) or {}
    materialization["activated"] = True
    write_yaml(materialization_path, materialization)
    write_yaml(receipt_path, {"schema_version": "1", "selection_sha256": manifest["selection_sha256"], "delta_count": 100, "activated": True})


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "validate", "activate-delta"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--target", type=Path, required=True)
        if command != "activate-delta":
            subparser.add_argument("--origin", type=Path, required=True)
            subparser.add_argument("--spec", type=Path, required=True)
        if command == "validate":
            subparser.add_argument("--activated", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = select_sample(args.origin.resolve(), args.spec.resolve())
        materialize(args.origin.resolve(), args.target.resolve(), manifest)
        validate_target(args.origin.resolve(), args.target.resolve(), manifest, activated=False)
        print(json.dumps({"status": manifest["status"], "sources": len(manifest["sources"])}, indent=2))
        return 0
    if args.command == "activate-delta":
        activate_delta(args.target.resolve())
        return 0
    manifest = select_sample(args.origin.resolve(), args.spec.resolve())
    stored = read_yaml(args.target.resolve() / "evaluation/v030-selection.yml", {}) or {}
    if manifest.get("selection_sha256") != stored.get("selection_sha256"):
        raise ValueError("stored selection differs from deterministic rerun")
    validate_target(args.origin.resolve(), args.target.resolve(), stored, activated=args.activated)
    print(json.dumps({"status": "valid", "activated": args.activated}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
