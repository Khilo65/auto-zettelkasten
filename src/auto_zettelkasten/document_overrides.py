"""Private body overrides. Source documents are never opened for writing."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat

from .documents import DocumentError, MAX_FILE_BYTES, _Anchor, _Directory, _identity, _linked, _stamp

try:
    import fcntl
except ImportError:  # Safe writes are disabled on platforms without directory handles.
    fcntl = None

_MAX_RECORD_BYTES = MAX_FILE_BYTES + 65536


def personal_metadata(display_title=None, properties=None):
    """Validate small personal values without allowing YAML objects or identities."""
    if display_title is not None and (not isinstance(display_title, str) or not display_title.strip()
            or len(display_title) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in display_title)):
        raise DocumentError("A personal title must be a single line of 1–512 characters.")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict) or len(properties) > 100:
        raise DocumentError("Personal properties require at most 100 named values.")
    result = {}
    reserved = {"__proto__", "prototype", "constructor", "note_id", "source_id", "source_ids",
                "zotero_item_key", "source_file", "machine_preservation_hash", "inspected_content_hash",
                "reader_provider", "reader_model", "engine_version", "artifact_schema_version", "cluster_id", "gap_id", "map_id"}
    for key, value in properties.items():
        if (not isinstance(key, str) or not key.strip() or key != key.strip() or len(key) > 128
                or key.casefold() in reserved or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise DocumentError("Personal property names must be bounded text and cannot replace source identities.")
        valid = isinstance(value, (str, bool))
        if type(value) in (int, float):
            try:
                valid = math.isfinite(value)
            except OverflowError:
                valid = False
        if isinstance(value, list):
            valid = len(value) <= 200 and all(isinstance(item, str) and len(item) <= 4096 and "\x00" not in item for item in value)
        if not valid or isinstance(value, str) and (len(value) > 4096 or "\x00" in value):
            raise DocumentError("Personal properties support text, string lists, finite numbers and checkboxes.")
        if key in {"tags", "aliases"} and not (isinstance(value, str) or isinstance(value, list)):
            raise DocumentError("Personal tags and aliases require text or a string list.")
        result[key] = value
    try:
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 12000:
            raise DocumentError("Personal properties exceed the 12 KiB limit.")
        if display_title is not None:
            display_title.encode("utf-8")
    except UnicodeError as exc:
        raise DocumentError("Personal metadata must contain valid Unicode.") from exc
    return display_title.strip() if display_title is not None else None, result


class Overrides:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        if not self.root.is_absolute() or ".." in self.root.parts or any(ord(c) < 32 for c in str(self.root)):
            raise DocumentError("Override storage requires an absolute local folder.")

    @contextmanager
    def guard(self, source: Path, identity, *, create=False):
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise DocumentError("Override storage must be outside the selected workspace.")
        key = hashlib.sha256(f"{source}\0{identity}".encode()).hexdigest()
        target = self.root / key
        anchors = []
        current = Path(target.anchor)
        try:
            for part in ("", *target.parts[1:]):
                if part:
                    current /= part
                parent_fd = anchors[-1].fd if anchors else None
                try:
                    fd = os.open(part if parent_fd is not None else current,
                                 os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                                 dir_fd=parent_fd)
                except FileNotFoundError:
                    if anchors and not anchors[-1].info.st_mode & 0o222:
                        raise DocumentError("Override storage is read-only.")
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                info = os.fstat(fd)
                anchors.append(_Anchor(current, info, fd, parent_fd))
                if _linked(info) or not stat.S_ISDIR(info.st_mode):
                    raise DocumentError("Override storage cannot contain links or non-directories.")
            directory = _Directory(anchors)
            directory.verify()
            fcntl.flock(directory.fd, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
            try:
                view = _View(directory)
                view.private()
                yield view
                directory.verify()
                view.private()
            finally:
                fcntl.flock(directory.fd, fcntl.LOCK_UN)
        finally:
            for anchor in reversed(anchors):
                os.close(anchor.fd)

    def generation(self, source: Path, identity):
        with self.guard(source, identity) as view:
            return view.generation() if view else None


class _View:
    def __init__(self, directory):
        self.directory = directory

    @staticmethod
    def name(path):
        return hashlib.sha256(path.encode()).hexdigest() + ".json"

    def private(self):
        info = os.fstat(self.directory.fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise DocumentError("Override storage must be private and owned by the current user (0700).")

    def writable(self):
        self.private()
        self.directory.verify()
        return bool(os.fstat(self.directory.fd).st_mode & 0o222)

    def generation(self):
        self.private()
        entries = []
        with os.scandir(self.directory.fd) as scan:
            for entry in scan:
                if len(entries) >= 10000:
                    raise DocumentError("Override storage exceeds its entry limit.")
                info = self.directory.stat(entry.name)
                if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                    raise DocumentError("Override storage contains an unsafe entry.")
                entries.append((entry.name, _stamp(info)))
        self.directory.verify()
        return (_stamp(os.fstat(self.directory.fd)), tuple(sorted(entries)))

    def read(self, path):
        self.private()
        name = self.name(path)
        try:
            before = self.directory.stat(name)
        except FileNotFoundError:
            return None, None, None
        if (_linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_RECORD_BYTES
                or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
            raise DocumentError("The saved override is not a safe regular file.")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=self.directory.fd)
        try:
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise DocumentError("The saved override changed while opening it.")
            raw = bytearray()
            while len(raw) <= _MAX_RECORD_BYTES:
                chunk = os.read(fd, min(65536, _MAX_RECORD_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_RECORD_BYTES or _stamp(os.fstat(fd)) != _stamp(before) or _stamp(self.directory.stat(name)) != _stamp(before):
                raise DocumentError("The saved override changed while reading it.")
            self.directory.verify()
        finally:
            os.close(fd)
        try:
            record = json.loads(raw)
            if (not isinstance(record, dict) or type(record.get("version")) is not int or record.get("version") not in (1, 2) or record.get("path") != path
                    or record.get("version") == 1 and "body" not in record
                    or "body" in record and (not isinstance(record["body"], str) or "\x00" in record["body"] or "\r" in record["body"]
                                              or len(record["body"].encode("utf-8")) > MAX_FILE_BYTES)
                    or not isinstance(record.get("source_revision"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["source_revision"])
                    ):
                raise ValueError()
            if record["version"] == 2:
                personal_metadata(record.get("display_title"), record.get("personal_properties", {}))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DocumentError("The saved override is invalid. Restore its private backup before editing.") from exc
        return record, hashlib.sha256(raw).hexdigest(), before

    def write(self, path, body, source_revision, verify, *, metadata=None):
        if not self.writable():
            raise DocumentError("Override storage is read-only.")
        record = {"version": 2, "path": path, "source_revision": source_revision,
                  "save_id": secrets.token_hex(16)}
        if body is not None:
            record["body"] = body
        if metadata:
            title, properties = personal_metadata(metadata.get("display_title"), metadata.get("personal_properties", {}))
            if title is not None:
                record["display_title"] = title
            if properties:
                record["personal_properties"] = properties
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > _MAX_RECORD_BYTES:
            raise DocumentError("The override exceeds the document limit.")
        temporary = f".az-override-{secrets.token_hex(16)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.directory.fd)
        identity = _identity(os.fstat(fd))
        try:
            offset = 0
            while offset < len(data):
                count = os.write(fd, data[offset:])
                if count <= 0:
                    raise DocumentError("The filesystem could not finish writing the override.")
                offset += count
            os.fsync(fd)
            verify()
            self.directory.verify()
            if _stamp(self.directory.stat(temporary)) != _stamp(os.fstat(fd)):
                raise DocumentError("The override temporary file changed.")
            os.replace(temporary, self.name(path), src_dir_fd=self.directory.fd, dst_dir_fd=self.directory.fd)
            os.fsync(self.directory.fd)
        finally:
            os.close(fd)
            try:
                if _identity(self.directory.stat(temporary)) == identity:
                    os.unlink(temporary, dir_fd=self.directory.fd)
            except FileNotFoundError:
                pass
