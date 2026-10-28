"""Confined, offline Markdown access for the local workspace UI.

Writes use directory handles, exclusive temporary files and revision checks.
These checks detect ordinary concurrent edits and path replacement; they are not
a cross-process transaction against a writer ignoring our lock. Generated
source files remain read-only; optional private overrides store body edits elsewhere.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import json
from itertools import chain
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
from typing import Any, Iterator
import unicodedata

import yaml

from .navigation import TYPED_SOURCE_RELATIONS
from .relationships import RELATIONSHIP_REGISTRY_SCHEMA_VERSION, SUBSTANTIVE_RELATION_TYPES


MAX_FILE_BYTES = 1024 * 1024
MAX_ENTRIES = 5000
MAX_SCANNED_ENTRIES = 10000
MAX_TREE_BYTES = 16 * MAX_FILE_BYTES
INDEX_PAGE_DOCUMENTS = 32
INDEX_PAGE_BYTES = MAX_FILE_BYTES
INDEX_PAGE_OPERATIONS = 1024
MAX_INDEX_PATHS = 200000
MAX_REGISTRY_BYTES = 32 * 1024 * 1024
_SEMANTIC_MAP_PARTS = (".auto-zettelkasten", "mapping")
_SEMANTIC_MAP_SCHEMA = "local-semantic-map-v1"
_SEMANTIC_DEPENDENCY_BYTES = 32 * 1024 * 1024
_ORIGIN_METADATA_PARTS = ("11_state", "note_metadata")
_ORIGIN_METADATA_BYTES = 16 * MAX_FILE_BYTES
_ORIGIN_RECORD_BYTES = 65536
_INDEX_TTL = 600
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_LOCK = threading.RLock()
_DIR_HANDLES = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open, os.stat} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)
_SAFE_WRITES = _DIR_HANDLES and {os.mkdir, os.unlink, os.rmdir, os.rename} <= os.supports_dir_fd
_DENIED = frozenset({
    "node_modules", "venv", "__pycache__", "credentials", "credential",
    "secrets", "secret", "private", "01_custody", "11_state", "custody",
    "state", "profiles", "profile", "indexes", "index", "bundles",
    "packets", "fingerprints", "note_metadata", "read_attempts",
    "export_manifest.md",
})
_ENGINE_DIRS = frozenset({"02_source_memory", "03_literature_synthesis"})
_ENGINE_MARKERS = ("auto-zettelkasten.yml", "workspace_manifest.yml", "EXPORT_MANIFEST.md")
_ENGINE_FIELDS = re.compile(
    r"(?im)^\s*['\"]?(?:note_id|source_id|source_ids|zotero_item_key|source_file|"
    r"machine_preservation_hash|inspected_content_hash|reader_provider|reader_model|"
    r"engine_version|artifact_schema_version|cluster_id|gap_id|map_id)['\"]?\s*:"
)
_ENGINE_REASON = "Generated workspace or engine-owned note is read-only."
_PLATFORM_REASON = "Safe filesystem writes are unavailable on this platform."
_PRIVATE_NAMES = frozenset({"credential", "credentials", "secret", "secrets", "password", "passwords", "api_key", "api_keys", "tokens"})


class DocumentError(ValueError):
    """A workspace operation could not be completed safely."""


class DocumentConflict(DocumentError):
    def __init__(self, current: dict[str, Any]):
        super().__init__("The note changed on disk. Review both versions before saving.")
        self.current = current


class DocumentIndexChanged(DocumentError):
    code = "index_changed"

    def __init__(self):
        super().__init__("The workspace changed during indexing. Retry the index.")


class _IndexResourceLimit(DocumentError):
    pass


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _stamp(info: os.stat_result) -> tuple[Any, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _denied(part: str) -> bool:
    return part.startswith(".") or part.casefold() in _DENIED or Path(part).stem.casefold() in _PRIVATE_NAMES


def _published_index_directory(parts: tuple[str, ...]) -> bool:
    return parts[:2] == ("02_source_memory", "indexes") and (
        len(parts) == 2
        or (len(parts) == 3 and parts[2] in {"by_literature", "by_topic", "collections"})
        or (len(parts) == 4 and parts[2] == "collections" and bool(re.fullmatch(r"[A-Za-z0-9_-]+", parts[3])))
    )


def _published_index_path(parts: tuple[str, ...], *, prefix: bool = False) -> bool:
    if parts[:2] != ("02_source_memory", "indexes"):
        return False
    tail = parts[2:]
    if prefix and _published_index_directory(parts):
        return True
    if not tail:
        return False
    if len(tail) == 1:
        return tail[0] in {"INDEX.md", "CLUSTERS.md"}
    if len(tail) == 2:
        if tail[0] in {"by_literature", "by_topic"}:
            return tail[1].endswith(".md")
        return False
    return len(tail) == 3 and tail[0] == "collections" and bool(re.fullmatch(r"[A-Za-z0-9_-]+", tail[1])) and (
        tail[2] == "INDEX.md" or bool(re.fullmatch(r"(?:sources|relationships)-[0-9]{3,}\.md", tail[2]))
    )


def _parts(value: str, *, empty: bool = False, published: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or (not value and not empty):
        raise DocumentError("A relative workspace path is required.")
    if not value:
        return ()
    parts = tuple(value.split("/"))
    published_path = published and _published_index_path(parts, prefix=True)
    if len(parts) > 64 or any(
        not part or part in {".", ".."} or (_denied(part) and not (published_path and number == 1 and part == "indexes"))
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in '\\:<>"|?*' for char in part)
        or part.endswith((" ", "."))
        or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
        for number, part in enumerate(parts)
    ):
        raise DocumentError("This path is outside the allowed Markdown workspace.")
    return parts


def _newline(text: str) -> str:
    without_pairs = text.replace("\r\n", "")
    if "\r" in without_pairs or ("\r\n" in text and "\n" in without_pairs):
        return "mixed"
    return "crlf" if "\r\n" in text else "lf"


def _decode(data: bytes) -> tuple[str, bool, str]:
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data[3 if bom else 0:].decode("utf-8")
    except UnicodeError as exc:
        raise DocumentError("Only UTF-8 Markdown notes are supported.") from exc
    if "\x00" in text:
        raise DocumentError("Only UTF-8 text without NUL characters is supported.")
    return text, bom, _newline(text)


def _frontmatter(text: str) -> dict[str, Any]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    match = re.search(r"(?m)^---[ \t]*$", text[4:65544])
    if match is None or match.start() > 65536:
        return {}
    try:
        header = text[4:4 + match.start()]
        depth = 0
        for event in yaml.parse(header, Loader=_YAML_LOADER):
            if isinstance(event, yaml.AliasEvent):
                return {}
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    return {}
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        fields = yaml.load(header, Loader=_YAML_LOADER)
        return fields if isinstance(fields, dict) else {}
    except (yaml.YAMLError, RecursionError, ValueError):
        return {}


def _origin_identity(data: bytes) -> dict[str, Any]:
    """Project four scalar identity claims without constructing private YAML graphs.

    Writer metadata can share unrelated coverage structures through aliases. Their
    events are checked and skipped, never expanded into objects or identity values.
    """
    if len(data) > _ORIGIN_RECORD_BYTES:
        return {}
    wanted = {("metadata_schema_version",), ("note_path",),
              ("frontmatter", "note_id"), ("frontmatter", "source_id")}
    identity_maps = {(), ("frontmatter",)}
    values = {}
    anchors, active_anchors = set(), set()
    try:
        events = iter(yaml.parse(data.decode("utf-8-sig"), Loader=_YAML_LOADER))
        event_count = 0
        resolver = yaml.resolver.Resolver()

        def advance():
            nonlocal event_count
            event_count += 1
            if event_count > 32768:
                raise ValueError("Origin metadata event limit")
            return next(events)

        def string(event):
            if not isinstance(event, yaml.ScalarEvent):
                raise ValueError("Origin identity requires scalar strings")
            tag = event.tag or resolver.resolve(yaml.ScalarNode, event.value, event.implicit)
            if tag != "tag:yaml.org,2002:str":
                raise ValueError("Origin identity requires string tags")
            return event.value

        def walk(event, depth, path=None):
            anchor = getattr(event, "anchor", None)
            if isinstance(event, yaml.AliasEvent):
                if path in wanted or path in identity_maps or anchor not in anchors or anchor in active_anchors:
                    raise ValueError("Invalid origin metadata alias")
                return
            if anchor:
                if anchor in anchors:
                    raise ValueError("Duplicate origin metadata anchor")
                anchors.add(anchor)
            if path in wanted:
                values[path] = string(event)
                return
            if path in identity_maps and (not isinstance(event, yaml.MappingStartEvent)
                    or event.tag not in (None, "tag:yaml.org,2002:map")):
                raise ValueError("Origin identity requires direct mappings")
            if isinstance(event, yaml.ScalarEvent):
                return
            if depth > 32:
                raise ValueError("Origin metadata depth limit")
            if anchor:
                active_anchors.add(anchor)
            if isinstance(event, yaml.MappingStartEvent):
                seen = set()
                key = advance()
                while not isinstance(key, yaml.MappingEndEvent):
                    key_name = string(key) if path in identity_maps else None
                    walk(key, depth + 1)
                    child_path = (*path, key_name) if path in identity_maps else None
                    selected = child_path in wanted or child_path == ("frontmatter",)
                    if path in identity_maps and key_name == "<<":
                        raise ValueError("Origin identity cannot use merged mappings")
                    if selected and key_name in seen:
                        raise ValueError("Duplicate origin identity claim")
                    if selected:
                        seen.add(key_name)
                    walk(advance(), depth + 1, child_path if selected else None)
                    key = advance()
            elif isinstance(event, yaml.SequenceStartEvent):
                item = advance()
                while not isinstance(item, yaml.SequenceEndEvent):
                    walk(item, depth + 1)
                    item = advance()
            else:
                raise ValueError("Unexpected origin metadata event")
            if anchor:
                active_anchors.remove(anchor)

        if not isinstance(advance(), yaml.StreamStartEvent) or not isinstance(advance(), yaml.DocumentStartEvent):
            return {}
        walk(advance(), 1, ())
        if not isinstance(advance(), yaml.DocumentEndEvent) or not isinstance(advance(), yaml.StreamEndEvent):
            return {}
        if set(values) != wanted:
            return {}
        return {"metadata_schema_version": values[("metadata_schema_version",)],
                "note_path": values[("note_path",)],
                "frontmatter": {"note_id": values[("frontmatter", "note_id")],
                                "source_id": values[("frontmatter", "source_id")]}}
    except (yaml.YAMLError, UnicodeError, RecursionError, ValueError, StopIteration):
        return {}


def _document_metadata(path: str, fields: dict[str, Any]) -> dict[str, Any]:
    kind = fields.get("type")
    parts = path.split("/")
    navigation = Path(path).name.casefold() == "index.md" or _published_index_path(tuple(parts)) or kind in (
        "literature_map", "literature_map_index", "literature_map_pointer", "literature_cluster_index",
        "literature_gap_index", "coverage_index", "source_index",
    )
    coverage = {"status": "unknown", "label": "Source coverage unknown"}
    recorded = fields.get("coverage")
    legacy = fields.get("source_scope")
    status = fields.get("note_status")
    choices = (
        ("metadata_only", "Metadata only", "metadata only", "metadata_only_source_note"),
        ("abstract_only", "Abstract only", "abstract only", "abstract_only_atomic_note"),
        ("partial_document", "Partial document", "partial document", "partial_document_atomic_note"),
        ("analysis_pending", "Full text available; analysis pending", "full text available; analysis pending", "fulltext_available"),
        ("full_document", "Full text recorded", "full text", "analytical_atomic_note"),
    )
    for value, label, public, note_status in choices:
        if recorded == public or legacy == value or status == note_status or value == "full_document" and status == "verified_atomic_note":
            coverage = {"status": value, "label": label}
            break
    return {"navigation": bool(navigation), "source_coverage": coverage}


def _personal_projection(document, record=None):
    fields = _frontmatter(document["text"])
    personal = copy.deepcopy((record or {}).get("personal_properties", {}))
    title = (record or {}).get("display_title")
    managed = document.get("reason") == _ENGINE_REASON
    conflicts = sorted(key for key in personal if key in fields and key not in {"tags", "aliases"})

    def strings(value):
        return [value] if isinstance(value, str) else [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    tags = list(dict.fromkeys(value.removeprefix("#") for value in strings(fields.get("tags", fields.get("tag")))
                              + strings(personal.get("tags")) if value))
    aliases = list(dict.fromkeys(strings(fields.get("aliases", fields.get("alias"))) + strings(personal.get("aliases"))))
    ownership = {key: "generated" if managed else "personal" for key in fields}
    for key in personal:
        if key not in fields:
            ownership[key] = "personal"
        elif key in {"tags", "aliases"}:
            ownership[key] = "mixed"
    return {"display_title": title or Path(document["path"]).stem, "personal_display_title": title,
            "personal_properties": personal, "property_ownership": ownership, "property_conflicts": conflicts,
            "effective_tags": tags, "effective_aliases": aliases}


def _generated_identity(path: str, fields: dict[str, Any], revision: str, effective_text: str):
    """Recognize the writer's explicit projections, never title or source-note lookalikes."""
    parts = path.split("/")
    if parts[0] != "03_literature_synthesis":
        return None
    canonical = len(parts) >= 4 and parts[1] == "maps"
    relative = parts[3:] if canonical else parts[1:]
    kind = fields.get("type")
    if not isinstance(kind, str):
        return None
    identity_field = {"literature_map": "map_id", "literature_map_index": "map_id",
                      "literature_cluster": "cluster_id", "literature_gap": "gap_id"}.get(kind)
    identity = fields.get(identity_field) if identity_field else None
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", identity):
        return None
    if kind in {"literature_map", "literature_map_index"}:
        if len(relative) != 1 or canonical and parts[2] != identity:
            return None
    elif kind == "literature_cluster":
        if len(relative) != 2 or relative[0] != "clusters":
            return None
    elif canonical:
        if len(relative) != 2 or relative[0] != "gaps":
            return None
    elif len(relative) != 3 or relative[:2] != ["gaps", "candidates"]:
        return None
    key = (kind, identity, parts[-1], revision, hashlib.sha256(effective_text.encode("utf-8")).hexdigest())
    return key, canonical, path


