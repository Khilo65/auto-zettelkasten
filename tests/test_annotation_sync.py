import copy
import json
import threading

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.annotation_sync import AnnotationSyncService
from auto_zettelkasten.documents import DocumentService
from auto_zettelkasten.zotero_annotations import START, END, api_to_reader
from auto_zettelkasten.zotero_workspace import VerifiedPDF, ZoteroWorkspaceError
from auto_zettelkasten.zotero_workspace_service import ZoteroWorkspaceService

pytestmark = pytest.mark.skipif(not documents._SAFE_WRITES, reason="Shared Markdown requires directory handles")
SERVER = "testserver22"
PARENT = "PARENT22"
PDF = "PDFTEST2"
ANN = "ANNTEST2"
BINDING = {"server_id": SERVER, "library_type": "user", "library_id": "0", "item_key": PARENT, "attachment_key": PDF}
HEADER = "---\nnote_id: note-example\nsource_id: source-example\nzotero_item_key: PARENT22\ntitle: Original\n---\n"


def annotation(key=ANN, *, parent=PDF, text="A source quotation", comment="My comment", version=2):
    return {"key": key, "version": version, "data": {"key": key, "version": version,
        "itemType": "annotation", "parentItem": parent, "annotationType": "highlight",
        "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[10, 10, 100, 20]]}),
        "annotationText": text, "annotationComment": comment, "annotationColor": "#ffd400",
        "annotationPageLabel": "458", "annotationSortIndex": "00000|000001|00100", "tags": [], "isExternal": False}}


class Client:
    server_id = SERVER

    def __init__(self, root):
        self.pdf = root / "source.pdf"
        self.pdf.write_bytes(b"%PDF synthetic fixture\n")
        self.rows = {PDF: [annotation()]}
        self.available = True
        self.fail_children = False
        self.reads = 0

    def status(self):
        return {"available": self.available, "server_id": self.server_id, "authorized": False, "write_supported": True}

    def children(self, scope, parent_key, *, start=0, limit=64):
        if self.fail_children:
            raise ZoteroWorkspaceError("Incomplete attachment page", code="incomplete_inventory")
        records = [self.item(scope, key) for key in sorted(self.rows)]
        return {"records": records[start:start + limit], "library_version": 4, "next_start": None, "total": len(records), "server_id": SERVER}

    def item(self, scope, key):
        if key in self.rows:
            return {"key": key, "version": 1, "data": {"key": key, "version": 1,
                "itemType": "attachment", "parentItem": PARENT, "contentType": "application/pdf", "title": "Source PDF"}}
        return next((copy.deepcopy(row) for rows in self.rows.values() for row in rows if row["key"] == key), None)

    def annotations(self, scope, attachment_key):
        self.reads += 1
        if not self.available:
            raise ZoteroWorkspaceError("Zotero closed", code="unavailable")
        return copy.deepcopy(self.rows[attachment_key])

    def pdf_file(self, scope, attachment_key):
        info = self.pdf.stat()
        return VerifiedPDF(self.pdf, attachment_key, PARENT, scope, SERVER, 1, info.st_size, info.st_mtime_ns)


class Registrar:
    def __init__(self):
        self.rows = {}

    def find(self, binding, path):
        return self.rows.get(str(path))

    def register(self, binding, path, *, key, write_token):
        row = {"key": key, "path": str(path), "parentItem": binding["item_key"]}
        self.rows[str(path)] = row
        return row

    def verify(self, binding, path, attachment):
        return self.find(binding, path) == attachment


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "vault"; root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    (root / "Note.md").write_text(HEADER + "Source summary.\n\nHuman prose.\n")
    docs = DocumentService(journal_root=tmp_path / "operations", override_root=tmp_path / "overrides", live_root=tmp_path / "live")
    ws = docs.open(root)
    current = docs.read(ws["id"], "Note.md")
    docs.migrate_live_note(ws["id"], current["id"], current["revision"], BINDING, Registrar())
    client = Client(tmp_path)
    service = ZoteroWorkspaceService(docs, tmp_path / "zotero", client=client)
    sync = AnnotationSyncService(docs, service, tmp_path / "sync", autostart=False)
    service.annotation_sync = sync
    yield root, docs, ws["id"], client, service, sync
    sync.close(); docs.close()


