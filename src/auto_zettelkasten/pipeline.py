from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .controller import LocalController
from .extraction import (
    ContentAdequacy,
    classify_content_adequacy,
    classify_metadata_only,
    extract_bytes,
    extract_path,
    ocr_pdf_bytes,
)
from .files import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_text,
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
    commit_tag_reviews,
    update_source_set_map,
    write_source_index,
    write_source_set,
)
from .navigation import build_typed_source_relations, rank_human_related_links
from .literature import (
    build_literature_map,
    cluster_display_title,
    cluster_note_stem,
    gap_display_title,
    gap_note_stem,
    stable_literature_map_id,
)
from .migration import migrate_workspace, review_hash_aliases
from .models import (
    ArtifactManifest,
    LiteratureMapRequest,
    MapRequest,
    ProcessingPolicy,
    RunReport,
)
from .notes import (
    item_data,
    item_key,
    note_id_for_item,
    original_tags,
    parse_atomic_note,
    propose_tags,
    read_note,
    semantic_note_hash,
    source_id_for_item,
    update_note_graph,
    validate_note,
    write_atomic_note,
    write_limited_note,
)
from .ports import (
    ControllerPort,
    ExternalDiscoveryProvider,
    LiteratureReasoner,
    ReaderProvider,
    VisionProvider,
    ZoteroClient,
)
from .profiles import (
    COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION,
    PROFILE_ALGORITHM_VERSION,
    PROFILE_CLASSIFIER_VERSION,
    PROFILE_PROMPT_VERSION,
    ProfileContractError,
    ProfileParseError,
    augment_profile_from_committed_note,
    build_evidence_profile,
    load_profile_checkpoint,
    load_profile_sidecar,
    profile_dependency_fingerprint,
    profile_sidecar_path,
    profile_to_dict,
    save_profile,
    validate_profile,
    write_profile_checkpoint,
)
from .readers import SECTION_KEYS, provider_from_name
from .workspace import (
    artifact_rows,
    assert_compatible,
    initialize,
    resolve_workspace,
    run_directory,
    validate_opaque_id,
)
from .zotero import ZoteroLocalClient

CHUNKING_VERSION = "2"
CONTENT_CLASSIFIER_VERSION = "3"


def _analytical_profile_source_ids(profiles: Sequence[Any]) -> set[str]:
    source_ids: set[str] = set()
    for profile in profiles:
        row = dict(profile) if isinstance(profile, Mapping) else profile_to_dict(profile)
        context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
        analytical = row.get("analytical")
        if analytical is None:
            analytical = bool(
                not row.get("excluded_from_synthesis", False)
                and str(context.get("note_status") or "")
                == "analytical_atomic_note"
            )
        if analytical and row.get("source_id"):
            source_ids.add(str(row["source_id"]))
    return source_ids


class DocumentPartialError(RuntimeError):
    def __init__(self, reason: str, completed_chunks: int, total_chunks: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.completed_chunks = completed_chunks
        self.total_chunks = total_chunks


class DocumentCoverageLimitError(RuntimeError):
    def __init__(self, total_chunks: int, maximum_chunks: int) -> None:
        super().__init__(
            f"document requires {total_chunks} chunks; maximum is {maximum_chunks}"
        )
        self.total_chunks = total_chunks
        self.maximum_chunks = maximum_chunks


class _RunProgress:
    """Thread-safe, atomically persisted visibility into an active run."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        resume: bool,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        previous = read_yaml(path, {}) or {} if resume else {}
        previous_items = (
            previous.get("items", {})
            if isinstance(previous.get("items", {}), Mapping)
            else {}
        )
        self.stage = str(previous.get("stage") or "preflight")
        prior_timestamps = previous.get("stage_timestamps", {})
        if not isinstance(prior_timestamps, Mapping):
            prior_timestamps = {}
        self.stage_timestamps: dict[str, dict[str, str]] = {
            str(key): dict(value)
            for key, value in prior_timestamps.items()
            if isinstance(value, Mapping)
        }
        self.literature: dict[str, Any] = {
            "profile_count": 0,
            "profile_valid_count": 0,
            "profile_excluded_count": 0,
            "unclustered_count": 0,
            "topic_neighborhood_count": 0,
            "subject_tag_count": 0,
            "subject_tag_assignment_count": 0,
            "typed_relation_count": 0,
            "singleton_facet_count": 0,
            "proposition_count": 0,
            "cluster_count": 0,
            "evidence_concentrated_cluster_count": 0,
            "debate_count": 0,
            "consensus_count": 0,
            "mixed_evidence_count": 0,
            "strict_consensus_established_count": 0,
            "strict_consensus_not_established_count": 0,
            "strict_contradiction_established_count": 0,
            "strict_contradiction_not_established_count": 0,
            "mapped_gap_count": 0,
            "gap_lead_count": 0,
            "strong_gap_established_count": 0,
            "strong_gap_not_established_count": 0,
            "synthesized_cluster_count": 0,
            "rejected_underspecified_gap_count": 0,
            "rejected_gap_quality_count": 0,
            "merged_gap_count": 0,
            "synthesis_call_count": 0,
            "synthesis_checkpoint_hit_count": 0,
            "synthesis_failure_count": 0,
            "active_cluster": "",
            "active_gap_packet": "",
            "active_synthesis_packet": "",
            "checkpoint_hit_count": 0,
            "source_provider_call_count": 0,
            "literature_provider_call_count": 0,
            "provider_call_count": 0,
            "literature_failure_count": 0,
            "internal_falsification_count": 0,
            **(
                dict(previous.get("literature", {}))
                if isinstance(previous.get("literature", {}), Mapping)
                else {}
            ),
        }
        # Literature counters describe the current invocation. A resume starts
        # from its frozen items/checkpoints but must not display stale counts
        # from the prior invocation while work is still in flight.
        self.literature.update(
            profile_count=0,
            profile_valid_count=0,
            profile_excluded_count=0,
            unclustered_count=0,
            topic_neighborhood_count=0,
            subject_tag_count=0,
            subject_tag_assignment_count=0,
            typed_relation_count=0,
            singleton_facet_count=0,
            proposition_count=0,
            evidence_base_group_count=0,
            cluster_count=0,
            cluster_source_contribution_count=0,
            debate_count=0,
            consensus_count=0,
            mixed_evidence_count=0,
            mapped_gap_count=0,
            gap_lead_count=0,
            synthesized_cluster_count=0,
            rejected_underspecified_gap_count=0,
            rejected_gap_quality_count=0,
            merged_gap_count=0,
            synthesis_call_count=0,
            synthesis_checkpoint_hit_count=0,
            synthesis_failure_count=0,
            quantitative_comparison_count=0,
            rejected_quantitative_comparison_count=0,
            rejected_generated_locator_count=0,
            coverage_inventory_count=0,
            coverage_exhausted_count=0,
            coverage_accounting_valid=False,
            active_cluster="",
            active_gap_packet="",
            active_synthesis_packet="",
            checkpoint_hit_count=0,
            source_provider_call_count=0,
            literature_provider_call_count=0,
            provider_call_count=0,
            literature_failure_count=0,
            internal_falsification_count=0,
        )
        # Source-item accounting is derived from ``self.items``. Older progress
        # files could also persist those keys inside the literature payload;
        # spreading that payload at the top level then replaced the live truth
        # with stale counts from a prior invocation.
        for source_count_key in (
            "inventory_count",
            "validated_note_count",
            "limited_note_count",
            "exhausted_count",
            "partial_count",
            "pending_count",
            "active_count",
            "terminal_count",
        ):
            self.literature.pop(source_count_key, None)
        self.items: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(items):
            key = item_key(item)
            prior = (
                previous_items.get(str(index), {})
                if isinstance(previous_items, Mapping)
                else {}
            )
            if (
                not isinstance(prior, Mapping)
                or str(prior.get("zotero_item_key", "")) != key
            ):
                prior = {}
            terminal_status = str(item.get("terminal_status") or "")
            default_phase = (
                "committed"
                if terminal_status in {"validated_note", "limited_note"}
                else "finished"
                if terminal_status in {"exhausted", "partial"}
                else "queued"
            )
            self.items[str(index)] = {
                "inventory_index": index,
                "zotero_item_key": key,
                "status": str(prior.get("status") or terminal_status or "pending"),
                "phase": str(prior.get("phase") or default_phase),
                "completed_chunks": int(prior.get("completed_chunks", 0) or 0),
                "total_chunks": int(prior.get("total_chunks", 0) or 0),
                "reason": str(prior.get("reason") or ""),
            }
        self._status = "running"
        self.stage_timestamps.setdefault(self.stage, {"started_at": now_iso()})
        self._write()

    def update(self, index: int, **values: Any) -> None:
        with self._lock:
            row = self.items.setdefault(
                str(index), {"inventory_index": index, "status": "pending"}
            )
            row.update(values)
            self._write()

    def finish(self, status: str) -> None:
        with self._lock:
            self._status = status
            self.stage_timestamps.setdefault(self.stage, {}).setdefault(
                "completed_at", now_iso()
            )
            self._write()

    def set_stage(self, stage: str, **literature_values: Any) -> None:
        with self._lock:
            timestamp = now_iso()
            if stage != self.stage:
                self.stage_timestamps.setdefault(self.stage, {}).setdefault(
                    "completed_at", timestamp
                )
                self.stage = stage
                self.stage_timestamps.setdefault(stage, {}).setdefault(
                    "started_at", timestamp
                )
            if "synthesis_call_count" in literature_values:
                prior_synthesis = int(
                    self.literature.get("synthesis_call_count", 0) or 0
                )
                profile_calls = max(
                    0,
                    int(self.literature.get("literature_provider_call_count", 0) or 0)
                    - prior_synthesis,
                )
                literature_values["literature_provider_call_count"] = (
                    profile_calls
                    + int(literature_values.get("synthesis_call_count", 0) or 0)
                )
            if "synthesis_checkpoint_hit_count" in literature_values:
                prior_synthesis_hits = int(
                    self.literature.get("synthesis_checkpoint_hit_count", 0) or 0
                )
                profile_hits = max(
                    0,
                    int(self.literature.get("checkpoint_hit_count", 0) or 0)
                    - prior_synthesis_hits,
                )
                literature_values["checkpoint_hit_count"] = profile_hits + int(
                    literature_values.get("synthesis_checkpoint_hit_count", 0) or 0
                )
            if "synthesis_failure_count" in literature_values:
                prior_synthesis_failures = int(
                    self.literature.get("synthesis_failure_count", 0) or 0
                )
                profile_failures = max(
                    0,
                    int(self.literature.get("literature_failure_count", 0) or 0)
                    - prior_synthesis_failures,
                )
                literature_values["literature_failure_count"] = profile_failures + int(
                    literature_values.get("synthesis_failure_count", 0) or 0
                )
            self.literature.update(literature_values)
            self._write()

    def update_literature(self, **values: Any) -> None:
        with self._lock:
            self.literature.update(values)
            self._write()

    def record_source_provider_call(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("provider call count cannot be negative")
        with self._lock:
            current = int(self.literature.get("source_provider_call_count", 0) or 0)
            self.literature["source_provider_call_count"] = current + count
            self._write()

    def _write(self) -> None:
        source_calls = int(self.literature.get("source_provider_call_count", 0) or 0)
        literature_calls = int(
            self.literature.get("literature_provider_call_count", 0) or 0
        )
        self.literature["provider_call_count"] = source_calls + literature_calls
        statuses = [str(row.get("status", "pending")) for row in self.items.values()]
        counts = {
            name: statuses.count(name)
            for name in (
                "validated_note",
                "limited_note",
                "exhausted",
                "partial",
                "pending",
                "active",
            )
        }
        terminal_count = (
            counts["validated_note"] + counts["limited_note"] + counts["exhausted"]
        )
        payload = {
            "status": self._status,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_timestamps": self.stage_timestamps,
            **self.literature,
            "inventory_count": len(self.items),
            "validated_note_count": counts["validated_note"],
            "limited_note_count": counts["limited_note"],
            "exhausted_count": counts["exhausted"],
            "partial_count": counts["partial"],
            "pending_count": counts["pending"] + counts["active"],
            "active_count": counts["active"],
            "terminal_count": terminal_count,
            "active_item_keys": [
                row.get("zotero_item_key", "")
                for row in self.items.values()
                if row.get("status") == "active"
            ],
            "completed_chunk_count": sum(
                int(row.get("completed_chunks", 0) or 0) for row in self.items.values()
            ),
            "total_chunk_count": sum(
                int(row.get("total_chunks", 0) or 0) for row in self.items.values()
            ),
            "literature": self.literature,
            "items": self.items,
            "updated_at": now_iso(),
        }
        write_yaml(self.path, payload)


def _zotero_collection_name(client: ZoteroClient, collection_key: str) -> str:
    """Resolve a display label through Zotero's read-only collections API."""

    try:
        rows = client.collections()
    except Exception:
        return ""
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), Mapping) else {}
        key = str(row.get("key") or data.get("key") or "")
        if key == collection_key:
            return str(row.get("name") or data.get("name") or "").strip()
    return ""


