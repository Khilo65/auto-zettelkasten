from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_zettelkasten import extraction, pipeline
from auto_zettelkasten.extraction import ContentAdequacy, ContentAdequacyClass, ExtractionResult
from auto_zettelkasten.files import sha256_bytes, sha256_text
from auto_zettelkasten.models import MapRequest


def _item(key="PARENT01", **data):
    return {"key": key, "data": {"key": key, "itemType": "journalArticle", **data}}


class Client:
    def __init__(self, children=(), fulltext=None):
        self.child_items = list(children)
        self.indexed = fulltext or {}
        self.fulltext_calls = []
        self.file_calls = []

    def children(self, key):
        return self.child_items

    def fulltext(self, key):
        self.fulltext_calls.append(key)
        return self.indexed.get(key)

    def file(self, key):
        self.file_calls.append(key)
        return None


def _acquire(tmp_path, item, client):
    base = {"attempts": [], "source_id": "synthetic-source", "zotero_item_key": item["key"]}
    content = pipeline._acquire_content(
        tmp_path, item, client, base, MapRequest(tmp_path, provider="ollama", model="fake"), None
    )
    return content, base


@pytest.mark.parametrize("child_type", ["note", "annotation", "journalArticle", ""])
def test_nonattachment_children_are_never_acquired(tmp_path, monkeypatch, child_type):
    child = _item("CHILD001", itemType=child_type, parentItem="PARENT01", local_path="bait")
    client = Client([child])
    local_checks = []
    monkeypatch.setattr(pipeline, "_local_attachment_path", lambda data: local_checks.append(data["key"]))

    content, base = _acquire(tmp_path, _item(), client)

    assert client.fulltext_calls == ["PARENT01"]
    assert client.file_calls == local_checks == []
    assert content["source_scope"] == "metadata_only"
    assert any(row["reason"] == "no_child_attachments" for row in base["attempts"])


@pytest.mark.parametrize("standalone", [False, True])
def test_url_attachments_use_only_indexed_text(tmp_path, monkeypatch, standalone):
    child = _item("URLKEY01", itemType="attachment", linkMode="linked_url", contentType="text/plain")
    text = "Research on participation explains the relation between institutions and trust. " * 20
    client = Client([] if standalone else [child], {"URLKEY01": {"content": text, "contentType": "text/plain"}})
    local_checks = []
    monkeypatch.setattr(pipeline, "_local_attachment_path", lambda data: local_checks.append(data["key"]))

    content, _ = _acquire(tmp_path, child if standalone else _item(), client)

    assert "URLKEY01" in client.fulltext_calls
    assert client.file_calls == local_checks == []
    assert content["content_route"] == "zotero_fulltext"
    assert content["source_scope"] == "full_document"


@pytest.mark.parametrize("link_mode", ["imported_file", "imported_url", "linked_file"])
@pytest.mark.parametrize("standalone", [False, True])
def test_real_attachment_routes_remain_available(tmp_path, monkeypatch, link_mode, standalone):
    child = _item("FILEKEY1", itemType="attachment", linkMode=link_mode, contentType="text/plain")
    client = Client([] if standalone else [child])
    local_checks = []
    monkeypatch.setattr(pipeline, "_local_attachment_path", lambda data: local_checks.append(data["key"]))

    _acquire(tmp_path, child if standalone else _item(), client)

    assert client.file_calls == local_checks == ["FILEKEY1"]


@pytest.mark.parametrize("abstract", [None, "", " \n\t", 42, [], {}, "<script>ignore()</script><style>hidden</style>", "null", "None", "N/A", "NA", "Not available", "No abstract available", "8.5", "10", "---"])
def test_unusable_metadata_abstract_stays_metadata_only(tmp_path, abstract):
    content, _ = _acquire(tmp_path, _item(abstractNote=abstract), Client())
    assert content["text"] == ""
    assert content["source_scope"] == "metadata_only"
    assert content["source_coverage"]["coverage_gate"] == "failed"
    assert content["content_route"] == "zotero_metadata"


