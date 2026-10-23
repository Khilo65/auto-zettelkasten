"""Bounded, local-only acquisition and loading for discovery and recall."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import yaml

from .extraction import extract_bytes
from .indexes import local_retrieval_projection
from .profiles import profile_from_dict, profile_sidecar_path, profile_to_dict
from .relationships import JEV_RELATIONSHIP_CONTRACT, profile_content_hash, profile_hash_aliases
from .retrieval import LocalRetrievalIndex
from .zotero import ZoteroLocalClient


CACHE_RELATIVE_PATH = Path(".auto-zettelkasten/discovery.sqlite3")
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_ATTACHMENTS = 8
MAX_RICH_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_INDEX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_RICH_YAML_NODES = 500_000
MAX_INDEX_YAML_NODES = 5_000_000
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _integer(value: int, name: str, *, maximum: int | None = None, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside its integer bounds")
    return value


def _scope(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string of at most 200 characters")
    return value.strip()


def _item_key(value: str) -> str:
    value = _scope(value, "item_key")
    if not re.fullmatch(r"[A-Z0-9]{1,32}", value):
        raise ValueError("invalid Zotero item key")
    return value


class _ReadBudget:
    def __init__(self, root: Path, *, max_files: int, max_file_bytes: int, max_total_bytes: int) -> None:
        self.root = root
        self.max_files = _integer(max_files, "max_files")
        self.max_file_bytes = _integer(max_file_bytes, "max_file_bytes")
        self.max_total_bytes = _integer(max_total_bytes, "max_total_bytes")
        self.sizes: dict[Path, int] = {}
        self.node_limits: dict[Path, int] = {}
        self.total_bytes = 0

    def reserve(self, paths: Sequence[Path], *, max_file_bytes: int | None = None, max_nodes: int = MAX_RICH_YAML_NODES) -> None:
        file_limit = self.max_file_bytes if max_file_bytes is None else _integer(max_file_bytes, "max_file_bytes")
        for path in paths:
            if path in self.sizes or not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("local retrieval input must remain inside the workspace")
            size = path.stat().st_size
            if not path.is_file() or size > file_limit:
                raise ValueError("local retrieval per-file byte limit exceeded")
            self.sizes[path] = size
            self.node_limits[path] = max_nodes
            self.total_bytes += size
            if len(self.sizes) > self.max_files or self.total_bytes > self.max_total_bytes:
                raise ValueError("local retrieval file count or total byte limit exceeded")

    def read(self, path: Path) -> dict[str, Any]:
        if path not in self.sizes:
            return {}
        with path.open("rb") as source:
            raw = source.read(self.sizes[path] + 1)
        if len(raw) != self.sizes[path]:
            raise ValueError("local retrieval input changed during read")
        text = raw.decode("utf-8")
        max_nodes = self.node_limits[path]
        depth = 0
        anchors: list[str | None] = []
        for index, event in enumerate(yaml.parse(text, Loader=_YAML_LOADER)):
            if index > max_nodes:
                raise ValueError("local retrieval YAML node limit exceeded")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                anchors.append(event.anchor)
                if depth > 32:
                    raise ValueError("local retrieval YAML depth limit exceeded")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
                anchors.pop()
            elif isinstance(event, yaml.AliasEvent) and event.anchor in anchors:
                raise ValueError("recursive YAML aliases are unsafe")
        value = yaml.load(text, Loader=_YAML_LOADER)
        if value is None:
            value = {}
        _check_shape(value, max_nodes=max_nodes)
        if not isinstance(value, dict):
            raise ValueError("local retrieval input must be a YAML mapping")
        return value


def _check_shape(value: Any, *, max_nodes: int = MAX_RICH_YAML_NODES) -> None:
    memo: dict[int, tuple[int, int]] = {}

    def visit(node: Any) -> tuple[int, int]:
        if not isinstance(node, (dict, list)):
            return 1, 0
        if id(node) in memo:
            return memo[id(node)]
        count, height = 1, 0
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            cost, level = visit(child)
            count += cost
            height = max(height, level + 1)
            if count > max_nodes or height > 32:
                raise ValueError("local retrieval YAML expansion/depth limit exceeded")
        memo[id(node)] = count, height
        return count, height

    visit(value)


def _catalogue(root: Path) -> dict[str, Any]:
    budget = _ReadBudget(root, max_files=1, max_file_bytes=MAX_INDEX_DOCUMENT_BYTES, max_total_bytes=MAX_INDEX_DOCUMENT_BYTES)
    path = root / "02_source_memory/indexes/source_catalogue.yml"
    budget.reserve([path], max_nodes=MAX_INDEX_YAML_NODES)
    return budget.read(path)


def _catalogue_scope_matches(row: Mapping[str, Any], instance_id: str, library_id: str) -> bool:
    """Legacy unscoped rows are compatible; every explicit scope field must match."""
    identity = row.get("identity", {})
    if not isinstance(identity, Mapping):
        return False
    return all(field not in container or container[field] == expected
               for container in (row, identity)
               for field, expected in (("instance_id", instance_id), ("library_type", "user"), ("library_id", library_id)))


def _binding(catalogue: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any] | None:
    matches = [row for row in catalogue.get("sources", []) if isinstance(row, dict)
               and _catalogue_scope_matches(row, identity["instance_id"], identity["library_id"])
               and identity["item_key"] in _catalogue_keys(row) and row.get("source_id")]
    return dict(matches[0]) if len(matches) == 1 else None


def _catalogue_keys(row: Mapping[str, Any]) -> set[str]:
    identity = row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
    aliases = identity.get("zotero_item_keys", row.get("zotero_item_keys", []))
    aliases = aliases if isinstance(aliases, list) else []
    return {value for value in [row.get("zotero_key"), *aliases] if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{1,32}", value)}


def _data(item: Mapping[str, Any]) -> Mapping[str, Any]:
    data = item.get("data", item)
    if not isinstance(data, Mapping):
        raise ValueError("Zotero item data must be a mapping")
    return data


def _complete_fulltext(value: Mapping[str, Any], media_type: str) -> bool:
    fields = ("indexedPages", "totalPages") if media_type == "application/pdf" else ("indexedChars", "totalChars")
    if any(type(value.get(key)) is not int for key in fields):
        return False
    indexed, total = (value[key] for key in fields)
    return total > 0 and indexed == total


def _coverage_counts(value: Mapping[str, Any]) -> dict[str, int]:
    return {key: value[key] for key in ("indexedPages", "totalPages", "indexedChars", "totalChars") if type(value.get(key)) is int and value[key] >= 0}


def _source_text(item: Mapping[str, Any], client: Any) -> tuple[str, dict[str, Any]]:
    from .pipeline import _attachment_candidate_rank, _target_media_type

    data = _data(item)
    key = str(item.get("key") or data.get("key") or "")
    coverage: dict[str, Any] = {"status": "not_examined", "reason": "local_document_unavailable", "source_scope": "metadata_only"}
    try:
        children = [] if data.get("itemType") == "attachment" else client.children(key, limit=64, max_bytes=2 * 1024 * 1024)
    except Exception as exc:
        children = []
        coverage.update(status="failed", reason=f"attachment_metadata_failed:{type(exc).__name__}")
    targets = [item] if data.get("itemType") == "attachment" else children
    attachments = [target for target in targets if isinstance(target, Mapping) and _data(target).get("itemType") == "attachment" and _data(target).get("linkMode") not in ("linked_url", 3)]
    ranked_attachments = []
    for target in attachments:
        target_data = _data(target)
        media_type = _target_media_type(target_data)
        if media_type == "application/octet-stream":
            media_type = _target_media_type({**target_data, "contentType": ""})
        if media_type == "application/octet-stream":
            media_type = _target_media_type({**target_data, "contentType": "", "filename": ""})
        ranked_attachments.append((_attachment_candidate_rank(
            target_data, data, media_type=media_type, actual_file=True,
        ) or 0, target, media_type))
    ranked_attachments.sort(key=lambda candidate: candidate[0], reverse=True)
    best_text = ""
    best_rank = -1
    for attachment_rank, target, media_type in ranked_attachments[:MAX_ATTACHMENTS]:
        target_data = _data(target)
        # A supported type lets existing label heuristics classify secondary files even when MIME is unknown.
        label_rank = _attachment_candidate_rank(target_data, data, media_type="application/pdf", actual_file=True)
        secondary = data.get("itemType") != "attachment" and label_rank is not None and label_rank < 90
        attachment_key = str(target.get("key") or target_data.get("key") or "")
        if not attachment_key:
            continue
        try:
            fulltext = client.fulltext(attachment_key, max_bytes=MAX_TEXT_CHARS * 4)
        except Exception:
            fulltext = None
        if isinstance(fulltext, Mapping):
            cached_text = next((fulltext[field] for field in ("content", "text", "fulltext") if isinstance(fulltext.get(field), str) and fulltext[field].strip()), "")
            cached_coverage = {"status": "partial", "reason": "incomplete_cached_fulltext", "source_scope": "partial_document", "coverage_gate": "limited", "route": "zotero_fulltext", **_coverage_counts(fulltext)}
            if _complete_fulltext(fulltext, media_type):
                if secondary:
                    cached_coverage["reason"] = "secondary_attachment_only"
                elif cached_text:
                    return _bounded_text(cached_text, {**cached_coverage, "status": "succeeded", "reason": "complete_cached_fulltext", "source_scope": "full_document", "coverage_gate": "passed"})
            if cached_text and (attachment_rank > best_rank or (attachment_rank == best_rank and len(cached_text) > len(best_text))):
                best_text, coverage = _bounded_text(cached_text, cached_coverage)
                best_rank = attachment_rank
        try:
            document = client.file(attachment_key, max_bytes=MAX_ATTACHMENT_BYTES)
            if document is None:
                continue
            raw, actual_type = document
            if not isinstance(raw, bytes) or len(raw) > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment_byte_limit")
            actual_type = str(actual_type or "").split(";", 1)[0].strip().casefold()
            if actual_type and actual_type != "application/octet-stream":
                media_type = actual_type
                attachment_rank = _attachment_candidate_rank(target_data, data, media_type=media_type, actual_file=True) or attachment_rank
            extracted = extract_bytes(raw, media_type=media_type, filename=str(target_data.get("filename") or ""), ocr_mode="off")
            scope = extracted.source_scope or "metadata_only"
            complete = scope == "full_document" and extracted.source_coverage == "passed"
            result = {"status": "succeeded" if complete else "partial" if extracted.text else "failed", "reason": extracted.reason or "unreadable_local_document", "source_scope": scope, "coverage_gate": extracted.source_coverage or "failed", "route": extracted.route}
            if extracted.page_count:
                result.update(total_pages=extracted.page_count, indexed_pages=extracted.page_count if complete else None)
            if secondary and extracted.text:
                complete = False
                result.update(status="partial", reason="secondary_attachment_only", source_scope="partial_document", coverage_gate="limited")
            if complete:
                return _bounded_text(extracted.text, result)
            if extracted.text and (attachment_rank > best_rank or (attachment_rank == best_rank and len(extracted.text) > len(best_text))):
                best_text, coverage = extracted.text, result
                best_rank = attachment_rank
            elif not best_text:
                coverage = result
        except Exception as exc:
            if not best_text:
                coverage = {"status": "failed", "reason": f"local_extraction_failed:{type(exc).__name__}:{str(exc)[:160]}", "source_scope": "metadata_only"}
    if len(attachments) > MAX_ATTACHMENTS:
        coverage = {**coverage, "status": "partial" if best_text else "not_examined", "reason": "attachment_limit"}
    elif len(children) >= 64 and not best_text:
        coverage = {**coverage, "reason": "no_usable_document_in_examined_child_page"}
    return _bounded_text(best_text, coverage)


def _bounded_text(text: str, coverage: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "\x00" in text:
        return "", {**coverage, "status": "failed", "reason": "invalid_extracted_text", "coverage_gate": "failed"}
    if len(text) > MAX_TEXT_CHARS:
        coverage = {**coverage, "status": "partial", "reason": "text_input_limit", "source_scope": "partial_document", "coverage_gate": "limited"}
    return text[:MAX_TEXT_CHARS], coverage


def scan_local_collection(
    workspace: Path | str, *, instance_id: str, item_keys: Sequence[str] = (),
    collection_key: str = "", start: int = 0, limit: int = 32, client: Any = None,
) -> dict[str, Any]:
    """Scan explicit item keys or one collection page; write only the private cache."""
    from .discovery import DiscoveryCache, ReferenceMatcher, scan_document

    instance_id = _scope(instance_id, "instance_id")
    _integer(start, "start", minimum=0)
    _integer(limit, "limit", maximum=64)
    if isinstance(item_keys, (str, bytes)) or len(item_keys) > 64:
        raise ValueError("item_keys must contain at most 64 explicit keys")
    keys = list(dict.fromkeys(_item_key(key) for key in item_keys))
    if bool(keys) == bool(collection_key) or (keys and start):
        raise ValueError("choose explicit item keys or one collection page")
    client = client or ZoteroLocalClient()
    library_id = _scope(str(client.library_id), "library_id")
    if not library_id.isdigit():
        raise ValueError("only explicit personal user libraries are supported")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    catalogue = _catalogue(root)
    errors = []
    if keys:
        items = []
        for key in keys:
            item = client.item(key, max_bytes=262_144)
            if item is None:
                errors.append({"item_key": key, "status": "failed", "reason": "item_not_found"})
            elif not isinstance(item, Mapping) or str(item.get("key") or _data(item).get("key") or "") != key:
                raise ValueError("Zotero returned a different item identity")
            else:
                items.append(item)
        page = {"start": 0, "limit": len(keys), "total": len(keys), "next_start": None}
    else:
        page = client.inventory_page("collection", collection_key, start=start, limit=limit)
        items = page["records"]
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("Zotero page exceeds requested bounds")
    library_items = []
    for item in items:
        key = _item_key(str(item.get("key") or _data(item).get("key") or ""))
        library_items.append({"identity": {"instance_id": instance_id, "library_type": "user", "library_id": library_id, "item_key": key}, "item": item})
    cache_path = root / CACHE_RELATIVE_PATH
    if not cache_path.resolve().is_relative_to(root) or cache_path.is_symlink():
        raise ValueError("discovery cache must remain inside the workspace")
    cache = DiscoveryCache(cache_path)
    matcher = ReferenceMatcher(_matcher_items(library_items, catalogue.get("sources", []), instance_id, library_id))
    records = []
    for entry in library_items:
        item, identity = entry["item"], entry["identity"]
        if _data(item).get("itemType") == "note":
            continue
        text, coverage = _source_text(item, client)
        record = scan_document(item, identity=identity, text=text, coverage=coverage, source_binding=_binding(catalogue, identity), matcher=matcher, cache=cache)
        records.append(record)
    return {"documents": records, "errors": errors, "page": {key: value for key, value in page.items() if key != "records"}, "cache_path": str(root / CACHE_RELATIVE_PATH), "instance_id": instance_id, "library_id": library_id, "matching_scope": "provided scoped current index; library presence outside this index is unexamined"}


def load_local_retrieval(
    workspace: Path | str, *, instance_id: str | None = None, library_id: str | None = "0",
    infer_scope: bool = False,
    max_files: int = 20_000, max_file_bytes: int | None = None,
    max_index_bytes: int = MAX_INDEX_DOCUMENT_BYTES,
    max_total_bytes: int = 384 * 1024 * 1024,
) -> dict[str, Any]:
    """Load current state; explicit max_file_bytes remains a ceiling for all YAML.

    When omitted, rich documents are limited to 8 MiB and compact indexes to
    max_index_bytes. Missing or unspecified cache scope stays unknown.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    if instance_id is not None:
        instance_id = _scope(instance_id, "instance_id")
    if type(infer_scope) is not bool:
        raise ValueError("infer_scope must be a boolean")
    library_id = None if infer_scope and library_id is None else _scope(library_id, "library_id")
    index_limit = _integer(max_index_bytes, "max_index_bytes")
    rich_limit = MAX_RICH_DOCUMENT_BYTES
    if max_file_bytes is not None:
        explicit_limit = _integer(max_file_bytes, "max_file_bytes")
        rich_limit = min(rich_limit, explicit_limit)
        index_limit = min(index_limit, explicit_limit)
    budget = _ReadBudget(root, max_files=max_files, max_file_bytes=rich_limit, max_total_bytes=max_total_bytes)
    catalogue_path = root / "02_source_memory/indexes/source_catalogue.yml"
    registry_path = root / "02_source_memory/indexes/typed_links.yml"
    positions_path = root / "02_source_memory/indexes/literature_positions.yml"
    budget.reserve([catalogue_path, registry_path, positions_path], max_file_bytes=index_limit, max_nodes=MAX_INDEX_YAML_NODES)
    catalogue = budget.read(catalogue_path)
    entries: dict[str, dict[str, Any]] = {}
    for entry in catalogue.get("sources", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str) or not entry["source_id"] or entry["source_id"] in entries:
            raise ValueError("duplicate or missing catalogue source identity")
        entries[entry["source_id"]] = entry
        if len(entries) > max_files:
            raise ValueError("local retrieval catalogue source count limit exceeded")
    if infer_scope:
        instance_id, library_id, scope_status = _cached_discovery_scope(list(entries.values()), (), instance_id, library_id)
    else:
        scope_status = "explicit" if instance_id is not None else "unknown"
    profile_paths, missing_profiles = {}, []
    for source_id, entry in entries.items():
        note_id = entry.get("note_id")
        path = profile_sidecar_path(root / "02_source_memory/profiles", note_id) if isinstance(note_id, str) and note_id else None
        if path is None or not path.exists():
            missing_profiles.append(source_id)
        else:
            profile_paths[source_id] = path
    metadata_paths = [root / "11_state/note_metadata" / path.name for path in profile_paths.values()]
    cache_path = root / CACHE_RELATIVE_PATH
    budget.reserve([*profile_paths.values(), *metadata_paths])
    cache_exists = instance_id is not None and cache_path.is_file()
    if cache_exists:
        if len(budget.sizes) + 1 > budget.max_files:
            raise ValueError("local retrieval file count limit exceeded")
        if cache_path.is_symlink() or not cache_path.resolve().is_relative_to(root):
            raise ValueError("discovery cache must remain inside the workspace")
    rows, hashes, stored_tag_omissions = [], {}, 0
    for source_id, path in sorted(profile_paths.items()):
        entry = entries[source_id]
        wrapper = budget.read(path)
        if wrapper.get("profile_schema_version") != "1" or not isinstance(wrapper.get("profile"), Mapping):
            raise ValueError("invalid canonical profile sidecar")
        profile = profile_from_dict(wrapper["profile"])
        if profile.source_id != source_id or profile.note_id != entry["note_id"]:
            raise ValueError("canonical profile does not match catalogue source/note identity")
        hashes[source_id] = (profile_content_hash(profile), list(profile_hash_aliases(profile)))
        row = profile_to_dict(profile)
        metadata_path = root / "11_state/note_metadata" / path.name
        metadata = budget.read(metadata_path)
        frontmatter = metadata.get("frontmatter", {})
        context = None
        if metadata:
            if not isinstance(frontmatter, Mapping) or frontmatter.get("source_id") != source_id or frontmatter.get("note_id") != profile.note_id:
                raise ValueError("note metadata does not match canonical profile")
            context = dict(row.get("context") or {})
            context["note_path"] = str(metadata.get("note_path") or context.get("note_path") or "")
            context["metadata"] = {**dict(context.get("metadata") or {}), **{key: frontmatter[key] for key in ("source_file", "source_pdf_uri", "zotero_item_uri", "url") if key in frontmatter}}
            row["context"] = context
        compact = local_retrieval_projection([row], {"sources": [entry]})
        if len(compact) != 1 or compact[0]["source_id"] != source_id:
            raise ValueError("canonical source projection did not preserve identity")
        facets = entry.get("facets_by_type") if isinstance(entry.get("facets_by_type"), Mapping) else {}
        tag_arrays = [row.get("normalized_tags") or [], frontmatter.get("normalized_tags") or [], facets.get("subject") or []]
        if any(not isinstance(values, (list, tuple)) or len(values) > 256 or any(not isinstance(value, str) for value in values) for values in tag_arrays):
            raise ValueError("canonical subject tags must be bounded text sequences")
        tag_values = sorted({tag for values in tag_arrays for tag in values})
        compact[0]["tags"] = tag_values[:256]
        stored_tag_omissions += max(0, len(tag_values) - 256)
        rows.extend(compact)
        del wrapper, profile, row, metadata, frontmatter, context
    scoped_sources = {source_id for source_id, entry in entries.items()
                      if instance_id is not None and _catalogue_scope_matches(entry, instance_id, library_id)}
    key_counts = Counter(row.get("zotero_key") for row in rows if row["source_id"] in scoped_sources)
    for row in rows:
        row["profile_hash"], row["profile_hash_aliases"] = hashes[row["source_id"]]
        if row["source_id"] in scoped_sources and row.get("zotero_key") and key_counts[row["zotero_key"]] == 1:
            row["identity"] = {"instance_id": instance_id, "library_type": "user", "library_id": library_id, "item_key": row["zotero_key"]}
    registry = budget.read(registry_path)
    relationships = _accepted_relationships(registry, hashes, workspace=root)
    citation_records, citation_status = _literature_candidate_records(budget.read(positions_path), rows)
    # Cache loading is deliberately separate from acquisition and never creates a DB.
    records, cache_status = _cached_records(root, rows, catalogue.get("sources", []), instance_id, library_id, max_bytes=min(16_000_000, budget.max_total_bytes - budget.total_bytes))
    cache_bytes = cache_status.get("cache_record_bytes", 0)
    return {"workspace": str(root), "index": LocalRetrievalIndex(rows, records, citation_records=citation_records), "rows": rows, "relationships": relationships, "discovery_records": records, "citation_records": citation_records, "status": {"source_count": len(rows), "catalogue_source_count": len(entries), "missing_profile_count": len(missing_profiles), "missing_profile_source_ids": missing_profiles[:32], "profile_load_status": "partial" if missing_profiles else "complete", "cached_document_count": len(records), "reference_coverage": "cached_unverified" if records else "not_examined", "files_read": len(budget.sizes) + int(cache_exists), "bytes_read": budget.total_bytes + cache_bytes,
            "recorded_unverified_relationships": sum(row.get("freshness") == "recorded_unverified" for row in relationships),
            "excluded_relationships": len(registry.get("relations", [])) - len(relationships),
            "stored_tag_omissions": stored_tag_omissions, "scope_status": scope_status,
            "instance_id": instance_id, "library_id": library_id, **cache_status, **citation_status}}