def run_pipeline(
    request: MapRequest,
    *,
    client: ZoteroClient | None = None,
    reader: ReaderProvider | None = None,
    vision: VisionProvider | None = None,
    controller: ControllerPort | None = None,
    literature_reasoner: LiteratureReasoner | None = None,
    external_discovery: ExternalDiscoveryProvider | None = None,
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
        reader = reader or provider_from_name(
            request.provider, request.model, allow_cloud=request.allow_cloud
        )
    except Exception as exc:
        return _blocked_report(
            request, run_id, f"reader_configuration:{type(exc).__name__}:{exc}"
        )
    _apply_reader_policy(reader, request.processing)
    preflight_reason = _reader_preflight_reason(reader, request.allow_cloud)
    if preflight_reason:
        return _blocked_report(request, run_id, preflight_reason)
    if literature_reasoner is None and isinstance(reader, LiteratureReasoner):
        literature_reasoner = reader
    if vision is None and hasattr(reader, "inspect_document"):
        vision = reader  # type: ignore[assignment]
    discovery_mode = request.literature_policy.external_discovery
    if discovery_mode != "disabled":
        return _blocked_report(
            request,
            run_id,
            f"external_discovery_disabled_in_standalone_mapper:{discovery_mode}",
        )
    if external_discovery is not None:
        return _blocked_report(
            request, run_id, "external_discovery_provider_not_used_by_standalone_mapper"
        )
    if (
        external_discovery is not None
        and bool(getattr(external_discovery, "is_cloud", True))
        and not request.allow_cloud
    ):
        return _blocked_report(
            request, run_id, "external_discovery_requires_allow_cloud"
        )
    if (
        literature_reasoner is not None
        and bool(getattr(literature_reasoner, "is_cloud", True))
        and not request.allow_cloud
    ):
        return _blocked_report(
            request, run_id, "literature_reasoner_requires_allow_cloud"
        )

    inventory_path = (
        workspace / "01_custody" / "zotero" / "inventory" / f"{slugify(run_id)}.json"
    )
    frozen_inventory_path = run_dir / "inventory.json"
    frozen_manifest_path = run_dir / "frozen_inventory.yml"
    effective_collection_key = request.collection_key
    effective_collection_name = ""
    inventory_scope = request.scope
    frozen_source_set_snapshot_id = ""
    if resume and frozen_inventory_path.exists():
        try:
            frozen_payload = json.loads(
                frozen_inventory_path.read_text(encoding="utf-8")
            )
            if not isinstance(frozen_payload, list) or any(
                not isinstance(item, Mapping) for item in frozen_payload
            ):
                raise ValueError(
                    "frozen inventory must be a list of Zotero item mappings"
                )
            items = [dict(item) for item in frozen_payload]
            frozen_manifest = read_yaml(frozen_manifest_path, {}) or {}
            if frozen_manifest and not isinstance(frozen_manifest, Mapping):
                raise ValueError("frozen inventory manifest must be a mapping")
            expected_hash = str(frozen_manifest.get("inventory_hash") or "")
            actual_hash = sha256_text(
                json.dumps(items, sort_keys=True, ensure_ascii=False, default=str)
            )
            if expected_hash and expected_hash != actual_hash:
                raise ValueError("frozen inventory hash mismatch")
            inventory_scope = str(
                frozen_manifest.get("effective_scope") or inventory_scope
            )
            effective_collection_key = (
                str(
                    frozen_manifest.get("effective_collection_key")
                    or effective_collection_key
                    or ""
                )
                or None
            )
            effective_collection_name = str(
                frozen_manifest.get("collection_name") or ""
            ).strip()
            frozen_source_set_snapshot_id = str(
                frozen_manifest.get("source_set_snapshot_id") or ""
            )
            if not frozen_source_set_snapshot_id:
                previous_report = read_yaml(run_dir / "run_report.yml", {}) or {}
                if isinstance(previous_report, Mapping):
                    frozen_source_set_snapshot_id = str(
                        previous_report.get("source_set_id") or ""
                    )
        except Exception as exc:
            return _blocked_report(
                request, run_id, f"frozen_inventory:{type(exc).__name__}:{exc}"
            )
    else:
        try:
            if request.scope == "selected":
                selected = client.selected_collection()
                effective_collection_key = str(selected.get("key") or "")
                effective_collection_name = str(selected.get("name") or "").strip()
                inventory_scope = (
                    "library" if selected.get("scope") == "library" else "collection"
                )
                if inventory_scope == "collection" and not effective_collection_key:
                    raise ValueError("selected collection has no key")
            items = [
                dict(item)
                for item in client.inventory(inventory_scope, effective_collection_key)
                if isinstance(item, Mapping)
            ]
            if effective_collection_key and not effective_collection_name:
                effective_collection_name = _zotero_collection_name(
                    client, effective_collection_key
                )
        except Exception as exc:
            return _blocked_report(
                request, run_id, f"zotero_inventory:{type(exc).__name__}:{exc}"
            )
        if request.limit:
            items = items[: request.limit]
        write_json(inventory_path, items)
        write_json(frozen_inventory_path, items)
        write_yaml(
            frozen_manifest_path,
            {
                "run_id": run_id,
                "requested_scope": request.scope,
                "effective_scope": inventory_scope,
                "effective_collection_key": effective_collection_key or "",
                "collection_name": effective_collection_name,
                "inventory_count": len(items),
                "inventory_hash": sha256_text(
                    json.dumps(items, sort_keys=True, ensure_ascii=False, default=str)
                ),
                "frozen_at": now_iso(),
                "refresh_requires_new_run": True,
            },
        )
    if effective_collection_key:
        write_yaml(
            workspace
            / "01_custody"
            / "zotero"
            / "collections"
            / f"{slugify(effective_collection_key)}.yml",
            {
                "collection_key": effective_collection_key,
                "collection_name": effective_collection_name,
                "scope": request.scope,
                "run_id": run_id,
                "zotero_item_keys": [item_key(item) for item in items],
                "sync_zotero_collection": False,
                "updated_at": now_iso(),
            },
        )

    progress = _RunProgress(run_dir / "progress.yml", run_id, items, resume=resume)
    progress.set_stage("frozen_inventory")
    progress.set_stage("source_processing")
    prepared: list[dict[str, Any]] = []
    terminal_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    attempt_path = (
        workspace / "01_custody" / "read_attempts" / f"{slugify(run_id)}.jsonl"
    )
    seen_keys: set[str] = set()
    pending: list[tuple[int, dict[str, Any]]] = []

    def commit_result(row: dict[str, Any]) -> None:
        prepared.append(row)
        public_row, note_row, row_proposals, row_decisions = _finalize_prepared_row(
            workspace,
            request,
            controller,
            row,
            attempt_path,
        )
        terminal_rows.append(public_row)
        if note_row:
            note_rows.append(note_row)
        proposals.extend(row_proposals)
        decisions.extend(row_decisions)
        progress.update(
            int(public_row["inventory_index"]),
            status=str(public_row["terminal_status"]),
            phase="committed" if public_row.get("note_path") else "finished",
            reason=str(public_row.get("reason", "")),
            completed_chunks=int(row.get("completed_chunks", 0) or 0),
            total_chunks=int(row.get("total_chunks", 0) or 0),
        )

    for index, item in enumerate(items):
        key = item_key(item)
        if key and key in seen_keys:
            commit_result(_duplicate_result(index, item))
            continue
        if key:
            seen_keys.add(key)
        pending.append((index, item))

    workers = max(1, min(request.parallel, len(pending) or 1))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="auto-zettelkasten"
    ) as executor:
        future_map = {
            executor.submit(
                _prepare_item,
                workspace,
                run_dir,
                index,
                item,
                request,
                client,
                reader,
                vision,
                progress,
            ): index
            for index, item in pending
        }
        for future in as_completed(future_map):
            try:
                commit_result(future.result())
            except (
                Exception
            ) as exc:  # defensive terminal accounting at the worker boundary
                index = future_map[future]
                commit_result(
                    _exhausted_result(
                        index,
                        items[index],
                        "pipeline_worker",
                        f"unhandled_worker_error:{type(exc).__name__}:{exc}",
                    )
                )
    prepared.sort(key=lambda row: int(row.get("inventory_index", 0)))
    tag_report = commit_tag_reviews(workspace, proposals, decisions)

    note_rows = _deduplicate_note_rows(note_rows)
    terminal_rows.sort(key=lambda row: int(row.get("inventory_index", 0)))
    run_source_set = write_source_set(
        workspace,
        run_id=run_id,
        scope=request.scope,
        collection_key=effective_collection_key,
        items=items,
        terminal_rows=terminal_rows,
        note_rows=note_rows,
        snapshot_id=frozen_source_set_snapshot_id or None,
        collection_name=effective_collection_name,
    )
    if not frozen_source_set_snapshot_id:
        frozen_source_set_snapshot_id = str(run_source_set["source_set_id"])
    frozen_manifest = read_yaml(frozen_manifest_path, {}) or {}
    if (
        isinstance(frozen_manifest, Mapping)
        and frozen_manifest.get("source_set_snapshot_id")
        != frozen_source_set_snapshot_id
    ):
        write_yaml(
            frozen_manifest_path,
            {
                **dict(frozen_manifest),
                "source_set_snapshot_id": frozen_source_set_snapshot_id,
            },
        )
    map_source_set = run_source_set
    progress.set_stage("profiling")
    map_result = rebuild_map(
        workspace,
        source_set=map_source_set,
        note_rows=note_rows,
        terminal_rows=terminal_rows,
        items=items,
        run_id=run_id,
        question=request.question,
        request=request,
        reasoner=literature_reasoner,
        external_discovery=external_discovery,
        progress=progress,
        resume=resume,
    )
    # The v0.4 map is already scoped to this run's frozen source set, so every
    # generated cluster and gap belongs to the run without a second heuristic filter.
    relevant_clusters = list(map_result["cluster_map"]["clusters"])
    relevant_gaps = list(map_result["gap_map"]["gap_candidates"])
    run_source_set = update_source_set_map(
        workspace, run_source_set, relevant_clusters, relevant_gaps
    )

    debate_payload = (
        read_yaml(workspace / "03_literature_synthesis" / "debate_registry.yml", {})
        or {}
    )
    debate_rows = (
        debate_payload.get("assessments", [])
        if isinstance(debate_payload, Mapping)
        else []
    )
    if not isinstance(debate_rows, list):
        debate_rows = []
    profile_count = len(map_result.get("profiles", []) or [])
    analytical_source_ids = _analytical_profile_source_ids(
        map_result.get("profiles", []) or []
    )
    unclustered_count = sum(
        1
        for row in map_result["cluster_map"].get("unclustered_sources", []) or []
        if isinstance(row, Mapping)
        and str(row.get("source_id") or "") in analytical_source_ids
    )
    cluster_count = len(map_result["cluster_map"].get("clusters", []) or [])
    topic_neighborhood_count = int(
        map_result["cluster_map"].get("topic_neighborhood_count", 0) or 0
    )
    navigation_summary = (
        map_result["cluster_map"].get("navigation", {})
        if isinstance(map_result["cluster_map"].get("navigation"), Mapping)
        else {}
    )
    subject_tag_count = int(
        navigation_summary.get("promoted_subject_tag_count", 0) or 0
    )
    subject_tag_assignment_count = len(navigation_summary.get("assignments", []) or [])
    typed_relation_count = len(navigation_summary.get("typed_relations", []) or [])
    singleton_facet_count = int(navigation_summary.get("singleton_facet_count", 0) or 0)
    proposition_count = int(map_result["cluster_map"].get("proposition_count", 0) or 0)
    debate_count = sum(
        1
        for row in debate_rows
        if isinstance(row, Mapping) and row.get("classification") == "mapped_debate"
    )
    consensus_count = sum(
        1
        for row in debate_rows
        if isinstance(row, Mapping) and row.get("classification") == "mapped_consensus"
    )
    mixed_evidence_count = sum(
        1
        for row in debate_rows
        if isinstance(row, Mapping) and row.get("classification") == "mixed_evidence"
    )
    search_payload = (
        read_yaml(workspace / "03_literature_synthesis" / "internal_search_log.yml", {})
        or {}
    )
    searches = (
        search_payload.get("searches", [])
        if isinstance(search_payload, Mapping)
        else []
    )
    internal_falsification_count = len(searches) if isinstance(searches, list) else 0
    mapped_gap_count = sum(
        1
        for row in map_result["gap_map"].get("gap_candidates", []) or []
        if row.get("status") == "collection_surviving_gap"
    )
    gap_lead_count = sum(
        1
        for row in map_result["gap_map"].get("gap_candidates", []) or []
        if row.get("status") == "collection_gap_lead"
    )
    synthesized_cluster_count = int(
        map_result["cluster_map"].get("synthesized_cluster_count", 0) or 0
    )
    rejected_underspecified_gap_count = int(
        map_result["gap_map"].get("rejected_underspecified_gap_count", 0) or 0
    )
    rejected_gap_quality_count = int(
        map_result["gap_map"].get("rejected_gap_quality_count", 0) or 0
    )
    merged_gap_count = int(map_result["gap_map"].get("merged_gap_count", 0) or 0)
    synthesis_call_count = int(
        map_result["literature_packet"].get("synthesis_call_count", 0) or 0
    )
    synthesis_checkpoint_hit_count = int(
        map_result["literature_packet"].get("synthesis_checkpoint_hit_count", 0) or 0
    )
    synthesis_failure_count = int(
        map_result["literature_packet"].get("synthesis_failure_count", 0) or 0
    )
    literature_partial_reason = str(map_result.get("partial_reason") or "")
    literature_summary = {
        "status": "partial" if literature_partial_reason else "completed",
        "stage": "reporting",
        "profile_count": profile_count,
        "profile_valid_count": int(
            (map_result.get("profile_result", {}) or {}).get("valid_count", 0) or 0
        ),
        "profile_excluded_count": int(
            (map_result.get("profile_result", {}) or {}).get("excluded_count", 0) or 0
        ),
        "unclustered_count": unclustered_count,
        "topic_neighborhood_count": topic_neighborhood_count,
        "subject_tag_count": subject_tag_count,
        "subject_tag_assignment_count": subject_tag_assignment_count,
        "typed_relation_count": typed_relation_count,
        "singleton_facet_count": singleton_facet_count,
        "proposition_count": proposition_count,
        "cluster_count": cluster_count,
        "debate_count": debate_count,
        "consensus_count": consensus_count,
        "mixed_evidence_count": mixed_evidence_count,
        "mapped_gap_count": mapped_gap_count,
        "gap_lead_count": gap_lead_count,
        "synthesized_cluster_count": synthesized_cluster_count,
        "rejected_underspecified_gap_count": rejected_underspecified_gap_count,
        "rejected_gap_quality_count": rejected_gap_quality_count,
        "merged_gap_count": merged_gap_count,
        "synthesis_call_count": synthesis_call_count,
        "synthesis_checkpoint_hit_count": synthesis_checkpoint_hit_count,
        "synthesis_failure_count": synthesis_failure_count,
        "internal_falsification_count": internal_falsification_count,
        "profile_result": map_result.get("profile_result", {}),
        "migration": map_result.get("migration", {}),
        "partial_reason": literature_partial_reason,
    }
    progress.set_stage(
        "reporting",
        profile_count=profile_count,
        unclustered_count=unclustered_count,
        topic_neighborhood_count=topic_neighborhood_count,
        subject_tag_count=subject_tag_count,
        subject_tag_assignment_count=subject_tag_assignment_count,
        typed_relation_count=typed_relation_count,
        singleton_facet_count=singleton_facet_count,
        proposition_count=proposition_count,
        cluster_count=cluster_count,
        debate_count=debate_count,
        consensus_count=consensus_count,
        mixed_evidence_count=mixed_evidence_count,
        mapped_gap_count=mapped_gap_count,
        gap_lead_count=gap_lead_count,
        synthesized_cluster_count=synthesized_cluster_count,
        rejected_underspecified_gap_count=rejected_underspecified_gap_count,
        rejected_gap_quality_count=rejected_gap_quality_count,
        merged_gap_count=merged_gap_count,
        synthesis_call_count=synthesis_call_count,
        synthesis_checkpoint_hit_count=synthesis_checkpoint_hit_count,
        synthesis_failure_count=synthesis_failure_count,
        internal_falsification_count=internal_falsification_count,
    )

    validated_count = sum(
        1 for row in terminal_rows if row.get("terminal_status") == "validated_note"
    )
    limited_count = sum(
        1 for row in terminal_rows if row.get("terminal_status") == "limited_note"
    )
    exhausted_count = sum(
        1 for row in terminal_rows if row.get("terminal_status") == "exhausted"
    )
    partial_count = sum(
        1 for row in terminal_rows if row.get("terminal_status") == "partial"
    )
    pending_count = max(0, len(items) - len(terminal_rows))
    reused_count = sum(
        1
        for row in prepared
        if row.get("reused")
        and row.get("terminal_status") in {"validated_note", "limited_note"}
    )
    status = (
        "partial"
        if partial_count or pending_count or literature_partial_reason
        else ("completed" if exhausted_count == 0 else "completed_with_exhausted_items")
    )
    progress.finish(status)
    errors = [
        {
            "zotero_item_key": row.get("zotero_item_key", ""),
            "reason": row.get("reason", ""),
        }
        for row in terminal_rows
        if row.get("terminal_status") in {"exhausted", "partial"}
    ]
    if literature_partial_reason:
        errors.append(
            {"stage": "literature_mapping", "reason": literature_partial_reason}
        )
    created_paths = [
        inventory_path,
        run_dir / "inventory.json",
        frozen_manifest_path,
        run_dir / "progress.yml",
        attempt_path,
        Path(run_source_set["path"]),
        Path(run_source_set.get("latest_path", run_source_set["path"])),
        Path(map_result["source_set"]["path"]),
        Path(tag_report["proposal_path"]),
        Path(tag_report["registry_path"]),
        *map_result["paths"],
        *[
            workspace / str(row["note_path"])
            for row in terminal_rows
            if row.get("note_path")
        ],
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
            "literature_map": literature_summary,
            "profiles": map_result.get("profile_result", {}),
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
        limited_note_count=limited_count,
        exhausted_count=exhausted_count,
        partial_count=partial_count,
        pending_count=pending_count,
        reused_count=reused_count,
        source_set_id=str(run_source_set["source_set_id"]),
        items=terminal_rows,
        errors=errors,
        source_set=run_source_set,
        cluster_map=map_result["cluster_map"],
        gap_map=map_result["gap_map"],
        literature_packet=map_result["literature_packet"],
        literature_map=literature_summary,
        literature_report=literature_summary,
        profile_count=profile_count,
        profile_valid_count=int(literature_summary["profile_valid_count"]),
        profile_excluded_count=int(literature_summary["profile_excluded_count"]),
        unclustered_count=unclustered_count,
        topic_neighborhood_count=topic_neighborhood_count,
        subject_tag_count=subject_tag_count,
        subject_tag_assignment_count=subject_tag_assignment_count,
        typed_relation_count=typed_relation_count,
        singleton_facet_count=singleton_facet_count,
        proposition_count=proposition_count,
        cluster_count=cluster_count,
        debate_count=debate_count,
        consensus_count=consensus_count,
        mixed_evidence_count=mixed_evidence_count,
        mapped_gap_count=mapped_gap_count,
        gap_lead_count=gap_lead_count,
        synthesized_cluster_count=synthesized_cluster_count,
        rejected_underspecified_gap_count=rejected_underspecified_gap_count,
        rejected_gap_quality_count=rejected_gap_quality_count,
        merged_gap_count=merged_gap_count,
        synthesis_call_count=synthesis_call_count,
        synthesis_checkpoint_hit_count=synthesis_checkpoint_hit_count,
        synthesis_failure_count=synthesis_failure_count,
        checkpoint_hit_count=int(
            progress.literature.get("checkpoint_hit_count", 0) or 0
        ),
        source_provider_call_count=int(
            progress.literature.get("source_provider_call_count", 0) or 0
        ),
        literature_provider_call_count=int(
            progress.literature.get("literature_provider_call_count", 0) or 0
        ),
        provider_call_count=int(progress.literature.get("provider_call_count", 0) or 0),
        literature_failure_count=int(
            progress.literature.get("literature_failure_count", 0) or 0
        ),
        internal_falsification_count=internal_falsification_count,
        artifact_manifest=manifest,
    )
    _write_run_report(run_dir, report)
    return report


