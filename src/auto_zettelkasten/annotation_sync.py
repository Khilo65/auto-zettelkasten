"""Bounded annotation projection for explicitly shared Markdown notes.

The worker reads Zotero and writes only the selected workspace's completed
shared notes. It never retries an annotation mutation. Open editors own their
projection; closed notes can be updated after the frontend registers its draft
inventory. Dirty drafts and work queues survive a desktop restart privately.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time

from .documents import DocumentConflict, DocumentError, MAX_FILE_BYTES, _LOCK as DOCUMENT_LOCK, _identity
from .zotero_annotations import AnnotationConflict
from .zotero_workspace import ZoteroWorkspaceError
from .zotero_workspace_service import _private_directory, _read_json, _receipt_guard, _write_json

_SCHEMA = 1
_MAX_JOBS = 20000
_MAX_BUFFERS = 256
_MAX_DRAFTS = 128
_MAX_DRAFT_BYTES = 8 * 1024 * 1024
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STATES = {"queued", "checking", "waiting_for_draft", "ready", "blocked", "waiting_for_zotero", "failed", "current", "paused"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _path(value):
    if not isinstance(value, str) or len(value.encode()) > 2048 or "\\" in value or "\x00" in value:
        raise ValueError("Annotation synchronization requires a bounded Markdown path.")
    parts = PurePosixPath(value)
    if parts.is_absolute() or not parts.parts or any(part in {".", ".."} or part.startswith(".") for part in parts.parts) or parts.suffix.lower() != ".md" or str(parts) != value:
        raise ValueError("Annotation synchronization requires a direct relative Markdown path.")
    return value


class AnnotationSyncService:
    POLL_SECONDS = 30
    BATCH_SIZE = 8
    BUFFER_LEASE_SECONDS = 60

    def __init__(self, documents, zotero_service, private_root, *, poll_seconds=30, autostart=True):
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0 < poll_seconds <= 3600:
            raise ValueError("Annotation polling interval must be positive and bounded.")
        self.documents = documents
        self.zotero = zotero_service
        self.root = _private_directory(Path(private_root))
        self.poll_seconds = float(poll_seconds)
        self._lock = threading.RLock()
        self._projection_lock = threading.RLock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._workspace_id = None
        self._workspace_identity = None
        self._state_path = None
        self._epoch = 0
        self._started = False
        self._sessions = {}
        self._last_inventory = 0.0
        self._inventory_error = ""
        self._binding_cache = None
        self._binding_scanned = 0.0
        self._discovery_cursor = 0
        self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._run, name="annotation-note-sync", daemon=True)
            self._thread.start()

    def _workspace(self, workspace_id):
        with DOCUMENT_LOCK:
            root = Path(self.documents.workspace_path(workspace_id))
            identity = _identity(root.lstat())
            if self.documents._root_identity != identity:
                raise DocumentError("The annotation workspace changed. Reopen it.")
            return {"path": str(root), "identity": list(identity)}

    def _empty(self):
        return {"schema_version": _SCHEMA, "workspace": self._workspace_identity,
                "jobs": {}, "drafts": {}}

    def _state(self):
        value = _read_json(self._state_path) or self._empty()
        if (value.get("schema_version") != _SCHEMA or value.get("workspace") != self._workspace_identity
                or not isinstance(value.get("jobs"), dict) or len(value["jobs"]) > _MAX_JOBS
                or not isinstance(value.get("drafts"), dict) or len(value["drafts"]) > _MAX_DRAFTS):
            raise ValueError("Annotation synchronization state failed validation.")
        for key, job in value["jobs"].items():
            if (not isinstance(job, dict) or job.get("id") != key or job.get("state") not in _STATES
                    or _path(job.get("note_path")) != job["note_path"]
                    or key != self._job_id(job["note_path"], job["binding"], job["scope"], job["attachment_key"])):
                raise ValueError("Annotation synchronization job identity is invalid.")
        total = 0
        for key, draft in value["drafts"].items():
            if (not isinstance(draft, dict) or _path(draft.get("note_path")) != draft["note_path"]
                    or not isinstance(draft.get("revision"), str) or len(draft["revision"]) > 256
                    or draft.get("text") is not None and (not isinstance(draft["text"], str) or len(draft["text"].encode()) > MAX_FILE_BYTES)
                    or key != _digest([draft["note_path"], draft["revision"], draft.get("text")])):
                raise ValueError("Annotation draft recovery identity is invalid.")
            total += len((draft.get("text") or "").encode())
        if total > _MAX_DRAFT_BYTES:
            raise ValueError("Retained annotation-note drafts exceed the private storage bound.")
        return value

    def _change(self, action):
        # The caller holds _lock. The on-disk lock also coordinates a second app
        # using this private state; no network or document call runs inside it.
        with _receipt_guard(self._state_path):
            value = self._state()
            before = copy.deepcopy(value)
            result = action(value)
            if value != before or not self._state_path.exists():
                _write_json(self._state_path, value)
            return result

    def select_workspace(self, workspace_id):
        identity = self._workspace(workspace_id)
        with self._projection_lock, self._lock:
            if (self._workspace_id, self._workspace_identity) != (workspace_id, identity):
                self._workspace_id, self._workspace_identity = workspace_id, identity
                self._state_path = self.root / _digest(identity) / "state.json"
                self._epoch += 1
                self._started = False
                self._sessions.clear()
                self._last_inventory = 0
                self._inventory_error = ""
                self._binding_cache = None
                self._discovery_cursor = 0
                # An incomplete prior run must re-read Zotero before continuing.
                def reset(value):
                    for job in value["jobs"].values():
                        job.update(state="queued", reason="Checking saved annotations after reopening.", due_at=0, ready=False)
                self._change(reset)
            return self._status_locked()

    def register_buffers(self, workspace_id, session_id, buffers, *, release_paths=None):
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise ValueError("A bounded editor session identity is required.")
        if not isinstance(buffers, list) or len(buffers) > _MAX_BUFFERS:
            raise ValueError("Register at most 256 open or recoverable note buffers.")
        release_paths = [] if release_paths is None else release_paths
        if not isinstance(release_paths, list) or len(release_paths) > _MAX_BUFFERS:
            raise ValueError("Release at most 256 explicitly resolved note drafts.")
        release = {_path(path) for path in release_paths}
        rows = {}
        for row in buffers:
            if not isinstance(row, dict):
                raise ValueError("Invalid annotation note-buffer state.")
            path = _path(row.get("path", row.get("note_path")))
            revision = row.get("revision")
            if (path in rows or not isinstance(revision, str) or not 0 < len(revision) <= 256
                    or type(row.get("dirty", False)) is not bool or type(row.get("recoverable", False)) is not bool):
                raise ValueError("Note buffers require distinct paths, revisions and explicit draft flags.")
            dirty = row.get("dirty", False) or row.get("recoverable", False)
            text = row.get("text") if dirty else None
            if text is not None and (not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES):
                raise ValueError("Retained note drafts exceed the document size limit.")
            rows[path] = {"note_path": path, "revision": revision, "dirty": dirty,
                          "recoverable": row.get("recoverable", False), "text": text}
        self.select_workspace(workspace_id)
        with self._projection_lock, self._lock:
            now = time.time()
            previous = self._sessions.get(session_id, {}).get("buffers", {})
            changed = set(previous) ^ set(rows)
            changed.update(path for path in rows if previous.get(path) != rows[path])
            other_dirty = {path for sid, session in self._sessions.items() if sid != session_id and now - session["updated_at"] < self.BUFFER_LEASE_SECONDS
                           for path, row in session["buffers"].items() if row["dirty"]}
            if release & other_dirty:
                raise ValueError("Another open editor still owns a draft for this note.")
            def update(value):
                for token, draft in list(value["drafts"].items()):
                    clean = draft["note_path"] in rows and not rows[draft["note_path"]]["dirty"]
                    if draft["note_path"] in release or clean and draft.get("session_id") == session_id:
                        del value["drafts"][token]
                for path, row in rows.items():
                    if row["dirty"]:
                        token = _digest([path, row["revision"], row["text"]])
                        retained = value["drafts"].get(token)
                        if retained and retained.get("session_id") == session_id:
                            continue
                        # Checkpoint replaces only this session's prior draft;
                        # another session's differing draft remains recoverable.
                        for old_key, draft in list(value["drafts"].items()):
                            if draft["note_path"] == path and draft.get("session_id") == session_id:
                                del value["drafts"][old_key]
                        value["drafts"][token] = {**row, "session_id": session_id, "updated_at": now}
                if len(value["drafts"]) > _MAX_DRAFTS or sum(len((d.get("text") or "").encode()) for d in value["drafts"].values()) > _MAX_DRAFT_BYTES:
                    raise ValueError("Retained note drafts exceed their private storage limit. Save a draft before continuing.")
                for job in value["jobs"].values():
                    if job["note_path"] in release or job["note_path"] in changed:
                        job["due_at"] = 0
            self._change(update)
            self._sessions[session_id] = {"buffers": rows, "updated_at": now}
            self._started = True
            self._event.set()
            return self._status_locked()

    def _job_id(self, path, binding, scope, attachment_key):
        return _digest([self._workspace_identity, path, binding, scope, attachment_key])

    def _open_paths(self):
        now = time.time()
        return {path for session in self._sessions.values() if now - session["updated_at"] < self.BUFFER_LEASE_SECONDS
                for path in session["buffers"]}

    def status(self, workspace_id):
        identity = self._workspace(workspace_id)
        with self._lock:
            if (self._workspace_id, self._workspace_identity) == (workspace_id, identity):
                return self._status_locked()
        return self.select_workspace(workspace_id)

    def _status_locked(self):
        with _receipt_guard(self._state_path):
            value = self._state()
        jobs = []
        for job in sorted(value["jobs"].values(), key=lambda row: (row["note_path"], row["attachment_key"])):
            item = {key: copy.deepcopy(job[key]) for key in ("id", "note_path", "scope", "attachment_key", "server_id", "state", "reason", "document_id", "revision", "ready", "updated_at") if key in job}
            jobs.append(item)
        counts = {state: sum(job["state"] == state for job in jobs) for state in sorted(_STATES)}
        drafts = [copy.deepcopy(row) for row in value["drafts"].values()]
        for draft in drafts:
            draft["conflict"] = sum(other["note_path"] == draft["note_path"] for other in drafts) > 1
        return {"enabled": True, "started": self._started, "workspace_id": self._workspace_id,
                "jobs": jobs, "counts": counts, "drafts": drafts, "error": self._inventory_error,
                "poll_seconds": self.poll_seconds}

    def enqueue_workspace(self, workspace_id):
        with self._lock:
            if self._workspace_id != workspace_id:
                return
            self._last_inventory = 0
            self._binding_cache = None
            self._discovery_cursor = 0
            def queue(value):
                for job in value["jobs"].values():
                    job.update(state="queued", ready=False, due_at=0)
                    job["generation"] = job.get("generation", 0) + 1
            self._change(queue)
            self._event.set()

    def enqueue_attachment(self, scope, attachment_key):
        with self._lock:
            if not self._state_path:
                return
            def queue(value):
                for job in value["jobs"].values():
                    if (job["scope"], job["attachment_key"]) == (scope, attachment_key):
                        job.update(state="queued", ready=False, due_at=0, priority=True)
                        job["generation"] = job.get("generation", 0) + 1
            self._change(queue)
            self._event.set()

    def projected(self, workspace_id, note_path, scope, attachment_key, revision=None):
        with self._lock:
            if self._workspace_id != workspace_id:
                return
            def done(value):
                for job in value["jobs"].values():
                    if (job["note_path"], job["scope"], job["attachment_key"]) == (note_path, scope, attachment_key):
                        job.update(state="current", ready=False, reason="Annotations are saved in Zotero and this note.",
                                   due_at=time.time() + self.poll_seconds, updated_at=time.time())
                        if job.get("checked_hash"):
                            job["snapshot_hash"] = job["checked_hash"]
                        if revision:
                            job["revision"] = revision
            self._change(done)

    def _bindings(self, workspace_id):
        with DOCUMENT_LOCK:
            self.documents._workspace(workspace_id)
            root = Path(self.documents.workspace_path(workspace_id))
            if not self.documents._live_notes:
                return []
            return self.documents._live_notes.list_active(root, self.documents._root_identity)

    def _pdfs(self, binding):
        scope = "users/0" if binding["library_type"] == "user" and binding["library_id"] == "0" else "groups/" + binding["library_id"]
        keys = {binding["attachment_key"]} if binding.get("attachment_key") else set()
        # Include secondary PDFs only after a complete version-consistent read.
        discovered, start, version = set(), 0, None
        for _ in range(8):
            page = self.zotero.handle("children", {"scope": scope, "parent_key": binding["item_key"], "start": start, "limit": 64})
            if not isinstance(page, dict) or not isinstance(page.get("records"), list):
                raise ValueError("The source attachment inventory is incomplete.")
            if start and page.get("library_version") != version:
                raise ValueError("The source attachments changed during synchronization.")
            version = page.get("library_version")
            for record in page["records"]:
                data = record.get("data", record)
                if data.get("parentItem") != binding["item_key"]:
                    raise ValueError("An attachment belongs to a different source.")
                if data.get("itemType") == "attachment" and data.get("contentType") == "application/pdf":
                    key = data.get("key", record.get("key"))
                    if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key):
                        raise ValueError("Invalid source PDF identity.")
                    discovered.add(key)
            following = page.get("next_start")
            if following is None:
                return scope, sorted(keys | discovered)
            if type(following) is not int or following <= start:
                raise ValueError("The source attachment inventory did not advance.")
            start = following
        raise ValueError("The source attachment inventory exceeds its limit.")

    def _discover(self, workspace_id, epoch):
        with self._lock:
            cached = self._binding_cache
            refresh = cached is None or time.monotonic() - self._binding_scanned >= 300
        if refresh:
            bindings = self._bindings(workspace_id)
            with self._lock:
                if epoch != self._epoch:
                    return
                self._binding_cache = bindings
                self._binding_scanned = time.monotonic()
                self._discovery_cursor = 0
        else:
            bindings = cached

        def enroll(rows, *, complete_bindings=False):
            with self._lock:
                if epoch != self._epoch:
                    return
                def update(value):
                    active = {(row["path"], _digest(row["binding"])) for row in bindings}
                    for path, binding, scope, key in rows:
                        token = self._job_id(path, binding, scope, key)
                        if token not in value["jobs"]:
                            value["jobs"][token] = {"id": token, "note_path": path, "binding": binding,
                                "scope": scope, "attachment_key": key, "server_id": binding["server_id"],
                                "state": "queued", "reason": "Checking annotations.", "ready": False,
                                "due_at": 0, "generation": 0, "updated_at": time.time()}
                    if len(value["jobs"]) > _MAX_JOBS:
                        raise ValueError("Annotation synchronization job inventory exceeds its limit.")
                    if complete_bindings:
                        for job in value["jobs"].values():
                            # Losing a source binding detaches its job. A missing
                            # PDF never deletes a note or its saved annotation text.
                            job["detached"] = (job["note_path"], _digest(job["binding"])) not in active
                self._change(update)

        # The originating PDF is already part of verified migration identity.
        # Enroll it without waiting for a library-sized attachment discovery pass.
        known = []
        for record in bindings:
            binding, path = record["binding"], _path(record["path"])
            scope = "users/0" if binding["library_type"] == "user" and binding["library_id"] == "0" else "groups/" + binding["library_id"]
            if binding.get("attachment_key"):
                known.append((path, binding, scope, binding["attachment_key"]))
        enroll(known, complete_bindings=True)
        status = self.zotero.handle("status", {})
        if not status.get("connected", status.get("available")) or not status.get("server_id"):
            raise ValueError("Zotero is unavailable. Saved annotation sections are preserved.")
        eligible = [row for row in bindings if row["binding"]["server_id"] == status["server_id"]]
        with self._lock:
            if epoch != self._epoch:
                return
            start = min(self._discovery_cursor, len(eligible))
        selected = eligible[start:start + self.BATCH_SIZE]
        rows, error = [], ""
        for record in selected:
            try:
                scope, keys = self._pdfs(record["binding"])
                rows.extend((record["path"], record["binding"], scope, key) for key in keys)
            except (ValueError, RuntimeError, OSError) as exc:
                error = error or str(exc)
        enroll(rows)
        with self._lock:
            if epoch != self._epoch:
                return
            following = start + len(selected)
            more = following < len(eligible)
            self._discovery_cursor = following if more else 0
            self._last_inventory = 0 if more else time.monotonic()
            self._inventory_error = error
            if more:
                self._event.set()

    def _update_job(self, token, epoch, **changes):
        with self._lock:
            if epoch != self._epoch:
                return
            def update(value):
                if token in value["jobs"]:
                    value["jobs"][token].update(changes, updated_at=time.time())
            self._change(update)

    @staticmethod
    def _snapshot_hash(annotations):
        # Transport versions, editability and annotation color do not change
        # the managed note text. A color-only edit must not rewrite Markdown.
        fields = ("id", "type", "position", "text", "comment", "pageLabel", "sortIndex", "tags")
        return _digest(sorted([{key: row.get(key) for key in fields} for row in annotations], key=lambda row: row["id"]))

    def _process(self, workspace_id, epoch, job):
        token = job["id"]
        try:
            connection = self.zotero.handle("status", {})
            if not connection.get("connected", connection.get("available")):
                raise ZoteroWorkspaceError("Zotero is unavailable. The existing note section is preserved.", code="unavailable")
            if connection.get("server_id") != job["server_id"]:
                raise ZoteroWorkspaceError("This shared note belongs to a different Zotero connection.", code="server_changed")
            current = self.documents.read(workspace_id, job["note_path"])
            if (not current.get("live_note") or current.get("live_note_phase") != "archived"
                    or current.get("live_identity_valid") is not True or current.get("live_binding") != job["binding"]):
                raise ValueError("The note is no longer a verified active shared note.")
            data = {"workspace_id": workspace_id, "note_path": job["note_path"], "document_id": current["id"],
                    "revision": current["revision"], "scope": job["scope"], "attachment_key": job["attachment_key"]}
            self._update_job(token, epoch, document_id=current["id"], revision=current["revision"], ready=False)
            projection = self.zotero.handle("annotation-projection-status", data)
            if projection.get("paused"):
                self._update_job(token, epoch, state="paused", reason="Automatic annotation updates are paused for this note.", due_at=time.time() + self.poll_seconds)
                return
            if projection.get("state") == "conflict":
                self._update_job(token, epoch, state="blocked", reason="The annotation section was changed. Review it before updating.", due_at=time.time() + self.poll_seconds)
                return
            result = self.zotero.handle("annotation-refresh", data)
            if result.get("pending") or result.get("pending_receipt_ids"):
                self._update_job(token, epoch, state="waiting_for_zotero", reason="Resolve the retained annotation changes before updating this note.", due_at=time.time() + self.poll_seconds)
                return
            if result.get("warning"):
                raise ValueError(result["warning"])
            annotations = result.get("annotations")
            if not isinstance(annotations, list):
                raise ValueError("A complete annotation snapshot is unavailable.")
            checked = self._snapshot_hash(annotations)
            self._update_job(token, epoch, checked_hash=checked)
            with self._projection_lock:
                with self._lock:
                    if epoch != self._epoch or not self._started:
                        return
                    with _receipt_guard(self._state_path):
                        value = self._state()
                    latest = value["jobs"].get(token)
                    if not latest:
                        return
                    blocked = any(row["note_path"] == job["note_path"] for row in value["drafts"].values())
                    opened = job["note_path"] in self._open_paths()
                if checked == latest.get("snapshot_hash") and projection.get("state") == "current":
                    self._update_job(token, epoch, state="current", reason="Annotations are saved in Zotero and this note.", due_at=time.time() + self.poll_seconds)
                elif blocked:
                    self._update_job(token, epoch, state="waiting_for_draft", reason="Saved in Zotero. Save or discard the note draft to update its annotation section.", due_at=time.time() + self.poll_seconds)
                elif opened:
                    self._update_job(token, epoch, state="ready", ready=True, reason="Saved in Zotero. The open editor can apply the note update.", due_at=time.time() + self.poll_seconds)
                else:
                    # Never hold the sync lock across a service call: confirmed
                    # projection calls projected(), and native writes enqueue jobs.
                    result = self.zotero.handle("annotation-project", data)
                    if result.get("deferred"):
                        self._update_job(token, epoch, state="waiting_for_draft", reason=result.get("reason", "The note update is waiting."), due_at=time.time() + self.poll_seconds)
                    else:
                        document = result.get("document", current)
                        self._update_job(token, epoch, state="current", snapshot_hash=checked, ready=False, revision=document.get("revision", current["revision"]),
                                         reason="Annotations are saved in Zotero and this note.", due_at=time.time() + self.poll_seconds)
        except (DocumentConflict, AnnotationConflict) as exc:
            self._update_job(token, epoch, state="blocked", ready=False, reason=str(exc), due_at=time.time() + self.poll_seconds)
        except (ValueError, RuntimeError, OSError) as exc:
            code = getattr(exc, "code", "")
            waiting = code in {"unavailable", "authorization_required", "access_denied", "server_changed"}
            self._update_job(token, epoch, state="waiting_for_zotero" if waiting else "failed", ready=False,
                             reason=str(exc), due_at=time.time() + self.poll_seconds)

    def _due_jobs(self, epoch):
        with self._lock:
            if epoch != self._epoch:
                return []
            with _receipt_guard(self._state_path):
                value = self._state()
            return sorted((copy.deepcopy(job) for job in value["jobs"].values()
                           if not job.get("detached") and job.get("due_at", 0) <= time.time()),
                          key=lambda row: (not row.get("priority", False), row.get("due_at", 0), row["note_path"], row["attachment_key"]))[:self.BATCH_SIZE]

    def run_once(self):
        """One bounded sweep; public for deterministic offline verification."""
        with self._lock:
            if not self._started or not self._workspace_id or self._stop.is_set():
                return 0
            workspace_id, epoch = self._workspace_id, self._epoch
            discover = self._last_inventory == 0 or time.monotonic() - self._last_inventory >= self.poll_seconds
        # A newly confirmed local write must not wait behind every library PDF.
        jobs = self._due_jobs(epoch)
        for job in jobs:
            if self._stop.is_set():
                break
            self._process(workspace_id, epoch, job)
        if discover:
            try:
                self._discover(workspace_id, epoch)
            except (ValueError, RuntimeError, OSError) as exc:
                with self._lock:
                    if epoch == self._epoch:
                        self._inventory_error = str(exc)
                        self._last_inventory = time.monotonic()
            if not jobs:
                jobs = self._due_jobs(epoch)
                for job in jobs:
                    if self._stop.is_set():
                        break
                    self._process(workspace_id, epoch, job)
        return len(jobs)

    def _run(self):
        while not self._stop.is_set():
            self._event.wait(1)
            self._event.clear()
            try:
                count = self.run_once()
                if count == self.BATCH_SIZE:
                    self._event.set()
            except (ValueError, RuntimeError, OSError) as exc:
                with self._lock:
                    self._inventory_error = str(exc)

    def close(self):
        with self._lock:
            self._started = False
            self._epoch += 1
        self._stop.set()
        self._event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
