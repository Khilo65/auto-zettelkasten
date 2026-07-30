from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from auto_zettelkasten.api import export_to_obsidian, initialize_workspace, sync_zotero
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import build_source_catalogue
from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.pipeline import _source_set_graph_inputs
from auto_zettelkasten.zotero import (
    ZoteroLocalClient,
    diff_collection_snapshots,
    normalize_collection_snapshot,
    scope_collection_snapshot,
)


def _encoded(value: Any) -> bytes:
    return json.dumps(value).encode()


class StubClient(ZoteroLocalClient):
    def __init__(self, responses: dict[str, tuple[bytes, Mapping[str, str]]]) -> None:
        super().__init__()
        self.responses = responses

    def _request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        api: bool = True,
    ) -> tuple[bytes, Mapping[str, str]]:
        del method, body, api
        if endpoint not in self.responses:
            from auto_zettelkasten.zotero import ZoteroError

            raise ZoteroError(f"HTTP 404: {endpoint}")
        return self.responses[endpoint]


def _collections() -> list[dict[str, Any]]:
    return [
        {
            "key": "PARENT",
            "version": 4,
            "data": {"key": "PARENT", "name": "Peace Studies", "parentCollection": False},
        },
        {
            "key": "CHILD",
            "version": 2,
            "data": {"key": "CHILD", "name": "Mediation", "parentCollection": "PARENT"},
        },
    ]


def _items() -> list[dict[str, Any]]:
    return [
        {
            "key": "ITEM1",
            "version": 7,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "Direct parent source",
                "collections": ["PARENT"],
            },
        },
        {
            "key": "ITEM2",
            "version": 3,
            "data": {
                "key": "ITEM2",
                "itemType": "attachment",
                "title": "download.pdf",
                "parentItem": "BOOK1",
                "collections": ["CHILD", "PARENT"],
            },
        },
    ]


def test_normalize_and_diff_complete_collection_snapshot() -> None:
    parent_items = {
        "BOOK1": {
            "key": "BOOK1",
            "version": 9,
            "data": {
                "key": "BOOK1",
                "itemType": "book",
                "title": "Canonical parent title",
                "creators": [{"creatorType": "author", "lastName": "Author"}],
                "date": "2020",
            },
        }
    }
    first = normalize_collection_snapshot(_collections(), _items(), parent_items=parent_items)
    replay = normalize_collection_snapshot(
        list(reversed(_collections())),
        list(reversed(_items())),
        parent_items=parent_items,
    )

    assert replay == first
    assert first["schema_version"] == "2"
    assert first["items"][0]["identity"]["title"] == "Direct parent source"
    assert first["items"][1]["identity"]["title"] == "Canonical parent title"
    assert first["items"][1]["identity"]["year"] == "2020"
    assert first["items"][1]["collection_keys"] == ["CHILD", "PARENT"]
    assert first["items"][1]["parent_metadata"]["title"] == "Canonical parent title"

    changed_collections = _collections()
    changed_collections[0]["data"]["name"] = "Peace and Conflict"
    changed_collections[1]["data"]["parentCollection"] = False
    changed_items = _items()
    changed_items[0]["data"]["title"] = "Revised source"
    changed_items[0]["data"]["collections"] = ["CHILD"]
    changed_items.pop()
    changed_items.append(
        {
            "key": "ITEM3",
            "version": 1,
            "data": {
                "key": "ITEM3",
                "itemType": "report",
                "title": "New report",
                "collections": ["CHILD"],
            },
        }
    )
    second = normalize_collection_snapshot(
        changed_collections,
        changed_items,
        parent_items=parent_items,
    )
    diff = diff_collection_snapshots(first, second)

    assert diff["new_item_keys"] == ["ITEM3"]
    assert diff["changed_item_keys"] == ["ITEM1"]
    assert diff["removed_item_keys"] == ["ITEM2"]
    assert diff["membership_changed_item_keys"] == ["ITEM1"]
    assert diff["renamed_collection_keys"] == ["PARENT"]
    assert diff["moved_collection_keys"] == ["CHILD"]


def test_zotero_item_lookup_is_read_only_and_optional() -> None:
    client = StubClient(
        {
            "users/0/items/ITEM1": (
                _encoded({"key": "ITEM1", "data": {"key": "ITEM1", "title": "Found"}}),
                {},
            )
        }
    )

    assert client.item("ITEM1")["data"]["title"] == "Found"
    assert client.item("MISSING") is None


def _note(key: str, title: str, collection_title: str) -> dict[str, Any]:
    return {
        "source_id": f"source-zotero-{key.lower()}",
        "note_id": f"note-{key.lower()}",
        "zotero_item_key": key,
        "title": title,
        "date": "2024",
        "creators": [{"creatorType": "author", "lastName": "Author"}],
        "thesis": f"{collection_title} thesis.",
        "method": "Comparative analysis.",
        "note_path": f"02_source_memory/atomic_notes/{key}.md",
    }