def _document_identities(candidates):
    groups = {}
    for key, canonical, path in candidates:
        groups.setdefault(key[:3], {True: [], False: []})[canonical].append((key, path))
    # A compatibility projection can belong to only one historical map.
    identities = []
    for _, values in sorted(groups.items()):
        if len(values[True]) != 1:
            continue
        key, canonical_path = values[True][0]
        aliases = sorted(path for candidate, path in values[False] if candidate == key)
        if aliases:
            identities.append({"canonical_path": canonical_path, "path_aliases": aliases, "verified": True})
    return identities


def _index_coverage(partial: bool, *, complete: bool = True):
    if not complete:
        return {"status": "indexing", "label": "Indexing Markdown; source and comparison coverage unknown"}
    return {"status": "partial" if partial else "complete", "label": (
        "Markdown index is partial; source and comparison coverage unknown" if partial else
        "Markdown indexing complete; source and comparison coverage unknown")}


@dataclass
class _Anchor:
    path: Path
    info: os.stat_result
    fd: int | None
    parent_fd: int | None


@dataclass
class _Directory:
    anchors: list[_Anchor]

    @property
    def path(self) -> Path:
        return self.anchors[-1].path

    @property
    def fd(self) -> int | None:
        return self.anchors[-1].fd

    def verify(self) -> None:
        for anchor in self.anchors:
            current = (
                os.stat(anchor.path.name, dir_fd=anchor.parent_fd, follow_symlinks=False)
                if anchor.parent_fd is not None else anchor.path.lstat()
            )
            if _linked(current) or not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(anchor.info):
                raise DocumentError("A workspace directory changed. Reopen the workspace.")

    def stat(self, name: str) -> os.stat_result:
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return (self.path / name).lstat()

    def engine_owned(self) -> bool:
        for anchor in self.anchors:
            if anchor.path.name.casefold() in _ENGINE_DIRS:
                return True
            for marker in _ENGINE_MARKERS:
                try:
                    if anchor.fd is not None:
                        os.stat(marker, dir_fd=anchor.fd, follow_symlinks=False)
                    else:
                        (anchor.path / marker).lstat()
                    return True
                except FileNotFoundError:
                    continue
        return False


def _registry_display_value(start, events, anchors):
    """Read one small public metadata field without loading private YAML trees."""
    stack = []
    safe = True
    count = 0
    result = None
    for at, event in enumerate(chain((start,), events)):
        count += 1
        if count > 8192 or len(stack) > 12:
            raise ValueError("Relation display metadata exceeds its structure limit")
        anchor = getattr(event, "anchor", None)
        if anchor and not isinstance(event, yaml.AliasEvent):
            if at and anchor in anchors:
                raise ValueError("Duplicate registry anchor")
            anchors.add(anchor)
            safe = False
        if getattr(event, "tag", None) not in {None, "tag:yaml.org,2002:map", "tag:yaml.org,2002:seq",
                                                "tag:yaml.org,2002:str", "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int",
                                                "tag:yaml.org,2002:float", "tag:yaml.org,2002:null"}:
            safe = False
        if isinstance(event, yaml.AliasEvent):
            safe = False
        elif isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            stack.append({"value": {} if isinstance(event, yaml.MappingStartEvent) else [], "key": None})
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            value = stack.pop()["value"]
            if not stack:
                return value if safe else None, count
            parent = stack[-1]
            if isinstance(parent["value"], list):
                parent["value"].append(value)
            elif parent["key"] is not None:
                parent["value"][parent["key"]] = value
                parent["key"] = None
            else:
                safe = False
        elif isinstance(event, yaml.ScalarEvent):
            parent = stack[-1]
            excerpt = isinstance(parent["value"], dict) and parent["key"] == "text"
            if len(event.value) > (24000 if excerpt else 8192) or event.tag not in {None, "tag:yaml.org,2002:str", "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:null"}:
                safe = False
            # Keep scalars as strings; projection validates just the fields it uses.
            value = event.value[:24000 if excerpt else 8192]
            if isinstance(parent["value"], list):
                parent["value"].append(value)
            elif parent["key"] is None:
                if value in parent["value"]:
                    safe = False
                parent["key"] = value
            else:
                parent["value"][parent["key"]] = value
                parent["key"] = None
    raise ValueError("Incomplete relation display metadata")


