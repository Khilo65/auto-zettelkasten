import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest

from auto_zettelkasten.documents import DocumentConflict, DocumentService
from auto_zettelkasten.zotero_annotations import AnnotationConflict
from auto_zettelkasten.zotero_workspace import VerifiedPDF, ZoteroWorkspaceError, ZoteroWriteUncertain
from auto_zettelkasten.zotero_workspace_service import ZoteroWorkspaceService, read_annotation_snapshot

HEADER = "---\nnote_id: note-one\nsource_id: source-one\nzotero_item_key: PARENTAA\nzotero_item_uri: zotero://select/library/items/PARENTAA\nattachment_key: PDFKEYAA\ntitle: Original\n---\n"


def record(key, **fields):
    version = fields.pop("version", 1)
    return {"key": key, "version": version, "data": {"key": key, "version": version, **fields}}


def draft(key="ANNAT222", version=0):
    return {"id": key, "key": key, "version": version, "scope": "users/0", "attachment_key": "PDFKEYAA",
        "parentItem": "PDFKEYAA", "type": "highlight", "position": {"pageIndex": 0, "rects": [[1, 2, 10, 12]]},
        "pageLabel": "458", "sortIndex": "00000|000000|00000", "text": "A synthetic finding", "comment": "A personal interpretation", "color": "#ffd400", "tags": []}


class Client:
    def __init__(self, root):
        self.server_id = "SERVER000001"
        self.pdf = root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nSynthetic fixture\n")
        self.records = {"PARENTAA": record("PARENTAA", itemType="journalArticle", title="Source"),
            "PDFKEYAA": record("PDFKEYAA", itemType="attachment", title="PDF title", parentItem="PARENTAA", contentType="application/pdf")}
        self.writes = 0
        self.linked = {}
        self.fail = None

    def status(self):
        return {"available": True, "authorized": True, "write_supported": True, "server_id": self.server_id}

    def libraries(self):
        return [{"scope": "users/0", "name": "My Library"}]

    def page(self, rows, start=0, limit=32, **kwargs):
        rows = copy.deepcopy(rows)
        return {"records": rows[start:start + limit], "start": start, "limit": limit, "total": len(rows),
            "next_start": start + limit if start + limit < len(rows) else None, "library_version": 1, "server_id": self.server_id}

    def items_page(self, scope, **kwargs):
        return self.page([self.records["PARENTAA"]], **kwargs)

    def collections(self, scope, **kwargs):
        return self.page([], **kwargs)

    def children(self, scope, key, **kwargs):
        return self.page([row for row in self.records.values() if row["data"].get("parentItem") == key], **kwargs)

    def item(self, scope, key):
        return copy.deepcopy(self.records.get(key))

    def pdf_file(self, scope, key):
        assert key == "PDFKEYAA"
        info = self.pdf.stat()
        return VerifiedPDF(self.pdf, key, "PARENTAA", scope, self.server_id, 1, info.st_size, info.st_mtime_ns)

    def annotations(self, scope, key):
        return copy.deepcopy([row for row in self.records.values() if row["data"].get("itemType") == "annotation" and row["data"]["parentItem"] == key])

    def save_annotation(self, scope, attachment_key, annotation, *, key, expected_version, write_token, known_internal=False):
        import re
        assert re.fullmatch(r"[0-9]{5}\|[0-9]{6}\|[0-9]{5}", annotation.get("annotationSortIndex", ""))
        self.writes += 1
        saved = copy.deepcopy(annotation)
        saved["version"] = expected_version + 1
        self.records[key] = {"key": key, "version": saved["version"], "data": saved}
        if self.fail == "lost_ack":
            raise ZoteroWriteUncertain()
        if self.fail == "not_saved":
            del self.records[key]
            raise ZoteroWriteUncertain()
        return copy.deepcopy(self.records[key])

    def delete_annotation(self, scope, attachment_key, key, **kwargs):
        self.writes += 1
        if self.fail == "delete_not_saved":
            raise ZoteroWriteUncertain()
        self.records.pop(key)
        return {"deleted": True, "key": key}

    def find_linked_note(self, scope, parent_key, path):
        return self.linked.get(str(path))

    def register_linked_note(self, scope, parent_key, path, *, key, title, write_token):
        self.writes += 1
        self.linked[str(path)] = record(key, itemType="attachment", parentItem=parent_key, path=str(path), title=title)
        return self.linked[str(path)]


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    (root / "Note.md").write_text(HEADER + "Original body.\n")
    docs = DocumentService(journal_root=tmp_path / "operations", override_root=tmp_path / "overrides", live_root=tmp_path / "live")
    ws = docs.open(root)
    client = Client(tmp_path)
    service = ZoteroWorkspaceService(docs, tmp_path / "private", client=client)
    yield root, docs, ws, client, service
    docs.close()