def test_collection_tree_indexes_use_direct_members_and_replay_byte_identically(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "links": [
                {
                    "relation_id": "relation-one-two",
                    "source_id": "source-zotero-item1",
                    "target_source_id": "source-zotero-item2",
                    "relation_type": "supports",
                    "reason": "The studies support the same bounded proposition.",
                    "confidence": 0.9,
                    "active": True,
                }
            ]
        },
    )
    snapshot = normalize_collection_snapshot(_collections(), _items())
    notes = [
        _note("ITEM1", "Direct parent source", "Parent"),
        _note("ITEM2", "Child source", "Child"),
    ]
    profiles = [
        {
            "source_id": note["source_id"],
            "note_id": note["note_id"],
            "concepts": ["peace"],
        }
        for note in notes
    ]

    first = build_source_catalogue(
        tmp_path,
        profiles,
        notes,
        collection_snapshot=snapshot,
    )
    collection_paths = [Path(path) for path in first["collection_index_paths"]]
    before = {
        path: path.read_bytes()
        for path in [
            Path(first["master_index_path"]),
            *collection_paths,
            *(Path(path) for path in first["collection_shard_paths"]),
        ]
    }

    parent_index = tmp_path / "02_source_memory" / "indexes" / "collections" / "PARENT" / "INDEX.md"
    child_index = tmp_path / "02_source_memory" / "indexes" / "collections" / "CHILD" / "INDEX.md"
    assert parent_index in collection_paths
    assert child_index in collection_paths
    assert "- Direct sources: 2" in parent_index.read_text()
    assert "- Descendant sources: 1" in parent_index.read_text()
    assert "[[../CHILD/INDEX|Mediation]]" in parent_index.read_text()
    assert "[[../PARENT/INDEX|Peace Studies]]" in child_index.read_text()

    parent_shard = parent_index.parent / "sources-001.md"
    child_shard = child_index.parent / "sources-001.md"
    assert "Direct parent source" in parent_shard.read_text()
    assert "Child source" in parent_shard.read_text()
    assert "Child source" in child_shard.read_text()
    assert "Direct parent source" not in child_shard.read_text()
    parent_relationships = parent_index.parent / "relationships-001.md"
    child_relationships = child_index.parent / "relationships-001.md"
    assert "within collection" in parent_relationships.read_text()
    assert "cross-collection" in child_relationships.read_text()
    assert "## Graph connections" in child_index.read_text()
    root = Path(first["master_index_path"]).read_text()
    assert "[[collections/PARENT/INDEX|Peace Studies]]" in root
    assert "  - [[collections/CHILD/INDEX|Mediation]]" in root

    replay = build_source_catalogue(
        tmp_path,
        profiles,
        notes,
        collection_snapshot=snapshot,
    )
    assert replay["changed_paths"] == []
    assert {path: path.read_bytes() for path in before} == before

    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)
    export_root = Path(exported.metadata["export_root"])
    assert (export_root / "Indexes" / "collections" / "PARENT" / "INDEX.md").exists()
    assert (export_root / "Indexes" / "collections" / "CHILD" / "sources-001.md").exists()
    assert not any(
        row["target"].startswith("collections/")
        for row in exported.metadata["missing_wikilinks"]
    )


def test_incremental_sync_returns_without_provider_calls_when_snapshot_is_unchanged(
    tmp_path: Path,
) -> None:
    class ReadOnlyClient:
        def collections(self) -> list[dict[str, Any]]:
            return _collections()

        def inventory(
            self, scope: str, collection_key: str | None = None
        ) -> list[dict[str, Any]]:
            assert scope == "library"
            assert collection_key is None
            return _items()

    initialize_workspace(tmp_path)
    snapshot = normalize_collection_snapshot(_collections(), _items())
    write_yaml(
        tmp_path / "11_state" / "zotero" / "last_processed_snapshot.yml",
        snapshot,
    )

    result = sync_zotero(
        MapRequest(tmp_path, provider="ollama", model="unused"),
        client=ReadOnlyClient(),  # type: ignore[arg-type]
    )

    assert result["status"] == "unchanged"
    assert result["provider_call_count"] == 0
    assert not any(result["changes"].values())


