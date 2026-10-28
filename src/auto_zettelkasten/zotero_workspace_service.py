"""Session-facing orchestration for an opt-in local Zotero workspace.

All writes are disabled by default. Documents retain their existing revision
checks; annotation transport never retries an uncertain dispatched operation.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import threading
import time
import webbrowser

try:
    import fcntl
except ImportError:
    fcntl = None

from .documents import DocumentConflict, DocumentError
from .zotero_annotations import (AnnotationConflict, PendingAnnotations, api_to_reader,
    project_annotations, reader_to_api, verified_annotation_span)
from .zotero_links import parse_zotero_uri
from .zotero_workspace import VerifiedPDF, ZoteroWorkspaceClient, ZoteroWorkspaceError

_KEY = re.compile(r"[A-Z0-9]{8}\Z")
_SCOPE = re.compile(r"users/0|groups/[1-9][0-9]{0,14}\Z")
_MAX_PRIVATE = 16 * 1024 * 1024


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def _record_data(record):
    if not isinstance(record, dict) or not isinstance(record.get("data", record), dict):
        raise ValueError("Invalid Zotero record.")
    return record.get("data", record)


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("A Zotero key must have eight uppercase letters or digits.")
    return value


def _scope(value):
    if not isinstance(value, str) or not _SCOPE.fullmatch(value):
        raise ValueError("Select a supported Zotero library scope.")
    return value


def _version(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("An explicit nonnegative object version is required.")
    return value


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError("Private Zotero storage must use an absolute, unlinked directory.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Private Zotero storage must be owned by this user with owner-only access.")
    return path


def _read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > _MAX_PRIVATE:
            raise ValueError("Invalid private Zotero record.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_PRIVATE + 1)
        if len(raw) > _MAX_PRIVATE:
            raise ValueError("Private Zotero record exceeds its limit.")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid private Zotero record.")
        return value
    finally:
        os.close(fd)


def _write_json(path, value):
    directory = _private_directory(path.parent)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    if len(raw) > _MAX_PRIVATE:
        raise ValueError("Private Zotero record exceeds its limit.")
    if path.is_symlink():
        raise ValueError("Private Zotero records cannot be symbolic links.")
    fd, temporary = tempfile.mkstemp(prefix=".zotero-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _receipt_guard(path):
    if fcntl is None:
        raise ValueError("Safe annotation projection locking is unavailable on this platform.")
    _private_directory(path.parent.parent)
    _private_directory(path.parent)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid annotation projection lock.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _workspace_identity(workspace_path, note_path):
    root = Path(workspace_path).resolve(strict=True)
    if not isinstance(note_path, str) or "\\" in note_path or "\x00" in note_path:
        raise ValueError("Invalid note path.")
    parts = PurePosixPath(note_path)
    if parts.is_absolute() or not parts.parts or any(part in {".", ".."} or part.startswith(".") for part in parts.parts) or parts.suffix.lower() != ".md":
        raise ValueError("Invalid note path.")
    info = root.stat()
    identity = [info.st_dev, info.st_ino]
    path = root.joinpath(*parts.parts)
    if path.resolve(strict=True) != path or any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError("Annotation snapshots require a direct active Markdown path.")
    return root, identity, path


def _snapshot_path(private_root, root, identity, note_path):
    workspace_key = _sha(json.dumps([str(root), identity], separators=(",", ":")))
    return Path(private_root) / "projections" / workspace_key / (_sha(note_path) + ".json")


def read_annotation_snapshot(private_root, workspace_path, note_path):
    """Return only a trusted receipt whose managed block still matches the file."""
    try:
        root, identity, path = _workspace_identity(workspace_path, note_path)
        receipt_path = _snapshot_path(private_root, root, identity, note_path)
        if not receipt_path.exists():
            return None
        for directory in [Path(private_root), receipt_path.parent.parent, receipt_path.parent]:
            if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
                return None
        receipt = _read_json(receipt_path)
        if not receipt:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(1024 * 1024 + 1)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                return None
        finally:
            os.close(fd)
        text = raw.decode("utf-8-sig")
        # Prepared receipts also recover a crash after document save but before
        # receipt finalization, only when the intended bytes reached the file.
        for candidate in (receipt, receipt.get("previous")):
            if not isinstance(candidate, dict) or (candidate.get("workspace"), candidate.get("workspace_identity"), candidate.get("path")) != (str(root), identity, note_path):
                continue
            if candidate.get("state") == "prepared" and _sha(raw) != candidate.get("projected_revision"):
                continue
            if candidate.get("state") in {"prepared", "confirmed"} and verified_annotation_span(text, candidate.get("block_hash")):
                return {"block_hash": candidate["block_hash"], "groups": copy.deepcopy(candidate["groups"])}
        return None
    except (OSError, ValueError, KeyError, UnicodeError):
        return None


class _Registrar:
    def __init__(self, client):
        self.client = client

    def _scope(self, binding):
        if binding["server_id"] != self.client.server_id:
            raise ValueError("Zotero server changed during migration.")
        if binding["library_type"] == "user":
            if binding["library_id"] != "0":
                raise ValueError("The source binding is not the connected personal library.")
            return "users/0"
        return _scope(f"groups/{binding['library_id']}")

    def find(self, binding, path):
        return self.client.find_linked_note(self._scope(binding), binding["item_key"], path)

    def register(self, binding, path, *, key, write_token):
        return self.client.register_linked_note(self._scope(binding), binding["item_key"], path,
            key=key, title=path.stem, write_token=write_token)

    def verify(self, binding, path, attachment):
        found = self.find(binding, path)
        return found is not None and _record_data(found).get("key") == _record_data(attachment).get("key")


class ZoteroWorkspaceService:
    PDF_TTL = 1800
    MAX_PDFS = 32

    def __init__(self, documents, private_root, client=None, writes_enabled=False):
        self.documents = documents
        self.private_root = _private_directory(Path(private_root))
        self.client = client or ZoteroWorkspaceClient(credential_path=self.private_root / "authorization.json")
        self.writes_enabled = writes_enabled is True
        self._lock = threading.RLock()
        self._pdfs = {}
        self._previews = {}
        self._source_parents = {}
        self._manual_sources = {}
        self._unbound_notes = {}

    def _status(self):
        value = self.client.status()
        writable = self.writes_enabled and value.get("available") is True and value.get("authorized") is True and value.get("write_supported") is True
        return {**value, "connected": value.get("available") is True, "writable": writable,
            "writes_enabled": self.writes_enabled, "writes_available": self.writes_enabled and value.get("write_supported") is True}

    def _require_writes(self, *, authorized=True):
        if not self.writes_enabled:
            raise ValueError("Zotero writes are disabled for this workspace session.")
        status = self._status()
        if not status["connected"] or not status["writes_available"] or authorized and not status["writable"]:
            raise ValueError("Authorize a supported Zotero connection before writing.")
        return status

    def _queue(self):
        if not self.client.server_id:
            raise ValueError("Connect to Zotero before accessing annotation operations.")
        root = self.private_root / "outbox" / _sha(self.client.server_id)
        _private_directory(root.parent)
        return PendingAnnotations(root)

    def _attachment_parent(self, scope, key):
        identity = (self.client.server_id, scope, key)
        cached = self._source_parents.get(identity)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        record = self.client.item(scope, key)
        data = _record_data(record) if record else {}
        parent = data.get("parentItem") if data.get("itemType") == "attachment" else None
        if not isinstance(parent, str) or not _KEY.fullmatch(parent):
            return None
        if len(self._source_parents) >= 128:
            self._source_parents.pop(next(iter(self._source_parents)))
        self._source_parents[identity] = (time.monotonic() + 5, parent)
        return parent

    def _note_inventory(self, workspace_id, *, targets=None):
        self._unbound_notes = {}
        if not workspace_id:
            return {}
        records = self.documents.recall_corpus(workspace_id)
        if records.get("truncated"):
            raise DocumentError("The note inventory is incomplete; an atomic note cannot be selected safely.")
        result = {}
        for row in records["documents"]:
            binding = row.get("live_binding")
            fields = dict(row.get("frontmatter", {}))
            navigation = row.get("source_navigation")
            if isinstance(navigation, dict) and navigation.get("verified") is True and navigation.get("basis") == "writer_metadata":
                fields.update(navigation)
            uri = parse_zotero_uri(fields.get("zotero_item_uri"))
            pdf_uri = parse_zotero_uri(fields.get("source_pdf_uri"))
            if binding and binding.get("server_id") == self.client.server_id and row.get("live_identity_valid") is True:
                binding_scope = "library" if (binding.get("library_type"), binding.get("library_id")) == ("user", "0") else (
                    f"groups/{binding.get('library_id')}" if binding.get("library_type") == "group" else None)
                bound_uri = parse_zotero_uri(f"zotero://select/{binding_scope}/items/{binding.get('item_key')}")
                if bound_uri and fields.get("zotero_item_key", binding["item_key"]) == binding["item_key"]:
                    uri = bound_uri
            if uri is None and pdf_uri and pdf_uri.action == "open-pdf":
                uri = pdf_uri
            if uri is None and isinstance(navigation, dict) and navigation.get("verified") is True and navigation.get("basis") == "writer_metadata":
                recorded_key = navigation.get("zotero_item_key")
                if isinstance(recorded_key, str) and _KEY.fullmatch(recorded_key):
                    manual = self._manual_sources.get((workspace_id, row["path"]))
                    if manual and manual["server_id"] == self.client.server_id and manual["item_key"] == recorded_key:
                        selected_scope = "library" if manual["scope"] == "users/0" else manual["scope"]
                        uri = parse_zotero_uri(f"zotero://select/{selected_scope}/items/{recorded_key}")
                    else:
                        self._unbound_notes.setdefault(recorded_key, []).append({"path": row["path"],
                            "title": row.get("display_title") or Path(row["path"]).stem,
                            "item_key": recorded_key, "requires_scope_confirmation": True})
            source_scope = "users/0" if uri and uri.scope == "library" else uri.scope if uri else None
            if not uri:
                continue
            item_key = uri.key
            recorded_key = fields.get("zotero_item_key")
            if uri.action == "open-pdf" or recorded_key and recorded_key != uri.key:
                if not isinstance(recorded_key, str) or not _KEY.fullmatch(recorded_key) or targets is not None and (source_scope, recorded_key) not in targets:
                    continue
                selected_attachment = fields.get("attachment_key") or (pdf_uri.key if pdf_uri and pdf_uri.action == "open-pdf" and pdf_uri.scope == uri.scope else None)
                if selected_attachment != uri.key or self._attachment_parent(source_scope, uri.key) != recorded_key:
                    continue
                item_key = recorded_key
            if targets is not None and (source_scope, item_key) not in targets:
                continue
            if binding and (binding.get("server_id") != self.client.server_id or row.get("live_identity_valid") is not True):
                continue
            result.setdefault((source_scope, item_key), []).append({"path": row["path"], "title": row.get("display_title") or Path(row["path"]).stem,
                "attachment_key": fields.get("attachment_key") or (binding.get("attachment_key") if binding else None), "source_pdf_uri": fields.get("source_pdf_uri")})
        return result

    def _notes(self, workspace_id, scope, item_key):
        return self._note_inventory(workspace_id, targets={(scope, item_key)}).get((scope, item_key), [])

    def _enrich(self, page, data):
        scope = _scope(data.get("scope", "users/0"))
        result = copy.deepcopy(page)
        targets = {(scope, _record_data(row).get("parentItem") or _record_data(row).get("key")) for row in result["records"]}
        notes_by_source = self._note_inventory(data.get("workspace_id"), targets=targets)
        for row in result["records"]:
            row["scope"] = scope
            item = _record_data(row)
            parent = item.get("parentItem") if item.get("itemType") in {"attachment", "note"} else item.get("key")
            notes = notes_by_source.get((scope, parent), [])
            if len(notes) == 1:
                row["note_path"] = notes[0]["path"]
            elif len(notes) > 1:
                row["note_candidates"] = notes
            if parent in self._unbound_notes:
                row["unbound_note_candidates"] = copy.deepcopy(self._unbound_notes[parent])
        return result

    @staticmethod
    def _stamp(path):
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("The PDF attachment is no longer a regular file.")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _pdf_identity(self, pdf):
        stamp = self._stamp(pdf.path)
        if stamp[2:4] != (pdf.size, pdf.mtime_ns):
            raise ValueError("The PDF changed while opening it.")
        return _sha(json.dumps(stamp)), stamp

    def _reader_id(self, pdf):
        return _sha(json.dumps([pdf.server_id, pdf.scope, pdf.attachment_key], separators=(",", ":")))

    def _pdf_baseline(self, pdf):
        identity, stamp = self._pdf_identity(pdf)
        path = self.private_root / "pdf-identities" / (self._reader_id(pdf) + ".json")
        previous = _read_json(path) if path.parent.exists() else None
        if previous is None:
            _write_json(path, {"identity": identity})
        return identity, stamp, previous is not None and previous.get("identity") != identity

    def pdf_capability(self, token):
        with self._lock:
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
                raise ValueError("Invalid PDF capability.")
            entry = self._pdfs.get(token)
            if not entry or entry["expires"] <= time.monotonic():
                self._pdfs.pop(token, None)
                raise ValueError("PDF access expired; reopen the source.")
            pdf = entry["pdf"]
            if pdf.server_id != self.client.server_id or self._stamp(pdf.path) != entry["stamp"]:
                self._pdfs.pop(token, None)
                raise ValueError("The source PDF or Zotero connection changed; reopen it.")
            return pdf

    def revoke_pdf(self, token=None):
        with self._lock:
            if token is None:
                self._pdfs.clear()
            else:
                self._pdfs.pop(token, None)

    def _refresh_pdf_leases(self, pdf, stamp):
        """Renew active reader leases only after a verified annotation refresh."""
        now = time.monotonic()
        current = pdf.server_id == self.client.server_id and self._stamp(pdf.path) == stamp
        for token, entry in list(self._pdfs.items()):
            saved = entry["pdf"]
            if (saved.scope, saved.attachment_key) != (pdf.scope, pdf.attachment_key):
                continue
            if (entry["expires"] <= now or not current or saved.server_id != pdf.server_id
                    or saved.path != pdf.path or entry["stamp"] != stamp):
                self._pdfs.pop(token, None)
                continue
            entry["expires"] = now + self.PDF_TTL

    def validate_pdf_stream(self, token, info):
        """Bind an already-open file descriptor before the HTTP handler sends bytes."""
        with self._lock:
            pdf = self.pdf_capability(token)
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if not stat.S_ISREG(info.st_mode) or stamp != self._pdfs[token]["stamp"]:
                self.revoke_pdf(token)
                raise ValueError("The PDF changed while opening the response stream.")
            return pdf

    def _internal_path(self, scope, attachment_key, key):
        return self.private_root / "annotation-internal" / (_sha(json.dumps([self.client.server_id, scope, attachment_key, key])) + ".json")

    def _known_internal(self, scope, attachment_key, key):
        record = _read_json(self._internal_path(scope, attachment_key, key))
        return bool(record and record.get("identity") == [self.client.server_id, scope, attachment_key, key])

    def _remember_internal(self, pending):
        if pending["state"] == "confirmed" and pending["operation"] == "upsert" and pending["base_version"] == 0:
            identity = [self.client.server_id, pending["scope"], pending["attachment_key"], pending["annotation_key"]]
            _write_json(self._internal_path(*identity[1:]), {"identity": identity, "creation_operation": pending["id"]})
        elif pending["state"] == "confirmed" and pending["operation"] == "delete":
            self._internal_path(pending["scope"], pending["attachment_key"], pending["annotation_key"]).unlink(missing_ok=True)

    def _request_path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", request_id):
            raise ValueError("Invalid saved annotation request identity.")
        return self.private_root / "annotation-requests" / (_sha(json.dumps([self.client.server_id, request_id])) + ".json")

    @staticmethod
    def _operation_ref(record):
        return {key: record[key] for key in ("id", "scope", "attachment_key", "annotation_key", "operation", "base_version")} | {
            "payload_hash": _sha(json.dumps(record.get("payload"), sort_keys=True, allow_nan=False))}

    def _saved_requests(self, data, scope, attachment_key, queue):
        identifiers = data.get("request_ids")
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 64 or any(not isinstance(value, str) for value in identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError("Select distinct saved annotation request identities (at most 64).")
        requests, unresolved = [], []
        count = 0
        for request_id in identifiers:
            path = self._request_path(request_id)
            receipt = _read_json(path)
            if not receipt or receipt.get("scope") != scope or receipt.get("attachment_key") != attachment_key:
                unresolved.append(request_id)
                continue
            drafts = receipt.get("drafts")
            if (not isinstance(drafts, list) or not 1 <= len(drafts) <= 64 or type(receipt.get("delete")) is not bool
                    or receipt.get("request_hash") != _sha(json.dumps([scope, attachment_key, receipt["delete"], drafts], sort_keys=True, allow_nan=False))):
                unresolved.append(request_id)
                continue
            count += len(drafts)
            if count > 64:
                raise ValueError("Recover at most 64 annotation changes per action.")
            refs = receipt.get("operation_refs")
            rows = []
            if receipt.get("state") not in {"confirmed", "discarded"}:
                if not isinstance(refs, list) or len(refs) != len(drafts):
                    requests.append({"id": request_id, "path": path, "receipt": receipt, "records": [], "needs_admission": True})
                    continue
                for ref, draft in zip(refs, drafts):
                    row = queue.get(ref.get("id")) if isinstance(ref, dict) else None
                    if (not row or not isinstance(draft, dict) or self._operation_ref(row) != ref
                            or (row["scope"], row["attachment_key"], row["annotation_key"], row["base_version"], row["operation"]) !=
                            (scope, attachment_key, draft.get("id"), draft.get("version", 0), "delete" if receipt["delete"] else "upsert")):
                        break
                    rows.append(row)
                if len(rows) != len(refs):
                    unresolved.append(request_id)
                    continue
            requests.append({"id": request_id, "path": path, "receipt": receipt, "records": rows})
        return requests, unresolved

    def _confirm_request(self, request, queue, *, event):
        receipt = request["receipt"]
        if receipt.get("state") in {"confirmed", "discarded"}:
            return True
        refs = receipt.get("operation_refs", [])
        if not refs or request.get("needs_admission"):
            return False
        rows = [queue.get(ref["id"]) for ref in refs]
        if any(not row or self._operation_ref(row) != ref for row, ref in zip(rows, refs)):
            return False
        complete = all(row["state"] in {"confirmed", "discarded"} for row in rows)
        receipt["events"] = (receipt.get("events", []) + [{"action": event, "states": [row["state"] for row in rows]}])[-64:]
        if complete:
            receipt["state"] = "discarded" if any(row["state"] == "discarded" for row in rows) else "confirmed"
            # Keep the original response as audit evidence; new transport replays
            # report the settled state rather than the old uncertain response.
            receipt.setdefault("initial_result", receipt.get("result"))
            receipt["result"] = {"annotations": [], "pending": [], "resolved": True,
                "discarded": [row["annotation_key"] for row in rows if row["state"] == "discarded"]}
        _write_json(request["path"], receipt)
        return complete

    def _annotations(self, scope, key, writable=False):
        return [api_to_reader(row, scope=scope, attachment_key=key, writable=writable,
                    known_internal=self._known_internal(scope, key, _record_data(row)["key"]))
                for row in self.client.annotations(scope, key)]

    def _open_pdf(self, data):
        scope, key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        status = self._status()
        pdf = self.client.pdf_file(scope, key)
        identity, stamp, replaced = self._pdf_baseline(pdf)
        annotations = self._annotations(scope, key, writable=status["writable"] and not replaced)
        notes = self._notes(data.get("workspace_id"), scope, pdf.parent_item) if pdf.parent_item else []
        selected = next((row for row in notes if row["path"] == data.get("note_path")), None)
        if data.get("note_path") and selected is None:
            raise ValueError("The requested note is not bound to this bibliographic item.")
        if selected is None and len(notes) == 1:
            selected = notes[0]
        page = data.get("page")
        if page is not None and (type(page) is not int or not 1 <= page <= 100001):
            raise ValueError("PDF page must be a positive physical page number.")
        annotation_key = data.get("annotation_key")
        if annotation_key is not None:
            _key(annotation_key)
        location = {"pageIndex": page - 1} if page is not None else {}
        warning = "The PDF file changed. Verify its annotations before editing or projecting them." if replaced else None
        target = next((row for row in annotations if row["id"] == annotation_key), None)
        if target and not replaced:
            location.update(annotationID=annotation_key, pageIndex=target["position"]["pageIndex"])
        elif annotation_key and not replaced:
            warning = "The recorded highlight is unavailable. Showing its recorded page." if page is not None else "The recorded highlight is unavailable; no page was recorded."
        now = time.monotonic()
        self._pdfs = {token: entry for token, entry in self._pdfs.items() if entry["expires"] > now}
        while len(self._pdfs) >= self.MAX_PDFS:
            self._pdfs.pop(next(iter(self._pdfs)))
        token = secrets.token_urlsafe(24)
        self._pdfs[token] = {"pdf": pdf, "stamp": stamp, "expires": now + self.PDF_TTL}
        source_scope = "library" if scope == "users/0" else scope
        source_uri = f"zotero://open-pdf/{source_scope}/items/{key}"
        if page is not None or annotation_key:
            source_uri += "?" + "&".join(part for part in [f"page={page}" if page is not None else "", f"annotation={annotation_key}" if annotation_key else ""] if part)
        attachment = self.client.item(scope, key)
        reader = {"id": self._reader_id(pdf), "title": _record_data(attachment).get("title") or "Source PDF",
            "url": f"/api/zotero/attachments/{token}/file", "scope": scope, "attachment_key": key,
            "item_key": pdf.parent_item, "annotations": annotations, "location": location,
            "readOnly": not status["writable"] or replaced, "pdf_identity": identity, "source_pdf_uri": source_uri}
        if selected:
            reader["note_path"] = selected["path"]
        if warning:
            reader["warning"] = warning
        return {"reader": reader, "note_candidates": notes if len(notes) > 1 else []}

    def _open_source(self, data):
        if not data.get("uri") and data.get("confirm_scope") is True:
            scope, key = _scope(data.get("scope")), _key(data.get("item_key"))
            if not isinstance(data.get("workspace_id"), str) or not isinstance(data.get("note_path"), str):
                raise ValueError("Select the source note whose library you are confirming.")
            current = self.documents.read(data["workspace_id"], data["note_path"])
            navigation = current.get("source_navigation")
            if not isinstance(navigation, dict) or navigation.get("verified") is not True or navigation.get("basis") != "writer_metadata" or navigation.get("zotero_item_key") != key:
                raise ValueError("The selected item does not match this note's verified source key.")
            status = self._status()
            item = self.client.item(scope, key)
            if not status["connected"] or item is None or _record_data(item).get("itemType") in {"attachment", "annotation", "note"}:
                raise ValueError("The selected source is not a bibliographic item in this library.")
            self._manual_sources[(data["workspace_id"], current["path"])] = {"server_id": self.client.server_id, "scope": scope, "item_key": key}
            source_scope = "library" if scope == "users/0" else scope
            data = {**data, "uri": f"zotero://select/{source_scope}/items/{key}"}
        source = parse_zotero_uri(data.get("uri"))
        if source is None:
            raise ValueError("Unsupported Zotero source URI.")
        scope = "users/0" if source.scope == "library" else _scope(source.scope)
        if source.action == "open-pdf":
            return self._open_pdf({**data, "scope": scope, "attachment_key": source.key,
                "page": source.page, "annotation_key": source.annotation_key})
        item = self.client.item(scope, source.key)
        if item is None:
            raise ValueError("The source item is unavailable in Zotero.")
        details = _record_data(item)
        if details.get("itemType") == "attachment" and details.get("contentType") == "application/pdf":
            return self._open_pdf({**data, "scope": scope, "attachment_key": source.key})
        notes = self._notes(data.get("workspace_id"), scope, source.key)
        children = self.client.children(scope, source.key, limit=64)
        if children.get("next_start") is not None:
            return {"scope": scope, "item": item, "attachments": children, "note_candidates": notes, "requires_attachment_selection": True}
        pdfs = [row for row in children["records"] if _record_data(row).get("itemType") == "attachment" and _record_data(row).get("contentType") == "application/pdf"]
        selected_note = next((row for row in notes if row["path"] == data.get("note_path")), notes[0] if len(notes) == 1 else None)
        selected_key = selected_note.get("attachment_key") if selected_note else None
        candidate = next((row for row in pdfs if _record_data(row).get("key") == selected_key), None)
        if candidate is None and len(pdfs) == 1:
            candidate = pdfs[0]
        if candidate:
            return self._open_pdf({**data, "scope": scope, "attachment_key": _record_data(candidate)["key"]})
        return {"scope": scope, "item": item, "attachments": children, "note_candidates": notes,
            "requires_attachment_selection": bool(pdfs), "warning": "Select the source PDF." if pdfs else "No local PDF attachment is available."}

    def _binding(self, data):
        status = self._status()
        if not status["connected"]:
            raise ValueError("Connect to Zotero before binding a source note.")
        scope, item_key = _scope(data.get("scope", "users/0")), _key(data.get("item_key"))
        item = self.client.item(scope, item_key)
        if item is None or _record_data(item).get("itemType") in {"attachment", "annotation", "note"}:
            raise ValueError("A source note must bind to a bibliographic parent item.")
        result = {"server_id": self.client.server_id, "library_type": "user" if scope == "users/0" else "group",
            "library_id": scope.split("/")[1], "item_key": item_key}
        if data.get("attachment_key"):
            pdf = self.client.pdf_file(scope, _key(data["attachment_key"]))
            if pdf.parent_item != item_key:
                raise ValueError("The PDF belongs to a different source item.")
            result["attachment_key"] = pdf.attachment_key
        return result

    def _migration_preview(self, data):
        binding = self._binding(data)
        notes = self._notes(data.get("workspace_id"), data.get("scope", "users/0"), binding["item_key"])
        current = self.documents.read(data["workspace_id"], data["note_path"])
        if current["path"] not in {row["path"] for row in notes}:
            raise ValueError("The note's source identity does not match the selected item.")
        if current["id"] != data.get("document_id"):
            raise ValueError("The document identity does not match the selected note.")
        preview = self.documents.preview_live_note(data["workspace_id"], current["id"], data["revision"], binding)
        token = secrets.token_urlsafe(24)
        self._previews = {key: value for key, value in self._previews.items() if value["expires"] > time.monotonic()}
        if len(self._previews) >= 32:
            self._previews.pop(next(iter(self._previews)))
        self._previews[token] = {"workspace_id": data["workspace_id"], "document_id": current["id"],
            "revision": data["revision"], "binding": binding, "expires": time.monotonic() + 900}
        return {**preview, "preview_token": token, "preview_identity": token, "binding": binding}

    def _migrate(self, data):
        self._require_writes()
        if data.get("promote") is not True:
            raise ValueError("Explicitly promote this workspace before attaching its active note.")
        preview = self._previews.get(data.get("preview_identity", data.get("preview_token")))
        if not preview or preview["expires"] <= time.monotonic() or any(data.get(key) != preview[key] for key in ("workspace_id", "document_id", "revision")):
            raise ValueError("Prepare and review a current migration preview first.")
        if preview["binding"]["server_id"] != self.client.server_id:
            raise ValueError("Zotero server changed; prepare a new migration preview.")
        return self.documents.migrate_live_note(preview["workspace_id"], preview["document_id"], preview["revision"],
            preview["binding"], registrar=_Registrar(self.client))

    def _annotation_write(self, data, *, delete=False):
        self._require_writes()
        scope, attachment_key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        pdf = self.client.pdf_file(scope, attachment_key)
        if self._pdf_baseline(pdf)[2]:
            raise ValueError("The PDF changed. Verify its annotations before writing.")
        drafts = data.get("annotations")
        if not isinstance(drafts, list) or not 1 <= len(drafts) <= 64 or any(not isinstance(row, dict) for row in drafts):
            raise ValueError("Save at most 64 explicitly identified annotations at once.")
        keys = [_key(row.get("id")) for row in drafts]
        if len(set(keys)) != len(keys):
            raise ValueError("A save cannot repeat an annotation identity.")
        operation = data.get("operation_id")
        if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", operation):
            raise ValueError("A stable operation identity is required.")
        queue = self._queue()
        request_hash = _sha(json.dumps([scope, attachment_key, delete, drafts], sort_keys=True, allow_nan=False))
        request_path = self._request_path(operation)
        prior_request = _read_json(request_path)
        if prior_request:
            if prior_request.get("request_hash") != request_hash:
                raise ValueError("This operation identity was already used for a different request.")
            if prior_request.get("result") is not None:
                return prior_request["result"]
            return {"annotations": [], "pending": [queue.lookup(scope, attachment_key, key) for key in keys if queue.lookup(scope, attachment_key, key)],
                "warning": "A prior operation was interrupted. Reconcile its saved state before continuing."}
        prepared = []
        for draft, key in zip(drafts, keys):
            version = _version(draft.get("version", 0))
            old = queue.lookup(scope, attachment_key, key)
            if old and old["state"] not in {"confirmed", "conflict", "discarded"}:
                return {"annotations": [], "pending": [old], "warning": "A saved operation requires reconciliation before another write."}
            remote = self.client.item(scope, key)
            existing = api_to_reader(remote, scope=scope, attachment_key=attachment_key, writable=True,
                known_internal=self._known_internal(scope, attachment_key, key)) if remote else None
            if delete:
                if any(field in draft and draft[field] != value for field, value in
                       (("scope", scope), ("attachment_key", attachment_key), ("parentItem", attachment_key), ("key", key))):
                    raise ValueError("Annotation deletion identity does not match its bound attachment.")
                if version == 0 or existing is None or existing["version"] != version or existing["readOnly"]:
                    raise AnnotationConflict("Only the current writable annotation version may be deleted.")
                payload = None
            else:
                if remote is not None and version == 0:
                    raise AnnotationConflict("This annotation key already exists.")
                payload = reader_to_api(draft, scope=scope, attachment_key=attachment_key, expected_version=version, existing=existing)
            prepared.append((draft, payload, old))
        request_record = {"request_hash": request_hash, "state": "prepared", "scope": scope,
            "attachment_key": attachment_key, "delete": delete, "drafts": drafts,
            "prepared_operations": [{"id": queue.operation_id(scope, attachment_key, draft["id"]), "scope": scope,
                "attachment_key": attachment_key, "annotation_key": draft["id"], "operation": "delete" if delete else "upsert",
                "base_version": draft.get("version", 0), "payload": payload} for draft, payload, _ in prepared]}
        _write_json(request_path, request_record)
        queued = []
        for draft, payload, old in prepared:
            record = queue.enqueue(scope=scope, attachment_key=attachment_key, annotation_key=draft["id"],
                operation="delete" if delete else "upsert", payload=payload, base_version=draft.get("version", 0),
                expected_revision=old["revision"] if old else None)
            queued.append(record)
        request_record["operation_ids"] = [record["id"] for record in queued]
        request_record["operation_refs"] = [self._operation_ref(record) for record in queued]
        _write_json(request_path, request_record)
        confirmed, pending = [], []
        for offset, ((draft, payload, old), record) in enumerate(zip(prepared, queued)):
            record = queue.mark_dispatched(record["id"], record["revision"])
            try:
                if delete:
                    self.client.delete_annotation(scope, attachment_key, draft["id"], expected_version=draft["version"], write_token=record["attempt_id"],
                        known_internal=self._known_internal(scope, attachment_key, draft["id"]))
                    if self.client.item(scope, draft["id"]) is not None:
                        raise AnnotationConflict("Zotero did not confirm annotation deletion.")
                    self._remember_internal(queue.confirm(record["id"], record["revision"], confirmed_absent=True))
                else:
                    saved = self.client.save_annotation(scope, attachment_key, payload, key=draft["id"],
                        expected_version=draft.get("version", 0), write_token=record["attempt_id"],
                        known_internal=self._known_internal(scope, attachment_key, draft["id"]))
                    self._remember_internal(queue.confirm(record["id"], record["revision"], remote_record=saved))
                    confirmed.append(api_to_reader(saved, scope=scope, attachment_key=attachment_key, writable=True,
                        known_internal=self._known_internal(scope, attachment_key, draft["id"])))
            except Exception as exc:
                current = queue.get(record["id"])
                if current["state"] == "dispatched":
                    current = queue.mark_uncertain(current["id"], current["revision"])
                pending.append(current)
                pending.extend(queue.get(row["id"]) for row in queued[offset + 1:])
                result = {"annotations": confirmed, "pending": pending, "warning": "The operation needs reconciliation; no retry was sent.", "code": getattr(exc, "code", "annotation_conflict")}
                _write_json(request_path, {**request_record, "result": result})
                return result
        result = {"annotations": confirmed, "pending": [], "deleted": keys if delete else []}
        _write_json(request_path, {**request_record, "state": "confirmed", "result": result})
        return result

    def _annotation_refresh(self, data, *, reconcile=False):
        scope, key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        status = self._status()
        pdf = self.client.pdf_file(scope, key)
        _, stamp, replaced = self._pdf_baseline(pdf)
        queue = self._queue()
        requested = data.get("operation_ids")
        requests, unresolved = None, []
        if reconcile and "request_ids" in data:
            if "operation_ids" in data:
                raise ValueError("Use saved request identities or operation identities, not both.")
            requests, unresolved = self._saved_requests(data, scope, key, queue)
            requested = list(dict.fromkeys(row["id"] for request in requests for row in request["records"]))
        if reconcile and requests is None and (not isinstance(requested, list) or not 1 <= len(requested) <= 64 or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in requested)):
            raise ValueError("Select at most 64 saved annotation operations to reconcile.")
        if reconcile and len(set(requested)) != len(requested):
            raise ValueError("A reconciliation cannot repeat an operation identity.")
        records = [queue.get(value) for value in requested] if reconcile else queue.pending()
        if reconcile and any(not row or (row["scope"], row["attachment_key"]) != (scope, key) for row in records):
            raise ValueError("The requested operations do not belong to this attachment.")
        for pending in records:
            if replaced or (pending["scope"], pending["attachment_key"]) != (scope, key) or pending["state"] not in {"dispatched", "uncertain"}:
                continue
            remote = self.client.item(scope, pending["annotation_key"])
            self._remember_internal(queue.reconcile(pending["id"], pending["revision"], remote_record=remote, confirmed_absent=remote is None,
                known_internal=self._known_internal(scope, key, pending["annotation_key"])))
        remaining = [row for row in queue.pending() if (row["scope"], row["attachment_key"]) == (scope, key)]
        result = {"annotations": self._annotations(scope, key, writable=status["writable"] and not replaced), "pending": remaining,
            "readOnly": not status["writable"] or replaced}
        if reconcile:
            if requests is not None:
                complete = [self._confirm_request(request, queue, event="reconciled") for request in requests]
                result["resolved"] = not replaced and not unresolved and all(complete)
                result["unresolved_request_ids"] = unresolved
                result["needs_admission_request_ids"] = [request["id"] for request in requests if request.get("needs_admission")]
                if unresolved:
                    result["warning"] = "Some saved request records are unavailable or do not match this attachment. Their outcome remains unresolved."
            else:
                result["resolved"] = not replaced and all(queue.get(value)["state"] in {"confirmed", "discarded"} for value in requested)
        if replaced:
            result["warning"] = "The PDF changed. Verify its annotations before editing or projecting them."
        if not reconcile and status["connected"] and not replaced:
            self._refresh_pdf_leases(pdf, stamp)
        return result

    def _validate_saved_change(self, scope, attachment_key, draft, *, delete):
        key, version = _key(draft.get("id")), _version(draft.get("version", 0))
        remote = self.client.item(scope, key)
        existing = api_to_reader(remote, scope=scope, attachment_key=attachment_key, writable=True,
            known_internal=self._known_internal(scope, attachment_key, key)) if remote else None
        if delete:
            if version == 0 or existing is None or existing["version"] != version or existing["readOnly"]:
                raise AnnotationConflict("A saved deletion no longer matches the current writable annotation version.")
            return None
        if version == 0 and existing is not None:
            raise AnnotationConflict("This saved creation key is now in use; it cannot be retried.")
        return reader_to_api(draft, scope=scope, attachment_key=attachment_key, expected_version=version, existing=existing)

    def _restore_request_admission(self, request, queue):
        """Explicit recovery for a crash after journaling drafts but before queuing all."""
        receipt = request["receipt"]
        scope, attachment_key = receipt["scope"], receipt["attachment_key"]
        prepared = []
        for draft in receipt["drafts"]:
            payload = self._validate_saved_change(scope, attachment_key, draft, delete=receipt["delete"])
            old = queue.lookup(scope, attachment_key, draft["id"])
            if old and old["state"] not in {"confirmed", "discarded"}:
                if (old["operation"], old["base_version"], old["payload"]) != (
                        "delete" if receipt["delete"] else "upsert", draft.get("version", 0), payload):
                    raise AnnotationConflict("Another pending operation owns this annotation. Its draft was preserved.")
                prepared.append((draft, payload, old, False))
            else:
                prepared.append((draft, payload, old, True))
        records = []
        for draft, payload, old, enqueue in prepared:
            row = queue.enqueue(scope=scope, attachment_key=attachment_key, annotation_key=draft["id"],
                operation="delete" if receipt["delete"] else "upsert", payload=payload, base_version=draft.get("version", 0),
                expected_revision=old["revision"] if old else None) if enqueue else old
            records.append(row)
        receipt["operation_ids"] = [row["id"] for row in records]
        receipt["operation_refs"] = [self._operation_ref(row) for row in records]
        _write_json(request["path"], receipt)

    def _annotation_retry(self, data):
        self._require_writes()
        if "request_ids" not in data:
            raise ValueError("Explicit retry requires the saved request identities.")
        before = self._annotation_refresh(data, reconcile=True)
        before["retried"] = 0
        if before.get("resolved") or before.get("unresolved_request_ids") or before.get("readOnly"):
            return before
        scope, attachment_key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        queue = self._queue()
        requests, unresolved = self._saved_requests(data, scope, attachment_key, queue)
        try:
            for request in requests:
                if request.get("needs_admission"):
                    self._restore_request_admission(request, queue)
            requests, unresolved = self._saved_requests(data, scope, attachment_key, queue)
            if unresolved:
                return {**before, "resolved": False, "unresolved_request_ids": unresolved}
            selected = {row["id"]: row for request in requests for row in request["records"] if row["state"] not in {"confirmed", "discarded"}}
            # Validate every original version before dispatching any retry.
            for row in selected.values():
                if row["state"] != "queued":
                    raise AnnotationConflict("An uncertain or conflicting operation cannot be retried. Reconcile or explicitly discard it first.")
                draft = {"id": row["annotation_key"], "version": row["base_version"]} if row["operation"] == "delete" else api_to_reader(
                    row["payload"], scope=scope, attachment_key=attachment_key, writable=True, known_internal=True)
                payload = self._validate_saved_change(scope, attachment_key, draft, delete=row["operation"] == "delete")
                if payload != row["payload"]:
                    raise AnnotationConflict("The saved annotation contract changed; preserve and review its draft before retrying.")
        except (ValueError, ZoteroWorkspaceError) as exc:
            return {**before, "resolved": False, "pending": [row for row in queue.pending() if (row["scope"], row["attachment_key"]) == (scope, attachment_key)],
                "warning": str(exc), "retry_blocked": True}
        retried = 0
        for row in selected.values():
            # mark_dispatched is a revision CAS; a concurrent request cannot send
            # this queued operation twice. Each explicit attempt gets one token.
            dispatched = queue.mark_dispatched(row["id"], row["revision"])
            retried += 1
            try:
                if row["operation"] == "delete":
                    self.client.delete_annotation(scope, attachment_key, row["annotation_key"], expected_version=row["base_version"],
                        write_token=dispatched["attempt_id"], known_internal=self._known_internal(scope, attachment_key, row["annotation_key"]))
                    if self.client.item(scope, row["annotation_key"]) is not None:
                        raise AnnotationConflict("Deletion was not confirmed.")
                    settled = queue.confirm(dispatched["id"], dispatched["revision"], confirmed_absent=True)
                else:
                    saved = self.client.save_annotation(scope, attachment_key, row["payload"], key=row["annotation_key"],
                        expected_version=row["base_version"], write_token=dispatched["attempt_id"],
                        known_internal=self._known_internal(scope, attachment_key, row["annotation_key"]))
                    settled = queue.confirm(dispatched["id"], dispatched["revision"], remote_record=saved,
                        known_internal=self._known_internal(scope, attachment_key, row["annotation_key"]))
                self._remember_internal(settled)
            except Exception:
                current = queue.get(dispatched["id"])
                if current["state"] == "dispatched":
                    queue.mark_uncertain(current["id"], current["revision"])
                break
        for request in requests:
            self._confirm_request(request, queue, event="explicit_retry")
        try:
            result = self._annotation_refresh(data, reconcile=True)
        except (ValueError, ZoteroWorkspaceError, OSError):
            result = {**before, "resolved": False, "pending": [row for row in queue.pending() if (row["scope"], row["attachment_key"]) == (scope, attachment_key)],
                "warning": "Retry outcome needs readback. Saved drafts remain in the recovery queue; no further retry was sent."}
        return {**result, "retried": retried}

    def _annotation_discard(self, data):
        if data.get("discard") is not True or "request_ids" not in data:
            raise ValueError("Explicitly confirm discarding the saved request changes.")
        before = self._annotation_refresh(data, reconcile=True)
        if before.get("unresolved_request_ids") or before.get("readOnly") and not self._status()["connected"]:
            return {**before, "discarded": False}
        scope, attachment_key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        queue = self._queue()
        requests, unresolved = self._saved_requests(data, scope, attachment_key, queue)
        records = {row["id"]: row for request in requests for row in request["records"]}
        for request in requests:
            if request.get("needs_admission"):
                # No HTTP dispatch follows an incomplete admission receipt.
                # Do not clear a different request's operation on the same key.
                prepared = request["receipt"].get("prepared_operations")
                if not isinstance(prepared, list) or len(prepared) != len(request["receipt"]["drafts"]):
                    return {**before, "resolved": False, "discarded": False, "warning": "The interrupted admission lacks verified operation records; its drafts remain preserved."}
                for draft, expected in zip(request["receipt"]["drafts"], prepared):
                    if (not isinstance(expected, dict) or (expected.get("scope"), expected.get("attachment_key"), expected.get("annotation_key"), expected.get("base_version"), expected.get("operation")) !=
                            (scope, attachment_key, draft["id"], draft.get("version", 0), "delete" if request["receipt"]["delete"] else "upsert")):
                        raise ValueError("The saved admission identities do not match their request.")
                    row = queue.lookup(scope, attachment_key, draft["id"])
                    if row and self._operation_ref(row) != self._operation_ref(expected):
                        continue
                    if row and row["state"] in {"dispatched", "uncertain"}:
                        return {**before, "resolved": False, "discarded": False, "warning": "An uncertain dispatched operation must be reconciled before discarding."}
                    if row and row["state"] in {"queued", "conflict"}:
                        records[row["id"]] = row
        if any(row["state"] in {"dispatched", "uncertain"} for row in records.values()):
            return {**before, "resolved": False, "discarded": False, "warning": "An uncertain dispatched operation must be reconciled before discarding."}
        for row in records.values():
            if row["state"] in {"queued", "conflict"}:
                queue.discard(row["id"], row["revision"])
        for request in requests:
            if request.get("needs_admission"):
                request["receipt"]["state"] = "discarded"
                request["receipt"]["events"] = request["receipt"].get("events", []) + [{"action": "explicit_discard_before_admission"}]
                _write_json(request["path"], request["receipt"])
            else:
                self._confirm_request(request, queue, event="explicit_discard")
        return {**self._annotation_refresh(data, reconcile=True), "discarded": True}

    def annotation_snapshot(self, workspace_path, note_path):
        with self._lock:
            return read_annotation_snapshot(self.private_root, workspace_path, note_path)

    def _project(self, data):
        if data.get("dirty") is True:
            return {"deferred": True, "reason": "Save or discard the note draft before projecting annotations."}
        workspace_id, note_path = data["workspace_id"], data["note_path"]
        root, root_identity, _ = _workspace_identity(self.documents.workspace_path(workspace_id), note_path)
        receipt_path = _snapshot_path(self.private_root, root, root_identity, note_path)
        with _receipt_guard(receipt_path):
            return self._project_locked(data, root, root_identity, receipt_path)

    def _project_locked(self, data, root, root_identity, receipt_path):
        workspace_id, note_path = data["workspace_id"], data["note_path"]
        current = self.documents.read(workspace_id, note_path)
        if current["id"] != data.get("document_id"):
            raise ValueError("The requested document is not the selected note.")
        if current["revision"] != data.get("revision"):
            raise DocumentConflict(current)
        if not current.get("live_note") or current.get("live_note_phase") != "archived" or current.get("live_identity_valid") is not True:
            raise ValueError("Annotations can be projected only into a verified active shared note.")
        scope, key = _scope(data.get("scope", "users/0")), _key(data.get("attachment_key"))
        pdf = self.client.pdf_file(scope, key)
        identity, _, replaced = self._pdf_baseline(pdf)
        binding = current["live_binding"]
        binding_scope = "users/0" if binding["library_type"] == "user" and binding["library_id"] == "0" else f"groups/{binding['library_id']}"
        if (binding["server_id"], binding_scope, binding["item_key"]) != (pdf.server_id, scope, pdf.parent_item) or replaced:
            raise ValueError("The PDF no longer matches this active source note.")
        previous = self.annotation_snapshot(root, note_path)
        groups = previous["groups"] if previous else []
        groups = [group for group in groups if (group["scope"], group["attachment_key"]) != (scope, key)]
        attachment = self.client.item(scope, key)
        groups.append({"scope": scope, "attachment_key": key, "title": _record_data(attachment).get("title") or "Source PDF",
            "confirmed": True, "pdf_hash": identity, "expected_pdf_hash": identity,
            "annotations": self._annotations(scope, key)})
        projected = project_annotations(current["text"], groups, expected_hash=previous["block_hash"] if previous else None)
        raw = (b"\xef\xbb\xbf" if current.get("bom") else b"") + projected["text"].encode()
        receipt = {"schema_version": 1, "workspace": str(root), "workspace_identity": root_identity, "path": note_path,
            "block_hash": projected["block_hash"], "groups": groups, "state": "prepared", "projected_revision": _sha(raw)}
        if previous:
            receipt["previous"] = {"schema_version": 1, "workspace": str(root), "workspace_identity": root_identity,
                "path": note_path, "state": "confirmed", **previous}
        _private_directory(receipt_path.parent.parent)
        _write_json(receipt_path, receipt)
        document = self.documents.save(workspace_id, current["id"], current["revision"], projected["text"])
        document = self.documents.confirm_annotation_projection(workspace_id, document["id"], document["revision"], projected["block_hash"])
        receipt["state"] = "confirmed"
        _write_json(receipt_path, receipt)
        return {"document": document, "block_hash": projected["block_hash"], "annotation_count": len(projected["annotations"])}

    def handle(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a bounded request object.")
        with self._lock:
            if action == "status":
                return self._status()
            if action == "libraries":
                status = self._status()
                return {"libraries": [{**row, "writable": status["writable"]} for row in self.client.libraries()]}
            scope = _scope(data.get("scope", "users/0"))
            if action in {"collections", "items", "children"}:
                options = {"start": data.get("start", 0), "limit": data.get("limit", 32)}
                if action == "collections":
                    return self.client.collections(scope, **options)
                if action == "items":
                    page = self.client.items_page(scope, collection_key=data.get("collection_key"), query=data.get("query", ""), **options)
                else:
                    page = self.client.children(scope, _key(data.get("parent_key")), **options)
                return self._enrich(page, data)
            if action == "authorize":
                self._require_writes(authorized=False)
                return self.client.authorize()
            if action in {"item-update", "collection-create", "collection-rename"}:
                self._require_writes()
                token = data.get("write_token")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,32}", token):
                    raise ValueError("A stable write token is required.")
                if action == "item-update":
                    return {"record": self.client.update_item(scope, _key(data.get("key")), data.get("changes"), expected_version=_version(data.get("expected_version")), write_token=token)}
                if action == "collection-create":
                    return {"record": self.client.create_collection(scope, key=_key(data.get("key")), name=data.get("name"), parent_key=data.get("parent_key"), write_token=token)}
                return {"record": self.client.rename_collection(scope, _key(data.get("key")), data.get("name"), expected_version=_version(data.get("expected_version")), write_token=token)}
            if action == "open-pdf":
                return self._open_pdf(data)
            if action == "open-source":
                return self._open_source(data)
            if action == "open-external":
                source = parse_zotero_uri(data.get("uri"))
                if source is None:
                    raise ValueError("Unsupported Zotero source URI.")
                return {"opened": bool(webbrowser.open(source.uri)), "uri": source.uri}
            if action == "reader-close":
                for token, entry in list(self._pdfs.items()):
                    if self._reader_id(entry["pdf"]) == data.get("reader_id"):
                        self._pdfs.pop(token)
                return {"closed": True}
            if action == "annotation-save":
                return self._annotation_write(data)
            if action == "annotation-delete":
                return self._annotation_write(data, delete=True)
            if action == "annotation-refresh":
                return self._annotation_refresh(data)
            if action == "annotation-reconcile":
                return self._annotation_refresh(data, reconcile=True)
            if action == "annotation-retry":
                return self._annotation_retry(data)
            if action == "annotation-discard":
                return self._annotation_discard(data)
            if action == "annotation-project":
                return self._project(data)
            if action == "migration-preview":
                return self._migration_preview(data)
            if action == "migrate":
                return self._migrate(data)
            if action == "migration-status":
                document = self.documents.read(data["workspace_id"], data["note_path"])
                return {key: document.get(key) for key in ("path", "revision", "live_note", "live_note_phase", "live_identity_valid", "live_binding")}
            raise ValueError("Unsupported Zotero workspace action.")