def buffers(setup, rows=None, **kwargs):
    _, _, ws, _, _, sync = setup
    return sync.register_buffers(ws, "editor-one", rows or [], **kwargs)


def test_active_inventory_only_completed_exact_workspace_bindings(setup, tmp_path):
    root, docs, ws, _, _, _ = setup
    active = docs._live_notes.list_active(root, docs._root_identity)
    assert active == [{"path": "Note.md", "binding": BINDING, "source_identity": {"note_id": "note-example", "source_id": "source-example"}, "phase": "archived"}]
    assert not any("baseline" in key for key in active[0])
    other = tmp_path / "copy"; other.mkdir()
    (other / "Note.md").write_bytes((root / "Note.md").read_bytes())
    assert docs._live_notes.list_active(other, documents._identity(other.stat())) == []
    with pytest.raises(Exception, match="limit"):
        docs._live_notes.list_active(root, docs._root_identity, limit=0)


def test_handshake_then_closed_note_projection_and_no_change_skip(setup):
    root, docs, ws, client, _, sync = setup
    sync.select_workspace(ws)
    assert sync.run_once() == 0
    assert START not in (root / "Note.md").read_text()
    buffers(setup)
    assert sync.run_once() == 1
    text = (root / "Note.md").read_text()
    assert START in text and "A source quotation" in text and "My comment" in text
    assert "page=1&annotation=" + ANN in text and "p. 458" in text
    assert sync.status(ws)["counts"]["current"] == 1
    before = (root / "Note.md").stat().st_mtime_ns
    sync.enqueue_attachment("users/0", PDF)
    assert sync.run_once() == 1
    assert (root / "Note.md").stat().st_mtime_ns == before
    assert text.count(START) == 1 and client.reads >= 2


def test_open_clean_buffer_is_ready_but_not_written_by_worker(setup):
    root, docs, ws, _, service, sync = setup
    current = docs.read(ws, "Note.md")
    buffers(setup, [{"path": "Note.md", "revision": current["revision"], "dirty": False}])
    sync.run_once()
    job = sync.status(ws)["jobs"][0]
    assert job["ready"] and job["state"] == "ready"
    assert START not in (root / "Note.md").read_text()
    assert job["document_id"] == current["id"] and job["revision"] == current["revision"]
    result = service.handle("annotation-project", {"workspace_id": ws, "note_path": job["note_path"], "document_id": job["document_id"],
        "revision": job["revision"], "scope": job["scope"], "attachment_key": job["attachment_key"]})
    assert result["document"]["text"].count(START) == 1
    assert sync.status(ws)["jobs"][0]["state"] == "current"


def test_draft_text_survives_restart_and_blocks_closed_note(setup, tmp_path):
    root, docs, ws, _, service, sync = setup
    current = docs.read(ws, "Note.md")
    draft = {"path": "Note.md", "revision": current["revision"], "dirty": True, "text": current["text"] + "Unsaved thought.\n"}
    buffers(setup, [draft])
    sync.run_once()
    assert sync.status(ws)["jobs"][0]["state"] == "waiting_for_draft"
    sync.close()
    resumed = AnnotationSyncService(docs, service, tmp_path / "sync", autostart=False)
    service.annotation_sync = resumed
    try:
        status = resumed.select_workspace(ws)
        assert not status["started"] and status["drafts"][0]["text"].endswith("Unsaved thought.\n")
        assert resumed.run_once() == 0
        resumed.register_buffers(ws, "new-editor", [])
        resumed.run_once()
        assert resumed.status(ws)["counts"]["waiting_for_draft"] == 1
        assert START not in (root / "Note.md").read_text()
        resumed.register_buffers(ws, "new-editor", [], release_paths=["Note.md"])
        resumed.run_once()
        assert resumed.status(ws)["drafts"] == []
        assert (root / "Note.md").read_text().count(START) == 1
    finally:
        resumed.close()