def _literature_candidate_records(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reuse matched source IDs as candidate hints, never semantic assertions."""
    positions = payload.get("positions", [])
    if not isinstance(positions, list) or len(positions) > 100_000:
        raise ValueError("discussed-work memory must contain bounded position rows")
    by_source = {row["source_id"]: row for row in rows}
    selected, stale, unresolved, unverified = [], 0, 0, 0
    per_source: Counter[str] = Counter()
    for position in positions:
        if not isinstance(position, Mapping):
            raise ValueError("discussed-work position must be a mapping")
        source, target = position.get("current_source_id"), position.get("matched_source_id")
        if (not isinstance(source, str) or not isinstance(target, str) or source == target
                or source not in by_source or target not in by_source
                or position.get("match_status") not in {"mapped", "matched"}):
            unresolved += 1
            continue
        checked = False
        mismatch = False
        for prefix, sid in (("source", source), ("target", target)):
            row = by_source[sid]
            aliases = row.get("profile_hash_aliases") or []
            profile_aliases = {value for value in [row.get("profile_hash"), *(aliases if isinstance(aliases, (list, tuple)) else [])] if isinstance(value, str) and value}
            note_hashes = {row["note_hash"]} if isinstance(row.get("note_hash"), str) and row["note_hash"] else set()
            for field, current in ((f"{prefix}_profile_hash", profile_aliases),
                                   (f"{prefix}_note_hash", note_hashes)):
                declared = position.get(field)
                if declared:
                    checked = True
                    if not isinstance(declared, str) or declared not in current:
                        mismatch = True
            supplied = position.get("input_profile_hashes")
            if isinstance(supplied, Mapping) and supplied.get(sid):
                checked = True
                if not isinstance(supplied[sid], str) or supplied[sid] not in profile_aliases:
                    mismatch = True
        if mismatch:
            stale += 1
            continue
        if per_source[source] >= 256:
            unresolved += 1
            continue
        per_source[source] += 1
        freshness = "current_declared_hashes" if checked else "recorded_unverified"
        unverified += int(not checked)
        selected.append({"source_id": source, "target_source_id": target,
                         **{key: str(position.get(key) or "")[:1000] for key in ("literature_position_id", "locator", "match_status", "match_basis")},
                         "freshness": freshness, "reference_origin": "discussed_work_memory"})
    selected.sort(key=lambda row: (row["source_id"], row["target_source_id"], row["literature_position_id"], row["locator"]))
    return selected, {"discussed_work_position_count": len(positions), "matched_discussed_work_count": len(selected),
                      "stale_discussed_work_count": stale, "unresolved_discussed_work_count": unresolved,
                      "unverified_discussed_work_count": unverified,
                      "discussed_work_coverage": "candidate_hints_only" if positions else "not_examined"}


def _cached_discovery_scope(
    catalogue_rows: Sequence[Mapping[str, Any]], projection_rows: Sequence[Mapping[str, Any]],
    instance_id: str | None, library_id: str | None,
) -> tuple[str | None, str, str]:
    """Infer a scope only from declared source identity, never cached contents."""
    if instance_id is not None:
        return _scope(instance_id, "instance_id"), _scope(library_id if library_id is not None else "0", "library_id"), "explicit"
    if library_id is not None:
        library_id = _scope(library_id, "library_id")
    scopes: set[tuple[str, str]] = set()
    for row in [*catalogue_rows, *projection_rows]:
        for candidate in (row, row.get("identity"), row.get("catalogue_identity")):
            if not isinstance(candidate, Mapping) or candidate.get("library_type") != "user":
                continue
            instance, library = candidate.get("instance_id"), candidate.get("library_id")
            if (isinstance(instance, str) and instance.strip() and isinstance(library, str) and library.strip()
                    and (library_id is None or library_id == library)):
                scopes.add((_scope(instance, "instance_id"), _scope(library, "library_id")))
    if len(scopes) == 1:
        instance, library = next(iter(scopes))
        return instance, library, "declared_source_identity"
    return None, library_id or "0", "ambiguous" if scopes else "unknown"


def load_cached_mapping_discovery(
    workspace: Path | str, projection_rows: Sequence[Mapping[str, Any]], *,
    instance_id: str | None = None, library_id: str | None = None,
    max_cache_bytes: int = 16_000_000,
) -> dict[str, Any]:
    """Enrich a mapping job from the existing scoped cache, without acquisition.

    Missing cache or source scope is unknown, not a reason to scan attachments.
    Current catalogue ownership and conservative reference rematching are reused.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    _integer(max_cache_bytes, "max_cache_bytes", maximum=MAX_INDEX_DOCUMENT_BYTES)
    if not isinstance(projection_rows, (list, tuple)) or len(projection_rows) > 20_000:
        raise ValueError("mapping discovery requires bounded projection rows")
    catalogue = _catalogue(root)
    entries: dict[str, dict[str, Any]] = {}
    for raw in catalogue.get("sources", []):
        if (not isinstance(raw, dict) or not isinstance(raw.get("source_id"), str)
                or not raw["source_id"] or raw["source_id"] in entries):
            raise ValueError("duplicate or missing catalogue source identity")
        entries[raw["source_id"]] = raw
        if len(entries) > 20_000:
            raise ValueError("mapping discovery catalogue source count limit exceeded")
    selected, seen = [], set()
    for row in projection_rows:
        if (not isinstance(row, Mapping) or not isinstance(row.get("source_id"), str)
                or not row["source_id"] or row["source_id"] in seen):
            raise ValueError("mapping projection requires unique source IDs")
        seen.add(row["source_id"])
        entry = entries.get(row["source_id"])
        if entry is None:
            continue
        if row.get("note_id") != entry.get("note_id"):
            raise ValueError("mapping discovery source/note identity conflicts with catalogue")
        selected.append(dict(row))
    catalogue_rows = list(entries.values())
    instance, library, scope_status = _cached_discovery_scope(
        [row for row in catalogue_rows if row["source_id"] in seen], selected, instance_id, library_id,
    )
    records, status = _cached_records(root, selected, catalogue_rows, instance, library, max_bytes=max_cache_bytes)
    position_budget = _ReadBudget(root, max_files=1, max_file_bytes=MAX_INDEX_DOCUMENT_BYTES, max_total_bytes=MAX_INDEX_DOCUMENT_BYTES)
    positions_path = root / "02_source_memory/indexes/literature_positions.yml"
    position_budget.reserve([positions_path], max_nodes=MAX_INDEX_YAML_NODES)
    citation_records, citation_status = _literature_candidate_records(position_budget.read(positions_path), selected)
    return {"records": records, "citation_records": citation_records, "status": {**status, **citation_status, "scope_status": scope_status,
            "instance_id": instance, "library_id": library, "cached_document_count": len(records),
            "reference_coverage": "cached_unverified" if records else "not_examined",
            "acquisition_performed": False}}


def _cached_records(root: Path, rows: Sequence[Mapping[str, Any]], catalogue_rows: Sequence[Mapping[str, Any]], instance_id: str | None, library_id: str, *, max_bytes: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if instance_id is None or not (root / CACHE_RELATIVE_PATH).is_file():
        return [], {"cache_truncated": False, "cache_skipped_records": 0, "cache_record_bytes": 0}
    if max_bytes <= 0:
        return [], {"cache_truncated": True, "cache_skipped_records": 0, "cache_record_bytes": 0}
    from .discovery import ReferenceMatcher, load_cached_records

    cache_path = root / CACHE_RELATIVE_PATH
    if cache_path.is_symlink() or not cache_path.resolve().is_relative_to(root):
        raise ValueError("discovery cache must remain inside the workspace")
    loaded = load_cached_records(cache_path, instance_id=instance_id, library_type="user", library_id=library_id, max_bytes=max_bytes)
    by_source = {row["source_id"]: row for row in rows}
    owners: dict[str, set[str]] = {}
    for row in catalogue_rows:
        if row.get("source_id") in by_source and _catalogue_scope_matches(row, instance_id, library_id):
            for key in _catalogue_keys(row):
                owners.setdefault(key, set()).add(row["source_id"])
    by_key = {key: by_source[next(iter(source_ids))] for key, source_ids in owners.items() if len(source_ids) == 1}
    metadata_items = []
    for record in loaded["records"]:
        metadata, identity = record["metadata"], record["identity"]
        identifiers = metadata["identifiers"]
        metadata_items.append({"identity": identity, "item": {"key": identity["item_key"], "data": {
            "key": identity["item_key"], "title": metadata["title"], "creators": metadata["creators"], "date": metadata["year"] or "",
            "itemType": metadata["item_type"], "edition": metadata["edition"], "DOI": identifiers.get("doi", ""), "ISBN": identifiers.get("isbn", "")}}})
    matcher = ReferenceMatcher(_matcher_items(metadata_items, catalogue_rows, instance_id, library_id))
    selected = []
    ordered = sorted(loaded["records"], key=lambda raw: (raw.get("identity", {}).get("item_key") != (by_key.get(raw.get("identity", {}).get("item_key")) or {}).get("zotero_key"), raw["identity"]["item_key"]))
    per_source: Counter[str] = Counter()
    alias_truncated = False
    for raw in ordered:
        identity = raw.get("identity") or {}
        if (identity.get("instance_id"), identity.get("library_type"), identity.get("library_id")) != (instance_id, "user", library_id):
            continue
        binding = by_key.get(identity.get("item_key"))
        if binding is None or raw.get("source_id") not in (None, "", binding["source_id"]):
            continue
        if per_source[binding["source_id"]] >= 4:
            alias_truncated = True
            continue
        per_source[binding["source_id"]] += 1
        coverage = dict(raw.get("coverage") or {})
        references = [matcher.match(reference, identity=identity) for reference in raw["references"]]
        selected.append({**dict(raw), "source_id": binding["source_id"], "note_id": binding["note_id"], "references": references,
                         "unresolved_count": sum(not reference["match_status"].startswith("matched_") for reference in references),
                         "coverage": {**coverage, "freshness": "cached_unverified"}})
    cache_bytes = sum(len(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for row in loaded["records"])
    return selected, {"cache_truncated": loaded.get("truncated", False) or alias_truncated, "cache_skipped_records": loaded.get("skipped_records", 0), "cache_record_bytes": cache_bytes, "matching_scope": "provided scoped current index; library presence outside this index is unexamined"}


def _accepted_relationships(
    registry: Mapping[str, Any], hashes: Mapping[str, tuple[str, list[str]]], *, workspace: Path | None = None,
) -> list[dict[str, Any]]:
    from .jev_relationships import jev_relationship_is_fresh

    current = {}
    for row in registry.get("current_pair_decisions", []):
        pair = row.get("source_ids", []) if isinstance(row, Mapping) else []
        if len(pair) == 2:
            current[tuple(sorted(pair))] = row
    accepted, freshness_memo = [], {}
    for raw in registry.get("relations", []):
        if not isinstance(raw, Mapping) or raw.get("active", True) is not True or raw.get("decision_status") != "accepted" or raw.get("refresh_pending") or raw.get("reconciliation_pending"):
            continue
        pair = (str(raw.get("source_id") or ""), str(raw.get("target_source_id") or ""))
        if any(source_id not in hashes for source_id in pair):
            continue
        memory = current.get(tuple(sorted(pair)))
        if memory is not None:
            if memory.get("status") != "accepted" or memory.get("active", True) is not True or memory.get("refresh_pending") or memory.get("reconciliation_pending"):
                continue
            inputs = memory.get("input_profile_hashes") or {}
            if any(inputs.get(source_id) not in hashes[source_id][1] for source_id in pair):
                continue
            if memory.get("relation_ids") and raw.get("relation_id") not in memory["relation_ids"]:
                continue
        if any(raw.get(field) and raw[field] not in hashes[source_id][1] for field, source_id in zip(("source_profile_hash", "target_profile_hash"), pair)):
            continue
        jev = (raw.get("output_contract") == JEV_RELATIONSHIP_CONTRACT
               or raw.get("provenance") == "probabilistic_relationship_jev"
               or str(raw.get("model") or "").startswith("jev-"))
        if jev and (workspace is None or not jev_relationship_is_fresh(workspace, raw, memo=freshness_memo)):
            continue
        qualified = memory is not None or all(raw.get(field) for field in ("source_profile_hash", "target_profile_hash"))
        accepted.append({**dict(raw), "freshness": "current_profile_hashes" if qualified else "recorded_unverified"})
    return accepted


def _matcher_items(items: Sequence[Mapping[str, Any]], catalogue_rows: Sequence[Mapping[str, Any]], instance_id: str, library_id: str) -> list[dict[str, Any]]:
    """Use one actual metadata representative for each authoritative source alias set."""
    scoped_rows, owners, canonical_keys = [], {}, {}
    for row in catalogue_rows:
        key = row.get("zotero_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", key):
            continue
        if not _catalogue_scope_matches(row, instance_id, library_id):
            continue
        scoped_rows.append(row)
        if row.get("source_id"):
            canonical_keys[row["source_id"]] = key
            for alias in _catalogue_keys(row):
                owners.setdefault(alias, set()).add(row["source_id"])
    source_by_key = {key: next(iter(source_ids)) for key, source_ids in owners.items() if len(source_ids) == 1}
    known, represented = {}, set()
    ordered = sorted(items, key=lambda entry: (entry["identity"]["item_key"] != canonical_keys.get(source_by_key.get(entry["identity"]["item_key"])), entry["identity"]["item_key"]))
    for entry in ordered:
        key = entry["identity"]["item_key"]
        source_id = source_by_key.get(key)
        if source_id is not None and source_id in represented:
            continue
        known[key] = dict(entry)
        if source_id is not None:
            represented.add(source_id)
    for row in scoped_rows:
        key = row["zotero_key"]
        source_id = row.get("source_id")
        if key in known or (source_id is not None and source_id in represented):
            continue
        identity = row.get("identity") or {}
        known[key] = {"identity": {"instance_id": instance_id, "library_type": "user", "library_id": library_id, "item_key": key},
                      "item": {"key": key, "data": {"key": key, "DOI": identity.get("doi") or row.get("doi") or "", "ISBN": identity.get("isbn") or row.get("isbn") or ""}}}
        if source_id is not None:
            represented.add(source_id)
        if len(known) > 20_000:
            raise ValueError("known matching metadata limit exceeded")
    return list(known.values())
