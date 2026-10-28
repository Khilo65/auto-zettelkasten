"""Optional portable Markdown snapshots of effective notes and saved suggestions.

This module never updates source notes, overrides, mappings or provider state.
Only an explicitly approved, fresh destination receives generated Markdown.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tempfile
import unicodedata
from urllib.parse import quote

import yaml

from .documents import DocumentService, MAX_FILE_BYTES
from .notes import GRAPH_END_MARKER, GRAPH_START_MARKER, RELATED_END_MARKER, RELATED_START_MARKER, RELATED_HEADING
from .workspace import resolve_workspace


SCHEMA_VERSION = "markdown-related-export-v1"
_MANIFEST = ".auto-zettelkasten/related-export.json"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_MARKER = re.compile(r"^ {0,3}<!-- auto-zettelkasten:([a-z-]+):(start|end) -->[ \t]*$")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _markers(text):
    """Locate real standalone markers, ignoring fenced/indented examples."""
    result, fence, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _FENCE.match(stripped)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match:
            fence = match[1][0], len(match[1])
        else:
            marker = _MARKER.fullmatch(stripped)
            if marker:
                result.append((marker[1], marker[2], offset, offset + len(line)))
            elif "<!-- auto-zettelkasten:related:" in stripped and not line.startswith(("    ", "\t")):
                raise ValueError("Ambiguous automatic related-note markers.")
        offset += len(line)
    stack = []
    for name, boundary, _, _ in result:
        if boundary == "start":
            if name == "related" and stack or "related" in stack:
                raise ValueError("Nested generated sections cannot be exported.")
            stack.append(name)
        elif stack and stack[-1] == name:
            stack.pop()
    return result


def project_related_links(text, targets, *, source_path=None, titles=None):
    """Replace only our owned section; preserve authored text and other links."""
    markers = _markers(text)
    related = [row for row in markers if row[0] == "related"]
    if related:
        if len(related) != 2 or [row[1] for row in related] != ["start", "end"]:
            raise ValueError("Ambiguous automatic related-note markers.")
        left, right = related
        if any(left[2] < row[2] < right[2] for row in markers if row[0] != "related"):
            raise ValueError("Nested generated sections cannot be exported.")
    newline = "\r\n" if "\r\n" in text and "\n" not in text.replace("\r\n", "") else "\n"
    lines = [RELATED_START_MARKER, RELATED_HEADING, "", "Semantic similarity, not a claim of agreement.", ""]
    lines.extend("- " + _link_markup(target, source_path=source_path, title=(titles or {}).get(target)) for target in targets)
    if not targets:
        lines.append("No automatic connections selected for this export. Unconnected does not mean unrelated.")
    lines.append(RELATED_END_MARKER)
    block = newline.join(lines) + newline
    if related:
        return text[:related[0][2]] + block + text[related[1][3]:]
    # Remove only the exact legacy empty, owned placeholder, never a similarly
    # named manual section or a populated historical relationship block.
    graph = [row for row in markers if row[0] == "graph"]
    if len(graph) == 2 and [row[1] for row in graph] == ["start", "end"]:
        pattern = re.compile(r"(?m)^## Graph Links[ \t]*\r?\n(?:[ \t]*\r?\n)*"
                             + re.escape(GRAPH_START_MARKER) + r"\r?\n"
                             + re.escape("No committed typed links or canonical clusters yet.") + r"\r?\n"
                             + re.escape(GRAPH_END_MARKER) + r"(?:\r?\n|$)")
        for match in pattern.finditer(text):
            if match.start() <= graph[0][2] and match.end() == graph[1][3]:
                text = text[:match.start()] + text[match.end():]
                break
    separator = "" if not text or text.endswith(newline * 2) else newline if text.endswith(newline) else newline * 2
    return text + separator + block


def strip_related_links(text):
    """Exclude owned exported navigation from semantic inputs, not user prose."""
    markers = _markers(text)
    related = [row for row in markers if row[0] == "related"]
    if not related:
        return text
    if len(related) != 2 or [row[1] for row in related] != ["start", "end"]:
        raise ValueError("Ambiguous automatic related-note markers.")
    left, right = related
    if any(left[2] < row[2] < right[2] for row in markers if row[0] != "related"):
        raise ValueError("Nested generated sections cannot be exported.")
    return text[:left[2]] + text[right[3]:]


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("Export note paths must be safe relative Markdown paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")) or path.suffix.casefold() != ".md":
        raise ValueError("Export note paths must be safe relative Markdown paths.")
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden export paths are not supported.")
    return path


def _wiki_target(value):
    path = _relative(value)
    if any(char in value for char in "#|[]^:%"):
        raise ValueError("An automatic link target cannot be represented safely as a wikilink.")
    return str(path.with_suffix(""))


def _link_markup(target, *, source_path=None, title=None):
    path = _relative(target)
    label = title if isinstance(title, str) and title.strip() else path.stem
    try:
        if any(char in label for char in "|[]\r\n"):
            raise ValueError("The display title needs Markdown escaping.")
        return f"[[{_wiki_target(target)}|{label}]]"
    except ValueError:
        # Valid filenames can contain Markdown/wikilink delimiters. An encoded,
        # source-relative Markdown link preserves identity without renaming it.
        parent = str(_relative(source_path).parent) if source_path is not None else "."
        relative = posixpath.relpath(str(path), start=parent)
        label = re.sub(r"([\\\[\]`*_<>])", r"\\\1", label)
        return f"[{label}]({quote(relative, safe='/')})"


def _project_personal_metadata(text, title, personal):
    """Project private labels into a fresh export, never into source Markdown."""
    if title is None and not personal:
        return text, None
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 512
                              or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise ValueError("Invalid personal export title.")
    if not isinstance(personal, dict):
        raise ValueError("Invalid personal export properties.")
    newline = "\r\n" if "\r\n" in text and "\n" not in text.replace("\r\n", "") else "\n"
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    source = text[len(bom):]
    header = re.match(r"^---\r?\n([\s\S]*?)^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", source, re.MULTILINE)
    fields, nodes = {}, {}
    if source.startswith(("---\n", "---\r\n")) and header is None:
        raise ValueError("Personal metadata cannot be exported into malformed YAML.")
    if header:
        raw = header[1]
        if len(raw) > 65536:
            raise ValueError("Export properties exceed the metadata limit.")
        try:
            depth = 0
            for count, event in enumerate(yaml.parse(raw, Loader=yaml.SafeLoader)):
                if isinstance(event, yaml.AliasEvent) or count > 4096:
                    raise ValueError("Unsupported export properties.")
                if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                    depth += 1
                    if depth > 32:
                        raise ValueError("Unsupported export properties.")
                elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                    depth -= 1
            tree = yaml.compose(raw, Loader=yaml.SafeLoader)
            fields = yaml.safe_load(raw) or {}
            if tree is not None and not isinstance(tree, yaml.MappingNode) or not isinstance(fields, dict):
                raise ValueError("Export properties must be a YAML mapping.")
            for key, value in tree.value if tree is not None else []:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in nodes:
                    raise ValueError("Export properties contain duplicate or unsupported keys.")
                nodes[key.value] = key, value
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError("Personal metadata cannot be exported into malformed YAML.") from exc
    changes, conflicts = {}, []

    def strings(value):
        return [value] if isinstance(value, str) else [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    for key, value in personal.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid personal export property name.")
        valid = isinstance(value, (str, bool)) or type(value) in (int, float) and math.isfinite(value)
        if isinstance(value, list):
            valid = all(isinstance(item, str) for item in value)
        if not valid:
            raise ValueError("Unsupported personal export property value.")
        if key in {"tags", "aliases"}:
            previous = fields.get(key, fields.get("tag" if key == "tags" else "alias"))
            values = strings(previous) + strings(value)
            changes[key] = list(dict.fromkeys(item.removeprefix("#") if key == "tags" else item for item in values))
        elif key in fields:
            conflicts.append(key)
        else:
            changes[key] = value
    if title is not None:
        changes["title"] = title
        changes["aliases"] = list(dict.fromkeys(strings(changes.get("aliases", fields.get("aliases", fields.get("alias")))) + [title]))
    provenance = {"canonical_title": fields.get("title") if isinstance(fields.get("title"), str) else None, "personal_display_title": title,
                  "personal_properties": personal, "property_conflicts": sorted(conflicts),
                  "source_identity": {key: fields[key] for key in ("note_id", "source_id") if isinstance(fields.get(key), str)}}
    edits, additions = [], []
    for key, value in sorted(changes.items()):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if key in nodes:
            key_node, value_node = nodes[key]
            # End at the final scalar, not the collection's trailing comments.
            last = value_node
            if isinstance(last, yaml.SequenceNode) and not last.flow_style and last.value:
                last = last.value[-1]
            end = last.end_mark.index
            suffix = newline if header[1][:end].endswith("\n") else ""
            edits.append((key_node.end_mark.index, end, ": " + encoded + suffix))
        else:
            additions.append(json.dumps(key, ensure_ascii=False) + ": " + encoded + newline)
    if header:
        projected = header[1]
        for start, end, replacement in sorted(edits, reverse=True):
            projected = projected[:start] + replacement + projected[end:]
        if additions:
            projected += ("" if not projected or projected.endswith("\n") else newline) + "".join(additions)
        return bom + source[:header.start(1)] + projected + source[header.end(1):], provenance
    return bom + "---" + newline + "".join(additions) + "---" + newline + source, provenance


def _destination(value, source, override_root):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("Choose an absolute, fresh export destination.")
    target = Path(value).expanduser()
    if not target.is_absolute() or ".." in target.parts or any(ord(c) < 32 for c in str(target)):
        raise ValueError("Choose an absolute, fresh export destination.")
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Export paths cannot contain symlinks or non-directories.")
    target = target.resolve(strict=False)
    protected = [source]
    if override_root is not None:
        protected.append(Path(override_root).expanduser().resolve(strict=False))
    if any(target == root or target.is_relative_to(root) or root.is_relative_to(target) for root in protected):
        raise ValueError("Export destination must be separate from source notes and override storage.")
    if not target.parent.is_dir():
        raise ValueError("The export destination parent must already exist.")
    return target


def _snapshot(documents, workspace_id, cap):
    snapshot = documents.index(workspace_id)
    semantic = snapshot.get("semantic_map") or {}
    if snapshot.get("truncated") or snapshot.get("index_coverage", {}).get("status") != "complete":
        raise ValueError("A complete note snapshot is required for export.")
    coverage = semantic.get("coverage") or {}
    stale_count = coverage.get("stale", 0)
    permitted_partial = (semantic.get("status") == "partial" and type(stale_count) is int and stale_count > 0
                         and coverage.get("unavailable", 0) == coverage.get("unresolved", 0) == 0
                         and not coverage.get("dependency_budget_reached"))
    if semantic.get("status") != "current" and not permitted_partial:
        raise ValueError("A current saved semantic map is required; stale links cannot be exported.")
    notes, normalized = {}, set()
    for row in snapshot.get("documents", []):
        path = str(_relative(row.get("path")))
        key = unicodedata.normalize("NFKC", path).casefold()
        if path in notes or key in normalized:
            raise ValueError("Export paths collide after portable case/Unicode normalization.")
        normalized.add(key)
        # Public reads preserve a note's BOM and expose the saved override.
        effective = documents.read(workspace_id, path)
        if effective.get("text") != row.get("text") or effective.get("revision") != row.get("revision"):
            raise ValueError("Note changed while preparing Markdown export.")
        text = effective["text"]
        if not isinstance(text, str):
            raise ValueError("Export notes require readable Markdown text.")
        text, provenance = _project_personal_metadata(("\ufeff" if effective.get("bom") else "") + text,
                                                     effective.get("personal_display_title"), effective.get("personal_properties", {}))
        notes[path] = {"text": text, "display_title": effective.get("display_title") or PurePosixPath(path).stem,
                       "personal_metadata": provenance, "revision": effective["revision"],
                       "override_saved": effective.get("override_saved") is True}
    if not notes:
        raise ValueError("No readable notes are available to export.")
    candidates = {}
    for row in snapshot.get("relations", []):
        if row.get("type") != "semantic_similarity":
            continue
        if (row.get("derived") is not True or row.get("accepted") is not False
                or row.get("evidence_backed") is not False or row.get("decision_status") != "suggested"):
            raise ValueError("Only validated saved embedding suggestions may be exported.")
        source, target, score = row.get("source"), row.get("target"), row.get("score")
        if source not in notes or target not in notes or source == target:
            raise ValueError("Automatic link endpoints must bind to distinct exported notes.")
        revisions = semantic.get("note_revisions") or {}
        if any(revisions.get(path) != notes[path]["revision"] for path in (source, target)):
            raise ValueError("Automatic link endpoints are not bound to current note revisions.")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Invalid saved similarity score.")
        pair = tuple(sorted((source, target)))
        tie = "discovery:" + json.dumps([source, target, row["type"]], ensure_ascii=False, separators=(",", ":"))
        edge = {"source": pair[0], "target": pair[1], "score": score, "tie": tie}
        if pair not in candidates or (-score, tie) < (-candidates[pair]["score"], candidates[pair]["tie"]):
            candidates[pair] = edge
    degrees, selected = defaultdict(int), []
    for edge in sorted(candidates.values(), key=lambda row: (-row["score"], row["tie"])):
        if degrees[edge["source"]] >= cap or degrees[edge["target"]] >= cap:
            continue
        selected.append({key: value for key, value in edge.items() if key != "tie"})
        degrees[edge["source"]] += 1
        degrees[edge["target"]] += 1
    neighbors = defaultdict(list)
    for edge in selected:
        neighbors[edge["source"]].append((edge["target"], edge["score"]))
        neighbors[edge["target"]].append((edge["source"], edge["score"]))
    titles = {path: row["display_title"] for path, row in notes.items()}
    contents = {path: project_related_links(row["text"], [target for target, _ in sorted(neighbors[path], key=lambda row: (-row[1], row[0]))], source_path=path, titles=titles)
                for path, row in sorted(notes.items())}
    if any(len(text.encode("utf-8")) > MAX_FILE_BYTES for text in contents.values()):
        raise ValueError("An exported note would exceed the document size limit.")
    identity = {"notes": notes, "semantic_map": semantic, "relations": snapshot.get("relations", [])}
    personal_metadata = {path: row["personal_metadata"] for path, row in sorted(notes.items()) if row["personal_metadata"] is not None}
    return contents, selected, _digest(identity), sum(row["override_saved"] for row in notes.values()), len(candidates), stale_count, personal_metadata


def _matches_export(target, contents, manifest):
    if not target.exists():
        return False
    if not any(target.iterdir()):
        return False
    metadata = target / ".auto-zettelkasten"
    marker = target / _MANIFEST
    if metadata.is_symlink() or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Export destination contains existing files; choose a fresh folder.")
    wanted = set(contents) | {_MANIFEST}
    actual = set()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError("Existing export contains a symlink; it will not be changed.")
        if path.is_file():
            actual.add(path.relative_to(target).as_posix())
    if actual != wanted:
        raise ValueError("Export destination contains existing files; choose a fresh folder.")
    for path, text in {**contents, _MANIFEST: _json(manifest) + "\n"}.items():
        if (target / path).read_bytes() != text.encode("utf-8"):
            raise ValueError("Existing export was edited or differs; choose a fresh folder.")
    return True


def export_markdown_links(workspace, destination, *, apply=False, preview_identity=None, degree_cap=8,
                          override_root=None, documents=None, workspace_id=None):
    """Preview a deterministic linked vault; apply only an exact current preview.

    Existing matching snapshots are inert replays. Different or edited exports
    are never replaced. No provider, encoder, citation scan or Zotero is invoked.
    """
    if type(apply) is not bool or type(degree_cap) is not int or degree_cap not in {2, 4, 8, 16, 32}:
        raise ValueError("Choose a supported discovery-degree cap: 2, 4, 8, 16 or 32.")
    if not isinstance(workspace, (str, Path)) or not str(workspace):
        raise ValueError("Choose an existing Markdown workspace.")
    if override_root is not None and not isinstance(override_root, (str, Path)):
        raise ValueError("Override storage must be an absolute folder path.")
    root = resolve_workspace(workspace)
    owned = documents is None
    if owned:
        overrides = Path(override_root).expanduser() if override_root else Path.home() / ".config/auto-zettelkasten/workspace-overrides"
        documents = DocumentService(override_root=overrides)
    protected_overrides = getattr(getattr(documents, "_overrides", None), "root", override_root)
    try:
        target = _destination(destination, root, protected_overrides)
        if owned:
            workspace_id = documents.open(root)["id"]
        elif not workspace_id or Path(documents.workspace_path(workspace_id)) != root:
            raise ValueError("Supply the current document workspace identity.")
        contents, selected, source_identity, override_count, candidate_count, stale_count, personal_metadata = _snapshot(documents, workspace_id, degree_cap)
        file_hashes = {path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in contents.items()}
        identity = _digest({"schema_version": SCHEMA_VERSION, "source": str(root), "destination": str(target),
                            "source_identity": source_identity, "degree_cap": degree_cap, "files": file_hashes})
        manifest = {"schema_version": SCHEMA_VERSION, "preview_identity": identity, "source_identity": source_identity,
                    "source_workspace": str(root), "degree_cap": degree_cap, "links": selected,
                    "files": file_hashes, "personal_metadata": personal_metadata,
                    "automatic_link_origin": "local_embedding_similarity", "jev_enabled": False, "provider_calls": 0}
        existing = _matches_export(target, contents, manifest)
        report = {"status": "preview", "applied": False, "preview_identity": identity, "destination": str(target),
                  "note_count": len(contents), "edge_count": len(selected), "candidate_count": candidate_count,
                  "override_count": override_count, "degree_cap": degree_cap, "provider_calls": 0,
                  "stale_source_count": stale_count, "personal_metadata_count": len(personal_metadata),
                  "personal_title_count": sum(row["personal_display_title"] is not None for row in personal_metadata.values()),
                  "personal_property_count": sum(len(row["personal_properties"]) for row in personal_metadata.values()),
                  "personal_property_conflict_count": sum(len(row["property_conflicts"]) for row in personal_metadata.values()),
                  "jev_enabled": False, "source_changed": False,
                  "planned_files": list(contents), "existing_matching_export": existing}
        if not apply:
            return report
        if preview_identity != identity:
            raise ValueError("Markdown export requires the exact current preview identity.")
        if existing:
            return {**report, "status": "unchanged", "applied": True}
        parent_stamp = target.parent.stat()
        stage = Path(tempfile.mkdtemp(prefix=".related-export-", dir=target.parent))
        try:
            for relative, text in {**contents, _MANIFEST: _json(manifest) + "\n"}.items():
                file = stage / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                with file.open("xb") as stream:
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
            fresh = _snapshot(documents, workspace_id, degree_cap)
            if fresh[2] != source_identity:
                raise ValueError("Notes or saved map changed before export; preview again.")
            if _destination(target, root, protected_overrides) != target or (target.parent.stat().st_dev, target.parent.stat().st_ino) != (parent_stamp.st_dev, parent_stamp.st_ino):
                raise ValueError("Export destination changed before publication.")
            if target.exists() and any(target.iterdir()):
                raise ValueError("Export destination gained files; nothing was replaced.")
            os.rename(stage, target)
            return {**report, "status": "exported", "applied": True}
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    finally:
        if owned:
            documents.close()