def test_manual_external_prose_and_annotation_deletion_preserved(setup):
    root, _, ws, client, _, sync = setup
    buffers(setup); sync.run_once()
    path = root / "Note.md"
    path.write_text(path.read_text().replace("Human prose.", "Human prose.\nExternal personal prose."))
    client.rows[PDF] = []
    sync.enqueue_attachment("users/0", PDF); sync.run_once()
    assert "External personal prose." in path.read_text()
    assert "No confirmed annotations." in path.read_text()
    assert "A source quotation" not in path.read_text()
    assert sync.status(ws)["counts"]["current"] == 1


@pytest.mark.parametrize("damage", ["edit", "delete", "duplicate"])
def test_modified_or_removed_managed_section_requires_reconciliation(setup, damage):
    root, _, ws, _, _, sync = setup
    buffers(setup); sync.run_once()
    path = root / "Note.md"; text = path.read_text()
    start, end = text.index(START), text.index(END) + len(END)
    if damage == "edit":
        changed = text.replace("A source quotation", "My modified quotation")
    elif damage == "delete":
        changed = text[:start] + text[end:]
    else:
        changed = text + "\n" + text[start:end]
    path.write_text(changed)
    sync.enqueue_attachment("users/0", PDF); sync.run_once()
    assert path.read_text() == changed
    assert sync.status(ws)["jobs"][0]["state"] == "blocked"


def test_incomplete_inventory_offline_and_replaced_pdf_never_remove_note_text(setup):
    root, _, ws, client, _, sync = setup
    buffers(setup); sync.run_once()
    before = (root / "Note.md").read_bytes()
    client.fail_children = True
    sync.enqueue_workspace(ws)
    assert sync.run_once() == 1
    assert "Incomplete" in sync.status(ws)["error"]
    assert (root / "Note.md").read_bytes() == before
    client.fail_children = False; client.available = False
    sync.enqueue_workspace(ws); sync.run_once()
    assert (root / "Note.md").read_bytes() == before
    client.available = True
    client.pdf.write_bytes(b"%PDF replaced source\n")
    sync.enqueue_workspace(ws); sync.run_once()
    assert (root / "Note.md").read_bytes() == before
    assert sync.status(ws)["jobs"][0]["state"] == "failed"


def test_multiple_attachments_keep_distinct_groups(setup):
    root, _, ws, client, _, sync = setup
    second = "PDFNEXT2"
    client.rows[second] = [annotation("ANNNEXT2", parent=second, text="Another attachment quote")]
    buffers(setup); assert sync.run_once() == 2
    text = (root / "Note.md").read_text()
    assert text.count(START) == 1 and "Another attachment quote" in text and "A source quotation" in text
    assert {row["attachment_key"] for row in sync.status(ws)["jobs"]} == {PDF, second}


def test_buffer_admission_validation_does_not_erase_retained_draft(setup):
    _, docs, ws, _, _, sync = setup
    current = docs.read(ws, "Note.md")
    buffers(setup, [{"path": "Note.md", "revision": current["revision"], "dirty": True, "text": "Keep me"}])
    before = sync.status(ws)["drafts"]
    for rows in ([{"path": "../Note.md", "revision": "a", "dirty": False}],
                 [{"path": "Note.md", "revision": "a", "dirty": True, "text": "x" * (1024 * 1024 + 1)}],
                 [{"path": "Note.md", "revision": "a", "dirty": "no"}]):
        with pytest.raises(ValueError):
            buffers(setup, rows)
    assert sync.status(ws)["drafts"] == before


