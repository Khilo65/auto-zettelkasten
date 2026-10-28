from __future__ import annotations

import json
import socket
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from auto_zettelkasten.zotero_workspace import ZoteroWorkspaceClient, ZoteroWorkspaceError, ZoteroWriteUncertain


def record(key="PARNT234", *, version=1, **fields):
    return {"key": key, "version": version, "data": {"key": key, "version": version, "itemType": "journalArticle", "title": "A source", **fields}}


@pytest.fixture
def server(tmp_path):
    state = {
        "items": {"PARNT234": record()}, "collections": {}, "version": 1,
        "server_id": "SERVER000001", "schema": 44, "calls": [], "revoked": False,
        "pdf_url": "", "drop_write": False, "change_after_page": False, "redirect": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, value=None, *, total=None, version=None, text=False):
            payload = (str(value).encode() if text else json.dumps(value).encode()) if value is not None else b""
            self.send_response(status)
            self.send_header("Zotero-API-Version", "3")
            self.send_header("Zotero-Schema-Version", str(state["schema"]))
            self.send_header("Zotero-Server-ID", state["server_id"])
            if total is not None:
                self.send_header("Total-Results", str(total))
            if version is not None:
                self.send_header("Last-Modified-Version", str(version))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            state["calls"].append(("GET", self.path, dict(self.headers), None))
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/leak")
                self.end_headers()
                return
            if path == "/api/":
                self.reply(200, "Local Zotero", text=True)
                return
            if self.headers.get("Zotero-Server-ID") != state["server_id"]:
                self.reply(412)
                return
            if path == "/api/users/0/groups":
                self.reply(200, [{"id": 123, "data": {"id": 123, "name": "Test group"}}], total=1)
                return
            pieces = path.removeprefix("/api/").split("/")
            if len(pieces) < 3 or pieces[2] not in {"items", "collections"}:
                self.reply(404)
                return
            kind = pieces[2]
            entries = state[kind]
            if len(pieces) >= 5 and pieces[4:] == ["file", "view", "url"]:
                self.reply(200, state["pdf_url"], text=True)
                return
            if len(pieces) == 4 and pieces[3] != "top":
                row = entries.get(pieces[3])
                self.reply(200 if row else 404, row)
                return
            rows = list(entries.values())
            if pieces[-1] == "children":
                # Native local API requires explicit itemType=annotation for
                # annotation children; the unfiltered route omits them.
                rows = [r for r in rows if r["data"].get("parentItem") == pieces[-2]
                        and (query.get("itemType") == ["annotation"] or r["data"].get("itemType") != "annotation")]
            elif pieces[-1] == "top":
                rows = [r for r in rows if not r["data"].get("parentItem")]
            if query.get("itemType") == ["annotation"]:
                rows = [r for r in rows if r["data"].get("itemType") == "annotation"]
            start = int(query.get("start", [0])[0])
            limit = int(query.get("limit", [64])[0])
            page = rows[start:start + limit]
            if query.get("format") == ["versions"]:
                page = {row["key"]: row["version"] for row in page}
            self.reply(200, page, total=len(rows), version=state["version"])
            if state["change_after_page"] and limit > 1:
                state["version"] += 1

        def do_POST(self):
            self.write("POST")

        def do_PATCH(self):
            self.write("PATCH")

        def do_DELETE(self):
            self.write("DELETE")

        def write(self, method):
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length)) if length else None
            state["calls"].append((method, self.path, dict(self.headers), data))
            if self.headers.get("Zotero-Server-ID") != state["server_id"]:
                self.reply(412)
                return
            if self.path == "/api/local/authorize":
                self.reply(200, {"key": "K" * 32, "remember": True})
                return
            if state["revoked"] or self.headers.get("Zotero-API-Key") != "K" * 32:
                self.reply(401)
                return
            pieces = self.path.removeprefix("/api/").split("/")
            kind = pieces[2]
            entries = state[kind]
            if method == "POST":
                new = data[0]
                key = new["key"]
                if key in entries:
                    self.reply(200, {"failed": {"0": {"code": 412}}})
                    return
                state["version"] += 1
                entries[key] = record(key, version=state["version"], **{k: v for k, v in new.items() if k not in {"key", "version"}})
            else:
                key = pieces[3]
                current = entries.get(key)
                expected = int(self.headers["If-Unmodified-Since-Version"]) if method == "DELETE" else data["version"]
                if current is None or current["data"]["version"] != expected:
                    self.reply(412)
                    return
                state["version"] += 1
                if method == "DELETE":
                    del entries[key]
                else:
                    current["data"].update(data)
                    current["data"]["version"] = current["version"] = state["version"]
            if method != "DELETE":
                saved = entries[key]["data"]
                for field in ("title", "annotationText", "annotationComment", "annotationPageLabel", "annotationSortIndex"):
                    if field in saved:
                        saved[field] = unicodedata.normalize("NFC", saved[field].strip())
                if "tags" in saved:
                    saved["tags"] = sorted([{"tag": unicodedata.normalize("NFC", tag["tag"].strip()), **({"type": 1} if tag.get("type") else {})} for tag in saved["tags"]], key=lambda tag: tag["tag"])
                if "collections" in saved:
                    saved["collections"] = sorted(saved["collections"])
            if state["drop_write"]:
                state["drop_write"] = False
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply(200 if method == "POST" else 204, {"successful": {"0": entries[key]}, "failed": {}} if method == "POST" else None)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = ZoteroWorkspaceClient(f"http://127.0.0.1:{httpd.server_port}", credential_path=tmp_path / "private" / "zotero.json")
    try:
        yield client, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def authorize(client):
    result = client.authorize("Synthetic test")
    assert result["authorized"]
    assert "key" not in result