def migrate(setup):
    _, docs, ws, _, service = setup
    current = docs.read(ws["id"], "Note.md")
    data = {"workspace_id": ws["id"], "document_id": current["id"], "note_path": "Note.md", "revision": current["revision"],
        "scope": "users/0", "item_key": "PARENTAA", "attachment_key": "PDFKEYAA"}
    preview = service.handle("migration-preview", data)
    return service.handle("migrate", {**data, "preview_identity": preview["preview_identity"], "promote": True})


def save(setup, *, operation="save-first", annotation=None):
    service = setup[-1]
    return service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": [annotation or draft()], "operation_id": operation})


def project(setup, current=None):
    _, docs, ws, _, service = setup
    current = current or docs.read(ws["id"], "Note.md")
    return service.handle("annotation-project", {"workspace_id": ws["id"], "document_id": current["id"], "note_path": "Note.md", "revision": current["revision"],
        "scope": "users/0", "attachment_key": "PDFKEYAA"})


def test_readonly_defaults_and_flat_library_navigation(setup):
    _, _, ws, client, service = setup
    assert service.handle("status", {})["connected"] is True
    assert service.handle("status", {})["writable"] is False
    assert service.handle("libraries", {})["libraries"][0]["writable"] is False
    page = service.handle("items", {"workspace_id": ws["id"]})
    assert page["records"][0]["note_path"] == "Note.md"
    assert page["total"] == 1
    for action in ("authorize", "item-update", "collection-create", "collection-rename", "annotation-save", "annotation-delete", "migrate"):
        with pytest.raises(ValueError, match="disabled"):
            service.handle(action, {})
    assert client.writes == 0


def test_exact_source_links_and_missing_annotation_fallback(setup):
    _, _, ws, _, service = setup
    result = service.handle("open-source", {"uri": "zotero://open-pdf/library/items/PDFKEYAA?page=1&annotation=ANNAT222", "workspace_id": ws["id"]})
    reader = result["reader"]
    assert reader["note_path"] == "Note.md"
    assert reader["item_key"] == "PARENTAA"
    assert reader["location"] == {"pageIndex": 0}
    assert "unavailable" in reader["warning"]
    assert reader["readOnly"]
    reopened = service.handle("open-pdf", {"attachment_key": "PDFKEYAA", "workspace_id": ws["id"]})["reader"]
    assert reopened["id"] == reader["id"] and reopened["url"] != reader["url"]
    assert service.handle("open-source", {"uri": "zotero://select/library/items/PARENTAA", "workspace_id": ws["id"]})["reader"]["attachment_key"] == "PDFKEYAA"


def test_duplicate_source_notes_never_guess_and_group_scope_does_not_collide(setup):
    root, _, ws, _, service = setup
    (root / "Copy.md").write_text(HEADER + "Copy body\n")
    result = service.handle("open-pdf", {"workspace_id": ws["id"], "attachment_key": "PDFKEYAA"})
    assert "note_path" not in result["reader"]
    assert len(result["note_candidates"]) == 2
    assert service._notes(ws["id"], "groups/42", "PARENTAA") == []
    with pytest.raises(ValueError, match="not bound"):
        service.handle("open-pdf", {"workspace_id": ws["id"], "attachment_key": "PDFKEYAA", "note_path": "Unknown.md"})


