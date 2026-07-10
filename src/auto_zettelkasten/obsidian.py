from __future__ import annotations

import re
import shutil
from pathlib import Path

from .files import atomic_write_text, ensure_dir, now_iso, sha256_text, write_yaml
from .expansion import list_expansion_candidates
from .models import ArtifactManifest
from .notes import normalize_note_frontmatter
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
    cluster_root = root / "03_literature_synthesis" / "clusters"
    projections.extend((path, Path("Clusters") / path.name) for path in sorted(cluster_root.glob("cluster-*.md")))
    if (cluster_root / "INDEX.md").exists():
        projections.append((cluster_root / "INDEX.md", Path("Indexes") / "Cluster Index.md"))
    gap_root = root / "03_literature_synthesis" / "gaps"
    projections.extend((path, Path("Gaps") / path.name) for path in sorted((gap_root / "candidates").glob("*.md")))
    if (gap_root / "INDEX.md").exists():
        projections.append((gap_root / "INDEX.md", Path("Indexes") / "Gap Index.md"))
    prior_root = root / "03_literature_synthesis" / "closest_prior_work"
    # Gap candidates and closest-prior reviews share the same canonical stem.
    # Give the generated review projection a distinct name so an unqualified
    # gap link cannot resolve nondeterministically inside Obsidian.
    projections.extend(
        (path, Path("Closest Prior Work") / f"closest-prior-{path.name}")
        for path in sorted(prior_root.glob("*.md"))
    )
    expansion_root = root / "03_literature_synthesis" / "expansion"
    expansion_candidates = sorted((expansion_root / "candidates").glob("*.md"))
    projections.extend((path, Path("Expansion") / "Candidates" / path.name) for path in expansion_candidates)

    contents: dict[Path, str] = {}
    for source, relative in projections:
        target = export_root / relative
        content = source.read_text(encoding="utf-8")
        if relative.parts and relative.parts[0] == "Sources":
            content = normalize_note_frontmatter(content)
        contents[target] = content
    contents.setdefault(export_root / "Indexes" / "Source Index.md", "# Source Index\n\nNo validated atomic notes yet.\n")
    contents.setdefault(export_root / "Indexes" / "Cluster Index.md", "# Cluster Index\n\nNo canonical clusters yet.\n")
    contents.setdefault(export_root / "Indexes" / "Gap Index.md", "# Gap Candidate Index\n\nNo candidate gaps yet.\n")
    expansion_counts = _add_expansion_indexes(root, export_root, expansion_candidates, contents)
    home = export_root / "Home.md"
    contents[home] = (
        "# Auto-Zettelkasten\n\n"
        "- [[Indexes/Source Index|Source Index]]\n"
        "- [[Indexes/Cluster Index|Cluster Index]]\n"
        "- [[Indexes/Gap Index|Gap Candidate Index]]\n"
        "- [[Expansion/Inbox|Expansion Inbox]]\n\n"
        "This vault is generated. Edit canonical workspace artifacts, then export again.\n"
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
                "expansion_candidate_count": len(expansion_candidates),
                "expansion_state_counts": expansion_counts,
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
            "expansion_candidate_count": len(expansion_candidates),
            "expansion_state_counts": expansion_counts,
        },
    )


def _add_expansion_indexes(
    workspace: Path,
    export_root: Path,
    candidate_paths: list[Path],
    contents: dict[Path, str],
) -> dict[str, int]:
    rows = [row.to_dict() for row in list_expansion_candidates(workspace)]
    candidates_by_id = {
        str(row.get("suggestion_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("suggestion_id")
    }
    entries: dict[str, list[str]] = {state: [] for state in ("proposed", "accepted", "parked", "rejected")}
    for path in candidate_paths:
        row = candidates_by_id.get(path.stem, {})
        state = str(row.get("state") or "proposed")
        if state not in entries:
            state = "proposed"
        title = str(row.get("title") or _markdown_title(path) or path.stem)
        alias = re.sub(r"\s+", " ", re.sub(r"[\[\]|]+", " ", title)).strip() or path.stem
        details = [str(row.get("primary_relation") or "").strip()]
        score = row.get("score")
        if isinstance(score, (int, float)):
            details.append(f"score {float(score):.3f}")
        suffix = f" — {', '.join(detail for detail in details if detail)}" if any(details) else ""
        entries[state].append(f"- [[Expansion/Candidates/{path.stem}|{alias}]]{suffix}")

    labels = {
        "proposed": ("Inbox.md", "Expansion Inbox", "No proposed expansion candidates yet."),
        "accepted": ("Accepted.md", "Accepted Expansion Candidates", "No accepted expansion candidates yet."),
        "parked": ("Parked.md", "Parked Expansion Candidates", "No parked expansion candidates yet."),
        "rejected": ("Rejected.md", "Rejected Expansion Candidates", "No rejected expansion candidates yet."),
    }
    navigation = (
        "[[Expansion/Inbox|Inbox]] · "
        "[[Expansion/Accepted|Accepted]] · "
        "[[Expansion/Parked|Parked]] · "
        "[[Expansion/Rejected|Rejected]]"
    )
    for state, (filename, heading, empty_message) in labels.items():
        body = "\n".join(entries[state]) if entries[state] else empty_message
        contents[export_root / "Expansion" / filename] = (
            "---\n"
            "generated: true\n"
            "canonical_state: read_only_projection\n"
            f"expansion_state: {state}\n"
            "---\n\n"
            f"# {heading}\n\n"
            f"{navigation}\n\n"
            f"{body}\n\n"
            "This index is generated. Review decisions must be made through Auto-Zettelkasten.\n"
        )
    return {state: len(state_entries) for state, state_entries in entries.items()}


def _markdown_title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _missing_links_from_contents(export_root: Path, contents: dict[Path, str]) -> list[dict[str, str]]:
    relative_targets = {str(path.relative_to(export_root).with_suffix("")) for path in contents}
    targets_by_stem: dict[str, list[str]] = {}
    for path in contents:
        relative = str(path.relative_to(export_root).with_suffix(""))
        targets_by_stem.setdefault(path.stem, []).append(relative)
    missing: list[dict[str, str]] = []
    for path, text in contents.items():
        for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text):
            target = match.group(1).strip()
            normalized = target.removesuffix(".md")
            if normalized in relative_targets:
                continue
            stem_matches = targets_by_stem.get(Path(normalized).name, [])
            if len(stem_matches) > 1:
                missing.append(
                    {
                        "source": str(path.relative_to(export_root)),
                        "target": target,
                        "reason": "ambiguous_target",
                    }
                )
            elif not stem_matches:
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
