"""Bounded, server-bound access to Zotero Desktop for the interactive workspace.

This deliberately does not extend the mapper's read-only client. Writes require
Zotero's own authorization and an explicit object version. There are no retries,
cloud fallbacks, database access, or authorization prompts during reads.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .files import require_loopback_http_url


class ZoteroWorkspaceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "invalid_response", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class ZoteroWriteUncertain(ZoteroWorkspaceError):
    def __init__(self):
        super().__init__("Zotero write outcome is unknown; reconcile the saved operation before retrying.", code="write_uncertain")


@dataclass(frozen=True, slots=True)
class VerifiedPDF:
    path: Path
    attachment_key: str
    parent_item: str | None
    scope: str
    server_id: str
    version: int
    size: int
    mtime_ns: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{8}", value):
        raise ValueError("Zotero keys must be eight uppercase letters or digits")
    return value


def _write_key(value: str) -> str:
    # Zotero's object-key alphabet excludes 0, 1 and O. Broad bounded keys
    # remain readable for historical fixtures, but may never be dispatched.
    if not isinstance(value, str) or not re.fullmatch(r"[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}", value):
        raise ValueError("Zotero write keys must use its eight-character object-key alphabet")
    return value


def _scope(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"users/0|groups/[1-9][0-9]{0,14}", value):
        raise ValueError("Use users/0 or groups/<positive group ID>")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("Expected a nonnegative integer Zotero version")
    return value


def _text(value: Any, *, limit: int = 10000, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (not empty and not value.strip()):
        raise ValueError("Invalid or excessively long text")
    return value


def _data(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("data", record)
    if not isinstance(value, dict):
        raise ZoteroWorkspaceError("Invalid Zotero record")
    return value


class ZoteroWorkspaceClient:
    MAX_BYTES = 2 * 1024 * 1024
    MAX_INVENTORY = 100000
    # Initial write contract is verified against local API v3/schema44. New
    # schemas remain readable but require a compatibility test before writing.
    WRITE_SCHEMA_VERSIONS = {44}
    METADATA_FIELDS = frozenset({
        "title", "abstractNote", "date", "publicationTitle", "journalAbbreviation",
        "volume", "issue", "pages", "publisher", "place", "DOI", "ISBN", "ISSN",
        "url", "language", "shortTitle", "rights", "extra", "archive",
        "archiveLocation", "libraryCatalog", "callNumber", "series", "seriesTitle",
        "seriesText", "seriesNumber", "edition", "bookTitle", "university",
        "thesisType", "reportType", "reportNumber", "institution", "accessDate",
    })
    ANNOTATION_FIELDS = frozenset({
        "annotationType", "annotationText", "annotationComment", "annotationColor",
        "annotationPageLabel", "annotationSortIndex", "annotationPosition", "tags",
    })

    def __init__(self, base_url: str = "http://127.0.0.1:23119", *, credential_path: Path | None = None, timeout: float = 20.0,
                 expected_server_id: str | None = None, allowed_origin: str | None = None):
        require_loopback_http_url(base_url, label="Zotero workspace URL")
        parts = urllib.parse.urlsplit(base_url)
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ValueError("Zotero workspace URL must contain only a loopback origin")
        if not 0 < timeout <= 120:
            raise ValueError("Timeout must be between 0 and 120 seconds")
        self.base_url = base_url.rstrip("/")
        if allowed_origin is not None and allowed_origin.rstrip("/") != self.base_url:
            raise ValueError("The Zotero connection is outside its allowed origin")
        if expected_server_id is not None and not re.fullmatch(r"[A-Za-z0-9]{12}", expected_server_id):
            raise ValueError("Expected Zotero server identity must contain 12 letters or digits")
        self._allowed_origin = self.base_url
        self.expected_server_id = expected_server_id
        self.credential_path = Path(credential_path) if credential_path is not None else None
        self.timeout = timeout
        self.server_id: str | None = None
        self.schema_version: int | None = None
        self._credential: dict[str, Any] | None = None
        self._created_annotations: set[tuple[str, str, str]] = set()
        self._bridge_status = "unchecked"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, endpoint: str, *, method: str = "GET", body: Any = None, write_token: str | None = None, authorize: bool = False, delete_version: int | None = None) -> tuple[bytes, Mapping[str, str]]:
        if self.base_url != self._allowed_origin:
            raise ZoteroWorkspaceError("Zotero connection origin changed; reconnect explicitly.", code="server_changed")
        if not endpoint.startswith(("users/0/", "groups/", "local/authorize")) and endpoint != "":
            raise ValueError("Unsupported local Zotero endpoint")
        headers = {"Zotero-API-Version": "3", "Accept": "application/json"}
        if self.server_id or self.expected_server_id:
            headers["Zotero-Server-ID"] = self.server_id or self.expected_server_id
        writing = method != "GET"
        if writing and authorize:
            self._require_write_capability()
        if writing and not authorize:
            self._require_write_capability()
            credential = self._load_credential()
            if credential is None:
                raise ZoteroWorkspaceError("Authorize this application in Zotero before writing.", code="authorization_required")
            headers["Zotero-API-Key"] = credential["key"]
            headers["Zotero-Server-ID"] = str(self.server_id)
            if write_token is None or not re.fullmatch(r"[A-Za-z0-9_-]{5,32}", write_token):
                raise ValueError("A stable write token of 5–32 characters is required")
            headers["Zotero-Write-Token"] = write_token
        if method == "DELETE":
            headers["If-Unmodified-Since-Version"] = str(_version(delete_version))
        payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if payload is not None:
            if len(payload) > self.MAX_BYTES:
                raise ValueError("Zotero write exceeds bounded request size")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + "/api/" + endpoint, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                response_headers = dict(response.headers.items())
                self._check_headers(response_headers)
                data = response.read(self.MAX_BYTES + 1)
                if len(data) > self.MAX_BYTES:
                    if writing:
                        raise ZoteroWriteUncertain()
                    raise ZoteroWorkspaceError("Zotero response exceeds bounded size", code="response_too_large")
                return data, response_headers
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                error_headers = dict(exc.headers.items())
                header_names = {name.lower() for name in error_headers}
                # Zotero's router answers an unregistered companion path before
                # local-API response decoration. Only this narrow read-only
                # absence may lack identity headers; it grants no permissions.
                absent_companion = (method == "GET" and status in {404, 405, 501}
                    and re.fullmatch(r"(?:users/0|groups/[1-9][0-9]{0,14})/items/[A-Z0-9]{8}/az-annotation-capabilities\?annotationKeys=[A-Z0-9%,]*", endpoint)
                    and not header_names.intersection({"zotero-server-id", "zotero-api-version", "zotero-schema-version"}))
                if not absent_companion:
                    self._check_headers(error_headers)
            finally:
                exc.close()
            if status == 401:
                self._forget_credential()
            codes = {401: "authorization_required", 403: "access_denied", 404: "not_found", 412: "version_conflict", 428: "precondition_required"}
            if writing and status >= 500:
                raise ZoteroWriteUncertain() from None
            raise ZoteroWorkspaceError(f"Zotero returned HTTP {status}.", code=codes.get(status, "http_error"), status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if writing:
                raise ZoteroWriteUncertain() from None
            raise ZoteroWorkspaceError("Zotero is unavailable; check that the local API is enabled.", code="unavailable") from None
        finally:
            if writing and not authorize and self._credential and not self._credential.get("remember"):
                self._forget_credential()

    def _check_headers(self, headers: Mapping[str, str]) -> None:
        lower = {key.lower(): value for key, value in headers.items()}
        identity = lower.get("zotero-server-id", "")
        if not re.fullmatch(r"[A-Za-z0-9]{12}", identity):
            raise ZoteroWorkspaceError("Zotero server identity is unavailable.", code="unsupported_server")
        if self.expected_server_id is not None and identity != self.expected_server_id:
            self._forget_credential()
            raise ZoteroWorkspaceError("This is not the selected Zotero server.", code="server_changed")
        if self.server_id is not None and self.server_id != identity:
            self._forget_credential()
            raise ZoteroWorkspaceError("Zotero server identity changed; reconnect explicitly.", code="server_changed")
        if lower.get("zotero-api-version") != "3":
            raise ZoteroWorkspaceError("This Zotero API version is unsupported.", code="unsupported_version")
        try:
            schema = int(lower["zotero-schema-version"])
        except (KeyError, ValueError):
            raise ZoteroWorkspaceError("Zotero schema version is unavailable.", code="unsupported_version") from None
        self.server_id = identity
        self.schema_version = schema

    def _ready(self) -> None:
        if self.server_id is None:
            self._request("")

    def _require_write_capability(self) -> None:
        # A remembered identity is not a current connection check. Preflight
        # every mutation/consent request before sending credentials or data.
        self._request("")
        if self.schema_version not in self.WRITE_SCHEMA_VERSIONS:
            raise ZoteroWorkspaceError("Zotero writes are disabled for this unverified schema version.", code="unsupported_version")

    def status(self) -> dict[str, Any]:
        try:
            self._request("")
            return {"available": True, "base_url": self.base_url, "server_id": self.server_id,
                    "api_version": 3, "schema_version": self.schema_version,
                    "write_supported": self.schema_version in self.WRITE_SCHEMA_VERSIONS,
                    "authorized": self._load_credential() is not None, "deletion_feed": False,
                    "annotation_capabilities": self._bridge_status}
        except ZoteroWorkspaceError as exc:
            return {"available": False, "base_url": self.base_url, "code": exc.code, "reason": str(exc), "authorized": False}

    def _private_path(self) -> Path:
        if self.credential_path is None:
            raise ZoteroWorkspaceError("Private credential storage is not configured.", code="credential_storage")
        path = self.credential_path
        if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
            raise ZoteroWorkspaceError("Credential path must be an absolute private path.", code="credential_storage")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for checked in (path.parent, path):
            if checked.exists():
                info = checked.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ZoteroWorkspaceError("Credential storage must have owner-only permissions.", code="credential_storage")
        return path

    def _load_credential(self) -> dict[str, Any] | None:
        if self._credential is not None:
            return self._credential
        if self.credential_path is None or not self.credential_path.exists():
            return None
        path = self._private_path()
        if path.stat().st_size > 4096:
            raise ZoteroWorkspaceError("Invalid private credential record.", code="credential_storage")
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ZoteroWorkspaceError("Unable to read private Zotero authorization.", code="credential_storage") from None
        if not isinstance(entry, dict) or entry.get("server_id") != self.server_id or entry.get("base_url") != self.base_url:
            return None
        if (not isinstance(entry.get("key"), str) or not re.fullmatch(r"[A-Za-z0-9]{32}", entry["key"])
                or type(entry.get("remember")) is not bool):
            raise ZoteroWorkspaceError("Invalid private credential record.", code="credential_storage")
        self._credential = entry
        return entry

    def _forget_credential(self) -> None:
        self._credential = None
        if self.credential_path is not None and self.credential_path.exists():
            # Never remove another server's authorization record.
            path = self._private_path()
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if entry.get("server_id") == self.server_id and entry.get("base_url") == self.base_url:
                    path.unlink()
            except (ValueError, OSError, AttributeError):
                pass

    def authorize(self, app_name: str = "Auto-Zettelkasten") -> dict[str, Any]:
        """Explicitly request Zotero's consent dialog; never called automatically."""
        self._require_write_capability()
        path = self._private_path()
        payload, _ = self._request("local/authorize", method="POST", body={"appName": _text(app_name, limit=120, empty=False)}, authorize=True)
        entry = self._json(payload)
        if not isinstance(entry, dict) or not re.fullmatch(r"[A-Za-z0-9]{32}", str(entry.get("key", ""))) or type(entry.get("remember")) is not bool:
            raise ZoteroWorkspaceError("Invalid Zotero authorization response.")
        entry = {"server_id": self.server_id, "base_url": self.base_url, "key": entry["key"], "remember": entry["remember"]}
        fd, temporary = tempfile.mkstemp(prefix=".zotero-authorization-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(entry, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._credential = entry
        return {"authorized": True, "remember": entry["remember"], "server_id": self.server_id}

    @staticmethod
    def _json(payload: bytes) -> Any:
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise ZoteroWorkspaceError("Zotero returned invalid JSON.") from None

    def _get_record(self, scope: str, key: str, kind: str = "items") -> dict[str, Any] | None:
        _scope(scope)
        _key(key)
        self._ready()
        try:
            payload, _ = self._request(f"{_scope(scope)}/{kind}/{_key(key)}")
        except ZoteroWorkspaceError as exc:
            if exc.status == 404:
                return None
            raise
        row = self._json(payload)
        if not isinstance(row, dict) or _data(row).get("key") != key or ("key" in row and row["key"] != key):
            raise ZoteroWorkspaceError("Zotero returned a different object identity.", code="identity_mismatch")
        try:
            _version(_data(row).get("version"))
        except ValueError:
            raise ZoteroWorkspaceError("Invalid Zotero object version.") from None
        return row

    def item(self, scope: str, key: str) -> dict[str, Any] | None:
        return self._get_record(scope, key)

    def annotation_capabilities(self, scope: str, attachment_key: str, keys: list[str]) -> dict[str, Any] | None:
        """Read authoritative permissions without consuming native write consent.

        This GET-only companion supplies no annotation content. Its response is
        never reused as an affirmative permission cache.
        """
        _scope(scope)
        _key(attachment_key)
        if (not isinstance(keys, list) or len(keys) > 64
                or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)):
            raise ValueError("Select at most 64 distinct annotation keys")
        for key in keys:
            _key(key)
        self._ready()
        endpoint = f"{scope}/items/{attachment_key}/az-annotation-capabilities?" + urllib.parse.urlencode({"annotationKeys": ",".join(keys)})
        try:
            payload, _ = self._request(endpoint)
        except ZoteroWorkspaceError as exc:
            if exc.status in {404, 405, 501}:
                self._bridge_status = "unavailable"
                return None
            raise
        value = self._json(payload)
        if (not isinstance(value, dict) or type(value.get("bridge_version")) is not int or value.get("bridge_version") != 1
                or not isinstance(value.get("zotero_version"), str) or not re.fullmatch(r"10\.0(?:\.[0-9A-Za-z.-]+)?", value["zotero_version"])
                or value.get("server_id") != self.server_id or value.get("scope") != scope
                or value.get("attachment_key") != attachment_key
                or type(value.get("attachment_read_only")) is not bool
                or not isinstance(value.get("annotations"), list) or not isinstance(value.get("missing"), list)):
            self._bridge_status = "incompatible"
            raise ZoteroWorkspaceError("Annotation capability response identity mismatch.", code="identity_mismatch")
        _version(value.get("attachment_version"))
        seen = set()
        for row in value["annotations"]:
            if (not isinstance(row, dict) or row.get("key") not in keys or row.get("key") in seen
                    or type(row.get("isExternal")) is not bool or type(row.get("readOnly")) is not bool):
                raise ZoteroWorkspaceError("Invalid annotation capabilities.", code="invalid_response")
            _version(row.get("version"))
            seen.add(row["key"])
        if (any(not isinstance(key, str) or key not in keys or key in seen for key in value["missing"])
                or len(set(value["missing"])) != len(value["missing"])
                or seen | set(value["missing"]) != set(keys)):
            raise ZoteroWorkspaceError("Incomplete annotation capabilities.", code="incomplete_inventory")
        self._bridge_status = "available"
        return value

    def attachment_read_only(self, scope: str, attachment_key: str) -> bool | None:
        """Fresh attachment permissions, including PDFs with no annotations.

        None means the companion endpoint is absent, never affirmative edit
        authority. An incompatible/stale response denies editing; connection
        failures propagate so callers cannot retain a stale writable state.
        """
        pdf = self._pdf_item(scope, attachment_key)
        try:
            capability = self.annotation_capabilities(scope, attachment_key, [])
        except (ValueError, ZoteroWorkspaceError) as exc:
            if (isinstance(exc, ZoteroWorkspaceError) and exc.status != 400 and exc.code not in {
                    "invalid_response", "identity_mismatch", "incomplete_inventory"}):
                raise
            self._bridge_status = "incompatible"
            return True
        if capability is None:
            return None
        if capability["attachment_version"] != pdf["version"]:
            self._bridge_status = "incompatible"
            return True
        return capability["attachment_read_only"]

    def _annotation_capability_records(self, scope: str, attachment_key: str, rows: list[dict[str, Any]], *, attachment_version: int) -> list[dict[str, Any]]:
        result = copy.deepcopy(rows)
        for start in range(0, len(result), 64):
            batch = result[start:start + 64]
            keys = [_key(_data(row).get("key")) for row in batch]
            try:
                capability = self.annotation_capabilities(scope, attachment_key, keys)
            except (ValueError, ZoteroWorkspaceError) as exc:
                if isinstance(exc, ZoteroWorkspaceError) and exc.code not in {
                        "invalid_response", "identity_mismatch", "incomplete_inventory"}:
                    raise
                self._bridge_status = "incompatible"
                for row in batch:
                    row["readOnly"] = True
                continue
            if capability is None:
                continue
            flags = {row["key"]: row for row in capability["annotations"]}
            for row in batch:
                data = _data(row)
                proof = flags.get(data["key"])
                # Missing or stale capabilities must override any historical
                # successful-creation provenance in the service.
                if (proof is None or proof["version"] != data["version"]
                        or capability["attachment_version"] != attachment_version):
                    row["readOnly"] = True
                    continue
                row["isExternal"] = proof["isExternal"]
                row["readOnly"] = capability["attachment_read_only"] or proof["readOnly"] or proof["isExternal"]
                data["isExternal"] = row["isExternal"]
                data["readOnly"] = row["readOnly"]
        return result

    def annotation_record(self, scope: str, attachment_key: str, key: str) -> dict[str, Any] | None:
        pdf = self._pdf_item(scope, attachment_key)
        row = self.item(scope, key)
        if row is None:
            return None
        data = _data(row)
        if data.get("itemType") != "annotation" or data.get("parentItem") != attachment_key:
            raise ZoteroWorkspaceError("Annotation is bound to another attachment.", code="identity_mismatch")
        library = row.get("library")
        if library is not None:
            expected_type = "user" if scope == "users/0" else "group"
            if (not isinstance(library, dict) or library.get("type") != expected_type
                    or scope != "users/0" and str(library.get("id")) != scope.split("/")[1]):
                raise ZoteroWorkspaceError("Annotation library identity mismatch.", code="identity_mismatch")
        return self._annotation_capability_records(scope, attachment_key, [row], attachment_version=pdf["version"])[0]

    def _page(self, endpoint: str, *, start: int, limit: int, query: Mapping[str, str] | None = None) -> dict[str, Any]:
        if type(start) is not int or not 0 <= start < self.MAX_INVENTORY or type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("Page requires start >= 0 and limit between 1 and 64")
        self._ready()
        params = {"start": start, "limit": limit, "sort": "dateAdded", "direction": "asc", **(query or {})}
        payload, headers = self._request(endpoint + "?" + urllib.parse.urlencode(params))
        records = self._json(payload)
        if query and query.get("format") == "versions":
            if not isinstance(records, dict):
                raise ZoteroWorkspaceError("Invalid Zotero version inventory.", code="incomplete_inventory")
            records = [{"key": key, "version": version} for key, version in records.items()]
        if not isinstance(records, list) or len(records) > limit or any(not isinstance(row, dict) for row in records):
            raise ZoteroWorkspaceError("Invalid Zotero page records.")
        lower = {key.lower(): value for key, value in headers.items()}
        try:
            total = int(lower["total-results"])
            version = int(lower["last-modified-version"]) if "last-modified-version" in lower else None
        except (KeyError, ValueError):
            raise ZoteroWorkspaceError("Zotero page lacks reliable pagination metadata.", code="incomplete_inventory") from None
        if total < 0 or total > self.MAX_INVENTORY or (start < total and not records) or start + len(records) > max(start, total):
            raise ZoteroWorkspaceError("Inconsistent Zotero pagination.", code="incomplete_inventory")
        next_start = start + len(records) if start + len(records) < total else None
        return {"records": records, "start": start, "limit": limit, "total": total, "next_start": next_start,
                "library_version": version, "server_id": self.server_id}

    def collections(self, scope: str = "users/0", *, start: int = 0, limit: int = 64) -> dict[str, Any]:
        return self._page(f"{_scope(scope)}/collections", start=start, limit=limit)

    def items_page(self, scope: str = "users/0", *, collection_key: str | None = None, query: str = "", start: int = 0, limit: int = 32) -> dict[str, Any]:
        endpoint = f"{_scope(scope)}/"
        if collection_key is not None:
            endpoint += f"collections/{_key(collection_key)}/"
        endpoint += "items/top"
        text = _text(query, limit=512)
        return self._page(endpoint, start=start, limit=limit, query={"q": text} if text else None)

    def children(self, scope: str, parent_key: str, *, start: int = 0, limit: int = 64) -> dict[str, Any]:
        page = self._page(f"{_scope(scope)}/items/{_key(parent_key)}/children", start=start, limit=limit)
        if any(_data(row).get("parentItem") != parent_key for row in page["records"]):
            raise ZoteroWorkspaceError("Zotero returned children of a different item.", code="identity_mismatch")
        return page

    def _all(self, endpoint: str, *, require_version: bool = True, query: Mapping[str, str] | None = None,
             record_filter: Callable[[dict[str, Any]], bool] | None = None) -> tuple[list[dict[str, Any]], int | None]:
        rows: list[dict[str, Any]] = []
        start = 0
        expected: tuple[int, int | None] | None = None
        while True:
            page = self._page(endpoint, start=start, limit=64, query=query)
            marker = (page["total"], page["library_version"])
            if (expected is not None and marker != expected) or (require_version and marker[1] is None):
                raise ZoteroWorkspaceError("Zotero inventory changed while reading; no deletions may be inferred.", code="incomplete_inventory")
            expected = marker
            rows.extend(row for row in page["records"] if record_filter is None or record_filter(row))
            if page["next_start"] is None:
                # Check again after the last page so a change during pagination
                # cannot masquerade as an authoritative deletion inventory.
                final = self._page(endpoint, start=0, limit=1, query=query)
                if (final["total"], final["library_version"]) != expected:
                    raise ZoteroWorkspaceError("Zotero inventory changed while reading.", code="incomplete_inventory")
                return rows, marker[1]
            start = page["next_start"]

    def libraries(self) -> list[dict[str, Any]]:
        groups, _ = self._all("users/0/groups", require_version=False)
        libraries = [{"scope": "users/0", "name": "My Library", "linked_files_supported": True}]
        for group in groups:
            data = _data(group)
            group_id = data.get("id", group.get("id"))
            scope = _scope(f"groups/{group_id}")
            libraries.append({"scope": scope, "name": str(data.get("name") or "Group library"), "linked_files_supported": False})
        return libraries

    def inventory_versions(self, scope: str = "users/0", *, kind: str = "items") -> dict[str, Any]:
        if kind not in {"items", "collections"}:
            raise ValueError("Inventory kind must be items or collections")
        rows, version = self._all(f"{_scope(scope)}/{kind}", query={"format": "versions"})
        versions: dict[str, int] = {}
        for row in rows:
            data = _data(row)
            key = _key(data.get("key"))
            if key in versions:
                raise ZoteroWorkspaceError("Duplicate key in Zotero inventory.", code="incomplete_inventory")
            versions[key] = _version(data.get("version"))
        return {"complete": True, "server_id": self.server_id, "scope": scope, "kind": kind, "library_version": version, "versions": versions}

    def annotations(self, scope: str, attachment_key: str) -> list[dict[str, Any]]:
        pdf = self._pdf_item(scope, attachment_key)
        # The native local children search omits annotations unless itemType is
        # explicit. Keep both predicates: a plain children response cannot prove
        # deletion, and a whole-library annotation scan makes each PDF open slow.
        seen: set[str] = set()
        def belongs_to_attachment(row):
            data = _data(row)
            try:
                key, parent = _key(data.get("key")), _key(data.get("parentItem"))
                _version(data.get("version"))
            except ValueError:
                raise ZoteroWorkspaceError("Incomplete annotation identity or version.", code="incomplete_inventory") from None
            if key in seen or data.get("itemType") != "annotation" or row.get("key", key) != key:
                raise ZoteroWorkspaceError("Invalid annotation inventory.", code="incomplete_inventory")
            if parent != attachment_key:
                raise ZoteroWorkspaceError("Annotation parent identity mismatch.", code="identity_mismatch")
            seen.add(key)
            library = row.get("library")
            if isinstance(library, dict):
                library_type = "user" if scope == "users/0" else "group"
                if library.get("type") != library_type or (library_type == "group" and str(library.get("id")) != scope.split("/")[1]):
                    raise ZoteroWorkspaceError("Annotation library identity mismatch.", code="identity_mismatch")
            return parent == attachment_key
        rows, _ = self._all(f"{_scope(scope)}/items/{_key(attachment_key)}/children",
                           query={"itemType": "annotation"}, record_filter=belongs_to_attachment)
        return self._annotation_capability_records(scope, attachment_key, rows, attachment_version=pdf["version"])

    def _pdf_item(self, scope: str, key: str) -> dict[str, Any]:
        row = self.item(scope, key)
        if row is None:
            raise ZoteroWorkspaceError("PDF attachment is missing.", code="not_found", status=404)
        data = _data(row)
        if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf" or data.get("linkMode") == "linked_url":
            raise ZoteroWorkspaceError("Item is not a PDF file attachment.", code="invalid_attachment")
        return data

    def pdf_file(self, scope: str, attachment_key: str) -> VerifiedPDF:
        data = self._pdf_item(scope, attachment_key)
        payload, _ = self._request(f"{_scope(scope)}/items/{_key(attachment_key)}/file/view/url")
        location = payload.decode("utf-8").strip()
        if location.startswith(('"', '{')):
            location = self._json(payload)
            if isinstance(location, dict):
                location = location.get("url")
        if not isinstance(location, str):
            raise ZoteroWorkspaceError("Invalid local attachment location.", code="invalid_attachment")
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ZoteroWorkspaceError("Zotero did not provide a local file URL.", code="invalid_attachment")
        path = Path(urllib.request.url2pathname(parsed.path))
        if not path.is_absolute():
            raise ZoteroWorkspaceError("PDF path is not absolute.", code="invalid_attachment")
        try:
            path = path.resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError()
            with path.open("rb") as stream:
                if not stream.read(5).startswith(b"%PDF-"):
                    raise OSError()
        except (OSError, ValueError):
            raise ZoteroWorkspaceError("PDF file is missing or invalid.", code="invalid_attachment") from None
        return VerifiedPDF(path, attachment_key, data.get("parentItem"), scope, str(self.server_id), data["version"], info.st_size, info.st_mtime_ns)

    @staticmethod
    def _tags(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 256:
            raise ValueError("Tags must be a bounded list")
        result = []
        for tag in value:
            if not isinstance(tag, dict) or set(tag) - {"tag", "type"} or type(tag.get("type", 0)) is not int or tag.get("type", 0) not in {0, 1}:
                raise ValueError("Invalid Zotero tag")
            result.append({"tag": unicodedata.normalize("NFC", _text(tag.get("tag"), limit=512, empty=False).strip()), "type": tag.get("type", 0)})
        return result

    def _update(self, scope: str, key: str, changes: dict[str, Any], *, kind: str, expected_version: int, write_token: str) -> dict[str, Any]:
        _write_key(key)
        current = self._get_record(scope, key, kind)
        if current is None:
            raise ZoteroWorkspaceError("Zotero object is missing.", code="not_found", status=404)
        if _data(current)["version"] != _version(expected_version):
            raise ZoteroWorkspaceError("Zotero object changed; reconcile before saving.", code="version_conflict", status=412)
        self._request(f"{_scope(scope)}/{kind}/{_key(key)}", method="PATCH", body={**changes, "version": expected_version}, write_token=write_token)
        try:
            saved = self._get_record(scope, key, kind)
        except ZoteroWorkspaceError:
            raise ZoteroWriteUncertain() from None
        if saved is None or not self._matches(_data(saved), changes):
            raise ZoteroWriteUncertain()
        return saved

    @staticmethod
    def _matches(data: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        """Compare acknowledged values using Zotero's serialization semantics.

        Zotero removes manual tag type=0, sorts tags/memberships, and normalizes
        item and annotation strings. Those changes do not make a confirmed write
        uncertain. Identity/path fields and annotation geometry stay exact.
        """
        def canonical(key, value):
            if key == "tags":
                if not isinstance(value, list):
                    return value
                try:
                    return sorted({(unicodedata.normalize("NFC", row["tag"].strip()), row.get("type", 0)) for row in value})
                except (AttributeError, KeyError, TypeError):
                    return value
            if key == "collections":
                return sorted(set(value)) if isinstance(value, list) and all(isinstance(v, str) for v in value) else value
            if key == "annotationColor" and isinstance(value, str):
                return value.lower()
            annotation_strings = {"annotationText", "annotationComment", "annotationPageLabel", "annotationSortIndex"}
            if key in ZoteroWorkspaceClient.METADATA_FIELDS or key in annotation_strings:
                if value is None or value is False:
                    return ""
                if isinstance(value, str):
                    value = unicodedata.normalize("NFC", value.strip())
                    if key in ZoteroWorkspaceClient.METADATA_FIELDS - {"abstractNote", "extra"}:
                        value = re.sub(r"[\r\n]+", " ", value)
                    if key == "ISBN":
                        value = re.sub(r"[-\s,;]+", "", value).upper()
                    return value
            return value
        return all(canonical(key, data.get(key)) == canonical(key, value) for key, value in expected.items() if key != "version")

    def update_item(self, scope: str, key: str, changes: Mapping[str, Any], *, expected_version: int, write_token: str) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not changes or set(changes) - (self.METADATA_FIELDS | {"tags", "collections"}):
            raise ValueError("Only bibliographic metadata, tags and collection membership may be edited")
        clean = {name: _text(value, limit=100000 if name in {"abstractNote", "extra"} else 10000) for name, value in changes.items() if name in self.METADATA_FIELDS}
        if "tags" in changes:
            clean["tags"] = self._tags(changes["tags"])
        if "collections" in changes:
            values = changes["collections"]
            if not isinstance(values, list) or len(values) > 256:
                raise ValueError("Collections must be a bounded key list")
            clean["collections"] = list(dict.fromkeys(_write_key(value) for value in values))
        current = self.item(scope, key)
        if current is None or _data(current).get("itemType") in {"annotation", "attachment", "note"}:
            raise ZoteroWorkspaceError("Metadata edits require a bibliographic source item.", code="invalid_item")
        return self._update(scope, key, clean, kind="items", expected_version=expected_version, write_token=write_token)

    def _create(self, scope: str, key: str, data: dict[str, Any], *, kind: str, write_token: str) -> dict[str, Any]:
        _write_key(key)
        existing = self._get_record(scope, key, kind)
        if existing is not None:
            if self._matches(_data(existing), data):
                return existing
            raise ZoteroWorkspaceError("Stable key is already bound to different data.", code="identity_mismatch")
        payload, _ = self._request(f"{_scope(scope)}/{kind}", method="POST", body=[{**data, "key": _key(key), "version": 0}], write_token=write_token)
        try:
            result = self._json(payload)
        except ZoteroWorkspaceError:
            raise ZoteroWriteUncertain() from None
        if not isinstance(result, dict) or not isinstance(result.get("failed", {}), dict):
            raise ZoteroWriteUncertain()
        failures = result.get("failed", {})
        if failures:
            failure = failures.get("0", {})
            status = failure.get("code") if isinstance(failure, dict) else None
            raise ZoteroWorkspaceError("Zotero rejected the object write.", code="version_conflict" if status == 412 else "write_rejected", status=status)
        try:
            saved = self._get_record(scope, key, kind)
        except ZoteroWorkspaceError:
            raise ZoteroWriteUncertain() from None
        if saved is None or not self._matches(_data(saved), data):
            raise ZoteroWriteUncertain()
        return saved

    def create_collection(self, scope: str, *, key: str, name: str, parent_key: str | None = None, write_token: str) -> dict[str, Any]:
        data = {"name": _text(name, limit=512, empty=False), "parentCollection": _write_key(parent_key) if parent_key else False}
        return self._create(scope, key, data, kind="collections", write_token=write_token)

    def rename_collection(self, scope: str, key: str, name: str, *, expected_version: int, write_token: str) -> dict[str, Any]:
        return self._update(scope, key, {"name": _text(name, limit=512, empty=False)}, kind="collections", expected_version=expected_version, write_token=write_token)

    def find_linked_note(self, scope: str, parent_key: str, path: Path) -> dict[str, Any] | None:
        if _scope(scope) != "users/0":
            raise ZoteroWorkspaceError("Zotero group libraries do not support linked files.", code="unsupported_linked_file")
        path = Path(path).resolve(strict=True)
        rows, _ = self._all(f"{scope}/items/{_key(parent_key)}/children")
        matches = []
        for row in rows:
            data = _data(row)
            if data.get("parentItem") != parent_key:
                raise ZoteroWorkspaceError("Linked attachment parent mismatch.", code="identity_mismatch")
            if data.get("itemType") == "attachment" and data.get("linkMode") == "linked_file" and data.get("path") == str(path):
                matches.append(row)
        if len(matches) > 1:
            raise ZoteroWorkspaceError("Multiple linked notes already reference this file; reconcile them.", code="identity_mismatch")
        return matches[0] if matches else None

    def register_linked_note(self, scope: str, parent_key: str, path: Path, *, key: str, title: str, write_token: str) -> dict[str, Any]:
        if _scope(scope) != "users/0":
            raise ZoteroWorkspaceError("Zotero group libraries do not support linked files.", code="unsupported_linked_file")
        _write_key(key)
        _write_key(parent_key)
        path = Path(path)
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".md":
            raise ValueError("Linked note must be an existing absolute Markdown file")
        path = path.resolve(strict=True)
        parent = self.item(scope, parent_key)
        if parent is None or _data(parent).get("itemType") in {"attachment", "annotation", "note"}:
            raise ZoteroWorkspaceError("Linked note requires a bibliographic parent.", code="invalid_item")
        existing = self.find_linked_note(scope, parent_key, path)
        if existing is not None:
            return existing
        data = {"itemType": "attachment", "parentItem": parent_key, "linkMode": "linked_file",
                "path": str(path), "contentType": "text/markdown", "title": _text(title, limit=512, empty=False)}
        return self._create(scope, key, data, kind="items", write_token=write_token)

    def _annotation_editable(self, scope: str, key: str, row: Mapping[str, Any], *, known_internal: bool) -> bool:
        data = _data(row)
        flags = {**dict(row.get("meta", {})), **data,
                 **{name: row[name] for name in ("annotationIsExternal", "isExternal", "readOnly") if name in row}}
        if flags.get("annotationIsExternal") or flags.get("isExternal") or flags.get("readOnly"):
            return False
        # Generic API item JSON omits the external-annotation flag. A missing
        # flag is not an assertion that a PDF-embedded annotation is writable.
        return (flags.get("annotationIsExternal") is False or flags.get("isExternal") is False
                or flags.get("readOnly") is False or known_internal
                or (str(self.server_id), scope, key) in self._created_annotations)

    def save_annotation(self, scope: str, attachment_key: str, annotation: Mapping[str, Any], *, key: str, expected_version: int, write_token: str, known_internal: bool = False) -> dict[str, Any]:
        """Save only internal annotations; known_internal must come from a durable creation journal."""
        self._pdf_item(scope, attachment_key)
        _write_key(attachment_key)
        _write_key(key)
        _version(expected_version)
        if not isinstance(annotation, Mapping) or set(annotation) - (self.ANNOTATION_FIELDS | {"key", "version", "itemType", "parentItem"}):
            raise ValueError("Unsupported annotation fields")
        identities = {"key": key, "version": expected_version, "itemType": "annotation", "parentItem": attachment_key}
        if any(name in annotation and annotation[name] != value for name, value in identities.items()):
            raise ValueError("Annotation identity/version does not match its operation")
        clean = {name: value for name, value in annotation.items() if name in self.ANNOTATION_FIELDS}
        for name, value in clean.items():
            if name == "tags":
                clean[name] = self._tags(value)
            else:
                _text(value, limit=65000 if name == "annotationPosition" else 100000)
                if name != "annotationPosition":
                    clean[name] = unicodedata.normalize("NFC", value.strip())
        if clean.get("annotationType") not in {"highlight", "underline", "note", "text", "image", "ink"}:
            raise ValueError("Unsupported annotation type")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(clean.get("annotationColor", ""))):
            raise ValueError("Annotation color is invalid")
        clean["annotationColor"] = clean["annotationColor"].lower()
        if not re.fullmatch(r"[0-9]{5}\|[0-9]{6}\|[0-9]{5}", str(clean.get("annotationSortIndex", ""))):
            raise ValueError("PDF annotations require a valid sort index")
        try:
            position = json.loads(clean["annotationPosition"])
            if not isinstance(position, dict) or type(position.get("pageIndex")) is not int or position["pageIndex"] < 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ValueError("Annotation requires a valid physical page position") from None
        current = self.annotation_record(scope, attachment_key, key)
        if current is not None:
            current_data = _data(current)
            if current_data.get("itemType") != "annotation" or current_data.get("parentItem") != attachment_key:
                raise ZoteroWorkspaceError("Annotation is bound to another attachment.", code="identity_mismatch")
            if not self._annotation_editable(scope, key, current, known_internal=known_internal):
                raise ZoteroWorkspaceError("Annotation editability has not been verified; use Zotero to edit it.", code="annotation_read_only")
            if current_data["version"] != expected_version:
                raise ZoteroWorkspaceError("Annotation changed; reconcile before saving.", code="version_conflict", status=412)
            if self._annotation_unchanged(current_data, clean):
                # Reader callbacks can repeat the already-saved value. Native
                # Zotero legitimately keeps its version unchanged for that case;
                # do not send a PATCH or consume single-use consent.
                self._require_write_capability()
                if self._load_credential() is None:
                    raise ZoteroWorkspaceError("Authorize this application in Zotero before writing.", code="authorization_required")
                if not isinstance(write_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,32}", write_token):
                    raise ValueError("A stable write token of 5–32 characters is required")
                return current
            return self._update(scope, key, clean, kind="items", expected_version=expected_version, write_token=write_token)
        if expected_version != 0:
            raise ZoteroWorkspaceError("Annotation no longer exists.", code="version_conflict", status=412)
        saved = self._create(scope, key, {**clean, "itemType": "annotation", "parentItem": attachment_key}, kind="items", write_token=write_token)
        self._created_annotations.add((str(self.server_id), scope, key))
        return saved

    @classmethod
    def _annotation_unchanged(cls, current: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
        if not cls._matches(current, {key: value for key, value in desired.items() if key != "annotationPosition"}):
            return False
        if "annotationPosition" not in desired:
            return True
        try:
            return json.loads(current.get("annotationPosition", "")) == json.loads(desired["annotationPosition"])
        except (ValueError, TypeError):
            return False

    def delete_annotation(self, scope: str, attachment_key: str, key: str, *, expected_version: int, write_token: str, known_internal: bool = False) -> dict[str, Any]:
        # A deletion is a narrow operation on one confirmed annotation, never a
        # general item-deletion interface. Query-free credentials stay in headers.
        _write_key(key)
        _write_key(attachment_key)
        self._pdf_item(scope, attachment_key)
        current = self.annotation_record(scope, attachment_key, key)
        if current is None:
            return {"deleted": True, "key": key, "already_absent": True}
        data = _data(current)
        if data.get("itemType") != "annotation" or data.get("parentItem") != attachment_key:
            raise ZoteroWorkspaceError("Only an internal annotation on this PDF may be removed.", code="identity_mismatch")
        if not self._annotation_editable(scope, key, current, known_internal=known_internal):
            raise ZoteroWorkspaceError("Annotation editability has not been verified; use Zotero to remove it.", code="annotation_read_only")
        if data.get("version") != _version(expected_version):
            raise ZoteroWorkspaceError("Annotation changed before deletion.", code="version_conflict", status=412)
        # Local single DELETE takes its version in a header; use the narrow
        # transport hook rather than a general caller-supplied header interface.
        self._request(f"{_scope(scope)}/items/{_key(key)}", method="DELETE", write_token=write_token, delete_version=expected_version)
        try:
            if self.item(scope, key) is not None:
                raise ZoteroWriteUncertain()
        except ZoteroWorkspaceError:
            raise ZoteroWriteUncertain() from None
        return {"deleted": True, "key": key}
