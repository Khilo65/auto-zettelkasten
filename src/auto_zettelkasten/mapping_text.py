"""Read-only local mapping inputs and offset-preserving embedding preparation.

The filtered spans are a derived representation. The supplied text, including
references, is retained intact for auditing and the existing citation pipeline.
Heading heuristics cannot certify chapter authorship or whole-book coverage.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .files import safe_filename
from .indexes import local_retrieval_projection
from .local_tools import MAX_INDEX_DOCUMENT_BYTES, MAX_INDEX_YAML_NODES, _ReadBudget
from .notes import (GRAPH_END_MARKER, GRAPH_START_MARKER, LITERATURE_END_MARKER,
                    LITERATURE_START_MARKER, SOURCE_END_MARKER, SOURCE_START_MARKER,
                    NOTE_METADATA_SCHEMA_VERSION, parse_atomic_note)
from .profiles import profile_from_dict, profile_sidecar_path, profile_to_dict
from .relationships import profile_content_hash, profile_hash_aliases


CLEANING_VERSION = "local-map-spans-v5"
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 384 * 1024 * 1024
MAX_FILES = 20_000
_EXCLUDED = {"bibliography", "acknowledgments", "funding", "contents", "frontmatter", "foreword", "endnotes", "generated_navigation"}
_KINDS = _EXCLUDED | {"body", "abstract", "authored_preface", "unknown"}
_NONBODY = {
    "references": "bibliography", "bibliography": "bibliography", "works cited": "bibliography",
    "references cited": "bibliography", "selected bibliography": "bibliography",
    "acknowledgments": "acknowledgments", "acknowledgements": "acknowledgments",
    "acknowledgment": "acknowledgments", "acknowledgement": "acknowledgments",
    "funding": "funding", "funding statement": "funding", "funding information": "funding",
    "table of contents": "contents", "contents": "contents", "list of figures": "contents",
    "list of tables": "contents", "copyright": "frontmatter", "title page": "frontmatter",
    "dedication": "frontmatter", "foreword": "foreword", "endnotes": "endnotes",
    "acknowledgments and funding": "acknowledgments", "acknowledgements and funding": "acknowledgments",
}
_MANAGED_MARKERS = {GRAPH_START_MARKER: GRAPH_END_MARKER, LITERATURE_START_MARKER: LITERATURE_END_MARKER, SOURCE_START_MARKER: SOURCE_END_MARKER}
_BODY_HEADING = re.compile(r"^(?:(?:chapter|part)\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b|\d+(?:\.\d+)*[.)]?\s+(?:introduction|background|conclusions?|discussion|results|methods|methodology|findings|appendix)\b)", re.I)
_BODY_NAMES = {"introduction", "conclusion", "conclusions", "discussion", "results", "methods", "methodology", "findings", "appendix"}

# Fixed template labels, not a vocabulary filter over substantive prose/headings.
_TEMPLATE_HEADINGS = frozenset({
    "thesis", "abstract", "key concepts and definitions", "method and research design",
    "evidence and data", "detailed findings", "plain-english interpretation",
    "strengths and contributions", "methodological critique", "limitations",
    "what this source can support", "what this source cannot support", "locators",
    "source structure and organization", "source metadata", "scope limitation",
})
_MANAGED_HEADING_MARKERS = {
    "graph links": GRAPH_START_MARKER,
    "position in the literature": LITERATURE_START_MARKER,
}


def _owns_following_block(text: str, start: int, title: str) -> bool:
    marker = _MANAGED_HEADING_MARKERS.get(title)
    if marker is None:
        return False
    # A personal section with the same heading is semantic. Only an immediately
    # following owned marker (allowing blank lines) identifies this writer's UI.
    return re.compile(r"(?:[ \t]*\r?\n)*[ \t]*" + re.escape(marker)
                      + r"[ \t]*(?:\r?\n|\Z)").match(text, start) is not None


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*(?:\r?\n)?$")


def _closing(text: str, start: int, left: str, right: str) -> int | None:
    depth = 1
    index = start + 1
    quote = ""
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if quote:
            if text[index] == quote:
                quote = ""
            index += 1
            continue
        if left == "(" and text[index] in "\"'" and text[index - 1].isspace():
            quote = text[index]
            index += 1
            continue
        if left == "(" and text[index] == "<":
            angle_end = text.find(">", index + 1)
            if angle_end >= 0:
                index = angle_end + 1
                continue
        if text[index] == left:
            depth += 1
        elif text[index] == right:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _publisher_footer_end(text: str, start: int) -> int | None:
    # Recognize a narrow publisher-export signature, not any mention of citations.
    tail = text[start:start + 8192]
    doi = re.search(r"\bDOI:[ \t]*10\.\d{4,9}/\S+", tail, re.I)
    jel = re.search(r"(?m)^JEL Classification[ \t]*\r?$", tail)
    journals = re.search(r"(?m)^Journals[ \t]*\r?$", tail)
    if not (doi and jel and journals and doi.end() < jel.start() < journals.start()):
        return None
    # A later authored section still belongs to the note, not this footer.
    next_heading = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+").search(text, start + journals.end())
    return next_heading.start() if next_heading else len(text)


def _semantic_exclusions(text: str) -> list[tuple[int, int, str]]:
    """Locate invisible syntax without rewriting source slices or code literals."""
    result = []
    offset = 0
    fence = ""
    fence_size = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence:
            if match and match[1][0] == fence and len(match[1]) >= fence_size and not match[2].strip():
                fence = ""
            offset += len(line)
            continue
        if match:
            fence, fence_size = match[1][0], len(match[1])
            offset += len(line)
            continue
        if line.strip() == "Citation":
            footer_end = _publisher_footer_end(text, offset)
            if footer_end is not None:
                result.append((offset, footer_end, "publisher_navigation"))
                offset += len(line)
                continue
        heading = _ATX.match(line)
        if heading:
            title = heading[2].strip().rstrip(":").casefold()
            if title in _TEMPLATE_HEADINGS or _owns_following_block(text, offset + len(line), title):
                result.append((offset, offset + len(line), "template_heading"))
                offset += len(line)
                continue
        if re.match(r"^ {0,3}\[[^]\r\n]+\]:[ \t]*\S", line):
            result.append((offset, offset + len(line), "link_destination"))
            offset += len(line)
            continue
        index = 0
        hidden_tails = {}
        while index < len(line):
            if index in hidden_tails:
                index = hidden_tails[index]
                continue
            if line[index] == "\\":
                index += 2
                continue
            if line[index] == "`":
                stop = index + 1
                while stop < len(line) and line[stop] == "`":
                    stop += 1
                size = stop - index
                close = line.find("`" * size, stop)
                index = close + size if close >= 0 else index + size
                continue
            if line.startswith("[[", index):
                close = line.find("]]", index + 2)
                if close < 0:
                    break
                content = line[index + 2:close]
                pipe = next((i for i, char in enumerate(content) if char == "|" and (i == 0 or content[i - 1] != "\\")), -1)
                start = index - 1 if index and line[index - 1] == "!" else index
                if pipe >= 0 and content[pipe + 1:].strip():
                    result.append((offset + start, offset + index + 3 + pipe, "link_destination"))
                else:
                    result.append((offset + start, offset + index + 2, "link_markup"))
                    if pipe >= 0:
                        result.append((offset + index + 2 + pipe, offset + close, "link_markup"))
                result.append((offset + close, offset + close + 2, "link_markup"))
                index = close + 2
                continue
            if line[index] == "[":
                close = _closing(line, index, "[", "]")
                if close is None:
                    break
                following = close + 1
                if following < len(line) and line[following] in "([":
                    ending = _closing(line, following, line[following], ")" if line[following] == "(" else "]")
                    if ending is None and line[following] == "(":
                        # Ordinary multiline destinations are bounded to one
                        # paragraph; never consume a following fenced block.
                        tail = text[offset + following:offset + following + 8192]
                        boundary = re.search(r"\n[ \t]*\n|\n {0,3}(?:`{3,}|~{3,})", tail)
                        if boundary:
                            tail = tail[:boundary.start()]
                        close_tail = _closing(tail, 0, "(", ")")
                        if close_tail is not None:
                            ending = following + close_tail
                    if ending is not None:
                        start = index - 1 if index and line[index - 1] == "!" else index
                        result.append((offset + start, offset + index + 1, "link_markup"))
                        result.append((offset + close, offset + ending + 1, "link_destination"))
                        hidden_tails[close] = ending + 1
                        index += 1  # Nested image/link labels remain visible syntax to inspect.
                        continue
                index = close + 1
                continue
            index += 1
        offset += len(line)
    # Semantic removals can touch at punctuation boundaries; merge overlapping
    # ranges so every source character contributes to coverage at most once.
    merged = []
    for start, end, kind in sorted(result):
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(end, previous[1]), previous[2])
        else:
            merged.append((start, end, kind))
    return merged


def _visible_spans(text: str, spans: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    removals = _semantic_exclusions(text)
    prepared, excluded = [], []
    removal_index = 0
    for group, span in enumerate(spans):
        start, end = span["start"], span["end"]
        while removal_index < len(removals) and removals[removal_index][1] <= start:
            removal_index += 1
        position = start
        index = removal_index
        while index < len(removals) and removals[index][0] < end:
            left, right, kind = removals[index]
            left, right = max(start, left), min(end, right)
            if position < left:
                prepared.append({**span, "start": position, "end": left, "text": text[position:left], "embedding_group": group})
            excluded.append({"start": left, "end": right, "owner": span["owner"], "section_kind": kind})
            position = right
            index += 1
        if position < end:
            prepared.append({**span, "start": position, "text": text[position:end], "embedding_group": group})
    nonempty = {row["embedding_group"] for row in prepared if row["text"].strip()}
    return [row for row in prepared if row["embedding_group"] in nonempty], excluded


class _MappingReadBudget(_ReadBudget):
    """Bind each bounded read to the first observed local file identity."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.input_stamps: dict[Path, tuple[int, int, int, int, int]] = {}

    def _stamp(self, path: Path) -> tuple[int, int, int, int, int]:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("mapping input must remain inside the workspace")
        stat = path.stat()
        if not path.is_file() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("mapping input must remain a confined regular file")
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def verify(self, path: Path) -> None:
        expected = self.input_stamps.get(path)
        if expected is None:
            return
        try:
            current = self._stamp(path)
        except OSError as exc:
            raise ValueError("mapping input disappeared during read") from exc
        if current != expected:
            raise ValueError("mapping input changed during read")

    def reserve(self, paths: Sequence[Path], **kwargs: Any) -> None:
        for path in paths:
            if path not in self.input_stamps and path.exists():
                self.input_stamps[path] = self._stamp(path)
            self.verify(path)
            super().reserve([path], **kwargs)
            self.verify(path)

    def read(self, path: Path) -> dict[str, Any]:
        self.verify(path)
        result = super().read(path)
        self.verify(path)
        return result


