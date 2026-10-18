import http.client
import json
import os
import threading

import pytest

from auto_zettelkasten import webapp


def test_workspace_paged_index_http_sessions_continuations_and_generation(local_editor, tmp_path):
    _, request = local_editor
    root = tmp_path / "paged-notes"
    root.mkdir()
    for number in range(70):
        (root / f"Note-{number}.md").write_text(f"# Note {number}\n[[Note-{(number + 1) % 70}]]\n")
    _, opened, _ = request("/api/workspace/open", {"path": str(root)})
    workspace_id = opened["workspace"]["id"]
    query = {"workspace_id": workspace_id, "paged": True}
    assert request("/api/workspace/index", query, authenticated=False)[0] == 403
    assert request("/api/workspace/index", query, origin="https://other.example")[0] == 403
    documents = []
    for _ in range(100):
        status, page, _ = request("/api/workspace/index", query)
        assert status == 200
        assert len(page["documents"]) <= 32
        documents.extend(page["documents"])
        if page["complete"]:
            break
        query["cursor"] = page["next_cursor"]
    else:
        pytest.fail("The paged index did not complete")
    assert len(documents) == 70 and not page["truncated"]
    assert request("/api/workspace/index", query)[1] == page
    query = {"workspace_id": workspace_id, "paged": True, "known_generation": page["generation"]}
    for _ in range(100):
        _, checked, _ = request("/api/workspace/index", query)
        assert checked["documents"] == []
        if checked["complete"]:
            break
        query["cursor"] = checked["next_cursor"]
    assert checked["not_modified"]
    (root / "Note-0.md").write_text("Changed externally\n")
    status, changed, _ = request("/api/workspace/index", {"workspace_id": workspace_id, "paged": True, "known_generation": page["generation"]})
    assert status == 409 and changed["code"] == "index_changed"


def test_workspace_workers_are_local_assets_under_existing_csp(local_editor):
    _, request = local_editor
    for worker in ["workspace-index-worker.js", "workspace-graph-worker.js"]:
        status, body, headers = request("/" + worker, authenticated=False)
        assert status == 200 and len(body) > 1000
        assert headers["Content-Type"].startswith("text/javascript")
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert "unsafe-eval" not in headers["Content-Security-Policy"]