def test_collection_scoped_snapshot_ignores_unrelated_library_changes() -> None:
    collections = [
        *_collections(),
        {
            "key": "OTHER",
            "data": {
                "key": "OTHER",
                "name": "Unrelated",
                "parentCollection": False,
            },
        },
    ]
    items = [
        *_items(),
        {
            "key": "OTHERITEM",
            "data": {
                "key": "OTHERITEM",
                "title": "Unrelated source",
                "itemType": "report",
                "collections": ["OTHER"],
            },
        },
    ]
    first_full = normalize_collection_snapshot(collections, items)
    first = scope_collection_snapshot(
        first_full,
        scope="collection",
        collection_key="CHILD",
        item_keys=["ITEM2"],
    )
    items[-1]["data"]["title"] = "Changed outside the selected collection"
    second_full = normalize_collection_snapshot(collections, items)
    second = scope_collection_snapshot(
        second_full,
        scope="collection",
        collection_key="CHILD",
        item_keys=["ITEM2"],
    )

    assert first == second
    assert not any(diff_collection_snapshots(first, second).values())


class SyncClient:
    def __init__(
        self,
        collections: list[dict[str, Any]],
        library_items: list[dict[str, Any]],
        collection_items: list[dict[str, Any]],
    ) -> None:
        self._collections = collections
        self._library_items = library_items
        self._collection_items = collection_items

    def collections(self) -> list[dict[str, Any]]:
        return self._collections

    def inventory(
        self,
        scope: str,
        collection_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if scope == "library":
            assert collection_key is None
            return self._library_items
        assert scope == "collection"
        assert collection_key
        return self._collection_items


def test_sync_resolves_attachment_parent_metadata_from_zotero(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    attachment = _items()[1]
    parent = {
        "key": "BOOK1",
        "version": 9,
        "data": {
            "key": "BOOK1",
            "itemType": "book",
            "title": "Canonical parent title",
            "creators": [{"creatorType": "editor", "lastName": "Editor"}],
            "date": "2020",
        },
    }
    full = normalize_collection_snapshot(
        _collections(),
        [attachment],
        parent_items={"BOOK1": parent},
    )
    write_yaml(
        tmp_path
        / "11_state"
        / "zotero"
        / "processed_snapshots"
        / "collection-child.yml",
        scope_collection_snapshot(
            full,
            scope="collection",
            collection_key="CHILD",
            item_keys=["ITEM2"],
        ),
    )

    class ParentClient(SyncClient):
        item_calls: list[str] = []

        def item(self, item_key: str) -> Mapping[str, Any] | None:
            self.item_calls.append(item_key)
            return parent if item_key == "BOOK1" else None

    client = ParentClient(_collections(), [attachment], [attachment])
    result = sync_zotero(
        MapRequest(
            tmp_path,
            scope="collection",
            collection_key="CHILD",
            provider="ollama",
            model="unused",
        ),
        client=client,  # type: ignore[arg-type]
    )
    snapshot = read_yaml(
        tmp_path / "01_custody" / "zotero" / "collection_snapshot.yml",
        {},
    )
    item = snapshot["items"][0]

    assert result["status"] == "unchanged"
    assert client.item_calls == ["BOOK1"]
    assert item["parent_metadata"]["title"] == "Canonical parent title"
    assert item["parent_metadata"]["creators"] == [
        {"creatorType": "editor", "lastName": "Editor"}
    ]


def test_collection_sync_does_not_acknowledge_unrelated_library_change(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    old_full = normalize_collection_snapshot(_collections(), _items())
    scoped = scope_collection_snapshot(
        old_full,
        scope="collection",
        collection_key="CHILD",
        item_keys=["ITEM2"],
    )
    write_yaml(
        tmp_path
        / "11_state"
        / "zotero"
        / "processed_snapshots"
        / "collection-child.yml",
        scoped,
    )
    changed_items = _items()
    changed_items[0]["data"]["title"] = "Changed outside child"
    result = sync_zotero(
        MapRequest(
            tmp_path,
            scope="collection",
            collection_key="CHILD",
            provider="ollama",
            model="unused",
        ),
        client=SyncClient(_collections(), changed_items, [changed_items[1]]),  # type: ignore[arg-type]
    )

    assert result["status"] == "unchanged"
    assert result["provider_call_count"] == 0
    assert not (
        tmp_path / "11_state" / "zotero" / "last_processed_snapshot.yml"
    ).exists()


def test_collection_rename_refreshes_indexes_without_relationship_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_workspace(tmp_path)
    old_full = normalize_collection_snapshot(_collections(), _items())
    old_scope = scope_collection_snapshot(
        old_full,
        scope="collection",
        collection_key="CHILD",
        item_keys=["ITEM2"],
    )
    state_path = (
        tmp_path
        / "11_state"
        / "zotero"
        / "processed_snapshots"
        / "collection-child.yml"
    )
    write_yaml(state_path, old_scope)
    renamed = _collections()
    renamed[1]["data"]["name"] = "Mediation and Negotiation"
    refreshed: list[Mapping[str, Any]] = []

    def refresh(*_args, **kwargs):
        refreshed.append(kwargs["collection_snapshot"])
        return {"status": "locally_refreshed"}

    monkeypatch.setattr("auto_zettelkasten.api._refresh_sync_projections", refresh)
    monkeypatch.setattr(
        "auto_zettelkasten.api.run_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rename must not run relationship discovery")
        ),
    )
    result = sync_zotero(
        MapRequest(
            tmp_path,
            scope="collection",
            collection_key="CHILD",
            provider="ollama",
            model="unused",
        ),
        client=SyncClient(renamed, _items(), [_items()[1]]),  # type: ignore[arg-type]
    )

    assert result["status"] == "synced"
    assert result["changes"]["renamed_collection_keys"] == ["CHILD"]
    assert result["relationship_discovery_performed"] is False
    assert result["provider_call_count"] == 0
    assert len(refreshed) == 1
    assert Path(result["processed_snapshot_path"]) == state_path


def test_changed_and_new_sources_are_the_only_frozen_processing_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_workspace(tmp_path)
    old_items = [_items()[0]]
    old_full = normalize_collection_snapshot(_collections(), old_items)
    write_yaml(
        tmp_path / "11_state" / "zotero" / "last_processed_snapshot.yml",
        scope_collection_snapshot(old_full, scope="library"),
    )
    current_items = _items()
    current_items[0]["data"]["title"] = "Changed source"

    class Report:
        status = "completed"
        provider_call_count = 2

        def to_dict(self) -> dict[str, Any]:
            return {"status": self.status, "provider_call_count": 2}

    monkeypatch.setattr(
        "auto_zettelkasten.api.run_map",
        lambda *_args, **_kwargs: Report(),
    )
    result = sync_zotero(
        MapRequest(tmp_path, provider="ollama", model="unused"),
        client=SyncClient(_collections(), current_items, []),  # type: ignore[arg-type]
        run_id="incremental-changes",
    )
    frozen = json.loads(
        (
            tmp_path
            / "11_state"
            / "runs"
            / "incremental-changes"
            / "inventory.json"
        ).read_text()
    )

    assert result["processed_item_keys"] == ["ITEM1", "ITEM2"]
    assert frozen == current_items
    assert result["relationship_discovery_performed"] is True


def test_removed_source_uses_full_snapshot_for_local_retirement_and_preserves_note(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_workspace(tmp_path)
    first = normalize_collection_snapshot(_collections(), _items())
    write_yaml(
        tmp_path / "11_state" / "zotero" / "last_processed_snapshot.yml",
        scope_collection_snapshot(first, scope="library"),
    )
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "source_catalogue.yml",
        {
            "sources": [
                {
                    "source_id": "source-zotero-item2",
                    "relationship_ids": ["relation-removed"],
                    "cluster_ids": ["cluster-removed"],
                }
            ]
        },
    )
    note = tmp_path / "02_source_memory" / "notes" / "preserved.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("human content must survive\n")
    snapshots: list[Mapping[str, Any]] = []

    def refresh(*_args, **kwargs):
        snapshots.append(kwargs["collection_snapshot"])
        return {"status": "locally_refreshed"}

    monkeypatch.setattr("auto_zettelkasten.api._refresh_sync_projections", refresh)
    monkeypatch.setattr(
        "auto_zettelkasten.api.run_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("removal must use local retirement")
        ),
    )
    result = sync_zotero(
        MapRequest(tmp_path, provider="ollama", model="unused"),
        client=SyncClient(_collections(), [_items()[0]], []),  # type: ignore[arg-type]
    )

    assert result["changes"]["removed_item_keys"] == ["ITEM2"]
    assert result["affected_relationship_ids"] == ["relation-removed"]
    assert result["affected_cluster_ids"] == ["cluster-removed"]
    assert {row["key"] for row in snapshots[0]["items"]} == {"ITEM1"}
    assert note.read_text() == "human content must survive\n"


def test_incremental_literature_input_uses_the_complete_source_set() -> None:
    notes = [
        {"source_id": "source-a", "note_id": "note-a"},
        {"source_id": "source-b", "note_id": "note-b"},
        {"source_id": "source-other", "note_id": "note-other"},
    ]
    profiles = [
        {"source_id": "source-a"},
        {"source_id": "source-b"},
        {"source_id": "source-other"},
    ]

    selected_notes, selected_profiles = _source_set_graph_inputs(
        {"source_ids": ["source-a", "source-b"]},
        notes,
        profiles,
    )

    assert {row["source_id"] for row in selected_notes} == {
        "source-a",
        "source-b",
    }
    assert {row["source_id"] for row in selected_profiles} == {
        "source-a",
        "source-b",
    }