def test_clean_buffer_heartbeat_does_not_write_private_state_or_repoll(setup):
    _, docs, ws, client, _, sync = setup
    current = docs.read(ws, "Note.md")
    rows = [{"path": "Note.md", "revision": current["revision"], "dirty": False}]
    buffers(setup, rows); sync.run_once()
    before, reads = sync._state_path.stat().st_mtime_ns, client.reads
    buffers(setup, rows)
    assert sync._state_path.stat().st_mtime_ns == before
    assert sync.run_once() == 0 and client.reads == reads


def test_concurrent_differing_drafts_survive_and_cannot_be_released_by_other_editor(setup):
    _, docs, ws, _, _, sync = setup
    revision = docs.read(ws, "Note.md")["revision"]
    buffers(setup, [{"path": "Note.md", "revision": revision, "dirty": True, "text": "First draft"}])
    sync.register_buffers(ws, "editor-two", [{"path": "Note.md", "revision": revision, "dirty": True, "text": "Second draft"}])
    assert {row["text"] for row in sync.status(ws)["drafts"]} == {"First draft", "Second draft"}
    assert all(row["conflict"] for row in sync.status(ws)["drafts"])
    with pytest.raises(ValueError, match="Another open editor"):
        sync.register_buffers(ws, "editor-three", [], release_paths=["Note.md"])


def test_worker_lifecycle_waits_for_handshake_and_stops(setup, tmp_path):
    root, docs, ws, _, service, _ = setup
    worker = AnnotationSyncService(docs, service, tmp_path / "thread-sync", poll_seconds=0.1)
    service.annotation_sync = worker
    observed = threading.Event()
    original = worker.projected
    def projected(*args, **kwargs):
        original(*args, **kwargs); observed.set()
    worker.projected = projected
    try:
        worker.select_workspace(ws)
        assert not observed.wait(0.15)
        worker.register_buffers(ws, "window", [])
        assert observed.wait(3)
        assert START in (root / "Note.md").read_text()
    finally:
        worker.close()
    assert not worker._thread.is_alive()


def test_secondary_attachment_discovery_is_bounded_and_local_updates_go_first(setup):
    root, docs, ws, client, service, sync = setup
    known = docs._live_notes.list_active(root, docs._root_identity)[0]
    inventory = [{"path": f"AA{index:03d}.md", "binding": {**BINDING, "item_key": f"PAR{index:05d}", "attachment_key": f"PDF{index:05d}"}}
                 for index in range(20)] + [known]
    sync._bindings = lambda workspace_id: inventory
    events = []
    original = service.handle
    def observed(action, data):
        events.append(action)
        return original(action, data)
    service.handle = observed
    buffers(setup)
    assert sync.run_once() == 8
    assert events.count("children") == 8
    assert len(sync.status(ws)["jobs"]) == 21
    assert START not in (root / "Note.md").read_text()
    events.clear()
    sync.enqueue_attachment("users/0", PDF)
    assert sync.run_once() == 8
    assert events.count("children") == 8
    assert events.index("annotation-project") < events.index("children")
    assert START in (root / "Note.md").read_text()


def test_shutdown_during_remote_read_cannot_project_after_close(setup, tmp_path):
    root, docs, ws, _, service, _ = setup
    worker = AnnotationSyncService(docs, service, tmp_path / "shutdown-sync")
    entered, release = threading.Event(), threading.Event()
    original = service.handle
    def delayed(action, data):
        if action == "annotation-refresh":
            entered.set()
            assert release.wait(2)
        return original(action, data)
    service.handle = delayed
    service.annotation_sync = worker
    try:
        worker.register_buffers(ws, "window", [])
        assert entered.wait(2)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        worker.close()
        timer.join()
        assert not worker._thread.is_alive()
        assert START not in (root / "Note.md").read_text()
    finally:
        release.set(); worker.close()


