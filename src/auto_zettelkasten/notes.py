from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .files import (
    atomic_write_text,
    now_iso,
    read_yaml,
    safe_filename,
    sha256_text,
    slugify,
    write_yaml,
)

# Immutable target of the historical review-status migration. These must not
# follow the package's current release constants.
REVIEW_STATUS_TARGET_ENGINE_VERSION = "0.5.0"
REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION = "1.4"

SECTION_HEADINGS = (
    ("thesis", "Thesis"),
    ("method_and_research_design", "Method and Research Design"),
    ("evidence_and_data", "Evidence and Data"),
    ("detailed_findings", "Detailed Findings"),
    ("plain_english_interpretation", "Plain-English Interpretation"),
    ("strengths_and_contributions", "Strengths and Contributions"),
    ("methodological_critique", "Methodological Critique"),
    ("limitations", "Limitations"),
    ("what_this_source_can_support", "What This Source Can Support"),
    ("what_this_source_cannot_support", "What This Source Cannot Support"),
    ("locators", "Locators"),
)

REQUIRED_FRONTMATTER = {
    "note_id",
    "source_id",
    "note_status",
    "zotero_item_key",
    "source_file",
    "inspected_content_hash",
    "content_route",
    "reader_provider",
    "reader_model",
    "original_zotero_tags",
    "normalized_tags",
    "related_notes",
}

ANALYTICAL_NOTE_STATUSES = {"analytical_atomic_note", "verified_atomic_note"}
LIMITED_NOTE_STATUSES = {
    "abstract_only_atomic_note",
    "metadata_only_source_note",
    "fulltext_available",
}
NON_SOURCE_FRONTMATTER_FIELDS = frozenset(
    {
        "artifact_schema_version",
        "cluster_links",
        "clusters",
        "created_at",
        "engine_version",
        "gap_links",
        "gaps",
        "human_review",
        "related_notes",
        "review_status",
        "source_faithfulness_review",
        "structural_validation",
        "tags",
        "updated_at",
    }
)
REVIEW_STATUS_FRONTMATTER_FIELDS = frozenset(
    {"human_review", "review_status", "source_faithfulness_review"}
)
REVIEW_STATUS_HEADINGS = ("Automated Validation", "Source-Faithfulness Review")
GRAPH_HEADING = "## Graph Links"
GRAPH_START_MARKER = "<!-- auto-zettelkasten:graph:start -->"
GRAPH_END_MARKER = "<!-- auto-zettelkasten:graph:end -->"
NOTE_METADATA_SCHEMA_VERSION = "1"
REQUIRED_LIMITED_FRONTMATTER = (REQUIRED_FRONTMATTER - {"reader_provider", "reader_model"}) | {
    "source_scope",
    "source_coverage",
}
_TRACEABLE_LOCATOR = re.compile(
    r"(?:\b(?:p{1,2}\.?|pages?|paragraphs?)\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\b|"
    r"\b(?:abstract|introduction|background|literature review|methods?|methodology|data|results?|findings?|"
    r"discussion|conclusions?|limitations?|appendix)\b|\b(?:table|figure)\s*\d+[a-z]?\b)",
    flags=re.IGNORECASE,
)
_LIMITED_NOTE_SPEC = {
    "abstract_only_atomic_note": {
        "scope": "abstract_only",
        "gates": {"limited"},
        "primary_heading": "Abstract",
        "secondary_heading": "Scope Limitation",
    },
    "metadata_only_source_note": {
        "scope": "metadata_only",
        "gates": {"failed", "limited"},
        "primary_heading": "Source Metadata",
        "secondary_heading": "Scope Limitation",
    },
    "fulltext_available": {
        "scope": "full_document",
        "gates": {"passed"},
        "primary_heading": "Full Text Availability",
        "secondary_heading": "Processing Status",
    },
}


def _dump_frontmatter(frontmatter: Mapping[str, Any]) -> str:
    # Obsidian parses wikilinks from raw Markdown. PyYAML's default 80-column
    # wrapping can split a long link target and create a different graph node.
    return yaml.safe_dump(
        dict(frontmatter),
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    ).strip()


