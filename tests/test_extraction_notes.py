from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.extraction import extract_bytes, extract_path
from auto_zettelkasten.notes import render_atomic_note, validate_atomic_note

from conftest import SECTION_KEYS


FIXTURES = Path(__file__).parent / "fixtures"


def test_text_and_html_extraction_are_source_only() -> None:
    text = extract_path(FIXTURES / "readable.txt")
    html = extract_path(FIXTURES / "snapshot.html")
    assert text.status == "succeeded"
    assert html.status == "succeeded"
    assert "secret" not in html.text
    assert "Synthetic source" in html.text


def test_blank_pdf_is_classified_for_vision_or_exhaustion() -> None:
    result = extract_bytes(_minimal_pdf(""), media_type="application/pdf", filename="scan.pdf")
    assert result.status == "failed"
    assert result.route == "pypdf_text"
    assert result.reason in {"empty_or_scanned_pdf", "pdf_error:PdfStreamError"}


def test_readable_pdf_extracts_text() -> None:
    result = extract_bytes(
        _minimal_pdf("Readable synthetic PDF content " * 8),
        media_type="application/pdf",
        filename="readable.pdf",
    )
    assert result.status == "succeeded"
    assert "Readable synthetic PDF" in result.text
    assert "--- Page 1 ---" in result.text


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
        "title": "Synthetic",
    }
    analysis = {key: f"Grounded {key}; see page 1." for key in SECTION_KEYS}
    text = render_atomic_note(frontmatter, analysis)
    assert validate_atomic_note(text).passed
    assert "## Strengths and Contributions" in text
    broken = text.replace("a" * 64, "not-a-hash")
    assert "invalid_inspected_content_hash" in validate_atomic_note(broken).errors


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