def test_pdf_capabilities_expire_revoke_bound_and_detect_replacement(setup, monkeypatch):
    _, _, _, client, service = setup
    reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    token = reader["url"].split("/")[-2]
    with client.pdf.open("rb") as stream:
        assert service.validate_pdf_stream(token, os.fstat(stream.fileno())).attachment_key == "PDFKEYAA"
    alternate = client.pdf.with_name("alternate.pdf")
    alternate.write_bytes(client.pdf.read_bytes())
    with alternate.open("rb") as stream, pytest.raises(ValueError, match="changed"):
        service.validate_pdf_stream(token, os.fstat(stream.fileno()))
    with pytest.raises(ValueError):
        service.pdf_capability(token)
    fresh = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    fresh_token = fresh["url"].split("/")[-2]
    service._pdfs[fresh_token]["expires"] = 0
    with pytest.raises(ValueError, match="expired"):
        service.pdf_capability(fresh_token)
    last = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    service.handle("reader-close", {"reader_id": last["id"]})
    assert not service._pdfs
    alternate.replace(client.pdf)
    replaced = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    assert replaced["readOnly"] and "changed" in replaced["warning"]


def test_pdf_capability_count_and_server_change(setup):
    _, _, _, client, service = setup
    first = None
    for _ in range(34):
        reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
        first = first or reader["url"].split("/")[-2]
    assert len(service._pdfs) == 32 and first not in service._pdfs
    client.server_id = "SERVER000002"
    with pytest.raises(ValueError, match="changed"):
        service.pdf_capability(reader["url"].split("/")[-2])


def test_annotation_refresh_renews_only_live_exact_pdf_leases(setup, monkeypatch):
    from dataclasses import replace

    service = setup[-1]
    clock = [100.0]
    monkeypatch.setattr("auto_zettelkasten.zotero_workspace_service.time.monotonic", lambda: clock[0])
    reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    token = reader["url"].split("/")[-2]
    original = service._pdfs[token]["expires"]
    group = service.handle("open-pdf", {"scope": "groups/42", "attachment_key": "PDFKEYAA"})["reader"]
    group_token = group["url"].split("/")[-2]
    other_token = "A" * 32
    service._pdfs[other_token] = {**service._pdfs[token], "pdf": replace(service._pdfs[token]["pdf"], attachment_key="ATHERA22")}

    clock[0] = original - 10
    service.handle("annotation-refresh", {"scope": "users/0", "attachment_key": "PDFKEYAA"})
    assert service._pdfs[token]["expires"] == clock[0] + service.PDF_TTL
    assert service._pdfs[group_token]["expires"] == original
    assert service._pdfs[other_token]["expires"] == original
    clock[0] = original + 1
    assert service.pdf_capability(token).attachment_key == "PDFKEYAA"
    with pytest.raises(ValueError, match="expired"):
        service.pdf_capability(group_token)


def test_annotation_refresh_cannot_revive_expired_or_revoked_pdf_leases(setup, monkeypatch):
    service = setup[-1]
    clock = [100.0]
    monkeypatch.setattr("auto_zettelkasten.zotero_workspace_service.time.monotonic", lambda: clock[0])
    first = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["url"].split("/")[-2]
    second = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["url"].split("/")[-2]
    service.revoke_pdf(second)
    clock[0] += service.PDF_TTL
    service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert first not in service._pdfs and second not in service._pdfs


@pytest.mark.parametrize("changed", ["server", "file"])
def test_annotation_refresh_never_renews_changed_pdf_identity(setup, monkeypatch, changed):
    _, _, _, client, service = setup
    clock = [100.0]
    monkeypatch.setattr("auto_zettelkasten.zotero_workspace_service.time.monotonic", lambda: clock[0])
    token = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["url"].split("/")[-2]
    original = service._pdfs[token]["expires"]
    clock[0] += 100
    if changed == "server":
        client.server_id = "SERVER000002"
    else:
        info = client.pdf.stat()
        replacement = client.pdf.with_name("replacement.pdf")
        replacement.write_bytes(client.pdf.read_bytes())
        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
        replacement.replace(client.pdf)
    service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert token not in service._pdfs or service._pdfs[token]["expires"] == original
    with pytest.raises(ValueError):
        service.pdf_capability(token)


