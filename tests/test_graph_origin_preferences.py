"""Synthetic provenance and display-only preferences; no model or provider work."""
from __future__ import annotations

import copy
import json
import socket

import pytest
import yaml

from auto_zettelkasten import documents, webapp
from auto_zettelkasten.documents import DocumentIndexChanged, DocumentService
from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION, public_note_frontmatter
from auto_zettelkasten.relationships import RELATIONSHIP_REGISTRY_SCHEMA_VERSION


def _pages(service, workspace_id, **kwargs):
    page = service.index_page(workspace_id, **kwargs)
    rows = list(page["documents"])
    while not page["complete"]:
        page = service.index_page(workspace_id, page["next_cursor"])
        rows.extend(page["documents"])
    return {**page, "documents": rows}


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("engine_version: synthetic\n")
    path = "02_source_memory/notes/Source.md"
    note = root / path
    note.parent.mkdir(parents=True)
    indexes = root / "02_source_memory/indexes"
    indexes.mkdir()
    (indexes / "typed_links.yml").write_text(yaml.safe_dump({"registry_schema_version": RELATIONSHIP_REGISTRY_SCHEMA_VERSION, "links": []}))
    front = {"note_id": "note-a", "source_id": "source-a", "note_status": "analytical_atomic_note"}
    note.write_text("---\n" + yaml.safe_dump(public_note_frontmatter(front)) + "---\nSynthetic source.\n")
    metadata = root / "11_state/note_metadata/note-a.yml"
    metadata.parent.mkdir(parents=True)
    record = {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION, "note_path": path,
              "frontmatter": front, "private_details": "Do not project private research details"}
    metadata.write_text(yaml.safe_dump(record))
    service = DocumentService(journal_root=tmp_path / "journal", override_root=tmp_path / "overrides")
    opened = service.open(root)
    yield root, path, metadata, record, service, opened["id"]
    service.close()


def test_managed_origin_uses_protected_identity_and_survives_personal_override(managed):
    root, path, _, _, service, workspace_id = managed
    before = {str(file.relative_to(root)): file.read_bytes() for file in root.rglob("*") if file.is_file()}
    opened = service.read(workspace_id, path)
    assert opened["note_origin"] == "managed"
    if documents._SAFE_WRITES:
        saved = service.save_override(workspace_id, opened["id"], opened["revision"], opened["text"] + "Personal correction.\n")
        assert saved["note_origin"] == "managed"
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        assert snapshot["documents"][0]["note_origin"] == "managed"
        assert "private_details" not in json.dumps(snapshot)
        assert "Do not project" not in json.dumps(snapshot)
    assert before == {str(file.relative_to(root)): file.read_bytes() for file in root.rglob("*") if file.is_file()}


@pytest.mark.parametrize("invalid", ["missing", "schema", "path", "note_id", "source_id", "explicit_conflict", "alias", "oversize", "symlink", "hardlink"])
def test_unsupported_or_unbound_metadata_is_unknown(managed, invalid):
    root, path, metadata, record, service, workspace_id = managed
    if invalid == "missing":
        metadata.unlink()
    elif invalid == "explicit_conflict":
        note = root / path
        note.write_text(note.read_text().replace("note_id: note-a", "source_id: other\nnote_id: note-a"))
    elif invalid in {"symlink", "hardlink"}:
        if not documents._DIR_HANDLES:
            pytest.skip("Descriptor-relative link protection unavailable")
        import os
        target = metadata.parent / "other.yml"
        metadata.rename(target)
        if invalid == "symlink":
            metadata.symlink_to(target)
        else:
            os.link(target, metadata)
    elif invalid == "alias":
        metadata.write_text("metadata_schema_version: '2'\nnote_path: " + path + "\nprivate_source: &source source-a\nfrontmatter: {note_id: note-a, source_id: *source}\n")
    elif invalid == "oversize":
        metadata.write_text(yaml.safe_dump(record) + "large: " + "x" * 65536)
    else:
        if invalid == "schema":
            record["metadata_schema_version"] = "unsupported"
        elif invalid == "path":
            record["note_path"] = "elsewhere.md"
        else:
            record["frontmatter"][invalid] = ""
        metadata.write_text(yaml.safe_dump(record))
    assert service.read(workspace_id, path)["note_origin"] == "unknown"
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        assert snapshot["documents"][0]["note_origin"] == "unknown"


