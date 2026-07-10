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
                "privacy": {"allow_cloud": False},
                "extraction": {"version": "1", "ocr": "auto", "vision": "configured_only"},
                "prompt_version": "1",
                "parallel": 4,
                "obsidian": {"vault": ""},
            },
        )

    manifest_path = root / "11_state" / "workspace_manifest.yml"
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
    path = root / "11_state" / "workspace_manifest.yml"
    payload = read_yaml(path, {}) or {}
    actual = str(payload.get("artifact_schema_version") or ARTIFACT_SCHEMA_VERSION)
    if _version_tuple(actual) > _version_tuple(ARTIFACT_SCHEMA_VERSION):
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
    parts: list[int] = []
    for part in value.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)
