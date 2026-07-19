"""Stable public Python API for standalone and Research OS integrations."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .files import now_iso, read_yaml, slugify, write_json, write_yaml
from .indexes import write_source_set
from .literature import run_literature_map as run_profile_literature_map
from .migration import migrate_workspace
from .models import (
    ArtifactManifest,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
    NavigationPolicy,
    ProcessingPolicy,
    RunReport,
    StatusReport,
)
from .obsidian import export_obsidian
from .pipeline import (
    _RunProgress,
    _apply_reader_policy,
    all_workspace_note_rows,
    rebuild_map,
    run_pipeline,
    workspace_source_set,
)
from .ports import (
    ControllerPort,
    ExternalDiscoveryProvider,
    LiteratureReasoner,
    ReaderProvider,
    VisionProvider,
    ZoteroClient,
)
from .readers import provider_from_name
from .workspace import (
    IncompatibleArtifactSchemaError,
    artifact_rows,
    assert_compatible,
    initialize,
    load_config,
    resolve_workspace,
    run_directory,
    validate_opaque_id,
)
from .zotero import ZoteroLocalClient

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ENGINE_VERSION",
    "ArtifactManifest",
    "MapRequest",
    "ProcessingPolicy",
    "NavigationPolicy",
    "LiteratureMappingPolicy",
    "LiteratureMapRequest",
    "LiteratureMapReport",
    "RunReport",
    "StatusReport",
    "build_map",
    "doctor",
    "export_to_obsidian",
    "get_status",
    "initialize_workspace",
    "inventory",
    "list_collections",
    "resume_map",
    "run_map",
    "run_literature_map",
]


def initialize_workspace(workspace: Path | str, *, overwrite: bool = False) -> ArtifactManifest:
    return initialize(workspace, overwrite=overwrite)


def doctor(workspace: Path | str, *, client: ZoteroClient | None = None) -> StatusReport:
    root = resolve_workspace(workspace)
    checks: dict[str, Any] = {}
    checks["workspace"] = {"status": "ready" if root.exists() else "missing", "path": str(root)}
    try:
        assert_compatible(root)
        checks["schema"] = {"status": "compatible", "supported": ARTIFACT_SCHEMA_VERSION}
        config = load_config(root)
    except (IncompatibleArtifactSchemaError, OSError) as exc:
        checks["schema"] = {"status": "incompatible", "reason": str(exc), "supported": ARTIFACT_SCHEMA_VERSION}
        config = {}
    zotero = client or ZoteroLocalClient()
    try:
        checks["zotero"] = dict(zotero.status())
    except Exception as exc:
        checks["zotero"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}", "read_only": True}
    provider_name = str(config.get("provider") or "deepseek")
    model = str(config.get("model") or "deepseek-v4-flash")
    checks["provider"] = _provider_check(provider_name, model)
    checks["pdf_extraction"] = {
        "status": "available" if importlib.util.find_spec("pypdf") else "missing",
        "tool": "pypdf",
    }
    tesseract = shutil.which("tesseract")
    checks["ocr"] = {
        "status": "available" if tesseract and importlib.util.find_spec("pypdf") else "missing",
        "tool": "pypdf_embedded_images+tesseract",
        "path": tesseract or "",
        "wired_to_pipeline": True,
    }
    privacy = config.get("privacy", {}) if isinstance(config.get("privacy", {}), Mapping) else {}
    configured_allow_cloud = privacy.get("allow_cloud", False)
    privacy_config_valid = isinstance(configured_allow_cloud, bool)
    checks["privacy"] = {
        "status": "cloud_allowed" if configured_allow_cloud is True else ("local_only" if privacy_config_valid else "invalid_configuration"),
        "allow_cloud": configured_allow_cloud is True,
        "keys_stored_in_workspace": False,
        "per_run_consent_still_required": True,
    }
    obsidian = config.get("obsidian", {}) if isinstance(config.get("obsidian", {}), Mapping) else {}
    target = str(obsidian.get("vault") or "")
    checks["obsidian"] = {
        "status": "configured" if target else "not_configured",
        "vault": target,
        "exists": bool(target and Path(target).expanduser().exists()),
    }
    provider_ready = checks["provider"]["status"] in {"configured", "configured_local"}
    if checks["provider"].get("cloud") and not checks["privacy"]["allow_cloud"]:
        provider_ready = False
        checks["provider"]["route_status"] = "blocked_cloud_consent_required"
    zotero_ready = checks["zotero"].get("status") == "available"
    critical_ready = (
        checks["workspace"]["status"] == "ready"
        and checks["schema"]["status"] == "compatible"
        and zotero_ready
        and provider_ready
    )
    status = "ready" if critical_ready else "blocked"
    return StatusReport(status=status, workspace=root, checks=checks)


def list_collections(*, client: ZoteroClient | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in (client or ZoteroLocalClient()).collections()]


def inventory(
    workspace: Path | str,
    run_id: str,
    scope: str = "library",
    collection_key: str = "",
    limit: int = 0,
    zotero_client: ZoteroClient | None = None,
) -> dict[str, Any]:
    if scope not in {"library", "collection", "selected"}:
        raise ValueError("scope must be library, collection, or selected")
    if scope == "collection" and not collection_key:
        raise ValueError("collection scope requires collection_key")
    if limit < 0:
        raise ValueError("limit cannot be negative")
    validate_opaque_id(run_id, field="run_id")
    root = resolve_workspace(workspace)
    initialize(root)
    client = zotero_client or ZoteroLocalClient()
    effective_key = collection_key
    effective_scope = scope
    collection_name = ""
    if scope == "selected":
        selected = client.selected_collection()
        effective_key = str(selected.get("key") or "")
        collection_name = str(selected.get("name") or "").strip()
        effective_scope = "library" if selected.get("scope") == "library" else "collection"
        if effective_scope == "collection" and not effective_key:
            raise ValueError("selected collection has no key")
    items = [dict(row) for row in client.inventory(effective_scope, effective_key or None)]
    if effective_key and not collection_name:
        try:
            for collection in client.collections():
                data = collection.get("data") if isinstance(collection.get("data"), Mapping) else {}
                key = str(collection.get("key") or data.get("key") or "")
                if key == effective_key:
                    collection_name = str(collection.get("name") or data.get("name") or "").strip()
                    break
        except Exception:
            collection_name = ""
    if limit:
        items = items[:limit]
    inventory_path = root / "01_custody" / "zotero" / "inventory" / f"{slugify(run_id)}.json"
    write_json(inventory_path, items)
    run_dir = run_directory(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "inventory.json", items)
    source_set = write_source_set(
        root,
        run_id=run_id,
        scope=scope,
        collection_key=effective_key or None,
        items=items,
        terminal_rows=[],
        note_rows=[],
        collection_name=collection_name,
    )
    manifest = ArtifactManifest(
        status="inventoried",
        workspace=root,
        run_id=run_id,
        created_at=now_iso(),
        artifacts=artifact_rows(root, [inventory_path, run_dir / "inventory.json", Path(source_set["path"])]),
        metadata={"source_set": source_set},
    )
    write_yaml(run_dir / "inventory_manifest.yml", manifest.to_dict())
    return {
        "status": "inventoried",
        "run_id": run_id,
        "scope": scope,
        "collection_key": effective_key,
        "item_count": len(items),
        "items": items,
        "source_set": source_set,
        "artifact_manifest": manifest.to_dict(),
    }


def run_map(
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
    return run_pipeline(
        request,
        client=client,
        reader=reader,
        vision=vision,
        controller=controller,
        literature_reasoner=literature_reasoner,
        external_discovery=external_discovery,
        run_id=run_id,
        resume=resume,
    )


def resume_map(
    workspace: Path | str,
    run_id: str,
    *,
    client: ZoteroClient | None = None,
    reader: ReaderProvider | None = None,
    vision: VisionProvider | None = None,
    controller: ControllerPort | None = None,
    literature_reasoner: LiteratureReasoner | None = None,
    external_discovery: ExternalDiscoveryProvider | None = None,
) -> RunReport:
    root = resolve_workspace(workspace)
    payload = read_yaml(run_directory(root, run_id) / "request.yml", {}) or {}
    if not payload:
        return RunReport(
            status="blocked",
            workspace=root,
            run_id=run_id,
            errors=[{"reason": "run_request_not_found"}],
        )
    payload["workspace"] = str(root)
    return run_map(
        MapRequest.from_dict(payload),
        client=client,
        reader=reader,
        vision=vision,
        controller=controller,
        literature_reasoner=literature_reasoner,
        external_discovery=external_discovery,
        run_id=run_id,
        resume=True,
    )


def get_status(workspace: Path | str, run_id: str | None = None) -> StatusReport:
    root = resolve_workspace(workspace)
    explicit_run = run_id is not None
    try:
        assert_compatible(root)
    except (IncompatibleArtifactSchemaError, OSError) as exc:
        return StatusReport(status="blocked", workspace=root, run_id=run_id, message=str(exc))
    run_id = run_id or _latest_run_id(root)
    report_path = run_directory(root, run_id) / "run_report.yml" if run_id else None
    progress_path = run_directory(root, run_id) / "progress.yml" if run_id else None
    if explicit_run and report_path and not report_path.exists() and (progress_path is None or not progress_path.exists()):
        return StatusReport(status="blocked", workspace=root, run_id=run_id, message="run_not_found")
    report = read_yaml(report_path, {}) or {} if report_path and report_path.exists() else {}
    progress = read_yaml(progress_path, {}) or {} if progress_path and progress_path.exists() else {}
    live = progress if progress.get("status") == "running" else (report or progress)
    literature_live = live.get("literature_map", {}) if isinstance(live.get("literature_map", {}), Mapping) else {}
    clusters = (report.get("cluster_map", {}) or {}).get("clusters", [])
    gaps = (report.get("gap_map", {}) or {}).get("gap_candidates", [])
    if not report:
        clusters = (read_yaml(root / "03_literature_synthesis" / "clusters" / "clusters.yml", {}) or {}).get("clusters", [])
        gaps = (read_yaml(root / "03_literature_synthesis" / "gaps" / "gaps.yml", {}) or {}).get("gap_candidates", [])
    source_sets = []
    for path in (root / "02_source_memory" / "indexes" / "source_sets").glob("*.yml"):
        payload = read_yaml(path, {}) or {}
        if isinstance(payload, Mapping) and str(payload.get("source_set_id") or "") == path.stem:
            source_sets.append(path)
    counts = {
        "inventory_count": int(live.get("inventory_count", 0) or 0),
        "validated_note_count": int(live.get("validated_note_count", 0) or 0),
        "limited_note_count": int(live.get("limited_note_count", 0) or 0),
        "exhausted_count": int(live.get("exhausted_count", 0) or 0),
        "partial_count": int(live.get("partial_count", 0) or 0),
        "pending_count": int(live.get("pending_count", 0) or 0),
        "terminal_count": int(live.get("terminal_count", 0) or 0),
        "reused_count": int(live.get("reused_count", 0) or 0),
        "profile_count": int(live.get("profile_count", literature_live.get("profile_count", 0)) or 0),
        "profile_valid_count": int(live.get("profile_valid_count", literature_live.get("profile_valid_count", 0)) or 0),
        "profile_excluded_count": int(live.get("profile_excluded_count", literature_live.get("profile_excluded_count", 0)) or 0),
        "unclustered_count": int(live.get("unclustered_count", literature_live.get("unclustered_count", 0)) or 0),
        "topic_neighborhood_count": int(
            live.get("topic_neighborhood_count", literature_live.get("topic_neighborhood_count", 0)) or 0
        ),
        "subject_tag_count": int(
            live.get("subject_tag_count", literature_live.get("subject_tag_count", 0)) or 0
        ),
        "subject_tag_assignment_count": int(
            live.get(
                "subject_tag_assignment_count",
                literature_live.get("subject_tag_assignment_count", 0),
            )
            or 0
        ),
        "typed_relation_count": int(
            live.get("typed_relation_count", literature_live.get("typed_relation_count", 0)) or 0
        ),
        "singleton_facet_count": int(
            live.get("singleton_facet_count", literature_live.get("singleton_facet_count", 0)) or 0
        ),
        "proposition_count": int(
            live.get("proposition_count", literature_live.get("proposition_count", 0)) or 0
        ),
        "evidence_base_group_count": int(
            live.get("evidence_base_group_count", literature_live.get("evidence_base_group_count", 0)) or 0
        ),
        "cluster_count": int(live.get("cluster_count", len(clusters) if isinstance(clusters, list) else 0) or 0),
        "cluster_source_contribution_count": int(
            live.get(
                "cluster_source_contribution_count",
                literature_live.get("cluster_source_contribution_count", 0),
            )
            or 0
        ),
        "debate_count": int(live.get("debate_count", literature_live.get("debate_count", 0)) or 0),
        "mapped_gap_count": int(live.get("mapped_gap_count", literature_live.get("mapped_gap_count", 0)) or 0),
        "gap_lead_count": int(live.get("gap_lead_count", literature_live.get("gap_lead_count", 0)) or 0),
        "synthesized_cluster_count": int(
            live.get("synthesized_cluster_count", literature_live.get("synthesized_cluster_count", 0)) or 0
        ),
        "rejected_underspecified_gap_count": int(
            live.get(
                "rejected_underspecified_gap_count",
                literature_live.get("rejected_underspecified_gap_count", 0),
            )
            or 0
        ),
        "rejected_gap_quality_count": int(
            live.get(
                "rejected_gap_quality_count",
                literature_live.get("rejected_gap_quality_count", 0),
            )
            or 0
        ),
        "merged_gap_count": int(
            live.get("merged_gap_count", literature_live.get("merged_gap_count", 0)) or 0
        ),
        "synthesis_call_count": int(
            live.get("synthesis_call_count", literature_live.get("synthesis_call_count", 0)) or 0
        ),
        "synthesis_checkpoint_hit_count": int(
            live.get(
                "synthesis_checkpoint_hit_count",
                literature_live.get("synthesis_checkpoint_hit_count", 0),
            )
            or 0
        ),
        "synthesis_failure_count": int(
            live.get("synthesis_failure_count", literature_live.get("synthesis_failure_count", 0)) or 0
        ),
        "quantitative_comparison_count": int(
            live.get("quantitative_comparison_count", literature_live.get("quantitative_comparison_count", 0)) or 0
        ),
        "rejected_quantitative_comparison_count": int(
            live.get(
                "rejected_quantitative_comparison_count",
                literature_live.get("rejected_quantitative_comparison_count", 0),
            )
            or 0
        ),
        "rejected_generated_locator_count": int(
            live.get(
                "rejected_generated_locator_count",
                literature_live.get("rejected_generated_locator_count", 0),
            )
            or 0
        ),
        "coverage_inventory_count": int(
            live.get("coverage_inventory_count", literature_live.get("coverage_inventory_count", 0)) or 0
        ),
        "coverage_exhausted_count": int(
            live.get("coverage_exhausted_count", literature_live.get("coverage_exhausted_count", 0)) or 0
        ),
        "gap_candidate_count": len(gaps) if isinstance(gaps, list) else 0,
        "active_count": int(live.get("active_count", 0) or 0),
        "completed_chunk_count": int(live.get("completed_chunk_count", 0) or 0),
        "total_chunk_count": int(live.get("total_chunk_count", 0) or 0),
        "checkpoint_hit_count": int(live.get("checkpoint_hit_count", progress.get("checkpoint_hit_count", 0)) or 0),
        "source_provider_call_count": int(
            live.get("source_provider_call_count", progress.get("source_provider_call_count", 0)) or 0
        ),
        "literature_provider_call_count": int(
            live.get("literature_provider_call_count", progress.get("literature_provider_call_count", 0)) or 0
        ),
        "provider_call_count": int(live.get("provider_call_count", progress.get("provider_call_count", 0)) or 0),
        "literature_failure_count": int(
            live.get("literature_failure_count", progress.get("literature_failure_count", 0)) or 0
        ),
        "internal_falsification_count": int(
            live.get("internal_falsification_count", literature_live.get("internal_falsification_count", 0)) or 0
        ),
        "source_set_count": len(source_sets),
    }
    status = str(live.get("status") or ("initialized" if root.exists() else "missing"))
    return StatusReport(
        status=status,
        workspace=root,
        run_id=run_id,
        counts=counts,
        checks={
            "progress": {
                "stage": str(live.get("stage") or (report.get("literature_report", {}) or {}).get("stage") or ""),
                "stage_timestamps": dict(progress.get("stage_timestamps", {}) or {}),
                "active_item_keys": list(progress.get("active_item_keys", []) or []),
                "active_synthesis_packet": str(progress.get("active_synthesis_packet") or ""),
                "active_cluster": str(progress.get("active_cluster") or ""),
                "active_gap_packet": str(progress.get("active_gap_packet") or ""),
            }
        },
    )


def build_map(
    workspace: Path | str,
    *,
    run_id: str | None = None,
    controller: ControllerPort | None = None,
    source_set: Mapping[str, Any] | Path | str | None = None,
    question: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    allow_cloud: bool = False,
    literature_policy: LiteratureMappingPolicy | Mapping[str, Any] | None = None,
    navigation_policy: NavigationPolicy | Mapping[str, Any] | None = None,
    reasoner: LiteratureReasoner | None = None,
    external_discovery: ExternalDiscoveryProvider | None = None,
    resume: bool = False,
) -> ArtifactManifest:
    del controller  # synthesis consumes validated notes and accepted typed-link evidence only
    if not isinstance(allow_cloud, bool):
        raise ValueError("allow_cloud must be a boolean")
    root = resolve_workspace(workspace)
    assert_compatible(root)
    config = load_config(root)
    provider = provider or str(config.get("provider") or "deepseek")
    model = model or str(config.get("model") or "deepseek-v4-flash")
    configured_policy = config.get("literature_mapping", {}) if isinstance(config.get("literature_mapping", {}), Mapping) else {}
    policy = (
        literature_policy
        if isinstance(literature_policy, LiteratureMappingPolicy)
        else LiteratureMappingPolicy.from_dict(literature_policy if isinstance(literature_policy, Mapping) else configured_policy)
    )
    configured_navigation = config.get("navigation", {}) if isinstance(config.get("navigation", {}), Mapping) else {}
    navigation = (
        navigation_policy
        if isinstance(navigation_policy, NavigationPolicy)
        else NavigationPolicy.from_dict(
            navigation_policy if isinstance(navigation_policy, Mapping) else configured_navigation
        )
    )
    if policy.external_discovery != "disabled":
        raise ValueError(
            f"external discovery is disabled in standalone Auto-Zettelkasten: {policy.external_discovery}"
        )
    if external_discovery is not None:
        raise ValueError("standalone Auto-Zettelkasten does not accept an external discovery provider")
    if reasoner is None and policy.synthesis_enabled and allow_cloud:
        built_in_reasoner = provider_from_name(provider, model, allow_cloud=allow_cloud)
        if not isinstance(built_in_reasoner, LiteratureReasoner):
            raise ValueError(f"provider {provider} does not implement literature reasoning")
        reasoner = built_in_reasoner
    if reasoner is not None and bool(getattr(reasoner, "is_cloud", True)) and not allow_cloud:
        raise ValueError("cloud literature reasoner requires allow_cloud=True")
    if external_discovery is not None and bool(getattr(external_discovery, "is_cloud", True)) and not allow_cloud:
        raise ValueError("cloud external discovery provider requires allow_cloud=True")
    run_id = run_id or _latest_run_id(root) or f"build-{now_iso().replace(':', '').replace('+00:00', 'Z')}"
    validate_opaque_id(run_id, field="run_id")
    note_rows = all_workspace_note_rows(root)
    selected_source_set = _resolve_source_set(root, source_set)
    if selected_source_set:
        allowed_note_ids = {str(value) for value in selected_source_set.get("note_ids", []) or []}
        if allowed_note_ids:
            note_rows = [row for row in note_rows if str(row.get("note_id") or "") in allowed_note_ids]
    else:
        selected_source_set = workspace_source_set(root, note_rows, run_id=run_id)
    map_request = MapRequest(
        workspace=root,
        provider=provider,
        model=model,
        allow_cloud=allow_cloud,
        question=question,
        processing=ProcessingPolicy.from_dict(
            config.get("processing") if isinstance(config.get("processing"), Mapping) else {}
        ),
        literature_policy=policy,
        navigation_policy=navigation,
    )
    if reasoner is not None:
        _apply_reader_policy(reasoner, map_request.processing)  # type: ignore[arg-type]
    source_rows = selected_source_set.get("rows", []) if isinstance(selected_source_set, Mapping) else []
    progress_items = [
        {**dict(row), "key": str(row.get("zotero_item_key") or "")}
        for row in source_rows
        if isinstance(row, Mapping)
    ]
    progress = _RunProgress(
        run_directory(root, run_id) / "progress.yml",
        run_id,
        progress_items,
        resume=resume,
    )
    progress.set_stage("preflight")
    try:
        result = rebuild_map(
            root,
            source_set=selected_source_set,
            note_rows=note_rows,
            terminal_rows=[],
            items=[],
            run_id=run_id,
            question=question,
            request=map_request,
            reasoner=reasoner,
            external_discovery=external_discovery,
            progress=progress,
            resume=resume,
        )
    except Exception:
        progress.finish("partial")
        raise
    literature_summary = {
        "status": "partial" if result.get("partial_reason") else "completed",
        "profile_count": len(result.get("profiles", []) or []),
        "profile_valid_count": int((result.get("profile_result", {}) or {}).get("valid_count", 0) or 0),
        "profile_excluded_count": int((result.get("profile_result", {}) or {}).get("excluded_count", 0) or 0),
        "unclustered_count": len(result["cluster_map"].get("unclustered_sources", []) or []),
        "cluster_count": len(result["cluster_map"].get("clusters", []) or []),
        "mapped_gap_count": sum(
            1
            for row in result["gap_map"].get("gap_candidates", []) or []
            if row.get("status") == "collection_surviving_gap"
        ),
        "gap_lead_count": sum(
            1
            for row in result["gap_map"].get("gap_candidates", []) or []
            if row.get("status") == "collection_gap_lead"
        ),
        "synthesized_cluster_count": int(result["cluster_map"].get("synthesized_cluster_count", 0) or 0),
        "rejected_underspecified_gap_count": int(
            result["gap_map"].get("rejected_underspecified_gap_count", 0) or 0
        ),
        "rejected_gap_quality_count": int(
            result["gap_map"].get("rejected_gap_quality_count", 0) or 0
        ),
        "merged_gap_count": int(result["gap_map"].get("merged_gap_count", 0) or 0),
        "synthesis_call_count": int(result["literature_packet"].get("synthesis_call_count", 0) or 0),
        "synthesis_checkpoint_hit_count": int(
            result["literature_packet"].get("synthesis_checkpoint_hit_count", 0) or 0
        ),
        "synthesis_failure_count": int(result["literature_packet"].get("synthesis_failure_count", 0) or 0),
        "partial_reason": str(result.get("partial_reason") or ""),
        "profile_result": result.get("profile_result", {}),
    }
    manifest = ArtifactManifest(
        status="partial" if result.get("partial_reason") else "built",
        workspace=root,
        run_id=run_id,
        created_at=now_iso(),
        artifacts=artifact_rows(root, result["paths"]),
        metadata={
            "source_set": result["source_set"],
            "cluster_map": result["cluster_map"],
            "gap_map": result["gap_map"],
            "literature_packet": result["literature_packet"],
            "literature_map": literature_summary,
        },
    )
    run_dir = run_directory(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(run_dir / "build_map_manifest.yml", manifest.to_dict())
    progress.set_stage("reporting")
    progress.finish("partial" if result.get("partial_reason") else "completed")
    return manifest


def run_literature_map(
    request: LiteratureMapRequest,
    *,
    source_set: Mapping[str, Any] | Path | str | None = None,
    profiles: Sequence[Any] | None = None,
    reasoner: LiteratureReasoner | None = None,
    external_discovery: ExternalDiscoveryProvider | None = None,
    resume: bool = False,
) -> LiteratureMapReport:
    selected_source_set = source_set or request.source_set_id or None
    if external_discovery is not None:
        return LiteratureMapReport(
            status="blocked",
            map_id=request.map_id,
            run_id=request.run_id,
            source_set_id=request.source_set_id,
            stage="policy_gate",
            partial_reason="external_discovery_provider_not_used_by_standalone_mapper",
        )
    if request.literature_policy.external_discovery != "disabled":
        return LiteratureMapReport(
            status="blocked",
            map_id=request.map_id,
            run_id=request.run_id,
            source_set_id=request.source_set_id,
            stage="policy_gate",
            partial_reason=(
                "external_discovery_disabled_in_standalone_mapper:"
                f"{request.literature_policy.external_discovery}"
            ),
        )
    if profiles is not None:
        resolved_source_set = _resolve_source_set(resolve_workspace(request.workspace), selected_source_set)
        if not resolved_source_set:
            raise ValueError("source_set is required when profiles are supplied")
        migrate_workspace(request.workspace)
        return run_profile_literature_map(
            request,
            profiles=profiles,
            source_set=resolved_source_set,
            reasoner=reasoner,
        )
    manifest = build_map(
        request.workspace,
        run_id=request.run_id or None,
        source_set=selected_source_set,
        question=request.question,
        provider=request.provider,
        model=request.model,
        allow_cloud=request.allow_cloud,
        literature_policy=request.literature_policy,
        reasoner=reasoner,
        external_discovery=external_discovery,
        resume=resume,
    )
    summary = manifest.metadata.get("literature_map", {}) if isinstance(manifest.metadata, Mapping) else {}
    source_payload = manifest.metadata.get("source_set", {}) if isinstance(manifest.metadata, Mapping) else {}
    return LiteratureMapReport(
        status="partial" if manifest.status == "partial" else "completed",
        map_id=str(request.map_id or summary.get("map_id") or ""),
        run_id=str(manifest.run_id or request.run_id),
        source_set_id=str(source_payload.get("source_set_id") or request.source_set_id),
        stage="reporting" if manifest.status != "partial" else "profiling",
        counts={key: int(value) for key, value in summary.items() if key.endswith("_count") and isinstance(value, int)},
        artifact_paths={row["path"]: row["path"] for row in manifest.artifacts if row.get("path")},
        partial_reason=str(summary.get("partial_reason") or ""),
    )


def _resolve_source_set(root: Path, value: Mapping[str, Any] | Path | str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    candidate = Path(value).expanduser() if isinstance(value, (str, Path)) else Path(str(value))
    if not candidate.is_absolute():
        workspace_candidate = root / candidate
        identifier_candidate = root / "02_source_memory" / "indexes" / "source_sets" / f"{candidate}.yml"
        candidate = workspace_candidate if workspace_candidate.is_file() else identifier_candidate
    payload = read_yaml(candidate, {}) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"source set must be a mapping: {candidate}")
    if not payload:
        raise ValueError(f"source set not found: {candidate}")
    return dict(payload)


def export_to_obsidian(
    workspace: Path | str,
    vault: Path | str,
    *,
    folder: str = "Auto-Zettelkasten",
    project_folder: str = "",
    dry_run: bool = False,
    replace: bool = False,
    new_vault: bool = False,
    record_link: bool = True,
) -> ArtifactManifest:
    return export_obsidian(
        workspace,
        vault,
        folder=folder,
        project_folder=project_folder,
        dry_run=dry_run,
        replace=replace,
        new_vault=new_vault,
        record_link=record_link,
    )


def _provider_check(provider: str, model: str) -> dict[str, Any]:
    env_by_provider = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    if provider == "ollama":
        return {"status": "configured_local", "provider": provider, "model": model, "key_required": False, "cloud": False}
    env = env_by_provider.get(provider)
    return {
        "status": "configured" if env and os.getenv(env) else "missing_key",
        "provider": provider,
        "model": model,
        "key_environment_variable": env or "",
        "key_stored_in_workspace": False,
        "cloud": True,
    }


def _latest_run_id(root: Path) -> str | None:
    runs = [path for path in (root / "11_state" / "runs").glob("*") if path.is_dir()]
    return max(runs, key=lambda path: path.stat().st_mtime).name if runs else None