def test_shared_private_coverage_aliases_preserve_direct_managed_identity(managed):
    _, _, metadata, record, service, workspace_id = managed
    coverage = {"inspected_pages": [1, 2], "recovered_pages": [2], "limitations": ["Synthetic partial sample"]}
    record["coverage_record"] = coverage
    record["frontmatter"]["coverage_record"] = coverage
    text = yaml.safe_dump(record)
    assert "&id" in text and "*id" in text
    metadata.write_text(text)
    assert documents._frontmatter("---\n" + text + "---\n") == {}, "Generic public frontmatter must remain alias-free"
    projected = documents._origin_identity(text.encode())
    assert projected == {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION,
                         "note_path": record["note_path"],
                         "frontmatter": {"note_id": "note-a", "source_id": "source-a"}}
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        assert snapshot["documents"][0]["note_origin"] == "managed"
        assert "recovered_pages" not in json.dumps(snapshot)
        assert "Synthetic partial sample" not in json.dumps(snapshot)


def test_direct_anchored_identity_with_only_unrelated_alias_reuse_is_managed(managed):
    _, path, metadata, _, service, workspace_id = managed
    metadata.write_text("metadata_schema_version: '2'\nnote_path: " + path
                        + "\nfrontmatter: &identity {note_id: note-a, source_id: source-a}\nprivate_details: *identity\n")
    assert service.read(workspace_id, path)["note_origin"] == "managed"


@pytest.mark.parametrize("fragment", [
    "metadata_schema_version: '2'\nmetadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a, source_id: source-a}",
    "metadata_schema_version: !!int 2\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: 2\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: !!binary U291cmNlLm1k\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: !custom {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nidentity: &identity {note_id: note-a, source_id: source-a}\nfrontmatter: *identity",
    "metadata_schema_version: '2'\nnote_path: Source.md\nidentity: &identity {note_id: note-a, source_id: source-a}\nfrontmatter: {<<: *identity}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nidentity: &identity {note_id: note-a, source_id: source-a}\nfrontmatter: {<<: *identity, note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nprivate_key: &key note_path\n? *key\n: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nprivate_key: &key source_id\nfrontmatter: {note_id: note-a, source_id: source-a, *key: source-a}",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\nprivate_details: &cycle [*cycle]",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\nprivate_details: *undefined",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\na: &x []\nb: &x []",
    "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\n---\nextra: document",
])
def test_origin_identity_rejects_ambiguous_aliased_or_invalid_claims(fragment):
    assert documents._origin_identity(fragment.encode()) == {}


@pytest.mark.parametrize("private", ["[" * 33 + "0" + "]" * 33,
                                    "[" + "[]," * 17000 + "]"])
def test_origin_identity_event_and_depth_limits_apply_to_unrelated_private_data(private):
    text = "metadata_schema_version: '2'\nnote_path: Source.md\nfrontmatter: {note_id: note-a, source_id: source-a}\nprivate: " + private
    assert len(text.encode()) < 65536
    assert documents._origin_identity(text.encode()) == {}


def test_editability_names_and_frontmatter_do_not_establish_personal_or_managed(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for name, text in {"My personal note.md": "My own draft.", "Managed.md": "---\nnote_id: fake\nsource_id: fake\n---\nCopied properties."}.items():
        (root / name).write_text(text)
    service = DocumentService(journal_root=tmp_path / "journal")
    workspace_id = service.open(root)["id"]
    try:
        assert {row["note_origin"] for row in service.index(workspace_id)["documents"]} == {"unknown"}
        if documents._SAFE_WRITES:
            created = service.create(workspace_id, "", "New personal note", "file")
            assert created["document"]["note_origin"] == "unknown"
    finally:
        service.close()


def test_origin_metadata_budget_falls_back_to_unknown(managed, monkeypatch):
    _, _, _, _, service, workspace_id = managed
    monkeypatch.setattr(documents, "_ORIGIN_METADATA_BYTES", 1)
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        assert snapshot["documents"][0]["note_origin"] == "unknown"


@pytest.mark.parametrize("initially_missing", [False, True])
def test_metadata_only_changes_invalidate_completed_paged_generation(managed, initially_missing):
    _, _, metadata, record, service, workspace_id = managed
    if initially_missing:
        metadata.unlink()
    first = _pages(service, workspace_id)
    assert _pages(service, workspace_id, known_generation=first["generation"])["not_modified"]
    record["frontmatter"]["source_id"] = ""
    metadata.write_text(yaml.safe_dump(record))
    with pytest.raises(DocumentIndexChanged):
        _pages(service, workspace_id, known_generation=first["generation"])
    refreshed = _pages(service, workspace_id)
    assert refreshed["documents"][0]["note_origin"] == "unknown"


def test_metadata_change_during_legacy_projection_rejects_mixed_origin(managed, monkeypatch):
    _, _, metadata, record, service, workspace_id = managed
    project = service._typed_relations
    def replace_after_read(*args):
        record["frontmatter"]["source_id"] = ""
        metadata.write_text(yaml.safe_dump(record))
        return project(*args)
    monkeypatch.setattr(service, "_typed_relations", replace_after_read)
    with pytest.raises(DocumentIndexChanged):
        service.index(workspace_id)


def test_metadata_change_rejects_cached_page_replay(managed, monkeypatch):
    _, _, metadata, record, service, workspace_id = managed
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    first = service.index_page(workspace_id)
    assert not first["complete"]
    cursor = first["next_cursor"]
    service.index_page(workspace_id, cursor)
    record["frontmatter"]["source_id"] = ""
    metadata.write_text(yaml.safe_dump(record))
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace_id, cursor)