def _relation_display_metadata(row, note_ids, source, target):
    """Publish bounded decision metadata and existing note destinations only."""
    result = {}
    for key in ("provider", "model", "template_version", "policy_version", "output_contract", "verification_status"):
        value = row.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value):
            result[key] = value
    for key in ("confidence", "top_probability", "probability_margin"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
            result[key] = value
    if row.get("evidence_basis") in {"note_summary", "supplied_passages"}:
        result["evidence_basis"] = row["evidence_basis"]
    probabilities = row.get("probabilities")
    if isinstance(probabilities, dict) and len(probabilities) <= 255:
        values = {}
        for key, raw in probabilities.items():
            try:
                value = float(raw) if not isinstance(raw, bool) else math.nan
            except (TypeError, ValueError, OverflowError):
                continue
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", key) and math.isfinite(value) and 0 <= value <= 1:
                values[key] = value
        if values:
            result["probabilities"] = values
    evidence = []
    rows = row.get("selected_evidence")
    for passage in rows[:2] if isinstance(rows, list) else ():
        if not isinstance(passage, dict):
            continue
        entry = note_ids.get(passage.get("note_id")) if isinstance(passage.get("note_id"), str) else None
        if not entry or entry[1] not in {source, target} or not isinstance(passage.get("text"), str):
            continue
        value = {"path": entry[1], "text": passage["text"][:1600]}
        for key in ("passage_id", "attribution"):
            if isinstance(passage.get(key), str):
                value[key] = passage[key][:256]
        locator = passage.get("locator")
        if isinstance(locator, dict):
            projected = {}
            for key in ("kind", "heading"):
                if isinstance(locator.get(key), str):
                    projected[key] = locator[key][:256]
            for key in ("line_start", "line_end"):
                raw = locator.get(key)
                if isinstance(raw, str) and raw.isascii() and raw.isdecimal() and len(raw) < 8:
                    raw = int(raw)
                if type(raw) is int and 1 <= raw <= 10_000_000:
                    projected[key] = raw
            if projected:
                value["locator"] = projected
        evidence.append(value)
    if evidence:
        result["selected_evidence"] = evidence
    return result


class DocumentService:
    """One explicitly selected root; opening another root invalidates old IDs.

    Text excludes an optional UTF-8 BOM, and retains its original line endings.
    Saves accept editor-normalized LF and restore the document's disk convention.
    Mixed/bare-CR documents can be read but are never written. Recovery keys are
    stable identifiers for UI recovery storage, not on-disk backup locations.
    """

    def __init__(self, *, journal_root: Path | None = None, override_root: Path | None = None) -> None:
        self._root: Path | None = None
        self._root_identity: tuple[int, int, int] | None = None
        self._workspace_id = ""
        self._documents: dict[str, tuple[str, tuple[int, int, int]]] = {}
        self._document_ids: dict[str, str] = {}
        self._journal_root = journal_root or Path.home() / ".config" / "auto-zettelkasten" / "workspace-operations"
        self._relocator = None
        self._index_run = None
        self._completed_index = None
        self._recall_snapshot = None
        self._overrides = None
        if override_root is not None:
            from .document_overrides import Overrides
            self._overrides = Overrides(override_root)

    def _relocations(self):
        if self._relocator is None:
            from .organization import Relocations
            self._relocator = Relocations(self, self._journal_root)
        return self._relocator

    def close(self) -> None:
        with _LOCK:
            self._close_index_run()
            self._completed_index = None
            self._recall_snapshot = None
            self._root = None
            self._root_identity = None
            self._workspace_id = ""
            self._documents.clear()
            self._document_ids.clear()
            if self._relocator:
                self._relocator.previews.clear()
                self._relocator.receipts.clear()

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except OSError as exc:
            raise DocumentError("The filesystem could not complete this operation safely.") from exc

    def _workspace(self, workspace_id: str) -> None:
        if not self._root or not workspace_id or workspace_id != self._workspace_id:
            raise DocumentError("Open a workspace before accessing its documents.")

    def workspace_key(self, workspace_id: str) -> str:
        """Stable private preference identity; session IDs deliberately change on open."""
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            with self._directory():
                return hashlib.sha256(str(self._root.resolve()).encode("utf-8")).hexdigest()

    def workspace_path(self, workspace_id: str) -> str:
        """Return the selected, revalidated root for local read-only services."""
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            with self._directory():
                return str(self._root)

    def recall_corpus(self, workspace_id: str) -> dict[str, Any]:
        """Read effective Markdown without the file-panel's display limits.

        This has its own inventory and never consumes or invalidates the UI's
        paged graph cursor. A second inventory pass binds every returned note to
        one unchanged filesystem/override snapshot. Search limits fail openly.
        """
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            stamp = self._index_root_stamp()
            previous = getattr(self, "_recall_snapshot", None)

            def run():
                return {"root_stamp": stamp, "progress": {"scanned": 0}}

            if previous and previous["workspace_id"] == workspace_id and previous["root_stamp"] == stamp:
                try:
                    for _ in self._index_inventory(run(), {}, previous["inventory"]):
                        pass
                    if self._index_root_stamp() == stamp:
                        return copy.deepcopy(previous["result"])
                except DocumentIndexChanged:
                    pass
            inventory, documents, skipped = {}, [], []
            total_bytes = 0
            iterator = self._index_inventory(run(), inventory)
            try:
                for item in iterator:
                    if item is None or item[0] != "file":
                        continue
                    _, path, info, _, directory = item
                    if len(documents) + len(skipped) >= 20_000:
                        raise DocumentError("The recall corpus exceeds the 20,000-note resource limit.")
                    try:
                        raw, current = self._read_bytes(directory, Path(path).name)
                        if _stamp(info) != _stamp(current):
                            raise DocumentIndexChanged()
                        text, _, newline = _decode(raw)
                        reason = self._reason(directory, text, newline)
                        document = {"path": path, "text": text, "revision": hashlib.sha256(raw).hexdigest(),
                                    "editable": not reason, "reason": reason, "newline": newline}
                        if self._overrides is not None:
                            document = self._override_document(document)
                        fields = _frontmatter(text)
                        document.update(_document_metadata(path, fields), frontmatter=fields)
                        total_bytes += len(document["text"].encode("utf-8"))
                        if total_bytes > 128 * 1024 * 1024:
                            raise _IndexResourceLimit("The recall corpus exceeds the 128 MiB text resource limit.")
                        documents.append(document)
                    except (DocumentIndexChanged, _IndexResourceLimit):
                        raise
                    except (DocumentError, UnicodeError):
                        skipped.append(path)
            finally:
                iterator.close()
            for _ in self._index_inventory(run(), {}, inventory):
                pass
            if self._index_root_stamp() != stamp:
                raise DocumentIndexChanged()
            result = {"documents": sorted(documents, key=lambda row: row["path"]),
                      "truncated": bool(skipped), "skipped_note_count": len(skipped),
                      "warnings": ["Some notes exceed the document size/format limit and were excluded from recall."] if skipped else []}
            self._recall_snapshot = {"workspace_id": workspace_id, "root_stamp": stamp,
                                     "inventory": inventory, "result": result}
            return copy.deepcopy(result)

    def recall_relationships(self, workspace_id: str, note_paths: list[str]) -> list[dict[str, Any]]:
        """Project saved accepted links only between explicitly retrieved notes."""
        if (not isinstance(note_paths, list) or len(note_paths) > 32
                or any(not isinstance(path, str) for path in note_paths)):
            raise DocumentError("Recall relationship endpoints must be at most 32 note paths.")
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            snapshot = self.recall_corpus(workspace_id)
            selected = [row for row in snapshot["documents"] if row["path"] in note_paths]
            stamp = self._index_root_stamp()
            run = {"root_stamp": stamp, "note_ids": {}, "cluster_ids": {}, "projected": {},
                   "unresolved": {}, "warnings": [], "truncated": False, "recall_only": True,
                   "recall_paths": set(note_paths),
                   "recall_overridden_paths": {row["path"] for row in selected if row.get("body_override_saved")}}
            for row in snapshot["documents"]:
                self._index_identity(run["note_ids"], row["frontmatter"].get("note_id"), row["path"],
                                     2 if row["path"].split("/")[:2] == ["02_source_memory", "notes"] else 1)
            for _ in self._index_registry(run):
                pass
            if self._index_root_stamp() != stamp:
                raise DocumentIndexChanged()
            return sorted(run["projected"].values(), key=lambda row: row["id"])

    def _note_origin(self, path, fields, *, dependencies=None, budget=None):
        """Only protected writer metadata establishes managed source provenance.

        Read-only status, folders, titles and public frontmatter are not authorship
        evidence. Existing notes without an origin receipt remain unknown.
        """
        note_id = fields.get("note_id")
        if not isinstance(note_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", note_id):
            return "unknown"
        name = f"{note_id}.yml"
        stamp = self._semantic_dependency_stamp(_ORIGIN_METADATA_PARTS, name)
        if dependencies is not None:
            if name in dependencies and dependencies[name] != stamp:
                raise DocumentIndexChanged()
            dependencies[name] = stamp
        if stamp is None or stamp == "unavailable":
            return "unknown"
        try:
            with self._directory(_ORIGIN_METADATA_PARTS) as directory:
                size = directory.stat(name).st_size
                if size > _ORIGIN_RECORD_BYTES or budget is not None and size > budget[0]:
                    return "unknown"
                data, info = self._read_derived_bytes(directory, name, max_bytes=_ORIGIN_RECORD_BYTES)
                if _stamp(info) != stamp:
                    raise DocumentIndexChanged()
            if budget is not None:
                budget[0] -= len(data)
            metadata = _origin_identity(data)
            from .notes import NOTE_METADATA_SCHEMA_VERSION
            stored = metadata.get("frontmatter")
            source_id = stored.get("source_id") if isinstance(stored, dict) else None
            if (metadata.get("metadata_schema_version") == NOTE_METADATA_SCHEMA_VERSION
                    and metadata.get("note_path") == path and isinstance(stored, dict)
                    and stored.get("note_id") == note_id
                    and isinstance(source_id, str) and 0 < len(source_id) <= 256
                    and ("source_id" not in fields or fields["source_id"] == source_id)):
                return "managed"
        except DocumentIndexChanged:
            raise
        except (DocumentError, OSError, UnicodeError, ValueError, RecursionError):
            pass
        return "unknown"

    @contextmanager
    def _directory(self, parts: tuple[str, ...] = ()) -> Iterator[_Directory]:
        assert self._root is not None
        target = self._root.joinpath(*parts)
        anchors: list[_Anchor] = []
        current = Path(target.anchor)
        try:
            for part in ("", *target.parts[1:]):
                if part:
                    current /= part
                parent_fd = anchors[-1].fd if anchors else None
                fd = None
                if _DIR_HANDLES:
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                    fd = os.open(part if parent_fd is not None else current, flags, dir_fd=parent_fd)
                    info = os.fstat(fd)
                else:
                    info = current.lstat()
                anchors.append(_Anchor(current, info, fd, parent_fd))
                if _linked(info) or not stat.S_ISDIR(info.st_mode):
                    raise DocumentError("Workspace paths cannot contain symlinks or non-directories.")
                if current == self._root and self._root_identity is not None:
                    if _identity(info) != self._root_identity:
                        raise DocumentError("The workspace root changed. Reopen the workspace.")
            directory = _Directory(anchors)
            directory.verify()
            yield directory
        finally:
            for anchor in reversed(anchors):
                if anchor.fd is not None:
                    os.close(anchor.fd)

    def _read_bytes(self, directory: _Directory, name: str) -> tuple[bytes, os.stat_result]:
        before = directory.stat(name)
        if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DocumentError("Only regular, non-linked Markdown files can be opened.")
        if before.st_size > MAX_FILE_BYTES:
            raise DocumentError("The note exceeds the 1 MiB document limit.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        if _DIR_HANDLES:
            flags |= os.O_NOFOLLOW
        fd = os.open(name if directory.fd is not None else directory.path / name, flags, dir_fd=directory.fd)
        try:
            opened = os.fstat(fd)
            if _stamp(opened) != _stamp(before):
                raise DocumentError("The note changed while opening it. Try again.")
            data = bytearray()
            while len(data) <= MAX_FILE_BYTES:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_FILE_BYTES:
                raise DocumentError("The note exceeds the 1 MiB document limit.")
            directory.verify()
            if _stamp(os.fstat(fd)) != _stamp(opened) or _stamp(directory.stat(name)) != _stamp(opened):
                raise DocumentError("The note changed while reading it. Try again.")
            return bytes(data), opened
        finally:
            os.close(fd)

    def _reason(self, directory: _Directory, text: str = "", newline: str = "lf") -> str:
        if directory.engine_owned() or "auto-zettelkasten:" in text.casefold() or _ENGINE_FIELDS.search(text):
            return _ENGINE_REASON
        if not _SAFE_WRITES:
            return _PLATFORM_REASON
        if newline == "mixed":
            return "Mixed or bare-CR line endings are read-only."
        if not directory.anchors[-1].info.st_mode & 0o222:
            return "The folder is read-only."
        return ""

    def _published_indexes(self, directory: _Directory) -> bool:
        for marker in _ENGINE_MARKERS:
            try:
                info = directory.stat(marker)
            except FileNotFoundError:
                continue
            if not _linked(info) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                directory.verify()
                return True
        return False

    @staticmethod
    def _override_parts(text: str):
        text = text.replace("\r\n", "\n")
        if text.startswith("\ufeff"):
            return None
        if not text.startswith("---\n"):
            return "", text
        closing = re.search(r"(?m)^(?:---|\.\.\.)[ \t]*(?:\n|$)", text[4:])
        if closing is None or closing.start() > 65536:
            return None
        header_end = 4 + closing.end()
        try:
            header = text[4:4 + closing.start()]
            depth = 0
            for event in yaml.parse(header, Loader=_YAML_LOADER):
                if isinstance(event, yaml.AliasEvent):
                    return None
                if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                    depth += 1
                    if depth > 32:
                        return None
                elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                    depth -= 1
            if not isinstance(yaml.load(header, Loader=_YAML_LOADER), dict):
                return None
        except (yaml.YAMLError, RecursionError, ValueError):
            return None
        return text[:header_end], text[header_end:]

    def _override_location(self):
        return bool(self._overrides and _SAFE_WRITES and self._root
                    and not self._overrides.root.is_relative_to(self._root)
                    and not self._root.is_relative_to(self._overrides.root))

    def _override_generation(self):
        return self._overrides.generation(self._root, self._root_identity) if self._override_location() else None

    def _override_document(self, document, view=None, *, guarded=False, protected=None):
        document = {**document, **_personal_projection(document)}
        if self._overrides is None:
            return document
        result = {**document, "override_available": False, "override_saved": False, "body_override_saved": False,
                  "source_changed": False, "source_revision": document["revision"]}
        if (not self._override_location() or document.get("reason") != _ENGINE_REASON
                or document.get("newline") == "mixed"):
            return result
        parts = protected if protected is not None else self._override_parts(document["text"])
        if parts is None:
            return result
        if not guarded:
            try:
                with self._overrides.guard(self._root, self._root_identity) as opened:
                    return self._override_document(document, opened, guarded=True, protected=parts)
            except DocumentError as exc:
                if str(exc) == "Override storage is read-only.":
                    return result
                raise
        record, digest, info = view.read(document["path"]) if view else (None, None, None)
        result["override_available"] = view.writable() if view else True
        if info is not None and not info.st_mode & 0o222:
            result["override_available"] = False
        if record:
            text = parts[0] + record["body"] if "body" in record else document["text"]
            if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise DocumentError("The regenerated properties and saved override exceed the document limit.")
            result.update(text=text, newline="lf" if "body" in record else document.get("newline", "lf"), override_saved=True,
                          body_override_saved=text.replace("\r\n", "\n") != document["text"].replace("\r\n", "\n"),
                          source_changed="body" in record and record["source_revision"] != document["revision"],
                          revision=hashlib.sha256(f"override-v1\0{document['revision']}\0{digest}".encode()).hexdigest())
            result.update(_personal_projection(document, record))
        return result

    def _document(self, relative: str, directory: _Directory, *, effective=True) -> dict[str, Any]:
        data, info = self._read_bytes(directory, Path(relative).name)
        text, bom, newline = _decode(data)
        reason = self._reason(directory, text, newline)
        if not info.st_mode & 0o222 and not reason:
            reason = "The note is read-only."
        document_id = self._document_ids.setdefault(relative, secrets.token_urlsafe(24))
        self._documents[document_id] = relative, _identity(info)
        recovery_key = hashlib.sha256(f"{self._root}\0{relative}".encode("utf-8")).hexdigest()
        fields = _frontmatter(text)
        document = {
            "id": document_id, "path": relative, "name": Path(relative).name,
            "text": text, "revision": hashlib.sha256(data).hexdigest(),
            "editable": not reason, "reason": reason, "bom": bom,
            "newline": newline, "recovery_key": recovery_key,
            **_document_metadata(relative, fields),
            "note_origin": self._note_origin(relative, fields),
        }
        return self._override_document(document) if effective else document

    def open(self, path: str | Path) -> dict[str, Any]:
        with _LOCK, self._errors():
            if not isinstance(path, (str, Path)) or not str(path) or any(
                ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in str(path)
            ):
                raise DocumentError("Select an existing absolute workspace folder.")
            try:
                root = Path(path).expanduser()
            except RuntimeError as exc:
                raise DocumentError("The workspace folder could not be located.") from exc
            # macOS /private is a system prefix; private workspace children stay excluded.
            denied_root = any(
                _denied(part) and not (index == 0 and part == "private" and sys.platform == "darwin")
                for index, part in enumerate(root.parts[1:])
            )
            if not root.is_absolute() or ".." in root.parts or denied_root:
                raise DocumentError("This folder is outside the allowed Markdown workspace.")
            previous = self._root, self._root_identity, self._workspace_id, self._documents, self._document_ids
            self._root, self._root_identity = root, None
            try:
                with self._directory() as directory:
                    self._root_identity = _identity(directory.anchors[-1].info)
                self._workspace_id = secrets.token_urlsafe(24)
                self._documents = {}
                self._document_ids = {}
                self._relocations().previews.clear()
                self._relocations().receipts.clear()
                self._relocations().recover()
                result = self.tree(self._workspace_id)
                self._close_index_run()
                self._completed_index = None
                self._recall_snapshot = None
                return result
            except (DocumentError, OSError):
                self._root, self._root_identity, self._workspace_id, self._documents, self._document_ids = previous
                raise

    def tree(self, workspace_id: str) -> dict[str, Any]:
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            entries: list[dict[str, Any]] = []
            pending = deque([()])
            scanned = 0
            inspected_bytes = 0
            truncated = False
            with self._directory() as root_directory:
                reason = self._reason(root_directory)
                published = self._published_indexes(root_directory)
            if self._relocator and self._relocator.blocked:
                reason = reason or self._relocator.blocked
            while pending and not truncated:
                parts = pending.popleft()
                with self._directory(parts) as directory:
                    children = []
                    with os.scandir(directory.fd if directory.fd is not None else directory.path) as scan:
                        for entry in scan:
                            scanned += 1
                            if scanned > MAX_SCANNED_ENTRIES:
                                truncated = True
                                break
                            children.append(entry.name)
                    for name in sorted(children, key=str.casefold):
                        try:
                            child = _parts("/".join((*parts, name)), published=published)
                        except DocumentError:
                            continue
                        info = directory.stat(name)
                        if _linked(info):
                            continue
                        relative = "/".join(child)
                        row_reason = self._reason(directory)
                        presentation = {}
                        if stat.S_ISDIR(info.st_mode):
                            if child[:2] == ("02_source_memory", "indexes") and not _published_index_directory(child):
                                continue
                            kind = "directory"
                            with self._directory(child) as child_directory:
                                row_reason = self._reason(child_directory)
                            pending.append(child)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and Path(name).suffix.casefold() == ".md":
                            kind = "file"
                            if info.st_size > MAX_FILE_BYTES:
                                row_reason = "The note exceeds the 1 MiB document limit."
                            elif inspected_bytes + info.st_size > MAX_TREE_BYTES:
                                truncated = True
                                break
                            else:
                                data, info = self._read_bytes(directory, name)
                                inspected_bytes += len(data)
                                try:
                                    text, _, newline = _decode(data)
                                    row_reason = self._reason(directory, text, newline)
                                    if self._overrides is not None:
                                        effective = self._override_document({"path": relative, "text": text, "reason": row_reason,
                                                                             "revision": hashlib.sha256(data).hexdigest(), "newline": newline})
                                        presentation = {key: effective[key] for key in ("display_title", "personal_display_title")}
                                except DocumentError as exc:
                                    row_reason = str(exc)
                                if not info.st_mode & 0o222 and not row_reason:
                                    row_reason = "The note is read-only."
                        else:
                            continue
                        entries.append({"path": relative, "name": name, "kind": kind, "editable": not row_reason, "reason": row_reason, **presentation})
                        if len(entries) >= MAX_ENTRIES:
                            truncated = True
                            break
                    directory.verify()
            assert self._root is not None
            workspace = {
                "id": self._workspace_id, "name": self._root.name,
                "path": str(self._root), "entries": sorted(entries, key=lambda row: row["path"].casefold()),
                "truncated": truncated, "editable": not reason, "reason": reason,
                **self._workspace_presentation(),
            }
            master = "02_source_memory/indexes/INDEX.md"
            if published and any(row["path"] == master and row["kind"] == "file" and row["reason"] == _ENGINE_REASON for row in entries):
                workspace["preferred_path"] = master
            return workspace

    def _workspace_presentation(self):
        """Classify only fixed, bounded metadata; never follow exported paths."""
        stamp = (str(self._root), self._root_identity, self._index_semantic_stamp())
        export_stamp = None
        try:
            with self._directory((".auto-zettelkasten",)) as directory:
                export_stamp = _stamp(directory.stat("related-export.json"))
        except FileNotFoundError:
            pass
        except (DocumentError, OSError):
            export_stamp = "unavailable"
        stamp = (*stamp, export_stamp)
        cached = getattr(self, "_workspace_presentation_cache", None)
        if cached and cached[0] == stamp:
            return dict(cached[1])
        result = {"workspace_kind": "markdown", "mapping_status": "absent"}
        mapping = self._read_semantic_map()
        if mapping is not None:
            result = {"workspace_kind": "mapped", "mapping_status": "invalid" if mapping.get("unavailable") else "saved"}
        elif export_stamp is not None:
            try:
                with self._directory((".auto-zettelkasten",)) as directory:
                    raw, _ = self._read_derived_bytes(directory, "related-export.json", max_bytes=16 * MAX_FILE_BYTES)
                manifest = json.loads(raw)
                source = manifest.get("source_workspace")
                files = manifest.get("files")
                if (manifest.get("schema_version") != "markdown-related-export-v1"
                        or not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts
                        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in source)
                        or not isinstance(files, dict) or len(files) > 20000
                        or any(not isinstance(path, str) or Path(path).is_absolute() or ".." in path.split("/")
                               or not path.endswith(".md") or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                               for path, digest in files.items())):
                    raise ValueError()
                result = {"workspace_kind": "markdown_export", "mapping_status": "absent", "source_workspace": source}
            except (DocumentError, OSError, UnicodeError, ValueError, AttributeError, RecursionError):
                result["export_status"] = "invalid"
        self._workspace_presentation_cache = (stamp, result)
        return dict(result)

    def read(self, workspace_id: str, path: str) -> dict[str, Any]:
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            with self._directory() as root_directory:
                published = self._published_indexes(root_directory)
            parts = _parts(path, published=published)
            if Path(parts[-1]).suffix.casefold() != ".md":
                raise DocumentError("Only Markdown files can be opened.")
            with self._directory(parts[:-1]) as directory:
                return self._document(path, directory)

    def _close_index_run(self) -> None:
        if self._index_run:
            iterator = self._index_run.get("iterator")
            close = getattr(iterator, "close", None)
            if close:
                close()
        self._index_run = None

    def _index_registry_stamp(self):
        try:
            with self._directory(("02_source_memory", "indexes")) as directory:
                return _stamp(directory.stat("typed_links.yml"))
        except FileNotFoundError:
            return None
        except (DocumentError, OSError):
            return ("unavailable",)

    def _index_semantic_stamp(self):
        try:
            with self._directory(_SEMANTIC_MAP_PARTS) as directory:
                info = directory.stat("result.json")
                if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return ("unavailable",)
                return _stamp(info)
        except FileNotFoundError:
            return None
        except (DocumentError, OSError):
            return ("unavailable",)

    def _read_semantic_map(self):
        """Read only the fixed derived result; never follow result-supplied paths."""
        try:
            with self._directory(_SEMANTIC_MAP_PARTS) as directory:
                data, _ = self._read_derived_bytes(directory, "result.json")
            value = json.loads(data)
            if (not isinstance(value, dict) or value.get("schema_version") != _SEMANTIC_MAP_SCHEMA
                    or not isinstance(value.get("identity"), dict)
                    or not isinstance(value["identity"].get("digest"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", value["identity"]["digest"])
                    or any(not isinstance(value.get(key), list) or len(value[key]) > 100000
                           for key in ("sources", "communities", "neighbors"))):
                raise ValueError("Unsupported semantic map")
            return value
        except FileNotFoundError:
            return None
        except (DocumentError, OSError, UnicodeError, ValueError, RecursionError):
            return {"unavailable": True}

    def _read_derived_bytes(self, directory, name, *, max_bytes=None):
        limit = MAX_REGISTRY_BYTES if max_bytes is None else min(max_bytes, MAX_REGISTRY_BYTES)
        before = directory.stat(name)
        if (_linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit):
            raise DocumentError("The derived map is unavailable.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name if directory.fd is not None else directory.path / name, flags, dir_fd=directory.fd)
        try:
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise DocumentIndexChanged()
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            directory.verify()
            if (len(data) > limit or _stamp(os.fstat(fd)) != _stamp(before)
                    or _stamp(directory.stat(name)) != _stamp(before)):
                raise DocumentIndexChanged()
            return bytes(data), before
        finally:
            os.close(fd)

    @staticmethod
    def _semantic_dependency_files(mapping):
        if not mapping or mapping.get("unavailable"):
            return []
        from .profiles import profile_sidecar_path
        result = set()
        for row in mapping["sources"]:
            if (not isinstance(row, dict) or not isinstance(row.get("note_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", row["note_id"])):
                continue
            result.add((("02_source_memory", "profiles"), profile_sidecar_path(Path(), row["note_id"]).name))
            result.add((("11_state", "note_metadata"), f"{row['note_id']}.yml"))
        return sorted(result)

    def _semantic_dependency_stamp(self, parts, name):
        try:
            with self._directory(parts) as directory:
                return _stamp(directory.stat(name))
        except FileNotFoundError:
            return None
        except (DocumentError, OSError):
            return "unavailable"

    @staticmethod
    def _semantic_document(path, text, effective, canonical_revision):
        fields = _frontmatter(text)
        return {"path": path, "source_id": fields.get("source_id"), "note_id": fields.get("note_id"),
                "declared_ids": {key: fields[key] for key in ("source_id", "note_id") if key in fields},
                "revision": effective["revision"],
                "projection_hash": canonical_revision,
                "effective_unchanged": effective["text"] == text}

    def _project_semantic_map(self, mapping, documents):
        """Project suggestions onto owned source notes without promoting evidence links."""
        if mapping is None:
            return [], None, []
        warning = "Some local semantic map results are unavailable or stale. Refresh the local map to update suggestions."
        if mapping.get("unavailable"):
            return [], {"status": "unavailable", "communities": [], "note_revisions": {}}, [warning]
        from .profiles import profile_sidecar_path
        by_path = {row["path"]: row for row in documents}
        source_owners = {}
        for row in documents:
            if (row["path"].startswith("02_source_memory/notes/")
                    and isinstance(row["source_id"], str) and isinstance(row["note_id"], str)):
                source_owners.setdefault(row["source_id"], set()).add(row["note_id"])
        sources, revisions, seen = {}, {}, set()
        dependency_budget = _SEMANTIC_DEPENDENCY_BYTES
        dependency_limited = False

        def read_dependency(parts, name):
            nonlocal dependency_budget, dependency_limited
            try:
                with self._directory(parts) as directory:
                    if directory.stat(name).st_size > dependency_budget:
                        dependency_limited = True
                        raise DocumentError("The local semantic map dependency byte budget was reached.")
                    data, _ = self._read_derived_bytes(directory, name, max_bytes=dependency_budget)
                dependency_budget -= len(data)
                return data
            except FileNotFoundError:
                return b""
        coverage = {"included": 0, "unavailable": 0, "stale": 0, "unresolved": 0}
        digest = mapping["identity"]["digest"]
        invalid = False
        for row in mapping["sources"]:
            if (not isinstance(row, dict) or not isinstance(row.get("source_id"), str)
                    or not 0 < len(row["source_id"]) <= 256 or not isinstance(row.get("note_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", row["note_id"])):
                invalid = True
                continue
            source_id = row["source_id"]
            if source_id in seen:
                return [], {"status": "unavailable", "communities": [], "note_revisions": {}}, [warning]
            seen.add(source_id)
            if not isinstance(row.get("coverage"), dict) or row["coverage"].get("status") != "included":
                coverage["unavailable"] += 1
                continue
            path = row.get("note_path")
            try:
                parts = _parts(path)
                if Path(parts[-1]).suffix.casefold() != ".md":
                    raise DocumentError("Invalid source note path")
            except DocumentError:
                coverage["unresolved"] += 1
                continue
            document = by_path.get(path)
            if (document is None or any(key in document["declared_ids"] and document["declared_ids"][key] != expected
                                        for key, expected in (("source_id", source_id), ("note_id", row["note_id"])))
                    or len(source_owners.get(source_id, ())) > 1):
                coverage["unresolved"] += 1
                continue
            if not document["effective_unchanged"] or row.get("projection_hash") != document["projection_hash"]:
                coverage["stale"] += 1
                continue
            profile_name = profile_sidecar_path(Path(), row["note_id"]).name
            try:
                metadata_bytes = read_dependency(("11_state", "note_metadata"), f"{row['note_id']}.yml")
                metadata_hash = hashlib.sha256(metadata_bytes).hexdigest() if metadata_bytes else ""
                if row.get("metadata_file_sha256", "") != metadata_hash:
                    coverage["stale"] += 1
                    continue
                if any(key not in document["declared_ids"] for key in ("source_id", "note_id")):
                    from .notes import NOTE_METADATA_SCHEMA_VERSION
                    metadata = yaml.load(metadata_bytes, Loader=_YAML_LOADER) if metadata_bytes else {}
                    stored = metadata.get("frontmatter") if isinstance(metadata, dict) else None
                    if (not isinstance(metadata, dict) or metadata.get("metadata_schema_version") != NOTE_METADATA_SCHEMA_VERSION
                            or not isinstance(stored, dict) or stored.get("source_id") != source_id or stored.get("note_id") != row["note_id"]
                            or metadata.get("note_path") != path):
                        coverage["unresolved"] += 1
                        continue
                profile_bytes = read_dependency(("02_source_memory", "profiles"), profile_name)
                profile_hash = hashlib.sha256(profile_bytes).hexdigest() if profile_bytes else ""
            except (DocumentError, OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
                coverage["stale"] += 1
                continue
            expected_profile_hash = row.get("profile_file_sha256", "")
            if (expected_profile_hash != profile_hash
                    or row.get("profile_hash") and "profile_file_sha256" not in row):
                coverage["stale"] += 1
                continue
            chunks = row.get("chunk_ids", [])
            if (not isinstance(chunks, list) or len(chunks) > 100000
                    or any(not isinstance(value, str) or not value or len(value) > 256 for value in chunks)):
                invalid = True
                continue
            sources[source_id] = {"path": path, "chunk_ids": set(chunks)}
            revisions[path] = document["revision"]
            coverage["included"] += 1
        communities = []
        community_ids = set()
        memberships = set()
        for row in mapping["communities"]:
            if (not isinstance(row, dict) or not isinstance(row.get("community_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", row["community_id"])
                    or row["community_id"] in community_ids or row.get("algorithm") not in {"leiden", "hdbscan"}
                    or "noise" in row and type(row["noise"]) is not bool
                    or not isinstance(row.get("source_ids"), list)
                    or any(not isinstance(value, str) for value in row["source_ids"])):
                invalid = True
                continue
            community_ids.add(row["community_id"])
            membership = {(row["algorithm"], value) for value in row["source_ids"] if value in sources}
            if membership & memberships:
                invalid = True
                continue
            memberships.update(membership)
            paths = sorted({sources[value]["path"] for value in row["source_ids"] if value in sources})
            if paths:
                communities.append({"community_id": row["community_id"], "algorithm": row["algorithm"], "paths": paths,
                                    **({"noise": row["noise"]} if "noise" in row else {})})
                # A title describes the recorded membership, so do not carry it
                # onto a partial community after one of its sources changes.
                label = row.get("display_label")
                if (len(paths) == len(set(row["source_ids"])) and isinstance(label, str)
                        and 0 < len(label.strip()) <= 160 and not any(ord(char) < 32 for char in label)):
                    communities[-1]["display_label"] = label.strip()
                if row.get("scale") == "default":
                    communities[-1]["scale"] = "default"
        community_scales = []
        scales = mapping.get("community_scales", [])
        if not isinstance(scales, list) or len(scales) > 3:
            invalid = True
            scales = []
        seen_scales = set()
        for scale in scales:
            if (not isinstance(scale, dict) or scale.get("scale") not in {"broad", "default", "fine"}
                    or scale["scale"] in seen_scales
                    or isinstance(scale.get("resolution"), bool)
                    or not isinstance(scale.get("resolution"), (int, float))
                    or not math.isfinite(scale["resolution"]) or scale["resolution"] <= 0
                    or not isinstance(scale.get("communities"), list) or len(scale["communities"]) > 100000):
                invalid = True
                continue
            seen_scales.add(scale["scale"])
            projected, scale_ids, scale_members = [], set(), set()
            for row in scale["communities"]:
                if (not isinstance(row, dict) or not isinstance(row.get("community_id"), str)
                        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", row["community_id"])
                        or row["community_id"] in scale_ids or row.get("algorithm") not in {"leiden", "hdbscan"}
                        or "noise" in row and type(row["noise"]) is not bool
                        or not isinstance(row.get("source_ids"), list)
                        or any(not isinstance(value, str) for value in row["source_ids"])):
                    invalid = True
                    continue
                members = set(row["source_ids"])
                if members & scale_members:
                    invalid = True
                    continue
                scale_ids.add(row["community_id"])
                scale_members.update(members)
                paths = sorted({sources[sid]["path"] for sid in members if sid in sources})
                if not paths:
                    continue
                item = {"community_id": row["community_id"], "algorithm": row["algorithm"],
                        "paths": paths, "scale": scale["scale"],
                        **({"noise": row["noise"]} if "noise" in row else {})}
                label = row.get("display_label")
                if (len(paths) == len(members) and isinstance(label, str) and 0 < len(label.strip()) <= 160
                        and not any(ord(char) < 32 for char in label)):
                    item["display_label"] = label.strip()
                projected.append(item)
            community_scales.append({"scale": scale["scale"], "resolution": scale["resolution"],
                                     "communities": sorted(projected, key=lambda row: row["community_id"])})
        relations, pairs = [], {}
        for row in mapping["neighbors"]:
            if not isinstance(row, dict):
                invalid = True
                continue
            source, target, score = row.get("source_id"), row.get("target_source_id"), row.get("score")
            if not isinstance(source, str) or not isinstance(target, str):
                invalid = True
                continue
            if source not in sources or target not in sources or source == target:
                continue
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1:
                invalid = True
                continue
            pair = tuple(sorted((source, target)))
            provenance = row.get("provenance", {})
            safe_provenance = {"kind": "local_embedding_similarity", "embedding_identity": digest,
                               "evidence_backed": False, "accepted": False}
            if not isinstance(provenance, dict):
                invalid = True
                continue
            if "discovery_scopes" in provenance:
                scopes = provenance["discovery_scopes"]
                if (not isinstance(scopes, list) or not 1 <= len(scopes) <= 2
                        or any(not isinstance(value, str) or value not in {"within_collection", "whole_library"} for value in scopes)
                        or len(set(scopes)) != len(scopes)):
                    invalid = True
                    continue
                safe_provenance["discovery_scopes"] = sorted(scopes)
            if "shared_collection_keys" in provenance:
                keys = provenance["shared_collection_keys"]
                if (not isinstance(keys, list) or len(keys) > 64
                        or any(not isinstance(value, str) or not 0 < len(value) <= 256
                               or any(ord(char) < 32 for char in value) for value in keys)):
                    invalid = True
                    continue
                safe_provenance["shared_collection_keys"] = sorted(set(keys))
            for key, owner in (("source_chunk_ids", source), ("target_chunk_ids", target)):
                chunks = provenance.get(key, [])
                if (not isinstance(chunks, list) or any(not isinstance(value, str) or value not in sources[owner]["chunk_ids"] for value in chunks)):
                    invalid = True
                    break
                safe_provenance[key] = list(dict.fromkeys(chunks))
            else:
                if pair in pairs:
                    previous = pairs[pair]["provenance"]
                    merged_metadata = {}
                    for key in ("discovery_scopes", "shared_collection_keys"):
                        if key in safe_provenance:
                            merged = sorted(set(previous.get(key, [])) | set(safe_provenance[key]))
                            if len(merged) > (2 if key == "discovery_scopes" else 64):
                                invalid = True
                                break
                            merged_metadata[key] = merged
                    else:
                        previous.update(merged_metadata)
                    continue
                relation_id = hashlib.sha256(json.dumps([digest, *pair]).encode()).hexdigest()[:24]
                relations.append({"id": f"semantic:{relation_id}", "source": sources[source]["path"],
                                  "target": sources[target]["path"], "type": "semantic_similarity", "derived": True,
                                  "decision_status": "suggested", "accepted": False, "evidence_backed": False,
                                  "score": float(score), "provenance": safe_provenance})
                pairs[pair] = relations[-1]
        partial = invalid or any(coverage[key] for key in ("unavailable", "stale", "unresolved"))
        metadata = {"status": "partial" if partial else "current", "identity_digest": digest,
                    "communities": sorted(communities, key=lambda row: row["community_id"]),
                    "community_scales": community_scales,
                    "note_revisions": revisions, "coverage": {**coverage,
                        "dependency_bytes_read": _SEMANTIC_DEPENDENCY_BYTES - dependency_budget,
                        "dependency_byte_limit": _SEMANTIC_DEPENDENCY_BYTES,
                        "dependency_budget_reached": dependency_limited}}
        warnings = [warning] if partial else []
        if dependency_limited:
            warnings.append("Local semantic map projection is partial because its protected metadata/profile byte budget was reached.")
        return relations, metadata, warnings

    def _index_root_stamp(self):
        with self._directory() as directory:
            markers = []
            for anchor in directory.anchors:
                for marker in _ENGINE_MARKERS:
                    try:
                        info = (os.stat(marker, dir_fd=anchor.fd, follow_symlinks=False)
                                if anchor.fd is not None else (anchor.path / marker).lstat())
                        markers.append(_stamp(info))
                    except FileNotFoundError:
                        markers.append(None)
            stamp = (self._root_identity, directory.engine_owned(), self._published_indexes(directory),
                     self._reason(directory), self._index_registry_stamp(), tuple(markers), self._index_semantic_stamp())
            return (*stamp, self._override_generation()) if self._overrides is not None else stamp

    def _index_inventory(self, run, inventory, expected=None):
        pending = deque([()])

        def record(path, value):
            if expected is not None and expected.get(path) != value:
                raise DocumentIndexChanged()
            inventory[path] = value
            if len(inventory) > MAX_INDEX_PATHS:
                raise _IndexResourceLimit("The workspace exceeds the catalogue path resource limit.")

        while pending:
            parts = pending.popleft()
            with self._directory(parts) as directory:
                before = directory.anchors[-1].info
                reason = self._reason(directory)
                record("/".join(parts), ("directory", _stamp(before), reason))
                with os.scandir(directory.fd if directory.fd is not None else directory.path) as scan:
                    for entry in scan:
                        run["progress"]["scanned"] += 1
                        try:
                            child = _parts("/".join((*parts, entry.name)), published=run["root_stamp"][2])
                        except DocumentError:
                            yield None
                            continue
                        info = directory.stat(entry.name)
                        if _linked(info):
                            yield None
                            continue
                        path = "/".join(child)
                        if stat.S_ISDIR(info.st_mode):
                            if child[:2] == ("02_source_memory", "indexes") and not _published_index_directory(child):
                                yield None
                                continue
                            with self._directory(child) as target:
                                if _stamp(target.anchors[-1].info) != _stamp(info):
                                    raise DocumentIndexChanged()
                                row_reason = self._reason(target)
                            record(path, ("directory", _stamp(info), row_reason))
                            pending.append(child)
                            yield ("directory", path, info, row_reason, directory)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and Path(entry.name).suffix.casefold() == ".md":
                            record(path, ("file", _stamp(info), reason))
                            yield ("file", path, info, reason, directory)
                        else:
                            yield None
                directory.verify()
                after = os.fstat(directory.fd) if directory.fd is not None else directory.path.lstat()
                if _stamp(after) != _stamp(before):
                    raise DocumentIndexChanged()
        for parts, name in self._semantic_dependency_files(run.get("semantic_mapping")):
            path = "/".join((*parts, name))
            record(path, ("semantic_dependency", self._semantic_dependency_stamp(parts, name)))
            yield None
        for name, stamp in sorted(run.get("origin_dependencies", {}).items()):
            if self._semantic_dependency_stamp(_ORIGIN_METADATA_PARTS, name) != stamp:
                raise DocumentIndexChanged()
            record("origin:" + "/".join((*_ORIGIN_METADATA_PARTS, name)), ("origin_dependency", stamp))
            yield None
        if expected is not None and inventory != expected:
            raise DocumentIndexChanged()

    @staticmethod
    def _index_warning(run, text):
        run["truncated"] = True
        if text not in run["warnings"]:
            run["warnings"].append(text)

    @staticmethod
    def _index_identity(mapping, identity, path, priority):
        if not isinstance(identity, str) or not identity or len(identity) > 1024:
            return
        previous = mapping.get(identity)
        if previous is None or priority > previous[0]:
            mapping[identity] = (priority, path)
        elif priority == previous[0] and previous[1] != path:
            mapping[identity] = (priority, None)

    def _index_note_ids(self, run, path, text):
        fields = _frontmatter(text)
        parts = path.split("/")
        self._index_identity(run["note_ids"], fields.get("note_id"), path,
                             2 if parts[:2] == ["02_source_memory", "notes"] else 1)
        cluster_priority = 0
        if len(parts) >= 5 and parts[:2] == ["03_literature_synthesis", "maps"] and parts[3] == "clusters":
            cluster_priority = 2
        elif parts[:2] == ["03_literature_synthesis", "clusters"]:
            cluster_priority = 1
        if cluster_priority:
            self._index_identity(run["cluster_ids"], fields.get("cluster_id"), path, cluster_priority)
        return fields

    def _index_project_relation(self, run, row):
        if (row.get("active", True) is not True or row.get("decision_status", "accepted") != "accepted"
                or row.get("reconciliation_pending") or row.get("refresh_pending")):
            return
        relation_id, relation_type = row.get("relation_id"), row.get("relation_type")
        allowed = TYPED_SOURCE_RELATIONS | SUBSTANTIVE_RELATION_TYPES | {"cluster_member", "has_member"}
        if (not isinstance(relation_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", relation_id)
                or not isinstance(relation_type, str) or relation_type not in allowed):
            self._index_warning(run, "Some typed relations were omitted because their registry records were invalid.")
            return
        if relation_type in SUBSTANTIVE_RELATION_TYPES and row.get("decision_status") != "accepted":
            return
        if run.get("recall_only"):
            endpoints = [run["note_ids"].get(row.get(key)) for key in ("source_note_id", "target_note_id")]
            if any(not entry or not entry[1] or entry[1] not in run["recall_paths"] for entry in endpoints):
                return
            if (relation_type in SUBSTANTIVE_RELATION_TYPES
                    and any(entry[1] in run["recall_overridden_paths"] for entry in endpoints)):
                return
        if row.get("output_contract") == "jev-relationship-policy-v1":
            from .jev_relationships import jev_relationship_is_fresh
            if not jev_relationship_is_fresh(self._root, row, memo=run.setdefault("jev_freshness", {})):
                warning = "Some Jev relationships were omitted because their source evidence changed or is unavailable."
                if warning not in run["warnings"]:
                    run["warnings"].append(warning)
                return

        unresolved = set()

        def resolve(mapping, identity):
            entry = mapping.get(identity) if isinstance(identity, str) else None
            if not entry or not entry[1]:
                unresolved.add("ambiguous" if entry else "missing")
            return entry[1] if entry else None

        if relation_type == "has_member":
            source = resolve(run["cluster_ids"], row.get("source_cluster_id") or (row.get("source_id") if row.get("source_kind") == "cluster" else None))
        else:
            source = resolve(run["note_ids"], row.get("source_note_id"))
        if relation_type == "cluster_member":
            target = resolve(run["cluster_ids"], row.get("target_cluster_id"))
        else:
            target = resolve(run["note_ids"], row.get("target_note_id"))
        if not source or not target:
            run["unresolved"][relation_id] = unresolved
            for status in sorted(unresolved):
                self._index_warning(run, f"Some typed relations have {status} note destinations.")
            return
        value = {"id": relation_id, "source": source, "target": target, "type": relation_type,
                 **_relation_display_metadata(row, run["note_ids"], source, target)}
        previous = run["projected"].get(relation_id)
        if previous is not None and previous != value:
            raise ValueError("Conflicting relation identities")
        run["projected"][relation_id] = value

    def _index_registry(self, run):
        """Parse only the exact internal registry, without constructing its private metadata tree."""
        unavailable = "Typed relations are unavailable. Select the generated workspace root containing a readable supported relation registry."
        try:
            with self._directory(("02_source_memory", "indexes")) as directory:
                before = directory.stat("typed_links.yml")
                if (_linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size > MAX_REGISTRY_BYTES):
                    raise ValueError("Unsupported registry")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                fd = os.open("typed_links.yml" if directory.fd is not None else directory.path / "typed_links.yml", flags, dir_fd=directory.fd)
                try:
                    if _stamp(os.fstat(fd)) != _stamp(before) or _stamp(before) != run["root_stamp"][4]:
                        raise DocumentIndexChanged()
                    data = bytearray()
                    while True:
                        chunk = os.read(fd, min(65536, MAX_REGISTRY_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        run["registry_bytes"] = len(data)
                        if len(data) > MAX_REGISTRY_BYTES:
                            raise ValueError("Unsupported registry")
                        yield None
                    directory.verify()
                    if _stamp(os.fstat(fd)) != _stamp(before) or _stamp(directory.stat("typed_links.yml")) != _stamp(before):
                        raise DocumentIndexChanged()
                finally:
                    os.close(fd)
            stack, schema, links_seen, documents, events = [], None, False, 0, 0
            root_keys, anchors, row_anchors = set(), set(), {}
            fields = {"relation_id", "relation_type", "source_note_id", "target_note_id", "source_cluster_id",
                      "target_cluster_id", "source_id", "target_source_id", "source_kind", "active", "decision_status", "reconciliation_pending", "refresh_pending",
                      "provider", "model", "template_version", "policy_version", "output_contract", "verification_status",
                      "confidence", "top_probability", "probability_margin", "evidence_basis", "decision_schema_version",
                      "prompt_version", "source_template_version", "source_profile_hash", "target_profile_hash"}
            public_structures = {"probabilities", "selected_evidence", "bindings"}
            scalar_loader = _YAML_LOADER("")
            try:
                event_stream = iter(yaml.parse(bytes(data), Loader=_YAML_LOADER))
                for event in event_stream:
                    events += 1
                    if events > 4000000 or len(stack) > 64:
                        raise ValueError("Unsupported registry structure")
                    if getattr(event, "tag", None) not in {None, "tag:yaml.org,2002:map", "tag:yaml.org,2002:seq",
                                                          "tag:yaml.org,2002:str", "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int",
                                                          "tag:yaml.org,2002:float", "tag:yaml.org,2002:null"}:
                        raise ValueError("Unsupported registry tag")
                    anchor = getattr(event, "anchor", None)
                    if anchor and not isinstance(event, yaml.AliasEvent):
                        if anchor in anchors:
                            raise ValueError("Duplicate registry anchor")
                        anchors.add(anchor)
                    if isinstance(event, yaml.AliasEvent):
                        frame = stack[-1] if stack else None
                        if anchor not in anchors or frame is None:
                            raise ValueError("Unknown registry alias")
                        if frame["role"] == "links":
                            if anchor not in row_anchors:
                                raise ValueError("Unsupported relation alias")
                            self._index_project_relation(run, row_anchors[anchor])
                        elif frame["kind"] == "map":
                            key = frame["key"]
                            if key is None or frame["role"] == "root" and key in {"links", "relations", "registry_schema_version"} or frame["role"] in {"row", "candidate"} and key in fields:
                                raise ValueError("Unsupported relation alias")
                            frame["key"] = None
                        # Private aliases are consumed without constructing or expanding their object graph.
                    elif isinstance(event, yaml.DocumentStartEvent):
                        documents += 1
                        if documents > 1:
                            raise ValueError("Multiple registry documents")
                    elif isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                        kind = "map" if isinstance(event, yaml.MappingStartEvent) else "sequence"
                        parent = stack[-1] if stack else None
                        key = parent.get("key") if parent else None
                        if parent and parent["role"] in {"row", "candidate"} and key in public_structures:
                            if key in parent["row"]:
                                raise ValueError("Duplicate relation field")
                            value, consumed = _registry_display_value(event, event_stream, anchors)
                            events += consumed - 1
                            if value is not None:
                                parent["row"][key] = value
                            parent["key"] = None
                            yield None
                            continue
                        role = "skip"
                        if parent is None:
                            if kind != "map":
                                raise ValueError("Invalid registry root")
                            role = "root"
                        elif parent["role"] == "root" and key == "links":
                            if kind != "sequence" or links_seen or anchor:
                                raise ValueError("Invalid registry links")
                            links_seen = True
                            role = "links"
                        elif parent["role"] == "root" and key == "relations":
                            if kind != "sequence" or anchor:
                                raise ValueError("Invalid registry relations")
                            role = "registry_rows"
                        elif parent["role"] == "links":
                            if kind != "map":
                                raise ValueError("Invalid relation record")
                            role = "row"
                        elif parent["role"] == "registry_rows":
                            role = "candidate" if kind == "map" else "skip"
                        elif parent["role"] in {"row", "candidate"} and key in fields:
                            raise ValueError("Invalid relation field")
                        if parent and parent["kind"] == "map":
                            if parent["key"] is None:
                                raise ValueError("Complex mapping key")
                            parent["key"] = None
                        stack.append({"kind": kind, "role": role, "key": None, "row": {}, "anchor": anchor})
                    elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                        frame = stack.pop()
                        if frame["role"] in {"row", "candidate"} and frame["anchor"]:
                            row_anchors[frame["anchor"]] = frame["row"]
                        if frame["role"] == "row":
                            self._index_project_relation(run, frame["row"])
                    elif isinstance(event, yaml.ScalarEvent):
                        if not stack:
                            raise ValueError("Invalid registry root")
                        frame = stack[-1]
                        if frame["kind"] == "map" and frame["key"] is None:
                            if frame["role"] == "root" and event.value in {"registry_schema_version", "links", "relations"}:
                                if event.value in root_keys:
                                    raise ValueError("Duplicate registry field")
                                root_keys.add(event.value)
                            frame["key"] = event.value
                        elif frame["role"] == "root" and frame["key"] == "registry_schema_version" or frame["role"] in {"row", "candidate"} and frame["key"] in fields:
                            tag = event.tag or scalar_loader.resolve(yaml.ScalarNode, event.value, event.implicit)
                            value = scalar_loader.construct_object(yaml.ScalarNode(tag, event.value))
                            scalar_loader.constructed_objects.clear()
                            if frame["role"] == "root":
                                if schema is not None:
                                    raise ValueError("Duplicate registry schema")
                                schema = value
                            else:
                                if frame["key"] in frame["row"]:
                                    raise ValueError("Duplicate relation field")
                                frame["row"][frame["key"]] = value
                            frame["key"] = None
                        elif frame["kind"] == "map":
                            if frame["role"] == "root" and frame["key"] == "links":
                                raise ValueError("Invalid registry links")
                            frame["key"] = None
                        elif frame["role"] == "links":
                            self._index_warning(run, "Some typed relations were omitted because their registry records were invalid.")
                    yield None
            finally:
                scalar_loader.dispose()
            if str(schema) != RELATIONSHIP_REGISTRY_SCHEMA_VERSION or not links_seen:
                raise ValueError("Unsupported registry schema")
        except DocumentIndexChanged:
            raise
        except FileNotFoundError:
            if run["root_stamp"][1]:
                self._index_warning(run, unavailable)
        except (DocumentError, OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
            run["projected"].clear()
            run["unresolved"].clear()
            self._index_warning(run, unavailable)

    def index_page(self, workspace_id: str, cursor=None, known_generation=None) -> dict[str, Any]:
        """A staged read-only catalogue; only the verified final page may be committed."""
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 256):
                raise DocumentError("The index cursor is invalid or expired. Restart indexing.")
            if known_generation is not None and (not isinstance(known_generation, str) or len(known_generation) > 256):
                raise DocumentError("The index generation is invalid.")
            if cursor is None:
                self._close_index_run()
                previous = self._completed_index
                checking = previous if previous and known_generation == previous["generation"] else None
                try:
                    root_stamp = self._index_root_stamp()
                except (DocumentError, OSError):
                    raise DocumentIndexChanged() from None
                run = {"generation": checking["generation"] if checking else secrets.token_urlsafe(24),
                       "root_stamp": root_stamp, "inventory": {}, "note_ids": {}, "cluster_ids": {},
                       "identity_candidates": [], "unresolved": {}, "documents_partial": False,
                       "projected": {}, "warnings": [], "truncated": False, "phase": "check" if checking else "documents",
                       "semantic_mapping": self._read_semantic_map(), "semantic_documents": [], "semantic_map": None,
                       "progress": {"documents": 0, "bytes": 0, "scanned": 0, "relations": 0},
                       "checking": checking, "pending": None, "iterator": None, "last_cursor": None,
                       "last_page": None, "cursor": None, "expires": time.monotonic() + _INDEX_TTL}
                run["origin_dependencies"] = dict(checking.get("origin_dependencies", {})) if checking else {}
                run["origin_budget"] = [_ORIGIN_METADATA_BYTES]
                self._index_run = run
                if checking and run["root_stamp"] != checking["root_stamp"]:
                    self._close_index_run()
                    raise DocumentIndexChanged()
                run["iterator"] = self._index_inventory(run, run["inventory"], checking["inventory"] if checking else None)
            else:
                run = self._index_run
                if not run or time.monotonic() > run["expires"]:
                    self._close_index_run()
                    raise DocumentError("The index cursor is invalid or expired. Restart indexing.")
                if cursor == run["last_cursor"]:
                    if ((self._overrides is not None and self._index_root_stamp() != run["root_stamp"])
                            or any(self._semantic_dependency_stamp(_ORIGIN_METADATA_PARTS, name) != stamp
                                   for name, stamp in run["origin_dependencies"].items())):
                        self._close_index_run()
                        raise DocumentIndexChanged()
                    return copy.deepcopy(run["last_page"])
                if cursor != run["cursor"]:
                    raise DocumentError("The index cursor is invalid or expired. Restart indexing.")
            documents, entries, relations = [], [], []
            page_bytes = 0
            registry_bytes = run.get("registry_bytes", 0)
            try:
                if self._index_root_stamp() != run["root_stamp"]:
                    raise DocumentIndexChanged()
                for _ in range(INDEX_PAGE_OPERATIONS):
                    try:
                        item = run["pending"] if run["pending"] is not None else next(run["iterator"])
                        run["pending"] = None
                    except StopIteration:
                        if run["phase"] in {"check", "verify"}:
                            if self._index_root_stamp() != run["root_stamp"]:
                                raise DocumentIndexChanged()
                            run["phase"] = "complete"
                            break
                        if run["phase"] == "documents":
                            run["phase"] = "registry"
                            run["iterator"] = self._index_registry(run)
                        elif run["phase"] == "registry":
                            derived, run["semantic_map"], derived_warnings = self._project_semantic_map(
                                run["semantic_mapping"], run["semantic_documents"])
                            for relation in derived:
                                if relation["id"] not in run["projected"]:
                                    run["projected"][relation["id"]] = relation
                            run["warnings"].extend(warning for warning in derived_warnings if warning not in run["warnings"])
                            run["phase"] = "relations"
                            run["iterator"] = iter(sorted(run["projected"].values(), key=lambda row: row["id"]))
                        else:
                            run["phase"] = "verify"
                            run["iterator"] = self._index_inventory(run, {}, run["inventory"])
                        continue
                    if item is None or run["phase"] in {"check", "verify", "registry"}:
                        if run.get("registry_bytes", 0) - registry_bytes >= INDEX_PAGE_BYTES:
                            break
                        continue
                    if run["phase"] == "relations":
                        relations.append(item)
                        run["progress"]["relations"] += 1
                        if len(relations) >= 500:
                            break
                        continue
                    kind, path, info, reason, directory = item
                    row = {"path": path, "name": Path(path).name,
                           "kind": kind, "editable": not reason, "reason": reason}
                    if kind == "file":
                        if info.st_size <= MAX_FILE_BYTES and page_bytes and page_bytes + info.st_size > INDEX_PAGE_BYTES:
                            run["pending"] = item
                            break
                        try:
                            if info.st_size > MAX_FILE_BYTES:
                                raise DocumentError("The note exceeds the 1 MiB document limit.")
                            data, current = self._read_bytes(directory, Path(path).name)
                            if _stamp(current) != _stamp(info):
                                raise DocumentIndexChanged()
                            text, _, newline = _decode(data)
                            reason = self._reason(directory, text, newline)
                            if not current.st_mode & 0o222 and not reason:
                                reason = "The note is read-only."
                            row.update(editable=not reason, reason=reason)
                            document = {"path": path, "text": text, "revision": hashlib.sha256(data).hexdigest(), "editable": not reason}
                            if self._overrides is not None:
                                document = self._override_document({**document, "reason": reason, "newline": newline})
                            row.update({key: document[key] for key in ("display_title", "personal_display_title") if key in document})
                            effective_bytes = len(document["text"].encode("utf-8")) if self._overrides is not None else len(data)
                            if page_bytes and page_bytes + effective_bytes > INDEX_PAGE_BYTES:
                                run["pending"] = item
                                break
                            fields = self._index_note_ids(run, path, text)
                            document["note_origin"] = self._note_origin(
                                path, fields, dependencies=run["origin_dependencies"], budget=run["origin_budget"])
                            if run["semantic_mapping"] and not run["semantic_mapping"].get("unavailable"):
                                run["semantic_documents"].append(self._semantic_document(
                                    path, text, document, hashlib.sha256(data).hexdigest()))
                            document.update(_document_metadata(path, fields))
                            identity = _generated_identity(path, fields, hashlib.sha256(data).hexdigest(), document["text"])
                            if identity and run["root_stamp"][2]:
                                run["identity_candidates"].append(identity)
                            documents.append(document)
                            page_bytes += effective_bytes
                            run["progress"]["documents"] += 1
                            run["progress"]["bytes"] += effective_bytes
                        except DocumentIndexChanged:
                            raise
                        except (DocumentError, OSError):
                            directory.verify()
                            if _stamp(directory.stat(Path(path).name)) != _stamp(info):
                                raise DocumentIndexChanged()
                            row.update(editable=False, reason="This note could not be indexed. Open it to review its format or size.")
                            run["documents_partial"] = True
                            self._index_warning(run, "Some notes could not be indexed because they were unreadable or exceeded the document format or size limits.")
                    entries.append(row)
                    if len(documents) >= INDEX_PAGE_DOCUMENTS or page_bytes >= INDEX_PAGE_BYTES:
                        break
                complete = run["phase"] == "complete"
                run["cursor"] = None if complete else secrets.token_urlsafe(24)
                result = {"generation": run["generation"], "next_cursor": run["cursor"], "complete": complete,
                          "documents": documents, "entries": entries, "relations": relations,
                          "warnings": list(run["warnings"]), "truncated": run["truncated"], "progress": dict(run["progress"])}
                result["index_coverage"] = _index_coverage(run["documents_partial"], complete=complete)
                if complete:
                    if run["semantic_map"] is not None:
                        result["semantic_map"] = run["semantic_map"]
                    result["document_identities"] = [] if run["documents_partial"] else _document_identities(run["identity_candidates"])
                    result["relation_resolution"] = {status: sum(status in values for values in run["unresolved"].values())
                                                     for status in ("missing", "ambiguous")}
                    if run["checking"]:
                        result["not_modified"] = True
                        result["progress"] = dict(run["checking"]["progress"])
                        for key in ("document_identities", "relation_resolution", "index_coverage"):
                            result[key] = copy.deepcopy(run["checking"][key])
                        if "semantic_map" in run["checking"]:
                            result["semantic_map"] = copy.deepcopy(run["checking"]["semantic_map"])
                    elif not run["truncated"]:
                        self._completed_index = {key: run[key] for key in ("generation", "root_stamp", "inventory", "progress", "origin_dependencies")}
                        self._completed_index.update({key: result[key] for key in ("document_identities", "relation_resolution", "index_coverage")})
                        if "semantic_map" in result:
                            self._completed_index["semantic_map"] = copy.deepcopy(result["semantic_map"])
                    run["iterator"] = None
                run["last_cursor"], run["last_page"] = cursor, copy.deepcopy(result)
                return result
            except _IndexResourceLimit:
                self._close_index_run()
                raise
            except (DocumentError, OSError):
                self._close_index_run()
                raise DocumentIndexChanged() from None

    def index(self, workspace_id: str) -> dict[str, Any]:
        """Bounded read-only snapshot for links and graphs; never opens save handles."""
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            documents: list[dict[str, Any]] = []
            canonical_documents = []
            semantic_documents = []
            semantic_stamp = self._index_semantic_stamp()
            semantic_mapping = self._read_semantic_map()
            semantic_dependencies = {(parts, name): self._semantic_dependency_stamp(parts, name)
                                     for parts, name in self._semantic_dependency_files(semantic_mapping)}
            identity_candidates = []
            origin_dependencies = {}
            origin_budget = [_ORIGIN_METADATA_BYTES]
            override_generation = self._override_generation()
            pending = deque([()])
            scanned = entries = inspected_bytes = 0
            truncated = skipped = False
            with self._directory() as directory:
                engine_owned = directory.engine_owned()
                published = self._published_indexes(directory)
            while pending and not truncated:
                parts = pending.popleft()
                try:
                    with self._directory(parts) as directory:
                        children = []
                        with os.scandir(directory.fd if directory.fd is not None else directory.path) as scan:
                            for entry in scan:
                                scanned += 1
                                if scanned > MAX_SCANNED_ENTRIES:
                                    truncated = True
                                    break
                                children.append(entry.name)
                        for name in sorted(children, key=str.casefold):
                            try:
                                child = _parts("/".join((*parts, name)), published=published)
                            except DocumentError:
                                continue
                            try:
                                info = directory.stat(name)
                                if _linked(info):
                                    continue
                                if stat.S_ISDIR(info.st_mode):
                                    if child[:2] == ("02_source_memory", "indexes") and not _published_index_directory(child):
                                        continue
                                    pending.append(child)
                                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and Path(name).suffix.casefold() == ".md":
                                    if info.st_size > MAX_FILE_BYTES:
                                        skipped = True
                                        continue
                                    if inspected_bytes + info.st_size > MAX_TREE_BYTES:
                                        truncated = True
                                        break
                                    data, info = self._read_bytes(directory, name)
                                    inspected_bytes += len(data)
                                    if inspected_bytes > MAX_TREE_BYTES:
                                        truncated = True
                                        break
                                    text, _, newline = _decode(data)
                                    reason = self._reason(directory, text, newline)
                                    document = {
                                        "path": "/".join(child), "text": text,
                                        "revision": hashlib.sha256(data).hexdigest(),
                                        "editable": not reason and bool(info.st_mode & 0o222),
                                    }
                                    if self._overrides is not None:
                                        effective = self._override_document({**document, "reason": reason, "newline": newline})
                                        inspected_bytes += len(effective["text"].encode("utf-8")) - len(data)
                                        if inspected_bytes > MAX_TREE_BYTES:
                                            truncated = True
                                            break
                                    else:
                                        effective = document
                                    fields = _frontmatter(text)
                                    effective["note_origin"] = self._note_origin(
                                        document["path"], fields, dependencies=origin_dependencies, budget=origin_budget)
                                    effective.update(_document_metadata(document["path"], fields))
                                    identity = _generated_identity(document["path"], fields, document["revision"], effective["text"])
                                    if identity and published:
                                        identity_candidates.append(identity)
                                    canonical_documents.append(document)
                                    if semantic_mapping and not semantic_mapping.get("unavailable"):
                                        semantic_documents.append(self._semantic_document(
                                            document["path"], text, effective, document["revision"]))
                                    documents.append(effective)
                                else:
                                    continue
                                entries += 1
                                if entries >= MAX_ENTRIES:
                                    truncated = True
                                    break
                            except (DocumentError, OSError):
                                skipped = True
                        directory.verify()
                except (DocumentError, OSError):
                    skipped = True
            # A root replacement invalidates the whole snapshot rather than returning
            # a mixture of two workspaces. Child failures produce a partial index.
            with self._directory():
                pass
            if self._override_generation() != override_generation:
                raise DocumentIndexChanged()
            warnings = []
            if truncated:
                warnings.append("Workspace index is partial because its scan or size limit was reached.")
            if skipped:
                warnings.append("Some notes could not be indexed because they changed, were unreadable, or exceeded the document limit.")
            documents.sort(key=lambda row: row["path"].casefold())
            relations, relation_warnings, resolution = self._typed_relations(canonical_documents, engine_owned)
            warnings.extend(relation_warnings)
            derived, semantic_map, derived_warnings = self._project_semantic_map(semantic_mapping, semantic_documents)
            if (semantic_stamp != self._index_semantic_stamp()
                    or any(self._semantic_dependency_stamp(*file) != stamp for file, stamp in semantic_dependencies.items())
                    or any(self._semantic_dependency_stamp(_ORIGIN_METADATA_PARTS, name) != stamp
                           for name, stamp in origin_dependencies.items())):
                raise DocumentIndexChanged()
            relations.extend(derived)
            warnings.extend(derived_warnings)
            return {"documents": documents, "relations": relations, "truncated": truncated, "warnings": warnings,
                    **({"semantic_map": semantic_map} if semantic_map is not None else {}),
                    "document_identities": [] if truncated or skipped else _document_identities(identity_candidates),
                    "index_coverage": _index_coverage(truncated or skipped), "relation_resolution": resolution}

    def _typed_relations(self, documents: list[dict[str, Any]], engine_owned: bool):
        """Project only the published registry inside the explicitly selected root."""
        unavailable = "Typed relations are unavailable. Select the generated workspace root containing a readable supported relation registry."
        try:
            # Project this exact internal registry without exposing its YAML.
            # Never search ancestors or expose its research metadata to the client.
            with self._directory(("02_source_memory", "indexes")) as directory:
                data, _ = self._read_bytes(directory, "typed_links.yml")
            registry = yaml.safe_load(data.decode("utf-8-sig"))
            if (not isinstance(registry, dict)
                    or str(registry.get("registry_schema_version")) != RELATIONSHIP_REGISTRY_SCHEMA_VERSION
                    or not isinstance(registry.get("links"), list)):
                return [], [unavailable], {"missing": 0, "ambiguous": 0}
        except FileNotFoundError:
            return [], [unavailable] if engine_owned else [], {"missing": 0, "ambiguous": 0}
        except (DocumentError, OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
            return [], [unavailable], {"missing": 0, "ambiguous": 0}
        run = {"note_ids": {}, "cluster_ids": {}, "projected": {}, "unresolved": {}, "warnings": [], "truncated": False}
        for document in documents:
            self._index_note_ids(run, document["path"], document["text"])
        for row in registry["links"]:
            if not isinstance(row, dict):
                self._index_warning(run, "Some typed relations were omitted because their registry records were invalid.")
                continue
            try:
                self._index_project_relation(run, row)
            except ValueError:
                return [], ["Typed relations are unavailable because the registry contains conflicting relation identities."], {"missing": 0, "ambiguous": 0}
        resolution = {status: sum(status in values for values in run["unresolved"].values()) for status in ("missing", "ambiguous")}
        return [run["projected"][key] for key in sorted(run["projected"])], run["warnings"], resolution

    def _writable(self, directory: _Directory) -> None:
        directory.verify()
        reason = self._reason(directory)
        if reason:
            raise DocumentError(reason)
        assert directory.fd is not None
        os.fsync(directory.fd)

    def prepare_relocation(self, workspace_id: str, source: str, destination: str) -> dict[str, Any]:
        with _LOCK, self._errors(), self._relocations().guard(workspace_id):
            self._workspace(workspace_id)
            return self._relocations().prepare(source, destination)

    def apply_relocation(self, workspace_id: str, token: str, repair: bool, patches: list) -> dict[str, Any]:
        with _LOCK, self._errors(), self._relocations().guard(workspace_id):
            self._workspace(workspace_id)
            return self._relocations().apply(token, repair, patches)

    def save(self, workspace_id: str, document_id: str, revision: str, text: str) -> dict[str, Any]:
        with _LOCK, self._errors(), self._relocations().guard(workspace_id):
            self._workspace(workspace_id)
            self._relocations().ensure_ready()
            if not isinstance(document_id, str) or document_id not in self._documents:
                raise DocumentError("Open the document before saving it.")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise DocumentError("A document revision is required.")
            if not isinstance(text, str) or len(text) > MAX_FILE_BYTES or "\x00" in text:
                raise DocumentError("Save requires bounded Markdown text without NUL characters.")
            if _newline(text) == "mixed":
                raise DocumentError("Mixed or bare-CR line endings cannot be saved.")
            relative, opened_identity = self._documents[document_id]
            parts = _parts(relative)
            with self._directory(parts[:-1]) as directory:
                current = self._document(relative, directory)
                if current["revision"] != revision or self._documents[document_id][1] != opened_identity:
                    raise DocumentConflict(current)
                if not current["editable"]:
                    raise DocumentError(current["reason"])
                normalized = text.replace("\r\n", "\n")
                if current["newline"] == "crlf":
                    normalized = normalized.replace("\n", "\r\n")
                try:
                    data = (b"\xef\xbb\xbf" if current["bom"] else b"") + normalized.encode("utf-8")
                except UnicodeError as exc:
                    raise DocumentError("The text cannot be encoded as UTF-8.") from exc
                if len(data) > MAX_FILE_BYTES:
                    raise DocumentError("The note exceeds the 1 MiB document limit.")
                self._writable(directory)
                if hashlib.sha256(data).hexdigest() == revision:
                    return current
                temporary = f".az-{secrets.token_hex(16)}.tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
                try:
                    temporary_identity = _identity(os.fstat(fd))
                    offset = 0
                    while offset < len(data):
                        written = os.write(fd, data[offset:])
                        if written <= 0:
                            raise DocumentError("The filesystem could not finish writing the note.")
                        offset += written
                    os.fchmod(fd, stat.S_IMODE(directory.stat(parts[-1]).st_mode) & 0o777)
                    os.fsync(fd)
                    latest = self._document(relative, directory)
                    if latest["revision"] != revision or self._documents[document_id][1] != opened_identity:
                        raise DocumentConflict(latest)
                    self._writable(directory)
                    if _stamp(directory.stat(temporary)) != _stamp(os.fstat(fd)):
                        raise DocumentError("The save temporary file changed.")
                    os.replace(temporary, parts[-1], src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
                    os.fsync(directory.fd)
                    saved = self._document(relative, directory)
                    if saved["revision"] != hashlib.sha256(data).hexdigest():
                        raise DocumentConflict(saved)
                    return saved
                finally:
                    os.close(fd)
                    try:
                        if _identity(directory.stat(temporary)) == temporary_identity:
                            os.unlink(temporary, dir_fd=directory.fd)
                    except FileNotFoundError:
                        pass

    def save_override(self, workspace_id: str, document_id: str, revision: str, text: str) -> dict[str, Any]:
        with _LOCK, self._errors():
            self._workspace(workspace_id)
            if not isinstance(document_id, str) or document_id not in self._documents:
                raise DocumentError("Open the document before saving an override.")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise DocumentError("A document revision is required.")
            if not isinstance(text, str) or len(text) > MAX_FILE_BYTES or "\x00" in text or _newline(text) == "mixed":
                raise DocumentError("Override saves require bounded UTF-8 Markdown with consistent line endings.")
            try:
                if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                    raise DocumentError("The note exceeds the 1 MiB document limit.")
            except UnicodeError as exc:
                raise DocumentError("The text cannot be encoded as UTF-8.") from exc
            if not self._override_location():
                raise DocumentError("Private override storage is unavailable or inside the workspace.")
            relative = self._documents[document_id][0]
            with self._directory() as root_directory:
                published = self._published_indexes(root_directory)
            parts = _parts(relative, published=published)
            with self._directory(parts[:-1]) as directory, self._overrides.guard(self._root, self._root_identity, create=True) as view:
                canonical = self._document(relative, directory, effective=False)
                current = self._override_document(canonical, view, guarded=True)
                if current["revision"] != revision:
                    raise DocumentConflict(current)
                if not current["override_available"]:
                    raise DocumentError("This note does not support a private body override.")
                original = self._override_parts(canonical["text"])
                proposed = self._override_parts(text)
                if proposed is None or proposed[0] != original[0]:
                    raise DocumentError("Generated properties and identity fields cannot be changed. Edit only the Markdown body.")
                record, _, _ = view.read(relative)
                if record and record.get("body") == proposed[1] and record["source_revision"] == canonical["revision"]:
                    return current

                def verify():
                    latest = self._override_document(self._document(relative, directory, effective=False), view, guarded=True)
                    if latest["revision"] != revision:
                        raise DocumentConflict(latest)
                    if not latest["override_available"]:
                        raise DocumentError("This note no longer supports a private body override.")

                view.write(relative, proposed[1], canonical["revision"], verify, metadata=record)
                return self._override_document(self._document(relative, directory, effective=False), view, guarded=True)

    def save_personal_metadata(self, workspace_id: str, document_id: str, revision: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Update only private display metadata, never the generated Markdown."""
        from .document_overrides import personal_metadata

        with _LOCK, self._errors():
            self._workspace(workspace_id)
            if not isinstance(document_id, str) or document_id not in self._documents:
                raise DocumentError("Open the document before saving personal metadata.")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise DocumentError("A document revision is required.")
            if (not isinstance(changes, dict) or not changes or set(changes) - {"display_title", "personal_properties"}):
                raise DocumentError("Unsupported personal metadata fields.")
            if not self._override_location():
                raise DocumentError("Private override storage is unavailable or inside the workspace.")
            relative = self._documents[document_id][0]
            with self._directory() as root_directory:
                published = self._published_indexes(root_directory)
            parts = _parts(relative, published=published)
            with self._directory(parts[:-1]) as directory, self._overrides.guard(self._root, self._root_identity, create=True) as view:
                canonical = self._document(relative, directory, effective=False)
                current = self._override_document(canonical, view, guarded=True)
                if current["revision"] != revision:
                    raise DocumentConflict(current)
                if not current["override_available"]:
                    raise DocumentError("This note does not support private personal metadata.")
                record, _, _ = view.read(relative)
                record = record or {}
                if "personal_properties" in changes and not isinstance(changes["personal_properties"], dict):
                    raise DocumentError("Personal properties require a named-value map.")
                title, properties = personal_metadata(changes.get("display_title", record.get("display_title")),
                                                       changes.get("personal_properties", record.get("personal_properties", {})))
                fields = _frontmatter(canonical["text"])
                old = record.get("personal_properties", {})
                if any(key in fields and key not in {"tags", "aliases"} and (key not in old or value != old[key])
                       for key, value in properties.items()):
                    raise DocumentError("Personal properties cannot replace generated properties.")
                if title == record.get("display_title") and properties == record.get("personal_properties", {}):
                    return current

                def verify():
                    latest = self._override_document(self._document(relative, directory, effective=False), view, guarded=True)
                    if latest["revision"] != revision:
                        raise DocumentConflict(latest)
                    if not latest["override_available"]:
                        raise DocumentError("This note no longer supports private personal metadata.")

                view.write(relative, record.get("body"), record.get("source_revision", canonical["revision"]), verify,
                           metadata={"display_title": title, "personal_properties": properties})
                return self._override_document(self._document(relative, directory, effective=False), view, guarded=True)

    def create(self, workspace_id: str, parent: str, name: str, kind: str) -> dict[str, Any]:
        with _LOCK, self._errors(), self._relocations().guard(workspace_id):
            self._workspace(workspace_id)
            self._relocations().ensure_ready()
            parts = _parts(parent, empty=True)
            if not isinstance(kind, str) or kind not in {"file", "directory"} or not isinstance(name, str):
                raise DocumentError("Choose a Markdown file or folder to create.")
            if kind == "file" and not Path(name).suffix:
                name += ".md"
            if len(_parts(name)) != 1 or len(name.encode("utf-8")) > 255:
                raise DocumentError("Use a single file or folder name.")
            if kind == "file" and Path(name).suffix.casefold() != ".md":
                raise DocumentError("Only Markdown files can be created.")
            with self._directory(parts) as directory:
                self._writable(directory)
                normalized = unicodedata.normalize("NFC", name).casefold()
                with os.scandir(directory.fd) as scan:
                    for count, entry in enumerate(scan, 1):
                        if count > MAX_SCANNED_ENTRIES:
                            raise DocumentError("Too many entries to safely check for name collisions.")
                        if unicodedata.normalize("NFC", entry.name).casefold() == normalized:
                            raise DocumentError("A file or folder with that name already exists.")
                self._writable(directory)
                if kind == "directory":
                    os.mkdir(name, 0o700, dir_fd=directory.fd)
                else:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                created_identity = _identity(directory.stat(name))
                try:
                    self._writable(directory)
                except (DocumentError, OSError):
                    if _identity(directory.stat(name)) == created_identity:
                        if kind == "directory":
                            os.rmdir(name, dir_fd=directory.fd)
                        else:
                            os.unlink(name, dir_fd=directory.fd)
                    raise
                os.fsync(directory.fd)
            result: dict[str, Any] = {"workspace": self.tree(workspace_id)}
            if kind == "file":
                result["document"] = self.read(workspace_id, "/".join((*parts, name)))
            return result
