"""Synthetic request, queue and managed-section recovery integration."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from auto_zettelkasten.documents import DocumentConflict
from auto_zettelkasten.zotero_annotations import AnnotationConflict, END, START
from auto_zettelkasten.zotero_workspace_service import ZoteroWorkspaceService
from test_zotero_workspace_service import setup, draft, migrate, project, save


def restarted(setup):
    old = setup[-1]
    return ZoteroWorkspaceService(setup[1], old.private_root, client=setup[-2], writes_enabled=True)


def recover(service, **extra):
    return service.handle("annotation-recovery", {"scope": "users/0", "attachment_key": "PDFKEYAA", **extra})


def receipt_context(row):
    return {"scope": row["scope"], "attachment_key": row["attachment_key"], "receipt_ids": [row["receipt_id"]]}


def projection_context(setup):
    current = setup[1].read(setup[2]["id"], "Note.md")
    return {"workspace_id": setup[2]["id"], "note_path": "Note.md", "document_id": current["id"],
        "revision": current["revision"], "scope": "users/0", "attachment_key": "PDFKEYAA"}


def test_restart_discovers_receipt_before_any_outbox_admission(setup, monkeypatch):
    client, service = setup[-2:]
    service.writes_enabled = True
    queue = service._queue()
    original = queue.enqueue
    monkeypatch.setattr(service, "_queue", lambda: queue)
    monkeypatch.setattr(queue, "enqueue", lambda **kw: (_ for _ in ()).throw(OSError("crash before admission")))
    with pytest.raises(OSError, match="crash"):
        save(setup)
    assert client.writes == 0 and queue.pending() == []
    monkeypatch.setattr(queue, "enqueue", original)
    fresh = restarted(setup)
    row = recover(fresh)["records"][0]
    assert row["status"] == "waiting_to_send" and row["durable"]
    assert row["request_id"] == "save-first" and row["drafts"][0]["text"] == "A synthetic finding"
    replay = fresh.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "save-first", "annotations": [draft()]})
    assert replay["pending"] == [] and replay["resolved"] is False and replay["retained"] is True
    assert replay["pending_receipt_ids"] == [row["receipt_id"]] and client.writes == 0
    reader = fresh.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    assert reader["readOnly"] and reader["pending_receipt_ids"] == [row["receipt_id"]]
    checked = fresh.handle("annotation-reconcile", receipt_context(row))
    assert checked["resolved"] is False and client.writes == 0
    result = fresh.handle("annotation-retry", receipt_context(row))
    assert result["resolved"] is True and result["retried"] == 1 and client.writes == 1
    assert result["readOnly"] is False
    again = fresh.handle("annotation-retry", receipt_context(row))
    assert again["resolved"] is True and again["retried"] == 0 and client.writes == 1


def test_legacy_hashed_receipt_is_discoverable_and_reconciles_without_resend(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    save(setup)
    path = service._request_path("save-first")
    receipt = json.loads(path.read_text())
    receipt.pop("server_id")
    receipt.pop("request_id")
    path.write_text(json.dumps(receipt))
    fresh = restarted(setup)
    row = recover(fresh)["records"][0]
    assert row["receipt_id"] == path.stem and row["request_id"] is None
    assert row["status"] == "checking_outcome"
    assert row["observed"][0]["remote"]["text"] == "A synthetic finding"
    result = fresh.handle("annotation-reconcile", receipt_context(row))
    assert result["resolved"] and not result["pending"] and not result["readOnly"]
    assert client.writes == 1
    assert recover(fresh)["records"][0]["status"] == "saved"


def test_recovery_rejects_foreign_server_and_tampered_batch(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    path = service._request_path("save-first")
    original = json.loads(path.read_text())
    bad = {**original, "server_id": "SERVER000002"}
    path.write_text(json.dumps(bad))
    rows = recover(service)["records"]
    assert len(rows) == 1 and rows[0]["receipt_id"] is None  # durable orphan stays visible
    result = service.handle("annotation-reconcile", {"scope": "users/0", "attachment_key": "PDFKEYAA", "receipt_ids": [path.stem]})
    assert not result["resolved"] and result["unresolved_request_ids"] == [path.stem]
    original["drafts"][0]["comment"] = "Altered without matching the request hash"
    path.write_text(json.dumps(original))
    assert recover(service)["records"][0]["receipt_id"] is None
    assert client.writes == 1


def test_conflict_survives_restart_and_cannot_be_overwritten(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    save(setup)
    remote = client.records["ANNAT222"]
    remote["version"] = remote["data"]["version"] = 2
    remote["data"]["annotationComment"] = "A concurrent Zotero edit"
    fresh = restarted(setup)
    row = recover(fresh)["records"][0]
    result = fresh.handle("annotation-reconcile", receipt_context(row))
    assert result["pending"][0]["state"] == "conflict" and result["readOnly"]
    before = copy.deepcopy(fresh._queue().pending()[0])
    blocked = fresh.handle("annotation-save", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "new-conflicting-edit", "annotations": [draft(version=2)]})
    assert blocked["pending"][0]["state"] == "conflict"
    assert fresh._queue().pending()[0] == before and client.writes == 1
    with pytest.raises(AnnotationConflict, match="reconciled"):
        fresh._queue().enqueue(scope="users/0", attachment_key="PDFKEYAA", annotation_key="ANNAT222",
            operation="upsert", payload=before["payload"], base_version=before["base_version"], expected_revision=before["revision"])
    rows = recover(fresh)["records"]
    assert len(rows) == 2  # the new attempted edit is retained separately too
    assert all(row["status"] == "conflict" and row["actions"]["discard"] and not row["actions"]["retry"] for row in rows)
    kept = fresh.handle("annotation-discard", {"scope": "users/0", "attachment_key": "PDFKEYAA", "receipt_ids": [row["receipt_id"] for row in rows], "discard": True})
    assert kept["discarded"] and kept["resolved"] and not kept["readOnly"]
    assert fresh._queue().get(before["id"])["payload"] == before["payload"]
    assert client.records["ANNAT222"]["data"]["annotationComment"] == "A concurrent Zotero edit"
    retained = recover(fresh)["records"][0]
    assert retained["status"] == "discarded" and not any(retained["actions"].values())


def test_receipt_retry_is_serialized_and_new_history_does_not_pin_old_version(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    row = recover(service)["records"][0]
    client.fail = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.handle("annotation-retry", receipt_context(row)), range(2)))
    assert all(result["resolved"] for result in results) and client.writes == 2
    current = service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})["annotations"][0]
    save(setup, operation="save-follow-up", annotation={**current, "comment": "A later edit"})
    rows = recover(service)["records"]
    assert len(rows) == 2 and all(row["status"] == "saved" for row in rows)
    assert not service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["readOnly"]


def test_recovery_pagination_and_selector_binding_are_bounded(setup):
    service = setup[-1]
    service.writes_enabled = True
    for n in range(3):
        save(setup, operation=f"save-item-{n}", annotation=draft(f"ANNAT22{n + 2}"))
    page = recover(service, limit=1)
    assert page["total"] == 3 and len(page["records"]) == 1 and page["next_start"] == 1
    assert len(recover(service, start=page["next_start"], limit=2)["records"]) == 2
    for data in ({"limit": 65}, {"start": -1}, {"limit": True}):
        with pytest.raises(ValueError, match="pagination"):
            recover(service, **data)
    with pytest.raises(ValueError, match="not both"):
        service.handle("annotation-reconcile", {"attachment_key": "PDFKEYAA", "receipt_ids": [page["records"][0]["receipt_id"]], "request_ids": ["save-item-0"]})
    with pytest.raises(ValueError, match="receipt"):
        service.handle("annotation-retry", {"attachment_key": "PDFKEYAA", "receipt_ids": ["../../outside"]})


def test_unknown_dispatch_cannot_be_discarded_when_remote_read_is_unavailable(setup, monkeypatch):
    service = setup[-1]
    service.writes_enabled = True
    setup[-2].fail = "lost_ack"
    save(setup)
    row = recover(service)["records"][0]
    before = service._queue().pending()[0]
    monkeypatch.setattr(setup[-2], "item", lambda *args: (_ for _ in ()).throw(OSError("connection closed")))
    with pytest.raises(OSError):
        service.handle("annotation-discard", {**receipt_context(row), "discard": True})
    assert service._queue().pending()[0] == before
    assert recover(service)["records"][0]["remote_error"] == "connection closed"


def test_deleted_managed_section_requires_explicit_resolution(setup):
    root, docs, ws, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    save(setup)
    first = project(setup)
    text = first["document"]["text"]
    start, end = text.index(START), text.index(END) + len(END)
    current = docs.save(ws["id"], first["document"]["id"], first["document"]["revision"], text[:start] + text[end:] + "\nMy external prose.\n")
    assert service.annotation_snapshot(root, "Note.md") is None
    with pytest.raises(AnnotationConflict, match="missing"):
        project(setup)
    context = projection_context(setup)
    status = service.handle("annotation-projection-status", context)
    assert status["state"] == "conflict"
    preview = service.handle("annotation-projection-preview", context)
    assert preview["existing_section"] == "" and "A synthetic finding" in preview["proposed_section"]
    resolved = service.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "preserve"})
    assert resolved["document"]["text"].count(START) == 1 and "My external prose." in resolved["document"]["text"]
    assert service.annotation_snapshot(root, "Note.md") is not None
    assert list((service.private_root / "projection-history").glob("*.json"))


def test_unrecorded_managed_markers_are_a_conflict_not_a_new_section(setup):
    _, docs, ws, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    current = docs.read(ws["id"], "Note.md")
    docs.save(ws["id"], current["id"], current["revision"], current["text"] + "\n" + START + "\nManually imported content.\n" + END + "\n")
    context = projection_context(setup)
    assert service.handle("annotation-projection-status", context)["state"] == "conflict"
    with pytest.raises(AnnotationConflict):
        service.handle("annotation-project", context)


def test_projection_pause_persists_and_stale_preview_is_rejected(setup):
    _, docs, ws, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    save(setup)
    project(setup)
    context = projection_context(setup)
    preview = service.handle("annotation-projection-preview", context)
    result = service.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "pause"})
    assert result["paused"]
    fresh = restarted(setup)
    assert fresh.handle("annotation-projection-status", context)["state"] == "paused"
    assert fresh.handle("annotation-project", context)["deferred"]
    preview = fresh.handle("annotation-projection-preview", context)
    current = docs.read(ws["id"], "Note.md")
    docs.save(ws["id"], current["id"], current["revision"], current["text"] + "\nMore prose.\n")
    with pytest.raises(DocumentConflict):
        fresh.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "replace"})
    context = projection_context(setup)
    preview = fresh.handle("annotation-projection-preview", context)
    fresh.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "replace"})
    assert fresh.handle("annotation-projection-status", context)["state"] == "current"


def test_projection_rejects_changed_remote_preview_and_preserves_damaged_prose(setup):
    _, docs, ws, _, service = setup
    service.writes_enabled = True
    migrate(setup)
    saved = save(setup)["annotations"][0]
    first = project(setup)["document"]
    docs.save(ws["id"], first["id"], first["revision"], first["text"].replace(END, "") + "\nHuman text after a damaged marker.\n")
    context = projection_context(setup)
    preview = service.handle("annotation-projection-preview", context)
    save(setup, operation="remote-update", annotation={**saved, "comment": "Changed since preview"})
    with pytest.raises(AnnotationConflict, match="changed since"):
        service.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "replace"})
    preview = service.handle("annotation-projection-preview", context)
    result = service.handle("annotation-projection-resolve", {**context, "preview_token": preview["preview_token"], "choice": "replace"})
    assert "Human text after a damaged marker." in result["document"]["text"]
    assert result["document"]["text"].count(START) == 1 and result["document"]["text"].count(END) == 1


def test_unchanged_projection_does_not_rewrite_note_and_sync_hooks_are_durable(setup, monkeypatch):
    events = []
    class Sync:
        def enqueue_workspace(self, *args): events.append(("workspace", args))
        def enqueue_attachment(self, *args): events.append(("attachment", args))
        def projected(self, *args): events.append(("projected", args))
    service = setup[-1]
    service.annotation_sync = Sync()
    service.writes_enabled = True
    migrate(setup)
    save(setup)
    first = project(setup)
    assert any(kind == "workspace" for kind, _ in events)
    assert any(kind == "attachment" for kind, _ in events)
    assert any(kind == "projected" for kind, _ in events)
    monkeypatch.setattr(setup[1], "save", lambda *args: (_ for _ in ()).throw(AssertionError("unchanged note was rewritten")))
    again = project(setup)
    assert again["unchanged"] and again["document"]["revision"] == first["document"]["revision"]


def test_stale_version_user_comment_is_retained_before_remote_validation(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    prior = save(setup)["annotations"][0]
    remote = client.records["ANNAT222"]
    remote["version"] = remote["data"]["version"] = 2
    remote["data"]["annotationComment"] = "Native Zotero changed the comment"
    incoming = {**prior, "comment": "My unsent app comment must survive a restart"}
    failed = save(setup, operation="save-stale-comment", annotation=incoming)
    assert failed["retained"] and failed["resolved"] is False and failed["pending"] == []
    assert failed["pending_receipt_ids"] and client.writes == 1
    fresh = restarted(setup)
    row = next(row for row in recover(fresh)["records"] if row["request_id"] == "save-stale-comment")
    assert row["status"] == "conflict" and row["durable"] and not row["actions"]["retry"]
    assert row["drafts"][0]["comment"] == incoming["comment"]
    assert row["observed"][0]["remote"]["comment"] == "Native Zotero changed the comment"
    result = fresh.handle("annotation-reconcile", receipt_context(row))
    assert not result["resolved"] and result["readOnly"] and client.writes == 1
    kept = fresh.handle("annotation-discard", {**receipt_context(row), "discard": True})
    assert kept["discarded"] and not kept["readOnly"]
    retained = next(row for row in recover(fresh)["records"] if row["request_id"] == "save-stale-comment")
    assert retained["status"] == "discarded" and retained["drafts"][0]["comment"] == incoming["comment"]


@pytest.mark.parametrize("failure", ["closed", "revoked"])
def test_pre_admission_connection_failure_keeps_bound_reader_draft(setup, monkeypatch, failure):
    client, service = setup[-2:]
    service.writes_enabled = True
    service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})
    original_status = client.status
    if failure == "closed":
        monkeypatch.setattr(client, "status", lambda: (_ for _ in ()).throw(OSError("Zotero was closed")))
    else:
        monkeypatch.setattr(client, "status", lambda: {"available": True, "authorized": False, "write_supported": True, "server_id": client.server_id})
    response = save(setup, operation="offline-reader-change", annotation={**draft(), "comment": "An offline user comment"})
    assert response["retained"] and response["resolved"] is False and client.writes == 0
    monkeypatch.setattr(client, "status", original_status)
    fresh = restarted(setup)
    row = recover(fresh)["records"][0]
    assert row["drafts"][0]["comment"] == "An offline user comment" and row["durable"]
    checked = fresh.handle("annotation-reconcile", receipt_context(row))
    assert not checked["resolved"] and client.writes == 0
    row = recover(fresh)["records"][0]
    assert row["status"] == "waiting_to_send" and row["actions"]["retry"]
    result = fresh.handle("annotation-retry", receipt_context(row))
    assert result["resolved"] and client.writes == 1


def test_invalid_raw_draft_binding_is_rejected_before_retention(setup):
    service = setup[-1]
    service.writes_enabled = True
    service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})
    with pytest.raises(ValueError, match="identity"):
        save(setup, annotation={**draft(), "attachment_key": "ATHERA22"})
    assert recover(service)["records"] == [] and setup[-2].writes == 0


def test_restart_offline_inventory_uses_expected_pin_only_for_local_recovery(setup, monkeypatch):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup, annotation={**draft(), "comment": "Visible even when Zotero is closed after restart"})
    pending_before = copy.deepcopy(service._queue().pending())
    server_id = client.server_id
    client.server_id = None
    client.expected_server_id = server_id
    client.base_url = client._allowed_origin = "http://127.0.0.1:23120"
    monkeypatch.setattr(client, "status", lambda: {"available": False, "authorized": False, "server_id": None})
    monkeypatch.setattr(client, "pdf_file", lambda *args: (_ for _ in ()).throw(AssertionError("offline inventory fetched a PDF")))
    monkeypatch.setattr(client, "item", lambda *args: (_ for _ in ()).throw(AssertionError("offline inventory read remote records")))
    fresh = restarted(setup)
    result = recover(fresh)
    assert result["offline"] and result["server_id"] == server_id and not result["connected"]
    assert client.server_id is None  # a local namespace is not remote authority
    row = result["records"][0]
    assert row["drafts"][0]["comment"] == "Visible even when Zotero is closed after restart"
    assert row["status"] == "unable_to_verify" and not any(row["actions"].values())
    assert fresh._queue().pending() == pending_before
    with pytest.raises(ValueError, match="Authorize"):
        fresh.handle("annotation-retry", receipt_context(row))
    assert client.writes == 1 and client.server_id is None


def test_offline_inventory_uses_last_verified_origin_without_claiming_connection(setup, monkeypatch):
    client, service = setup[-2:]
    client.base_url = client._allowed_origin = "http://127.0.0.1:23120"
    service.writes_enabled = True
    client.fail = "not_saved"
    save(setup)
    server_id = client.server_id
    assert json.loads((service.private_root / "last-connection.json").read_text()) == {"base_url": client.base_url, "server_id": server_id}
    client.server_id = None
    monkeypatch.setattr(client, "status", lambda: {"available": False, "authorized": False})
    fresh = restarted(setup)
    result = recover(fresh)
    assert result["offline"] and result["records"][0]["drafts"][0]["text"] == "A synthetic finding"
    assert client.server_id is None
    client.base_url = client._allowed_origin = "http://127.0.0.1:23121"
    with pytest.raises(ValueError, match="profile once"):
        recover(fresh)


def test_retain_only_journals_latest_reader_draft_without_remote_calls(setup, monkeypatch):
    client, service = setup[-2:]
    service.writes_enabled = True
    service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})
    original_status = client.status
    original_item, original_pdf = client.item, client.pdf_file
    monkeypatch.setattr(client, "status", lambda: (_ for _ in ()).throw(AssertionError("retain-only must not authorize or read status")))
    monkeypatch.setattr(client, "item", lambda *args: (_ for _ in ()).throw(AssertionError("retain-only must not fetch an annotation")))
    monkeypatch.setattr(client, "pdf_file", lambda *args: (_ for _ in ()).throw(AssertionError("retain-only must reuse the reader binding")))
    data = {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "retain-latest-comment", "annotations": [{**draft(), "comment": "The complete newer user comment"}]}
    result = service.handle("annotation-retain", data)
    assert result["retained"] and result["retain_only"] and result["resolved"] is False
    assert result["pending"] == [] and result["pending_receipt_ids"] and client.writes == 0
    assert service.handle("annotation-retain", data) == result
    monkeypatch.setattr(client, "status", original_status)
    monkeypatch.setattr(client, "item", original_item)
    monkeypatch.setattr(client, "pdf_file", original_pdf)
    fresh = restarted(setup)
    row = recover(fresh)["records"][0]
    assert row["drafts"][0]["comment"] == "The complete newer user comment"
    assert row["durable"] and row["status"] == "waiting_to_send"
    assert fresh._queue().pending() == [] and client.writes == 0


def test_retaining_newer_comment_does_not_replace_or_retry_unknown_first_save(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    save(setup, operation="first-in-flight", annotation={**draft(), "comment": "F"})
    uncertain = copy.deepcopy(service._queue().pending()[0])
    response = service.handle("annotation-retain", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "retain-newer-input",
        "annotations": [{**draft(), "comment": "Full text typed while the first request was in flight"}]})
    assert response["retained"] and response["resolved"] is False and client.writes == 1
    assert service._queue().pending()[0] == uncertain
    rows = recover(restarted(setup))["records"]
    assert len(rows) == 2
    assert {row["drafts"][0]["comment"] for row in rows} == {"F", "Full text typed while the first request was in flight"}
    assert client.writes == 1


def test_retain_only_existing_deletion_is_durable_without_native_calls(setup, monkeypatch):
    client, service = setup[-2:]
    service.writes_enabled = True
    saved = save(setup)["annotations"][0]
    service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})
    with monkeypatch.context() as patch:
        patch.setattr(client, "status", lambda: (_ for _ in ()).throw(AssertionError("retained deletion called status")))
        patch.setattr(client, "item", lambda *args: (_ for _ in ()).throw(AssertionError("retained deletion fetched remote data")))
        patch.setattr(client, "pdf_file", lambda *args: (_ for _ in ()).throw(AssertionError("retained deletion fetched the PDF")))
        result = service.handle("annotation-retain", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "retain-delete-known",
            "delete": True, "annotations": [{"id": saved["id"], "version": saved["version"]}]})
    assert result["retained"] and result["retain_only"] and not result["resolved"]
    row = next(row for row in recover(restarted(setup))["records"] if row["request_id"] == "retain-delete-known")
    assert row["delete"] and row["drafts"] == [{"id": saved["id"], "version": saved["version"]}]
    assert row["status"] == "waiting_to_send" and client.writes == 1 and saved["id"] in client.records


def test_retained_zero_version_deletion_intent_never_dispatches(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.fail = "lost_ack"
    save(setup, operation="creation-unknown")
    original = copy.deepcopy(service._queue().pending()[0])
    result = service.handle("annotation-retain", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "retain-delete-unknown",
        "delete": True, "annotations": [{"id": "ANNAT222", "version": 0}]})
    assert result["retained"] and not result["resolved"] and client.writes == 1
    assert service._queue().pending()[0] == original
    fresh = restarted(setup)
    row = next(row for row in recover(fresh)["records"] if row["request_id"] == "retain-delete-unknown")
    assert row["delete"] and row["drafts"][0]["version"] == 0 and row["status"] == "conflict"
    assert not row["actions"]["retry"]
    retried = fresh.handle("annotation-retry", receipt_context(row))
    assert not retried["resolved"] and retried.get("retry_blocked") and client.writes == 1
    assert "ANNAT222" in client.records


def test_same_comment_save_is_confirmed_locally_without_native_write(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    prior = save(setup, annotation={**draft(), "comment": "E"})["annotations"][0]
    result = save(setup, operation="same-comment-noop", annotation={**prior, "comment": " E "})
    assert result["annotations"][0]["comment"] == "E" and result["annotations"][0]["version"] == prior["version"]
    assert result["pending"] == [] and client.writes == 1
    settled = service._queue().lookup("users/0", "PDFKEYAA", prior["id"])
    assert settled["state"] == "confirmed" and settled["attempt_id"] is None
    assert settled["observed"]["no_change"] and "no_change" in settled["events"]
    changed = save(setup, operation="meaningful-comment", annotation={**result["annotations"][0], "comment": "Edited"})
    assert changed["annotations"][0]["version"] == prior["version"] + 1 and client.writes == 2


def test_explicit_retry_of_retained_exact_noop_skips_dispatch(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    prior = save(setup, annotation={**draft(), "comment": "E"})["annotations"][0]
    service.handle("annotation-retain", {"scope": "users/0", "attachment_key": "PDFKEYAA", "operation_id": "retained-same-comment",
        "annotations": [prior]})
    row = next(row for row in recover(service)["records"] if row["request_id"] == "retained-same-comment")
    result = service.handle("annotation-retry", receipt_context(row))
    assert result["resolved"] and result["retried"] == 0 and client.writes == 1
    assert result["annotations"][0]["version"] == prior["version"]


def test_authoritative_attachment_readonly_blocks_tools_and_native_dispatch(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.attachment_read_only = lambda scope, key: True
    reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    assert reader["readOnly"] and reader["attachment_read_only"]
    assert "read-only" in reader["readOnlyReason"]
    failed = save(setup)
    assert failed["retained"] and failed["resolved"] is False and client.writes == 0
    row = recover(service)["records"][0]
    checked = service.handle("annotation-reconcile", receipt_context(row))
    assert checked["readOnly"] and checked["permission_read_only"]
    retried = service.handle("annotation-retry", receipt_context(row))
    assert retried["retried"] == 0 and retried["readOnly"] and client.writes == 0


def test_attachment_permission_change_updates_open_reader_and_denial_survives_absence(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    permission = [False]
    client.attachment_read_only = lambda scope, key: permission[0]
    reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    assert not reader["readOnly"]
    permission[0] = True
    refreshed = service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert refreshed["readOnly"] and refreshed["attachment_read_only"] and refreshed["permission_read_only"]
    permission[0] = None  # uninstalling a companion must not clear a known denial
    fresh = restarted(setup)
    assert fresh.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["readOnly"]
    permission[0] = False
    granted = fresh.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert not granted["readOnly"] and not granted["attachment_read_only"]


def test_missing_companion_keeps_personal_compatibility_but_groups_readonly(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.attachment_read_only = lambda scope, key: None
    assert not service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]["readOnly"]
    group = service.handle("open-pdf", {"scope": "groups/42", "attachment_key": "PDFKEYAA"})["reader"]
    assert group["readOnly"] and "Group" in group["readOnlyReason"]
    client.attachment_read_only = lambda scope, key: False
    assert not service.handle("annotation-refresh", {"scope": "groups/42", "attachment_key": "PDFKEYAA"})["readOnly"]


def test_unverified_attachment_permission_error_locks_tools(setup):
    client, service = setup[-2:]
    service.writes_enabled = True
    client.attachment_read_only = lambda *args: (_ for _ in ()).throw(OSError("Companion disconnected"))
    reader = service.handle("open-pdf", {"attachment_key": "PDFKEYAA"})["reader"]
    assert reader["readOnly"] and "could not be verified" in reader["readOnlyReason"]
    refreshed = service.handle("annotation-refresh", {"attachment_key": "PDFKEYAA"})
    assert refreshed["readOnly"] and refreshed["permission_read_only"]