def _heading(line: str) -> tuple[str, int]:
    value = line.strip().strip("\ufeff")
    markdown = re.match(r"^(#{1,6})\s+", value)
    level = len(markdown[1]) if markdown else 0
    value = re.sub(r"^#{1,6}\s+", "", value).strip().rstrip("#").strip().rstrip(":").casefold()
    if re.search(r"\.{3,}\s*\d+\s*$", value):
        return "", 0
    if len(value) > 140:
        return "", level
    if value in _NONBODY:
        return _NONBODY[value], level
    if re.fullmatch(r"(?:(?:author|editor)(?:'s|s')?\s+)?preface(?:\s+to\s+.{1,65}|\s+\d{4})?", value) or re.fullmatch(r"editors?(?:'s|s')?\s+introduction", value):
        return "authored_preface", level
    if re.fullmatch(r"foreword(?:\s+by\s+.{1,80})?", value):
        return "foreword", level
    if value == "abstract":
        return "abstract", level
    if value in _BODY_NAMES or _BODY_HEADING.match(value):
        return "body", level
    return "", level


def _ownership(text: str, spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(spans, (str, bytes)) or not isinstance(spans, Sequence):
        raise ValueError("ownership_spans must be a sequence of offset mappings")
    result = []
    for raw in spans:
        if not isinstance(raw, Mapping):
            raise ValueError("ownership span must be a mapping")
        start, end, owner = raw.get("start"), raw.get("end"), raw.get("owner", "")
        kind = raw.get("section_kind", "")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("ownership offsets must address the exact supplied text")
        if not isinstance(owner, str) or len(owner) > 500 or "\x00" in owner:
            raise ValueError("ownership label must be a bounded string")
        if not isinstance(kind, str) or (kind and kind not in _KINDS):
            raise ValueError("unrecognized explicit ownership section kind")
        if "text" in raw and raw["text"] != text[start:end]:
            raise ValueError("ownership text does not match its original offsets")
        result.append({"start": start, "end": end, "owner": owner, "section_kind": kind})
    result.sort(key=lambda row: (row["start"], row["end"]))
    if any(left["end"] > right["start"] for left, right in zip(result, result[1:])):
        raise ValueError("ownership spans must not overlap")
    return result


def prepare_embedding_spans(text: str, ownership_spans: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Filter nonbody and invisible syntax, retaining exact original slices.

    Fragments sharing ``embedding_group`` must be rejoined before encoding, so
    removing a link destination does not turn its label into a tiny new chunk.

    ``owner`` is an explicit supplied ownership label, never inferred from a
    book's editor or catalogue creator. Prefaces remain separately identified;
    unresolved boundaries and authorship are reported rather than certified.
    """
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("mapping text must be a string without NUL bytes")
    owners = _ownership(text, ownership_spans)
    boundaries = {0, len(text)}
    segments = []
    state, position, managed_end = "unknown", 0, ""
    excluded_level = 0
    fence = ""
    fence_size = 0
    # Public note YAML is metadata, not substantive embedding content.
    yaml_open = re.match(r"\A\ufeff?---[ \t]*\r?\n", text)
    yaml_close = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.M).search(text, yaml_open.end()) if yaml_open else None
    yaml_end = yaml_close.end() if yaml_close else 0
    for line in text.splitlines(keepends=True):
        end = position + len(line)
        stripped = line.strip()
        if not fence and stripped in _MANAGED_MARKERS:
            managed_end = _MANAGED_MARKERS[stripped]
        if managed_end:
            kind = "generated_navigation"
            if stripped == managed_end:
                managed_end = ""
        elif position < yaml_end:
            kind = "frontmatter"
        else:
            marker = _FENCE.match(line)
            if fence:
                heading_kind, level = "", 0
                if marker and marker[1][0] == fence and len(marker[1]) >= fence_size and not marker[2].strip():
                    fence = ""
            elif marker:
                fence, fence_size = marker[1][0], len(marker[1])
                heading_kind, level = "", 0
            else:
                heading_kind, level = _heading(line)
            # Nested headings still belong to the excluded section; a peer or
            # higher heading starts a new section whose content may be unknown.
            nested_excluded = state in _EXCLUDED and excluded_level and level > excluded_level
            if not nested_excluded:
                if heading_kind:
                    state = heading_kind
                elif level:
                    state = "unknown" if state in _EXCLUDED else "body"
                if heading_kind or level:
                    excluded_level = level if state in _EXCLUDED else 0
            kind = state
        if segments and segments[-1][2] == kind:
            segments[-1] = (segments[-1][0], end, kind)
        else:
            segments.append((position, end, kind))
        position = end
    for start, end, _ in segments:
        boundaries.update((start, end))
    for row in owners:
        boundaries.update((row["start"], row["end"]))
    cuts = sorted(boundaries)
    prepared, excluded = [], []
    segment_index = owner_index = 0
    for start, end in zip(cuts, cuts[1:]):
        while segment_index + 1 < len(segments) and segments[segment_index][1] <= start:
            segment_index += 1
        while owner_index < len(owners) and owners[owner_index]["end"] <= start:
            owner_index += 1
        kind = segments[segment_index][2] if segments else "unknown"
        owned = owners[owner_index] if owner_index < len(owners) and owners[owner_index]["start"] <= start < owners[owner_index]["end"] else {}
        kind = owned.get("section_kind") or kind
        row = {"start": start, "end": end, "text": text[start:end], "owner": owned.get("owner", ""), "section_kind": kind}
        if kind in _EXCLUDED:
            excluded.append({key: value for key, value in row.items() if key != "text"})
        elif row["text"].strip():
            prepared.append(row)
    prepared, semantic_excluded = _visible_spans(text, prepared)
    excluded.extend(semantic_excluded)
    excluded.sort(key=lambda row: (row["start"], row["end"]))
    excluded_by_kind = Counter()
    for row in excluded:
        excluded_by_kind[row["section_kind"]] += row["end"] - row["start"]
    unowned = sum(row["end"] - row["start"] for row in prepared if not row["owner"])
    unknown = sum(row["end"] - row["start"] for row in prepared if row["section_kind"] == "unknown")
    return {"spans": prepared, "coverage": {
        "cleaning_version": CLEANING_VERSION, "input_characters": len(text),
        "embedded_characters": sum(row["end"] - row["start"] for row in prepared),
        "excluded_characters": sum(excluded_by_kind.values()), "excluded_by_kind": dict(sorted(excluded_by_kind.items())),
        "excluded_spans": excluded, "unowned_characters": unowned, "unclassified_characters": unknown,
        "ownership_status": "unresolved" if unowned else "explicit" if prepared else "no_eligible_text",
        "boundary_status": "heuristic_unresolved" if unknown or unowned else "explicit_or_heading_based",
        "full_text_preserved": True, "whole_document_coverage_certified": False,
    }}


def _path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "://" in value:
        raise ValueError("mapping input path must be a local workspace path")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else root / path
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("mapping input must remain inside the workspace")
    return path


def _text(budget: _MappingReadBudget, path: Path) -> str:
    budget.reserve([path], max_file_bytes=MAX_TEXT_BYTES)
    if path not in budget.sizes:
        return ""
    budget.verify(path)
    with path.open("rb") as source:
        raw = source.read(budget.sizes[path] + 1)
    budget.verify(path)
    if len(raw) != budget.sizes[path]:
        raise ValueError("mapping input changed during read")
    text = raw.decode("utf-8")
    if "\x00" in text:
        raise ValueError("mapping input contains NUL bytes")
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cached_source(root: Path, budget: _MappingReadBudget, metadata: Mapping[str, Any], item_key: str) -> tuple[str, dict[str, Any], str] | None:
    content_hash = str(metadata.get("inspected_content_hash") or metadata.get("content_hash") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash):
        return None
    recovery_root = root / "11_state/pdf_local_recovery" / content_hash
    recovery_manifest = recovery_root / "manifest.yml"
    candidates: list[tuple[Path, str]] = [(recovery_manifest, "pdf_local_recovery")]
    # Reuse only the exact declared item-key checkpoints, not arbitrary files or
    # globally scanned Zotero attachments. Every match must bind source bytes.
    if re.fullmatch(r"[A-Z0-9]{1,32}", item_key):
        runs = root / "11_state/runs"
        if runs.exists():
            if runs.is_symlink():
                raise ValueError("mapping checkpoint directory must remain inside the workspace")
            run_dirs = sorted(runs.iterdir(), key=lambda path: path.name, reverse=True)
            if len(run_dirs) > MAX_FILES:
                raise ValueError("mapping checkpoint directory count limit exceeded")
            candidates.extend((run / "items" / safe_filename(item_key) / "frozen_content.yml", "frozen_content") for run in run_dirs if run.is_dir())
    for manifest_path, origin in candidates:
        budget.reserve([manifest_path])
        if manifest_path not in budget.sizes:
            continue
        manifest = budget.read(manifest_path)
        if origin == "pdf_local_recovery":
            matches = manifest.get("cache_version") == "2" and manifest.get("custody_sha256") == content_hash
            expected = manifest.get("text_sha256")
            adequacy = manifest.get("source_adequacy") or {}
        else:
            matches = manifest.get("checkpoint_version") == "1" and manifest.get("content_hash") == content_hash
            expected = manifest.get("text_hash")
            adequacy = manifest.get("source_coverage") or {}
        if not matches:
            continue
        text = _text(budget, manifest_path.parent / "source.txt")
        if not text.strip() or expected != _digest(text):
            raise ValueError("mapping cached source text integrity mismatch")
        scope = str(manifest.get("source_scope") or (adequacy.get("source_scope") if isinstance(adequacy, Mapping) else "") or "unknown")
        return text, {"source_scope": scope, "source_coverage": dict(adequacy) if isinstance(adequacy, Mapping) else {},
                      "custody_content_sha256": content_hash, "source_text_path": str((manifest_path.parent / "source.txt").relative_to(root)),
                      "source_manifest_path": str(manifest_path.relative_to(root)), "whole_document_coverage_certified": False}, origin
    return None


def load_mapping_inputs(workspace: Path | str, note_rows: Sequence[Mapping[str, Any]] | None = None, source_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Load complete selected local text with honest source/note-only coverage.

    No acquisition, network calls, model work, note generation or cache writes
    occur here. Limits fail explicitly; they never silently sample a corpus.
    An explicit empty selection stays empty.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    if note_rows is not None and (isinstance(note_rows, (str, bytes)) or not isinstance(note_rows, Sequence)):
        raise ValueError("note_rows must be a sequence of source/note mappings")
    if source_ids is not None and (isinstance(source_ids, (str, bytes)) or any(not isinstance(sid, str) or not sid for sid in source_ids)):
        raise ValueError("source_ids must be a sequence of nonempty source identities")
    selected = set(source_ids) if source_ids is not None else None
    if selected == set() or note_rows == []:
        return {"projection_rows": [], "text_sources": {}, "input_paths": [], "input_stamps": {}, "coverage": {"selected_source_count": 0, "loaded_source_count": 0, "missing_source_ids": [], "source_scope": "empty_selection"}}
    budget = _MappingReadBudget(root, max_files=MAX_FILES, max_file_bytes=MAX_TEXT_BYTES, max_total_bytes=MAX_TOTAL_BYTES)
    catalogue_path = root / "02_source_memory/indexes/source_catalogue.yml"
    projection: dict[str, dict[str, Any]] = {}
    entries: dict[str, Mapping[str, Any]] = {}
    if catalogue_path.exists():
        budget.reserve([catalogue_path], max_file_bytes=MAX_INDEX_DOCUMENT_BYTES, max_nodes=MAX_INDEX_YAML_NODES)
        catalogue = budget.read(catalogue_path)
        for entry in catalogue.get("sources", []):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("source_id"), str) or not entry["source_id"] or entry["source_id"] in entries:
                raise ValueError("duplicate or missing canonical catalogue source identity")
            entries[entry["source_id"]] = entry
        # Project selected existing canonical profiles with the same retrieval
        # contract. Do not construct or load a parallel citation/discovery index.
        wanted = selected if selected is not None else {row.get("source_id") for row in note_rows if isinstance(row, Mapping)} if note_rows is not None else set(entries)
        for sid in sorted(set(entries) & wanted):
            nid = entries[sid].get("note_id")
            if not isinstance(nid, str) or not nid:
                raise ValueError("canonical catalogue note identity is required")
            profile_path = profile_sidecar_path(root / "02_source_memory/profiles", nid)
            budget.reserve([profile_path])
            wrapper = budget.read(profile_path)
            if not wrapper:
                continue
            if wrapper.get("profile_schema_version") != "1" or not isinstance(wrapper.get("profile"), Mapping):
                raise ValueError("invalid canonical mapping profile")
            profile = profile_from_dict(wrapper["profile"])
            if profile.source_id != sid or profile.note_id != nid:
                raise ValueError("mapping canonical profile identity mismatch")
            compact = local_retrieval_projection([profile_to_dict(profile)], {"sources": [entries[sid]]})
            row = compact[0]
            row.update(profile_hash=profile_content_hash(profile), profile_hash_aliases=list(profile_hash_aliases(profile)))
            projection[sid] = row
    if note_rows is None:
        supplied = [{**dict(entry), **projection.get(sid, {})} for sid, entry in entries.items()]
    else:
        supplied = list(note_rows)
    if len(supplied) > MAX_FILES:
        raise ValueError("mapping source count limit exceeded")
    texts, rows, seen = {}, [], set()
    for supplied_row in supplied:
        if not isinstance(supplied_row, Mapping):
            raise ValueError("mapping note row must be a mapping")
        sid, nid = supplied_row.get("source_id"), supplied_row.get("note_id")
        if not isinstance(sid, str) or not sid or not isinstance(nid, str) or not nid:
            raise ValueError("mapping requires canonical source_id and note_id")
        if Path(nid).name != nid or nid in {".", ".."} or "\\" in nid or "\x00" in sid or "\x00" in nid:
            raise ValueError("mapping source/note identity is unsafe")
        if selected is not None and sid not in selected:
            continue
        if sid in seen:
            raise ValueError("duplicate mapping source identity")
        seen.add(sid)
        row = {**dict(supplied_row), **projection.get(sid, {})}
        identity = row.pop("identity", entries.get(sid, {}).get("identity"))
        if identity is not None:
            if not isinstance(identity, Mapping):
                raise ValueError("mapping catalogue identity must be a mapping")
            row["catalogue_identity"] = dict(identity)
            fields = ("instance_id", "library_type", "library_id", "item_key")
            if all(isinstance(identity.get(key), str) and identity[key].strip() for key in fields):
                row["identity"] = {key: identity[key] for key in fields}
            if "zotero_item_keys" not in row and isinstance(identity.get("zotero_item_keys"), (list, tuple)):
                row["zotero_item_keys"] = list(identity["zotero_item_keys"])
        if row.get("note_id") != nid:
            raise ValueError("mapping source/note identity conflicts with canonical profile")
        metadata_path = root / "11_state/note_metadata" / f"{nid}.yml"
        profile_path = profile_sidecar_path(root / "02_source_memory/profiles", nid)
        budget.reserve([metadata_path, profile_path])
        metadata = budget.read(metadata_path)
        stored = metadata.get("frontmatter") or {}
        if stored and (not isinstance(stored, Mapping) or stored.get("source_id") != sid or stored.get("note_id") != nid):
            raise ValueError("mapping note metadata identity mismatch")
        declared_path = supplied_row.get("note_path") or row.get("note_path")
        metadata_note_path = metadata.get("note_path")
        if metadata_note_path and declared_path and _path(root, metadata_note_path).resolve() != _path(root, declared_path).resolve():
            raise ValueError("mapping metadata note path conflicts with selected note")
        path_value = metadata_note_path or declared_path
        if not path_value:
            continue
        note_path = _path(root, path_value)
        raw = _text(budget, note_path)
        if not raw:
            continue
        front, _ = parse_atomic_note(raw)
        if any(key in front and front[key] != expected for key, expected in (("source_id", sid), ("note_id", nid))):
            raise ValueError("mapping note identity does not match its selected source")
        if any(key not in front for key in ("source_id", "note_id")):
            # Current readable public notes omit machine source identity. Only
            # the existing protected canonical sidecar may restore it. A caller
            # row or a path supplied by a different note is not such a binding.
            if (metadata.get("metadata_schema_version") != NOTE_METADATA_SCHEMA_VERSION
                    or not isinstance(stored, Mapping) or stored.get("source_id") != sid or stored.get("note_id") != nid
                    or not metadata_note_path or _path(root, metadata_note_path).resolve() != note_path.resolve()):
                raise ValueError("mapping public note identity requires its verified canonical metadata sidecar")
        full_metadata = {**front, **stored}
        profile = budget.read(profile_path).get("profile") or {}
        if not isinstance(profile, Mapping):
            raise ValueError("mapping profile must be a canonical mapping")
        context = (profile.get("context") or {}) if isinstance(profile, Mapping) else {}
        if profile and (profile.get("source_id") != sid or profile.get("note_id") != nid):
            raise ValueError("mapping profile identity mismatch")
        bundle_value = context.get("source_analysis_bundle_path") if isinstance(context, Mapping) else None
        if bundle_value:
            bundle_path = _path(root, bundle_value)
            budget.reserve([bundle_path])
            bundle = budget.read(bundle_path).get("bundle") or {}
            if not isinstance(bundle, Mapping):
                raise ValueError("mapping source bundle must be a canonical mapping")
            identity = (bundle.get("source_identity") or {}) if isinstance(bundle, Mapping) else {}
            if bundle and (not isinstance(identity, Mapping) or identity.get("source_id") != sid):
                raise ValueError("mapping source bundle identity mismatch")
        item_key = str(full_metadata.get("zotero_item_key") or supplied_row.get("zotero_item_key") or row.get("zotero_key") or "")
        cached = _cached_source(root, budget, full_metadata, item_key)
        if cached:
            text, coverage, origin = cached
            ownership_spans = full_metadata.get("mapping_ownership_spans") or []
            source_scope = coverage["source_scope"]
        else:
            text, origin, source_scope = raw, "canonical_note", "note_only"
            ownership_spans = []
            coverage = {"source_scope": "note_only", "source_document_scope": str(full_metadata.get("source_scope") or "unknown"), "reason": "source_text_cache_unavailable; complete_note_only", "whole_document_coverage_certified": False}
        _ownership(text, ownership_spans)
        row.update(source_id=sid, note_id=nid, note_path=str(note_path.relative_to(root)), projection_hash=_digest(raw))
        row.setdefault("note_hash", supplied_row.get("note_hash") or _digest(raw))
        if profile:
            row.update(profile_path=str(profile_path.relative_to(root)), profile_file_sha256=_digest(_text(budget, profile_path)))
        if metadata:
            row.update(metadata_path=str(metadata_path.relative_to(root)), metadata_file_sha256=_digest(_text(budget, metadata_path)))
        rows.append(row)
        texts[sid] = {"source_id": sid, "note_id": nid, "text": text, "ownership_spans": ownership_spans,
                      "content_sha256": _digest(text), "source_scope": source_scope, "text_origin": origin, "coverage": coverage,
                      "note_path": row["note_path"], "note_content_sha256": _digest(raw)}
    requested = selected if selected is not None else seen
    scopes = Counter(row["source_scope"] for row in texts.values())
    for path in budget.sizes:
        budget.verify(path)
    return {"projection_rows": sorted(rows, key=lambda row: row["source_id"]), "text_sources": dict(sorted(texts.items())),
            "input_paths": [str(path) for path in sorted(budget.sizes)],
            "input_stamps": {str(path): list(budget.input_stamps[path]) for path in sorted(budget.sizes)}, "coverage": {
                "selected_source_count": len(requested), "loaded_source_count": len(texts), "missing_source_ids": sorted(requested - texts.keys()),
                "scope_counts": dict(sorted(scopes.items())), "files_read": len(budget.sizes), "bytes_reserved": budget.total_bytes,
                "no_acquisition": True, "whole_document_coverage_certified": False}}