def test_interrupted_receipt_without_outbox_operations_blocks_note_projection(setup, tmp_path, monkeypatch):
    root, docs, ws, client, service, _ = setup
    service.writes_enabled = True
    monkeypatch.setattr(client, "status", lambda: {"available": True, "server_id": SERVER, "authorized": True, "write_supported": True})
    queue = service._queue()
    monkeypatch.setattr(service, "_queue", lambda: queue)
    monkeypatch.setattr(queue, "enqueue", lambda **kwargs: (_ for _ in ()).throw(OSError("interrupted before admission")))
    new_annotation = api_to_reader(annotation("ANNNEW22", version=0), scope="users/0", attachment_key=PDF, writable=True, known_internal=True)
    with pytest.raises(OSError, match="before admission"):
        service.handle("annotation-save", {"scope": "users/0", "attachment_key": PDF,
            "operation_id": "new-highlight", "annotations": [new_annotation]})
    assert queue.pending() == []
    fresh = ZoteroWorkspaceService(docs, service.private_root, client=client)
    refresh = fresh.handle("annotation-refresh", {"scope": "users/0", "attachment_key": PDF})
    assert refresh["pending"] == [] and refresh["pending_receipt_ids"]
    resumed = AnnotationSyncService(docs, fresh, tmp_path / "receipt-sync", autostart=False)
    fresh.annotation_sync = resumed
    try:
        resumed.register_buffers(ws, "new-window", [])
        assert resumed.run_once() == 1
        job = resumed.status(ws)["jobs"][0]
        assert job["state"] == "waiting_for_zotero" and not job["ready"]
        assert "Resolve" in job["reason"]
        assert START not in (root / "Note.md").read_text()
    finally:
        resumed.close()


def _preview_pdf(client):
    from pypdf import PdfWriter
    writer = PdfWriter(); writer.add_blank_page(width=200, height=300)
    with client.pdf.open("wb") as stream:
        writer.write(stream)


def test_area_previews_update_closed_note_without_text_change_and_rebuild_missing_asset(setup):
    root, _, ws, client, _, sync = setup
    _preview_pdf(client)
    row = annotation(); row["data"]["annotationType"] = "image"
    client.rows[PDF] = [row]
    buffers(setup); sync.run_once()
    text = (root / "Note.md").read_text()
    assert "![Image preview · PDF page 1](attachments/zotero/" in text
    assert "[Open in PDF](zotero://open-pdf/library/items/" in text
    job = sync.status(ws)["jobs"][0]
    assert job["preview_status"] == "current" and job["preview_pending"] == 0
    asset = next((root / "attachments" / "zotero").glob("*.png"))
    asset.unlink()
    sync.enqueue_attachment("users/0", PDF); sync.run_once()
    assert asset.exists()
    assert (root / "Note.md").read_text() == text
    assert sync.status(ws)["counts"]["current"] == 1


def test_optional_highlight_preview_is_read_only_derivative_and_survives_restart(setup, tmp_path):
    from auto_zettelkasten.annotation_previews import AnnotationPreviews
    root, docs, ws, client, service, sync = setup
    _preview_pdf(client)
    buffers(setup); sync.run_once()
    current = docs.read(ws, "Note.md")
    request = {"workspace_id": ws, "note_path": "Note.md", "document_id": current["id"], "revision": current["revision"],
               "scope": "users/0", "attachment_key": PDF, "annotation_key": ANN, "expected_version": 2, "enabled": True}
    assert not service.writes_enabled and not client.status()["authorized"]
    enabled = service.handle("annotation-preview-request", request)
    assert enabled["queued"] and enabled["enabled"]
    sync.run_once()
    text = (root / "Note.md").read_text()
    assert "A source quotation" in text and "My comment" in text and "![Highlight preview" in text
    assert not (service.private_root / "requests").exists()
    service._annotation_previews = AnnotationPreviews(docs, service._annotation_previews.root)
    sync.enqueue_attachment("users/0", PDF); sync.run_once()
    assert (root / "Note.md").read_text() == text
    current = docs.read(ws, "Note.md")
    service.handle("annotation-preview-request", {**request, "revision": current["revision"], "enabled": False})
    sync.run_once()
    assert "![Highlight preview" not in (root / "Note.md").read_text()
    assert list((root / "attachments" / "zotero").glob("*.png"))