def _apply_reader_policy(reader: ReaderProvider, policy: ProcessingPolicy) -> None:
    for attribute, value in (
        ("request_deadline", policy.request_deadline_seconds),
        ("max_output_tokens", policy.synthesis_output_tokens),
        ("chunk_output_tokens", policy.chunk_output_tokens),
        ("direct_read_fraction", policy.context_window_fraction),
    ):
        if hasattr(reader, attribute):
            try:
                setattr(reader, attribute, value)
            except (AttributeError, TypeError):
                pass


def _finalize_prepared_row(
    workspace: Path,
    request: MapRequest,
    controller: ControllerPort,
    row: dict[str, Any],
    attempt_path: Path,
) -> tuple[
    dict[str, Any], dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]
]:
    for attempt in row.pop("attempts", []):
        append_jsonl(attempt_path, attempt)
    if row.get("reused") and row.get("note_path"):
        return (
            _public_terminal_row(row),
            _note_summary_from_path(workspace, row),
            [],
            [],
        )
    has_note_content = bool(row.get("analysis") or row.get("limited_analysis"))
    if not has_note_content:
        return _public_terminal_row(row), None, [], []

    proposals = [
        dict(value) for value in propose_tags(row["item"], str(row["note_id"]))
    ]
    decisions = _review_tags(controller, proposals)
    normalized_tags = accepted_tags_by_note(decisions).get(str(row["note_id"]), [])
    frontmatter = _frontmatter(row, request, normalized_tags)
    route = (
        "limited_note_commit" if row.get("limited_analysis") else "atomic_note_commit"
    )
    try:
        if row.get("limited_analysis"):
            path, validation = write_limited_note(
                workspace, frontmatter, row["limited_analysis"]
            )
        else:
            path, validation = write_atomic_note(
                workspace, frontmatter, row["analysis"]
            )
    except Exception as exc:
        row.update(
            terminal_status="exhausted",
            reason=f"{route}_failed:{type(exc).__name__}:{exc}",
            note_path="",
        )
        append_jsonl(attempt_path, _attempt(row, route, "failed", row["reason"]))
        decisions = _park_note_decisions(decisions)
        commit_tag_reviews(workspace, proposals, decisions)
        return _public_terminal_row(row), None, proposals, decisions
    if not validation.passed:
        row.update(
            terminal_status="exhausted",
            reason=f"{route}_validation_failed:" + ",".join(validation.errors),
            note_path="",
        )
        append_jsonl(
            attempt_path,
            _attempt(row, route, "failed", row["reason"], output_path=str(path)),
        )
        decisions = _park_note_decisions(decisions)
        commit_tag_reviews(workspace, proposals, decisions)
        return _public_terminal_row(row), None, proposals, decisions

    relative_path = str(path.relative_to(workspace))
    terminal_status = (
        "limited_note" if row.get("limited_analysis") else "validated_note"
    )
    row.update(terminal_status=terminal_status, note_path=relative_path)
    _write_fingerprint(workspace, row, relative_path)
    append_jsonl(
        attempt_path,
        _attempt(row, route, "succeeded", "note_committed", output_path=str(path)),
    )
    commit_tag_reviews(workspace, proposals, decisions)
    return (
        _public_terminal_row(row),
        _note_summary_from_path(workspace, row),
        proposals,
        decisions,
    )


def _park_note_decisions(
    decisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **dict(row),
            "decision": "parked",
            "decision_reason": "source_note_not_committed",
        }
        for row in decisions
    ]


def _write_fingerprint(
    workspace: Path, row: Mapping[str, Any], relative_path: str
) -> None:
    fingerprint = str(row.get("fingerprint") or "")
    if not fingerprint:
        return
    write_yaml(
        workspace / "11_state" / "fingerprints" / f"{fingerprint}.yml",
        {
            "fingerprint": fingerprint,
            "zotero_item_key": row.get("zotero_item_key", ""),
            "note_id": row.get("note_id", ""),
            "source_id": row.get("source_id", ""),
            "note_status": row.get("note_status", ""),
            "source_scope": row.get("source_scope", ""),
            "note_path": relative_path,
            "content_hash": row.get("content_hash", ""),
            "metadata_hash": _prompt_metadata_hash(row.get("item", {})),
            "engine_version": ENGINE_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "updated_at": now_iso(),
        },
    )


def _existing_source_set_paths(
    workspace: Path, source_set: Mapping[str, Any]
) -> list[Path]:
    candidates = [
        source_set.get("path"),
        source_set.get("latest_path"),
        workspace
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / f"{source_set.get('source_set_id')}.yml"
        if source_set.get("source_set_id")
        else None,
        workspace
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / f"{source_set.get('source_set_alias')}.yml"
        if source_set.get("source_set_alias")
        else None,
    ]
    return list(
        dict.fromkeys(
            Path(value) for value in candidates if value and Path(value).is_file()
        )
    )


