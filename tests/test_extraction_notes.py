from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.extraction import (
    ContentAdequacyClass,
    classify_content_adequacy,
    classify_html_content,
    classify_metadata_only,
    classify_pdf_text,
    extract_bytes,
    extract_path,
)
from auto_zettelkasten.notes import (
    parse_atomic_note,
    read_note,
    render_atomic_note,
    render_limited_note,
    validate_atomic_note,
    validate_limited_note,
    write_atomic_note,
    write_limited_note,
)

from conftest import SECTION_KEYS


FIXTURES = Path(__file__).parent / "fixtures"


def test_text_and_html_extraction_are_source_only() -> None:
    text = extract_path(FIXTURES / "readable.txt")
    html = extract_path(FIXTURES / "snapshot.html")
    assert text.status == "succeeded"
    assert html.status == "succeeded"
    assert "secret" not in html.text
    assert "Synthetic source" in html.text


def test_blank_pdf_is_classified_for_vision_or_review_parking() -> None:
    result = extract_bytes(_minimal_pdf(""), media_type="application/pdf", filename="scan.pdf")
    assert result.status == "failed"
    assert result.route == "pypdf_text"
    assert result.reason in {"empty_or_scanned_pdf", "pdf_error:PdfStreamError"}


def test_readable_pdf_extracts_text() -> None:
    result = extract_bytes(
        _minimal_pdf("Readable synthetic PDF content " * 55),
        media_type="application/pdf",
        filename="readable.pdf",
    )
    assert result.status == "succeeded"
    assert "Readable synthetic PDF" in result.text
    assert "--- Page 1 ---" in result.text
    assert result.adequacy is not None
    assert result.adequacy.classification == ContentAdequacyClass.FULL_PDF_TEXT
    assert result.source_scope == "full_document"
    assert result.source_coverage == "passed"
    assert result.coverage_metrics["page_count"] == 1


def test_low_density_multi_page_pdf_is_not_treated_as_full_publication() -> None:
    pages = "\n".join(
        f"--- Page {page} ---\nDownloaded from SAGE. Please retry the article download."
        for page in range(1, 12)
    )

    adequacy = classify_pdf_text(pages, page_count=11)

    assert adequacy.classification == ContentAdequacyClass.METADATA_ONLY
    assert adequacy.coverage_gate == "failed"
    assert adequacy.reason == "insufficient_pdf_text_density"
    assert adequacy.metrics["covered_page_ratio"] == 1.0
    assert adequacy.metrics["word_density_passed"] is False


def test_clean_full_article_html_passes_full_document_gate() -> None:
    paragraph = "This paragraph reports source evidence, design choices, and findings in enough detail for coverage checks. " * 40
    raw_html = f"""
    <html><body><article>
      <h1>Complete article</h1>
      <h2>Introduction</h2><p>{paragraph}</p>
      <h2>Methods</h2><p>{paragraph}</p>
      <h2>Results</h2><p>{paragraph}</p>
      <h2>Discussion</h2><p>{paragraph}</p>
    </article></body></html>
    """
    adequacy = classify_html_content(raw_html)
    assert adequacy.classification == ContentAdequacyClass.FULL_ARTICLE_HTML
    assert adequacy.is_full_publication
    assert adequacy.metrics["article_section_count"] >= 2
    assert adequacy.metrics["paragraph_count"] == 4


def test_abstract_paywall_html_stays_limited_when_indexed_chars_are_complete() -> None:
    abstract = "This abstract states the question, method, and one bounded finding without exposing the article body."
    raw_html = f"""
    <html><head><meta name="citation_abstract" content="{abstract}"></head>
    <body><article><h1>Restricted article</h1><p>{abstract}</p></article>
    <aside>Purchase access or sign in to access the full text.</aside></body></html>
    """
    adequacy = classify_html_content(
        raw_html,
        coverage_metadata={"indexedChars": 500, "totalChars": 500},
    )
    assert adequacy.classification == ContentAdequacyClass.ABSTRACT_PAYWALL_HTML
    assert adequacy.source_scope == "abstract_only"
    assert adequacy.coverage_gate == "limited"
    assert adequacy.abstract == abstract
    assert adequacy.paywall_markers == ("purchase access",)
    assert adequacy.access_markers == ("sign in to access",)
    assert adequacy.metrics["indexed_chars_reported_complete"] is True
    assert not adequacy.is_full_publication