@pytest.fixture
def local_editor(tmp_path):
    class OfflineClient:
        def __getattr__(self, name):
            raise AssertionError("Workspace access must not contact Zotero")

    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json", client=OfflineClient())
    server = webapp.LocalServer(("127.0.0.1", 0), app=app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(route, data=None, *, authenticated=True, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        headers = {"X-Session-Token": app.token} if authenticated else {}
        if origin:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request("POST" if data is not None else "GET", route,
                               body=json.dumps(data).encode() if data is not None else None, headers=headers)
            response = connection.getresponse()
            content = response.read()
            payload = json.loads(content) if response.getheader("Content-Type") == "application/json" else content
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    try:
        yield app, request
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.mark.skipif(os.name != "posix", reason="Safe writes require descriptor-relative filesystem support")
def test_offline_workspace_http_read_save_conflict_and_session(local_editor, tmp_path):
    app, request = local_editor
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "Ideas.md"
    note.write_bytes(b"\xef\xbb\xbf# Ideas\r\n\r\nOriginal\r\n")
    status, opened, _ = request("/api/workspace/open", {"path": str(root)})
    assert status == 200 and not app.auth["ready"]
    workspace_id = opened["workspace"]["id"]
    status, loaded, _ = request("/api/workspace/read", {"workspace_id": workspace_id, "path": "Ideas.md"})
    assert status == 200
    doc = loaded["document"]
    text = "# Ideas\n\n" + "A long local paragraph.\n" * 1500
    save = {"workspace_id": workspace_id, "document_id": doc["id"], "revision": doc["revision"], "text": text}
    status, saved, _ = request("/api/workspace/save", save)
    assert status == 200
    assert note.read_bytes() == b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
    note.write_text("External edit\n")
    save["revision"] = saved["document"]["revision"]
    status, conflict, _ = request("/api/workspace/save", save)
    assert status == 409 and conflict["code"] == "conflict"
    assert conflict["current"]["text"] == "External edit\n"
    assert note.read_text() == "External edit\n"
    assert request("/api/workspace/tree", {"workspace_id": workspace_id}, authenticated=False)[0] == 403
    assert request("/api/workspace/tree", {"workspace_id": workspace_id}, origin="https://other.example")[0] == 403


def test_workspace_http_rejects_invalid_and_private_paths(local_editor, tmp_path):
    _, request = local_editor
    root = tmp_path / "notes"
    root.mkdir()
    (root / ".codex").mkdir()
    (root / ".codex" / "Hidden.md").write_text("Private")
    _, opened, _ = request("/api/workspace/open", {"path": str(root)})
    workspace_id = opened["workspace"]["id"]
    assert opened["workspace"]["entries"] == []
    for path in ["../outside.md", ".codex/Hidden.md", "/etc/passwd", "missing.md"]:
        assert request("/api/workspace/read", {"workspace_id": workspace_id, "path": path})[0] == 400
    assert request("/api/workspace/create", {"workspace_id": workspace_id, "name": "../escape.md", "kind": "file"})[0] == 400
    assert request("/api/workspace/read", {"workspace_id": "old-session", "path": "missing.md"})[0] == 400


def test_workspace_reads_during_mapping_but_mutations_wait(local_editor, tmp_path):
    app, request = local_editor
    root = tmp_path / "notes"
    root.mkdir()
    (root / "Ideas.md").write_text("Original\n")
    app.run = {"status": "running"}
    _, opened, _ = request("/api/workspace/open", {"path": str(root)})
    workspace_id = opened["workspace"]["id"]
    status, loaded, _ = request("/api/workspace/read", {"workspace_id": workspace_id, "path": "Ideas.md"})
    assert status == 200
    doc = loaded["document"]
    status, error, _ = request("/api/workspace/save", {"workspace_id": workspace_id, "document_id": doc["id"], "revision": doc["revision"], "text": "Changed"})
    assert status == 400 and "current operation" in error["error"]
    assert (root / "Ideas.md").read_text() == "Original\n"


def test_workspace_assets_are_explicit_and_nonce_scoped(local_editor):
    app, request = local_editor
    for route in ["/workspace.js", "/workspace.css", "/workspace-vendor.js", "/workspace-licenses.txt"]:
        status, body, headers = request(route, authenticated=False)
        assert status == 200 and body
        policy = headers["Content-Security-Policy"]
        assert f"'nonce-{app.token}'" in policy
        assert "'unsafe-inline'" not in policy and "'unsafe-eval'" not in policy
        assert "img-src 'none'" in policy
    for route in ["/documents.py", "/package-lock.json", "/../AGENTS.md", "/.codex/"]:
        assert request(route)[0] == 404


def test_workspace_index_is_offline_read_only_and_session_protected(local_editor, tmp_path):
    app, request = local_editor
    root = tmp_path / "notes"
    root.mkdir()
    (root / "One.md").write_text("[[Two]]")
    (root / "Two.md").write_text("# Two")
    (root / "private").mkdir()
    (root / "private" / "Planning.md").write_text("Private research")
    _, opened, _ = request("/api/workspace/open", {"path": str(root)})
    workspace_id = opened["workspace"]["id"]
    app.run = {"status": "running"}
    status, snapshot, _ = request("/api/workspace/index", {"workspace_id": workspace_id})
    assert status == 200 and not app.auth["ready"]
    assert [row["path"] for row in snapshot["documents"]] == ["One.md", "Two.md"]
    assert snapshot["relations"] == [] and snapshot["warnings"] == []
    assert "Private research" not in json.dumps(snapshot)
    assert request("/api/workspace/index", {"workspace_id": workspace_id}, authenticated=False)[0] == 403
    assert request("/api/workspace/index", {"workspace_id": workspace_id}, origin="https://other.example")[0] == 403
    assert request("/api/workspace/index", {"workspace_id": "old-session"})[0] == 400


@pytest.mark.skipif(os.name != "posix", reason="Safe relocation requires descriptor-relative POSIX writes")
def test_workspace_relocation_preview_apply_large_repairs_and_session_guards(local_editor, tmp_path):
    app, request = local_editor
    root = tmp_path / "notes"
    root.mkdir()
    (root / "Target.md").write_text("# Authored title\n")
    source = ("[[Target]] " * 300).strip()
    (root / "Source.md").write_text(source)
    _, opened, _ = request("/api/workspace/open", {"path": str(root)})
    workspace_id = opened["workspace"]["id"]
    preview_body = {"workspace_id": workspace_id, "source": "Target.md", "destination": "Renamed.md"}
    assert request("/api/workspace/relocate-preview", preview_body, authenticated=False)[0] == 403
    assert request("/api/workspace/relocate-preview", preview_body, origin="https://other.example")[0] == 403
    status, preview, _ = request("/api/workspace/relocate-preview", preview_body)
    assert status == 200 and not app.auth["ready"]
    assert (root / "Target.md").exists() and not (root / "Renamed.md").exists()
    doc = next(row for row in preview["documents"] if row["path"] == "Source.md")
    edits = [{"from": offset + 2, "to": offset + 8, "before": "Target", "after": "Renamed"}
             for offset in range(0, len(source), len("[[Target]] "))]
    apply_body = {"workspace_id": workspace_id, "token": preview["token"], "repair": True,
                  "patches": [{"path": "Source.md", "revision": doc["revision"], "edits": edits}]}
    assert len(json.dumps(apply_body)) > 16384
    assert request("/api/workspace/relocate", apply_body, authenticated=False)[0] == 403
    status, result, _ = request("/api/workspace/relocate", apply_body)
    assert status == 200 and result["documents"]
    assert (root / "Renamed.md").read_text() == "# Authored title\n"
    assert (root / "Source.md").read_text() == source.replace("Target", "Renamed")
    assert request("/api/workspace/relocate", apply_body)[1] == result
    app.run = {"status": "running"}
    assert request("/api/workspace/relocate-preview", {**preview_body, "source": "Renamed.md", "destination": "Later.md"})[0] == 400
