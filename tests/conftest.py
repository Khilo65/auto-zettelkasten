from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest


SECTION_KEYS = (
    "thesis",
    "method_and_research_design",
    "evidence_and_data",
    "detailed_findings",
    "plain_english_interpretation",
    "strengths_and_contributions",
    "methodological_critique",
    "limitations",
    "what_this_source_can_support",
    "what_this_source_cannot_support",
    "locators",
)


class FakeReader:
    name = "fake-reader"
    model = "fake-1"
    is_cloud = False

    def __init__(self) -> None:
        self.calls = 0

    def read_source(self, text: str, metadata: Mapping[str, Any], question: str | None = None) -> Mapping[str, Any]:
        self.calls += 1
        title = str(metadata.get("title") or "untitled")
        return {key: f"Source-grounded {key.replace('_', ' ')} for {title}; see page 1." for key in SECTION_KEYS}


class FakeZotero:
    def __init__(self, items: list[dict[str, Any]], *, selected_key: str = "COLL1", missing: set[str] | None = None) -> None:
        self.items = deepcopy(items)
        self.selected_key = selected_key
        self.missing = missing or set()
        self.inventory_calls: list[tuple[str, str | None]] = []
        self.children_calls = 0
        self.fulltext_calls = 0
        self.file_calls = 0

    def status(self) -> Mapping[str, Any]:
        return {"status": "available", "read_only": True, "base_url": "fake://zotero"}

    def collections(self) -> list[dict[str, Any]]:
        return [{"key": "COLL1", "data": {"key": "COLL1", "name": "Synthetic Collection"}}]

    def selected_collection(self) -> Mapping[str, Any]:
        return {"key": self.selected_key, "name": "Selected Synthetic Collection"}

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]:
        self.inventory_calls.append((scope, collection_key))
        return deepcopy(self.items)

    def children(self, item_key: str) -> list[dict[str, Any]]:
        self.children_calls += 1
        if item_key in self.missing:
            return []
        return [
            {
                "key": f"{item_key}PDF",
                "data": {
                    "key": f"{item_key}PDF",
                    "parentItem": item_key,
                    "itemType": "attachment",
                    "contentType": "text/plain",
                    "filename": f"{item_key}.txt",
                },
            }
        ]

    def fulltext(self, item_key: str) -> Mapping[str, Any] | None:
        self.fulltext_calls += 1
        parent = item_key.removesuffix("PDF")
        if parent in self.missing or not item_key.endswith("PDF"):
            return None
        return {"content": (f"Inspected synthetic document content for {parent}. " * 30).strip(), "contentType": "text/plain"}

    def file(self, item_key: str) -> tuple[bytes, str] | None:
        self.file_calls += 1
        return None


@pytest.fixture
def sample_items() -> list[dict[str, Any]]:
    return [
        {
            "key": "ITEMA",
            "data": {
                "key": "ITEMA",
                "itemType": "journalArticle",
                "title": "Institutions and Reform",
                "date": "2024",
                "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "One"}],
                "tags": [{"tag": "Shared Topic"}, {"tag": "Exact Tag CASE"}],
                "relations": {"dc:references": "http://zotero.org/users/local/items/ITEMB"},
            },
        },
        {
            "key": "ITEMB",
            "data": {
                "key": "ITEMB",
                "itemType": "journalArticle",
                "title": "Institutions in Practice",
                "date": "2022",
                "creators": [{"creatorType": "author", "firstName": "Bea", "lastName": "Two"}],
                "tags": [{"tag": "Shared Topic"}],
            },
        },
    ]
