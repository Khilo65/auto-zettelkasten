"""Offline bibliography discovery; signals are candidates, never research decisions.

Callers supply extracted text and its coverage; this module does no file extraction,
network access or Zotero writes. Library entries are ``{identity, item}`` wrappers
around existing Zotero metadata. A source_binding is an existing source/catalogue
row, not a source ID derived from an attachment path.

An optional local parser accepts text and returns ``{references: [...], complete:
bool}``. Each reference may contain identifiers (doi/isbn), title, author (first
author's surname or full name), year, item_type, edition, and locator. Only the
parser can assert bibliography completeness. Bump parser_version whenever its
behavior changes. The baseline retains unparsed bibliography lines honestly.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


CACHE_FILENAME = "discovery.sqlite3"
CACHE_RELATIVE_PATH = Path(".auto-zettelkasten") / CACHE_FILENAME
BASELINE_PARSER_VERSION = "bibliography-lines-v1"
_SCHEMA_VERSION = "1"
_MAX_TEXT_BYTES = 8_000_000
_MAX_METADATA_BYTES = 262_144
_MAX_RECORD_BYTES = 2_000_000
_MAX_REFERENCES = 2_000
_MAX_LIBRARY_ITEMS = 20_000
_IDENTITY_FIELDS = ("instance_id", "library_type", "library_id", "item_key")
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
_HEADING = re.compile(r"^(?:references|bibliography|works cited)\s*:?$", re.IGNORECASE)
_CHAPTER = re.compile(r"^(?:chapter\s+\w+|appendix\b|#{1,6}\s+)", re.IGNORECASE)


def _string(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("expected a bounded string without NUL characters")
    return value.strip()


def _json(value: Any, limit: int) -> str:
    # Reject oversized/deep structures before allocating their serialized copy.
    remaining = [limit]

    def check(node: Any, depth: int = 0) -> None:
        remaining[0] -= 1
        if depth > 12 or remaining[0] < 0:
            raise ValueError("discovery input is too large or deeply nested")
        if isinstance(node, str):
            remaining[0] -= len(node)
        elif isinstance(node, Mapping):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError("discovery object keys must be strings")
                check(key, depth + 1)
                check(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                check(item, depth + 1)
        elif node is not None and not isinstance(node, (int, float, bool)):
            raise ValueError("discovery inputs must be JSON-compatible")
        if remaining[0] < 0:
            raise ValueError("discovery input is too large")

    check(value)
    try:
        result = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid discovery JSON input") from exc
    if len(result.encode("utf-8")) > limit:
        raise ValueError("discovery input is too large")
    return result


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("explicit Zotero identity is required")
    result = {key: _string(value.get(key), 200) for key in _IDENTITY_FIELDS}
    if any(not part for part in result.values()):
        raise ValueError("instance, library and item identity must be explicit")
    if result["library_type"] != "user" or not re.fullmatch(r"[0-9]+", result["library_id"]):
        raise ValueError("only explicit personal (user) Zotero libraries are supported")
    if not re.fullmatch(r"[A-Z0-9]{1,32}", result["item_key"]):
        raise ValueError("invalid Zotero item key")
    return result


def _key(identity: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(identity[name] for name in _IDENTITY_FIELDS)


def _norm(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _year(value: Any) -> str | None:
    match = re.search(r"\b[12]\d{3}\b", _string(value or ""))
    return match.group() if match else None


def _identifiers(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("identifiers must be an object")
    result = {}
    doi = unquote(_string(value.get("doi", value.get("DOI", "")) or ""))
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.IGNORECASE).rstrip(".,;")
    while doi and doi[-1] in ")]" and doi.count(doi[-1]) > doi.count({")": "(", "]": "["}[doi[-1]]):
        doi = doi[:-1].rstrip(".,;")
    if _DOI.fullmatch(doi):
        result["doi"] = doi.casefold()
    isbn = re.sub(r"[\s-]", "", _string(value.get("isbn", value.get("ISBN", "")) or "")).upper()
    if re.fullmatch(r"\d{9}[\dX]", isbn):
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn)) % 11 == 0:
            prefix = "978" + isbn[:9]
            check = (-sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(prefix))) % 10
            result["isbn"] = prefix + str(check)
    elif re.fullmatch(r"\d{13}", isbn):
        if sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn)) % 10 == 0:
            result["isbn"] = isbn
    return result


def _metadata(item: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError("item must be existing Zotero metadata")
    _json(item, _MAX_METADATA_BYTES)
    data = item.get("data", item)
    if not isinstance(data, Mapping):
        raise ValueError("item data must be an object")
    keys = [item.get("key"), data.get("key"), data.get("zotero_item_key")]
    if not any(keys) or any(key != identity["item_key"] for key in keys if key):
        raise ValueError("metadata item key does not match explicit identity")
    library = item.get("library")
    if library is not None:
        if not isinstance(library, Mapping) or library.get("type") != "user" or str(library.get("id")) != identity["library_id"]:
            raise ValueError("metadata library does not match explicit personal library")
    creators = data.get("creators", [])
    tags = data.get("tags", [])
    collections = data.get("collections", [])
    relations = data.get("relations", data.get("zotero_relations", {}))
    if not all(isinstance(value, list) for value in (creators, tags, collections)) or not isinstance(relations, Mapping):
        raise ValueError("invalid creators, tags, collections or relations")
    clean_creators = []
    for creator in creators:
        if not isinstance(creator, Mapping):
            raise ValueError("creator must be an object")
        clean_creators.append({key: _string(creator[key]) for key in ("firstName", "lastName", "name", "creatorType") if key in creator})
    clean_relations = {}
    for predicate, values in relations.items():
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list):
            raise ValueError("relation values must be strings or lists")
        clean_relations[_string(predicate)] = sorted({_string(value) for value in values})
    return {
        "title": _string(data.get("title", "")),
        "creators": clean_creators,
        "year": _year(data.get("year", data.get("date", ""))),
        "item_type": _string(data.get("itemType", data.get("item_type", ""))),
        "edition": _string(data.get("edition", "")),
        "identifiers": _identifiers(data.get("identifiers", data)),
        "tags": sorted({_string(tag.get("tag") if isinstance(tag, Mapping) else tag) for tag in tags}),
        "collections": sorted({_string(value) for value in collections}),
        "relations": clean_relations,
    }


def _binding(value: Mapping[str, Any] | None, identity: Mapping[str, str]) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        raise ValueError("source_binding must be existing source metadata or a catalogue row")
    _json(value, _MAX_METADATA_BYTES)
    supplied = value.get("identity", {})
    if not isinstance(supplied, Mapping):
        raise ValueError("invalid source binding identity")
    for container in (value, supplied):
        for field in _IDENTITY_FIELDS[:3]:
            if field in container and container[field] != identity[field]:
                raise ValueError("source binding crosses Zotero instance or library")
    keys = {_string(key) for key in (value.get("zotero_item_key"), value.get("zotero_key"), value.get("key"), supplied.get("item_key"), supplied.get("canonical_zotero_key")) if key not in (None, "")}
    aliases = supplied.get("zotero_item_keys", [])
    if not isinstance(aliases, list):
        raise ValueError("source binding aliases must be a list")
    aliases = {_string(key) for key in aliases}
    if len(keys) > 1 and not keys.issubset(aliases):
        raise ValueError("conflicting source binding keys require explicit catalogue aliases")
    keys.update(aliases)
    if identity["item_key"] not in keys:
        raise ValueError("source binding requires an exact existing Zotero item key")
    source_id = _string(value.get("source_id"))
    if not source_id:
        raise ValueError("source binding requires an existing source_id")
    note_id = _string(value.get("note_id", "")) or None
    return source_id, note_id


def _coverage(value: Mapping[str, Any], text: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("coverage must be explicit")
    _json(value, _MAX_METADATA_BYTES)
    status = _string(value.get("status", "not_examined"))
    scope = _string(value.get("source_scope", "metadata_only"))
    if status not in {"succeeded", "partial", "failed", "scanned", "not_examined", "unavailable"}:
        raise ValueError("invalid extraction status")
    if scope not in {"full_document", "partial_document", "abstract_only", "metadata_only"}:
        raise ValueError("invalid source scope")
    result = {"status": status, "reason": _string(value.get("reason", "")), "route": _string(value.get("route", "")), "source_scope": scope}
    result["coverage_gate"] = _string(value.get("coverage_gate", value.get("gate", "unknown")))
    if result["coverage_gate"] not in {"passed", "limited", "failed", "unknown"}:
        raise ValueError("invalid coverage gate")
    for field, alias in (("indexed_pages", "indexedPages"), ("total_pages", "totalPages"), ("indexed_chars", "indexedChars"), ("total_chars", "totalChars")):
        count = value.get(field, value.get(alias))
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("coverage counts must be nonnegative integers or null")
        result[field] = count
    incomplete = any(result[f"total_{unit}"] is not None and result[f"total_{unit}"] > 0 and
                     (result[f"indexed_{unit}"] is None or result[f"indexed_{unit}"] < result[f"total_{unit}"])
                     for unit in ("pages", "chars"))
    result["sufficient"] = bool(text.strip() and status == "succeeded" and scope == "full_document" and not incomplete
                                and result["coverage_gate"] == "passed")
    if incomplete or (scope == "full_document" and not result["sufficient"]):
        result["reason"] = result["reason"] or "full_document_coverage_not_established"
    return result


def baseline_references(text: str) -> dict[str, Any]:
    """Retain bounded lines after explicit bibliography headings, without name guesses."""
    rows = []
    active = False
    for number, line in enumerate(io.StringIO(text), 1):
        line = line.strip()
        if _HEADING.fullmatch(line.lstrip("# ")):
            active = True
            continue
        if active and _CHAPTER.match(line):
            active = False
        if not active or not line or line.isdecimal():
            continue
        if len(rows) >= _MAX_REFERENCES:
            return {"references": rows, "complete": False, "reason": "reference_limit"}
        dois = _DOI.findall(line)
        rows.append({"identifiers": {"doi": dois[0]} if len(dois) == 1 else {},
                     "unparsed": line[:512], "raw_fingerprint": _hash(line), "locator": {"line": number}})
    return {"references": rows, "complete": False, "reason": "baseline_has_no_structured_bibliography_parser"}


def _reference(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("reference must be an object")
    _json(row, _MAX_METADATA_BYTES)
    identifiers = row.get("identifiers", {})
    locator = row.get("locator", {})
    if not isinstance(identifiers, Mapping) or not isinstance(locator, Mapping):
        raise ValueError("invalid reference identifiers or locator")
    clean_locator = {}
    for key in ("page", "line", "chapter", "section"):
        if key in locator:
            value = locator[key]
            if type(value) is int and value > 0:
                clean_locator[key] = value
            elif isinstance(value, str):
                clean_locator[key] = _string(value, 200)
            else:
                raise ValueError("invalid reference locator")
    result = {"identifiers": _identifiers(identifiers), "title": _string(row.get("title") or "") or None,
              "author": _string(row.get("author") or "") or None, "year": _year(row.get("year")),
              "item_type": _string(row.get("item_type") or ""), "edition": _string(row.get("edition") or ""),
              "locator": clean_locator}
    if row.get("unparsed"):
        result["unparsed"] = _string(row["unparsed"], 512)
    if row.get("raw_fingerprint"):
        digest = _string(row["raw_fingerprint"], 64)
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid raw reference fingerprint")
        result["raw_fingerprint"] = digest
    return result


def _author_names(metadata: Mapping[str, Any]) -> set[str]:
    for creator in metadata["creators"]:
        if creator.get("creatorType", "author") == "author":
            return {_norm(name) for name in (creator.get("lastName", ""), creator.get("name", ""),
                    " ".join((creator.get("firstName", ""), creator.get("lastName", ""))),
                    ", ".join((creator.get("lastName", ""), creator.get("firstName", "")))) if name.strip(", ")}
    return set()


class ReferenceMatcher:
    """Reusable compact library index; matching never crosses instance/library scope."""

    def __init__(self, library_items: Sequence[Mapping[str, Any]] = ()) -> None:
        if not isinstance(library_items, (list, tuple)) or len(library_items) > _MAX_LIBRARY_ITEMS:
            raise ValueError("library_items must be a bounded list or tuple")
        self.entries = {}
        self.by_identifier = defaultdict(set)
        self.by_title = defaultdict(set)
        byte_count = 0
        for row in library_items:
            if not isinstance(row, Mapping):
                raise ValueError("library entries require identity and item metadata")
            identity = _identity(row.get("identity"))
            key = _key(identity)
            metadata = _metadata(row.get("item"), identity)
            byte_count += len(_json(metadata, _MAX_METADATA_BYTES).encode("utf-8"))
            if byte_count > 32_000_000:
                raise ValueError("compact library metadata is too large")
            if key in self.entries:
                raise ValueError("duplicate full library item identity")
            self.entries[key] = metadata
            scope = key[:3]
            for kind, identifier in metadata["identifiers"].items():
                self.by_identifier[(*scope, kind, identifier)].add(key)
            self.by_title[(*scope, _norm(metadata["title"]))].add(key)
        self.fingerprint = _hash(_json([(list(key), value) for key, value in sorted(self.entries.items())], 32_000_000))

    def match(self, reference: Mapping[str, Any], *, identity: Mapping[str, Any]) -> dict[str, Any]:
        row = _reference(reference)
        scope = _key(_identity(identity))[:3]
        identifiers = row["identifiers"]
        candidates = set()
        for kind, value in identifiers.items():
            # An ISBN identifies a volume, not a chapter within that volume.
            if kind == "isbn" and row["item_type"] == "bookSection":
                continue
            candidates.update(self.by_identifier.get((*scope, kind, value), ()))
        identifier_match = bool(candidates)
        if not candidates and row["title"] and row["author"] and row["year"]:
            for key in self.by_title.get((*scope, _norm(row["title"])), ()):
                item = self.entries[key]
                if item["year"] == row["year"] and _norm(row["author"]) in _author_names(item):
                    candidates.add(key)
        conflicting = False
        for key in candidates:
            target = self.entries[key]
            if any(kind in target["identifiers"] and target["identifiers"][kind] != value
                   for kind, value in identifiers.items()):
                conflicting = True
            for field in ("item_type", "edition"):
                if row[field] and target[field] != row[field]:
                    conflicting = True
            if identifier_match:
                if "isbn" in identifiers and "doi" not in identifiers and target["item_type"] == "bookSection":
                    conflicting = True
                for field in ("title", "year"):
                    if row[field] and target[field] and _norm(row[field]) != _norm(target[field]):
                        conflicting = True
                if row["author"] and _author_names(target) and _norm(row["author"]) not in _author_names(target):
                    conflicting = True
        if conflicting:
            status = "identifier_conflict" if identifiers else "unresolved"
        elif len(candidates) > 1:
            status = "ambiguous"
        elif candidates:
            status = "matched_identifier" if identifier_match else "matched_title_author_year"
        else:
            status = "external" if identifiers or (row["title"] and row["author"] and row["year"]) else "unresolved"
        matched = sorted(candidates) if status.startswith("matched_") else []
        work_id = None
        if status not in {"ambiguous", "identifier_conflict", "unresolved"}:
            # An unknown DOI-free edition must not become an invented shared work.
            canonical = self.entries[matched[0]]["identifiers"] if matched else identifiers
            item_type = self.entries[matched[0]]["item_type"] if matched else row["item_type"]
            if "doi" in canonical:
                work_id = "doi:" + canonical["doi"]
            elif "isbn" in canonical and item_type == "book":
                work_id = "isbn:" + canonical["isbn"]
            elif matched:
                work_id = "zotero:" + _hash(_json(list(matched[0]), 2_000))
            elif row["item_type"] == "journalArticle" and row["title"] and row["author"] and row["year"]:
                work_id = "title:" + _hash(_json([_norm(row["title"]), _norm(row["author"]), row["year"]], 10_000))
        return {**row, "work_id": work_id, "match_status": status,
                "matched_item_keys": [key[-1] for key in matched],
                "candidate_item_keys": [key[-1] for key in sorted(candidates)] if not matched else []}


class DiscoveryCache:
    """One hidden compact SQLite file; corrupt caches fail explicitly and stay intact."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        _cache_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS documents (instance_id TEXT NOT NULL, library_type TEXT NOT NULL, library_id TEXT NOT NULL, item_key TEXT NOT NULL, fingerprint TEXT NOT NULL, parser_version TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY (instance_id, library_type, library_id, item_key))")
            connection.commit()

    def get(self, identity: Mapping[str, Any], fingerprint: str) -> dict[str, Any] | None:
        identity = _identity(identity)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT record FROM documents WHERE instance_id = ? AND library_type = ? AND library_id = ? AND item_key = ? AND fingerprint = ? AND length(CAST(record AS BLOB)) <= ?",
                                     (*_key(identity), fingerprint, _MAX_RECORD_BYTES)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
            _validate_record(record)
            if (record["identity"] == identity and record["input_fingerprint"] == fingerprint and
                    record["coverage"]["sufficient"] is True and record["bibliography"]["status"] != "failed"):
                return record
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            pass
        return None

    def put(self, record: Mapping[str, Any]) -> None:
        _validate_record(record)
        identity = _identity(record.get("identity"))
        serialized = _json(record, _MAX_RECORD_BYTES)
        fingerprint = _string(record.get("input_fingerprint"), 64)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)", (*_key(identity), fingerprint, record["parser_version"], serialized))
            connection.commit()


