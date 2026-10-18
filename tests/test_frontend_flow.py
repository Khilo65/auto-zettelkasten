from dataclasses import replace
from pathlib import Path
import time
import pytest

from auto_zettelkasten import webapp
from conftest import FakeReader, FakeZotero


def test_approved_mapping_exports_real_notes_and_preserves_vault(tmp_path, sample_items, monkeypatch):
    """Exercise the engine/export integration with a strictly offline reader."""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    personal = vault / "My note.md"
    personal.write_text("Keep my note.\n")
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json", client=FakeZotero(sample_items))
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "offline-placeholder"})
    real_run_map = webapp.api.run_map

    def offline_map(request, **kwargs):
        return real_run_map(
            replace(request, provider="ollama", model="fake-1", literature_model=None, reasoning_effort=None),
            reader=FakeReader(),
            **kwargs,
        )

    monkeypatch.setattr(webapp.api, "run_map", offline_map)
    ticket = app.handle("POST", "/api/estimate", {
        "collection_key": "COLL1",
        "output_folder": str(tmp_path / "output"),
        "destination": "obsidian",
        "vault": str(vault),
        "remember_vault": True,
    })
    assert ticket["estimate"]["provider_calls"] == 0
    assert not Path(ticket["workspace"]).exists()
    app.handle("POST", "/api/approve", {"estimate_id": ticket["estimate_id"], "allow_cloud": True})
    deadline = time.monotonic() + 15
    while app.run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    assert app.run["status"] in {"completed", "partial"}, app.run
    assert app.run["report"]["validated_note_count"] == 2
    export = Path(ticket["obsidian_path"])
    notes = list((export / "Sources").glob("*.md"))
    assert len(notes) == 2
    assert any("[[" in note.read_text() for note in notes)
    assert (export / "Indexes" / "Source Index.md").is_file()
    assert personal.read_text() == "Keep my note.\n"
    reopened = webapp.LocalApp(preferences_path=app.preferences_path, client=FakeZotero(sample_items))
    assert reopened.preferences["vault"] == str(vault)


@pytest.mark.parametrize("scope", ["library", "collection"])
def test_mapper_builds_real_agent_memory_and_opens_published_index(tmp_path, sample_items, monkeypatch, scope):
    client = FakeZotero(sample_items)
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json", client=client)
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "offline-placeholder"})
    real_run_map = webapp.api.run_map

    def offline_map(request, **kwargs):
        return real_run_map(
            replace(request, provider="ollama", model="fake-1", literature_model=None, reasoning_effort=None),
            reader=FakeReader(), **kwargs,
        )

    monkeypatch.setattr(webapp.api, "run_map", offline_map)
    ticket = app.handle("POST", "/api/estimate", {
        "scope": scope,
        "collection_key": "COLL1" if scope == "collection" else "",
        "output_folder": str(tmp_path / "output"),
        "destination": "folder",
    })
    root = Path(ticket["workspace"])
    assert ticket["estimate"]["provider_calls"] == 0 and not root.exists()
    app.handle("POST", "/api/approve", {"estimate_id": ticket["estimate_id"], "allow_cloud": True})
    deadline = time.monotonic() + 15
    while app.run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    run = app.handle("GET", "/api/run")
    assert run["status"] in {"completed", "partial"}, run
    assert run["report"]["validated_note_count"] == 2
    assert run["workspace_available"] and run["memory_workspace"] == str(root)
    assert (root / "02_source_memory/indexes/source_catalogue.yml").is_file()
    assert (root / "02_source_memory/indexes/typed_links.yml").is_file()
    assert list((root / "02_source_memory/profiles").glob("*.yml"))

    workspace = app.handle("POST", "/api/workspace/open", {"path": str(root)})["workspace"]
    workspace_id = workspace["id"]
    entry = "02_source_memory/indexes/INDEX.md"
    assert any(row["path"] == entry and not row["editable"] for row in workspace["entries"])
    index = app.handle("POST", "/api/workspace/read", {"workspace_id": workspace_id, "path": entry})["document"]
    assert "# Source Index" in index["text"] and not index["editable"]
    snapshot = app.handle("POST", "/api/workspace/index", {"workspace_id": workspace_id})
    assert len([doc for doc in snapshot["documents"] if doc["path"].startswith("02_source_memory/notes/")]) == 2
    assert any(doc["path"] == entry for doc in snapshot["documents"])
    assert all(not doc["editable"] for doc in snapshot["documents"])
    assert all(not doc["path"].endswith(".yml") for doc in snapshot["documents"])
    app.documents.close()