def test_status_and_server_bound_private_authorization(server):
    client, state = server
    assert client.status()["write_supported"]
    assert not client.status()["authorized"]
    authorize(client)
    assert client.credential_path.stat().st_mode & 0o777 == 0o600
    assert client.credential_path.parent.stat().st_mode & 0o777 == 0o700
    persisted = ZoteroWorkspaceClient(client.base_url, credential_path=client.credential_path)
    assert persisted.status()["authorized"]
    assert all("K" * 32 not in call[1] for call in state["calls"])
    state["server_id"] = "SERVER000002"
    assert client.status()["code"] == "server_changed"
    assert not client.credential_path.exists()


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1/api", "http://user:secret@127.0.0.1", "http://127.0.0.1?key=secret", "file:///tmp"])
def test_only_loopback_origin(url):
    with pytest.raises(ValueError):
        ZoteroWorkspaceClient(url)


def test_scopes_keys_and_pages_are_bounded(server):
    client, state = server
    for scope in ("users/1", "groups/0", "groups/1/../../", "http://example.com"):
        with pytest.raises(ValueError):
            client.items_page(scope)
    for options in ({"start": -1}, {"limit": True}, {"limit": 65}, {"query": "x" * 513}):
        with pytest.raises(ValueError):
            client.items_page(**options)
    with pytest.raises(ValueError):
        client.item("users/0", "../bad")
    assert state["calls"] == []
    assert client.items_page()["total"] == 1
    assert client.libraries()[1]["scope"] == "groups/123"


def test_schema_gate_and_no_implicit_authorization(server):
    client, state = server
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.update_item("users/0", "PARNT234", {"title": "New"}, expected_version=1, write_token="update-1")
    assert error.value.code == "authorization_required"
    assert not any(method != "GET" for method, *_ in state["calls"])
    state["schema"] = 45
    assert not client.status()["write_supported"]
    with pytest.raises(ZoteroWorkspaceError, match="unverified"):
        client.authorize()


