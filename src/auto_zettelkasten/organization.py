"""Reviewed ordinary-note relocation with confined writes and private recovery."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
try:
    import fcntl
except ImportError:  # Organization remains unavailable on platforms without safe POSIX writes.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import unicodedata

from .documents import (
    DocumentError, MAX_ENTRIES, MAX_FILE_BYTES, MAX_SCANNED_ENTRIES, MAX_TREE_BYTES,
    _Anchor, _Directory, _ENGINE_DIRS, _SAFE_WRITES, _decode, _identity, _linked,
    _parts, _stamp,
)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _key(value):
    return unicodedata.normalize("NFC", value).casefold()


def _mapped(path, source, destination):
    return destination + path[len(source):] if path == source or path.startswith(source + "/") else path


def _exclusive_rename(source_fd, source, destination_fd, destination):
    library = ctypes.CDLL(None, use_errno=True)
    name, flags = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(library, name, None)
    if function is None:
        raise DocumentError("Safe exclusive relocation is unavailable on this platform.")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flags):
        raise OSError(ctypes.get_errno(), "Exclusive relocation could not complete")


@contextmanager
def _private_directory(path, *, create=False, forbidden=None):
    """Open every ancestor without following links, including the private leaf."""
    if not path.is_absolute() or ".." in path.parts:
        raise DocumentError("The private recovery directory must be an absolute local path.")
    anchors = []
    current = Path(path.anchor)
    try:
        for part in ("", *path.parts[1:]):
            if part:
                current /= part
            parent = anchors[-1].fd if anchors else None
            if create and part:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            fd = os.open(part if parent is not None else current,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(fd)
            anchors.append(_Anchor(current, info, fd, parent))
            if forbidden is not None and _identity(info) == forbidden:
                raise DocumentError("Choose a workspace outside the app's private recovery storage before organizing notes.")
        directory = _Directory(anchors)
        directory.verify()
        info = anchors[-1].info
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DocumentError("The private recovery directory must be owned by this user with mode 0700.")
        yield directory
        directory.verify()
    finally:
        for anchor in reversed(anchors):
            os.close(anchor.fd)


def _private_read(directory, name, limit=MAX_FILE_BYTES):
    if not re.fullmatch(r"(?:journal\.json|[0-9]+\.before)", name):
        raise DocumentError("The recovery journal contains an invalid backup name.")
    before = directory.stat(name)
    if (not stat.S_ISREG(before.st_mode) or _linked(before) or before.st_nlink != 1
            or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > limit):
        raise DocumentError("The private recovery journal or backup could not be verified.")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory.fd)
    try:
        opened = os.fstat(fd)
        if _stamp(opened) != _stamp(before):
            raise DocumentError("The private recovery journal changed.")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        directory.verify()
        if (len(chunks) > limit or _stamp(os.fstat(fd)) != _stamp(opened)
                or _stamp(directory.stat(name)) != _stamp(opened)):
            raise DocumentError("The private recovery journal changed.")
        return bytes(chunks)
    finally:
        os.close(fd)


def _write_all(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise DocumentError("The filesystem could not finish the relocation backup.")
        offset += written
    os.fsync(fd)


class Relocations:
    def __init__(self, service, journal_root):
        self.service = service
        self.journal_root = journal_root
        self.previews = {}
        self.receipts = {}
        self.blocked = ""
        self._guard_depth = 0

    @contextmanager
    def guard(self, workspace_id=None):
        if workspace_id is not None:
            self.service._workspace(workspace_id)
        if not _SAFE_WRITES or fcntl is None or self._guard_depth:
            self._guard_depth += 1
            try:
                yield
            finally:
                self._guard_depth -= 1
            return
        with _private_directory(self._store(), create=True, forbidden=self.service._root_identity) as store:
            fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=store.fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077 or _identity(store.stat(".lock")) != _identity(info)):
                    raise DocumentError("The organization lock could not be verified.")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise DocumentError("Another app is changing this workspace. Wait for it to finish and retry.") from exc
                self._guard_depth += 1
                try:
                    yield
                finally:
                    self._guard_depth -= 1
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _store(self):
        root = self.service._root
        try:
            self.journal_root.relative_to(root)
        except ValueError:
            return self.journal_root / _hash(json.dumps(self.service._root_identity).encode())
        raise DocumentError("Choose a workspace outside the app's private recovery storage before organizing notes.")

    def _capture(self):
        service = self.service
        snapshot = service.index(service._workspace_id)
        if snapshot["truncated"] or snapshot["warnings"]:
            raise DocumentError("Organization requires a complete readable workspace index. Resolve its warnings and retry.")
        documents = {row["path"]: row for row in snapshot["documents"]}
        inventory = {}
        data = {}
        pending = [()]
        scanned = 0
        while pending:
            parts = pending.pop()
            with service._directory(parts) as directory:
                inventory["/".join(parts)] = {"identity": list(_identity(directory.anchors[-1].info)),
                                               "mode": stat.S_IMODE(directory.anchors[-1].info.st_mode), "kind": "directory"}
                with os.scandir(directory.fd) as scan:
                    names = []
                    for entry in scan:
                        scanned += 1
                        if scanned > MAX_SCANNED_ENTRIES:
                            raise DocumentError("Organization requires a complete bounded directory inventory.")
                        names.append(entry.name)
                for name in sorted(names):
                    path = "/".join((*parts, name))
                    info = directory.stat(name)
                    value = {"identity": list(_identity(info)), "mode": stat.S_IMODE(info.st_mode),
                             "kind": "other", "stamp": list(_stamp(info))}
                    try:
                        child = _parts(path)
                    except DocumentError:
                        child = None
                    if child and not _linked(info) and stat.S_ISDIR(info.st_mode):
                        pending.append(child)
                        value = {"identity": list(_identity(info)), "mode": stat.S_IMODE(info.st_mode), "kind": "directory"}
                    elif path in documents:
                        raw, info = service._read_bytes(directory, name)
                        if _hash(raw) != documents[path]["revision"]:
                            raise DocumentError("The workspace changed while preparing organization. Review a new preview.")
                        data[path] = raw
                        value = {"identity": list(_identity(info)), "mode": stat.S_IMODE(info.st_mode),
                                 "kind": "file", "revision": _hash(raw), "editable": documents[path]["editable"]}
                    inventory[path] = value
                directory.verify()
        if len(inventory) > MAX_SCANNED_ENTRIES + 1 or len(documents) > MAX_ENTRIES:
            raise DocumentError("Organization requires a complete bounded directory inventory.")
        if set(data) != set(documents):
            raise DocumentError("The workspace changed while preparing organization. Review a new preview.")
        return {"documents": documents, "inventory": inventory, "data": data}

    def _collision(self, destination):
        parts = _parts(destination)
        with self.service._directory(parts[:-1]) as directory:
            self.service._writable(directory)
            with os.scandir(directory.fd) as scan:
                for count, entry in enumerate(scan, 1):
                    if count > MAX_SCANNED_ENTRIES:
                        raise DocumentError("Too many destination entries to safely check collisions.")
                    if _key(entry.name) == _key(parts[-1]):
                        raise DocumentError("A file or folder with that name already exists. Choose a different name.")

    def prepare(self, source, destination):
        service = self.service
        self.ensure_ready()
        if not _SAFE_WRITES or sys.platform not in {"darwin", "linux"}:
            raise DocumentError("Safe relocation is unavailable on this platform.")
        source_parts, destination_parts = _parts(source), _parts(destination)
        if (len(destination_parts[-1].encode()) > 255 or _key(source) == _key(destination)
                or _key(destination).startswith(_key(source) + "/")
                or any(_key(part) in _ENGINE_DIRS for part in destination_parts)):
            raise DocumentError("Choose a different ordinary destination outside the folder being moved.")
        with service._directory() as directory:
            service._writable(directory)
        self._store()
        capture = self._capture()
        parent = "/".join(destination_parts[:-1])
        if capture["inventory"].get(parent, {}).get("kind") != "directory":
            raise DocumentError("Choose an existing destination folder using its current workspace path.")
        with service._directory(source_parts[:-1]) as directory:
            service._writable(directory)
        entry = capture["inventory"].get(source)
        if not entry or entry["kind"] not in {"file", "directory"}:
            raise DocumentError("Choose an existing ordinary Markdown note or folder.")
        kind = entry["kind"]
        if kind == "file" and Path(destination_parts[-1]).suffix.casefold() != ".md":
            raise DocumentError("The destination must remain a Markdown file ending in .md.")
        for path, item in capture["inventory"].items():
            if path != source and not path.startswith(source + "/"):
                continue
            if item["kind"] == "file":
                if not item["editable"]:
                    raise DocumentError("Generated or read-only notes cannot be moved or renamed.")
            elif item["kind"] == "directory":
                with service._directory(_parts(path)) as directory:
                    service._writable(directory)
            else:
                raise DocumentError("Folders containing hidden files, attachments, linked paths, or engine state cannot be moved.")
        self._collision(destination)
        token = secrets.token_urlsafe(32)
        self.previews.clear()
        self.previews[token] = {"workspace_id": service._workspace_id, "source": source, "destination": destination,
                                "kind": kind, "capture": capture, "created": time.monotonic()}
        moves = [{"from": path, "to": _mapped(path, source, destination),
                  "recovery_key": _hash(f"{service._root}\0{path}".encode()),
                  "target_recovery_key": _hash(f"{service._root}\0{_mapped(path, source, destination)}".encode())}
                 for path in sorted(capture["documents"]) if path == source or path.startswith(source + "/")]
        for path, document in capture["documents"].items():
            document["recovery_key"] = _hash(f"{service._root}\0{path}".encode())
        self.previews[token]["moves"] = moves
        return {"token": token, "source": source, "destination": destination, "kind": kind,
                "moves": moves, "documents": list(capture["documents"].values()), "warnings": [], "truncated": False}

    def _patch(self, capture, patches):
        if not isinstance(patches, list) or len(patches) > MAX_ENTRIES:
            raise DocumentError("Supply a bounded list of reviewed reference repairs.")
        results = {}
        total_edits = 0
        for patch in patches:
            if not isinstance(patch, dict):
                raise DocumentError("Invalid reference repair.")
            path = patch.get("path")
            _parts(path)
            document = capture["documents"].get(path)
            if path in results or not document or not document["editable"] or patch.get("revision") != document["revision"]:
                raise DocumentError("Reference repairs require unique writable notes at their reviewed revisions.")
            edits = patch.get("edits")
            if not isinstance(edits, list) or not edits:
                raise DocumentError("A reference repair must include reviewed text edits.")
            total_edits += len(edits)
            if total_edits > 10000:
                raise DocumentError("Too many reference repairs in one operation.")
            text, bom, newline = _decode(capture["data"][path])
            encoded = text.replace("\r\n", "\n").encode("utf-16-le")
            spans = []
            for edit in edits:
                if not isinstance(edit, dict):
                    raise DocumentError("Invalid reference repair range.")
                start, end, before, after = (edit.get(key) for key in ("from", "to", "before", "after"))
                if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(encoded) // 2
                        or not isinstance(before, str) or not isinstance(after, str)
                        or any(char in before + after for char in "\r\n\0")):
                    raise DocumentError("Reference repairs need bounded single-line UTF-16 ranges.")
                try:
                    selected = encoded[start * 2:end * 2].decode("utf-16-le")
                    replacement = after.encode("utf-16-le")
                except UnicodeError as exc:
                    raise DocumentError("A repair range splits a Unicode character.") from exc
                if selected != before:
                    raise DocumentError("The reviewed reference text changed. Review a new preview.")
                spans.append((start * 2, end * 2, replacement))
            spans.sort()
            if any(right[0] < left[1] or right[0] == left[0] for left, right in zip(spans, spans[1:])):
                raise DocumentError("Reference repair ranges cannot overlap.")
            for start, end, after in reversed(spans):
                encoded = encoded[:start] + after + encoded[end:]
            text = encoded.decode("utf-16-le")
            if newline == "crlf":
                text = text.replace("\n", "\r\n")
            raw = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
            if len(raw) > MAX_FILE_BYTES or sum(map(len, results.values())) + len(raw) > MAX_TREE_BYTES:
                raise DocumentError("The reviewed repairs exceed the document or operation size limit.")
            with self.service._directory(_parts(path)[:-1]) as directory:
                if self.service._reason(directory, text, newline):
                    raise DocumentError("Reference repairs cannot change protected generated-note ownership.")
            results[path] = raw
        return results

    def _journal_write(self, journal):
        raw = json.dumps(journal, ensure_ascii=True, sort_keys=True).encode()
        if len(raw) > 8 * MAX_FILE_BYTES:
            raise DocumentError("The recovery journal exceeds its size limit.")
        with _private_directory(self._store() / journal["id"]) as directory:
            temporary = ".journal-" + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
            try:
                _write_all(fd, raw)
                directory.verify()
                os.replace(temporary, "journal.json", src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
                os.fsync(directory.fd)
            finally:
                os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=directory.fd)
                except FileNotFoundError:
                    pass

    def _journal_create(self, preview, patches):
        capture = preview["capture"]
        affected = sorted({move["from"] for move in preview["moves"]} | set(patches))
        journal = {"version": 1, "id": secrets.token_hex(16), "root": str(self.service._root),
                   "root_identity": list(self.service._root_identity), "source": preview["source"],
                   "destination": preview["destination"], "kind": preview["kind"], "committed": False,
                   "subtree": {path: entry for path, entry in capture["inventory"].items()
                               if path == preview["source"] or path.startswith(preview["source"] + "/")}, "records": []}
        for number, path in enumerate(affected):
            entry = capture["inventory"][path]
            journal["records"].append({"path": path, "identity": entry["identity"], "mode": entry["mode"],
                                       "revision": entry["revision"], "backup": f"{number}.before",
                                       "after_revision": _hash(patches.get(path, capture["data"][path])),
                                       "after_identity": None, "restored_identity": None, "temporary": None})
        with _private_directory(self._store(), create=True) as store:
            os.mkdir(journal["id"], 0o700, dir_fd=store.fd)
            os.fsync(store.fd)
        self._journal_write(journal)
        with _private_directory(self._store() / journal["id"]) as directory:
            for record in journal["records"]:
                path, backup = record["path"], record["backup"]
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
                try:
                    _write_all(fd, capture["data"][path])
                finally:
                    os.close(fd)
            os.fsync(directory.fd)
        return journal

    def _move(self, source, destination, identity):
        source_parts, destination_parts = _parts(source), _parts(destination)
        with self.service._directory(source_parts[:-1]) as origin, self.service._directory(destination_parts[:-1]) as target:
            self.service._writable(origin)
            self.service._writable(target)
            if list(_identity(origin.stat(source_parts[-1]))) != identity:
                raise DocumentError("The relocation source changed. Review a new preview.")
            if stat.S_ISREG(origin.stat(source_parts[-1]).st_mode):
                raw, info = self.service._read_bytes(origin, source_parts[-1])
                text, _, newline = _decode(raw)
                if self.service._reason(origin, text, newline) or not info.st_mode & 0o222:
                    raise DocumentError("Generated or read-only notes cannot be relocated.")
            else:
                with self.service._directory(source_parts) as source_directory:
                    self.service._writable(source_directory)
            self._collision(destination)
            origin.verify(); target.verify()
            _exclusive_rename(origin.fd, source_parts[-1], target.fd, destination_parts[-1])
            os.fsync(origin.fd)
            os.fsync(target.fd)

    def _install(self, journal, record, path, raw, *, rollback=False):
        parts = _parts(path)
        with self.service._directory(parts[:-1]) as directory:
            self.service._writable(directory)
            expected = record["after_identity"] if rollback else record["identity"]
            expected_revision = record["after_revision"] if rollback else record["revision"]
            current, info = self.service._read_bytes(directory, parts[-1])
            if list(_identity(info)) != expected or _hash(current) != expected_revision or stat.S_IMODE(info.st_mode) != record["mode"]:
                raise DocumentError("A note changed during relocation. Its external changes were preserved.")
            text, _, newline = _decode(current)
            if self.service._reason(directory, text, newline):
                raise DocumentError("Generated or read-only notes cannot be repaired or recovered automatically.")
            temporary = f".az-relocate-{journal['id']}-{record['backup'].split('.')[0]}-{'restore' if rollback else 'edit'}.tmp"
            record["temporary"] = {"path": "/".join((*parts[:-1], temporary)), "identity": None}
            self._journal_write(journal)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
            try:
                _write_all(fd, raw)
                os.fchmod(fd, record["mode"])
                os.fsync(fd)
                created_identity = list(_identity(os.fstat(fd)))
                record["temporary"]["identity"] = created_identity
                record["restored_identity" if rollback else "after_identity"] = created_identity
                self._journal_write(journal)
                current, info = self.service._read_bytes(directory, parts[-1])
                if list(_identity(info)) != expected or _hash(current) != expected_revision or stat.S_IMODE(info.st_mode) != record["mode"]:
                    raise DocumentError("A note changed during relocation. Its external changes were preserved.")
                self.service._writable(directory)
                if list(_identity(directory.stat(temporary))) != created_identity:
                    raise DocumentError("The relocation temporary file changed.")
                os.replace(temporary, parts[-1], src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
                os.fsync(directory.fd)
                record["temporary"] = None
                self._journal_write(journal)
            finally:
                os.close(fd)

    def _cleanup(self, journal):
        path = self._store() / journal["id"]
        with _private_directory(path) as directory:
            with os.scandir(directory.fd) as scan:
                names = [entry.name for entry in scan]
            allowed = {record["backup"] for record in journal["records"]} | {"journal.json"}
            for name in names:
                if name in allowed:
                    continue
                if not re.fullmatch(r"\.journal-[a-f0-9]{24}", name):
                    raise DocumentError("An unexpected private journal entry needs review.")
                info = directory.stat(name)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    raise DocumentError("A private journal temporary file could not be verified.")
                os.unlink(name, dir_fd=directory.fd)
            for name in [record["backup"] for record in journal["records"]] + ["journal.json"]:
                try:
                    os.unlink(name, dir_fd=directory.fd)
                except FileNotFoundError:
                    pass
            os.fsync(directory.fd)
        with _private_directory(self._store()) as store:
            os.rmdir(journal["id"], dir_fd=store.fd)
            os.fsync(store.fd)

    def _location(self, journal):
        found = []
        expected = journal["subtree"][journal["source"]]["identity"]
        source_record = next((record for record in journal["records"] if record["path"] == journal["source"]), None)
        for path in (journal["source"], journal["destination"]):
            parts = _parts(path)
            try:
                with self.service._directory(parts[:-1]) as directory:
                    info = directory.stat(parts[-1])
                identities = [expected]
                if journal["kind"] == "file" and source_record:
                    identities += [source_record.get("after_identity"), source_record.get("restored_identity")]
                if list(_identity(info)) in identities:
                    found.append(path)
                else:
                    raise DocumentError("A relocation location was replaced externally. Preserve the recovery journal for manual review.")
            except FileNotFoundError:
                pass
        if len(found) != 1:
            raise DocumentError("The relocation source cannot be identified safely. Preserve the recovery journal for manual review.")
        return found[0]

    def _rollback(self, journal):
        location = self._location(journal)
        for record in journal["records"]:
            temporary = record.get("temporary")
            if temporary:
                parts = tuple(temporary["path"].split("/"))
                # Only the journal's own generated hidden temporary basename is allowed.
                _parts("/".join(parts[:-1]), empty=True)
                if not re.fullmatch(r"\.az-relocate-" + journal["id"] + r"-[0-9]+-(?:restore|edit)\.tmp", parts[-1]):
                    raise DocumentError("Invalid recovery temporary path.")
                with self.service._directory(parts[:-1]) as directory:
                    try:
                        info = directory.stat(parts[-1])
                        if list(_identity(info)) != temporary.get("identity"):
                            raise DocumentError("A relocation temporary file could not be identified safely.")
                        os.unlink(parts[-1], dir_fd=directory.fd)
                        os.fsync(directory.fd)
                    except FileNotFoundError:
                        pass
                record["temporary"] = None
                self._journal_write(journal)
        capture = self._capture()
        states = {}
        expected_subtree = {}
        for path, entry in journal["subtree"].items():
            expected_subtree[_mapped(path, journal["source"], location)] = dict(entry)
        for record in journal["records"]:
            path = _mapped(record["path"], journal["source"], location)
            entry = capture["inventory"].get(path)
            identity = entry and entry.get("identity")
            revision = entry and entry.get("revision")
            if (identity in (record["identity"], record.get("restored_identity")) and revision == record["revision"]):
                states[record["path"]] = False
            elif identity == record.get("after_identity") and revision == record["after_revision"]:
                states[record["path"]] = True
            else:
                raise DocumentError("External note changes prevent automatic relocation recovery. The private journal and backups were preserved.")
            if path in expected_subtree:
                expected_subtree[path]["identity"] = identity
                expected_subtree[path]["revision"] = revision
        actual_subtree = {path: entry for path, entry in capture["inventory"].items()
                          if path == location or path.startswith(location + "/")}
        if actual_subtree != expected_subtree:
            raise DocumentError("The moved folder changed externally. Automatic recovery will not remove or overwrite its new contents.")
        for record in journal["records"]:
            if states[record["path"]]:
                with _private_directory(self._store() / journal["id"]) as directory:
                    raw = _private_read(directory, record["backup"])
                if _hash(raw) != record["revision"]:
                    raise DocumentError("A recovery backup could not be verified.")
                path = _mapped(record["path"], journal["source"], location)
                self._install(journal, record, path, raw, rollback=True)
        if location == journal["destination"]:
            parts = _parts(location)
            with self.service._directory(parts[:-1]) as directory:
                identity = list(_identity(directory.stat(parts[-1])))
            self._move(location, journal["source"], identity)
        self._cleanup(journal)

    def recover(self):
        try:
            with self.guard():
                self._recover()
        except (DocumentError, OSError) as exc:
            self.blocked = str(exc)

    def _recover(self):
        self.blocked = ""
        if not _SAFE_WRITES:
            return
        try:
            store_path = self._store()
            try:
                with _private_directory(store_path) as store:
                    with os.scandir(store.fd) as scan:
                        operations = sorted(entry.name for entry in scan if entry.name != ".lock")
            except FileNotFoundError:
                return
            if len(operations) > 32:
                raise DocumentError("Too many pending organization journals require manual review.")
            for operation in operations:
                if not re.fullmatch(r"[a-f0-9]{32}", operation):
                    raise DocumentError("An unexpected recovery entry requires manual review.")
                with _private_directory(store_path / operation) as directory:
                    try:
                        journal = json.loads(_private_read(directory, "journal.json", 8 * MAX_FILE_BYTES))
                    except FileNotFoundError:
                        with os.scandir(directory.fd) as scan:
                            if any(scan):
                                raise DocumentError("An incomplete private journal needs manual review.")
                        with _private_directory(store_path) as store:
                            os.rmdir(operation, dir_fd=store.fd)
                            os.fsync(store.fd)
                        continue
                self._validate_journal(journal, operation)
                if journal["committed"]:
                    self._cleanup(journal)
                else:
                    self._rollback(journal)
        except (DocumentError, OSError, ValueError, TypeError, KeyError) as exc:
            self.blocked = f"Organization recovery needs review before further writes. {exc} Private journal: {self.journal_root}"

    def _validate_journal(self, journal, operation):
        def identity(value, kind=None, optional=False):
            return (optional and value is None) or (
                isinstance(value, list) and len(value) == 3
                and all(type(part) is int and part >= 0 for part in value)
                and value[1] > 0 and value[2] in {stat.S_IFREG, stat.S_IFDIR}
                and (kind is None or value[2] == kind)
            )

        def revision(value):
            return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)

        def mode(value):
            return type(value) is int and 0 <= value <= 0o777

        if (not isinstance(journal, dict) or type(journal.get("version")) is not int or journal.get("version") != 1 or journal.get("id") != operation
                or not isinstance(journal.get("root"), str) or not Path(journal["root"]).is_absolute()
                or ".." in Path(journal["root"]).parts
                or not identity(journal.get("root_identity"), stat.S_IFDIR)
                or journal.get("root_identity") != list(self.service._root_identity)
                or type(journal.get("committed")) is not bool or journal.get("kind") not in {"file", "directory"}
                or not isinstance(journal.get("subtree"), dict) or not isinstance(journal.get("records"), list)
                or len(journal["records"]) > MAX_ENTRIES or len(journal["subtree"]) > MAX_ENTRIES):
            raise DocumentError("The recovery journal is not valid for this selected workspace.")
        _parts(journal["source"]); _parts(journal["destination"])
        if (journal["source"] not in journal["subtree"] or _key(journal["source"]) == _key(journal["destination"])
                or _key(journal["destination"]).startswith(_key(journal["source"]) + "/")
                or any(_key(part) in _ENGINE_DIRS for part in (*_parts(journal["source"]), *_parts(journal["destination"])))):
            raise DocumentError("The recovery journal has no source identity.")
        paths, backups = set(), set()
        for path, entry in journal["subtree"].items():
            _parts(path)
            if ((path != journal["source"] and not path.startswith(journal["source"] + "/"))
                    or not isinstance(entry, dict) or entry.get("kind") not in {"file", "directory"}
                    or not identity(entry.get("identity"), stat.S_IFDIR if entry.get("kind") == "directory" else stat.S_IFREG)
                    or not mode(entry.get("mode"))):
                raise DocumentError("The recovery journal contains an invalid subtree.")
            if entry["kind"] == "file" and (not revision(entry.get("revision")) or entry.get("editable") is not True):
                raise DocumentError("The recovery journal contains an invalid note identity.")
        if journal["subtree"][journal["source"]]["kind"] != journal["kind"]:
            raise DocumentError("The recovery source kind does not match its identity.")
        for record in journal["records"]:
            if not isinstance(record, dict):
                raise DocumentError("The recovery journal contains an invalid note record.")
            _parts(record["path"])
            if (Path(record["path"]).suffix.casefold() != ".md" or record["path"] in paths
                    or not isinstance(record.get("backup"), str) or not re.fullmatch(r"[0-9]+\.before", record["backup"])
                    or record["backup"] in backups or not revision(record.get("revision"))
                    or not revision(record.get("after_revision")) or not mode(record.get("mode"))
                    or not identity(record.get("identity"), stat.S_IFREG)
                    or not identity(record.get("after_identity"), stat.S_IFREG, optional=True)
                    or not identity(record.get("restored_identity"), stat.S_IFREG, optional=True)):
                raise DocumentError("The recovery journal contains an invalid note record.")
            temporary = record.get("temporary")
            if temporary is not None:
                if (not isinstance(temporary, dict) or not isinstance(temporary.get("path"), str)
                        or not identity(temporary.get("identity"), stat.S_IFREG, optional=True)):
                    raise DocumentError("The recovery journal contains an invalid temporary record.")
                parts = temporary["path"].split("/")
                _parts("/".join(parts[:-1]), empty=True)
                expected_name = r"\.az-relocate-" + operation + "-" + record["backup"].split(".")[0] + r"-(?:restore|edit)\.tmp"
                parents = {str(Path(record["path"]).parent), str(Path(_mapped(record["path"], journal["source"], journal["destination"])).parent)}
                if not re.fullmatch(expected_name, parts[-1]) or str(Path(temporary["path"]).parent) not in parents:
                    raise DocumentError("The recovery journal contains an invalid temporary path.")
            paths.add(record["path"]); backups.add(record["backup"])
        if any(entry["kind"] == "file" and path not in paths for path, entry in journal["subtree"].items()):
            raise DocumentError("The recovery journal is missing moved note backups.")

    def ensure_ready(self):
        self.recover()
        if self.blocked:
            raise DocumentError(self.blocked)

    def apply(self, token, repair, patches):
        receipt = self.receipts.get(token) if isinstance(token, str) else None
        if receipt and receipt["workspace_id"] == self.service._workspace_id:
            if receipt["result"] is None:
                receipt["result"] = self._result(receipt["preview"], receipt["journal"])
            return receipt["result"]
        self.ensure_ready()
        if not isinstance(token, str) or token not in self.previews:
            raise DocumentError("This organization preview expired. Review a new preview.")
        preview = self.previews.pop(token)
        if preview["workspace_id"] != self.service._workspace_id or time.monotonic() - preview["created"] > 1800:
            raise DocumentError("This organization preview expired. Review a new preview.")
        if type(repair) is not bool or (repair is False and patches != []):
            raise DocumentError("Choose whether to repair references for this operation.")
        fresh = self._capture()
        if fresh["inventory"] != preview["capture"]["inventory"]:
            raise DocumentError("The workspace changed after this preview. Review a new preview before moving notes.")
        with self.service._directory() as directory:
            self.service._writable(directory)
        self._collision(preview["destination"])
        replacements = self._patch(preview["capture"], patches)
        try:
            journal = self._journal_create(preview, replacements)
        except Exception:
            self.recover()
            raise
        try:
            if self._capture()["inventory"] != preview["capture"]["inventory"]:
                raise DocumentError("The workspace changed while saving relocation backups. Review a new preview.")
            self._move(preview["source"], preview["destination"], preview["capture"]["inventory"][preview["source"]]["identity"])
            for record in journal["records"]:
                path = record["path"]
                if path in replacements and _hash(replacements[path]) != record["revision"]:
                    self._install(journal, record, _mapped(path, preview["source"], preview["destination"]), replacements[path])
            expected = {_mapped(path, preview["source"], preview["destination"]): dict(entry)
                        for path, entry in preview["capture"]["inventory"].items()}
            for record in journal["records"]:
                if record["after_identity"]:
                    entry = expected[_mapped(record["path"], preview["source"], preview["destination"])]
                    entry["identity"] = record["after_identity"]; entry["revision"] = record["after_revision"]
            if self._capture()["inventory"] != expected:
                raise DocumentError("The workspace changed during relocation. Recovery will preserve external changes.")
            journal["committed"] = True
            self._journal_write(journal)
        except Exception as exc:
            try:
                self._rollback(journal)
            except Exception as recovery_error:
                self.blocked = f"Organization recovery needs review before further writes. {recovery_error} Private journal: {self.journal_root}"
                raise DocumentError(self.blocked) from exc
            raise DocumentError("Relocation did not finish. Original notes and paths were restored; review a new preview.") from exc
        self.receipts[token] = {"workspace_id": self.service._workspace_id, "result": None,
                                "preview": {key: preview[key] for key in ("source", "destination", "kind", "moves")}, "journal": journal}
        while len(self.receipts) > 4:
            self.receipts.pop(next(iter(self.receipts)))
        self.receipts[token]["result"] = self._result(preview, journal)
        return self.receipts[token]["result"]

    def _result(self, preview, journal):
        # Once the committed marker is durable, response/cleanup failures never undo
        # a completed operation. Surviving committed journals are cleaned on reopen.
        for document_id, (path, identity) in list(self.service._documents.items()):
            destination = _mapped(path, preview["source"], preview["destination"])
            if destination != path:
                self.service._document_ids.pop(path, None)
                self.service._document_ids[destination] = document_id
                self.service._documents[document_id] = destination, identity
        documents = [self.service.read(self.service._workspace_id, _mapped(record["path"], preview["source"], preview["destination"]))
                     for record in journal["records"]]
        try:
            self._cleanup(journal)
        except (OSError, DocumentError):
            pass
        return {"workspace": self.service.tree(self.service._workspace_id), "moves": preview["moves"], "documents": documents}
