from __future__ import annotations

from pathlib import Path
import re

import pytest

from auto_zettelkasten.notes import (
    SECTION_HEADINGS,
    canonical_source_note_text,
    internal_note_text,
    public_note_frontmatter,
    read_note,
    render_atomic_note,
    render_limited_note,
    semantic_note_hash,
    update_note_frontmatter,
    write_atomic_note,
    write_limited_note,
)


def _metadata(**updates):
    return {
        "note_id": "note-source-links",
        "source_id": "source-links",
        "title": "Source navigation",
        "note_status": "analytical_atomic_note",
        "zotero_item_key": "ITEM0001",
        "source_file": "/private/custody/source.pdf",
        "source_scope": "full_document",
        "source_coverage": {"gate": "passed"},
        "inspected_content_hash": "a" * 64,
        "content_route": "pypdf_text",
        "reader_provider": "test",
        "reader_model": "test",
        "original_zotero_tags": [],
        "normalized_tags": [],
        "related_notes": [],
        **updates,
    }


def _analysis():
    return {key: f"Source discussion of {heading} (p. 3)." for key, heading in SECTION_HEADINGS}


@pytest.mark.parametrize("uri", [
    "zotero://open-pdf/library/items/PDFTEST1",
    "zotero://open-pdf/library/items/PDFTEST1?page=1&annotation=ANNOT001",
    "zotero://open-pdf/groups/123/items/PDFTEST1?page=2",
])
def test_pdf_navigation_is_visible_but_not_source_analysis(uri):
    base = render_atomic_note(_metadata(), _analysis())
    rendered = render_atomic_note(_metadata(source_pdf_uri=uri), _analysis())
    assert f"[Open PDF in Zotero]({uri})" in rendered
    assert "source_pdf_uri" not in public_note_frontmatter(_metadata(source_pdf_uri=uri))
    assert semantic_note_hash(base) == semantic_note_hash(rendered)
    assert canonical_source_note_text(base) == canonical_source_note_text(rendered)


@pytest.mark.parametrize("invalid", [
    "javascript:alert(1)",
    "file:///private/custody/source.pdf",
    "zotero://select/library/items/PDFTEST1",
    "zotero://open-pdf/library/items/PDFTEST1?page=-1",
    "zotero://open-pdf/library/items/PDFTEST1?annotation=made-up",
    "zotero://open-pdf/library/items/PDFTEST1)\n[bad](https://evil.test",
])
def test_invalid_pdf_uri_falls_back_truthfully(invalid):
    rendered = render_atomic_note(_metadata(source_pdf_uri=invalid), _analysis())
    body = rendered.split("\n---\n", 1)[1]
    assert "Open PDF" not in body
    assert "[Open source in Zotero](zotero://select/library/items/ITEM0001)" in body
    assert "/private/custody" not in body


def test_source_fallback_prefers_url_then_group_item():
    rendered = render_atomic_note(_metadata(url="https://example.org/paper"), _analysis())
    assert "[Open source](https://example.org/paper)" in rendered
    rendered = render_atomic_note(_metadata(zotero_item_uri="zotero://select/groups/123/items/ITEM0001"), _analysis())
    assert "[Open source in Zotero](zotero://select/groups/123/items/ITEM0001)" in rendered


def test_refresh_adds_and_updates_navigation_without_regeneration(tmp_path: Path):
    metadata = _metadata()
    path, validation = write_atomic_note(tmp_path, metadata, _analysis())
    assert validation.passed
    baseline = semantic_note_hash(internal_note_text(path))
    uri = "zotero://open-pdf/library/items/PDFTEST1"
    update_note_frontmatter(path, {"source_pdf_uri": uri})
    text = path.read_text()
    assert f"[Open PDF in Zotero]({uri})" in text
    assert "/private/custody" not in text
    assert read_note(path)["frontmatter"]["source_pdf_uri"] == uri
    assert semantic_note_hash(internal_note_text(path)) == baseline
    stat = path.stat().st_mtime_ns
    update_note_frontmatter(path, {"source_pdf_uri": uri})
    assert path.stat().st_mtime_ns == stat
    assert path.read_text() == text
    replacement = "zotero://open-pdf/library/items/ABCD1234"
    update_note_frontmatter(path, {"source_pdf_uri": replacement})
    assert uri not in path.read_text()
    assert path.read_text().count("[Open PDF in Zotero]") == 1
    assert semantic_note_hash(internal_note_text(path)) == baseline


def test_legacy_note_refresh_adds_missing_navigation_even_if_metadata_unchanged(tmp_path: Path):
    path, _ = write_atomic_note(tmp_path, _metadata(), _analysis())
    text = path.read_text()
    old = re.sub(r"<!-- auto-zettelkasten:source:start -->.*?<!-- auto-zettelkasten:source:end -->\n\n", "", text, flags=re.DOTALL)
    path.write_text(old)
    update_note_frontmatter(path, {"title": "Source navigation"})
    assert "[Open source in Zotero]" in path.read_text()


def test_limited_and_fulltext_available_notes_do_not_expose_custody(tmp_path: Path):
    uri = "zotero://open-pdf/library/items/PDFTEST1"
    metadata = _metadata(note_status="metadata_only_source_note", source_scope="metadata_only", source_coverage={"gate": "limited"}, source_pdf_uri=uri)
    path, validation = write_limited_note(tmp_path, metadata, {"available_content": "Bibliographic metadata only."})
    assert validation.passed
    assert f"[Open PDF in Zotero]({uri})" in path.read_text()
    text = render_limited_note(_metadata(note_status="fulltext_available", source_pdf_uri=uri))
    assert "/private/custody" not in text.split("\n---\n", 1)[1]


def test_source_navigation_does_not_mask_prose_edits_or_invent_missing_links():
    base = render_atomic_note(_metadata(zotero_item_key=""), _analysis())
    assert "auto-zettelkasten:source:start" not in base
    assert semantic_note_hash(base) != semantic_note_hash(base.replace("Source discussion", "Changed discussion", 1))


def test_ambiguous_source_block_refresh_fails_without_changing_note(tmp_path: Path):
    path, _ = write_atomic_note(tmp_path, _metadata(), _analysis())
    broken = path.read_text() + "\n<!-- auto-zettelkasten:source:start -->\n"
    path.write_text(broken)
    with pytest.raises(ValueError, match="ambiguous_managed_source_block"):
        update_note_frontmatter(path, {"source_pdf_uri": "zotero://open-pdf/library/items/PDFTEST1"})
    assert path.read_text() == broken


@pytest.mark.parametrize("route", ["pypdf_text", "pdfium_tesseract", "codex_pdf_input_file"])
def test_pdf_verification_notice_is_once_outside_analysis_and_survives_refresh(tmp_path, route):
    metadata = _metadata(content_route=route, source_pdf_uri="zotero://open-pdf/library/items/PDFTEST1")
    path, validation = write_atomic_note(tmp_path, metadata, _analysis())
    assert validation.passed
    notice = "Verify table and chart values, labels and comparisons against the original PDF before relying on them."
    text = path.read_text()
    assert text.count(notice) == 1
    assert notice not in canonical_source_note_text(internal_note_text(path))
    update_note_frontmatter(path, {"title": "Corrected metadata"})
    assert path.read_text().count(notice) == 1
    frozen = (path.read_bytes(), path.stat().st_mtime_ns)
    update_note_frontmatter(path, {"title": "Corrected metadata"})
    assert (path.read_bytes(), path.stat().st_mtime_ns) == frozen
    html = render_atomic_note(_metadata(source_file="/private/source.html", content_route="html_text"), _analysis())
    assert notice not in html
