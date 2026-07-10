from __future__ import annotations

import json
import mimetypes
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .controller import LocalController
from .extraction import extract_bytes, extract_path, ocr_pdf_bytes
from .files import (
    append_jsonl,
    atomic_write_bytes,
    now_iso,
    read_yaml,
    safe_filename,
    sha256_bytes,
    sha256_file,
    sha256_text,
    slugify,
    write_json,
    write_yaml,
)
from .indexes import (
    accepted_tags_by_note,
    build_typed_links,
    commit_tag_reviews,
    update_source_set_map,
    write_source_index,
    write_source_set,
)
from .literature import build_literature_map
from .models import ArtifactManifest, MapRequest, RunReport
from .notes import (
    item_data,
    item_key,
    note_id_for_item,
    original_tags,
    parse_atomic_note,
    propose_tags,
    read_note,
    source_id_for_item,
    update_note_graph,
    validate_atomic_note,
    write_atomic_note,
)
from .ports import ControllerPort, ReaderProvider, VisionProvider, ZoteroClient
from .readers import SECTION_KEYS, provider_from_name
from .workspace import artifact_rows, assert_compatible, initialize, resolve_workspace, run_directory, validate_opaque_id
from .zotero import ZoteroLocalClient

FULL_DOCUMENT_CHAR_LIMIT = 320_000
CHUNK_CHAR_LIMIT = 80_000
MAX_DOCUMENT_CHUNKS = 64
CHUNKING_VERSION = "1"


