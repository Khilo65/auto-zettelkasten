from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.pipeline import (
    AtomicFidelityError,
    _apply_bibliographic_scope,
    _limited_analysis,
    _verify_atomic_fidelity,
)
from auto_zettelkasten.readers import SECTION_KEYS


def _analysis(**overrides: str) -> dict[str, str]:
    analysis = {key: f"Supported {key}." for key in SECTION_KEYS}
    analysis.update(overrides)
    return analysis


class _Verifier:
    name = "local-test"
    model = "test-model"

    def __init__(self, replacement: str | None) -> None:
        self.replacement = replacement
        self.calls = 0

    def verify_atomic_claims(self, analysis, *, context):
        self.calls += 1
        if self.replacement is None:
            return {"replacements": []}
        risk = context["risks"][0]
        return {
            "replacements": [
                {
                    "section_key": risk["section_key"],
                    "original": risk["claim"],
                    "replacement": self.replacement,
                    "evidence_locator": "available source text",
                    "risk_ids": [risk["risk_id"]],
                }
            ]
        }


def test_atomic_fidelity_is_surgical_and_checkpointed(tmp_path: Path) -> None:
    original = "Inclusion makes agreements more durable."
    replacement = "The study reports that inclusion is associated with durability."
    analysis = _analysis(
        method_and_research_design="Observational cross-national regression.",
        detailed_findings=original,
    )
    verifier = _Verifier(replacement)
    request = MapRequest(workspace=tmp_path)

    first = _verify_atomic_fidelity(
        verifier,
        analysis,
        source_text="The study reports an association between inclusion and durability.",
        source_scope="full_document",
        coverage_metrics={},
        checkpoint_root=tmp_path,
        request=request,
        progress=None,
    )
    second = _verify_atomic_fidelity(
        verifier,
        analysis,
        source_text="The study reports an association between inclusion and durability.",
        source_scope="full_document",
        coverage_metrics={},
        checkpoint_root=tmp_path,
        request=request,
        progress=None,
    )

    assert first == second
    assert first["detailed_findings"] == replacement
    assert first["method_and_research_design"] == analysis[
        "method_and_research_design"
    ]
    assert verifier.calls == 1


def test_unresolved_atomic_fidelity_risk_is_terminal_for_same_inputs(
    tmp_path: Path,
) -> None:
    analysis = _analysis(
        method_and_research_design="Observational cross-national regression.",
        detailed_findings="Inclusion makes agreements more durable.",
    )
    verifier = _Verifier(None)
    request = MapRequest(workspace=tmp_path)

    for _ in range(2):
        with pytest.raises(AtomicFidelityError):
            _verify_atomic_fidelity(
                verifier,
                analysis,
                source_text="The study reports an association.",
                source_scope="full_document",
                coverage_metrics={},
                checkpoint_root=tmp_path,
                request=request,
                progress=None,
            )

    assert verifier.calls == 1


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
