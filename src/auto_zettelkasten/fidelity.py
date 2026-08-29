from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


ANALYSIS_SECTION_KEYS = (
    "thesis",
    "key_concepts_and_definitions",
    "method_and_research_design",
    "evidence_and_data",
    "detailed_findings",
    "plain_english_interpretation",
    "strengths_and_contributions",
    "methodological_critique",
    "limitations",
    "what_this_source_can_support",
    "what_this_source_cannot_support",
    "locators",
)
ATOMIC_FIDELITY_VERSION = "6"

_CAUSAL_SCAN_KEYS = {
    "thesis",
    "detailed_findings",
    "plain_english_interpretation",
    "strengths_and_contributions",
    "what_this_source_can_support",
}
_PAGE_LOCATOR = re.compile(
    r"\b(?:PDF\s+)?(?:p{1,2}\.\s*|p{1,2}\s+|pages?\s+)\[?\s*"
    r"(?P<start>\d+)\s*\]?"
    r"(?:\s*[-\u2013\u2014]\s*\[?\s*(?P<end>\d+)\s*\]?)?",
    flags=re.IGNORECASE,
)
_OBJECT_LOCATOR = re.compile(
    r"\b(?P<kind>table|figure)\s+"
    r"(?P<label>(?:\d+[A-Z]?)(?:\.(?:\d+[A-Z]?))*|[IVXLCDM]+|[A-Z])\b",
    flags=re.IGNORECASE,
)
_EXPLICIT_HEADING = re.compile(
    r"\bsection\s+(?P<section_label>\d+(?:\.\d+)*|[ivxlcdm]+|[A-Z])\b|"
    r"\bheading\s+[\"“']?(?P<heading_label>[A-Za-z0-9][^\"”';,()]{1,80})",
    flags=re.IGNORECASE,
)
_CAUSAL_LANGUAGE = re.compile(
    r"\b(?:caused|causing|leads?\s+to|led\s+to|results?\s+in|"
    r"drives?|driven\s+by|prevents?|prevented|reduces?|reduced|"
    r"increases\s+(?!(?:by|from|to|between|since|over)\b)"
    r"(?:the\s+)?[a-z][a-z-]*|"
    r"increase\s+(?:the\s+)?(?:risk|chance|likelihood|probability|odds|"
    r"duration|incidence|frequency|level|amount|number|extent|severity|"
    r"participation|durability|relapse|conflict|fighting|violence)|"
    r"increased\s+(?:the\s+)?(?:risk|chance|likelihood|probability|odds|"
    r"duration|incidence|frequency|level|amount|number|extent|severity|"
    r"participation|durability|relapse|conflict|fighting|violence)|"
    r"produces?|produced|determines?|makes?\s+[^.;:]{0,60}\s+more|"
    r"works?\s+only\s+if|effect\s+of|impact\s+of)\b",
    flags=re.IGNORECASE,
)
_CAUSAL_DESIGN = re.compile(
    r"\b(?:randomi[sz](?:ed|ation)|randomized controlled trial|rct|"
    r"quasi[- ]experimental|natural experiment|instrumental variables?|"
    r"regression discontinuity|difference[- ]in[- ]differences|"
    r"causal identification|identified causal effect|synthetic control)\b",
    flags=re.IGNORECASE,
)
_CAUSAL_QUALIFIER = re.compile(
    r"\b(?:according to (?:the )?(?:author|authors|report|study|source|volume|"
    r"guidance|document|book|paper|article|panel|panelists?|manual|commission|"
    r"session)|"
    r"(?:the )?(?:author|authors|report|study|source|volume|guidance|document|"
    r"book|paper|article|panel|panelists?|manual|commission|session|it) "
    r"(?:argues?|claims?|suggests?|proposes?|interprets?|reports?|finds?|"
    r"concludes?|states?|describes?|identifies?|recommends?|notes?|"
    r"emphasizes?|emphasized|highlights?|highlighted|observes?|observed|"
    r"noted|reported)|"
    r"may|might|could|can|should|must|appears?\s+to|aims?\s+to|seeks?\s+to|"
    r"designed\s+to|required\s+to|needed\s+to|intended\s+to|"
    r"strateg(?:y|ies)\s+to|"
    r"(?:is|are|was|were)\s+associated with|association|correlates? with|"
    r"predicts?|predicted probability|coefficient|hazard ratio|"
    r"model(?:s|led)?\s+(?:predicts?|suggests?)|preliminary|conditional|"
    r"(?:partly\s+)?attributed to|"
    r"(?:an?|the|this|that|reported|observed|descriptive)\s+"
    r"(?:increase|reduction|decrease|trend)\s+(?:in|of)|"
    r"does not|do not|did not|cannot|no evidence (?:that|of)|not establish)\b",
    flags=re.IGNORECASE,
)
_NAMED_ATTRIBUTION = re.compile(
    r"\b(?:According to (?!the\b)[A-Z][A-Za-z'-]{2,}|"
    r"as (?:argued|reported|noted|described) by [A-Z][A-Za-z'-]{2,}|"
    r"[A-Z][A-Za-z'-]{2,}\s+"
    r"(?:argues?|claims?|suggests?|proposes?|interprets?|reports?|finds?|"
    r"concludes?|states?|describes?|identifies?|recommends?|notes?|"
    r"emphasizes?|emphasized|highlights?|highlighted|observes?|observed|"
    r"noted|reported))\b"
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)%?")
_STOPWORDS = {
    "about",
    "after",
    "against",
    "because",
    "before",
    "between",
    "could",
    "document",
    "during",
    "finding",
    "findings",
    "from",
    "have",
    "into",
    "more",
    "page",
    "pages",
    "paper",
    "reported",
    "reports",
    "source",
    "study",
    "table",
    "their",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "using",
    "were",
    "which",
    "with",
}
_REPLACEMENT_KEYS = {
    "section_key",
    "original",
    "replacement",
    "evidence_locator",
    "risk_ids",
}