def test_metadata_version_and_identity_protection(server):
    client, state = server
    authorize(client)
    updated = client.update_item("users/0", "PARNT234", {"title": "New title", "tags": [{"tag": "example"}], "collections": []}, expected_version=1, write_token="edit-1")
    assert updated["data"]["title"] == "New title"
    patch = next(call for call in state["calls"] if call[0] == "PATCH")
    assert patch[3]["version"] == 1
    assert patch[2]["Zotero-Write-Token"] == "edit-1"
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.update_item("users/0", "PARNT234", {"title": "Overwrite"}, expected_version=1, write_token="edit-2")
    assert error.value.code == "version_conflict"
    for changes in ({"itemType": "book"}, {"key": "THER2345"}, {"parentItem": "THER2345"}, {"tags": ["bad"]}):
        with pytest.raises(ValueError):
            client.update_item("users/0", "PARNT234", changes, expected_version=2, write_token="edit-3")
    assert len([c for c in state["calls"] if c[0] == "PATCH"]) == 1


def test_revocation_removes_saved_key_without_retry(server):
    client, state = server
    authorize(client)
    state["revoked"] = True
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.update_item("users/0", "PARNT234", {"title": "New title"}, expected_version=1, write_token="revoked")
    assert error.value.code == "authorization_required"
    assert not client.credential_path.exists()
    assert len([c for c in state["calls"] if c[0] == "PATCH"]) == 1


def test_collection_create_and_rename(server):
    client, state = server
    authorize(client)
    created = client.create_collection("users/0", key="CLLECT23", name="A", write_token="create-1")
    same = client.create_collection("users/0", key="CLLECT23", name="A", write_token="create-1")
    assert created == same
    renamed = client.rename_collection("users/0", "CLLECT23", "B", expected_version=created["version"], write_token="rename-1")
    assert renamed["data"]["name"] == "B"
    assert client.collections()["records"][0]["data"]["name"] == "B"
    assert len([c for c in state["calls"] if c[0] == "POST" and c[1].endswith("collections")]) == 1


def test_linked_note_idempotent_after_lost_ack(server, tmp_path):
    client, state = server
    authorize(client)
    path = tmp_path / "note.md"
    path.write_text("Personal words", encoding="utf-8")
    state["drop_write"] = True
    with pytest.raises(ZoteroWriteUncertain):
        client.register_linked_note("users/0", "PARNT234", path, key="LINKED23", title="Atomic note", write_token="link-1")
    resumed = ZoteroWorkspaceClient(client.base_url, credential_path=client.credential_path)
    saved = resumed.register_linked_note("users/0", "PARNT234", path, key="LINKED23", title="Atomic note", write_token="link-1")
    assert saved["data"]["path"] == str(path)
    assert path.read_text() == "Personal words"
    assert len([c for c in state["calls"] if c[0] == "POST" and c[1].endswith("items")]) == 1
    assert len(state["items"]) == 2


def test_group_linked_note_and_wrong_stable_key_rejected(server, tmp_path):
    client, state = server
    path = tmp_path / "note.md"
    path.write_text("Personal words")
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.register_linked_note("groups/123", "PARNT234", path, key="LINKED23", title="Note", write_token="link-1")
    assert error.value.code == "unsupported_linked_file"
    assert state["calls"] == []
    state["items"]["LINKED23"] = record("LINKED23")
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.register_linked_note("users/0", "PARNT234", path, key="LINKED23", title="Note", write_token="link-1")
    assert error.value.code == "identity_mismatch"
    assert not any(c[0] != "GET" for c in state["calls"])


def test_pdf_verified_without_whole_file_read(server, tmp_path, monkeypatch):
    client, state = server
    pdf = tmp_path / "A paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"0" * 4096)
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    state["pdf_url"] = pdf.as_uri()
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("Whole PDF must not be read"))
    verified = client.pdf_file("users/0", "PDFATT23")
    assert verified.path == pdf
    assert verified.parent_item == "PARNT234"
    assert verified.size == pdf.stat().st_size
    state["pdf_url"] = "file://remote-host/tmp/file.pdf"
    with pytest.raises(ZoteroWorkspaceError, match="local file"):
        client.pdf_file("users/0", "PDFATT23")
    state["pdf_url"] = "https://example.com/a.pdf"
    with pytest.raises(ZoteroWorkspaceError, match="local file"):
        client.pdf_file("users/0", "PDFATT23")


