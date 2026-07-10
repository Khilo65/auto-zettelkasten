from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .extraction import extract_path
from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .files import now_iso, read_yaml, sha256_file, sha256_text, write_yaml
from .identity import DOI_RE, identify_work, normalize_doi, normalize_url, work_metadata, year_value
from .notes import item_data, item_key, parse_atomic_note
from .ports import ZoteroClient
from .workspace import confined_child, validate_opaque_id

REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:references|bibliography|works cited|literature cited)\s*:?[\s]*$",
    flags=re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", flags=re.IGNORECASE)
NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\[?\d{1,4}\]?|\(\d{1,4}\))\s*[.)]?\s*")


def extract_reference_candidates(text: str) -> list[dict[str, Any]]:
    """Extract conservative citation leads without an LLM or network access."""

    candidates: dict[str, dict[str, Any]] = {}
    for match in DOI_RE.finditer(text):
        line = _line_containing(text, match.start())
        row = _reference_from_text(line, forced_doi=match.group(0))
        _add_reference(candidates, row)
    for reference in _bibliography_entries(text):
        _add_reference(candidates, _reference_from_text(reference))
    return sorted(candidates.values(), key=lambda row: str(row["work_id"]))


def write_citation_sidecar(
    workspace: Path,
    *,
    item: Mapping[str, Any],
    source_id: str,
    text: str,
    content_hash: str,
    source_file: str,
    extraction_version: str = "1",
) -> Path:
    safe_source_id = validate_opaque_id(source_id, field="source_id")
    path = confined_child(workspace / "01_custody" / "citation_leads", f"{safe_source_id}.yml")
    existing = read_yaml(path, {}) or {}
    data = item_data(item)
    relations = data.get("relations", {}) if isinstance(data.get("relations"), Mapping) else {}
    metadata_hash = _citation_metadata_hash(data)
    if (
        str(existing.get("inspected_content_hash") or "") == content_hash
        and str(existing.get("extraction_version") or "") == extraction_version
        and str(existing.get("metadata_hash") or "") == metadata_hash
        and (str(existing.get("reference_extraction_status") or "") == "succeeded" or not text)
    ):
        return path
    relation_rows: list[dict[str, Any]] = []
    for predicate, values in sorted(relations.items(), key=lambda row: str(row[0])):
        relation_type = _zotero_relation_type(str(predicate))
        for value in values if isinstance(values, list) else [values]:
            target_key = str(value or "").rstrip("/").rsplit("/", 1)[-1]
            if target_key:
                relation_rows.append(
                    {
                        "relation_type": relation_type,
                        "target_zotero_item_key": target_key,
                        "predicate": str(predicate),
                        "provenance": "exact_zotero_item_relation",
                    }
                )
    preserve_succeeded_references = (
        not text
        and str(existing.get("inspected_content_hash") or "") == content_hash
        and str(existing.get("extraction_version") or "") == extraction_version
        and str(existing.get("reference_extraction_status") or "") == "succeeded"
    )
    references = (
        [dict(row) for row in existing.get("references", []) if isinstance(row, Mapping)]
        if preserve_succeeded_references
        else (extract_reference_candidates(text) if text else [])
    )
    payload = {
        "engine_version": ENGINE_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "source_id": source_id,
        "zotero_item_key": item_key(item),
        "inspected_content_hash": content_hash,
        "metadata_hash": metadata_hash,
        "source_file": source_file,
        "extraction_version": extraction_version,
        "reference_extraction_status": "succeeded" if text or preserve_succeeded_references else "unavailable",
        "updated_at": now_iso(),
        "references": references,
        "zotero_relations": relation_rows,
    }
    write_yaml(path, payload)
    return path