def _cache_path(path: Path) -> None:
    if path.name != CACHE_FILENAME or path.parent.name != ".auto-zettelkasten":
        raise ValueError(f"discovery cache path must end with {CACHE_RELATIVE_PATH}")


def load_cached_records(path: Path | str, *, instance_id: str, library_id: str,
                        library_type: str = "user", parser_version: str = BASELINE_PARSER_VERSION,
                        limit: int = 10_000, max_bytes: int = 16_000_000) -> dict[str, Any]:
    """Read one scoped/current-parser snapshot without creating or updating a cache.

    Freshness is always cached_unverified: callers must compare text_hash (SHA256
    of UTF-8 extracted text, not attachment bytes), metadata_fingerprint and any
    current bindings before treating these records as current. Partial records
    retain their unknown bibliography status. Limits bound rows and total bytes.
    """
    path = Path(path)
    _cache_path(path)
    scope = _identity({"instance_id": instance_id, "library_type": library_type, "library_id": library_id, "item_key": "SCOPE"})
    parser_version = _string(parser_version, 200)
    if not parser_version or type(limit) is not int or not 1 <= limit <= _MAX_LIBRARY_ITEMS or type(max_bytes) is not int or not 1 <= max_bytes <= 64_000_000:
        raise ValueError("invalid cached record bounds or parser version")
    result = {"records": [], "freshness": "cached_unverified", "truncated": False, "skipped_records": 0}
    if not path.exists():
        return result
    byte_count = 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        cursor = connection.execute("SELECT item_key, fingerprint, length(CAST(record AS BLOB)), CASE WHEN length(CAST(record AS BLOB)) <= ? THEN record END FROM documents WHERE instance_id = ? AND library_type = ? AND library_id = ? AND parser_version = ? ORDER BY item_key LIMIT ?",
                                    (_MAX_RECORD_BYTES, *_key(scope)[:3], parser_version, limit + 1))
        for number, (item_key, fingerprint, size, serialized) in enumerate(cursor):
            if number >= limit or size > max_bytes - byte_count:
                result["truncated"] = True
                break
            byte_count += size
            try:
                record = json.loads(serialized)
                _validate_record(record)
                if record["identity"] != {**scope, "item_key": item_key} or record["input_fingerprint"] != fingerprint or record["parser_version"] != parser_version:
                    raise ValueError("cache identity mismatch")
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                result["skipped_records"] += 1
                continue
            result["records"].append(record)
    return result


