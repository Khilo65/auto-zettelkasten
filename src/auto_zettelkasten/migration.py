from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .files import (
    atomic_write_text,
    now_iso,
    read_yaml,
    safe_filename,
    sha256_file,
    sha256_text,
    slugify,
    write_yaml,
)
from .notes import (
    COMPATIBILITY_REQUIRED_SECTION_HEADINGS,
    REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION,
    REVIEW_STATUS_TARGET_ENGINE_VERSION,
    SECTION_HEADINGS,
    item_data,
    item_key,
    legacy_semantic_note_hash_v1,
    parse_atomic_note,
    semantic_note_hash,
    source_id_for_item,
    strip_review_status_material,
)
from .models import (
    CURRENT_ARTIFACT_SCHEMA_VERSION,
    CURRENT_ATOMIC_PROMPT_VERSION,
    CURRENT_ENGINE_VERSION,
    NavigationPolicy,
)
from .workspace import resolve_workspace

MIGRATION_ID = "auto-zettelkasten-0.3-literature-map"
MIGRATION_VERSION = "1"
REVIEW_MIGRATION_ID = "auto-zettelkasten-0.4-review-status-removal"
REVIEW_MIGRATION_VERSION = "1"
GAP_QUALITY_MIGRATION_ID = "auto-zettelkasten-0.5-gap-quality"
GAP_QUALITY_MIGRATION_VERSION = "1"
PROPOSITION_ANCHOR_MIGRATION_ID = "auto-zettelkasten-0.6-proposition-anchors"
PROPOSITION_ANCHOR_MIGRATION_VERSION = "1"
PROPOSITION_ANCHOR_TARGET_ENGINE_VERSION = "0.6.0"
PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION = "1.5"
NAVIGATION_MIGRATION_ID = "auto-zettelkasten-0.7-tag-graph-projection"
NAVIGATION_MIGRATION_VERSION = "1"
NAVIGATION_TARGET_ENGINE_VERSION = "0.7.0"
NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION = "1.6"
RESEARCHER_GRADE_MIGRATION_ID = "auto-zettelkasten-0.8-researcher-grade-synthesis"
RESEARCHER_GRADE_MIGRATION_VERSION = "1"

RESEARCHER_GRADE_TARGET_ENGINE_VERSION = "0.8.0"

RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION = "1.7"

DEBATE_FAMILY_MIGRATION_ID = "auto-zettelkasten-0.9-debate-family-mapping"

DEBATE_FAMILY_MIGRATION_VERSION = "1"

DEBATE_FAMILY_TARGET_ENGINE_VERSION = "0.9.0"

DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION = "1.8"

THEMATIC_CLUSTER_MIGRATION_ID = "auto-zettelkasten-0.10-thematic-cluster-mapping"

THEMATIC_CLUSTER_MIGRATION_VERSION = "1"

THEMATIC_CLUSTER_TARGET_ENGINE_VERSION = "0.10.0"

THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION = "1.9"

MANAGED_GRAPH_MIGRATION_ID = "auto-zettelkasten-0.11-managed-graph-markers"

MANAGED_GRAPH_MIGRATION_VERSION = "1"

MANAGED_GRAPH_TARGET_ENGINE_VERSION = "0.11.0"

MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION = "1.10"

VERIFIED_GRAPH_MIGRATION_ID = "auto-zettelkasten-0.12-verified-relationship-graph"

VERIFIED_GRAPH_MIGRATION_VERSION = "1"

VERIFIED_GRAPH_TARGET_ENGINE_VERSION = "0.12.0"

VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION = "1.11"

V013_MIGRATION_ID = "auto-zettelkasten-0.13-source-bundle-graph"
V013_MIGRATION_VERSION = "1"
V013_TARGET_ENGINE_VERSION = "0.13.0"
V013_TARGET_ARTIFACT_SCHEMA_VERSION = "1.12"

V014_MIGRATION_ID = "auto-zettelkasten-0.14-streamlined-full-note-synthesis"
V014_MIGRATION_VERSION = "1"
V014_TARGET_ENGINE_VERSION = "0.14.0"
V014_TARGET_ARTIFACT_SCHEMA_VERSION = "1.13"

V015_MIGRATION_ID = "auto-zettelkasten-0.15-statistical-explanation-metadata"
V015_TARGET_ENGINE_VERSION = "0.15.0"
V015_TARGET_ARTIFACT_SCHEMA_VERSION = "1.13"
V016_MIGRATION_ID = "auto-zettelkasten-0.16-scalable-discovery-replay"
V029_MIGRATION_ID = "auto-zettelkasten-0.29-lean-index-state"

_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "archive_directory",
    "archived_files",
    "completed_at",
}
_REVIEW_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "archive_directory",
    "rewritten_files",
    "hash_aliases",
    "source_notes_rewritten",
    "provider_calls",
    "completed_at",
}
_GAP_QUALITY_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "archive_directory",
    "rewritten_files",
    "provider_calls",
    "completed_at",
}
_PROPOSITION_ANCHOR_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "rewritten_files",
    "archived_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "profile_upgrade",
    "completed_at",
}
_NAVIGATION_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "archive_directory",
    "archived_files",
    "rewritten_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "analytical_identity_changes",
    "completed_at",
}
_RESEARCHER_GRADE_MARKER_FIELDS = set(_NAVIGATION_MARKER_FIELDS)

_DEBATE_FAMILY_MARKER_FIELDS = set(_NAVIGATION_MARKER_FIELDS)

_THEMATIC_CLUSTER_MARKER_FIELDS = set(_NAVIGATION_MARKER_FIELDS)

_MANAGED_GRAPH_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "rewritten_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "completed_at",
}

_VERIFIED_GRAPH_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "rewritten_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "legacy_relationships_deactivated",
    "human_relationships_preserved",
    "completed_at",
}

_V013_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "rewritten_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "zotero_calls",
    "legacy_relationships_pending",
    "legacy_source_bundles_created",
    "legacy_source_bundle_conflicts",
    "fidelity_drafts_recovered",
    "partial_documents_promoted",
    "completed_at",
}

_V014_MARKER_FIELDS = {
    "migration_id",
    "migration_version",
    "status",
    "target_engine_version",
    "target_artifact_schema_version",
    "rewritten_files",
    "provider_calls",
    "source_documents_reread",
    "source_notes_rewritten",
    "profile_files_rewritten",
    "legacy_cluster_syntheses_marked",
    "relationship_rows_consolidated",
    "stale_cluster_memberships_retired",
    "global_cluster_registry_selected",
    "completed_at",
}

_REVIEW_FIELDS = {"human_review", "review_status", "source_faithfulness_review"}
_VERSION_FILE_RELATIVES = ("auto-zettelkasten.yml", "11_state/workspace_manifest.yml")
_RELATIONSHIP_REGISTRY_RELATIVES = (
    "02_source_memory/indexes/typed_links.yml",
    "02_source_memory/indexes/typed_note_links.yml",
)


