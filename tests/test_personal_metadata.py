from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentConflict, DocumentError, DocumentService


pytestmark = pytest.mark.skipif(not documents._SAFE_WRITES, reason="Private metadata requires safe directory handles")
HEADER = "---\nnote_id: stable-note\nsource_id: stable-source\ntitle: Generated title\ntags:\n- generated\naliases: [Original]\n---\n"


@pytest.fixture
def metadata_workspace(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    (root / "Note.md").write_text(HEADER + "Original body.\n")
    store = tmp_path / "overrides"
    service = DocumentService(journal_root=tmp_path / "journal", override_root=store)
    workspace = service.open(root)
    yield root, service, workspace, store
    service.close()


def update(fixture, **changes):
    _, service, workspace, _ = fixture
    current = service.read(workspace["id"], "Note.md")
    return service.save_personal_metadata(workspace["id"], current["id"], current["revision"], changes)


def test_title_only_tracks_new_generated_body_and_preserves_canonical_bytes(metadata_workspace):
    root, service, workspace, store = metadata_workspace
    original = (root / "Note.md").read_bytes()
    result = update(metadata_workspace, display_title="My title")
    assert result["display_title"] == "My title" and result["text"].encode() == original
    record = json.loads(next(store.rglob("*.json")).read_text())
    assert record["version"] == 2 and "body" not in record
    assert (root / "Note.md").read_bytes() == original
    (root / "Note.md").write_text(HEADER + "New generated body.\n")
    current = service.read(workspace["id"], "Note.md")
    assert current["text"].endswith("New generated body.\n") and current["display_title"] == "My title"
    assert not current["source_changed"]
    reset = update(metadata_workspace, display_title=None)
    assert reset["display_title"] == "Note" and reset["personal_display_title"] is None


def test_body_and_metadata_saves_preserve_one_another_and_v1_replays(metadata_workspace):
    root, service, workspace, store = metadata_workspace
    current = service.read(workspace["id"], "Note.md")
    body = service.save_override(workspace["id"], current["id"], current["revision"], HEADER + "Personal body\n")
    file = next(store.rglob("*.json"))
    legacy = json.loads(file.read_text()); legacy["version"] = 1
    file.write_text(json.dumps(legacy))
    current = update(metadata_workspace, display_title="Personal title", personal_properties={"tags": ["own"], "rating": 4})
    assert current["text"].endswith("Personal body\n")
    saved = service.save_override(workspace["id"], current["id"], current["revision"], HEADER + "Edited body\n")
    assert saved["display_title"] == "Personal title" and saved["personal_properties"]["rating"] == 4
    assert saved["effective_tags"] == ["generated", "own"]
    assert (root / "Note.md").read_text() == HEADER + "Original body.\n"


def test_effective_metadata_reaches_tree_indexes_recall_and_isolates_workspaces(metadata_workspace, tmp_path):
    root, service, workspace, store = metadata_workspace
    current = update(metadata_workspace, display_title="Same visible title", personal_properties={"aliases": ["Personal alias"], "tags": ["#own", "generated"]})
    assert current["effective_tags"] == ["generated", "own"]
    assert current["effective_aliases"] == ["Original", "Personal alias"]
    assert current["property_ownership"]["aliases"] == "mixed"
    assert service.tree(workspace["id"])["entries"][0]["display_title"] == "Same visible title"
    assert service.index(workspace["id"])["documents"][0]["personal_properties"]["tags"] == ["#own", "generated"]
    page = service.index_page(workspace["id"])
    rows = list(page["documents"])
    while not page["complete"]:
        page = service.index_page(workspace["id"], cursor=page["next_cursor"])
        rows += page["documents"]
    assert rows[0]["display_title"] == "Same visible title"
    assert service.recall_corpus(workspace["id"])["documents"][0]["display_title"] == "Same visible title"
    reopened = DocumentService(journal_root=tmp_path / "journal2", override_root=store)
    try:
        other = reopened.open(root)
        assert reopened.read(other["id"], "Note.md")["display_title"] == "Same visible title"
        independent = tmp_path / "other"; independent.mkdir()
        (independent / "Note.md").write_text(HEADER + "Other body")
        other = reopened.open(independent)
        assert reopened.read(other["id"], "Note.md")["personal_display_title"] is None
    finally:
        reopened.close()


@pytest.mark.parametrize("changes", [
    {"display_title": "bad\nname"}, {"display_title": ""}, {"display_title": "x" * 513},
    {"personal_properties": {"source_id": "different"}}, {"personal_properties": {"title": "different"}},
    {"personal_properties": {"constructor": "unsafe"}}, {"personal_properties": {"nested": {"key": "value"}}},
    {"personal_properties": {"score": float("nan")}}, {"personal_properties": {"tags": 1}},
    {"personal_properties": {"score": 10 ** 1000}}, {"personal_properties": None},
    {"unknown": True},
])
def test_invalid_metadata_never_writes_sources_or_override_records(metadata_workspace, changes):
    root, service, workspace, store = metadata_workspace
    original = (root / "Note.md").read_bytes()
    with pytest.raises(DocumentError):
        update(metadata_workspace, **changes)
    assert (root / "Note.md").read_bytes() == original
    assert not list(store.rglob("*.json"))


def test_new_generated_property_collision_is_preserved_without_shadowing(metadata_workspace):
    root, service, workspace, _ = metadata_workspace
    update(metadata_workspace, personal_properties={"rating": 4})
    (root / "Note.md").write_text(HEADER.replace("title: Generated title", "title: Generated title\nrating: 9") + "New body")
    current = service.read(workspace["id"], "Note.md")
    assert current["property_conflicts"] == ["rating"] and current["personal_properties"]["rating"] == 4
    assert current["property_ownership"]["rating"] == "generated"
    updated = update(metadata_workspace, display_title="Retain my title")
    assert updated["personal_properties"]["rating"] == 4
    with pytest.raises(DocumentError, match="replace generated"):
        update(metadata_workspace, personal_properties={"rating": 7})
    assert update(metadata_workspace, personal_properties={})["property_conflicts"] == []


def test_metadata_concurrency_rejects_stale_updates(metadata_workspace, tmp_path):
    root, service, workspace, store = metadata_workspace
    second = DocumentService(journal_root=tmp_path / "journal2", override_root=store)
    other = second.open(root)
    first_note = service.read(workspace["id"], "Note.md")
    other_note = second.read(other["id"], "Note.md")
    def attempt(args):
        current, ws, note, title = args
        try:
            return current.save_personal_metadata(ws["id"], note["id"], note["revision"], {"display_title": title})
        except DocumentConflict:
            return "conflict"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, [(service, workspace, first_note, "One"), (second, other, other_note, "Two")]))
        assert sum(outcome == "conflict" for outcome in outcomes) == 1
    finally:
        second.close()