def test_preview_request_rejects_stale_version_detached_copy_and_wrong_source(setup, tmp_path):
    root, docs, ws, _, service, _ = setup
    current = docs.read(ws, "Note.md")
    request = {"workspace_id": ws, "note_path": "Note.md", "document_id": current["id"], "revision": current["revision"],
               "scope": "users/0", "attachment_key": PDF, "annotation_key": ANN, "expected_version": 1}
    with pytest.raises(ValueError, match="annotation changed"):
        service.handle("annotation-preview-request", request)
    with pytest.raises(ValueError, match="boolean"):
        service.handle("annotation-preview-request", {**request, "expected_version": 2, "enabled": 1})
    with pytest.raises(ValueError):
        service.handle("annotation-preview-request", {**request, "expected_version": 2, "scope": "groups/42"})
    copied = tmp_path / "copy"; copied.mkdir()
    (copied / "Note.md").write_text(current["text"])
    new = docs.open(copied); copy_note = docs.read(new["id"], "Note.md")
    with pytest.raises(ValueError, match="shared note"):
        service.handle("annotation-preview-request", {**request, "workspace_id": new["id"], "document_id": copy_note["id"], "revision": copy_note["revision"], "expected_version": 2})


def test_bad_preview_does_not_turn_saved_annotation_into_a_failed_note_update(setup):
    root, _, ws, client, _, sync = setup
    row = annotation(); row["data"]["annotationType"] = "image"
    client.rows[PDF] = [row]  # deliberately invalid PDF bytes from legacy fixture
    buffers(setup); sync.run_once()
    text = (root / "Note.md").read_text()
    assert START in text and "image preview pending" in text
    status = sync.status(ws)
    assert status["counts"]["current"] == 1 and status["jobs"][0]["preview_status"] == "unavailable"


def test_previews_defer_draft_projection_and_refresh_ink_color_without_mutating_source(setup):
    root, docs, ws, client, _, sync = setup
    _preview_pdf(client)
    row = annotation(); row["data"].update(annotationType="ink", annotationPosition=json.dumps({"pageIndex": 0, "paths": [[20, 40, 60, 80]], "width": 2}))
    client.rows[PDF] = [row]
    current = docs.read(ws, "Note.md")
    buffers(setup, [{"path": "Note.md", "revision": current["revision"], "dirty": True, "text": current["text"] + "Pending thought.\n"}])
    sync.run_once()
    assert sync.status(ws)["jobs"][0]["state"] == "waiting_for_draft"
    assert START not in (root / "Note.md").read_text()
    buffers(setup, release_paths=["Note.md"]); sync.run_once()
    first = (root / "Note.md").read_text()
    assert "![Ink preview" in first
    old_assets = set((root / "attachments" / "zotero").glob("*.png"))
    row["data"]["annotationColor"] = "#ff0000"
    sync.enqueue_attachment("users/0", PDF); sync.run_once()
    assert (root / "Note.md").read_text() != first
    assert set((root / "attachments" / "zotero").glob("*.png")) > old_assets
    assert "Source summary." in (root / "Note.md").read_text()
    assert client.rows[PDF][0]["data"]["annotationColor"] == "#ff0000"


def test_unavailable_preview_inventory_preserves_confirmed_text_projection(setup, monkeypatch):
    root, _, ws, _, service, sync = setup
    def unavailable(*args, **kwargs):
        raise ValueError("Invalid private annotation-preview inventory")
    monkeypatch.setattr(service._annotation_previews, "prepare", unavailable)
    buffers(setup); sync.run_once()
    assert "A source quotation" in (root / "Note.md").read_text()
    job = sync.status(ws)["jobs"][0]
    assert job["state"] == "current" and job["preview_status"] == "unavailable"