def rebuild_map(
    workspace: Path,
    *,
    source_set: Mapping[str, Any],
    note_rows: Sequence[Mapping[str, Any]],
    terminal_rows: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    run_id: str,
    question: str | None,
    request: MapRequest | None = None,
    reasoner: LiteratureReasoner | None = None,
    external_discovery: ExternalDiscoveryProvider | None = None,
    progress: _RunProgress | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    del (
        external_discovery
    )  # Auto-Zettelkasten 0.4 maps only the frozen internal collection.
    effective_request = request or MapRequest(
        workspace=workspace, provider="ollama", model="deterministic-v1"
    )
    if not effective_request.literature_policy.synthesis_enabled:
        relations = build_typed_source_relations(
            note_rows,
            max_inferred_links_per_source=effective_request.navigation_policy.max_inferred_related_note_links,
        )
        typed = {
            "path": str(workspace / "02_source_memory" / "indexes" / "typed_links.yml"),
            "compatibility_path": str(
                workspace / "02_source_memory" / "indexes" / "typed_note_links.yml"
            ),
            "links": relations,
            "link_count": len(relations),
        }
        typed_payload = {
            "updated_at": now_iso(),
            "relations": relations,
            "links": relations,
        }
        write_yaml(Path(typed["path"]), typed_payload)
        write_yaml(Path(typed["compatibility_path"]), typed_payload)
        note_paths = [
            workspace / str(row["note_path"])
            for row in note_rows
            if row.get("note_path") and (workspace / str(row["note_path"])).is_file()
        ]
        source_index = write_source_index(workspace, note_paths)
        if progress is not None:
            progress.set_stage("reporting")
        return {
            "source_set": dict(source_set),
            "cluster_map": {
                "status": "synthesis_disabled",
                "clusters": [],
                "relations": [],
                "rejected_proposals": [],
                "unclustered_sources": [],
            },
            "gap_map": {
                "status": "synthesis_disabled",
                "gap_candidates": [],
                "novelty_claimed": False,
            },
            "literature_packet": {"status": "disabled", "reason": "synthesis_disabled"},
            "typed_links": typed,
            "profiles": [],
            "profile_result": {
                "valid_count": 0,
                "excluded_count": 0,
                "checkpoint_hits": 0,
                "provider_calls": 0,
                "failure_count": 0,
                "profile_packet_count": 0,
            },
            "migration": {"status": "not_run", "reason": "synthesis_disabled"},
            "paths": [
                Path(typed["path"]),
                Path(typed["compatibility_path"]),
                source_index,
            ],
        }
    migration = migrate_workspace(workspace)
    try:
        profile_result = _build_profiles_for_map(
            workspace,
            note_rows,
            source_set=source_set,
            run_id=run_id,
            request=effective_request,
            reasoner=reasoner,
            progress=progress,
            resume=resume,
        )
    except Exception as exc:
        reason = f"literature_profiling_partial:{type(exc).__name__}:{exc}"
        if progress is not None:
            progress.update_literature(literature_failure_count=1)
        checkpoint_paths = sorted(
            (run_directory(workspace, run_id) / "literature").rglob("*.yml")
        )
        profile_paths = sorted(
            (workspace / "02_source_memory" / "profiles").glob("*.yml")
        )
        return {
            "source_set": dict(source_set),
            "cluster_map": {
                "status": "partial",
                "clusters": [],
                "unclustered_sources": [],
            },
            "gap_map": {
                "status": "partial",
                "gap_candidates": [],
                "novelty_claimed": False,
            },
            "literature_packet": {"status": "partial", "reason": reason},
            "typed_links": {"links": [], "link_count": 0},
            "profiles": [],
            "profile_result": {"failure_count": 1, "partial_reason": reason},
            "migration": migration,
            "partial_reason": reason,
            "paths": [*profile_paths, *checkpoint_paths],
        }
    profiles = profile_result["profiles"]
    profile_partial_reason = ""
    if int(profile_result.get("failure_count", 0) or 0):
        profile_partial_reason = f"literature_profiling_partial:{int(profile_result['failure_count'])}_profile_failure"
    if progress is not None:
        progress.set_stage(
            "relation_mapping",
            profile_count=len(profiles),
            profile_valid_count=int(profile_result["valid_count"]),
            profile_excluded_count=int(profile_result["excluded_count"]),
            checkpoint_hit_count=int(profile_result["checkpoint_hits"]),
            literature_provider_call_count=int(profile_result["provider_calls"]),
            literature_failure_count=int(profile_result["failure_count"]),
        )
    typed = {
        "path": str(workspace / "02_source_memory" / "indexes" / "typed_links.yml"),
        "compatibility_path": str(
            workspace / "02_source_memory" / "indexes" / "typed_note_links.yml"
        ),
        "links": [],
        "link_count": 0,
    }
    base_literature_request = LiteratureMapRequest(
        workspace=workspace,
        source_set_id=str(source_set.get("source_set_id") or ""),
        run_id=run_id,
        map_id=stable_literature_map_id(source_set),
        question=None,
        provider=effective_request.provider,
        model=effective_request.model,
        allow_cloud=effective_request.allow_cloud,
        literature_policy=effective_request.literature_policy,
    )
    try:
        cluster_map, gap_map, packet, paths = build_literature_map(
            workspace,
            source_set=source_set,
            notes=note_rows,
            question=question,
            run_id=run_id,
            profiles=profiles,
            request=base_literature_request,
            policy=effective_request.literature_policy,
            reasoner=reasoner,
            stage_callback=(progress.set_stage if progress is not None else None),
            navigation_policy=effective_request.navigation_policy,
        )
    except Exception as exc:
        reason = f"literature_synthesis_partial:{type(exc).__name__}:{exc}"
        if profile_partial_reason:
            reason = f"{profile_partial_reason};{reason}"
        if progress is not None:
            progress.update_literature(
                literature_failure_count=int(
                    progress.literature.get("literature_failure_count", 0) or 0
                )
                + 1
            )
        return {
            "source_set": dict(source_set),
            "cluster_map": {
                "status": "partial",
                "clusters": [],
                "relations": [],
                "unclustered_sources": [],
            },
            "gap_map": {
                "status": "partial",
                "gap_candidates": [],
                "novelty_claimed": False,
            },
            "literature_packet": {"status": "partial", "reason": reason},
            "typed_links": typed,
            "profiles": profiles,
            "profile_result": {
                key: value for key, value in profile_result.items() if key != "profiles"
            },
            "migration": migration,
            "partial_reason": reason,
            "paths": [
                Path(typed["path"]),
                Path(typed["compatibility_path"]),
                *profile_result["paths"],
                *_existing_source_set_paths(workspace, source_set),
            ],
        }
    navigation = (
        cluster_map.get("navigation", {})
        if isinstance(cluster_map.get("navigation"), Mapping)
        else {}
    )
    navigation_relations = [
        dict(row)
        for row in navigation.get("typed_relations", []) or []
        if isinstance(row, Mapping)
    ]
    typed = {
        "path": str(workspace / "02_source_memory" / "indexes" / "typed_links.yml"),
        "compatibility_path": str(
            workspace / "02_source_memory" / "indexes" / "typed_note_links.yml"
        ),
        "links": navigation_relations,
        "link_count": len(navigation_relations),
        "relation_counts": dict(navigation.get("typed_relation_counts", {}) or {}),
        "graph_projection_hash": str(navigation.get("graph_projection_hash") or ""),
    }
    profile_packet_paths = [
        path
        for path in profile_result["paths"]
        if path.parent.name == "packets" and path.name.startswith("packet-")
    ]
    packet = {
        **packet,
        "profile_packet_count": len(profile_packet_paths),
        "profile_packet_paths": [str(path) for path in profile_packet_paths],
    }
    synthesis_partial_reason = str(packet.get("partial_reason") or "")
    _attach_profile_packet_lineage(paths, profile_packet_paths)
    if progress is not None:
        synthesis_calls = int(packet.get("synthesis_call_count", 0) or 0)
        synthesis_checkpoint_hits = int(
            packet.get("synthesis_checkpoint_hit_count", 0) or 0
        )
        synthesis_failures = int(packet.get("synthesis_failure_count", 0) or 0)
        analytical_source_ids = _analytical_profile_source_ids(profiles)
        progress.update_literature(
            unclustered_count=sum(
                1
                for row in cluster_map.get("unclustered_sources", []) or []
                if isinstance(row, Mapping)
                and str(row.get("source_id") or "") in analytical_source_ids
            ),
            topic_neighborhood_count=int(
                cluster_map.get("topic_neighborhood_count", 0) or 0
            ),
            subject_tag_count=int(navigation.get("promoted_subject_tag_count", 0) or 0),
            subject_tag_assignment_count=len(navigation.get("assignments", []) or []),
            typed_relation_count=len(navigation_relations),
            singleton_facet_count=int(navigation.get("singleton_facet_count", 0) or 0),
            proposition_count=int(cluster_map.get("proposition_count", 0) or 0),
            evidence_base_group_count=int(
                cluster_map.get("evidence_base_group_count", 0) or 0
            ),
            cluster_count=len(cluster_map.get("clusters", []) or []),
            evidence_concentrated_cluster_count=int(
                cluster_map.get("evidence_concentrated_cluster_count", 0) or 0
            ),
            cluster_source_contribution_count=int(
                cluster_map.get("cluster_source_contribution_count", 0) or 0
            ),
            synthesized_cluster_count=int(
                cluster_map.get("synthesized_cluster_count", 0) or 0
            ),
            mapped_gap_count=sum(
                1
                for gap in gap_map.get("gap_candidates", []) or []
                if gap.get("status") == "collection_surviving_gap"
            ),
            gap_lead_count=sum(
                1
                for gap in gap_map.get("gap_candidates", []) or []
                if gap.get("status") == "collection_gap_lead"
            ),
            rejected_underspecified_gap_count=int(
                gap_map.get("rejected_underspecified_gap_count", 0) or 0
            ),
            rejected_gap_quality_count=int(
                gap_map.get("rejected_gap_quality_count", 0) or 0
            ),
            merged_gap_count=int(gap_map.get("merged_gap_count", 0) or 0),
            strict_consensus_established_count=int(
                cluster_map.get("strict_consensus_established_count", 0) or 0
            ),
            strict_consensus_not_established_count=int(
                cluster_map.get("strict_consensus_not_established_count", 0) or 0
            ),
            strict_contradiction_established_count=int(
                cluster_map.get("strict_contradiction_established_count", 0) or 0
            ),
            strict_contradiction_not_established_count=int(
                cluster_map.get("strict_contradiction_not_established_count", 0) or 0
            ),
            strong_gap_established_count=int(
                gap_map.get("strong_gap_established_count", 0) or 0
            ),
            strong_gap_not_established_count=int(
                gap_map.get("strong_gap_not_established_count", 0) or 0
            ),
            synthesis_call_count=synthesis_calls,
            synthesis_checkpoint_hit_count=synthesis_checkpoint_hits,
            synthesis_failure_count=synthesis_failures,
            quantitative_comparison_count=int(
                cluster_map.get("quantitative_comparison_count", 0) or 0
            ),
            rejected_quantitative_comparison_count=int(
                cluster_map.get("rejected_quantitative_comparison_count", 0) or 0
            ),
            rejected_generated_locator_count=int(
                cluster_map.get("rejected_generated_locator_count", 0) or 0
            ),
            coverage_inventory_count=int(
                cluster_map.get("coverage_inventory_count", 0) or 0
            ),
            coverage_exhausted_count=int(
                cluster_map.get("coverage_exhausted_count", 0) or 0
            ),
            coverage_accounting_valid=bool(
                cluster_map.get("coverage_accounting_valid", False)
            ),
            literature_provider_call_count=int(
                profile_result.get("provider_calls", 0) or 0
            )
            + synthesis_calls,
            checkpoint_hit_count=int(profile_result.get("checkpoint_hits", 0) or 0)
            + synthesis_checkpoint_hits,
            literature_failure_count=int(profile_result.get("failure_count", 0) or 0)
            + synthesis_failures,
            active_cluster="",
            active_gap_packet="",
            active_synthesis_packet="",
        )
    profile_rows_for_links = [profile_to_dict(profile) for profile in profiles]
    related: dict[str, list[dict[str, Any]]] = {}
    for profile in profile_rows_for_links:
        source_id = str(profile.get("source_id") or "")
        note_id = str(profile.get("note_id") or "")
        if not source_id or not note_id:
            continue
        related[note_id] = [
            {
                "note_id": str(link.get("target_note_id") or ""),
                "relation_type": str(
                    link.get("primary_relation_type") or "semantic_similarity"
                ),
                "reason": str(link.get("reason") or ""),
            }
            for link in rank_human_related_links(
                source_id,
                profile_rows_for_links,
                navigation_relations,
                max_inferred_links=effective_request.navigation_policy.max_inferred_related_note_links,
            )
            if link.get("target_note_id")
        ]
    subject_tags_by_note: dict[str, list[str]] = defaultdict(list)
    for assignment in navigation.get("assignments", []) or []:
        if not isinstance(assignment, Mapping) or not assignment.get("visible"):
            continue
        note_id = str(assignment.get("note_id") or "")
        canonical_tag = str(assignment.get("canonical_tag") or "")
        if (
            note_id
            and canonical_tag
            and canonical_tag not in subject_tags_by_note[note_id]
        ):
            subject_tags_by_note[note_id].append(canonical_tag)
    clusters_by_note: dict[str, list[str]] = {}
    cluster_by_id = {
        str(cluster["cluster_id"]): cluster for cluster in cluster_map["clusters"]
    }
    for cluster in cluster_by_id.values():
        for note_id in cluster.get("note_ids", []):
            clusters_by_note.setdefault(str(note_id), []).append(
                str(cluster["cluster_id"])
            )
    gap_by_id = {
        str(gap["gap_id"]): gap
        for gap in gap_map.get("gap_candidates", []) or []
        if gap.get("gap_id")
    }
    note_id_by_source = {
        str(row["source_id"]): str(row["note_id"])
        for row in note_rows
        if row.get("source_id") and row.get("note_id")
    }
    gaps_by_note: dict[str, list[dict[str, str]]] = {}

    def add_gap_relation(source_id: Any, gap_id: str, relation_type: str) -> None:
        note_id = note_id_by_source.get(str(source_id or ""))
        if not note_id:
            return
        relation = {"gap_id": gap_id, "relation_type": relation_type}
        if relation not in gaps_by_note.setdefault(note_id, []):
            gaps_by_note[note_id].append(relation)

    for gap in gap_map.get("gap_candidates", []) or []:
        gap_id = str(gap.get("gap_id") or "")
        if not gap_id:
            continue
        for evidence in gap.get("supporting_evidence", []) or []:
            add_gap_relation(evidence.get("source_id"), gap_id, "supports_gap_rule")
        for evidence in gap.get("countervailing_evidence", []) or []:
            add_gap_relation(
                evidence.get("source_id"), gap_id, "countervailing_gap_evidence"
            )
        for warning in gap.get("warnings", []) or []:
            if warning.get("warning") == "possible_counterevidence_requires_full_text":
                add_gap_relation(
                    warning.get("source_id"), gap_id, str(warning["warning"])
                )
    note_stem_by_id = {
        str(row["note_id"]): Path(str(row["note_path"])).stem for row in note_rows
    }
    note_paths: list[Path] = []
    for row in note_rows:
        path = workspace / str(row["note_path"])
        if not path.exists():
            continue
        related_links = [
            {
                **link,
                "target_stem": note_stem_by_id.get(
                    str(link["note_id"]), str(link["note_id"])
                ),
            }
            for link in sorted(
                related.get(str(row["note_id"]), []),
                key=lambda value: (value["note_id"], value["relation_type"]),
            )
        ]
        cluster_ids = sorted(clusters_by_note.get(str(row["note_id"]), []))
        note_gap_links = sorted(
            gaps_by_note.get(str(row["note_id"]), []),
            key=lambda value: (value["gap_id"], value["relation_type"]),
        )
        note_gap_links = [
            {
                **link,
                "wikilink": (
                    f"[[{gap_note_stem(gap_by_id[link['gap_id']])}|{gap_display_title(gap_by_id[link['gap_id']])}]]"
                ),
            }
            for link in note_gap_links
            if link["gap_id"] in gap_by_id
        ]
        gap_ids = sorted({link["gap_id"] for link in note_gap_links})
        cluster_wikilinks = {
            cluster_id: (
                f"[[{cluster_note_stem(cluster_by_id[cluster_id])}|{cluster_display_title(cluster_by_id[cluster_id])}]]"
            )
            for cluster_id in cluster_ids
            if cluster_id in cluster_by_id
        }
        gap_wikilinks = {
            link["gap_id"]: str(link["wikilink"]) for link in note_gap_links
        }
        update_note_graph(
            path,
            {
                "related_notes": [
                    {
                        "note_id": link["note_id"],
                        "relation_type": link["relation_type"],
                        "reason": link.get("reason", ""),
                        "wikilink": f"[[{link['target_stem']}]]",
                    }
                    for link in related_links
                ],
                "clusters": cluster_ids,
                "cluster_links": [
                    cluster_wikilinks[cluster_id]
                    for cluster_id in cluster_ids
                    if cluster_id in cluster_wikilinks
                ],
                "gaps": gap_ids,
                "gap_links": [gap_wikilinks[gap_id] for gap_id in gap_ids],
                "tags": sorted(
                    subject_tags_by_note.get(str(row.get("note_id") or ""), [])
                ),
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "updated_at": now_iso(),
            },
            related_links,
            cluster_ids,
            note_gap_links,
            cluster_wikilinks,
        )
        note_paths.append(path)
    source_index = write_source_index(workspace, note_paths)
    source_set = update_source_set_map(
        workspace, source_set, cluster_map["clusters"], gap_map["gap_candidates"]
    )
    projection_hashes = {
        str(row["note_id"]): sha256_file(workspace / str(row["note_path"]))
        for row in note_rows
        if row.get("note_id")
        and row.get("note_path")
        and (workspace / str(row["note_path"])).is_file()
    }
    profile_payloads = [profile_to_dict(profile) for profile in profiles]
    _finalize_literature_projection_hashes(
        paths,
        projection_hashes,
        profile_hashes={
            str(row.get("note_id") or ""): str(row.get("note_hash") or "")
            for row in profile_payloads
        },
        profile_dependency_hashes={
            str(row.get("note_id") or ""): str(row.get("dependency_hash") or "")
            for row in profile_payloads
        },
        source_set=source_set,
        request=effective_request,
    )
    result = {
        "source_set": source_set,
        "cluster_map": cluster_map,
        "gap_map": gap_map,
        "literature_packet": packet,
        "typed_links": typed,
        "profiles": profiles,
        "profile_result": {
            key: value for key, value in profile_result.items() if key != "profiles"
        },
        "migration": migration,
        "paths": [
            Path(typed["path"]),
            Path(typed["compatibility_path"]),
            source_index,
            *profile_result["paths"],
            *paths,
            Path(source_set["path"]),
            Path(source_set.get("latest_path", source_set["path"])),
        ],
    }
    partial_reasons = [
        reason
        for reason in (profile_partial_reason, synthesis_partial_reason)
        if reason
    ]
    if partial_reasons:
        result["partial_reason"] = ";".join(partial_reasons)
    return result


def _build_profiles_for_map(
    workspace: Path,
    note_rows: Sequence[Mapping[str, Any]],
    *,
    source_set: Mapping[str, Any],
    run_id: str,
    request: MapRequest,
    reasoner: LiteratureReasoner | None,
    progress: _RunProgress | None,
    resume: bool,
) -> dict[str, Any]:
    del (
        resume
    )  # Checkpoint fingerprints, rather than a caller flag, determine reuse safety.
    profiles_dir = workspace / "02_source_memory" / "profiles"
    literature_state = run_directory(workspace, run_id) / "literature"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    literature_state.mkdir(parents=True, exist_ok=True)
    review_aliases = review_hash_aliases(workspace)
    profiles_by_index: dict[int, Any] = {}
    paths: list[Path] = []
    failure_records: list[dict[str, Any]] = []
    checkpoint_hits = 0
    provider_calls = 0
    failures = 0
    valid_count = 0
    excluded_count = 0
    started = time.monotonic()
    provider_lock = threading.Lock()

    def reserve_provider_call() -> None:
        nonlocal provider_calls
        with provider_lock:
            if provider_calls >= request.literature_policy.max_profile_calls:
                raise RuntimeError("literature_profile_call_budget_reached")
            provider_calls += 1
            current = provider_calls
        if progress is not None:
            progress.update_literature(literature_provider_call_count=current)

    def build_one(index: int, row: Mapping[str, Any]) -> dict[str, Any]:
        if (
            time.monotonic() - started
            >= request.literature_policy.literature_deadline_seconds
        ):
            raise RuntimeError("literature_stage_deadline_reached")
        path = workspace / str(row.get("note_path") or "")
        if not path.is_file():
            return {
                "index": index,
                "profile": None,
                "paths": [],
                "checkpoint_hit": 0,
                "failure": 1,
            }
        text = path.read_text(encoding="utf-8")
        note_id = str(row.get("note_id") or "")
        note_status = str(row.get("note_status") or "")
        terminal_status = str(row.get("terminal_status") or "")
        analytical = (
            note_status in {"analytical_atomic_note", "verified_atomic_note"}
            or terminal_status == "validated_note"
        ) and str(row.get("source_scope") or "full_document") == "full_document"
        profile_policy, profile_route, reasoner_identity = _profile_dependency_policy(
            request,
            reasoner,
            analytical=analytical,
        )
        fingerprint = profile_dependency_fingerprint(
            text,
            source_set_id=str(source_set.get("source_set_id") or ""),
            provider=request.provider,
            model=request.model,
            policy=profile_policy,
        )
        profile = load_profile_checkpoint(literature_state, note_id, fingerprint)
        checkpoint_hit = 0
        mechanically_upgraded = False
        if profile is not None:
            checkpoint_hit = 1
        else:
            sidecar = profile_sidecar_path(profiles_dir, note_id)
            if sidecar.exists():
                existing = load_profile_sidecar(sidecar)
                existing_payload = profile_to_dict(existing)
                existing_dependency = str(existing_payload.get("dependency_hash") or "")
                if existing_dependency == fingerprint:
                    profile = existing
                    checkpoint_hit = 1
                else:
                    existing_context = (
                        dict(existing_payload.get("context", {}))
                        if isinstance(existing_payload.get("context", {}), Mapping)
                        else {}
                    )
                    recorded_source_set_id = str(
                        existing_context.get("source_set_id") or ""
                    )
                    current_note_hash = semantic_note_hash(text)
                    lineage_matches = (
                        str(existing_payload.get("note_id") or "") == note_id
                        and str(existing_payload.get("source_id") or "")
                        == str(row.get("source_id") or "")
                        and str(existing_payload.get("note_hash") or "")
                        in {
                            current_note_hash,
                            str(
                                review_aliases.get(note_id, {}).get(
                                    "legacy_semantic_hash"
                                )
                                or ""
                            ),
                        }
                    )
                    stored_route = str(
                        existing_context.get("profile_generation_route") or ""
                    )
                    stored_identity = str(
                        existing_context.get("reasoner_identity") or ""
                    )
                    stored_validity = dict(existing_payload.get("validity") or {})
                    if bool(
                        stored_validity.get("legacy_profile_upgraded_mechanically")
                    ):
                        # Older replay code could relabel a reused mechanical
                        # profile as built_in_reader while leaving its dependency
                        # hash and content untouched. The durable migration marker
                        # is authoritative for repairing that metadata drift.
                        stored_route = "mechanical_legacy_upgrade"
                        stored_identity = "auto_zettelkasten.profiles.legacy_upgrade:v4"
                        existing_context["profile_generation_route"] = stored_route
                        existing_context["reasoner_identity"] = stored_identity
                    if stored_route and stored_identity and lineage_matches:
                        stored_policy = dict(profile_policy)
                        stored_policy["profile_generation_route"] = stored_route
                        stored_policy["reasoner_identity"] = stored_identity
                        stored_recorded_fingerprint = profile_dependency_fingerprint(
                            text,
                            source_set_id=recorded_source_set_id,
                            provider=request.provider,
                            model=request.model,
                            policy=stored_policy,
                        )
                        if existing_dependency == stored_recorded_fingerprint:
                            fingerprint = profile_dependency_fingerprint(
                                text,
                                source_set_id=str(
                                    source_set.get("source_set_id") or ""
                                ),
                                provider=request.provider,
                                model=request.model,
                                policy=stored_policy,
                            )
                            existing.note_hash = current_note_hash
                            existing_context["source_set_id"] = str(
                                source_set.get("source_set_id") or ""
                            )
                            existing.context = existing_context
                            existing.dependency_hash = fingerprint
                            profile = existing
                            checkpoint_hit = 1
                    mechanical_route_is_current = (
                        stored_route
                        in {
                            "deterministic",
                            "mechanical_legacy_upgrade",
                            "mechanical_reasoner_contract_fallback",
                        }
                        and all(
                            str(stored_validity.get(field_name) or "")
                            == expected_version
                            for field_name, expected_version in (
                                ("profile_prompt_version", PROFILE_PROMPT_VERSION),
                                ("classifier_version", PROFILE_CLASSIFIER_VERSION),
                                ("algorithm_version", PROFILE_ALGORITHM_VERSION),
                            )
                        )
                        and str(existing_payload.get("provider") or "")
                        == request.provider
                        and str(existing_payload.get("model") or "") == request.model
                    )
                    note_frontmatter, _ = parse_atomic_note(text)
                    source_content_matches = bool(
                        str(note_frontmatter.get("inspected_content_hash") or "")
                        and str(existing_payload.get("source_hash") or "")
                        == str(note_frontmatter.get("inspected_content_hash") or "")
                    )
                    stored_profile_validation = stored_validity.get(
                        "profile_validation", {}
                    )
                    if (
                        profile is None
                        and mechanical_route_is_current
                        and source_content_matches
                        and str(existing_payload.get("note_id") or "") == note_id
                        and str(existing_payload.get("source_id") or "")
                        == str(row.get("source_id") or "")
                        and isinstance(stored_profile_validation, Mapping)
                        and bool(stored_profile_validation.get("passed"))
                    ):
                        # A mechanical legacy profile is derived from the same
                        # inspected source content.  Projection/frontmatter
                        # changes must not force a paid profile refresh when the
                        # source lineage and current profile contract still pass.
                        stored_policy = dict(profile_policy)
                        stored_policy["profile_generation_route"] = stored_route
                        stored_policy["reasoner_identity"] = stored_identity
                        fingerprint = profile_dependency_fingerprint(
                            text,
                            source_set_id=str(source_set.get("source_set_id") or ""),
                            provider=request.provider,
                            model=request.model,
                            policy=stored_policy,
                        )
                        existing.note_hash = current_note_hash
                        existing_context["source_set_id"] = str(
                            source_set.get("source_set_id") or ""
                        )
                        existing_context["profile_generation_route"] = stored_route
                        existing_context["reasoner_identity"] = stored_identity
                        existing_context["profile_reuse_basis"] = (
                            "unchanged_inspected_source_content"
                        )
                        existing.context = existing_context
                        existing.dependency_hash = fingerprint
                        profile = existing
                        checkpoint_hit = 1
                    if (
                        profile is None
                        and lineage_matches
                        and mechanical_route_is_current
                    ):
                        # Mechanical profiles come from committed notes, not
                        # literature-stage budgets or promotion settings. Migrate
                        # the dependency hash without converting replay into a
                        # paid profiling call.
                        stored_policy = dict(profile_policy)
                        stored_policy["profile_generation_route"] = stored_route
                        stored_policy["reasoner_identity"] = stored_identity
                        fingerprint = profile_dependency_fingerprint(
                            text,
                            source_set_id=str(source_set.get("source_set_id") or ""),
                            provider=request.provider,
                            model=request.model,
                            policy=stored_policy,
                        )
                        existing.note_hash = current_note_hash
                        existing_context["source_set_id"] = str(
                            source_set.get("source_set_id") or ""
                        )
                        existing_context["profile_generation_route"] = stored_route
                        existing_context["reasoner_identity"] = stored_identity
                        existing.context = existing_context
                        existing.dependency_hash = fingerprint
                        profile = existing
                        checkpoint_hit = 1
                    alias = review_aliases.get(note_id, {})
                    alias_matches = str(existing_payload.get("note_hash") or "") == str(
                        alias.get("legacy_semantic_hash") or ""
                    ) and semantic_note_hash(text) == str(
                        alias.get("semantic_hash") or ""
                    )
                    if profile is None and alias_matches:
                        existing.note_hash = str(alias["semantic_hash"])
                        existing_context["source_set_id"] = str(
                            source_set.get("source_set_id") or ""
                        )
                        existing.context = existing_context
                        existing.dependency_hash = fingerprint
                        profile = existing
                        checkpoint_hit = 1
                    recorded_fingerprint = profile_dependency_fingerprint(
                        text,
                        source_set_id=recorded_source_set_id,
                        provider=request.provider,
                        model=request.model,
                        policy=profile_policy,
                    )
                    if (
                        profile is None
                        and recorded_source_set_id
                        and existing_dependency == recorded_fingerprint
                    ):
                        existing_context["source_set_id"] = str(
                            source_set.get("source_set_id") or ""
                        )
                        existing.context = existing_context
                        existing.dependency_hash = fingerprint
                        profile = existing
                        checkpoint_hit = 1
                    existing_validity = dict(existing_payload.get("validity") or {})
                    legacy_profile = any(
                        str(existing_validity.get(field_name) or "1")
                        != expected_version
                        for field_name, expected_version in (
                            ("profile_prompt_version", PROFILE_PROMPT_VERSION),
                            ("classifier_version", PROFILE_CLASSIFIER_VERSION),
                            ("algorithm_version", PROFILE_ALGORITHM_VERSION),
                        )
                    )
                    if profile is None and legacy_profile and lineage_matches:
                        existing.note_hash = current_note_hash
                        existing.provider = request.provider
                        existing.model = request.model
                        existing.dependency_hash = fingerprint
                        existing_context["source_set_id"] = str(
                            source_set.get("source_set_id") or ""
                        )
                        existing_context["profile_generation_route"] = (
                            "mechanical_legacy_upgrade"
                        )
                        existing_context["reasoner_identity"] = (
                            "auto_zettelkasten.profiles.legacy_upgrade:v4"
                        )
                        existing.context = existing_context
                        existing_validity.update(
                            profile_prompt_version=PROFILE_PROMPT_VERSION,
                            classifier_version=PROFILE_CLASSIFIER_VERSION,
                            algorithm_version=PROFILE_ALGORITHM_VERSION,
                            legacy_profile_upgraded_mechanically=True,
                        )
                        existing.validity = existing_validity
                        profile = existing
                        checkpoint_hit = 1
                        mechanically_upgraded = True
            if profile is None:
                if reasoner is not None and analytical:
                    reserve_provider_call()

                    def reasoner_method(
                        prompt: str, *, _row: Mapping[str, Any] = row, _text: str = text
                    ) -> Any:
                        return reasoner.profile_source(
                            {
                                **dict(_row),
                                "committed_note": _text,
                                "profile_prompt": prompt,
                            },
                            question=None,
                            context={
                                "source_set_id": source_set.get("source_set_id", ""),
                                "profile_prompt_version": PROFILE_PROMPT_VERSION,
                                "profile_generation_route": profile_route,
                                "reasoner_identity": reasoner_identity,
                            },
                        )
                else:
                    reasoner_method = None
                try:
                    profile = build_evidence_profile(
                        text,
                        source_set_id=str(source_set.get("source_set_id") or ""),
                        provider=request.provider,
                        model=request.model,
                        policy=profile_policy,
                        reasoner_method=reasoner_method,
                    )
                except (ProfileContractError, ProfileParseError) as exc:
                    # A complete but contract-invalid model response is not a
                    # transient transport failure. Preserve collection progress
                    # with a conservative committed-note profile that cannot
                    # support synthesis until a later lazy reprofile succeeds.
                    profile_route = "mechanical_reasoner_contract_fallback"
                    reasoner_identity = (
                        "auto_zettelkasten.profiles.contract_fallback:v1"
                    )
                    fallback_policy = dict(profile_policy)
                    fallback_policy.update(
                        profile_generation_route=profile_route,
                        reasoner_identity=reasoner_identity,
                    )
                    fingerprint = profile_dependency_fingerprint(
                        text,
                        source_set_id=str(source_set.get("source_set_id") or ""),
                        provider=request.provider,
                        model=request.model,
                        policy=fallback_policy,
                    )
                    profile = build_evidence_profile(
                        text,
                        source_set_id=str(source_set.get("source_set_id") or ""),
                        provider=request.provider,
                        model=request.model,
                        policy=fallback_policy,
                    )
                    profile.excluded_from_synthesis = True
                    profile.exclusion_reason = (
                        "profile_reasoner_contract_fallback_requires_lazy_reprofile"
                    )
                    fallback_context = dict(getattr(profile, "context", {}) or {})
                    fallback_context.update(
                        profile_generation_route=profile_route,
                        reasoner_identity=reasoner_identity,
                        profile_fallback_reason=f"{type(exc).__name__}: {exc}",
                        lazy_reprofile_required=True,
                    )
                    profile.context = fallback_context
        note_valid = bool(row.get("validation_passed", True))
        prior_exclusion_reason = str(getattr(profile, "exclusion_reason", "") or "")
        if (
            analytical
            and note_valid
            and prior_exclusion_reason.startswith("profile_or_note_validation_failed:")
        ):
            # A legacy profile may have been excluded before v1.2
            # source-locator and quantitative records were mechanically
            # derivable. Re-evaluate it after the zero-call committed-note
            # upgrade instead of turning one ambiguous anchor into a
            # permanent document-level exclusion. This must also run for an
            # exact profile-checkpoint hit.
            profile.excluded_from_synthesis = False
            profile.exclusion_reason = ""
        stored_context = dict(getattr(profile, "context", {}) or {})
        stored_route = str(stored_context.get("profile_generation_route") or "")
        if stored_route in {
            "deterministic",
            "mechanical_legacy_upgrade",
            "mechanical_reasoner_contract_fallback",
        }:
            profile, _ = augment_profile_from_committed_note(
                profile,
                text,
                source_set_id=str(source_set.get("source_set_id") or ""),
                provider=request.provider,
                model=request.model,
                policy=profile_policy,
            )
        validation = validate_profile(
            profile,
            require_substantive=analytical,
        )
        validity = dict(getattr(profile, "validity", {}) or {})
        validity.update(
            {
                "status": "valid" if validation.passed and note_valid else "invalid",
                "profile_validation": validation.to_dict(),
                "note_validation_passed": note_valid,
                "validation_mode": "automated",
            }
        )
        profile.validity = validity
        context = dict(getattr(profile, "context", {}) or {})
        metadata = (
            context.get("metadata", {})
            if isinstance(context.get("metadata", {}), Mapping)
            else {}
        )
        preserved_route = (
            str(context.get("profile_generation_route") or "") if checkpoint_hit else ""
        )
        preserved_identity = (
            str(context.get("reasoner_identity") or "") if checkpoint_hit else ""
        )
        context.update(
            {
                "title": row.get("title", metadata.get("title", "")),
                "date": row.get("date", metadata.get("date", "")),
                "note_path": row.get("note_path", ""),
                "zotero_item_key": row.get("zotero_item_key", ""),
                "zotero_relations": dict(row.get("zotero_relations", {}) or {}),
                "profile_generation_route": (
                    preserved_route
                    or (
                        "mechanical_legacy_upgrade"
                        if mechanically_upgraded
                        else profile_route
                    )
                ),
                "reasoner_identity": (
                    preserved_identity
                    or (
                        "auto_zettelkasten.profiles.legacy_upgrade:v2"
                        if mechanically_upgraded
                        else reasoner_identity
                    )
                ),
            }
        )
        profile.context = context
        if not validation.passed or not note_valid:
            profile.excluded_from_synthesis = True
            reasons = list(validation.errors) + list(
                row.get("validation_errors", []) or []
            )
            profile.exclusion_reason = "profile_or_note_validation_failed:" + ",".join(
                sorted(set(reasons))
            )
        elif prior_exclusion_reason.startswith("profile_or_note_validation_failed:"):
            profile.excluded_from_synthesis = False
            profile.exclusion_reason = ""
        profile_path = profile_sidecar_path(profiles_dir, note_id)
        save_profile(profiles_dir, profile)
        # Feed synthesis the exact canonical object that resume will reload.
        # Mechanical migrations can normalize nested dataclass fields during
        # serialization; returning the pre-write instance makes the first
        # migrated run hash differently from its immediate replay.
        profile = load_profile_sidecar(profile_path)
        write_profile_checkpoint(literature_state, note_id, fingerprint, profile)
        final_validation = validate_profile(profile, require_substantive=False)
        checkpoint_path = (
            literature_state
            / "profile_calls"
            / f"{slugify(note_id, fallback='profile')}.yml"
        )
        failure_path = (
            literature_state
            / "profile_failures"
            / f"{slugify(note_id, fallback='profile')}.yml"
        )
        if failure_path.exists():
            failure_path.unlink()
        return {
            "index": index,
            "profile": profile,
            "paths": [
                candidate
                for candidate in (profile_path, checkpoint_path)
                if candidate.exists()
            ],
            "checkpoint_hit": checkpoint_hit,
            "failure": 0,
            "excluded": bool(getattr(profile, "excluded_from_synthesis", False))
            or not final_validation.passed,
        }

    workers = max(
        1, min(request.literature_policy.profile_workers, len(note_rows) or 1)
    )
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="auto-zettelkasten-profile"
    ) as executor:
        future_map = {
            executor.submit(build_one, index, row): index
            for index, row in enumerate(note_rows)
        }
        for future in as_completed(future_map):
            try:
                result = future.result()
            except Exception as exc:
                index = future_map[future]
                row = note_rows[index]
                note_id = str(row.get("note_id") or f"inventory-{index}")
                failure_path = (
                    literature_state
                    / "profile_failures"
                    / f"{slugify(note_id, fallback='profile')}.yml"
                )
                failure_record = {
                    "status": "partial",
                    "note_id": note_id,
                    "source_id": str(row.get("source_id") or ""),
                    "zotero_item_key": str(row.get("zotero_item_key") or ""),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "retry_on_resume": True,
                    "updated_at": now_iso(),
                }
                write_yaml(failure_path, failure_record)
                failure_records.append(failure_record)
                paths.append(failure_path)
                failures += 1
                if progress is not None:
                    progress.update_literature(
                        profile_count=len(profiles_by_index),
                        profile_valid_count=valid_count,
                        profile_excluded_count=excluded_count,
                        checkpoint_hit_count=checkpoint_hits,
                        literature_provider_call_count=provider_calls,
                        literature_failure_count=failures,
                    )
                continue
            failures += int(result["failure"])
            checkpoint_hits += int(result["checkpoint_hit"])
            paths.extend(result["paths"])
            profile = result["profile"]
            if profile is not None:
                profiles_by_index[int(result["index"])] = profile
                if result["excluded"]:
                    excluded_count += 1
                else:
                    valid_count += 1
            if progress is not None:
                progress.update_literature(
                    profile_count=len(profiles_by_index),
                    profile_valid_count=valid_count,
                    profile_excluded_count=excluded_count,
                    checkpoint_hit_count=checkpoint_hits,
                    literature_provider_call_count=provider_calls,
                    literature_failure_count=failures,
                )
    profiles = [profiles_by_index[index] for index in sorted(profiles_by_index)]
    packet_result = _write_profile_packets(
        literature_state,
        profiles,
        source_set=source_set,
        request=request,
        reasoner=reasoner,
        progress=progress,
    )
    paths.extend(packet_result["paths"])
    checkpoint_hits += int(packet_result["checkpoint_hits"])
    return {
        "profiles": profiles,
        "paths": sorted(set(paths)),
        "valid_count": valid_count,
        "excluded_count": excluded_count,
        "checkpoint_hits": checkpoint_hits,
        "provider_calls": provider_calls,
        "failure_count": failures,
        "failures": failure_records,
        "profile_packet_count": int(packet_result["packet_count"]),
    }