def backfill_citation_sidecars(
    workspace: Path,
    *,
    source_ids: Sequence[str] = (),
    client: ZoteroClient | None = None,
) -> list[Path]:
    """Backfill missing sidecars from locally recorded files or Zotero indexed text."""

    wanted = {str(value) for value in source_ids if value}
    written: list[Path] = []
    for note_path in sorted((workspace / "02_source_memory" / "notes").glob("*.md")):
        try:
            front, _ = parse_atomic_note(note_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        source_id = str(front.get("source_id") or "")
        if not source_id or (wanted and source_id not in wanted):
            continue
        try:
            safe_source_id = validate_opaque_id(source_id, field="source_id")
        except ValueError:
            continue
        sidecar = confined_child(workspace / "01_custody" / "citation_leads", f"{safe_source_id}.yml")
        inspected_hash = str(front.get("inspected_content_hash") or "")
        present = read_yaml(sidecar, {}) or {}
        source_file = str(front.get("source_file") or "")
        pseudo_item = {
            "key": str(front.get("zotero_item_key") or ""),
            "data": {
                "key": str(front.get("zotero_item_key") or ""),
                "title": str(front.get("title") or ""),
                "date": str(front.get("date") or ""),
                "DOI": str(front.get("doi") or ""),
                "url": str(front.get("url") or ""),
                "creators": front.get("creators", []),
                "tags": [{"tag": value} for value in front.get("original_zotero_tags", []) or []],
                "relations": front.get("zotero_relations", {}),
            },
        }
        extraction_version = str(front.get("extraction_version") or "1")
        if (
            str(present.get("inspected_content_hash") or "") == inspected_hash
            and str(present.get("extraction_version") or "") == extraction_version
            and str(present.get("metadata_hash") or "") == _citation_metadata_hash(item_data(pseudo_item))
            and str(present.get("reference_extraction_status") or "") == "succeeded"
        ):
            continue
        text = ""
        observed_hash = ""
        local_path = Path(source_file).expanduser() if source_file and not source_file.startswith("zotero://") else None
        if local_path and local_path.is_absolute() and local_path.exists() and local_path.is_file():
            extracted = extract_path(local_path)
            if extracted.status == "succeeded":
                text = extracted.text
                observed_hash = sha256_file(local_path)
        elif client is not None and front.get("zotero_item_key"):
            target_key = (
                source_file.rstrip("/").rsplit("/", 1)[-1]
                if source_file.startswith("zotero://") and "/items/" in source_file
                else str(front["zotero_item_key"])
            )
            fulltext = client.fulltext(target_key)
            if fulltext:
                text = str(fulltext.get("content") or fulltext.get("text") or fulltext.get("fulltext") or "")
                observed_hash = sha256_text(text) if text else ""
        written.append(
            write_citation_sidecar(
                workspace,
                item=pseudo_item,
                source_id=source_id,
                text=text if observed_hash == inspected_hash else "",
                content_hash=inspected_hash,
                source_file=source_file,
                extraction_version=extraction_version,
            )
        )
    return written


def _bibliography_entries(text: str) -> list[str]:
    lines = text.splitlines()
    start = next((index + 1 for index, line in enumerate(lines) if REFERENCE_HEADING_RE.match(line)), None)
    if start is None:
        return []
    entries: list[str] = []
    current: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+\S", stripped) and not REFERENCE_HEADING_RE.match(stripped):
            break
        if not stripped:
            if current:
                entries.append(" ".join(current))
                current = []
            continue
        numbered = bool(NUMBER_PREFIX_RE.match(stripped))
        if numbered and current:
            entries.append(" ".join(current))
            current = []
        current.append(stripped)
    if current:
        entries.append(" ".join(current))
    return [entry for entry in entries[:1000] if len(entry) >= 20]


def _reference_from_text(value: str, *, forced_doi: str = "") -> dict[str, Any]:
    raw = re.sub(r"\s+", " ", NUMBER_PREFIX_RE.sub("", value)).strip()
    doi = normalize_doi(forced_doi or raw)
    url_match = URL_RE.search(raw)
    url = normalize_url(url_match.group(0)) if url_match else ""
    year = year_value(raw)
    title = _probable_title(raw, year)
    author = _probable_author(raw, year)
    metadata = {
        "title": title,
        "year": year,
        "authors": [author] if author else [],
        "doi": doi,
        "url": url,
        "raw_reference": raw,
    }
    work_id, actionability = identify_work(metadata)
    return {
        **work_metadata(metadata),
        "work_id": work_id,
        "actionability": actionability,
        "raw_reference": raw,
        "relation_type": "cites",
        "provenance": "deterministic_bibliography_extraction",
    }


def _probable_title(raw: str, year: str) -> str:
    text = raw
    if year:
        _, _, text = text.partition(year)
    text = text.lstrip(".():,; ")
    if not text:
        return ""
    for marker in (". https://", ". http://", ". doi:", " https://", " http://"):
        if marker in text.casefold():
            index = text.casefold().find(marker)
            text = text[:index]
            break
    sentences = [part.strip() for part in re.split(r"\.\s+", text) if part.strip()]
    return (sentences[0] if sentences else text).strip(" .")[:500]


def _probable_author(raw: str, year: str) -> str:
    if not year:
        return ""
    prefix = raw.split(year, 1)[0].strip(" .(),;")
    return prefix[:240]


def _add_reference(rows: dict[str, dict[str, Any]], row: Mapping[str, Any]) -> None:
    work_id = str(row.get("work_id") or "")
    if not work_id:
        return
    present = rows.get(work_id)
    if present is None or len(str(row.get("raw_reference") or "")) > len(str(present.get("raw_reference") or "")):
        rows[work_id] = dict(row)


def _line_containing(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : len(text) if end < 0 else end]


def _zotero_relation_type(predicate: str) -> str:
    normalized = predicate.casefold()
    if "isreferencedby" in normalized or "cited_by" in normalized:
        return "cited_by"
    if "references" in normalized or "cites" in normalized:
        return "cites"
    return "zotero_related"


def _citation_metadata_hash(data: Mapping[str, Any]) -> str:
    return sha256_text(
        json.dumps(
            {
                key: data.get(key)
                for key in ("title", "date", "DOI", "doi", "url", "ISBN", "creators", "tags", "relations")
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    )
