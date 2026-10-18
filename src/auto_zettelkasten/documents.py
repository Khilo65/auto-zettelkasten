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
        if self._overrides is None:
            return document
        result = {**document, "override_available": False, "override_saved": False,
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
            text = parts[0] + record["body"]
            if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise DocumentError("The regenerated properties and saved override exceed the document limit.")
            result.update(text=text, newline="lf", override_saved=True,
                          source_changed=record["source_revision"] != document["revision"],
                          revision=hashlib.sha256(f"override-v1\0{document['revision']}\0{digest}".encode()).hexdigest())
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
        document = {
            "id": document_id, "path": relative, "name": Path(relative).name,
            "text": text, "revision": hashlib.sha256(data).hexdigest(),
            "editable": not reason, "reason": reason, "bom": bom,
            "newline": newline, "recovery_key": recovery_key,
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
                                except DocumentError as exc:
                                    row_reason = str(exc)
                                if not info.st_mode & 0o222 and not row_reason:
                                    row_reason = "The note is read-only."
                        else:
                            continue
                        entries.append({"path": relative, "name": name, "kind": kind, "editable": not row_reason, "reason": row_reason})
                        if len(entries) >= MAX_ENTRIES:
                            truncated = True
                            break
                    directory.verify()
            assert self._root is not None
            workspace = {
                "id": self._workspace_id, "name": self._root.name,
                "path": str(self._root), "entries": sorted(entries, key=lambda row: row["path"].casefold()),
                "truncated": truncated, "editable": not reason, "reason": reason,
            }
            master = "02_source_memory/indexes/INDEX.md"
            if published and any(row["path"] == master and row["kind"] == "file" and row["reason"] == _ENGINE_REASON for row in entries):
                workspace["preferred_path"] = master
            return workspace

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
                     self._reason(directory), self._index_registry_stamp(), tuple(markers))
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
        text = text.replace("\r\n", "\n")
        if not text.startswith("---\n"):
            return
        match = re.search(r"(?m)^---[ \t]*$", text[4:])
        if match is None or match.start() > 65536:
            return
        try:
            header = text[4:4 + match.start()]
            if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(header, Loader=_YAML_LOADER)):
                return
            fields = yaml.load(header, Loader=_YAML_LOADER)
        except (yaml.YAMLError, RecursionError, ValueError):
            return
        if not isinstance(fields, dict):
            return
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

        def resolve(mapping, identity):
            return mapping.get(identity, (0, None))[1] if isinstance(identity, str) else None

        source = resolve(run["note_ids"], row.get("source_note_id"))
        target = resolve(run["note_ids"], row.get("target_note_id"))
        if relation_type == "has_member":
            source = resolve(run["cluster_ids"], row.get("source_cluster_id") or (row.get("source_id") if row.get("source_kind") == "cluster" else None))
        elif relation_type == "cluster_member":
            target = resolve(run["cluster_ids"], row.get("target_cluster_id"))
        if not source or not target:
            self._index_warning(run, "Some typed relations have unavailable or ambiguous note identities.")
            return
        value = {"id": relation_id, "source": source, "target": target, "type": relation_type}
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
                      "target_cluster_id", "source_id", "source_kind", "active", "decision_status", "reconciliation_pending", "refresh_pending"}
            scalar_loader = _YAML_LOADER("")
            try:
                for event in yaml.parse(bytes(data), Loader=_YAML_LOADER):
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
                       "projected": {}, "warnings": [], "truncated": False, "phase": "check" if checking else "documents",
                       "progress": {"documents": 0, "bytes": 0, "scanned": 0, "relations": 0},
                       "checking": checking, "pending": None, "iterator": None, "last_cursor": None,
                       "last_page": None, "cursor": None, "expires": time.monotonic() + _INDEX_TTL}
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
                    if self._overrides is not None and self._index_root_stamp() != run["root_stamp"]:
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
                            effective_bytes = len(document["text"].encode("utf-8")) if self._overrides is not None else len(data)
                            if page_bytes and page_bytes + effective_bytes > INDEX_PAGE_BYTES:
                                run["pending"] = item
                                break
                            self._index_note_ids(run, path, text)
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
                            self._index_warning(run, "Some notes could not be indexed because they were unreadable or exceeded the document format or size limits.")
                    entries.append(row)
                    if len(documents) >= INDEX_PAGE_DOCUMENTS or page_bytes >= INDEX_PAGE_BYTES:
                        break
                complete = run["phase"] == "complete"
                run["cursor"] = None if complete else secrets.token_urlsafe(24)
                result = {"generation": run["generation"], "next_cursor": run["cursor"], "complete": complete,
                          "documents": documents, "entries": entries, "relations": relations,
                          "warnings": list(run["warnings"]), "truncated": run["truncated"], "progress": dict(run["progress"])}
                if complete:
                    if run["checking"]:
                        result["not_modified"] = True
                        result["progress"] = dict(run["checking"]["progress"])
                    elif not run["truncated"]:
                        self._completed_index = {key: run[key] for key in ("generation", "root_stamp", "inventory", "progress")}
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
                                    canonical_documents.append(document)
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
            relations, relation_warning = self._typed_relations(canonical_documents, engine_owned)
            if relation_warning:
                warnings.append(relation_warning)
            return {"documents": documents, "relations": relations, "truncated": truncated, "warnings": warnings}

    def _typed_relations(self, documents: list[dict[str, Any]], engine_owned: bool) -> tuple[list[dict[str, str]], str]:
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
                return [], unavailable
        except FileNotFoundError:
            return [], unavailable if engine_owned else ""
        except (DocumentError, OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
            return [], unavailable
        paths: dict[str, str | None] = {}
        for document in documents:
            text = document["text"].replace("\r\n", "\n")
            if not text.startswith("---\n"):
                continue
            match = re.search(r"(?m)^---[ \t]*$", text[4:])
            if match is None:
                continue
            try:
                frontmatter = yaml.safe_load(text[4:4 + match.start()])
            except (yaml.YAMLError, RecursionError, ValueError):
                continue
            note_id = frontmatter.get("note_id") if isinstance(frontmatter, dict) else None
            if isinstance(note_id, str) and note_id:
                paths[note_id] = document["path"] if note_id not in paths else None
        allowed_types = TYPED_SOURCE_RELATIONS | SUBSTANTIVE_RELATION_TYPES | {"cluster_member", "has_member"}
        relations: dict[str, dict[str, str]] = {}
        malformed = False
        for row in registry["links"]:
            if not isinstance(row, dict):
                malformed = True
                continue
            if (row.get("active", True) is not True
                    or row.get("decision_status", "accepted") != "accepted"
                    or row.get("reconciliation_pending") or row.get("refresh_pending")):
                continue
            relation_id, relation_type = row.get("relation_id"), row.get("relation_type")
            source_id, target_id = row.get("source_note_id"), row.get("target_note_id")
            if (not isinstance(relation_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", relation_id)
                    or not isinstance(relation_type, str) or relation_type not in allowed_types
                    or not isinstance(source_id, str) or not isinstance(target_id, str)):
                malformed = True
                continue
            if relation_type in SUBSTANTIVE_RELATION_TYPES and row.get("decision_status") != "accepted":
                continue
            source, target = paths.get(source_id), paths.get(target_id)
            if source and target:
                projected = {"id": relation_id, "source": source, "target": target, "type": relation_type}
                if relation_id in relations and relations[relation_id] != projected:
                    return [], "Typed relations are unavailable because the registry contains conflicting relation identities."
                relations[relation_id] = projected
        warning = "Some typed relations were omitted because their registry records were invalid." if malformed else ""
        return [relations[key] for key in sorted(relations)], warning

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
                if record and record["body"] == proposed[1] and record["source_revision"] == canonical["revision"]:
                    return current

                def verify():
                    latest = self._override_document(self._document(relative, directory, effective=False), view, guarded=True)
                    if latest["revision"] != revision:
                        raise DocumentConflict(latest)
                    if not latest["override_available"]:
                        raise DocumentError("This note no longer supports a private body override.")

                view.write(relative, proposed[1], canonical["revision"], verify)
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