def _profile_dependency_policy(
    request: MapRequest,
    reasoner: LiteratureReasoner | None,
    *,
    analytical: bool,
) -> tuple[dict[str, Any], str, str]:
    if not analytical or reasoner is None:
        route = "deterministic"
        identity = "auto_zettelkasten.profiles.deterministic_profile:v1"
    else:
        route = str(
            getattr(reasoner, "profile_generation_route", "explicit_reasoner")
            or "explicit_reasoner"
        )
        identity = str(getattr(reasoner, "profile_reasoner_identity", "") or "")
        if not identity:
            reasoner_type = type(reasoner)
            identity = (
                f"{reasoner_type.__module__}.{reasoner_type.__qualname__}:"
                f"{getattr(reasoner, 'name', 'unknown')}:{getattr(reasoner, 'model', 'unknown')}"
            )
    profile_relevant_policy = request.literature_policy.to_dict()
    for field_name in (
        "weak_gap_handling",
        "cluster_gap_projection",
        "require_executable_gap_design",
    ):
        profile_relevant_policy.pop(field_name, None)
    policy = {
        "literature_policy": profile_relevant_policy,
        "profile_generation_route": route,
        "reasoner_identity": identity,
    }
    return policy, route, identity


def _write_profile_packets(
    literature_state: Path,
    profiles: Sequence[Any],
    *,
    source_set: Mapping[str, Any],
    request: MapRequest,
    reasoner: LiteratureReasoner | None,
    progress: _RunProgress | None,
) -> dict[str, Any]:
    profile_payloads = [profile_to_dict(profile) for profile in profiles]
    configured_context = int(getattr(reasoner, "context_window_tokens", 0) or 0)
    if configured_context <= 0:
        configured_context = (
            1_000_000
            if (request.provider, request.model)
            in {
                ("deepseek", "deepseek-v4-flash"),
                ("gemini", "gemini-2.5-flash"),
            }
            else 128_000
        )
    max_chars = max(
        8_000,
        int(
            configured_context
            * request.processing.estimated_chars_per_token
            * request.literature_policy.deepseek_packet_context_fraction
        ),
    )
    packets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for profile in profile_payloads:
        profile_chars = len(
            json.dumps(profile, sort_keys=True, ensure_ascii=False, default=str)
        )
        if current and current_chars + profile_chars > max_chars:
            packets.append(current)
            current = []
            current_chars = 0
        current.append(profile)
        current_chars += profile_chars
    if current or not packets:
        packets.append(current)
    packet_root = literature_state / "packets"
    paths: list[Path] = []
    checkpoint_hits = 0
    for index, packet_profiles in enumerate(packets, start=1):
        path = packet_root / f"packet-{index:04d}.yml"
        dependency = {
            "source_set_id": source_set.get("source_set_id", ""),
            "source_set_dependency_hash": source_set.get("dependency_hash", ""),
            "provider": request.provider,
            "model": request.model,
            "literature_policy": request.literature_policy.to_dict(),
            "profile_dependency_hashes": [
                row.get("dependency_hash", "") for row in packet_profiles
            ],
            "packet_index": index,
            "packet_count": len(packets),
        }
        payload = {
            "packet_schema_version": "1",
            "fingerprint": sha256_text(
                json.dumps(dependency, sort_keys=True, ensure_ascii=False, default=str)
            ),
            "packet_index": index,
            "packet_count": len(packets),
            "context_window_tokens": configured_context,
            "context_fraction": request.literature_policy.deepseek_packet_context_fraction,
            "max_packet_characters": max_chars,
            "profile_count": len(packet_profiles),
            "profiles": packet_profiles,
            "merge_required": len(packets) > 1,
        }
        existing = read_yaml(path, {}) or {}
        if existing == payload:
            checkpoint_hits += 1
        else:
            if progress is not None:
                progress.update_literature(active_synthesis_packet=str(path))
            write_yaml(path, payload)
        paths.append(path)
    if progress is not None:
        progress.update_literature(
            active_synthesis_packet="",
            checkpoint_hit_count=int(
                progress.literature.get("checkpoint_hit_count", 0) or 0
            )
            + checkpoint_hits,
        )
    return {
        "paths": paths,
        "checkpoint_hits": checkpoint_hits,
        "packet_count": len(packets),
    }


