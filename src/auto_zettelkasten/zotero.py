from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .files import require_loopback_http_url, sha256_text


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
        """Return one Zotero item without making the client port stricter."""

        quoted = urllib.parse.quote(item_key, safe="")
        try:
            payload, _ = self._request(f"users/{self.library_id}/items/{quoted}")
        except ZoteroError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        value = json.loads(payload.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ZoteroError(f"expected an item object for {item_key}")
        return value

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
                "User-Agent": "auto-zettelkasten/0.7.0",
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


def normalize_collection_snapshot(
    collections: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    *,
    parent_items: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize a complete read-only Zotero inventory into stable diffable state."""

    normalized_collections: list[dict[str, Any]] = []
    for raw in collections:
        data = raw.get("data", raw)
        if not isinstance(data, Mapping):
            data = {}
        key = str(raw.get("key") or data.get("key") or "").strip()
        if not key:
            continue
        semantic = {
            "key": key,
            "name": str(data.get("name") or "").strip(),
            "parent_key": str(data.get("parentCollection") or "").strip(),
            "version": _stable_revision(raw, data),
        }
        normalized_collections.append(
            {
                **semantic,
                "fingerprint": _stable_fingerprint(semantic),
            }
        )
    normalized_collections.sort(key=lambda row: row["key"])

    parents = parent_items or {}
    normalized_items: list[dict[str, Any]] = []
    for raw in items:
        data = raw.get("data", raw)
        if not isinstance(data, Mapping):
            data = {}
        key = str(raw.get("key") or data.get("key") or "").strip()
        if not key:
            continue
        collection_keys = sorted(
            {
                str(value).strip()
                for value in data.get("collections", []) or []
                if str(value).strip()
            }
        )
        parent_key = str(data.get("parentItem") or "").strip()
        parent_raw = parents.get(parent_key, {}) if parent_key else {}
        parent_metadata = _parent_metadata(parent_raw) if parent_raw else {}
        identity_metadata = parent_metadata or {
            "title": str(data.get("title") or ""),
            "creators": _stable_value(data.get("creators", []) or []),
            "date": str(data.get("date") or ""),
            "doi": str(data.get("DOI") or data.get("doi") or ""),
            "isbn": str(data.get("ISBN") or data.get("isbn") or ""),
            "url": str(data.get("url") or ""),
            "relations": _stable_value(data.get("relations", {}) or {}),
        }
        content = {
            key_name: _stable_value(value)
            for key_name, value in data.items()
            if key_name != "collections"
        }
        semantic = {
            "key": key,
            "version": _stable_revision(raw, data),
            "item_type": str(data.get("itemType") or "").strip(),
            "parent_item_key": parent_key,
            "collection_keys": collection_keys,
            "content_fingerprint": _stable_fingerprint(
                {
                    "content": content,
                    "parent_metadata": parent_metadata,
                }
            ),
            "parent_metadata": parent_metadata,
            "identity": {
                "title": str(identity_metadata.get("title") or ""),
                "creators": _stable_value(
                    identity_metadata.get("creators", []) or []
                ),
                "year": _publication_year(
                    str(identity_metadata.get("date") or "")
                ),
                "doi": str(identity_metadata.get("doi") or ""),
                "isbn": str(identity_metadata.get("isbn") or ""),
                "url": str(identity_metadata.get("url") or ""),
                "relations": _stable_value(
                    identity_metadata.get("relations", {}) or {}
                ),
            },
        }
        normalized_items.append(
            {
                **semantic,
                "fingerprint": _stable_fingerprint(semantic),
            }
        )
    normalized_items.sort(key=lambda row: row["key"])

    semantic_snapshot = {
        "schema_version": "2",
        "collections": normalized_collections,
        "items": normalized_items,
    }
    return {
        **semantic_snapshot,
        "fingerprint": _stable_fingerprint(semantic_snapshot),
    }


def diff_collection_snapshots(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return stable Zotero item, membership, rename, and move changes."""

    previous = previous or {}
    old_items = _rows_by_key(previous.get("items"))
    new_items = _rows_by_key(current.get("items"))
    old_collections = _rows_by_key(previous.get("collections"))
    new_collections = _rows_by_key(current.get("collections"))
    shared_items = sorted(old_items.keys() & new_items.keys())
    shared_collections = sorted(old_collections.keys() & new_collections.keys())
    return {
        "new_item_keys": sorted(new_items.keys() - old_items.keys()),
        "changed_item_keys": [
            key
            for key in shared_items
            if old_items[key].get("content_fingerprint")
            != new_items[key].get("content_fingerprint")
        ],
        "removed_item_keys": sorted(old_items.keys() - new_items.keys()),
        "membership_changed_item_keys": [
            key
            for key in shared_items
            if list(old_items[key].get("collection_keys") or [])
            != list(new_items[key].get("collection_keys") or [])
        ],
        "new_collection_keys": sorted(new_collections.keys() - old_collections.keys()),
        "removed_collection_keys": sorted(old_collections.keys() - new_collections.keys()),
        "renamed_collection_keys": [
            key
            for key in shared_collections
            if str(old_collections[key].get("name") or "")
            != str(new_collections[key].get("name") or "")
        ],
        "moved_collection_keys": [
            key
            for key in shared_collections
            if str(old_collections[key].get("parent_key") or "")
            != str(new_collections[key].get("parent_key") or "")
        ],
    }


def scope_collection_snapshot(
    snapshot: Mapping[str, Any],
    *,
    scope: str,
    collection_key: str = "",
    item_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the portion whose successful processing a sync may acknowledge."""

    if scope not in {"library", "collection"}:
        raise ValueError("snapshot scope must be library or collection")
    if scope == "collection" and not collection_key:
        raise ValueError("collection snapshot scope requires collection_key")
    if scope == "library":
        collections = [
            dict(row)
            for row in snapshot.get("collections", []) or []
            if isinstance(row, Mapping)
        ]
        items = [
            dict(row)
            for row in snapshot.get("items", []) or []
            if isinstance(row, Mapping)
        ]
    else:
        by_collection = _rows_by_key(snapshot.get("collections"))
        relevant_collection_keys: set[str] = set()
        current_key = collection_key
        while current_key and current_key not in relevant_collection_keys:
            relevant_collection_keys.add(current_key)
            current = by_collection.get(current_key, {})
            current_key = str(current.get("parent_key") or "")
        collections = [
            dict(by_collection[key])
            for key in sorted(relevant_collection_keys)
            if key in by_collection
        ]
        wanted_items = {str(value).upper() for value in item_keys if str(value)}
        items = [
            dict(row)
            for row in snapshot.get("items", []) or []
            if isinstance(row, Mapping)
            and str(row.get("key") or "").upper() in wanted_items
        ]
    semantic = {
        "schema_version": str(snapshot.get("schema_version") or "1"),
        "scope": {
            "kind": scope,
            "collection_key": collection_key if scope == "collection" else "",
        },
        "collections": collections,
        "items": items,
    }
    return {**semantic, "fingerprint": _stable_fingerprint(semantic)}


def _stable_revision(raw: Mapping[str, Any], data: Mapping[str, Any]) -> int | str:
    value = raw.get("version", data.get("version", ""))
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value or "")


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_stable_value(item) for item in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def _stable_fingerprint(value: Mapping[str, Any]) -> str:
    return sha256_text(
        json.dumps(
            _stable_value(value),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _parent_metadata(raw: Mapping[str, Any]) -> dict[str, Any]:
    data = raw.get("data", raw)
    if not isinstance(data, Mapping):
        data = {}
    return {
        "key": str(raw.get("key") or data.get("key") or ""),
        "version": _stable_revision(raw, data),
        "item_type": str(data.get("itemType") or ""),
        "title": str(data.get("title") or ""),
        "creators": _stable_value(data.get("creators", []) or []),
        "date": str(data.get("date") or ""),
        "publication_title": str(data.get("publicationTitle") or ""),
        "publisher": str(data.get("publisher") or ""),
        "doi": str(data.get("DOI") or ""),
        "isbn": str(data.get("ISBN") or ""),
        "url": str(data.get("url") or ""),
        "relations": _stable_value(data.get("relations", {}) or {}),
    }


def _publication_year(value: str) -> str:
    match = re.search(r"(?:19|20)\d{2}", value)
    return match.group(0) if match else ""


def _rows_by_key(value: Any) -> dict[str, Mapping[str, Any]]:
    rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
    return {
        str(row.get("key")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("key")
    }


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
