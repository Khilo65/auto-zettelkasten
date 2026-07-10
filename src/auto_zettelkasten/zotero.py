from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from . import ENGINE_VERSION
from .files import require_loopback_http_url


class ZoteroError(RuntimeError):
    pass


@dataclass(slots=True)
class ZoteroLocalClient:
    """First-party, read-only client for Zotero Desktop's local HTTP API."""

    base_url: str = "http://127.0.0.1:23119"
    library_id: str = "0"
    timeout: float = 20.0
    page_size: int = 100

    def __post_init__(self) -> None:
        require_loopback_http_url(self.base_url, label="Zotero base_url")

    def status(self) -> Mapping[str, Any]:
        try:
            self.collections(limit=1)
        except Exception as exc:
            return {
                "status": "unavailable",
                "base_url": self.base_url,
                "reason": f"{type(exc).__name__}: {exc}",
                "read_only": True,
            }
        return {"status": "available", "base_url": self.base_url, "read_only": True}

    def collections(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._paginate(f"users/{self.library_id}/collections")
        return rows[:limit] if limit is not None else rows

    def selected_collection(self) -> Mapping[str, Any]:
        # Connector endpoints use POST for queries, but this operation is read-only.
        last_error: ZoteroError | None = None
        for method in ("POST", "GET"):
            try:
                payload, _ = self._request(
                    "connector/getSelectedCollection",
                    method=method,
                    body=b"{}" if method == "POST" else None,
                    api=False,
                )
                value = json.loads(payload.decode("utf-8") or "{}")
                if isinstance(value, dict):
                    if "id" in value and value.get("id") is None:
                        raise ZoteroError("Zotero library root is selected; select a collection")
                    nested = value.get("collection") if isinstance(value.get("collection"), dict) else {}
                    key = (
                        value.get("collection_key")
                        or value.get("collectionKey")
                        or value.get("selected_collection_key")
                        or value.get("selectedCollectionKey")
                        or value.get("key")
                        or nested.get("key")
                    )
                    if key:
                        return {**value, "key": str(key), "scope": "collection"}
                    resolved_key = self._resolve_connector_collection_key(value)
                    if resolved_key:
                        return {**value, "key": resolved_key, "scope": "collection"}
            except ZoteroError as exc:
                if "library root is selected" in str(exc):
                    raise
                last_error = exc
                continue
        raise last_error or ZoteroError("Zotero did not report a selected collection")

    def _resolve_connector_collection_key(self, selected: Mapping[str, Any]) -> str:
        internal_id = selected.get("id")
        if internal_id is None:
            return ""
        selected_path = _connector_selected_path(selected, internal_id)
        if not selected_path:
            raise ZoteroError("cannot reconstruct selected Zotero collection path")
        collections = self.collections()
        matches = [
            str(row.get("key") or (row.get("data") or {}).get("key") or "")
            for row in collections
            if _api_collection_path(row, collections) == selected_path
        ]
        matches = [key for key in matches if key]
        if len(matches) != 1:
            raise ZoteroError(
                f"selected Zotero collection path is {'ambiguous' if matches else 'not present'} in the local API: {' / '.join(selected_path)}"
            )
        return matches[0]

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]:
        if scope == "selected":
            selected = self.selected_collection()
            collection_key = str(selected.get("key") or "")
            scope = "library" if selected.get("scope") == "library" else "collection"
            if scope == "collection" and not collection_key:
                raise ZoteroError("selected Zotero collection has no key")
        if scope == "collection":
            if not collection_key:
                raise ValueError("collection scope requires collection_key")
            quoted = urllib.parse.quote(collection_key, safe="")
            return self._paginate(f"users/{self.library_id}/collections/{quoted}/items/top")
        if scope != "library":
            raise ValueError("scope must be library, collection, or selected")
        return self._paginate(f"users/{self.library_id}/items/top")

    def children(self, item_key: str) -> list[dict[str, Any]]:
        quoted = urllib.parse.quote(item_key, safe="")
        return self._paginate(f"users/{self.library_id}/items/{quoted}/children")

    def item(self, item_key: str) -> Mapping[str, Any] | None:
        quoted = urllib.parse.quote(item_key, safe="")
        try:
            payload, _ = self._request(f"users/{self.library_id}/items/{quoted}")
        except ZoteroError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        value = json.loads(payload.decode("utf-8") or "{}")
        return value if isinstance(value, dict) else None

    def fulltext(self, item_key: str) -> Mapping[str, Any] | None:
        quoted = urllib.parse.quote(item_key, safe="")
        try:
            payload, _ = self._request(f"users/{self.library_id}/items/{quoted}/fulltext")
        except ZoteroError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        value = json.loads(payload.decode("utf-8") or "{}")
        return value if isinstance(value, dict) else None

    def file(self, item_key: str) -> tuple[bytes, str] | None:
        quoted = urllib.parse.quote(item_key, safe="")
        direct_error: ZoteroError | None = None
        try:
            payload, headers = self._request(f"users/{self.library_id}/items/{quoted}/file")
        except ZoteroError as exc:
            direct_error = exc
        else:
            return payload, str(headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]

        # Zotero may return a redirect that urllib refuses because its target is
        # file://. The view/url route exposes that local URI without requiring
        # direct database access or a Zotero write.
        try:
            location_payload, _ = self._request(f"users/{self.library_id}/items/{quoted}/file/view/url")
            location = _file_location(location_payload)
            if location:
                path = _local_file_uri(location)
                if path and path.exists() and path.is_file():
                    return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        except (OSError, ZoteroError, ValueError):
            pass
        if direct_error and "HTTP 404" not in str(direct_error):
            raise direct_error
        return None

    def _paginate(self, endpoint: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            separator = "&" if "?" in endpoint else "?"
            page_endpoint = f"{endpoint}{separator}limit={self.page_size}&start={start}"
            payload, headers = self._request(page_endpoint)
            value = json.loads(payload.decode("utf-8") or "[]")
            if not isinstance(value, list):
                raise ZoteroError(f"expected a list from {endpoint}")
            page = [row for row in value if isinstance(row, dict)]
            rows.extend(page)
            total = _integer_header(headers, "Total-Results")
            start += len(page)
            if not page or len(page) < self.page_size or (total is not None and start >= total):
                break
        return rows

    def _request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        api: bool = True,
    ) -> tuple[bytes, Mapping[str, str]]:
        prefix = "/api/" if api else "/"
        url = f"{self.base_url.rstrip('/')}{prefix}{endpoint.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"auto-zettelkasten/{ENGINE_VERSION}",
                "Zotero-API-Version": "3",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(512).decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise ZoteroError(f"HTTP {exc.code} from Zotero: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ZoteroError(f"cannot reach Zotero at {self.base_url}: {exc.reason}") from exc


def _integer_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name) or headers.get(name.lower())
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _file_location(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("url") or value.get("file") or value.get("path") or "")
    return ""