def _finalize_literature_projection_hashes(
    paths: Sequence[Path],
    projection_hashes: Mapping[str, str],
    *,
    profile_hashes: Mapping[str, str],
    profile_dependency_hashes: Mapping[str, str],
    source_set: Mapping[str, Any],
    request: MapRequest,
) -> None:
    manifest_paths = [path for path in paths if path.name == "manifest.yml"]
    for path in manifest_paths:
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"literature manifest must be a mapping: {path}")
        updated = dict(payload)
        lineage = {
            "note_projection_hashes": dict(sorted(projection_hashes.items())),
            "semantic_note_hashes": dict(sorted(profile_hashes.items())),
            "profile_dependency_hashes": dict(
                sorted(profile_dependency_hashes.items())
            ),
            "source_set_id": source_set.get("source_set_id", ""),
            "source_set_dependency_hash": source_set.get("dependency_hash", ""),
            "provider": request.provider,
            "model": request.model,
            "literature_policy": request.literature_policy.to_dict(),
            "algorithm_versions": {
                "collection_mapper": ENGINE_VERSION,
                "profile_prompt": PROFILE_PROMPT_VERSION,
                "profile_classifier": PROFILE_CLASSIFIER_VERSION,
                "profile_algorithm": PROFILE_ALGORITHM_VERSION,
                "committed_note_anchor_augmentation": COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION,
                "source_classifier": CONTENT_CLASSIFIER_VERSION,
                "chunking": CHUNKING_VERSION,
            },
            "validation_mode": "automated",
        }
        if all(updated.get(key) == value for key, value in lineage.items()):
            continue
        updated.update(lineage)
        write_yaml(path, updated)


def _attach_profile_packet_lineage(
    paths: Sequence[Path], profile_packet_paths: Sequence[Path]
) -> None:
    values = [str(path) for path in profile_packet_paths]
    for path in paths:
        if path.name != "packet.yml" and not path.name.startswith("literature-packet-"):
            continue
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"literature packet must be a mapping: {path}")
        if payload.get("profile_packet_paths") == values:
            continue
        write_yaml(
            path,
            {
                **dict(payload),
                "profile_packet_count": len(values),
                "profile_packet_paths": values,
            },
        )


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