def analyze_atomic_fidelity(
    analysis: Mapping[str, Any],
    source_text: str,
    coverage_metrics: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return conservative, reviewable risks without changing the analysis."""

    metrics = dict(coverage_metrics or {})
    pages = _source_pages(source_text)
    page_count = _positive_int(metrics.get("page_count")) or max(pages, default=0)
    unresolved_pages = set(_positive_ints(metrics.get("unresolved_pages")))
    printed_to_ordinals = _printed_to_ordinals(
        metrics.get("ordinal_to_printed_page")
    )
    inferred = _inferred_printed_to_ordinals(pages)
    if inferred:
        printed_to_ordinals = inferred
    object_spans = {
        kind: _span_index(metrics.get(f"{kind}_spans"))
        for kind in ("table", "figure", "heading")
    }
    risks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for section_key in ANALYSIS_SECTION_KEYS:
        section = str(analysis.get(section_key) or "").strip()
        if not section:
            continue
        for claim in _claim_units(section):
            cited_pages = _cited_page_ordinals(claim, printed_to_ordinals)
            invalid_pages = sorted(
                page
                for page in cited_pages
                if page <= 0
                or (page_count and page > page_count)
                or page in unresolved_pages
            )
            if invalid_pages:
                _add_risk(
                    risks,
                    seen,
                    kind="nonexistent_page_locator",
                    section_key=section_key,
                    claim=claim,
                    locator=", ".join(f"PDF page {page}" for page in invalid_pages),
                    reason="The cited PDF page is absent or unresolved.",
                    details={"invalid_pages": invalid_pages},
                )

            object_rows = list(_OBJECT_LOCATOR.finditer(claim))
            for object_match in object_rows:
                kind = object_match.group("kind").casefold()
                label = _canonical_object_label(
                    kind,
                    object_match.group("label"),
                )
                span = object_spans[kind].get(label)
                if span is None and not re.search(
                    rf"\b{re.escape(label)}\b", source_text, flags=re.IGNORECASE
                ):
                    _add_risk(
                        risks,
                        seen,
                        kind=f"nonexistent_{kind}_locator",
                        section_key=section_key,
                        claim=claim,
                        locator=object_match.group(0),
                        reason=f"The cited {kind} does not occur in the extracted source.",
                    )
                    continue
                span_page = _positive_int((span or {}).get("page_ordinal"))
                if span_page and cited_pages and span_page not in cited_pages:
                    _add_risk(
                        risks,
                        seen,
                        kind="locator_page_mismatch",
                        section_key=section_key,
                        claim=claim,
                        locator=object_match.group(0),
                        reason=(
                            f"The cited {kind} is indexed on PDF page {span_page}, "
                            "not on the page attached to this claim."
                        ),
                        details={"suggested_page": span_page},
                    )

            for heading_match in _EXPLICIT_HEADING.finditer(claim):
                label = _normalized_label(
                    heading_match.group("section_label")
                    or heading_match.group("heading_label")
                    or ""
                )
                source_has_heading = bool(
                    label
                    and re.search(
                        rf"(?im)^\s*(?:section\s+)?{re.escape(label)}(?:\s|[:.-]|$)",
                        source_text,
                    )
                )
                if (
                    label
                    and not source_has_heading
                    and not _matching_heading(label, object_spans["heading"])
                ):
                    _add_risk(
                        risks,
                        seen,
                        kind="nonexistent_heading_locator",
                        section_key=section_key,
                        claim=claim,
                        locator=heading_match.group(0),
                        reason="The named heading does not occur in the extracted source.",
                    )

            if (
                pages
                and len(cited_pages) == 1
                and not invalid_pages
                and not _is_citation_index_claim(section_key, claim)
            ):
                cited_page = next(iter(cited_pages))
                nearby_pages = [
                    page
                    for page in (cited_page - 1, cited_page, cited_page + 1)
                    if page in pages
                ]
                claim_numbers = set(
                    _numeric_tokens(_without_page_locators(claim))
                )
                nearby_numbers = {
                    number
                    for page in nearby_pages
                    for number in _numeric_tokens(pages[page])
                }
                if claim_numbers and not claim_numbers <= nearby_numbers:
                    _add_risk(
                        risks,
                        seen,
                        kind="numeric_not_found_near_locator",
                        section_key=section_key,
                        claim=claim,
                        locator=f"PDF page {cited_page}",
                        reason=(
                            "One or more numeric tokens do not occur on the cited "
                            "page or either adjacent page."
                        ),
                        details={
                            "missing_numeric_tokens": sorted(
                                claim_numbers - nearby_numbers
                            )
                        },
                    )
                adjacent_hint = _adjacent_page_hint(
                    claim,
                    cited_page,
                    pages,
                )
                if adjacent_hint is not None:
                    _add_risk(
                        risks,
                        seen,
                        kind="low_page_support",
                        section_key=section_key,
                        claim=claim,
                        locator=f"PDF page {cited_page}",
                        reason=(
                            f"Claim tokens align more closely with adjacent PDF page "
                            f"{adjacent_hint['suggested_page']}."
                        ),
                        details=adjacent_hint,
                    )

    method = str(analysis.get("method_and_research_design") or "")
    if not _CAUSAL_DESIGN.search(method):
        for section_key in _CAUSAL_SCAN_KEYS:
            section = str(analysis.get(section_key) or "").strip()
            for claim in _claim_units(section):
                if (
                    _CAUSAL_LANGUAGE.search(claim)
                    and not _CAUSAL_QUALIFIER.search(claim)
                    and not _NAMED_ATTRIBUTION.search(claim)
                ):
                    _add_risk(
                        risks,
                        seen,
                        kind="unqualified_causal_wording",
                        section_key=section_key,
                        claim=claim,
                        locator="",
                        reason=(
                            "The analysis uses causal wording without a causal design "
                            "or an explicit source-attribution qualifier."
                        ),
                    )
    return risks


def source_passages_for_risks(
    source_text: str,
    risks: Sequence[Mapping[str, Any]],
    *,
    page_map: Mapping[str, Any] | None = None,
    limit: int = 12,
) -> list[dict[str, str]]:
    """Select only the pages needed to verify flagged claims."""

    pages = _source_pages(source_text)
    if not pages:
        return [{"locator": "available source text", "text": source_text[:36_000]}]
    selected: set[int] = set()
    printed_to_ordinals = _printed_to_ordinals(page_map)
    for risk in risks:
        risk_selected: set[int] = set()
        claim = str(risk.get("claim") or "")
        for page in _cited_page_ordinals(claim, printed_to_ordinals):
            risk_selected.update((page - 1, page, page + 1))
        details = risk.get("details")
        if isinstance(details, Mapping):
            for key in ("cited_page", "suggested_page"):
                page = _positive_int(details.get(key))
                if page is not None:
                    risk_selected.update((page - 1, page, page + 1))
        if str(risk.get("kind") or "") in {
            "numeric_not_found_near_locator",
            "low_page_support",
            "locator_page_mismatch",
        }:
            claim_numbers = set(_numeric_tokens(_without_page_locators(claim)))
            claim_words = _key_tokens(_without_page_locators(claim))
            best = max(
                pages,
                key=lambda page: _page_support_score(
                    claim_numbers,
                    claim_words,
                    pages[page],
                ),
            )
            risk_selected.update((best - 1, best, best + 1))
        if not risk_selected:
            claim_words = _key_tokens(claim)
            if claim_words:
                best = max(
                    pages,
                    key=lambda page: len(claim_words & _key_tokens(pages[page])),
                )
                risk_selected.update((best - 1, best, best + 1))
        selected.update(risk_selected)
    selected = {page for page in selected if page in pages}
    if not selected:
        selected.add(min(pages))
    return [
        {"locator": f"PDF page {page}", "text": pages[page][:6_000]}
        for page in sorted(selected)[: max(1, limit)]
    ]


def validate_atomic_replacements(
    analysis: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    allowed_risk_ids: Sequence[str] | None = None,
    discard_invalid: bool = False,
) -> list[dict[str, Any]]:
    """Validate bounded replacements against the unchanged structured analysis."""

    if set(payload) != {"replacements"} or not isinstance(
        payload.get("replacements"), list
    ):
        raise ValueError("atomic fidelity response must contain only a replacements list")
    if discard_invalid:
        validated: list[dict[str, Any]] = []
        for raw in payload["replacements"]:
            try:
                candidate = validate_atomic_replacements(
                    analysis,
                    {"replacements": [raw]},
                    allowed_risk_ids=allowed_risk_ids,
                )
                validated = validate_atomic_replacements(
                    analysis,
                    {"replacements": [*validated, *candidate]},
                    allowed_risk_ids=allowed_risk_ids,
                )
            except ValueError:
                continue
        return validated
    allowed = set(str(value) for value in allowed_risk_ids or ())
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    by_section: dict[str, list[str]] = {}
    for index, raw in enumerate(payload["replacements"]):
        if not isinstance(raw, Mapping) or set(raw) != _REPLACEMENT_KEYS:
            raise ValueError(f"replacement {index} has an invalid shape")
        row = {key: raw[key] for key in _REPLACEMENT_KEYS}
        section_key = str(row["section_key"]).strip()
        original = str(row["original"]).strip()
        replacement = str(row["replacement"]).strip()
        evidence_locator = str(row["evidence_locator"]).strip()
        risk_ids = row["risk_ids"]
        if section_key not in ANALYSIS_SECTION_KEYS:
            raise ValueError(f"replacement {index} targets an unknown section")
        section = str(analysis.get(section_key) or "")
        if not original or len(original) > 2_000:
            raise ValueError(f"replacement {index} original is empty or too large")
        if not replacement or len(replacement) > 2_500 or replacement == original:
            raise ValueError(f"replacement {index} replacement is empty, unchanged, or too large")
        if not evidence_locator:
            raise ValueError(f"replacement {index} lacks an evidence locator")
        if section.count(original) != 1:
            raise ValueError(
                f"replacement {index} original must occur exactly once in its section"
            )
        if replacement.startswith(("##", "---", "<!--")) or any(
            marker in replacement for marker in ("\n## ", "\n---", "<!--")
        ):
            raise ValueError(f"replacement {index} attempts a structural Markdown edit")
        if not isinstance(risk_ids, list) or not risk_ids or any(
            not isinstance(value, str) or not value.strip() for value in risk_ids
        ):
            raise ValueError(f"replacement {index} risk_ids must be non-empty strings")
        normalized_risk_ids = list(dict.fromkeys(value.strip() for value in risk_ids))
        if allowed and not set(normalized_risk_ids) <= allowed:
            raise ValueError(f"replacement {index} cites an unknown risk_id")
        introduced_numbers = (
            set(_numeric_tokens(replacement))
            - set(_numeric_tokens(original))
            - set(_numeric_tokens(evidence_locator))
        )
        if introduced_numbers:
            raise ValueError(f"replacement {index} introduces unsupported numbers")
        identity = (section_key, original)
        if identity in seen:
            raise ValueError(f"replacement {index} duplicates an earlier edit")
        for other in by_section.get(section_key, []):
            if original in other or other in original:
                raise ValueError(f"replacement {index} overlaps an earlier edit")
        seen.add(identity)
        by_section.setdefault(section_key, []).append(original)
        validated.append(
            {
                "section_key": section_key,
                "original": original,
                "replacement": replacement,
                "evidence_locator": evidence_locator,
                "risk_ids": normalized_risk_ids,
            }
        )
    return validated


def apply_atomic_replacements(
    analysis: Mapping[str, Any],
    replacements: Sequence[Mapping[str, Any]],
    *,
    allowed_risk_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Apply validated edits locally; provider output never rewrites a whole note."""

    validated = validate_atomic_replacements(
        analysis,
        {"replacements": list(replacements)},
        allowed_risk_ids=allowed_risk_ids,
    )
    updated = dict(analysis)
    for row in validated:
        section_key = row["section_key"]
        updated[section_key] = str(updated.get(section_key) or "").replace(
            row["original"],
            row["replacement"],
            1,
        )
    return updated


def _source_pages(source_text: str) -> dict[int, str]:
    matches = list(re.finditer(r"(?m)^--- Page (\d+) ---\s*$", source_text))
    if not matches:
        return {}
    pages: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source_text)
        pages[int(match.group(1))] = source_text[match.end() : end].strip()
    return pages


