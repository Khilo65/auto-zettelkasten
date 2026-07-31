from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import unicodedata

from .files import atomic_write_text, now_iso, read_yaml, sha256_text, slugify, write_yaml
from .notes import read_note

SOURCE_CATALOGUE_SCHEMA_VERSION = "6"
SOURCE_CATALOGUE_SHARD_MAX_CHARS = 36_000
SOURCE_CATALOGUE_ROUTING_CARD_MAX_CHARS = 1_500

_VIRTUAL_TOPIC_FACET_ORDER = (
    "mechanism",
    "outcome",
    "concept",
    "subject",
    "case",
    "population",
    "dataset",
    "method",
    "period",
)
_GENERIC_VIRTUAL_TOPICS = {
    "analysis",
    "article",
    "conflict",
    "data",
    "document",
    "general",
    "none",
    "other topics",
    "research",
    "study",
}

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
    cluster_ids: Sequence[str] | None = None,
    gap_ids: Sequence[str] | None = None,
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
    path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{source_set_id}.yml"
    existing = read_yaml(path, {}) or {}
    status_counts = {
        status: sum(
            1
            for row in terminal_rows
            if (
                "parked_for_review"
                if str(row.get("terminal_status", "")) == "exhausted"
                else str(row.get("terminal_status", ""))
            )
            == status
        )
        for status in (
            "validated_note",
            "limited_note",
            "duplicate_alias",
            "parked_for_review",
            "partial",
            "pending",
        )
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
        "terminal_count": status_counts["validated_note"]
        + status_counts["limited_note"]
        + status_counts["duplicate_alias"]
        + status_counts["parked_for_review"],
        "validated_note_count": status_counts["validated_note"],
        "limited_note_count": status_counts["limited_note"],
        "duplicate_alias_count": status_counts["duplicate_alias"],
        "parked_for_review_count": status_counts["parked_for_review"],
        "partial_count": status_counts["partial"],
        "pending_count": status_counts["pending"],
        "source_ids": [str(row.get("source_id")) for row in note_rows],
        "note_ids": [str(row.get("note_id")) for row in note_rows],
        "note_paths": [str(row.get("note_path")) for row in note_rows],
        "obsidian_links": [f"[[{Path(str(row.get('note_path'))).stem}]]" for row in note_rows],
        "zotero_item_keys": zotero_keys,
        "original_zotero_tags": original_tags,
        "normalized_tags": normalized_tags,
        "cluster_ids": sorted(
            set(
                cluster_ids
                if cluster_ids is not None
                else existing.get("cluster_ids", []) or []
            )
        ),
        "gap_ids": sorted(
            set(
                gap_ids
                if gap_ids is not None
                else existing.get("gap_ids", []) or []
            )
        ),
        "dependency_hash": dependency_hash,
        "frozen_inventory": True,
        "refresh_requires_new_run": True,
        "stale": False,
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
    existing_without_timestamp = dict(existing)
    existing_without_timestamp.pop("updated_at", None)
    payload["updated_at"] = (
        str(existing.get("updated_at") or "")
        if existing_without_timestamp == payload
        else now_iso()
    )
    if existing != payload:
        write_yaml(path, payload)
    latest_path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{source_set_alias}.yml"
    latest_payload = {
        **payload,
        "latest_snapshot_id": source_set_id,
        "latest_snapshot_path": str(path),
    }
    if (read_yaml(latest_path, {}) or {}) != latest_payload:
        write_yaml(latest_path, latest_payload)
    payload["path"] = str(path)
    payload["latest_path"] = str(latest_path)
    return payload


def update_source_set_map(workspace: Path, source_set: Mapping[str, Any], clusters: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(source_set)
    payload["cluster_ids"] = sorted(str(row.get("cluster_id")) for row in clusters if row.get("cluster_id"))
    payload["gap_ids"] = sorted(str(row.get("gap_id")) for row in gaps if row.get("gap_id"))
    payload.pop("path", None)
    payload.pop("latest_path", None)
    path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{payload['source_set_id']}.yml"
    existing = read_yaml(path, {}) or {}
    existing_without_timestamp = dict(existing)
    existing_without_timestamp.pop("updated_at", None)
    payload_without_timestamp = dict(payload)
    payload_without_timestamp.pop("updated_at", None)
    payload["updated_at"] = (
        str(existing.get("updated_at") or "")
        if existing_without_timestamp == payload_without_timestamp
        else now_iso()
    )
    if existing != payload:
        write_yaml(path, payload)
    alias = str(payload.get("source_set_alias") or payload["source_set_id"])
    latest_path = workspace / "02_source_memory" / "indexes" / "source_sets" / f"{alias}.yml"
    latest_payload = {
        **payload,
        "latest_snapshot_id": payload["source_set_id"],
        "latest_snapshot_path": str(path),
    }
    if (read_yaml(latest_path, {}) or {}) != latest_payload:
        write_yaml(latest_path, latest_payload)
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
    collection_snapshot: Mapping[str, Any] | None = None,
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
    relationship_rows = _catalogue_relationship_rows(workspace)
    relationship_ids_by_source = _catalogue_relationship_ids(relationship_rows)
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
        entry["collections"] = sorted(
            str(literature["title"])
            for literature in literatures
            if literature["literature_id"] in entry["literature_ids"]
        )
    snapshot_collections = {
        str(row.get("key")): row
        for row in (collection_snapshot or {}).get("collections", []) or []
        if isinstance(row, Mapping) and row.get("key")
    }
    snapshot_memberships = {
        str(row.get("key")).upper(): sorted(
            str(value)
            for value in row.get("collection_keys", []) or []
            if str(value)
        )
        for row in (collection_snapshot or {}).get("items", []) or []
        if isinstance(row, Mapping) and row.get("key")
    }
    snapshot_items = {
        str(row.get("key")).upper(): row
        for row in (collection_snapshot or {}).get("items", []) or []
        if isinstance(row, Mapping) and row.get("key")
    }
    if collection_snapshot:
        available_zotero_keys = set(snapshot_memberships)
        for entry in entries:
            zotero_key = str(entry.get("zotero_key") or "").upper()
            collection_keys = snapshot_memberships.get(
                zotero_key,
                [],
            )
            entry["zotero_availability"] = (
                "available"
                if not zotero_key or zotero_key in available_zotero_keys
                else "unavailable"
            )
            entry["collection_keys"] = collection_keys
            entry["collections"] = sorted(
                str(snapshot_collections[key].get("name") or key)
                for key in collection_keys
                if key in snapshot_collections
            )
    for entry in entries:
        _refresh_catalogue_sections(
            entry,
            snapshot_items.get(str(entry.get("zotero_key") or "").upper(), {}),
        )

    virtual_projection = _virtual_catalogue_projection(entries)

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
            routing_card = _catalogue_shard_routing_card(
                shard_id=stem,
                title=heading,
                scope=str(literature["scope"]),
                entries=chunk_entries,
            )
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
                    "routing_card": routing_card,
                    "revision_hash": revision_hash,
                    "text": text,
                }
            )

    compact_clusters = _compact_cluster_catalogue(clusters)
    collection_projection = _collection_catalogue_projection(
        entries,
        collection_snapshot,
        relationship_rows,
    )
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
                    "routing_card",
                    "revision_hash",
                )
            }
            for row in shard_specs
        ],
        "clusters": compact_clusters,
        "collections": collection_projection["catalogue"],
        "virtual_topics": virtual_projection["catalogue"],
        "virtual_shards": [
            {
                key: row[key]
                for key in (
                    "topic_id",
                    "shard_id",
                    "title",
                    "path",
                    "source_count",
                    "source_ids",
                    "note_ids",
                    "routing_card",
                    "revision_hash",
                )
            }
            for row in virtual_projection["shards"]
        ],
        "collection_snapshot_fingerprint": str(
            (collection_snapshot or {}).get("fingerprint") or ""
        ),
        "sources": entries,
    }
    revision_hash = sha256_text(
        json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    routing_payload = {
        "schema_version": SOURCE_CATALOGUE_SCHEMA_VERSION,
        "literatures": compact_literatures,
        "shards": semantic_payload["shards"],
        "collections": [
            {
                key: row.get(key)
                for key in (
                    "key",
                    "name",
                    "parent_key",
                    "child_keys",
                    "direct_source_count",
                    "descendant_source_count",
                    "direct_source_ids",
                )
            }
            | {
                "routing_card": {
                    key: value
                    for key, value in dict(row.get("routing_card") or {}).items()
                    if key
                    not in {
                        "active_cluster_ids",
                        "cross_collection_relationship_count",
                        "revision_hash",
                    }
                }
            }
            for row in collection_projection["catalogue"]
        ],
        "virtual_topics": virtual_projection["catalogue"],
        "virtual_shards": semantic_payload["virtual_shards"],
        "collection_snapshot_fingerprint": semantic_payload[
            "collection_snapshot_fingerprint"
        ],
        "sources": [
            {
                "source_id": entry["source_id"],
                "note_id": entry["note_id"],
                "identity": entry["identity"],
                "navigation": entry["navigation"],
                "literature_ids": entry["literature_ids"],
                "collection_keys": entry.get("collection_keys", []),
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
    if collection_projection["catalogue"]:
        master_lines.extend(["## Zotero Collections", ""])
        master_lines.extend(collection_projection["tree_lines"])
        master_lines.extend(["", "### Collection routing cards", ""])
        for collection in collection_projection["catalogue"]:
            card = dict(collection.get("routing_card") or {})
            master_lines.append(
                f"- `{collection['key']}` — {card.get('scope') or collection['name']}; "
                f"{int(card.get('direct_source_count', 0) or 0)} direct, "
                f"{int(card.get('descendant_source_count', 0) or 0)} descendant"
            )
        master_lines.append("")
    if virtual_projection["shards"]:
        master_lines.extend(
            [
                "## Virtual topic indexes",
                "",
                "- [[by_topic/INDEX|Browse context-bounded topic indexes]]",
                "",
            ]
        )
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
        *(
            (workspace / str(spec["path"]), str(spec["text"]))
            for spec in collection_projection["files"]
        ),
        *(
            (workspace / str(spec["path"]), str(spec["text"]))
            for spec in virtual_projection["files"]
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
    virtual_root = workspace / "02_source_memory" / "indexes" / "by_topic"
    expected_virtual_files = {
        (workspace / str(row["path"])).resolve()
        for row in virtual_projection["files"]
    }
    for stale in sorted(virtual_root.glob("*.md")):
        if stale.resolve() not in expected_virtual_files:
            stale.unlink()
            changed_paths.append(str(stale))
    if collection_snapshot is not None:
        _prune_stale_collection_indexes(
            workspace,
            {
                (workspace / str(spec["path"])).resolve()
                for spec in collection_projection["files"]
            },
            changed_paths,
        )

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
        "virtual_index_path": str(
            workspace / "02_source_memory" / "indexes" / "by_topic" / "INDEX.md"
        ),
        "virtual_shard_paths": [
            str(workspace / str(row["path"]))
            for row in virtual_projection["shards"]
        ],
        "collection_index_paths": [
            str(workspace / str(row["path"]))
            for row in collection_projection["files"]
            if str(row["path"]).endswith("/INDEX.md")
        ],
        "collection_shard_paths": [
            str(workspace / str(row["path"]))
            for row in collection_projection["files"]
            if not str(row["path"]).endswith("/INDEX.md")
        ],
        "revision_hash": revision_hash,
        "routing_revision_hash": routing_revision_hash,
        "source_count": len(entries),
        "literature_count": len(compact_literatures),
        "shard_count": len(shard_specs),
        "virtual_shard_count": len(virtual_projection["shards"]),
        "collection_count": len(collection_projection["catalogue"]),
        "changed_paths": changed_paths,
    }


def _collection_catalogue_projection(
    entries: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
    relationships: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if not snapshot:
        return {"catalogue": [], "tree_lines": [], "files": []}
    collections = {
        str(row.get("key")): dict(row)
        for row in snapshot.get("collections", []) or []
        if isinstance(row, Mapping) and row.get("key")
    }
    if not collections:
        return {"catalogue": [], "tree_lines": [], "files": []}

    directories = {
        key: _collection_directory_name(key)
        for key in collections
    }
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for key, row in collections.items():
        parent = str(row.get("parent_key") or "")
        if parent and parent in collections and parent != key:
            children[parent].append(key)
        else:
            roots.append(key)
    for values in children.values():
        values.sort(key=lambda key: (str(collections[key].get("name") or "").casefold(), key))
    roots.sort(key=lambda key: (str(collections[key].get("name") or "").casefold(), key))

    entries_by_zotero_key = {
        str(entry.get("zotero_key") or "").upper(): dict(entry)
        for entry in entries
        if entry.get("zotero_key")
    }
    direct_item_keys: dict[str, set[str]] = defaultdict(set)
    for item in snapshot.get("items", []) or []:
        if not isinstance(item, Mapping) or not item.get("key"):
            continue
        item_key = str(item["key"]).upper()
        for collection_key in item.get("collection_keys", []) or []:
            key = str(collection_key)
            if key in collections:
                direct_item_keys[key].add(item_key)

    def nested_item_keys(collection_key: str, trail: frozenset[str] = frozenset()) -> set[str]:
        if collection_key in trail:
            return set()
        nested: set[str] = set()
        next_trail = trail | {collection_key}
        for child in children.get(collection_key, []):
            nested.update(direct_item_keys.get(child, set()))
            nested.update(nested_item_keys(child, next_trail))
        return nested

    files: list[dict[str, str]] = []
    catalogue: list[dict[str, Any]] = []
    for key in sorted(collections):
        row = collections[key]
        name = str(row.get("name") or key)
        direct_keys = sorted(direct_item_keys.get(key, set()))
        direct_entries = sorted(
            (
                entries_by_zotero_key[item_key]
                for item_key in direct_keys
                if item_key in entries_by_zotero_key
            ),
            key=lambda entry: (
                str(entry.get("author") or "").casefold(),
                str(entry.get("year") or ""),
                str(entry.get("title") or "").casefold(),
                str(entry.get("source_id") or ""),
            ),
        )
        descendant_keys = nested_item_keys(key)
        routing_keys = set(direct_keys) | descendant_keys
        routing_entries = sorted(
            (
                entries_by_zotero_key[item_key]
                for item_key in routing_keys
                if item_key in entries_by_zotero_key
            ),
            key=lambda entry: str(entry.get("source_id") or ""),
        )
        direct_source_ids = [
            str(entry.get("source_id") or entry.get("note_id") or "")
            for entry in direct_entries
        ]
        clusters = sorted(
            {
                str(cluster_id)
                for entry in direct_entries
                for cluster_id in entry.get("cluster_ids", []) or []
                if str(cluster_id)
            }
        )
        chunks = _meaningful_catalogue_chunks(direct_entries) if direct_entries else []
        shard_links: list[str] = []
        shard_revision_payload: list[str] = []
        relationship_links: list[str] = []
        relationship_revision_payload: list[str] = []
        collection_dir = directories[key]
        for number, (_label, chunk_entries) in enumerate(chunks, start=1):
            filename = f"sources-{number:03d}.md"
            shard_path = (
                f"02_source_memory/indexes/collections/{collection_dir}/{filename}"
            )
            rendered = "".join(_render_catalogue_source(entry) for entry in chunk_entries)
            rendered_or_empty = rendered or "No processed direct sources yet.\n"
            shard_body = (
                f"# {name} — Direct sources {number}\n\n"
                f"Collection key: `{key}`\n\n"
                f"{rendered_or_empty}"
            )
            shard_revision = sha256_text(shard_body)
            files.append(
                {
                    "path": shard_path,
                    "text": f"{shard_body}\nCatalogue revision: `{shard_revision}`\n",
                }
            )
            shard_links.append(
                f"- [[{Path(filename).stem}|Direct sources {number}]] — {len(chunk_entries)} sources"
            )
            shard_revision_payload.append(shard_revision)
        direct_source_set = set(direct_source_ids)
        entry_by_source = {
            str(entry.get("source_id") or ""): entry for entry in entries
        }
        known_source_ids = set(entry_by_source)
        view_relationships = sorted(
            (
                dict(relationship)
                for relationship in relationships
                if (
                    endpoints := {
                        str(relationship.get("source_id") or ""),
                        str(relationship.get("target_source_id") or ""),
                    }
                ).issubset(known_source_ids)
                and endpoints & direct_source_set
            ),
            key=lambda relationship: (
                str(relationship.get("source_id") or ""),
                str(relationship.get("target_source_id") or ""),
                str(relationship.get("relation_type") or ""),
                str(relationship.get("relation_id") or ""),
            ),
        )
        for number, start in enumerate(range(0, len(view_relationships), 100), start=1):
            relation_chunk = view_relationships[start : start + 100]
            filename = f"relationships-{number:03d}.md"
            relation_path = (
                f"02_source_memory/indexes/collections/{collection_dir}/{filename}"
            )
            lines = [
                f"# {name} — Graph connections {number}",
                "",
                f"Collection key: `{key}`",
                "",
            ]
            for relationship in relation_chunk:
                left_id = str(relationship.get("source_id") or "")
                right_id = str(relationship.get("target_source_id") or "")
                left = entry_by_source.get(left_id, {})
                right = entry_by_source.get(right_id, {})
                if not left or not right:
                    continue
                relation_type = str(
                    relationship.get("relation_type") or "related"
                ).replace("_", " ")
                scope_label = (
                    "within collection"
                    if left_id in direct_source_set and right_id in direct_source_set
                    else "cross-collection"
                )
                reason = " ".join(
                    str(relationship.get("reason") or "").split()
                )[:240]
                lines.append(
                    f"- {left['note_link']} **{relation_type}** "
                    f"{right['note_link']} — {scope_label}"
                    + (f"; {reason}" if reason else "")
                )
            relation_body = "\n".join(lines).rstrip() + "\n"
            relation_revision = sha256_text(relation_body)
            files.append(
                {
                    "path": relation_path,
                    "text": (
                        f"{relation_body}\n"
                        f"View revision: `{relation_revision}`\n"
                    ),
                }
            )
            relationship_links.append(
                f"- [[{Path(filename).stem}|Graph connections {number}]] "
                f"— {len(relation_chunk)} relationships"
            )
            relationship_revision_payload.append(relation_revision)

        status_counts = _collection_status_counts(direct_entries)
        missing_count = sum(
            1 for item_key in direct_keys if item_key not in entries_by_zotero_key
        )
        parent_key = str(row.get("parent_key") or "")
        routing_card = _collection_routing_card(
            key=key,
            name=name,
            parent_key=parent_key,
            child_keys=children.get(key, []),
            direct_source_count=len(direct_keys),
            descendant_source_count=len(descendant_keys),
            entries=routing_entries,
            cluster_ids=clusters,
            cross_collection_relationship_count=sum(
                1
                for relationship in view_relationships
                if not {
                    str(relationship.get("source_id") or ""),
                    str(relationship.get("target_source_id") or ""),
                }.issubset(direct_source_set)
            ),
        )
        semantic = {
            "key": key,
            "name": name,
            "parent_key": parent_key,
            "child_keys": children.get(key, []),
            "direct_source_count": len(direct_keys),
            "descendant_source_count": len(descendant_keys),
            "direct_source_ids": direct_source_ids,
            "missing_source_count": missing_count,
            "status_counts": status_counts,
            "cluster_ids": clusters,
            "shard_revisions": shard_revision_payload,
            "relationship_count": len(view_relationships),
            "cross_collection_relationship_count": sum(
                1
                for relationship in view_relationships
                if not {
                    str(relationship.get("source_id") or ""),
                    str(relationship.get("target_source_id") or ""),
                }.issubset(direct_source_set)
            ),
            "relationship_view_revisions": relationship_revision_payload,
            "routing_card": routing_card,
        }
        revision = sha256_text(
            json.dumps(
                semantic,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        index_lines = [
            f"# {name}",
            "",
            f"- Collection key: `{key}`",
            f"- Direct sources: {len(direct_keys)}",
            f"- Descendant sources: {len(descendant_keys)}",
            "- Membership: shards list direct members only; inherited members remain in child collections.",
            f"- Processed: {status_counts['processed']}",
            f"- Context-only: {status_counts['context_only']}",
            f"- Partial documents: {status_counts['partial']}",
            f"- Parked for review: {status_counts['parked_for_review']}",
            f"- Missing atomic notes: {missing_count}",
            f"- Routing scope: {routing_card['scope']}",
            f"- Dominant facets: {', '.join(routing_card['dominant_facets']) or 'none yet'}",
        ]
        if parent_key in collections:
            index_lines.append(
                f"- Parent: [[../{directories[parent_key]}/INDEX|{collections[parent_key].get('name') or parent_key}]]"
            )
        if children.get(key):
            index_lines.extend(["", "## Child collections", ""])
            index_lines.extend(
                f"- [[../{directories[child]}/INDEX|{collections[child].get('name') or child}]]"
                for child in children[key]
            )
        index_lines.extend(["", "## Direct source shards", ""])
        index_lines.extend(shard_links or ["No processed direct sources yet."])
        if relationship_links:
            index_lines.extend(["", "## Graph connections", ""])
            index_lines.extend(relationship_links)
        if clusters:
            index_lines.extend(
                [
                    "",
                    "## Relevant clusters",
                    "",
                    *(
                        f"- [[../../CLUSTERS|{cluster_id}]]"
                        for cluster_id in clusters
                    ),
                ]
            )
        index_lines.extend(["", f"Catalogue revision: `{revision}`", ""])
        index_path = (
            f"02_source_memory/indexes/collections/{collection_dir}/INDEX.md"
        )
        files.append({"path": index_path, "text": "\n".join(index_lines)})
        catalogue.append(
            {
                **semantic,
                "path": index_path,
                "revision_hash": revision,
            }
        )

    tree_lines: list[str] = []
    visited: set[str] = set()

    def add_tree(collection_key: str, depth: int) -> None:
        if collection_key in visited:
            return
        visited.add(collection_key)
        row = collections[collection_key]
        tree_lines.append(
            f"{'  ' * depth}- [[collections/{directories[collection_key]}/INDEX|{row.get('name') or collection_key}]]"
        )
        for child_key in children.get(collection_key, []):
            add_tree(child_key, depth + 1)

    for root in roots:
        add_tree(root, 0)
    for key in sorted(collections):
        add_tree(key, 0)
    return {
        "catalogue": catalogue,
        "tree_lines": tree_lines,
        "files": files,
    }


def _collection_status_counts(
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        "processed": len(entries),
        "context_only": 0,
        "partial": 0,
        "parked_for_review": 0,
    }
    for entry in entries:
        eligibility = str(entry.get("evidence_eligibility") or "")
        coverage = str(entry.get("evidence_coverage") or "")
        scope = str(entry.get("source_scope") or "")
        status = str(entry.get("note_status") or "")
        if eligibility == "context_only" or coverage in {"abstract", "metadata"}:
            counts["context_only"] += 1
        if scope == "partial_document":
            counts["partial"] += 1
        if status in {"parked_for_review", "exhausted"}:
            counts["parked_for_review"] += 1
    return counts


def _collection_routing_card(
    *,
    key: str,
    name: str,
    parent_key: str,
    child_keys: Sequence[str],
    direct_source_count: int,
    descendant_source_count: int,
    entries: Sequence[Mapping[str, Any]],
    cluster_ids: Sequence[str],
    cross_collection_relationship_count: int,
) -> dict[str, Any]:
    card = _catalogue_shard_routing_card(
        shard_id=f"collection-{key}",
        title=name,
        scope=f"Sources filed in {name} and its child collections.",
        entries=entries,
    )
    method_counts = Counter(
        _compact_catalogue_text(entry.get("method"), 100)
        for entry in entries
        if _compact_catalogue_text(entry.get("method"), 100)
        not in {"", "Not specified"}
    )

    def dominant_facet(facet_type: str) -> list[str]:
        counts = Counter(
            _compact_catalogue_text(value, 80)
            for entry in entries
            for value in (
                entry.get("facets_by_type", {}).get(facet_type, [])
                if isinstance(entry.get("facets_by_type"), Mapping)
                else []
            )
            if _compact_catalogue_text(value, 80)
        )
        return [
            value
            for value, _count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0].casefold())
            )[:3]
        ]

    result = {
        "collection_key": key,
        "name": name,
        "parent_key": parent_key,
        "child_keys": list(child_keys),
        "direct_source_count": direct_source_count,
        "descendant_source_count": descendant_source_count,
        "scope": card["scope"],
        "dominant_facets": card["dominant_facets"],
        "method_mix": [
            {"method": method, "count": count}
            for method, count in sorted(
                method_counts.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )[:4]
        ],
        "representative_theses": card["representative_theses"],
        "important_cases": dominant_facet("case"),
        "important_periods": dominant_facet("period"),
        "important_datasets": dominant_facet("dataset"),
        "active_cluster_ids": sorted(str(value) for value in cluster_ids),
        "cross_collection_relationship_count": int(
            cross_collection_relationship_count
        ),
    }
    result["revision_hash"] = sha256_text(
        json.dumps(
            result,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return result


def _collection_directory_name(key: str) -> str:
    return (
        key
        if re.fullmatch(r"[A-Za-z0-9_-]+", key)
        else f"collection-{sha256_text(key)[:12]}"
    )


def _prune_stale_collection_indexes(
    workspace: Path,
    expected: set[Path],
    changed_paths: list[str],
) -> None:
    root = workspace / "02_source_memory" / "indexes" / "collections"
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for stale in sorted(
            [
                directory / "INDEX.md",
                *directory.glob("sources-*.md"),
                *directory.glob("relationships-*.md"),
            ]
        ):
            if stale.is_file() and stale.resolve() not in expected:
                stale.unlink()
                changed_paths.append(str(stale))
        try:
            directory.rmdir()
        except OSError:
            pass


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


def lean_discovery_projection(
    profiles: Sequence[Any] | Mapping[str, Any],
    catalogue: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Render the complete, graph-free model index from canonical source rows."""

    entries = {
        str(row.get("source_id") or ""): row
        for row in catalogue.get("sources", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    projected: dict[str, dict[str, Any]] = {}
    for profile in _catalogue_profile_rows(profiles):
        source_id = str(profile.get("source_id") or "")
        entry = entries.get(source_id)
        if not source_id or entry is None:
            continue
        context = (
            profile.get("context")
            if isinstance(profile.get("context"), Mapping)
            else {}
        )
        metadata = (
            context.get("metadata")
            if isinstance(context.get("metadata"), Mapping)
            else {}
        )
        thesis = context.get("thesis") or profile.get("thesis") or entry.get("thesis")
        method = (
            context.get("method_or_knowledge_basis")
            or profile.get("method")
            or next(iter(profile.get("methods", []) or []), "")
            or entry.get("method")
        )
        facets = {
            facet_type: sorted(
                {
                    _normalized_discovery_text(value)
                    for field in fields
                    for value in (
                        profile.get(field, [])
                        if isinstance(profile.get(field), Sequence)
                        and not isinstance(profile.get(field), (str, bytes))
                        else [profile.get(field)]
                    )
                    if _normalized_discovery_text(value)
                },
                key=str.casefold,
            )
            for facet_type, fields in (
                ("concept", ("concepts",)),
                ("mechanism", ("mechanisms",)),
                ("outcome", ("outcomes",)),
                ("case", ("cases",)),
                ("population", ("populations",)),
                ("period", ("periods",)),
                ("dataset", ("datasets",)),
            )
        }
        projected[source_id] = {
            "source_id": source_id,
            "zotero_key": str(
                entry.get("zotero_key")
                or metadata.get("zotero_item_key")
                or ""
            ),
            "title": _normalized_discovery_text(
                entry.get("title") or context.get("title")
            ),
            "author": _normalized_discovery_text(entry.get("author")),
            "year": str(entry.get("year") or context.get("date") or ""),
            "thesis": _normalized_discovery_text(thesis),
            "method": _normalized_discovery_text(method),
            "source_scope": str(
                profile.get("source_scope")
                or context.get("source_scope")
                or entry.get("source_scope")
                or "unknown"
            ),
            "evidence_eligibility": str(
                profile.get("evidence_eligibility")
                or context.get("evidence_eligibility")
                or entry.get("evidence_eligibility")
                or ""
            ),
            "collection_keys": sorted(
                {str(value) for value in entry.get("collection_keys", []) or [] if str(value)}
            ),
            "facets": {
                key: values for key, values in facets.items() if values
            },
        }
    return [projected[source_id] for source_id in sorted(projected)]


def _normalized_discovery_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _catalogue_relationship_rows(workspace: Path) -> list[dict[str, Any]]:
    payload = read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    rows = payload.get("links", []) if isinstance(payload, Mapping) else []
    return [
        dict(row)
        for row in rows
        if isinstance(row, Mapping)
        and bool(row.get("active", True))
        and row.get("source_id")
        and row.get("target_source_id")
        and row.get("relation_id", row.get("link_id"))
        and _relationship_confidence(row) >= 0.55
    ]


def _relationship_confidence(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("confidence", 1))
    except (TypeError, ValueError):
        return 0


def _catalogue_relationship_ids(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        relation_id = str(row.get("relation_id") or row.get("link_id") or "")
        if not relation_id:
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
                    or cluster.get("semantic_identity")
                    or cluster.get("title"),
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


def _catalogue_shard_routing_card(
    *,
    shard_id: str,
    title: str,
    scope: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a hard-bounded semantic card for probabilistic shard routing."""

    facet_counts = Counter(
        _compact_catalogue_text(value, 60)
        for entry in entries
        for value in [
            *(entry.get("facets", []) or []),
            *(
                facet
                for values in (
                    entry.get("facets_by_type")
                    if isinstance(entry.get("facets_by_type"), Mapping)
                    else {}
                ).values()
                for facet in (values or [])
            ),
        ]
        if _compact_catalogue_text(value, 60)
    )
    thesis_snippets: list[str] = []
    for entry in entries:
        thesis = _compact_catalogue_text(entry.get("thesis"), 180)
        if thesis and thesis.casefold() not in {
            value.casefold() for value in thesis_snippets
        }:
            thesis_snippets.append(thesis)
        if len(thesis_snippets) == 5:
            break
    card: dict[str, Any] = {
        "shard_id": str(shard_id),
        "title": _compact_catalogue_text(title, 180),
        "scope": _compact_catalogue_text(scope, 280),
        "source_count": len(entries),
        "dominant_facets": [
            value
            for value, _count in sorted(
                facet_counts.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )[:6]
        ],
        "representative_theses": thesis_snippets,
    }
    while (
        len(
            json.dumps(
                card,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        > SOURCE_CATALOGUE_ROUTING_CARD_MAX_CHARS
    ):
        if len(card["representative_theses"]) > 1:
            card["representative_theses"].pop()
        elif card["dominant_facets"]:
            card["dominant_facets"].pop()
        elif len(card["scope"]) > 80:
            card["scope"] = _compact_catalogue_text(card["scope"], len(card["scope"]) - 40)
        elif len(card["title"]) > 80:
            card["title"] = _compact_catalogue_text(card["title"], len(card["title"]) - 40)
        else:
            break
    return card


def _catalogue_entry(profile: Mapping[str, Any], note: Mapping[str, Any]) -> dict[str, Any]:
    creators = note.get("creators", []) or []
    author_names = []
    for creator in creators if isinstance(creators, Sequence) and not isinstance(creators, (str, bytes)) else []:
        if isinstance(creator, Mapping):
            creator_type = str(creator.get("creatorType") or "").strip()
            if creator_type and creator_type not in {"author", "bookAuthor"}:
                continue
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
    context = (
        profile.get("context")
        if isinstance(profile.get("context"), Mapping)
        else {}
    )
    source_scope = _compact_catalogue_text(
        profile.get("source_scope")
        or context.get("source_scope")
        or note.get("source_scope")
        or "unknown",
        60,
    )
    coverage_values = {
        str(envelope.get("coverage") or "")
        for anchor in profile.get("evidence_anchors", []) or []
        if isinstance(anchor, Mapping)
        for envelope in [anchor.get("support_envelope")]
        if isinstance(envelope, Mapping) and envelope.get("coverage")
    }
    evidence_coverage = next(
        (
            value
            for value in ("full_text", "limited_text", "abstract", "metadata", "unknown")
            if value in coverage_values
        ),
        "unknown",
    )
    evidence_eligibility = _compact_catalogue_text(
        profile.get("evidence_eligibility")
        or context.get("evidence_eligibility")
        or note.get("evidence_eligibility"),
        40,
    )
    facets_by_type = {
        facet_type: _bounded_profile_values(profile, fields)
        for facet_type, fields in (
            ("concept", ("concepts", "concept", "topics", "topic")),
            ("mechanism", ("mechanisms", "mechanism")),
            ("outcome", ("outcomes", "outcome")),
            ("case", ("cases", "case", "geography")),
            ("population", ("populations", "population")),
            ("period", ("periods", "period")),
            ("dataset", ("datasets", "dataset", "data")),
            ("method", ("methods", "method", "methodology")),
            ("subject", ("normalized_tags",)),
        )
    }
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
    source_id = str(profile.get("source_id") or note.get("source_id") or "")
    zotero_key = str(note.get("zotero_item_key") or "").strip().upper()
    if not zotero_key and source_id.startswith("source-zotero-"):
        zotero_key = source_id.removeprefix("source-zotero-").upper()
    entry = {
        "source_id": source_id,
        "zotero_key": zotero_key,
        "note_id": str(profile.get("note_id") or note.get("note_id") or ""),
        "title": _compact_catalogue_text(note.get("title") or "Untitled", 240),
        "author": author,
        "year": year_match.group(0) if year_match else "n.d.",
        "thesis": thesis or "Not specified.",
        "method": method or "Not specified.",
        "source_scope": source_scope or "unknown",
        "evidence_coverage": evidence_coverage,
        "evidence_eligibility": evidence_eligibility or "unavailable",
        "note_status": _compact_catalogue_text(
            note.get("note_status") or note.get("terminal_status"),
            40,
        ),
        "facets": facets,
        "facets_by_type": {
            key: value for key, value in facets_by_type.items() if value
        },
        "note_link": note_link,
        "profile_hash": profile_hash,
    }
    entry["identity"] = _catalogue_identity(entry, note)
    entry["navigation"] = _catalogue_navigation(entry)
    return entry


def _catalogue_identity(
    entry: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    current = (
        dict(entry.get("identity") or {})
        if isinstance(entry.get("identity"), Mapping)
        else {}
    )
    supplied = (
        dict(source.get("identity") or {})
        if isinstance(source.get("identity"), Mapping)
        else {}
    )

    def first(*values: Any) -> str:
        return next((str(value).strip() for value in values if str(value or "").strip()), "")

    canonical_key = first(
        supplied.get("canonical_zotero_key"),
        source.get("canonical_zotero_key"),
        current.get("canonical_zotero_key"),
        entry.get("zotero_key"),
        source.get("key"),
        source.get("zotero_item_key"),
    ).upper()
    item_keys = {
        str(value).strip().upper()
        for value in [
            *(
                current.get("zotero_item_keys", [])
                if isinstance(current.get("zotero_item_keys"), Sequence)
                and not isinstance(current.get("zotero_item_keys"), (str, bytes))
                else []
            ),
            *(
                supplied.get("zotero_item_keys", [])
                if isinstance(supplied.get("zotero_item_keys"), Sequence)
                and not isinstance(supplied.get("zotero_item_keys"), (str, bytes))
                else []
            ),
            *(
                source.get("zotero_item_keys", [])
                if isinstance(source.get("zotero_item_keys"), Sequence)
                and not isinstance(source.get("zotero_item_keys"), (str, bytes))
                else []
            ),
            canonical_key,
        ]
        if str(value or "").strip()
    }
    doi = first(
        supplied.get("doi"),
        source.get("doi"),
        source.get("DOI"),
        current.get("doi"),
    ).casefold()
    if doi.startswith("https://doi.org/"):
        doi = doi.removeprefix("https://doi.org/")
    isbn = re.sub(
        r"[^0-9Xx]",
        "",
        first(
            supplied.get("isbn"),
            source.get("isbn"),
            source.get("ISBN"),
            current.get("isbn"),
        ),
    ).upper()
    title = first(
        supplied.get("normalized_title"),
        source.get("title"),
        current.get("normalized_title"),
        entry.get("title"),
    )
    creator_surnames = [
        str(creator.get("lastName") or creator.get("name") or "").strip()
        for creator in source.get("creators", []) or []
        if isinstance(creator, Mapping)
        and str(creator.get("lastName") or creator.get("name") or "").strip()
    ]
    author_values = (
        supplied.get("normalized_author_surnames")
        or creator_surnames
        or current.get("normalized_author_surnames")
        or str(entry.get("author") or "").split(",")
    )
    author_surnames = sorted(
        {
            _normalize_identity_text(value)
            for value in author_values
            if _normalize_identity_text(value)
        }
    )
    year_text = first(
        supplied.get("year"),
        source.get("year"),
        source.get("date"),
        current.get("year"),
        entry.get("year"),
    )
    year_match = re.search(r"(?:19|20)\d{2}", year_text)
    relations = next(
        (
            dict(value)
            for value in (
                supplied.get("zotero_relations"),
                source.get("zotero_relations"),
                source.get("relations"),
                current.get("zotero_relations"),
            )
            if isinstance(value, Mapping)
        ),
        {},
    )
    return {
        "canonical_zotero_key": canonical_key,
        "zotero_item_keys": sorted(item_keys),
        "doi": doi,
        "isbn": isbn,
        "url": first(
            supplied.get("url"),
            source.get("url"),
            source.get("URL"),
            current.get("url"),
        ),
        "normalized_title": _normalize_identity_text(title),
        "normalized_author_surnames": author_surnames,
        "year": year_match.group(0) if year_match else "n.d.",
        "zotero_relations": relations,
    }


def _normalize_identity_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text)).strip()


def _catalogue_navigation(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": str(entry.get("title") or ""),
        "author": str(entry.get("author") or ""),
        "year": str(entry.get("year") or ""),
        "thesis": str(entry.get("thesis") or ""),
        "method": str(entry.get("method") or ""),
        "source_scope": str(entry.get("source_scope") or ""),
        "evidence_eligibility": str(entry.get("evidence_eligibility") or ""),
        "facets_by_type": dict(entry.get("facets_by_type") or {}),
        "collections": list(entry.get("collections", []) or []),
        "virtual_topic_ids": list(entry.get("virtual_topic_ids", []) or []),
    }


def _refresh_catalogue_sections(
    entry: dict[str, Any], snapshot_item: Mapping[str, Any]
) -> None:
    entry["identity"] = _catalogue_identity(entry, snapshot_item)
    entry["navigation"] = _catalogue_navigation(entry)


def _virtual_catalogue_projection(
    entries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    candidates_by_source: dict[str, list[tuple[str, str, str]]] = {}
    topic_counts: Counter[str] = Counter()
    for entry in entries:
        candidates: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        facets_by_type = (
            entry.get("facets_by_type")
            if isinstance(entry.get("facets_by_type"), Mapping)
            else {}
        )
        for facet_type in _VIRTUAL_TOPIC_FACET_ORDER:
            for raw_label in facets_by_type.get(facet_type, []) or []:
                label = _compact_catalogue_text(raw_label, 80)
                normalized = _normalize_identity_text(label)
                if (
                    not normalized
                    or normalized in _GENERIC_VIRTUAL_TOPICS
                    or normalized in seen
                ):
                    continue
                seen.add(normalized)
                topic_id = f"topic-{slugify(facet_type)}-{slugify(normalized)}"
                candidates.append((topic_id, facet_type, label))
        source_id = str(entry.get("source_id") or entry.get("note_id") or "")
        candidates_by_source[source_id] = candidates
        topic_counts.update(topic_id for topic_id, _facet_type, _label in candidates)

    topic_rows: dict[str, dict[str, Any]] = {}
    for entry in entries:
        source_id = str(entry.get("source_id") or entry.get("note_id") or "")
        memberships = [
            candidate
            for candidate in candidates_by_source.get(source_id, [])
            if topic_counts[candidate[0]] >= 2
        ][:3]
        if not memberships:
            memberships = [("topic-catch-all", "catch_all", "Other sources")]
        entry["virtual_topic_ids"] = [topic_id for topic_id, _facet, _label in memberships]
        entry["navigation"] = _catalogue_navigation(entry)
        for topic_id, facet_type, label in memberships:
            topic = topic_rows.setdefault(
                topic_id,
                {
                    "topic_id": topic_id,
                    "facet_type": facet_type,
                    "label": label,
                    "entries": [],
                },
            )
            topic["entries"].append(entry)

    shards: list[dict[str, Any]] = []
    catalogue: list[dict[str, Any]] = []
    for topic_id in sorted(topic_rows):
        topic = topic_rows[topic_id]
        topic_entries = sorted(
            topic["entries"],
            key=lambda row: (
                str(row.get("author") or "").casefold(),
                str(row.get("year") or ""),
                str(row.get("title") or "").casefold(),
                str(row.get("source_id") or ""),
            ),
        )
        chunks = _meaningful_catalogue_chunks(topic_entries)
        shard_ids: list[str] = []
        for number, (_group, chunk_entries) in enumerate(chunks, start=1):
            shard_id = (
                topic_id
                if len(chunks) == 1
                else f"{topic_id}-part-{number:02d}"
            )
            shard_ids.append(shard_id)
            heading = (
                str(topic["label"])
                if len(chunks) == 1
                else f"{topic['label']} — part {number}"
            )
            body = (
                f"# Virtual topic: {heading}\n\n"
                "Navigation only; this index makes no synthesis claim.\n\n"
                + "".join(_render_catalogue_source(entry) for entry in chunk_entries)
            )
            revision_hash = sha256_text(body)
            shards.append(
                {
                    "topic_id": topic_id,
                    "shard_id": shard_id,
                    "title": heading,
                    "path": f"02_source_memory/indexes/by_topic/{shard_id}.md",
                    "source_count": len(chunk_entries),
                    "source_ids": [
                        str(entry.get("source_id") or "")
                        for entry in chunk_entries
                        if entry.get("source_id")
                    ],
                    "note_ids": [
                        str(entry.get("note_id") or "")
                        for entry in chunk_entries
                        if entry.get("note_id")
                    ],
                    "routing_card": _catalogue_shard_routing_card(
                        shard_id=shard_id,
                        title=heading,
                        scope=f"Sources indexed under {topic['label']}.",
                        entries=chunk_entries,
                    ),
                    "revision_hash": revision_hash,
                    "text": f"{body}\nCatalogue revision: `{revision_hash}`\n",
                }
            )
        catalogue.append(
            {
                "topic_id": topic_id,
                "facet_type": topic["facet_type"],
                "label": topic["label"],
                "source_count": len(topic_entries),
                "source_ids": [
                    str(entry.get("source_id") or "")
                    for entry in topic_entries
                    if entry.get("source_id")
                ],
                "shard_ids": shard_ids,
                "routing_card": _catalogue_shard_routing_card(
                    shard_id=topic_id,
                    title=str(topic["label"]),
                    scope=f"Sources indexed under {topic['label']}.",
                    entries=topic_entries,
                ),
            }
        )

    index_lines = [
        "# Virtual Topic Indexes",
        "",
        "Navigation only; these shards are not literature-synthesis clusters.",
        "",
    ]
    for shard in shards:
        index_lines.append(
            f"- [[{Path(str(shard['path'])).stem}|{shard['title']}]] "
            f"— {shard['source_count']} sources"
        )
    index_body = "\n".join(index_lines).rstrip() + "\n"
    files = [
        *(
            {"path": str(shard["path"]), "text": str(shard["text"])}
            for shard in shards
        ),
        {
            "path": "02_source_memory/indexes/by_topic/INDEX.md",
            "text": index_body,
        },
    ]
    return {
        "catalogue": catalogue,
        "shards": shards,
        "files": files,
    }


def _bounded_profile_values(
    profile: Mapping[str, Any],
    fields: Sequence[str],
    *,
    max_values: int = 2,
) -> list[str]:
    result: list[str] = []
    for field in fields:
        raw = profile.get(field)
        values = (
            raw
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
            else [raw]
        )
        for value in values:
            if isinstance(value, Mapping):
                value = next(
                    (
                        value.get(key)
                        for key in ("label", "name", "value", "text")
                        if value.get(key)
                    ),
                    "",
                )
            cleaned = _compact_catalogue_text(value, 80)
            if cleaned and cleaned.casefold() not in {
                item.casefold() for item in result
            }:
                result.append(cleaned)
            if len(result) >= max_values:
                return result
    return result


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