def workspace_source_set(
    workspace: Path, note_rows: Sequence[Mapping[str, Any]], *, run_id: str
) -> dict[str, Any]:
    items = [
        {
            "key": str(row.get("zotero_item_key", "")),
            "data": {
                "key": str(row.get("zotero_item_key", "")),
                "title": str(row.get("title", "")),
            },
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
                if row.get("note_status")
                in {"analytical_atomic_note", "verified_atomic_note"}
                else "limited_note"
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
    run_dir: Path,
    index: int,
    item: dict[str, Any],
    request: MapRequest,
    client: ZoteroClient,
    reader: ReaderProvider,
    vision: VisionProvider | None,
    progress: _RunProgress | None = None,
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
    if progress is not None:
        progress.update(index, status="active", phase="acquiring_content", reason="")
    if not key:
        return _exhausted_result(index, item, "identity", "missing_zotero_item_key")
    checkpoint_root = run_dir / "items" / safe_filename(key)
    try:
        content = _load_frozen_content(checkpoint_root)
    except Exception as exc:
        base["reason"] = f"frozen_content_invalid:{type(exc).__name__}:{exc}"
        base["attempts"].append(
            _attempt(base, "frozen_content", "failed", base["reason"])
        )
        return base
    if content is not None:
        base["attempts"].append(
            _attempt(base, "frozen_content", "succeeded", "run_snapshot_reused")
        )
    else:
        content = _acquire_content(workspace, item, client, base, request, vision)
        if content:
            _write_frozen_content(checkpoint_root, content)
    if not content:
        base["reason"] = "all_allowed_extraction_routes_exhausted"
        base["attempts"].append(
            _attempt(
                base,
                "extraction_router",
                "failed",
                "all_allowed_extraction_routes_exhausted",
            )
        )
        return base
    content_hash = str(content["content_hash"])
    effective_provider = str(content.get("reader_provider") or base["reader_provider"])
    effective_model = str(content.get("reader_model") or base["reader_model"])
    fingerprint = _fingerprint(
        key,
        content_hash,
        request,
        item,
        effective_provider,
        effective_model,
        str(content.get("source_scope") or "full_document"),
    )
    base.update(
        content,
        fingerprint=fingerprint,
        reader_provider=effective_provider,
        reader_model=effective_model,
    )
    prior = (
        read_yaml(workspace / "11_state" / "fingerprints" / f"{fingerprint}.yml", {})
        or {}
    )
    prior_path = workspace / str(prior.get("note_path", ""))
    if prior.get("note_path") and _reusable_note(prior_path, base, request):
        prior_frontmatter, _ = parse_atomic_note(prior_path.read_text(encoding="utf-8"))
        prior_status = str(
            prior_frontmatter.get("note_status") or "analytical_atomic_note"
        )
        base.update(
            terminal_status="validated_note"
            if prior_status in {"analytical_atomic_note", "verified_atomic_note"}
            else "limited_note",
            note_path=str(prior["note_path"]),
            note_status=prior_status,
            reused=True,
            reason="fingerprint_match",
        )
        base["attempts"].append(
            _attempt(
                base,
                "resume_fingerprint",
                "skipped",
                "existing_validated_note_reused",
                output_path=str(prior_path),
            )
        )
        return base
    compatible_path = _compatible_committed_note(workspace, base, request)
    if compatible_path is not None:
        prior_frontmatter, _ = parse_atomic_note(
            compatible_path.read_text(encoding="utf-8")
        )
        prior_status = str(
            prior_frontmatter.get("note_status") or "analytical_atomic_note"
        )
        relative_path = str(compatible_path.relative_to(workspace))
        base.update(
            terminal_status="validated_note"
            if prior_status in {"analytical_atomic_note", "verified_atomic_note"}
            else "limited_note",
            note_path=relative_path,
            note_status=prior_status,
            reused=True,
            reason="compatible_committed_note",
        )
        _write_fingerprint(workspace, base, relative_path)
        base["attempts"].append(
            _attempt(
                base,
                "resume_compatible_note",
                "skipped",
                "existing_current_schema_note_reused",
                output_path=str(compatible_path),
            )
        )
        return base
    if content.get("analysis"):
        base["analysis"] = content["analysis"]
        return base
    source_scope = str(content.get("source_scope") or "full_document")
    if source_scope != "full_document":
        note_status = {
            "abstract_only": "abstract_only_atomic_note",
            "metadata_only": "metadata_only_source_note",
            "fulltext_available": "fulltext_available",
        }.get(source_scope, "metadata_only_source_note")
        base.update(
            terminal_status="limited_note",
            note_status=note_status,
            limited_analysis=_limited_analysis(content, item),
            reason=str(content.get("coverage_reason") or source_scope),
        )
        return base
    if bool(getattr(reader, "is_cloud", True)) and not request.allow_cloud:
        base["attempts"].append(
            _attempt(base, f"{reader.name}_text", "disallowed", "cloud_not_allowed")
        )
        base["reason"] = "reader_disallowed_by_privacy_policy"
        return base
    try:
        if progress is not None:
            progress.update(index, status="active", phase="reading_document")
        analysis, reader_route, reader_reason = _read_document(
            reader,
            str(content["text"]),
            item_data(item),
            None,
            request=request,
            checkpoint_root=checkpoint_root,
            progress=progress,
            inventory_index=index,
        )
    except DocumentPartialError as exc:
        base.update(
            terminal_status="partial",
            reason=exc.reason,
            completed_chunks=exc.completed_chunks,
            total_chunks=exc.total_chunks,
        )
        base["attempts"].append(
            _attempt(base, "hierarchical_reader", "partial", exc.reason)
        )
        return base
    except DocumentCoverageLimitError as exc:
        base.update(
            terminal_status="limited_note",
            note_status="fulltext_available",
            reason="document_exceeds_hard_chunk_limit",
            limited_analysis=_limited_analysis(
                {
                    **content,
                    "source_scope": "fulltext_available",
                    "coverage_reason": str(exc),
                },
                item,
            ),
        )
        base["attempts"].append(
            _attempt(base, "hierarchical_reader", "limited", base["reason"])
        )
        return base
    except Exception as exc:
        base["attempts"].append(
            _attempt(
                base, f"{reader.name}_text", "failed", f"{type(exc).__name__}:{exc}"
            )
        )
        base["reason"] = f"reader_failed:{type(exc).__name__}"
        return base
    base["attempts"].append(
        _attempt(
            base,
            reader_route,
            "succeeded",
            reader_reason,
            output_path="pending_atomic_note",
        )
    )
    base["analysis"] = dict(analysis)
    return base


def _write_frozen_content(checkpoint_root: Path, content: Mapping[str, Any]) -> None:
    text = str(content.get("text") or "")
    atomic_write_text(checkpoint_root / "source.txt", text)
    allowed = {
        "content_hash",
        "source_file",
        "content_route",
        "media_type",
        "source_scope",
        "source_coverage",
        "coverage_reason",
        "coverage_metrics",
        "abstract_text",
        "reader_provider",
        "reader_model",
        "analysis",
    }
    payload = {key: content[key] for key in allowed if key in content}
    payload.update(
        {
            "checkpoint_version": "1",
            "text_hash": sha256_text(text),
            "captured_at": now_iso(),
        }
    )
    write_yaml(checkpoint_root / "frozen_content.yml", payload)


def _load_frozen_content(checkpoint_root: Path) -> dict[str, Any] | None:
    manifest_path = checkpoint_root / "frozen_content.yml"
    if not manifest_path.exists():
        return None
    payload = read_yaml(manifest_path, {})
    if not isinstance(payload, Mapping):
        raise ValueError("frozen content manifest must be a mapping")
    if str(payload.get("checkpoint_version") or "") != "1":
        raise ValueError("unsupported frozen content checkpoint version")
    source_path = checkpoint_root / "source.txt"
    if not source_path.exists():
        raise ValueError("frozen source text is missing")
    text = source_path.read_text(encoding="utf-8")
    if str(payload.get("text_hash") or "") != sha256_text(text):
        raise ValueError("frozen source text hash mismatch")
    return {
        key: value
        for key, value in payload.items()
        if key not in {"checkpoint_version", "text_hash", "captured_at"}
    } | {"text": text}


def _limited_analysis(
    content: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, str]:
    data = item_data(item)
    scope = str(content.get("source_scope") or "metadata_only")
    text = str(content.get("abstract_text") or content.get("text") or "").strip()
    available = (
        text
        if text
        else "No abstract or document text was available; this note preserves Zotero citation metadata only."
    )
    reason = str(content.get("coverage_reason") or scope)
    title = str(data.get("title") or item_key(item) or "Untitled Zotero item")
    limitation = (
        f"Coverage classification: {scope}. Reason: {reason}. "
        "Methods, evidence, detailed findings, qualifications, and limitations require the complete document."
    )
    if scope in {"abstract", "abstract_only"}:
        return {"abstract": available, "scope_limitation": limitation}
    if scope == "fulltext_available":
        return {
            "availability": f"A full-document source is recorded at {content.get('source_file') or 'the Zotero attachment'}.",
            "processing_status": limitation,
        }
    return {
        "metadata": f"{title}; {data.get('date') or 'date unavailable'}; DOI: {data.get('DOI') or data.get('doi') or 'unavailable'}.",
        "scope_limitation": limitation,
    }


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
    candidates: list[dict[str, Any]] = []
    try:
        children = client.children(key)
        targets.extend(children)
        if not children:
            base["attempts"].append(
                _attempt(base, "zotero_children", "skipped", "no_child_attachments")
            )
    except Exception as exc:
        base["attempts"].append(
            _attempt(base, "zotero_children", "failed", f"{type(exc).__name__}:{exc}")
        )
    for target in targets:
        target_key = item_key(target)
        if not target_key:
            continue
        data = item_data(target)
        media_type = _target_media_type(data)
        try:
            fulltext = client.fulltext(target_key)
        except Exception as exc:
            base["attempts"].append(
                _attempt(
                    base,
                    "zotero_fulltext",
                    "failed",
                    f"{target_key}:{type(exc).__name__}:{exc}",
                )
            )
            fulltext = None
        text = _fulltext_value(fulltext)
        if text:
            effective_media_type = str(
                (fulltext or {}).get("contentType") or media_type or "text/html"
            )
            if _indexed_page_coverage_incomplete(fulltext) or (
                effective_media_type == "application/pdf"
                and not _indexed_pdf_complete(fulltext)
            ):
                base["attempts"].append(
                    _attempt(
                        base,
                        "zotero_fulltext",
                        "failed",
                        f"{target_key}:partial_or_unproven_indexed_pdf",
                        input_hash=sha256_text(text),
                    )
                )
            else:
                adequacy = classify_content_adequacy(
                    text,
                    media_type=effective_media_type,
                    raw_html=text
                    if effective_media_type in {"text/html", "application/xhtml+xml"}
                    else None,
                    page_count=int((fulltext or {}).get("totalPages", 0) or 0),
                    coverage_metadata=fulltext,
                )
                candidate = _content_candidate(
                    adequacy,
                    text=text,
                    content_hash=sha256_text(text),
                    source_file=f"zotero://select/library/items/{target_key}",
                    content_route="zotero_fulltext",
                    media_type=effective_media_type,
                )
                candidates.append(candidate)
                base["attempts"].append(
                    _attempt(
                        base,
                        "zotero_fulltext",
                        "succeeded" if adequacy.is_full_publication else "limited",
                        f"{target_key}:{adequacy.reason}",
                        input_hash=sha256_text(text),
                    )
                )
        elif fulltext is not None:
            base["attempts"].append(
                _attempt(
                    base,
                    "zotero_fulltext",
                    "skipped",
                    f"{target_key}:indexed_fulltext_empty",
                )
            )
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
                candidates.append(
                    _content_candidate(
                        extracted.adequacy
                        or classify_content_adequacy(
                            extracted.text,
                            media_type=extracted.media_type,
                            page_count=extracted.page_count,
                        ),
                        text=extracted.text,
                        content_hash=sha256_file(local),
                        source_file=str(local),
                        content_route=extracted.route,
                        media_type=extracted.media_type,
                    )
                )
            if extracted.status != "succeeded" and local.suffix.lower() == ".pdf":
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
                    candidates.append(
                        _content_candidate(
                            ocr.adequacy
                            or classify_content_adequacy(
                                ocr.text,
                                media_type="application/pdf",
                                page_count=ocr.page_count,
                            ),
                            text=ocr.text,
                            content_hash=sha256_bytes(document),
                            source_file=str(local),
                            content_route=ocr.route,
                            media_type="application/pdf",
                        )
                    )
                if vision is not None:
                    visual = _vision_content(
                        base,
                        vision,
                        request,
                        document,
                        "application/pdf",
                        item_data(item),
                        str(local),
                    )
                    if visual:
                        candidates.append(
                            {
                                **visual,
                                "source_scope": "full_document",
                                "source_coverage": {
                                    "coverage_gate": "passed",
                                    "reason": "document_vision",
                                },
                                "coverage_reason": "document_vision",
                                "coverage_metrics": {},
                                "rank": 95,
                            }
                        )
        if target is item and str(data.get("itemType", "")) != "attachment":
            continue
        try:
            file_result = client.file(target_key)
        except Exception as exc:
            base["attempts"].append(
                _attempt(
                    base,
                    "zotero_file",
                    "failed",
                    f"{target_key}:{type(exc).__name__}:{exc}",
                )
            )
            file_result = None
        if not file_result:
            base["attempts"].append(
                _attempt(
                    base,
                    "zotero_file",
                    "skipped",
                    f"{target_key}:attachment_file_unavailable",
                )
            )
            continue
        document, media_type = file_result
        extension = (
            mimetypes.guess_extension(media_type)
            or Path(str(data.get("filename") or "")).suffix
            or ".bin"
        )
        custody_path = (
            workspace
            / "01_custody"
            / "files"
            / f"{safe_filename(target_key)}{extension}"
        )
        atomic_write_bytes(custody_path, document)
        extracted = extract_bytes(
            document, media_type=media_type, filename=custody_path.name
        )
        document_hash = sha256_bytes(document)
        base["attempts"].append(
            _attempt(
                base,
                extracted.route,
                "succeeded" if extracted.status == "succeeded" else "failed",
                extracted.reason or "extracted",
                input_hash=document_hash,
                output_path=str(custody_path),
            )
        )
        if extracted.status == "succeeded":
            candidates.append(
                _content_candidate(
                    extracted.adequacy
                    or classify_content_adequacy(
                        extracted.text,
                        media_type=extracted.media_type,
                        page_count=extracted.page_count,
                    ),
                    text=extracted.text,
                    content_hash=document_hash,
                    source_file=str(custody_path),
                    content_route=extracted.route,
                    media_type=extracted.media_type,
                )
            )
        if extracted.status != "succeeded" and media_type == "application/pdf":
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
                candidates.append(
                    _content_candidate(
                        ocr.adequacy
                        or classify_content_adequacy(
                            ocr.text, media_type=media_type, page_count=ocr.page_count
                        ),
                        text=ocr.text,
                        content_hash=document_hash,
                        source_file=str(custody_path),
                        content_route=ocr.route,
                        media_type=media_type,
                    )
                )
            if vision is not None:
                visual = _vision_content(
                    base,
                    vision,
                    request,
                    document,
                    media_type,
                    item_data(item),
                    str(custody_path),
                )
                if visual:
                    candidates.append(
                        {
                            **visual,
                            "source_scope": "full_document",
                            "source_coverage": {
                                "coverage_gate": "passed",
                                "reason": "document_vision",
                            },
                            "coverage_reason": "document_vision",
                            "coverage_metrics": {},
                            "rank": 95,
                        }
                    )
    if candidates:
        return max(candidates, key=lambda row: int(row.get("rank", 0)))
    metadata = item_data(item)
    adequacy = classify_metadata_only(metadata)
    metadata_hash = sha256_text(
        json.dumps(metadata, sort_keys=True, ensure_ascii=False, default=str)
    )
    base["attempts"].append(
        _attempt(
            base,
            "zotero_metadata",
            "limited",
            "metadata_only",
            input_hash=metadata_hash,
        )
    )
    return _content_candidate(
        adequacy,
        text="",
        content_hash=metadata_hash,
        source_file=f"zotero://select/library/items/{key}",
        content_route="zotero_metadata",
        media_type="application/json",
    )


def _content_candidate(
    adequacy: ContentAdequacy,
    *,
    text: str,
    content_hash: str,
    source_file: str,
    content_route: str,
    media_type: str,
) -> dict[str, Any]:
    scope = (
        "abstract_only"
        if adequacy.source_scope == "abstract"
        else adequacy.source_scope
    )
    rank = (
        100
        if adequacy.is_full_publication and media_type == "application/pdf"
        else 80
        if adequacy.is_full_publication
        else 40
        if scope == "abstract_only"
        else 10
    )
    usable_text = (
        adequacy.abstract if scope == "abstract_only" and adequacy.abstract else text
    )
    return {
        "text": usable_text,
        "abstract_text": adequacy.abstract,
        "content_hash": content_hash,
        "source_file": source_file,
        "content_route": content_route,
        "media_type": media_type,
        "source_scope": scope,
        "source_coverage": adequacy.to_dict(),
        "coverage_reason": adequacy.reason,
        "coverage_metrics": dict(adequacy.metrics or {}),
        "rank": rank,
    }


def _target_media_type(data: Mapping[str, Any]) -> str:
    direct = str(data.get("contentType") or "").split(";", 1)[0].strip().casefold()
    if direct:
        return direct
    filename = str(data.get("filename") or data.get("title") or "")
    return mimetypes.guess_type(filename)[0] or (
        "text/html"
        if str(data.get("itemType") or "") != "attachment"
        else "application/octet-stream"
    )


def _indexed_pdf_complete(fulltext: Mapping[str, Any] | None) -> bool:
    if not fulltext:
        return False
    try:
        indexed = int(fulltext.get("indexedPages", 0) or 0)
        total = int(fulltext.get("totalPages", 0) or 0)
    except (TypeError, ValueError):
        return False
    return total > 0 and indexed == total


def _indexed_page_coverage_incomplete(fulltext: Mapping[str, Any] | None) -> bool:
    if (
        not fulltext
        or fulltext.get("indexedPages") is None
        or fulltext.get("totalPages") is None
    ):
        return False
    try:
        indexed = int(fulltext.get("indexedPages", 0) or 0)
        total = int(fulltext.get("totalPages", 0) or 0)
    except (TypeError, ValueError):
        return True
    return total > 0 and indexed < total


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
        base["attempts"].append(
            _attempt(
                base,
                f"{vision.name}_document_vision",
                "disallowed",
                "cloud_not_allowed",
                input_hash=document_hash,
            )
        )
        return None
    try:
        analysis = vision.inspect_document(document, media_type, metadata, None)
    except Exception as exc:
        base["attempts"].append(
            _attempt(
                base,
                f"{vision.name}_document_vision",
                "failed",
                f"{type(exc).__name__}:{exc}",
                input_hash=document_hash,
            )
        )
        return None
    base["attempts"].append(
        _attempt(
            base,
            f"{vision.name}_document_vision",
            "succeeded",
            "document_inspected",
            input_hash=document_hash,
            output_path=source_file,
        )
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


def _frontmatter(
    row: Mapping[str, Any], request: MapRequest, normalized_tags: Sequence[str]
) -> dict[str, Any]:
    data = item_data(row["item"])
    return {
        "note_id": row["note_id"],
        "source_id": row["source_id"],
        "note_status": str(row.get("note_status") or "analytical_atomic_note"),
        "title": str(
            data.get("title") or row["zotero_item_key"] or "Untitled Zotero item"
        ),
        "citation_key": _citation_key(data),
        "zotero_item_key": row["zotero_item_key"],
        "source_file": row["source_file"],
        "creators": data.get("creators", [])
        if isinstance(data.get("creators", []), list)
        else [],
        "date": str(data.get("date") or ""),
        "doi": str(data.get("DOI") or data.get("doi") or ""),
        "url": str(data.get("url") or ""),
        "original_zotero_tags": original_tags(row["item"]),
        "zotero_relations": data.get("relations", {})
        if isinstance(data.get("relations", {}), Mapping)
        else {},
        "normalized_tags": list(normalized_tags),
        "tags": [],
        "clusters": [],
        "cluster_links": [],
        "gaps": [],
        "gap_links": [],
        "aliases": [str(data.get("title") or "")],
        "related_notes": [],
        "inspected_content_hash": row["content_hash"],
        "content_route": row["content_route"],
        "source_scope": str(row.get("source_scope") or "full_document"),
        "source_coverage": dict(
            row.get("source_coverage", {})
            or {"coverage_gate": "passed", "reason": "full_document"}
        ),
        "coverage_reason": str(row.get("coverage_reason") or ""),
        "coverage_metrics": dict(row.get("coverage_metrics", {}) or {}),
        "structural_validation": {"status": "pending"},
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
    text = path.read_text(encoding="utf-8")
    note = read_note(path)
    front = note["frontmatter"]
    body = str(note["body"])
    validation = validate_note(text)
    return {
        "note_id": str(front.get("note_id", row.get("note_id", ""))),
        "source_id": str(front.get("source_id", row.get("source_id", ""))),
        "zotero_item_key": str(
            front.get("zotero_item_key", row.get("zotero_item_key", ""))
        ),
        "note_status": str(front.get("note_status", "")),
        "title": str(front.get("title", "")),
        "date": str(front.get("date", "")),
        "creators": list(front.get("creators", []) or []),
        "doi": str(front.get("doi", "")),
        "url": str(front.get("url", "")),
        "source_scope": str(front.get("source_scope", "")),
        "source_coverage": dict(front.get("source_coverage", {}) or {}),
        "coverage_reason": str(front.get("coverage_reason", "")),
        "thesis": _note_section(body, "Thesis"),
        "method": _note_section(body, "Method and Research Design"),
        "evidence_and_data": _note_section(body, "Evidence and Data"),
        "detailed_findings": _note_section(body, "Detailed Findings"),
        "plain_english_interpretation": _note_section(
            body, "Plain-English Interpretation"
        ),
        "limitations": _note_section(body, "Limitations"),
        "support_boundary": _note_section(body, "What This Source Can Support"),
        "cannot_support": _note_section(body, "What This Source Cannot Support"),
        "locators": _note_section(body, "Locators"),
        "body": body,
        "original_zotero_tags": list(front.get("original_zotero_tags", []) or []),
        "normalized_tags": list(front.get("normalized_tags", []) or []),
        "zotero_relations": dict(front.get("zotero_relations", {}) or {}),
        "note_path": str(path.relative_to(workspace)),
        "note_hash": note["sha256"],
        "projection_hash": note["sha256"],
        "semantic_note_hash": semantic_note_hash(text),
        "validation_passed": validation.passed,
        "validation_errors": list(validation.errors),
        "validation_warnings": list(validation.warnings),
    }


def _review_tags(
    controller: ControllerPort, proposals: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
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
            row.update(
                decision="parked",
                decision_reason="controller_returned_duplicate_decisions",
            )
        if row.get("decision") not in {"accepted", "parked", "rejected"}:
            row.update(
                decision="parked",
                decision_reason="controller_returned_no_valid_decision",
            )
        decisions.append(row)
    return decisions


def _fingerprint(
    key: str,
    content_hash: str,
    request: MapRequest,
    item: Mapping[str, Any],
    reader_provider: str,
    reader_model: str,
    source_scope: str,
) -> str:
    payload = {
        "zotero_item_key": key,
        "content_hash": content_hash,
        "extraction_version": request.extraction_version,
        "prompt_version": request.prompt_version,
        "reader_provider": reader_provider,
        "reader_model": reader_model,
        "source_scope": source_scope,
        "question_lens_policy_version": "collection_invariant-1",
        "metadata_hash": _prompt_metadata_hash(item),
        "chunking_version": CHUNKING_VERSION,
        "content_classifier_version": CONTENT_CLASSIFIER_VERSION,
        "processing_policy_hash": sha256_text(
            json.dumps(request.to_dict().get("processing", {}), sort_keys=True)
        ),
    }
    return sha256_text(json.dumps(payload, sort_keys=True))


def _prompt_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = item_data(item)
    return {
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


def _prompt_metadata_hash(item: Mapping[str, Any]) -> str:
    return sha256_text(
        json.dumps(
            _prompt_metadata(item),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    )


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


def _exhausted_result(
    index: int, item: Mapping[str, Any], route: str, reason: str
) -> dict[str, Any]:
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
    return _exhausted_result(
        index, item, "identity_reconciliation", "duplicate_zotero_item_key"
    )


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
    report = RunReport(
        status="blocked",
        workspace=workspace,
        run_id=run_id,
        errors=[{"reason": reason}],
    )
    run_dir = run_directory(workspace, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_run_report(run_dir, report)
    return report


def _write_run_report(run_dir: Path, report: RunReport) -> None:
    payload = report.to_dict()
    digest = sha256_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    )
    snapshot = run_dir / "reports" / f"run-report-{digest[:16]}.yml"
    if not snapshot.exists():
        write_yaml(snapshot, payload)
    write_yaml(run_dir / "run_report.yml", payload)


def _fulltext_value(value: Mapping[str, Any] | None) -> str:
    if not value:
        return ""
    for key in ("content", "text", "fulltext"):
        if value.get(key):
            return str(value[key]).strip()
    return ""


def _reusable_note(path: Path, row: Mapping[str, Any], request: MapRequest) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_atomic_note(text)
    except OSError:
        return False
    if not validate_note(text).passed:
        return False
    return all(
        (
            str(frontmatter.get("zotero_item_key", ""))
            == str(row.get("zotero_item_key", "")),
            str(frontmatter.get("inspected_content_hash", ""))
            == str(row.get("content_hash", "")),
            str(frontmatter.get("reader_provider", ""))
            == str(row.get("reader_provider", "")),
            str(frontmatter.get("reader_model", ""))
            == str(row.get("reader_model", "")),
            str(frontmatter.get("source_scope", ""))
            == str(row.get("source_scope", "")),
            str(frontmatter.get("extraction_version", ""))
            == request.extraction_version,
            str(frontmatter.get("prompt_version", "")) == request.prompt_version,
        )
    )


def _compatible_committed_note(
    workspace: Path,
    row: Mapping[str, Any],
    request: MapRequest,
) -> Path | None:
    """Reuse a current-schema note when only the processing budget changed.

    Processing limits belong in paid-call checkpoint identities, but changing a
    timeout or chunk budget must not force the same provider and prompt to reread
    source content that already produced a valid committed note. Restrict this
    fallback to a committed note that passes the current validator and still
    matches the source content, scope, provider, model, prompt, extraction
    version, and prompt-visible metadata. This also honors the migration policy
    for readable legacy notes without rereading their source documents.
    """

    fingerprint_root = workspace / "11_state" / "fingerprints"
    if not fingerprint_root.is_dir():
        return None
    zotero_item_key = str(row.get("zotero_item_key") or "")
    source_id = str(row.get("source_id") or "")
    current_metadata_hash = _prompt_metadata_hash(row.get("item", {}))
    candidates: list[Path] = []
    for fingerprint_path in fingerprint_root.glob("*.yml"):
        payload = read_yaml(fingerprint_path, {}) or {}
        if str(payload.get("zotero_item_key") or "") != zotero_item_key:
            continue
        recorded_source_id = str(payload.get("source_id") or "")
        if source_id and recorded_source_id and recorded_source_id != source_id:
            continue
        recorded_metadata_hash = str(payload.get("metadata_hash") or "")
        if recorded_metadata_hash and recorded_metadata_hash != current_metadata_hash:
            continue
        note_path = workspace / str(payload.get("note_path") or "")
        if note_path.is_file() and (
            recorded_metadata_hash
            or _legacy_note_metadata_matches(note_path, row.get("item", {}))
        ):
            candidates.append(note_path)
    for note_path in sorted(set(candidates), key=lambda path: str(path)):
        if _reusable_note(note_path, row, request):
            return note_path
    return None


def _legacy_note_metadata_matches(note_path: Path, item: Mapping[str, Any]) -> bool:
    """Check prompt-visible fields for fingerprints written before metadata hashes."""

    try:
        frontmatter, _ = parse_atomic_note(note_path.read_text(encoding="utf-8"))
    except OSError:
        return False
    metadata = item_data(item)
    comparisons = (
        (frontmatter.get("title", ""), metadata.get("title", "")),
        (frontmatter.get("creators", []), metadata.get("creators", [])),
        (frontmatter.get("date", ""), metadata.get("date", "")),
        (frontmatter.get("doi", ""), metadata.get("DOI") or metadata.get("doi") or ""),
        (frontmatter.get("url", ""), metadata.get("url", "")),
        (frontmatter.get("citation_key", ""), _citation_key(metadata)),
        (frontmatter.get("original_zotero_tags", []), original_tags(item)),
        (frontmatter.get("zotero_relations", {}), metadata.get("relations", {})),
    )
    return all(
        json.dumps(left, sort_keys=True, ensure_ascii=False, default=str)
        == json.dumps(right, sort_keys=True, ensure_ascii=False, default=str)
        for left, right in comparisons
    )


def _local_attachment_path(data: Mapping[str, Any]) -> Path | None:
    for key in ("source_file", "sourceFile", "local_path", "localPath", "path"):
        value = str(data.get(key) or "")
        if (
            not value
            or value.startswith("attachments:")
            or value.startswith("storage:")
        ):
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

    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
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
    *,
    request: MapRequest | None = None,
    checkpoint_root: Path | None = None,
    progress: _RunProgress | None = None,
    inventory_index: int = 0,
) -> tuple[Mapping[str, Any], str, str]:
    policy = request.processing if request is not None else ProcessingPolicy()
    context_tokens = int(getattr(reader, "context_window_tokens", 0) or 0)
    if context_tokens:
        context_chars = int(context_tokens * policy.estimated_chars_per_token)
        direct_limit = int(context_chars * policy.context_window_fraction)
        chunk_limit = max(
            policy.chunk_char_limit,
            int(context_chars * min(policy.context_window_fraction, 0.65)),
        )
    else:
        direct_limit = policy.direct_read_char_limit
        chunk_limit = policy.chunk_char_limit
    document_hash = sha256_text(text)
    checkpoint_enabled = checkpoint_root is not None
    checkpoint_root = checkpoint_root or Path()
    common_identity = {
        "document_hash": document_hash,
        "provider": str(getattr(reader, "name", "unknown")),
        "model": str(getattr(reader, "model", "unknown")),
        "prompt_version": request.prompt_version if request else "2",
        "chunking_version": CHUNKING_VERSION,
        "content_classifier_version": CONTENT_CLASSIFIER_VERSION,
        "question_hash": sha256_text(question or ""),
        "metadata_hash": sha256_text(
            json.dumps(dict(metadata), sort_keys=True, ensure_ascii=False, default=str)
        ),
        "chunk_output_tokens": policy.chunk_output_tokens,
        "synthesis_output_tokens": policy.synthesis_output_tokens,
    }
    if len(text) <= direct_limit:
        direct_path = checkpoint_root / "direct.yml"
        direct_checkpoint = (
            read_yaml(direct_path, {}) or {} if checkpoint_enabled else {}
        )
        direct_identity = {
            **common_identity,
            "mode": "direct",
            "direct_limit": direct_limit,
        }
        if direct_checkpoint.get("identity") == direct_identity and isinstance(
            direct_checkpoint.get("analysis"), Mapping
        ):
            return (
                _ensure_analysis_contract(dict(direct_checkpoint["analysis"])),
                f"{reader.name}_text",
                "reused_direct_source_checkpoint",
            )
        try:
            if progress is not None:
                progress.record_source_provider_call()
            analysis = _ensure_analysis_contract(
                dict(reader.read_source(text, metadata, question))
            )
            if checkpoint_enabled:
                write_yaml(
                    direct_path,
                    {
                        "identity": direct_identity,
                        "analysis": analysis,
                        "updated_at": now_iso(),
                    },
                )
            return analysis, f"{reader.name}_text", "full_document_source_read"
        except Exception as exc:
            message = str(exc).casefold()
            if not any(
                token in message
                for token in (
                    "context",
                    "token",
                    "too long",
                    "too large",
                    "request size",
                    "length",
                )
            ):
                raise
    chunks = _split_document(
        text, chunk_char_limit=chunk_limit, max_chunks=policy.max_total_chunks
    )
    checkpoint_identity = {
        **common_identity,
        "mode": "hierarchical",
        "chunk_char_limit": chunk_limit,
        "total_chunks": len(chunks),
    }
    started = time.monotonic()
    calls = 0
    analyses: list[Mapping[str, Any]] = []
    for index, chunk in enumerate(chunks):
        checkpoint_path = (
            checkpoint_root
            / "chunks"
            / f"{index + 1:04d}-{sha256_text(chunk)[:12]}.yml"
        )
        checkpoint = read_yaml(checkpoint_path, {}) or {} if checkpoint_enabled else {}
        if checkpoint.get("identity") == checkpoint_identity and isinstance(
            checkpoint.get("analysis"), Mapping
        ):
            analysis = dict(checkpoint["analysis"])
        else:
            if calls >= policy.max_calls_per_document_run:
                raise DocumentPartialError(
                    "document_call_budget_reached", len(analyses), len(chunks)
                )
            if time.monotonic() - started >= policy.document_deadline_seconds:
                raise DocumentPartialError(
                    "document_deadline_reached", len(analyses), len(chunks)
                )
            locator = _chunk_locator(chunk, index, len(chunks))
            attempts_before = int(getattr(reader, "transport_attempt_count", 0) or 0)
            if progress is not None:
                progress.record_source_provider_call()
            if hasattr(reader, "summarize_chunk"):
                analysis = reader.summarize_chunk(  # type: ignore[attr-defined]
                    chunk,
                    metadata,
                    question,
                    chunk_id=f"chunk-{index + 1:04d}",
                    locator=locator,
                    max_output_tokens=policy.chunk_output_tokens,
                    deadline_seconds=policy.request_deadline_seconds,
                )
            else:
                analysis = reader.read_source(chunk, metadata, question)
            attempts_after = int(getattr(reader, "transport_attempt_count", 0) or 0)
            calls += max(1, attempts_after - attempts_before)
            if checkpoint_enabled:
                write_yaml(
                    checkpoint_path,
                    {
                        "identity": checkpoint_identity,
                        "chunk_index": index,
                        "chunk_id": f"chunk-{index + 1:04d}",
                        "locator": locator,
                        "analysis": dict(analysis),
                        "updated_at": now_iso(),
                    },
                )
        analyses.append(dict(analysis))
        if progress is not None:
            progress.update(
                inventory_index,
                status="active",
                phase="reading_chunks",
                completed_chunks=len(analyses),
                total_chunks=len(chunks),
            )
    synthesis_path = checkpoint_root / "synthesis.yml"
    synthesis = read_yaml(synthesis_path, {}) or {} if checkpoint_enabled else {}
    if synthesis.get("identity") == checkpoint_identity and isinstance(
        synthesis.get("analysis"), Mapping
    ):
        merged = _ensure_analysis_contract(dict(synthesis["analysis"]))
    elif hasattr(reader, "synthesize_document"):
        if calls >= policy.max_calls_per_document_run:
            raise DocumentPartialError(
                "document_call_budget_reached_before_synthesis",
                len(analyses),
                len(chunks),
            )
        if time.monotonic() - started >= policy.document_deadline_seconds:
            raise DocumentPartialError(
                "document_deadline_reached_before_synthesis", len(analyses), len(chunks)
            )
        if progress is not None:
            progress.record_source_provider_call()
        merged = _ensure_analysis_contract(
            dict(
                reader.synthesize_document(  # type: ignore[attr-defined]
                    analyses,
                    metadata,
                    question,
                    max_output_tokens=policy.synthesis_output_tokens,
                    deadline_seconds=policy.request_deadline_seconds,
                )
            )
        )
        if checkpoint_enabled:
            write_yaml(
                synthesis_path,
                {
                    "identity": checkpoint_identity,
                    "analysis": merged,
                    "updated_at": now_iso(),
                },
            )
    else:
        merged = _ensure_analysis_contract(
            {
                key: "\n\n".join(
                    f"Chunk {index + 1}/{len(analyses)}: {str(analysis.get(key, '')).strip()}"
                    for index, analysis in enumerate(analyses)
                    if str(analysis.get(key, "")).strip()
                )
                for key in SECTION_KEYS
            }
        )
    return (
        merged,
        f"{reader.name}_hierarchical_text",
        f"hierarchical_source_read:{len(chunks)}",
    )


def _ensure_analysis_contract(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Keep legacy ReaderProvider integrations usable after the prompt-v2 note contract."""

    completed = dict(analysis)
    if str(completed.get("plain_english_interpretation") or "").strip():
        return completed
    findings = str(
        completed.get("detailed_findings")
        or "The reader did not report detailed findings."
    ).strip()
    completed["plain_english_interpretation"] = (
        "Direction: See the reported direction in Detailed Findings; this legacy reader did not provide a separate translation.\n"
        "Magnitude: The technical result is retained in Detailed Findings, but no additional intuitive scale was supplied.\n"
        "Reference point: Use only the comparison or baseline stated in Detailed Findings; no new benchmark has been inferred.\n"
        "Uncertainty: Consult the source's reported uncertainty. This compatibility fallback does not add an uncertainty estimate.\n"
        "Practical meaning: A statistically accessible interpretation was not supplied by this legacy reader; remap with a prompt-v2 "
        f"built-in reader for a full explanation. Technical finding retained: {findings}"
    )
    return completed


def _split_document(
    text: str, *, chunk_char_limit: int | None = None, max_chunks: int | None = None
) -> list[str]:
    chunk_char_limit = chunk_char_limit or ProcessingPolicy().chunk_char_limit
    max_chunks = max_chunks or ProcessingPolicy().max_total_chunks
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        pieces = [
            paragraph[index : index + chunk_char_limit]
            for index in range(0, len(paragraph), chunk_char_limit)
        ] or [""]
        for piece in pieces:
            addition = len(piece) + (2 if current else 0)
            if current and current_size + addition > chunk_char_limit:
                chunks.append("\n\n".join(current))
                current = []
                current_size = 0
            current.append(piece)
            current_size += len(piece) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    if len(chunks) > max_chunks:
        raise DocumentCoverageLimitError(len(chunks), max_chunks)
    return [
        f"--- Document Chunk {index + 1}/{len(chunks)} ---\n{chunk}"
        for index, chunk in enumerate(chunks)
    ]


def _chunk_locator(chunk: str, index: int, total: int) -> str:
    import re

    pages = [int(value) for value in re.findall(r"--- Page (\d+) ---", chunk)]
    if pages:
        return f"pages {min(pages)}-{max(pages)}"
    return f"document chunk {index + 1}/{total}"


def _reader_preflight_reason(reader: ReaderProvider, allow_cloud: bool) -> str:
    if bool(getattr(reader, "is_cloud", True)) and not allow_cloud:
        return f"cloud_reader_requires_allow_cloud:{getattr(reader, 'name', 'unknown')}"
    key_environment = getattr(reader, "api_key_env", "")
    if key_environment and not os.getenv(str(key_environment)):
        return f"missing_provider_api_key:{key_environment}"
    if getattr(reader, "name", "") == "gemini" and not os.getenv("GEMINI_API_KEY"):
        return "missing_provider_api_key:GEMINI_API_KEY"
    return ""