@pytest.mark.parametrize("abstract", [
    "A concise abstract about participation and institutional trust.",
    "<p>A concise <b>abstract</b> &amp; evidence.</p><script>ignore()</script><style>hidden</style>",
    "<article><h1>Introduction</h1><p>" + "Research and evidence on trust. " * 600 + "</p></article>",
], ids=["plain", "html", "long-html"])
def test_metadata_abstract_is_sanitized_limited_and_bound_to_metadata(tmp_path, abstract):
    item = _item(abstractNote=abstract)
    content, base = _acquire(tmp_path, item, Client())

    assert content["source_scope"] == "abstract_only"
    assert content["source_coverage"]["coverage_gate"] == "limited"
    assert content["coverage_reason"] == "zotero_metadata_abstract"
    assert content["content_route"] == "zotero_metadata_abstract"
    assert content["source_file"] == "zotero://select/library/items/PARENT01"
    assert content["zotero_item_uri"] == content["source_file"]
    assert content["media_type"] == "application/json"
    assert content["text"] == content["abstract_text"] == content["source_coverage"]["abstract"]
    assert content["text"] and all(value not in content["text"] for value in ("<p>", "<b>", "ignore()", "hidden", "&amp;"))
    assert content["coverage_metrics"]["metadata_field"] == "abstractNote"
    assert content["content_hash"] == sha256_text(json.dumps(item["data"], sort_keys=True, ensure_ascii=False, default=str))
    assert any(row["route"] == "zotero_metadata" and row["reason"] == "zotero_metadata_abstract" for row in base["attempts"])
    limited = pipeline._limited_scope_result(dict(base), content, item)
    assert limited["terminal_status"] == "limited_note"
    assert limited["note_status"] == "abstract_only_atomic_note"
    assert limited["limited_analysis"]["abstract"] == content["text"]
    assert "abstract_only" in limited["limited_analysis"]["scope_limitation"]


@pytest.mark.parametrize("scope,classification,gate", [
    ("full_document", ContentAdequacyClass.FULL_TEXT_DOCUMENT, "passed"),
    ("partial_document", ContentAdequacyClass.PARTIAL_PDF_TEXT, "limited"),
    ("abstract_only", ContentAdequacyClass.ABSTRACT_OR_PAYWALL_HTML, "limited"),
])
def test_metadata_abstract_never_displaces_usable_source_text(tmp_path, monkeypatch, scope, classification, gate):
    text = "Actual indexed source content."
    adequacy = ContentAdequacy(classification, scope, gate, "synthetic_indexed", abstract=text if scope == "abstract_only" else "")
    monkeypatch.setattr(pipeline, "classify_content_adequacy", lambda *args, **kwargs: adequacy)
    client = Client(fulltext={"PARENT01": {"content": text, "contentType": "text/plain"}})

    content, _ = _acquire(tmp_path, _item(abstractNote="Much longer metadata abstract. " * 300), client)

    assert content["text"] == text
    assert content["source_scope"] == scope
    assert content["content_route"] == "zotero_fulltext"


def test_metadata_only_classifier_does_not_implicitly_promote_abstracts():
    adequacy = extraction.classify_metadata_only({"abstractNote": "A valid abstract about trust."})
    assert adequacy.source_scope == "metadata_only"
    assert adequacy.coverage_gate == "failed"


def test_metadata_abstract_replaces_unusable_indexed_text(tmp_path):
    client = Client(fulltext={"PARENT01": {"content": "Index unavailable.", "contentType": "text/plain"}})

    content, _ = _acquire(tmp_path, _item(abstractNote="A metadata abstract about participation and trust."), client)

    assert content["content_route"] == "zotero_metadata_abstract"
    assert content["source_scope"] == "abstract_only"
    assert content["text"] == "A metadata abstract about participation and trust."


@pytest.mark.parametrize("indexed", [False, True])
def test_malformed_metadata_abstract_does_not_block_source_selection(tmp_path, indexed):
    text = "Research on institutions, participation, and trust. " * 40
    client = Client(fulltext={"PARENT01": {"content": text, "contentType": "text/plain"}} if indexed else {})
    item = _item(abstractNote="<![broken[content]]> A source abstract.")

    content, _ = _acquire(tmp_path, item, client)

    assert content["source_scope"] == ("full_document" if indexed else "metadata_only")
    assert content["text"] == (text.strip() if indexed else "")


