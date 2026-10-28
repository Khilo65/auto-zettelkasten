"""Independent HTTP integration checks; no running Zotero or real files needed."""
from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from auto_zettelkasten import webapp
from auto_zettelkasten.zotero_workspace import VerifiedPDF, ZoteroWorkspaceError


class OfflineWorkspaceClient:
    server_id = "TESTSERVER01"
    base_url = "http://127.0.0.1:23129"
    _allowed_origin = base_url
    expected_server_id = None

    def __init__(self, path):
        self.path = path
        self.calls = []

    def status(self):
        self.calls.append(("status",))
        return {"available": True, "authorized": False, "write_supported": True,
                "server_id": self.server_id, "api_version": 3, "schema_version": 44}

    def libraries(self):
        self.calls.append(("libraries",))
        return [{"scope": "users/0", "name": "Synthetic library", "linked_files_supported": True}]

    def item(self, scope, key):
        self.calls.append(("item", scope, key))
        if key == "PDFITEM1":
            return {"key": key, "version": 1, "data": {"key": key, "version": 1, "title": "Synthetic PDF",
                    "itemType": "attachment", "parentItem": "SOURCE01", "contentType": "application/pdf"}}
        return {"key": key, "version": 1, "data": {"key": key, "version": 1, "title": "Synthetic source", "itemType": "journalArticle"}}

    def pdf_file(self, scope, key):
        self.calls.append(("pdf_file", scope, key))
        assert scope == "users/0" and key == "PDFITEM1"
        info = self.path.stat()
        return VerifiedPDF(self.path, key, "SOURCE01", scope, self.server_id, 1, info.st_size, info.st_mtime_ns)

    def annotations(self, scope, key):
        self.calls.append(("annotations", scope, key))
        return []

    def attachment_read_only(self, scope, key):
        self.calls.append(("attachment_read_only", scope, key))
        return None

    def items_page(self, scope, **options):
        self.calls.append(("items_page", scope, options))
        return {"records": [self.item(scope, "SOURCE01")], "start": 0, "limit": 32,
                "total": 1, "next_start": None, "library_version": 1, "server_id": self.server_id}

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected Zotero operation: {name}")


