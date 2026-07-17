from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .files import atomic_write_text, now_iso, read_yaml, sha256_file, sha256_text, slugify, write_yaml
from .notes import (
    legacy_semantic_note_hash_v1,
    parse_atomic_note,
    semantic_note_hash,
    strip_review_status_material,
)
from .models import CURRENT_ARTIFACT_SCHEMA_VERSION, CURRENT_ENGINE_VERSION
from .workspace import resolve_workspace

MIGRATION_ID = "auto-zettelkasten-0.3-literature-map"
MIGRATION_VERSION = "1"
REVIEW_MIGRATION_ID = "auto-zettelkasten-0.4-review-status-removal"
REVIEW_MIGRATION_VERSION = "1"
GAP_QUALITY_MIGRATION_ID = "auto-zettelkasten-0.5-gap-quality"
GAP_QUALITY_MIGRATION_VERSION = "1"
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
_REVIEW_FIELDS = {"human_review", "review_status", "source_faithfulness_review"}


def migrate_literature_map(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Archive pre-0.3 generated map projections without rewriting source notes."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{MIGRATION_ID}.yml"
    if marker.exists():
        payload = read_yaml(marker, {})
        _validate_marker(payload)
        return {
            "status": "already_migrated",
            "dry_run": dry_run,
            "migration_id": MIGRATION_ID,
            "archive_directory": str(payload.get("archive_directory") or ""),
            "archived_files": list(payload.get("archived_files", []) or []),
            "source_notes_rewritten": False,
        }

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

    legacy = migrate_literature_map(workspace, dry_run=dry_run)
    review = migrate_review_status(workspace, dry_run=dry_run)
    gap_quality = migrate_gap_quality_schema(workspace, dry_run=dry_run)
    return {
        "status": "dry_run" if dry_run else "completed",
        "dry_run": dry_run,
        "provider_calls": 0,
        "migrations": [legacy, review, gap_quality],
        "review_status": review,
        "gap_quality": gap_quality,
    }


def migrate_gap_quality_schema(workspace: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Upgrade only workspace version files; existing notes and maps remain untouched."""

    root = resolve_workspace(workspace)
    marker = root / "11_state" / "migrations" / f"{GAP_QUALITY_MIGRATION_ID}.yml"
    if marker.is_file():
        payload = read_yaml(marker, {})
        if not isinstance(payload, Mapping) or set(payload) != {
            "migration_id",
            "migration_version",
            "status",
            "archive_directory",
            "rewritten_files",
            "provider_calls",
            "completed_at",
        }:
            raise ValueError("malformed gap-quality migration marker")
        return {**dict(payload), "status": "already_migrated", "dry_run": dry_run}

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
        updated["engine_version"] = CURRENT_ENGINE_VERSION
        updated["artifact_schema_version"] = CURRENT_ARTIFACT_SCHEMA_VERSION
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
        cleaned_payload["engine_version"] = "0.5.0"
        cleaned_payload["artifact_schema_version"] = "1.4"
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
                result[str(key)] = "0.5.0"
            elif key == "artifact_schema_version":
                result[str(key)] = "1.4"
            else:
                result[str(key)] = _clean_review_payload(child)
        return result
    if isinstance(value, list):
        return [_clean_review_payload(child) for child in value]
    return value


def _validate_review_marker(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("review-status migration marker must be a mapping")
    unknown = sorted(set(value) - _REVIEW_MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown review-status migration fields: {', '.join(unknown)}")
    if value.get("migration_id") != REVIEW_MIGRATION_ID or str(value.get("migration_version")) != REVIEW_MIGRATION_VERSION:
        raise ValueError("review-status migration marker has an unsupported identity or version")
    if value.get("status") != "completed" or int(value.get("provider_calls", -1)) != 0:
        raise ValueError("review-status migration marker is incomplete or unsafe")
    if not isinstance(value.get("rewritten_files", []), list) or not isinstance(value.get("hash_aliases", []), list):
        raise ValueError("review-status migration lists are malformed")
    for row in value.get("rewritten_files", []) or []:
        if not isinstance(row, Mapping):
            raise ValueError("review-status migration file record must be a mapping")
        archive = root / str(row.get("archive") or "")
        if not archive.is_file() or sha256_file(archive) != str(row.get("before_sha256") or ""):
            raise ValueError(f"review-status migration archive is missing or corrupt: {archive}")


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


def _validate_marker(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("literature-map migration marker must be a mapping")
    unknown = sorted(set(value) - _MARKER_FIELDS)
    if unknown:
        raise ValueError(f"unknown literature-map migration fields: {', '.join(unknown)}")
    if value.get("migration_id") != MIGRATION_ID or str(value.get("migration_version")) != MIGRATION_VERSION:
        raise ValueError("literature-map migration marker has an unsupported identity or version")
    if value.get("status") != "completed":
        raise ValueError("literature-map migration marker is not complete")
    if not isinstance(value.get("archived_files", []), list):
        raise ValueError("literature-map migration archived_files must be a list")