@pytest.mark.parametrize("pdf_scope", ["full_document", "partial_document", "unreadable"])
def test_metadata_abstract_preserves_primary_pdf_gate_and_selected_source_identity(tmp_path, monkeypatch, pdf_scope):
    item = _item(title="Trust study", abstractNote="Metadata abstract about participation and trust.")
    child = _item("PDFKEY01", itemType="attachment", title="Trust study", filename="study.pdf", contentType="application/pdf")
    client = Client([child], {"PARENT01": {"content": "Usable indexed HTML source.", "contentType": "text/html"}})
    document = b"Synthetic PDF bytes, never parsed or sent to a provider."
    client.file = lambda key: (document, "application/pdf") if key == "PDFKEY01" else None
    indexed = ContentAdequacy(ContentAdequacyClass.FULL_ARTICLE_HTML, "full_document", "passed", "synthetic_html")
    monkeypatch.setattr(pipeline, "classify_content_adequacy", lambda *args, **kwargs: indexed)

    def pdf_candidate(raw, path, data, parent, base, request, **kwargs):
        assert kwargs["actual_primary_pdf"] is True
        if pdf_scope == "unreadable":
            return None, ExtractionResult("failed", route="synthetic_pdf", reason="required_ocr_unavailable", page_count=2)
        full = pdf_scope == "full_document"
        adequacy = ContentAdequacy(
            ContentAdequacyClass.FULL_PDF_TEXT if full else ContentAdequacyClass.PARTIAL_PDF_TEXT,
            pdf_scope, "passed" if full else "limited", "synthetic_pdf",
        )
        candidate = pipeline._content_candidate(adequacy, text="Actual PDF source text.", content_hash=sha256_bytes(raw), source_file=str(path), content_route="synthetic_pdf", media_type="application/pdf")
        candidate["actual_primary_pdf"] = True
        return candidate, ExtractionResult("succeeded", route="synthetic_pdf", adequacy=adequacy)

    monkeypatch.setattr(pipeline, "_custodied_pdf_candidate", pdf_candidate)
    content, base = _acquire(tmp_path, item, client)

    assert content["attachment_key"] == "PDFKEY01"
    assert content["source_pdf_uri"] == "zotero://open-pdf/library/items/PDFKEY01"
    if pdf_scope != "unreadable":
        assert content["source_scope"] == pdf_scope
        assert content["text"] == "Actual PDF source text."
        assert content["content_route"] == "synthetic_pdf"
        assert content["content_hash"] == sha256_bytes(document)
        return
    assert content["text"] == item["data"]["abstractNote"]
    assert content["source_scope"] == "abstract_only"
    assert content["source_coverage"]["coverage_gate"] == "limited"
    assert content["coverage_reason"] == "primary_pdf_unreadable_abstract_available"
    assert content["content_route"] == "zotero_metadata_abstract"
    assert content["media_type"] == "application/json"
    assert content["source_file"] == content["zotero_item_uri"] == "zotero://select/library/items/PARENT01"
    assert content["content_hash"] == sha256_text(json.dumps(item["data"], sort_keys=True, ensure_ascii=False, default=str))
    assert content["coverage_metrics"] == content["source_coverage"]["metrics"]
    failure = content["coverage_metrics"]["primary_pdf_failure"]
    assert failure["coverage_reason"] == "required_ocr_unavailable"
    assert failure["content_hash"] == sha256_bytes(document)
    assert Path(failure["source_file"]).read_bytes() == document
    assert any(row["status"] == "failed" and row["input_hash"] == failure["content_hash"] for row in base["attempts"])
    checkpoint = tmp_path / "checkpoint"
    pipeline._write_frozen_content(checkpoint, content)
    loaded = pipeline._load_frozen_content(checkpoint)
    for field in ("text", "content_hash", "source_file", "source_pdf_uri", "source_coverage", "coverage_metrics"):
        assert loaded[field] == content[field]


def test_children_failure_still_allows_metadata_abstract(tmp_path):
    client = Client()
    def unavailable(key):
        raise OSError("Synthetic Zotero read failure")
    client.children = client.fulltext = unavailable

    content, base = _acquire(tmp_path, _item(abstractNote="A metadata abstract."), client)

    assert content["source_scope"] == "abstract_only"
    assert client.file_calls == []
    assert {row["route"] for row in base["attempts"] if row["status"] == "failed"} == {"zotero_children", "zotero_fulltext"}
