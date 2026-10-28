"""Independent recovery regressions using disposable source notes and fake APIs."""
from __future__ import annotations

import base64
import json

import pytest

from auto_zettelkasten import live_notes, webapp
from auto_zettelkasten.documents import DocumentError, DocumentService
from auto_zettelkasten.zotero_workspace_service import ZoteroWorkspaceService

# Reuse only the existing synthetic API transport, not its assertions.
from test_zotero_workspace_service import Client, HEADER, draft


BINDING = {"server_id": "SERVER000001", "library_type": "user", "library_id": "0",
           "item_key": "PARENTAA", "attachment_key": "PDFKEYAA"}


class Registrar:
    def __init__(self):
        self.rows = {}

    def find(self, binding, path):
        return self.rows.get(str(path))

    def register(self, binding, path, *, key, write_token):
        row = {"key": key, "parentItem": binding["item_key"], "path": str(path)}
        self.rows[str(path)] = row
        return row

    def verify(self, binding, path, row):
        return self.rows.get(str(path)) == row


@pytest.fixture
def recovery(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    (root / "Note.md").write_text(HEADER + "Original summary.\n")
    documents = DocumentService(override_root=tmp_path / "overrides", live_root=tmp_path / "shared-live")
    opened = documents.open(root)
    client = Client(tmp_path)
    service = ZoteroWorkspaceService(documents, tmp_path / "zotero-private", client=client, writes_enabled=True)
    registrar = Registrar()
    try:
        yield root, documents, opened["id"], registrar, service, client
    finally:
        documents.close()


def migrate(recovery):
    _, documents, workspace_id, registrar, _, _ = recovery
    note = documents.read(workspace_id, "Note.md")
    return documents.migrate_live_note(workspace_id, note["id"], note["revision"], BINDING, registrar)


def test_restore_interruption_preserves_first_active_recovery_snapshot(recovery, monkeypatch):
    root, documents, workspace_id, registrar, _, _ = recovery
    note = migrate(recovery)
    note = documents.save(workspace_id, note["id"], note["revision"], note["text"] + "Important active personal edit.\n")
    registrar.rows.clear()  # Synthetic user explicitly detached in Zotero.
    original_atomic = live_notes._atomic
    crashed = False

    def interrupt_after_baseline_write(directory, name, raw, **options):
        nonlocal crashed
        result = original_atomic(directory, name, raw, **options)
        if name == "Note.md" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash after baseline restore")
        return result

    monkeypatch.setattr(live_notes, "_atomic", interrupt_after_baseline_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        documents.restore_live_note(workspace_id, note["id"], note["revision"], registrar)
    monkeypatch.setattr(live_notes, "_atomic", original_atomic)
    assert crashed and "Important active" not in (root / "Note.md").read_text()
    # Reopen through a fresh service, as on restart, before resuming restoration.
    reopened = DocumentService(override_root=documents._overrides.root, live_root=documents._live_notes.root)
    try:
        workspace = reopened.open(root)
        current = reopened.read(workspace["id"], "Note.md")
        restored = reopened.restore_live_note(workspace["id"], current["id"], current["revision"], registrar)
        assert not restored.get("live_note")
        recovery_records = [json.loads(path.read_text()) for path in reopened._live_notes.root.rglob("*.json")]
        snapshots = [base64.b64decode(row["restored_active_b64"]) for row in recovery_records if "restored_active_b64" in row]
        assert any(b"Important active personal edit." in raw for raw in snapshots), "Retry erased the original active recovery snapshot"
    finally:
        reopened.close()


def test_profile_preferences_do_not_partition_active_source_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    roots = [tmp_path / "vault-a", tmp_path / "vault-b"]
    apps = []
    registrar = Registrar()
    try:
        for index, root in enumerate(roots):
            root.mkdir()
            (root / "auto-zettelkasten.yml").write_text("version: 1\n")
            (root / "Note.md").write_text(HEADER + "Summary.\n")
            apps.append(webapp.LocalApp(preferences_path=tmp_path / f"profile-{index}" / "prefs.json"))
        first = apps[0].documents
        ws = first.open(roots[0]); current = first.read(ws["id"], "Note.md")
        first.migrate_live_note(ws["id"], current["id"], current["revision"], BINDING, registrar)
        assert live_notes.managed_note(roots[0], "Note.md") is not None, "The mapper could not see the app's active-note reservation"
        second = apps[1].documents
        ws = second.open(roots[1]); current = second.read(ws["id"], "Note.md")
        with pytest.raises(DocumentError, match="another workspace|already bound"):
            second.migrate_live_note(ws["id"], current["id"], current["revision"], BINDING, registrar)
    finally:
        for app in apps:
            app.documents.close()


def test_batch_failure_preserves_all_undispatched_annotation_drafts(recovery):
    _, documents, _, _, service, client = recovery
    client.fail = "lost_ack"
    drafts = [draft(f"ANNAT22{index}") for index in (2, 3, 4)]
    result = service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "batch-recovery", "annotations": drafts})
    assert result["pending"] and client.writes == 1
    # In-memory drafts and the browser disappear. Every unsaved edit must
    # nevertheless remain recoverable from the durable outbox.
    restarted = ZoteroWorkspaceService(documents, service.private_root, client=client, writes_enabled=True)
    pending = restarted._queue().pending()
    assert {row["annotation_key"] for row in pending} == {row["id"] for row in drafts}
    assert len([row for row in pending if row["state"] in {"dispatched", "uncertain"}]) == 1
    assert len([row for row in pending if row["state"] == "queued"]) == 2


def test_annotation_projection_keeps_verified_summary_equivalence(recovery):
    _, documents, workspace_id, _, service, _ = recovery
    note = migrate(recovery)
    baseline = note["live_equivalent_revision"]
    assert baseline
    result = service.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "projection-create", "annotations": [draft()]})
    assert not result["pending"]
    projected = service.handle("annotation-project", {"workspace_id": workspace_id, "note_path": "Note.md",
        "document_id": note["id"], "revision": note["revision"], "scope": "users/0", "attachment_key": "PDFKEYAA", "dirty": False})
    saved = documents.read(workspace_id, "Note.md")
    assert projected["annotation_count"] == 1 and "A synthetic finding" in saved["text"]
    assert saved["annotation_block_hash"] == projected["block_hash"], "Projection receipt was never bound to the active note"
    assert saved["live_equivalent_revision"] == baseline, "Annotations invalidated the unchanged source-summary representation"