@pytest.fixture
def unified_server(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 4)
    client = OfflineWorkspaceClient(pdf)
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json", zotero_client=client)
    server = webapp.LocalServer(("127.0.0.1", 0), app=app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, data=None, *, authenticated=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        outgoing = {"X-Session-Token": app.token} if authenticated else {}
        outgoing.update(headers or {})
        if data is not None:
            outgoing["Content-Type"] = "application/json"
        try:
            connection.request("POST" if data is not None else "GET", path,
                               json.dumps(data).encode() if data is not None else None, outgoing)
            response = connection.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw
            return response.status, body, dict(response.getheaders())
        finally:
            connection.close()

    try:
        yield app, client, pdf, request
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def open_pdf(request):
    status, result, _ = request("/api/zotero/open-pdf", {"scope": "users/0", "attachment_key": "PDFITEM1", "page": 1})
    assert status == 200, result
    assert result["reader"]["location"]["pageIndex"] == 0
    return result["reader"]


def test_unified_init_status_and_library_envelopes_are_offline(unified_server):
    app, client, _, request = unified_server
    assert client.calls == []
    status, result, _ = request("/api/zotero/status", {})
    assert status == 200 and result["connected"] and not result["writable"]
    assert not result["writes_enabled"]
    status, result, _ = request("/api/zotero/libraries", {})
    assert status == 200 and result["libraries"][0]["scope"] == "users/0"
    assert not result["libraries"][0]["writable"]
    status, result, _ = request("/api/zotero/items", {"scope": "users/0", "start": 0, "limit": 32})
    assert status == 200 and result["records"][0]["key"] == "SOURCE01"
    assert result["next_start"] is None and result["total"] == 1
    assert request("/api/zotero/authorize", {})[0] == 400
    assert request("/api/zotero/item-update", {"scope": "users/0", "key": "SOURCE01", "changes": {"title": "Cannot write"}, "expected_version": 1, "write_token": "write-1"})[0] == 400


def test_zotero_actions_require_current_session_and_same_origin(unified_server):
    _, client, _, request = unified_server
    for kwargs in ({"authenticated": False}, {"headers": {"X-Session-Token": "wrong"}},
                   {"headers": {"Origin": "https://remote.example"}}, {"headers": {"Sec-Fetch-Site": "cross-site"}},
                   {"headers": {"Host": "evil.example"}}):
        assert request("/api/zotero/status", {}, **kwargs)[0] == 403
    assert client.calls == []
    assert request("/api/zotero/items", {"scope": "groups/../../etc"})[0] == 400


@pytest.mark.parametrize("code,http_status", [("authorization_required", 401), ("version_conflict", 412)])
def test_zotero_transport_errors_retain_actionable_codes(unified_server, code, http_status):
    _, client, _, request = unified_server

    def fail(*_args, **_kwargs):
        raise ZoteroWorkspaceError("Synthetic Zotero failure.", code=code, status=http_status)

    client.items_page = fail
    status, result, _ = request("/api/zotero/items", {"scope": "users/0"})
    assert 400 <= status < 500
    assert result["code"] == code


def test_pdf_url_is_opaque_scoped_and_revoked_when_reader_closes(unified_server, tmp_path):
    app, client, pdf, request = unified_server
    reader = open_pdf(request)
    assert str(pdf) not in json.dumps(reader)
    assert request(reader["url"], authenticated=False)[1] == pdf.read_bytes()
    assert request(reader["url"], authenticated=False, headers={"Origin": "https://remote.example"})[0] == 403
    assert request(reader["url"], authenticated=False, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    other = webapp.LocalApp(preferences_path=tmp_path / "other" / "prefs.json", zotero_client=client)
    try:
        with pytest.raises(ValueError, match="expired"):
            other.zotero_workspace.pdf_capability(reader["url"].split("/")[-2])
    finally:
        other.documents.close()
    status, result, _ = request("/api/zotero/reader-close", {"reader_id": reader["id"]})
    assert status == 200 and result["closed"]
    assert request(reader["url"], authenticated=False)[0] == 409
    for route in ("/api/zotero/attachments/../../source.pdf/file", "/api/zotero/attachments/%2e%2e/file", "/api/zotero/attachments/" + "A" * 32 + "/file?path=/etc/passwd"):
        assert request(route, authenticated=False)[0] in {403, 409}


@pytest.mark.parametrize("value,expected", [("bytes=0-4", (0, 4)), ("bytes=10-", (10, None)), ("bytes=-7", (-7, None)), ("bytes=0-99999", (0, None))])
def test_pdf_bounded_and_suffix_byte_ranges(unified_server, value, expected):
    _, _, pdf, request = unified_server
    reader = open_pdf(request)
    status, body, headers = request(reader["url"], authenticated=False, headers={"Range": value})
    raw = pdf.read_bytes()
    start, end = expected
    assert status == 206
    assert body == raw[start:None if end is None else end + 1]
    assert int(headers["Content-Length"]) == len(body)
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Content-Range"].endswith(f"/{len(raw)}")


@pytest.mark.parametrize("value", ["bytes=", "bytes=-", "bytes=-0", "bytes=20-10", "bytes=999999-", "bytes=0-1,3-4", "items=0-1", "bytes=x-y"])
def test_invalid_pdf_ranges_are_rejected(unified_server, value):
    _, _, _, request = unified_server
    reader = open_pdf(request)
    assert request(reader["url"], authenticated=False, headers={"Range": value})[0] == 416


def test_changed_pdf_and_changed_server_revoke_capability(unified_server):
    _, client, pdf, request = unified_server
    reader = open_pdf(request)
    pdf.write_bytes(b"%PDF-1.7\nChanged")
    assert request(reader["url"], authenticated=False)[0] == 409
    reader = open_pdf(request)
    client.server_id = "NEWTSERVER01"
    assert request(reader["url"], authenticated=False)[0] == 409


def test_same_size_mtime_pdf_replacement_between_validation_and_open_is_rejected(unified_server, monkeypatch):
    _, _, pdf, request = unified_server
    reader = open_pdf(request)
    before = pdf.stat()
    replacement = pdf.with_name("replacement.pdf")
    replacement.write_bytes(b"X" * before.st_size)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    real_open = os.open
    raced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal raced
        if Path(path) == pdf and not raced:
            raced = True
            os.replace(replacement, pdf)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(webapp.os, "open", racing_open)
    status, body, _ = request(reader["url"], authenticated=False)
    assert raced
    assert status == 409, "The capability admitted replacement bytes after identity validation"
    assert not isinstance(body, bytes) or not body.startswith(b"XXX")


def test_static_reader_csp_and_path_restrictions(unified_server, tmp_path, monkeypatch):
    _, _, _, request = unified_server
    module = tmp_path / "module"
    root = module / "web" / "zotero-reader"
    root.mkdir(parents=True)
    (root / "reader.js").write_text("window.syntheticReader = true;")
    (module / "web" / "zotero-reader-host.html").write_text("<!doctype html><title>Reader</title>")
    monkeypatch.setattr(webapp, "__file__", str(module / "webapp.py"))
    status, body, headers = request("/zotero-reader/reader.js", authenticated=False)
    assert status == 200 and b"syntheticReader" in body
    policy = headers["Content-Security-Policy"]
    assert "script-src 'self' 'wasm-unsafe-eval'" in policy and "'unsafe-eval'" not in policy
    assert "frame-ancestors 'self'" in policy and "object-src 'none'" in policy
    assert request("/zotero-reader-host.html", authenticated=False)[0] == 200
    for route in ("/zotero-reader/../webapp.py", "/zotero-reader/%2e%2e/webapp.py", "/zotero-reader//reader.js", "/zotero-reader/./reader.js", "/zotero-reader/reader.js/../secret"):
        assert request(route, authenticated=False)[0] == 404
    external = tmp_path / "external.js"
    external.write_text("secret")
    (root / "linked.js").symlink_to(external)
    assert request("/zotero-reader/linked.js", authenticated=False)[0] == 404


def test_static_reader_rejects_symlinked_ancestor_directories(unified_server, tmp_path, monkeypatch):
    _, _, _, request = unified_server
    module = tmp_path / "module"
    root = module / "web" / "zotero-reader"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.js").write_text("private contents")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(webapp, "__file__", str(module / "webapp.py"))
    status, body, _ = request("/zotero-reader/linked/private.js", authenticated=False)
    assert status == 404, "A symlinked parent escaped the reader asset subtree"
    assert body != b"private contents"

@pytest.mark.parametrize('asset,marker', [('/workspace-annotations.js', b'createAnnotationPanel'), ('/workspace-annotations.css', b'annotation-change')])
def test_annotation_recovery_assets_are_served_by_the_real_http_handler(unified_server, asset, marker):
    _, _, _, request = unified_server
    status, body, headers = request(asset, authenticated=False)
    assert status == 200 and marker in body
    assert headers['Content-Type'].startswith(('text/', 'application/javascript'))
