from __future__ import annotations

from collections import defaultdict
import csv
from pathlib import Path
from typing import Any, Mapping, Sequence

from .files import atomic_write_text, now_iso, read_yaml, sha256_text, slugify, write_yaml
from .identity import identify_work
from .notes import read_note
from .workspace import confined_child, validate_opaque_id

TYPED_RELATIONS = {
    "cites",
    "cited_by",
    "same_concept",
    "same_case",
    "same_method",
    "extends",
    "challenges",
    "closest_prior_work",
    "possible_gap_relation",
    "zotero_related",
    "co_cited_with",
    "bibliographic_coupling",
}


def commit_tag_reviews(
    workspace: Path,
    proposals: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    proposal_path = workspace / "02_source_memory" / "indexes" / "tag_proposals.yml"
    registry_path = workspace / "02_source_memory" / "indexes" / "tag_registry.yml"
    existing_rows = (read_yaml(proposal_path, {}) or {}).get("proposals", [])
    by_id = {
        str(row.get("proposal_id")): dict(row)
        for row in existing_rows
        if isinstance(row, Mapping) and row.get("proposal_id")
    }
    for row in proposals:
        if row.get("proposal_id"):
            by_id[str(row["proposal_id"])] = dict(row)
    for row in decisions:
        proposal_id = str(row.get("proposal_id", ""))
        if proposal_id:
            by_id[proposal_id] = {**by_id.get(proposal_id, {}), **dict(row), "reviewed_at": now_iso()}
    reviewed = sorted(by_id.values(), key=lambda row: str(row.get("proposal_id", "")))
    write_yaml(proposal_path, {"updated_at": now_iso(), "proposals": reviewed})

    accepted = [row for row in reviewed if row.get("decision") == "accepted"]
    registry: dict[str, dict[str, Any]] = {}
    for row in accepted:
        normalized = str(row.get("proposed_tag", ""))
        if not normalized:
            continue
        entry = registry.setdefault(
            normalized,
            {"normalized_tag": normalized, "original_tags": [], "note_ids": [], "accepted_proposal_ids": []},
        )
        for key, value in (
            ("original_tags", row.get("original_tag")),
            ("note_ids", row.get("note_id")),
            ("accepted_proposal_ids", row.get("proposal_id")),
        ):
            if value and value not in entry[key]:
                entry[key].append(value)
    write_yaml(registry_path, {"updated_at": now_iso(), "tags": sorted(registry.values(), key=lambda row: row["normalized_tag"])})
    return {
        "proposal_path": str(proposal_path),
        "registry_path": str(registry_path),
        "proposal_count": len(reviewed),
        "accepted_count": len(accepted),
        "parked_count": sum(1 for row in reviewed if row.get("decision") == "parked"),
        "rejected_count": sum(1 for row in reviewed if row.get("decision") == "rejected"),
    }


def accepted_tags_by_note(decisions: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for row in decisions:
        if row.get("decision") == "accepted" and row.get("note_id") and row.get("proposed_tag"):
            values[str(row["note_id"])].add(str(row["proposed_tag"]))
    return {key: sorted(tags) for key, tags in values.items()}


def build_typed_links(workspace: Path, notes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    links: list[dict[str, Any]] = []
    sorted_notes = sorted(notes, key=lambda row: str(row.get("note_id", "")))
    note_by_zotero_key = {str(row.get("zotero_item_key")): row for row in sorted_notes if row.get("zotero_item_key")}
    seen: set[tuple[str, str, str]] = set()
    for index, left in enumerate(sorted_notes):
        left_tags = set(left.get("normalized_tags", []) or [])
        for right in sorted_notes[index + 1 :]:
            shared = sorted(left_tags & set(right.get("normalized_tags", []) or []))
            if not shared:
                continue
            relation = {
                "link_id": f"link-{sha256_text(str(left['note_id']) + '|' + str(right['note_id']) + '|same_concept')[:12]}",
                "source_note_id": left["note_id"],
                "target_note_id": right["note_id"],
                "relation_type": "same_concept",
                "shared_normalized_tags": shared,
                "provenance": "controller_accepted_tag_registry",
            }
            links.append(relation)
            seen.add((str(left["note_id"]), str(right["note_id"]), "same_concept"))
    for source in sorted_notes:
        relations = source.get("zotero_relations", {})
        if not isinstance(relations, Mapping):
            continue
        for predicate, values in relations.items():
            relation_type = _zotero_relation_type(str(predicate))
            for value in values if isinstance(values, list) else [values]:
                target_key = str(value or "").rstrip("/").rsplit("/", 1)[-1]
                target = note_by_zotero_key.get(target_key)
                if not target or target.get("note_id") == source.get("note_id"):
                    continue
                key = (str(source["note_id"]), str(target["note_id"]), relation_type)
                if key in seen:
                    continue
                seen.add(key)
                links.append(
                    {
                        "link_id": f"link-{sha256_text('|'.join(key))[:12]}",
                        "source_note_id": source["note_id"],
                        "target_note_id": target["note_id"],
                        "relation_type": relation_type,
                        "zotero_predicate": str(predicate),
                        "provenance": "exact_zotero_item_relation",
                    }
                )
    note_by_source_id = {str(row.get("source_id")): row for row in sorted_notes if row.get("source_id")}
    note_by_work_id = {identify_work(row)[0]: row for row in sorted_notes}
    references_by_note: dict[str, set[str]] = {}
    mapped_references_by_origin: dict[str, set[str]] = {}
    for source in sorted_notes:
        source_id = str(source.get("source_id") or "")
        if not source_id:
            continue
        try:
            sidecar_path = confined_child(
                workspace / "01_custody" / "citation_leads",
                f"{validate_opaque_id(source_id, field='source_id')}.yml",
            )
        except ValueError:
            continue
        sidecar = read_yaml(sidecar_path, {}) or {}
        reference_ids = {
            str(row.get("work_id") or identify_work(row)[0])
            for row in sidecar.get("references", [])
            if isinstance(row, Mapping)
        }
        references_by_note[str(source["note_id"])] = reference_ids
        mapped_targets: set[str] = set()
        for work_id in sorted(reference_ids):
            target = note_by_work_id.get(work_id)
            if not target or target.get("note_id") == source.get("note_id"):
                continue
            target_note_id = str(target["note_id"])
            mapped_targets.add(target_note_id)
            key = (str(source["note_id"]), target_note_id, "cites")
            if key in seen:
                continue
            seen.add(key)
            links.append(
                {
                    "link_id": f"link-{sha256_text('|'.join(key))[:12]}",
                    "source_note_id": source["note_id"],
                    "target_note_id": target_note_id,
                    "relation_type": "cites",
                    "provenance": f"01_custody/citation_leads/{source_id}.yml",
                }
            )
        mapped_references_by_origin[str(source["note_id"])] = mapped_targets
    for targets in mapped_references_by_origin.values():
        ordered = sorted(targets)
        for index, left_id in enumerate(ordered):
            for right_id in ordered[index + 1 :]:
                key = (left_id, right_id, "co_cited_with")
                if key in seen:
                    continue
                seen.add(key)
                links.append(
                    {
                        "link_id": f"link-{sha256_text('|'.join(key))[:12]}",
                        "source_note_id": left_id,
                        "target_note_id": right_id,
                        "relation_type": "co_cited_with",
                        "provenance": "shared_origin_citation_sidecar",
                    }
                )
    note_ids = sorted(references_by_note)
    for index, left_id in enumerate(note_ids):
        for right_id in note_ids[index + 1 :]:
            shared = sorted(references_by_note[left_id] & references_by_note[right_id])
            if not shared:
                continue
            key = (left_id, right_id, "bibliographic_coupling")
            if key in seen:
                continue
            seen.add(key)
            links.append(
                {
                    "link_id": f"link-{sha256_text('|'.join(key))[:12]}",
                    "source_note_id": left_id,
                    "target_note_id": right_id,
                    "relation_type": "bibliographic_coupling",
                    "shared_reference_work_ids": shared,
                    "provenance": "deterministic_citation_sidecars",
                }
            )
    relation_registry = workspace / "01_custody" / "source_relation_registry.csv"
    if relation_registry.exists():
        with relation_registry.open("r", encoding="utf-8", newline="") as handle:
            relation_rows = list(csv.DictReader(handle))
        for relation in relation_rows:
            source = note_by_source_id.get(str(relation.get("source_id", "")))
            target = note_by_source_id.get(str(relation.get("related_source_id", "")))
            if not source or not target or source.get("note_id") == target.get("note_id"):
                continue
            relation_type = str(relation.get("relation_type") or "zotero_related")
            if relation_type not in TYPED_RELATIONS:
                relation_type = "zotero_related"
            key = (str(source["note_id"]), str(target["note_id"]), relation_type)
            if key in seen:
                continue
            seen.add(key)
            links.append(
                {
                    "link_id": str(relation.get("relation_id") or f"link-{sha256_text('|'.join(key))[:12]}"),
                    "source_note_id": source["note_id"],
                    "target_note_id": target["note_id"],
                    "relation_type": relation_type,
                    "route": str(relation.get("route") or "source_relation_registry"),
                    "confidence": str(relation.get("confidence") or ""),
                    "provenance": "01_custody/source_relation_registry.csv",
                }
            )
    links.sort(key=lambda row: (str(row["source_note_id"]), str(row["target_note_id"]), str(row["relation_type"])))
    path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility_path = workspace / "02_source_memory" / "indexes" / "typed_note_links.yml"
    payload = {"updated_at": now_iso(), "allowed_relation_types": sorted(TYPED_RELATIONS), "links": links}
    write_yaml(path, payload)
    write_yaml(compatibility_path, payload)
    return {"path": str(path), "compatibility_path": str(compatibility_path), "links": links, "link_count": len(links)}


def write_source_set(
    workspace: Path,
    *,
    run_id: str,
    scope: str,
    collection_key: str | None,
    items: Sequence[Mapping[str, Any]],
    terminal_rows: Sequence[Mapping[str, Any]],
    note_rows: Sequence[Mapping[str, Any]],
    cluster_ids: Sequence[str] = (),
    gap_ids: Sequence[str] = (),
    source_set_id: str | None = None,
    source_set_type: str | None = None,
) -> dict[str, Any]:
    suffix = f"zotero-{slugify(collection_key)}" if collection_key else f"run-{slugify(run_id)}"
    source_set_id = source_set_id or f"source-set-{suffix}"
    zotero_keys = [_item_key(item) for item in items]
    original_tags = sorted({tag for row in note_rows for tag in row.get("original_zotero_tags", []) or []})
    normalized_tags = sorted({tag for row in note_rows for tag in row.get("normalized_tags", []) or []})
    terminal_by_index = {int(row.get("inventory_index", -1)): row for row in terminal_rows}
    dependency_payload = [
        {
            "inventory_index": index,
            "zotero_item_key": key,
            "terminal_status": terminal_by_index.get(index, {}).get("terminal_status"),
            "fingerprint": terminal_by_index.get(index, {}).get("fingerprint"),
        }
        for index, key in enumerate(zotero_keys)
    ]
    payload = {
        "source_set_id": source_set_id,
        "source_set_type": source_set_type or ("zotero_collection" if collection_key else "auto_zettelkasten_run"),
        "scope": scope,
        "run_id": run_id,
        "zotero_collection_key": collection_key or "",
        "upstream_scope": {"kind": f"zotero_{scope}", "id": collection_key or run_id},
        "inventory_count": len(items),
        "terminal_count": len(terminal_rows),
        "source_ids": [str(row.get("source_id")) for row in note_rows],
        "note_ids": [str(row.get("note_id")) for row in note_rows],
        "note_paths": [str(row.get("note_path")) for row in note_rows],
        "obsidian_links": [f"[[{Path(str(row.get('note_path'))).stem}]]" for row in note_rows],
        "zotero_item_keys": zotero_keys,
        "original_zotero_tags": original_tags,
        "normalized_tags": normalized_tags,
        "cluster_ids": sorted(set(cluster_ids)),
        "gap_ids": sorted(set(gap_ids)),
        "dependency_hash": sha256_text(repr(dependency_payload)),
        "stale": False,
        "updated_at": now_iso(),
        "rows": [
            {
                "inventory_index": index,
                "zotero_item_key": key,
                "source_id": terminal_by_index.get(index, {}).get("source_id", ""),
                "note_id": terminal_by_index.get(index, {}).get("note_id", ""),
                "note_path": terminal_by_index.get(index, {}).get("note_path", ""),
                "terminal_status": terminal_by_index.get(index, {}).get("terminal_status", ""),
            }
            for index, key in enumerate(zotero_keys)
        ],
    }
    path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{source_set_id}.yml"
    write_yaml(path, payload)
    payload["path"] = str(path)
    return payload


def update_source_set_map(workspace: Path, source_set: Mapping[str, Any], clusters: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(source_set)
    payload["cluster_ids"] = sorted(str(row.get("cluster_id")) for row in clusters if row.get("cluster_id"))
    payload["gap_ids"] = sorted(str(row.get("gap_id")) for row in gaps if row.get("gap_id"))
    payload["updated_at"] = now_iso()
    payload.pop("path", None)
    path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{payload['source_set_id']}.yml"
    write_yaml(path, payload)
    payload["path"] = str(path)
    return payload


def write_source_index(workspace: Path, note_paths: Sequence[Path]) -> Path:
    rows = []
    for path in sorted(note_paths):
        note = read_note(path)
        front = note["frontmatter"]
        rows.append(
            f"- [[{path.stem}]] — {front.get('title', path.stem)} "
            f"(`{front.get('note_id', '')}`, `{front.get('note_status', '')}`)"
        )
    target = workspace / "02_source_memory" / "indexes" / "INDEX.md"
    atomic_write_text(target, "# Source Index\n\n" + ("\n".join(rows) if rows else "No validated atomic notes yet.") + "\n")
    return target


def _item_key(item: Mapping[str, Any]) -> str:
    data = item.get("data", item)
    if not isinstance(data, Mapping):
        data = {}
    return str(item.get("key") or data.get("key") or "")


def _zotero_relation_type(predicate: str) -> str:
    normalized = predicate.casefold()
    if "isreferencedby" in normalized or "cited_by" in normalized:
        return "cited_by"
    if "references" in normalized or "cites" in normalized:
        return "cites"
    if "replaces" in normalized or "extends" in normalized:
        return "extends"
    return "zotero_related"