class OfflineClient:
    def __getattr__(self, name):
        pytest.fail("Graph display operations must not access Zotero")


def _app(tmp_path):
    return webapp.LocalApp(preferences_path=tmp_path / "settings/frontend.json", client=OfflineClient())


def _graph(app, workspace_id, **kwargs):
    return app.handle("POST", "/api/workspace/graph-preferences", {"workspace_id": workspace_id, **kwargs})["preferences"]


def test_workspace_graph_preferences_persist_without_changing_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network for display preferences"))
    root = tmp_path / "workspace"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "Note.md").write_text("A synthetic note.")
    app = _app(tmp_path)
    workspace_id = app.documents.open(root)["id"]
    before = (root / "Note.md").read_bytes()
    defaults = _graph(app, workspace_id)
    assert defaults == {"version": 1, "colorBy": "none", "origins": {"managed": True, "personal": True, "unknown": True}}
    assert not app.preferences_path.exists()
    chosen = {"version": 1, "colorBy": "community", "origins": {"managed": True, "personal": False, "unknown": False}}
    assert _graph(app, workspace_id, preferences=chosen) == chosen
    defaults["origins"]["managed"] = False
    chosen["origins"]["managed"] = False
    assert _graph(app, workspace_id)["origins"]["managed"] is True
    saved = json.loads(app.preferences_path.read_text())
    assert len(saved["graph_preferences"]) == 1
    assert str(root) not in app.preferences_path.read_text()
    other_id = app.documents.open(other)["id"]
    assert _graph(app, other_id)["colorBy"] == "none"
    with pytest.raises(webapp.RequestError):
        _graph(app, workspace_id)
    app.handle("POST", "/api/preferences", {"remember_vault": False})
    reopened = _app(tmp_path)
    new_id = reopened.documents.open(root)["id"]
    assert new_id != workspace_id
    assert _graph(reopened, new_id)["colorBy"] == "community"
    assert _graph(reopened, new_id)["origins"] == {"managed": True, "personal": False, "unknown": False}
    assert (root / "Note.md").read_bytes() == before
    assert sorted(file.name for file in root.iterdir()) == ["Note.md"]
    app.documents.close()
    reopened.documents.close()


@pytest.mark.parametrize("field,value", [("version", True), ("version", 2), ("colorBy", "arbitrary"), ("colorBy", []), ("origins", {}), ("origins", {"managed": 1, "personal": True, "unknown": True}), ("extra", "unknown")])
def test_graph_preferences_validate_complete_schema(tmp_path, field, value):
    root = tmp_path / "workspace"
    root.mkdir()
    app = _app(tmp_path)
    workspace_id = app.documents.open(root)["id"]
    selected = copy.deepcopy(webapp._GRAPH_PREFERENCES_DEFAULT)
    selected[field] = value
    try:
        with pytest.raises(webapp.RequestError):
            _graph(app, workspace_id, preferences=selected)
        assert not app.preferences_path.exists()
        assert _graph(app, workspace_id) == webapp._GRAPH_PREFERENCES_DEFAULT
    finally:
        app.documents.close()


def test_graph_preference_write_failure_preserves_prior_values(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    app = _app(tmp_path)
    workspace_id = app.documents.open(root)["id"]
    selected = copy.deepcopy(webapp._GRAPH_PREFERENCES_DEFAULT)
    selected["colorBy"] = "origin"
    def fail_write(*args):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(app, "_write_preferences", fail_write)
    try:
        with pytest.raises(webapp.RequestError, match="could not be saved"):
            _graph(app, workspace_id, preferences=selected)
        assert _graph(app, workspace_id) == webapp._GRAPH_PREFERENCES_DEFAULT
        assert not list(root.iterdir())
    finally:
        app.documents.close()


def test_invalid_saved_graph_preferences_use_defaults(tmp_path):
    app = _app(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace_id = app.documents.open(root)["id"]
    key = app.documents.workspace_key(workspace_id)
    app.preferences_path.parent.mkdir(exist_ok=True)
    app.preferences_path.write_text(json.dumps({"graph_preferences": {key: {"version": 0}}}))
    reopened = _app(tmp_path)
    try:
        assert _graph(reopened, reopened.documents.open(root)["id"]) == webapp._GRAPH_PREFERENCES_DEFAULT
    finally:
        app.documents.close()
        reopened.documents.close()
