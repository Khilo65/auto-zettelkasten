from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
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
)
from .fidelity import analyze_atomic_fidelity
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
    build_source_catalogue,
    commit_tag_reviews,
    lean_discovery_projection,
    update_source_set_map,
    write_source_set,
)
from .navigation import build_typed_source_relations
from .literature import (
    LITERATURE_FAMILY_PLAN_PROMPT_VERSION,
    _CheckpointedReasonerCalls,
    _preserve_last_valid_clusters_on_refresh_failure,
    _provider_worker_count,
    _reasoner_packet_chars,
    _synthesis_failure_class,
    build_navigation_projection,
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
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMappingPolicy,
    LiteratureMapRequest,
    MapRequest,
    ProcessingPolicy,
    RelationshipPairJob,
    RelationshipProviderBatch,
    RunReport,
    SourceAnalysisBundle,
)
from .notes import (
    internal_note_text,
    item_data,
    item_key,
    note_id_for_item,
    original_tags,
    parse_atomic_note,
    propose_tags,
    read_note,
    semantic_note_hash,
    source_note_semantic_components,
    source_id_for_item,
    update_note_graph,
    update_note_frontmatter,
    update_note_literature,
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
from .relationships import (
    canonical_pair,
    persist_relationship_registry,
    projected_related_links,
    relationship_decision_key,
    RELATIONSHIP_DECISION_CONTRACT,
    RELATIONSHIP_DECISION_NORMALIZATION_VERSION,
    RELATIONSHIP_DISCOVERY_PROMPT_VERSION,
    RELATIONSHIP_PROMPT_VERSION,
    stable_hash,
    validate_relationship_decision_rows,
)
from .readers import (
    ProviderError,
    SECTION_KEYS,
    SOURCE_BUNDLE_ENVELOPE_CONTRACT,
    SOURCE_BUNDLE_MAX_OUTPUT_TOKENS,
    SOURCE_BUNDLE_PROMPT_VERSION,
    SOURCE_CHUNK_MAX_OUTPUT_TOKENS,
    _normalize_source_bundle_payload,
    _normalize_provider_evidence_anchor,
    _parse_source_bundle_response,
    provider_from_name,
)
from .workspace import (
    artifact_rows,
    assert_compatible,
    initialize,
    resolve_workspace,
    run_directory,
    validate_opaque_id,
)
from .zotero import (
    ZoteroLocalClient,
    normalize_collection_snapshot,
    scope_collection_snapshot,
)

CHUNKING_VERSION = "2"
CONTENT_CLASSIFIER_VERSION = "4"
_RELATIONSHIP_BATCH_MAX_JOBS = 30
_LEGACY_RELATIONSHIP_BATCH_MAX_JOBS = 8
_RELATIONSHIP_GENERAL_CANDIDATE_MAX = 70
_RELATIONSHIP_BRIDGE_CANDIDATE_MAX = 50
_RELATIONSHIP_CANDIDATE_MAX = 120
_LITERATURE_MEMORY_LOCK = threading.Lock()
_AUTO_SOURCE_WORKER_LIMIT = 32


def _allocate_complementary_candidate_quotas(
    jobs: Sequence[Mapping[str, Any]],
    *,
    capacity: int,
) -> dict[str, int]:
    """Give every eligible family a floor, then honor planner weights."""

    ordered = sorted(
        (
            str(job.get("bridge_job_id") or ""),
            max(1, int(job.get("target_candidate_count", 0) or 0)),
            (
                len(set(job.get("left_source_ids", []) or []))
                * len(set(job.get("right_source_ids", []) or []))
                if "left_source_ids" in job and "right_source_ids" in job
                else capacity
            ),
        )
        for job in jobs
        if str(job.get("bridge_job_id") or "")
    )
    if not ordered or capacity <= 0:
        return {}
    floor = 3
    if sum(
        min(floor, maximum) for _job_id, _weight, maximum in ordered
    ) > capacity:
        quotas = {job_id: 0 for job_id, _weight, _maximum in ordered}
        for _round in range(floor):
            for job_id, _weight, maximum in ordered:
                if sum(quotas.values()) == capacity:
                    return quotas
                if quotas[job_id] < maximum:
                    quotas[job_id] += 1
        return quotas
    quotas = {
        job_id: min(floor, maximum)
        for job_id, _weight, maximum in ordered
    }
    remaining = capacity - sum(quotas.values())
    weights = {
        job_id: max(1, planned)
        for job_id, planned, _maximum in ordered
    }
    maximums = {
        job_id: maximum for job_id, _weight, maximum in ordered
    }
    while remaining:
        active = {
            job_id: weight
            for job_id, weight in weights.items()
            if quotas[job_id] < maximums[job_id]
        }
        if not active:
            break
        weight_total = sum(active.values())
        shares = {
            job_id: max(1, (remaining * weight) // weight_total)
            for job_id, weight in active.items()
        }
        progressed = 0
        for job_id in sorted(
            active,
            key=lambda value: (
                -((remaining * active[value]) % weight_total),
                value,
            ),
        ):
            addition = min(
                shares[job_id],
                maximums[job_id] - quotas[job_id],
                remaining - progressed,
            )
            quotas[job_id] += addition
            progressed += addition
            if progressed == remaining:
                break
        if not progressed:
            break
        remaining -= progressed
    return quotas


def _balance_complementary_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    measured_sizes: Mapping[str, int],
    packet_count: int | None = None,
) -> list[list[dict[str, Any]]]:
    """Place whole family jobs into two stable, roughly equal packets."""

    if not jobs:
        return []
    packet_count = min(
        len(jobs),
        packet_count if packet_count is not None else 2 if len(jobs) > 6 else 1,
    )
    packets: list[list[dict[str, Any]]] = [[] for _ in range(packet_count)]
    packet_sizes = [0] * packet_count
    packet_quotas = [0] * packet_count
    ordered = sorted(
        (dict(job) for job in jobs),
        key=lambda job: (
            -int(measured_sizes.get(str(job.get("bridge_job_id") or ""), 0)),
            str(job.get("bridge_job_id") or ""),
        ),
    )
    for job in ordered:
        packet_index = min(
            range(packet_count),
            key=lambda index: (
                packet_sizes[index],
                packet_quotas[index],
                index,
            ),
        )
        packets[packet_index].append(job)
        packet_sizes[packet_index] += int(
            measured_sizes.get(str(job.get("bridge_job_id") or ""), 0)
        )
        packet_quotas[packet_index] += int(
            job.get("target_candidate_count", 0) or 0
        )
    return [
        sorted(packet, key=lambda job: str(job.get("bridge_job_id") or ""))
        for packet in packets
        if packet
    ]


def _analytical_profile_source_ids(profiles: Sequence[Any]) -> set[str]:
    source_ids: set[str] = set()
    for profile in profiles:
        row = dict(profile) if isinstance(profile, Mapping) else profile_to_dict(profile)
        context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
        analytical = row.get("analytical")
        if analytical is None:
            analytical = (
                str(row.get("evidence_eligibility") or "substantive_bounded")
                == "substantive_bounded"
                and str(context.get("note_status") or "")
                in {
                    "analytical_atomic_note",
                    "verified_atomic_note",
                    "partial_document_atomic_note",
                }
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


class AtomicFidelityError(RuntimeError):
    pass


class _ProfileProviderBudget:
    """Persist the cumulative source and profile provider-call ceiling."""

    def __init__(self, path: Path, max_calls: int) -> None:
        self.path = path
        self._lock = threading.Lock()
        usage = read_yaml(path, {}) or {}
        persisted_max = int(usage.get("max_calls", 0) or 0)
        self.max_calls = min(max_calls, persisted_max) if persisted_max else max_calls
        self.attempts = [
            dict(row)
            for row in usage.get("attempts", []) or []
            if isinstance(row, Mapping)
        ]
        self.cumulative_calls = len(self.attempts)
        self.new_calls = 0
        self._write()

    def reserve(self, stage: str, key: str, fingerprint: str) -> str:
        with self._lock:
            if self.cumulative_calls >= self.max_calls:
                raise RuntimeError("source_profile_call_budget_reached")
            attempt_number = 1 + sum(
                1
                for row in self.attempts
                if row.get("stage") == stage
                and row.get("key") == key
                and row.get("fingerprint") == fingerprint
            )
            attempt_id = stable_hash(
                {
                    "stage": stage,
                    "key": key,
                    "fingerprint": fingerprint,
                    "attempt": attempt_number,
                }
            )
            self.attempts.append(
                {
                    "attempt_id": attempt_id,
                    "stage": stage,
                    "key": key,
                    "fingerprint": fingerprint,
                    "attempt": attempt_number,
                    "status": "started",
                    "started_at": now_iso(),
                }
            )
            self.cumulative_calls += 1
            self.new_calls += 1
            self._write()
            return attempt_id

    def finish(self, attempt_id: str, *, status: str) -> None:
        with self._lock:
            for row in self.attempts:
                if row.get("attempt_id") == attempt_id:
                    row["status"] = status
                    row["finished_at"] = now_iso()
                    break
            self._write()

    def _write(self) -> None:
        write_yaml(
            self.path,
            {
                "usage_schema_version": "2",
                "max_calls": self.max_calls,
                "provider_call_count": self.cumulative_calls,
                "attempts": self.attempts,
            },
        )


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
            coverage_parked_for_review_count=0,
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
            "parked_for_review_count",
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
            if terminal_status == "exhausted":
                terminal_status = "parked_for_review"
            default_phase = (
                "committed"
                if terminal_status in {"validated_note", "limited_note"}
                else "finished"
                if terminal_status in {"exhausted", "parked_for_review"}
                else "paused"
                if terminal_status == "partial"
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
                "duplicate_alias",
                "parked_for_review",
                "partial",
                "pending",
                "active",
            )
        }
        terminal_count = (
            counts["validated_note"]
            + counts["limited_note"]
            + counts["duplicate_alias"]
            + counts["parked_for_review"]
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
            "duplicate_alias_count": counts["duplicate_alias"],
            "parked_for_review_count": counts["parked_for_review"],
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
    migrate_workspace(workspace)
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
    frozen_collection_snapshot_path = run_dir / "collection_snapshot.yml"
    collection_snapshot: dict[str, Any] = {}
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
            collection_snapshot = dict(
                read_yaml(frozen_collection_snapshot_path, {}) or {}
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
            collection_snapshot = normalize_collection_snapshot(
                [
                    dict(row)
                    for row in client.collections()
                    if isinstance(row, Mapping)
                ],
                [
                    dict(row)
                    for row in (
                        items
                        if inventory_scope == "library" and not request.limit
                        else client.inventory("library")
                    )
                    if isinstance(row, Mapping)
                ],
            )
        except Exception as exc:
            return _blocked_report(
                request, run_id, f"zotero_inventory:{type(exc).__name__}:{exc}"
            )
        if request.limit:
            items = items[: request.limit]
        write_json(inventory_path, items)
        write_json(frozen_inventory_path, items)
        write_yaml(frozen_collection_snapshot_path, collection_snapshot)
        write_yaml(
            workspace
            / "01_custody"
            / "zotero"
            / "collection_snapshot.yml",
            collection_snapshot,
        )
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
    profile_budget = _ProfileProviderBudget(
        run_dir / "literature" / "profiles" / "provider_usage.yml",
        request.literature_policy.max_profile_calls,
    )
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
    pending, duplicate_aliases = _canonical_inventory_plan(workspace, items)
    source_match_index = _source_match_index(workspace)

    def commit_result(row: dict[str, Any]) -> None:
        prepared.append(row)
        public_row, note_row, row_proposals, row_decisions = _finalize_prepared_row(
            workspace,
            request,
            controller,
            row,
            attempt_path,
            source_match_index=source_match_index,
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

    requested_concurrency = request.provider_concurrency
    automatic_workers = (
        min(len(pending) or 1, _AUTO_SOURCE_WORKER_LIMIT)
        if bool(getattr(reader, "is_cloud", False))
        else request.parallel
    )
    workers = max(
        1,
        min(
            len(pending) or 1,
            (
                automatic_workers
                if requested_concurrency == "auto"
                else int(requested_concurrency or request.parallel)
            ),
        ),
    )
    source_stage_started = time.monotonic()
    concurrency_lock = threading.Lock()
    active_source_jobs = 0
    peak_source_concurrency = 0

    def prepare_one(index: int, item: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal active_source_jobs, peak_source_concurrency
        with concurrency_lock:
            active_source_jobs += 1
            peak_source_concurrency = max(
                peak_source_concurrency, active_source_jobs
            )
        try:
            return _prepare_item(
                workspace,
                run_dir,
                index,
                item,
                request,
                client,
                reader,
                vision,
                progress,
                profile_budget,
                source_match_index,
            )
        finally:
            with concurrency_lock:
                active_source_jobs -= 1

    progress.set_stage(
        "source_processing",
        source_worker_count=workers,
        provider_concurrency_mode=str(requested_concurrency or request.parallel),
    )
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="auto-zettelkasten"
    ) as executor:
        future_map = {
            executor.submit(
                prepare_one,
                index,
                item,
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
    canonical_results = {
        int(row.get("inventory_index", -1)): row for row in prepared
    }
    for alias in duplicate_aliases:
        existing = dict(alias.get("existing_canonical") or {})
        canonical = (
            existing
            if existing
            else canonical_results.get(
                int(alias.get("canonical_inventory_index", -1)), {}
            )
        )
        if not canonical or not canonical.get("note_path"):
            commit_result(
                _exhausted_result(
                    int(alias["inventory_index"]),
                    dict(alias["item"]),
                    "identity_reconciliation",
                    "duplicate_canonical_note_unavailable",
                )
            )
            continue
        alias_row = _duplicate_alias_result(alias, canonical)
        prepared.append(alias_row)
        for attempt in alias_row.pop("attempts", []):
            append_jsonl(attempt_path, attempt)
        public_alias = _public_terminal_row(alias_row)
        terminal_rows.append(public_alias)
        canonical_note = workspace / str(public_alias["note_path"])
        if canonical_note.is_file():
            canonical_front = read_note(canonical_note)["frontmatter"]
            canonical_keys = sorted(
                {
                    str(
                        canonical_front.get("canonical_zotero_key")
                        or canonical.get("zotero_item_key")
                        or canonical.get("zotero_key")
                        or ""
                    ),
                    *(
                        str(value)
                        for value in canonical_front.get(
                            "zotero_item_keys", []
                        )
                        or []
                        if str(value)
                    ),
                    str(public_alias["zotero_item_key"]),
                }
                - {""}
            )
            update_note_frontmatter(
                canonical_note,
                {
                    "canonical_zotero_key": str(
                        canonical.get("zotero_item_key")
                        or canonical.get("zotero_key")
                        or canonical_keys[0]
                    ),
                    "zotero_item_keys": canonical_keys,
                },
            )
            note_rows.append(_note_summary_from_path(workspace, public_alias))
        progress.update(
            int(public_alias["inventory_index"]),
            status="duplicate_alias",
            phase="finished",
            reason=str(public_alias["reason"]),
        )
    progress.set_stage(
        "source_terminal_barrier",
        source_peak_concurrency=peak_source_concurrency,
        source_stage_wall_seconds=round(
            time.monotonic() - source_stage_started, 3
        ),
    )
    prepared.sort(key=lambda row: int(row.get("inventory_index", 0)))
    tag_report = commit_tag_reviews(workspace, proposals, decisions)
    _reconcile_literature_memory(workspace)

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
        profile_budget=profile_budget,
        collection_snapshot=collection_snapshot,
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
        "source_peak_concurrency": int(
            progress.literature.get("source_peak_concurrency", 0) or 0
        ),
        "source_stage_wall_seconds": float(
            progress.literature.get("source_stage_wall_seconds", 0.0) or 0.0
        ),
        "relationship_stage_wall_seconds": float(
            progress.literature.get("relationship_stage_wall_seconds", 0.0)
            or 0.0
        ),
        "cluster_peak_concurrency": int(
            progress.literature.get("cluster_peak_concurrency", 0) or 0
        ),
        "cluster_stage_wall_seconds": float(
            progress.literature.get("cluster_stage_wall_seconds", 0.0) or 0.0
        ),
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
    duplicate_alias_count = sum(
        1 for row in terminal_rows if row.get("terminal_status") == "duplicate_alias"
    )
    parked_for_review_count = sum(
        1
        for row in terminal_rows
        if row.get("terminal_status") in {"parked_for_review", "exhausted"}
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
        else (
            "completed"
            if parked_for_review_count == 0
            else "completed_with_parked_items"
        )
    )
    progress.finish(status)
    errors = [
        {
            "zotero_item_key": row.get("zotero_item_key", ""),
            "reason": row.get("reason", ""),
        }
        for row in terminal_rows
        if row.get("terminal_status")
        in {"parked_for_review", "exhausted", "partial"}
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
        duplicate_alias_count=duplicate_alias_count,
        parked_for_review_count=parked_for_review_count,
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
        source_peak_concurrency=int(
            progress.literature.get("source_peak_concurrency", 0) or 0
        ),
        source_stage_wall_seconds=float(
            progress.literature.get("source_stage_wall_seconds", 0.0) or 0.0
        ),
        relationship_stage_wall_seconds=float(
            progress.literature.get("relationship_stage_wall_seconds", 0.0)
            or 0.0
        ),
        cluster_peak_concurrency=int(
            progress.literature.get("cluster_peak_concurrency", 0) or 0
        ),
        cluster_stage_wall_seconds=float(
            progress.literature.get("cluster_stage_wall_seconds", 0.0) or 0.0
        ),
        artifact_manifest=manifest,
    )
    _write_run_report(run_dir, report)
    if status.startswith("completed"):
        scoped_item_keys = [
            str(row.get("key") or "")
            for row in collection_snapshot.get("items", []) or []
            if isinstance(row, Mapping)
            and (
                inventory_scope == "library"
                or str(effective_collection_key or "")
                in {
                    str(value)
                    for value in row.get("collection_keys", []) or []
                }
            )
        ]
        processed_snapshot = scope_collection_snapshot(
            collection_snapshot,
            scope=inventory_scope,
            collection_key=str(effective_collection_key or ""),
            item_keys=scoped_item_keys,
        )
        processed_path = (
            workspace
            / "11_state"
            / "zotero"
            / "last_processed_snapshot.yml"
            if inventory_scope == "library"
            else workspace
            / "11_state"
            / "zotero"
            / "processed_snapshots"
            / f"collection-{slugify(str(effective_collection_key or ''))}.yml"
        )
        write_yaml(
            processed_path,
            {
                **processed_snapshot,
                "processed_run_id": run_id,
                "processed_at": now_iso(),
            },
        )
    return report


def _apply_reader_policy(reader: ReaderProvider, policy: ProcessingPolicy) -> None:
    for attribute, value in (
        ("request_deadline", policy.request_deadline_seconds),
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
    *,
    source_match_index: Mapping[str, Any] | None = None,
) -> tuple[
    dict[str, Any], dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]
]:
    for attempt in row.pop("attempts", []):
        append_jsonl(attempt_path, attempt)
    if row.get("reused") and row.get("note_path"):
        path = workspace / str(row["note_path"])
        data = item_data(row["item"])
        update_note_frontmatter(
            path,
            {
                "title": str(
                    data.get("title")
                    or row.get("zotero_item_key")
                    or "Untitled Zotero item"
                ),
                "citation_key": _citation_key(data),
                "creators": (
                    data.get("creators", [])
                    if isinstance(data.get("creators", []), list)
                    else []
                ),
                "date": str(data.get("date") or ""),
                "doi": str(data.get("DOI") or data.get("doi") or ""),
                "isbn": str(data.get("ISBN") or data.get("isbn") or ""),
                "url": str(data.get("url") or ""),
                "original_zotero_tags": original_tags(row["item"]),
                "zotero_relations": (
                    data.get("relations", {})
                    if isinstance(data.get("relations", {}), Mapping)
                    else {}
                ),
                "item_type": str(data.get("itemType") or ""),
                "aliases": [str(data.get("title") or "")],
                "updated_at": now_iso(),
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            },
        )
        _write_fingerprint(workspace, row, str(row["note_path"]))
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
            terminal_status="parked_for_review",
            reason=f"{route}_failed:{type(exc).__name__}:{exc}",
            note_path="",
        )
        append_jsonl(attempt_path, _attempt(row, route, "failed", row["reason"]))
        decisions = _park_note_decisions(decisions)
        commit_tag_reviews(workspace, proposals, decisions)
        return _public_terminal_row(row), None, proposals, decisions
    if not validation.passed:
        row.update(
            terminal_status="parked_for_review",
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
    if isinstance(row.get("source_analysis_bundle"), Mapping):
        _commit_source_bundle(
            workspace,
            row,
            path,
            request,
            source_match_index=source_match_index,
        )
    if row.get("quality_diagnostics"):
        write_yaml(
            workspace
            / "02_source_memory"
            / "profiles"
            / f"{safe_filename(str(row['note_id']))}.quality.yml",
            {
                "diagnostic_schema_version": "1",
                "note_id": row["note_id"],
                "source_id": row["source_id"],
                "advisory_only": True,
                "warnings": list(row.get("quality_diagnostics", []) or []),
                "updated_at": now_iso(),
            },
        )
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


def _commit_source_bundle(
    workspace: Path,
    row: Mapping[str, Any],
    note_path: Path,
    request: MapRequest,
    *,
    source_match_index: Mapping[str, Any] | None = None,
) -> None:
    bundle = SourceAnalysisBundle.from_dict(
        dict(row["source_analysis_bundle"])
    )
    bundle_path = (
        workspace
        / "02_source_memory"
        / "bundles"
        / f"{safe_filename(str(row['source_id']))}.yml"
    )
    semantic_fingerprint = stable_hash(bundle.semantic_dict())
    dependency_fingerprint = _source_bundle_dependency_fingerprint(row, request)
    bundle_record = {
        "source_analysis_bundle_schema_version": "1",
        "semantic_fingerprint": semantic_fingerprint,
        "dependency_fingerprint": dependency_fingerprint,
        "bundle": bundle.to_dict(),
    }
    write_yaml(bundle_path, bundle_record)
    immutable_bundle_path = (
        workspace
        / "02_source_memory"
        / "bundles"
        / "by_fingerprint"
        / f"{dependency_fingerprint}.yml"
    )
    if not immutable_bundle_path.is_file():
        write_yaml(immutable_bundle_path, bundle_record)
    note_text = internal_note_text(note_path)
    profile = build_evidence_profile(
        note_text,
        source_set_id="",
        provider=request.provider,
        model=request.model,
        policy={
            "profile_generation_route": "source_analysis_bundle",
            "reasoner_identity": "source-analysis-bundle:v1",
        },
    )
    profile.context = {
        **dict(profile.context or {}),
        "source_analysis_bundle_path": str(bundle_path.relative_to(workspace)),
        "source_analysis_bundle_fingerprint": semantic_fingerprint,
        "source_analysis_bundle_dependency_fingerprint": dependency_fingerprint,
        "profile_generation_route": "source_analysis_bundle",
    }
    eligibility = str(
        bundle.scope_assessment.get("evidence_eligibility")
        or row.get("evidence_eligibility")
        or "substantive_bounded"
    )
    profile.evidence_eligibility = eligibility  # type: ignore[assignment]
    profile.excluded_from_synthesis = eligibility != "substantive_bounded"
    if bundle.evidence_anchors:
        profile.evidence_anchors = sorted(
            bundle.evidence_anchors,
            key=lambda anchor: -anchor.salience_priority,
        )[:24]
    compact = dict(bundle.compact_profile)
    for field_name in (
        "research_questions",
        "concepts",
        "theories",
        "mechanisms",
        "methods",
        "cases",
        "datasets",
        "data",
        "geography",
        "periods",
        "populations",
        "outcomes",
        "measures",
        "limitations",
        "boundaries",
        "gaps",
        "future_research",
    ):
        values = compact.get(field_name, [])
        if isinstance(values, list):
            setattr(
                profile,
                field_name,
                list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in values
                        if str(value).strip()
                    )
                ),
            )
    method = str(compact.get("method_or_knowledge_basis") or "").strip()
    if method and method not in profile.methods:
        profile.methods.insert(0, method)
    profile.source_role = str(
        compact.get("source_genre") or profile.source_role or ""
    )
    compact_coverage = compact.get("coverage")
    if isinstance(compact_coverage, Mapping):
        profile.coverage = {**dict(profile.coverage or {}), **compact_coverage}
    elif str(compact_coverage or "").strip():
        profile.coverage = {
            **dict(profile.coverage or {}),
            "description": str(compact_coverage).strip(),
        }
    profile.context = {
        **dict(profile.context or {}),
        "thesis": str(compact.get("thesis") or ""),
        "method_or_knowledge_basis": method,
        "source_genre": str(compact.get("source_genre") or ""),
        "inferential_design": str(compact.get("inferential_design") or ""),
    }
    profile.validity = {
        **dict(profile.validity or {}),
        "source_analysis_bundle": "1",
        "profile_prompt_version": "bundle-v1",
    }
    save_profile(workspace / "02_source_memory" / "profiles", profile)
    with _LITERATURE_MEMORY_LOCK:
        _commit_literature_memory(
            workspace,
            bundle,
            note_path,
            source_index=source_match_index,
        )
        _commit_remediation_ledgers(workspace, row, bundle)


def _source_bundle_dependency_fingerprint(
    row: Mapping[str, Any], request: MapRequest
) -> str:
    return stable_hash(
        {
            "source_fingerprint": str(row.get("fingerprint") or ""),
            "content_hash": str(row.get("content_hash") or ""),
            "provider": request.provider,
            "model": request.model,
            "prompt_version": request.prompt_version,
            "source_bundle_prompt_version": SOURCE_BUNDLE_PROMPT_VERSION,
            "source_bundle_normalization_version": "9",
        }
    )


def _commit_literature_memory(
    workspace: Path,
    bundle: SourceAnalysisBundle,
    note_path: Path,
    *,
    source_index: Mapping[str, Any] | None = None,
) -> None:
    index_root = workspace / "02_source_memory" / "indexes"
    positions_path = index_root / "literature_positions.yml"
    existing = read_yaml(positions_path, {}) or {}
    prior_rows = (
        existing.get("positions", []) if isinstance(existing, Mapping) else []
    )
    by_id = {
        str(row.get("literature_position_id") or ""): dict(row)
        for row in prior_rows
        if isinstance(row, Mapping) and row.get("literature_position_id")
    }
    current_source_id = str(bundle.source_identity.get("source_id") or "")
    by_id = {
        key: value
        for key, value in by_id.items()
        if str(value.get("current_source_id") or "") != current_source_id
    }
    source_index = source_index or _source_match_index(workspace)
    wikilinks: dict[str, str] = {}
    projected_positions = []
    for position in bundle.literature_positions:
        row = position.to_dict()
        match = _match_literature_position_detail(row, source_index)
        matched = str(match.get("source_id") or "")
        if matched == current_source_id:
            matched = ""
            match = {
                **match,
                "status": "not_in_snapshot",
                "basis": "self_citation_ignored",
                "source_id": "",
                "candidates": [],
            }
        row["matched_source_id"] = matched
        row["match_status"] = str(match.get("status") or "not_in_snapshot")
        row["match_basis"] = str(match.get("basis") or "")
        row["match_confidence"] = str(match.get("confidence") or "")
        row["matched_zotero_key"] = str(match.get("zotero_key") or "")
        row["match_candidates"] = list(match.get("candidates") or [])
        by_id[position.literature_position_id] = row
        projected_positions.append(row)
        if matched and matched in source_index["by_source_id"]:
            wikilinks[position.literature_position_id] = str(
                source_index["by_source_id"][matched]["stem"]
            )
    rows = [by_id[key] for key in sorted(by_id)]
    write_yaml(
        positions_path,
        {
            "literature_position_registry_schema_version": "2",
            "positions": rows,
            "revision_hash": stable_hash(rows),
        },
    )
    update_note_literature(note_path, projected_positions, wikilinks)

    missing_path = index_root / "missing_sources.yml"
    missing_existing = read_yaml(missing_path, {}) or {}
    missing_by_id = {
        str(row.get("external_source_id") or ""): dict(row)
        for row in (
            missing_existing.get("sources", [])
            if isinstance(missing_existing, Mapping)
            else []
        )
        if isinstance(row, Mapping) and row.get("external_source_id")
    }
    for external_id, prior in list(missing_by_id.items()):
        discussing = [
            str(value)
            for value in prior.get("discussed_by_source_ids", []) or []
            if str(value) and str(value) != current_source_id
        ]
        if discussing:
            prior["discussed_by_source_ids"] = sorted(set(discussing))
        else:
            del missing_by_id[external_id]
    recommendations = [
        recommendation.to_dict()
        for recommendation in bundle.missing_source_recommendations
    ]
    recommendations.extend(
        {
            "external_source_id": "external-source-"
            + stable_hash(
                {
                    "citation": row.get("raw_citation", ""),
                    "identifiers": row.get("identifiers", {}),
                }
            )[:16],
            "raw_citation": row.get("raw_citation", ""),
            "normalized_citation": {
                key: str(row.get(key) or "")
                for key in ("author", "year", "title")
            },
            "identifiers": dict(row.get("identifiers") or {}),
            "discussed_by_source_ids": [
                current_source_id
            ],
            "importance": str(row.get("engagement") or ""),
            "relevant_collections": [],
            "relevant_topics": [],
            "relevant_clusters": [],
            "acquisition_priority": "normal",
            "match_status": "unresolved",
            "retrieval_status": "not_requested",
            "ambiguity_notes": "",
            "zotero_key": "",
            "source_id": "",
            "note_id": "",
        }
        for row in projected_positions
        if not row.get("matched_source_id")
    )
    for row in recommendations:
        external_id = str(row.get("external_source_id") or "")
        if not external_id:
            continue
        prior = missing_by_id.get(external_id, {})
        discussing = sorted(
            {
                str(value)
                for value in (
                    list(prior.get("discussed_by_source_ids", []) or [])
                    + list(row.get("discussed_by_source_ids", []) or [])
                )
                if str(value)
            }
        )
        missing_by_id[external_id] = {
            **dict(row),
            "discussed_by_source_ids": discussing,
            **{
                field: sorted(
                    {
                        str(value)
                        for value in (
                            list(prior.get(field, []) or [])
                            + list(row.get(field, []) or [])
                        )
                        if str(value)
                    }
                )
                for field in (
                    "relevant_collections",
                    "relevant_topics",
                    "relevant_clusters",
                )
            },
            "acquisition_priority": str(
                prior.get("acquisition_priority")
                or row.get("acquisition_priority")
                or "normal"
            ),
            "retrieval_status": str(
                prior.get("retrieval_status")
                or row.get("retrieval_status")
                or "not_requested"
            ),
        }
    missing_rows = [missing_by_id[key] for key in sorted(missing_by_id)]
    write_yaml(
        missing_path,
        {
            "missing_source_registry_schema_version": "1",
            "sources": missing_rows,
            "revision_hash": stable_hash(missing_rows),
        },
    )


def _reconcile_literature_memory(workspace: Path) -> None:
    """Resolve old citations when new canonical source notes become available."""

    index_root = workspace / "02_source_memory" / "indexes"
    positions_path = index_root / "literature_positions.yml"
    existing = read_yaml(positions_path, {}) or {}
    positions = [
        dict(row)
        for row in existing.get("positions", []) or []
        if isinstance(row, Mapping)
    ]
    if not positions:
        return
    source_index = _source_match_index(workspace)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in positions:
        match = _match_literature_position_detail(row, source_index)
        matched = str(match.get("source_id") or "")
        current_source_id = str(row.get("current_source_id") or "")
        row["matched_source_id"] = (
            matched if matched and matched != current_source_id else ""
        )
        row["match_status"] = (
            str(match.get("status") or "not_in_snapshot")
            if row["matched_source_id"]
            else "not_in_snapshot"
            if matched == current_source_id
            else str(match.get("status") or "not_in_snapshot")
        )
        row["match_basis"] = str(match.get("basis") or "")
        row["match_confidence"] = str(match.get("confidence") or "")
        row["matched_zotero_key"] = str(match.get("zotero_key") or "")
        row["match_candidates"] = list(match.get("candidates") or [])
        if current_source_id:
            by_source[current_source_id].append(row)
    positions.sort(key=lambda row: str(row.get("literature_position_id") or ""))
    projection_errors: list[dict[str, str]] = []
    for source_id, rows in sorted(by_source.items()):
        source = source_index["by_source_id"].get(source_id)
        if not source:
            continue
        wikilinks = {
            str(row.get("literature_position_id") or ""): str(
                source_index["by_source_id"][
                    str(row["matched_source_id"])
                ]["stem"]
            )
            for row in rows
            if row.get("matched_source_id")
            and str(row["matched_source_id"]) in source_index["by_source_id"]
        }
        try:
            update_note_literature(
                workspace / "02_source_memory" / "notes" / f"{source['stem']}.md",
                rows,
                wikilinks,
            )
        except (OSError, ValueError) as exc:
            projection_errors.append(
                {"source_id": source_id, "reason": f"{type(exc).__name__}:{exc}"}
            )
    write_yaml(
        positions_path,
        {
            "literature_position_registry_schema_version": "2",
            "positions": positions,
            "projection_errors": projection_errors,
            "revision_hash": stable_hash(positions),
        },
    )

    missing_path = index_root / "missing_sources.yml"
    missing = read_yaml(missing_path, {}) or {}
    missing_rows = [
        dict(row)
        for row in missing.get("sources", []) or []
        if isinstance(row, Mapping)
    ]
    missing_by_id = {
        str(row.get("external_source_id") or ""): row
        for row in missing_rows
        if row.get("external_source_id")
    }
    for position in positions:
        if position.get("matched_source_id"):
            continue
        external_id = "external-source-" + stable_hash(
            {
                "citation": position.get("raw_citation", ""),
                "identifiers": position.get("identifiers", {}),
            }
        )[:16]
        prior = missing_by_id.get(external_id, {})
        missing_by_id[external_id] = {
            "external_source_id": external_id,
            "raw_citation": str(position.get("raw_citation") or ""),
            "normalized_citation": {
                key: str(position.get(key) or "")
                for key in ("author", "year", "title")
            },
            "identifiers": dict(position.get("identifiers") or {}),
            "discussed_by_source_ids": sorted(
                {
                    *(
                        str(value)
                        for value in prior.get("discussed_by_source_ids", []) or []
                        if str(value)
                    ),
                    str(position.get("current_source_id") or ""),
                }
                - {""}
            ),
            "importance": str(position.get("engagement") or ""),
            "relevant_collections": list(
                prior.get("relevant_collections", []) or []
            ),
            "relevant_topics": list(prior.get("relevant_topics", []) or []),
            "relevant_clusters": list(prior.get("relevant_clusters", []) or []),
            "acquisition_priority": str(
                prior.get("acquisition_priority") or "normal"
            ),
            "match_status": "unresolved",
            "retrieval_status": str(
                prior.get("retrieval_status") or "not_requested"
            ),
            "ambiguity_notes": str(prior.get("ambiguity_notes") or ""),
            "zotero_key": "",
            "source_id": "",
            "note_id": "",
        }
    missing_rows = list(missing_by_id.values())
    for row in missing_rows:
        normalized = (
            dict(row.get("normalized_citation") or {})
            if isinstance(row.get("normalized_citation"), Mapping)
            else {}
        )
        match = _match_literature_position_detail(
            {
                **normalized,
                "identifiers": dict(row.get("identifiers") or {}),
            },
            source_index,
        )
        matched = str(match.get("source_id") or "")
        if not matched:
            row.update(
                match_status=str(match.get("status") or "not_in_snapshot"),
                zotero_key=str(match.get("zotero_key") or ""),
                ambiguity_notes=(
                    ", ".join(str(value) for value in match.get("candidates", []) or [])
                    if match.get("status") == "ambiguous"
                    else str(row.get("ambiguity_notes") or "")
                ),
            )
            continue
        target = source_index["by_source_id"].get(matched, {})
        row.update(
            match_status="matched",
            zotero_key=str(target.get("zotero_key") or ""),
            source_id=matched,
            note_id=str(target.get("note_id") or ""),
        )
    missing_rows.sort(key=lambda row: str(row.get("external_source_id") or ""))
    write_yaml(
        missing_path,
        {
            "missing_source_registry_schema_version": "1",
            "sources": missing_rows,
            "revision_hash": stable_hash(missing_rows),
        },
    )


def _reconcile_cluster_acquisition_recommendations(
    workspace: Path,
    cluster_syntheses: Mapping[str, Any],
) -> Path | None:
    path = workspace / "02_source_memory" / "indexes" / "missing_sources.yml"
    existing = read_yaml(path, {}) or {}
    if not isinstance(existing, Mapping):
        return None
    relevant: dict[str, set[str]] = defaultdict(set)
    for cluster_id, synthesis in cluster_syntheses.items():
        if not isinstance(synthesis, Mapping) or synthesis.get("parked_for_review"):
            continue
        for row in synthesis.get("important_cited_works_not_yet_mapped", []) or []:
            if isinstance(row, Mapping) and row.get("external_source_id"):
                relevant[str(row["external_source_id"])].add(str(cluster_id))
    rows = []
    for raw in existing.get("sources", []) or []:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["relevant_clusters"] = sorted(
            relevant.get(str(row.get("external_source_id") or ""), set())
        )
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("external_source_id") or ""))
    payload = {
        **dict(existing),
        "sources": rows,
        "revision_hash": stable_hash(rows),
    }
    if payload != existing:
        write_yaml(path, payload)
    return path


def _source_match_index(workspace: Path) -> dict[str, Any]:
    by_source_id: dict[str, dict[str, Any]] = {}
    by_zotero_key: dict[str, list[str]] = defaultdict(list)
    by_doi: dict[str, list[str]] = defaultdict(list)
    by_isbn: dict[str, list[str]] = defaultdict(list)
    by_url: dict[str, list[str]] = defaultdict(list)
    by_identity: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for path in sorted((workspace / "02_source_memory" / "notes").glob("*.md")):
        try:
            front = read_note(path)["frontmatter"]
        except (OSError, ValueError):
            continue
        source_id = str(front.get("source_id") or "")
        if not source_id:
            continue
        creators = list(front.get("creators", []) or [])
        author_surnames = _creator_surnames(creators)
        author = author_surnames[0] if author_surnames else ""
        year_match = re.search(r"(?:19|20)\d{2}", str(front.get("date") or ""))
        identity = (
            _normalized_match_text(str(front.get("title") or "")),
            author,
            year_match.group(0) if year_match else "",
        )
        zotero_key = str(front.get("zotero_item_key") or "")
        zotero_keys = {
            zotero_key,
            str(front.get("canonical_zotero_key") or ""),
            *(
                str(value)
                for value in front.get("zotero_item_keys", []) or []
                if str(value)
            ),
        } - {""}
        by_source_id[source_id] = {
            "stem": path.stem,
            "note_path": str(path.relative_to(workspace)),
            "note_id": str(front.get("note_id") or ""),
            "zotero_key": zotero_key,
            "title": identity[0],
            "author": identity[1],
            "year": identity[2],
            "author_surnames": author_surnames,
            "item_type": str(front.get("item_type") or "").casefold(),
        }
        for value in zotero_keys:
            by_zotero_key[value.upper()].append(source_id)
        doi = _normalized_doi_identifier(str(front.get("doi") or ""))
        if doi:
            by_doi[doi].append(source_id)
        isbn = _normalized_strong_identifier(
            str(front.get("isbn") or front.get("ISBN") or "")
        )
        if isbn:
            by_isbn[isbn].append(source_id)
        url = _normalized_url_identifier(str(front.get("url") or ""))
        if url:
            by_url[url].append(source_id)
        by_identity[identity].append(source_id)

    mapped_keys = set(by_zotero_key)
    known_items: list[dict[str, Any]] = []
    snapshot = read_yaml(
        workspace / "01_custody" / "zotero" / "collection_snapshot.yml",
        {},
    ) or {}
    for row in snapshot.get("items", []) or []:
        if not isinstance(row, Mapping):
            continue
        zotero_key = str(row.get("key") or "")
        if not zotero_key or zotero_key.upper() in mapped_keys:
            continue
        identity = (
            dict(row.get("identity") or {})
            if isinstance(row.get("identity"), Mapping)
            else {}
        )
        known_items.append(
            {
                "zotero_key": zotero_key,
                "title": _normalized_match_text(str(identity.get("title") or "")),
                "author_surnames": _creator_surnames(
                    list(identity.get("creators", []) or [])
                ),
                "year": str(identity.get("year") or ""),
                "doi": _normalized_doi_identifier(
                    str(identity.get("doi") or "")
                ),
                "isbn": _normalized_strong_identifier(
                    str(identity.get("isbn") or "")
                ),
                "url": _normalized_url_identifier(str(identity.get("url") or "")),
            }
        )
    return {
        "by_source_id": by_source_id,
        "by_zotero_key": dict(by_zotero_key),
        "by_doi": dict(by_doi),
        "by_isbn": dict(by_isbn),
        "by_url": dict(by_url),
        "by_identity": by_identity,
        "known_zotero_items": known_items,
    }


def _match_literature_position_detail(
    position: Mapping[str, Any],
    source_index: Mapping[str, Any],
) -> dict[str, Any]:
    identifiers = (
        dict(position.get("identifiers") or {})
        if isinstance(position.get("identifiers"), Mapping)
        else {}
    )
    zotero_key = str(
        identifiers.get("zotero_key")
        or identifiers.get("zoteroKey")
        or position.get("zotero_key")
        or ""
    ).upper()
    if zotero_key:
        matches = _unique_index_matches(
            source_index.get("by_zotero_key", {}), zotero_key
        )
        if len(matches) == 1:
            return _mapped_literature_match(
                matches[0], "zotero_key", source_index
            )
        if len(matches) > 1:
            return _ambiguous_literature_match(matches, "zotero_key")
    doi = _normalized_doi_identifier(
        str(identifiers.get("doi") or identifiers.get("DOI") or "")
    )
    if doi:
        matches = _compatible_citation_matches(
            position,
            _unique_index_matches(source_index.get("by_doi", {}), doi),
            source_index,
        )
        if len(matches) == 1:
            return _mapped_literature_match(matches[0], "doi", source_index)
        if len(matches) > 1:
            return _ambiguous_literature_match(matches, "doi")
    for identifier, index_name, normalizer in (
        ("isbn", "by_isbn", _normalized_strong_identifier),
        ("ISBN", "by_isbn", _normalized_strong_identifier),
        ("url", "by_url", _normalized_url_identifier),
        ("URL", "by_url", _normalized_url_identifier),
    ):
        value = normalizer(str(identifiers.get(identifier) or ""))
        if value:
            matches = _compatible_citation_matches(
                position,
                _unique_index_matches(
                    source_index.get(index_name, {}), value
                ),
                source_index,
            )
            if len(matches) == 1:
                return _mapped_literature_match(
                    matches[0], identifier.casefold(), source_index
                )
            if len(matches) > 1:
                return _ambiguous_literature_match(
                    matches, identifier.casefold()
                )
    title = _normalized_match_text(str(position.get("title") or ""))
    author_surnames = _citation_author_surnames(
        str(position.get("author") or "")
    )
    first_author = author_surnames[0] if author_surnames else ""
    year_match = re.search(r"(?:19|20)\d{2}", str(position.get("year") or ""))
    year = year_match.group(0) if year_match else ""
    exact_candidates = [
        source_id
        for source_id, row in dict(source_index.get("by_source_id", {})).items()
        if title
        and str(row.get("title") or "") == title
        and (not year or not row.get("year") or str(row.get("year")) == year)
        and _compatible_first_author(
            first_author, list(row.get("author_surnames", []) or [])
        )
    ]
    matches = sorted(set(exact_candidates))
    if len(matches) == 1:
        return _mapped_literature_match(
            matches[0], "title_year_first_author", source_index
        )
    if len(matches) > 1:
        return _ambiguous_literature_match(
            matches, "title_year_first_author"
        )
    if not title:
        return _known_or_absent_literature_match(
            position,
            source_index,
            title=title,
            first_author=first_author,
            year=year,
            identifiers=identifiers,
        )
    ranked = sorted(
        (
            (
                SequenceMatcher(None, title, str(row.get("title") or "")).ratio(),
                source_id,
            )
            for source_id, row in dict(
                source_index.get("by_source_id", {})
            ).items()
            if (not year or not row.get("year") or row.get("year") == year)
            and _compatible_first_author(
                first_author, list(row.get("author_surnames", []) or [])
            )
        ),
        reverse=True,
    )
    if ranked and ranked[0][0] >= 0.92 and (
        len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.03
    ):
        return _mapped_literature_match(
            str(ranked[0][1]), "unique_fuzzy_title", source_index, confidence="high"
        )
    if (
        len(ranked) > 1
        and ranked[0][0] >= 0.92
        and ranked[0][0] - ranked[1][0] < 0.03
    ):
        return _ambiguous_literature_match(
            [str(row[1]) for row in ranked if row[0] >= 0.92],
            "fuzzy_title",
        )
    return _known_or_absent_literature_match(
        position,
        source_index,
        title=title,
        first_author=first_author,
        year=year,
        identifiers=identifiers,
    )


def _match_literature_position(
    position: Mapping[str, Any],
    source_index: Mapping[str, Any],
) -> str:
    return str(
        _match_literature_position_detail(position, source_index).get(
            "source_id"
        )
        or ""
    )


def _unique_index_matches(index: Any, key: str) -> list[str]:
    value = dict(index or {}).get(key)
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return sorted({str(item) for item in value if str(item)})
    return []


def _compatible_citation_matches(
    position: Mapping[str, Any],
    matches: Sequence[str],
    source_index: Mapping[str, Any],
) -> list[str]:
    title = _normalized_match_text(str(position.get("title") or ""))
    if not title:
        return list(matches)
    sources = dict(source_index.get("by_source_id", {}))
    return [
        source_id
        for source_id in matches
        if _normalized_match_text(
            str(dict(sources.get(source_id, {}) or {}).get("title") or "")
        )
        == title
    ]


def _mapped_literature_match(
    source_id: str,
    basis: str,
    source_index: Mapping[str, Any],
    *,
    confidence: str = "exact",
) -> dict[str, Any]:
    source = dict(source_index.get("by_source_id", {})).get(source_id, {})
    return {
        "status": "mapped",
        "basis": basis,
        "confidence": confidence,
        "source_id": source_id,
        "zotero_key": str(source.get("zotero_key") or ""),
        "candidates": [source_id],
    }


def _ambiguous_literature_match(
    candidates: Sequence[str], basis: str
) -> dict[str, Any]:
    return {
        "status": "ambiguous",
        "basis": basis,
        "confidence": "ambiguous",
        "source_id": "",
        "zotero_key": "",
        "candidates": sorted({str(value) for value in candidates if str(value)}),
    }


def _known_or_absent_literature_match(
    position: Mapping[str, Any],
    source_index: Mapping[str, Any],
    *,
    title: str,
    first_author: str,
    year: str,
    identifiers: Mapping[str, Any],
) -> dict[str, Any]:
    doi = _normalized_doi_identifier(
        str(identifiers.get("doi") or identifiers.get("DOI") or "")
    )
    isbn = _normalized_strong_identifier(
        str(identifiers.get("isbn") or identifiers.get("ISBN") or "")
    )
    url = _normalized_url_identifier(
        str(identifiers.get("url") or identifiers.get("URL") or "")
    )
    known = [
        dict(row)
        for row in source_index.get("known_zotero_items", []) or []
        if isinstance(row, Mapping)
        and (
            (
                (
                    (doi and str(row.get("doi") or "") == doi)
                    or (isbn and str(row.get("isbn") or "") == isbn)
                    or (url and str(row.get("url") or "") == url)
                )
                and (
                    not title
                    or _normalized_match_text(str(row.get("title") or "")) == title
                )
            )
            or (
                title
                and str(row.get("title") or "") == title
                and (not year or not row.get("year") or row.get("year") == year)
                and _compatible_first_author(
                    first_author, list(row.get("author_surnames", []) or [])
                )
            )
        )
    ]
    unique_keys = sorted(
        {str(row.get("zotero_key") or "") for row in known if row.get("zotero_key")}
    )
    if len(unique_keys) == 1:
        return {
            "status": "known_zotero_unmapped",
            "basis": (
                "doi"
                if doi
                else "isbn"
                if isbn
                else "url"
                if url
                else "title_year_first_author"
            ),
            "confidence": "exact",
            "source_id": "",
            "zotero_key": unique_keys[0],
            "candidates": unique_keys,
        }
    if len(unique_keys) > 1:
        return {
            "status": "ambiguous",
            "basis": "zotero_snapshot",
            "confidence": "ambiguous",
            "source_id": "",
            "zotero_key": "",
            "candidates": unique_keys,
        }
    return {
        "status": "not_in_snapshot",
        "basis": "",
        "confidence": "",
        "source_id": "",
        "zotero_key": "",
        "candidates": [],
    }


def _normalized_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _creator_surnames(creators: Sequence[Any]) -> list[str]:
    surnames: list[str] = []
    for creator in creators:
        if isinstance(creator, Mapping):
            value = str(
                creator.get("lastName")
                or creator.get("name")
                or creator.get("firstName")
                or ""
            )
        else:
            value = str(creator)
        surname = _person_surname(value)
        if surname:
            surnames.append(surname)
    return list(dict.fromkeys(surnames))


def _citation_author_surnames(value: str) -> list[str]:
    if not value.strip():
        return []
    parts = re.split(r"\s*(?:;|&|\band\b|\bet al\.?)\s*", value)
    return list(
        dict.fromkeys(
            surname
            for part in parts
            if (surname := _person_surname(part))
        )
    )


def _person_surname(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    candidate = value.split(",", 1)[0] if "," in value else value.split()[-1]
    return _normalized_match_text(candidate)


def _compatible_first_author(
    cited_first_author: str, candidate_surnames: Sequence[str]
) -> bool:
    if not cited_first_author or not candidate_surnames:
        return True
    return cited_first_author == _normalized_match_text(str(candidate_surnames[0]))


def _normalized_strong_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _normalized_doi_identifier(value: str) -> str:
    return re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        value.strip().casefold(),
    ).rstrip("/")


def _normalized_url_identifier(value: str) -> str:
    return value.strip().casefold().rstrip("/")


def _commit_remediation_ledgers(
    workspace: Path,
    row: Mapping[str, Any],
    bundle: SourceAnalysisBundle,
) -> None:
    canonical = item_data(row.get("item", {}))
    observed = dict(bundle.observed_bibliographic_identity)
    differences = {
        field: {
            "current": canonical.get(field, ""),
            "observed": observed.get(field, ""),
        }
        for field in ("title", "creators", "date", "itemType")
        if observed.get(field)
        and _normalized_match_text(str(observed.get(field) or ""))
        != _normalized_match_text(str(canonical.get(field) or ""))
    }
    if differences:
        issue_types = []
        if "itemType" in differences:
            issue_types.append("probable_document_type_mismatch")
            current_type = _normalized_match_text(
                str(differences["itemType"]["current"] or "")
            )
            observed_type = _normalized_match_text(
                str(differences["itemType"]["observed"] or "")
            )
            if current_type == "book" and "report" in observed_type:
                issue_types.append("institutional_report_represented_as_book")
        if "creators" in differences:
            issue_types.append("probable_creator_role_mismatch")
        if "title" in differences:
            issue_types.append("probable_title_mismatch")
        path = (
            workspace
            / "01_custody"
            / "zotero"
            / "zotero_metadata_issues.yml"
        )
        existing = read_yaml(path, {}) or {}
        rows = {
            str(value.get("issue_id") or ""): dict(value)
            for value in (
                existing.get("issues", [])
                if isinstance(existing, Mapping)
                else []
            )
            if isinstance(value, Mapping) and value.get("issue_id")
        }
        issue_id = "zotero-metadata-" + stable_hash(
            [row.get("zotero_item_key", ""), differences]
        )[:16]
        prior = rows.get(issue_id, {})
        rows[issue_id] = {
            "issue_id": issue_id,
            "zotero_item_key": str(row.get("zotero_item_key") or ""),
            "attachment_key": str(
                bundle.source_identity.get("attachment_key") or ""
            ),
            "current_metadata": {
                key: canonical.get(key)
                for key in ("title", "creators", "date", "itemType")
            },
            "recommended_correction": differences,
            "issue_types": issue_types,
            "evidence": observed,
            "confidence": "review_required",
            "ambiguity": "Document-body identity is diagnostic; Zotero remains canonical.",
            "status": str(prior.get("status") or "open"),
            "last_observed_zotero_version": (
                row.get("item", {}).get("version", "")
                if isinstance(row.get("item"), Mapping)
                else ""
            ),
        }
        values = [rows[key] for key in sorted(rows)]
        write_yaml(
            path,
            {
                "zotero_metadata_issue_schema_version": "1",
                "issues": values,
                "revision_hash": stable_hash(values),
            },
        )
    diagnostics = list(bundle.component_diagnostics)
    for field in ("source_scope", "evidence_eligibility", "content_kind"):
        observed_value = bundle.scope_assessment.get(field)
        pipeline_value = row.get(field)
        if (
            observed_value
            and pipeline_value
            and _normalized_match_text(str(observed_value))
            != _normalized_match_text(str(pipeline_value))
        ):
            diagnostics.append(
                {
                    "component": "scope_assessment",
                    "field": field,
                    "pipeline_value": pipeline_value,
                    "model_observation": observed_value,
                    "severity": "advisory",
                }
            )
    if diagnostics:
        path = workspace / "11_state" / "pipeline_classification_issues.yml"
        existing = read_yaml(path, {}) or {}
        rows = {
            str(value.get("issue_id") or ""): dict(value)
            for value in (
                existing.get("issues", [])
                if isinstance(existing, Mapping)
                else []
            )
            if isinstance(value, Mapping) and value.get("issue_id")
        }
        issue_id = "pipeline-classification-" + stable_hash(
            [row.get("source_id", ""), diagnostics]
        )[:16]
        prior = rows.get(issue_id, {})
        rows[issue_id] = {
            "issue_id": issue_id,
            "source_id": str(row.get("source_id") or ""),
            "zotero_item_key": str(row.get("zotero_item_key") or ""),
            "diagnostics": diagnostics,
            "status": str(prior.get("status") or "open"),
        }
        values = [rows[key] for key in sorted(rows)]
        write_yaml(
            path,
            {
                "pipeline_classification_issue_schema_version": "1",
                "issues": values,
                "revision_hash": stable_hash(values),
            },
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
            "source_item_type": item_data(row.get("item", {})).get("itemType", ""),
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


def _workspace_graph_inputs(
    workspace: Path,
    current_profiles: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[Any]]:
    note_rows = all_workspace_note_rows(workspace)
    current_by_source = {
        str(profile_to_dict(profile).get("source_id") or ""): profile
        for profile in current_profiles
    }
    current_by_note = {
        str(profile_to_dict(profile).get("note_id") or ""): profile
        for profile in current_profiles
    }
    profiles: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for row in note_rows:
        source_id = str(row.get("source_id") or "")
        note_id = str(row.get("note_id") or "")
        profile = current_by_source.get(source_id) or current_by_note.get(note_id)
        if profile is None:
            sidecar = profile_sidecar_path(
                workspace / "02_source_memory" / "profiles", note_id
            )
            if sidecar.is_file():
                try:
                    profile = load_profile_sidecar(sidecar)
                except (OSError, ValueError, TypeError):
                    profile = None
        if profile is None or (source_id, note_id) in seen:
            continue
        seen.add((source_id, note_id))
        profiles.append(profile)
    return note_rows, profiles


def _source_set_graph_inputs(
    source_set: Mapping[str, Any],
    note_rows: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[Any]]:
    source_ids = {
        str(value)
        for value in source_set.get("source_ids", []) or []
        if str(value)
    }
    if not source_ids:
        return [dict(row) for row in note_rows], list(profiles)
    return (
        [
            dict(row)
            for row in note_rows
            if str(row.get("source_id") or "") in source_ids
        ],
        [
            profile
            for profile in profiles
            if str(
                (
                    profile
                    if isinstance(profile, Mapping)
                    else profile_to_dict(profile)
                ).get("source_id")
                or ""
            )
            in source_ids
        ],
    )


def _literature_position_relations(
    workspace: Path, profiles: Sequence[Any]
) -> list[dict[str, Any]]:
    note_id_by_source = {
        str(row.get("source_id") or ""): str(row.get("note_id") or "")
        for row in (profile_to_dict(profile) for profile in profiles)
        if row.get("source_id")
    }
    payload = read_yaml(
        workspace / "02_source_memory" / "indexes" / "literature_positions.yml",
        {},
    ) or {}
    relations: list[dict[str, Any]] = []
    for row in payload.get("positions", []) or []:
        if not isinstance(row, Mapping):
            continue
        source_id = str(row.get("current_source_id") or "")
        target_id = str(row.get("matched_source_id") or "")
        if (
            source_id == target_id
            or source_id not in note_id_by_source
            or target_id not in note_id_by_source
        ):
            continue
        evidence = [
            {
                "literature_position_id": str(
                    row.get("literature_position_id") or ""
                ),
                "locator": str(row.get("locator") or ""),
                "engagement": str(row.get("engagement") or ""),
            }
        ]
        for left, right, relation_type in (
            (source_id, target_id, "cites"),
            (target_id, source_id, "cited_by"),
        ):
            relations.append(
                {
                    "relation_id": "typed-relation-"
                    + stable_hash([left, right, relation_type])[:16],
                    "source_id": left,
                    "target_source_id": right,
                    "source_note_id": note_id_by_source[left],
                    "target_note_id": note_id_by_source[right],
                    "relation_type": relation_type,
                    "evidence": evidence,
                    "provenance": "resolved_literature_position",
                    "inferred": False,
                    "strength": 100,
                    "active": True,
                }
            )
    return relations


def _cluster_catalogue_rows(workspace: Path) -> list[dict[str, Any]]:
    payload = read_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml", {}
    ) or {}
    rows = payload.get("clusters", []) if isinstance(payload, Mapping) else []
    result = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("cluster_id"):
            continue
        result.append(
            {
                "cluster_id": str(row.get("cluster_id") or ""),
                "title": str(
                    row.get("display_label")
                    or row.get("label")
                    or row.get("semantic_identity")
                    or ""
                ),
                "shared_question": str(
                    row.get("display_question")
                    or row.get("shared_question")
                    or ""
                ),
                "bounded_scope": str(row.get("bounded_object") or ""),
                "central_debate": str(
                    row.get("central_debate")
                    or row.get("debate_state")
                    or row.get("relationship_among_findings")
                    or ""
                ),
                "source_ids": list(
                    row.get("source_ids") or row.get("core_source_ids") or []
                ),
                "core_source_ids": list(row.get("core_source_ids", []) or []),
                "neighboring_cluster_ids": [
                    str(value)
                    for value in row.get("related_cluster_ids", []) or []
                    if str(value)
                ],
                "refresh_pending": bool(row.get("refresh_pending")),
            }
        )
    return sorted(result, key=lambda row: row["cluster_id"])


def _cluster_membership_relations(
    clusters: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
) -> list[dict[str, Any]]:
    note_id_by_source = {
        str(row.get("source_id") or ""): str(row.get("note_id") or "")
        for row in (profile_to_dict(profile) for profile in profiles)
        if row.get("source_id")
    }
    relations = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        source_ids = (
            cluster.get("source_ids")
            or cluster.get("core_source_ids")
            or []
        )
        for source_id in sorted(str(value) for value in source_ids if str(value)):
            note_id = note_id_by_source.get(source_id, "")
            relations.extend(
                [
                    {
                        "relation_id": (
                            "cluster-member-"
                            + stable_hash([source_id, cluster_id])[:16]
                        ),
                        "source_kind": "source",
                        "source_id": source_id,
                        "source_note_id": note_id,
                        "target_kind": "cluster",
                        "target_cluster_id": cluster_id,
                        "relation_type": "cluster_member",
                        "provenance": "admitted_cluster_registry",
                        "active": True,
                    },
                    {
                        "relation_id": (
                            "cluster-has-member-"
                            + stable_hash([cluster_id, source_id])[:16]
                        ),
                        "source_kind": "cluster",
                        "source_id": cluster_id,
                        "target_kind": "source",
                        "target_source_id": source_id,
                        "target_note_id": note_id,
                        "relation_type": "has_member",
                        "provenance": "admitted_cluster_registry",
                        "active": True,
                    },
                ]
            )
    return relations


def _plan_literature_families(
    workspace: Path,
    *,
    profiles: Sequence[Any],
    catalogue: Mapping[str, Any],
    reasoner: LiteratureReasoner | None,
    reasoner_calls: _CheckpointedReasonerCalls | None,
    request: LiteratureMapRequest,
) -> dict[str, Any] | None:
    planner = getattr(reasoner, "plan_literature_families", None)
    if reasoner_calls is None or not callable(planner):
        return None
    catalogue_path = Path(str(catalogue.get("catalogue_path") or ""))
    catalogue_payload = read_yaml(catalogue_path, {}) or {}
    lean_rows = lean_discovery_projection(profiles, catalogue_payload)
    if len(lean_rows) < 2:
        return None
    plan_path = (
        workspace
        / "02_source_memory"
        / "indexes"
        / "literature_family_plan.yml"
    )
    prior_plan = read_yaml(plan_path, {}) or {}
    lean_source_hashes = {
        str(row["source_id"]): stable_hash(row) for row in lean_rows
    }
    planning_identity = stable_hash(
        {
            "provider": str(getattr(reasoner, "name", "")),
            "model": str(getattr(reasoner, "model", "")),
            "prompt_version": LITERATURE_FAMILY_PLAN_PROMPT_VERSION,
            "policy": request.literature_policy.to_dict(),
            "requested_collection_keys": list(
                request.comparison_collection_keys
            ),
        }
    )
    prior_source_hashes = dict(prior_plan.get("lean_source_hashes", {}) or {})
    incremental_source_ids = sorted(
        {
            source_id
            for source_id, source_hash in lean_source_hashes.items()
            if str(prior_source_hashes.get(source_id) or "") != source_hash
        }
        | (set(prior_source_hashes) - set(lean_source_hashes))
    )
    incremental_mode = bool(
        prior_plan.get("literature_families")
        and prior_source_hashes
        and str(prior_plan.get("planning_identity") or "")
        == planning_identity
        and incremental_source_ids
    )
    source_ids = {str(row["source_id"]) for row in lean_rows}
    collections = [
        {
            "key": str(row.get("key") or ""),
            "name": str(row.get("name") or ""),
            "parent_key": str(row.get("parent_key") or ""),
            "source_ids": sorted(
                str(value)
                for value in row.get("direct_source_ids", []) or []
                if str(value) in source_ids
            ),
        }
        for row in catalogue_payload.get("collections", []) or []
        if isinstance(row, Mapping)
        and row.get("key")
        and any(str(value) in source_ids for value in row.get("direct_source_ids", []) or [])
    ]
    requested_keys = list(request.comparison_collection_keys)
    known_collection_keys = {str(row["key"]) for row in collections}
    unknown_requested = sorted(set(requested_keys) - known_collection_keys)
    if unknown_requested:
        raise ValueError(
            "unknown comparison collection keys: " + ", ".join(unknown_requested)
        )
    context = {
        "planning_mode": (
            "incremental_patch" if incremental_mode else "initial_global"
        ),
        "collections": collections,
        "requested_collection_keys": requested_keys,
    }
    planner_rows = lean_rows
    if incremental_mode:
        changed = set(incremental_source_ids)
        changed_rows = [
            row for row in lean_rows if str(row["source_id"]) in changed
        ]
        changed_collections = {
            str(value)
            for row in changed_rows
            for value in row.get("collection_keys", []) or []
        }
        changed_facets = {
            str(value).casefold()
            for row in changed_rows
            for values in (row.get("facets", {}) or {}).values()
            for value in values
        }
        neighboring_rows = [
            row
            for row in lean_rows
            if str(row["source_id"]) not in changed
            and (
                bool(
                    changed_collections
                    & {
                        str(value)
                        for value in row.get("collection_keys", []) or []
                    }
                )
                or bool(
                    changed_facets
                    & {
                        str(value).casefold()
                        for values in (row.get("facets", {}) or {}).values()
                        for value in values
                    }
                )
            )
        ]
        # ponytail: bounded deterministic retrieval; switch to indexed search
        # only if incremental packets become a measured bottleneck.
        planner_rows = sorted(
            [*changed_rows, *neighboring_rows[:300]],
            key=lambda row: str(row["source_id"]),
        )
        context.update(
            {
                "changed_source_ids": incremental_source_ids,
                "removed_source_ids": sorted(
                    set(prior_source_hashes) - set(lean_source_hashes)
                ),
                "existing_family_cards": [
                    {
                        key: row.get(key)
                        for key in (
                            "family_id",
                            "label",
                            "organizing_problem",
                            "source_ids",
                            "proposed_roles",
                            "candidate_cluster",
                        )
                    }
                    for row in prior_plan.get("literature_families", []) or []
                    if isinstance(row, Mapping)
                ],
            }
        )
    fits = getattr(reasoner, "literature_family_plan_fits", None)
    flat_path = (
        bool(fits(planner_rows, request, context=context))
        if callable(fits)
        else _reasoner_packet_chars(planner_rows, context)
        <= _relationship_context_char_budget(reasoner, request)
    )
    if flat_path:
        plan = reasoner_calls(
            "literature_family_plan",
            str(context["planning_mode"])
            + "-"
            + stable_hash(
                {
                    "index": planner_rows,
                    "planning_identity": planning_identity,
                    "incremental_source_ids": incremental_source_ids,
                }
            )[:16],
            "plan_literature_families",
            planner_rows,
            context,
        )
        planning_path = (
            "incremental_patch"
            if incremental_mode
            else "flat_complete_index"
        )
        packet_source_ids = [
            sorted(str(row["source_id"]) for row in planner_rows)
        ]
    else:
        global_spine = [
            {
                key: row[key]
                for key in ("source_id", "title", "author", "year")
            }
            for row in planner_rows
        ]
        chunk_context = {
            **context,
            "planning_mode": "chunk",
            "global_spine": global_spine,
        }
        chunks = _lean_family_plan_chunks(
            planner_rows,
            request=request,
            context=chunk_context,
            fits=fits,
        )
        local_plans = []
        for index, chunk in enumerate(chunks, start=1):
            local_context = {
                **chunk_context,
                "chunk_id": f"chunk-{index}",
            }
            local_plans.append(
                reasoner_calls(
                    "literature_family_plan",
                    f"chunk-{index}-" + stable_hash(chunk)[:16],
                    "plan_literature_families",
                    chunk,
                    local_context,
                )
            )
        compact_local_plans = [
            {
                "literature_families": list(
                    value.get("literature_families", []) or []
                ),
                "neighboring_families": list(
                    value.get("neighboring_families", []) or []
                ),
            }
            for value in local_plans
        ]
        plan = reasoner_calls(
            "literature_family_plan",
            "reconcile-" + stable_hash(compact_local_plans)[:16],
            "plan_literature_families",
            [],
            {
                **context,
                "planning_mode": "chunk_reconciliation",
                "global_spine": global_spine,
                "local_family_plans": compact_local_plans,
            },
        )
        planning_path = (
            "incremental_chunked_reconciliation"
            if incremental_mode
            else "chunked_index_reconciliation"
        )
        packet_source_ids = [
            [str(row["source_id"]) for row in chunk] for chunk in chunks
        ]
    if incremental_mode:
        affected = set(incremental_source_ids)
        patch_families = [
            dict(row)
            for row in plan.get("literature_families", []) or []
            if isinstance(row, Mapping)
        ]
        patch_family_ids = {
            str(row.get("family_id") or "") for row in patch_families
        }
        retained_families = [
            dict(row)
            for row in prior_plan.get("literature_families", []) or []
            if isinstance(row, Mapping)
            and str(row.get("family_id") or "") not in patch_family_ids
            and affected.isdisjoint(
                {
                    str(value)
                    for value in row.get("source_ids", []) or []
                }
            )
        ]
        patch_jobs = [
            dict(row)
            for row in plan.get("discovery_jobs", []) or []
            if isinstance(row, Mapping)
        ]
        patch_job_ids = {str(row.get("job_id") or "") for row in patch_jobs}
        retained_jobs = [
            dict(row)
            for row in prior_plan.get("discovery_jobs", []) or []
            if isinstance(row, Mapping)
            and str(row.get("job_id") or "") not in patch_job_ids
            and affected.isdisjoint(
                {
                    str(value)
                    for side in ("left_source_ids", "right_source_ids")
                    for value in row.get(side, []) or []
                }
            )
        ]
        merged_families = [*retained_families, *patch_families]
        merged_family_ids = {
            str(row.get("family_id") or "") for row in merged_families
        }
        merged_neighbors: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in [
            *(prior_plan.get("neighboring_families", []) or []),
            *(plan.get("neighboring_families", []) or []),
        ]:
            if not isinstance(raw, Mapping):
                continue
            left = str(raw.get("left_family_id") or "")
            right = str(raw.get("right_family_id") or "")
            if (
                left in merged_family_ids
                and right in merged_family_ids
                and left != right
            ):
                merged_neighbors[tuple(sorted((left, right)))] = dict(raw)
        plan = {
            "literature_families": merged_families,
            "discovery_jobs": [*retained_jobs, *patch_jobs],
            "neighboring_families": list(merged_neighbors.values()),
        }
    validated = _validate_literature_family_plan(
        plan,
        lean_rows=lean_rows,
        requested_collection_keys=requested_keys,
    )
    completion_status = "not_needed_incremental" if incremental_mode else "completed"
    if not incremental_mode:
        represented = {
            str(source_id)
            for family in validated["literature_families"]
            for source_id in family.get("source_ids", []) or []
        }
        completion_context = {
            **context,
            "planning_mode": "coverage_completion",
            "existing_family_cards": validated["literature_families"],
            "covered_source_ids": sorted(represented),
            "unassigned_source_ids": sorted(source_ids - represented),
        }
        completion_rows = (
            planner_rows
            if flat_path
            else [
                row
                for row in planner_rows
                if str(row["source_id"]) not in represented
            ]
        )
        if not flat_path:
            completion_context["global_spine"] = [
                {
                    key: row[key]
                    for key in ("source_id", "title", "author", "year")
                }
                for row in lean_rows
            ]
            completion_context["local_family_cards"] = validated[
                "literature_families"
            ]
        try:
            completion = reasoner_calls(
                "literature_family_plan",
                "coverage-"
                + stable_hash(
                    {
                        "index": lean_source_hashes,
                        "primary": validated,
                    }
                )[:16],
                "plan_literature_families",
                completion_rows,
                completion_context,
            )
            additions = _validate_literature_family_plan(
                completion,
                lean_rows=lean_rows,
                requested_collection_keys=requested_keys,
                allow_empty=True,
                add_requested_jobs=False,
            )
            validated = _merge_literature_family_plans(validated, additions)
        except Exception as exc:
            completion_status = f"advisory_failure:{type(exc).__name__}"
    lean_serialized = json.dumps(
        lean_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result = {
        **validated,
        "planning_path": planning_path,
        "planning_mode": str(context["planning_mode"]),
        "planning_identity": planning_identity,
        "incremental_source_ids": incremental_source_ids,
        "lean_source_hashes": lean_source_hashes,
        "lean_index_hash": stable_hash(lean_rows),
        "lean_index_source_count": len(lean_rows),
        "lean_index_serialized_chars": len(lean_serialized),
        "estimated_input_tokens": max(1, len(lean_serialized) // 4),
        "packet_source_ids": packet_source_ids,
        "requested_collection_keys": requested_keys,
        "coverage_completion_status": completion_status,
    }
    receipt_path = (
        workspace
        / "02_source_memory"
        / "indexes"
        / "lean_discovery_receipt.yml"
    )
    receipt = {
        key: result[key]
        for key in (
            "planning_path",
            "planning_mode",
            "incremental_source_ids",
            "lean_index_hash",
            "lean_index_source_count",
            "lean_index_serialized_chars",
            "estimated_input_tokens",
            "packet_source_ids",
            "requested_collection_keys",
            "coverage_completion_status",
        )
    }
    if (read_yaml(receipt_path, {}) or {}) != receipt:
        write_yaml(receipt_path, receipt)
    persisted_plan = {
        key: result[key]
        for key in (
            "literature_families",
            "discovery_jobs",
            "neighboring_families",
            "planning_path",
            "planning_identity",
            "incremental_source_ids",
            "lean_source_hashes",
            "lean_index_hash",
            "requested_collection_keys",
            "coverage_completion_status",
        )
    }
    if (read_yaml(plan_path, {}) or {}) != persisted_plan:
        write_yaml(plan_path, persisted_plan)
    result["receipt_path"] = str(receipt_path)
    result["plan_path"] = str(plan_path)
    return result


def _merge_literature_family_plans(
    primary: Mapping[str, Any], additions: Mapping[str, Any]
) -> dict[str, Any]:
    families = {
        str(row["family_id"]): dict(row)
        for row in primary.get("literature_families", []) or []
    }
    for raw in additions.get("literature_families", []) or []:
        row = dict(raw)
        family_id = str(row["family_id"])
        if family_id not in families:
            families[family_id] = row
            continue
        current = families[family_id]
        source_ids = sorted(
            set(current.get("source_ids", []) or [])
            | set(row.get("source_ids", []) or [])
        )
        current["source_ids"] = source_ids
        current["proposed_roles"] = {
            source_id: str(
                (row.get("proposed_roles", {}) or {}).get(source_id)
                or (current.get("proposed_roles", {}) or {}).get(source_id)
                or "supporting"
            )
            for source_id in source_ids
        }
        current["candidate_cluster"] = bool(
            current.get("candidate_cluster") or row.get("candidate_cluster")
        )
    jobs = {
        str(row["job_id"]): dict(row)
        for row in primary.get("discovery_jobs", []) or []
    }
    for row in additions.get("discovery_jobs", []) or []:
        jobs.setdefault(str(row["job_id"]), dict(row))
    neighbors: dict[tuple[str, str], dict[str, Any]] = {}
    for row in [
        *(primary.get("neighboring_families", []) or []),
        *(additions.get("neighboring_families", []) or []),
    ]:
        pair = tuple(
            sorted(
                (
                    str(row.get("left_family_id") or ""),
                    str(row.get("right_family_id") or ""),
                )
            )
        )
        if pair[0] in families and pair[1] in families and pair[0] != pair[1]:
            neighbors.setdefault(pair, dict(row))
    return {
        "literature_families": [families[key] for key in sorted(families)],
        "discovery_jobs": [jobs[key] for key in sorted(jobs)],
        "neighboring_families": [neighbors[key] for key in sorted(neighbors)],
    }


def _lean_family_plan_chunks(
    rows: Sequence[Mapping[str, Any]],
    *,
    request: LiteratureMapRequest,
    context: Mapping[str, Any],
    fits: Any,
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        candidate = [*current, row]
        candidate_fits = (
            bool(fits(candidate, request, context=context))
            if callable(fits)
            else _reasoner_packet_chars(candidate, context)
            <= 1_200_000
        )
        if current and not candidate_fits:
            chunks.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        chunks.append(current)
    if any(
        callable(fits) and not fits(chunk, request, context=context)
        for chunk in chunks
    ):
        raise ValueError("one complete lean index record exceeds the planning context")
    return chunks


def _validate_literature_family_plan(
    response: Mapping[str, Any],
    *,
    lean_rows: Sequence[Mapping[str, Any]],
    requested_collection_keys: Sequence[str],
    allow_empty: bool = False,
    add_requested_jobs: bool = True,
) -> dict[str, Any]:
    available = {str(row.get("source_id") or "") for row in lean_rows}
    memberships = {
        str(row.get("source_id") or ""): {
            str(value) for value in row.get("collection_keys", []) or []
        }
        for row in lean_rows
    }
    raw_families = response.get("literature_families")
    raw_jobs = response.get("discovery_jobs")
    raw_neighbors = response.get("neighboring_families", [])
    if not isinstance(raw_families, list) or not isinstance(raw_jobs, list):
        raise ValueError("literature family plan requires family and discovery lists")
    if not isinstance(raw_neighbors, list):
        raise ValueError("literature family plan neighboring_families must be a list")
    families = []
    family_ids: set[str] = set()
    for raw in raw_families:
        if not isinstance(raw, Mapping):
            continue
        family_id = str(raw.get("family_id") or "").strip()
        source_ids = sorted(
            {
                str(value)
                for value in raw.get("source_ids", []) or []
                if str(value) in available
            }
        )
        if not family_id or family_id in family_ids or len(source_ids) < 2:
            continue
        family_ids.add(family_id)
        roles = (
            dict(raw.get("proposed_roles") or {})
            if isinstance(raw.get("proposed_roles"), Mapping)
            else {}
        )
        families.append(
            {
                "family_id": family_id,
                "label": str(raw.get("label") or family_id).strip(),
                "organizing_problem": str(
                    raw.get("organizing_problem") or ""
                ).strip(),
                "source_ids": source_ids,
                "proposed_roles": {
                    source_id: str(roles.get(source_id) or "supporting")
                    for source_id in source_ids
                },
                "candidate_cluster": bool(raw.get("candidate_cluster", True)),
            }
        )
    if not families and not allow_empty:
        raise ValueError("literature family plan contained no valid families")
    requested_pairs = {
        tuple(sorted((left_key, right_key)))
        for index, left_key in enumerate(requested_collection_keys)
        for right_key in requested_collection_keys[index + 1 :]
    }
    jobs = []
    seen_jobs: set[str] = set()
    for raw in raw_jobs:
        if not isinstance(raw, Mapping):
            continue
        job_id = str(raw.get("job_id") or "").strip()
        left = sorted(
            {str(value) for value in raw.get("left_source_ids", []) or [] if str(value) in available}
        )
        right = sorted(
            {str(value) for value in raw.get("right_source_ids", []) or [] if str(value) in available}
        )
        if not job_id or job_id in seen_jobs or not left or not right:
            continue
        seen_jobs.add(job_id)
        requested_pair = tuple(sorted(
            {
                str(value)
                for value in raw.get("requested_collection_pair", []) or []
                if str(value)
            }
        ))
        if requested_pair not in requested_pairs:
            requested_pair = ()
        elif not (
            all(requested_pair[0] in memberships[source_id] for source_id in left)
            and all(requested_pair[1] in memberships[source_id] for source_id in right)
            or all(requested_pair[1] in memberships[source_id] for source_id in left)
            and all(requested_pair[0] in memberships[source_id] for source_id in right)
        ):
            requested_pair = ()
        try:
            quota = max(1, min(120, int(raw.get("candidate_quota", 24) or 24)))
        except (TypeError, ValueError):
            quota = 24
        jobs.append(
            {
                "job_id": job_id,
                "family": str(raw.get("family") or ""),
                "left_source_ids": left,
                "right_source_ids": right,
                "requested_collection_pair": list(requested_pair),
                "discovery_goal": str(raw.get("discovery_goal") or "").strip(),
                "candidate_quota": quota,
            }
        )
    collection_sources = {
        key: {
            source_id
            for source_id, keys in memberships.items()
            if key in keys
        }
        for key in requested_collection_keys
    }
    covered_pairs: set[tuple[str, str]] = set()
    for row in jobs:
        pair = tuple(row["requested_collection_pair"])
        if len(pair) != 2:
            continue
        left = set(row["left_source_ids"])
        right = set(row["right_source_ids"])
        first = collection_sources[pair[0]] - collection_sources[pair[1]]
        second = collection_sources[pair[1]] - collection_sources[pair[0]]
        if (left == first and right == second) or (
            left == second and right == first
        ):
            covered_pairs.add(pair)
    for pair in sorted(requested_pairs - covered_pairs) if add_requested_jobs else []:
        left = sorted(
            source_id for source_id, keys in memberships.items() if pair[0] in keys
        )
        right = sorted(
            source_id for source_id, keys in memberships.items() if pair[1] in keys
        )
        overlap = set(left) & set(right)
        left = [value for value in left if value not in overlap]
        right = [value for value in right if value not in overlap]
        if not left or not right:
            raise ValueError(
                "requested collection comparison has no distinct mapped endpoints: "
                + " / ".join(pair)
            )
        jobs.append(
            {
                "job_id": "requested-comparison-" + stable_hash(pair)[:12],
                "family": "explicit_requested_collection_comparison",
                "left_source_ids": left,
                "right_source_ids": right,
                "requested_collection_pair": list(pair),
                "discovery_goal": "Compare the explicitly requested collections across distinct literature families.",
                "candidate_quota": 40,
            }
        )
    if not jobs and not allow_empty:
        raise ValueError("literature family plan contained no valid discovery jobs")
    neighbors = [
        {
            "left_family_id": str(row.get("left_family_id") or ""),
            "right_family_id": str(row.get("right_family_id") or ""),
            "reason": str(row.get("reason") or "").strip(),
        }
        for row in raw_neighbors
        if isinstance(row, Mapping)
        and str(row.get("left_family_id") or "") in family_ids
        and str(row.get("right_family_id") or "") in family_ids
        and str(row.get("left_family_id") or "")
        != str(row.get("right_family_id") or "")
    ]
    return {
        "literature_families": families,
        "discovery_jobs": jobs,
        "neighboring_families": neighbors,
    }


def _run_relationship_reasoning(
    workspace: Path,
    *,
    profiles: Sequence[Any],
    source_set: Mapping[str, Any],
    catalogue: Mapping[str, Any],
    reasoner: LiteratureReasoner | None,
    reasoner_calls: _CheckpointedReasonerCalls | None,
    request: LiteratureMapRequest,
    shared_family_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run global discovery and one complete decision per immutable pair job."""

    selector = getattr(reasoner, "select_relationship_candidates", None)
    adjudicator = getattr(reasoner, "adjudicate_relationships", None)
    if (
        reasoner_calls is None
        or not callable(selector)
        or not callable(adjudicator)
    ):
        return {
            "accepted": [],
            "no_relationship": [],
            "parked": [],
            "cluster_candidates": [],
            "selected_profile_hashes": {},
            "reconciled_catalogue_revision": "",
        }
    decision_contract = str(
        getattr(reasoner, "relationship_decision_contract", "")
        or "relationship-decision-v4"
    )
    batch_max_jobs = (
        _RELATIONSHIP_BATCH_MAX_JOBS
        if decision_contract == RELATIONSHIP_DECISION_CONTRACT
        else _LEGACY_RELATIONSHIP_BATCH_MAX_JOBS
    )
    profile_by_source = {
        str(row.get("source_id") or ""): profile
        for profile in profiles
        for row in [profile_to_dict(profile)]
        if row.get("source_id")
        and str(row.get("evidence_eligibility") or "substantive_bounded")
        == "substantive_bounded"
    }
    if len(profile_by_source) < 2:
        return {
            "accepted": [],
            "no_relationship": [],
            "parked": [],
            "cluster_candidates": [],
            "selected_profile_hashes": {},
            "reconciled_catalogue_revision": "",
        }
    note_row_by_source = {
        str(row.get("source_id") or ""): row
        for row in all_workspace_note_rows(workspace)
        if row.get("source_id") and row.get("note_path")
    }
    atomic_note_by_source: dict[str, dict[str, str]] = {}
    for source_id, profile in profile_by_source.items():
        profile_row = profile_to_dict(profile)
        context = (
            profile_row.get("context")
            if isinstance(profile_row.get("context"), Mapping)
            else {}
        )
        raw_path = str(
            note_row_by_source.get(source_id, {}).get("note_path")
            or context.get("note_path")
            or ""
        )
        note_path = Path(raw_path)
        if raw_path and not note_path.is_absolute():
            note_path = workspace / note_path
        if not raw_path or not note_path.is_file():
            continue
        internal_text = internal_note_text(note_path)
        _frontmatter, semantic_body = source_note_semantic_components(internal_text)
        atomic_note_by_source[source_id] = {
            "source_id": source_id,
            "semantic_hash": semantic_note_hash(internal_text),
            "markdown": semantic_body.strip(),
        }
    catalogue_payload = read_yaml(Path(str(catalogue["catalogue_path"])), {}) or {}
    available_catalogue_ids = {
        str(row.get("source_id") or "")
        for row in catalogue_payload.get("sources", []) or []
        if isinstance(row, Mapping)
        and str(row.get("zotero_availability") or "available") != "unavailable"
    }
    profile_by_source = {
        source_id: profile
        for source_id, profile in profile_by_source.items()
        if source_id in available_catalogue_ids
    }
    if len(profile_by_source) < 2:
        return {
            "accepted": [],
            "no_relationship": [],
            "parked": [],
            "cluster_candidates": [],
            "selected_profile_hashes": {},
            "reconciled_catalogue_revision": "",
        }
    entries = [
        _compact_relationship_catalogue_entry(row)
        for row in catalogue_payload.get("sources", []) or []
        if isinstance(row, Mapping)
        and str(row.get("source_id") or "") in profile_by_source
    ]
    entry_by_source = {
        str(row["source_id"]): row for row in entries if row.get("source_id")
    }
    collection_structure = {
        "literatures": [
            {
                "literature_id": str(row.get("literature_id") or ""),
                "title": str(row.get("title") or ""),
                "scope": str(row.get("scope") or ""),
                "source_count": int(row.get("source_count", 0) or 0),
            }
            for row in catalogue_payload.get("literatures", []) or []
            if isinstance(row, Mapping)
        ],
        "collections": [
            {
                "key": str(row.get("key") or ""),
                "parent_key": str(row.get("parent_key") or ""),
                "direct_source_ids": sorted(
                    str(value)
                    for value in row.get("direct_source_ids", []) or []
                    if str(value) in profile_by_source
                ),
                "routing_card": {
                    str(key): value
                    for key, value in dict(
                        row.get("routing_card") or {}
                    ).items()
                    if str(key)
                    not in {
                        "active_cluster_ids",
                        "cross_collection_relationship_count",
                        "relationship_count",
                        "relationship_view_revisions",
                        "revision_hash",
                    }
                },
            }
            for row in catalogue_payload.get("collections", []) or []
            if isinstance(row, Mapping)
        ],
        "shards": [
            {
                "shard_id": str(row.get("shard_id") or ""),
                "literature_id": str(row.get("literature_id") or ""),
                "source_ids": sorted(
                    str(value)
                    for value in row.get("source_ids", []) or []
                    if str(value) in profile_by_source
                ),
                "routing_card": dict(row.get("routing_card") or {}),
            }
            for row in catalogue_payload.get("shards", []) or []
            if isinstance(row, Mapping)
        ],
        "virtual_shards": [
            {
                "shard_id": str(row.get("shard_id") or ""),
                "topic_id": str(row.get("topic_id") or ""),
                "source_ids": sorted(
                    str(value)
                    for value in row.get("source_ids", []) or []
                    if str(value) in profile_by_source
                ),
                "routing_card": dict(row.get("routing_card") or {}),
            }
            for row in catalogue_payload.get("virtual_shards", []) or []
            if isinstance(row, Mapping)
        ],
    }
    shared_plan_active = bool(
        shared_family_plan
        and shared_family_plan.get("discovery_jobs")
        and shared_family_plan.get("literature_families")
    )
    catalogue_revision = stable_hash(
        {
            "lean_index_hash": str(
                shared_family_plan.get("lean_index_hash") or ""
            ),
            "literature_families": list(
                shared_family_plan.get("literature_families", []) or []
            ),
            "discovery_jobs": list(
                shared_family_plan.get("discovery_jobs", []) or []
            ),
            "requested_collection_keys": list(
                shared_family_plan.get("requested_collection_keys", []) or []
            ),
        }
        if shared_plan_active
        else {"entries": entries, "collection_structure": collection_structure}
    )
    current_hashes = {
        source_id: stable_hash(profile_to_dict(profile))
        for source_id, profile in sorted(profile_by_source.items())
    }
    registry = read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    positions = read_yaml(
        workspace / "02_source_memory" / "indexes" / "literature_positions.yml",
        {},
    ) or {}
    memory_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in registry.get("relations", []) or registry.get("links", []) or []:
        if not isinstance(row, Mapping) or str(row.get("relation_type") or "") not in {
            "cites",
            "cited_by",
            "zotero_related",
        }:
            continue
        pair = canonical_pair(
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or ""),
        )
        if pair[0] not in profile_by_source or pair[1] not in profile_by_source:
            continue
        memory = {
            "kind": "explicit_relation",
            "pair": list(pair),
            "relation_type": str(row.get("relation_type") or ""),
            "reason": str(row.get("reason") or ""),
            "provenance": str(row.get("provenance") or ""),
        }
        for source_id in pair:
            memory_rows[source_id].append(memory)
    for row in positions.get("positions", []) or []:
        if not isinstance(row, Mapping) or not row.get("matched_source_id"):
            continue
        pair = canonical_pair(
            str(row.get("current_source_id") or ""),
            str(row.get("matched_source_id") or ""),
        )
        if pair[0] not in profile_by_source or pair[1] not in profile_by_source:
            continue
        memory = {
            "kind": "literature_position",
            **{
                key: value
                for key, value in row.items()
                if key not in {"match_status", "updated_at"}
            },
        }
        for source_id in pair:
            memory_rows[source_id].append(memory)
    current_memory_hashes = {
        source_id: stable_hash(
            sorted(memory_rows.get(source_id, []), key=stable_hash)
        )
        for source_id in sorted(profile_by_source)
    }
    relationship_policy_identity = stable_hash(
        request.literature_policy.to_dict()
    )
    selection_identity = stable_hash(
        {
            "provider": str(getattr(reasoner, "name", "")),
            "model": str(getattr(reasoner, "model", "")),
            "discovery_prompt_version": RELATIONSHIP_DISCOVERY_PROMPT_VERSION,
            "adjudication_prompt_version": RELATIONSHIP_PROMPT_VERSION,
            "output_contract": decision_contract,
            "decision_normalization_version": RELATIONSHIP_DECISION_NORMALIZATION_VERSION,
            "policy_identity": relationship_policy_identity,
        }
    )
    state_path = (
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml"
    )
    prior_state = read_yaml(state_path, {}) or {}
    prior_hashes = dict(prior_state.get("profile_hashes", {}) or {})
    prior_memory_hashes = dict(
        prior_state.get("relationship_memory_hashes", {}) or {}
    )
    identity_changed = (
        str(prior_state.get("selection_identity") or "") != selection_identity
    )
    focus_source_ids = sorted(
        source_id
        for source_id, profile_hash in current_hashes.items()
        if identity_changed
        or str(prior_hashes.get(source_id) or "") != profile_hash
        or str(prior_memory_hashes.get(source_id) or "")
        != current_memory_hashes[source_id]
    )
    catalogue_changed = (
        str(
            prior_state.get("reconciled_catalogue_revision")
            or prior_state.get("catalogue_revision")
            or ""
        )
        != catalogue_revision
    )
    if (
        not focus_source_ids
        and not catalogue_changed
        and not bool(getattr(request, "retry_terminal_failures", False))
    ):
        prior_stage_complete = bool(
            prior_state.get("relationship_stage_complete", True)
        )
        return {
            "accepted": [],
            "no_relationship": [],
            "parked": [],
            "cluster_candidates": [],
            "selected_profile_hashes": {},
            "reconciled_catalogue_revision": "",
            "selection_identity": selection_identity,
            "state_path": str(state_path),
            "pair_job_count": 0,
            "accounted_pair_job_count": 0,
            "relationship_stage_complete": prior_stage_complete,
            "relationship_retry_on_resume": bool(
                prior_state.get("relationship_retry_on_resume", False)
            ),
            "relationship_discovery_status": str(
                prior_state.get("relationship_discovery_status") or "complete"
            ),
            "relationship_discovery_incomplete_jobs": list(
                prior_state.get("relationship_discovery_incomplete_jobs", [])
                or []
            ),
            "provider_batch_count": 0,
        }
    mandatory_basis: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in registry.get("relations", []) or registry.get("links", []) or []:
        if not isinstance(row, Mapping) or str(row.get("relation_type") or "") not in {
            "cites",
            "cited_by",
            "zotero_related",
        }:
            continue
        pair = canonical_pair(
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or ""),
        )
        if pair[0] in profile_by_source and pair[1] in profile_by_source:
            mandatory_basis[pair].append(
                {
                    "discovery_route": str(row.get("relation_type") or ""),
                    "reason": str(row.get("reason") or "Explicit source relation."),
                    "mandatory": True,
                }
            )
    for row in positions.get("positions", []) or []:
        if not isinstance(row, Mapping) or not row.get("matched_source_id"):
            continue
        pair = canonical_pair(
            str(row.get("current_source_id") or ""),
            str(row.get("matched_source_id") or ""),
        )
        if pair[0] in profile_by_source and pair[1] in profile_by_source:
            mandatory_basis[pair].append(
                {
                    "discovery_route": "matched_literature_position",
                    "reason": str(row.get("engagement") or "Matched citation."),
                    "mandatory": True,
                    "literature_position_id": str(
                        row.get("literature_position_id") or ""
                    ),
                }
            )
    prior_decisions = [
        dict(row)
        for row in registry.get("pair_decisions", []) or []
        if isinstance(row, Mapping)
    ]
    relationship_provider = str(getattr(reasoner, "name", ""))
    relationship_model = str(getattr(reasoner, "model", ""))
    active_relation_ids = {
        str(row.get("relation_id") or "")
        for row in registry.get("relations", []) or []
        if isinstance(row, Mapping)
        and bool(row.get("active", True))
        and row.get("relation_id")
    }
    visible_pairs: set[tuple[str, str]] = set()
    inactive_accepted_pairs: set[tuple[str, str]] = set()
    for row in registry.get("current_pair_decisions", []) or []:
        if not isinstance(row, Mapping) or str(row.get("status") or "") not in {
            "accepted",
            "reconciliation_pending",
        }:
            continue
        pair_values = list(row.get("source_ids", []) or [])
        if len(pair_values) != 2:
            continue
        pair = canonical_pair(str(pair_values[0]), str(pair_values[1]))
        if active_relation_ids & {
            str(value) for value in row.get("relation_ids", []) or []
        }:
            visible_pairs.add(pair)
        else:
            inactive_accepted_pairs.add(pair)
    negative_pairs: set[tuple[str, str]] = set()
    for row in prior_decisions:
        if (
            str(
                row.get("status")
                or row.get("decision_status")
                or row.get("decision")
                or ""
            )
            != "no_relationship"
        ):
            continue
        pair = canonical_pair(
            str(row.get("source_id") or row.get("left_source_id") or ""),
            str(
                row.get("target_source_id")
                or row.get("right_source_id")
                or ""
            ),
        )
        if pair[0] not in current_hashes or pair[1] not in current_hashes:
            continue
        if prior_state and any(
            str(prior_memory_hashes.get(source_id) or "")
            != current_memory_hashes[source_id]
            for source_id in pair
        ):
            continue
        expected_key = relationship_decision_key(
            pair[0],
            pair[1],
            current_hashes[pair[0]],
            current_hashes[pair[1]],
            provider=relationship_provider,
            model=relationship_model,
            prompt_version=RELATIONSHIP_PROMPT_VERSION,
            policy_identity=relationship_policy_identity,
        )
        matches_current = str(row.get("decision_key") or "") == expected_key
        matches_legacy_default = bool(
            not row.get("relationship_policy_identity")
            and relationship_policy_identity
            == stable_hash(LiteratureMappingPolicy().to_dict())
            and str(row.get("decision_key") or "")
            == relationship_decision_key(
                pair[0],
                pair[1],
                current_hashes[pair[0]],
                current_hashes[pair[1]],
                provider=relationship_provider,
                model=relationship_model,
                prompt_version=RELATIONSHIP_PROMPT_VERSION,
            )
        )
        if matches_current or matches_legacy_default:
            negative_pairs.add(pair)
    for pair in negative_pairs:
        mandatory_basis.pop(pair, None)
    if shared_plan_active:
        # Structural citations remain visible without consuming full-note
        # adjudication capacity. The shared plan chooses which pairs merit it.
        mandatory_basis.clear()

    configured_max_calls = getattr(reasoner_calls, "max_calls", None)
    remaining_calls = (
        22
        if configured_max_calls is None
        else max(
            0,
            int(configured_max_calls or 0)
            - int(
                getattr(reasoner_calls, "cumulative_provider_calls", 0) or 0
            ),
        )
    )
    mandatory_call_count = (
        len(mandatory_basis) + batch_max_jobs - 1
    ) // batch_max_jobs
    if mandatory_call_count > min(20, remaining_calls):
        return {
            "accepted": [],
            "no_relationship": [],
            "parked": [
                {
                    "source_id": pair[0],
                    "target_source_id": pair[1],
                    "reason": "mandatory_relationship_budget_conflict",
                }
                for pair in sorted(mandatory_basis)
            ],
            "cluster_candidates": [],
            "selected_profile_hashes": current_hashes,
            "selected_relationship_memory_hashes": current_memory_hashes,
            "reconciled_catalogue_revision": catalogue_revision,
            "selection_identity": selection_identity,
            "state_path": str(state_path),
            "pair_job_count": len(mandatory_basis),
            "accounted_pair_job_count": len(mandatory_basis),
            "relationship_stage_complete": False,
            "relationship_retry_on_resume": False,
            "provider_batch_count": 0,
        }
    catalogue_char_budget = _relationship_context_char_budget(reasoner, request)
    discovery_profiles = [
        _relationship_evidence_projection(
            profile_by_source[source_id],
            entry_by_source.get(source_id, {}),
            include_anchors=False,
        )
        for source_id in sorted(profile_by_source)
    ]
    discovery_entries = entries
    discovery_parked: list[dict[str, Any]] = []
    discovery_job_accounting: dict[str, dict[str, Any]] = {}
    routing_cards = [
        {
            **dict(row.get("routing_card") or {}),
            "shard_id": str(row.get("shard_id") or ""),
            "literature_id": str(row.get("literature_id") or ""),
        }
        for row in catalogue_payload.get("shards", []) or []
        if isinstance(row, Mapping) and row.get("shard_id")
    ]
    collection_rows = [
        dict(row)
        for row in catalogue_payload.get("collections", []) or []
        if isinstance(row, Mapping) and row.get("key")
    ]
    collection_cards = [
        {
            **dict(row.get("routing_card") or {}),
            "shard_id": f"collection-{row['key']}",
            "literature_id": f"collection-{row.get('parent_key') or row['key']}",
            "routing_kind": "zotero_collection",
        }
        for row in collection_rows
        if isinstance(row.get("routing_card"), Mapping)
    ]
    virtual_shard_rows = [
        dict(row)
        for row in catalogue_payload.get("virtual_shards", []) or []
        if isinstance(row, Mapping) and row.get("shard_id")
    ]
    virtual_cards = [
        {
            **dict(row.get("routing_card") or {}),
            "shard_id": str(row["shard_id"]),
            "literature_id": f"virtual-{row.get('topic_id') or row['shard_id']}",
            "routing_kind": "virtual_topic",
        }
        for row in virtual_shard_rows
        if isinstance(row.get("routing_card"), Mapping)
    ]
    collection_index_cards = [
        {
            "literature_id": str(row.get("literature_id") or ""),
            "title": " ".join(str(row.get("title") or "").split())[:240],
            "scope": " ".join(str(row.get("scope") or "").split())[:360],
            "source_count": int(row.get("source_count", 0) or 0),
        }
        for row in catalogue_payload.get("literatures", []) or []
        if isinstance(row, Mapping) and row.get("literature_id")
    ]
    if not collection_index_cards:
        collection_index_cards = [
            {
                "literature_id": literature_id,
                "title": literature_id,
                "scope": "",
                "source_count": sum(
                    literature_id
                    in set(
                        entry.get("literature_ids", [])
                        or entry.get("collections", [])
                        or []
                    )
                    for entry in entries
                ),
            }
            for literature_id in sorted(
                {
                    str(value)
                    for entry in entries
                    for value in (
                        entry.get("literature_ids", [])
                        or entry.get("collections", [])
                        or []
                    )
                    if str(value)
                }
            )
        ]
    position_context = [
        {
            "literature_position_id": str(
                row.get("literature_position_id") or ""
            ),
            "current_source_id": str(row.get("current_source_id") or ""),
            "matched_source_id": str(row.get("matched_source_id") or ""),
            "engagement": " ".join(
                str(row.get("engagement") or "").split()
            )[:400],
        }
        for row in positions.get("positions", []) or []
        if isinstance(row, Mapping)
        and str(row.get("current_source_id") or "") in profile_by_source
        and str(row.get("matched_source_id") or "") in profile_by_source
    ]
    memberships_by_source = {
        source_id: set(
            entry.get("literature_ids", [])
            or entry.get("collections", [])
            or []
        )
        for source_id, entry in entry_by_source.items()
    }
    bridge_position_context = [
        row
        for row in position_context
        if memberships_by_source.get(row["current_source_id"])
        and memberships_by_source.get(row["matched_source_id"])
        and memberships_by_source[row["current_source_id"]].isdisjoint(
            memberships_by_source[row["matched_source_id"]]
        )
    ]
    def discovery_memory(source_ids: set[str]) -> dict[str, Any]:
        return {
            "literature_positions": [
                row
                for row in position_context
                if row["current_source_id"] in source_ids
                or row["matched_source_id"] in source_ids
            ],
        }

    base_discovery_context = {
        "discovery_mode": "global",
        "catalogue_revision": catalogue_revision,
        "focus_source_ids": focus_source_ids,
        "mandatory_pairs": [list(pair) for pair in sorted(mandatory_basis)],
        "prior_negative_pairs": [list(pair) for pair in sorted(negative_pairs)],
        "reserved_bridge_fraction": 0.4,
    }
    full_discovery_context = {
        **base_discovery_context,
        "catalogue": discovery_entries,
        **discovery_memory(set(profile_by_source)),
    }
    requires_routing = (
        _reasoner_packet_chars(
            [profile_to_dict(profile) for profile in discovery_profiles],
            full_discovery_context,
        )
        > catalogue_char_budget
    )
    routing_call_count = (
        (2 if collection_cards else 1) if requires_routing else 0
    )
    bridge_routing_call_count = int(bool(collection_cards or virtual_cards))
    discovery_call_count = 2 + routing_call_count + bridge_routing_call_count
    can_discover = (
        remaining_calls >= mandatory_call_count + discovery_call_count + 1
    )
    adjudication_call_capacity = min(
        20,
        max(
            mandatory_call_count,
            remaining_calls - (discovery_call_count if can_discover else 0),
        ),
    )
    pair_capacity = adjudication_call_capacity * batch_max_jobs
    inferred_capacity = (
        min(
            max(0, _RELATIONSHIP_CANDIDATE_MAX - len(mandatory_basis)),
            max(0, pair_capacity - len(mandatory_basis)),
        )
        if can_discover
        else 0
    )
    general_capacity = min(
        _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
        (inferred_capacity * 2 + 4) // 5,
    )
    bridge_capacity = min(
        _RELATIONSHIP_BRIDGE_CANDIDATE_MAX,
        max(0, inferred_capacity - general_capacity),
    )
    discovery_completed = False
    discovery_terminal = False
    if shared_plan_active and not requires_routing:
        can_discover = False
    if can_discover and requires_routing:
        selected_collection_source_ids = set(profile_by_source)
        if collection_cards:
            try:
                collection_routing = reasoner_calls(
                    "relationship_collection_selection",
                    f"collections-{catalogue_revision[:16]}",
                    "select_relationship_shards",
                    [],
                    {
                        "catalogue_revision": catalogue_revision,
                        "routing_cards": collection_cards,
                        "focus_source_ids": [],
                        "discovery_mode": "relationship_collection_routing",
                    },
                )
            except Exception as exc:
                collection_routing = {}
                failure_class = _synthesis_failure_class(exc)
                discovery_terminal |= failure_class != "transport"
                discovery_parked.append(
                    {
                        "reason": "relationship_collection_routing_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retry_on_resume": failure_class == "transport",
                    }
                )
            selected_collection_keys = {
                str(value).removeprefix("collection-")
                for value in collection_routing.get("shard_ids", []) or []
                if str(value).startswith("collection-")
            }
            if selected_collection_keys:
                children_by_key = {
                    str(row.get("key") or ""): {
                        str(value) for value in row.get("child_keys", []) or []
                    }
                    for row in collection_rows
                }
                pending = list(selected_collection_keys)
                while pending:
                    current = pending.pop()
                    for child in children_by_key.get(current, set()):
                        if child not in selected_collection_keys:
                            selected_collection_keys.add(child)
                            pending.append(child)
                selected_collection_source_ids = {
                    str(source_id)
                    for row in collection_rows
                    if str(row.get("key") or "") in selected_collection_keys
                    for source_id in row.get("direct_source_ids", []) or []
                    if str(source_id) in profile_by_source
                }
                routing_cards = [
                    card
                    for card in routing_cards
                    if set(
                        next(
                            (
                                shard.get("source_ids", []) or []
                                for shard in catalogue_payload.get("shards", []) or []
                                if isinstance(shard, Mapping)
                                and str(shard.get("shard_id") or "")
                                == str(card.get("shard_id") or "")
                            ),
                            [],
                        )
                    )
                    & selected_collection_source_ids
                ]
        routing_context = {
            "catalogue_revision": catalogue_revision,
            "routing_cards": routing_cards,
            "focus_source_ids": focus_source_ids,
            "discovery_mode": "relationship_shard_routing",
            **discovery_memory(set(focus_source_ids)),
        }
        routing_profiles: list[EvidenceProfile] = []
        shard_selector = getattr(reasoner, "select_relationship_shards", None)
        if (
            not callable(shard_selector)
            or not routing_cards
            or _reasoner_packet_chars(
                [profile_to_dict(profile) for profile in routing_profiles],
                routing_context,
            )
            > catalogue_char_budget
        ):
            can_discover = False
            general_capacity = 0
            discovery_parked.append(
                {
                    "reason": "relationship_catalogue_routing_unavailable",
                    "retry_on_resume": False,
                }
            )
        else:
            try:
                routing = reasoner_calls(
                    "relationship_shard_selection",
                    f"global-{catalogue_revision[:16]}",
                    "select_relationship_shards",
                    routing_profiles,
                    routing_context,
                )
            except Exception as exc:
                routing = {}
                can_discover = False
                general_capacity = 0
                failure_class = _synthesis_failure_class(exc)
                discovery_terminal |= failure_class != "transport"
                discovery_parked.append(
                    {
                        "reason": "relationship_catalogue_routing_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retry_on_resume": failure_class == "transport",
                    }
                )
            selected_shards = {
                str(value)
                for value in routing.get("shard_ids", []) or []
                if str(value)
            }
            selected_source_ids = {
                str(source_id)
                for shard in catalogue_payload.get("shards", []) or []
                if isinstance(shard, Mapping)
                and str(shard.get("shard_id") or "") in selected_shards
                for source_id in shard.get("source_ids", []) or []
                if str(source_id) in profile_by_source
            }
            selected_source_ids &= selected_collection_source_ids
            discovery_entries = [
                row
                for row in entries
                if str(row.get("source_id") or "") in selected_source_ids
            ]
            discovery_profiles = [
                _relationship_evidence_projection(
                    profile_by_source[source_id],
                    entry_by_source.get(source_id, {}),
                    include_anchors=False,
                )
                for source_id in sorted(selected_source_ids)
            ]
            if not discovery_profiles:
                can_discover = False
                general_capacity = 0
                discovery_parked.append(
                    {"reason": "relationship_catalogue_routing_selected_no_sources"}
                )
    candidate_tasks: list[
        tuple[str, str, Sequence[Any], dict[str, Any], str]
    ] = []
    if can_discover and general_capacity:
        discovery_source_ids = {
            str(row.get("source_id") or "")
            for row in discovery_entries
            if str(row.get("source_id") or "")
        }
        discovery_context = {
            **base_discovery_context,
            "discovery_mode": (
                "routed_shards" if requires_routing else "global"
            ),
            "catalogue": discovery_entries,
            "max_inferred_pairs": general_capacity,
            **discovery_memory(discovery_source_ids),
        }
        if (
            _reasoner_packet_chars(
                [profile_to_dict(profile) for profile in discovery_profiles],
                discovery_context,
            )
            <= catalogue_char_budget
        ):
            candidate_tasks.append(
                (
                    "relationship_candidate_selection",
                    f"global-{catalogue_revision[:16]}",
                    discovery_profiles,
                    discovery_context,
                    "general",
                )
            )
        else:
            discovery_parked.append(
                {
                    "reason": "relationship_catalogue_partition_exceeds_context_budget",
                    "retry_on_resume": False,
                }
            )
            discovery_terminal = True

    bridge_source_sets: list[tuple[set[str], tuple[str, str]]] = []
    bridge_side_sources: dict[str, set[str]] = {}
    bridge_route_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    bridge_selector = getattr(reasoner, "select_relationship_bridge_shards", None)
    collection_side_sources = {
        f"collection-{row['key']}": {
            str(source_id)
            for source_id in row.get("direct_source_ids", []) or []
            if str(source_id) in profile_by_source
        }
        for row in collection_rows
    }
    usable_collection_cards = [
        card
        for card in collection_cards
        if collection_side_sources.get(str(card.get("shard_id") or ""))
    ]
    bridge_cards = (
        usable_collection_cards
        if len(usable_collection_cards) >= 2
        else virtual_cards
    )
    offered_bridge_ids = {
        str(card.get("shard_id") or "") for card in bridge_cards
    }
    if can_discover and bridge_capacity:
        if callable(bridge_selector) and bridge_cards:
            try:
                routed = reasoner_calls(
                    "relationship_bridge_shard_selection",
                    f"bridge-{catalogue_revision[:16]}",
                    "select_relationship_bridge_shards",
                    [],
                    {
                        "catalogue_revision": catalogue_revision,
                        "routing_cards": bridge_cards,
                        "collection_index_cards": collection_index_cards,
                        "navigation_cards": virtual_cards,
                        "discovery_mode": "bridge_shard_routing",
                    },
                )
                bridge_side_sources = {
                    **collection_side_sources,
                    **{
                        str(row.get("shard_id") or ""): {
                            str(source_id)
                            for source_id in row.get("source_ids", []) or []
                            if str(source_id) in profile_by_source
                        }
                        for row in virtual_shard_rows
                    },
                }
                bridge_source_sets = []
                for row in routed.get("shard_pairs", []) or []:
                    if not isinstance(row, Mapping):
                        continue
                    left_shard_id = str(row.get("left_shard_id") or "")
                    right_shard_id = str(row.get("right_shard_id") or "")
                    if (
                        left_shard_id not in offered_bridge_ids
                        or right_shard_id not in offered_bridge_ids
                        or left_shard_id == right_shard_id
                    ):
                        continue
                    left_sources = bridge_side_sources.get(left_shard_id, set())
                    right_sources = bridge_side_sources.get(right_shard_id, set())
                    overlap = left_sources & right_sources
                    left_sources = left_sources - overlap
                    right_sources = right_sources - overlap
                    if not left_sources or not right_sources:
                        continue
                    try:
                        target_count = int(
                            row.get("target_candidate_count", 12) or 12
                        )
                    except (TypeError, ValueError):
                        target_count = 12
                    bridge_route_metadata[(left_shard_id, right_shard_id)] = {
                        "bridge_family": str(row.get("bridge_family") or ""),
                        "why_examine": str(
                            row.get("why_examine") or row.get("reason") or ""
                        ),
                        "target_candidate_count": max(
                            1,
                            min(32, target_count),
                        ),
                        "left_source_ids": sorted(left_sources),
                        "right_source_ids": sorted(right_sources),
                    }
                    bridge_source_sets.append(
                        (
                            left_sources | right_sources,
                            (left_shard_id, right_shard_id),
                        )
                    )
            except Exception as exc:
                failure_class = _synthesis_failure_class(exc)
                discovery_terminal |= failure_class != "transport"
                discovery_parked.append(
                    {
                        "reason": "relationship_bridge_routing_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retry_on_resume": failure_class == "transport",
                    }
                )
        elif bridge_cards:
            discovery_terminal = True
            discovery_parked.append(
                {
                    "reason": "relationship_bridge_routing_unavailable",
                    "retry_on_resume": False,
                }
            )

    packed_bridge_source_sets: list[
        tuple[set[str], list[tuple[str, str]]]
    ] = []
    current_bridge_sources: set[str] = set()
    current_bridge_pairs: list[tuple[str, str]] = []
    for source_ids, literature_pair in bridge_source_sets:
        candidate_sources = current_bridge_sources | source_ids
        candidate_entries = [
            row
            for row in entries
            if str(row.get("source_id") or "") in candidate_sources
        ]
        candidate_literature_ids = {
            str(value)
            for row in candidate_entries
            for value in (
                row.get("literature_ids", [])
                or row.get("collections", [])
                or []
            )
            if str(value)
        }
        candidate_context = {
            **base_discovery_context,
            "discovery_mode": "bridge_only",
            "catalogue": candidate_entries,
            "collection_index_cards": [
                card
                for card in collection_index_cards
                if str(card.get("literature_id") or "")
                in candidate_literature_ids
            ],
            "max_inferred_pairs": bridge_capacity,
            "reserved_bridge_fraction": 1.0,
            "literature_positions": [
                row
                for row in bridge_position_context
                if row["current_source_id"] in candidate_sources
                and row["matched_source_id"] in candidate_sources
            ],
        }
        if current_bridge_sources and (
            _reasoner_packet_chars(
                [],
                candidate_context,
            )
            > catalogue_char_budget
        ):
            packed_bridge_source_sets.append(
                (current_bridge_sources, current_bridge_pairs)
            )
            current_bridge_sources = set(source_ids)
            current_bridge_pairs = [literature_pair]
        else:
            current_bridge_sources = candidate_sources
            if literature_pair not in current_bridge_pairs:
                current_bridge_pairs.append(literature_pair)
    if current_bridge_sources:
        packed_bridge_source_sets.append(
            (current_bridge_sources, current_bridge_pairs)
        )

    seen_bridge_packets: set[
        tuple[tuple[str, ...], tuple[tuple[str, str], ...]]
    ] = set()
    for packet_index, (source_ids, literature_pairs) in enumerate(
        packed_bridge_source_sets
    ):
        packet_ids = tuple(sorted(source_ids))
        packet_key = (packet_ids, tuple(sorted(literature_pairs)))
        if packet_key in seen_bridge_packets:
            continue
        seen_bridge_packets.add(packet_key)
        packet_entries = [
            row
            for row in entries
            if str(row.get("source_id") or "") in source_ids
        ]
        packet_literature_ids = {
            str(value)
            for row in packet_entries
            for value in (
                row.get("literature_ids", [])
                or row.get("collections", [])
                or []
            )
            if str(value)
        }
        bridge_context = {
            **base_discovery_context,
            "discovery_mode": "bridge_only",
            "catalogue": packet_entries,
            "collection_index_cards": [
                card
                for card in collection_index_cards
                if str(card.get("literature_id") or "")
                in packet_literature_ids
            ],
            "max_inferred_pairs": min(32, bridge_capacity),
            "reserved_bridge_fraction": 1.0,
            "literature_positions": [
                row
                for row in bridge_position_context
                if row["current_source_id"] in source_ids
                and row["matched_source_id"] in source_ids
            ],
        }
        if (
            _reasoner_packet_chars(
                [],
                bridge_context,
            )
            > catalogue_char_budget
        ):
            discovery_terminal = True
            discovery_parked.append(
                {
                    "reason": "relationship_bridge_packet_exceeds_context_budget",
                    "retry_on_resume": False,
                }
            )
            continue
        bridge_jobs = [
            {
                "bridge_job_id": f"bridge-job-{packet_index + 1}-{index + 1}",
                "left_shard_id": pair[0],
                "right_shard_id": pair[1],
                "left_source_ids": sorted(
                    bridge_route_metadata.get(pair, {}).get(
                        "left_source_ids",
                        bridge_side_sources.get(pair[0], set()),
                    )
                ),
                "right_source_ids": sorted(
                    bridge_route_metadata.get(pair, {}).get(
                        "right_source_ids",
                        bridge_side_sources.get(pair[1], set()),
                    )
                ),
                **bridge_route_metadata.get(pair, {}),
            }
            for index, pair in enumerate(literature_pairs)
        ]
        candidate_tasks.append(
            (
                "relationship_bridge_candidate_selection",
                f"bridge-{catalogue_revision[:16]}-{packet_index + 1}",
                [],
                {
                    **bridge_context,
                    "bridge_jobs": bridge_jobs,
                    "max_inferred_pairs": min(
                        bridge_capacity,
                        sum(
                            int(row.get("target_candidate_count", 12) or 12)
                            for row in bridge_jobs
                        ),
                    ),
                },
                "bridge",
            )
        )

    if shared_plan_active:
        shared_discovery_active = True
        routed_fallback_tasks = list(candidate_tasks)
        shared_packet_overflow = False
        lean_by_source = {
            str(row.get("source_id") or ""): row
            for row in lean_discovery_projection(
                profiles,
                catalogue_payload,
            )
            if str(row.get("source_id") or "") in profile_by_source
        }
        broad_jobs: list[dict[str, Any]] = []
        complement_jobs: list[dict[str, Any]] = []
        analytical_source_ids = _analytical_profile_source_ids(
            list(profile_by_source.values())
        )
        for raw_job in shared_family_plan.get("discovery_jobs", []) or []:
            if not isinstance(raw_job, Mapping):
                continue
            job_id = str(
                raw_job.get("job_id")
                or "family-job-" + stable_hash(raw_job)[:12]
            )
            left_ids = sorted(
                {
                    str(value)
                    for value in raw_job.get("left_source_ids", []) or []
                    if str(value) in lean_by_source
                    and str(value) in analytical_source_ids
                }
            )
            right_ids = sorted(
                {
                    str(value)
                    for value in raw_job.get("right_source_ids", []) or []
                    if str(value) in lean_by_source
                    and str(value) in analytical_source_ids
                }
            )
            overlap = set(left_ids) & set(right_ids)
            left_ids = [value for value in left_ids if value not in overlap]
            right_ids = [value for value in right_ids if value not in overlap]
            if not left_ids or not right_ids:
                discovery_job_accounting[job_id] = {
                    "bridge_job_id": job_id,
                    "status": "insufficient_analytical_endpoints",
                    "candidate_floor": 0,
                    "valid_unique_candidates": 0,
                    "distinct_left_endpoints": 0,
                    "distinct_right_endpoints": 0,
                    "explicit_no_more_candidates": False,
                    "packet_status": "not_scheduled",
                }
                continue
            job = {
                "bridge_job_id": job_id,
                "left_source_ids": left_ids,
                "right_source_ids": right_ids,
                "bridge_family": str(raw_job.get("family") or ""),
                "why_examine": str(raw_job.get("discovery_goal") or ""),
                "target_candidate_count": int(
                    raw_job.get("candidate_quota", 24) or 24
                ),
                "requested_collection_pair": list(
                    raw_job.get("requested_collection_pair", []) or []
                ),
            }
            (
                broad_jobs
                if job["requested_collection_pair"]
                else complement_jobs
            ).append(job)
        candidate_tasks = []
        family_rows = {
            str(row.get("family_id") or ""): dict(row)
            for row in shared_family_plan.get("literature_families", []) or []
            if isinstance(row, Mapping) and row.get("family_id")
        }

        def shared_plan_task(
            pass_name: str,
            pool: str,
            packet_jobs: Sequence[Mapping[str, Any]],
            packet_index: int,
        ) -> tuple[str, str, Sequence[Any], dict[str, Any], str]:
            source_ids = {
                source_id
                for job in packet_jobs
                for side in ("left_source_ids", "right_source_ids")
                for source_id in job[side]
            }
            task_profiles = [
                _relationship_evidence_projection(
                    profile_by_source[source_id],
                    lean_by_source[source_id],
                    include_anchors=False,
                )
                for source_id in sorted(source_ids)
            ]
            relevant_families = [
                family_rows[family_id]
                for family_id in sorted(
                    {
                        str(job.get("bridge_family") or "")
                        for job in packet_jobs
                    }
                    & set(family_rows)
                )
            ]
            stage = (
                "relationship_bridge_candidate_selection"
                if pool == "bridge"
                else "relationship_candidate_selection"
            )
            task_context = {
                **base_discovery_context,
                "discovery_mode": (
                    "bridge_only"
                    if pass_name == "broad"
                    else "complementary_family_discovery"
                ),
                "discovery_pass": pass_name,
                "catalogue": [
                    lean_by_source[source_id]
                    for source_id in sorted(source_ids)
                ],
                "collection_index_cards": collection_index_cards,
                "bridge_jobs": packet_jobs,
                "literature_families": relevant_families,
                "max_inferred_pairs": (
                    50
                    if pass_name == "broad"
                    else min(
                        _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
                        sum(
                            int(row.get("target_candidate_count", 24) or 24)
                            for row in packet_jobs
                        ),
                    )
                ),
            }
            return (
                stage,
                "shared-plan-"
                + pass_name
                + f"-{packet_index}-"
                + stable_hash(list(packet_jobs))[:16],
                task_profiles,
                task_context,
                pool,
            )

        if broad_jobs:
            for job in broad_jobs:
                job_id = str(job["bridge_job_id"])
                discovery_job_accounting[job_id] = {
                    "bridge_job_id": job_id,
                    "status": "eligible",
                    "candidate_floor": 0,
                    "valid_unique_candidates": 0,
                    "distinct_left_endpoints": 0,
                    "distinct_right_endpoints": 0,
                    "explicit_no_more_candidates": False,
                    "packet_status": "scheduled",
                }
            candidate_tasks.append(
                shared_plan_task("broad", "bridge", broad_jobs, 1)
            )

        complementary_allocations = _allocate_complementary_candidate_quotas(
            complement_jobs,
            capacity=_RELATIONSHIP_GENERAL_CANDIDATE_MAX,
        )
        allocated_complement_jobs = [
            {
                **job,
                "target_candidate_count": complementary_allocations.get(
                    str(job.get("bridge_job_id") or ""), 0
                ),
            }
            for job in complement_jobs
            if complementary_allocations.get(
                str(job.get("bridge_job_id") or ""), 0
            )
        ]
        for job in allocated_complement_jobs:
            job_id = str(job["bridge_job_id"])
            discovery_job_accounting[job_id] = {
                "bridge_job_id": job_id,
                "status": "eligible",
                "candidate_floor": int(job["target_candidate_count"]),
                "valid_unique_candidates": 0,
                "distinct_left_endpoints": 0,
                "distinct_right_endpoints": 0,
                "explicit_no_more_candidates": False,
                "packet_status": "scheduled",
            }
        measured_job_sizes: dict[str, int] = {}
        for job in allocated_complement_jobs:
            measurement_task = shared_plan_task(
                "complement", "general", [job], 0
            )
            measured_job_sizes[str(job.get("bridge_job_id") or "")] = (
                _reasoner_packet_chars(
                    measurement_task[2], measurement_task[3]
                )
            )
        complement_packets = _balance_complementary_jobs(
            allocated_complement_jobs,
            measured_sizes=measured_job_sizes,
        )
        context_budget = _relationship_context_char_budget(reasoner, request)
        complement_tasks = [
            shared_plan_task(
                "complement", "general", packet_jobs, packet_index
            )
            for packet_index, packet_jobs in enumerate(
                complement_packets, start=1
            )
        ]
        oversized = [
            index
            for index, task in enumerate(complement_tasks)
            if _reasoner_packet_chars(task[2], task[3]) > context_budget
        ]
        if oversized and len(complement_packets) == 2:
            complement_packets = _balance_complementary_jobs(
                allocated_complement_jobs,
                measured_sizes=measured_job_sizes,
                packet_count=3,
            )
            complement_tasks = [
                shared_plan_task(
                    "complement", "general", packet_jobs, packet_index
                )
                for packet_index, packet_jobs in enumerate(
                    complement_packets, start=1
                )
            ]
        shared_packet_overflow = any(
            _reasoner_packet_chars(task[2], task[3]) > context_budget
            for task in [*candidate_tasks, *complement_tasks]
        ) or len(complement_tasks) > 3
        candidate_tasks.extend(complement_tasks)
        if shared_packet_overflow:
            candidate_tasks = routed_fallback_tasks
            discovery_job_accounting = {}
            shared_discovery_active = False
    else:
        shared_discovery_active = False

    current_remaining_calls = (
        remaining_calls
        if configured_max_calls is None
        else max(
            0,
            int(configured_max_calls or 0)
            - int(
                getattr(reasoner_calls, "cumulative_provider_calls", 0) or 0
            ),
        )
    )
    available_discovery_calls = max(
        0, current_remaining_calls - mandatory_call_count - 2
    )
    if len(candidate_tasks) > available_discovery_calls:
        discovery_terminal = True
        discovery_parked.append(
            {
                "reason": "relationship_discovery_budget_conflict",
                "required_calls": len(candidate_tasks),
                "available_calls": available_discovery_calls,
                "retry_on_resume": False,
            }
        )
        candidate_tasks = []
    else:
        adjudication_call_capacity = min(
            20,
            max(
                mandatory_call_count,
                current_remaining_calls - len(candidate_tasks) - 1,
            ),
        )
        pair_capacity = adjudication_call_capacity * batch_max_jobs
        inferred_capacity = min(
            max(0, _RELATIONSHIP_CANDIDATE_MAX - len(mandatory_basis)),
            max(0, pair_capacity - len(mandatory_basis)),
        )
        if shared_discovery_active:
            bridge_capacity = min(
                _RELATIONSHIP_BRIDGE_CANDIDATE_MAX, inferred_capacity
            )
            general_capacity = min(
                _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
                max(0, inferred_capacity - bridge_capacity),
            )
        else:
            general_capacity = min(
                _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
                (inferred_capacity * 2 + 4) // 5,
            )
            bridge_capacity = min(
                _RELATIONSHIP_BRIDGE_CANDIDATE_MAX,
                max(0, inferred_capacity - general_capacity),
            )
        candidate_tasks = [
            (
                stage,
                key,
                task_profiles,
                {
                    **task_context,
                    "max_inferred_pairs": min(
                        int(task_context.get("max_inferred_pairs", 0) or 0),
                        bridge_capacity if pool == "bridge" else general_capacity,
                    ),
                },
                pool,
            )
            for stage, key, task_profiles, task_context, pool in candidate_tasks
        ]

    candidate_results: dict[
        str, list[tuple[str, Mapping[str, Any]]]
    ] = defaultdict(list)

    def run_candidate_task(
        task: tuple[str, str, Sequence[Any], dict[str, Any], str]
    ) -> tuple[str, Mapping[str, Any]]:
        stage, key, task_profiles, task_context, pool = task
        response = reasoner_calls(
            stage,
            key,
            "select_relationship_candidates",
            task_profiles,
            task_context,
        )
        if (
            str(getattr(reasoner, "profile_generation_route", ""))
            == "built_in_reader"
            and str(getattr(reasoner, "name", "")) == "deepseek"
            and task_context.get("discovery_pass")
            in {"complement", "coverage_followup"}
        ):
            expected_job_ids = {
                str(row.get("bridge_job_id") or "")
                for row in task_context.get("bridge_jobs", []) or []
                if isinstance(row, Mapping) and row.get("bridge_job_id")
            }
            raw_outcomes = response.get("job_outcomes", []) or []
            outcome_ids = [
                str(row.get("bridge_job_id") or row.get("job_id") or "")
                for row in raw_outcomes
                if isinstance(row, Mapping)
                and str(row.get("status") or "")
                in {"completed", "no_more_candidates"}
            ]
            if set(outcome_ids) != expected_job_ids or len(outcome_ids) != len(
                expected_job_ids
            ):
                raise ValueError(
                    "built-in complementary discovery must account for every job"
                )
        if task_context.get("bridge_jobs"):
            jobs = {
                str(row.get("bridge_job_id") or ""): row
                for row in task_context.get("bridge_jobs", []) or []
                if isinstance(row, Mapping) and row.get("bridge_job_id")
            }
            candidates: list[dict[str, Any]] = []
            for raw in response.get("candidates", []) or []:
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                job = jobs.get(str(row.get("bridge_job_id") or ""))
                if not job:
                    row["_candidate_disposition"] = "parked_contract_failure"
                    candidates.append(row)
                    continue
                left_source_id = str(
                    row.get("left_source_id") or row.get("source_id") or ""
                )
                right_source_id = str(
                    row.get("right_source_id")
                    or row.get("target_source_id")
                    or row.get("target_id")
                    or ""
                )
                left_side = {
                    str(value) for value in job.get("left_source_ids", []) or []
                }
                right_side = {
                    str(value) for value in job.get("right_source_ids", []) or []
                }
                if not (
                    (left_source_id in left_side and right_source_id in right_side)
                    or (
                        left_source_id in right_side
                        and right_source_id in left_side
                    )
                ):
                    row["_candidate_disposition"] = "wrong_scope"
                    candidates.append(row)
                    continue
                row["discovery_route"] = "routed_cross_collection_bridge"
                row["discovery_job_id"] = str(
                    job.get("bridge_job_id") or ""
                )
                row["discovery_family"] = str(
                    job.get("bridge_family") or ""
                )
                row["discovery_job_quota"] = int(
                    job.get("target_candidate_count", 24) or 24
                )
                row["requested_collection_pair"] = list(
                    job.get("requested_collection_pair", []) or []
                )
                row["discovery_pass"] = str(
                    task_context.get("discovery_pass") or ""
                )
                candidates.append(row)
            response = {**dict(response), "candidates": candidates}
        return (
            pool,
            response,
        )

    def execute_candidate_tasks(
        tasks: Sequence[
            tuple[str, str, Sequence[Any], dict[str, Any], str]
        ],
    ) -> None:
        nonlocal discovery_terminal
        if not tasks:
            return
        with ThreadPoolExecutor(
            max_workers=_provider_worker_count(request, len(tasks)),
            thread_name_prefix="auto-zettelkasten-discovery",
        ) as executor:
            futures = {
                executor.submit(run_candidate_task, task): task
                for task in tasks
            }
            for future in as_completed(futures):
                task = futures[future]
                try:
                    pool, response = future.result()
                    candidate_results[pool].append((task[1], response))
                    outcome_by_job: dict[str, str] = {}
                    raw_outcomes = response.get("job_outcomes", []) or []
                    if isinstance(raw_outcomes, Mapping):
                        raw_outcomes = [
                            {
                                "bridge_job_id": str(job_id),
                                "status": status,
                            }
                            for job_id, status in raw_outcomes.items()
                        ]
                    for raw_outcome in raw_outcomes:
                        if not isinstance(raw_outcome, Mapping):
                            continue
                        job_id = str(
                            raw_outcome.get("bridge_job_id")
                            or raw_outcome.get("job_id")
                            or ""
                        )
                        status = str(raw_outcome.get("status") or "")
                        if status in {"completed", "no_more_candidates"}:
                            outcome_by_job[job_id] = status
                    for raw_job in task[3].get("bridge_jobs", []) or []:
                        if not isinstance(raw_job, Mapping):
                            continue
                        job_id = str(raw_job.get("bridge_job_id") or "")
                        accounting = discovery_job_accounting.get(job_id)
                        if accounting is None:
                            continue
                        pairs = {
                            canonical_pair(
                                str(
                                    row.get("left_source_id")
                                    or row.get("source_id")
                                    or ""
                                ),
                                str(
                                    row.get("right_source_id")
                                    or row.get("target_source_id")
                                    or row.get("target_id")
                                    or ""
                                ),
                            )
                            for row in response.get("candidates", []) or []
                            if isinstance(row, Mapping)
                            and str(row.get("discovery_job_id") or "") == job_id
                            and not row.get("_candidate_disposition")
                        }
                        left_side = set(raw_job.get("left_source_ids", []) or [])
                        right_side = set(raw_job.get("right_source_ids", []) or [])
                        accounting["valid_unique_candidates"] = len(pairs)
                        accounting["distinct_left_endpoints"] = len(
                            {
                                endpoint
                                for pair in pairs
                                for endpoint in pair
                                if endpoint in left_side
                            }
                        )
                        accounting["distinct_right_endpoints"] = len(
                            {
                                endpoint
                                for pair in pairs
                                for endpoint in pair
                                if endpoint in right_side
                            }
                        )
                        accounting["explicit_no_more_candidates"] = (
                            outcome_by_job.get(job_id) == "no_more_candidates"
                        )
                        accounting["packet_status"] = "completed"
                except Exception as exc:
                    failure_class = _synthesis_failure_class(exc)
                    discovery_terminal |= failure_class != "transport"
                    discovery_parked.append(
                        {
                            "reason": (
                                "relationship_bridge_candidate_discovery_failed"
                                if task[4] == "bridge"
                                else "relationship_candidate_discovery_failed"
                            ),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "discovery_pass": str(
                                task[3].get("discovery_pass") or ""
                            ),
                            "discovery_task_key": task[1],
                            "affected_job_ids": sorted(
                                str(row.get("bridge_job_id") or "")
                                for row in task[3].get("bridge_jobs", []) or []
                                if isinstance(row, Mapping)
                                and row.get("bridge_job_id")
                            ),
                            "retry_on_resume": failure_class == "transport",
                        }
                    )
                    for raw_job in task[3].get("bridge_jobs", []) or []:
                        if not isinstance(raw_job, Mapping):
                            continue
                        accounting = discovery_job_accounting.get(
                            str(raw_job.get("bridge_job_id") or "")
                        )
                        if accounting is not None:
                            accounting["packet_status"] = "failed"
                            accounting["packet_failure_class"] = failure_class
    broad_tasks = [
        task
        for task in candidate_tasks
        if task[3].get("discovery_pass") != "complement"
    ]
    complement_tasks = [
        task
        for task in candidate_tasks
        if task[3].get("discovery_pass") == "complement"
    ]
    execute_candidate_tasks(broad_tasks)
    prior_candidate_pairs = sorted(
        {
            canonical_pair(
                str(row.get("left_source_id") or row.get("source_id") or ""),
                str(
                    row.get("right_source_id")
                    or row.get("target_source_id")
                    or row.get("target_id")
                    or ""
                ),
            )
            for rows in candidate_results.values()
            for _key, response in rows
            for row in response.get("candidates", []) or []
            if isinstance(row, Mapping)
        }
    )
    complement_tasks = [
        (
            stage,
            key,
            task_profiles,
            {**task_context, "prior_candidate_pairs": prior_candidate_pairs},
            pool,
        )
        for stage, key, task_profiles, task_context, pool in complement_tasks
    ]
    execute_candidate_tasks(complement_tasks)
    if shared_discovery_active:
        undercovered_ids = {
            job_id
            for job_id, row in discovery_job_accounting.items()
            if row.get("status") == "eligible"
            and not row.get("explicit_no_more_candidates")
            and (
                (
                    row.get("packet_status") == "failed"
                    and row.get("packet_failure_class") == "transport"
                )
                or (
                    row.get("packet_status") == "completed"
                    and int(row.get("valid_unique_candidates", 0) or 0)
                    < int(row.get("candidate_floor", 0) or 0)
                )
            )
            and any(
                str(job.get("bridge_job_id") or "") == job_id
                for job in allocated_complement_jobs
            )
        }
        prior_candidate_pairs = sorted(
            {
                canonical_pair(
                    str(row.get("left_source_id") or row.get("source_id") or ""),
                    str(
                        row.get("right_source_id")
                        or row.get("target_source_id")
                        or row.get("target_id")
                        or ""
                    ),
                )
                for rows in candidate_results.values()
                for _key, response in rows
                for row in response.get("candidates", []) or []
                if isinstance(row, Mapping)
                and not row.get("_candidate_disposition")
            }
        )
        remaining_candidate_capacity = max(
            0, _RELATIONSHIP_CANDIDATE_MAX - len(prior_candidate_pairs)
        )
        followup_jobs = [
            {
                **job,
                "target_candidate_count": max(
                    1,
                    int(
                        discovery_job_accounting[str(job["bridge_job_id"])][
                            "candidate_floor"
                        ]
                    )
                    - int(
                        discovery_job_accounting[str(job["bridge_job_id"])][
                            "valid_unique_candidates"
                        ]
                    ),
                ),
            }
            for job in allocated_complement_jobs
            if str(job.get("bridge_job_id") or "") in undercovered_ids
        ]
        if followup_jobs and remaining_candidate_capacity:
            requested_keys = list(
                shared_family_plan.get("requested_collection_keys", []) or []
            )
            max_followup_packets = 1 if len(requested_keys) <= 2 else 2
            followup_packets = [followup_jobs]
            followup_task = shared_plan_task(
                "coverage_followup", "general", followup_jobs, 1
            )
            if (
                _reasoner_packet_chars(followup_task[2], followup_task[3])
                > context_budget
                and max_followup_packets == 2
            ):
                followup_packets = _balance_complementary_jobs(
                    followup_jobs,
                    measured_sizes=measured_job_sizes,
                    packet_count=2,
                )
            followup_tasks = []
            for packet_index, packet_jobs in enumerate(
                followup_packets, start=1
            ):
                task = shared_plan_task(
                    "coverage_followup", "general", packet_jobs, packet_index
                )
                task = (
                    task[0],
                    task[1],
                    task[2],
                    {
                        **task[3],
                        "prior_candidate_pairs": prior_candidate_pairs,
                        "excluded_candidate_pairs": prior_candidate_pairs,
                        "remaining_global_capacity": remaining_candidate_capacity,
                        "max_inferred_pairs": min(
                            remaining_candidate_capacity,
                            sum(
                                int(job["target_candidate_count"])
                                for job in packet_jobs
                            ),
                        ),
                    },
                    task[4],
                )
                if _reasoner_packet_chars(task[2], task[3]) <= context_budget:
                    followup_tasks.append(task)
                else:
                    for job in packet_jobs:
                        discovery_job_accounting[str(job["bridge_job_id"])][
                            "status"
                        ] = "deferred_context"
            followup_calls_available = (
                configured_max_calls is None
                or int(
                    getattr(reasoner_calls, "cumulative_provider_calls", 0)
                    or 0
                )
                + len(followup_tasks)
                <= int(configured_max_calls or 0)
            )
            if followup_tasks and followup_calls_available:
                execute_candidate_tasks(followup_tasks[:max_followup_packets])
            elif followup_tasks:
                for job in followup_jobs:
                    discovery_job_accounting[str(job["bridge_job_id"])][
                        "status"
                    ] = "deferred_budget"
        elif followup_jobs:
            for job in followup_jobs:
                discovery_job_accounting[str(job["bridge_job_id"])][
                    "status"
                ] = "deferred_budget"

        # Recompute exact per-job coverage across initial and follow-up calls.
        for job in [*broad_jobs, *allocated_complement_jobs]:
            job_id = str(job["bridge_job_id"])
            accounting = discovery_job_accounting[job_id]
            pairs = {
                canonical_pair(
                    str(row.get("left_source_id") or row.get("source_id") or ""),
                    str(
                        row.get("right_source_id")
                        or row.get("target_source_id")
                        or row.get("target_id")
                        or ""
                    ),
                )
                for rows in candidate_results.values()
                for _key, response in rows
                for row in response.get("candidates", []) or []
                if isinstance(row, Mapping)
                and str(row.get("discovery_job_id") or "") == job_id
                and not row.get("_candidate_disposition")
            }
            left_side = set(job.get("left_source_ids", []) or [])
            right_side = set(job.get("right_source_ids", []) or [])
            accounting["valid_unique_candidates"] = len(pairs)
            accounting["distinct_left_endpoints"] = len(
                {
                    endpoint
                    for pair in pairs
                    for endpoint in pair
                    if endpoint in left_side
                }
            )
            accounting["distinct_right_endpoints"] = len(
                {
                    endpoint
                    for pair in pairs
                    for endpoint in pair
                    if endpoint in right_side
                }
            )
            if accounting.get("status") == "eligible":
                accounting["status"] = (
                    "completed"
                    if int(accounting["valid_unique_candidates"])
                    >= int(accounting["candidate_floor"])
                    or accounting.get("explicit_no_more_candidates")
                    else "under_covered"
                )
        repaired_job_ids = {
            job_id
            for job_id, row in discovery_job_accounting.items()
            if row.get("packet_status") == "completed"
        }
        discovery_parked = [
            row
            for row in discovery_parked
            if not (
                row.get("affected_job_ids")
                and set(row.get("affected_job_ids", []) or [])
                <= repaired_job_ids
            )
        ]
        if not discovery_parked:
            discovery_terminal = False
    successful_discovery_tasks = sum(
        len(rows) for rows in candidate_results.values()
    )
    discovery_retryable = any(
        bool(row.get("retry_on_resume")) for row in discovery_parked
    )
    discovery_completed = bool(candidate_tasks) and bool(
        (
            all(
                row.get("status")
                in {"completed", "insufficient_analytical_endpoints"}
                for row in discovery_job_accounting.values()
            )
            if shared_discovery_active
            else successful_discovery_tasks == len(candidate_tasks)
        )
        and not discovery_parked
    )
    discovery_usable = bool(
        discovery_completed
        or (
            successful_discovery_tasks > 0
            and not discovery_retryable
        )
    )
    discovery_status = (
        "complete"
        if discovery_completed
        else "partial"
        if discovery_usable
        else "failed"
    )

    merged_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    merged_candidate_pools: dict[tuple[str, str], set[str]] = defaultdict(set)
    unmergeable_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_items = [
        (pool, key, index, dict(row))
        for pool, responses in candidate_results.items()
        for key, response in responses
        for index, row in enumerate(response.get("candidates", []) or [])
        if isinstance(row, Mapping)
    ]

    def candidate_item_rank(value: tuple[str, str, int, dict[str, Any]]) -> int:
        try:
            return int(value[3].get("rank") or value[2] + 1)
        except (TypeError, ValueError):
            return value[2] + 1

    candidate_items.sort(
        key=lambda value: (
            0 if value[0] == "bridge" else 1,
            candidate_item_rank(value),
            value[1],
            value[2],
        )
    )
    for pool, _key, _index, row in candidate_items:
        pair = canonical_pair(
            str(row.get("left_source_id") or row.get("source_id") or ""),
            str(
                row.get("right_source_id")
                or row.get("target_source_id")
                or row.get("target_id")
                or ""
            ),
        )
        if not pair[0] or not pair[1] or row.get("_candidate_disposition"):
            unmergeable_candidates[pool].append(row)
            continue
        provenance = {
            key: row.get(key)
            for key in (
                "discovery_job_id",
                "discovery_family",
                "discovery_job_quota",
                "discovery_pass",
                "requested_collection_pair",
                "rank",
                "comparison_proposition",
                "why_compare",
            )
            if row.get(key) not in (None, "", [])
        }
        provenance_rows = [
            dict(value)
            for value in row.get("discovery_provenance", []) or []
            if isinstance(value, Mapping)
        ]
        if provenance and provenance not in provenance_rows:
            provenance_rows.append(provenance)
        current = merged_candidates.get(pair)
        if current is None:
            row["discovery_provenance"] = provenance_rows
            merged_candidates[pair] = row
        else:
            existing = list(current.get("discovery_provenance", []) or [])
            for value in provenance_rows:
                if value not in existing:
                    existing.append(value)
            current["discovery_provenance"] = existing
        merged_candidate_pools[pair].add(pool)
    bridge_payload = {
        "candidates": [
            *unmergeable_candidates.get("bridge", []),
            *[
                merged_candidates[pair]
                for pair in sorted(merged_candidates)
                if "bridge" in merged_candidate_pools[pair]
            ],
        ]
    }
    general_payload = {
        "candidates": [
            *unmergeable_candidates.get("general", []),
            *[
                merged_candidates[pair]
                for pair in sorted(merged_candidates)
                if "bridge" not in merged_candidate_pools[pair]
            ],
        ]
    }
    if configured_max_calls is not None:
        post_discovery_remaining = max(
            0,
            int(configured_max_calls or 0)
            - int(
                getattr(reasoner_calls, "cumulative_provider_calls", 0) or 0
            ),
        )
        adjudication_call_capacity = min(
            20, max(0, post_discovery_remaining - 1)
        )
        pair_capacity = adjudication_call_capacity * batch_max_jobs
        inferred_capacity = min(
            max(0, _RELATIONSHIP_CANDIDATE_MAX - len(mandatory_basis)),
            max(0, pair_capacity - len(mandatory_basis)),
        )
        if shared_discovery_active:
            bridge_capacity = min(
                _RELATIONSHIP_BRIDGE_CANDIDATE_MAX, inferred_capacity
            )
            general_capacity = min(
                _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
                max(0, inferred_capacity - bridge_capacity),
            )
        else:
            general_capacity = min(
                _RELATIONSHIP_GENERAL_CANDIDATE_MAX,
                (inferred_capacity * 2 + 4) // 5,
            )
            bridge_capacity = min(
                _RELATIONSHIP_BRIDGE_CANDIDATE_MAX,
                max(0, inferred_capacity - general_capacity),
            )
    excluded_pairs = set(mandatory_basis) | negative_pairs | visible_pairs
    excluded_pair_reasons = {
        **{pair: "current_no_relationship" for pair in negative_pairs},
        **{pair: "already_visible" for pair in visible_pairs},
    }
    candidate_dispositions: list[dict[str, Any]] = []
    bridge_rows = _ranked_relationship_candidates(
        bridge_payload,
        available_source_ids=set(profile_by_source),
        entry_by_source=entry_by_source,
        excluded_pairs=excluded_pairs,
        maximum=bridge_capacity,
        bridge_fraction=1.0,
        scope="bridge",
        dispositions=candidate_dispositions,
        excluded_pair_reasons=excluded_pair_reasons,
    )
    bridge_pairs = {
        canonical_pair(str(row["source_id"]), str(row["target_id"]))
        for row in bridge_rows
    }
    general_rows = _ranked_relationship_candidates(
        general_payload,
        available_source_ids=set(profile_by_source),
        entry_by_source=entry_by_source,
        excluded_pairs=excluded_pairs | bridge_pairs,
        maximum=general_capacity,
        bridge_fraction=0.4,
        dispositions=candidate_dispositions,
        excluded_pair_reasons={
            **excluded_pair_reasons,
            **{pair: "duplicate_merged" for pair in bridge_pairs},
        },
        job_floors={
            job_id: int(row.get("candidate_floor", 0) or 0)
            for job_id, row in discovery_job_accounting.items()
            if row.get("status") != "insufficient_analytical_endpoints"
            and not any(
                str(job.get("bridge_job_id") or "") == job_id
                for job in broad_jobs
            )
        }
        if shared_discovery_active
        else None,
    )
    inferred_rows = [*bridge_rows, *general_rows][:inferred_capacity]
    for row in candidate_dispositions:
        pair_values = list(row.get("pair", []) or [])
        if len(pair_values) != 2:
            continue
        pair = canonical_pair(str(pair_values[0]), str(pair_values[1]))
        if (
            pair in inactive_accepted_pairs
            and row.get("disposition") == "selected_for_adjudication"
        ):
            row["reconsideration"] = "inactive_or_retired_reconsidered"
    if focus_source_ids and not identity_changed:
        focus = set(focus_source_ids)
        inferred_rows = [
            row
            for row in inferred_rows
            if str(row.get("source_id") or "") in focus
            or str(row.get("target_id") or "") in focus
        ]
    final_inferred_pairs = {
        canonical_pair(str(row["source_id"]), str(row["target_id"]))
        for row in inferred_rows
    }
    for disposition in candidate_dispositions:
        pair_values = list(disposition.get("pair", []) or [])
        if (
            len(pair_values) == 2
            and disposition.get("disposition") == "selected_for_adjudication"
            and canonical_pair(str(pair_values[0]), str(pair_values[1]))
            not in final_inferred_pairs
        ):
            disposition["disposition"] = "deferred_capacity"
    candidate_basis_by_pair: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for raw in [
        *(bridge_payload.get("candidates", []) or []),
        *(general_payload.get("candidates", []) or []),
    ]:
        if not isinstance(raw, Mapping):
            continue
        pair = canonical_pair(
            str(raw.get("left_source_id") or raw.get("source_id") or ""),
            str(
                raw.get("right_source_id")
                or raw.get("target_id")
                or raw.get("target_source_id")
                or ""
            ),
        )
        if pair[0] in profile_by_source and pair[1] in profile_by_source:
            candidate_basis_by_pair[pair].append(dict(raw))
    candidate_by_pair = {
        canonical_pair(str(row["source_id"]), str(row["target_id"])): list(
            candidate_basis_by_pair.get(
                canonical_pair(str(row["source_id"]), str(row["target_id"])),
                [dict(row)],
            )
        )
        for row in inferred_rows
    }
    for pair, basis in mandatory_basis.items():
        candidate_by_pair[pair] = list(basis)

    run_id = str(getattr(reasoner_calls, "run_id", "") or "")
    job_root = (
        workspace / "11_state" / "runs" / run_id / "relationship_jobs"
    )
    global_job_root = workspace / "11_state" / "relationship_jobs"
    capabilities = dict(getattr(reasoner, "capabilities", {}) or {})
    provider_name = str(getattr(reasoner, "name", ""))
    model_name = str(getattr(reasoner, "model", ""))
    reasoner_backend = str(
        getattr(reasoner, "reasoner_backend", "") or provider_name
    )
    decision_identity = stable_hash(
        {
            "provider": provider_name,
            "model": model_name,
            "prompt_version": RELATIONSHIP_PROMPT_VERSION,
            "output_contract": decision_contract,
            "transport_policy": "source-evidence-only-v2",
            "policy_identity": relationship_policy_identity,
        }
    )
    jobs: list[RelationshipPairJob] = []
    for pair in sorted(candidate_by_pair):
        selected = {
            side: _selected_relationship_evidence(
                profile_by_source[source_id],
                requested_ids={
                    str(value)
                    for row in candidate_by_pair[pair]
                    for value in row.get(f"{side}_evidence_anchor_ids", []) or []
                    if str(value)
                },
            )
            for side, source_id in zip(("left", "right"), pair, strict=True)
        }
        literature_rows = [
            dict(row)
            for row in positions.get("positions", []) or []
            if isinstance(row, Mapping)
            and {
                str(row.get("current_source_id") or ""),
                str(row.get("matched_source_id") or ""),
            }
            == set(pair)
        ]
        explicit_pair_rows = [
            dict(row)
            for row in (
                registry.get("relations", [])
                or registry.get("links", [])
                or []
            )
            if isinstance(row, Mapping)
            and str(row.get("relation_type") or "")
            in {"cites", "cited_by", "zotero_related"}
            and canonical_pair(
                str(row.get("source_id") or ""),
                str(row.get("target_source_id") or ""),
            )
            == pair
        ]
        citation_direction = [
            {
                "citing_source_id": str(row.get("current_source_id") or ""),
                "cited_source_id": str(row.get("matched_source_id") or ""),
                "basis": "matched_literature_position",
            }
            for row in literature_rows
        ]
        for row in explicit_pair_rows:
            relation_type = str(row.get("relation_type") or "")
            source_id = str(row.get("source_id") or "")
            target_source_id = str(row.get("target_source_id") or "")
            if relation_type == "cites":
                citation_direction.append(
                    {
                        "citing_source_id": source_id,
                        "cited_source_id": target_source_id,
                        "basis": "explicit_citation",
                    }
                )
            elif relation_type == "cited_by":
                citation_direction.append(
                    {
                        "citing_source_id": target_source_id,
                        "cited_source_id": source_id,
                        "basis": "explicit_citation",
                    }
                )
        reuse_signals = [
            {
                "source": str(row.get("discovery_route") or "candidate"),
                "description": " ".join(
                    str(
                        row.get("reason")
                        or row.get("why_compare")
                        or row.get("why_relevant")
                        or row.get("comparison_proposition")
                        or row.get("engagement")
                        or ""
                    ).split()
                )[:500],
            }
            for row in [*candidate_by_pair[pair], *literature_rows]
            if any(
                token in str(row).casefold()
                for token in ("dataset", "coding reuse", "coded data", "reuses")
            )
        ]
        job = RelationshipPairJob(
            catalogue_revision=catalogue_revision,
            left_source_id=pair[0],
            right_source_id=pair[1],
            profiles={
                "left": profile_to_dict(
                    _relationship_evidence_projection(
                        profile_by_source[pair[0]],
                        entry_by_source.get(pair[0], {}),
                        include_anchors=False,
                    )
                ),
                "right": profile_to_dict(
                    _relationship_evidence_projection(
                        profile_by_source[pair[1]],
                        entry_by_source.get(pair[1], {}),
                        include_anchors=False,
                    )
                ),
            },
            atomic_notes={
                side: atomic_note_by_source.get(source_id, {})
                for side, source_id in zip(
                    ("left", "right"), pair, strict=True
                )
            },
            literature_positions=literature_rows,
            selected_evidence=selected,
            graph_context={
                "pair_context": {
                    "canonical_pair": {
                        "left_source_id": pair[0],
                        "right_source_id": pair[1],
                    },
                    "endpoint_profiles": {
                        source_id: {
                            "title": str(
                                entry_by_source.get(source_id, {}).get("title")
                                or ""
                            ),
                            "author": str(
                                entry_by_source.get(source_id, {}).get("author")
                                or ""
                            ),
                            "year": str(
                                entry_by_source.get(source_id, {}).get("year")
                                or ""
                            ),
                            "profile_hash": current_hashes[source_id],
                        }
                        for source_id in pair
                    },
                    "citation_direction": citation_direction,
                    "reuse_signals": reuse_signals,
                },
                "existing_neighbors": _relationship_neighbors(
                    pair, registry.get("links", []) or []
                ),
            },
            candidate_basis=candidate_by_pair[pair],
            prior_pair_memory={
                "decisions": [
                    row
                    for row in prior_decisions
                    if canonical_pair(
                        str(
                            row.get("source_id")
                            or row.get("left_source_id")
                            or ""
                        ),
                        str(
                            row.get("target_source_id")
                            or row.get("right_source_id")
                            or ""
                        ),
                    )
                    == pair
                ]
            },
            output_contract=decision_contract,
        )
        path = job_root / job.pair_job_id
        write_json(path / "input.json", job.to_dict())
        if not (path / "status.yml").is_file():
            write_yaml(
                path / "status.yml",
                {
                    "pair_job_id": job.pair_job_id,
                    "status": "pending",
                    "output_contract": job.output_contract,
                    "decision_identity": decision_identity,
                    "reasoner_backend": reasoner_backend,
                    "provider": provider_name,
                    "model": model_name,
                },
            )
        jobs.append(job)

    def validate_cached_job(
        job: RelationshipPairJob,
        payload: Mapping[str, Any],
    ) -> tuple[bool, dict[str, list[dict[str, Any]]]]:
        validation = validate_relationship_decision_rows(
            {"decisions": [dict(payload)]},
            jobs=[job],
            profiles=list(profile_by_source.values()),
            provider=provider_name,
            model=model_name,
            reasoner_backend=reasoner_backend,
            prompt_version=RELATIONSHIP_PROMPT_VERSION,
        )
        valid = bool(
            validation["accepted"] or validation["no_relationship"]
        ) and not bool(validation["needs_more_context"])
        return valid, validation

    responses: list[dict[str, Any]] = []
    unresolved: list[RelationshipPairJob] = []
    preparked: list[dict[str, Any]] = list(discovery_parked)
    for job in jobs:
        job_path = job_root / job.pair_job_id
        result_path = job_path / "result.json"
        status = read_yaml(job_path / "status.yml", {}) or {}
        cached_path = global_job_root / job.pair_job_id
        cached_status = read_yaml(cached_path / "status.yml", {}) or {}
        cached_result_path = cached_path / "result.json"
        same_identity = (
            str(status.get("decision_identity") or "") == decision_identity
        )
        cache_same_identity = (
            str(cached_status.get("decision_identity") or "")
            == decision_identity
        )
        if (
            str(status.get("status") or "") == "parked_for_review"
            and same_identity
            and not bool(getattr(request, "retry_terminal_failures", False))
        ):
            preparked.append(
                {
                    "pair_job_id": job.pair_job_id,
                    "source_id": job.left_source_id,
                    "target_source_id": job.right_source_id,
                    "reason": str(status.get("reason") or "terminal_pair_job_failure"),
                }
            )
        elif (
            (result_path.is_file() and same_identity)
            or (cached_result_path.is_file() and cache_same_identity)
        ):
            reusable_result_path = (
                result_path
                if result_path.is_file() and same_identity
                else cached_result_path
            )
            try:
                payload = json.loads(
                    reusable_result_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                unresolved.append(job)
                continue
            if isinstance(payload, Mapping):
                valid, validation = validate_cached_job(job, payload)
                if not valid:
                    preparked.extend(
                        {
                            **dict(row),
                            "pair_job_id": job.pair_job_id,
                            "source_id": job.left_source_id,
                            "target_source_id": job.right_source_id,
                        }
                        for row in [
                            *validation["needs_more_context"],
                            *validation["parked"],
                        ]
                    )
                    continue
                responses.append(
                    {
                        **dict(payload),
                        "reasoner_backend": str(
                            payload.get("reasoner_backend")
                            or status.get("reasoner_backend")
                            or reasoner_backend
                        ),
                        "provider": str(
                            payload.get("provider")
                            or status.get("provider")
                            or provider_name
                        ),
                        "model": str(
                            payload.get("model")
                            or status.get("model")
                            or model_name
                        ),
                    }
                )
                write_yaml(
                    job_path / "status.yml",
                    {
                        "pair_job_id": job.pair_job_id,
                        "status": "completed",
                        "decision_identity": decision_identity,
                        "reasoner_backend": str(
                            payload.get("reasoner_backend") or reasoner_backend
                        ),
                        "provider": str(payload.get("provider") or provider_name),
                        "model": str(payload.get("model") or model_name),
                        "checkpoint_hit": True,
                    },
                )
        else:
            unresolved.append(job)
    provider_batch_count = 0
    job_packets = _pack_relationship_rows(
        unresolved,
        pair_for=lambda job: (job.left_source_id, job.right_source_id),
        profile_by_source=profile_by_source,
        context_for=lambda packet: _relationship_transport_context(
            packet, decision_contract=decision_contract
        ),
        max_chars=catalogue_char_budget,
        max_rows=batch_max_jobs,
    )
    concurrent_batch_results: dict[
        tuple[str, ...], Mapping[str, Any] | BaseException
    ] = {}

    def adjudicate_packet(
        packet: Sequence[RelationshipPairJob],
    ) -> Mapping[str, Any]:
        packet_context = _relationship_transport_context(
            packet, decision_contract=decision_contract
        )
        packet_source_ids = sorted(
            {
                source_id
                for job in packet
                for source_id in (job.left_source_id, job.right_source_id)
            }
        )
        packet_profiles = (
            []
            if decision_contract == RELATIONSHIP_DECISION_CONTRACT
            else [profile_by_source[source_id] for source_id in packet_source_ids]
        )
        batch = RelationshipProviderBatch(
            pair_job_ids=[job.pair_job_id for job in packet],
            provider=str(getattr(reasoner, "name", "")),
            model=str(getattr(reasoner, "model", "")),
            capability_identity=str(
                capabilities.get("capability_identity") or ""
            ),
            serialized_context_fingerprint=stable_hash(
                [job.to_dict() for job in packet]
            ),
        )
        return reasoner_calls(
            "relationship_adjudication",
            batch.batch_id,
            "adjudicate_relationships",
            packet_profiles,
            packet_context,
        )

    runnable_packets = []
    for packet in job_packets:
        packet_context = _relationship_transport_context(
            packet, decision_contract=decision_contract
        )
        packet_source_ids = sorted(
            {
                source_id
                for job in packet
                for source_id in (job.left_source_id, job.right_source_id)
            }
        )
        packet_profiles = (
            []
            if decision_contract == RELATIONSHIP_DECISION_CONTRACT
            else [profile_by_source[source_id] for source_id in packet_source_ids]
        )
        if (
            _reasoner_packet_chars(
                [profile_to_dict(profile) for profile in packet_profiles],
                packet_context,
            )
            <= catalogue_char_budget
        ):
            runnable_packets.append(packet)
    if runnable_packets:
        relationship_started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=_provider_worker_count(
                request, len(runnable_packets)
            ),
            thread_name_prefix="auto-zettelkasten-relationship",
        ) as executor:
            future_map = {
                executor.submit(adjudicate_packet, packet): tuple(
                    job.pair_job_id for job in packet
                )
                for packet in runnable_packets
            }
            for future in as_completed(future_map):
                packet_key = future_map[future]
                try:
                    concurrent_batch_results[packet_key] = future.result()
                except BaseException as exc:
                    concurrent_batch_results[packet_key] = exc
        relationship_stage_seconds = round(
            time.monotonic() - relationship_started, 3
        )
    else:
        relationship_stage_seconds = 0.0
    for packet in job_packets:
        packet_context = _relationship_transport_context(
            packet, decision_contract=decision_contract
        )
        packet_source_ids = sorted(
            {
                source_id
                for job in packet
                for source_id in (job.left_source_id, job.right_source_id)
            }
        )
        packet_profiles = (
            []
            if decision_contract == RELATIONSHIP_DECISION_CONTRACT
            else [profile_by_source[source_id] for source_id in packet_source_ids]
        )
        if (
            _reasoner_packet_chars(
                [profile_to_dict(profile) for profile in packet_profiles],
                packet_context,
            )
            > catalogue_char_budget
        ):
            for job in packet:
                write_yaml(
                    job_root / job.pair_job_id / "status.yml",
                    {
                        "pair_job_id": job.pair_job_id,
                        "status": "parked_for_review",
                        "reason": "relationship_pair_job_exceeds_context_budget",
                        "decision_identity": decision_identity,
                    },
                )
                preparked.append(
                    {
                        "pair_job_id": job.pair_job_id,
                        "source_id": job.left_source_id,
                        "target_source_id": job.right_source_id,
                        "reason": "relationship_pair_job_exceeds_context_budget",
                    }
                )
            continue
        batch = RelationshipProviderBatch(
            pair_job_ids=[job.pair_job_id for job in packet],
            provider=str(getattr(reasoner, "name", "")),
            model=str(getattr(reasoner, "model", "")),
            capability_identity=str(capabilities.get("capability_identity") or ""),
            serialized_context_fingerprint=stable_hash(
                [job.to_dict() for job in packet]
            ),
        )
        batch_root = (
            workspace
            / "11_state"
            / "runs"
            / run_id
            / "relationship_batches"
            / batch.batch_id
        )
        write_yaml(
            batch_root / "batch.yml",
            {
                **batch.to_dict(),
                "status": "started",
            },
        )
        provider_batch_count += 1
        try:
            response_or_error = concurrent_batch_results.get(
                tuple(job.pair_job_id for job in packet)
            )
            if isinstance(response_or_error, BaseException):
                raise response_or_error
            if response_or_error is None:
                raise RuntimeError("relationship_batch_was_not_scheduled")
            response = response_or_error
            write_json(batch_root / "provider_result.json", dict(response))
            raw_decisions = response.get("decisions", []) or []
            if isinstance(raw_decisions, Mapping):
                raw_decisions = [
                    {**dict(value), "pair_job_id": str(job_id)}
                    if isinstance(value, Mapping)
                    else value
                    for job_id, value in raw_decisions.items()
                ]
            rows = [
                dict(row)
                for row in raw_decisions
                if isinstance(row, Mapping)
            ]
            by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                by_job[str(row.get("pair_job_id") or "")].append(row)
            for job in packet:
                job_rows = by_job.get(job.pair_job_id, [])
                status_path = job_root / job.pair_job_id / "status.yml"
                if len(job_rows) != 1:
                    reason = (
                        "provider_batch_missing_pair_row"
                        if not job_rows
                        else "duplicate_pair_job_decision"
                    )
                    preparked.append(
                        {
                            "pair_job_id": job.pair_job_id,
                            "source_id": job.left_source_id,
                            "target_source_id": job.right_source_id,
                            "status": "parked_for_review",
                            "reason": reason,
                        }
                    )
                    write_yaml(
                        status_path,
                        {
                            "pair_job_id": job.pair_job_id,
                            "status": "parked_for_review",
                            "reason": reason,
                            "decision_identity": decision_identity,
                        },
                    )
                    continue
                row = job_rows[0]
                row = {
                    **row,
                    "reasoner_backend": reasoner_backend,
                    "provider": provider_name,
                    "model": model_name,
                }
                write_json(
                    job_root / job.pair_job_id / "provider_result.json",
                    row,
                )
                write_json(
                    global_job_root
                    / job.pair_job_id
                    / "provider_result.json",
                    row,
                )
                valid, validation = validate_cached_job(job, row)
                if not valid:
                    reason_rows = [
                        *validation["needs_more_context"],
                        *validation["parked"],
                    ]
                    reason = ",".join(
                        sorted(
                            {
                                str(value.get("reason") or "")
                                for value in reason_rows
                                if str(value.get("reason") or "")
                            }
                        )
                    ) or "relationship_decision_needs_review"
                    preparked.append(
                        {
                            "pair_job_id": job.pair_job_id,
                            "source_id": job.left_source_id,
                            "target_source_id": job.right_source_id,
                            "status": "parked_for_review",
                            "reason": reason,
                        }
                    )
                    write_yaml(
                        status_path,
                        {
                            "pair_job_id": job.pair_job_id,
                            "status": "parked_for_review",
                            "reason": reason,
                            "decision_identity": decision_identity,
                        },
                    )
                    continue
                write_json(job_root / job.pair_job_id / "result.json", row)
                write_json(
                    global_job_root / job.pair_job_id / "result.json", row
                )
                write_yaml(
                    status_path,
                    {
                        "pair_job_id": job.pair_job_id,
                        "status": "completed",
                        "batch_id": batch.batch_id,
                        "decision_identity": decision_identity,
                        "reasoner_backend": reasoner_backend,
                        "provider": provider_name,
                        "model": model_name,
                    },
                )
                write_yaml(
                    global_job_root / job.pair_job_id / "status.yml",
                    {
                        "pair_job_id": job.pair_job_id,
                        "status": "completed",
                        "decision_identity": decision_identity,
                        "reasoner_backend": reasoner_backend,
                        "provider": provider_name,
                        "model": model_name,
                    },
                )
                responses.append(row)
            write_yaml(
                batch_root / "batch.yml",
                {**batch.to_dict(), "status": "completed"},
            )
        except Exception as exc:
            failure_class = _synthesis_failure_class(exc)
            retry_on_resume = failure_class == "transport"
            write_yaml(
                batch_root / "batch.yml",
                {
                    **batch.to_dict(),
                    "status": (
                        "pending" if retry_on_resume else "parked_for_review"
                    ),
                    "reason": f"{type(exc).__name__}:{exc}",
                    "retry_on_resume": retry_on_resume,
                },
            )
            for job in packet:
                preparked.append(
                    {
                        "pair_job_id": job.pair_job_id,
                        "source_id": job.left_source_id,
                        "target_source_id": job.right_source_id,
                        "status": (
                            "pending"
                            if retry_on_resume
                            else "parked_for_review"
                        ),
                        "reason": "provider_batch_failed",
                        "retry_on_resume": retry_on_resume,
                    }
                )
                write_yaml(
                    job_root / job.pair_job_id / "status.yml",
                    {
                        "pair_job_id": job.pair_job_id,
                        "status": (
                            "pending"
                            if retry_on_resume
                            else "parked_for_review"
                        ),
                        "reason": "provider_batch_failed",
                        "decision_identity": decision_identity,
                        "retry_on_resume": retry_on_resume,
                    },
                )
    validated = validate_relationship_decision_rows(
        {"decisions": responses},
        jobs=jobs,
        profiles=list(profile_by_source.values()),
        provider=provider_name,
        model=model_name,
        reasoner_backend=reasoner_backend,
        prompt_version=RELATIONSHIP_PROMPT_VERSION,
    )
    for row in [*validated["accepted"], *validated["no_relationship"]]:
        row["relationship_policy_identity"] = relationship_policy_identity
    preparked_job_ids = {
        str(row.get("pair_job_id") or "")
        for row in preparked
        if row.get("pair_job_id")
    }
    terminal_rows = [
        row
        for row in [*validated["needs_more_context"], *validated["parked"]]
        if str(row.get("pair_job_id") or "") not in preparked_job_ids
    ]
    for row in terminal_rows:
        pair_job_id = str(row.get("pair_job_id") or "")
        if not pair_job_id:
            continue
        write_yaml(
            job_root / pair_job_id / "status.yml",
            {
                "pair_job_id": pair_job_id,
                "status": "parked_for_review",
                "reason": str(
                    row.get("reason")
                    or "relationship_decision_needs_review"
                ),
                "decision_identity": decision_identity,
            },
        )
    accounted_job_ids = {
        str(row.get("pair_job_id") or "")
        for row in [
            *validated["accepted"],
            *validated["no_relationship"],
            *preparked,
            *terminal_rows,
        ]
        if row.get("pair_job_id")
    }
    retryable_relationship_failure = any(
        bool(row.get("retry_on_resume"))
        for row in [*preparked, *terminal_rows]
    )
    relationship_retry_on_resume = bool(
        retryable_relationship_failure
        or any(
            bool(row.get("retry_on_resume"))
            for row in discovery_parked
        )
    )
    selection_settled = bool(
        not relationship_retry_on_resume
        and (
            discovery_completed
            or discovery_terminal
            or not can_discover
        )
    )
    relationship_stage_complete = bool(
        discovery_usable
        and len(accounted_job_ids) == len(jobs)
        and not relationship_retry_on_resume
    )
    disposition_priority = {
        "selected_for_adjudication": 0,
        "already_visible": 1,
        "current_no_relationship": 2,
        "wrong_scope": 3,
        "deferred_capacity": 4,
        "parked_contract_failure": 5,
        "duplicate_merged": 6,
    }
    dispositions_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in candidate_dispositions:
        pair_values = list(raw.get("pair", []) or [])
        pair = canonical_pair(
            str(pair_values[0]) if pair_values else "",
            str(pair_values[1]) if len(pair_values) > 1 else "",
        )
        disposition = str(raw.get("disposition") or "parked_contract_failure")
        current = dispositions_by_pair.get(pair)
        if current is None:
            current = {
                **dict(raw),
                "pair": list(pair),
                "raw_candidate_count": 0,
                "observed_dispositions": [],
            }
            dispositions_by_pair[pair] = current
        current["raw_candidate_count"] += 1
        current["observed_dispositions"] = sorted(
            set(current["observed_dispositions"]) | {disposition}
        )
        if disposition_priority.get(disposition, 99) < disposition_priority.get(
            str(current.get("disposition") or ""), 99
        ):
            current["disposition"] = disposition
        if raw.get("reconsideration"):
            current["reconsideration"] = raw["reconsideration"]
    for raw in [
        *(bridge_payload.get("candidates", []) or []),
        *(general_payload.get("candidates", []) or []),
    ]:
        if not isinstance(raw, Mapping):
            continue
        job_ids = {
            str(value.get("discovery_job_id") or "")
            for value in raw.get("discovery_provenance", []) or []
            if isinstance(value, Mapping)
        } | {str(raw.get("discovery_job_id") or "")}
        job_ids &= set(discovery_job_accounting)
        if not job_ids:
            continue
        pair = canonical_pair(
            str(raw.get("left_source_id") or raw.get("source_id") or ""),
            str(
                raw.get("right_source_id")
                or raw.get("target_source_id")
                or raw.get("target_id")
                or ""
            ),
        )
        disposition = str(
            dispositions_by_pair.get(pair, {}).get("disposition")
            or raw.get("_candidate_disposition")
            or "parked_contract_failure"
        )
        for job_id in job_ids:
            counts = discovery_job_accounting[job_id].setdefault(
                "dispositions", {}
            )
            counts[disposition] = int(counts.get(disposition, 0) or 0) + 1
    return {
        "accepted": validated["accepted"],
        "no_relationship": validated["no_relationship"],
        "parked": [*preparked, *terminal_rows],
        "cluster_candidates": [],
        "selected_profile_hashes": (
            current_hashes if selection_settled else {}
        ),
        "selected_relationship_memory_hashes": (
            current_memory_hashes if selection_settled else {}
        ),
        "reconciled_catalogue_revision": (
            catalogue_revision if selection_settled else ""
        ),
        "selection_identity": selection_identity,
        "pair_job_count": len(jobs),
        "accounted_pair_job_count": len(accounted_job_ids),
        "relationship_stage_complete": relationship_stage_complete,
        "relationship_retry_on_resume": relationship_retry_on_resume,
        "relationship_discovery_status": discovery_status,
        "relationship_discovery_incomplete_jobs": sorted(
            {
                str(job_id)
                for row in discovery_parked
                for job_id in row.get("affected_job_ids", []) or []
                if str(job_id)
            }
            | {
                job_id
                for job_id, row in discovery_job_accounting.items()
                if row.get("status")
                not in {"completed", "insufficient_analytical_endpoints"}
            }
        ),
        "provider_batch_count": provider_batch_count,
        "candidate_dispositions": [
            dispositions_by_pair[pair] for pair in sorted(dispositions_by_pair)
        ],
        "relationship_discovery_jobs": [
            discovery_job_accounting[job_id]
            for job_id in sorted(discovery_job_accounting)
        ],
        "relationship_stage_wall_seconds": relationship_stage_seconds,
        "state_path": str(
            state_path
        ),
        "job_root": str(job_root),
    }


def _ranked_relationship_candidates(
    response: Mapping[str, Any],
    *,
    available_source_ids: set[str],
    entry_by_source: Mapping[str, Mapping[str, Any]],
    excluded_pairs: set[tuple[str, str]],
    maximum: int,
    bridge_fraction: float,
    scope: str = "all",
    dispositions: list[dict[str, Any]] | None = None,
    excluded_pair_reasons: Mapping[tuple[str, str], str] | None = None,
    job_floors: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    excluded_pair_reasons = excluded_pair_reasons or {}
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    within: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    for index, raw in enumerate(response.get("candidates", []) or []):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        source_id = str(
            row.get("left_source_id") or row.get("source_id") or ""
        )
        target_id = str(
            row.get("right_source_id")
            or row.get("target_id")
            or row.get("target_source_id")
            or ""
        )
        pair = canonical_pair(source_id, target_id)
        forced_disposition = str(row.get("_candidate_disposition") or "")
        if forced_disposition:
            if dispositions is not None:
                dispositions.append(
                    {"pair": list(pair), "disposition": forced_disposition}
                )
            continue
        if (
            source_id not in available_source_ids
            or target_id not in available_source_ids
            or source_id == target_id
        ):
            if dispositions is not None:
                dispositions.append(
                    {"pair": list(pair), "disposition": "parked_contract_failure"}
                )
            continue
        if pair in excluded_pairs:
            if dispositions is not None:
                dispositions.append(
                    {
                        "pair": list(pair),
                        "disposition": excluded_pair_reasons.get(
                            pair, "current_no_relationship"
                        ),
                    }
                )
            continue
        provenance = {
            key: row.get(key)
            for key in (
                "discovery_job_id",
                "discovery_family",
                "discovery_job_quota",
                "discovery_pass",
                "requested_collection_pair",
                "rank",
                "comparison_proposition",
                "why_compare",
            )
            if row.get(key) not in (None, "", [])
        }
        provenance_rows = [
            dict(value)
            for value in row.get("discovery_provenance", []) or []
            if isinstance(value, Mapping)
        ]
        if provenance and provenance not in provenance_rows:
            provenance_rows.append(provenance)
        left_collections = set(
            entry_by_source.get(source_id, {}).get("literature_ids", [])
            or entry_by_source.get(source_id, {}).get("collections", [])
            or []
        )
        right_collections = set(
            entry_by_source.get(target_id, {}).get("literature_ids", [])
            or entry_by_source.get(target_id, {}).get("collections", [])
            or []
        )
        bridge = bool(row.get("requested_collection_pair")) or (
            left_collections.isdisjoint(right_collections)
            if left_collections and right_collections
            else bool(
                row.get("cross_literature")
                or row.get("discovery_route") == "cross_literature_bridge"
            )
        )
        if scope == "bridge" and not bridge:
            if dispositions is not None:
                dispositions.append(
                    {"pair": list(pair), "disposition": "wrong_scope"}
                )
            continue
        if scope == "within" and bridge:
            if dispositions is not None:
                dispositions.append(
                    {"pair": list(pair), "disposition": "wrong_scope"}
                )
            continue
        if pair in seen:
            existing = seen[pair].setdefault("discovery_provenance", [])
            for value in provenance_rows:
                if value not in existing:
                    existing.append(value)
            if dispositions is not None:
                dispositions.append(
                    {"pair": list(pair), "disposition": "duplicate_merged"}
                )
            continue
        row["source_id"], row["target_id"] = pair
        try:
            row["_model_rank"] = int(
                row.get("rank") or row.get("priority") or index + 1
            )
        except (TypeError, ValueError):
            row["_model_rank"] = index + 1
        row["discovery_provenance"] = provenance_rows
        seen[pair] = row
        (bridges if bridge else within).append(row)

    def fair_take(rows: Sequence[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[
                str(row.get("discovery_job_id") or row.get("discovery_route") or "global")
            ].append(row)
        for values in buckets.values():
            values.sort(
                key=lambda value: (
                    value["_model_rank"],
                    value["source_id"],
                    value["target_id"],
                )
            )
        selected: list[dict[str, Any]] = []
        while len(selected) < count:
            progressed = False
            for key in sorted(buckets):
                if buckets[key] and len(selected) < count:
                    selected.append(buckets[key].pop(0))
                    progressed = True
            if not progressed:
                break
        return selected

    def floor_then_rank(
        rows: Sequence[dict[str, Any]], count: int
    ) -> list[dict[str, Any]]:
        if not job_floors:
            return fair_take(rows, count)
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            provenance = row.get("discovery_provenance", []) or []
            job_ids = {
                str(value.get("discovery_job_id") or "")
                for value in provenance
                if isinstance(value, Mapping)
                and str(value.get("discovery_job_id") or "") in job_floors
            } or {str(row.get("discovery_job_id") or "")}
            for job_id in job_ids:
                if job_id in job_floors:
                    buckets[job_id].append(row)
        for values in buckets.values():
            values.sort(
                key=lambda value: (
                    value["_model_rank"],
                    value["source_id"],
                    value["target_id"],
                )
            )
        selected: list[dict[str, Any]] = []
        selected_ids: set[int] = set()
        for round_index in range(max(job_floors.values(), default=0)):
            for job_id in sorted(job_floors):
                if (
                    round_index >= int(job_floors[job_id])
                    or len(selected) >= count
                ):
                    continue
                while buckets.get(job_id):
                    row = buckets[job_id].pop(0)
                    if id(row) in selected_ids:
                        continue
                    selected.append(row)
                    selected_ids.add(id(row))
                    break
        leftovers = sorted(
            (row for row in rows if id(row) not in selected_ids),
            key=lambda value: (
                value["_model_rank"],
                value["source_id"],
                value["target_id"],
            ),
        )
        selected.extend(leftovers[: max(0, count - len(selected))])
        return selected

    bridge_slots = min(len(bridges), int(maximum * bridge_fraction + 0.999))
    within_slots = min(len(within), maximum - bridge_slots)
    selected = floor_then_rank(bridges, bridge_slots) + floor_then_rank(
        within, within_slots
    )
    remaining = maximum - len(selected)
    if remaining:
        leftovers = [row for row in [*bridges, *within] if row not in selected]
        selected.extend(floor_then_rank(leftovers, remaining))
    selected_pairs = {
        canonical_pair(str(row["source_id"]), str(row["target_id"]))
        for row in selected
    }
    if dispositions is not None:
        dispositions.extend(
            {
                "pair": list(pair),
                "disposition": (
                    "selected_for_adjudication"
                    if pair in selected_pairs
                    else "deferred_capacity"
                ),
            }
            for pair in sorted(seen)
            if not any(
                row.get("pair") == list(pair)
                and row.get("disposition") in {"wrong_scope", "current_no_relationship"}
                for row in dispositions
            )
        )
    for row in selected:
        row.pop("_model_rank", None)
    return selected


def _selected_relationship_evidence(
    profile: Any,
    *,
    requested_ids: set[str],
) -> list[dict[str, Any]]:
    del requested_ids
    row = profile_to_dict(profile)
    anchors = [
        dict(anchor)
        for anchor in row.get("evidence_anchors", []) or []
        if isinstance(anchor, Mapping)
    ]
    anchors.sort(
        key=lambda anchor: (
            -int(anchor.get("salience_priority", 0) or 0),
            str(anchor.get("evidence_anchor_id") or ""),
        )
    )
    return anchors


def _relationship_neighbors(
    source_ids: Sequence[str], relations: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    focus = set(source_ids)
    return [
        dict(row)
        for row in relations
        if str(row.get("source_id") or "") in focus
        or str(row.get("target_source_id") or "") in focus
    ]


def _relationship_context_char_budget(reasoner: Any, request: Any) -> int:
    context_tokens = int(
        getattr(reasoner, "context_window_tokens", 0) or 128_000
    )
    prompt_reserve = int(
        getattr(reasoner, "prompt_reserve_tokens", 0) or 0
    )
    input_tokens = max(0, int(context_tokens * 0.65) - prompt_reserve)
    return max(8_000, input_tokens * 3)


def _relationship_evidence_projection(
    profile: Any,
    catalogue_entry: Mapping[str, Any],
    *,
    include_anchors: bool,
) -> EvidenceProfile:
    row = profile_to_dict(profile)
    anchors = row.get("evidence_anchors") or row.get("claims") or []
    compact_anchors = [
        EvidenceAnchor(
            evidence_anchor_id=str(
                anchor.get("evidence_anchor_id")
                or anchor.get("claim_id")
                or ""
            ),
            source_id=str(anchor.get("source_id") or row.get("source_id") or ""),
            claim=" ".join(
                str(
                    anchor.get("claim")
                    or anchor.get("proposition")
                    or anchor.get("finding")
                    or ""
                ).split()
            )[:360],
            locator=str(anchor.get("locator") or "")[:160],
            planning_roles=[
                str(value)[:80]
                for value in anchor.get("planning_roles", [])[:4]
                if str(value)
            ],
            salience_priority=int(anchor.get("salience_priority", 0) or 0),
        )
        for anchor in anchors
        if isinstance(anchor, Mapping)
        and (anchor.get("evidence_anchor_id") or anchor.get("claim_id"))
    ][:3]
    return EvidenceProfile(
        source_id=str(row.get("source_id") or ""),
        note_id=str(row.get("note_id") or ""),
        context={
            "title": str(
                catalogue_entry.get("title")
                or row.get("title")
                or ""
            ),
            "catalogue_entry": dict(catalogue_entry),
        },
        evidence_anchors=(
            compact_anchors if include_anchors else []
        ),
    )


def _compact_relationship_catalogue_entry(
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    def text(field: str, limit: int) -> str:
        return " ".join(str(entry.get(field) or "").split())[:limit]

    raw_facets_by_type = (
        entry.get("facets_by_type")
        if isinstance(entry.get("facets_by_type"), Mapping)
        else {}
    )
    facets_by_type = {
        facet_type: [
            " ".join(str(value).split())[:120]
            for value in raw_facets_by_type.get(facet_type, [])[:2]
            if str(value).strip()
        ]
        for facet_type in (
            "mechanism",
            "outcome",
            "case",
            "population",
            "period",
            "dataset",
        )
    }
    return {
        "source_id": text("source_id", 80),
        "zotero_key": text("zotero_key", 32),
        "title": text("title", 240),
        "author": text("author", 120),
        "year": text("year", 16),
        "thesis": text("thesis", 360),
        "method": text("method", 220),
        "source_scope": text("source_scope", 60),
        "evidence_coverage": text("evidence_coverage", 60),
        "facets": [
            " ".join(str(value).split())[:120]
            for value in entry.get("facets", [])[:6]
            if str(value).strip()
        ],
        "facets_by_type": {
            key: value for key, value in facets_by_type.items() if value
        },
        "collections": [
            " ".join(str(value).split())[:120]
            for value in entry.get("collections", [])[:2]
            if str(value).strip()
        ],
        "literature_ids": [
            " ".join(str(value).split())[:120]
            for value in entry.get("literature_ids", [])[:3]
            if str(value).strip()
        ],
    }


def _relationship_transport_context(
    jobs: Sequence[RelationshipPairJob],
    *,
    decision_contract: str,
) -> dict[str, Any]:
    if decision_contract not in {
        "relationship-decision-v6",
        "relationship-decision-v7",
        RELATIONSHIP_DECISION_CONTRACT,
    }:
        return {"pair_jobs": [job.to_dict() for job in jobs]}
    source_documents: dict[str, Any] = {}
    source_profiles: dict[str, Any] = {}
    source_evidence: dict[str, Any] = {}
    pair_jobs: list[dict[str, Any]] = []
    for job in jobs:
        row = job.to_dict()
        row.pop("prior_pair_memory", None)
        graph_context = dict(row.get("graph_context") or {})
        graph_context.pop("existing_neighbors", None)
        row["graph_context"] = graph_context
        profiles = row.pop("profiles", {})
        atomic_notes = row.pop("atomic_notes", {})
        selected_evidence = (
            row.pop("selected_evidence", {})
            if decision_contract
            in {"relationship-decision-v7", RELATIONSHIP_DECISION_CONTRACT}
            else {}
        )
        for side, source_id in (
            ("left", job.left_source_id),
            ("right", job.right_source_id),
        ):
            if source_id not in source_documents:
                source_documents[source_id] = dict(
                    atomic_notes.get(side)
                    or atomic_notes.get(source_id)
                    or {}
                )
            if source_id not in source_profiles:
                source_profiles[source_id] = dict(
                    profiles.get(side)
                    or profiles.get(source_id)
                    or {}
                )
            if (
                decision_contract
                in {"relationship-decision-v7", RELATIONSHIP_DECISION_CONTRACT}
                and source_id not in source_evidence
            ):
                source_evidence[source_id] = list(
                    selected_evidence.get(side)
                    or selected_evidence.get(source_id)
                    or []
                )
        pair_jobs.append(row)
    payload = {
        "source_documents": {
            key: source_documents[key] for key in sorted(source_documents)
        },
        "source_profiles": {
            key: source_profiles[key] for key in sorted(source_profiles)
        },
        "pair_jobs": pair_jobs,
    }
    if decision_contract in {
        "relationship-decision-v7",
        RELATIONSHIP_DECISION_CONTRACT,
    }:
        payload["source_evidence"] = {
            key: source_evidence[key] for key in sorted(source_evidence)
        }
    return payload


def _pack_relationship_rows(
    rows: Sequence[Any],
    *,
    pair_for: Any,
    profile_by_source: Mapping[str, Any],
    context_for: Any,
    max_chars: int,
    max_rows: int = 16,
) -> list[list[Any]]:
    packets: list[list[Any]] = []
    current: list[Any] = []
    for row in rows:
        candidate = [*current, row]
        pairs = [pair_for(value) for value in candidate]
        source_ids = sorted({source_id for pair in pairs for source_id in pair})
        profiles = [
            (
                dict(profile_by_source[source_id])
                if isinstance(profile_by_source[source_id], Mapping)
                else profile_to_dict(profile_by_source[source_id])
            )
            for source_id in source_ids
            if source_id in profile_by_source
        ]
        if current and (
            len(candidate) > max_rows
            or _reasoner_packet_chars(profiles, context_for(candidate))
            > max_chars
        ):
            packets.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        packets.append(current)
    return packets
def _commit_relationship_selection_state(
    workspace: Path,
    result: Mapping[str, Any],
    *,
    catalogue_revision: str,
) -> Path | None:
    if bool(result.get("relationship_retry_on_resume")):
        return None
    selected = dict(result.get("selected_profile_hashes", {}) or {})
    selected_memory = dict(
        result.get("selected_relationship_memory_hashes", {}) or {}
    )
    reconciled = str(result.get("reconciled_catalogue_revision") or "")
    selection_identity = str(result.get("selection_identity") or "")
    if not selected and not reconciled and not selection_identity:
        return None
    path = Path(
        str(
            result.get("state_path")
            or workspace
            / "02_source_memory"
            / "indexes"
            / "relationship_selection_state.yml"
        )
    )
    existing = read_yaml(path, {}) or {}
    profile_hashes = dict(existing.get("profile_hashes", {}) or {})
    profile_hashes.update(selected)
    relationship_memory_hashes = dict(
        existing.get("relationship_memory_hashes", {}) or {}
    )
    relationship_memory_hashes.update(selected_memory)
    payload = {
        "state_schema_version": "3",
        "profile_hashes": dict(sorted(profile_hashes.items())),
        "relationship_memory_hashes": dict(
            sorted(relationship_memory_hashes.items())
        ),
        "reconciled_catalogue_revision": reconciled
        or str(existing.get("reconciled_catalogue_revision") or ""),
        "catalogue_revision": catalogue_revision
        or str(existing.get("catalogue_revision") or ""),
        "selection_identity": selection_identity
        or str(existing.get("selection_identity") or ""),
    }
    if "candidate_dispositions" in result or "candidate_dispositions" in existing:
        payload["candidate_dispositions"] = list(
            result.get(
                "candidate_dispositions",
                existing.get("candidate_dispositions", []),
            )
            or []
        )
    if (
        "relationship_discovery_jobs" in result
        or "relationship_discovery_jobs" in existing
    ):
        payload["relationship_discovery_jobs"] = list(
            result.get(
                "relationship_discovery_jobs",
                existing.get("relationship_discovery_jobs", []),
            )
            or []
        )
    if (
        "relationship_stage_complete" in result
        or "relationship_stage_complete" in existing
    ):
        payload["relationship_stage_complete"] = bool(
            result.get(
                "relationship_stage_complete",
                existing.get("relationship_stage_complete", True),
            )
        )
        payload["relationship_retry_on_resume"] = bool(
            result.get(
                "relationship_retry_on_resume",
                existing.get("relationship_retry_on_resume", False),
            )
        )
    if (
        "relationship_discovery_status" in result
        or "relationship_discovery_status" in existing
    ):
        payload["relationship_discovery_status"] = str(
            result.get(
                "relationship_discovery_status",
                existing.get("relationship_discovery_status", "complete"),
            )
            or "complete"
        )
        payload["relationship_discovery_incomplete_jobs"] = sorted(
            str(value)
            for value in result.get(
                "relationship_discovery_incomplete_jobs",
                existing.get("relationship_discovery_incomplete_jobs", []),
            )
            or []
            if str(value)
        )
    if existing != payload:
        write_yaml(path, payload)
    return path


def _write_relationship_run_ledger(
    workspace: Path, run_id: str, result: Mapping[str, Any]
) -> Path:
    path = (
        run_directory(workspace, run_id)
        / "literature"
        / "relationships"
        / "parked.yml"
    )
    existing = read_yaml(path, {}) or {}
    registry = read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    events = {
        str(row.get("event_id") or ""): dict(row)
        for row in existing.get("events", []) or []
        if isinstance(row, Mapping) and row.get("event_id")
    }

    def merge_event(
        event_type: str,
        row: Mapping[str, Any],
        *,
        preserve_existing: bool = False,
    ) -> None:
        payload = dict(row)
        if event_type == "no_relationship" and not payload.get("decision_key"):
            payload["decision_key"] = relationship_decision_key(
                str(payload.get("source_id") or ""),
                str(payload.get("target_source_id") or ""),
                str(payload.get("source_profile_hash") or ""),
                str(payload.get("target_profile_hash") or ""),
                provider=str(payload.get("provider") or ""),
                model=str(payload.get("model") or ""),
                prompt_version=str(
                    payload.get("prompt_version") or RELATIONSHIP_PROMPT_VERSION
                ),
                policy_identity=str(
                    payload.get("relationship_policy_identity") or ""
                ),
            )
        event_id = _relationship_event_id(event_type, payload)
        if preserve_existing and event_id in events:
            return
        prior = events.get(event_id, {})
        history = {
            stable_hash(value): dict(value)
            for value in prior.get("payload_history", []) or []
            if isinstance(value, Mapping)
        }
        prior_payload = prior.get("payload")
        if isinstance(prior_payload, Mapping):
            history[stable_hash(prior_payload)] = dict(prior_payload)
        history[stable_hash(payload)] = payload
        events[event_id] = {
            "event_id": event_id,
            "event_type": event_type,
            "payload": payload,
            "payload_history": [history[key] for key in sorted(history)],
        }

    if not events:
        for row in existing.get("parked", []) or []:
            if isinstance(row, Mapping):
                merge_event("parked", row)
        for row in existing.get("cluster_candidates", []) or []:
            if isinstance(row, Mapping):
                merge_event("cluster_candidate", row)
        for relation_id in existing.get("accepted_relation_ids", []) or []:
            if str(relation_id):
                merge_event("accepted", {"relation_id": str(relation_id)})
        for decision_key in existing.get("no_relationship_decision_keys", []) or []:
            if str(decision_key):
                merge_event(
                    "no_relationship", {"decision_key": str(decision_key)}
                )

    for event_type, field_name in (
        ("accepted", "accepted"),
        ("no_relationship", "no_relationship"),
        ("parked", "parked"),
        ("cluster_candidate", "cluster_candidates"),
    ):
        for row in result.get(field_name, []) or []:
            if isinstance(row, Mapping):
                merge_event(event_type, row)
    for row in registry.get("pair_decisions", []) or []:
        if isinstance(row, Mapping) and row.get("status") == "no_relationship":
            merge_event("no_relationship", row, preserve_existing=True)
    for row in registry.get("relations", []) or []:
        if (
            isinstance(row, Mapping)
            and str(row.get("decision_status") or "") == "retired"
        ):
            merge_event("retired", row, preserve_existing=True)

    event_rows = [events[key] for key in sorted(events)]

    def event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = event.get("payload")
        return payload if isinstance(payload, Mapping) else {}

    accepted_relation_ids = sorted(
        {
            str(event_payload(event).get("relation_id") or "")
            for event in event_rows
            if event.get("event_type") == "accepted"
            and event_payload(event).get("relation_id")
        }
    )
    no_relationship_keys = sorted(
        {
            str(event_payload(event).get("decision_key") or "")
            for event in event_rows
            if event.get("event_type") == "no_relationship"
            and event_payload(event).get("decision_key")
        }
    )
    parked = [
        dict(event_payload(event))
        for event in event_rows
        if event.get("event_type") == "parked"
    ]
    cluster_candidates = [
        dict(event_payload(event))
        for event in event_rows
        if event.get("event_type") == "cluster_candidate"
    ]
    prior_count = int(existing.get("no_relationship_count", 0) or 0)
    prior_identified = len(existing.get("no_relationship_decision_keys", []) or [])
    legacy_unidentified = int(
        existing.get(
            "legacy_unidentified_no_relationship_count",
            max(0, prior_count - prior_identified),
        )
        or 0
    )
    payload = {
        "ledger_schema_version": "2",
        "events": event_rows,
        "parked": parked,
        "accepted_relation_ids": accepted_relation_ids,
        "no_relationship_decision_keys": no_relationship_keys,
        "legacy_unidentified_no_relationship_count": legacy_unidentified,
        "no_relationship_count": legacy_unidentified + len(no_relationship_keys),
        "cluster_candidates": cluster_candidates,
    }
    if existing != payload:
        write_yaml(path, payload)
    return path




def _relationship_event_id(event_type: str, row: Mapping[str, Any]) -> str:
    pair = sorted(
        str(value)
        for value in (
            row.get("source_id"),
            row.get("target_source_id") or row.get("target_id"),
        )
        if str(value or "")
    )
    reason = str(
        row.get("reason_code")
        or row.get("rejection_reason")
        or row.get("reason")
        or row.get("status")
        or ""
    )
    return stable_hash(
        {
            "event_type": event_type,
            "pair": pair,
            "source_ids": sorted(
                str(value)
                for value in row.get("source_ids", []) or []
                if str(value)
            ),
            "target_kind": str(row.get("target_kind") or ""),
            "relation_id": str(row.get("relation_id") or ""),
            "decision_key": str(row.get("decision_key") or ""),
            "provider": str(row.get("provider") or ""),
            "model": str(row.get("model") or ""),
            "prompt_version": str(row.get("prompt_version") or ""),
            "source_profile_hash": str(row.get("source_profile_hash") or ""),
            "target_profile_hash": str(row.get("target_profile_hash") or ""),
            "status": str(
                row.get("verification_status")
                or row.get("decision_status")
                or row.get("status")
                or ""
            ),
            "reason_code": reason.split(":", 1)[0].strip().replace(" ", "_"),
        }
    )


def _existing_gap_projection(
    frontmatter: Mapping[str, Any], body: str
) -> list[dict[str, str]]:
    gap_ids = [str(value) for value in frontmatter.get("gaps", []) or []]
    wikilinks = [str(value) for value in frontmatter.get("gap_links", []) or []]
    rows = []
    for index, gap_id in enumerate(gap_ids):
        wikilink = wikilinks[index] if index < len(wikilinks) else f"[[{gap_id}]]"
        relation_type = "gap"
        target = wikilink.split("|", 1)[0].removeprefix("[[")
        match = re.search(
            rf"^-\s+([^:]+):\s+{re.escape(wikilink)}\s*$|"
            rf"^-\s+([^:]+):\s+\[\[{re.escape(target)}(?:\|[^\]]+)?\]\]\s*$",
            body,
            flags=re.MULTILINE,
        )
        if match:
            relation_type = str(match.group(1) or match.group(2) or "gap")
        rows.append(
            {
                "gap_id": gap_id,
                "relation_type": relation_type,
                "wikilink": wikilink,
            }
        )
    return rows


def _project_atomic_graph(
    workspace: Path,
    *,
    note_rows: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]],
    navigation: Mapping[str, Any],
    navigation_policy: Any,
    clusters: Sequence[Mapping[str, Any]] = (),
    gaps: Sequence[Mapping[str, Any]] = (),
    cluster_scope_note_ids: Sequence[str] = (),
) -> list[Path]:
    profile_rows = [
        dict(profile) if isinstance(profile, Mapping) else profile_to_dict(profile)
        for profile in profiles
    ]
    note_stem_by_id = {
        str(row.get("note_id") or ""): Path(str(row.get("note_path") or "")).stem
        for row in note_rows
    }
    subject_tags_by_note: dict[str, list[str]] = defaultdict(list)
    for assignment in navigation.get("assignments", []) or []:
        if not isinstance(assignment, Mapping) or not assignment.get("visible"):
            continue
        note_id = str(assignment.get("note_id") or "")
        tag = str(assignment.get("canonical_tag") or "")
        if note_id and tag and tag not in subject_tags_by_note[note_id]:
            subject_tags_by_note[note_id].append(tag)
    cluster_by_id = {
        str(row.get("cluster_id") or ""): dict(row)
        for row in clusters
        if row.get("cluster_id")
    }
    clusters_by_note: dict[str, list[str]] = defaultdict(list)
    for cluster_id, cluster in cluster_by_id.items():
        for note_id in cluster.get("note_ids", []) or []:
            clusters_by_note[str(note_id)].append(cluster_id)
    gap_by_id = {
        str(row.get("gap_id") or ""): dict(row)
        for row in gaps
        if row.get("gap_id")
    }
    note_id_by_source = {
        str(row.get("source_id") or ""): str(row.get("note_id") or "")
        for row in note_rows
        if row.get("source_id") and row.get("note_id")
    }
    gaps_by_note: dict[str, list[dict[str, str]]] = defaultdict(list)

    def add_gap(source_id: Any, gap_id: str, relation_type: str) -> None:
        note_id = note_id_by_source.get(str(source_id or ""))
        row = {"gap_id": gap_id, "relation_type": relation_type}
        if note_id and row not in gaps_by_note[note_id]:
            gaps_by_note[note_id].append(row)

    for gap in gaps:
        gap_id = str(gap.get("gap_id") or "")
        for evidence in gap.get("supporting_evidence", []) or []:
            add_gap(evidence.get("source_id"), gap_id, "supports_gap_rule")
        for evidence in gap.get("countervailing_evidence", []) or []:
            add_gap(
                evidence.get("source_id"), gap_id, "countervailing_gap_evidence"
            )
    explicit_scope = set(cluster_scope_note_ids)
    paths: list[Path] = []
    for row in note_rows:
        path = workspace / str(row.get("note_path") or "")
        if not path.is_file():
            continue
        note_id = str(row.get("note_id") or "")
        source_id = str(row.get("source_id") or "")
        related_links = [
            {
                "relation_id": str(link.get("relation_id") or ""),
                "note_id": str(link.get("target_note_id") or ""),
                "relation_type": str(
                    link.get("primary_relation_type") or "semantic_similarity"
                ),
                "reason": str(link.get("reason") or ""),
                "target_stem": note_stem_by_id.get(
                    str(link.get("target_note_id") or ""),
                    str(link.get("target_note_id") or ""),
                ),
            }
            for link in projected_related_links(
                source_id,
                profile_rows,
                relations,
                max_inferred_links=int(
                    getattr(
                        navigation_policy,
                        "max_inferred_related_note_links",
                        8,
                    )
                ),
            )
            if link.get("target_note_id")
        ]
        current = read_note(path)
        front = current["frontmatter"]
        if note_id in explicit_scope:
            cluster_ids = sorted(clusters_by_note.get(note_id, []))
            cluster_wikilinks = {
                cluster_id: (
                    f"[[{cluster_note_stem(cluster_by_id[cluster_id])}|"
                    f"{cluster_display_title(cluster_by_id[cluster_id])}]]"
                )
                for cluster_id in cluster_ids
                if cluster_id in cluster_by_id
            }
            note_gap_links = [
                {
                    **link,
                    "wikilink": (
                        f"[[{gap_note_stem(gap_by_id[link['gap_id']])}|"
                        f"{gap_display_title(gap_by_id[link['gap_id']])}]]"
                    ),
                }
                for link in sorted(
                    gaps_by_note.get(note_id, []),
                    key=lambda value: (
                        value["gap_id"],
                        value["relation_type"],
                    ),
                )
                if link["gap_id"] in gap_by_id
            ]
            tags = sorted(subject_tags_by_note.get(note_id, []))
        else:
            cluster_ids = [str(value) for value in front.get("clusters", []) or []]
            cluster_links = [
                str(value) for value in front.get("cluster_links", []) or []
            ]
            cluster_wikilinks = {
                cluster_id: (
                    cluster_links[index]
                    if index < len(cluster_links)
                    else f"[[{cluster_id}]]"
                )
                for index, cluster_id in enumerate(cluster_ids)
            }
            note_gap_links = _existing_gap_projection(
                front, str(current.get("body") or "")
            )
            tags = list(front.get("tags", []) or [])
        gap_ids = sorted({link["gap_id"] for link in note_gap_links})
        gap_wikilinks = {
            link["gap_id"]: str(link["wikilink"]) for link in note_gap_links
        }
        try:
            update_note_graph(
                path,
                {
                "related_notes": [
                    {
                        "relation_id": link.get("relation_id", ""),
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
                "gap_links": [
                    gap_wikilinks[gap_id]
                    for gap_id in gap_ids
                    if gap_id in gap_wikilinks
                ],
                "tags": tags,
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "updated_at": now_iso(),
                },
                related_links,
                cluster_ids,
                note_gap_links,
                cluster_wikilinks,
            )
        except ValueError as exc:
            _park_projection_failure(
                workspace,
                artifact_kind="atomic_note",
                artifact_id=note_id,
                path=path,
                reason=str(exc),
            )
            continue
        _resolve_projection_failure(
            workspace, artifact_kind="atomic_note", artifact_id=note_id
        )
        paths.append(path)
    return paths


def _park_projection_failure(
    workspace: Path,
    *,
    artifact_kind: str,
    artifact_id: str,
    path: Path,
    reason: str,
) -> None:
    ledger_path = workspace / "11_state" / "projection_failures.yml"
    existing = read_yaml(ledger_path, {}) or {}
    failures = {
        str(row.get("event_id") or ""): dict(row)
        for row in existing.get("failures", []) or []
        if isinstance(row, Mapping) and row.get("event_id")
    }
    event = {
        "artifact_kind": artifact_kind,
        "artifact_id": artifact_id,
        "path": str(path),
        "content_hash": sha256_file(path) if path.is_file() else "",
        "reason": reason,
    }
    event_id = stable_hash(event)
    failures[event_id] = {"event_id": event_id, **event}
    write_yaml(
        ledger_path,
        {
            "projection_failure_schema_version": "1",
            "failures": [failures[key] for key in sorted(failures)],
            "resolved_failures": list(existing.get("resolved_failures", []) or []),
        },
    )


def _resolve_projection_failure(
    workspace: Path, *, artifact_kind: str, artifact_id: str
) -> None:
    ledger_path = workspace / "11_state" / "projection_failures.yml"
    existing = read_yaml(ledger_path, {}) or {}
    active = [
        dict(row)
        for row in existing.get("failures", []) or []
        if isinstance(row, Mapping)
    ]
    resolved_now = [
        row
        for row in active
        if row.get("artifact_kind") == artifact_kind
        and row.get("artifact_id") == artifact_id
    ]
    if not resolved_now:
        return
    active = [row for row in active if row not in resolved_now]
    resolved = {
        str(row.get("event_id") or ""): dict(row)
        for row in existing.get("resolved_failures", []) or []
        if isinstance(row, Mapping) and row.get("event_id")
    }
    for row in resolved_now:
        resolved[str(row["event_id"])] = {**row, "resolved_at": now_iso()}
    write_yaml(
        ledger_path,
        {
            "projection_failure_schema_version": "1",
            "failures": active,
            "resolved_failures": [resolved[key] for key in sorted(resolved)],
        },
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
    profile_budget: _ProfileProviderBudget | None = None,
    collection_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    del (
        external_discovery
    )  # Auto-Zettelkasten 0.4 maps only the frozen internal collection.
    effective_request = request or MapRequest(
        workspace=workspace, provider="ollama", model="deterministic-v1"
    )
    collection_snapshot = collection_snapshot or read_yaml(
        workspace / "01_custody" / "zotero" / "collection_snapshot.yml",
        {},
    )
    if not effective_request.literature_policy.synthesis_enabled:
        workspace_note_rows, workspace_profiles = _workspace_graph_inputs(
            workspace, []
        )
        orphaned_source_ids = _orphaned_source_ids(
            workspace_note_rows, collection_snapshot
        )
        relations = build_typed_source_relations(
            note_rows,
            max_inferred_links_per_source=effective_request.navigation_policy.max_inferred_related_note_links,
        )
        typed = {
            **persist_relationship_registry(
                workspace,
                structural_relations=relations,
                orphaned_source_ids=orphaned_source_ids,
            )
        }
        graph_profiles: Sequence[Any] = (
            workspace_profiles or workspace_note_rows
        )
        note_paths = _project_atomic_graph(
            workspace,
            note_rows=workspace_note_rows,
            profiles=graph_profiles,
            relations=typed.get("links", []) or [],
            navigation={"typed_relations": relations, "assignments": []},
            navigation_policy=effective_request.navigation_policy,
        )
        catalogue = build_source_catalogue(
            workspace,
            workspace_profiles,
            workspace_note_rows,
            _cluster_catalogue_rows(workspace),
            collection_snapshot=collection_snapshot,
        )
        source_index = Path(catalogue["master_index_path"])
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
                Path(catalogue["catalogue_path"]),
                Path(catalogue["cluster_catalogue_path"]),
                Path(catalogue["cluster_index_path"]),
                *(Path(path) for path in catalogue.get("shard_paths", []) or []),
                *note_paths,
            ],
        }
    migration = migrate_workspace(workspace)
    if profile_budget is None:
        profile_budget = _ProfileProviderBudget(
            run_directory(workspace, run_id)
            / "literature"
            / "profiles"
            / "provider_usage.yml",
            effective_request.literature_policy.max_profile_calls,
        )
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
            profile_budget=profile_budget,
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
    workspace_note_rows, workspace_profiles = _workspace_graph_inputs(
        workspace, profiles
    )
    global_source_ids = sorted(
        {
            str(row.get("source_id") or "")
            for row in workspace_note_rows
            if row.get("source_id")
        }
    )
    global_source_set = {
        "source_set_id": "source-set-auto-zettelkasten-workspace",
        "source_set_type": "auto_zettelkasten_workspace",
        "scope": "workspace",
        "source_ids": global_source_ids,
        "note_ids": sorted(
            str(row.get("note_id") or "")
            for row in workspace_note_rows
            if row.get("note_id")
        ),
        "dependency_hash": stable_hash(global_source_ids),
    }
    global_map_id = stable_literature_map_id(global_source_set)
    base_literature_request = LiteratureMapRequest(
        workspace=workspace,
        source_set_id=str(global_source_set["source_set_id"]),
        run_id=run_id,
        map_id=global_map_id,
        question=None,
        provider=effective_request.provider,
        model=effective_request.model,
        allow_cloud=effective_request.allow_cloud,
        provider_concurrency=(
            effective_request.provider_concurrency or effective_request.parallel
        ),
        comparison_collection_keys=list(
            source_set.get("comparison_collection_keys", []) or []
        ),
        literature_policy=effective_request.literature_policy,
    )
    reasoner_calls = (
        _CheckpointedReasonerCalls(
            workspace,
            run_id,
            reasoner,
            base_literature_request,
            stage_callback=(
                progress.set_stage if progress is not None else None
            ),
            retry_terminal_failures=effective_request.retry_terminal_failures,
        )
        if reasoner is not None
        else None
    )
    orphaned_source_ids = _orphaned_source_ids(
        workspace_note_rows, collection_snapshot
    )
    existing_clusters = _cluster_catalogue_rows(workspace)
    navigation = build_navigation_projection(
        workspace,
        workspace_profiles,
        workspace_note_rows,
        navigation_policy=effective_request.navigation_policy,
    )
    citation_relations = _literature_position_relations(
        workspace, workspace_profiles
    )
    persist_relationship_registry(
        workspace,
        structural_relations=[
            *(navigation.get("typed_relations", []) or []),
            *citation_relations,
        ],
        preserve_unmentioned_structural=True,
        orphaned_source_ids=orphaned_source_ids,
    )
    catalogue = build_source_catalogue(
        workspace,
        workspace_profiles,
        workspace_note_rows,
        existing_clusters,
        collection_snapshot=collection_snapshot,
    )
    catalogue_payload = read_yaml(Path(str(catalogue["catalogue_path"])), {}) or {}
    collection_rows = [
        dict(row)
        for row in catalogue_payload.get("collections", []) or []
        if isinstance(row, Mapping)
    ]
    global_source_set["collection_memberships"] = {
        str(source_id): sorted(
            str(row.get("name") or row.get("key") or "")
            for row in collection_rows
            if str(source_id) in set(row.get("direct_source_ids", []) or [])
        )
        for source_id in global_source_ids
    }
    global_source_set["collection_views"] = [
        {
            "collection_key": str(row.get("key") or ""),
            "name": str(row.get("name") or ""),
            "parent_key": str(row.get("parent_key") or ""),
            "child_keys": list(row.get("child_keys", []) or []),
            "direct_source_ids": list(row.get("direct_source_ids", []) or []),
        }
        for row in collection_rows
    ]
    try:
        shared_family_plan = _plan_literature_families(
            workspace,
            profiles=workspace_profiles,
            catalogue=catalogue,
            reasoner=reasoner,
            reasoner_calls=reasoner_calls,
            request=base_literature_request,
        )
        relationship_result = _run_relationship_reasoning(
            workspace,
            profiles=workspace_profiles,
            source_set=global_source_set,
            catalogue=catalogue,
            reasoner=reasoner,
            reasoner_calls=reasoner_calls,
            request=base_literature_request,
            shared_family_plan=shared_family_plan,
        )
    except Exception as exc:
        failure_reason = (
            "literature_family_planning_failure"
            if "shared_family_plan" not in locals()
            else "relationship_stage_failure"
        )
        relationship_result = {
            "accepted": [],
            "no_relationship": [],
            "parked": [
                {
                    "reason": failure_reason,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "retry_on_resume": _synthesis_failure_class(exc) == "transport",
                }
            ],
            "cluster_candidates": [],
            "selected_profile_hashes": {},
            "reconciled_catalogue_revision": "",
            "relationship_stage_complete": False,
            "relationship_retry_on_resume": (
                _synthesis_failure_class(exc) == "transport"
            ),
            "planning_failed": failure_reason
            == "literature_family_planning_failure",
        }
    relationship_ledger_path = _write_relationship_run_ledger(
        workspace, run_id, relationship_result
    )
    if progress is not None:
        progress.update_literature(
            relationship_stage_wall_seconds=float(
                relationship_result.get("relationship_stage_wall_seconds", 0.0)
                or 0.0
            )
        )
    typed = persist_relationship_registry(
        workspace,
        structural_relations=navigation.get("typed_relations", []) or [],
        accepted_relations=relationship_result.get("accepted", []) or [],
        no_relationship_decisions=relationship_result.get(
            "no_relationship", []
        )
        or [],
        parked_rows=relationship_result.get("parked", []) or [],
        preserve_unmentioned_structural=True,
        orphaned_source_ids=orphaned_source_ids,
        reconcile_machine_prompt_version=(
            None
            if relationship_result.get("planning_failed")
            else RELATIONSHIP_PROMPT_VERSION
        ),
    )
    global_source_set["rejected_pair_memory"] = [
        {
            "source_id": str(row.get("source_id") or row.get("left_source_id") or ""),
            "target_source_id": str(
                row.get("target_source_id") or row.get("right_source_id") or ""
            ),
        }
        for row in typed.get("pair_decisions", []) or []
        if isinstance(row, Mapping)
        and str(
            row.get("status")
            or row.get("decision_status")
            or row.get("decision")
            or ""
        )
        == "no_relationship"
    ]
    graph_note_paths = _project_atomic_graph(
        workspace,
        note_rows=workspace_note_rows,
        profiles=workspace_profiles,
        relations=typed.get("links", []) or [],
        navigation=navigation,
        navigation_policy=effective_request.navigation_policy,
    )
    selection_state_path = _commit_relationship_selection_state(
        workspace,
        relationship_result,
        catalogue_revision=str(
            relationship_result.get("reconciled_catalogue_revision")
            or ""
        ),
    )
    graph_paths = [
        Path(typed["path"]),
        Path(typed["compatibility_path"]),
        Path(catalogue["catalogue_path"]),
        Path(catalogue["master_index_path"]),
        Path(catalogue["cluster_catalogue_path"]),
        Path(catalogue["cluster_index_path"]),
        *(Path(path) for path in catalogue.get("shard_paths", []) or []),
        relationship_ledger_path,
        *graph_note_paths,
        *([selection_state_path] if selection_state_path is not None else []),
    ]
    if not bool(
        relationship_result.get("relationship_stage_complete", True)
    ):
        retry_on_resume = bool(
            relationship_result.get("relationship_retry_on_resume")
        )
        reason = (
            "relationship_stage_partial:retryable_incomplete_pair_jobs"
            if retry_on_resume
            else "relationship_stage_partial:terminal_incomplete_relationships"
        )
        if progress is not None:
            progress.update_literature(literature_failure_count=1)
        preserved_clusters, refresh_paths = (
            _preserve_last_valid_clusters_on_refresh_failure(
                workspace,
                global_map_id,
                reason,
            )
        )
        return {
            "source_set": dict(source_set),
            "cluster_map": {
                "status": "partial",
                "clusters": preserved_clusters,
                "relations": [],
                "unclustered_sources": [],
                "refresh_pending_cluster_count": len(preserved_clusters),
                "planning_refresh_pending": bool(
                    relationship_result.get("planning_failed")
                ),
            },
            "gap_map": {
                "status": "partial",
                "gap_candidates": [],
                "novelty_claimed": False,
            },
            "literature_packet": {
                "status": "partial",
                "reason": reason,
                "retry_on_resume": retry_on_resume,
                "synthesis_call_count": int(
                    getattr(reasoner_calls, "cumulative_provider_calls", 0)
                    or 0
                ),
                "synthesis_new_call_count": int(
                    getattr(reasoner_calls, "provider_calls", 0) or 0
                ),
                "synthesis_checkpoint_hit_count": int(
                    getattr(reasoner_calls, "checkpoint_hits", 0) or 0
                ),
                "synthesis_failure_count": int(
                    getattr(reasoner_calls, "failures", 0) or 0
                ),
            },
            "typed_links": typed,
            "relationship_result": relationship_result,
            "profiles": profiles,
            "profile_result": {
                key: value
                for key, value in profile_result.items()
                if key != "profiles"
            },
            "migration": migration,
            "partial_reason": reason,
            "paths": [
                *graph_paths,
                *profile_result["paths"],
                *_existing_source_set_paths(workspace, source_set),
                *refresh_paths,
            ],
        }
    literature_note_rows = list(workspace_note_rows)
    literature_profiles = list(workspace_profiles)
    try:
        cluster_map, gap_map, packet, paths = build_literature_map(
            workspace,
            source_set=global_source_set,
            notes=literature_note_rows,
            question=question,
            run_id=run_id,
            profiles=literature_profiles,
            request=base_literature_request,
            policy=effective_request.literature_policy,
            reasoner=reasoner,
            stage_callback=(progress.set_stage if progress is not None else None),
            navigation_policy=effective_request.navigation_policy,
            reasoner_calls=reasoner_calls,
            accepted_relationships=typed.get("links", []) or [],
            relationship_candidates=relationship_result.get(
                "cluster_candidates", []
            )
            or [],
            shared_literature_plan=shared_family_plan,
            catalogue_shards=[
                *[
                    {
                        "literature_id": (
                            "collection-"
                            + str(row.get("key") or "unfiled").casefold()
                        ),
                        "shard_id": (
                            "collection-"
                            + str(row.get("key") or "unfiled").casefold()
                        ),
                        "source_ids": list(
                            row.get("direct_source_ids", []) or []
                        ),
                    }
                    for row in collection_rows
                    if row.get("direct_source_ids")
                ],
                *list(catalogue_payload.get("shards", []) or []),
                *list(catalogue_payload.get("virtual_shards", []) or []),
            ],
        )
        acquisition_ledger_path = _reconcile_cluster_acquisition_recommendations(
            workspace,
            cluster_map.get("cluster_syntheses", {}) or {},
        )
        if acquisition_ledger_path is not None:
            paths = [*paths, acquisition_ledger_path]
    except Exception as exc:
        reason = f"literature_synthesis_partial:{type(exc).__name__}:{exc}"
        retry_on_resume = _synthesis_failure_class(exc) == "transport"
        if profile_partial_reason:
            reason = f"{profile_partial_reason};{reason}"
        preserved_clusters, refresh_paths = (
            _preserve_last_valid_clusters_on_refresh_failure(
                workspace,
                global_map_id,
                reason,
            )
        )
        synthesis_calls = int(
            getattr(reasoner_calls, "cumulative_provider_calls", 0) or 0
        )
        synthesis_new_calls = int(
            getattr(reasoner_calls, "provider_calls", 0) or 0
        )
        synthesis_hits = int(
            getattr(reasoner_calls, "checkpoint_hits", 0) or 0
        )
        synthesis_failures = int(
            getattr(reasoner_calls, "failures", 0) or 0
        )
        if progress is not None:
            progress.update_literature(
                synthesis_call_count=synthesis_calls,
                synthesis_new_call_count=synthesis_new_calls,
                synthesis_checkpoint_hit_count=synthesis_hits,
                synthesis_failure_count=synthesis_failures,
                literature_provider_call_count=int(
                    profile_result.get("provider_calls", 0) or 0
                )
                + synthesis_calls,
                checkpoint_hit_count=int(
                    profile_result.get("checkpoint_hits", 0) or 0
                )
                + synthesis_hits,
                literature_failure_count=int(
                    profile_result.get("failure_count", 0) or 0
                )
                + synthesis_failures,
            )
        return {
            "source_set": dict(source_set),
            "cluster_map": {
                "status": "partial",
                "clusters": preserved_clusters,
                "relations": [],
                "unclustered_sources": [],
                "refresh_pending_cluster_count": len(preserved_clusters),
            },
            "gap_map": {
                "status": "partial",
                "gap_candidates": [],
                "novelty_claimed": False,
            },
            "literature_packet": {
                "status": "partial",
                "reason": reason,
                "retry_on_resume": retry_on_resume,
                "synthesis_call_count": synthesis_calls,
                "synthesis_new_call_count": synthesis_new_calls,
                "synthesis_checkpoint_hit_count": synthesis_hits,
                "synthesis_failure_count": synthesis_failures,
            },
            "typed_links": typed,
            "relationship_result": relationship_result,
            "profiles": profiles,
            "profile_result": {
                key: value for key, value in profile_result.items() if key != "profiles"
            },
            "migration": migration,
            "partial_reason": reason,
            "paths": [
                *graph_paths,
                *profile_result["paths"],
                *_existing_source_set_paths(workspace, source_set),
                *refresh_paths,
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
    combined_structural = {
        str(
            row.get("relation_id")
            or row.get("link_id")
            or stable_hash(row)
        ): dict(row)
        for row in [
            *(navigation.get("typed_relations", []) or []),
            *(
                build_navigation_projection(
                    workspace,
                    workspace_profiles,
                    workspace_note_rows,
                    navigation_policy=effective_request.navigation_policy,
                ).get("typed_relations", [])
                or []
            ),
            *_cluster_membership_relations(
                cluster_map.get("clusters", []) or [],
                workspace_profiles,
            ),
        ]
        if isinstance(row, Mapping)
    }
    typed = persist_relationship_registry(
        workspace,
        structural_relations=combined_structural.values(),
        orphaned_source_ids=orphaned_source_ids,
    )
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
            coverage_parked_for_review_count=int(
                cluster_map.get("coverage_parked_for_review_count", 0) or 0
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
    current_note_ids = [
        str(row.get("note_id") or "")
        for row in workspace_note_rows
        if row.get("note_id")
    ]
    catalogue_clusters = {
        str(cluster.get("cluster_id") or ""): dict(cluster)
        for cluster in cluster_map.get("clusters", []) or []
        if isinstance(cluster, Mapping) and cluster.get("cluster_id")
    }
    catalogue = build_source_catalogue(
        workspace,
        workspace_profiles,
        workspace_note_rows,
        catalogue_clusters.values(),
        collection_snapshot=collection_snapshot,
    )
    note_paths = _project_atomic_graph(
        workspace,
        note_rows=workspace_note_rows,
        profiles=workspace_profiles,
        relations=typed.get("links", []) or [],
        navigation=navigation,
        navigation_policy=effective_request.navigation_policy,
        clusters=cluster_map.get("clusters", []) or [],
        gaps=gap_map.get("gap_candidates", []) or [],
        cluster_scope_note_ids=current_note_ids,
    )
    source_index = Path(catalogue["master_index_path"])
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
        request=effective_request,
    )
    result_paths = list(
        dict.fromkeys(
            [
                *graph_paths,
                Path(typed["path"]),
                Path(typed["compatibility_path"]),
                source_index,
                Path(catalogue["catalogue_path"]),
                Path(catalogue["cluster_catalogue_path"]),
                Path(catalogue["cluster_index_path"]),
                *(Path(path) for path in catalogue.get("shard_paths", []) or []),
                *note_paths,
                *profile_result["paths"],
                *paths,
                Path(source_set["path"]),
                Path(source_set.get("latest_path", source_set["path"])),
            ]
        )
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
        "relationship_result": relationship_result,
        "paths": result_paths,
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
    profile_budget: _ProfileProviderBudget,
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
    def reserve_provider_call(key: str, fingerprint: str) -> str:
        nonlocal provider_calls
        attempt_id = profile_budget.reserve("profile_source", key, fingerprint)
        provider_calls += 1
        current = provider_calls
        if progress is not None:
            progress.update_literature(literature_provider_call_count=current)
        return attempt_id

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
        text = internal_note_text(path)
        note_id = str(row.get("note_id") or "")
        failure_path = (
            literature_state
            / "profile_failures"
            / f"{slugify(note_id, fallback='profile')}.yml"
        )
        prior_profile_failure = read_yaml(failure_path, {}) or {}
        note_status = str(row.get("note_status") or "")
        terminal_status = str(row.get("terminal_status") or "")
        analytical = (
            note_status
            in {
                "analytical_atomic_note",
                "verified_atomic_note",
                "partial_document_atomic_note",
            }
            or terminal_status == "validated_note"
        )
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
        reasoner_attempt_fingerprint = fingerprint
        profile_contract_fallback = False
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
                existing_context = (
                    dict(existing_payload.get("context", {}))
                    if isinstance(existing_payload.get("context", {}), Mapping)
                    else {}
                )
                if (
                    existing_context.get("profile_generation_route")
                    == "source_analysis_bundle"
                    and (
                        workspace
                        / str(
                            existing_context.get("source_analysis_bundle_path") or ""
                        )
                    ).is_file()
                ):
                    profile = existing
                    checkpoint_hit = 1
                existing_dependency = str(existing_payload.get("dependency_hash") or "")
                if profile is None and existing_dependency == fingerprint:
                    profile = existing
                    checkpoint_hit = 1
                elif profile is None:
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
            if (
                profile is not None
                and profile_route != "deterministic"
                and reasoner is not None
                and analytical
            ):
                cached_context = dict(getattr(profile, "context", {}) or {})
                cached_retryable = bool(
                    cached_context.get("lazy_reprofile_required")
                )
                cached_terminal = bool(
                    cached_context.get("profile_retry_terminal")
                )
                if cached_retryable or cached_terminal:
                    # A contract-fallback profile is a progress-preserving placeholder,
                    # not a successful profile checkpoint. Once a reasoner is available,
                    # retry from the committed note instead of mechanically blessing the
                    # omnibus support-unknown anchor under a newer prompt version.
                    terminal_retry = bool(
                        isinstance(prior_profile_failure, Mapping)
                        and prior_profile_failure.get("fingerprint")
                        == reasoner_attempt_fingerprint
                        and prior_profile_failure.get("terminal")
                    )
                    profile_contract_fallback = True
                    if (
                        cached_retryable
                        and not terminal_retry
                        or request.retry_terminal_failures
                    ):
                        profile = None
                        checkpoint_hit = 0
                        mechanically_upgraded = False
                        profile_contract_fallback = False
            if profile is None:
                if (
                    profile_route != "deterministic"
                    and reasoner is not None
                    and analytical
                ):
                    profile_attempt_id = reserve_provider_call(
                        note_id, reasoner_attempt_fingerprint
                    )

                    def reasoner_method(
                        prompt: str, *, _row: Mapping[str, Any] = row, _text: str = text
                    ) -> Any:
                        try:
                            response = reasoner.profile_source(
                                {
                                    **dict(_row),
                                    "committed_note": _text,
                                    "profile_prompt": prompt,
                                },
                                question=None,
                                context={
                                    "source_set_id": source_set.get(
                                        "source_set_id", ""
                                    ),
                                    "profile_prompt_version": PROFILE_PROMPT_VERSION,
                                    "profile_generation_route": profile_route,
                                    "reasoner_identity": reasoner_identity,
                                },
                            )
                        except Exception:
                            profile_budget.finish(
                                profile_attempt_id, status="failed"
                            )
                            raise
                        profile_budget.finish(
                            profile_attempt_id, status="completed"
                        )
                        return response
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
                    profile.evidence_eligibility = "context_only"
                    profile.excluded_from_synthesis = True
                    profile.exclusion_reason = (
                        "profile_reasoner_contract_fallback_requires_lazy_reprofile"
                    )
                    fallback_context = dict(getattr(profile, "context", {}) or {})
                    fallback_context.update(
                        profile_generation_route=profile_route,
                        reasoner_identity=reasoner_identity,
                        profile_fallback_reason=f"{type(exc).__name__}: {exc}",
                    )
                    prior_attempt_count = (
                        int(prior_profile_failure.get("attempt_count", 0) or 0)
                        if isinstance(prior_profile_failure, Mapping)
                        and prior_profile_failure.get("fingerprint")
                        == reasoner_attempt_fingerprint
                        else 0
                    )
                    attempt_count = prior_attempt_count + 1
                    terminal = attempt_count >= 2
                    fallback_context.update(
                        lazy_reprofile_required=not terminal,
                        profile_retry_terminal=terminal,
                    )
                    profile.context = fallback_context
                    profile_contract_fallback = True
                    write_yaml(
                        failure_path,
                        {
                            "failure_schema_version": "2",
                            "status": "terminal" if terminal else "retryable",
                            "note_id": note_id,
                            "source_id": str(row.get("source_id") or ""),
                            "zotero_item_key": str(
                                row.get("zotero_item_key") or ""
                            ),
                            "fingerprint": reasoner_attempt_fingerprint,
                            "failure_class": "contract",
                            "attempt_count": attempt_count,
                            "terminal": terminal,
                            "retry_on_resume": not terminal,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "updated_at": now_iso(),
                        },
                    )
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
            profile.evidence_eligibility = "substantive_bounded"
            profile.excluded_from_synthesis = False
            profile.exclusion_reason = ""
        if analytical:
            # This is a zero-call committed-note normalization for every
            # analytical route. It binds source-controlled coverage and keeps
            # existing reasoner anchors intact; it is not a second semantic
            # verification pass.
            profile, _ = augment_profile_from_committed_note(
                profile,
                text,
                source_set_id="",
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
        context.pop("source_set_id", None)
        profile.context = context
        if not validation.passed or not note_valid:
            profile.evidence_eligibility = "context_only"
            profile.excluded_from_synthesis = True
            reasons = list(validation.errors) + list(
                row.get("validation_errors", []) or []
            )
            profile.exclusion_reason = "profile_or_note_validation_failed:" + ",".join(
                sorted(set(reasons))
            )
        elif prior_exclusion_reason.startswith("profile_or_note_validation_failed:"):
            profile.evidence_eligibility = "substantive_bounded"
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
        if failure_path.exists() and not profile_contract_fallback:
            failure_path.unlink()
        return {
            "index": index,
            "profile": profile,
            "paths": [
                candidate
                for candidate in (profile_path, checkpoint_path)
                if candidate.exists()
            ]
            + ([failure_path] if failure_path.exists() else []),
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
        "cumulative_provider_calls": profile_budget.cumulative_calls,
        "provider_call_limit": profile_budget.max_calls,
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
    del analytical, reasoner
    route = "deterministic"
    identity = "auto_zettelkasten.profiles.deterministic_profile:v1"
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
            frontmatter = read_note(path)["frontmatter"]
        except OSError:
            continue
        if frontmatter.get("note_status") not in {
            "analytical_atomic_note",
            "verified_atomic_note",
            "fulltext_available",
            "abstract_only_atomic_note",
            "metadata_only_source_note",
            "partial_document_atomic_note",
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


def _orphaned_source_ids(
    note_rows: Sequence[Mapping[str, Any]],
    collection_snapshot: Mapping[str, Any] | None,
) -> list[str]:
    if not collection_snapshot:
        return []
    available_keys = {
        str(row.get("key") or "").upper()
        for row in collection_snapshot.get("items", []) or []
        if isinstance(row, Mapping) and row.get("key")
    }
    return sorted(
        {
            str(row.get("source_id") or "")
            for row in note_rows
            if row.get("source_id")
            and row.get("zotero_item_key")
            and str(row.get("zotero_item_key") or "").upper()
            not in available_keys
        }
    )


def _recover_saved_source_bundle(
    checkpoint_root: Path,
    *,
    source_id: str,
    zotero_key: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    """Reparse a saved provider response locally before making another call."""

    failure = read_yaml(checkpoint_root / "source_failure.yml", {}) or {}
    if (
        not isinstance(failure, Mapping)
        or str(failure.get("fingerprint") or "") != fingerprint
    ):
        return None
    raw = failure.get("raw_response")
    if not isinstance(raw, (str, Mapping, list)) or raw in ("", {}, []):
        return None
    try:
        recovered = _parse_source_bundle_response(
            raw,
            label="saved source analysis bundle response",
            expected_identity={
                "source_id": source_id,
                "zotero_key": zotero_key,
            },
        )
    except (TypeError, ValueError, ProviderError):
        return None
    write_yaml(
        checkpoint_root / "source_recovery.yml",
        {
            "source_id": source_id,
            "zotero_item_key": zotero_key,
            "fingerprint": fingerprint,
            "status": "recovered_locally",
            "source_bundle_envelope_contract": SOURCE_BUNDLE_ENVELOPE_CONTRACT,
            "raw_response_hash": str(failure.get("raw_response_hash") or ""),
            "provider_calls": 0,
        },
    )
    return recovered


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
    profile_budget: _ProfileProviderBudget | None = None,
    source_match_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = item_key(item)
    base = {
        "inventory_index": index,
        "item": item,
        "zotero_item_key": key,
        "source_id": source_id_for_item(item),
        "note_id": note_id_for_item(item),
        "attempts": [],
        "terminal_status": "parked_for_review",
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
        effective_provider,
        effective_model,
        str(content.get("source_scope") or "full_document"),
        str(item_data(item).get("itemType") or ""),
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
    if prior.get("note_path") and _reusable_note(
        prior_path,
        base,
        request,
        source_match_index=source_match_index,
    ):
        prior_frontmatter = read_note(prior_path)["frontmatter"]
        prior_status = str(
            prior_frontmatter.get("note_status") or "analytical_atomic_note"
        )
        base.update(
            terminal_status="validated_note"
            if prior_status
            in {
                "analytical_atomic_note",
                "verified_atomic_note",
                "partial_document_atomic_note",
            }
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
    compatible_path = _compatible_committed_note(
        workspace,
        base,
        request,
        source_match_index=source_match_index,
    )
    if compatible_path is not None:
        prior_frontmatter = read_note(compatible_path)["frontmatter"]
        prior_status = str(
            prior_frontmatter.get("note_status") or "analytical_atomic_note"
        )
        relative_path = str(compatible_path.relative_to(workspace))
        base.update(
            terminal_status="validated_note"
            if prior_status
            in {
                "analytical_atomic_note",
                "verified_atomic_note",
                "partial_document_atomic_note",
            }
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
    if source_scope not in {"full_document", "partial_document"}:
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
    recovered_source_result = _recover_saved_source_bundle(
        checkpoint_root,
        source_id=str(base["source_id"]),
        zotero_key=key,
        fingerprint=fingerprint,
    )
    saved_source_failure = read_yaml(
        checkpoint_root / "source_failure.yml", {}
    ) or {}
    unchanged_contract_failure = (
        recovered_source_result is None
        and isinstance(saved_source_failure, Mapping)
        and str(saved_source_failure.get("fingerprint") or "") == fingerprint
        and str(
            saved_source_failure.get("source_bundle_envelope_contract") or ""
        )
        == SOURCE_BUNDLE_ENVELOPE_CONTRACT
        and str(saved_source_failure.get("error_type") or "")
        in {"ProviderError", "ValueError"}
        and saved_source_failure.get("raw_response") not in (None, "", {}, [])
    )
    if unchanged_contract_failure:
        base["reason"] = "terminal_source_contract_failure"
        base["attempts"].append(
            _attempt(
                base,
                "source_failure_checkpoint",
                "skipped",
                "unchanged_contract_failure_not_retried",
            )
        )
        return base
    if (
        recovered_source_result is None
        and bool(getattr(reader, "is_cloud", True))
        and not request.allow_cloud
    ):
        base["attempts"].append(
            _attempt(base, f"{reader.name}_text", "disallowed", "cloud_not_allowed")
        )
        base["reason"] = "reader_disallowed_by_privacy_policy"
        return base
    try:
        if progress is not None:
            progress.update(index, status="active", phase="reading_document")
        extraction_metrics = dict(content.get("coverage_metrics", {}) or {})
        reader_metadata = {
            **item_data(item),
            "_source_context": {
                "source_id": str(base["source_id"]),
                "zotero_key": key,
                "attachment_key": str(
                    item_data(item).get("parentItem") and key or ""
                ),
                "source_file": str(content.get("source_file") or ""),
                "route": str(content.get("content_route") or ""),
                "media_type": str(content.get("media_type") or ""),
                "source_scope": source_scope,
                "page_count": int(extraction_metrics.get("page_count", 0) or 0),
                "embedded_text_page_count": int(
                    extraction_metrics.get("embedded_text_page_count", 0) or 0
                ),
                "ocr_page_count": int(
                    extraction_metrics.get("ocr_page_count", 0) or 0
                ),
                "unresolved_pages": list(
                    extraction_metrics.get("unresolved_pages", []) or []
                ),
                "recovered_pages": list(
                    extraction_metrics.get("recovered_pages", []) or []
                ),
                "recovered_page_ratio": extraction_metrics.get(
                    "recovered_page_ratio"
                ),
                "content_kind": str(
                    extraction_metrics.get("content_kind") or ""
                ),
                "ordinal_to_printed_page": dict(
                    extraction_metrics.get("ordinal_to_printed_page", {}) or {}
                ),
                "heading_spans": list(
                    extraction_metrics.get("heading_spans", []) or []
                ),
                "table_spans": list(
                    extraction_metrics.get("table_spans", []) or []
                ),
                "figure_spans": list(
                    extraction_metrics.get("figure_spans", []) or []
                ),
            },
        }
        if recovered_source_result is not None:
            source_result = recovered_source_result
            reader_route = "local_source_envelope_recovery"
            reader_reason = "saved_provider_response_reparsed_without_call"
        else:
            source_result, reader_route, reader_reason = _read_document(
                reader,
                str(content["text"]),
                reader_metadata,
                None,
                request=request,
                checkpoint_root=checkpoint_root,
                progress=progress,
                inventory_index=index,
                provider_budget=profile_budget,
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
        raw_response = str(getattr(exc, "raw_response", "") or "")
        write_yaml(
            checkpoint_root / "source_failure.yml",
            {
                "source_id": str(base.get("source_id") or ""),
                "zotero_item_key": key,
                "fingerprint": fingerprint,
                "status": "parked_for_review",
                "source_bundle_envelope_contract": SOURCE_BUNDLE_ENVELOPE_CONTRACT,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "raw_response": raw_response,
                "raw_response_hash": (
                    sha256_text(raw_response) if raw_response else ""
                ),
                "provider_completion": dict(
                    getattr(exc, "provider_completion", {}) or {}
                ),
                "updated_at": now_iso(),
            },
        )
        base["attempts"].append(
            _attempt(
                base, f"{reader.name}_text", "failed", f"{type(exc).__name__}:{exc}"
            )
        )
        base["reason"] = f"reader_failed:{type(exc).__name__}"
        return base
    try:
        bundle = _source_bundle_from_result(source_result, base, source_scope)
    except ValueError as exc:
        write_yaml(
            checkpoint_root / "source_failure.yml",
            {
                "source_id": str(base.get("source_id") or ""),
                "zotero_item_key": key,
                "fingerprint": fingerprint,
                "status": "parked_for_review",
                "source_bundle_envelope_contract": SOURCE_BUNDLE_ENVELOPE_CONTRACT,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "raw_response": dict(source_result),
                "raw_response_hash": stable_hash(source_result),
                "provider_completion": {},
                "updated_at": now_iso(),
            },
        )
        base["reason"] = f"source_bundle_ownership_invalid:{exc}"
        base["attempts"].append(
            _attempt(base, reader_route, "failed", base["reason"])
        )
        return base
    analysis = (
        dict(bundle.analysis_sections)
        if bundle is not None
        else _ensure_analysis_contract(source_result)
    )
    if bundle is not None:
        base["source_analysis_bundle"] = bundle.to_dict()
    base["attempts"].append(
        _attempt(
            base,
            reader_route,
            "succeeded",
            reader_reason,
            output_path="pending_atomic_note",
        )
    )
    if source_scope == "partial_document":
        base.update(
            terminal_status="validated_note",
            note_status="partial_document_atomic_note",
            analysis=dict(analysis),
            evidence_eligibility="substantive_bounded",
            reason=str(content.get("coverage_reason") or source_scope),
        )
    risks = analyze_atomic_fidelity(
        analysis, str(content["text"]), extraction_metrics
    )
    if risks:
        base["quality_diagnostics"] = risks
    base["analysis"] = dict(analysis)
    return base


def _source_bundle_from_result(
    result: Mapping[str, Any],
    row: Mapping[str, Any],
    source_scope: str,
) -> SourceAnalysisBundle | None:
    if str(result.get("bundle_schema_version") or "") != "1":
        return None
    payload = dict(result)
    expected_source_id = str(row.get("source_id") or "")
    expected_zotero_key = str(row.get("zotero_item_key") or "")
    identity = (
        dict(payload.get("source_identity") or {})
        if isinstance(payload.get("source_identity"), Mapping)
        else {}
    )
    returned_source_id = str(identity.get("source_id") or "")
    returned_zotero_key = str(identity.get("zotero_key") or "")
    if returned_source_id and returned_source_id != expected_source_id:
        raise ValueError("source_identity.source_id does not match requested source")
    if (
        returned_zotero_key
        and returned_zotero_key.casefold() != expected_zotero_key.casefold()
    ):
        raise ValueError("source_identity.zotero_key does not match requested source")
    identity.update(
        {
            "source_id": expected_source_id,
            "zotero_key": expected_zotero_key,
        }
    )
    payload["source_identity"] = identity
    values = payload.get("evidence_anchors", [])
    if isinstance(values, list):
        payload["evidence_anchors"] = [
            _normalize_provider_evidence_anchor(
                value,
                expected_source_id=expected_source_id,
            )
            if isinstance(value, Mapping)
            else value
            for value in values
        ]
    values = payload.get("literature_positions", [])
    if isinstance(values, list):
        normalized_positions = []
        for value in values:
            if not isinstance(value, Mapping):
                normalized_positions.append(value)
                continue
            owned = dict(value)
            returned_owner = str(owned.get("current_source_id") or "")
            if returned_owner and returned_owner != expected_source_id:
                raise ValueError(
                    "literature_positions.current_source_id does not match "
                    "requested source"
                )
            owned["current_source_id"] = expected_source_id
            normalized_positions.append(owned)
        payload["literature_positions"] = normalized_positions

    diagnostics = payload.get("component_diagnostics", [])
    if isinstance(diagnostics, list):
        anchors = (
            list(payload.get("evidence_anchors", []))
            if isinstance(payload.get("evidence_anchors"), list)
            else []
        )
        for diagnostic in diagnostics:
            if (
                not isinstance(diagnostic, Mapping)
                or diagnostic.get("component") != "evidence_anchors"
                or not isinstance(diagnostic.get("raw"), Mapping)
            ):
                continue
            raw = dict(diagnostic["raw"])
            if str(raw.get("source_id") or "") != expected_source_id:
                continue
            try:
                recovered = _normalized_bundle_evidence_anchor(
                    raw,
                    expected_source_id=expected_source_id,
                    source_scope=source_scope,
                    discard_generated_ids=True,
                )
                EvidenceAnchor.from_dict(recovered)
            except (TypeError, ValueError):
                continue
            anchors.append(recovered)
        payload["evidence_anchors"] = anchors

    payload = _normalize_source_bundle_payload(payload)
    scope = (
        dict(payload.get("scope_assessment") or {})
        if isinstance(payload.get("scope_assessment"), Mapping)
        else {}
    )
    model_scope = str(scope.get("source_scope") or "")
    model_eligibility = str(scope.get("evidence_eligibility") or "")
    authoritative_eligibility = (
        "substantive_bounded"
        if source_scope in {"full_document", "partial_document"}
        else "context_only"
    )
    if model_scope and model_scope != source_scope:
        scope["model_source_scope"] = model_scope
    if model_eligibility and model_eligibility != authoritative_eligibility:
        scope["model_evidence_eligibility"] = model_eligibility
    scope["source_scope"] = source_scope
    scope["evidence_eligibility"] = authoritative_eligibility
    payload["scope_assessment"] = scope
    anchors = payload.get("evidence_anchors", [])
    if isinstance(anchors, list):
        normalized_anchors = []
        seen_anchors: set[str] = set()
        for value in anchors:
            if not isinstance(value, Mapping):
                normalized_anchors.append(value)
                continue
            anchor = _normalized_bundle_evidence_anchor(
                value,
                expected_source_id=expected_source_id,
                source_scope=source_scope,
            )
            anchor_key = _evidence_anchor_semantic_key(anchor)
            if anchor_key in seen_anchors:
                continue
            seen_anchors.add(anchor_key)
            normalized_anchors.append(anchor)
        payload["evidence_anchors"] = normalized_anchors
    positions = payload.get("literature_positions", [])
    if isinstance(positions, list):
        normalized_positions = []
        seen_positions: set[str] = set()
        for value in positions:
            if not isinstance(value, Mapping):
                normalized_positions.append(value)
                continue
            position = dict(value)
            if not position.get("author") and position.get("flat_author"):
                position["author"] = position["flat_author"]
            position.pop("flat_author", None)
            if position.get("year") is not None:
                position["year"] = str(position["year"])
            if isinstance(position.get("identifiers"), str):
                identifier = str(position["identifiers"]).strip()
                position["identifiers"] = (
                    {"other": identifier} if identifier else {}
                )
            position_key = stable_hash(
                {
                    "current_source_id": position.get("current_source_id"),
                    "raw_citation": position.get("raw_citation"),
                    "engagement": position.get("engagement"),
                }
            )
            if position_key in seen_positions:
                continue
            seen_positions.add(position_key)
            normalized_positions.append(position)
        payload["literature_positions"] = normalized_positions
    recommendations = payload.get("missing_source_recommendations", [])
    if isinstance(recommendations, list):
        payload["missing_source_recommendations"] = [
            _normalized_missing_source_recommendation(value, expected_source_id)
            if isinstance(value, Mapping)
            else value
            for value in recommendations
        ]
    return SourceAnalysisBundle.from_dict(payload)


def _normalized_bundle_evidence_anchor(
    value: Mapping[str, Any],
    *,
    expected_source_id: str,
    source_scope: str,
    discard_generated_ids: bool = False,
) -> dict[str, Any]:
    anchor = _normalize_provider_evidence_anchor(
        value,
        expected_source_id=expected_source_id,
        discard_generated_ids=discard_generated_ids,
    )
    envelope = anchor.get("support_envelope")
    if isinstance(envelope, str):
        boundary = envelope.strip()
        anchor["support_envelope"] = {
            "coverage": (
                "limited_text"
                if source_scope == "partial_document"
                else "full_text"
            ),
            "restrictions": [boundary] if boundary else [],
            "support_status": "supported",
        }
    elif isinstance(envelope, Mapping):
        anchor["support_envelope"] = _normalized_support_envelope(
            envelope, source_scope
        )
    elif envelope in (None, ""):
        anchor["support_envelope"] = _normalized_support_envelope(
            {}, source_scope
        )
    return anchor


def _evidence_anchor_semantic_key(value: Mapping[str, Any]) -> str:
    canonical = EvidenceAnchor.from_dict(value).to_dict()
    canonical.pop("evidence_anchor_id", None)
    canonical.pop("revision_hash", None)
    quantitative = canonical.get("quantitative_result")
    if isinstance(quantitative, Mapping):
        normalized_quantitative = dict(quantitative)
        for key in (
            "quantitative_result_id",
            "source_id",
            "evidence_anchor_id",
        ):
            normalized_quantitative.pop(key, None)
        canonical["quantitative_result"] = normalized_quantitative
    source_locators = canonical.get("source_locators")
    if isinstance(source_locators, list):
        canonical["source_locators"] = [
            {
                key: item
                for key, item in source_locator.items()
                if key not in {"locator_id", "evidence_anchor_id"}
            }
            if isinstance(source_locator, Mapping)
            else source_locator
            for source_locator in source_locators
        ]
    return stable_hash(canonical)


def _normalized_support_envelope(
    value: Mapping[str, Any], source_scope: str
) -> dict[str, Any]:
    empirical = str(value.get("empirical_role") or "").casefold()
    argument = str(value.get("argument_role") or "").casefold()
    status = str(value.get("support_status") or "").casefold()
    scope = value.get("scope")
    restrictions = value.get("restrictions")
    return {
        "empirical_role": (
            "causal"
            if "causal" in empirical
            else "associational"
            if any(token in empirical for token in ("statistic", "associat", "regression"))
            else "descriptive"
            if any(token in empirical for token in ("descript", "illustrat"))
            else "mechanism_evidence"
            if any(token in empirical for token in ("mechanism", "qualitative"))
            else "none"
        ),
        "argument_role": (
            "methodological"
            if any(token in argument for token in ("method", "data", "evidence"))
            else "normative"
            if "normative" in argument
            else "practitioner_guidance"
            if any(token in argument for token in ("pract", "guidance"))
            else "conceptual"
            if any(token in argument for token in ("concept", "theor", "claim", "thesis"))
            else "interpretive"
            if argument
            else "none"
        ),
        "coverage": (
            "limited_text" if source_scope == "partial_document" else "full_text"
        ),
        "scope": (
            {
                str(key): (
                    [str(item) for item in items]
                    if isinstance(items, list)
                    else [str(items)]
                )
                for key, items in scope.items()
                if str(key) and (items if isinstance(items, list) else str(items))
            }
            if isinstance(scope, Mapping)
            else {"description": [str(scope)]}
            if str(scope or "").strip()
            else {}
        ),
        "restrictions": (
            [str(item) for item in restrictions if str(item).strip()]
            if isinstance(restrictions, list)
            else [str(restrictions)]
            if str(restrictions or "").strip()
            else []
        ),
        "support_status": (
            "unsupported"
            if "unsupported" in status
            else "limited"
            if any(token in status for token in ("limited", "mixed", "plausible"))
            else "supported"
            if any(
                token in status
                for token in ("supported", "supportive", "robust", "consistent")
            )
            else "support_unknown"
        ),
    }


def _normalized_missing_source_recommendation(
    value: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    allowed = {
        "external_source_id",
        "raw_citation",
        "normalized_citation",
        "identifiers",
        "discussed_by_source_ids",
        "importance",
        "relevant_collections",
        "relevant_topics",
        "relevant_clusters",
        "acquisition_priority",
        "match_status",
        "retrieval_status",
        "ambiguity_notes",
        "zotero_key",
        "source_id",
        "note_id",
    }
    normalized = {key: item for key, item in value.items() if key in allowed}
    citation = (
        dict(normalized.get("normalized_citation") or {})
        if isinstance(normalized.get("normalized_citation"), Mapping)
        else {}
    )
    for field in ("author", "year", "title"):
        if str(value.get(field) or "").strip():
            citation[field] = str(value[field]).strip()
    normalized["normalized_citation"] = citation
    normalized["discussed_by_source_ids"] = list(
        dict.fromkeys(
            [
                *(
                    normalized.get("discussed_by_source_ids", [])
                    if isinstance(normalized.get("discussed_by_source_ids"), list)
                    else []
                ),
                str(value.get("current_source_id") or source_id),
            ]
        )
    )
    if not str(normalized.get("importance") or "").strip():
        normalized["importance"] = str(
            value.get("engagement") or value.get("provenance") or ""
        ).strip()
    ambiguity = " | ".join(
        str(value.get(field) or "").strip()
        for field in ("relation_label", "locator")
        if str(value.get(field) or "").strip()
    )
    if ambiguity and not str(normalized.get("ambiguity_notes") or "").strip():
        normalized["ambiguity_notes"] = ambiguity
    return normalized


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
    if scope == "partial_document":
        analysis = content.get("analysis")
        analysis = dict(analysis) if isinstance(analysis, Mapping) else {}
        metrics = dict(content.get("coverage_metrics", {}) or {})
        recovered = ", ".join(
            str(page) for page in metrics.get("recovered_pages", []) or []
        )
        unresolved = ", ".join(
            str(page) for page in metrics.get("unresolved_pages", []) or []
        )
        missing_scope = (
            "remaining parent work (page range unavailable from the supplied attachment)"
            if metrics.get("missing_scope") == "remainder_of_parent_work"
            else ""
        )
        findings = "\n".join(
            [
                "The following points describe only the recovered pages.",
                f"- Available-content argument: {analysis.get('thesis') or 'Not established from the recovered pages.'}",
                f"- Available-content knowledge basis: {analysis.get('method_and_research_design') or 'Not established from the recovered pages.'}",
                f"- Available-content findings: {analysis.get('detailed_findings') or 'No substantive finding was recoverable.'}",
                f"- Available-content limitations: {analysis.get('limitations') or 'Not established from the recovered pages.'}",
                f"- Locators within the recovered content: {analysis.get('locators') or 'No traceable locator was recovered.'}",
            ]
        )
        missing_disclosure = (
            f"Missing or unresolved scope: {missing_scope}. "
            if missing_scope
            else f"Missing or unresolved PDF pages: {unresolved or 'none recorded'}. "
        )
        return {
            "available_content_findings": findings,
            "scope_limitation": (
                f"Recovered PDF pages: {recovered or 'none recorded'}. "
                f"{missing_disclosure}"
                "This is not a complete-document analysis; absent sections and the "
                "source's complete argument, findings, method, and limitations are "
                "not inferred."
            ),
        }
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
    parent_data = item_data(item)
    targets: list[Mapping[str, Any]] = [item]
    candidates: list[dict[str, Any]] = []
    primary_pdf_attempted = False
    failed_primary_pdf: dict[str, Any] | None = None
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
            if effective_media_type == "application/pdf":
                marked_text = _indexed_pdf_text_with_page_markers(text, fulltext)
                if marked_text is None:
                    base["attempts"].append(
                        _attempt(
                            base,
                            "zotero_fulltext",
                            "failed",
                            f"{target_key}:indexed_pdf_missing_page_boundaries",
                            input_hash=sha256_text(text),
                        )
                    )
                    text = ""
                else:
                    text = marked_text
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
            elif text:
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
                    rank_override=_attachment_candidate_rank(
                        data,
                        parent_data,
                        media_type=effective_media_type,
                        actual_file=False,
                    ),
                )
                candidate = _apply_bibliographic_scope(
                    candidate,
                    parent_data,
                    data,
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
            extraction_path = local
            local_media_type = _target_media_type(data)
            local_primary_pdf = _is_primary_pdf_attachment(
                data, parent_data, local_media_type
            )
            primary_pdf_attempted = primary_pdf_attempted or local_primary_pdf
            if local.suffix.lower() == ".pdf" or local_media_type == "application/pdf":
                custody_path = (
                    workspace
                    / "01_custody"
                    / "files"
                    / f"{safe_filename(target_key)}{local.suffix.lower() or '.pdf'}"
                )
                atomic_write_bytes(custody_path, local.read_bytes())
                extraction_path = custody_path
            extracted = extract_path(
                extraction_path,
                ocr_mode=request.extraction_policy.ocr,
                ocr_languages=request.extraction_policy.languages,
            )
            base["attempts"].append(
                _attempt(
                    base,
                    extracted.route,
                    "succeeded" if extracted.status == "succeeded" else "failed",
                    extracted.reason or "extracted",
                    input_hash=sha256_file(local),
                    output_path=str(extraction_path),
                )
            )
            if extracted.status == "succeeded":
                local_candidate = _content_candidate(
                    extracted.adequacy
                    or classify_content_adequacy(
                        extracted.text,
                        media_type=extracted.media_type,
                        page_count=extracted.page_count,
                    ),
                    text=extracted.text,
                    content_hash=sha256_file(local),
                    source_file=str(extraction_path),
                    content_route=extracted.route,
                    media_type=extracted.media_type,
                    rank_override=_attachment_candidate_rank(
                        data,
                        parent_data,
                        media_type=extracted.media_type,
                        actual_file=True,
                    ),
                )
                local_candidate = _apply_bibliographic_scope(
                    local_candidate,
                    parent_data,
                    data,
                )
                local_candidate["actual_primary_pdf"] = local_primary_pdf
                candidates.append(local_candidate)
            elif local_primary_pdf:
                failed_primary_pdf = _failed_pdf_candidate(
                    extracted,
                    content_hash=sha256_file(local),
                    source_file=str(extraction_path),
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
        downloaded_primary_pdf = _is_primary_pdf_attachment(
            data, parent_data, media_type
        )
        primary_pdf_attempted = primary_pdf_attempted or downloaded_primary_pdf
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
            document,
            media_type=media_type,
            filename=custody_path.name,
            ocr_mode=request.extraction_policy.ocr,
            ocr_languages=request.extraction_policy.languages,
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
            downloaded_candidate = _content_candidate(
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
                rank_override=_attachment_candidate_rank(
                    data,
                    parent_data,
                    media_type=extracted.media_type,
                    actual_file=True,
                ),
            )
            downloaded_candidate = _apply_bibliographic_scope(
                downloaded_candidate,
                parent_data,
                data,
            )
            downloaded_candidate["actual_primary_pdf"] = downloaded_primary_pdf
            candidates.append(downloaded_candidate)
        elif downloaded_primary_pdf:
            failed_primary_pdf = _failed_pdf_candidate(
                extracted,
                content_hash=document_hash,
                source_file=str(custody_path),
            )
    if primary_pdf_attempted:
        actual_primary = [
            row for row in candidates if row.get("actual_primary_pdf") is True
        ]
        if actual_primary:
            candidates = actual_primary
        elif failed_primary_pdf is not None:
            abstract_candidates = [
                row
                for row in candidates
                if str(row.get("source_scope") or "") == "abstract_only"
                and str(row.get("text") or "").strip()
            ]
            if abstract_candidates:
                best_abstract = max(
                    abstract_candidates, key=lambda row: len(str(row.get("text") or ""))
                )
                abstract_text = str(best_abstract.get("text") or "").strip()
                failed_primary_pdf.update(
                    {
                        "text": abstract_text,
                        "abstract_text": abstract_text,
                        "source_scope": "abstract_only",
                        "source_coverage": {
                            **dict(best_abstract.get("source_coverage", {}) or {}),
                            "coverage_gate": "limited",
                            "source_scope": "abstract_only",
                            "reason": "primary_pdf_unreadable_abstract_available",
                        },
                        "coverage_reason": "primary_pdf_unreadable_abstract_available",
                    }
                )
            return failed_primary_pdf
    if candidates:
        return max(
            candidates,
            key=lambda row: (
                int(row.get("rank", 0)),
                len(str(row.get("text") or "")),
                str(row.get("content_route") or ""),
                str(row.get("source_file") or ""),
            ),
        )
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
    rank_override: int | None = None,
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
        else 60
        if scope == "partial_document"
        else 10
    )
    if rank_override is not None and adequacy.is_full_publication:
        rank = rank_override
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


_EXPLICIT_BOUNDED_ATTACHMENT_RE = re.compile(
    r"\b(?:chapter\s+(?:\d+|[ivxlcdm]+)|appendix|excerpt)\b",
    flags=re.IGNORECASE,
)
_EXPLICIT_CHAPTER_LABEL_RE = re.compile(
    r"^\s*chapter(?:\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|"
    r"seven|eight|nine|ten|[a-z][\w'-]*))?(?:\s*[:.-].*)?\s*$",
    flags=re.IGNORECASE,
)
_WEAK_BOUNDED_ATTACHMENT_RE = re.compile(
    r"\b(?:introduction|foreword|preface)\b",
    flags=re.IGNORECASE,
)
_WEAK_BOUNDED_ATTACHMENT_PAGE_LIMIT = 100
_GENERIC_ATTACHMENT_LABEL_RE = re.compile(
    r"^(?:pdf|full\s*text|attachment|download(?:_file)?(?:\.pdf)?)$",
    flags=re.IGNORECASE,
)


def _apply_bibliographic_scope(
    candidate: Mapping[str, Any],
    parent: Mapping[str, Any],
    attachment: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(candidate)
    if (
        row.get("source_scope") != "full_document"
        or str(parent.get("itemType") or "") not in {"book", "thesis", "report"}
    ):
        return row
    label = str(
        attachment.get("title")
        or attachment.get("filename")
        or ""
    ).strip()
    meaningful_label = label and not _GENERIC_ATTACHMENT_LABEL_RE.fullmatch(label)
    first_page = str(row.get("text") or "")[:8_000]
    scope_probe = re.split(
        r"(?im)^\s*(?:table\s+of\s+)?contents\s*$",
        first_page,
        maxsplit=1,
    )[0]
    metrics = dict(row.get("coverage_metrics", {}) or {})
    page_count = int(metrics.get("page_count", 0) or 0)
    recovered_pages = metrics.get("recovered_pages", ()) or ()
    recovered_page_count = (
        len(recovered_pages)
        if isinstance(recovered_pages, (list, tuple, set))
        else page_count
    )
    if recovered_page_count <= 0:
        recovered_page_count = page_count
    plausibly_short = (
        0
        < recovered_page_count
        <= _WEAK_BOUNDED_ATTACHMENT_PAGE_LIMIT
    )
    parent_type = str(parent.get("itemType") or "")
    bounded_match = (
        _EXPLICIT_BOUNDED_ATTACHMENT_RE.search(label)
        if meaningful_label
        else None
    ) or (
        _EXPLICIT_CHAPTER_LABEL_RE.fullmatch(label)
        if meaningful_label
        else None
    ) or (
        _WEAK_BOUNDED_ATTACHMENT_RE.search(label)
        if meaningful_label and plausibly_short
        else None
    ) or re.search(
        r"(?im)^(?:--- Page 1 ---\s*)?(?:chapter\s+(?:\d+|[ivxlcdm]+)"
        r"(?:\s*[:.-]\s*|\s+)|appendix\s+[a-z0-9]+)",
        scope_probe,
    ) or (
        re.search(
            r"(?im)^(?:--- Page 1 ---\s*)?(?:chapter\s+)?"
            r"(?:one|two|three|four|five|six|seven|eight|nine|ten)\s*$",
            scope_probe,
        )
        if parent_type == "book" and 0 < page_count <= 100
        else None
    )
    bounded_source_object = (
        str(bounded_match.group(0)).strip() if bounded_match else ""
    )
    if not bounded_source_object and 0 < page_count <= 100:
        contents_page_numbers = [
            int(value)
            for value in re.findall(
                r"(?im)^.{3,100}?\.{2,}\s*(\d{2,4})\s*$",
                first_page,
            )
        ]
        if contents_page_numbers and max(contents_page_numbers) > page_count + 20:
            bounded_source_object = "table of contents exceeds attachment span"
    if not bounded_source_object:
        return row
    coverage = dict(row.get("source_coverage", {}) or {})
    metrics.update(
        {
            "bibliographic_scope": "bounded_attachment_excerpt",
            "missing_scope": "remainder_of_parent_work",
            "recovered_pages": list(
                metrics.get("recovered_pages", []) or range(1, page_count + 1)
            ),
        }
    )
    coverage.update(
        {
            "source_scope": "partial_document",
            "coverage_gate": "limited",
            "reason": "bounded_attachment_excerpt",
            "metrics": metrics,
        }
    )
    row.update(
        source_scope="partial_document",
        source_coverage=coverage,
        coverage_reason="bounded_attachment_excerpt",
        coverage_metrics=metrics,
        bounded_source_object=bounded_source_object,
        rank=70,
    )
    return row


_SUPPLEMENTARY_ATTACHMENT_RE = re.compile(
    r"\b(?:supplement(?:ary)?|supporting\s+(?:information|material)|appendix|"
    r"data\s*set|dataset|codebook|replication\s+(?:data|files?)|tables?\s+only|"
    r"figures?\s+only)\b",
    flags=re.IGNORECASE,
)

_NONARTICLE_ATTACHMENT_RE = re.compile(
    r"\b(?:cover\s+letter|response\s+to\s+(?:the\s+)?reviewers?|author\s+response|"
    r"rebuttal|editorial\s+decision|decision\s+letter|reviewer\s+comments?|"
    r"copyright\s+(?:form|transfer)|licen[cs]e\s+agreement|certificate|"
    r"graphical\s+abstract|highlights?)\b",
    flags=re.IGNORECASE,
)

_PRIMARY_ATTACHMENT_RE = re.compile(
    r"\b(?:full\s*text(?:\s+pdf)?|accepted\s+manuscript|author\s+manuscript|"
    r"main\s+(?:article|document)|published\s+version|journal\s+article)\b",
    flags=re.IGNORECASE,
)


def _attachment_candidate_rank(
    attachment: Mapping[str, Any],
    parent: Mapping[str, Any],
    *,
    media_type: str,
    actual_file: bool,
) -> int | None:
    """Rank primary PDFs above indexed text without selecting supplements."""

    if media_type != "application/pdf":
        return None
    label = " ".join(
        str(attachment.get(field) or "")
        for field in ("title", "filename", "attachmentPath")
    )
    if _SUPPLEMENTARY_ATTACHMENT_RE.search(label):
        return 60 if actual_file else 50
    if _NONARTICLE_ATTACHMENT_RE.search(label):
        return 40 if actual_file else 35

    parent_title = _selection_terms(str(parent.get("title") or ""))
    attachment_title = _selection_terms(label)
    matched_title_terms = parent_title & attachment_title
    title_match = bool(
        parent_title
        and attachment_title
        and (
            len(matched_title_terms) >= 2
            or (
                len(parent_title) <= 2
                and len(matched_title_terms) == len(parent_title)
            )
            or len(matched_title_terms) / len(parent_title) >= 0.3
        )
    )
    generic_primary_label = bool(_PRIMARY_ATTACHMENT_RE.search(label))

    parent_doi = str(parent.get("DOI") or parent.get("doi") or "").strip().casefold()
    attachment_doi = str(
        attachment.get("DOI") or attachment.get("doi") or ""
    ).strip().casefold()
    doi_match = bool(parent_doi and attachment_doi and parent_doi == attachment_doi)
    doi_mismatch = bool(parent_doi and attachment_doi and not doi_match)

    # Any actual non-supplementary, non-administrative PDF outranks its indexed
    # representation. Sparse Zotero attachment metadata is common (for example,
    # title ``PDF`` plus an opaque publisher filename), so absence of a title or
    # DOI match is not evidence that the attachment is secondary. Positive
    # evidence still raises the primary file above other actual PDF candidates.
    positive_primary_evidence = title_match or generic_primary_label or doi_match
    rank = 125 if actual_file and positive_primary_evidence else 110 if actual_file else 100
    if parent_title and attachment_title:
        overlap = len(matched_title_terms) / len(parent_title)
        rank += min(10, round(overlap * 10))

    if doi_match:
        rank += 6
    elif doi_mismatch:
        rank -= 15
    return rank


def _is_primary_pdf_attachment(
    attachment: Mapping[str, Any], parent: Mapping[str, Any], media_type: str
) -> bool:
    rank = _attachment_candidate_rank(
        attachment, parent, media_type=media_type, actual_file=True
    )
    return rank is not None and rank >= 100


def _failed_pdf_candidate(
    extracted: Any, *, content_hash: str, source_file: str
) -> dict[str, Any]:
    adequacy = extracted.adequacy
    prior_coverage = adequacy.to_dict() if adequacy is not None else {}
    metrics = dict(prior_coverage.get("metrics", {}) or {})
    if extracted.page_count and not metrics.get("page_count"):
        metrics["page_count"] = int(extracted.page_count)
    # An extraction-level failure overrides any earlier density-only pass.
    # Keeping a stale `passed` gate here makes the honest limited note fail its
    # own schema validation and incorrectly exhausts the Zotero item.
    coverage = {
        "classification": "metadata_only",
        "source_scope": "metadata_only",
        "coverage_gate": "failed",
        "reason": str(extracted.reason or "pdf_extraction_failed"),
        "abstract": "",
        "paywall_markers": [],
        "access_markers": [],
        "metrics": metrics,
    }
    return {
        "text": "",
        "abstract_text": "",
        "content_hash": content_hash,
        "source_file": source_file,
        "content_route": str(extracted.route or "pdf_extraction"),
        "media_type": "application/pdf",
        "source_scope": "metadata_only",
        "source_coverage": coverage,
        "coverage_reason": str(extracted.reason or "pdf_extraction_failed"),
        "coverage_metrics": metrics,
        "rank": 0,
    }


def _selection_terms(value: str) -> set[str]:
    ignored = {
        "attachment",
        "download",
        "file",
        "full",
        "pdf",
        "text",
        "the",
        "and",
        "for",
        "with",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in ignored
    }


def _indexed_pdf_text_with_page_markers(
    text: str, fulltext: Mapping[str, Any] | None
) -> str | None:
    """Return a page-addressable Zotero fallback or reject ambiguous indexing."""

    if not _indexed_pdf_complete(fulltext):
        return None
    total = int((fulltext or {}).get("totalPages", 0) or 0)
    marker_numbers = [
        int(value) for value in re.findall(r"---\s*Page\s+(\d+)\s*---", text)
    ]
    if marker_numbers == list(range(1, total + 1)):
        return text
    pages = re.split(r"\f+", text)
    if len(pages) != total:
        return None
    return "\n\n".join(
        f"--- Page {index} ---\n{page.strip()}"
        for index, page in enumerate(pages, start=1)
    ).strip()


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
        "isbn": str(data.get("ISBN") or data.get("isbn") or ""),
        "url": str(data.get("url") or ""),
        "original_zotero_tags": original_tags(row["item"]),
        "zotero_relations": data.get("relations", {})
        if isinstance(data.get("relations", {}), Mapping)
        else {},
        "item_type": str(data.get("itemType") or ""),
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
        "evidence_eligibility": str(
            row.get("evidence_eligibility")
            or (
                "substantive_bounded"
                if str(row.get("source_scope") or "")
                in {"full_document", "partial_document"}
                else "context_only"
            )
        ),
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
        "source_bundle_prompt_version": SOURCE_BUNDLE_PROMPT_VERSION,
        "source_bundle_dependency_fingerprint": (
            _source_bundle_dependency_fingerprint(row, request)
            if row.get("source_analysis_bundle")
            else ""
        ),
        "engine_version": ENGINE_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def _note_summary_from_path(workspace: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    path = workspace / str(row.get("note_path", ""))
    note = read_note(path)
    internal_text = internal_note_text(path)
    front = note["frontmatter"]
    body = str(note["body"])
    validation = validate_note(internal_text)
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
        "semantic_note_hash": semantic_note_hash(internal_text),
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
    reader_provider: str,
    reader_model: str,
    source_scope: str,
    source_item_type: str = "",
) -> str:
    payload = {
        "zotero_item_key": key,
        "content_hash": content_hash,
        "extraction_version": request.extraction_version,
        "prompt_version": request.prompt_version,
        "source_bundle_prompt_version": SOURCE_BUNDLE_PROMPT_VERSION,
        "reader_provider": reader_provider,
        "reader_model": reader_model,
        "source_scope": source_scope,
        "source_item_type": str(source_item_type),
        "question_lens_policy_version": "collection_invariant-1",
        "chunking_version": CHUNKING_VERSION,
        "content_classifier_version": CONTENT_CLASSIFIER_VERSION,
        "extraction_policy_hash": sha256_text(
            json.dumps(
                request.to_dict().get("extraction_policy", {}), sort_keys=True
            )
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
        "terminal_status": "parked_for_review",
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


def _inventory_work_identity(
    item: Mapping[str, Any],
    *,
    shared_same_as: set[str] | None = None,
) -> tuple[str, ...]:
    data = item_data(item)
    relations = (
        dict(data.get("relations") or {})
        if isinstance(data.get("relations"), Mapping)
        else {}
    )
    same_as = sorted(
        {
            _normalized_url_identifier(str(value))
            for value in relations.get("owl:sameAs", []) or []
            if _normalized_url_identifier(str(value))
        }
    )
    if len(same_as) == 1 and (
        shared_same_as is None or same_as[0] in shared_same_as
    ):
        return ("zotero_same_as", same_as[0])
    doi = _normalized_doi_identifier(
        str(data.get("DOI") or data.get("doi") or "")
    )
    title = _normalized_match_text(str(data.get("title") or ""))
    item_type = str(data.get("itemType") or "").casefold()
    if doi and title:
        return ("doi", doi, title, item_type)
    isbn = _normalized_strong_identifier(
        str(data.get("ISBN") or data.get("isbn") or "")
    )
    if isbn and title:
        return ("isbn", isbn, title, item_type)
    surnames = tuple(_creator_surnames(list(data.get("creators", []) or [])))
    year_match = re.search(r"(?:19|20)\d{2}", str(data.get("date") or ""))
    if title and surnames and year_match and item_type:
        return (
            "bibliographic",
            title,
            *surnames,
            year_match.group(0),
            item_type,
        )
    return ("zotero_key", item_key(item).upper())


def _compatible_work_identity(
    item: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    data = item_data(item)
    item_title = _normalized_match_text(str(data.get("title") or ""))
    candidate_title = _normalized_match_text(str(candidate.get("title") or ""))
    if not item_title or not candidate_title or item_title != candidate_title:
        return False
    item_type = str(data.get("itemType") or "").casefold()
    candidate_type = str(candidate.get("item_type") or "").casefold()
    return not item_type or not candidate_type or item_type == candidate_type


def _existing_canonical_for_item(
    item: Mapping[str, Any], source_index: Mapping[str, Any]
) -> dict[str, Any] | None:
    key = item_key(item).upper()
    exact_key = _unique_index_matches(source_index.get("by_zotero_key", {}), key)
    if len(exact_key) == 1:
        return dict(source_index.get("by_source_id", {})).get(exact_key[0])
    data = item_data(item)
    doi = _normalized_doi_identifier(
        str(data.get("DOI") or data.get("doi") or "")
    )
    isbn = _normalized_strong_identifier(
        str(data.get("ISBN") or data.get("isbn") or "")
    )
    for index_name, value in (("by_doi", doi), ("by_isbn", isbn)):
        if not value:
            continue
        matches = _unique_index_matches(source_index.get(index_name, {}), value)
        compatible = [
            dict(source_index.get("by_source_id", {})).get(source_id)
            for source_id in matches
        ]
        compatible = [
            candidate
            for candidate in compatible
            if isinstance(candidate, Mapping)
            and _compatible_work_identity(item, candidate)
        ]
        if len(compatible) == 1:
            return dict(compatible[0])
    title = _normalized_match_text(str(data.get("title") or ""))
    surnames = _creator_surnames(list(data.get("creators", []) or []))
    year_match = re.search(r"(?:19|20)\d{2}", str(data.get("date") or ""))
    item_type = str(data.get("itemType") or "").casefold()
    if not title or not surnames or not year_match:
        return None
    matches = [
        dict(row)
        for row in source_index.get("by_source_id", {}).values()
        if isinstance(row, Mapping)
        and str(row.get("title") or "") == title
        and list(row.get("author_surnames", []) or []) == surnames
        and str(row.get("year") or "") == year_match.group(0)
        and (
            not item_type
            or not row.get("item_type")
            or str(row.get("item_type") or "") == item_type
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _canonical_item_rank(item: Mapping[str, Any]) -> tuple[int, str]:
    data = item_data(item)
    richness = sum(
        bool(data.get(field))
        for field in (
            "DOI",
            "doi",
            "ISBN",
            "isbn",
            "title",
            "creators",
            "date",
            "abstractNote",
            "publicationTitle",
            "publisher",
            "url",
        )
    )
    return (-richness, item_key(item).casefold())


def _canonical_inventory_plan(
    workspace: Path,
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    source_index = _source_match_index(workspace)
    grouped: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = defaultdict(
        list
    )
    same_as_counts: dict[str, int] = defaultdict(int)
    for raw in items:
        relations = item_data(raw).get("relations")
        if not isinstance(relations, Mapping):
            continue
        for value in {
            _normalized_url_identifier(str(value))
            for value in relations.get("owl:sameAs", []) or []
            if _normalized_url_identifier(str(value))
        }:
            same_as_counts[value] += 1
    shared_same_as = {
        value for value, count in same_as_counts.items() if count > 1
    }
    for index, raw in enumerate(items):
        item = dict(raw)
        grouped[
            _inventory_work_identity(item, shared_same_as=shared_same_as)
        ].append((index, item))

    pending: list[tuple[int, dict[str, Any]]] = []
    aliases: list[dict[str, Any]] = []
    for group in grouped.values():
        existing = next(
            (
                match
                for _, item in group
                if (match := _existing_canonical_for_item(item, source_index))
            ),
            None,
        )
        existing_key = str((existing or {}).get("zotero_key") or "")
        matching_existing = [
            row for row in group if item_key(row[1]).casefold() == existing_key.casefold()
        ]
        if matching_existing:
            canonical_index, canonical_item = min(
                matching_existing, key=lambda row: row[0]
            )
            pending.append((canonical_index, canonical_item))
            existing = None
        elif existing is None:
            canonical_index, canonical_item = min(
                group,
                key=lambda row: (*_canonical_item_rank(row[1]), row[0]),
            )
            pending.append((canonical_index, canonical_item))
        else:
            canonical_index = -1
        for index, item in group:
            if existing is None and index == canonical_index:
                continue
            aliases.append(
                {
                    "inventory_index": index,
                    "item": item,
                    "canonical_inventory_index": canonical_index,
                    "existing_canonical": dict(existing or {}),
                }
            )
    pending.sort(key=lambda row: row[0])
    aliases.sort(key=lambda row: int(row["inventory_index"]))
    return pending, aliases


def _duplicate_alias_result(
    alias: Mapping[str, Any],
    canonical: Mapping[str, Any],
) -> dict[str, Any]:
    item = dict(alias["item"])
    index = int(alias["inventory_index"])
    canonical_key = str(canonical.get("zotero_item_key") or canonical.get("zotero_key") or "")
    return {
        "inventory_index": index,
        "item": item,
        "zotero_item_key": item_key(item),
        "canonical_zotero_key": canonical_key,
        "source_id": str(canonical.get("source_id") or ""),
        "note_id": str(canonical.get("note_id") or ""),
        "note_path": str(canonical.get("note_path") or ""),
        "terminal_status": "duplicate_alias",
        "reason": f"duplicate_alias_of:{canonical_key}",
        "fingerprint": "",
        "content_hash": "",
        "reused": True,
        "attempts": [
            _attempt(
                {
                    "source_id": str(canonical.get("source_id") or ""),
                    "zotero_item_key": item_key(item),
                },
                "identity_reconciliation",
                "skipped",
                f"duplicate_alias_of:{canonical_key}",
                output_path=str(canonical.get("note_path") or ""),
            )
        ],
    }


def _public_terminal_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "inventory_index": int(row.get("inventory_index", 0)),
        "zotero_item_key": str(row.get("zotero_item_key", "")),
        "source_id": str(row.get("source_id", "")),
        "note_id": str(row.get("note_id", "")),
        "note_path": str(row.get("note_path", "")),
        "terminal_status": str(row.get("terminal_status", "parked_for_review")),
        "reason": str(row.get("reason", "")),
        "fingerprint": str(row.get("fingerprint", "")),
        "content_hash": str(row.get("content_hash", "")),
        "canonical_zotero_key": str(row.get("canonical_zotero_key", "")),
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


def _reusable_note(
    path: Path,
    row: Mapping[str, Any],
    request: MapRequest,
    *,
    source_match_index: Mapping[str, Any] | None = None,
) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        text = internal_note_text(path)
        frontmatter, _ = parse_atomic_note(text)
    except OSError:
        return False
    if not validate_note(text).passed:
        return False
    prior_item_type = str(frontmatter.get("item_type") or "")
    current_item_type = str(item_data(row.get("item", {})).get("itemType") or "")
    if prior_item_type and prior_item_type != current_item_type:
        return False
    reusable = all(
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
            str(frontmatter.get("source_bundle_prompt_version", ""))
            == SOURCE_BUNDLE_PROMPT_VERSION,
        )
    )
    if not reusable:
        return False
    if str(frontmatter.get("note_status") or "") not in {
        "analytical_atomic_note",
        "verified_atomic_note",
        "partial_document_atomic_note",
    }:
        return True
    bundle_path = (
        path.parents[2]
        / "02_source_memory"
        / "bundles"
        / f"{safe_filename(str(row.get('source_id') or ''))}.yml"
    )
    if (
        not bundle_path.is_file()
        and not str(frontmatter.get("source_bundle_dependency_fingerprint") or "")
    ):
        return True
    bundle = read_yaml(bundle_path, {}) or {}
    expected_dependency = _source_bundle_dependency_fingerprint(row, request)
    if str(bundle.get("dependency_fingerprint") or "") == expected_dependency:
        return True
    stored_payload = bundle.get("bundle")
    if not isinstance(stored_payload, Mapping):
        return False
    try:
        normalized = _source_bundle_from_result(
            stored_payload,
            row,
            str(row.get("source_scope") or "full_document"),
        )
    except (TypeError, ValueError):
        return False
    if normalized is None:
        return False
    try:
        stored_bundle = SourceAnalysisBundle.from_dict(stored_payload)
    except (TypeError, ValueError):
        stored_bundle = None
    if (
        stored_bundle is not None
        and stored_bundle.semantic_dict() == normalized.semantic_dict()
    ):
        write_yaml(
            bundle_path,
            {
                **dict(bundle),
                "dependency_fingerprint": expected_dependency,
            },
        )
        return True
    _commit_source_bundle(
        path.parents[2],
        {**dict(row), "source_analysis_bundle": normalized.to_dict()},
        path,
        request,
        source_match_index=source_match_index,
    )
    return True


def _compatible_committed_note(
    workspace: Path,
    row: Mapping[str, Any],
    request: MapRequest,
    *,
    source_match_index: Mapping[str, Any] | None = None,
) -> Path | None:
    """Reuse a current-schema note when only the processing budget changed.

    Processing limits belong in paid-call checkpoint identities, but changing a
    timeout or chunk budget must not force the same provider and prompt to reread
    source content that already produced a valid committed note. Restrict this
    fallback to a committed note that passes the current validator and still
    matches the source content, scope, provider, model, prompt, extraction
    version, and source item type. Mutable Zotero bibliography fields are
    projected locally onto the existing note. This also honors the migration
    policy for readable legacy notes without rereading their source documents.
    """

    fingerprint_root = workspace / "11_state" / "fingerprints"
    if not fingerprint_root.is_dir():
        return None
    zotero_item_key = str(row.get("zotero_item_key") or "")
    source_id = str(row.get("source_id") or "")
    candidates: list[Path] = []
    for fingerprint_path in fingerprint_root.glob("*.yml"):
        payload = read_yaml(fingerprint_path, {}) or {}
        if str(payload.get("zotero_item_key") or "") != zotero_item_key:
            continue
        recorded_source_id = str(payload.get("source_id") or "")
        if source_id and recorded_source_id and recorded_source_id != source_id:
            continue
        note_path = workspace / str(payload.get("note_path") or "")
        if note_path.is_file():
            candidates.append(note_path)
    for note_path in sorted(set(candidates), key=lambda path: str(path)):
        if _reusable_note(
            note_path,
            row,
            request,
            source_match_index=source_match_index,
        ):
            return note_path
    return None


def _legacy_note_metadata_matches(note_path: Path, item: Mapping[str, Any]) -> bool:
    """Check prompt-visible fields for fingerprints written before metadata hashes."""

    try:
        frontmatter = read_note(note_path)["frontmatter"]
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
    provider_budget: _ProfileProviderBudget | None = None,
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
    source_chunk_output_tokens = max(
        policy.chunk_output_tokens, SOURCE_CHUNK_MAX_OUTPUT_TOKENS
    )
    common_identity = {
        "document_hash": document_hash,
        "provider": str(getattr(reader, "name", "unknown")),
        "model": str(getattr(reader, "model", "unknown")),
        "prompt_version": request.prompt_version if request else "11",
        "source_bundle_prompt_version": SOURCE_BUNDLE_PROMPT_VERSION,
        "chunking_version": CHUNKING_VERSION,
        "content_classifier_version": CONTENT_CLASSIFIER_VERSION,
        "question_hash": sha256_text(question or ""),
        "metadata_hash": _source_read_metadata_hash(metadata),
        "chunk_output_tokens": source_chunk_output_tokens,
        "synthesis_output_tokens": policy.synthesis_output_tokens,
    }
    bundle_reader = getattr(reader, "read_source_bundle", None)
    bundle_fit = getattr(reader, "should_read_source_bundle_directly", None)
    direct_admitted = len(text) <= direct_limit and (
        not callable(bundle_reader)
        or not callable(bundle_fit)
        or bool(bundle_fit(text, metadata, question))
    )
    if direct_admitted:
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
                _ensure_source_result_contract(dict(direct_checkpoint["analysis"])),
                f"{reader.name}_text",
                "reused_direct_source_checkpoint",
            )
        attempt_id = (
            provider_budget.reserve(
                "source_bundle_direct",
                str(
                    (
                        metadata.get("_source_context", {})
                        if isinstance(metadata.get("_source_context"), Mapping)
                        else {}
                    ).get("zotero_key")
                    or document_hash
                ),
                stable_hash(direct_identity),
            )
            if provider_budget is not None
            else ""
        )
        try:
            if progress is not None:
                progress.record_source_provider_call()
            if callable(bundle_reader):
                analysis = dict(bundle_reader(text, metadata, question))
                SourceAnalysisBundle.from_dict(analysis)
            else:
                analysis = _ensure_analysis_contract(
                    dict(reader.read_source(text, metadata, question))
                )
            if provider_budget is not None:
                provider_budget.finish(attempt_id, status="completed")
            if checkpoint_enabled:
                write_yaml(
                    direct_path,
                    {
                        "identity": direct_identity,
                        "analysis": analysis,
                        "updated_at": now_iso(),
                    },
                )
            return (
                _ensure_source_result_contract(analysis),
                f"{reader.name}_text",
                "full_document_source_read",
            )
        except Exception as exc:
            if provider_budget is not None:
                provider_budget.finish(attempt_id, status="failed")
            message = str(exc).casefold()
            if not any(
                token in message
                for token in (
                    "context length",
                    "context window",
                    "input token",
                    "maximum context",
                    "context budget",
                    "prompt too long",
                    "too large",
                    "request size",
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
            attempt_id = (
                provider_budget.reserve(
                    "source_chunk",
                    str(
                        (
                            metadata.get("_source_context", {})
                            if isinstance(metadata.get("_source_context"), Mapping)
                            else {}
                        ).get("zotero_key")
                        or document_hash
                    ),
                    stable_hash(
                        {
                            **checkpoint_identity,
                            "chunk_index": index,
                            "chunk_hash": sha256_text(chunk),
                        }
                    ),
                )
                if provider_budget is not None
                else ""
            )
            try:
                if progress is not None:
                    progress.record_source_provider_call()
                if hasattr(reader, "summarize_chunk"):
                    analysis = reader.summarize_chunk(  # type: ignore[attr-defined]
                        chunk,
                        metadata,
                        question,
                        chunk_id=f"chunk-{index + 1:04d}",
                        locator=locator,
                        max_output_tokens=source_chunk_output_tokens,
                        deadline_seconds=policy.request_deadline_seconds,
                    )
                else:
                    analysis = reader.read_source(chunk, metadata, question)
            except Exception:
                if provider_budget is not None:
                    provider_budget.finish(attempt_id, status="failed")
                raise
            if provider_budget is not None:
                provider_budget.finish(attempt_id, status="completed")
            # One chunk call is one document-budget unit. A reader-wide
            # transport counter races when independent documents are read in
            # parallel and can incorrectly charge one document for another.
            calls += 1
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
        merged = _ensure_source_result_contract(dict(synthesis["analysis"]))
    elif hasattr(reader, "synthesize_document_bundle") or hasattr(
        reader, "synthesize_document"
    ):
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
        attempt_id = (
            provider_budget.reserve(
                "source_bundle_synthesis",
                str(
                    (
                        metadata.get("_source_context", {})
                        if isinstance(metadata.get("_source_context"), Mapping)
                        else {}
                    ).get("zotero_key")
                    or document_hash
                ),
                stable_hash(checkpoint_identity),
            )
            if provider_budget is not None
            else ""
        )
        try:
            if progress is not None:
                progress.record_source_provider_call()
            bundle_synthesizer = getattr(reader, "synthesize_document_bundle", None)
            if callable(bundle_synthesizer):
                merged = _ensure_source_result_contract(
                    dict(
                        bundle_synthesizer(
                            analyses,
                            metadata,
                            question,
                            max_output_tokens=SOURCE_BUNDLE_MAX_OUTPUT_TOKENS,
                            deadline_seconds=policy.request_deadline_seconds,
                        )
                    )
                )
            else:
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
        except Exception:
            if provider_budget is not None:
                provider_budget.finish(attempt_id, status="failed")
            raise
        if provider_budget is not None:
            provider_budget.finish(attempt_id, status="completed")
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


def _source_read_metadata_hash(metadata: Mapping[str, Any]) -> str:
    """Hash stable identity/extraction context, not mutable display metadata."""

    context = (
        dict(metadata.get("_source_context") or {})
        if isinstance(metadata.get("_source_context"), Mapping)
        else {}
    )
    stable = {
        key: context.get(key)
        for key in (
            "source_id",
            "zotero_key",
            "attachment_key",
            "source_scope",
            "page_count",
            "unresolved_pages",
            "recovered_pages",
            "recovered_page_ratio",
            "content_kind",
            "ordinal_to_printed_page",
            "heading_spans",
            "table_spans",
            "figure_spans",
        )
        if context.get(key) not in (None, "", [], {})
    }
    return sha256_text(
        json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    )


def _ensure_source_result_contract(result: Mapping[str, Any]) -> dict[str, Any]:
    if str(result.get("bundle_schema_version") or "") == "1":
        return SourceAnalysisBundle.from_dict(result).to_dict()
    return _ensure_analysis_contract(result)


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