def test_workspace_kind_only_uses_valid_fixed_metadata(metadata_workspace):
    root, service, workspace, _ = metadata_workspace
    assert workspace["workspace_kind"] == "markdown" and workspace["mapping_status"] == "absent"
    hidden = root / ".auto-zettelkasten"; hidden.mkdir()
    manifest = hidden / "related-export.json"
    manifest.write_text(json.dumps({"schema_version": "markdown-related-export-v1", "source_workspace": "/not-followed/source", "files": {"Note.md": "0" * 64}}))
    current = service.tree(workspace["id"])
    assert current["workspace_kind"] == "markdown_export" and current["source_workspace"] == "/not-followed/source"
    manifest.write_text("{}")
    assert service.tree(workspace["id"])["export_status"] == "invalid"
    mapping = hidden / "mapping"; mapping.mkdir(); (mapping / "result.json").write_text("{}")
    current = service.tree(workspace["id"])
    assert current["workspace_kind"] == "mapped" and current["mapping_status"] == "invalid"


def test_personal_metadata_api_is_revision_checked_and_rejects_unknown_fields(metadata_workspace, tmp_path):
    from auto_zettelkasten.webapp import LocalApp, RequestError

    root, service, workspace, _ = metadata_workspace
    app = LocalApp(preferences_path=tmp_path / "preferences.json", client=object())
    app.documents.close()
    app.documents = service
    current = service.read(workspace["id"], "Note.md")
    request = {"workspace_id": workspace["id"], "document_id": current["id"], "revision": current["revision"], "display_title": "API title"}
    try:
        saved = app.handle("POST", "/api/workspace/save-personal-metadata", request)["document"]
        assert saved["display_title"] == "API title"
        with pytest.raises(DocumentConflict):
            app.handle("POST", "/api/workspace/save-personal-metadata", {**request, "display_title": "Stale title"})
        with pytest.raises(RequestError, match="Unsupported"):
            app.handle("POST", "/api/workspace/save-personal-metadata", {**request, "unknown": 1})
        assert (root / "Note.md").read_text() == HEADER + "Original body.\n"
    finally:
        app.note_recall.close()


def test_metadata_only_keeps_accepted_recall_links_but_body_changes_suppress_them(metadata_workspace):
    root, service, workspace, _ = metadata_workspace
    (root / "Other.md").write_text(HEADER.replace("stable-note", "other-note").replace("stable-source", "other-source") + "Other body.\n")
    registry = root / "02_source_memory" / "indexes" / "typed_links.yml"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"registry_schema_version": documents.RELATIONSHIP_REGISTRY_SCHEMA_VERSION,
                                   "links": [{"relation_id": "accepted-link", "source_note_id": "stable-note",
                                              "target_note_id": "other-note", "relation_type": "supports",
                                              "decision_status": "accepted", "active": True}]}))
    endpoints = ["Note.md", "Other.md"]
    assert [row["id"] for row in service.recall_relationships(workspace["id"], endpoints)] == ["accepted-link"]
    metadata = update(metadata_workspace, display_title="Personal title", personal_properties={"tags": ["mine"]})
    assert metadata["override_saved"] and not metadata["body_override_saved"]
    assert [row["id"] for row in service.recall_relationships(workspace["id"], endpoints)] == ["accepted-link"]
    edited = service.save_override(workspace["id"], metadata["id"], metadata["revision"], HEADER + "Changed evidence.\n")
    assert edited["body_override_saved"]
    assert service.recall_relationships(workspace["id"], endpoints) == []
    restored = service.save_override(workspace["id"], edited["id"], edited["revision"], HEADER + "Original body.\n")
    assert not restored["body_override_saved"]
    assert [row["id"] for row in service.recall_relationships(workspace["id"], endpoints)] == ["accepted-link"]