def run_pipeline(
    request: MapRequest,
    *,
    client: ZoteroClient | None = None,
    reader: ReaderProvider | None = None,
    vision: VisionProvider | None = None,
    controller: ControllerPort | None = None,
    run_id: str | None = None,
    resume: bool = False,
) -> RunReport:
    workspace = resolve_workspace(request.workspace)
    initialize(workspace)
    assert_compatible(workspace)
    run_id = run_id or _new_run_id()
    validate_opaque_id(run_id, field="run_id")
    run_dir = run_directory(workspace, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(run_dir / "request.yml", request.to_dict())

    client = client or ZoteroLocalClient()
    controller = controller or LocalController()
    try:
        reader = reader or provider_from_name(request.provider, request.model, allow_cloud=request.allow_cloud)
    except Exception as exc:
        return _blocked_report(request, run_id, f"reader_configuration:{type(exc).__name__}:{exc}")
    preflight_reason = _reader_preflight_reason(reader, request.allow_cloud)
    if preflight_reason:
        return _blocked_report(request, run_id, preflight_reason)
    if vision is None and hasattr(reader, "inspect_document"):
        vision = reader  # type: ignore[assignment]

    effective_collection_key = request.collection_key
    inventory_scope = request.scope
    try:
        if request.scope == "selected":
            selected = client.selected_collection()
            effective_collection_key = str(selected.get("key") or "")
            inventory_scope = "library" if selected.get("scope") == "library" else "collection"
            if inventory_scope == "collection" and not effective_collection_key:
                raise ValueError("selected collection has no key")
        items = client.inventory(inventory_scope, effective_collection_key)
    except Exception as exc:
        return _blocked_report(request, run_id, f"zotero_inventory:{type(exc).__name__}:{exc}")
    items = [dict(item) for item in items if isinstance(item, Mapping)]
    if request.limit:
        items = items[: request.limit]
    inventory_path = workspace / "01_custody" / "zotero" / "inventory" / f"{slugify(run_id)}.json"
    write_json(inventory_path, items)
    write_json(run_dir / "inventory.json", items)
    if effective_collection_key:
        write_yaml(
            workspace / "01_custody" / "zotero" / "collections" / f"{slugify(effective_collection_key)}.yml",
            {
                "collection_key": effective_collection_key,
                "scope": request.scope,
                "run_id": run_id,
                "zotero_item_keys": [item_key(item) for item in items],
                "sync_zotero_collection": False,
                "updated_at": now_iso(),
            },
        )

    del resume  # fingerprints, not row positions, determine resumability
    prepared: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    pending: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        key = item_key(item)
        if key and key in seen_keys:
            prepared.append(_duplicate_result(index, item))
            continue
        if key:
            seen_keys.add(key)
        pending.append((index, item))

    workers = max(1, min(request.parallel, len(pending) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="auto-zettelkasten") as executor:
        future_map = {
            executor.submit(
                _prepare_item,
                workspace,
                index,
                item,
                request,
                client,
                reader,
                vision,
            ): index
            for index, item in pending
        }
        for future in as_completed(future_map):
            try:
                prepared.append(future.result())
            except Exception as exc:  # defensive terminal accounting at the worker boundary
                index = future_map[future]
                prepared.append(
                    _exhausted_result(
                        index,
                        items[index],
                        "pipeline_worker",
                        f"unhandled_worker_error:{type(exc).__name__}:{exc}",
                    )
                )
    prepared.sort(key=lambda row: int(row.get("inventory_index", 0)))

    proposals = [proposal for row in prepared if row.get("analysis") for proposal in propose_tags(row["item"], row["note_id"])]
    decisions = _review_tags(controller, proposals)
    tag_report = commit_tag_reviews(workspace, proposals, decisions)
    normalized_by_note = accepted_tags_by_note(decisions)

    terminal_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    failed_note_ids: set[str] = set()
    attempt_path = workspace / "01_custody" / "read_attempts" / f"{slugify(run_id)}.jsonl"
    for row in prepared:
        for attempt in row.pop("attempts", []):
            append_jsonl(attempt_path, attempt)
        if row.get("reused") and row.get("note_path"):
            terminal_rows.append(_public_terminal_row(row))
            note_rows.append(_note_summary_from_path(workspace, row))
            continue
        if not row.get("analysis"):
            terminal_rows.append(_public_terminal_row(row))
            continue

        normalized_tags = normalized_by_note.get(str(row["note_id"]), [])
        frontmatter = _frontmatter(row, request, normalized_tags)
        try:
            path, validation = write_atomic_note(workspace, frontmatter, row["analysis"])
        except Exception as exc:
            row.update(
                terminal_status="exhausted",
                reason=f"atomic_note_commit_failed:{type(exc).__name__}:{exc}",
                note_path="",
            )
            failed_note_ids.add(str(row["note_id"]))
            append_jsonl(attempt_path, _attempt(row, "atomic_note_commit", "failed", row["reason"]))
            terminal_rows.append(_public_terminal_row(row))
            continue
        if not validation.passed:
            row.update(
                terminal_status="exhausted",
                reason="atomic_note_validation_failed:" + ",".join(validation.errors),
                note_path="",
            )
            failed_note_ids.add(str(row["note_id"]))
            append_jsonl(
                attempt_path,
                _attempt(row, "atomic_note_validation", "failed", row["reason"], output_path=str(path)),
            )
            terminal_rows.append(_public_terminal_row(row))
            continue
        relative_path = str(path.relative_to(workspace))
        row.update(terminal_status="validated_note", note_path=relative_path, note_status="analytical_atomic_note")
        fingerprint_path = workspace / "11_state" / "fingerprints" / f"{row['fingerprint']}.yml"
        write_yaml(
            fingerprint_path,
            {
                "fingerprint": row["fingerprint"],
                "zotero_item_key": row["zotero_item_key"],
                "note_id": row["note_id"],
                "source_id": row["source_id"],
                "note_path": relative_path,
                "content_hash": row["content_hash"],
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "updated_at": now_iso(),
            },
        )
        terminal_rows.append(_public_terminal_row(row))
        note_rows.append(_note_summary_from_path(workspace, row))

    if failed_note_ids:
        decisions = [
            {
                **row,
                "decision": "parked",
                "decision_reason": "atomic_note_not_committed",
            }
            if str(row.get("note_id", "")) in failed_note_ids
            else row
            for row in decisions
        ]
        tag_report = commit_tag_reviews(workspace, proposals, decisions)

    note_rows = _deduplicate_note_rows(note_rows)
    run_source_set = write_source_set(
        workspace,
        run_id=run_id,
        scope=request.scope,
        collection_key=effective_collection_key,
        items=items,
        terminal_rows=terminal_rows,
        note_rows=note_rows,
    )
    workspace_note_rows = all_workspace_note_rows(workspace)
    map_source_set = workspace_source_set(workspace, workspace_note_rows, run_id=run_id)
    map_result = rebuild_map(
        workspace,
        source_set=map_source_set,
        note_rows=workspace_note_rows,
        terminal_rows=[],
        items=[],
        run_id=run_id,
        question=request.question,
    )
    run_note_ids = {str(row.get("note_id", "")) for row in note_rows}
    relevant_clusters = [
        cluster
        for cluster in map_result["cluster_map"]["clusters"]
        if run_note_ids & {str(note_id) for note_id in cluster.get("note_ids", [])}
    ]
    relevant_cluster_ids = {str(cluster["cluster_id"]) for cluster in relevant_clusters}
    relevant_gaps = [
        gap
        for gap in map_result["gap_map"]["gap_candidates"]
        if relevant_cluster_ids & {str(cluster_id) for cluster_id in gap.get("related_clusters", [])}
    ]
    run_source_set = update_source_set_map(workspace, run_source_set, relevant_clusters, relevant_gaps)

    validated_count = sum(1 for row in terminal_rows if row.get("terminal_status") == "validated_note")
    exhausted_count = sum(1 for row in terminal_rows if row.get("terminal_status") == "exhausted")
    reused_count = sum(1 for row in prepared if row.get("reused") and row.get("terminal_status") == "validated_note")
    status = "completed" if exhausted_count == 0 else "completed_with_exhausted_items"
    errors = [
        {"zotero_item_key": row.get("zotero_item_key", ""), "reason": row.get("reason", "")}
        for row in terminal_rows
        if row.get("terminal_status") == "exhausted"
    ]
    created_paths = [
        inventory_path,
        run_dir / "inventory.json",
        attempt_path,
        Path(run_source_set["path"]),
        Path(map_result["source_set"]["path"]),
        Path(tag_report["proposal_path"]),
        Path(tag_report["registry_path"]),
        *map_result["paths"],
        *[workspace / str(row["note_path"]) for row in terminal_rows if row.get("note_path")],
    ]
    manifest = ArtifactManifest(
        status="built",
        workspace=workspace,
        run_id=run_id,
        created_at=now_iso(),
        artifacts=artifact_rows(workspace, created_paths),
        metadata={
            "source_set": run_source_set,
            "map_source_set": map_result["source_set"],
            "cluster_map": map_result["cluster_map"],
            "gap_map": map_result["gap_map"],
            "literature_packet": map_result["literature_packet"],
            "tag_review": tag_report,
        },
    )
    write_yaml(run_dir / "artifact_manifest.yml", manifest.to_dict())
    report = RunReport(
        status=status,
        workspace=workspace,
        run_id=run_id,
        inventory_count=len(items),
        validated_note_count=validated_count,
        exhausted_count=exhausted_count,
        reused_count=reused_count,
        source_set_id=str(run_source_set["source_set_id"]),
        items=terminal_rows,
        errors=errors,
        source_set=run_source_set,
        cluster_map=map_result["cluster_map"],
        gap_map=map_result["gap_map"],
        literature_packet=map_result["literature_packet"],
        artifact_manifest=manifest,
    )
    write_yaml(run_dir / "run_report.yml", report.to_dict())
    return report


def rebuild_map(
    workspace: Path,
    *,
    source_set: Mapping[str, Any],
    note_rows: Sequence[Mapping[str, Any]],
    terminal_rows: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    run_id: str,
    question: str | None,
) -> dict[str, Any]:
    typed = build_typed_links(workspace, note_rows)
    cluster_map, gap_map, packet, paths = build_literature_map(
        workspace,
        source_set=source_set,
        notes=note_rows,
        question=question,
        run_id=run_id,
    )
    related: dict[str, list[dict[str, Any]]] = {}
    for link in typed["links"]:
        left, right = str(link["source_note_id"]), str(link["target_note_id"])
        relation_type = str(link["relation_type"])
        reverse_type = "cited_by" if relation_type == "cites" else ("cites" if relation_type == "cited_by" else relation_type)
        related.setdefault(left, []).append({"note_id": right, "relation_type": relation_type})
        related.setdefault(right, []).append({"note_id": left, "relation_type": reverse_type})
    clusters_by_note: dict[str, list[str]] = {}
    for cluster in cluster_map["clusters"]:
        for note_id in cluster.get("note_ids", []):
            clusters_by_note.setdefault(str(note_id), []).append(str(cluster["cluster_id"]))
    note_stem_by_id = {str(row["note_id"]): Path(str(row["note_path"])).stem for row in note_rows}
    note_paths: list[Path] = []
    for row in note_rows:
        path = workspace / str(row["note_path"])
        if not path.exists():
            continue
        related_links = [
            {**link, "target_stem": note_stem_by_id.get(str(link["note_id"]), str(link["note_id"]))}
            for link in sorted(related.get(str(row["note_id"]), []), key=lambda value: (value["note_id"], value["relation_type"]))
        ]
        cluster_ids = sorted(clusters_by_note.get(str(row["note_id"]), []))
        update_note_graph(
            path,
            {
                "related_notes": [
                    {"note_id": link["note_id"], "relation_type": link["relation_type"], "wikilink": f"[[{link['target_stem']}]]"}
                    for link in related_links
                ],
                "clusters": cluster_ids,
                "updated_at": now_iso(),
            },
            related_links,
            cluster_ids,
        )
        note_paths.append(path)
    source_index = write_source_index(workspace, note_paths)
    source_set = update_source_set_map(workspace, source_set, cluster_map["clusters"], gap_map["gap_candidates"])
    return {
        "source_set": source_set,
        "cluster_map": cluster_map,
        "gap_map": gap_map,
        "literature_packet": packet,
        "typed_links": typed,
        "paths": [Path(typed["path"]), Path(typed["compatibility_path"]), source_index, *paths, Path(source_set["path"])],
    }


def all_workspace_note_rows(workspace: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((workspace / "02_source_memory" / "notes").glob("*.md")):
        try:
            frontmatter, _ = parse_atomic_note(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if frontmatter.get("note_status") not in {
            "analytical_atomic_note",
            "verified_atomic_note",
            "fulltext_available",
            "abstract_only_atomic_note",
            "metadata_only_source_note",
        }:
            continue
        if not frontmatter.get("note_id") or not frontmatter.get("source_id"):
            continue
        rows.append(
            _note_summary_from_path(
                workspace,
                {
                    "note_id": frontmatter.get("note_id", ""),
                    "source_id": frontmatter.get("source_id", ""),
                    "zotero_item_key": frontmatter.get("zotero_item_key", ""),
                    "note_path": str(path.relative_to(workspace)),
                },
            )
        )
    return _deduplicate_note_rows(rows)


def workspace_source_set(workspace: Path, note_rows: Sequence[Mapping[str, Any]], *, run_id: str) -> dict[str, Any]:
    items = [
        {
            "key": str(row.get("zotero_item_key", "")),
            "data": {"key": str(row.get("zotero_item_key", "")), "title": str(row.get("title", ""))},
        }
        for row in note_rows
    ]
    terminal_rows = [
        {
            "inventory_index": index,
            "zotero_item_key": row.get("zotero_item_key", ""),
            "source_id": row.get("source_id", ""),
            "note_id": row.get("note_id", ""),
            "note_path": row.get("note_path", ""),
            "terminal_status": (
                "validated_note"
                if row.get("note_status") in {"analytical_atomic_note", "verified_atomic_note"}
                else "indexed_limited_note"
            ),
            "fingerprint": "",
        }
        for index, row in enumerate(note_rows)
    ]
    return write_source_set(
        workspace,
        run_id=run_id,
        scope="workspace",
        collection_key=None,
        items=items,
        terminal_rows=terminal_rows,
        note_rows=note_rows,
        source_set_id="source-set-auto-zettelkasten-workspace",
        source_set_type="auto_zettelkasten_workspace",
    )


def _prepare_item(
    workspace: Path,
    index: int,
    item: dict[str, Any],
    request: MapRequest,
    client: ZoteroClient,
    reader: ReaderProvider,
    vision: VisionProvider | None,
) -> dict[str, Any]:
    key = item_key(item)
    base = {
        "inventory_index": index,
        "item": item,
        "zotero_item_key": key,
        "source_id": source_id_for_item(item),
        "note_id": note_id_for_item(item),
        "attempts": [],
        "terminal_status": "exhausted",
        "reason": "",
        "reader_provider": str(getattr(reader, "name", request.provider)),
        "reader_model": str(getattr(reader, "model", request.model)),
    }
    if not key:
        return _exhausted_result(index, item, "identity", "missing_zotero_item_key")
    content = _acquire_content(workspace, item, client, base, request, vision)
    if not content:
        base["reason"] = "all_allowed_extraction_routes_exhausted"
        base["attempts"].append(
            _attempt(base, "extraction_router", "failed", "all_allowed_extraction_routes_exhausted")
        )
        return base
    content_hash = str(content["content_hash"])
    effective_provider = str(content.get("reader_provider") or base["reader_provider"])
    effective_model = str(content.get("reader_model") or base["reader_model"])
    fingerprint = _fingerprint(key, content_hash, request, item, effective_provider, effective_model)
    base.update(
        content,
        fingerprint=fingerprint,
        reader_provider=effective_provider,
        reader_model=effective_model,
    )
    prior = read_yaml(workspace / "11_state" / "fingerprints" / f"{fingerprint}.yml", {}) or {}
    prior_path = workspace / str(prior.get("note_path", ""))
    if prior.get("note_path") and _reusable_note(prior_path, base, request):
        base.update(
            terminal_status="validated_note",
            note_path=str(prior["note_path"]),
            note_status="analytical_atomic_note",
            reused=True,
            reason="fingerprint_match",
        )
        base["attempts"].append(_attempt(base, "resume_fingerprint", "skipped", "existing_validated_note_reused", output_path=str(prior_path)))
        return base
    if content.get("analysis"):
        base["analysis"] = content["analysis"]
        return base
    if bool(getattr(reader, "is_cloud", True)) and not request.allow_cloud:
        base["attempts"].append(_attempt(base, f"{reader.name}_text", "disallowed", "cloud_not_allowed"))
        base["reason"] = "reader_disallowed_by_privacy_policy"
        return base
    try:
        analysis, reader_route, reader_reason = _read_document(
            reader,
            str(content["text"]),
            item_data(item),
            request.question,
        )
    except Exception as exc:
        base["attempts"].append(_attempt(base, f"{reader.name}_text", "failed", f"{type(exc).__name__}:{exc}"))
        base["reason"] = f"reader_failed:{type(exc).__name__}"
        return base
    base["attempts"].append(_attempt(base, reader_route, "succeeded", reader_reason, output_path="pending_atomic_note"))
    base["analysis"] = dict(analysis)
    return base


def _acquire_content(
    workspace: Path,
    item: Mapping[str, Any],
    client: ZoteroClient,
    base: dict[str, Any],
    request: MapRequest,
    vision: VisionProvider | None,
) -> dict[str, Any] | None:
    key = item_key(item)
    targets: list[Mapping[str, Any]] = [item]
    try:
        children = client.children(key)
        targets.extend(children)
        if not children:
            base["attempts"].append(_attempt(base, "zotero_children", "skipped", "no_child_attachments"))
    except Exception as exc:
        base["attempts"].append(_attempt(base, "zotero_children", "failed", f"{type(exc).__name__}:{exc}"))
    for target in targets:
        target_key = item_key(target)
        if not target_key:
            continue
        try:
            fulltext = client.fulltext(target_key)
        except Exception as exc:
            base["attempts"].append(_attempt(base, "zotero_fulltext", "failed", f"{target_key}:{type(exc).__name__}:{exc}"))
            fulltext = None
        text = _fulltext_value(fulltext)
        fulltext_complete = _fulltext_complete(fulltext)
        if len(text.strip()) >= 40 and fulltext_complete:
            content_hash = sha256_text(text)
            base["attempts"].append(_attempt(base, "zotero_fulltext", "succeeded", "indexed_fulltext", input_hash=content_hash))
            return {
                "text": text,
                "content_hash": content_hash,
                "source_file": f"zotero://select/library/items/{target_key}",
                "content_route": "zotero_fulltext",
                "media_type": str((fulltext or {}).get("contentType") or "text/plain"),
            }
        if text and not fulltext_complete:
            base["attempts"].append(
                _attempt(base, "zotero_fulltext", "failed", f"{target_key}:partial_indexed_fulltext", input_hash=sha256_text(text))
            )
        elif text:
            base["attempts"].append(
                _attempt(base, "zotero_fulltext", "failed", f"{target_key}:insufficient_indexed_fulltext", input_hash=sha256_text(text))
            )
        elif fulltext is not None:
            base["attempts"].append(_attempt(base, "zotero_fulltext", "skipped", f"{target_key}:indexed_fulltext_empty"))
        data = item_data(target)
        local = _local_attachment_path(data)
        if local:
            extracted = extract_path(local)
            base["attempts"].append(
                _attempt(
                    base,
                    extracted.route,
                    "succeeded" if extracted.status == "succeeded" else "failed",
                    extracted.reason or "extracted",
                    input_hash=sha256_file(local),
                    output_path=str(local),
                )
            )
            if extracted.status == "succeeded":
                return {
                    "text": extracted.text,
                    "content_hash": sha256_file(local),
                    "source_file": str(local),
                    "content_route": extracted.route,
                    "media_type": extracted.media_type,
                }
            if local.suffix.lower() == ".pdf" and vision is not None:
                document = local.read_bytes()
            elif local.suffix.lower() == ".pdf":
                document = local.read_bytes()
            else:
                document = b""
            if document:
                ocr = ocr_pdf_bytes(document)
                base["attempts"].append(
                    _attempt(
                        base,
                        ocr.route,
                        "succeeded" if ocr.status == "succeeded" else "failed",
                        ocr.reason or "ocr_extracted",
                        input_hash=sha256_bytes(document),
                        output_path=str(local),
                    )
                )
                if ocr.status == "succeeded":
                    return {
                        "text": ocr.text,
                        "content_hash": sha256_bytes(document),
                        "source_file": str(local),
                        "content_route": ocr.route,
                        "media_type": "application/pdf",
                    }
                if vision is not None:
                    visual = _vision_content(base, vision, request, document, "application/pdf", item_data(item), str(local))
                    if visual:
                        return visual
        if target is item and str(data.get("itemType", "")) != "attachment":
            continue
        try:
            file_result = client.file(target_key)
        except Exception as exc:
            base["attempts"].append(_attempt(base, "zotero_file", "failed", f"{target_key}:{type(exc).__name__}:{exc}"))
            file_result = None
        if not file_result:
            base["attempts"].append(_attempt(base, "zotero_file", "skipped", f"{target_key}:attachment_file_unavailable"))
            continue
        document, media_type = file_result
        extension = mimetypes.guess_extension(media_type) or Path(str(data.get("filename") or "")).suffix or ".bin"
        custody_path = workspace / "01_custody" / "files" / f"{safe_filename(target_key)}{extension}"
        atomic_write_bytes(custody_path, document)
        extracted = extract_bytes(document, media_type=media_type, filename=custody_path.name)
        document_hash = sha256_bytes(document)
        base["attempts"].append(
            _attempt(base, extracted.route, "succeeded" if extracted.status == "succeeded" else "failed", extracted.reason or "extracted", input_hash=document_hash, output_path=str(custody_path))
        )
        if extracted.status == "succeeded":
            return {
                "text": extracted.text,
                "content_hash": document_hash,
                "source_file": str(custody_path),
                "content_route": extracted.route,
                "media_type": extracted.media_type,
            }
        if media_type == "application/pdf":
            ocr = ocr_pdf_bytes(document)
            base["attempts"].append(
                _attempt(
                    base,
                    ocr.route,
                    "succeeded" if ocr.status == "succeeded" else "failed",
                    ocr.reason or "ocr_extracted",
                    input_hash=document_hash,
                    output_path=str(custody_path),
                )
            )
            if ocr.status == "succeeded":
                return {
                    "text": ocr.text,
                    "content_hash": document_hash,
                    "source_file": str(custody_path),
                    "content_route": ocr.route,
                    "media_type": media_type,
                }
            if vision is not None:
                visual = _vision_content(base, vision, request, document, media_type, item_data(item), str(custody_path))
                if visual:
                    return visual
    return None


def _vision_content(
    base: dict[str, Any],
    vision: VisionProvider,
    request: MapRequest,
    document: bytes,
    media_type: str,
    metadata: Mapping[str, Any],
    source_file: str,
) -> dict[str, Any] | None:
    document_hash = sha256_bytes(document)
    if bool(getattr(vision, "is_cloud", True)) and not request.allow_cloud:
        base["attempts"].append(_attempt(base, f"{vision.name}_document_vision", "disallowed", "cloud_not_allowed", input_hash=document_hash))
        return None
    try:
        analysis = vision.inspect_document(document, media_type, metadata, request.question)
    except Exception as exc:
        base["attempts"].append(
            _attempt(base, f"{vision.name}_document_vision", "failed", f"{type(exc).__name__}:{exc}", input_hash=document_hash)
        )
        return None
    base["attempts"].append(
        _attempt(base, f"{vision.name}_document_vision", "succeeded", "document_inspected", input_hash=document_hash, output_path=source_file)
    )
    return {
        "analysis": dict(analysis),
        "content_hash": document_hash,
        "source_file": source_file,
        "content_route": f"{vision.name}_document_vision",
        "media_type": media_type,
        "reader_provider": str(getattr(vision, "name", "vision")),
        "reader_model": str(getattr(vision, "model", "unknown")),
    }


def _frontmatter(row: Mapping[str, Any], request: MapRequest, normalized_tags: Sequence[str]) -> dict[str, Any]:
    data = item_data(row["item"])
    return {
        "note_id": row["note_id"],
        "source_id": row["source_id"],
        "note_status": "analytical_atomic_note",
        "title": str(data.get("title") or row["zotero_item_key"] or "Untitled Zotero item"),
        "citation_key": _citation_key(data),
        "zotero_item_key": row["zotero_item_key"],
        "source_file": row["source_file"],
        "creators": data.get("creators", []) if isinstance(data.get("creators", []), list) else [],
        "date": str(data.get("date") or ""),
        "doi": str(data.get("DOI") or data.get("doi") or ""),
        "url": str(data.get("url") or ""),
        "original_zotero_tags": original_tags(row["item"]),
        "zotero_relations": data.get("relations", {}) if isinstance(data.get("relations", {}), Mapping) else {},
        "normalized_tags": list(normalized_tags),
        "clusters": [],
        "aliases": [str(data.get("title") or "")],
        "related_notes": [],
        "inspected_content_hash": row["content_hash"],
        "content_route": row["content_route"],
        "reader_provider": row["reader_provider"],
        "reader_model": row["reader_model"],
        "extraction_version": request.extraction_version,
        "prompt_version": request.prompt_version,
        "engine_version": ENGINE_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def _note_summary_from_path(workspace: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    path = workspace / str(row.get("note_path", ""))
    note = read_note(path)
    front = note["frontmatter"]
    return {
        "note_id": str(front.get("note_id", row.get("note_id", ""))),
        "source_id": str(front.get("source_id", row.get("source_id", ""))),
        "zotero_item_key": str(front.get("zotero_item_key", row.get("zotero_item_key", ""))),
        "note_status": str(front.get("note_status", "")),
        "title": str(front.get("title", "")),
        "date": str(front.get("date", "")),
        "method": _note_section(note["body"], "Method and Research Design")[:240],
        "original_zotero_tags": list(front.get("original_zotero_tags", []) or []),
        "normalized_tags": list(front.get("normalized_tags", []) or []),
        "zotero_relations": dict(front.get("zotero_relations", {}) or {}),
        "note_path": str(path.relative_to(workspace)),
        "note_hash": note["sha256"],
    }


def _review_tags(controller: ControllerPort, proposals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not proposals:
        return []
    try:
        provided = [dict(row) for row in controller.review_tag_proposals(proposals)]
    except Exception:
        provided = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in provided:
        if row.get("proposal_id"):
            by_id.setdefault(str(row["proposal_id"]), []).append(row)
    decisions = []
    for proposal in proposals:
        matches = by_id.get(str(proposal["proposal_id"]), [])
        row = dict(proposal)
        if len(matches) == 1:
            row["decision"] = matches[0].get("decision")
            row["decision_reason"] = str(matches[0].get("decision_reason") or "")
        elif len(matches) > 1:
            row.update(decision="parked", decision_reason="controller_returned_duplicate_decisions")
        if row.get("decision") not in {"accepted", "parked", "rejected"}:
            row.update(decision="parked", decision_reason="controller_returned_no_valid_decision")
        decisions.append(row)
    return decisions


def _fingerprint(
    key: str,
    content_hash: str,
    request: MapRequest,
    item: Mapping[str, Any],
    reader_provider: str,
    reader_model: str,
) -> str:
    metadata = item_data(item)
    prompt_metadata = {
        field: metadata.get(field)
        for field in (
            "title",
            "creators",
            "date",
            "publicationTitle",
            "publisher",
            "DOI",
            "doi",
            "url",
            "itemType",
            "tags",
            "relations",
            "citationKey",
            "extra",
        )
    }
    payload = {
        "zotero_item_key": key,
        "content_hash": content_hash,
        "extraction_version": request.extraction_version,
        "prompt_version": request.prompt_version,
        "reader_provider": reader_provider,
        "reader_model": reader_model,
        "question_hash": sha256_text(request.question or ""),
        "metadata_hash": sha256_text(json.dumps(prompt_metadata, sort_keys=True, ensure_ascii=False, default=str)),
        "chunking_version": CHUNKING_VERSION,
    }
    return sha256_text(json.dumps(payload, sort_keys=True))


def _attempt(
    row: Mapping[str, Any],
    route: str,
    status: str,
    reason: str,
    *,
    input_hash: str = "",
    output_path: str = "",
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "source_id": row.get("source_id", ""),
        "zotero_item_key": row.get("zotero_item_key", ""),
        "route": route,
        "model_or_tool": route,
        "status": status,
        "reason": reason,
        "input_hash": input_hash or row.get("content_hash", ""),
        "output_path": output_path,
        "cost_estimate": 0,
        "started_at": timestamp,
        "completed_at": timestamp,
    }


def _exhausted_result(index: int, item: Mapping[str, Any], route: str, reason: str) -> dict[str, Any]:
    row = {
        "inventory_index": index,
        "item": dict(item),
        "zotero_item_key": item_key(item),
        "source_id": source_id_for_item(item),
        "note_id": note_id_for_item(item),
        "terminal_status": "exhausted",
        "reason": reason,
        "fingerprint": "",
        "note_path": "",
        "attempts": [],
    }
    row["attempts"].append(_attempt(row, route, "failed", reason))
    return row


def _duplicate_result(index: int, item: Mapping[str, Any]) -> dict[str, Any]:
    return _exhausted_result(index, item, "identity_reconciliation", "duplicate_zotero_item_key")


def _public_terminal_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "inventory_index": int(row.get("inventory_index", 0)),
        "zotero_item_key": str(row.get("zotero_item_key", "")),
        "source_id": str(row.get("source_id", "")),
        "note_id": str(row.get("note_id", "")),
        "note_path": str(row.get("note_path", "")),
        "terminal_status": str(row.get("terminal_status", "exhausted")),
        "reason": str(row.get("reason", "")),
        "fingerprint": str(row.get("fingerprint", "")),
        "content_hash": str(row.get("content_hash", "")),
    }


def _blocked_report(request: MapRequest, run_id: str, reason: str) -> RunReport:
    workspace = resolve_workspace(request.workspace)
    report = RunReport(status="blocked", workspace=workspace, run_id=run_id, errors=[{"reason": reason}])
    run_dir = run_directory(workspace, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(run_dir / "run_report.yml", report.to_dict())
    return report


def _fulltext_value(value: Mapping[str, Any] | None) -> str:
    if not value:
        return ""
    for key in ("content", "text", "fulltext"):
        if value.get(key):
            return str(value[key]).strip()
    return ""


def _fulltext_complete(value: Mapping[str, Any] | None) -> bool:
    if not value:
        return False
    for indexed_field, total_field in (("indexedPages", "totalPages"), ("indexedChars", "totalChars")):
        indexed = value.get(indexed_field)
        total = value.get(total_field)
        if indexed is None or total is None:
            continue
        try:
            if int(total) > 0 and int(indexed) < int(total):
                return False
        except (TypeError, ValueError):
            continue
    return True


def _reusable_note(path: Path, row: Mapping[str, Any], request: MapRequest) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_atomic_note(text)
    except OSError:
        return False
    if not validate_atomic_note(text).passed:
        return False
    return all(
        (
            str(frontmatter.get("zotero_item_key", "")) == str(row.get("zotero_item_key", "")),
            str(frontmatter.get("inspected_content_hash", "")) == str(row.get("content_hash", "")),
            str(frontmatter.get("reader_provider", "")) == str(row.get("reader_provider", "")),
            str(frontmatter.get("reader_model", "")) == str(row.get("reader_model", "")),
            str(frontmatter.get("extraction_version", "")) == request.extraction_version,
            str(frontmatter.get("prompt_version", "")) == request.prompt_version,
        )
    )


def _local_attachment_path(data: Mapping[str, Any]) -> Path | None:
    for key in ("source_file", "sourceFile", "local_path", "localPath", "path"):
        value = str(data.get(key) or "")
        if not value or value.startswith("attachments:") or value.startswith("storage:"):
            continue
        path = Path(value).expanduser()
        if path.is_absolute() and path.exists() and path.is_file():
            return path.resolve()
    return None


def _citation_key(data: Mapping[str, Any]) -> str:
    direct = data.get("citationKey") or data.get("citation_key")
    if direct:
        return str(direct)
    extra = str(data.get("extra") or "")
    for line in extra.splitlines():
        if line.casefold().startswith("citation key:"):
            return line.split(":", 1)[1].strip()
    return ""


def _note_section(body: str, heading: str) -> str:
    import re

    match = re.search(rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _deduplicate_note_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("note_id"):
            by_id[str(row["note_id"])] = dict(row)
    return sorted(by_id.values(), key=lambda row: row["note_id"])


def _new_run_id() -> str:
    import secrets
    from datetime import UTC, datetime

    return f"az-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _read_document(
    reader: ReaderProvider,
    text: str,
    metadata: Mapping[str, Any],
    question: str | None,
) -> tuple[Mapping[str, Any], str, str]:
    if len(text) <= FULL_DOCUMENT_CHAR_LIMIT:
        try:
            return reader.read_source(text, metadata, question), f"{reader.name}_text", "full_document_source_read"
        except Exception as exc:
            message = str(exc).casefold()
            if not any(token in message for token in ("context", "token", "too long", "too large", "request size", "length")):
                raise
    chunks = _split_document(text)
    analyses = [reader.read_source(chunk, metadata, question) for chunk in chunks]
    merged = {
        key: "\n\n".join(
            f"Chunk {index + 1}/{len(analyses)}: {str(analysis.get(key, '')).strip()}"
            for index, analysis in enumerate(analyses)
            if str(analysis.get(key, "")).strip()
        )
        for key in SECTION_KEYS
    }
    return merged, f"{reader.name}_chunked_text", f"chunked_source_read:{len(chunks)}"


def _split_document(text: str) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        pieces = [paragraph[index : index + CHUNK_CHAR_LIMIT] for index in range(0, len(paragraph), CHUNK_CHAR_LIMIT)] or [""]
        for piece in pieces:
            addition = len(piece) + (2 if current else 0)
            if current and current_size + addition > CHUNK_CHAR_LIMIT:
                chunks.append("\n\n".join(current))
                current = []
                current_size = 0
            current.append(piece)
            current_size += len(piece) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    if len(chunks) > MAX_DOCUMENT_CHUNKS:
        raise ValueError(f"document requires {len(chunks)} chunks; maximum is {MAX_DOCUMENT_CHUNKS}")
    return [f"--- Document Chunk {index + 1}/{len(chunks)} ---\n{chunk}" for index, chunk in enumerate(chunks)]


def _reader_preflight_reason(reader: ReaderProvider, allow_cloud: bool) -> str:
    if bool(getattr(reader, "is_cloud", True)) and not allow_cloud:
        return f"cloud_reader_requires_allow_cloud:{getattr(reader, 'name', 'unknown')}"
    key_environment = getattr(reader, "api_key_env", "")
    if key_environment and not os.getenv(str(key_environment)):
        return f"missing_provider_api_key:{key_environment}"
    if getattr(reader, "name", "") == "gemini" and not os.getenv("GEMINI_API_KEY"):
        return "missing_provider_api_key:GEMINI_API_KEY"
    return ""
