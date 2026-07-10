"""Stable public Python API for standalone and Research OS integrations."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .files import now_iso, read_yaml, slugify, write_json, write_yaml
from .indexes import write_source_set
from .models import ArtifactManifest, MapRequest, RunReport, StatusReport
from .obsidian import export_obsidian
from .pipeline import all_workspace_note_rows, rebuild_map, run_pipeline, workspace_source_set
from .ports import ControllerPort, ReaderProvider, VisionProvider, ZoteroClient
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
    if scope == "selected":
        selected = client.selected_collection()
        effective_key = str(selected.get("key") or "")
        effective_scope = "library" if selected.get("scope") == "library" else "collection"
        if effective_scope == "collection" and not effective_key:
            raise ValueError("selected collection has no key")
    items = [dict(row) for row in client.inventory(effective_scope, effective_key or None)]
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
    run_id: str | None = None,
    resume: bool = False,
) -> RunReport:
    return run_pipeline(
        request,
        client=client,
        reader=reader,
        vision=vision,
        controller=controller,
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
    if explicit_run and report_path and not report_path.exists():
        return StatusReport(status="blocked", workspace=root, run_id=run_id, message="run_not_found")
    report = read_yaml(report_path, {}) or {} if report_path else {}
    clusters = (report.get("cluster_map", {}) or {}).get("clusters", [])
    gaps = (report.get("gap_map", {}) or {}).get("gap_candidates", [])
    if not report:
        clusters = (read_yaml(root / "03_literature_synthesis" / "clusters" / "clusters.yml", {}) or {}).get("clusters", [])
        gaps = (read_yaml(root / "03_literature_synthesis" / "gaps" / "gaps.yml", {}) or {}).get("gap_candidates", [])
    source_sets = list((root / "02_source_memory" / "indexes" / "source_sets").glob("*.yml"))
    counts = {
        "inventory_count": int(report.get("inventory_count", 0) or 0),
        "validated_note_count": int(report.get("validated_note_count", 0) or 0),
        "exhausted_count": int(report.get("exhausted_count", 0) or 0),
        "terminal_count": int(report.get("terminal_count", 0) or 0),
        "reused_count": int(report.get("reused_count", 0) or 0),
        "cluster_count": len(clusters) if isinstance(clusters, list) else 0,
        "gap_candidate_count": len(gaps) if isinstance(gaps, list) else 0,
        "source_set_count": len(source_sets),
    }
    status = str(report.get("status") or ("initialized" if root.exists() else "missing"))
    return StatusReport(status=status, workspace=root, run_id=run_id, counts=counts)


def build_map(
    workspace: Path | str,
    *,
    run_id: str | None = None,
    controller: ControllerPort | None = None,
) -> ArtifactManifest:
    del controller  # map building consumes only already-controller-accepted canonical tags
    root = resolve_workspace(workspace)
    assert_compatible(root)
    run_id = run_id or _latest_run_id(root) or f"build-{now_iso().replace(':', '').replace('+00:00', 'Z')}"
    validate_opaque_id(run_id, field="run_id")
    note_rows = all_workspace_note_rows(root)
    source_set = workspace_source_set(root, note_rows, run_id=run_id)
    result = rebuild_map(
        root,
        source_set=source_set,
        note_rows=note_rows,
        terminal_rows=[],
        items=[],
        run_id=run_id,
        question=None,
    )
    manifest = ArtifactManifest(
        status="built",
        workspace=root,
        run_id=run_id,
        created_at=now_iso(),
        artifacts=artifact_rows(root, result["paths"]),
        metadata={
            "source_set": result["source_set"],
            "cluster_map": result["cluster_map"],
            "gap_map": result["gap_map"],
            "literature_packet": result["literature_packet"],
        },
    )
    run_dir = run_directory(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(run_dir / "build_map_manifest.yml", manifest.to_dict())
    return manifest


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
