"""Strict source-navigation URIs shared by notes and the local reader bridge."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_KEY = re.compile(r"[A-Z0-9]{8}")
_URI = re.compile(r"zotero://(select|open-pdf)/(library|groups/([1-9][0-9]*))/items/([A-Z0-9]{8})(?:\?([^#]+))?")


@dataclass(frozen=True)
class ZoteroLink:
    action: str
    scope: str
    library_type: str
    library_id: str | None
    key: str
    page: int | None
    page_index: int | None
    annotation_key: str | None
    uri: str


def parse_zotero_uri(value: object) -> ZoteroLink | None:
    """Parse supported targets only; ``page`` is physical, one-based, never a label.

    ``library`` identifies the connected personal library. A caller persisting a
    binding must also record that connection's library identity.
    """
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    match = _URI.fullmatch(re.sub(r"^zotero://", "zotero://", value, flags=re.I))
    if not match:
        return None
    action, scope, group_id, key, query = match.groups()
    params: dict[str, str] = {}
    if query:
        if action != "open-pdf":
            return None
        for part in query.split("&"):
            entry = re.fullmatch(r"(page|annotation)=([^=]+)", part)
            if not entry or entry[1] in params:
                return None
            name, value = entry.groups()
            if name == "page" and (not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 9007199254740991):
                return None
            if name == "annotation" and not _KEY.fullmatch(value):
                return None
            params[name] = value
    page = int(params["page"]) if "page" in params else None
    annotation = params.get("annotation")
    suffix = "&".join(part for part in (
        f"page={page}" if page is not None else "",
        f"annotation={annotation}" if annotation else "",
    ) if part)
    return ZoteroLink(action, scope, "group" if group_id else "user", group_id, key,
        page, page - 1 if page is not None else None, annotation,
        f"zotero://{action}/{scope}/items/{key}" + (f"?{suffix}" if suffix else ""))


def item_library_scope(item: Mapping[str, Any], *, fallback: str | None = None) -> str | None:
    """Use explicit library metadata, never a title or an internal libraryID.

    Legacy callers of the personal-only mapper can supply ``library`` as their
    fallback. Explicit invalid/unknown library metadata never takes that fallback.
    """
    if "library" not in item:
        return fallback
    library = item.get("library")
    if not isinstance(library, Mapping):
        return None
    if library.get("type") == "user":
        return "library"
    if library.get("type") == "group":
        group_id = str(library.get("id") or "")
        return f"groups/{group_id}" if re.fullmatch(r"[1-9][0-9]*", group_id) else None
    return None


def uri_for_item(item: Mapping[str, Any], action: str = "select", *, key: str | None = None,
                 fallback_scope: str | None = None) -> str | None:
    data = item.get("data", item)
    if not isinstance(data, Mapping):
        return None
    selected_key = key if key is not None else item.get("key", data.get("key"))
    scope = item_library_scope(item, fallback=fallback_scope)
    if scope is None or not isinstance(selected_key, str) or not _KEY.fullmatch(selected_key):
        return None
    parsed = parse_zotero_uri(f"zotero://{action}/{scope}/items/{selected_key}")
    return parsed.uri if parsed else None
