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