class NoteCollisionError(RuntimeError):
    pass


@dataclass(slots=True)
class NoteValidation:
    passed: bool
    errors: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "errors": self.errors, "warnings": self.warnings}


def item_data(item: Mapping[str, Any]) -> dict[str, Any]:
    data = item.get("data", item)
    return dict(data) if isinstance(data, Mapping) else {}


def item_key(item: Mapping[str, Any]) -> str:
    data = item_data(item)
    return str(item.get("key") or data.get("key") or "").strip()


def source_id_for_item(item: Mapping[str, Any]) -> str:
    key = item_key(item)
    if key:
        return f"source-zotero-{slugify(key)}"
    data = item_data(item)
    identity = "|".join(str(data.get(field, "")) for field in ("title", "date", "DOI", "url"))
    return f"source-unkeyed-{sha256_text(identity)[:12]}"


def note_id_for_item(item: Mapping[str, Any]) -> str:
    return f"note-{sha256_text(source_id_for_item(item))[:12]}"


def original_tags(item: Mapping[str, Any]) -> list[str]:
    tags = item_data(item).get("tags", [])
    result: list[str] = []
    for tag in tags if isinstance(tags, list) else []:
        value = tag.get("tag") if isinstance(tag, Mapping) else tag
        if value is not None and str(value) not in result:
            result.append(str(value))
    return result


