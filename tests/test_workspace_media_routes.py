"""Synthetic HTTP checks for workspace-bound image capabilities."""
from __future__ import annotations

import binascii
from io import BytesIO
import http.client
import json
import struct
import threading
from urllib.parse import urlencode

from PIL import Image
import pytest

from auto_zettelkasten import documents, webapp
from auto_zettelkasten.workspace_media import MAX_IMAGE_BYTES


pytestmark = pytest.mark.skipif(not documents._SAFE_WRITES, reason="Confined image imports require directory handles")


def raster(color="purple"):
    output = BytesIO()
    Image.new("RGB", (20, 10), color).save(output, format="PNG")
    return output.getvalue()


class NoExternalCalls:
    def status(self):
        pytest.fail("Workspace image operations must not contact Zotero")


class MediaHTTP:
    def __init__(self, app, server, root, workspace):
        self.app, self.server, self.root, self.workspace = app, server, root, workspace

    def request(self, method, path, body=None, *, session=True, headers=None):
        values = {"X-Session-Token": self.app.token} if session else {}
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, data, **options):
        headers = {"Content-Type": "application/json", **options.pop("headers", {})}
        status, response_headers, body = self.request("POST", path, json.dumps(data), headers=headers, **options)
        return status, response_headers, json.loads(body)

    def import_image(self, payload=None, *, filename="日本 語.png", source_path="Nested/Note.md", **options):
        query = urlencode({"workspace_id": self.workspace["id"], "source_path": source_path, "filename": filename})
        status, headers, body = self.request("POST", "/api/workspace/import-image?" + query,
                                             raster() if payload is None else payload, **options)
        return status, headers, json.loads(body)


