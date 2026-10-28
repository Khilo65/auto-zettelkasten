"""Opt-in, recoverable shared Markdown. No Zotero access is performed implicitly.

The global private journal owns source identity; a workspace copy is detached
unless explicitly migrated. A registrar must find/register/read back the exact
linked attachment before the former overlay can be archived.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

import yaml

from .document_overrides import Overrides, personal_metadata
from .documents import (DocumentConflict, DocumentError, MAX_FILE_BYTES, _decode,
                        _frontmatter, _identity, _linked, _stamp)

_MAX_JOURNAL = 6 * MAX_FILE_BYTES
_EDITABLE = {"title", "tags", "tag", "aliases", "alias"}
_IDENTITY = {"note_id", "source_id", "source_ids", "zotero_item_key", "zotero_item_uri",
             "attachment_key", "source_pdf_uri", "source_file", "machine_preservation_hash",
             "inspected_content_hash", "reader_provider", "reader_model", "engine_version",
             "artifact_schema_version", "cluster_id", "gap_id", "map_id"}
_PHASES = {"prepared", "written", "attachment_registered", "verified", "archived", "restoring", "restored"}


def _sha(raw):
    return hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()


def binding_identity(binding):
    if not isinstance(binding, dict) or set(binding) - {"server_id", "library_type", "library_id", "item_key", "attachment_key"}:
        raise DocumentError("A complete Zotero source binding is required.")
    result = dict(binding)
    if (not isinstance(result.get("server_id"), str) or not 1 <= len(result["server_id"]) <= 256
            or any(ord(c) < 32 for c in result["server_id"])
            or result.get("library_type") not in {"user", "group"}
            or not re.fullmatch(r"[0-9]{1,20}", str(result.get("library_id", "")))
            or not re.fullmatch(r"[A-Z0-9]{8}", str(result.get("item_key", "")))
            or "attachment_key" in result and not re.fullmatch(r"[A-Z0-9]{8}", str(result["attachment_key"]))):
        raise DocumentError("Invalid Zotero server, library, item or attachment identity.")
    result["library_id"] = str(result["library_id"])
    # The originating PDF is provenance, not a second active summary identity.
    scope = {key: result[key] for key in ("server_id", "library_type", "library_id", "item_key")}
    return result, _sha(json.dumps(scope, sort_keys=True, separators=(",", ":")))


def _fields(text):
    """Reject ambiguous identity headers instead of accepting last-key-wins YAML."""
    from .documents import DocumentService
    parts = DocumentService._override_parts(text)
    if parts is None or not parts[0]:
        raise DocumentError("Shared source notes require valid, bounded YAML properties.")
    header = parts[0].split("\n", 1)[1].rsplit("\n", 2)[0]
    try:
        tree = yaml.compose(header, Loader=yaml.SafeLoader)
        if not isinstance(tree, yaml.MappingNode):
            raise ValueError()
        keys = [key.value for key, _ in tree.value if isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:str"]
        if len(keys) != len(tree.value) or len(set(keys)) != len(keys):
            raise ValueError()
        return json.loads(json.dumps(_frontmatter(text), default=str)), parts[1]
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise DocumentError("Shared source properties contain duplicate or unsupported keys.") from exc


def source_navigation(service, relative, text):
    """Verify sidecar identity against its schema, exact note ID and note path.

    This is provenance lookup, not a claim that the source text is complete or
    unchanged. No title-based or filename-based matching is used.
    """
    fields = _frontmatter(text)
    note_id = fields.get("note_id")
    if not isinstance(note_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", note_id):
        return None
    try:
        with service._directory(("11_state", "note_metadata")) as directory:
            raw, _ = service._read_derived_bytes(directory, note_id + ".yml", max_bytes=1024 * 1024)
        from .notes import NOTE_METADATA_SCHEMA_VERSION
        from .documents import _origin_identity
        receipt = _origin_identity(raw, include_source_navigation=True)
        if not receipt or receipt.get("note_path") != relative or receipt.get("frontmatter", {}).get("note_id") != note_id:
            return None
        metadata = receipt.get("frontmatter")
        if (receipt.get("metadata_schema_version") != NOTE_METADATA_SCHEMA_VERSION or receipt.get("note_path") != relative
                or not isinstance(metadata, dict) or metadata.get("note_id") != note_id
                or not isinstance(metadata.get("source_id"), str) or not 0 < len(metadata["source_id"]) <= 256):
            return None
        result = {"verified": True, "basis": "writer_metadata", "sidecar_sha256": _sha(raw)}
        for key in ("note_id", "source_id", "zotero_item_key", "attachment_key", "zotero_item_uri", "source_pdf_uri"):
            value = metadata.get(key)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or len(value) > 4096 or key in fields and fields[key] != value:
                return None
            result[key] = value
        return result
    except (FileNotFoundError, DocumentError, UnicodeError):
        return None


def _source_fields(service, relative, fields):
    if not isinstance(fields.get("note_id"), str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", fields["note_id"]):
        raise DocumentError("A source note needs a recorded note identity before migration.")
    navigation = source_navigation(service, relative, "---\n" + yaml.safe_dump(fields) + "---\n")
    resolved = dict(fields)
    if navigation:
        resolved.update({key: value for key, value in navigation.items() if key not in {"verified", "basis", "sidecar_sha256"}})
    if not isinstance(resolved.get("source_id"), str) or not resolved["source_id"]:
        raise DocumentError("A source note needs a recorded source identity before migration.")
    return resolved


def patch_properties(text, changes):
    """Patch only selected scalar/list properties, preserving comments and body."""
    fields, _ = _fields(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    header = re.match(r"^---\r?\n([\s\S]*?)^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", text, re.MULTILINE)
    raw = header[1]
    tree = yaml.compose(raw, Loader=yaml.SafeLoader)
    nodes = {key.value: (key, value) for key, value in tree.value}
    edits, additions = [], []
    for key, value in changes.items():
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if key in nodes:
            key_node, value_node = nodes[key]
            last = value_node.value[-1] if isinstance(value_node, yaml.SequenceNode) and not value_node.flow_style and value_node.value else value_node
            end = last.end_mark.index
            suffix = newline if raw[:end].endswith("\n") else ""
            edits.append((key_node.end_mark.index, end, ": " + encoded + suffix))
        else:
            additions.append(json.dumps(key, ensure_ascii=False) + ": " + encoded + newline)
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    if additions:
        raw += ("" if raw.endswith("\n") else newline) + "".join(additions)
    return text[:header.start(1)] + raw + text[header.end(1):]


def _safe_read(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > _MAX_JOURNAL):
            raise DocumentError("Invalid private shared-note record.")
        chunks, remaining = [], _MAX_JOURNAL + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk); remaining -= len(chunk)
        if _stamp(before) != _stamp(os.fstat(fd)) or _stamp(before) != _stamp(directory.stat(name)):
            raise DocumentError("The shared-note journal changed while reading.")
        value = json.loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError) as exc:
        raise DocumentError("Invalid private shared-note journal.") from exc
    finally:
        os.close(fd)


def _atomic(directory, name, raw, *, verify=lambda: None, mode=0o600):
    temporary = ".az-live-" + secrets.token_hex(16) + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
    info = os.fstat(fd)
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                raise DocumentError("Could not complete the shared-note write.")
            offset += count
        os.fchmod(fd, mode); os.fsync(fd)
        verify(); directory.verify()
        if _stamp(directory.stat(temporary)) != _stamp(os.fstat(fd)):
            raise DocumentError("The shared-note temporary file changed.")
        os.replace(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        os.fsync(directory.fd)
    finally:
        os.close(fd)
        try:
            if _identity(directory.stat(temporary)) == _identity(info):
                os.unlink(temporary, dir_fd=directory.fd)
        except FileNotFoundError:
            pass


def _write(directory, name, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > _MAX_JOURNAL:
        raise DocumentError("The shared-note recovery journal exceeds its limit.")
    _atomic(directory, name, raw)


class LiveNotes:
    def __init__(self, root):
        self.root = Path(root).expanduser()
        self._store = Overrides(self.root)

    @contextmanager
    def guard(self, workspace, *, create=False):
        if self.root.is_relative_to(workspace) or workspace.is_relative_to(self.root):
            raise DocumentError("Shared-note history must be outside the active workspace.")
        # Constant lock namespace intentionally coordinates different workspaces.
        with self._store.guard(Path("/__auto_zettelkasten_live_notes__"), (0, 0, 0), create=create) as view:
            yield view.directory if view else None

    @staticmethod
    def location(workspace, identity, path):
        return _sha(f"{workspace}\0{identity}\0{path}")

    def _record(self, directory, workspace, identity, path):
        if directory is None:
            return None
        reference = _safe_read(directory, "path-" + self.location(workspace, identity, path) + ".json")
        if not reference:
            return None
        token = reference.get("binding")
        if not isinstance(token, str) or not re.fullmatch("[a-f0-9]{64}", token):
            raise DocumentError("Invalid shared-note binding reference.")
        record = _safe_read(directory, "binding-" + token + ".json")
        if (not record or record.get("version") != 1 or record.get("path") != path
                or record.get("workspace") != str(workspace) or record.get("workspace_identity") != list(identity)
                or record.get("phase") not in _PHASES or binding_identity(record.get("binding"))[1] != token):
            raise DocumentError("Shared-note binding does not match this workspace.")
        return record if record["phase"] != "restored" else None

    def read(self, workspace, identity, path):
        if self.root.is_relative_to(workspace) or workspace.is_relative_to(self.root):
            return None
        with self.guard(workspace) as directory:
            return self._record(directory, workspace, identity, path)

    def generation(self, workspace):
        if self.root.is_relative_to(workspace) or workspace.is_relative_to(self.root):
            return None
        with self.guard(workspace) as directory:
            return _stamp(os.fstat(directory.fd)) if directory else None

    @staticmethod
    def validate(record, text):
        fields, _ = _fields(text)
        protected = record["protected"]
        if (any(fields.get(key) != value for key, value in protected.items())
                or any(key in fields and key not in protected for key in _IDENTITY)):
            raise DocumentError("Source identity or generated provenance changed. Restore it before synchronizing this note.")
        # Materialized personal metadata is deliberately editable in the active file.
        return fields

    @staticmethod
    def projection(record, document):
        from .documents import _personal_projection
        result = dict(document)
        try:
            fields = LiveNotes.validate(record, document["text"])
            identity_ok = True
        except DocumentError as exc:
            fields, identity_ok = _frontmatter(document["text"]), False
            result["reason"] = str(exc)
        ready = (record["phase"] == "archived" and identity_ok and document.get("newline") != "mixed"
                 and document.get("disk_writable", True))
        if identity_ok:
            result["reason"] = "" if ready else ("This shared note is read-only." if record["phase"] == "archived"
                                               else "Shared-note migration is pending; resume it before editing.")
        original = record["generated_properties"]
        personal = {key: value for key, value in fields.items() if key not in original and key not in _IDENTITY}
        for key, singular in (("tags", "tag"), ("aliases", "alias")):
            existing = original.get(key, original.get(singular, []))
            current = fields.get(key, fields.get(singular, []))
            existing = [existing] if isinstance(existing, str) else existing if isinstance(existing, list) else []
            current = [current] if isinstance(current, str) else current if isinstance(current, list) else []
            added = [value for value in current if value not in existing]
            if added:
                personal[key] = added
        title = fields.get("title") if isinstance(fields.get("title"), str) else None
        result.update(_personal_projection({**document, "reason": ""}, {"display_title": title, "personal_properties": personal}))
        result["property_ownership"] = {key: "generated" if key in record["protected"] else "personal" for key in fields}
        # Equivalence is computed against preserved bytes, never by editing saved map hashes.
        baseline = base64.b64decode(record["baseline_b64"]).decode("utf-8-sig")
        try:
            active_text = document["text"]
            if record.get("annotation_block_hash"):
                from .zotero_annotations import strip_annotation_projection
                active_text = strip_annotation_projection(active_text, expected_hash=record["annotation_block_hash"])
            normalize = lambda value: re.sub(r"\n{3,}", "\n\n", value.replace("\r\n", "\n")).strip()
            unchanged = identity_ok and normalize(_fields(baseline)[1]) == normalize(_fields(active_text)[1])
        except DocumentError:
            unchanged = False
        result.update(editable=ready, live_note=True, live_note_phase=record["phase"],
                      live_identity_valid=identity_ok, live_binding=record["binding"],
                      live_source_identity=record.get("source_identity", {}), live_protected_fields=sorted(record["protected"]),
                      override_available=False, override_saved=False, body_override_saved=not unchanged,
                      source_changed=False, source_revision=record["baseline_revision"],
                      live_equivalent_revision=record["baseline_revision"] if unchanged else None,
                      semantic_baseline_text=baseline if unchanged else None,
                      annotation_block_hash=record.get("annotation_block_hash"))
        return result

    def preview(self, service, workspace_id, document_id, revision, binding):
        service._workspace(workspace_id)
        if document_id not in service._documents:
            raise DocumentError("Open the note before preparing a shared-note migration.")
        normalized, token = binding_identity(binding)
        if normalized["library_type"] == "group":
            raise DocumentError("Zotero group libraries do not support linked Markdown attachments.")
        relative = service._documents[document_id][0]
        with service._directory(tuple(Path(relative).parts[:-1])) as directory:
            canonical = service._document(relative, directory, effective=False)
            current = service._override_document(canonical)
            if current["revision"] != revision:
                raise DocumentConflict(current)
            if current.get("live_note"):
                return {"document": current, "binding": normalized, "already_active": True}
            fields, _ = _fields(canonical["text"])
            source_fields = _source_fields(service, relative, fields)
            if source_fields.get("zotero_item_key", normalized["item_key"]) != normalized["item_key"]:
                raise DocumentError("The selected Zotero item differs from the recorded source.")
            from .markdown_link_export import _project_personal_metadata
            try:
                materialized, metadata = _project_personal_metadata(current["text"], current.get("personal_display_title"), current.get("personal_properties", {}))
            except ValueError as exc:
                raise DocumentError(str(exc)) from exc
            if metadata and metadata["property_conflicts"]:
                raise DocumentError("Resolve conflicting personal properties before migrating this note.")
            provenance = {key: source_fields[key] for key in ("note_id", "source_id", "zotero_item_key", "attachment_key", "zotero_item_uri", "source_pdf_uri") if key in source_fields}
            materialized = patch_properties(materialized, provenance)
            target = materialized.replace("\r\n", "\n")
            if canonical["newline"] == "mixed":
                raise DocumentError("Mixed line endings cannot be migrated.")
            if canonical["newline"] == "crlf":
                target = target.replace("\n", "\r\n")
            raw = (b"\xef\xbb\xbf" if canonical["bom"] else b"") + target.encode()
            if len(raw) > MAX_FILE_BYTES:
                raise DocumentError("The materialized note exceeds the 1 MiB limit.")
            with self.guard(service._root) as store:
                existing = _safe_read(store, "binding-" + token + ".json") if store else None
                if existing and existing["phase"] != "restored" and (existing["workspace"] != str(service._root)
                        or existing["workspace_identity"] != list(service._root_identity) or existing["path"] != relative):
                    raise DocumentError("This Zotero item already has an active note in another workspace. This copy remains detached.")
            return {"document": current, "binding": normalized, "path": relative, "text": target,
                    "baseline_revision": canonical["revision"], "materialized_revision": _sha(raw), "provenance": provenance, "source_identity": {key: source_fields[key] for key in ("note_id", "source_id")},
                    "changed": canonical["text"] != target, "already_active": False}

    def migrate(self, service, workspace_id, document_id, revision, binding, registrar, *, prepare_only=False):
        preview = self.preview(service, workspace_id, document_id, revision, binding)
        relative = preview["document"]["path"]
        normalized, token = binding_identity(binding)
        with self.guard(service._root, create=True) as store, service._directory(tuple(Path(relative).parts[:-1])) as directory:
            record = self._record(store, service._root, service._root_identity, relative)
            filename = "binding-" + token + ".json"
            if record and record["binding"] != normalized:
                raise DocumentError("A pending migration cannot change its source binding.")
            if record and record["phase"] == "restoring":
                raise DocumentError("Resume the pending baseline restoration before migrating this source again.")
            other = _safe_read(store, filename)
            if other and other["phase"] != "restored" and not record:
                if (other["workspace"] == str(service._root) and other["workspace_identity"] == list(service._root_identity)
                        and other["path"] == relative and other["binding"] == normalized):
                    # Crash between saving the reservation and saving its path pointer.
                    record = other
                    _write(store, "path-" + self.location(service._root, service._root_identity, relative) + ".json", {"binding": token})
                else:
                    raise DocumentError("This source note is already bound to another workspace.")
            raw, info = service._read_bytes(directory, Path(relative).name)
            canonical = service._document(relative, directory, effective=False)
            if not record:
                if other and other["phase"] == "restored":
                    # A later opt-in must not erase edits retained by a previous rollback.
                    history_id = _sha(json.dumps(other, sort_keys=True, separators=(",", ":")))
                    _write(store, "history-" + token + "-" + history_id + ".json", other)
                # Recheck the effective revision under the private overlay lock below.
                fields, _ = _fields(canonical["text"])
                materialized = (b"\xef\xbb\xbf" if canonical["bom"] else b"") + preview["text"].encode()
                record = {"version": 1, "binding": normalized, "workspace": str(service._root),
                          "workspace_identity": list(service._root_identity), "path": relative, "phase": "prepared",
                          "baseline_b64": base64.b64encode(raw).decode(), "baseline_revision": _sha(raw),
                          "materialized_b64": base64.b64encode(materialized).decode(),
                          "materialized_revision": _sha(materialized), "effective_revision": revision,
                          "source_identity": preview["source_identity"], "generated_properties": fields, "protected": {**{key: value for key, value in fields.items() if key not in _EDITABLE}, **preview["provenance"]},
                          "registration_key": "".join(secrets.choice("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(8)),
                          "write_token": secrets.token_hex(16)}
            with service._overrides.guard(service._root, service._root_identity, create=True) if service._overrides else _empty() as overlay:
                if "override_digest" not in record:
                    old, digest, _ = overlay.read(relative) if overlay else (None, None, None)
                    effective = service._legacy_override_document(canonical, overlay, guarded=True)
                    if effective["revision"] != revision or _sha(raw) != preview["baseline_revision"]:
                        raise DocumentConflict(effective)
                    record.update(override_record=old, override_digest=digest)
                    _write(store, filename, record)
                    _write(store, "path-" + self.location(service._root, service._root_identity, relative) + ".json", {"binding": token})
                if prepare_only:
                    return {"phase": record["phase"], "binding": normalized, "path": relative}
                if record["phase"] == "prepared":
                    _, digest, _ = overlay.read(relative) if overlay else (None, None, None)
                    if digest != record["override_digest"]:
                        raise DocumentError("The personal override changed after migration preparation. Preserve it and reconcile before continuing.")
                    raw, info = service._read_bytes(directory, Path(relative).name)
                    if _sha(raw) == record["baseline_revision"]:
                        def verify():
                            latest, _ = service._read_bytes(directory, Path(relative).name)
                            if _sha(latest) != record["baseline_revision"]:
                                raise DocumentError("The original source note changed during migration.")
                        service._writable(directory, live=True)
                        _atomic(directory, Path(relative).name, base64.b64decode(record["materialized_b64"]),
                                verify=verify, mode=stat.S_IMODE(info.st_mode) & 0o777)
                    elif _sha(raw) != record["materialized_revision"]:
                        raise DocumentError("The source changed after migration preparation; no text was overwritten.")
                    record["phase"] = "written"; _write(store, filename, record)
                if record["phase"] == "written":
                    if registrar is None:
                        return {"phase": record["phase"], "binding": normalized, "path": relative}
                    path = service._root / relative
                    attachment = registrar.find(normalized, path)
                    if attachment is None:
                        try:
                            attachment = registrar.register(normalized, path, key=record["registration_key"], write_token=record["write_token"])
                        except TimeoutError as exc:
                            raise DocumentError("Registration outcome is unknown; resume to reconcile the saved attachment identity.") from exc
                    if not isinstance(attachment, dict):
                        raise DocumentError("The linked attachment was not confirmed; resume to reconcile it.")
                    record["attachment"] = attachment; record["phase"] = "attachment_registered"; _write(store, filename, record)
                if record["phase"] == "attachment_registered":
                    if registrar is None:
                        return {"phase": record["phase"], "binding": normalized, "path": relative}
                    if not registrar.verify(normalized, service._root / relative, record["attachment"]):
                        raise DocumentError("The linked attachment could not be read back; the old override is preserved.")
                    record["phase"] = "verified"; _write(store, filename, record)
                if record["phase"] == "verified":
                    raw, _ = service._read_bytes(directory, Path(relative).name)
                    if _sha(raw) != record["materialized_revision"]:
                        raise DocumentError("The active note changed during migration; reconcile it before retiring the override.")
                    if overlay:
                        overlay.archive(relative, record["override_digest"])
                    record["phase"] = "archived"; _write(store, filename, record)
        return service.read(workspace_id, relative)

    def confirm_annotation_projection(self, service, workspace_id, document_id, revision, block_hash):
        from .zotero_annotations import verified_annotation_span
        service._workspace(workspace_id)
        relative = service._documents.get(document_id, (None,))[0]
        if not relative:
            raise DocumentError("Open the shared note before recording its annotations.")
        with self.guard(service._root, create=True) as store, service._directory(tuple(Path(relative).parts[:-1])) as directory:
            record = self._record(store, service._root, service._root_identity, relative)
            raw, _ = service._read_bytes(directory, Path(relative).name)
            text, _, _ = _decode(raw)
            if not record or record["phase"] != "archived" or _sha(raw) != revision:
                raise DocumentError("The shared note changed before its annotation projection was confirmed.")
            self.validate(record, text)
            if not verified_annotation_span(text, block_hash):
                raise DocumentError("The annotation section differs from the confirmed projection.")
            record["annotation_block_hash"] = block_hash
            _, token = binding_identity(record["binding"])
            _write(store, "binding-" + token + ".json", record)
        return service.read(workspace_id, relative)

    def restore(self, service, workspace_id, document_id, revision, registrar=None):
        service._workspace(workspace_id)
        relative = service._documents.get(document_id, (None,))[0]
        if not relative:
            raise DocumentError("Open the shared note before restoring its baseline.")
        current = service.read(workspace_id, relative)
        if current["revision"] != revision:
            raise DocumentConflict(current)
        with self.guard(service._root, create=True) as store, service._directory(tuple(Path(relative).parts[:-1])) as directory:
            record = self._record(store, service._root, service._root_identity, relative)
            if not record:
                raise DocumentError("This note has no active shared-note migration.")
            if record.get("restore_from_phase", record["phase"]) != "prepared" and registrar is None:
                raise DocumentError("Reconcile any possibly registered attachment before restoring this migration.")
            if registrar and registrar.find(record["binding"], service._root / relative) is not None:
                raise DocumentError("The linked attachment still exists. Detach it explicitly before restoring.")
            raw, info = service._read_bytes(directory, Path(relative).name)
            expected_raw = current.get("source_revision", revision) if record["phase"] == "prepared" else revision
            if _sha(raw) != expected_raw:
                raise DocumentConflict(service._document(relative, directory, effective=False))
            # Snapshot exactly once before the first restore write. A restart after
            # replacing the file must not overwrite this snapshot with the baseline.
            if record["phase"] != "restoring":
                record["restored_active_b64"] = base64.b64encode(raw).decode()
                record["restore_from_phase"] = record["phase"]
                record["phase"] = "restoring"
            _, token = binding_identity(record["binding"])
            _write(store, "binding-" + token + ".json", record)
            def verify():
                latest, _ = service._read_bytes(directory, Path(relative).name)
                if _sha(latest) != expected_raw:
                    raise DocumentError("The note changed before baseline restoration.")
            _atomic(directory, Path(relative).name, base64.b64decode(record["baseline_b64"]), verify=verify,
                    mode=stat.S_IMODE(info.st_mode) & 0o777)
            if record.get("override_record") and service._overrides:
                with service._overrides.guard(service._root, service._root_identity, create=True) as overlay:
                    existing, digest, _ = overlay.read(relative)
                    original = record["override_record"]
                    same_content = existing is not None and all(existing.get(key) == original.get(key)
                        for key in ("body", "display_title", "personal_properties", "source_revision"))
                    if existing is not None and digest != record["override_digest"] and not same_content:
                        raise DocumentError("An independent override appeared; recover it before restoring the migration.")
                    if existing is None:
                        overlay.write(relative, original.get("body"), original["source_revision"], lambda: None, metadata=original)
            record["phase"] = "restored"; _write(store, "binding-" + token + ".json", record)
        return service.read(workspace_id, relative)


@contextmanager
def _empty():
    yield None


def managed_note(workspace, note_path, *, live_root=None):
    """Read-only mapper guard. A migrated path must never be generated in place."""
    workspace = Path(workspace).expanduser().resolve(strict=True)
    note_path = Path(note_path)
    if note_path.is_absolute():
        try:
            note_path = note_path.relative_to(workspace)
        except ValueError as exc:
            raise DocumentError("The active note must be inside its recorded workspace.") from exc
    from .documents import _parts
    relative = "/".join(_parts(note_path.as_posix()))
    store = LiveNotes(live_root or Path.home() / ".config/auto-zettelkasten/workspace-live-notes")
    return store.read(workspace, _identity(workspace.stat()), relative)


def _stage_record(store, directory, current, text):
    if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
        raise DocumentError("Generated baseline exceeds the note size limit.")
    fields, _ = _fields(text)
    if any(key in fields and fields[key] != {**current["protected"], **current.get("source_identity", {})}.get(key)
           for key in ("note_id", "source_id", "zotero_item_key")) or fields.get("note_id") != current["source_identity"]["note_id"]:
        raise DocumentError("Generated baseline changes the bound source identity.")
    _, token = binding_identity(current["binding"])
    digest = _sha(text)
    _write(directory, "baseline-" + token + "-" + digest + ".json", {"version": 1, "text": text, "sha256": digest})
    current["pending_baseline_revision"] = digest
    current["baseline_review_required"] = True
    _write(directory, "binding-" + token + ".json", current)


@contextmanager
def generated_note_write(workspace, note_path, text, *, live_root=None):
    """Serialize mapper writes with migration, staging proposals for active notes.

    Callers write Markdown and writer metadata only while the yielded flag is
    True. The shared lock prevents a just-opted-in file being overwritten in the
    gap between looking up its binding and committing ordinary mapper output.
    """
    workspace = Path(workspace).expanduser().resolve(strict=True)
    note_path = Path(note_path)
    if note_path.is_absolute():
        note_path = note_path.relative_to(workspace)
    from .documents import _parts
    relative = "/".join(_parts(note_path.as_posix()))
    store = LiveNotes(live_root or Path.home() / ".config/auto-zettelkasten/workspace-live-notes")
    with store.guard(workspace, create=True) as directory:
        current = store._record(directory, workspace, _identity(workspace.stat()), relative)
        if current:
            _stage_record(store, directory, current, text)
        yield current is None


def stage_generated_baseline(workspace, note_path, text, *, live_root=None):
    """Store a proposed mapper revision privately; never replace active Markdown.

    Returns False for an unmigrated note. Callers must propagate storage errors
    rather than interpreting them as permission to overwrite the active file.
    Use generated_note_write when an ordinary write will follow this check.
    """
    with generated_note_write(workspace, note_path, text, live_root=live_root) as permitted:
        return not permitted