def _validate_record(record: Mapping[str, Any]) -> None:
    """The public cache boundary accepts only compact discovery records."""
    _json(record, _MAX_RECORD_BYTES)
    fields = {"identity", "source_id", "note_id", "input_fingerprint", "parser_version", "extraction_policy", "schema_version", "text_hash", "metadata_fingerprint", "metadata_version",
              "metadata", "coverage", "bibliography", "references", "unresolved_count"}
    if not isinstance(record, Mapping) or set(record) != fields:
        raise ValueError("invalid compact discovery record fields")
    if _identity(record["identity"]) != record["identity"]:
        raise ValueError("identity must contain only normalized compact fields")
    for field in ("source_id", "note_id"):
        if record[field] is not None:
            _string(record[field])
    for field in ("input_fingerprint", "text_hash", "metadata_fingerprint"):
        if not re.fullmatch(r"[a-f0-9]{64}", _string(record[field], 64)):
            raise ValueError("invalid discovery fingerprint")
    if record["schema_version"] != _SCHEMA_VERSION or (record["metadata_version"] is not None and type(record["metadata_version"]) not in {str, int}):
        raise ValueError("invalid discovery schema or metadata version")
    if isinstance(record["metadata_version"], str):
        _string(record["metadata_version"], 200)
    _string(record["parser_version"], 200)
    _string(record["extraction_policy"])
    metadata = record["metadata"]
    metadata_fields = {"title", "creators", "year", "item_type", "edition", "identifiers", "tags", "collections", "relations"}
    if not isinstance(metadata, Mapping) or set(metadata) != metadata_fields:
        raise ValueError("invalid compact metadata fields")
    if _metadata({"key": record["identity"]["item_key"], **metadata}, record["identity"]) != metadata:
        raise ValueError("metadata must contain only normalized compact fields")
    coverage = record["coverage"]
    if not isinstance(coverage, Mapping) or set(coverage) != {"status", "reason", "route", "source_scope", "coverage_gate", "indexed_pages", "total_pages", "indexed_chars", "total_chars", "sufficient"} or type(coverage["sufficient"]) is not bool:
        raise ValueError("invalid compact coverage fields")
    validated_coverage = _coverage(coverage, "coverage-validation")
    if coverage["sufficient"] and not validated_coverage["sufficient"]:
        raise ValueError("contradictory sufficient coverage")
    bibliography = record["bibliography"]
    if not isinstance(bibliography, Mapping) or set(bibliography) != {"status", "reason"} or bibliography["status"] not in {"unknown", "complete", "failed"}:
        raise ValueError("invalid bibliography status")
    _string(bibliography["reason"])
    references = record["references"]
    if not isinstance(references, list) or len(references) > _MAX_REFERENCES:
        raise ValueError("invalid reference list")
    for row in references:
        required = {"identifiers", "title", "author", "year", "item_type", "edition", "locator", "work_id", "match_status", "matched_item_keys", "candidate_item_keys"}
        if not isinstance(row, Mapping) or not required.issubset(row) or set(row) - required - {"unparsed", "raw_fingerprint"}:
            raise ValueError("invalid compact reference fields")
        clean = _reference(row)
        if any(row.get(field) != value for field, value in clean.items()):
            raise ValueError("reference must contain only normalized compact fields")
        if row["work_id"] is not None:
            _string(row["work_id"])
        if row["match_status"] not in {"matched_identifier", "matched_title_author_year", "ambiguous", "identifier_conflict", "external", "unresolved"}:
            raise ValueError("invalid reference match status")
        for field in ("matched_item_keys", "candidate_item_keys"):
            if not isinstance(row[field], list):
                raise ValueError("invalid reference item keys")
            for key in row[field]:
                _identity({**record["identity"], "item_key": key})
    if type(record["unresolved_count"]) is not int or record["unresolved_count"] != sum(not row["match_status"].startswith("matched_") for row in references):
        raise ValueError("invalid unresolved count")
    if bibliography["status"] == "complete" and (not coverage["sufficient"] or any("unparsed" in row for row in references)):
        raise ValueError("contradictory complete bibliography")


