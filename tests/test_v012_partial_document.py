from __future__ import annotations

from pathlib import Path

import auto_zettelkasten.extraction as extraction
from auto_zettelkasten.extraction import (
    ContentAdequacyClass,
    classify_html_content,
    classify_pdf_text,
)
from auto_zettelkasten.navigation import _profile_is_analytical
from auto_zettelkasten.notes import (
    parse_atomic_note,
    public_note_frontmatter,
    render_limited_note,
    validate_limited_note,
    write_limited_note,
)
from auto_zettelkasten.pipeline import all_workspace_note_rows
from auto_zettelkasten.profiles import deterministic_profile, profile_to_dict


def test_substantive_mixed_pdf_with_one_unresolved_page_is_partial(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        extraction,
        "_ocr_pdf_page",
        lambda *_args: extraction._OCRPageResult(),
    )
    result = extraction.extract_bytes(
        _pdf(
            [
                _prose("introduction", 110),
                _prose("method", 110),
                _prose("results", 110),
                _prose("discussion", 110),
                "scan",
            ]
        ),
        media_type="application/pdf",
        filename="mostly-readable.pdf",
    )

    assert result.status == "succeeded"
    assert result.reason == "partial_pdf_text_extracted"
    assert result.source_scope == "partial_document"
    assert result.source_coverage == "limited"
    assert result.adequacy is not None
    assert result.adequacy.classification == ContentAdequacyClass.PARTIAL_PDF_TEXT
    assert result.coverage_metrics["unresolved_pages"] == (5,)
    assert result.coverage_metrics["recovered_pages"] == (1, 2, 3, 4)
    assert result.coverage_metrics["recovered_page_ratio"] == 0.8
    assert result.coverage_metrics["ordinal_to_printed_page"]["1"] == "1"


def test_pdf_classification_records_printed_page_map_and_source_spans() -> None:
    text = "\n\n".join(
        (
            "--- Page 1 ---\nIntroduction\n" + _prose("opening", 120),
            "--- Page 2 ---\nTable 2.1: Settlement outcomes\n"
            + _prose("table", 120),
            "--- Page 3 ---\nFigure 3: Relapse trend\n"
            + _prose("figure", 120),
        )
    )

    adequacy = classify_pdf_text(
        text,
        page_count=3,
        coverage_metadata={
            "ordinal_to_printed_page": {"1": "i", "2": "1", "3": "2"}
        },
    )

    assert adequacy.is_full_publication
    assert adequacy.metrics is not None
    assert adequacy.metrics["ordinal_to_printed_page"] == {
        "1": "i",
        "2": "1",
        "3": "2",
    }
    assert adequacy.metrics["heading_spans"][0]["label"] == "Introduction"
    assert adequacy.metrics["table_spans"][0]["page_ordinal"] == 2
    assert adequacy.metrics["figure_spans"][0]["printed_page"] == "2"


def test_reference_only_pdf_is_not_analytical() -> None:
    references = "\n".join(
        f"Scholar, A. ({1980 + index}). Evidence and institutions in conflict "
        f"resolution. Journal of Peace Studies {index}. https://example.test/{index} "
        + ("citation detail volume issue pages publisher archive " * 3)
        for index in range(40)
    )
    text = f"--- Page 1 ---\nReferences\n{references}"

    adequacy = classify_pdf_text(text, page_count=1)

    assert adequacy.classification == ContentAdequacyClass.METADATA_ONLY
    assert adequacy.source_scope == "metadata_only"
    assert adequacy.coverage_gate == "failed"
    assert adequacy.reason == "bibliography_only_attachment"
    assert adequacy.metrics is not None
    assert adequacy.metrics["content_kind"] == "bibliography_only"


def test_complete_institutional_html_passes_without_article_or_p_tags() -> None:
    blocks = [
        "The institution explains its mandate governance membership history and "
        "mediation work across regional organizations. " * 24
        for _ in range(5)
    ]
    raw_html = "<html><body>" + "".join(
        f"<div><strong>Section {index}</strong><br>{block}</div>"
        for index, block in enumerate(blocks, start=1)
    ) + "<footer>Institutional access information</footer></body></html>"

    adequacy = classify_html_content(raw_html)

    assert adequacy.is_full_publication
    assert adequacy.metrics is not None
    assert adequacy.metrics["strong_visible_body"] is True
    assert adequacy.metrics["article_word_count"] == 0
    assert adequacy.access_markers == ("institutional access",)


def test_incidental_access_marker_does_not_demote_complete_html() -> None:
    paragraph = (
        "This complete report describes its evidence method findings limitations "
        "and institutional context in source-grounded prose. " * 25
    )
    raw_html = (
        "<html><body><article><h1>Complete report</h1>"
        f"<p>{paragraph}</p><p>{paragraph}</p>"
        "<aside>Sign in via your institution for saved searches.</aside>"
        "</article></body></html>"
    )

    adequacy = classify_html_content(raw_html)

    assert adequacy.is_full_publication
    assert adequacy.access_markers == ("sign in via your institution",)
    assert adequacy.metrics is not None
    assert adequacy.metrics["enclosing_paywall"] is False


