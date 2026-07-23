from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .files import ensure_dir, now_iso, read_yaml, sha256_file, write_yaml
from .models import (
    CURRENT_ARTIFACT_SCHEMA_VERSION,
    CURRENT_ENGINE_VERSION,
    ArtifactManifest,
    ExtractionPolicy,
    LiteratureMappingPolicy,
    NavigationPolicy,
    ProcessingPolicy,
)

CONFIG_FIELDS = {
    "engine_version",
    "artifact_schema_version",
    "scope",
    "provider",
    "model",
    "privacy",
    "extraction",
    "prompt_version",
    "parallel",
    "processing",
    "literature_mapping",
    "navigation",
    "obsidian",
}

WORKSPACE_DIRECTORIES = (
    "01_custody/zotero/inventory",
    "01_custody/zotero/collections",
    "01_custody/files",
    "01_custody/read_attempts",
    "02_source_memory/notes",
    "02_source_memory/profiles",
    "02_source_memory/indexes/source_sets",
    "03_literature_synthesis/maps",
    "03_literature_synthesis/clusters",
    "03_literature_synthesis/gaps/candidates",
    "03_literature_synthesis/closest_prior_work",
    "03_literature_synthesis/packets",
    "11_state/runs",
    "11_state/fingerprints",
    "11_state/note_metadata",
    "11_state/exports",
    "11_state/legacy_maps",
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
    config_path = root / "auto-zettelkasten.yml"
    manifest_path = root / "11_state" / "workspace_manifest.yml"
    if not overwrite and (config_path.exists() or manifest_path.exists()):
        assert_compatible(root)
    ensure_dir(root)
    for relative in WORKSPACE_DIRECTORIES:
        ensure_dir(root / relative)

    if overwrite or not config_path.exists():
        write_yaml(
            config_path,
            {
                "engine_version": CURRENT_ENGINE_VERSION,
                "artifact_schema_version": CURRENT_ARTIFACT_SCHEMA_VERSION,
                "scope": "library",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "privacy": {"allow_cloud": False},
                "extraction": {
                    "version": "2",
                    **ExtractionPolicy().to_dict(),
                    "vision": "configured_only",
                },
                "prompt_version": "8",
                "parallel": 4,
                "processing": {
                    "direct_read_char_limit": 120000,
                    "chunk_char_limit": 60000,
                    "max_total_chunks": 64,
                    "max_calls_per_document_run": 24,
                    "request_deadline_seconds": 120,
                    "document_deadline_seconds": 900,
                    "chunk_output_tokens": 900,
                    "synthesis_output_tokens": 3000,
                    "context_window_fraction": 0.8,
                    "estimated_chars_per_token": 3.5,
                },
                "literature_mapping": LiteratureMappingPolicy().to_dict(),
                "navigation": NavigationPolicy().to_dict(),
                "obsidian": {"vault": ""},
            },
        )

    if overwrite or not manifest_path.exists():
        write_yaml(
            manifest_path,
            {
                "engine_version": CURRENT_ENGINE_VERSION,
                "artifact_schema_version": CURRENT_ARTIFACT_SCHEMA_VERSION,
                "created_at": now_iso(),
                "workspace": str(root),
            },
        )

    artifacts = artifact_rows(root, [config_path, manifest_path])
    return ArtifactManifest(
        status="initialized",
        workspace=root,
        artifacts=artifacts,
        created_at=now_iso(),
        metadata={"directory_count": len(WORKSPACE_DIRECTORIES)},
    )


def assert_compatible(workspace: Path | str) -> None:
    root = resolve_workspace(workspace)
    manifest_path = root / "11_state" / "workspace_manifest.yml"
    config_path = root / "auto-zettelkasten.yml"
    if not manifest_path.is_file() or not config_path.is_file():
        raise IncompatibleArtifactSchemaError("existing workspace requires both config and workspace manifest")
    manifest = read_yaml(manifest_path, {})
    config = read_yaml(config_path, {})
    if not isinstance(manifest, Mapping) or not isinstance(config, Mapping):
        raise IncompatibleArtifactSchemaError("workspace config and manifest must be mappings")
    manifest_version = _parse_schema_version(manifest.get("artifact_schema_version"), field="workspace manifest")
    config_version = _parse_schema_version(config.get("artifact_schema_version"), field="workspace config")
    if manifest_version != config_version:
        raise IncompatibleArtifactSchemaError(
            f"workspace config schema {config_version} disagrees with manifest schema {manifest_version}"
        )
    supported = {
        (1, 0),
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
        (1, 5),
        (1, 6),
        (1, 7),
        (1, 8),
        (1, 9),
    }
    current = _parse_schema_version(CURRENT_ARTIFACT_SCHEMA_VERSION, field="current artifact schema")
    if manifest_version not in supported:
        actual = ".".join(str(value) for value in manifest_version)
        relation = "newer than" if manifest_version > current else "not supported by"
        raise IncompatibleArtifactSchemaError(
            f"workspace artifact schema {actual} is {relation} supported schema {CURRENT_ARTIFACT_SCHEMA_VERSION}"
        )


def load_config(workspace: Path | str) -> dict[str, Any]:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    payload = read_yaml(root / "auto-zettelkasten.yml", {}) or {}
    if not isinstance(payload, Mapping):
        raise ValueError("workspace config must be a mapping")
    config = dict(payload)
    unknown = sorted(set(config) - CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"unknown workspace config fields: {', '.join(unknown)}")
    if "processing" in config:
        if not isinstance(config["processing"], Mapping):
            raise ValueError("processing must be a mapping")
        ProcessingPolicy.from_dict(config["processing"])
    if "literature_mapping" in config:
        configured_policy = config["literature_mapping"]
        if not isinstance(configured_policy, Mapping):
            raise ValueError("literature_mapping must be a mapping")
        LiteratureMappingPolicy.from_dict(configured_policy)
    if "navigation" in config:
        configured_navigation = config["navigation"]
        if not isinstance(configured_navigation, Mapping):
            raise ValueError("navigation must be a mapping")
        NavigationPolicy.from_dict(configured_navigation)
    privacy = config.get("privacy", {})
    if not isinstance(privacy, Mapping):
        raise ValueError("privacy must be a mapping")
    if "allow_cloud" in privacy and not isinstance(privacy["allow_cloud"], bool):
        raise ValueError("privacy.allow_cloud must be a boolean")
    return config


def artifact_rows(workspace: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted({item.resolve() for item in paths if item.exists() and item.is_file()}):
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            relative = path
        rows.append({"path": str(relative), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return rows


def _parse_schema_version(value: Any, *, field: str) -> tuple[int, int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.0+)?", text)
    if not match:
        raise IncompatibleArtifactSchemaError(f"{field} has malformed artifact schema: {text or '<missing>'}")
    return int(match.group(1)), int(match.group(2))