@pytest.fixture
def media_http(tmp_path):
    root = tmp_path / "workspace"
    (root / "Nested").mkdir(parents=True)
    (root / "Nested/Note.md").write_text("# Synthetic note\n\nPreserve this prose.\n", encoding="utf-8")
    app = webapp.LocalApp(preferences_path=tmp_path / "private/preferences.json",
                          client=NoExternalCalls(), zotero_client=NoExternalCalls())
    workspace = app.handle("POST", "/api/workspace/open", {"path": str(root)})["workspace"]
    server = webapp.LocalServer(("127.0.0.1", 0), app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield MediaHTTP(app, server, root, workspace)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_raw_import_resolve_capability_and_note_index_separation(media_http):
    http = media_http
    original = (http.root / "Nested/Note.md").read_bytes()
    status, _, imported = http.import_image()
    assert status == 200
    asset = imported["asset"]
    assert asset["path"].startswith("attachments/images/")
    assert asset["relative_path"].startswith("../attachments/images/")
    assert asset["mime"] == "image/png"
    assert (asset["width"], asset["height"]) == (20, 10)
    assert (http.root / "Nested/Note.md").read_bytes() == original
    assert next(row for row in imported["workspace"]["entries"] if row["path"] == asset["path"])["kind"] == "asset"

    status, _, resolved = http.post("/api/workspace/media-resolve", {
        "workspace_id": http.workspace["id"], "source_path": "Nested/Note.md", "target": asset["relative_path"],
    })
    assert status == 200
    assert resolved["asset"]["url"] == asset["url"]
    assert "path=" not in asset["url"] and "token=" not in asset["url"]
    status, headers, body = http.request("GET", asset["url"], session=False)
    assert status == 200 and body == raster()
    assert headers["Content-Type"] == "image/png"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "img-src 'self'" in headers["Content-Security-Policy"]

    index = http.app.documents.index(http.workspace["id"])
    assert [row["path"] for row in index["documents"]] == ["Nested/Note.md"]
    corpus = http.app.documents.recall_corpus(http.workspace["id"])
    assert [row["path"] for row in corpus["documents"]] == ["Nested/Note.md"]


def test_import_requires_session_and_media_capability_preserves_loopback_protections(media_http):
    http = media_http
    assert http.import_image(session=False)[0] == 403
    status, _, result = http.import_image()
    assert status == 200
    url = result["asset"]["url"]
    for headers in ({"Origin": "https://foreign.example"}, {"Host": "foreign.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert http.request("GET", url, session=False, headers=headers)[0] == 403
    assert http.request("GET", "/api/workspace/media/" + "a" * 43, session=False)[0] == 400
    assert http.request("GET", "/api/workspace/media/" + result["asset"]["revision"], session=False)[0] == 400
    assert http.request("GET", url, session=False)[0] == 200


def test_file_tree_image_can_open_before_any_note_is_opened(media_http):
    http = media_http
    (http.root / "Existing image.png").write_bytes(raster())
    assert http.app.documents._documents == {}
    payload = {"workspace_id": http.workspace["id"], "path": "Existing image.png"}
    assert http.post("/api/workspace/media-open", payload, session=False)[0] == 403
    status, _, response = http.post("/api/workspace/media-open", payload)
    assert status == 200 and response["asset"]["path"] == "Existing image.png"
    assert http.request("GET", response["asset"]["url"], session=False)[0] == 200
    assert http.app.documents._documents == {}
    assert http.post("/api/workspace/media-open", {**payload, "source_path": "Nested/Note.md"})[0] == 400


def test_capability_rejects_replaced_image_and_workspace_switch(media_http, tmp_path):
    http = media_http
    status, _, result = http.import_image()
    assert status == 200
    asset = result["asset"]
    (http.root / asset["path"]).write_bytes(raster("green"))
    assert http.request("GET", asset["url"], session=False)[0] in {400, 409}
    status, _, updated = http.post("/api/workspace/media-resolve", {
        "workspace_id": http.workspace["id"], "source_path": "Nested/Note.md", "target": asset["relative_path"],
    })
    assert status == 200 and updated["asset"]["url"] != asset["url"]
    other = tmp_path / "other"
    other.mkdir()
    (other / "Other.md").write_text("Other note\n", encoding="utf-8")
    assert http.post("/api/workspace/open", {"path": str(other)})[0] == 200
    assert http.request("GET", updated["asset"]["url"], session=False)[0] == 400
    assert http.post("/api/workspace/media-resolve", {
        "workspace_id": http.workspace["id"], "source_path": "Nested/Note.md", "target": asset["relative_path"],
    })[0] == 400


def test_resolve_rejects_remote_escape_and_symlink_paths(media_http, tmp_path):
    http = media_http
    outside = tmp_path / "outside.png"
    outside.write_bytes(raster())
    (http.root / "shortcut.png").symlink_to(outside)
    for target in ("../../outside.png", "%2e%2e/%2e%2e/outside.png", "shortcut.png", str(outside), "https://foreign.example/image.png", "file://" + str(outside)):
        status, _, _ = http.post("/api/workspace/media-resolve", {
            "workspace_id": http.workspace["id"], "source_path": "Nested/Note.md", "target": target,
        })
        assert status == 400, target
    assert http.post("/api/workspace/media-resolve", {
        "workspace_id": http.workspace["id"], "source_path": "Nested/Note.md", "target": "shortcut.png", "wiki": "true",
    })[0] == 400


def test_raw_import_rejects_invalid_content_query_bytes_and_pixel_count(media_http):
    http = media_http
    original = (http.root / "Nested/Note.md").read_bytes()
    for payload, filename in ((b"not a raster", "broken.png"), (raster(), "mismatch.jpg"), (raster(), "unsupported.svg")):
        assert http.import_image(payload, filename=filename)[0] == 400
    assert http.import_image(headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)})[0] == 400
    assert http.request("POST", "/api/workspace/import-image?workspace_id=x&source_path=Note.md&filename=a.png&filename=b.png", raster())[0] == 400
    assert http.request("POST", "/api/workspace/import-image?workspace_id=x&source_path=Note.md", raster())[0] == 400

    # Reject dimensions before decoding a potentially huge bitmap. Only the
    # tiny PNG header changes; no large allocation is required for this test.
    oversized = bytearray(raster())
    oversized[16:24] = struct.pack(">II", 8000, 6000)
    oversized[29:33] = struct.pack(">I", binascii.crc32(oversized[12:29]) & 0xFFFFFFFF)
    assert http.import_image(bytes(oversized), filename="oversized.png")[0] == 400
    assert (http.root / "Nested/Note.md").read_bytes() == original
    assert not (http.root / "attachments").exists()