def test_plain_oxford_snapshot_is_limited_at_abstract_access_boundary() -> None:
    prefix = "Oxford Academic\nResearch Article\nAbstract\n"
    suffix = (
        "\n© The Author 2026\n"
        "You do not currently have access to this article\n"
        "Sign in via your Institution\nPurchase\nRental"
    )
    seed = "The abstract reports a bounded claim, evidence, and method without supplying the publication body. "
    available = 3_784 - len(prefix) - len(suffix)
    abstract_body = (seed * ((available // len(seed)) + 1))[:available]
    snapshot = prefix + abstract_body + suffix
    assert len(snapshot) == 3_784

    adequacy = classify_content_adequacy(
        snapshot,
        media_type="text/plain",
        coverage_metadata={"indexedChars": 3_784, "totalChars": 3_784},
    )
    assert adequacy.classification == ContentAdequacyClass.ABSTRACT_PAYWALL_HTML
    assert adequacy.source_scope == "abstract_only"
    assert adequacy.coverage_gate == "limited"
    assert adequacy.abstract.startswith("The abstract reports a bounded claim")
    assert "©" not in adequacy.abstract
    assert "You do not currently have access" not in adequacy.abstract
    assert "you do not currently have access to this article" in adequacy.paywall_markers
    assert "sign in via your institution" in adequacy.access_markers
    assert adequacy.metrics["indexed_chars_reported_complete"] is True
    assert not adequacy.is_full_publication


def test_complete_plain_text_article_with_abstract_heading_passes_full_document_gate() -> None:
    paragraph = "This sentence reports evidence, design choices, and bounded findings from the complete article. " * 130
    article = "\n".join(
        (
            "Abstract\nA concise summary of the complete article.",
            f"Introduction\n{paragraph}",
            f"Methods\n{paragraph}",
            f"Results\n{paragraph}",
        )
    )

    adequacy = classify_content_adequacy(article, media_type="text/plain")

    assert adequacy.classification == ContentAdequacyClass.FULL_TEXT_DOCUMENT
    assert adequacy.is_full_publication


def test_metadata_only_classification_records_available_coverage() -> None:
    adequacy = classify_metadata_only({"title": "Catalog record", "DOI": "10.1/example", "indexedChars": 90, "totalChars": 90})
    assert adequacy.classification == ContentAdequacyClass.METADATA_ONLY
    assert adequacy.source_scope == "metadata_only"
    assert adequacy.coverage_gate == "failed"
    assert adequacy.metrics["metadata_field_count"] == 4
    assert adequacy.metrics["indexed_chars_reported_complete"] is True
    assert not adequacy.is_full_publication


def test_atomic_note_validator_requires_lineage_and_all_sections() -> None:
    frontmatter = {
        "note_id": "n1",
        "source_id": "s1",
        "note_status": "analytical_atomic_note",
        "zotero_item_key": "Z1",
        "source_file": "zotero://Z1",
        "inspected_content_hash": "a" * 64,
        "content_route": "zotero_fulltext",
        "reader_provider": "fake",
        "reader_model": "fake-1",
        "original_zotero_tags": ["Exact Tag"],
        "normalized_tags": [],
        "related_notes": [],
        "source_scope": "full_document",
        "source_coverage": {"gate": "passed", "char_count": 1200},
        "title": "Synthetic",
    }
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}
    text = render_atomic_note(frontmatter, analysis)
    assert validate_atomic_note(text).passed
    assert "## Strengths and Contributions" in text
    broken = text.replace("a" * 64, "not-a-hash")
    assert "invalid_inspected_content_hash" in validate_atomic_note(broken).errors
    assert "human_review" not in text
    assert "## Automated Validation" not in text
    no_scope = text.replace("source_scope: full_document\n", "")
    assert "source_scope_full_document_required" in validate_atomic_note(no_scope).errors
    limited_coverage = text.replace("gate: passed", "gate: limited")
    assert "source_coverage_gate_not_passed" in validate_atomic_note(limited_coverage).errors
    untraceable = text.replace("Grounded locators; see page 1.", "N/A")
    untraceable_validation = validate_atomic_note(untraceable)
    assert untraceable_validation.passed
    assert "untraceable_locators" in untraceable_validation.warnings
    web_locator = text.replace(
        "Grounded locators; see page 1.",
        'https://example.org/report, heading "Findings"',
    )
    assert "untraceable_locators" not in validate_atomic_note(web_locator).errors


def test_atomic_note_validator_requires_lay_explanation_of_statistical_findings() -> None:
    frontmatter = _note_frontmatter(
        status="analytical_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    frontmatter.update(reader_provider="fake", reader_model="fake-1")
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}
    analysis["detailed_findings"] = (
        "The treatment coefficient was 0.12, equivalent to a reported 12 percent increase versus the control group "
        "(95% confidence interval 0.04 to 0.20; p < 0.05; sample size n = 800). See page 1."
    )
    analysis["plain_english_interpretation"] = (
        "Direction: The measured outcome was higher in the treatment group. "
        "Magnitude: The source reports about 12 more units per 100 relative to the stated control comparison. "
        "Reference point: The comparison is the study's control group, not the population as a whole. "
        "Uncertainty: The interval runs from 4 to 20 per 100; p < 0.05 is evidence against the study's null model, "
        "not a 95 percent probability that the claim is true. "
        "Practical meaning: In groups of 100 measured under the study conditions, the reported difference is roughly 12 outcomes."
    )

    valid = render_atomic_note(frontmatter, analysis)
    assert validate_atomic_note(valid).passed

    copied = dict(analysis)
    copied["plain_english_interpretation"] = copied["detailed_findings"]
    copied_validation = validate_atomic_note(render_atomic_note(frontmatter, copied))
    assert copied_validation.passed
    assert (
        "plain_english_interpretation_repeats_detailed_findings"
        in copied_validation.warnings
    )

    fluent = dict(analysis)
    fluent["plain_english_interpretation"] = (
        "The treated group had a higher measured outcome than the comparison group. The source reports a difference "
        "of 12 per 100, with a plausible range of 4 to 20 per 100 under its model. That is meaningful within this "
        "study, but it does not automatically describe people or settings outside the sample."
    )
    assert validate_atomic_note(render_atomic_note(frontmatter, fluent)).passed


def test_qualitative_note_can_explain_absent_statistics_without_forced_labels() -> None:
    frontmatter = _note_frontmatter(
        status="analytical_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    frontmatter.update(reader_provider="fake", reader_model="fake-1")
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}
    analysis["detailed_findings"] = (
        "No quantitative estimates, sample sizes, confidence intervals, or technical effect measures are reported. "
        "The source instead compares qualitative strengths and weaknesses. See page 1."
    )
    analysis["plain_english_interpretation"] = (
        "The paper offers qualitative comparisons only, so its claims cannot be translated into numerical effect sizes "
        "or statistical certainty."
    )

    validation = validate_atomic_note(render_atomic_note(frontmatter, analysis))
    assert validation.passed, validation.errors


def test_atomic_note_validator_accepts_traceable_paragraph_ranges() -> None:
    frontmatter = _note_frontmatter(
        status="analytical_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    frontmatter.update(reader_provider="fake", reader_model="fake-1")
    analysis = {key: f"Grounded {key}; see paragraph 1." for key in SECTION_KEYS}
    analysis["locators"] = "Entire resolution, paragraphs 1–21."

    validation = validate_atomic_note(render_atomic_note(frontmatter, analysis))
    assert validation.passed, validation.errors


@pytest.mark.parametrize(
    ("status", "scope", "gate", "content", "heading"),
    [
        (
            "abstract_only_atomic_note",
            "abstract_only",
            "limited",
            {
                "available_summary": "A bounded abstract with no full-text claims.",
                "what_requires_full_text": "Methods and evidence require the complete document.",
            },
            "Abstract",
        ),
        ("metadata_only_source_note", "metadata_only", "failed", {"metadata": {"title": "Metadata source"}}, "Source Metadata"),
        ("fulltext_available", "full_document", "passed", {"availability": "The complete PDF is locally available."}, "Full Text Availability"),
    ],
)
def test_limited_note_renderer_and_validator_are_status_specific(
    status: str,
    scope: str,
    gate: str,
    content: dict[str, object],
    heading: str,
) -> None:
    frontmatter = _note_frontmatter(status=status, source_scope=scope, coverage_gate=gate)
    text = render_limited_note(frontmatter, content)
    validation = validate_limited_note(text)
    assert validation.passed, validation.errors
    assert f"## {heading}" in text
    assert "## Thesis" not in text
    assert text.count("\n## ") == 2
    assert "human review" not in text.casefold()


def test_limited_note_validator_rejects_scope_status_mismatch() -> None:
    frontmatter = _note_frontmatter(
        status="abstract_only_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    text = render_limited_note(frontmatter, {"abstract": "An abstract."})
    validation = validate_limited_note(text)
    assert "invalid_source_scope:abstract_only_required" in validation.errors
    assert "invalid_source_coverage_gate" in validation.errors


def test_write_limited_note_reuses_note_id_path_and_atomically_replaces_old_note(tmp_path: Path) -> None:
    frontmatter = _note_frontmatter(
        status="metadata_only_source_note",
        source_scope="metadata_only",
        coverage_gate="failed",
    )
    first_path, first_validation = write_limited_note(tmp_path, frontmatter, {"metadata": {"title": "Old catalog record"}})
    assert first_validation.passed
    assert "Old catalog record" in first_path.read_text()

    updated = dict(frontmatter)
    updated["title"] = "Renamed Source"
    second_path, second_validation = write_limited_note(tmp_path, updated, {"metadata": {"title": "New catalog record"}})
    assert second_validation.passed
    assert second_path == first_path
    replaced = second_path.read_text()
    assert "New catalog record" in replaced
    assert "Old catalog record" not in replaced
    projected, _ = parse_atomic_note(replaced)
    assert projected["type"] == "limited-source-note"
    assert projected["coverage"] == "metadata only"
    assert "content_route" not in projected
    assert read_note(second_path)["frontmatter"]["note_status"] == "metadata_only_source_note"


def test_committed_note_projects_clean_frontmatter_and_keeps_machine_sidecar(
    tmp_path: Path,
) -> None:
    frontmatter = _note_frontmatter(
        status="analytical_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    frontmatter.update(
        note_id="note-clean",
        source_id="source-clean",
        title="Clean Projection",
        creators=[{"firstName": "Ada", "lastName": "Lovelace"}],
        date="1843",
        doi="10.1/clean",
        reader_provider="deepseek",
        reader_model="deepseek-v4-flash",
        inspected_content_hash="c" * 64,
        content_route="pypdf_pdfium_tesseract",
        coverage_metrics={
            "ocr_page_count": 11,
            "page_routes": ["pdfium_tesseract"] * 11,
        },
    )
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}

    path, validation = write_atomic_note(tmp_path, frontmatter, analysis)

    assert validation.passed
    projected, _ = parse_atomic_note(path.read_text())
    assert projected == {
        "note_id": "note-clean",
        "type": "atomic-note",
        "title": "Clean Projection",
        "authors": ["Ada Lovelace"],
        "date": "1843",
        "doi": "10.1/clean",
        "coverage": "full text",
    }
    machine = read_note(path)["frontmatter"]
    assert machine["content_route"] == "pypdf_pdfium_tesseract"
    assert machine["coverage_metrics"]["ocr_page_count"] == 11
    assert (tmp_path / "11_state" / "note_metadata" / "note-clean.yml").is_file()


def test_current_workspace_does_not_scan_all_notes_for_a_new_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "11_state" / "note_metadata").mkdir(parents=True)
    notes_dir = tmp_path / "02_source_memory" / "notes"
    notes_dir.mkdir(parents=True)
    original_glob = Path.glob

    def reject_note_scan(path: Path, pattern: str):
        if path == notes_dir and pattern == "*.md":
            raise AssertionError("new-note lookup scanned the whole note library")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_note_scan)
    frontmatter = _note_frontmatter(
        status="analytical_atomic_note",
        source_scope="full_document",
        coverage_gate="passed",
    )
    frontmatter.update(
        note_id="note-new",
        source_id="source-new",
        title="New Note",
        reader_provider="deepseek",
        reader_model="deepseek-v4-flash",
    )
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}

    path, validation = write_atomic_note(tmp_path, frontmatter, analysis)

    assert validation.passed
    assert path.is_file()


def _note_frontmatter(*, status: str, source_scope: str, coverage_gate: str) -> dict[str, object]:
    return {
        "note_id": "limited-n1",
        "source_id": "limited-s1",
        "note_status": status,
        "zotero_item_key": "LIMITED1",
        "source_file": "zotero://LIMITED1",
        "inspected_content_hash": "b" * 64,
        "content_route": "zotero_indexed_content",
        "original_zotero_tags": [],
        "normalized_tags": [],
        "related_notes": [],
        "source_scope": source_scope,
        "source_coverage": {"gate": coverage_gate},
        "title": "Limited Synthetic Source",
    }


def _minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
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
    content.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(content)
