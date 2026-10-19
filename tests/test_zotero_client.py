from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from auto_zettelkasten.zotero import ZoteroError, ZoteroLocalClient


class StubClient(ZoteroLocalClient):
    def __init__(self, responses: dict[str, tuple[bytes, Mapping[str, str]]]) -> None:
        super().__init__(page_size=2)
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def _request(self, endpoint: str, *, method: str = "GET", body: bytes | None = None, api: bool = True, max_bytes: int | None = None):
        self.calls.append((method, endpoint))
        if endpoint not in self.responses:
            raise ZoteroError(f"HTTP 404: {endpoint}")
        return self.responses[endpoint]


def _encoded(value: Any) -> bytes:
    return json.dumps(value).encode()


def test_collection_pagination_and_selected_key_shapes() -> None:
    responses = {
        "users/0/collections?limit=2&start=0": (_encoded([{"key": "A"}, {"key": "B"}]), {"Total-Results": "3"}),
        "users/0/collections?limit=2&start=2": (_encoded([{"key": "C"}]), {"Total-Results": "3"}),
        "connector/getSelectedCollection": (_encoded({"collectionKey": "SELECTED"}), {}),
        "users/0/collections/SELECTED/items/top?limit=2&start=0": (_encoded([{"key": "I1"}]), {"Total-Results": "1"}),
    }
    client = StubClient(responses)
    assert [row["key"] for row in client.collections()] == ["A", "B", "C"]
    assert client.selected_collection()["key"] == "SELECTED"
    assert client.inventory("selected") == [{"key": "I1"}]
    assert ("POST", "connector/getSelectedCollection") in client.calls


@pytest.mark.parametrize("payload", [{"collection_key": "A"}, {"selected_collection_key": "B"}, {"key": "C"}])
def test_selected_collection_accepts_connector_key_variants(payload: dict[str, str]) -> None:
    client = StubClient({"connector/getSelectedCollection": (_encoded(payload), {})})
    assert client.selected_collection()["key"] in {"A", "B", "C"}


def test_file_view_url_fallback_reads_local_file(tmp_path: Path) -> None:
    attachment = tmp_path / "paper.txt"
    attachment.write_text("local Zotero attachment")
    client = StubClient(
        {
            "users/0/items/ATT/file/view/url": (_encoded({"url": attachment.as_uri()}), {}),
        }
    )
    payload, content_type = client.file("ATT") or (b"", "")
    assert payload == b"local Zotero attachment"
    assert content_type == "text/plain"
    assert all(method == "GET" for method, endpoint in client.calls if "connector/" not in endpoint)


def test_real_connector_selected_shape_resolves_unique_api_collection_path() -> None:
    connector = {
        "libraryID": 1,
        "libraryName": "My Library",
        "id": 20,
        "name": "Selected Child",
        "targets": [
            {"id": "L1", "name": "My Library", "level": 0},
            {"id": "C10", "name": "Parent", "level": 1},
            {"id": "C20", "name": "Selected Child", "level": 2},
        ],
        "tags": {},
    }
    collections = [
        {"key": "PARENTKEY", "data": {"key": "PARENTKEY", "name": "Parent", "parentCollection": False}},
        {"key": "CHILDKEY", "data": {"key": "CHILDKEY", "name": "Selected Child", "parentCollection": "PARENTKEY"}},
    ]
    client = StubClient(
        {
            "connector/getSelectedCollection": (_encoded(connector), {}),
            "users/0/collections?limit=2&start=0": (_encoded(collections), {"Total-Results": "2"}),
        }
    )
    selected = client.selected_collection()
    assert selected["key"] == "CHILDKEY"
    assert selected["scope"] == "collection"


def test_library_root_is_not_silently_widened_to_selected_collection_scope() -> None:
    client = StubClient({"connector/getSelectedCollection": (_encoded({"id": None, "name": "My Library", "targets": []}), {})})
    with pytest.raises(ZoteroError, match="library root is selected"):
        client.selected_collection()


def test_non_loopback_zotero_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="loopback"):
        ZoteroLocalClient(base_url="https://example.com")


def test_inventory_page_is_one_request_with_continuation() -> None:
    endpoint = "users/0/collections/ONE%2FTWO/items/top?limit=2&start=4"
    client = StubClient({endpoint: (_encoded([{"key": "A"}, {"key": "B"}]), {"Total-Results": "9"})})
    page = client.inventory_page(collection_key="ONE/TWO", start=4, limit=2)
    assert page == {"records": [{"key": "A"}, {"key": "B"}], "start": 4, "limit": 2, "total": 9, "next_start": 6}
    assert client.calls == [("GET", endpoint)]


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 65}, {"limit": True}, {"start": -1}, {"start": False}, {"scope": "selected"}])
def test_inventory_page_rejects_unbounded_or_ambiguous_scope(options: dict[str, Any]) -> None:
    client = StubClient({})
    with pytest.raises(ValueError):
        client.inventory_page(collection_key="C", **options)
    assert client.calls == []


@pytest.mark.parametrize("payload", [{"records": []}, [None], [{}, {}]])
def test_inventory_page_rejects_bad_result(payload: Any) -> None:
    client = StubClient({"users/0/collections/C/items/top?limit=1&start=0": (_encoded(payload), {})})
    with pytest.raises(ZoteroError, match="page records"):
        client.inventory_page(collection_key="C", limit=1)


def test_bounded_file_fallback_checks_size_before_reading(tmp_path: Path) -> None:
    attachment = tmp_path / "large.pdf"
    attachment.write_bytes(b"x" * 20)
    client = StubClient({"users/0/items/ATT/file/view/url": (_encoded(attachment.as_uri()), {})})
    with pytest.raises(ZoteroError, match="byte limit"):
        client.file("ATT", max_bytes=10)


@pytest.mark.parametrize("header", [None, "100"])
def test_bounded_transport_limits_reads(monkeypatch: pytest.MonkeyPatch, header: str | None) -> None:
    reads: list[int] = []

    class Response:
        headers = {"Content-Length": header} if header else {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, count: int) -> bytes:
            reads.append(count)
            return b"x" * count

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ZoteroError, match="byte limit"):
        ZoteroLocalClient().file("ATT", max_bytes=10)
    assert reads == ([] if header else [11])


def test_children_optional_limit_uses_one_bounded_page() -> None:
    endpoint = "users/0/items/A/children?limit=2&start=0"
    client = StubClient({endpoint: (_encoded([{"key": "ATT"}]), {})})
    assert client.children("A", limit=2) == [{"key": "ATT"}]
    assert client.calls == [("GET", endpoint)]


def test_inventory_page_rejects_large_transport_before_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Length": str(3 * 1024 * 1024)}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            pytest.fail("large body read before size rejection")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ZoteroError, match="byte limit"):
        ZoteroLocalClient().inventory_page(collection_key="C")