def test_complete_inventory_and_mutation_never_imply_deletion(server):
    client, state = server
    for number in range(65):
        key = f"I{number:07d}"
        state["items"][key] = record(key)
    inventory = client.inventory_versions()
    assert inventory["complete"] and len(inventory["versions"]) == 66
    assert all("format=versions" in c[1] for c in state["calls"] if "/items?" in c[1])
    state["change_after_page"] = True
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.inventory_versions()
    assert error.value.code == "incomplete_inventory"
    assert len(inventory["versions"]) == 66


def test_redirects_not_followed(server):
    client, state = server
    state["redirect"] = True
    assert client.status()["available"] is False
    assert len(state["calls"]) == 1


def annotation_payload():
    return {"annotationType": "highlight", "annotationText": "A claim", "annotationComment": "Check this",
            "annotationColor": "#ffcc00", "annotationPageLabel": "458", "annotationSortIndex": "00000|000001|00000",
            "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[1, 2, 3, 4]]}), "tags": []}


def test_annotation_roundtrip_and_bound_versioned_delete(server):
    client, state = server
    authorize(client)
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    saved = client.save_annotation("users/0", "PDFATT23", annotation_payload(), key="ANNTA234", expected_version=0, write_token="annot-1")
    assert client.children("users/0", "PDFATT23")["records"] == []
    assert client.annotations("users/0", "PDFATT23")[0] == saved
    assert any("itemType=annotation" in call[1] for call in state["calls"])
    result = client.delete_annotation("users/0", "PDFATT23", "ANNTA234", expected_version=saved["version"], write_token="delete-1")
    assert result["deleted"]
    deletion = next(c for c in state["calls"] if c[0] == "DELETE")
    assert deletion[2]["If-Unmodified-Since-Version"] == str(saved["version"])
    assert client.annotations("users/0", "PDFATT23") == []


def test_native_annotation_inventory_is_complete_and_parent_bound(server):
    client, state = server
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    for index in range(140):
        key = f"AN{index:06d}"
        state["items"][key] = record(key, itemType="annotation", parentItem="PDFATT23" if index % 2 == 0 else "PDFATT24", **annotation_payload())
        state["items"][key]["library"] = {"type": "user", "id": 987654}
    annotations = client.annotations("users/0", "PDFATT23")
    assert len(annotations) == 70
    assert {row["data"]["parentItem"] for row in annotations} == {"PDFATT23"}
    assert any("start=64" in call[1] and "itemType=annotation" in call[1] for call in state["calls"])
    assert all("/items/PDFATT23/children?" in call[1] for call in state["calls"] if "itemType=annotation" in call[1])
    state["change_after_page"] = True
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.annotations("users/0", "PDFATT23")
    assert error.value.code == "incomplete_inventory"


def test_annotation_envelope_cannot_cross_group_identity(server):
    client, state = server
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    state["items"]["ANNTA234"] = record("ANNTA234", itemType="annotation", parentItem="PDFATT23", **annotation_payload())
    state["items"]["ANNTA234"]["library"] = {"type": "group", "id": 456}
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.annotations("groups/123", "PDFATT23")
    assert error.value.code == "identity_mismatch"


@pytest.mark.parametrize("key", ["ABCD0123", "ABCD1234", "ABCOD234"])
def test_invalid_native_object_keys_rejected_before_write(server, key):
    client, state = server
    with pytest.raises(ValueError, match="object-key alphabet"):
        client.create_collection("users/0", key=key, name="Synthetic", write_token="key-check")
    assert not state["calls"]


def test_annotation_external_and_parent_mismatch_not_written(server):
    client, state = server
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    state["items"]["ANNTA234"] = record("ANNTA234", itemType="annotation", parentItem="PDFATT23", annotationIsExternal=True)
    with pytest.raises(ZoteroWorkspaceError):
        client.save_annotation("users/0", "PDFATT23", annotation_payload(), key="ANNTA234", expected_version=1, write_token="annot-1")
    with pytest.raises(ValueError):
        client.save_annotation("users/0", "PDFATT23", {**annotation_payload(), "parentItem": "THERPDF2"}, key="ANNTA234", expected_version=1, write_token="annot-2")
    assert not any(c[0] != "GET" for c in state["calls"])


def test_annotation_missing_external_flag_requires_internal_attestation(server):
    client, state = server
    authorize(client)
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    state["items"]["ANNTA234"] = record("ANNTA234", itemType="annotation", parentItem="PDFATT23", **annotation_payload())
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.save_annotation("users/0", "PDFATT23", annotation_payload(), key="ANNTA234", expected_version=1, write_token="annot-1")
    assert error.value.code == "annotation_read_only"
    with pytest.raises(ZoteroWorkspaceError) as error:
        client.delete_annotation("users/0", "PDFATT23", "ANNTA234", expected_version=1, write_token="delete-1")
    assert error.value.code == "annotation_read_only"
    saved = client.save_annotation("users/0", "PDFATT23", annotation_payload(), key="ANNTA234", expected_version=1, write_token="annot-2", known_internal=True)
    assert saved["data"]["annotationText"] == "A claim"


def test_annotation_native_normalization_and_manual_tags_confirm_cleanly(server):
    client, state = server
    authorize(client)
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    payload = {**annotation_payload(), "annotationText": "  Cafe\u0301  ", "annotationComment": " Comment\n", "annotationColor": "#FFCCAA",
               "tags": [{"tag": " Zed ", "type": 0}, {"tag": " Alpha ", "type": 1}]}
    saved = client.save_annotation("users/0", "PDFATT23", payload, key="ANNTA234", expected_version=0, write_token="native-1")
    assert saved["data"]["annotationText"] == "Café"
    assert saved["data"]["annotationColor"] == "#ffccaa"
    assert saved["data"]["tags"] == [{"tag": "Alpha", "type": 1}, {"tag": "Zed"}]
    assert not client._matches(saved["data"], {"annotationText": "A different claim"})
    assert not client._matches(saved["data"], {"parentItem": "THERPDF2"})


@pytest.mark.parametrize("sort_index", ["", "00000|000001", "1|2|3", "00000|000001|abcde"])
def test_invalid_native_pdf_sort_index_never_dispatches(server, sort_index):
    client, state = server
    state["items"]["PDFATT23"] = record("PDFATT23", itemType="attachment", parentItem="PARNT234", linkMode="imported_file", contentType="application/pdf")
    with pytest.raises(ValueError, match="sort index"):
        client.save_annotation("users/0", "PDFATT23", {**annotation_payload(), "annotationSortIndex": sort_index}, key="ANNTA234", expected_version=0, write_token="native-1")
    assert not any(call[0] != "GET" for call in state["calls"])


def test_metadata_readback_uses_unordered_memberships_and_normalized_tags(server):
    client, state = server
    authorize(client)
    saved = client.update_item("users/0", "PARNT234", {"title": "  Cafe\u0301  ", "collections": ["CLLECT24", "CLLECT23"],
        "tags": [{"tag": "Z"}, {"tag": "A", "type": 1}]}, expected_version=1, write_token="native-1")
    assert saved["data"]["title"] == "Café"
    assert saved["data"]["collections"] == ["CLLECT23", "CLLECT24"]
    assert client._matches({"ISBN": "978-0-306-40615-7"}, {"ISBN": "9780306406157"})
    assert not client._matches({"ISBN": "978-0-306-40615-7"}, {"ISBN": "9780306406158"})


def test_credential_permissions_are_not_silently_widened_or_reused(server):
    client, _ = server
    authorize(client)
    client.credential_path.chmod(0o644)
    another = ZoteroWorkspaceClient(client.base_url, credential_path=client.credential_path)
    assert another.status()["code"] == "credential_storage"
    client.credential_path.chmod(0o600)
    stored = json.loads(client.credential_path.read_text())
    stored["server_id"] = "DIFFERENT001"
    client.credential_path.write_text(json.dumps(stored))
    assert not another.status()["authorized"]
