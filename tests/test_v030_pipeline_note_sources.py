"""Source navigation and note-only publication do not depend on anchors/providers."""
from pathlib import Path

import pytest

from auto_zettelkasten import pipeline, readers
from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.workspace import initialize


def test_selected_pdf_attachment_identity_survives_frozen_content(tmp_path: Path):
    initialize(tmp_path)
    parent = {"key": "PARENT01", "data": {"key": "PARENT01", "itemType": "journalArticle", "title": "Primary study"}}
    attachments = [
        {"key": key, "data": {"key": key, "parentItem": "PARENT01", "itemType": "attachment", "title": "Primary study", "contentType": "application/pdf"}}
        for key in ("ATTACH01", "ATTACH02")
    ]

    class Client:
        def children(self, key):
            return attachments

        def fulltext(self, key):
            if key == "PARENT01":
                return None
            return {"content": ("This study investigates collective participation and institutional trust. " * (90 if key == "ATTACH02" else 60)),
                    "contentType": "application/pdf", "indexedPages": 1, "totalPages": 1}

        def file(self, key):
            return None

    content = pipeline._acquire_content(tmp_path, parent, Client(),
        {"attempts": [], "source_id": "source-parent", "zotero_item_key": "PARENT01"},
        MapRequest(tmp_path, provider="deepseek", model="fake"), None)
    assert content is not None
    # Equal route/priority uses the richer actual candidate, retaining its identity.
    assert content["attachment_key"] == "ATTACH02"
    assert content["source_pdf_uri"] == "zotero://open-pdf/library/items/ATTACH02"
    checkpoint = tmp_path / "checkpoint"
    pipeline._write_frozen_content(checkpoint, content)
    assert pipeline._load_frozen_content(checkpoint)["source_pdf_uri"] == content["source_pdf_uri"]
    metadata = pipeline._source_reader_metadata(parent, "source-parent", "PARENT01", content)
    assert metadata["_source_context"]["attachment_key"] == "ATTACH02"


@pytest.mark.parametrize("item_type,media_type,expected", [
    ("attachment", "application/pdf", True),
    ("journalArticle", "application/pdf", False),
    ("attachment", "text/html", False),
])
def test_pdf_navigation_never_uses_parent_as_attachment(item_type, media_type, expected):
    target = {"key": "SOURCE01", "data": {"itemType": item_type}}
    result = pipeline._with_source_navigation({"media_type": media_type}, target)
    assert bool(result.get("source_pdf_uri")) is expected
    assert result["zotero_item_uri"] == "zotero://select/library/items/SOURCE01"


def test_annotation_extension_must_belong_to_selected_attachment():
    uri = "zotero://open-pdf/library/items/ATTACH01?page=2&annotation=ANNOT001"
    target = {"key": "ATTACH01", "data": {"itemType": "attachment", "source_pdf_uri": uri}}
    assert pipeline._with_source_navigation({"media_type": "application/pdf"}, target)["source_pdf_uri"] == uri
    reverse = uri.replace("?page=2&annotation=ANNOT001", "?annotation=ANNOT001&page=2")
    target["data"]["source_pdf_uri"] = reverse
    assert pipeline._with_source_navigation({"media_type": "application/pdf"}, target)["source_pdf_uri"] == reverse
    target["data"]["source_pdf_uri"] = uri.replace("ATTACH01", "OTHER001")
    assert pipeline._with_source_navigation({"media_type": "application/pdf"}, target)["source_pdf_uri"] == "zotero://open-pdf/library/items/ATTACH01"


def test_new_source_bundle_keeps_analysis_without_legacy_anchor_validation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("retired quantitative anchor validator invoked")
    monkeypatch.setattr(pipeline, "_validate_quantitative_provenance", forbidden)
    sections = {key: "The source explains participation and trust." for key in readers.REQUIRED_SECTION_KEYS}
    sections["evidence_and_data"] = "The study reports 12% higher trust (Table 2)."
    raw = {"analysis_sections": sections, "compact_profile": {"thesis": "Participation and trust"}, "literature_positions": []}
    result = readers._parse_source_bundle_response(raw, label="test", expected_identity={"source_id": "s1", "zotero_key": "SOURCE01"})
    bundle = pipeline._source_bundle_from_result(result, {"source_id": "s1", "zotero_item_key": "SOURCE01", "text": "The study reports 12% higher trust.", "media_type": "text/plain"}, "full_document")
    assert bundle is not None and bundle.bundle_schema_version == "2"
    assert bundle.analysis_sections == sections
    assert "evidence_anchors" not in bundle.to_dict()
    assert pipeline._ensure_source_result_contract(bundle.to_dict()) == bundle.to_dict()
    with pytest.raises(ValueError, match="does not match"):
        pipeline._source_bundle_from_result(result, {"source_id": "someone-else", "zotero_item_key": "SOURCE01"}, "full_document")


def test_unreadable_pdf_keeps_open_link_in_limited_result(tmp_path, monkeypatch):
    from auto_zettelkasten.extraction import ExtractionResult
    initialize(tmp_path)
    parent = {"key": "PARENT01", "data": {"key": "PARENT01", "itemType": "journalArticle", "title": "Study"}}
    child = {"key": "PDFKEY01", "data": {"key": "PDFKEY01", "itemType": "attachment", "parentItem": "PARENT01", "title": "Study", "contentType": "application/pdf"}}

    class Client:
        def children(self, key):
            return [child]

        def fulltext(self, key):
            return None

        def file(self, key):
            return (b"unreadable pdf", "application/pdf")

    monkeypatch.setattr(pipeline, "_custodied_pdf_candidate", lambda *args, **kwargs: (
        None, ExtractionResult(status="failed", route="pypdf_text", reason="unreadable", media_type="application/pdf")))
    content = pipeline._acquire_content(tmp_path, parent, Client(), {"attempts": [], "source_id": "s1", "zotero_item_key": "PARENT01"}, MapRequest(tmp_path, provider="deepseek", model="fake"), None)
    assert content["source_scope"] == "metadata_only"
    assert content["source_pdf_uri"] == "zotero://open-pdf/library/items/PDFKEY01"