def test_long_abstract_inside_paywall_is_not_mistaken_for_full_html() -> None:
    abstract = (
        "This abstract reports the research question method and one bounded finding "
        "without exposing the publication body. " * 45
    )
    raw_html = (
        f"<html><head><meta name='citation_abstract' content='{abstract}'></head>"
        "<body><div>Abstract</div>"
        f"<div>{abstract}</div><div>Purchase access</div>"
        "<div>Sign in via your institution</div></body></html>"
    )

    adequacy = classify_html_content(raw_html)

    assert adequacy.source_scope == "abstract_only"
    assert adequacy.coverage_gate == "limited"
    assert adequacy.metrics is not None
    assert adequacy.metrics["enclosing_paywall"] is True


def test_partial_document_note_is_limited_and_discloses_missing_pages(
    tmp_path: Path,
) -> None:
    frontmatter = _partial_frontmatter()
    content = {
        "available_content_findings": (
            "Across the recovered text, the source reports a negotiated transition "
            "and identifies implementation constraints (PDF pages 1-4)."
        ),
        "coverage_limitation": (
            "Recovered PDF pages 1, 2, 3, and 4 were inspected. PDF page 5 is "
            "missing or unresolved, so this note does not represent the complete document."
        ),
    }

    text = render_limited_note(frontmatter, content)
    validation = validate_limited_note(text)
    path, written_validation = write_limited_note(tmp_path, frontmatter, content)

    assert validation.passed, validation.errors
    assert written_validation.passed, written_validation.errors
    assert "## Available-content Findings" in text
    assert "## Coverage Limitation" in text
    assert "## Thesis" not in text
    projected, _ = parse_atomic_note(path.read_text())
    assert projected["type"] == "limited-source-note"
    assert projected["coverage"] == "partial document"
    assert public_note_frontmatter(frontmatter)["coverage"] == "partial document"


def test_partial_document_profile_is_context_only() -> None:
    note = render_limited_note(
        _partial_frontmatter(),
        {
            "available_content_findings": (
                "The recovered pages describe mediation practice (PDF pages 1-4)."
            ),
            "coverage_limitation": (
                "Recovered PDF pages 1-4 were inspected; PDF page 5 is missing."
            ),
        },
    )

    profile = profile_to_dict(deterministic_profile(note))

    assert profile["excluded_from_synthesis"] is True
    assert profile["validity"]["status"] == "excluded_context_only"
    assert profile["findings"] == []
    assert profile["evidence_anchors"] == []
    assert _profile_is_analytical(profile) is False
    assert "Partial-document context" in profile["context"]["exclusion_reason"]


def test_workspace_source_set_keeps_partial_document_context(tmp_path: Path) -> None:
    write_limited_note(
        tmp_path,
        _partial_frontmatter(),
        {
            "available_content_findings": (
                "The recovered pages describe mediation practice (PDF pages 1-4)."
            ),
            "coverage_limitation": (
                "Recovered PDF pages 1-4 were inspected; PDF page 5 is missing."
            ),
        },
    )

    rows = all_workspace_note_rows(tmp_path)

    assert len(rows) == 1
    assert rows[0]["note_status"] == "partial_document_atomic_note"
    assert rows[0]["source_scope"] == "partial_document"


def test_public_projection_does_not_promote_editors_to_authors() -> None:
    frontmatter = _partial_frontmatter()
    frontmatter["creators"] = [
        {"creatorType": "editor", "firstName": "John", "lastName": "Editor"},
        {"creatorType": "author", "firstName": "Ada", "lastName": "Author"},
        {"creatorType": "author", "name": "Institutional Research Unit"},
    ]

    assert public_note_frontmatter(frontmatter)["authors"] == [
        "Ada Author",
        "Institutional Research Unit",
    ]


def _partial_frontmatter() -> dict[str, object]:
    return {
        "note_id": "note-partial",
        "source_id": "source-partial",
        "note_status": "partial_document_atomic_note",
        "zotero_item_key": "PARTIAL1",
        "source_file": "partial.pdf",
        "inspected_content_hash": "a" * 64,
        "content_route": "pypdf_text",
        "original_zotero_tags": [],
        "normalized_tags": [],
        "related_notes": [],
        "source_scope": "partial_document",
        "source_coverage": {
            "coverage_gate": "limited",
            "source_scope": "partial_document",
        },
        "coverage_metrics": {
            "page_count": 5,
            "recovered_pages": [1, 2, 3, 4],
            "unresolved_pages": [5],
            "recovered_page_ratio": 0.8,
        },
        "title": "Partial Source",
    }


def _prose(label: str, words: int) -> str:
    vocabulary = (
        f"{label} evidence mediation process actors outcome comparison period "
        "context method finding limitation "
    ).split()
    return " ".join(vocabulary[index % len(vocabulary)] for index in range(words))


def _pdf(pages: list[str]) -> bytes:
    page_numbers = [4 + index * 2 for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{number} 0 R".encode() for number in page_numbers)
            + f"] /Count {len(pages)} >>".encode()
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(pages):
        page_number = 4 + index * 2
        content_number = page_number + 1
        escaped = (
            text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        )
        stream = f"BT /F1 9 Tf 36 756 Td ({escaped}) Tj ET".encode("latin-1")
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {content_number} 0 R >>"
                ).encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
            ]
        )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)
