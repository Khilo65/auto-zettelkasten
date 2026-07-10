from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .files import atomic_write_text, now_iso, safe_filename, sha256_text, slugify

SECTION_HEADINGS = (
    ("thesis", "Thesis"),
    ("method_and_research_design", "Method and Research Design"),
    ("evidence_and_data", "Evidence and Data"),
    ("detailed_findings", "Detailed Findings"),
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


def render_atomic_note(frontmatter: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    yaml_text = yaml.safe_dump(dict(frontmatter), sort_keys=False, allow_unicode=True).strip()
    title = str(frontmatter.get("title") or "Untitled Source")
    lines = ["---", yaml_text, "---", "", f"# {title}", ""]
    for key, heading in SECTION_HEADINGS:
        lines.extend([f"## {heading}", "", str(analysis.get(key, "")).strip(), ""])
    review = str(
        analysis.get("source_faithfulness_review")
        or "Passed deterministic structure and lineage checks. Analytical claims still require source-aware human review."
    ).strip()
    lines.extend(["## Source-Faithfulness Review", "", review, ""])
    return "\n".join(lines)


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
    if frontmatter.get("note_status") not in {"analytical_atomic_note", "verified_atomic_note"}:
        errors.append("invalid_note_status")
    if not re.fullmatch(r"[0-9a-f]{64}", str(frontmatter.get("inspected_content_hash", ""))):
        errors.append("invalid_inspected_content_hash")
    for _, heading in SECTION_HEADINGS + (("source_faithfulness_review", "Source-Faithfulness Review"),):
        match = re.search(rf"^## {re.escape(heading)}\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
        if not match or not match.group(1).strip():
            errors.append(f"missing_section:{slugify(heading)}")
    locator_match = re.search(r"^## Locators\s*$\n+(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
    if locator_match and "not supplied" in locator_match.group(1).casefold():
        warnings.append("weak_locators")
    return NoteValidation(passed=not errors, errors=errors, warnings=warnings)


def write_atomic_note(workspace: Path, frontmatter: Mapping[str, Any], analysis: Mapping[str, Any]) -> tuple[Path, NoteValidation]:
    notes_dir = workspace / "02_source_memory" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
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
    text = render_atomic_note(frontmatter, analysis)
    validation = validate_atomic_note(text)
    if not validation.passed:
        return candidate, validation
    committed = dict(frontmatter)
    committed["note_status"] = "analytical_atomic_note"
    committed["source_faithfulness_review"] = validation.to_dict()
    committed["updated_at"] = now_iso()
    text = render_atomic_note(committed, analysis)
    final_validation = validate_atomic_note(text)
    if final_validation.passed:
        atomic_write_text(candidate, text)
    return candidate, final_validation


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
    frontmatter, body = parse_atomic_note(text)
    return {"path": str(path), "frontmatter": frontmatter, "body": body, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def update_note_frontmatter(path: Path, updates: Mapping[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_atomic_note(text)
    frontmatter.update(dict(updates))
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    atomic_write_text(path, f"---\n{yaml_text}\n---\n{body}")


def update_note_graph(
    path: Path,
    updates: Mapping[str, Any],
    related_links: list[Mapping[str, Any]],
    cluster_ids: list[str],
) -> None:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_atomic_note(text)
    frontmatter.update(dict(updates))
    body = re.sub(r"\n*## Graph Links\s*\n.*\Z", "", body, flags=re.DOTALL).rstrip()
    graph_lines = ["", "", "## Graph Links", ""]
    for link in related_links:
        graph_lines.append(f"- {link['relation_type']}: [[{link['target_stem']}]]")
    for cluster_id in cluster_ids:
        graph_lines.append(f"- cluster: [[{cluster_id}]]")
    if not related_links and not cluster_ids:
        graph_lines.append("No committed typed links or canonical clusters yet.")
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    graph_text = "\n".join(graph_lines)
    atomic_write_text(path, f"---\n{yaml_text}\n---\n{body}{graph_text}\n")


def _existing_note_for_id(notes_dir: Path, note_id: str) -> Path | None:
    for path in notes_dir.glob("*.md"):
        try:
            frontmatter, _ = parse_atomic_note(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if str(frontmatter.get("note_id", "")) == note_id:
            return path
    return None
