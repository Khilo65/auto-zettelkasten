"""The app and agent entry points share local full-note recall."""
import json
import socket
import threading

import pytest

from auto_zettelkasten import api, cli, webapp
from auto_zettelkasten.relationship_mapping import RelationshipMappingOptions


@pytest.fixture
def notes(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    # The answer is neither a filename nor a compact-profile field.
    (root / "Methods.md").write_text("# Methods\n\n" + "Background context. " * 600 +
                                    "\nThe uncommon mechanism is participatory budgeting.\n")
    (root / "Elsewhere.md").write_text("# Elsewhere\n\nMarine ecosystems and coral reefs.\n")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Search contacted the network"))
    return root


def test_public_api_and_agent_cli_search_full_note_body(notes, capsys):
    before = {p.name: p.read_bytes() for p in notes.iterdir()}
    result = api.recall(notes, "participatory budgeting")
    assert result["provider_calls"] == 0
    assert result["notes"][0]["note_path"] == "Methods.md"
    assert "participatory budgeting" in result["notes"][0]["text"]
    assert result["provenance"]["route"] == "full_note_lexical"
    assert cli.main(["recall", "--workspace", str(notes), "--query", "participatory budgeting"]) == 0
    command = json.loads(capsys.readouterr().out)
    assert command["notes"][0]["source_id"] == result["notes"][0]["source_id"]
    assert command["provider_calls"] == 0
    assert {p.name: p.read_bytes() for p in notes.iterdir() if p.is_file()} == before


def test_agent_recall_does_not_filter_to_an_unspecified_library(notes, capsys):
    (notes / "Scoped.md").write_text("---\nlibrary_id: '42'\n---\n\nA rare scopedlibrary mechanism.\n")
    assert cli.main(["recall", "--workspace", str(notes), "--query", "scopedlibrary"]) == 0
    assert json.loads(capsys.readouterr().out)["notes"][0]["note_path"] == "Scoped.md"
    assert cli.main(["recall", "--workspace", str(notes), "--query", "scopedlibrary", "--library-id", "0"]) == 0
    assert not json.loads(capsys.readouterr().out)["notes"]


def test_app_search_uses_opened_workspace_and_grounded_body(notes, tmp_path):
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json")
    opened = app.handle("POST", "/api/workspace/open", {"path": str(notes)})["workspace"]
    result = app.handle("POST", "/api/workspace/recall", {"workspace_id": opened["id"], "query": "participatory budgeting"})
    assert result["notes"][0]["note_path"] == "Methods.md"
    assert "participatory budgeting" in result["notes"][0]["text"]
    assert result["provider_calls"] == 0 and not result["relationships"]
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/workspace/recall", {"workspace_id": "another-workspace", "query": "budgeting"})
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/workspace/recall", {"workspace_id": opened["id"], "query": "budgeting", "allow_cloud": True})


def test_slow_search_does_not_hold_the_app_lock(notes, tmp_path):
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json")
    opened = app.handle("POST", "/api/workspace/open", {"path": str(notes)})["workspace"]
    entered, finish, state_read = threading.Event(), threading.Event(), threading.Event()
    errors = []
    class Search:
        def recall(self, *args, **kwargs):
            entered.set()
            assert finish.wait(5)
            return {"notes": [], "provider_calls": 0}
    app.note_recall = Search()
    def search():
        try:
            app.handle("POST", "/api/workspace/recall", {"workspace_id": opened["id"], "query": "budgeting"})
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=search)
    worker.start()
    try:
        assert entered.wait(5)
        reader = threading.Thread(target=lambda: (app.handle("GET", "/api/state"), state_read.set()))
        reader.start()
        assert state_read.wait(2), "Search blocked independent app requests"
        reader.join(5)
    finally:
        finish.set()
        worker.join(5)
    assert not errors


def test_jev_remains_optional_off_and_unfunded_by_default():
    options = RelationshipMappingOptions.from_dict({})
    assert options.backend == "off" and options.dry_run and not options.apply
    assert options.max_spend_usd == "0"


def test_local_indexing_is_a_separate_explicit_app_and_cli_action(notes, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "prepare_recall", lambda workspace, **opts: calls.append((workspace, opts)) or {"status": "prepared", "provider_calls": 0})
    assert cli.main(["index-recall", "--workspace", str(notes), "--model-path", "cached-model"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert calls[-1][1]["model_path"] == "cached-model"
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json")
    opened = app.handle("POST", "/api/workspace/open", {"path": str(notes)})["workspace"]
    app.embedding_model_path = "cached-model"
    class Search:
        def prepare(self, workspace, **options):
            calls.append((workspace, options))
            return {"status": "prepared", "provider_calls": 0}
    app.note_recall = Search()
    result = app.handle("POST", "/api/workspace/recall-index", {"workspace_id": opened["id"]})
    assert result["provider_calls"] == 0
    assert calls[-1][1]["documents"] is app.documents
    assert calls[-1][1]["model_path"] == "cached-model"
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/workspace/recall-index", {"workspace_id": opened["id"], "allow_cloud": True})


def test_local_model_setup_failures_have_an_actionable_app_error(notes, tmp_path):
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json")
    opened = app.handle("POST", "/api/workspace/open", {"path": str(notes)})["workspace"]
    class Search:
        def prepare(self, *args, **kwargs):
            raise RuntimeError("Install the local MiniLM dependencies before indexing notes.")
    app.note_recall = Search()
    with pytest.raises(webapp.RequestError, match="local MiniLM dependencies"):
        app.handle("POST", "/api/workspace/recall-index", {"workspace_id": opened["id"]})
