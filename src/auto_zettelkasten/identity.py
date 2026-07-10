from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any, Mapping, Sequence

from .files import sha256_text
from .notes import item_data, item_key

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE)
YEAR_RE = re.compile(r"(?:18|19|20|21)\d{2}")


def normalize_doi(value: Any) -> str:
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    return match.group(0).rstrip(".,;)]}").casefold() if match else ""


def normalize_url(value: Any) -> str:
    text = str(value or "").strip().rstrip(".,;)]}")
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, val) for key, val in query if not key.casefold().startswith("utm_")]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, urllib.parse.urlencode(query), "")
    )


def normalize_isbn(value: Any) -> str:
    text = re.sub(r"[^0-9Xx]", "", str(value or ""))
    return text.upper() if len(text) in {10, 13} else ""


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def year_value(value: Any) -> str:
    match = YEAR_RE.search(str(value or ""))
    return match.group(0) if match else ""


def creator_names(value: Any) -> list[str]:
    rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
    names: list[str] = []
    for creator in rows:
        if isinstance(creator, Mapping):
            name = str(
                creator.get("name")
                or " ".join(
                    part for part in (str(creator.get("firstName") or ""), str(creator.get("lastName") or "")) if part
                )
            ).strip()
        else:
            name = str(creator).strip()
        if name and name not in names:
            names.append(name)
    return names


def first_author(value: Any) -> str:
    names = creator_names(value)
    if not names:
        return ""
    return normalize_title(names[0])


def bibliographic_tuple(value: Mapping[str, Any]) -> tuple[str, str, str]:
    metadata = work_metadata(value)
    return (
        normalize_title(metadata.get("title")),
        str(metadata.get("year") or ""),
        first_author(metadata.get("authors")),
    )


def work_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize Zotero, provider, and citation rows without retaining source text."""

    data = item_data(value) if ("data" in value or "key" in value) else dict(value)
    external_ids = data.get("externalIds", {}) if isinstance(data.get("externalIds"), Mapping) else {}
    provider_ids = data.get("provider_ids", {}) if isinstance(data.get("provider_ids"), Mapping) else {}
    paper_id = str(data.get("paperId") or data.get("semantic_scholar_id") or provider_ids.get("semantic_scholar") or "")
    raw_url = data.get("url") or data.get("URL")
    doi = normalize_doi(data.get("DOI") or data.get("doi") or external_ids.get("DOI") or raw_url)
    url = normalize_url(raw_url)
    authors = creator_names(data.get("creators") or data.get("authors") or [])
    title = str(data.get("title") or "").strip()
    year = year_value(data.get("year") or data.get("date") or data.get("issued"))
    isbn = normalize_isbn(data.get("ISBN") or data.get("isbn"))
    result = {
        "title": title,
        "year": year,
        "authors": authors,
        "doi": doi,
        "url": url,
        "isbn": isbn,
        "provider_ids": ({"semantic_scholar": paper_id} if paper_id else {}),
        "zotero_item_key": item_key(value),
    }
    if result["zotero_item_key"]:
        result["local_zotero_item"] = dict(value)
    return result


def identify_work(value: Mapping[str, Any]) -> tuple[str, str]:
    data = work_metadata(value)
    if data["doi"]:
        return f"work-doi-{sha256_text(str(data['doi']))[:20]}", "ready"
    semantic_id = str(data["provider_ids"].get("semantic_scholar") or "")
    if semantic_id:
        return f"work-s2-{sha256_text(semantic_id)[:20]}", "ready"
    if data["url"]:
        return f"work-url-{sha256_text(str(data['url']))[:20]}", "ready"
    if data["isbn"]:
        return f"work-isbn-{sha256_text(str(data['isbn']))[:20]}", "ready"
    title, year, author = bibliographic_tuple(data)
    payload = "|".join((title, year, author))
    if title and year and author:
        # A bibliographic tuple is only a candidate identity until the engine
        # proves it unique within the reconciliation universe.
        return f"work-title-{sha256_text(payload)[:20]}", "resolve_identity"
    if title:
        return f"work-unresolved-{sha256_text(payload)[:20]}", "resolve_identity"
    return f"work-unresolved-{sha256_text(repr(sorted(value.items())))[:20]}", "resolve_identity"


def merge_work_metadata(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key in ("title", "year", "doi", "url", "isbn", "zotero_item_key"):
        if not result.get(key) and right.get(key):
            result[key] = right[key]
    result["authors"] = list(result.get("authors") or right.get("authors") or [])
    result["provider_ids"] = {**dict(right.get("provider_ids") or {}), **dict(result.get("provider_ids") or {})}
    if not result.get("local_zotero_item") and right.get("local_zotero_item"):
        result["local_zotero_item"] = dict(right["local_zotero_item"])
    return result
