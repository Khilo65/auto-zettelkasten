from __future__ import annotations

from auto_zettelkasten.pipeline import (
    _apply_bibliographic_scope,
    _limited_analysis,
)
from auto_zettelkasten.readers import SECTION_KEYS


def _analysis(**overrides: str) -> dict[str, str]:
    analysis = {key: f"Supported {key}." for key in SECTION_KEYS}
    analysis.update(overrides)
    return analysis


def test_partial_analysis_discloses_recovered_and_missing_pages() -> None:
    limited = _limited_analysis(
        {
            "source_scope": "partial_document",
            "coverage_reason": "partial_pdf_text_extracted",
            "coverage_metrics": {
                "recovered_pages": [1, 2, 4],
                "unresolved_pages": [3],
            },
            "analysis": _analysis(
                thesis="The recovered pages describe a mediation mechanism (PDF page 2).",
                locators="PDF page 2.",
            ),
        },
        {"key": "ITEM", "data": {"title": "Partial source"}},
    )

    assert "only the recovered pages" in limited["available_content_findings"]
    assert "Recovered PDF pages: 1, 2, 4" in limited["scope_limitation"]
    assert "Missing or unresolved PDF pages: 3" in limited["scope_limitation"]
    assert "not a complete-document analysis" in limited["scope_limitation"]


def test_book_introduction_attachment_is_not_treated_as_the_full_book() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {"page_count": 18},
            "text": "--- Page 1 ---\nChapter 1 Introduction\nOpening argument.",
            "rank": 100,
        },
        {"itemType": "book", "title": "Elusive Peace"},
        {"itemType": "attachment", "title": "PDF"},
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["coverage_reason"] == "bounded_attachment_excerpt"
    assert scoped["coverage_metrics"]["missing_scope"] == "remainder_of_parent_work"
    assert scoped["coverage_metrics"]["recovered_pages"] == list(range(1, 19))


def test_numbered_book_chapter_excerpt_is_not_treated_as_the_full_book() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {"page_count": 17},
            "text": (
                "--- Page 1 ---\nOne\nPEACEKEEPING AND THE PEACEKEPT\n"
                + ("Chapter argument. " * 300)
                + "\n--- Page 2 ---\n2 CHAPTER ONE"
            ),
            "rank": 100,
        },
        {"itemType": "book", "title": "Does Peacekeeping Work?"},
        {"itemType": "attachment", "title": "PDF"},
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["coverage_reason"] == "bounded_attachment_excerpt"


def test_complete_thesis_introduction_is_not_misclassified_as_an_excerpt() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {"page_count": 60},
            "text": (
                "--- Page 1 ---\nA COMPLETE THESIS\n"
                "Introduction\nThis thesis examines conflict recurrence.\n"
            ),
            "rank": 100,
        },
        {"itemType": "thesis", "title": "A Complete Thesis"},
        {"itemType": "attachment", "title": "A Complete Thesis.pdf"},
    )

    assert scoped["source_scope"] == "full_document"


def test_partial_fulfillment_in_complete_thesis_title_does_not_mean_excerpt() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 60,
                "recovered_pages": list(range(1, 61)),
            },
            "text": (
                "--- Page 1 ---\nA dissertation submitted in partial fulfillment "
                "of the requirements.\n"
            ),
            "rank": 100,
        },
        {
            "itemType": "thesis",
            "title": "Ethnicity and Conflict Recurrence: An Analysis on the Deterioration of Peace",
        },
        {
            "itemType": "attachment",
            "title": "Civil War Recurrence - Partial Fulfillment.pdf",
        },
    )

    assert scoped["source_scope"] == "full_document"


def test_pathways_for_peace_introduction_label_is_not_treated_as_excerpt() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 337,
                "recovered_pages": list(range(1, 338)),
            },
            "text": "--- Page 1 ---\nPathways for Peace\nInclusive Approaches.",
            "rank": 100,
        },
        {"itemType": "book", "title": "Pathways for Peace"},
        {"itemType": "attachment", "title": "Introduction"},
    )

    assert scoped["source_scope"] == "full_document"


def test_pathways_for_peace_contents_does_not_make_full_book_an_excerpt() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 337,
                "recovered_pages": list(range(1, 338)),
            },
            "text": (
                "--- Page 1 ---\nPathways for Peace\n"
                "--- Page 7 ---\nContents\nIntroduction 1\nAppendix A 295\n"
            ),
            "rank": 100,
        },
        {"itemType": "book", "title": "Pathways for Peace"},
        {
            "itemType": "attachment",
            "title": "World Bank Group_United Nations_2018_Pathways for Peace.pdf",
        },
    )

    assert scoped["source_scope"] == "full_document"


def test_short_report_preface_label_is_treated_as_an_excerpt() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 40,
                "recovered_pages": list(range(1, 41)),
            },
            "text": "--- Page 1 ---\nPreface\nOpening context.",
            "rank": 100,
        },
        {"itemType": "report", "title": "Longer Parent Report"},
        {"itemType": "attachment", "title": "Preface"},
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["coverage_reason"] == "bounded_attachment_excerpt"


def test_explicit_appendix_label_remains_authoritative_when_long() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 337,
                "recovered_pages": list(range(1, 338)),
            },
            "text": "--- Page 1 ---\nAppendix materials.",
            "rank": 100,
        },
        {"itemType": "report", "title": "Longer Parent Report"},
        {"itemType": "attachment", "title": "Appendix A"},
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["bounded_source_object"] == "Appendix"


def test_spelled_book_chapter_label_remains_authoritative_when_long() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {
                "page_count": 140,
                "recovered_pages": list(range(1, 141)),
            },
            "text": "--- Page 1 ---\nDurable Peace\nChapter argument.",
            "rank": 100,
        },
        {"itemType": "book", "title": "Parent Book"},
        {
            "itemType": "attachment",
            "title": "Chapter Four - Durable Peace",
        },
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["coverage_reason"] == "bounded_attachment_excerpt"


def test_thesis_excerpt_with_missing_contents_span_is_partial() -> None:
    scoped = _apply_bibliographic_scope(
        {
            "source_scope": "full_document",
            "source_coverage": {
                "source_scope": "full_document",
                "coverage_gate": "passed",
            },
            "coverage_metrics": {"page_count": 52},
            "text": (
                "--- Page 1 ---\nTABLE OF CONTENTS\n"
                "Introduction ........ 1\n"
                "Essay I ........ 37\n"
                "Essay IV ........ 112\n"
                "References ........ 135\n"
            ),
            "rank": 100,
        },
        {"itemType": "thesis", "title": "Dismantling the Conflict Trap"},
        {"itemType": "attachment", "title": "PDF"},
    )

    assert scoped["source_scope"] == "partial_document"
    assert scoped["bounded_source_object"] == (
        "table of contents exceeds attachment span"
    )