def migrate_literature_map(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Archive pre-0.3 generated map projections without rewriting source notes."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{MIGRATION_ID}.yml"
    if marker.exists():
        payload = read_yaml(marker, {})
        _validate_marker(root, payload)
        return {
            "status": "already_migrated",
            "dry_run": dry_run,
            "migration_id": MIGRATION_ID,
            "archive_directory": str(payload.get("archive_directory") or ""),
            "archived_files": list(payload.get("archived_files", []) or []),
            "source_notes_rewritten": False,
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is not None and schema_version >= (1, 2):
        return _literature_map_not_applicable(dry_run=dry_run, reason="schema_1.2_or_newer")

    legacy_paths = _legacy_generated_files(root)
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.3-{slugify(timestamp)}"
    planned = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in legacy_paths
    ]
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": MIGRATION_ID,
            "archive_directory": str(archive),
            "archived_files": planned,
            "source_notes_rewritten": False,
        }

    for row, source in zip(planned, legacy_paths, strict=True):
        target = root / str(row["archive"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    payload = {
        "migration_id": MIGRATION_ID,
        "migration_version": MIGRATION_VERSION,
        "status": "completed",
        "archive_directory": str(archive),
        "archived_files": planned,
        "completed_at": now_iso(),
    }
    write_yaml(marker, payload)
    return {
        "status": "migrated",
        "dry_run": False,
        "migration_id": MIGRATION_ID,
        "archive_directory": str(archive),
        "archived_files": planned,
        "source_notes_rewritten": False,
        "marker": str(marker),
    }


def migrate_workspace(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run all local, idempotent schema migrations without provider access."""

    root = resolve_workspace(workspace)
    _validate_existing_markers(root)
    starting_schema = _workspace_schema_version(root)
    if starting_schema is not None and starting_schema >= (1, 2):
        legacy = _literature_map_not_applicable(dry_run=dry_run, reason="schema_1.2_or_newer")
    else:
        legacy = migrate_literature_map(root, dry_run=dry_run)
    review = (
        _review_not_applicable(dry_run=dry_run, reason="schema_1.3_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 3)
        and not (root / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml").is_file()
        else migrate_review_status(workspace, dry_run=dry_run)
    )
    gap_quality = (
        _gap_quality_not_applicable(dry_run=dry_run, reason="schema_1.4_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 4)
        and not (root / "11_state" / "migrations" / f"{GAP_QUALITY_MIGRATION_ID}.yml").is_file()
        else migrate_gap_quality_schema(workspace, dry_run=dry_run)
    )
    proposition_anchors = (
        _proposition_anchor_not_applicable(dry_run=dry_run, reason="schema_1.5_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 5)
        and not (root / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml").is_file()
        else migrate_proposition_anchor_schema(workspace, dry_run=dry_run)
    )
    navigation = (
        _navigation_not_applicable(dry_run=dry_run, reason="schema_1.6_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 6)
        and not (root / "11_state" / "migrations" / f"{NAVIGATION_MIGRATION_ID}.yml").is_file()
        else migrate_navigation_projection_schema(workspace, dry_run=dry_run)
    )
    researcher_grade = (
        _researcher_grade_not_applicable(dry_run=dry_run, reason="schema_1.7_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 7)
        and not (root / "11_state" / "migrations" / f"{RESEARCHER_GRADE_MIGRATION_ID}.yml").is_file()
        else migrate_researcher_grade_schema(workspace, dry_run=dry_run)
    )
    debate_family = (
        _debate_family_not_applicable(dry_run=dry_run, reason="schema_1.8_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 8)
        and not (root / "11_state" / "migrations" / f"{DEBATE_FAMILY_MIGRATION_ID}.yml").is_file()
        else migrate_debate_family_schema(workspace, dry_run=dry_run)
    )
    thematic_clusters = (
        _thematic_cluster_not_applicable(dry_run=dry_run, reason="schema_1.9_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 9)
        and not (root / "11_state" / "migrations" / f"{THEMATIC_CLUSTER_MIGRATION_ID}.yml").is_file()
        else migrate_thematic_cluster_schema(workspace, dry_run=dry_run)
    )
    managed_graph = (
        _managed_graph_not_applicable(dry_run=dry_run, reason="schema_1.10_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 10)
        and not (root / "11_state" / "migrations" / f"{MANAGED_GRAPH_MIGRATION_ID}.yml").is_file()
        else migrate_managed_graph_schema(workspace, dry_run=dry_run)
    )
    verified_graph = (
        _verified_graph_not_applicable(dry_run=dry_run, reason="schema_1.11_or_newer")
        if starting_schema is not None
        and starting_schema >= (1, 11)
        and not (
            root / "11_state" / "migrations" / f"{VERIFIED_GRAPH_MIGRATION_ID}.yml"
        ).is_file()
        else migrate_verified_relationship_graph_schema(workspace, dry_run=dry_run)
    )
    v013 = (
        {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V013_MIGRATION_ID,
            "reason": "schema_1.12_or_newer",
            "provider_calls": 0,
        }
        if starting_schema is not None
        and starting_schema >= (1, 12)
        and not (
            root / "11_state" / "migrations" / f"{V013_MIGRATION_ID}.yml"
        ).is_file()
        else migrate_v013_schema(workspace, dry_run=dry_run)
    )
    v014 = migrate_v014_schema(workspace, dry_run=dry_run)
    v015 = migrate_v015_metadata(workspace, dry_run=dry_run)
    v016 = migrate_v016_metadata(workspace, dry_run=dry_run)
    v029 = migrate_v029_lean_state(workspace, dry_run=dry_run)
    return {
        "status": "dry_run" if dry_run else "completed",
        "dry_run": dry_run,
        "provider_calls": 0,
        "migrations": [
            legacy,
            review,
            gap_quality,
            proposition_anchors,
            navigation,
            researcher_grade,
            debate_family,
            thematic_clusters,
            managed_graph,
            verified_graph,
            v013,
            v014,
            v015,
            v016,
            v029,
        ],
        "literature_map": legacy,
        "review_status": review,
        "gap_quality": gap_quality,
        "proposition_anchors": proposition_anchors,
        "navigation": navigation,
        "researcher_grade": researcher_grade,
        "debate_family": debate_family,
        "thematic_clusters": thematic_clusters,
        "managed_graph": managed_graph,
        "verified_graph": verified_graph,
        "v013": v013,
        "v014": v014,
        "v015": v015,
        "v029": v029,
    }


def migrate_v029_lean_state(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Retire only recognized, reproducible v0.28 state explosions."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{V029_MIGRATION_ID}.yml"
    if marker.is_file():
        return {**dict(read_yaml(marker, {}) or {}), "status": "already_migrated", "dry_run": dry_run}
    candidates = [
        root / "03_literature_synthesis" / "tag_concept_registry.yml",
        *sorted(
            (root / "03_literature_synthesis" / "maps").glob(
                "*/tag_concept_registry.yml"
            )
        ),
        *sorted((root / "11_state" / "runs").glob("*/build_map_manifest.yml")),
    ]
    recognized = []
    for path in candidates:
        if not path.is_file():
            continue
        if path.name == "build_map_manifest.yml" and (
            path.parent / "semantic_build_receipt.yml"
        ).is_file():
            continue
        if path.name == "build_map_manifest.yml":
            with path.open("rb") as handle:
                header = handle.read(65_536).decode("utf-8", errors="replace")
            if not re.search(
                r"artifact_schema_version:\s*['\"]?1\.19(?:['\"]|\s|$)",
                header,
            ):
                continue
        stat = path.stat()
        recognized.append(
            {
                "path": str(path.relative_to(root)),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    payload = {
        "migration_id": V029_MIGRATION_ID,
        "target_engine_version": CURRENT_ENGINE_VERSION,
        "target_artifact_schema_version": CURRENT_ARTIFACT_SCHEMA_VERSION,
        "retired_generated_files": recognized,
        "provider_calls": 0,
        "source_notes_rewritten": 0,
        "cleanup_pending": bool(recognized),
    }
    if dry_run:
        return {"status": "dry_run", "dry_run": True, **payload}
    write_yaml(
        marker,
        {**payload, "status": "completed", "completed_at": now_iso()},
    )
    return {"status": "migrated", "dry_run": False, **payload}


def finalize_v029_lean_state(
    workspace: Path | str, *, replacement_receipt: Path
) -> list[str]:
    """Remove only unchanged legacy files after a compact receipt is durable."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{V029_MIGRATION_ID}.yml"
    if not marker.is_file() or not replacement_receipt.is_file():
        return []
    receipt = read_yaml(replacement_receipt, {}) or {}
    if (
        not isinstance(receipt, Mapping)
        or str(receipt.get("receipt_schema_version") or "") not in {"1", "2"}
        or str(receipt.get("engine_version") or "") != CURRENT_ENGINE_VERSION
        or str(receipt.get("artifact_schema_version") or "")
        != CURRENT_ARTIFACT_SCHEMA_VERSION
        or str(receipt.get("status") or "") != "built"
        or not bool(receipt.get("semantic_replayable"))
        or not str(receipt.get("identity") or "")
    ):
        return []
    payload = read_yaml(marker, {}) or {}
    removed: list[str] = []
    for row in payload.get("retired_generated_files", []) or []:
        if not isinstance(row, Mapping) or not row.get("path"):
            continue
        path = root / str(row["path"])
        if not path.is_file():
            continue
        stat = path.stat()
        if stat.st_size != int(row.get("bytes", -1)) or stat.st_mtime_ns != int(
            row.get("mtime_ns", -1)
        ):
            continue
        path.unlink()
        removed.append(str(row["path"]))
    remaining = [
        str(row.get("path") or "")
        for row in payload.get("retired_generated_files", []) or []
        if isinstance(row, Mapping)
        and row.get("path")
        and (root / str(row["path"])).is_file()
    ]
    updated = {
        **dict(payload),
        "cleanup_pending": bool(remaining),
        "remaining_generated_files": remaining,
        "removed_generated_files": sorted(
            set(payload.get("removed_generated_files", []) or []) | set(removed)
        ),
    }
    if not remaining:
        updated["cleanup_completed_at"] = now_iso()
    if updated != payload:
        write_yaml(marker, updated)
    return removed


def migrate_gap_quality_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Upgrade only workspace version files; existing notes and maps remain untouched."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{GAP_QUALITY_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_gap_quality_marker(root, payload)
        return {**dict(payload), "status": "already_migrated", "dry_run": dry_run}

    schema_version = _workspace_schema_version(root)
    if schema_version is not None and schema_version >= (1, 4):
        return _gap_quality_not_applicable(dry_run=dry_run, reason="schema_1.4_or_newer")

    paths = (root / "auto-zettelkasten.yml", root / "11_state" / "workspace_manifest.yml")
    existing_paths = [path for path in paths if path.is_file()]
    if not existing_paths:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": GAP_QUALITY_MIGRATION_ID,
            "rewritten_files": [],
            "provider_calls": 0,
        }
    if len(existing_paths) != len(paths):
        missing = next(path for path in paths if not path.is_file())
        raise ValueError(f"workspace version file is missing: {missing}")
    changes: list[tuple[Path, str, str]] = []
    for path in paths:
        original = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(original)
        if not isinstance(payload, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(payload)
        updated["engine_version"] = REVIEW_STATUS_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            changes.append((path, original, cleaned))

    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.5-schema-{slugify(timestamp)}"
    planned = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": GAP_QUALITY_MIGRATION_ID,
            "archive_directory": str(archive),
            "rewritten_files": planned,
            "provider_calls": 0,
        }

    for row, (path, _, cleaned) in zip(planned, changes, strict=True):
        target = root / str(row["archive"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        atomic_write_text(path, cleaned)
    payload = {
        "migration_id": GAP_QUALITY_MIGRATION_ID,
        "migration_version": GAP_QUALITY_MIGRATION_VERSION,
        "status": "completed",
        "archive_directory": str(archive),
        "rewritten_files": planned,
        "provider_calls": 0,
        "completed_at": now_iso(),
    }
    write_yaml(marker, payload)
    return {"dry_run": False, **payload, "marker": str(marker)}


def migrate_proposition_anchor_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Record schema-1.5 compatibility without rereading or rewriting research artifacts."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_proposition_anchor_marker(payload)
        return {"dry_run": dry_run, **dict(payload), "status": "already_migrated", "marker": str(marker)}

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _proposition_anchor_not_applicable(dry_run=dry_run, reason="workspace_version_files_absent")
    target_schema = _parse_schema_version(
        PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="proposition-anchor artifact schema",
    )
    if schema_version >= target_schema:
        return _proposition_anchor_not_applicable(dry_run=dry_run, reason="schema_1.5_or_newer")
    if schema_version > target_schema:
        actual = ".".join(str(value) for value in schema_version)
        raise ValueError(
            f"workspace artifact schema {actual} is newer than migration target "
            f"{PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION}"
        )

    changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(original)
        if not isinstance(payload, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(payload)
        updated["engine_version"] = PROPOSITION_ANCHOR_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            changes.append((path, original, cleaned))

    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    safety_report = {
        "target_engine_version": PROPOSITION_ANCHOR_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": rewritten_files,
        "archived_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "profile_upgrade": "lazy_on_read",
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": PROPOSITION_ANCHOR_MIGRATION_ID,
            **safety_report,
        }

    written: list[tuple[Path, str]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        for row in rewritten_files:
            if sha256_file(root / str(row["source"])) != row["after_sha256"]:
                raise RuntimeError(f"migration target checksum mismatch: {row['source']}")
        payload = {
            "migration_id": PROPOSITION_ANCHOR_MIGRATION_ID,
            "migration_version": PROPOSITION_ANCHOR_MIGRATION_VERSION,
            "status": "completed",
            **safety_report,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_navigation_projection_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Upgrade only graph projections and workspace versions to schema 1.6."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{NAVIGATION_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_navigation_marker(root, payload)
        workspace_schema = _workspace_schema_version(root)
        if workspace_schema is None or workspace_schema < _parse_schema_version(
            NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="navigation artifact schema",
        ):
            raise ValueError("completed navigation migration marker disagrees with workspace schema")
        return {"dry_run": dry_run, **dict(payload), "status": "already_migrated", "marker": str(marker)}

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _navigation_not_applicable(dry_run=dry_run, reason="workspace_version_files_absent")
    target_schema = _parse_schema_version(
        NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="navigation artifact schema",
    )
    if schema_version > target_schema:
        return _navigation_not_applicable(dry_run=dry_run, reason="schema_newer_than_1.6")
    if schema_version >= target_schema:
        return _navigation_not_applicable(dry_run=dry_run, reason="schema_1.6_or_newer")

    version_changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = NAVIGATION_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION
        if relative == "auto-zettelkasten.yml" and "navigation" not in updated:
            updated["navigation"] = NavigationPolicy().to_dict()
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            version_changes.append((path, original, cleaned))

    legacy_relatives = (
        "02_source_memory/indexes/tag_registry.yml",
        "02_source_memory/indexes/typed_links.yml",
        "02_source_memory/indexes/typed_note_links.yml",
        "03_literature_synthesis/topic_neighborhoods.yml",
        "03_literature_synthesis/subject_tag_registry.yml",
        "03_literature_synthesis/subject_tag_assignments.yml",
        "03_literature_synthesis/typed_source_relations.yml",
    )
    legacy_paths = [root / relative for relative in legacy_relatives if (root / relative).is_file()]
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_navigation" / f"pre-0.7-{slugify(timestamp)}"
    archived_files = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in legacy_paths
    ]
    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in version_changes
    ]
    safety = {
        "target_engine_version": NAVIGATION_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": str(archive),
        "archived_files": archived_files,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": NAVIGATION_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str]] = []
    try:
        for row, source in zip(archived_files, legacy_paths, strict=True):
            target = root / str(row["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != row["sha256"]:
                raise RuntimeError(f"navigation migration archive checksum mismatch: {target}")
        for path, original, cleaned in version_changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        payload = {
            "migration_id": NAVIGATION_MIGRATION_ID,
            "migration_version": NAVIGATION_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_researcher_grade_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Archive mutable v0.7 projections and advance only workspace version files."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{RESEARCHER_GRADE_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_researcher_grade_marker(root, payload)
        schema = _workspace_schema_version(root)
        target = _parse_schema_version(
            RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="researcher-grade artifact schema",
        )
        if schema is None or schema < target:
            raise ValueError("completed researcher-grade migration marker disagrees with workspace schema")
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _researcher_grade_not_applicable(dry_run=dry_run, reason="workspace_version_files_absent")
    target_schema = _parse_schema_version(
        RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="researcher-grade artifact schema",
    )
    if schema_version > target_schema:
        return _researcher_grade_not_applicable(
            dry_run=dry_run,
            reason="schema_newer_than_1.7",
        )
    if schema_version >= target_schema:
        return _researcher_grade_not_applicable(dry_run=dry_run, reason="schema_1.7_or_newer")

    version_changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = RESEARCHER_GRADE_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            version_changes.append((path, original, cleaned))

    projection_root = root / "03_literature_synthesis"
    legacy_paths: list[Path] = []
    if projection_root.is_dir():
        legacy_paths.extend(path for path in projection_root.glob("*") if path.is_file())
        for relative in ("clusters", "gaps"):
            directory = projection_root / relative
            if directory.is_dir():
                legacy_paths.extend(path for path in directory.rglob("*") if path.is_file())
    legacy_paths = sorted(set(legacy_paths))
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.8-{slugify(timestamp)}"
    archived_files = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in legacy_paths
    ]
    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in version_changes
    ]
    safety = {
        "target_engine_version": RESEARCHER_GRADE_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": str(archive),
        "archived_files": archived_files,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": RESEARCHER_GRADE_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str]] = []
    try:
        for row, source in zip(archived_files, legacy_paths, strict=True):
            target = root / str(row["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != row["sha256"]:
                raise RuntimeError(f"researcher-grade migration archive checksum mismatch: {target}")
        for path, original, cleaned in version_changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        payload = {
            "migration_id": RESEARCHER_GRADE_MIGRATION_ID,
            "migration_version": RESEARCHER_GRADE_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_debate_family_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Archive mutable v0.8 projections and advance workspace versions to schema 1.8."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{DEBATE_FAMILY_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_debate_family_marker(root, payload)
        schema = _workspace_schema_version(root)
        target = _parse_schema_version(
            DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="debate-family artifact schema",
        )
        if schema is None or schema < target:
            raise ValueError("completed debate-family migration marker disagrees with workspace schema")
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _debate_family_not_applicable(dry_run=dry_run, reason="workspace_version_files_absent")
    target_schema = _parse_schema_version(
        DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="debate-family artifact schema",
    )
    if schema_version > target_schema:
        actual = ".".join(str(value) for value in schema_version)
        raise ValueError(
            f"workspace artifact schema {actual} is newer than migration target "
            f"{DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION}"
        )
    if schema_version >= target_schema:
        return _debate_family_not_applicable(dry_run=dry_run, reason="schema_1.8_or_newer")
    if schema_version < (1, 7) and not dry_run:
        raise ValueError("debate-family migration requires the schema-1.7 migration first")

    version_changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = DEBATE_FAMILY_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            version_changes.append((path, original, cleaned))

    projection_root = root / "03_literature_synthesis"
    legacy_paths: list[Path] = []
    if projection_root.is_dir():
        legacy_paths.extend(path for path in projection_root.glob("*") if path.is_file())
        for relative in ("clusters", "gaps"):
            directory = projection_root / relative
            if directory.is_dir():
                legacy_paths.extend(path for path in directory.rglob("*") if path.is_file())
    legacy_paths = sorted(set(legacy_paths))
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.9-{slugify(timestamp)}"
    archived_files = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in legacy_paths
    ]
    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in version_changes
    ]
    safety = {
        "target_engine_version": DEBATE_FAMILY_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": str(archive),
        "archived_files": archived_files,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": DEBATE_FAMILY_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str]] = []
    try:
        for row, source in zip(archived_files, legacy_paths, strict=True):
            target = root / str(row["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != row["sha256"]:
                raise RuntimeError(f"debate-family migration archive checksum mismatch: {target}")
        for path, original, cleaned in version_changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        payload = {
            "migration_id": DEBATE_FAMILY_MIGRATION_ID,
            "migration_version": DEBATE_FAMILY_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_thematic_cluster_schema(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Retire v0.9 Markdown projections and advance workspace versions to schema 1.9."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{THEMATIC_CLUSTER_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_thematic_cluster_marker(root, payload)
        schema = _workspace_schema_version(root)
        target = _parse_schema_version(
            THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="thematic-cluster artifact schema",
        )
        if schema is None or schema < target:
            raise ValueError("completed thematic-cluster migration marker disagrees with workspace schema")
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _thematic_cluster_not_applicable(
            dry_run=dry_run,
            reason="workspace_version_files_absent",
        )
    target_schema = _parse_schema_version(
        THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="thematic-cluster artifact schema",
    )
    if schema_version > target_schema:
        actual = ".".join(str(value) for value in schema_version)
        raise ValueError(
            f"workspace artifact schema {actual} is newer than migration target "
            f"{THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION}"
        )
    if schema_version >= target_schema:
        return _thematic_cluster_not_applicable(
            dry_run=dry_run,
            reason="schema_1.9_or_newer",
        )
    if schema_version < (1, 8) and not dry_run:
        raise ValueError("thematic-cluster migration requires the schema-1.8 migration first")

    version_changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = THEMATIC_CLUSTER_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            version_changes.append((path, original, cleaned))

    # Canonical maps under maps/<map-id>/ are immutable history. Only mutable,
    # researcher-facing compatibility Markdown is retired before v0.10 renders it again.
    projection_root = root / "03_literature_synthesis"
    legacy_paths: list[Path] = []
    if projection_root.is_dir():
        legacy_paths.extend(projection_root.glob("*.md"))
        for relative in ("clusters", "gaps"):
            directory = projection_root / relative
            if directory.is_dir():
                legacy_paths.extend(directory.rglob("*.md"))
    legacy_paths = sorted({path for path in legacy_paths if path.is_file()})
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.10-{slugify(timestamp)}"
    archived_files = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in legacy_paths
    ]
    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in version_changes
    ]
    safety = {
        "target_engine_version": THEMATIC_CLUSTER_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": str(archive),
        "archived_files": archived_files,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": THEMATIC_CLUSTER_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str]] = []
    removed: list[Path] = []
    try:
        for row, source in zip(archived_files, legacy_paths, strict=True):
            target = root / str(row["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != row["sha256"]:
                raise RuntimeError(f"thematic-cluster migration archive checksum mismatch: {target}")
        for path, original, cleaned in version_changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        for source in legacy_paths:
            source.unlink()
            removed.append(source)
        payload = {
            "migration_id": THEMATIC_CLUSTER_MIGRATION_ID,
            "migration_version": THEMATIC_CLUSTER_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path in removed:
            archived = archive / path.relative_to(root)
            if archived.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archived, path)
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_managed_graph_schema(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Advance workspace versions to schema 1.10 without rewriting research artifacts."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{MANAGED_GRAPH_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_managed_graph_marker(payload)
        schema = _workspace_schema_version(root)
        target = _parse_schema_version(
            MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="managed-graph artifact schema",
        )
        if schema is None or schema < target:
            raise ValueError("completed managed-graph migration marker disagrees with workspace schema")
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _managed_graph_not_applicable(
            dry_run=dry_run,
            reason="workspace_version_files_absent",
        )
    target_schema = _parse_schema_version(
        MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="managed-graph artifact schema",
    )
    if schema_version > target_schema:
        actual = ".".join(str(value) for value in schema_version)
        raise ValueError(
            f"workspace artifact schema {actual} is newer than migration target "
            f"{MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION}"
        )
    if schema_version >= target_schema:
        return _managed_graph_not_applicable(
            dry_run=dry_run,
            reason="schema_1.10_or_newer",
        )
    if schema_version < (1, 9) and not dry_run:
        raise ValueError("managed-graph migration requires the schema-1.9 migration first")

    changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = MANAGED_GRAPH_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            changes.append((path, original, cleaned))

    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    safety = {
        "target_engine_version": MANAGED_GRAPH_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": MANAGED_GRAPH_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        for row in rewritten_files:
            if sha256_file(root / str(row["source"])) != row["after_sha256"]:
                raise RuntimeError(f"migration target checksum mismatch: {row['source']}")
        payload = {
            "migration_id": MANAGED_GRAPH_MIGRATION_ID,
            "migration_version": MANAGED_GRAPH_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_verified_relationship_graph_schema(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Advance to schema 1.11 and quarantine unverified machine relationships."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{VERIFIED_GRAPH_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_verified_graph_marker(payload)
        schema = _workspace_schema_version(root)
        target = _parse_schema_version(
            VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
            field="verified-graph artifact schema",
        )
        if schema is None or schema < target:
            raise ValueError(
                "completed verified-graph migration marker disagrees with workspace schema"
            )
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is None:
        return _verified_graph_not_applicable(
            dry_run=dry_run,
            reason="workspace_version_files_absent",
        )
    target_schema = _parse_schema_version(
        VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
        field="verified-graph artifact schema",
    )
    if schema_version > target_schema:
        actual = ".".join(str(value) for value in schema_version)
        raise ValueError(
            f"workspace artifact schema {actual} is newer than migration target "
            f"{VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION}"
        )
    if schema_version >= target_schema:
        return _verified_graph_not_applicable(
            dry_run=dry_run,
            reason="schema_1.11_or_newer",
        )
    if schema_version < (1, 10) and not dry_run:
        raise ValueError(
            "verified-relationship-graph migration requires the schema-1.10 migration first"
        )

    changes: list[tuple[Path, str | None, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = dict(value)
        updated["engine_version"] = VERIFIED_GRAPH_TARGET_ENGINE_VERSION
        updated["artifact_schema_version"] = (
            VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        )
        cleaned = yaml.safe_dump(
            updated,
            sort_keys=False,
            allow_unicode=True,
            width=10_000,
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    registry_changes, deactivated, human_preserved = _verified_registry_changes(root)
    changes.extend(registry_changes)
    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original or ""),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    safety = {
        "target_engine_version": VERIFIED_GRAPH_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": (
            VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        ),
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "legacy_relationships_deactivated": deactivated,
        "human_relationships_preserved": human_preserved,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": VERIFIED_GRAPH_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str | None]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        for row in rewritten_files:
            if sha256_file(root / str(row["source"])) != row["after_sha256"]:
                raise RuntimeError(
                    f"migration target checksum mismatch: {row['source']}"
                )
        payload = {
            "migration_id": VERIFIED_GRAPH_MIGRATION_ID,
            "migration_version": VERIFIED_GRAPH_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_v013_schema(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Advance local state to v0.13 without rereading sources or calling providers."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{V013_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_v013_marker(payload)
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }
    schema = _workspace_schema_version(root)
    if schema is None:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V013_MIGRATION_ID,
            "reason": "workspace_version_files_absent",
            "provider_calls": 0,
        }
    target = _parse_schema_version(
        V013_TARGET_ARTIFACT_SCHEMA_VERSION, field="v0.13 artifact schema"
    )
    if schema > target:
        raise ValueError("workspace is newer than the v0.13 migration target")
    if schema == target:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V013_MIGRATION_ID,
            "reason": "schema_1.12_or_newer",
            "provider_calls": 0,
        }
    if schema < (1, 11) and not dry_run:
        raise ValueError("v0.13 migration requires the schema-1.11 migration first")

    changes: list[tuple[Path, str | None, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = {
            **dict(value),
            "engine_version": V013_TARGET_ENGINE_VERSION,
            "artifact_schema_version": V013_TARGET_ARTIFACT_SCHEMA_VERSION,
        }
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    bundle_changes, bundle_stats = _legacy_source_bundle_changes(root)
    changes.extend(bundle_changes)

    legacy_pending = 0
    for relative in _RELATIONSHIP_REGISTRY_RELATIVES:
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"relationship registry must be a mapping: {path}")
        relations = []
        for raw in value.get("relations") or value.get("links") or []:
            if not isinstance(raw, Mapping):
                raise ValueError("relationship registry rows must be mappings")
            row = dict(raw)
            provenance = str(row.get("provenance") or "")
            if (
                not provenance.startswith("human")
                and str(row.get("relation_type") or "")
                not in {"", "cites", "cited_by", "zotero_related"}
            ):
                row["decision_status"] = "legacy_review_pending"
                row["cluster_eligible"] = False
                legacy_pending += 1
            relations.append(row)
        updated = {
            **dict(value),
            "registry_schema_version": "4",
            "relations": relations,
            "links": [row for row in relations if bool(row.get("active", True))],
            "updated_at": now_iso(),
        }
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original or ""),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    safety = {
        "target_engine_version": V013_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": V013_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "zotero_calls": 0,
        "legacy_relationships_pending": legacy_pending,
        **bundle_stats,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": V013_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str | None]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        payload = {
            "migration_id": V013_MIGRATION_ID,
            "migration_version": V013_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_v014_schema(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Advance local state to v0.14 without rewriting notes or calling providers."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{V014_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        _validate_v014_marker(payload)
        return {
            "dry_run": dry_run,
            **dict(payload),
            "status": "already_migrated",
            "marker": str(marker),
        }
    schema = _workspace_schema_version(root)
    if schema is None:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V014_MIGRATION_ID,
            "reason": "workspace_version_files_absent",
            "provider_calls": 0,
        }
    target = _parse_schema_version(
        V014_TARGET_ARTIFACT_SCHEMA_VERSION, field="v0.14 artifact schema"
    )
    if schema > target:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V014_MIGRATION_ID,
            "reason": "schema_newer_than_1.13",
            "provider_calls": 0,
        }
    if schema == target:
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V014_MIGRATION_ID,
            "reason": "schema_1.13_or_newer",
            "provider_calls": 0,
        }
    if schema < (1, 12) and not dry_run:
        raise ValueError("v0.14 migration requires the schema-1.12 migration first")

    changes: list[tuple[Path, str | None, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = {
            **dict(value),
            "engine_version": V014_TARGET_ENGINE_VERSION,
            "artifact_schema_version": V014_TARGET_ARTIFACT_SCHEMA_VERSION,
        }
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    registry_changes, registry_stats = _v014_registry_changes(root)
    changes.extend(registry_changes)

    legacy_cluster_syntheses_marked = 0
    synthesis_root = root / "03_literature_synthesis"
    for path in (
        sorted(synthesis_root.rglob("cluster_syntheses.yml"))
        if synthesis_root.is_dir()
        else []
    ):
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"cluster synthesis registry must be a mapping: {path}")
        syntheses = value.get("syntheses")
        if not isinstance(syntheses, Mapping):
            continue
        updated_syntheses: dict[str, Any] = {}
        for cluster_id, raw in syntheses.items():
            if not isinstance(raw, Mapping):
                updated_syntheses[str(cluster_id)] = raw
                continue
            row = dict(raw)
            if row.get("cluster_contract") != "streamlined-full-note-v1":
                row["legacy_projection"] = True
                row["projection_status"] = "legacy_v013"
                legacy_cluster_syntheses_marked += 1
            updated_syntheses[str(cluster_id)] = row
        updated = {**dict(value), "syntheses": updated_syntheses}
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    rewritten_files = [
        {
            "source": str(path.relative_to(root)),
            "before_sha256": sha256_text(original or ""),
            "after_sha256": sha256_text(cleaned),
        }
        for path, original, cleaned in changes
    ]
    safety = {
        "target_engine_version": V014_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": V014_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": rewritten_files,
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "legacy_cluster_syntheses_marked": legacy_cluster_syntheses_marked,
        **registry_stats,
    }
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": V014_MIGRATION_ID,
            **safety,
        }

    written: list[tuple[Path, str | None]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
        payload = {
            "migration_id": V014_MIGRATION_ID,
            "migration_version": V014_MIGRATION_VERSION,
            "status": "completed",
            **safety,
            "completed_at": now_iso(),
        }
        write_yaml(marker, payload)
    except Exception:
        for path, original in reversed(written):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, original)
        raise
    return {"dry_run": False, **payload, "status": "migrated", "marker": str(marker)}


def migrate_v015_metadata(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update release and prompt metadata without touching semantic artifacts."""

    root = resolve_workspace(workspace)
    schema = _workspace_schema_version(root)
    if schema is not None and schema > (1, 13):
        return {
            "status": "not_applicable",
            "dry_run": dry_run,
            "migration_id": V015_MIGRATION_ID,
            "reason": "schema_newer_than_1.13",
            "provider_calls": 0,
        }
    changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = {
            **dict(value),
            "engine_version": V015_TARGET_ENGINE_VERSION,
            "artifact_schema_version": V015_TARGET_ARTIFACT_SCHEMA_VERSION,
        }
        if relative == "auto-zettelkasten.yml":
            prompt_version = str(updated.get("prompt_version") or "")
            if not prompt_version or (
                prompt_version.isdigit() and int(prompt_version) < 10
            ):
                updated["prompt_version"] = "10"
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    result = {
        "migration_id": V015_MIGRATION_ID,
        "target_engine_version": V015_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": V015_TARGET_ARTIFACT_SCHEMA_VERSION,
        "prompt_version": "10",
        "rewritten_files": [
            {
                "source": str(path.relative_to(root)),
                "before_sha256": sha256_text(original),
                "after_sha256": sha256_text(cleaned),
            }
            for path, original, cleaned in changes
        ],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
    }
    if dry_run:
        return {"status": "dry_run", "dry_run": True, **result}
    written: list[tuple[Path, str]] = []
    try:
        for path, original, cleaned in changes:
            atomic_write_text(path, cleaned)
            written.append((path, original))
    except Exception:
        for path, original in reversed(written):
            atomic_write_text(path, original)
        raise
    return {
        "status": "migrated" if changes else "already_current",
        "dry_run": False,
        **result,
    }


def migrate_v016_metadata(
    workspace: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upgrade release metadata and relationship tiers without model or source calls."""

    root = resolve_workspace(workspace)
    changes: list[tuple[Path, str, str]] = []
    for relative in _VERSION_FILE_RELATIVES:
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        updated = {
            **dict(value),
            "engine_version": CURRENT_ENGINE_VERSION,
            "artifact_schema_version": CURRENT_ARTIFACT_SCHEMA_VERSION,
        }
        if relative == "auto-zettelkasten.yml":
            prompt_version = str(updated.get("prompt_version") or "")
            if not prompt_version or (
                prompt_version.isdigit()
                and int(prompt_version) < int(CURRENT_ATOMIC_PROMPT_VERSION)
            ):
                updated["prompt_version"] = CURRENT_ATOMIC_PROMPT_VERSION
        elif relative == "11_state/workspace_manifest.yml":
            updated["workspace"] = str(root)
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    for relative in (
        "02_source_memory/indexes/typed_links.yml",
        "02_source_memory/indexes/typed_note_links.yml",
    ):
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        value = yaml.safe_load(original)
        if not isinstance(value, Mapping):
            continue
        relations = []
        for raw in value.get("relations") or value.get("links") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            if str(row.get("provenance") or "").startswith(
                "probabilistic_relationship_"
            ):
                row.setdefault(
                    "relationship_tier",
                    "legacy_unclassified"
                    if str(row.get("relation_type") or "") == "complements"
                    else "direct",
                )
            relations.append(row)
        updated = {
            **dict(value),
            "registry_schema_version": (
                "7"
                if str(value.get("registry_schema_version") or "") == "7"
                else "6"
            ),
            "relations": relations,
            "links": [
                row for row in relations if bool(row.get("active", True))
            ],
        }
        semantic = {
            key: item
            for key, item in updated.items()
            if key
            not in {
                "updated_at",
                "revision_hash",
                "graph_projection_hash",
                "relation_counts",
            }
        }
        updated["revision_hash"] = sha256_text(
            json.dumps(
                semantic,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        updated["graph_projection_hash"] = sha256_text(
            json.dumps(
                updated["links"],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        relation_counts: dict[str, int] = {}
        for row in updated["links"]:
            relation_type = str(row.get("relation_type") or "")
            relation_counts[relation_type] = (
                relation_counts.get(relation_type, 0) + 1
            )
        updated["relation_counts"] = dict(sorted(relation_counts.items()))
        cleaned = yaml.safe_dump(
            updated, sort_keys=False, allow_unicode=True, width=10_000
        )
        if cleaned != original:
            changes.append((path, original, cleaned))

    result = {
        "migration_id": V016_MIGRATION_ID,
        "target_engine_version": CURRENT_ENGINE_VERSION,
        "target_artifact_schema_version": CURRENT_ARTIFACT_SCHEMA_VERSION,
        "prompt_version": CURRENT_ATOMIC_PROMPT_VERSION,
        "rewritten_files": [
            {
                "source": str(path.relative_to(root)),
                "before_sha256": sha256_text(original),
                "after_sha256": sha256_text(cleaned),
            }
            for path, original, cleaned in changes
        ],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
    }
    if dry_run:
        return {"status": "dry_run", "dry_run": True, **result}
    for path, _original, cleaned in changes:
        atomic_write_text(path, cleaned)
    return {
        "status": "migrated" if changes else "already_current",
        "dry_run": False,
        **result,
    }


def _v014_registry_changes(
    root: Path,
) -> tuple[list[tuple[Path, str | None, str]], dict[str, int]]:
    """Merge legacy cluster maps and unify the graph registries."""

    changes: list[tuple[Path, str | None, str]] = []
    cluster_path = root / "03_literature_synthesis" / "cluster_registry.yml"
    cluster_candidates = [
        path
        for path in (
            cluster_path,
            *sorted(
                (
                    root / "03_literature_synthesis" / "maps"
                ).glob("*/cluster_registry.yml")
            ),
        )
        if path.is_file()
    ]
    cluster_payloads: dict[Path, Mapping[str, Any]] = {}
    for path in cluster_candidates:
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"cluster registry must be a mapping: {path}")
        rows = payload.get("clusters", []) or []
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise ValueError(f"cluster registry rows must be mappings: {path}")
        cluster_payloads[path] = payload

    def cluster_score(path: Path) -> tuple[int, int, int]:
        rows = cluster_payloads[path].get("clusters", []) or []
        source_ids = {
            str(source_id)
            for row in rows
            for source_id in (
                row.get("source_ids", [])
                or [
                    member.get("source_id")
                    for member in row.get("members", []) or []
                    if isinstance(member, Mapping)
                ]
            )
            if str(source_id)
        }
        return (len(source_ids), len(rows), path == cluster_path)

    selected_cluster_path = (
        max(cluster_candidates, key=cluster_score) if cluster_candidates else None
    )
    selected_cluster_payload = (
        cluster_payloads[selected_cluster_path]
        if selected_cluster_path is not None
        else {}
    )
    merged_clusters: list[dict[str, Any]] = []
    semantic_indexes: dict[str, int] = {}
    seen_member_sets: set[tuple[str, ...]] = set()
    seen_fallback_ids: set[str] = set()
    for path in sorted(cluster_candidates, key=cluster_score, reverse=True):
        for raw in cluster_payloads[path].get("clusters", []) or []:
            row = dict(raw)
            semantic_identity = re.sub(
                r"\s+",
                " ",
                str(row.get("semantic_identity") or "").strip().casefold(),
            )
            raw_source_ids = row.get("source_ids", []) or [
                member.get("source_id")
                for member in row.get("members", []) or []
                if isinstance(member, Mapping)
            ]
            member_set = tuple(
                sorted(
                    {
                        str(source_id)
                        for source_id in raw_source_ids
                        if str(source_id)
                    }
                )
            )
            fallback_id = str(row.get("cluster_id") or "")
            if member_set and member_set in seen_member_sets:
                continue
            if semantic_identity and semantic_identity in semantic_indexes:
                existing = merged_clusters[semantic_indexes[semantic_identity]]
                source_ids = sorted(
                    {
                        *(
                            str(value)
                            for value in (
                                existing.get("source_ids", [])
                                or [
                                    member.get("source_id")
                                    for member in existing.get("members", []) or []
                                    if isinstance(member, Mapping)
                                ]
                            )
                            if str(value)
                        ),
                        *member_set,
                    }
                )
                if source_ids:
                    existing["source_ids"] = source_ids
                    seen_member_sets.add(tuple(source_ids))
                members = {
                    str(member.get("source_id") or ""): dict(member)
                    for member in existing.get("members", []) or []
                    if isinstance(member, Mapping) and member.get("source_id")
                }
                for member in row.get("members", []) or []:
                    if isinstance(member, Mapping) and member.get("source_id"):
                        members.setdefault(
                            str(member["source_id"]), dict(member)
                        )
                if members:
                    existing["members"] = [
                        members[source_id] for source_id in sorted(members)
                    ]
                continue
            if (
                not semantic_identity
                and not member_set
                and fallback_id in seen_fallback_ids
            ):
                continue
            merged_clusters.append(row)
            if semantic_identity:
                semantic_indexes[semantic_identity] = len(merged_clusters) - 1
            if member_set:
                seen_member_sets.add(member_set)
            if fallback_id:
                seen_fallback_ids.add(fallback_id)
    merged_cluster_payload = {
        **dict(selected_cluster_payload),
        "clusters": sorted(
            merged_clusters, key=lambda row: str(row.get("cluster_id") or "")
        ),
    }
    current_cluster_payload = read_yaml(cluster_path, {}) or {}
    global_cluster_registry_selected = int(
        bool(cluster_candidates)
        and dict(current_cluster_payload) != merged_cluster_payload
    )
    if global_cluster_registry_selected:
        _append_yaml_change(changes, cluster_path, merged_cluster_payload)
    active_cluster_ids = {
        str(row.get("cluster_id") or "")
        for row in merged_cluster_payload.get("clusters", []) or []
        if row.get("cluster_id")
    }

    registry_paths = [root / relative for relative in _RELATIONSHIP_REGISTRY_RELATIVES]
    registry_payloads: list[Mapping[str, Any]] = []
    for path in registry_paths:
        if not path.is_file():
            continue
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"relationship registry must be a mapping: {path}")
        registry_payloads.append(payload)
    if not registry_payloads:
        return changes, {
            "relationship_rows_consolidated": 0,
            "stale_cluster_memberships_retired": 0,
            "global_cluster_registry_selected": global_cluster_registry_selected,
        }

    relations: dict[str, dict[str, Any]] = {}
    pair_decisions: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    parked: dict[str, dict[str, Any]] = {}
    machine_identity_by_relation_id: dict[str, str] = {}
    for payload in reversed(registry_payloads):
        for raw in payload.get("relations", []) or payload.get("links", []) or []:
            if not isinstance(raw, Mapping):
                raise ValueError("relationship registry rows must be mappings")
            row = dict(raw)
            relation_id = str(row.get("relation_id") or row.get("link_id") or "")
            human_authored = str(row.get("provenance") or "").startswith("human")
            source_id = str(row.get("source_id") or "")
            target_id = str(
                row.get("target_cluster_id")
                or row.get("target_source_id")
                or row.get("target_id")
                or ""
            )
            relation_type = str(row.get("relation_type") or "")
            probabilistic_pair = str(row.get("provenance") or "").startswith(
                "probabilistic_relationship_"
            )
            semantic_identity = (
                str(row.get("source_kind") or "source"),
                source_id,
                str(row.get("target_kind") or "source"),
                target_id,
                relation_type,
            )
            if human_authored or not all((source_id, target_id, relation_type)):
                identity = f"id:{relation_id or _json_hash(row)}"
            elif probabilistic_pair:
                identity = "probabilistic-pair:" + _json_hash(
                    tuple(sorted((source_id, target_id)))
                )
            else:
                identity = "semantic:" + _json_hash(semantic_identity)
            if not human_authored and relation_id:
                prior_identity = machine_identity_by_relation_id.get(relation_id)
                if prior_identity and prior_identity != identity:
                    relations.pop(prior_identity, None)
                machine_identity_by_relation_id[relation_id] = identity
            relations[identity] = row
        for raw in payload.get("pair_decisions", []) or []:
            if isinstance(raw, Mapping):
                row = dict(raw)
                pair_decisions[
                    str(row.get("decision_key") or _json_hash(row))
                ] = row
        for raw in payload.get("events", []) or []:
            if isinstance(raw, Mapping):
                row = dict(raw)
                events[str(row.get("event_id") or _json_hash(row))] = row
        for raw in payload.get("parked", []) or []:
            if isinstance(raw, Mapping):
                row = dict(raw)
                parked[_json_hash(row)] = row

    retired = 0
    if active_cluster_ids:
        for row in relations.values():
            if str(row.get("provenance") or "").startswith("human"):
                continue
            referenced_clusters = {
                str(row.get("source_id") or "")
                if str(row.get("source_kind") or "") == "cluster"
                else "",
                str(row.get("target_cluster_id") or row.get("target_source_id") or "")
                if str(row.get("target_kind") or "") == "cluster"
                else "",
            } - {""}
            if referenced_clusters and not referenced_clusters.issubset(
                active_cluster_ids
            ):
                if bool(row.get("active", True)):
                    retired += 1
                row.update(
                    active=False,
                    decision_status="retired",
                    retirement_reason="cluster_absent_from_global_registry",
                )

    relation_rows = sorted(
        relations.values(),
        key=lambda row: (
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or row.get("target_cluster_id") or ""),
            str(row.get("relation_type") or ""),
            str(row.get("relation_id") or row.get("link_id") or ""),
        ),
    )
    links = [row for row in relation_rows if bool(row.get("active", True))]
    semantic = {
        "registry_schema_version": "5",
        "relations": relation_rows,
        "links": links,
        "pair_decisions": [pair_decisions[key] for key in sorted(pair_decisions)],
        "events": [events[key] for key in sorted(events)],
        "parked": [parked[key] for key in sorted(parked)],
    }
    base = dict(registry_payloads[0])
    updated = {
        **base,
        **semantic,
        "revision_hash": _json_hash(semantic),
        "graph_projection_hash": _json_hash(links),
        "relation_counts": _active_relation_counts(links),
    }
    for path in registry_paths:
        _append_yaml_change(changes, path, updated)
    return changes, {
        "relationship_rows_consolidated": len(relation_rows),
        "stale_cluster_memberships_retired": retired,
        "global_cluster_registry_selected": global_cluster_registry_selected,
    }


def _legacy_source_bundle_changes(
    root: Path,
) -> tuple[list[tuple[Path, str | None, str]], dict[str, int]]:
    """Wrap usable v0.12 artifacts without changing notes or profile sidecars."""

    candidates = _legacy_note_profile_candidates(root)
    draft_candidates = _legacy_fidelity_draft_candidates(root)
    note_sources = {str(row["source_id"]) for row in candidates}
    candidates.extend(
        row
        for row in draft_candidates
        if str(row["source_id"]) not in note_sources
    )
    by_source: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        source_id = str(candidate["source_id"])
        canonical = (
            root
            / "02_source_memory"
            / "bundles"
            / f"{safe_filename(source_id)}.yml"
        )
        if canonical.exists():
            continue
        by_source.setdefault(source_id, []).append(candidate)

    changes: list[tuple[Path, str | None, str]] = []
    conflicts: list[dict[str, Any]] = []
    created = recovered = promoted = 0
    for source_id in sorted(by_source):
        unique = {
            str(row["semantic_fingerprint"]): row
            for row in by_source[source_id]
        }
        variants = [unique[key] for key in sorted(unique)]
        if len(variants) == 1:
            candidate = variants[0]
            target = (
                root
                / "02_source_memory"
                / "bundles"
                / f"{safe_filename(source_id)}.yml"
            )
            _append_yaml_change(changes, target, candidate["sidecar"])
            created += 1
            recovered += int(candidate["origin"] == "fidelity_parked_draft")
            promoted += int(candidate["partial_promoted"])
            continue
        variant_rows = []
        for candidate in variants:
            fingerprint = str(candidate["semantic_fingerprint"])
            target = (
                root
                / "02_source_memory"
                / "bundles"
                / "legacy_variants"
                / safe_filename(source_id)
                / f"{fingerprint[:16]}.yml"
            )
            _append_yaml_change(changes, target, candidate["sidecar"])
            variant_rows.append(
                {
                    "semantic_fingerprint": fingerprint,
                    "path": str(target.relative_to(root)),
                    "origin": candidate["origin"],
                    "note_path": candidate.get("note_path", ""),
                    "profile_path": candidate.get("profile_path", ""),
                }
            )
            recovered += int(candidate["origin"] == "fidelity_parked_draft")
            promoted += int(candidate["partial_promoted"])
        conflicts.append(
            {
                "source_id": source_id,
                "status": "parked_for_review",
                "reason": "conflicting_legacy_source_analysis_variants",
                "variants": variant_rows,
            }
        )
    if conflicts:
        _append_yaml_change(
            changes,
            root
            / "11_state"
            / "migrations"
            / "v013_source_bundle_conflicts.yml",
            {
                "conflict_schema_version": "1",
                "conflicts": conflicts,
            },
        )
    return changes, {
        "legacy_source_bundles_created": created,
        "legacy_source_bundle_conflicts": len(conflicts),
        "fidelity_drafts_recovered": recovered,
        "partial_documents_promoted": promoted,
    }


def _legacy_note_profile_candidates(root: Path) -> list[dict[str, Any]]:
    notes_by_id: dict[str, tuple[Path, dict[str, Any], str]] = {}
    notes_by_source: dict[str, list[tuple[Path, dict[str, Any], str]]] = {}
    note_root = root / "02_source_memory" / "notes"
    for path in sorted(note_root.glob("*.md")) if note_root.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_atomic_note(text)
        note_id = str(frontmatter.get("note_id") or "")
        source_id = str(frontmatter.get("source_id") or "")
        row = (path, frontmatter, text)
        if note_id:
            notes_by_id[note_id] = row
        if source_id:
            notes_by_source.setdefault(source_id, []).append(row)

    candidates = []
    profile_root = root / "02_source_memory" / "profiles"
    profile_paths = (
        sorted(profile_root.glob("*.yml")) if profile_root.is_dir() else []
    )
    for profile_path in profile_paths:
        if profile_path.name.endswith(".quality.yml"):
            continue
        stored = read_yaml(profile_path, {})
        if not isinstance(stored, Mapping):
            continue
        raw_profile = stored.get("profile", stored)
        if not isinstance(raw_profile, Mapping):
            continue
        profile = dict(raw_profile)
        note_id = str(profile.get("note_id") or "")
        source_id = str(profile.get("source_id") or "")
        note = notes_by_id.get(note_id)
        if note is None and source_id and len(notes_by_source.get(source_id, [])) == 1:
            note = notes_by_source[source_id][0]
        if note is None:
            continue
        note_path, frontmatter, text = note
        source_id = source_id or str(frontmatter.get("source_id") or "")
        if not source_id:
            continue
        bundle = _legacy_bundle_from_note_profile(
            source_id=source_id,
            frontmatter=frontmatter,
            note_text=text,
            profile=profile,
        )
        if bundle is None:
            continue
        candidates.append(
            _legacy_candidate(
                bundle,
                source_id=source_id,
                origin="legacy_note_profile_pair",
                note_path=str(note_path.relative_to(root)),
                profile_path=str(profile_path.relative_to(root)),
                profile_hash=_legacy_profile_hash(profile),
                note_hash=semantic_note_hash(text),
                source_content_hash=str(
                    frontmatter.get("inspected_content_hash") or ""
                ),
            )
        )
    return candidates


def _legacy_fidelity_draft_candidates(root: Path) -> list[dict[str, Any]]:
    candidates = []
    run_root = root / "11_state" / "runs"
    for run_dir in sorted(run_root.iterdir()) if run_root.is_dir() else []:
        inventory_path = run_dir / "inventory.json"
        if not inventory_path.is_file():
            continue
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(inventory, list):
            continue
        for item in inventory:
            if not isinstance(item, Mapping) or not item_key(item):
                continue
            item_root = run_dir / "items" / safe_filename(item_key(item))
            fidelity = read_yaml(item_root / "atomic_fidelity.yml", {})
            frozen = read_yaml(item_root / "frozen_content.yml", {})
            if (
                not isinstance(fidelity, Mapping)
                or str(fidelity.get("status") or "") != "failed"
                or not isinstance(frozen, Mapping)
                or not (item_root / "source.txt").is_file()
            ):
                continue
            checkpoint = _legacy_analysis_checkpoint(item_root)
            if checkpoint is None:
                continue
            analysis, identity = checkpoint
            text_hash = str(frozen.get("text_hash") or "")
            fidelity_identity = fidelity.get("identity", {})
            if (
                not text_hash
                or not isinstance(fidelity_identity, Mapping)
                or str(identity.get("document_hash") or "") != text_hash
                or str(fidelity_identity.get("source_hash") or "") != text_hash
                or str(fidelity_identity.get("analysis_hash") or "")
                != _json_hash(analysis)
            ):
                continue
            bundle = _legacy_bundle_from_draft(item, frozen, analysis, fidelity)
            source_id = source_id_for_item(item)
            candidates.append(
                _legacy_candidate(
                    bundle,
                    source_id=source_id,
                    origin="fidelity_parked_draft",
                    note_path="",
                    profile_path="",
                    profile_hash="",
                    note_hash="",
                    source_content_hash=text_hash,
                )
            )
    return candidates


def _legacy_analysis_checkpoint(
    item_root: Path,
) -> tuple[dict[str, Any], Mapping[str, Any]] | None:
    for name in ("synthesis.yml", "direct.yml"):
        value = read_yaml(item_root / name, {})
        if not isinstance(value, Mapping) or not isinstance(
            value.get("analysis"), Mapping
        ):
            continue
        analysis = {
            str(key): str(raw)
            for key, raw in value["analysis"].items()
            if str(raw).strip()
        }
        if all(
            analysis.get(key)
            for key, _ in COMPATIBILITY_REQUIRED_SECTION_HEADINGS
        ):
            identity = value.get("identity", {})
            if isinstance(identity, Mapping):
                return analysis, identity
    return None


def _legacy_bundle_from_note_profile(
    *,
    source_id: str,
    frontmatter: Mapping[str, Any],
    note_text: str,
    profile: Mapping[str, Any],
) -> dict[str, Any] | None:
    _, body = parse_atomic_note(note_text)
    analysis = _legacy_note_sections(body)
    if not analysis:
        stripped = re.sub(r"^# .*$", "", body, count=1, flags=re.MULTILINE).strip()
        if not stripped:
            return None
        analysis = {"available_content": stripped}
    profile_coverage = (
        profile.get("coverage")
        if isinstance(profile.get("coverage"), Mapping)
        else {}
    )
    scope = str(
        frontmatter.get("source_scope")
        or profile_coverage.get("source_scope")
        or "full_document"
    )
    analytical = all(
        analysis.get(key) for key, _ in COMPATIBILITY_REQUIRED_SECTION_HEADINGS
    )
    legacy_excluded = bool(profile.get("excluded_from_synthesis", False))
    substantive = analytical and (
        scope in {"full_document", "partial_document"}
        or not legacy_excluded
    )
    eligibility = "substantive_bounded" if substantive else "context_only"
    profile_anchors = profile.get("evidence_anchors", [])
    anchors = (
        [dict(row) for row in profile_anchors if isinstance(row, Mapping)]
        if substantive and isinstance(profile_anchors, list)
        else []
    )
    return {
        "bundle_schema_version": "1",
        "source_identity": {
            "source_id": source_id,
            "zotero_key": str(frontmatter.get("zotero_item_key") or ""),
        },
        "observed_bibliographic_identity": {
            key: frontmatter[key]
            for key in ("title", "creators", "date", "itemType", "DOI", "doi", "url")
            if frontmatter.get(key) not in (None, "", [], {})
        },
        "scope_assessment": {
            "source_scope": scope,
            "evidence_eligibility": eligibility,
            "source_coverage": frontmatter.get("source_coverage", {}),
            "coverage_boundary": (
                "Claims remain bounded to the recovered content."
                if scope == "partial_document"
                else ""
            ),
        },
        "analysis_sections": analysis,
        "compact_profile": _legacy_compact_profile(profile, analysis),
        "evidence_anchors": anchors,
        "literature_positions": [],
        "missing_source_recommendations": [],
        "self_review": {
            "migration_origin": "legacy_source_analysis_bundle",
            "provider_verified": False,
            "literature_positions_backfilled": False,
        },
        "component_diagnostics": [],
    }


def _legacy_bundle_from_draft(
    item: Mapping[str, Any],
    frozen: Mapping[str, Any],
    analysis: Mapping[str, Any],
    fidelity: Mapping[str, Any],
) -> dict[str, Any]:
    data = item_data(item)
    source_id = source_id_for_item(item)
    scope = str(frozen.get("source_scope") or "full_document")
    return {
        "bundle_schema_version": "1",
        "source_identity": {
            "source_id": source_id,
            "zotero_key": item_key(item),
        },
        "observed_bibliographic_identity": {
            key: data[key]
            for key in ("title", "creators", "date", "itemType", "DOI", "url")
            if data.get(key) not in (None, "", [], {})
        },
        "scope_assessment": {
            "source_scope": scope,
            "evidence_eligibility": (
                "substantive_bounded"
                if scope in {"full_document", "partial_document"}
                else "context_only"
            ),
            "source_coverage": frozen.get("source_coverage", ""),
            "coverage_metrics": frozen.get("coverage_metrics", {}),
        },
        "analysis_sections": dict(analysis),
        "compact_profile": _legacy_compact_profile({}, analysis),
        "evidence_anchors": [],
        "literature_positions": [],
        "missing_source_recommendations": [],
        "self_review": {
            "migration_origin": "recovered_fidelity_parked_draft",
            "provider_verified": False,
            "advisory_warnings": list(fidelity.get("risks", []) or []),
            "original_failure_reason": str(fidelity.get("reason") or ""),
        },
        "component_diagnostics": [],
    }


def _legacy_note_sections(body: str) -> dict[str, str]:
    sections = {}
    for key, heading in SECTION_HEADINGS:
        match = re.search(
            rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)",
            body,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match and match.group(1).strip():
            sections[key] = match.group(1).strip()
    return sections


def _legacy_compact_profile(
    profile: Mapping[str, Any], analysis: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "thesis": str(analysis.get("thesis") or ""),
        "method_or_knowledge_basis": str(
            analysis.get("method_and_research_design") or ""
        ),
        "source_genre": str(profile.get("source_role") or ""),
        "inferential_design": "; ".join(
            str(value) for value in profile.get("methods", []) or []
        ),
        "coverage": dict(profile.get("coverage") or {})
        if isinstance(profile.get("coverage"), Mapping)
        else {},
        **{
            key: [
                str(value)
                for value in profile.get(key, []) or []
                if str(value).strip()
            ][:8]
            for key in (
                "mechanisms",
                "outcomes",
                "cases",
                "populations",
                "periods",
                "datasets",
            )
        },
    }


def _legacy_candidate(
    bundle: Mapping[str, Any],
    *,
    source_id: str,
    origin: str,
    note_path: str,
    profile_path: str,
    profile_hash: str,
    note_hash: str,
    source_content_hash: str,
) -> dict[str, Any]:
    fingerprint = _json_hash(bundle)
    sidecar = {
        "source_analysis_bundle_schema_version": "1",
        "bundle_origin": "legacy_source_analysis_bundle",
        "migration_status": (
            "recovered_fidelity_parked_draft"
            if origin == "fidelity_parked_draft"
            else "migrated_legacy_note_profile_pair"
        ),
        "semantic_fingerprint": fingerprint,
        "source_content_hash": source_content_hash,
        "note_semantic_hash": note_hash,
        "legacy_profile_semantic_hash": profile_hash,
        "bundle": dict(bundle),
    }
    return {
        "source_id": source_id,
        "origin": origin,
        "semantic_fingerprint": fingerprint,
        "sidecar": sidecar,
        "note_path": note_path,
        "profile_path": profile_path,
        "partial_promoted": (
            bundle.get("scope_assessment", {}).get("source_scope")
            == "partial_document"
            and bundle.get("scope_assessment", {}).get("evidence_eligibility")
            == "substantive_bounded"
        ),
    }


def _legacy_profile_hash(profile: Mapping[str, Any]) -> str:
    payload = dict(profile)
    payload.pop("dependency_hash", None)
    payload.pop("provider", None)
    payload.pop("model", None)
    context = payload.get("context")
    if isinstance(context, Mapping):
        payload["context"] = {
            key: value
            for key, value in context.items()
            if key != "source_set_id"
        }
    return _json_hash(payload)


def _json_hash(value: Any) -> str:
    return sha256_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    )


def _append_yaml_change(
    changes: list[tuple[Path, str | None, str]],
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    cleaned = yaml.safe_dump(
        dict(payload), sort_keys=False, allow_unicode=True, width=10_000
    )
    if cleaned != original:
        changes.append((path, original, cleaned))


def _verified_registry_changes(
    root: Path,
) -> tuple[list[tuple[Path, str | None, str]], int, int]:
    from .relationships import SUBSTANTIVE_RELATION_TYPES, stable_hash

    primary, compatibility = (
        root / relative for relative in _RELATIONSHIP_REGISTRY_RELATIVES
    )
    source = primary if primary.is_file() else compatibility
    if not source.is_file():
        return [], 0, 0
    existing = read_yaml(source, {})
    if not isinstance(existing, Mapping):
        raise ValueError(f"relationship registry must be a mapping: {source}")
    try:
        registry_schema = int(str(existing.get("registry_schema_version") or "0"))
    except ValueError as exc:
        raise ValueError(
            "relationship registry schema version must be an integer"
        ) from exc
    if registry_schema >= 3:
        return [], 0, 0

    relations: list[dict[str, Any]] = []
    deactivated = 0
    human_preserved = 0
    raw_relations = existing.get("relations") or existing.get("links") or []
    if not isinstance(raw_relations, list):
        raise ValueError("relationship registry relations must be a list")
    for value in raw_relations:
        if not isinstance(value, Mapping):
            raise ValueError("relationship registry rows must be mappings")
        row = dict(value)
        provenance = str(row.get("provenance") or "")
        human_authored = provenance.startswith("human")
        if human_authored:
            human_preserved += 1
        elif str(row.get("relation_type") or "") in SUBSTANTIVE_RELATION_TYPES:
            row["active"] = False
            row["decision_status"] = "legacy_unverified"
            deactivated += 1
        relations.append(row)

    links = [row for row in relations if bool(row.get("active", True))]
    pair_decisions = list(existing.get("pair_decisions", []) or [])
    semantic = {
        "registry_schema_version": "3",
        "relations": relations,
        "links": links,
        "pair_decisions": pair_decisions,
    }
    migrated = {
        **dict(existing),
        "updated_at": now_iso(),
        **semantic,
        "revision_hash": stable_hash(semantic),
        "graph_projection_hash": stable_hash(links),
        "relation_counts": _active_relation_counts(links),
    }
    cleaned = yaml.safe_dump(
        migrated,
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    )
    changes: list[tuple[Path, str | None, str]] = []
    for path in (primary, compatibility):
        original = path.read_text(encoding="utf-8") if path.is_file() else None
        if cleaned != original:
            changes.append((path, original, cleaned))
    return changes, deactivated, human_preserved


def _active_relation_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        relation_type = str(row.get("relation_type") or "")
        counts[relation_type] = counts.get(relation_type, 0) + 1
    return dict(sorted(counts.items()))


def migrate_review_status(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Remove generated review-status material and record safe profile-hash aliases."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml"
    if marker.exists():
        payload = read_yaml(marker, {})
        _validate_review_marker(root, payload)
        return {
            "status": "already_migrated",
            "dry_run": dry_run,
            "migration_id": REVIEW_MIGRATION_ID,
            "archive_directory": str(payload.get("archive_directory") or ""),
            "rewritten_files": list(payload.get("rewritten_files", []) or []),
            "hash_aliases": list(payload.get("hash_aliases", []) or []),
            "source_notes_rewritten": int(payload.get("source_notes_rewritten", 0) or 0),
            "provider_calls": 0,
        }

    schema_version = _workspace_schema_version(root)
    if schema_version is not None and schema_version >= (1, 3):
        return _review_not_applicable(dry_run=dry_run, reason="schema_1.3_or_newer")

    changes: list[tuple[Path, str, str]] = []
    aliases: list[dict[str, str]] = []
    note_root = root / "02_source_memory" / "notes"
    for path in sorted(note_root.glob("*.md")) if note_root.exists() else []:
        original = path.read_text(encoding="utf-8")
        if not any(
            marker in original
            for marker in (
                "human_review:",
                "review_status:",
                "source_faithfulness_review:",
                "## Automated Validation",
                "## Review Status",
                "## Source-Faithfulness Review",
            )
        ):
            continue
        cleaned = strip_review_status_material(original, update_versions=False)
        if cleaned == original:
            continue
        frontmatter, _ = parse_atomic_note(original)
        aliases.append(
            {
                "note_id": str(frontmatter.get("note_id") or path.stem),
                "legacy_semantic_hash": legacy_semantic_note_hash_v1(original),
                "semantic_hash": semantic_note_hash(cleaned),
            }
        )
        changes.append((path, original, cleaned))

    synthesis_root = root / "03_literature_synthesis"
    for path in sorted(synthesis_root.rglob("*.md")) if synthesis_root.exists() else []:
        original = path.read_text(encoding="utf-8")
        cleaned = strip_review_status_material(original, update_versions=True)
        if cleaned != original:
            changes.append((path, original, cleaned))

    yaml_roots = (
        root / "02_source_memory" / "profiles",
        root / "03_literature_synthesis",
        root / "11_state" / "runs",
    )
    for directory in yaml_roots:
        if not directory.exists():
            continue
        for path in sorted({*directory.rglob("*.yml"), *directory.rglob("*.yaml")}):
            original = path.read_text(encoding="utf-8")
            payload = yaml.safe_load(original)
            cleaned_payload = _clean_review_payload(payload)
            cleaned = yaml.safe_dump(cleaned_payload, sort_keys=False, allow_unicode=True, width=10_000)
            if cleaned != original:
                changes.append((path, original, cleaned))

    for path in (root / "auto-zettelkasten.yml", root / "11_state" / "workspace_manifest.yml"):
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(original)
        if not isinstance(payload, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        cleaned_payload = dict(_clean_review_payload(payload))
        cleaned_payload["engine_version"] = REVIEW_STATUS_TARGET_ENGINE_VERSION
        cleaned_payload["artifact_schema_version"] = REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION
        cleaned = yaml.safe_dump(cleaned_payload, sort_keys=False, allow_unicode=True, width=10_000)
        if cleaned != original:
            changes.append((path, original, cleaned))

    # Avoid duplicate archive/write rows when a file was selected by two scopes.
    by_path = {path: (original, cleaned) for path, original, cleaned in changes}
    timestamp = now_iso().replace(":", "").replace("+00:00", "Z")
    archive = root / "11_state" / "legacy_maps" / f"pre-0.4-review-{slugify(timestamp)}"
    planned = [
        {
            "source": str(path.relative_to(root)),
            "archive": str((archive / path.relative_to(root)).relative_to(root)),
            "before_sha256": sha256_text(original),
            "after_sha256": sha256_text(cleaned),
        }
        for path, (original, cleaned) in sorted(by_path.items(), key=lambda row: str(row[0]))
    ]
    source_note_count = sum(1 for row in planned if str(row["source"]).startswith("02_source_memory/notes/"))
    if dry_run:
        return {
            "status": "dry_run",
            "dry_run": True,
            "migration_id": REVIEW_MIGRATION_ID,
            "archive_directory": str(archive),
            "rewritten_files": planned,
            "hash_aliases": aliases,
            "source_notes_rewritten": source_note_count,
            "provider_calls": 0,
        }

    written: list[Path] = []
    try:
        for row in planned:
            source = root / str(row["source"])
            target = root / str(row["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != row["before_sha256"]:
                raise RuntimeError(f"migration archive checksum mismatch: {target}")
        for path, (_, cleaned) in sorted(by_path.items(), key=lambda row: str(row[0])):
            atomic_write_text(path, cleaned)
            written.append(path)
        for row in planned:
            if sha256_file(root / str(row["source"])) != row["after_sha256"]:
                raise RuntimeError(f"migration target checksum mismatch: {row['source']}")
    except Exception:
        for path in reversed(written):
            archived = archive / path.relative_to(root)
            if archived.is_file():
                shutil.copy2(archived, path)
        raise

    payload = {
        "migration_id": REVIEW_MIGRATION_ID,
        "migration_version": REVIEW_MIGRATION_VERSION,
        "status": "completed",
        "archive_directory": str(archive),
        "rewritten_files": planned,
        "hash_aliases": aliases,
        "source_notes_rewritten": source_note_count,
        "provider_calls": 0,
        "completed_at": now_iso(),
    }
    write_yaml(marker, payload)
    return {"status": "migrated", "dry_run": False, **payload, "marker": str(marker)}


def review_hash_aliases(workspace: Path | str) -> dict[str, dict[str, str]]:
    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml"
    if not marker.is_file():
        return {}
    payload = read_yaml(marker, {})
    _validate_review_marker(root, payload)
    return {
        str(row.get("note_id") or ""): {
            "legacy_semantic_hash": str(row.get("legacy_semantic_hash") or ""),
            "semantic_hash": str(row.get("semantic_hash") or ""),
        }
        for row in payload.get("hash_aliases", []) or []
        if isinstance(row, Mapping) and row.get("note_id")
    }


def _clean_review_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if str(key) in _REVIEW_FIELDS:
                continue
            if key == "engine_version":
                result[str(key)] = REVIEW_STATUS_TARGET_ENGINE_VERSION
            elif key == "artifact_schema_version":
                result[str(key)] = REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION
            else:
                result[str(key)] = _clean_review_payload(child)
        return result
    if isinstance(value, list):
        return [_clean_review_payload(child) for child in value]
    return value


def _workspace_schema_version(root: Path) -> tuple[int, int] | None:
    paths = tuple(root / relative for relative in _VERSION_FILE_RELATIVES)
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    if len(existing) != len(paths):
        missing = next(path for path in paths if not path.is_file())
        raise ValueError(f"workspace version file is missing: {missing}")
    versions: list[tuple[int, int]] = []
    for path in paths:
        payload = read_yaml(path, {})
        if not isinstance(payload, Mapping):
            raise ValueError(f"workspace version file must be a mapping: {path}")
        versions.append(_parse_schema_version(payload.get("artifact_schema_version"), field=str(path)))
    if versions[0] != versions[1]:
        raise ValueError(
            f"workspace config schema {versions[0]} disagrees with manifest schema {versions[1]}"
        )
    return versions[0]


def _parse_schema_version(value: Any, *, field: str) -> tuple[int, int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.0+)?", text)
    if not match:
        raise ValueError(f"{field} has malformed artifact schema: {text or '<missing>'}")
    return int(match.group(1)), int(match.group(2))


def _literature_map_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": MIGRATION_ID,
        "reason": reason,
        "archive_directory": "",
        "archived_files": [],
        "source_notes_rewritten": False,
    }


def _review_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": REVIEW_MIGRATION_ID,
        "reason": reason,
        "archive_directory": "",
        "rewritten_files": [],
        "hash_aliases": [],
        "source_notes_rewritten": 0,
        "provider_calls": 0,
    }


def _gap_quality_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": GAP_QUALITY_MIGRATION_ID,
        "reason": reason,
        "archive_directory": "",
        "rewritten_files": [],
        "provider_calls": 0,
    }


def _proposition_anchor_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": PROPOSITION_ANCHOR_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": PROPOSITION_ANCHOR_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": [],
        "archived_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "profile_upgrade": "lazy_on_read",
    }


def _navigation_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": NAVIGATION_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": NAVIGATION_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": "",
        "archived_files": [],
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }


def _researcher_grade_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": RESEARCHER_GRADE_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": RESEARCHER_GRADE_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": "",
        "archived_files": [],
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }


def _debate_family_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": DEBATE_FAMILY_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": DEBATE_FAMILY_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": "",
        "archived_files": [],
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }


def _thematic_cluster_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": THEMATIC_CLUSTER_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": THEMATIC_CLUSTER_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION,
        "archive_directory": "",
        "archived_files": [],
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "analytical_identity_changes": 0,
    }


def _managed_graph_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": MANAGED_GRAPH_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": MANAGED_GRAPH_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION,
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
    }


def _verified_graph_not_applicable(*, dry_run: bool, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "dry_run": dry_run,
        "migration_id": VERIFIED_GRAPH_MIGRATION_ID,
        "reason": reason,
        "target_engine_version": VERIFIED_GRAPH_TARGET_ENGINE_VERSION,
        "target_artifact_schema_version": (
            VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        ),
        "rewritten_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "legacy_relationships_deactivated": 0,
        "human_relationships_preserved": 0,
    }


def _validate_navigation_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("navigation migration marker must be a mapping")
    unknown = sorted(set(value) - _NAVIGATION_MARKER_FIELDS)
    missing = sorted(_NAVIGATION_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = f"unknown fields: {', '.join(unknown)}" if unknown else f"missing fields: {', '.join(missing)}"
        raise ValueError(f"malformed navigation migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
        "analytical_identity_changes",
    )
    if (
        value.get("migration_id") != NAVIGATION_MIGRATION_ID
        or str(value.get("migration_version")) != NAVIGATION_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != NAVIGATION_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version") != NAVIGATION_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed navigation migration marker")
    if not isinstance(value.get("archived_files"), list) or not isinstance(value.get("rewritten_files"), list):
        raise ValueError("malformed navigation migration marker file lists")
    for row in value.get("archived_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "archive", "sha256"}:
            raise ValueError("malformed navigation migration archive record")
        archive = _confined_marker_path(root, row.get("archive"), label="navigation migration archive")
        if not archive.is_file() or sha256_file(archive) != str(row.get("sha256") or ""):
            raise ValueError(f"navigation migration archive is missing or corrupt: {archive}")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "before_sha256", "after_sha256"}:
            raise ValueError("malformed navigation migration rewritten-file record")
        if str(row.get("source") or "") not in _VERSION_FILE_RELATIVES:
            raise ValueError("malformed navigation migration rewritten-file source")


def _validate_researcher_grade_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("researcher-grade migration marker must be a mapping")
    unknown = sorted(set(value) - _RESEARCHER_GRADE_MARKER_FIELDS)
    missing = sorted(_RESEARCHER_GRADE_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = f"unknown fields: {', '.join(unknown)}" if unknown else f"missing fields: {', '.join(missing)}"
        raise ValueError(f"malformed researcher-grade migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
        "analytical_identity_changes",
    )
    if (
        value.get("migration_id") != RESEARCHER_GRADE_MIGRATION_ID
        or str(value.get("migration_version")) != RESEARCHER_GRADE_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != RESEARCHER_GRADE_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version") != RESEARCHER_GRADE_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed researcher-grade migration marker")
    if not isinstance(value.get("archived_files"), list) or not isinstance(value.get("rewritten_files"), list):
        raise ValueError("malformed researcher-grade migration marker file lists")
    for row in value.get("archived_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "archive", "sha256"}:
            raise ValueError("malformed researcher-grade migration archive record")
        archive = _confined_marker_path(root, row.get("archive"), label="researcher-grade migration archive")
        if not archive.is_file() or sha256_file(archive) != str(row.get("sha256") or ""):
            raise ValueError(f"researcher-grade migration archive is missing or corrupt: {archive}")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed researcher-grade migration rewritten-file record")
        if str(row.get("source") or "") not in _VERSION_FILE_RELATIVES:
            raise ValueError("malformed researcher-grade migration rewritten-file source")


def _validate_debate_family_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("debate-family migration marker must be a mapping")
    unknown = sorted(set(value) - _DEBATE_FAMILY_MARKER_FIELDS)
    missing = sorted(_DEBATE_FAMILY_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = f"unknown fields: {', '.join(unknown)}" if unknown else f"missing fields: {', '.join(missing)}"
        raise ValueError(f"malformed debate-family migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
        "analytical_identity_changes",
    )
    if (
        value.get("migration_id") != DEBATE_FAMILY_MIGRATION_ID
        or str(value.get("migration_version")) != DEBATE_FAMILY_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != DEBATE_FAMILY_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version") != DEBATE_FAMILY_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed debate-family migration marker")
    if not isinstance(value.get("archived_files"), list) or not isinstance(value.get("rewritten_files"), list):
        raise ValueError("malformed debate-family migration marker file lists")
    for row in value.get("archived_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "archive", "sha256"}:
            raise ValueError("malformed debate-family migration archive record")
        archive = _confined_marker_path(root, row.get("archive"), label="debate-family migration archive")
        if not archive.is_file() or sha256_file(archive) != str(row.get("sha256") or ""):
            raise ValueError(f"debate-family migration archive is missing or corrupt: {archive}")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed debate-family migration rewritten-file record")
        if str(row.get("source") or "") not in _VERSION_FILE_RELATIVES:
            raise ValueError("malformed debate-family migration rewritten-file source")


def _validate_thematic_cluster_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("thematic-cluster migration marker must be a mapping")
    unknown = sorted(set(value) - _THEMATIC_CLUSTER_MARKER_FIELDS)
    missing = sorted(_THEMATIC_CLUSTER_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = (
            f"unknown fields: {', '.join(unknown)}"
            if unknown
            else f"missing fields: {', '.join(missing)}"
        )
        raise ValueError(f"malformed thematic-cluster migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
        "analytical_identity_changes",
    )
    if (
        value.get("migration_id") != THEMATIC_CLUSTER_MIGRATION_ID
        or str(value.get("migration_version")) != THEMATIC_CLUSTER_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != THEMATIC_CLUSTER_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version")
        != THEMATIC_CLUSTER_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed thematic-cluster migration marker")
    if not isinstance(value.get("archived_files"), list) or not isinstance(
        value.get("rewritten_files"), list
    ):
        raise ValueError("malformed thematic-cluster migration marker file lists")
    allowed_projection_roots = (
        "03_literature_synthesis/clusters/",
        "03_literature_synthesis/gaps/",
    )
    for row in value.get("archived_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "archive", "sha256"}:
            raise ValueError("malformed thematic-cluster migration archive record")
        source = str(row.get("source") or "")
        is_top_level_markdown = (
            source.startswith("03_literature_synthesis/")
            and source.endswith(".md")
            and source.count("/") == 1
        )
        is_projection_markdown = source.endswith(".md") and source.startswith(
            allowed_projection_roots
        )
        if not is_top_level_markdown and not is_projection_markdown:
            raise ValueError("malformed thematic-cluster migration archive source")
        archive = _confined_marker_path(
            root,
            row.get("archive"),
            label="thematic-cluster migration archive",
        )
        if not archive.is_file() or sha256_file(archive) != str(row.get("sha256") or ""):
            raise ValueError(f"thematic-cluster migration archive is missing or corrupt: {archive}")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed thematic-cluster migration rewritten-file record")
        if str(row.get("source") or "") not in _VERSION_FILE_RELATIVES:
            raise ValueError("malformed thematic-cluster migration rewritten-file source")


def _validate_managed_graph_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("managed-graph migration marker must be a mapping")
    unknown = sorted(set(value) - _MANAGED_GRAPH_MARKER_FIELDS)
    missing = sorted(_MANAGED_GRAPH_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = (
            f"unknown fields: {', '.join(unknown)}"
            if unknown
            else f"missing fields: {', '.join(missing)}"
        )
        raise ValueError(f"malformed managed-graph migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
    )
    if (
        value.get("migration_id") != MANAGED_GRAPH_MIGRATION_ID
        or str(value.get("migration_version")) != MANAGED_GRAPH_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != MANAGED_GRAPH_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version")
        != MANAGED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed managed-graph migration marker")
    rewritten_files = value.get("rewritten_files")
    if not isinstance(rewritten_files, list):
        raise ValueError("malformed managed-graph migration rewritten-file list")
    sources: set[str] = set()
    for row in rewritten_files:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed managed-graph migration rewritten-file record")
        source = str(row.get("source") or "")
        if source not in _VERSION_FILE_RELATIVES or source in sources or not all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
            for field in ("before_sha256", "after_sha256")
        ):
            raise ValueError("malformed managed-graph migration rewritten-file record")
        sources.add(source)


def _validate_verified_graph_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("verified-graph migration marker must be a mapping")
    unknown = sorted(set(value) - _VERIFIED_GRAPH_MARKER_FIELDS)
    missing = sorted(_VERIFIED_GRAPH_MARKER_FIELDS - set(value))
    if unknown or missing:
        detail = (
            f"unknown fields: {', '.join(unknown)}"
            if unknown
            else f"missing fields: {', '.join(missing)}"
        )
        raise ValueError(f"malformed verified-graph migration marker: {detail}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
    )
    count_fields = (
        "legacy_relationships_deactivated",
        "human_relationships_preserved",
    )
    if (
        value.get("migration_id") != VERIFIED_GRAPH_MIGRATION_ID
        or str(value.get("migration_version")) != VERIFIED_GRAPH_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != VERIFIED_GRAPH_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version")
        != VERIFIED_GRAPH_TARGET_ARTIFACT_SCHEMA_VERSION
        or any(
            type(value.get(field)) is not int or value.get(field) != 0
            for field in zero_fields
        )
        or any(
            type(value.get(field)) is not int or value.get(field) < 0
            for field in count_fields
        )
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed verified-graph migration marker")
    rewritten_files = value.get("rewritten_files")
    if not isinstance(rewritten_files, list):
        raise ValueError("malformed verified-graph migration rewritten-file list")
    allowed_sources = {
        *_VERSION_FILE_RELATIVES,
        *_RELATIONSHIP_REGISTRY_RELATIVES,
    }
    sources: set[str] = set()
    for row in rewritten_files:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed verified-graph migration rewritten-file record")
        source = str(row.get("source") or "")
        if (
            source not in allowed_sources
            or source in sources
            or not all(
                re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
                for field in ("before_sha256", "after_sha256")
            )
        ):
            raise ValueError("malformed verified-graph migration rewritten-file record")
        sources.add(source)


def _validate_proposition_anchor_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("proposition-anchor migration marker must be a mapping")
    unknown = sorted(set(value) - _PROPOSITION_ANCHOR_MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown proposition-anchor migration fields: {', '.join(unknown)}")
    missing = sorted(_PROPOSITION_ANCHOR_MARKER_FIELDS - set(value))
    if missing:
        raise ValueError(f"missing proposition-anchor migration fields: {', '.join(missing)}")
    zero_fields = (
        "provider_calls",
        "source_documents_reread",
        "source_notes_rewritten",
        "profile_files_rewritten",
    )
    if (
        value.get("migration_id") != PROPOSITION_ANCHOR_MIGRATION_ID
        or str(value.get("migration_version")) != PROPOSITION_ANCHOR_MIGRATION_VERSION
        or value.get("status") != "completed"
        or value.get("target_engine_version") != PROPOSITION_ANCHOR_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version") != PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION
        or value.get("profile_upgrade") != "lazy_on_read"
        or any(type(value.get(field)) is not int or value.get(field) != 0 for field in zero_fields)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed proposition-anchor migration marker")
    rewritten_files = value.get("rewritten_files")
    archived_files = value.get("archived_files")
    if not isinstance(rewritten_files, list) or not isinstance(archived_files, list):
        raise ValueError("malformed proposition-anchor migration marker file lists")
    if archived_files:
        raise ValueError("malformed proposition-anchor migration marker: unexpected archived files")
    allowed_sources = set(_VERSION_FILE_RELATIVES)
    for row in rewritten_files:
        if not isinstance(row, Mapping) or set(row) != {"source", "before_sha256", "after_sha256"}:
            raise ValueError("malformed proposition-anchor migration rewritten-file record")
        if str(row.get("source") or "") not in allowed_sources or not all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
            for field in ("before_sha256", "after_sha256")
        ):
            raise ValueError("malformed proposition-anchor migration rewritten-file record")
def _validate_gap_quality_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("gap-quality migration marker must be a mapping")
    unknown = sorted(set(value) - _GAP_QUALITY_MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown gap-quality migration fields: {', '.join(unknown)}")
    missing = sorted(_GAP_QUALITY_MARKER_FIELDS - set(value))
    if missing:
        raise ValueError(f"missing gap-quality migration fields: {', '.join(missing)}")
    if (
        value.get("migration_id") != GAP_QUALITY_MIGRATION_ID
        or str(value.get("migration_version")) != GAP_QUALITY_MIGRATION_VERSION
        or value.get("status") != "completed"
        or type(value.get("provider_calls")) is not int
        or value.get("provider_calls") != 0
        or not isinstance(value.get("archive_directory"), str)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("malformed gap-quality migration marker")
    rewritten_files = value.get("rewritten_files")
    if not isinstance(rewritten_files, list):
        raise ValueError("malformed gap-quality migration rewritten_files")
    seen_sources: set[str] = set()
    for row in rewritten_files:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "archive",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("malformed gap-quality migration file record")
        source = str(row.get("source") or "")
        if source not in _VERSION_FILE_RELATIVES or source in seen_sources:
            raise ValueError("malformed gap-quality migration file record")
        seen_sources.add(source)
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
            for field in ("before_sha256", "after_sha256")
        ):
            raise ValueError("malformed gap-quality migration file record")
        archive = _confined_marker_path(root, row.get("archive"), label="gap-quality migration archive")
        if not archive.is_file() or sha256_file(archive) != str(row["before_sha256"]):
            raise ValueError(f"gap-quality migration archive is missing or corrupt: {archive}")


def _validate_existing_markers(root: Path) -> None:
    marker_root = root / "11_state" / "migrations"
    marker_validators = (
        (MIGRATION_ID, lambda value: _validate_marker(root, value)),
        (REVIEW_MIGRATION_ID, lambda value: _validate_review_marker(root, value)),
        (
            GAP_QUALITY_MIGRATION_ID,
            lambda value: _validate_gap_quality_marker(root, value),
        ),
        (
            PROPOSITION_ANCHOR_MIGRATION_ID,
            lambda value: _validate_proposition_anchor_marker(value),
        ),
        (
            NAVIGATION_MIGRATION_ID,
            lambda value: _validate_navigation_marker(root, value),
        ),
        (
            RESEARCHER_GRADE_MIGRATION_ID,
            lambda value: _validate_researcher_grade_marker(root, value),
        ),
        (
            DEBATE_FAMILY_MIGRATION_ID,
            lambda value: _validate_debate_family_marker(root, value),
        ),
        (
            THEMATIC_CLUSTER_MIGRATION_ID,
            lambda value: _validate_thematic_cluster_marker(root, value),
        ),
        (
            MANAGED_GRAPH_MIGRATION_ID,
            _validate_managed_graph_marker,
        ),
        (
            VERIFIED_GRAPH_MIGRATION_ID,
            _validate_verified_graph_marker,
        ),
        (V013_MIGRATION_ID, _validate_v013_marker),
        (V014_MIGRATION_ID, _validate_v014_marker),
    )
    for migration_id, validate in marker_validators:
        marker = marker_root / f"{migration_id}.yml"
        if marker.exists() and not marker.is_file():
            raise ValueError(f"migration marker must be a regular file: {marker}")
        if marker.is_file():
            validate(read_yaml(marker, {}))
            if migration_id == PROPOSITION_ANCHOR_MIGRATION_ID:
                schema = _workspace_schema_version(root)
                target = _parse_schema_version(
                    PROPOSITION_ANCHOR_TARGET_ARTIFACT_SCHEMA_VERSION,
                    field="proposition-anchor artifact schema",
                )
                if schema is None or schema < target:
                    raise ValueError("completed proposition-anchor migration marker disagrees with workspace schema")


def _validate_v013_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("v0.13 migration marker must be a mapping")
    unknown = sorted(set(value) - _V013_MARKER_FIELDS)
    missing = sorted(_V013_MARKER_FIELDS - set(value))
    if unknown or missing:
        raise ValueError(
            "invalid v0.13 migration marker fields"
            f"; unknown={','.join(unknown)}; missing={','.join(missing)}"
        )
    if (
        value.get("migration_id") != V013_MIGRATION_ID
        or str(value.get("migration_version")) != V013_MIGRATION_VERSION
        or value.get("target_engine_version") != V013_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version")
        != V013_TARGET_ARTIFACT_SCHEMA_VERSION
        or int(value.get("provider_calls", -1)) != 0
        or int(value.get("source_documents_reread", -1)) != 0
        or int(value.get("source_notes_rewritten", -1)) != 0
        or int(value.get("profile_files_rewritten", -1)) != 0
        or int(value.get("zotero_calls", -1)) != 0
        or any(
            int(value.get(field_name, -1)) < 0
            for field_name in (
                "legacy_relationships_pending",
                "legacy_source_bundles_created",
                "legacy_source_bundle_conflicts",
                "fidelity_drafts_recovered",
                "partial_documents_promoted",
            )
        )
    ):
        raise ValueError("invalid v0.13 migration marker")


def _validate_v014_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("v0.14 migration marker must be a mapping")
    unknown = sorted(set(value) - _V014_MARKER_FIELDS)
    missing = sorted(_V014_MARKER_FIELDS - set(value))
    if unknown or missing:
        raise ValueError(
            "invalid v0.14 migration marker fields"
            f"; unknown={','.join(unknown)}; missing={','.join(missing)}"
        )
    if (
        value.get("migration_id") != V014_MIGRATION_ID
        or str(value.get("migration_version")) != V014_MIGRATION_VERSION
        or value.get("target_engine_version") != V014_TARGET_ENGINE_VERSION
        or value.get("target_artifact_schema_version")
        != V014_TARGET_ARTIFACT_SCHEMA_VERSION
        or int(value.get("provider_calls", -1)) != 0
        or int(value.get("source_documents_reread", -1)) != 0
        or int(value.get("source_notes_rewritten", -1)) != 0
        or int(value.get("profile_files_rewritten", -1)) != 0
        or any(
            int(value.get(field_name, -1)) < 0
            for field_name in (
                "legacy_cluster_syntheses_marked",
                "relationship_rows_consolidated",
                "stale_cluster_memberships_retired",
                "global_cluster_registry_selected",
            )
        )
    ):
        raise ValueError("invalid v0.14 migration marker")


def _confined_marker_path(root: Path, value: Any, *, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} path is missing")
    candidate = (root / text).resolve(strict=False)
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"{label} path escapes workspace: {candidate}")
    return candidate


def _validate_review_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("review-status migration marker must be a mapping")
    unknown = sorted(set(value) - _REVIEW_MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown review-status migration fields: {', '.join(unknown)}")
    if value.get("migration_id") != REVIEW_MIGRATION_ID or str(value.get("migration_version")) != REVIEW_MIGRATION_VERSION:
        raise ValueError("review-status migration marker has an unsupported identity or version")
    if (
        value.get("status") != "completed"
        or type(value.get("provider_calls")) is not int
        or value.get("provider_calls") != 0
        or type(value.get("source_notes_rewritten")) is not int
        or int(value.get("source_notes_rewritten", -1)) < 0
        or not isinstance(value.get("archive_directory"), str)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("review-status migration marker is incomplete or unsafe")
    if not isinstance(value.get("rewritten_files", []), list) or not isinstance(value.get("hash_aliases", []), list):
        raise ValueError("review-status migration lists are malformed")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {
            "source",
            "archive",
            "before_sha256",
            "after_sha256",
        }:
            raise ValueError("review-status migration file record must be a mapping")
        _confined_marker_path(root, row.get("source"), label="review-status migration source")
        archive = _confined_marker_path(root, row.get("archive"), label="review-status migration archive")
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
            for field in ("before_sha256", "after_sha256")
        ):
            raise ValueError("review-status migration file record has malformed checksums")
        if not archive.is_file() or sha256_file(archive) != str(row.get("before_sha256") or ""):
            raise ValueError(f"review-status migration archive is missing or corrupt: {archive}")
    for row in value.get("hash_aliases", []) or []:
        if not isinstance(row, Mapping) or set(row) != {
            "note_id",
            "legacy_semantic_hash",
            "semantic_hash",
        }:
            raise ValueError("review-status migration hash alias is malformed")
        if not str(row.get("note_id") or "").strip() or not all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field) or ""))
            for field in ("legacy_semantic_hash", "semantic_hash")
        ):
            raise ValueError("review-status migration hash alias is malformed")


def _legacy_generated_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for relative in (
        "03_literature_synthesis/clusters",
        "03_literature_synthesis/gaps",
        "03_literature_synthesis/closest_prior_work",
        "03_literature_synthesis/packets",
    ):
        directory = root / relative
        if directory.exists():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    for relative in (
        "02_source_memory/indexes/gap_candidates.yml",
        "02_source_memory/indexes/cluster_map.yml",
    ):
        path = root / relative
        if path.is_file():
            candidates.append(path)
    return sorted(set(candidates))


def _validate_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("literature-map migration marker must be a mapping")
    unknown = sorted(set(value) - _MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown literature-map migration fields: {', '.join(unknown)}")
    if value.get("migration_id") != MIGRATION_ID or str(value.get("migration_version")) != MIGRATION_VERSION:
        raise ValueError("literature-map migration marker has an unsupported identity or version")
    if (
        value.get("status") != "completed"
        or not isinstance(value.get("archive_directory"), str)
        or not isinstance(value.get("completed_at"), str)
        or not str(value.get("completed_at") or "").strip()
    ):
        raise ValueError("literature-map migration marker is not complete")
    if not isinstance(value.get("archived_files", []), list):
        raise ValueError("literature-map migration archived_files must be a list")
    for row in value.get("archived_files", []) or []:
        if not isinstance(row, Mapping) or set(row) != {"source", "archive", "sha256"}:
            raise ValueError("literature-map migration archived-file record is malformed")
        _confined_marker_path(root, row.get("source"), label="literature-map migration source")
        archive = _confined_marker_path(root, row.get("archive"), label="literature-map migration archive")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or "")):
            raise ValueError("literature-map migration archived-file record is malformed")
        if not archive.is_file() or sha256_file(archive) != str(row["sha256"]):
            raise ValueError(f"literature-map migration archive is missing or corrupt: {archive}")
