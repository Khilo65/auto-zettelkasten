"""Concise atomic-note coverage retains the extraction audit and source locators."""
from copy import deepcopy

from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.notes import (
    SECTION_HEADINGS,
    parse_atomic_note,
    render_atomic_note,
    render_limited_note,
    validate_limited_note,
    write_atomic_note,
)


def _partial():
    return {
        "note_id": "note-partial", "source_id": "source-partial",
        "note_status": "partial_document_atomic_note", "title": "Synthetic source",
        "zotero_item_key": "PARTIAL1", "source_file": "source.pdf",
        "inspected_content_hash": "a" * 64, "content_route": "pypdf_text",
        "reader_provider": "fake", "reader_model": "fake-1",
        "original_zotero_tags": [], "normalized_tags": [], "related_notes": [],
        "source_scope": "partial_document",
        "source_coverage": {"coverage_gate": "limited", "source_scope": "partial_document"},
        "coverage_metrics": {"page_count": 416,
                             "recovered_pages": [p for p in range(1, 417) if p != 18],
                             "unresolved_pages": [18], "recovered_page_ratio": 415 / 416},
    }


def _analysis():
    return {key: "A supported source finding appears on PDF page 19."
            for key, _ in SECTION_HEADINGS}


def test_large_page_audit_does_not_fill_the_atomic_note_body():
    metadata = _partial()
    before = deepcopy(metadata)
    text = render_atomic_note(metadata, _analysis())
    parsed, body = parse_atomic_note(text)
    assert "> [!warning] Coverage boundary" in body
    assert "> This note is based on an incomplete extraction of the document." in body
    assert "Recovered PDF pages" not in body
    assert "Missing or unresolved PDF pages" not in body
    assert "PDF page 19" in body
    assert parsed["coverage_metrics"] == before["coverage_metrics"]
    assert metadata == before


def test_written_note_keeps_complete_coverage_metrics_in_sidecar(tmp_path):
    metadata = _partial()
    path, validation = write_atomic_note(tmp_path, metadata, _analysis())
    assert validation.passed, validation.errors
    text = path.read_text()
    assert "Recovered PDF pages" not in text
    assert "recovered_pages" not in text
    assert "PDF page 19" in text
    public, _ = parse_atomic_note(text)
    assert public["coverage"] == "partial document"
    sidecar = read_yaml(tmp_path / "11_state/note_metadata/note-partial.yml")
    assert sidecar["frontmatter"]["coverage_metrics"] == metadata["coverage_metrics"]
    assert sidecar["frontmatter"]["source_coverage"] == metadata["source_coverage"]


def test_bounded_excerpt_has_distinct_short_notice():
    metadata = _partial()
    metadata["coverage_metrics"].update(missing_scope="remainder_of_parent_work",
                                        bibliographic_scope="bounded_attachment_excerpt")
    _, body = parse_atomic_note(render_atomic_note(metadata, _analysis()))
    assert "This note covers only the available excerpt, not the complete work." in body
    assert "incomplete extraction" not in body
    assert "Recovered PDF pages" not in body


def test_full_document_receives_no_partial_coverage_notice():
    metadata = _partial()
    metadata.update(note_status="analytical_atomic_note", source_scope="full_document",
                    source_coverage={"coverage_gate": "passed", "source_scope": "full_document"})
    _, body = parse_atomic_note(render_atomic_note(metadata, _analysis()))
    assert "Coverage boundary" not in body
    assert "incomplete extraction" not in body


def test_legacy_limited_note_retains_page_disclosure_and_validation():
    text = render_limited_note(_partial(), {"available_content_findings": "A finding on PDF page 19."})
    _, body = parse_atomic_note(text)
    assert "Recovered PDF pages: 1, 2, 3" in body
    assert "Missing or unresolved PDF pages: 18" in body
    assert validate_limited_note(text).passed