def _claim_units(section: str) -> list[str]:
    units: list[str] = []
    for raw_line in section.splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", raw_line).strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line)
        units.extend(part.strip() for part in parts if part.strip())
    return units or ([section.strip()] if section.strip() else [])


def _page_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _PAGE_LOCATOR.finditer(value):
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        ranges.append((min(start, end), max(start, end)))
    return ranges


def _printed_to_ordinals(value: Any) -> dict[str, tuple[int, ...]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[int]] = {}
    for raw_ordinal, raw_label in value.items():
        ordinal = _positive_int(raw_ordinal)
        label = str(raw_label or "").strip().casefold()
        bracketed = re.fullmatch(r"\[\s*([^\[\]]+?)\s*\]", label)
        if bracketed:
            label = bracketed.group(1).strip()
        if ordinal is not None and label:
            result.setdefault(label, []).append(ordinal)
    return {label: tuple(ordinals) for label, ordinals in result.items()}


def _inferred_printed_to_ordinals(
    pages: Mapping[int, str],
) -> dict[str, tuple[int, ...]]:
    offsets: list[int] = []
    for ordinal, text in pages.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates: set[int] = set()
        for line in [*lines[:5], *lines[-5:]]:
            match = re.search(r"^(?P<first>\d{1,4})\b|(?P<last>\d{1,4})\s*$", line)
            if match:
                candidates.add(int(match.group("first") or match.group("last")))
        for printed in candidates:
            offset = printed - ordinal
            offsets.append(offset)
    if not offsets:
        return {}
    counts = {offset: offsets.count(offset) for offset in set(offsets)}
    offset, count = max(counts.items(), key=lambda row: (row[1], -abs(row[0])))
    if count < 3:
        return {}
    return {
        str(ordinal + offset): (ordinal,)
        for ordinal in sorted(pages)
        if ordinal + offset > 0
    }