def scan_document(
    item: Mapping[str, Any], *, identity: Mapping[str, Any], text: str,
    coverage: Mapping[str, Any], source_binding: Mapping[str, Any] | None = None,
    library_items: Sequence[Mapping[str, Any]] = (), matcher: ReferenceMatcher | None = None,
    parser: Callable[[str], Mapping[str, Any]] | None = None,
    parser_version: str = BASELINE_PARSER_VERSION, extraction_policy: str = "local-text-v1",
    cache: DiscoveryCache | None = None,
) -> dict[str, Any]:
    """Scan supplied evidence; partial/unknown coverage is recorded but never reused.

    Pass coverage as extraction status/route/reason/source_scope plus optional
    coverage_gate and indexed/total pages/chars (Zotero camelCase also accepted).
    A custom parser requires an explicit, non-baseline parser_version. Metadata
    and reference limits reject oversized input rather than silently dropping it.
    """
    identity = _identity(identity)
    if not isinstance(text, str) or len(text) > _MAX_TEXT_BYTES or len(text.encode("utf-8")) > _MAX_TEXT_BYTES or "\x00" in text:
        raise ValueError("extracted text is invalid or oversized")
    metadata = _metadata(item, identity)
    source_id, note_id = _binding(source_binding, identity)
    normalized_coverage = _coverage(coverage, text)
    parser_version = _string(parser_version, 200)
    extraction_policy = _string(extraction_policy, 2_000)
    if not parser_version or not extraction_policy or (parser is not None and parser_version == BASELINE_PARSER_VERSION):
        raise ValueError("explicit parser version and extraction policy are required")
    if matcher is not None and library_items:
        raise ValueError("supply matcher or library_items, not both")
    matcher = matcher if matcher is not None else ReferenceMatcher(library_items)
    fingerprint = _hash(_json({"schema": _SCHEMA_VERSION, "identity": identity, "item": item,
                              "source_binding": source_binding, "text_sha256": _hash(text), "coverage": coverage,
                              "parser": parser_version, "policy": extraction_policy, "library": matcher.fingerprint}, 1_000_000))
    if cache is not None:
        cached = cache.get(identity, fingerprint)
        if cached is not None:
            return cached
    references = []
    bibliography = {"status": "unknown", "reason": "extraction_not_available"}
    if text.strip() and normalized_coverage["status"] in {"succeeded", "partial"} and normalized_coverage["source_scope"] in {"full_document", "partial_document"}:
        try:
            parsed = (parser or baseline_references)(text)
            if not isinstance(parsed, Mapping) or not isinstance(parsed.get("references"), (list, tuple)) or type(parsed.get("complete")) is not bool:
                raise ValueError("parser requires references and explicit completeness")
            if len(parsed["references"]) > _MAX_REFERENCES:
                raise ValueError("parser reference limit exceeded")
            references = [matcher.match(row, identity=identity) for row in parsed["references"]]
            complete = parsed["complete"] and normalized_coverage["sufficient"] and all("unparsed" not in row for row in references)
            bibliography = {"status": "complete" if complete else "unknown",
                            "reason": _string(parsed.get("reason", "")) or ("parser_complete" if complete else "coverage_or_parser_incomplete")}
        except Exception:
            # Adapter errors must not turn a failed bibliography into a known empty one.
            references = []
            bibliography = {"status": "failed", "reason": "local_parser_failed"}
    data = item.get("data", item)
    record = {"identity": identity, "source_id": source_id, "note_id": note_id, "input_fingerprint": fingerprint,
              "schema_version": _SCHEMA_VERSION, "text_hash": _hash(text), "metadata_fingerprint": _hash(_json(item, _MAX_METADATA_BYTES)),
              "metadata_version": item.get("version", data.get("version")),
              "parser_version": parser_version, "extraction_policy": extraction_policy, "metadata": metadata,
              "coverage": normalized_coverage, "bibliography": bibliography, "references": references,
              "unresolved_count": sum(not row["match_status"].startswith("matched_") for row in references)}
    _validate_record(record)
    if cache is not None:
        cache.put(record)
    return record