def _local_file_uri(value: str) -> Path | None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    return Path(path).expanduser().resolve()


def _connector_selected_path(selected: Mapping[str, Any], internal_id: Any) -> tuple[str, ...]:
    targets = selected.get("targets", [])
    if not isinstance(targets, list):
        return ()
    wanted = {str(internal_id), f"C{internal_id}"}
    stack: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        try:
            level = max(0, int(target.get("level", 0)))
        except (TypeError, ValueError):
            level = 0
        stack = stack[:level]
        stack.append(str(target.get("name") or ""))
        if str(target.get("id")) in wanted:
            return tuple(part for part in stack[1:] if part)
    return ()


def _api_collection_path(row: Mapping[str, Any], rows: list[dict[str, Any]]) -> tuple[str, ...]:
    by_key = {
        str(item.get("key") or (item.get("data") or {}).get("key") or ""): item
        for item in rows
        if isinstance(item, Mapping)
    }
    path: list[str] = []
    current: Mapping[str, Any] | None = row
    seen: set[str] = set()
    while current:
        data = current.get("data", current)
        if not isinstance(data, Mapping):
            break
        key = str(current.get("key") or data.get("key") or "")
        if key in seen:
            break
        seen.add(key)
        path.append(str(data.get("name") or ""))
        parent = str(data.get("parentCollection") or "")
        current = by_key.get(parent) if parent else None
    return tuple(part for part in reversed(path) if part)
