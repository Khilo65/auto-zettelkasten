from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from .files import atomic_write_text, ensure_dir, now_iso, read_yaml, sha256_text, write_yaml
from .models import ArtifactManifest
from .workspace import artifact_rows, assert_compatible, resolve_workspace


def export_obsidian(
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
    root = resolve_workspace(workspace)
    assert_compatible(root)
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.exists() and not new_vault:
        return ArtifactManifest(
            status="blocked",
            workspace=root,
            created_at=now_iso(),
            metadata={"reason": "vault_not_found", "vault": str(vault_path)},
        )
    try:
        folder_path = _safe_relative(folder or "Auto-Zettelkasten")
        project_path = _safe_relative(project_folder or root.name)
    except ValueError as exc:
        return ArtifactManifest(
            status="blocked",
            workspace=root,
            created_at=now_iso(),
            metadata={"reason": "unsafe_export_folder", "error": str(exc)},
        )
    try:
        export_root = _safe_export_root(vault_path, folder_path / project_path)
    except ValueError as exc:
        return ArtifactManifest(
            status="blocked",
            workspace=root,
            created_at=now_iso(),
            metadata={"reason": "unsafe_export_target", "error": str(exc), "vault": str(vault_path)},
        )

    projections: list[tuple[Path, Path]] = []
    projections.extend((path, Path("Sources") / path.name) for path in sorted((root / "02_source_memory" / "notes").glob("*.md")))
    source_index = root / "02_source_memory" / "indexes" / "INDEX.md"
    if source_index.exists():
        projections.append((source_index, Path("Indexes") / "Source Index.md"))
    source_cluster_index = root / "02_source_memory" / "indexes" / "CLUSTERS.md"
    if source_cluster_index.exists():
        projections.append(
            (source_cluster_index, Path("Indexes") / "CLUSTERS.md")
        )
    literature_indexes = root / "02_source_memory" / "indexes" / "by_literature"
    projections.extend(
        (path, Path("Indexes") / "by_literature" / path.name)
        for path in sorted(literature_indexes.glob("*.md"))
    )
    collection_indexes = root / "02_source_memory" / "indexes" / "collections"
    projections.extend(
        (path, Path("Indexes") / "collections" / path.relative_to(collection_indexes))
        for path in sorted(collection_indexes.rglob("*.md"))
    )
    latest_map = _latest_canonical_map(root / "03_literature_synthesis" / "maps")
    cluster_root = (
        latest_map / "clusters"
        if latest_map is not None and (latest_map / "clusters").is_dir()
        else root / "03_literature_synthesis" / "clusters"
    )
    projections.extend(
        (path, Path("Clusters") / path.name)
        for path in sorted(cluster_root.glob("*.md"))
        if path.name != "INDEX.md"
    )
    if (cluster_root / "INDEX.md").exists():
        projections.append((cluster_root / "INDEX.md", Path("Indexes") / "Cluster Index.md"))
    gap_root = (
        latest_map / "gaps"
        if latest_map is not None and (latest_map / "gaps").is_dir()
        else root / "03_literature_synthesis" / "gaps"
    )
    gap_notes_root = gap_root if latest_map is not None and gap_root.parent == latest_map else gap_root / "candidates"
    projections.extend((path, Path("Gaps") / path.name) for path in sorted(gap_notes_root.glob("*.md")) if path.name != "INDEX.md")
    if (gap_root / "INDEX.md").exists():
        projections.append((gap_root / "INDEX.md", Path("Indexes") / "Gap Index.md"))
    prior_root = root / "03_literature_synthesis" / "closest_prior_work"
    projections.extend((path, Path("Closest Prior Work") / path.name) for path in sorted(prior_root.glob("*.md")))
    contents: dict[Path, str] = {}
    for source, relative in projections:
        target = export_root / relative
        contents[target] = source.read_text(encoding="utf-8")
    contents.setdefault(export_root / "Indexes" / "Source Index.md", "# Source Index\n\nNo validated atomic notes yet.\n")
    contents.setdefault(export_root / "Indexes" / "Cluster Index.md", "# Cluster Index\n\nNo canonical clusters yet.\n")
    contents.setdefault(export_root / "Indexes" / "Gap Index.md", "# Gap Candidate Index\n\nNo candidate gaps yet.\n")
    map_export_relative: Path | None = None
    if latest_map is not None:
        map_manifest = read_yaml(latest_map / "manifest.yml", {}) or {}
        artifacts = map_manifest.get("artifacts", {}) if isinstance(map_manifest, dict) else {}
        primary_value = artifacts.get("literature_map_markdown", "") if isinstance(artifacts, dict) else ""
        primary_path = Path(str(primary_value)) if primary_value else latest_map / "INDEX.md"
        if not primary_path.is_file():
            primary_path = latest_map / "INDEX.md"
    else:
        primary_path = Path()
    if latest_map is not None and primary_path.is_file():
        map_index = primary_path.read_text(encoding="utf-8")
        map_index = map_index.replace("[[clusters/INDEX|Cluster Index]]", "[[Cluster Index]]")
        map_index = map_index.replace("[[gaps/INDEX|Gap Index]]", "[[Gap Index]]")
        map_index = map_index.replace("[[gaps/INDEX|Gap Registry Index]]", "[[Gap Index]]")
        map_index = map_index.replace("[[02_source_memory/indexes/INDEX|Source Index]]", "[[Source Index]]")
        map_export_relative = Path("Indexes") / primary_path.name
        contents[export_root / map_export_relative] = map_index
    home = export_root / "Home.md"
    contents[home] = (
        "# Auto-Zettelkasten\n\n"
        "- [[Indexes/Source Index|Source Index]]\n"
        "- [[Indexes/Cluster Index|Cluster Index]]\n"
        "- [[Indexes/Gap Index|Gap Candidate Index]]\n\n"
        + (
            f"- [[{map_export_relative.with_suffix('')}|Canonical Literature Map]]\n\n"
            if map_export_relative is not None
            else ""
        )
        + "This vault is generated. Edit canonical workspace artifacts, then export again.\n"
    )
    missing = _missing_links_from_contents(export_root, contents)
    if dry_run:
        return ArtifactManifest(
            status="dry_run",
            workspace=root,
            created_at=now_iso(),
            metadata={
                "vault": str(vault_path),
                "export_root": str(export_root),
                "file_count": len(contents) + 1,
                "missing_wikilink_count": len(missing),
                "missing_wikilinks": missing,
                "planned_files": [str(path.relative_to(export_root)) for path in sorted(contents)],
                "canonical_state_edited": False,
            },
        )
    if new_vault:
        ensure_dir(vault_path / ".obsidian")
    try:
        export_root = _safe_export_root(vault_path, folder_path / project_path)
    except ValueError as exc:
        return ArtifactManifest(
            status="blocked",
            workspace=root,
            created_at=now_iso(),
            metadata={"reason": "unsafe_export_target", "error": str(exc), "vault": str(vault_path)},
        )
    if replace and export_root.exists():
        shutil.rmtree(export_root)
    ensure_dir(export_root)
    for target, content in contents.items():
        ensure_dir(target.parent)
        atomic_write_text(target, content)
    written = list(contents)
    manifest_md = export_root / "EXPORT_MANIFEST.md"
    atomic_write_text(
        manifest_md,
        "# Export Manifest\n\n"
        f"- Workspace: `{root}`\n"
        f"- Generated at: `{now_iso()}`\n"
        f"- File count: {len(written)}\n"
        f"- Missing wiki links: {len(missing)}\n",
    )
    written.append(manifest_md)
    if record_link:
        link_id = sha256_text(str(vault_path) + "|" + str(export_root))[:12]
        write_yaml(
            root / "11_state" / "exports" / f"obsidian-{link_id}.yml",
            {
                "vault": str(vault_path),
                "folder": str(folder_path),
                "project_folder": str(project_path),
                "export_root": str(export_root),
                "updated_at": now_iso(),
            },
        )
    status = "exported" if not missing else "exported_with_missing_links"
    return ArtifactManifest(
        status=status,
        workspace=root,
        artifacts=artifact_rows(export_root, written),
        created_at=now_iso(),
        metadata={
            "vault": str(vault_path),
            "export_root": str(export_root),
            "file_count": len(written),
            "missing_wikilink_count": len(missing),
            "missing_wikilinks": missing,
            "canonical_state_edited": False,
            "record_link": record_link,
        },
    )


def _missing_links_from_contents(export_root: Path, contents: dict[Path, str]) -> list[dict[str, str]]:
    def link_key(value: str) -> str:
        return (
            unicodedata.normalize("NFKC", value)
            .translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))
            .casefold()
        )

    relative_targets = {
        link_key(str(path.relative_to(export_root).with_suffix("")))
        for path in contents
    }
    stems = {link_key(path.stem) for path in contents}
    missing: list[dict[str, str]] = []
    for path, text in contents.items():
        for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text):
            target = match.group(1).strip()
            normalized = link_key(target.removesuffix(".md"))
            if normalized not in relative_targets and link_key(Path(normalized).name) not in stems:
                missing.append({"source": str(path.relative_to(export_root)), "target": target})
    return missing


def _safe_folder(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value).strip(" .-")
    return value or "Workspace"


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe relative folder: {value}")
    return Path(*(_safe_folder(part) for part in path.parts))


def _safe_export_root(vault: Path, relative: Path) -> Path:
    vault = vault.resolve(strict=False)
    current = vault
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink export component is not allowed: {current}")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(vault):
        raise ValueError(f"export target escapes vault: {resolved}")
    return resolved


def _latest_canonical_map(maps_root: Path) -> Path | None:
    if not maps_root.exists():
        return None
    candidates = [path for path in maps_root.iterdir() if path.is_dir() and (path / "manifest.yml").is_file()]
    return max(candidates, key=lambda path: (path / "manifest.yml").stat().st_mtime_ns, default=None)
