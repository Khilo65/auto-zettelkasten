from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .files import atomic_write_text, now_iso, read_yaml, sha256_text, slugify, write_yaml
from .notes import read_note

SOURCE_CATALOGUE_SCHEMA_VERSION = "1"
SOURCE_CATALOGUE_SHARD_MAX_CHARS = 36_000

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
    snapshot_id: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    if collection_key:
        suffix = f"zotero-{slugify(collection_key)}"
    elif scope in {"library", "selected"}:
        suffix = "zotero-library"
    else:
        suffix = f"run-{slugify(run_id)}"
    source_set_alias = source_set_id or f"source-set-{suffix}"
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
            "semantic_note_hash": next(
                (
                    str(note.get("semantic_note_hash") or note.get("note_hash") or "")
                    for note in note_rows
                    if str(note.get("zotero_item_key") or "") == key
                ),
                "",
            ),
        }
        for index, key in enumerate(zotero_keys)
    ]
    dependency_hash = sha256_text(json.dumps(dependency_payload, sort_keys=True, ensure_ascii=False, default=str))
    source_set_id = snapshot_id or f"{source_set_alias}-{dependency_hash[:12]}"
    status_counts = {
        status: sum(1 for row in terminal_rows if str(row.get("terminal_status", "")) == status)
        for status in ("validated_note", "limited_note", "exhausted", "partial", "pending")
    }
    payload = {
        "source_set_id": source_set_id,
        "source_set_alias": source_set_alias,
        "source_set_type": source_set_type
        or ("zotero_collection" if collection_key else ("zotero_library" if scope in {"library", "selected"} else "auto_zettelkasten_run")),
        "scope": scope,
        "run_id": run_id,
        "zotero_collection_key": collection_key or "",
        "collection_name": str(collection_name or "").strip(),
        "upstream_scope": {"kind": f"zotero_{scope}", "id": collection_key or run_id},
        "inventory_count": len(items),
        "terminal_count": status_counts["validated_note"] + status_counts["limited_note"] + status_counts["exhausted"],
        "validated_note_count": status_counts["validated_note"],
        "limited_note_count": status_counts["limited_note"],
        "exhausted_count": status_counts["exhausted"],
        "partial_count": status_counts["partial"],
        "pending_count": status_counts["pending"],
        "source_ids": [str(row.get("source_id")) for row in note_rows],
        "note_ids": [str(row.get("note_id")) for row in note_rows],
        "note_paths": [str(row.get("note_path")) for row in note_rows],
        "obsidian_links": [f"[[{Path(str(row.get('note_path'))).stem}]]" for row in note_rows],
        "zotero_item_keys": zotero_keys,
        "original_zotero_tags": original_tags,
        "normalized_tags": normalized_tags,
        "cluster_ids": sorted(set(cluster_ids)),
        "gap_ids": sorted(set(gap_ids)),
        "dependency_hash": dependency_hash,
        "frozen_inventory": True,
        "refresh_requires_new_run": True,
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
    latest_path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{source_set_alias}.yml"
    write_yaml(latest_path, {**payload, "latest_snapshot_id": source_set_id, "latest_snapshot_path": str(path)})
    payload["path"] = str(path)
    payload["latest_path"] = str(latest_path)
    return payload


def update_source_set_map(workspace: Path, source_set: Mapping[str, Any], clusters: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(source_set)
    payload["cluster_ids"] = sorted(str(row.get("cluster_id")) for row in clusters if row.get("cluster_id"))
    payload["gap_ids"] = sorted(str(row.get("gap_id")) for row in gaps if row.get("gap_id"))
    payload["updated_at"] = now_iso()
    payload.pop("path", None)
    payload.pop("latest_path", None)
    path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{payload['source_set_id']}.yml"
    write_yaml(path, payload)
    alias = str(payload.get("source_set_alias") or payload["source_set_id"])
    latest_path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{alias}.yml"
    write_yaml(latest_path, {**payload, "latest_snapshot_id": payload["source_set_id"], "latest_snapshot_path": str(path)})
    payload["path"] = str(path)
    payload["latest_path"] = str(latest_path)
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
    atomic_write_text(target, "# Source Index\n\n" + ("\n".join(rows) if rows else "No source notes yet.") + "\n")
    return target


def build_source_catalogue(
    workspace: Path,
    profiles: Sequence[Any] | Mapping[str, Any],
    note_rows: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Project compact, stable source and literature indexes without provider work."""

    profile_rows = _catalogue_profile_rows(profiles)
    notes_by_source = {
        str(row.get("source_id")): dict(row)
        for row in note_rows
        if row.get("source_id")
    }
    notes_by_note = {
        str(row.get("note_id")): dict(row)
        for row in note_rows
        if row.get("note_id")
    }

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for profile in sorted(
        profile_rows,
        key=lambda row: (str(row.get("source_id", "")), str(row.get("note_id", ""))),
    ):
        note = _catalogue_note_summary(
            workspace,
            notes_by_source.get(str(profile.get("source_id") or ""))
            or notes_by_note.get(str(profile.get("note_id") or ""))
            or {},
        )
        source_id = str(profile.get("source_id") or note.get("source_id") or "")
        note_id = str(profile.get("note_id") or note.get("note_id") or "")
        if not source_id and not note_id:
            continue
        identity = (source_id, note_id)
        if identity in seen:
            continue
        seen.add(identity)
        entries.append(_catalogue_entry(profile, note))
    for note in sorted(
        note_rows,
        key=lambda row: (str(row.get("source_id", "")), str(row.get("note_id", ""))),
    ):
        identity = (str(note.get("source_id") or ""), str(note.get("note_id") or ""))
        if identity == ("", "") or identity in seen:
            continue
        seen.add(identity)
        entries.append(_catalogue_entry({}, _catalogue_note_summary(workspace, note)))

    literatures = _catalogue_literatures(workspace, entries)
    relationship_ids_by_source = _catalogue_relationship_ids(workspace)
    cluster_ids_by_source: dict[str, set[str]] = defaultdict(set)
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id:
            continue
        for source_id in (
            cluster.get("source_ids")
            or cluster.get("core_source_ids")
            or []
        ):
            cluster_ids_by_source[str(source_id)].add(cluster_id)
    for entry in entries:
        entry["cluster_ids"] = sorted(cluster_ids_by_source.get(entry["source_id"], set()))
        entry["relationship_ids"] = sorted(
            relationship_ids_by_source.get(entry["source_id"], set())
        )
        entry["literature_ids"] = sorted(
            literature["literature_id"]
            for literature in literatures
            if entry["source_id"] in literature["source_ids"]
            or entry["note_id"] in literature["note_ids"]
        )

    entry_by_identity = {
        (entry["source_id"], entry["note_id"]): entry for entry in entries
    }
    shard_specs: list[dict[str, Any]] = []
    for literature in literatures:
        literature_entries = [
            entry
            for identity, entry in entry_by_identity.items()
            if identity[0] in literature["source_ids"]
            or identity[1] in literature["note_ids"]
        ]
        literature_entries.sort(
            key=lambda row: (
                str(row["author"]).casefold(),
                str(row["year"]),
                str(row["title"]).casefold(),
                str(row["source_id"]),
            )
        )
        chunks = _meaningful_catalogue_chunks(literature_entries)
        part_count = len(chunks)
        for index, (group_label, chunk_entries) in enumerate(chunks, start=1):
            suffix = (
                f"-part-{index:02d}-{slugify(group_label, 'sources')}"
                if part_count > 1
                else ""
            )
            stem = f"{literature['literature_id']}{suffix}"
            display_title = str(literature["title"])
            heading = (
                display_title
                if part_count == 1
                else f"{display_title} — {group_label}"
            )
            chunk = [_render_catalogue_source(row) for row in chunk_entries]
            body = (
                f"# {heading}\n\n"
                f"Scope: {literature['scope']}\n\n"
                + ("".join(chunk) if chunk else "No source profiles yet.\n")
            )
            revision_hash = sha256_text(body)
            text = f"{body}\nCatalogue revision: `{revision_hash}`\n"
            shard_specs.append(
                {
                    "literature_id": literature["literature_id"],
                    "shard_id": stem,
                    "title": heading,
                    "scope": str(literature["scope"]),
                    "path": f"02_source_memory/indexes/by_literature/{stem}.md",
                    "source_count": len(chunk),
                    "source_ids": [
                        str(row.get("source_id") or "")
                        for row in chunk_entries
                        if row.get("source_id")
                    ],
                    "note_ids": [
                        str(row.get("note_id") or "")
                        for row in chunk_entries
                        if row.get("note_id")
                    ],
                    "revision_hash": revision_hash,
                    "text": text,
                }
            )

    compact_clusters = _compact_cluster_catalogue(clusters)
    compact_literatures = [
        {
            "literature_id": row["literature_id"],
            "title": row["title"],
            "scope": row["scope"],
            "source_count": sum(
                1
                for entry in entries
                if row["literature_id"] in entry["literature_ids"]
            ),
        }
        for row in literatures
    ]
    semantic_payload = {
        "schema_version": SOURCE_CATALOGUE_SCHEMA_VERSION,
        "literatures": compact_literatures,
        "shards": [
            {
                key: row[key]
                for key in (
                    "literature_id",
                    "shard_id",
                    "title",
                    "scope",
                    "path",
                    "source_count",
                    "source_ids",
                    "note_ids",
                    "revision_hash",
                )
            }
            for row in shard_specs
        ],
        "clusters": compact_clusters,
        "sources": entries,
    }
    revision_hash = sha256_text(
        json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    routing_payload = {
        **semantic_payload,
        "sources": [
            {
                key: value
                for key, value in entry.items()
                if key != "relationship_ids"
            }
            for entry in entries
        ],
    }
    routing_revision_hash = sha256_text(
        json.dumps(
            routing_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    catalogue_payload = {
        **semantic_payload,
        "revision_hash": revision_hash,
        "routing_revision_hash": routing_revision_hash,
    }
    catalogue_text = json.dumps(catalogue_payload, indent=2, ensure_ascii=False) + "\n"

    master_lines = ["# Source Index", "", f"Catalogue revision: `{revision_hash}`", ""]
    for literature in compact_literatures:
        master_lines.extend(
            [
                f"## {literature['title']} ({literature['source_count']})",
                "",
                str(literature["scope"]),
                "",
            ]
        )
        for shard in shard_specs:
            if shard["literature_id"] == literature["literature_id"]:
                link = Path(str(shard["path"])).relative_to("02_source_memory/indexes").with_suffix("")
                master_lines.append(
                    f"- [[{link.as_posix()}|{shard['title']}]] — {shard['source_count']} sources"
                )
        cluster_ids = sorted(
            {
                cluster_id
                for entry in entries
                if literature["literature_id"] in entry["literature_ids"]
                for cluster_id in entry["cluster_ids"]
            }
        )
        if cluster_ids:
            master_lines.append(
                "- Clusters: [[CLUSTERS|Cluster Catalogue]] — "
                + ", ".join(f"`{value}`" for value in cluster_ids)
            )
        master_lines.append("")
    master_text = "\n".join(master_lines).rstrip() + "\n"
    cluster_semantic = {
        "schema_version": SOURCE_CATALOGUE_SCHEMA_VERSION,
        "clusters": compact_clusters,
    }
    cluster_revision = sha256_text(
        json.dumps(
            cluster_semantic,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    cluster_catalogue_text = (
        json.dumps(
            {**cluster_semantic, "revision_hash": cluster_revision},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    cluster_lines = [
        "# Cluster Catalogue",
        "",
        f"Catalogue revision: `{cluster_revision}`",
        "",
    ]
    for cluster in compact_clusters:
        cluster_lines.extend(
            [
                f"## {cluster['title'] or cluster['cluster_id']}",
                "",
                str(cluster["shared_question"] or cluster["bounded_scope"] or "Scope not specified."),
                "",
                f"- Cluster ID: `{cluster['cluster_id']}`",
                f"- Core sources: {', '.join(f'`{value}`' for value in cluster['core_source_ids']) or 'none'}",
                f"- Neighbors: {', '.join(f'`{value}`' for value in cluster['neighboring_cluster_ids']) or 'none'}",
                f"- Refresh pending: {'yes' if cluster['refresh_pending'] else 'no'}",
                "",
            ]
        )
    cluster_index_text = "\n".join(cluster_lines).rstrip() + "\n"

    changed_paths: list[str] = []
    for path, text in [
        *(
            (workspace / str(shard["path"]), str(shard["text"]))
            for shard in shard_specs
        ),
        (workspace / "02_source_memory" / "indexes" / "source_catalogue.yml", catalogue_text),
        (workspace / "02_source_memory" / "indexes" / "INDEX.md", master_text),
        (
            workspace / "02_source_memory" / "indexes" / "cluster_catalogue.yml",
            cluster_catalogue_text,
        ),
        (
            workspace / "02_source_memory" / "indexes" / "CLUSTERS.md",
            cluster_index_text,
        ),
    ]:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write_text(path, text)
            changed_paths.append(str(path))
    shard_root = workspace / "02_source_memory" / "indexes" / "by_literature"
    expected_shards = {
        (workspace / str(row["path"])).resolve() for row in shard_specs
    }
    for stale in sorted(shard_root.glob("*.md")):
        if stale.resolve() not in expected_shards:
            stale.unlink()
            changed_paths.append(str(stale))

    return {
        "catalogue_path": str(workspace / "02_source_memory" / "indexes" / "source_catalogue.yml"),
        "master_index_path": str(workspace / "02_source_memory" / "indexes" / "INDEX.md"),
        "cluster_catalogue_path": str(
            workspace / "02_source_memory" / "indexes" / "cluster_catalogue.yml"
        ),
        "cluster_index_path": str(
            workspace / "02_source_memory" / "indexes" / "CLUSTERS.md"
        ),
        "cluster_revision_hash": cluster_revision,
        "shard_paths": [str(workspace / str(row["path"])) for row in shard_specs],
        "revision_hash": revision_hash,
        "routing_revision_hash": routing_revision_hash,
        "source_count": len(entries),
        "literature_count": len(compact_literatures),
        "shard_count": len(shard_specs),
        "changed_paths": changed_paths,
    }


def _catalogue_profile_rows(profiles: Sequence[Any] | Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(profiles, Mapping):
        values: Sequence[Any] = (
            [profiles]
            if "source_id" in profiles or "note_id" in profiles
            else list(profiles.values())
        )
    else:
        values = profiles
    rows: list[dict[str, Any]] = []
    for profile in values:
        if isinstance(profile, Mapping):
            rows.append(dict(profile))
        elif callable(getattr(profile, "to_dict", None)):
            rows.append(dict(profile.to_dict()))
        elif is_dataclass(profile):
            rows.append(asdict(profile))
        else:
            raise TypeError("profiles must contain mappings or dataclasses")
    return rows


def _catalogue_relationship_ids(workspace: Path) -> dict[str, set[str]]:
    payload = read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    rows = payload.get("links", []) if isinstance(payload, Mapping) else []
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if not isinstance(row, Mapping) or not bool(row.get("active", True)):
            continue
        try:
            confidence = float(row.get("confidence", 1))
        except (TypeError, ValueError):
            confidence = 0
        relation_id = str(row.get("relation_id") or row.get("link_id") or "")
        if not relation_id or confidence < 0.55:
            continue
        for source_id in (
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or ""),
        ):
            if source_id:
                result[source_id].add(relation_id)
    return result


def _catalogue_note_summary(
    workspace: Path, note: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(note)
    if result.get("thesis") and result.get("method"):
        return result
    note_path = workspace / str(result.get("note_path") or "")
    if not note_path.is_file():
        return result
    try:
        body = str(read_note(note_path).get("body") or "")
    except (OSError, ValueError):
        return result
    for field, heading in (
        ("thesis", "Thesis"),
        ("method", "Method and Research Design"),
    ):
        if result.get(field):
            continue
        match = re.search(
            rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)",
            body,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match:
            result[field] = match.group(1).strip()
    return result


def _compact_cluster_catalogue(
    clusters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id:
            continue
        rows.append(
            {
                "cluster_id": cluster_id,
                "title": _compact_catalogue_text(
                    cluster.get("display_label")
                    or cluster.get("label")
                    or cluster.get("semantic_identity"),
                    180,
                ),
                "shared_question": _compact_catalogue_text(
                    cluster.get("display_question")
                    or cluster.get("shared_question"),
                    320,
                ),
                "bounded_scope": _compact_catalogue_text(
                    cluster.get("bounded_scope")
                    or cluster.get("bounded_object"),
                    320,
                ),
                "central_debate": _compact_catalogue_text(
                    cluster.get("central_debate")
                    or cluster.get("debate_state")
                    or cluster.get("relationship_among_findings"),
                    360,
                ),
                "core_source_ids": sorted(
                    str(value)
                    for value in (
                        cluster.get("core_source_ids")
                        or cluster.get("source_ids")
                        or []
                    )
                    if str(value)
                ),
                "neighboring_cluster_ids": sorted(
                    str(value)
                    for value in (
                        cluster.get("neighboring_cluster_ids")
                        or cluster.get("related_cluster_ids")
                        or []
                    )
                    if str(value)
                ),
                "refresh_pending": bool(cluster.get("refresh_pending")),
            }
        )
    return sorted(rows, key=lambda row: row["cluster_id"])


def _meaningful_catalogue_chunks(
    entries: Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    rendered = [_render_catalogue_source(row) for row in entries]
    if sum(len(value) for value in rendered) <= SOURCE_CATALOGUE_SHARD_MAX_CHARS - 512:
        return [("all sources", list(entries))]
    facet_counts = Counter(
        str((entry.get("facets", []) or ["general"])[0])
        for entry in entries
    )
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in entries:
        facets = [str(value) for value in entry.get("facets", []) or [] if str(value)]
        label = facets[0] if facets and facet_counts[facets[0]] >= 3 else "other topics"
        groups[label].append(entry)
    chunks: list[tuple[str, list[Mapping[str, Any]]]] = []
    for label in sorted(groups, key=str.casefold):
        part: list[Mapping[str, Any]] = []
        part_number = 1
        part_chars = 0
        for entry in groups[label]:
            text = _render_catalogue_source(entry)
            if (
                part
                and part_chars + len(text)
                > SOURCE_CATALOGUE_SHARD_MAX_CHARS - 512
            ):
                chunks.append(
                    (
                        label if part_number == 1 else f"{label} {part_number}",
                        part,
                    )
                )
                part = []
                part_chars = 0
                part_number += 1
            part.append(entry)
            part_chars += len(text)
        chunks.append(
            (
                label if part_number == 1 else f"{label} {part_number}",
                part,
            )
        )
    return chunks


def _catalogue_entry(profile: Mapping[str, Any], note: Mapping[str, Any]) -> dict[str, Any]:
    creators = note.get("creators", []) or []
    author_names = []
    for creator in creators if isinstance(creators, Sequence) and not isinstance(creators, (str, bytes)) else []:
        if isinstance(creator, Mapping):
            name = str(creator.get("lastName") or creator.get("name") or "").strip()
        else:
            name = str(creator).strip()
        if name:
            author_names.append(name)
    author = ", ".join(author_names[:2]) or "Unknown"
    year_match = re.search(r"(?:19|20)\d{2}", str(note.get("date") or ""))
    findings = profile.get("findings", []) or []
    first_claim = ""
    if findings:
        first = findings[0]
        first_claim = (
            str(first.get("claim") or "")
            if isinstance(first, Mapping)
            else str(getattr(first, "claim", "") or "")
        )
    thesis = _compact_catalogue_text(
        note.get("thesis") or first_claim or next(iter(profile.get("research_questions", []) or []), ""),
        360,
    )
    methods = profile.get("methods", []) or []
    method = _compact_catalogue_text(
        note.get("method") or (methods[0] if methods else "Not specified"),
        220,
    )
    facets: list[str] = []
    for field in ("concepts", "mechanisms", "outcomes", "cases"):
        for value in profile.get(field, []) or []:
            cleaned = _compact_catalogue_text(value, 80)
            if cleaned and cleaned.casefold() not in {facet.casefold() for facet in facets}:
                facets.append(cleaned)
            if len(facets) == 3:
                break
        if len(facets) == 3:
            break
    note_path = str(note.get("note_path") or "")
    link_target = (
        Path(note_path).stem
        if note_path
        else note.get("note_id") or profile.get("note_id") or profile.get("source_id")
    )
    note_link = f"[[{link_target}]]"
    profile_hash = (
        sha256_text(
            json.dumps(
                dict(profile),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        if profile
        else ""
    )
    return {
        "source_id": str(profile.get("source_id") or note.get("source_id") or ""),
        "note_id": str(profile.get("note_id") or note.get("note_id") or ""),
        "title": _compact_catalogue_text(note.get("title") or "Untitled", 240),
        "author": author,
        "year": year_match.group(0) if year_match else "n.d.",
        "thesis": thesis or "Not specified.",
        "method": method or "Not specified.",
        "facets": facets,
        "note_link": note_link,
        "profile_hash": profile_hash,
    }


def _compact_catalogue_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\n-", " ")).strip(" -*")
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0] if text else ""
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def _catalogue_literatures(
    workspace: Path, entries: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    selected: dict[str, tuple[int, dict[str, Any]]] = {}
    source_set_dir = workspace / "02_source_memory" / "indexes" / "source_sets"
    for path in sorted(source_set_dir.glob("*.yml")):
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping) or not (
            payload.get("zotero_collection_key")
            or payload.get("source_set_type") == "zotero_collection"
        ):
            continue
        alias = str(payload.get("source_set_alias") or payload.get("source_set_id") or path.stem)
        score = int(bool(payload.get("latest_snapshot_id"))) + int(path.stem == alias)
        candidate = dict(payload)
        if alias not in selected or score > selected[alias][0]:
            selected[alias] = (score, candidate)

    literatures: list[dict[str, Any]] = []
    covered_sources: set[str] = set()
    covered_notes: set[str] = set()
    for alias, (_, payload) in sorted(selected.items()):
        source_ids = {str(value) for value in payload.get("source_ids", []) or [] if value}
        note_ids = {str(value) for value in payload.get("note_ids", []) or [] if value}
        if not source_ids and not note_ids:
            continue
        title = str(payload.get("collection_name") or alias).strip()
        literature_id = slugify(alias, "library")
        literatures.append(
            {
                "literature_id": literature_id,
                "title": title,
                "scope": f"Sources in the {title} collection.",
                "source_ids": source_ids,
                "note_ids": note_ids,
            }
        )
        covered_sources.update(source_ids)
        covered_notes.update(note_ids)

    unassigned_sources = {
        str(entry.get("source_id") or "")
        for entry in entries
        if entry.get("source_id") and entry.get("source_id") not in covered_sources
    }
    unassigned_notes = {
        str(entry.get("note_id") or "")
        for entry in entries
        if entry.get("note_id") and entry.get("note_id") not in covered_notes
        and entry.get("source_id") not in covered_sources
    }
    if unassigned_sources or unassigned_notes or not literatures:
        literatures.append(
            {
                "literature_id": "library",
                "title": "Library",
                "scope": "Profiled sources without a current Zotero collection shard.",
                "source_ids": unassigned_sources
                or {str(entry.get("source_id") or "") for entry in entries if entry.get("source_id")},
                "note_ids": unassigned_notes
                or {str(entry.get("note_id") or "") for entry in entries if entry.get("note_id")},
            }
        )
    return literatures


def _render_catalogue_source(entry: Mapping[str, Any]) -> str:
    facets = ", ".join(str(value) for value in entry.get("facets", []) or []) or "none"
    return (
        f"- **{entry['author']} {entry['year']} — {entry['title']}** "
        f"({entry['note_link']}; `{entry['source_id']}`; `{entry['note_id']}`)\n"
        f"  - Thesis: {entry['thesis']}\n"
        f"  - Method: {entry['method']}\n"
        f"  - Facets: {facets}\n"
    )


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