def test_failed_refresh_or_reconciliation_cannot_extend_pdf_lease(setup, monkeypatch):
    _, _, _, client, service = setup
    clock = [100.0]
    monkeypatch.setattr("auto_zettelkasten.zotero_workspace_service.time.monotonic", lambda: clock[0])
    token = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["url"].split("/")[-2]
    original = service._pdfs[token]["expires"]
    clock[0] += 100
    service.handle("annotation-reconcile", {"attachment_key": "PDFKEYAA", "request_ids": ["unknown-request"]})
    assert service._pdfs[token]["expires"] == original
    def unavailable(*args, **kwargs):
        raise ValueError("Annotations unavailable")
    monkeypatch.setattr(client, "annotations", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert service._pdfs[token]["expires"] == original


def test_external_open_accepts_only_strict_source_uri(setup, monkeypatch):
    service = setup[-1]
    opened = []
    monkeypatch.setattr("auto_zettelkasten.zotero_workspace_service.webbrowser.open", lambda uri: opened.append(uri) or True)
    for uri in ("file:///etc/passwd", "https://example.invalid", "zotero://debug/anything", "zotero://open-pdf/library/items/PDFKEYAA?page=0"):
        with pytest.raises(ValueError):
            service.handle("open-external", {"uri": uri})
    assert not opened
    service.handle("open-external", {"uri": "zotero://select/library/items/PARENTAA"})
    assert opened == ["zotero://select/library/items/PARENTAA"]


def test_migration_requires_preview_promotion_and_preserves_overrides(setup):
    root, docs, ws, client, service = setup
    current = docs.read(ws["id"], "Note.md")
    current = docs.save_override(ws["id"], current["id"], current["revision"], HEADER + "Personal edit.\n")
    docs.save_personal_metadata(ws["id"], current["id"], current["revision"], {"display_title": "Personal title", "personal_properties": {"tags": ["mine"]}})
    service.writes_enabled = True
    with pytest.raises(ValueError, match="promote"):
        service.handle("migrate", {})
    with pytest.raises(ValueError, match="preview"):
        service.handle("migrate", {"promote": True})
    migrated = migrate(setup)
    assert migrated["live_note"] and migrated["editable"]
    assert "Personal edit" in (root / "Note.md").read_text()
    assert migrated["display_title"] == "Personal title"
    assert client.writes == 1 and len(client.linked) == 1
    assert service.handle("migration-status", {"workspace_id": ws["id"], "note_path": "Note.md"})["live_note_phase"] == "archived"


def test_annotation_round_trip_replays_and_serializes_without_duplicate_writes(setup):
    service, client = setup[-1], setup[-2]
    service.writes_enabled = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(setup), range(2)))
    assert client.writes == 1
    assert results[0] == results[1]
    saved = results[0]["annotations"][0]
    assert saved["version"] == 1 and saved["readOnly"] is False
    second = save(setup, operation="save-second", annotation={**saved, "comment": "Updated"})
    assert second["annotations"][0]["comment"] == "Updated" and client.writes == 2
    with pytest.raises(ValueError, match="different request"):
        save(setup, annotation=draft("ANNAT223"))
    removed = service.handle("annotation-delete", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": [{"id": "ANNAT222", "version": 2}], "operation_id": "delete-first"})
    assert removed["deleted"] == ["ANNAT222"] and "ANNAT222" not in client.records


def test_unknown_outcome_is_reconciled_from_remote_without_retry(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    saved = save(setup)
    pending = saved["pending"][0]
    assert pending["state"] == "uncertain" and client.writes == 1
    repeated = save(setup)
    assert repeated == saved and client.writes == 1
    result = service.handle("annotation-reconcile", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_ids": [pending["id"]]})
    assert result["resolved"] is True and not result["pending"] and client.writes == 1
    assert result["annotations"][0]["readOnly"] is False


def test_unsaved_remote_outcome_stays_unresolved_no_automatic_retry(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    saved = save(setup)
    result = service.handle("annotation-reconcile", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_ids": [saved["pending"][0]["id"]]})
    assert result["resolved"] is False and client.writes == 1
    assert result["pending"][0]["state"] == "queued"
    with pytest.raises(ValueError, match="do not belong"):
        service.handle("annotation-reconcile", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_ids": ["0" * 64]})


def test_invalid_batch_is_rejected_before_any_annotation_write(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    for bad in [{**draft("ANNAT223"), "attachment_key": "ATHERA22"}, {**draft("ANNAT223"), "version": -1}, draft()]:
        with pytest.raises(ValueError):
            service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": [draft(), bad], "operation_id": "bad-batch"})
    assert client.writes == 0


def test_projection_requires_active_note_and_revision_preserves_human_text_and_receipt(setup):
    root, docs, ws, _, service = setup
    with pytest.raises(ValueError, match="active shared"):
        project(setup)
    service.writes_enabled = True
    migrated = migrate(setup)
    save(setup)
    result = project(setup)
    assert "Original body." in result["document"]["text"]
    assert "Source quotation:" in result["document"]["text"] and "p. 458" in result["document"]["text"]
    assert "page=1&annotation=ANNAT222" in result["document"]["text"]
    receipt = service.annotation_snapshot(root, "Note.md")
    assert receipt["block_hash"] == result["block_hash"]
    assert read_annotation_snapshot(service.private_root, root, "Note.md") == receipt
    with pytest.raises(DocumentConflict):
        project(setup, migrated)
    assert service.annotation_snapshot(root, "Note.md") == receipt
    current = docs.read(ws["id"], "Note.md")
    docs.save(ws["id"], current["id"], current["revision"], current["text"].replace("A synthetic finding", "Hand edited managed block"))
    assert service.annotation_snapshot(root, "Note.md") is None
    with pytest.raises(AnnotationConflict, match="edited"):
        project(setup)


def test_dirty_projection_is_deferred_without_writing(setup):
    root, _, _, _, service = setup
    before = (root / "Note.md").read_bytes()
    assert service.handle("annotation-project", {"dirty": True})["deferred"]
    assert (root / "Note.md").read_bytes() == before


def test_library_page_builds_note_lookup_once(setup, monkeypatch):
    _, docs, ws, client, service = setup
    original = docs.recall_corpus
    calls = []
    monkeypatch.setattr(docs, "recall_corpus", lambda workspace: calls.append(workspace) or original(workspace))
    monkeypatch.setattr(client, "items_page", lambda scope, **kwargs: client.page([client.records["PARENTAA"]] * 32, **kwargs))
    page = service.handle("items", {"workspace_id": ws["id"], "limit": 32})
    assert len(page["records"]) == 32 and len(calls) == 1
    assert all(row["scope"] == "users/0" and row["note_path"] == "Note.md" for row in page["records"])


def test_projection_crash_after_document_save_recovers_trusted_receipt(setup, monkeypatch):
    import auto_zettelkasten.zotero_workspace_service as module
    root, _, _, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    save(setup)
    original = module._write_json
    def interrupted(path, value):
        if "projections" in path.parts and value.get("state") == "confirmed":
            raise OSError("crash after note save")
        return original(path, value)
    monkeypatch.setattr(module, "_write_json", interrupted)
    with pytest.raises(OSError, match="crash"):
        project(setup)
    snapshot = service.annotation_snapshot(root, "Note.md")
    assert snapshot and snapshot["groups"][0]["annotations"][0]["text"] == "A synthetic finding"
    assert "Source quotation:" in (root / "Note.md").read_text()
    monkeypatch.setattr(module, "_write_json", original)
    assert project(setup)["annotation_count"] == 1


def test_failed_projection_preserves_previous_receipt(setup, monkeypatch):
    root, docs, _, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    saved = save(setup)["annotations"][0]
    first = project(setup)
    before = service.annotation_snapshot(root, "Note.md")
    save(setup, operation="save-second", annotation={**saved, "comment": "New comment"})
    original = docs.save
    monkeypatch.setattr(docs, "save", lambda *args: (_ for _ in ()).throw(DocumentConflict(first["document"])))
    with pytest.raises(DocumentConflict):
        project(setup)
    assert service.annotation_snapshot(root, "Note.md") == before
    monkeypatch.setattr(docs, "save", original)
    assert "New comment" in project(setup)["document"]["text"]


def test_snapshot_cannot_cross_workspace_copies_or_follow_note_symlink(setup, tmp_path):
    root, _, _, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    save(setup)
    project(setup)
    other = tmp_path / "copy"
    other.mkdir()
    (other / "Note.md").write_bytes((root / "Note.md").read_bytes())
    assert service.annotation_snapshot(other, "Note.md") is None
    original = root / "Original.md"
    (root / "Note.md").rename(original)
    (root / "Note.md").symlink_to(original)
    assert service.annotation_snapshot(root, "Note.md") is None


def test_annotation_delete_rejects_mismatched_explicit_scope(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    save(setup)
    with pytest.raises(ValueError, match="identity"):
        service.handle("annotation-delete", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": [
            {"id": "ANNAT222", "version": 1, "scope": "groups/42"}], "operation_id": "wrong-delete"})
    assert client.writes == 1 and "ANNAT222" in client.records


def test_verified_writer_navigation_corrects_historical_attachment_select_uri(setup, monkeypatch):
    _, docs, ws, client, service = setup
    navigation = {"verified": True, "basis": "writer_metadata", "zotero_item_key": "PARENTAA",
        "zotero_item_uri": "zotero://select/library/items/PDFKEYAA", "attachment_key": "PDFKEYAA",
        "source_pdf_uri": "zotero://open-pdf/library/items/PDFKEYAA"}
    corpus = {"documents": [{"path": "Note.md", "frontmatter": {"note_id": "note-one"}, "source_navigation": navigation}]}
    monkeypatch.setattr(docs, "recall_corpus", lambda _: corpus)
    assert service._notes(ws["id"], "users/0", "PARENTAA")[0]["path"] == "Note.md"
    assert service._notes(ws["id"], "groups/42", "PARENTAA") == []
    service._source_parents.clear()
    client.records["PDFKEYAA"]["data"]["parentItem"] = "ATHERA22"
    assert service._notes(ws["id"], "users/0", "PARENTAA") == []
    navigation["verified"] = False
    client.records["PDFKEYAA"]["data"]["parentItem"] = "PARENTAA"
    assert service._notes(ws["id"], "users/0", "PARENTAA") == []


def test_pdf_uri_supplies_scope_but_titles_and_bare_keys_do_not(setup, monkeypatch):
    _, docs, ws, _, service = setup
    navigation = {"verified": True, "basis": "writer_metadata", "zotero_item_key": "PARENTAA", "attachment_key": "PDFKEYAA",
        "source_pdf_uri": "zotero://open-pdf/library/items/PDFKEYAA"}
    corpus = {"documents": [{"path": "Note.md", "frontmatter": {}, "source_navigation": navigation}]}
    monkeypatch.setattr(docs, "recall_corpus", lambda _: corpus)
    assert service._notes(ws["id"], "users/0", "PARENTAA")[0]["path"] == "Note.md"
    del navigation["source_pdf_uri"]
    navigation["title"] = "Source"
    assert service._notes(ws["id"], "users/0", "PARENTAA") == []


def test_real_sidecar_shape_requires_explicit_scope_then_can_pair_and_migrate(setup):
    from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION
    root, docs, ws, client, service = setup
    (root / "Note.md").write_text("---\nnote_id: note-one\ntitle: Source\n---\nOriginal body.\n")
    metadata = root / "11_state" / "note_metadata"
    metadata.mkdir(parents=True)
    (metadata / "note-one.yml").write_text(json.dumps({"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION,
        "note_path": "Note.md", "frontmatter": {"note_id": "note-one", "source_id": "source-one", "zotero_item_key": "PARENTAA"}}))
    document = docs.read(ws["id"], "Note.md")
    assert document["source_navigation"]["verified"] is True
    page = service.handle("items", {"workspace_id": ws["id"]})
    assert "note_path" not in page["records"][0]
    assert page["records"][0]["unbound_note_candidates"][0]["path"] == "Note.md"
    data = {"workspace_id": ws["id"], "note_path": "Note.md", "item_key": "PARENTAA", "scope": "users/0"}
    with pytest.raises(ValueError):
        service.handle("open-source", data)
    result = service.handle("open-source", {**data, "confirm_scope": True})
    assert result["reader"]["note_path"] == "Note.md"
    assert client.writes == 0
    service.writes_enabled = True
    migrated = migrate(setup)
    assert migrated["live_note"] and '"zotero_item_key": "PARENTAA"' in migrated["text"]
    # A different session does not inherit an uncommitted, guessed scope.
    detached = ZoteroWorkspaceService(docs, service.private_root, client=client)
    # Migrated notes have a durable binding; un-migrated copies would not.
    assert migrated["live_binding"]["library_id"] == "0"
    assert detached._notes(ws["id"], "users/0", "PARENTAA")[0]["path"] == "Note.md"


def test_scope_confirmation_cannot_bind_a_different_sidecar_item(setup, monkeypatch):
    _, docs, ws, _, service = setup
    original = docs.read
    def read(workspace, path):
        return {**original(workspace, path), "source_navigation": {"verified": True, "basis": "writer_metadata", "zotero_item_key": "ATHERA22"}}
    monkeypatch.setattr(docs, "read", read)
    with pytest.raises(ValueError, match="verified source key"):
        service.handle("open-source", {"workspace_id": ws["id"], "note_path": "Note.md", "item_key": "PARENTAA", "scope": "users/0", "confirm_scope": True})


def recover(service, action, request_id="save-first", **extra):
    return service.handle(action, {"scope": "users/0", "attachment_key": "PDFKEYAA", "request_ids": [request_id], **extra})


def test_host_request_identity_resolves_private_outbox_operations(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    request_id = "5ed669b4-794b-4b1c-b95f-8df99491ecc9-7"
    save(setup, operation=request_id)
    result = recover(service, "annotation-reconcile", request_id)
    assert result["resolved"] and result["unresolved_request_ids"] == [] and client.writes == 1
    assert result["annotations"][0]["id"] == "ANNAT222"
    assert recover(service, "annotation-retry", request_id)["retried"] == 0
    assert client.writes == 1


def test_unknown_or_mismatched_request_never_becomes_resolved(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    unknown = recover(service, "annotation-reconcile", "unknown-request")
    assert unknown["resolved"] is False and unknown["unresolved_request_ids"] == ["unknown-request"]
    client.fail = "not_saved"
    save(setup)
    wrong_scope = service.handle("annotation-reconcile", {"scope": "groups/42", "attachment_key": "PDFKEYAA", "request_ids": ["save-first"]})
    assert wrong_scope["resolved"] is False and wrong_scope["unresolved_request_ids"] == ["save-first"]
    assert recover(service, "annotation-retry", "unknown-request")["retried"] == 0
    assert client.writes == 1


def test_explicit_retry_sends_only_original_queued_batch_once(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    rows = [draft(key) for key in ("ANNAT222", "ANNAT223", "ANNAT224")]
    service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": rows, "operation_id": "batch-host-request"})
    assert client.writes == 1
    client.fail = None
    result = recover(service, "annotation-retry", "batch-host-request")
    assert result["resolved"] is True and result["retried"] == 2 and client.writes == 3
    assert {row["id"] for row in result["annotations"]} == {row["id"] for row in rows}
    assert not result["pending"]
    assert recover(service, "annotation-retry", "batch-host-request")["retried"] == 0
    assert client.writes == 3


def test_retry_rechecks_original_version_and_never_overwrites_new_remote_work(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    recover(service, "annotation-reconcile")
    # A different writer occupies the originally absent key after readback.
    client.records["ANNAT222"] = record("ANNAT222", version=9, itemType="annotation", parentItem="PDFKEYAA", annotationType="highlight",
        annotationPosition=json.dumps({"pageIndex": 0, "rects": [[1, 2, 10, 12]]}), annotationSortIndex="00000|000000|00000",
        annotationColor="#ffd400", annotationText="Another writer's annotation", annotationComment="", tags=[])
    client.fail = None
    result = recover(service, "annotation-retry")
    assert result["resolved"] is False and result["retry_blocked"] and result["retried"] == 0
    assert client.writes == 1 and client.records["ANNAT222"]["version"] == 9


def test_retry_and_discard_keep_unknown_outcomes_when_readback_fails(setup, monkeypatch):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    monkeypatch.setattr(client, "item", lambda *args: (_ for _ in ()).throw(ZoteroWorkspaceError("Unavailable", code="unavailable")))
    for action in ("annotation-retry", "annotation-discard"):
        with pytest.raises(ZoteroWorkspaceError):
            recover(service, action, discard=True)
    assert service._queue().lookup("users/0", "PDFKEYAA", "ANNAT222")["state"] == "uncertain"
    assert client.writes == 1


def test_explicit_discard_preserves_audit_and_allows_a_fresh_edit(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    with pytest.raises(ValueError, match="Explicitly"):
        recover(service, "annotation-discard")
    result = recover(service, "annotation-discard", discard=True)
    assert result["resolved"] and result["discarded"]
    row = service._queue().lookup("users/0", "PDFKEYAA", "ANNAT222")
    assert row["state"] == "discarded" and row["payload"]["annotationText"] == "A synthetic finding"
    assert "discarded" in row["events"] and client.writes == 1
    client.fail = None
    saved = save(setup, operation="fresh-after-discard")
    assert len(saved["annotations"]) == 1 and client.writes == 2
    assert recover(service, "annotation-reconcile")["resolved"] is True


def test_explicit_retry_recovers_interrupted_admission_without_losing_drafts(setup, monkeypatch):
    from auto_zettelkasten.zotero_annotations import PendingAnnotations
    client, service = setup[-2:]
    service.writes_enabled = True
    original = PendingAnnotations.enqueue
    def failed(*args, **kwargs):
        raise OSError("interrupted queue admission")
    monkeypatch.setattr(PendingAnnotations, "enqueue", failed)
    with pytest.raises(OSError, match="interrupted"):
        save(setup)
    assert client.writes == 0
    check = recover(service, "annotation-reconcile")
    assert check["resolved"] is False and check["needs_admission_request_ids"] == ["save-first"]
    monkeypatch.setattr(PendingAnnotations, "enqueue", original)
    result = recover(service, "annotation-retry")
    assert result["resolved"] and result["retried"] == 1 and client.writes == 1


def test_internal_annotation_delete_can_retry_unchanged_version_after_readback(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    save(setup)
    client.fail = "delete_not_saved"
    service.handle("annotation-delete", {"scope": "users/0", "attachment_key": "PDFKEYAA", "annotations": [{"id": "ANNAT222", "version": 1}], "operation_id": "delete-retry"})
    assert client.writes == 2
    client.fail = None
    result = recover(service, "annotation-retry", "delete-retry")
    assert result["resolved"] and result["retried"] == 1 and client.writes == 3
    assert "ANNAT222" not in client.records


def test_explicit_discard_recovers_a_partly_queued_batch_without_dispatch(setup, monkeypatch):
    from auto_zettelkasten.zotero_annotations import PendingAnnotations
    client, service = setup[-2:]
    service.writes_enabled = True
    original = PendingAnnotations.enqueue
    def interrupted(queue, **kwargs):
        if kwargs["annotation_key"] == "ANNAT223":
            raise OSError("interrupted after first queue entry")
        return original(queue, **kwargs)
    monkeypatch.setattr(PendingAnnotations, "enqueue", interrupted)
    with pytest.raises(OSError):
        service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "partial-admission",
            "annotations": [draft("ANNAT222"), draft("ANNAT223")]})
    assert client.writes == 0
    result = recover(service, "annotation-discard", "partial-admission", discard=True)
    assert result["resolved"] and result["discarded"] and client.writes == 0
    queued = service._queue().lookup("users/0", "PDFKEYAA", "ANNAT222")
    assert queued["state"] == "discarded" and queued["payload"]["annotationText"] == "A synthetic finding"
    receipt = json.loads(service._request_path("partial-admission").read_text())
    assert len(receipt["drafts"]) == 2 and len(receipt["prepared_operations"]) == 2


def test_saved_request_cannot_rebind_to_another_annotation_operation(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    other = save(setup, operation="another-failed-request", annotation=draft("ANNAT223"))
    path = service._request_path("save-first")
    receipt = json.loads(path.read_text())
    receipt["operation_refs"] = [service._operation_ref(service._queue().get(other["pending"][0]["id"]))]
    path.write_text(json.dumps(receipt))
    result = recover(service, "annotation-retry")
    assert result["resolved"] is False and result["unresolved_request_ids"] == ["save-first"]
    assert result["retried"] == 0 and client.writes == 2
