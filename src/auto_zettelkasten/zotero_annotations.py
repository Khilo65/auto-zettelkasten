"""Bounded Zotero annotation translation, reversible projection and a private outbox.

This module does not contact Zotero or write Markdown. Callers supply confirmed
records, defer projection for dirty editors, and write returned text through the
document revision check. A dispatched outbox operation is never safe to retry
until its remote outcome has been reconciled.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata

try:
    import fcntl
except ImportError:
    fcntl = None


class AnnotationError(ValueError):
    pass


class AnnotationConflict(AnnotationError):
    pass


TYPES = frozenset({"highlight", "underline", "note", "text", "image", "ink"})
START = "<!-- auto-zettelkasten:annotations:start -->"
END = "<!-- auto-zettelkasten:annotations:end -->"
HEADING = "## Highlights and annotations"
MAX_POSITION_BYTES = 65000
MAX_ANNOTATIONS = 5000
MAX_PROJECTION_BYTES = 8 * 1024 * 1024
_KEY = re.compile(r"[A-Z0-9]{8}\Z")
_WRITE_KEY = re.compile(r"[23456789A-NP-Z]{8}\Z")
_SCOPE = re.compile(r"(?:users/(?:0|[1-9][0-9]*)|groups/[1-9][0-9]*)\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise AnnotationError("Annotation data must be bounded JSON.") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnnotationError("Duplicate annotation JSON field.")
        result[key] = value
    return result


def _text(value, limit=32768):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise AnnotationError("Annotation text exceeds its limit or is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise AnnotationError("Annotation text must be valid Unicode.") from exc
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _native_text(value, limit=32768):
    """Match Zotero annotation-string setters before round-trip comparison."""
    return unicodedata.normalize("NFC", _text(value, limit).strip())


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise AnnotationError("Invalid Zotero item key.")
    return value


def _identity(scope, attachment_key, annotation_key=None):
    if not isinstance(scope, str) or len(scope) > 40 or not _SCOPE.fullmatch(scope):
        raise AnnotationError("Expected an explicit users/id or groups/id library scope.")
    _key(attachment_key)
    if annotation_key is not None:
        _key(annotation_key)
    return (scope, attachment_key, annotation_key)


def _version(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise AnnotationError("Annotation version must be a non-negative integer.")
    return value


def validate_position(value, annotation_type):
    """Validate PDF-space geometry without confusing page labels with indices."""
    if annotation_type not in TYPES:
        raise AnnotationError("Unsupported annotation type.")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_POSITION_BYTES:
            raise AnnotationError("Annotation position exceeds its limit.")
        try:
            value = json.loads(value, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError) as exc:
            raise AnnotationError("Invalid annotation position JSON.") from exc
    if not isinstance(value, dict) or set(value) - {"pageIndex", "rects", "nextPageRects", "paths", "width", "fontSize", "rotation"}:
        raise AnnotationError("Unsupported annotation position fields.")
    page = value.get("pageIndex")
    if type(page) is not int or not 0 <= page <= 100000:
        raise AnnotationError("Invalid physical PDF page index.")
    if len(_json(value).encode("utf-8")) > MAX_POSITION_BYTES:
        raise AnnotationError("Annotation position exceeds its limit.")

    def coordinate(number):
        return type(number) in (int, float) and abs(number) <= 1000000 and math.isfinite(number)

    for field in ("rects", "nextPageRects"):
        if field not in value:
            continue
        rects = value[field]
        if not isinstance(rects, list) or not 1 <= len(rects) <= 2000:
            raise AnnotationError("Annotation rectangles must be a nonempty bounded list.")
        for rect in rects:
            if (not isinstance(rect, list) or len(rect) != 4 or not all(coordinate(n) for n in rect)
                    or rect[0] > rect[2] or rect[1] > rect[3]):
                raise AnnotationError("Invalid annotation rectangle.")
    if annotation_type == "ink":
        paths = value.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 1000 or "rects" in value or "nextPageRects" in value:
            raise AnnotationError("Drawing annotations require paths.")
        for path in paths:
            if not isinstance(path, list) or len(path) < 2 or len(path) % 2 or not all(coordinate(n) for n in path):
                raise AnnotationError("Invalid annotation drawing path.")
        width = value.get("width")
        if not coordinate(width) or not 0 < width <= 1000:
            raise AnnotationError("Invalid drawing width.")
    elif "rects" not in value or "paths" in value or "width" in value:
        raise AnnotationError("This annotation requires rectangles, not drawing paths.")
    if "nextPageRects" in value and annotation_type not in {"highlight", "underline"}:
        raise AnnotationError("Only text highlights can extend onto another page.")
    if annotation_type == "text":
        if not coordinate(value.get("fontSize")) or not 0 < value["fontSize"] <= 1000:
            raise AnnotationError("Invalid text annotation font size.")
        if not coordinate(value.get("rotation")) or not -360 <= value["rotation"] <= 360:
            raise AnnotationError("Invalid text annotation rotation.")
    elif "fontSize" in value or "rotation" in value:
        raise AnnotationError("Only text annotations have font size or rotation.")
    return copy.deepcopy(value)


def _tags(value, field):
    if not isinstance(value, list) or len(value) > 200:
        raise AnnotationError("Annotation tags must be a bounded list.")
    names = []
    for item in value:
        if not isinstance(item, dict):
            raise AnnotationError("Annotation tags require named objects.")
        name = _native_text(item.get(field), 256)
        if not name.strip():
            raise AnnotationError("Empty annotation tag.")
        if name not in names:
            names.append(name)
    return sorted(names)


def api_to_reader(record, *, scope, attachment_key, writable=False, known_internal=False):
    """Decode an API envelope with conservative permission handling.

    Generic local-API records can omit external/read-only flags. Writable host
    capability alone cannot unlock them. ``known_internal`` must originate in a
    durable successful creation journal, never an untrusted frontend field.
    """
    _identity(scope, attachment_key)
    if not isinstance(record, dict):
        raise AnnotationError("Expected an annotation object.")
    data = record.get("data", record)
    if not isinstance(data, dict) or data.get("itemType") != "annotation" or data.get("parentItem") != attachment_key:
        raise AnnotationError("Annotation is not a child of the requested attachment.")
    key = _key(data.get("key", record.get("key")))
    version = _version(data.get("version", record.get("version", 0)))
    if record.get("key", key) != key or record.get("version", version) != version:
        raise AnnotationError("Annotation envelope identity disagrees with its data.")
    library = record.get("library")
    if library is not None:
        kind, identifier = scope.split("/")
        if not isinstance(library, dict) or library.get("type") != {"users": "user", "groups": "group"}[kind]:
            raise AnnotationError("Annotation belongs to another library.")
        # users/0 aliases the active local account; the caller pins server identity.
        if scope != "users/0" and str(library.get("id")) != identifier:
            raise AnnotationError("Annotation belongs to another library identity.")
    kind = data.get("annotationType")
    position = validate_position(data.get("annotationPosition"), kind)
    external = any(record.get(k) is True or data.get(k) is True for k in ("isExternal", "annotationIsExternal"))
    attested_internal = known_internal is True or any(
        record.get(k) is False or data.get(k) is False for k in ("isExternal", "annotationIsExternal", "readOnly")
    )
    read_only = external or writable is not True or not attested_internal or record.get("readOnly") is True or data.get("readOnly") is True
    color = data.get("annotationColor", "#ffd400")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise AnnotationError("Invalid annotation color.")
    result = {
        "id": key, "key": key, "scope": scope, "attachment_key": attachment_key,
        "parentItem": attachment_key, "version": version, "type": kind, "position": position,
        "pageLabel": _text(data.get("annotationPageLabel", ""), 128),
        "sortIndex": _text(data.get("annotationSortIndex", ""), 128),
        "comment": _text(data.get("annotationComment", "")), "color": color,
        "tags": [{"name": name} for name in _tags(data.get("tags", []), "tag")],
        "isExternal": external, "readOnly": read_only,
    }
    if kind in {"highlight", "underline"}:
        result["text"] = _text(data.get("annotationText", ""))
    for field in ("dateAdded", "dateModified", "annotationAuthorName"):
        if field in data:
            result[{"dateAdded": "dateCreated", "annotationAuthorName": "authorName"}.get(field, field)] = _text(data[field], 256)
    return result


def _reader_identity(record, scope, attachment_key):
    if not isinstance(record, dict):
        raise AnnotationError("Expected a reader annotation object.")
    key = record.get("id")
    _identity(scope, attachment_key, key)
    if (record.get("scope") != scope or record.get("attachment_key") != attachment_key
            or record.get("parentItem", attachment_key) != attachment_key or record.get("key", key) != key):
        raise AnnotationError("Reader annotation identity does not match its bound attachment.")
    return key


def reader_to_api(record, *, scope, attachment_key, expected_version, existing=None):
    """Encode a draft with authoritative current state for every existing item."""
    key = _reader_identity(record, scope, attachment_key)
    if not _WRITE_KEY.fullmatch(key) or not _WRITE_KEY.fullmatch(attachment_key):
        raise AnnotationError("Zotero write keys cannot contain zero, one or the letter O.")
    expected_version = _version(expected_version)
    if _version(record.get("version", 0)) != expected_version:
        raise AnnotationConflict("Annotation draft has a stale version.")
    if expected_version and existing is None:
        raise AnnotationError("Updating an annotation requires its current bound record.")
    if existing is not None:
        if _reader_identity(existing, scope, attachment_key) != key:
            raise AnnotationError("Existing annotation identity differs from the draft.")
        if existing.get("version") != expected_version:
            raise AnnotationConflict("Annotation changed in Zotero; reconcile before saving.")
        if existing.get("readOnly", True) or existing.get("isExternal"):
            raise AnnotationError("This annotation is read-only; import external annotations in Zotero.")
        if existing.get("type") != record.get("type"):
            raise AnnotationError("An existing annotation cannot change type.")
    if record.get("readOnly") or record.get("isExternal"):
        raise AnnotationError("Read-only annotations cannot be saved.")
    kind = record.get("type")
    position = validate_position(record.get("position"), kind)
    color = record.get("color", "#ffd400")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise AnnotationError("Invalid annotation color.")
    sort_index = _native_text(record.get("sortIndex", ""), 128)
    if not re.fullmatch(r"\d{5}\|\d{6}\|\d{5}", sort_index, flags=re.ASCII):
        raise AnnotationError("PDF annotations require the reader's physical sort index.")
    result = {
        "key": key, "version": expected_version, "itemType": "annotation", "parentItem": attachment_key,
        "annotationType": kind, "annotationPosition": _json(position), "annotationColor": color.lower(),
        "annotationPageLabel": _native_text(record.get("pageLabel", ""), 128),
        "annotationSortIndex": sort_index,
        "annotationComment": _native_text(record.get("comment", "")),
        "tags": [{"tag": name} for name in _tags(record.get("tags", []), "name")],
    }
    if kind in {"highlight", "underline"}:
        result["annotationText"] = _native_text(record.get("text", ""))
    return result


def annotation_uri(scope, attachment_key, annotation_key, page_index):
    _identity(scope, attachment_key, annotation_key)
    if type(page_index) is not int or not 0 <= page_index <= 100000:
        raise AnnotationError("Invalid physical PDF page index.")
    library = "library" if scope.startswith("users/") else scope
    return f"zotero://open-pdf/{library}/items/{attachment_key}?page={page_index + 1}&annotation={annotation_key}"


def _escape(value):
    # Prevent Markdown/HTML/wikilink injection, including accidental managed markers.
    value = _text(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", value)


def _projection_span(text):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise AnnotationConflict("Annotation markers are damaged or duplicated; preserve and reconcile the note.")
    if not text.count(START):
        return None
    start, end = text.index(START), text.index(END) + len(END)
    if end <= start:
        raise AnnotationConflict("Annotation markers are out of order.")
    if (start and text[start - 1] != "\n") or (end < len(text) and text[end] not in "\r\n"):
        raise AnnotationConflict("Annotation markers must occupy complete lines.")
    return start, end


def verified_annotation_span(text, expected_hash):
    """Return verified managed offsets; unverified text remains personal prose."""
    if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
        return None
    try:
        span = _projection_span(text)
    except AnnotationConflict:
        return None
    if span and hashlib.sha256(text[span[0]:span[1]].encode()).hexdigest() == expected_hash:
        return span
    return None


def strip_annotation_projection(text, *, expected_hash=None):
    """Exclude verified annotation text from source-summary embedding input."""
    span = verified_annotation_span(text, expected_hash)
    return text[:span[0]] + text[span[1]:] if span else text


def annotation_recall_records(groups):
    """Return separate quoted evidence and personal comments with exact provenance."""
    records = []
    seen = set()
    text_bytes = 0
    if not isinstance(groups, list) or len(groups) > 100:
        raise AnnotationError("Expected a bounded attachment list.")
    attachment_seen = set()
    for group in groups:
        if not isinstance(group, dict) or group.get("confirmed") is not True:
            raise AnnotationError("Only confirmed annotation snapshots may be projected.")
        scope, attachment_key = group.get("scope"), group.get("attachment_key")
        _identity(scope, attachment_key)
        if (scope, attachment_key) in attachment_seen:
            raise AnnotationError("Duplicate attachment group.")
        attachment_seen.add((scope, attachment_key))
        if group.get("expected_pdf_hash") is not None and group.get("expected_pdf_hash") != group.get("pdf_hash"):
            raise AnnotationConflict("The attachment PDF changed; verify its annotations before projection.")
        title = _text(group.get("title", attachment_key), 512)
        annotations = group.get("annotations")
        if not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
            raise AnnotationError("Expected a bounded annotation snapshot.")
        for annotation in annotations:
            key = _reader_identity(annotation, scope, attachment_key)
            identity = (scope, attachment_key, key)
            if identity in seen:
                raise AnnotationError("Duplicate annotation identity in snapshot.")
            seen.add(identity)
            if len(seen) > MAX_ANNOTATIONS:
                raise AnnotationError("Annotation snapshot exceeds its limit.")
            kind = annotation.get("type")
            position = validate_position(annotation.get("position"), kind)
            quote = _text(annotation.get("text", "")) if kind in {"highlight", "underline"} else ""
            comment = _text(annotation.get("comment", ""))
            text_bytes += len(quote.encode("utf-8")) + len(comment.encode("utf-8")) + len(title.encode("utf-8")) + 1024
            if text_bytes > MAX_PROJECTION_BYTES:
                raise AnnotationError("Annotation projection exceeds its text limit.")
            records.append({
                "scope": scope, "attachment_key": attachment_key, "annotation_key": key,
                "attachment_title": title, "type": kind, "version": _version(annotation.get("version")),
                "page_index": position["pageIndex"], "page_label": _text(annotation.get("pageLabel", ""), 128),
                "sort_index": _text(annotation.get("sortIndex", ""), 128), "quote": quote,
                "comment": comment,
                "uri": annotation_uri(scope, attachment_key, key, position["pageIndex"]),
                "read_only": bool(annotation.get("readOnly") or annotation.get("isExternal")),
            })
    return sorted(records, key=lambda row: (row["scope"], row["attachment_key"], row["page_index"], row["sort_index"], row["annotation_key"]))


def project_annotations(markdown, attachment_groups, *, expected_hash=None, previews=None):
    """Return a projection, never mutate the note. Its returned hash is a CAS token.

    ``previews`` maps (scope, attachment key, annotation key) to already-created
    relative PNG/JPEG/WebP assets. Image extraction is the caller's responsibility.
    """
    if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > 16 * 1024 * 1024:
        raise AnnotationError("Note exceeds the annotation projection limit.")
    if expected_hash is not None and (not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash)):
        raise AnnotationError("Invalid prior annotation block hash.")
    span = _projection_span(markdown)
    if span:
        old_hash = hashlib.sha256(markdown[span[0]:span[1]].encode()).hexdigest()
        if expected_hash is None or old_hash != expected_hash:
            raise AnnotationConflict("Managed annotations were edited; preserve and reconcile the existing text.")
    elif expected_hash is not None:
        raise AnnotationConflict("The previously projected annotation section is missing.")
    records = annotation_recall_records(attachment_groups)
    previews = previews or {}
    if not isinstance(previews, dict) or len(previews) > MAX_ANNOTATIONS:
        raise AnnotationError("Invalid annotation preview mapping.")
    lines = [START, HEADING, ""]
    current = None
    for record in records:
        attachment = (record["scope"], record["attachment_key"])
        if attachment != current:
            if current is not None:
                lines.append("")
            lines += [f"### {_escape(record['attachment_title'])}", ""]
            current = attachment
        label = _escape(record["page_label"] or str(record["page_index"] + 1))
        lines.append(f"- [{record['type'].capitalize()} · p. {label}]({record['uri']})")
        if record["quote"]:
            lines.append("  **Source quotation:**")
            lines.extend(f"  > {_escape(line)}" for line in record["quote"].split("\n"))
        if record["comment"]:
            lines.append("  **Personal comment:**")
            lines.extend(f"  {_escape(line)}  " for line in record["comment"].split("\n"))
        preview = previews.get((*attachment, record["annotation_key"]))
        if preview is not None:
            if (record["type"] not in {"image", "ink"} or not isinstance(preview, str) or len(preview) > 512
                    or not re.fullmatch(r"[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp)", preview, re.I)
                    or preview.startswith("/") or ".." in preview.split("/") or "//" in preview):
                raise AnnotationError("Annotation previews require a safe relative image asset.")
            lines.append(f"  ![Annotation preview]({preview})")
        elif record["type"] in {"image", "ink"}:
            lines.append("  Visual annotation; open the PDF to inspect it.")
        lines.append("")
    if not records:
        lines += ["No confirmed annotations.", ""]
    lines.append(END)
    newline = "\r\n" if "\r\n" in markdown and "\n" not in markdown.replace("\r\n", "") else "\n"
    block = newline.join(lines)
    if len(block.encode("utf-8")) > MAX_PROJECTION_BYTES:
        raise AnnotationError("Annotation projection exceeds its rendered limit.")
    if span:
        updated = markdown[:span[0]] + block + markdown[span[1]:]
    else:
        match = re.search(r"(?m)^## (?:Related notes — automatic|Graph Links)\r?$", markdown)
        if not match:
            match = re.search(r"(?m)^<!-- auto-zettelkasten:(?:related|graph):start -->\r?$", markdown)
        insertion = match.start() if match else len(markdown)
        before, after = markdown[:insertion], markdown[insertion:]
        separator = "" if not before or before.endswith(newline * 2) else newline if before.endswith(newline) else newline * 2
        updated = before + separator + block + newline * 2 + after
    return {"text": updated, "block_hash": hashlib.sha256(block.encode()).hexdigest(), "annotations": records}


class PendingAnnotations:
    """Revision-checked durable operations. The caller supplies transport and auth.

    Each annotation has one operation at a time. ``dispatched`` and ``uncertain``
    are both blocked for retry. Reconcile using a fresh remote read, then dispatch
    only when the outcome is confirmed unchanged. Files contain no credentials.
    """

    def __init__(self, root):
        self.root = Path(root).expanduser()
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise AnnotationError("Annotation outbox requires an absolute private path.")

    @contextmanager
    def _guard(self):
        if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            raise AnnotationError("Safe annotation outbox writes are unavailable on this platform.")
        descriptors = []
        lock = None
        try:
            descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(descriptor)
            for part in self.root.parts[1:]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(child)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise AnnotationError("Annotation outbox must be owner-only (0700).")
            try:
                lock = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                except FileExistsError:
                    lock = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=descriptor)
            self._safe_file(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield descriptor
        finally:
            if lock is not None:
                os.close(lock)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _safe_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024):
            raise AnnotationError("Unsafe annotation outbox entry.")

    @staticmethod
    def _id(scope, attachment_key, annotation_key):
        _identity(scope, attachment_key, annotation_key)
        return hashlib.sha256(f"{scope}\0{attachment_key}\0{annotation_key}".encode()).hexdigest()

    def _read(self, directory, operation_id):
        if not isinstance(operation_id, str) or not _HASH.fullmatch(operation_id):
            raise AnnotationError("Invalid annotation outbox identity.")
        try:
            descriptor = os.open(operation_id + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            self._safe_file(os.fstat(descriptor))
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                record = json.load(handle, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise AnnotationError("Damaged annotation outbox record.") from exc
        finally:
            os.close(descriptor)
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or self._id(record.get("scope"), record.get("attachment_key"), record.get("annotation_key")) != operation_id):
            raise AnnotationError("Annotation outbox record identity mismatch.")
        if (record.get("id") != operation_id or type(record.get("revision")) is not int or record["revision"] < 1
                or record.get("state") not in {"queued", "dispatched", "uncertain", "confirmed", "conflict", "discarded"}
                or record.get("operation") not in {"upsert", "delete"}):
            raise AnnotationError("Damaged annotation outbox state.")
        return record

    def _write(self, directory, record):
        raw = (_json(record) + "\n").encode()
        if len(raw) > 1024 * 1024:
            raise AnnotationError("Annotation outbox entry exceeds its limit.")
        temporary = ".pending-" + secrets.token_hex(12)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(descriptor)
            os.replace(temporary, record["id"] + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def get(self, operation_id):
        with self._guard() as directory:
            return self._read(directory, operation_id)

    def operation_id(self, scope, attachment_key, annotation_key):
        return self._id(scope, attachment_key, annotation_key)

    def lookup(self, scope, attachment_key, annotation_key):
        return self.get(self._id(scope, attachment_key, annotation_key))

    def pending(self):
        with self._guard() as directory:
            names = os.listdir(directory)
            if len(names) > 10002:
                raise AnnotationError("Annotation outbox exceeds its inventory limit.")
            records = [self._read(directory, name[:-5]) for name in sorted(names) if name.endswith(".json")]
            return [record for record in records if record["state"] not in {"confirmed", "discarded"}]

    def enqueue(self, *, scope, attachment_key, annotation_key, operation, payload=None, base_version=0, expected_revision=None):
        operation_id = self._id(scope, attachment_key, annotation_key)
        if not _WRITE_KEY.fullmatch(annotation_key) or not _WRITE_KEY.fullmatch(attachment_key):
            raise AnnotationError("Zotero write keys cannot contain zero, one or the letter O.")
        _version(base_version)
        if operation not in {"upsert", "delete"}:
            raise AnnotationError("Unsupported annotation operation.")
        if operation == "upsert":
            if not isinstance(payload, dict) or payload.get("key") != annotation_key or payload.get("version") != base_version:
                raise AnnotationError("Outbox payload identity/version mismatch.")
            # Queue admission validates shape, not permission. The transport must
            # establish editability again from its own authoritative provenance.
            decoded = api_to_reader(payload, scope=scope, attachment_key=attachment_key, writable=True, known_internal=True)
            if decoded["readOnly"]:
                raise AnnotationError("External annotations cannot enter the write queue.")
        elif payload is not None or base_version == 0:
            raise AnnotationError("Deletion requires an existing version and no replacement payload.")
        with self._guard() as directory:
            current = self._read(directory, operation_id)
            revision = current["revision"] if current else None
            if expected_revision != revision:
                raise AnnotationConflict("Annotation outbox changed; reload before saving.")
            if current and current["state"] not in {"confirmed", "conflict", "discarded"}:
                raise AnnotationConflict("A pending annotation operation must finish or be reconciled first.")
            record = {"schema_version": 1, "id": operation_id, "revision": (revision or 0) + 1,
                      "scope": scope, "attachment_key": attachment_key, "annotation_key": annotation_key,
                      "operation": operation, "payload": copy.deepcopy(payload), "base_version": base_version,
                      "state": "queued", "attempt_id": None, "events": ["queued"]}
            self._write(directory, record)
            return record

    def _transition(self, operation_id, revision, allowed, state, *, observed=None):
        with self._guard() as directory:
            record = self._read(directory, operation_id)
            if record is None or record["revision"] != revision:
                raise AnnotationConflict("Annotation outbox revision changed.")
            if record["state"] not in allowed:
                raise AnnotationConflict("The annotation operation requires reconciliation before this transition.")
            record["revision"] += 1
            record["state"] = state
            if state == "dispatched":
                record["attempt_id"] = secrets.token_hex(16)
            record["events"] = (record.get("events", []) + [state])[-64:]
            if observed is not None:
                record["observed"] = observed
            self._write(directory, record)
            return record

    def mark_dispatched(self, operation_id, revision):
        return self._transition(operation_id, revision, {"queued"}, "dispatched")

    def mark_uncertain(self, operation_id, revision):
        return self._transition(operation_id, revision, {"dispatched"}, "uncertain")

    def discard(self, operation_id, revision):
        """Explicitly cancel a known unsent/conflicting edit, retaining its payload."""
        return self._transition(operation_id, revision, {"queued", "conflict"}, "discarded")

    def reconcile(self, operation_id, revision, *, remote_record=None, confirmed_absent=False, known_internal=False):
        """Compare a fresh complete remote read; failed/partial reads are not absence.

        A matching remote payload confirms an upsert. The same unchanged version
        permits an explicit retry. Another edit becomes a conflict. A missing
        item confirms deletion only when the caller verified an authoritative 404.
        """
        current = self.get(operation_id)
        if current is None or current["revision"] != revision:
            raise AnnotationConflict("Annotation outbox revision changed.")
        if (remote_record is None) == (confirmed_absent is not True):
            raise AnnotationError("Reconciliation needs either a remote record or confirmed absence.")
        if confirmed_absent:
            state = "confirmed" if current["operation"] == "delete" else "queued" if current["base_version"] == 0 else "conflict"
            observed = {"absent": True}
        else:
            remote = api_to_reader(remote_record, scope=current["scope"], attachment_key=current["attachment_key"], writable=True, known_internal=known_internal)
            if remote["id"] != current["annotation_key"]:
                raise AnnotationError("Remote response refers to another annotation.")
            observed = {"version": remote["version"]}
            state = "conflict"
            if current["operation"] == "upsert":
                wanted = api_to_reader(current["payload"], scope=current["scope"], attachment_key=current["attachment_key"], writable=True, known_internal=True)
                fields = ("type", "position", "text", "comment", "color", "pageLabel", "sortIndex", "tags")
                if remote["version"] > current["base_version"] and all(remote.get(k) == wanted.get(k) for k in fields):
                    state = "confirmed"
            if state != "confirmed" and remote["version"] == current["base_version"] and not remote["readOnly"]:
                state = "queued"
        return self._transition(operation_id, revision, {"dispatched", "uncertain"}, state, observed=observed)

    def confirm(self, operation_id, revision, *, remote_record=None, confirmed_absent=False, known_internal=False):
        result = self.reconcile(operation_id, revision, remote_record=remote_record, confirmed_absent=confirmed_absent, known_internal=known_internal)
        if result["state"] != "confirmed":
            raise AnnotationConflict("Remote read did not confirm the submitted annotation operation.")
        return result
