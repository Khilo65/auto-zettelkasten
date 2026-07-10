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

    def _request(self, endpoint: str, *, method: str = "GET", body: bytes | None = None, api: bool = True):
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


def test_exact_item_lookup_is_read_only_and_404_is_none() -> None:
    client = StubClient({"users/0/items/ITEM": (_encoded({"key": "ITEM", "data": {"key": "ITEM"}}), {})})
    assert client.item("ITEM") == {"key": "ITEM", "data": {"key": "ITEM"}}
    assert client.item("MISSING") is None
    assert client.calls == [("GET", "users/0/items/ITEM"), ("GET", "users/0/items/MISSING")]


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