def discover_signals(records: Sequence[Mapping[str, Any]], *, max_signals: int = 10_000,
                     max_group_size: int = 100) -> dict[str, Any]:
    """Return bounded, distinct local signals; none asserts a substantive relation.

    Direct citations and Zotero Related retain direction. Shared signals connect
    pairs once. Popular groups are skipped rather than expanded into all pairs.
    """
    if type(max_signals) is not int or not 1 <= max_signals <= 100_000 or type(max_group_size) is not int or not 2 <= max_group_size <= 1_000:
        raise ValueError("invalid discovery signal bounds")
    if not isinstance(records, (list, tuple)) or len(records) > _MAX_LIBRARY_ITEMS:
        raise ValueError("records must be bounded")
    by_key = {}
    groups = defaultdict(set)
    byte_count = 0
    for record in records:
        _validate_record(record)
        byte_count += len(_json(record, _MAX_RECORD_BYTES).encode("utf-8"))
        if byte_count > 32_000_000:
            raise ValueError("compact discovery records are too large; use bounded batches")
        key = _key(_identity(record.get("identity")))
        if key in by_key:
            raise ValueError("duplicate document identity")
        by_key[key] = record
        for reference in record["references"]:
            if reference.get("work_id"):
                groups[(key[:3], "shared_reference", reference["work_id"])].add(key)
        for tag in record["metadata"]["tags"]:
            if _norm(tag):
                groups[(key[:3], "shared_tag", _norm(tag))].add(key)
    signals = []
    seen = set()
    skipped = 0

    def add(left: tuple[str, ...], right: tuple[str, ...], kind: str, evidence: str) -> bool:
        if left == right or right not in by_key:
            return True
        token = (left, right, kind, evidence)
        if token in seen:
            return True
        if len(signals) >= max_signals:
            return False
        seen.add(token)
        signals.append({"kind": kind, "from_identity": dict(zip(_IDENTITY_FIELDS, left)),
                        "to_identity": dict(zip(_IDENTITY_FIELDS, right)), "evidence": evidence})
        return True

    for key, record in sorted(by_key.items()):
        for reference in record["references"]:
            if reference["match_status"].startswith("matched_"):
                for item_key in reference["matched_item_keys"]:
                    if not add(key, (*key[:3], item_key), "direct_citation", reference.get("work_id") or "matched_reference"):
                        return {"signals": signals, "truncated": True, "skipped_groups": skipped}
        for value in record["metadata"]["relations"].get("dc:relation", []):
            target = value
            if "://" in value:
                uri = urlsplit(value)
                match = re.fullmatch(r"/users/([^/]+)/items/([A-Z0-9]+)", uri.path)
                if uri.hostname not in {"zotero.org", "www.zotero.org"} or not match or match[1] != key[2]:
                    continue
                target = match[2]
            if not add(key, (*key[:3], target), "zotero_related", value):
                return {"signals": signals, "truncated": True, "skipped_groups": skipped}
    for (_, kind, evidence), members in sorted(groups.items()):
        if len(members) > max_group_size:
            skipped += 1
            continue
        for left, right in itertools.combinations(sorted(members), 2):
            if not add(left, right, kind, evidence):
                return {"signals": signals, "truncated": True, "skipped_groups": skipped}
    return {"signals": signals, "truncated": bool(skipped), "skipped_groups": skipped}