def _cited_page_ordinals(
    value: str,
    printed_to_ordinals: Mapping[str, Sequence[int]],
) -> set[int]:
    pages: set[int] = set()
    for match in _PAGE_LOCATOR.finditer(value):
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        explicit_pdf = match.group(0).lstrip().casefold().startswith("pdf")
        for page in range(min(start, end), max(start, end) + 1):
            printed = () if explicit_pdf else printed_to_ordinals.get(str(page), ())
            pages.update(int(ordinal) for ordinal in printed)
            if not printed:
                pages.add(page)
    return pages


def _span_index(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        label = _normalized_label(str(raw.get("label") or ""))
        if label:
            result[label] = dict(raw)
            object_match = _OBJECT_LOCATOR.search(label)
            if object_match:
                result[
                    _canonical_object_label(
                        object_match.group("kind"),
                        object_match.group("label"),
                    )
                ] = dict(raw)
    return result


def _matching_heading(
    label: str,
    headings: Mapping[str, Mapping[str, Any]],
) -> bool:
    return any(
        candidate == label
        or candidate.startswith(f"{label} ")
        or label.startswith(f"{candidate} ")
        for candidate in headings
    )


def _normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", value.casefold()).strip()


def _canonical_object_label(kind: str, label: str) -> str:
    normalized = label.strip().casefold()
    if re.fullmatch(r"[ivxlcdm]+", normalized):
        normalized = str(_roman_to_int(normalized))
    return f"{kind.casefold()} {normalized}"


def _roman_to_int(value: str) -> int:
    numerals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for character in reversed(value.casefold()):
        current = numerals[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def _is_citation_index_claim(section_key: str, claim: str) -> bool:
    return section_key == "locators" and bool(
        re.search(r"\b(?:footnotes?|notes?)\b", claim, flags=re.IGNORECASE)
    )


def _adjacent_page_hint(
    claim: str,
    cited_page: int,
    pages: Mapping[int, str],
) -> dict[str, Any] | None:
    without_locators = _without_page_locators(claim)
    claim_numbers = set(_numeric_tokens(without_locators))
    claim_words = _key_tokens(without_locators)
    if not claim_numbers and len(claim_words) < 4:
        return None
    current_score = _page_support_score(claim_numbers, claim_words, pages[cited_page])
    candidates = [
        page
        for page in (cited_page - 1, cited_page + 1)
        if page in pages
    ]
    if not candidates:
        return None
    suggested_page = max(
        candidates,
        key=lambda page: _page_support_score(
            claim_numbers,
            claim_words,
            pages[page],
        ),
    )
    suggested_score = _page_support_score(
        claim_numbers,
        claim_words,
        pages[suggested_page],
    )
    numeric_moved = bool(
        claim_numbers
        and not claim_numbers <= set(_numeric_tokens(pages[cited_page]))
        and claim_numbers <= set(_numeric_tokens(pages[suggested_page]))
    )
    if not numeric_moved and not (
        current_score < 0.2
        and suggested_score >= 0.4
        and suggested_score >= current_score + 0.2
    ):
        return None
    return {
        "cited_page": cited_page,
        "suggested_page": suggested_page,
        "cited_page_score": round(current_score, 4),
        "suggested_page_score": round(suggested_score, 4),
    }


def _without_page_locators(value: str) -> str:
    result = value
    for start, end in reversed(
        [(match.start(), match.end()) for match in _PAGE_LOCATOR.finditer(value)]
    ):
        result = result[:start] + result[end:]
    return result


def _page_support_score(
    claim_numbers: set[str],
    claim_words: set[str],
    page_text: str,
) -> float:
    page_numbers = set(_numeric_tokens(page_text))
    page_words = _key_tokens(page_text)
    numeric_score = (
        len(claim_numbers & page_numbers) / len(claim_numbers)
        if claim_numbers
        else 0.0
    )
    lexical_score = (
        len(claim_words & page_words) / len(claim_words) if claim_words else 0.0
    )
    return (
        0.7 * numeric_score + 0.3 * lexical_score
        if claim_numbers
        else lexical_score
    )


def _numeric_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _NUMBER.finditer(value):
        token = match.group(0).replace(",", "").casefold()
        if token.startswith("+"):
            token = token[1:]
        if token.startswith("."):
            token = f"0{token}"
        elif token.startswith("-."):
            token = f"-0{token[1:]}"
        tokens.append(token)
    return tokens


def _key_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W\d_]{5,}", value.casefold(), flags=re.UNICODE)
        if token not in _STOPWORDS
    }


def _positive_ints(value: Any) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(
        number
        for raw in value
        if (number := _positive_int(raw)) is not None
    )


def _positive_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _add_risk(
    risks: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
    *,
    kind: str,
    section_key: str,
    claim: str,
    locator: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    identity = (kind, section_key, claim, locator)
    if identity in seen:
        return
    seen.add(identity)
    risk_id = "atomic-risk-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    risks.append(
        {
            "risk_id": risk_id,
            "kind": kind,
            "section_key": section_key,
            "claim": claim,
            "locator": locator,
            "reason": reason,
            "details": dict(details or {}),
        }
    )