def propose_tags(item: Mapping[str, Any], note_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tag in original_tags(item):
        normalized = normalize_tag(tag)
        if not normalized:
            continue
        proposal_id = f"tag-proposal-{sha256_text(note_id + '|' + tag + '|' + normalized)[:12]}"
        rows.append(
            {
                "proposal_id": proposal_id,
                "note_id": note_id,
                "original_tag": tag,
                "proposed_tag": normalized,
                "proposal_kind": "mechanical_normalization",
                "confidence": 0.95,
                "rationale": "Unicode, case, whitespace, and punctuation normalization only.",
                "status": "proposed",
            }
        )
    return rows


def normalize_tag(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"[\s_/]+", "-", value)
    value = re.sub(r"[^\w-]+", "", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def source_obsidian_tags(normalized_tags: Sequence[str], note_status: str) -> list[str]:
    """Legacy projection helper; structural status belongs in YAML properties."""

    del note_status
    tags = {normalize_tag(str(value)) for value in normalized_tags}
    tags.discard("")
    tags = {tag for tag in tags if not tag.startswith("auto-zettelkasten")}
    return sorted(tags)


def render_atomic_note(frontmatter: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    yaml_text = _dump_frontmatter(frontmatter)
    title = str(frontmatter.get("title") or "Untitled Source")
    lines = ["---", yaml_text, "---", "", f"# {title}", ""]
    for key, heading in SECTION_HEADINGS:
        lines.extend([f"## {heading}", "", str(analysis.get(key, "")).strip(), ""])
    return "\n".join(lines)


def public_note_frontmatter(frontmatter: Mapping[str, Any]) -> dict[str, Any]:
    """Project only readable, useful Obsidian properties into Markdown."""

    title = str(frontmatter.get("title") or "Untitled Source")
    status = str(frontmatter.get("note_status") or "")
    creators = frontmatter.get("creators") or []
    authors: list[str] = []
    if isinstance(creators, Sequence) and not isinstance(creators, (str, bytes)):
        for creator in creators:
            if isinstance(creator, Mapping):
                name = " ".join(
                    str(creator.get(key) or "").strip()
                    for key in ("firstName", "lastName")
                ).strip() or str(creator.get("name") or "").strip()
            else:
                name = str(creator).strip()
            if name and name not in authors:
                authors.append(name)

    coverage = {
        "analytical_atomic_note": "full text",
        "verified_atomic_note": "full text",
        "abstract_only_atomic_note": "abstract only",
        "metadata_only_source_note": "metadata only",
        "fulltext_available": "full text available; analysis pending",
    }.get(status, str(frontmatter.get("source_scope") or "").replace("_", " "))
    related = [
        str(row.get("wikilink") or "")
        for row in frontmatter.get("related_notes", []) or []
        if isinstance(row, Mapping) and row.get("wikilink")
    ]
    aliases = [
        str(value)
        for value in frontmatter.get("aliases", []) or []
        if str(value).strip() and str(value).strip() != title
    ]
    projected = {
        "note_id": str(frontmatter.get("note_id") or ""),
        "type": "atomic-note"
        if status in ANALYTICAL_NOTE_STATUSES
        else "limited-source-note",
        "title": title,
        "authors": authors,
        "date": str(frontmatter.get("date") or ""),
        "citation_key": str(frontmatter.get("citation_key") or ""),
        "doi": str(frontmatter.get("doi") or frontmatter.get("DOI") or ""),
        "url": str(frontmatter.get("url") or ""),
        "coverage": coverage,
        "zotero_tags": list(frontmatter.get("original_zotero_tags", []) or []),
        "tags": list(frontmatter.get("tags", []) or []),
        "clusters": list(frontmatter.get("cluster_links", []) or []),
        "gaps": list(frontmatter.get("gap_links", []) or []),
        "related": related,
        "aliases": aliases,
    }
    return {
        key: value
        for key, value in projected.items()
        if value not in (None, "", [], {})
    }


def validate_atomic_note(text: str) -> NoteValidation:
    frontmatter, body = parse_atomic_note(text)
    errors: list[str] = []
    warnings: list[str] = []
    for field in sorted(REQUIRED_FRONTMATTER):
        value = frontmatter.get(field)
        if field not in frontmatter or value is None or value == "":
            errors.append(f"missing_frontmatter:{field}")
    for field in ("original_zotero_tags", "normalized_tags", "related_notes"):
        if field in frontmatter and not isinstance(frontmatter[field], list):
            errors.append(f"invalid_frontmatter_type:{field}")
    if frontmatter.get("note_status") not in ANALYTICAL_NOTE_STATUSES:
        errors.append("invalid_note_status")
    if frontmatter.get("source_scope") != "full_document":
        errors.append("source_scope_full_document_required")
    if _coverage_gate(frontmatter.get("source_coverage")) != "passed":
        errors.append("source_coverage_gate_not_passed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(frontmatter.get("inspected_content_hash", ""))):
        errors.append("invalid_inspected_content_hash")
    for _, heading in SECTION_HEADINGS:
        match = re.search(rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
        if not match or not match.group(1).strip():
            errors.append(f"missing_section:{slugify(heading)}")
    detailed_findings = _section_text(body, "Detailed Findings")
    plain_english = _section_text(body, "Plain-English Interpretation")
    if plain_english and _normalized_prose(plain_english) == _normalized_prose(detailed_findings):
        errors.append("plain_english_interpretation_repeats_detailed_findings")
    locator_match = re.search(r"^## Locators\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
    locator_text = locator_match.group(1).strip().casefold() if locator_match else ""
    weak_locator = any(
        marker in locator_text
        for marker in ("not supplied", "unavailable", "unknown", "not reported", "n/a", "not applicable")
    )
    if not locator_text or weak_locator or not _TRACEABLE_LOCATOR.search(locator_text):
        errors.append("untraceable_locators")
    return NoteValidation(passed=not errors, errors=errors, warnings=warnings)


def render_limited_note(frontmatter: Mapping[str, Any], content: Mapping[str, Any] | str | None = None) -> str:
    status = str(frontmatter.get("note_status") or "")
    if status not in LIMITED_NOTE_STATUSES:
        raise ValueError(f"unsupported limited note status: {status or '<missing>'}")
    payload = dict(content) if isinstance(content, Mapping) else {"available_content": str(content or "")}
    spec = _LIMITED_NOTE_SPEC[status]
    primary = _limited_primary_content(status, frontmatter, payload)
    secondary = str(
        payload.get("scope_limitation")
        or payload.get("processing_status")
        or payload.get("what_requires_full_text")
        or payload.get("source_coverage")
        or ""
    ).strip()
    if not secondary:
        secondary = {
            "abstract_only_atomic_note": "Only the abstract was available. Do not treat this note as evidence from the full publication.",
            "metadata_only_source_note": "No abstract or full publication text was available. This note records bibliographic metadata only.",
            "fulltext_available": "Full text is available, but no analytical atomic note has been produced from it yet.",
        }[status]
    yaml_text = _dump_frontmatter(frontmatter)
    title = str(frontmatter.get("title") or "Untitled Source")
    return "\n".join(
        [
            "---",
            yaml_text,
            "---",
            "",
            f"# {title}",
            "",
            f"## {spec['primary_heading']}",
            "",
            primary,
            "",
            f"## {spec['secondary_heading']}",
            "",
            secondary,
            "",
        ]
    )


def validate_limited_note(text: str) -> NoteValidation:
    frontmatter, body = parse_atomic_note(text)
    errors: list[str] = []
    warnings: list[str] = []
    status = str(frontmatter.get("note_status") or "")
    for field in sorted(REQUIRED_LIMITED_FRONTMATTER):
        value = frontmatter.get(field)
        if field not in frontmatter or value is None or value == "":
            errors.append(f"missing_frontmatter:{field}")
    for field in ("original_zotero_tags", "normalized_tags", "related_notes"):
        if field in frontmatter and not isinstance(frontmatter[field], list):
            errors.append(f"invalid_frontmatter_type:{field}")
    if status not in LIMITED_NOTE_STATUSES:
        errors.append("invalid_limited_note_status")
    if not re.fullmatch(r"[0-9a-f]{64}", str(frontmatter.get("inspected_content_hash", ""))):
        errors.append("invalid_inspected_content_hash")
    spec = _LIMITED_NOTE_SPEC.get(status)
    if spec:
        if frontmatter.get("source_scope") != spec["scope"]:
            errors.append(f"invalid_source_scope:{spec['scope']}_required")
        if _coverage_gate(frontmatter.get("source_coverage")) not in spec["gates"]:
            errors.append("invalid_source_coverage_gate")
        for heading in (spec["primary_heading"], spec["secondary_heading"]):
            if not _section_text(body, str(heading)):
                errors.append(f"missing_section:{slugify(str(heading))}")
    if any(_section_text(body, heading) for _, heading in SECTION_HEADINGS):
        errors.append("analytical_sections_not_allowed_in_limited_note")
    return NoteValidation(passed=not errors, errors=errors, warnings=warnings)


def validate_note(text: str) -> NoteValidation:
    frontmatter, _ = parse_atomic_note(text)
    if frontmatter.get("note_status") in LIMITED_NOTE_STATUSES:
        return validate_limited_note(text)
    return validate_atomic_note(text)


# Explicit aliases make the limited-source API discoverable without breaking the concise names.
render_limited_source_note = render_limited_note
validate_limited_source_note = validate_limited_note


def write_atomic_note(workspace: Path, frontmatter: Mapping[str, Any], analysis: Mapping[str, Any]) -> tuple[Path, NoteValidation]:
    notes_dir = workspace / "02_source_memory" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    candidate = _note_candidate(notes_dir, frontmatter)
    text = render_atomic_note(frontmatter, analysis)
    validation = validate_atomic_note(text)
    if not validation.passed:
        return candidate, validation
    committed = dict(frontmatter)
    committed["note_status"] = "analytical_atomic_note"
    committed["structural_validation"] = validation.to_dict()
    committed.pop("human_review", None)
    committed["updated_at"] = now_iso()
    text = render_atomic_note(committed, analysis)
    final_validation = validate_atomic_note(text)
    if final_validation.passed:
        _write_note_metadata(workspace, candidate, committed)
        atomic_write_text(candidate, _public_note_text(text, committed))
    return candidate, final_validation


def write_limited_note(
    workspace: Path,
    frontmatter: Mapping[str, Any],
    content: Mapping[str, Any] | str | None = None,
) -> tuple[Path, NoteValidation]:
    notes_dir = workspace / "02_source_memory" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    candidate = _note_candidate(notes_dir, frontmatter)
    text = render_limited_note(frontmatter, content)
    validation = validate_limited_note(text)
    if validation.passed:
        committed = dict(frontmatter)
        committed["structural_validation"] = validation.to_dict()
        committed.pop("human_review", None)
        committed["updated_at"] = now_iso()
        text = render_limited_note(committed, content)
        validation = validate_limited_note(text)
        if validation.passed:
            _write_note_metadata(workspace, candidate, committed)
            atomic_write_text(candidate, _public_note_text(text, committed))
    return candidate, validation


def note_filename(frontmatter: Mapping[str, Any]) -> str:
    authors = frontmatter.get("creators") or []
    first_author = "Unknown"
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, Mapping):
            first_author = str(first.get("lastName") or first.get("name") or "Unknown")
        else:
            first_author = str(first)
    year_match = re.search(r"(?:19|20)\d{2}", str(frontmatter.get("date") or ""))
    year = year_match.group(0) if year_match else "n.d."
    title = safe_filename(str(frontmatter.get("title") or "Untitled"))
    return safe_filename(f"{first_author}{year} - {title}")


def parse_atomic_note(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    payload = yaml.safe_load(text[4:end]) or {}
    return (dict(payload) if isinstance(payload, Mapping) else {}), text[end + 5 :]


def read_note(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    projected_frontmatter, body = parse_atomic_note(text)
    frontmatter = _read_note_metadata(path, projected_frontmatter)
    return {"path": str(path), "frontmatter": frontmatter, "body": body, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def internal_note_text(path: Path) -> str:
    """Reconstruct the canonical note used by validators and synthesis."""

    note = read_note(path)
    return (
        f"---\n{_dump_frontmatter(note['frontmatter'])}\n---\n"
        f"{str(note['body'])}"
    )


def semantic_note_hash(text: str) -> str:
    """Hash source-note meaning while excluding generated graph projections."""

    semantic_frontmatter, semantic_body = source_note_semantic_components(text)
    canonical = json.dumps(semantic_frontmatter, sort_keys=True, ensure_ascii=False, default=str)
    return sha256_text(f"{canonical}\n---\n{semantic_body}\n")


def source_note_semantic_components(text: str) -> tuple[dict[str, Any], str]:
    """Return the source-authored note content shared by hashes and profile prompts."""

    frontmatter, body = parse_atomic_note(text)
    semantic_frontmatter = {
        key: value for key, value in frontmatter.items() if key not in NON_SOURCE_FRONTMATTER_FIELDS
    }
    semantic_body = _strip_generated_note_sections(body)
    return semantic_frontmatter, semantic_body


def canonical_source_note_text(text: str) -> str:
    frontmatter, body = source_note_semantic_components(text)
    if not frontmatter:
        return body.rstrip() + "\n"
    yaml_text = _dump_frontmatter(frontmatter)
    return f"---\n{yaml_text}\n---\n{body.rstrip()}\n"


def legacy_semantic_note_hash_v1(text: str) -> str:
    """Reproduce schema-1.2 hashing for local migration aliasing only."""

    frontmatter, body = parse_atomic_note(text)
    excluded = {"cluster_links", "clusters", "gap_links", "gaps", "related_notes", "tags", "updated_at"}
    semantic_frontmatter = {key: value for key, value in frontmatter.items() if key not in excluded}
    semantic_body = re.sub(r"\n*## Graph Links\s*\n.*\Z", "", body, flags=re.DOTALL).rstrip()
    canonical = json.dumps(semantic_frontmatter, sort_keys=True, ensure_ascii=False, default=str)
    return sha256_text(f"{canonical}\n---\n{semantic_body}\n")


def strip_review_status_material(text: str, *, update_versions: bool = False) -> str:
    """Mechanically remove generated review-status material without changing analysis."""

    frontmatter, body = parse_atomic_note(text)
    if not frontmatter:
        cleaned = _strip_review_status_sections(body)
        return cleaned.rstrip() + ("\n" if text.endswith("\n") else "")
    for field in REVIEW_STATUS_FRONTMATTER_FIELDS:
        frontmatter.pop(field, None)
    if update_versions:
        if "engine_version" in frontmatter:
            frontmatter["engine_version"] = REVIEW_STATUS_TARGET_ENGINE_VERSION
        if "artifact_schema_version" in frontmatter:
            frontmatter["artifact_schema_version"] = REVIEW_STATUS_TARGET_ARTIFACT_SCHEMA_VERSION
    cleaned_body = _strip_review_status_sections(body)
    return f"---\n{_dump_frontmatter(frontmatter)}\n---\n{cleaned_body.rstrip()}\n"


def _strip_generated_note_sections(body: str) -> str:
    body = _strip_review_status_sections(body)
    section = _graph_section(body)
    managed = _managed_graph_block(body)
    if section and managed and section.start() <= managed.start() < section.end():
        section_content = body[section.start("content") : section.end()]
        managed_offset = managed.start() - section.start("content")
        remaining = (
            section_content[:managed_offset]
            + section_content[managed.end() - section.start("content") :]
        )
        if remaining.strip():
            return _without_markdown_span(body, managed.start(), managed.end()).rstrip()
        return _without_markdown_span(body, section.start(), section.end()).rstrip()
    if managed:
        return _without_markdown_span(body, managed.start(), managed.end()).rstrip()
    if section:
        return _without_markdown_span(body, section.start(), section.end()).rstrip()
    return body.rstrip()


def _graph_section(body: str) -> re.Match[str] | None:
    return re.search(
        rf"^{re.escape(GRAPH_HEADING)}[ \t]*$\n?(?P<content>.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )


def _managed_graph_block(body: str) -> re.Match[str] | None:
    return re.search(
        rf"^{re.escape(GRAPH_START_MARKER)}[ \t]*$\n?.*?"
        rf"^{re.escape(GRAPH_END_MARKER)}[ \t]*$",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )


def _without_markdown_span(body: str, start: int, end: int) -> str:
    prefix = body[:start].rstrip("\n")
    suffix = body[end:].lstrip("\n")
    if prefix and suffix:
        return f"{prefix}\n\n{suffix}"
    return prefix or suffix


def _project_graph_block(body: str, graph_block: str) -> str:
    managed = _managed_graph_block(body)
    if managed:
        return f"{body[:managed.start()]}{graph_block}{body[managed.end():]}"
    graph_section = f"{GRAPH_HEADING}\n\n{graph_block}"
    section = _graph_section(body)
    if section:
        prefix = body[: section.start()].rstrip("\n")
        suffix = body[section.end() :].lstrip("\n")
        return "\n\n".join(part for part in (prefix, graph_section, suffix) if part)
    return f"{body.rstrip()}\n\n{graph_section}"


def _strip_review_status_sections(body: str) -> str:
    cleaned = body
    for heading in REVIEW_STATUS_HEADINGS:
        cleaned = re.sub(
            rf"\n*^## {re.escape(heading)}\s*$\n.*?(?=^## |\Z)",
            "\n",
            cleaned,
            flags=re.MULTILINE | re.DOTALL,
        )
    cleaned = re.sub(
        r"(?im)^\s*[-*]?\s*(?:human review|review status|source-faithfulness review)\s*:\s*(?:not[_ -]?performed|pending|none)\s*$\n?",
        "",
        cleaned,
    )
    return cleaned.rstrip()


def update_note_frontmatter(path: Path, updates: Mapping[str, Any]) -> None:
    note = read_note(path)
    frontmatter = dict(note["frontmatter"])
    body = str(note["body"])
    frontmatter.update(dict(updates))
    workspace = _workspace_for_note(path)
    if workspace is not None:
        _write_note_metadata(workspace, path, frontmatter)
        frontmatter = public_note_frontmatter(frontmatter)
    yaml_text = _dump_frontmatter(frontmatter)
    atomic_write_text(path, f"---\n{yaml_text}\n---\n{body}")


def update_note_graph(
    path: Path,
    updates: Mapping[str, Any],
    related_links: list[Mapping[str, Any]],
    cluster_ids: list[str],
    gap_links: Sequence[Mapping[str, Any]] = (),
    cluster_wikilinks: Mapping[str, str] | None = None,
) -> bool:
    text = path.read_text(encoding="utf-8")
    note = read_note(path)
    frontmatter = dict(note["frontmatter"])
    body = str(note["body"])
    before_semantic_hash = semantic_note_hash(
        f"---\n{_dump_frontmatter(frontmatter)}\n---\n{body}"
    )
    for field in REVIEW_STATUS_FRONTMATTER_FIELDS:
        frontmatter.pop(field, None)
    body = _strip_review_status_sections(body).rstrip() + "\n"
    graph_lines: list[str] = []
    for link in related_links:
        reason = str(link.get("reason") or "").strip()
        suffix = f" — {reason}" if reason else ""
        graph_lines.append(f"- {link['relation_type']}: [[{link['target_stem']}]]{suffix}")
    cluster_wikilinks = cluster_wikilinks or {}
    for cluster_id in cluster_ids:
        graph_lines.append(f"- cluster: {cluster_wikilinks.get(cluster_id, f'[[{cluster_id}]]')}")
    for gap_link in gap_links:
        wikilink = str(gap_link.get("wikilink") or f"[[{gap_link['gap_id']}]]")
        graph_lines.append(f"- {gap_link['relation_type']}: {wikilink}")
    if not related_links and not cluster_ids and not gap_links:
        graph_lines.append("No committed typed links or canonical clusters yet.")
    desired_updates = dict(updates)
    desired_updated_at = desired_updates.pop("updated_at", None)
    unchanged_frontmatter = all(frontmatter.get(key) == value for key, value in desired_updates.items())
    graph_block = (
        f"{GRAPH_START_MARKER}\n"
        f"{'\n'.join(graph_lines)}\n"
        f"{GRAPH_END_MARKER}"
    )
    desired_body = _project_graph_block(body, graph_block).rstrip() + "\n"
    projected_frontmatter = {**frontmatter, **desired_updates}
    if not unchanged_frontmatter and desired_updated_at is not None:
        projected_frontmatter["updated_at"] = desired_updated_at
    after_semantic_hash = semantic_note_hash(
        f"---\n{_dump_frontmatter(projected_frontmatter)}\n---\n{desired_body}"
    )
    if after_semantic_hash != before_semantic_hash:
        raise ValueError("graph projection changed semantic note content")
    workspace = _workspace_for_note(path)
    rendered_frontmatter = (
        public_note_frontmatter(projected_frontmatter)
        if workspace is not None
        else projected_frontmatter
    )
    yaml_text = _dump_frontmatter(rendered_frontmatter)
    frontmatter_end = text.find("\n---\n", 4)
    existing_yaml_text = text[4:frontmatter_end] if frontmatter_end >= 0 else ""
    if unchanged_frontmatter and body == desired_body and existing_yaml_text == yaml_text:
        return False
    if workspace is not None:
        _write_note_metadata(workspace, path, projected_frontmatter)
    atomic_write_text(path, f"---\n{yaml_text}\n---\n{desired_body}")
    return True


def _workspace_for_note(path: Path) -> Path | None:
    path = path.resolve()
    if path.parent.name != "notes" or path.parent.parent.name != "02_source_memory":
        return None
    return path.parent.parent.parent


def _note_metadata_path(workspace: Path, note_id: str) -> Path:
    return workspace / "11_state" / "note_metadata" / f"{note_id}.yml"


def _write_note_metadata(
    workspace: Path,
    note_path: Path,
    frontmatter: Mapping[str, Any],
) -> None:
    note_id = str(frontmatter.get("note_id") or "")
    if not note_id:
        raise ValueError("note metadata requires note_id")
    write_yaml(
        _note_metadata_path(workspace, note_id),
        {
            "metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION,
            "note_path": str(note_path.resolve().relative_to(workspace.resolve())),
            "frontmatter": dict(frontmatter),
        },
    )


def _read_note_metadata(
    path: Path,
    projected_frontmatter: Mapping[str, Any],
) -> dict[str, Any]:
    workspace = _workspace_for_note(path)
    note_id = str(projected_frontmatter.get("note_id") or "")
    if workspace is None or not note_id:
        return dict(projected_frontmatter)
    payload = read_yaml(_note_metadata_path(workspace, note_id), {}) or {}
    stored = payload.get("frontmatter") if isinstance(payload, Mapping) else None
    return dict(stored) if isinstance(stored, Mapping) else dict(projected_frontmatter)


def _public_note_text(text: str, frontmatter: Mapping[str, Any]) -> str:
    _, body = parse_atomic_note(text)
    return (
        f"---\n{_dump_frontmatter(public_note_frontmatter(frontmatter))}\n---\n"
        f"{body}"
    )


def _existing_note_for_id(notes_dir: Path, note_id: str) -> Path | None:
    for path in notes_dir.glob("*.md"):
        try:
            frontmatter, _ = parse_atomic_note(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if str(frontmatter.get("note_id", "")) == note_id:
            return path
    return None


def _note_candidate(notes_dir: Path, frontmatter: Mapping[str, Any]) -> Path:
    note_id = str(frontmatter["note_id"])
    existing = _existing_note_for_id(notes_dir, note_id)
    candidate = existing or notes_dir / f"{note_filename(frontmatter)}.md"
    if candidate.exists() and existing is None:
        present, _ = parse_atomic_note(candidate.read_text(encoding="utf-8"))
        if str(present.get("note_id", "")) != note_id:
            suffix = slugify(str(frontmatter.get("zotero_item_key") or note_id))[:16]
            candidate = notes_dir / f"{note_filename(frontmatter)} [{suffix}].md"
            if candidate.exists():
                present, _ = parse_atomic_note(candidate.read_text(encoding="utf-8"))
                if str(present.get("note_id", "")) != note_id:
                    raise NoteCollisionError(f"note filename collision: {candidate.name}")
    return candidate


def _coverage_gate(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("gate", "coverage_gate", "status"):
            if key in value:
                return str(value[key]).strip().casefold()
        if value.get("passed") is True:
            return "passed"
        if value.get("passed") is False:
            return "failed"
        return ""
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return str(value or "").strip().casefold()


def _section_text(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _normalized_prose(value: str) -> str:
    return re.sub(r"\W+", " ", value, flags=re.UNICODE).casefold().strip()


def _limited_primary_content(
    status: str,
    frontmatter: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str:
    if status == "abstract_only_atomic_note":
        return str(
            payload.get("abstract")
            or payload.get("available_summary")
            or payload.get("available_content")
            or frontmatter.get("abstract")
            or ""
        ).strip()
    if status == "fulltext_available":
        supplied = str(
            payload.get("availability") or payload.get("available_summary") or payload.get("available_content") or ""
        ).strip()
        if supplied:
            return supplied
        source_file = str(frontmatter.get("source_file") or "the recorded source location")
        return f"A full-document source is available at `{source_file}`."
    supplied_metadata = payload.get("metadata") or payload.get("citation_metadata") or payload.get("available_summary") or payload.get("available_content")
    if isinstance(supplied_metadata, Mapping):
        rows = [(str(key), value) for key, value in supplied_metadata.items() if value not in (None, "", [], {})]
        return "\n".join(f"- {key}: {value}" for key, value in rows)
    if supplied_metadata:
        return str(supplied_metadata).strip()
    rows = []
    for field in ("title", "creators", "date", "doi", "url", "zotero_item_key"):
        value = frontmatter.get(field)
        if value not in (None, "", [], {}):
            rows.append(f"- {field}: {value}")
    return "\n".join(rows)
