from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .files import ensure_dir, now_iso, read_yaml, sha256_file, write_yaml
from .models import ArtifactManifest

WORKSPACE_DIRECTORIES = (
    "01_custody/zotero/inventory",
    "01_custody/zotero/collections",
    "01_custody/files",
    "01_custody/read_attempts",
    "02_source_memory/notes",
    "02_source_memory/indexes/source_sets",
    "03_literature_synthesis/clusters",
    "03_literature_synthesis/gaps/candidates",
    "03_literature_synthesis/closest_prior_work",
    "03_literature_synthesis/packets",
    "11_state/runs",
    "11_state/fingerprints",
    "11_state/exports",
)

EXPANSION_DIRECTORIES = (
    "01_custody/citation_leads",
    "03_literature_synthesis/expansion/candidates",
)

EXPANSION_REGISTRIES = (
    "03_literature_synthesis/expansion/candidates.yml",
    "03_literature_synthesis/expansion/decisions.yml",
)


class IncompatibleArtifactSchemaError(RuntimeError):
    pass


def validate_opaque_id(value: str, *, field: str = "id") -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) or value in {".", ".."}:
        raise ValueError(f"{field} must be an opaque 1-128 character identifier")
    return value


def confined_child(root: Path, *parts: str) -> Path:
    root = root.expanduser().resolve()
    candidate = root.joinpath(*parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes confined root: {candidate}")
    return candidate


def run_directory(workspace: Path | str, run_id: str) -> Path:
    root = resolve_workspace(workspace)
    return confined_child(root / "11_state" / "runs", validate_opaque_id(run_id, field="run_id"))


def resolve_workspace(workspace: Path | str) -> Path:
    return Path(workspace).expanduser().resolve()


def initialize(workspace: Path | str, *, overwrite: bool = False) -> ArtifactManifest:
    root = resolve_workspace(workspace)
    ensure_dir(root)
    for relative in WORKSPACE_DIRECTORIES:
        ensure_dir(root / relative)

    manifest_path = root / "11_state" / "workspace_manifest.yml"
    existing_manifest = read_yaml(manifest_path, {}) or {}
    existing_schema = str(existing_manifest.get("artifact_schema_version") or "")
    if overwrite or not manifest_path.exists() or (_parse_version(existing_schema) and _version_tuple(existing_schema) >= (1, 1)):
        for relative in EXPANSION_DIRECTORIES:
            ensure_dir(root / relative)

    config_path = root / "auto-zettelkasten.yml"
    if overwrite or not config_path.exists():
        write_yaml(
            config_path,
            {
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "scope": "library",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "privacy": {"allow_cloud": False, "allow_network": False},
                "extraction": {"version": "1", "ocr": "auto", "vision": "configured_only"},
                "prompt_version": "1",
                "parallel": 4,
                "obsidian": {"vault": ""},
                "expansion": {"provider": "internal", "depth": 1, "budget": 100},
            },
        )

    if overwrite or not manifest_path.exists():
        write_yaml(
            manifest_path,
            {
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "created_at": now_iso(),
                "workspace": str(root),
            },
        )
    else:
        assert_compatible(root)

    registry_paths: list[Path] = []
    if _version_tuple(workspace_schema_version(root)) >= (1, 1):
        candidates_path = root / EXPANSION_REGISTRIES[0]
        decisions_path = root / EXPANSION_REGISTRIES[1]
        if not candidates_path.exists():
            write_yaml(
                candidates_path,
                {
                    "engine_version": ENGINE_VERSION,
                    "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                    "updated_at": now_iso(),
                    "candidates": [],
                },
            )
        if not decisions_path.exists():
            write_yaml(
                decisions_path,
                {
                    "engine_version": ENGINE_VERSION,
                    "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                    "updated_at": now_iso(),
                    "decisions": [],
                },
            )
        registry_paths = [candidates_path, decisions_path]

    artifacts = artifact_rows(root, [config_path, manifest_path, *registry_paths])
    return ArtifactManifest(
        status="initialized",
        workspace=root,
        artifacts=artifacts,
        created_at=now_iso(),
        metadata={
            "directory_count": len(WORKSPACE_DIRECTORIES)
            + (len(EXPANSION_DIRECTORIES) if _version_tuple(workspace_schema_version(root)) >= (1, 1) else 0)
        },
        artifact_schema_version=workspace_schema_version(root),
    )


def workspace_schema_version(workspace: Path | str) -> str:
    root = resolve_workspace(workspace)
    payload = read_yaml(root / "11_state" / "workspace_manifest.yml", {}) or {}
    return str(payload.get("artifact_schema_version") or "")


def require_schema(workspace: Path | str, minimum: str, *, operation: str) -> None:
    actual = workspace_schema_version(workspace)
    actual_tuple = _parse_version(actual)
    minimum_tuple = _parse_version(minimum)
    supported_tuple = _parse_version(ARTIFACT_SCHEMA_VERSION)
    if actual_tuple is None:
        raise IncompatibleArtifactSchemaError(f"workspace artifact schema is missing or malformed: {actual or '<missing>'}")
    if minimum_tuple is None or supported_tuple is None:
        raise RuntimeError("engine artifact schema constants are malformed")
    if actual_tuple > supported_tuple:
        raise IncompatibleArtifactSchemaError(
            f"workspace artifact schema {actual} is newer than supported schema {ARTIFACT_SCHEMA_VERSION}"
        )
    if actual_tuple < minimum_tuple:
        raise IncompatibleArtifactSchemaError(
            f"{operation} requires artifact schema {minimum}; workspace uses {actual}. "
            f"Run `auto-zettelkasten migrate --workspace WORKSPACE --to {minimum}`."
        )


def migrate(workspace: Path | str, *, target: str = "1.1", dry_run: bool = False) -> ArtifactManifest:
    """Perform the additive, idempotent 1.0 -> 1.1 workspace migration."""

    root = resolve_workspace(workspace)
    manifest_path = root / "11_state" / "workspace_manifest.yml"
    manifest = read_yaml(manifest_path, {}) or {}
    if not manifest:
        raise IncompatibleArtifactSchemaError("workspace manifest not found; initialize the workspace first")
    actual = str(manifest.get("artifact_schema_version") or "")
    if target != "1.1":
        raise ValueError("only additive migration target 1.1 is supported")
    if actual not in {"1.0", "1.1"}:
        raise IncompatibleArtifactSchemaError(
            f"workspace schema must be exactly 1.0 or 1.1 for this migration; found {actual or '<missing>'}"
        )

    planned_directories = [relative for relative in EXPANSION_DIRECTORIES if not (root / relative).exists()]
    planned_files = [relative for relative in EXPANSION_REGISTRIES if not (root / relative).exists()]
    needs_manifest_update = actual != target or str(manifest.get("engine_version") or "") != ENGINE_VERSION
    config_path = root / "auto-zettelkasten.yml"
    config = read_yaml(config_path, {}) or {}
    if not isinstance(config, dict):
        raise IncompatibleArtifactSchemaError("auto-zettelkasten.yml must contain a mapping")
    privacy = dict(config.get("privacy") or {}) if isinstance(config.get("privacy"), dict) else {}
    privacy.setdefault("allow_cloud", False)
    privacy.setdefault("allow_network", False)
    expansion = dict(config.get("expansion") or {}) if isinstance(config.get("expansion"), dict) else {}
    expansion.setdefault("provider", "internal")
    expansion.setdefault("depth", 1)
    expansion.setdefault("budget", 100)
    migrated_config = {
        **config,
        "engine_version": ENGINE_VERSION,
        "artifact_schema_version": target,
        "privacy": privacy,
        "expansion": expansion,
    }
    needs_config_update = migrated_config != config
    metadata = {
        "from_version": actual,
        "to_version": target,
        "planned_directories": planned_directories,
        "planned_files": planned_files,
        "manifest_update": needs_manifest_update,
        "config_update": needs_config_update,
        "canonical_notes_rewritten": False,
        "idempotent": True,
    }
    if dry_run:
        return ArtifactManifest(status="dry_run", workspace=root, created_at=now_iso(), metadata=metadata)

    for relative in EXPANSION_DIRECTORIES:
        ensure_dir(root / relative)
    candidates_path = root / EXPANSION_REGISTRIES[0]
    decisions_path = root / EXPANSION_REGISTRIES[1]
    if not candidates_path.exists():
        write_yaml(
            candidates_path,
            {
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": target,
                "updated_at": now_iso(),
                "candidates": [],
            },
        )
    if not decisions_path.exists():
        write_yaml(
            decisions_path,
            {
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": target,
                "updated_at": now_iso(),
                "decisions": [],
            },
        )
    if needs_manifest_update:
        write_yaml(
            manifest_path,
            {
                **dict(manifest),
                "engine_version": ENGINE_VERSION,
                "artifact_schema_version": target,
                "migrated_at": now_iso(),
            },
        )
    if needs_config_update:
        write_yaml(config_path, migrated_config)
    written = [candidates_path, decisions_path, manifest_path, config_path]
    return ArtifactManifest(
        status=(
            "already_current"
            if not planned_directories and not planned_files and not needs_manifest_update and not needs_config_update
            else "migrated"
        ),
        workspace=root,
        artifacts=artifact_rows(root, written),
        created_at=now_iso(),
        metadata=metadata,
    )


def assert_compatible(workspace: Path | str) -> None:
    root = resolve_workspace(workspace)
    path = root / "11_state" / "workspace_manifest.yml"
    payload = read_yaml(path, {}) or {}
    actual = str(payload.get("artifact_schema_version") or "")
    parsed = _parse_version(actual)
    supported = _parse_version(ARTIFACT_SCHEMA_VERSION)
    if parsed is None:
        raise IncompatibleArtifactSchemaError(
            f"workspace artifact schema is missing or malformed: {actual or '<missing>'}"
        )
    if supported is None:
        raise RuntimeError("engine artifact schema constant is malformed")
    if parsed < (1, 0):
        raise IncompatibleArtifactSchemaError(f"workspace artifact schema {actual} is older than supported schema 1.0")
    if parsed > supported:
        raise IncompatibleArtifactSchemaError(
            f"workspace artifact schema {actual} is newer than supported schema {ARTIFACT_SCHEMA_VERSION}"
        )


def load_config(workspace: Path | str) -> dict[str, Any]:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    return dict(read_yaml(root / "auto-zettelkasten.yml", {}) or {})


def artifact_rows(workspace: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted({item.resolve() for item in paths if item.exists() and item.is_file()}):
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            relative = path
        rows.append({"path": str(relative), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return rows


def _version_tuple(value: str) -> tuple[int, ...]:
    return _parse_version(value) or ()


def _parse_version(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)", str(value).strip())
    return (int(match.group(1)), int(match.group(2))) if match else None
