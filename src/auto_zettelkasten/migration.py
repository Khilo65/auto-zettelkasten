from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .files import atomic_write_text, now_iso, read_yaml, sha256_file, sha256_text, slugify, write_yaml
from .notes import (
    REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION,
    REVIEW_STATUS_TARGET_ENGINE_VERSION,
    legacy_semantic_note_hash_v1,
    parse_atomic_note,
    semantic_note_hash,
    strip_review_status_material,
)
from .models import NavigationPolicy
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

_REVIEW_FIELDS = {"human_review", "review_status", "source_faithfulness_review"}
_VERSION_FILE_RELATIVES = ("auto-zettelkasten.yml", "11_state/workspace_manifest.yml")


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
    review = migrate_review_status(workspace, dry_run=dry_run)
    gap_quality = migrate_gap_quality_schema(workspace, dry_run=dry_run)
    proposition_anchors = migrate_proposition_anchor_schema(workspace, dry_run=dry_run)
    navigation = migrate_navigation_projection_schema(workspace, dry_run=dry_run)
    researcher_grade = migrate_researcher_grade_schema(workspace, dry_run=dry_run)
    debate_family = migrate_debate_family_schema(workspace, dry_run=dry_run)
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
        ],
        "literature_map": legacy,
        "review_status": review,
        "gap_quality": gap_quality,
        "proposition_anchors": proposition_anchors,
        "navigation": navigation,
        "researcher_grade": researcher_grade,
        "debate_family": debate_family,
    }


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
