import json
from pathlib import Path

import pytest

from auto_zettelkasten import cli, webapp, markdown_link_export


def test_cli_export_is_preview_only_unless_exact_application_is_requested(tmp_path, monkeypatch, capsys):
    calls = []
    def export(workspace, destination, **options):
        calls.append((workspace, destination, options))
        return {"status": "preview", "provider_calls": 0, "jev_enabled": False}
    monkeypatch.setattr(markdown_link_export, "export_markdown_links", export)
    args = ["export", "links", "--workspace", str(tmp_path), "--destination", str(tmp_path / "output")]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert calls[-1][2]["apply"] is False
    assert calls[-1][2]["preview_identity"] is None
    assert cli.main([*args, "--apply", "--preview-identity", "exact", "--degree-cap", "4"]) == 0
    assert calls[-1][2]["apply"] is True
    assert calls[-1][2]["preview_identity"] == "exact"
    assert calls[-1][2]["degree_cap"] == 4


def test_app_export_uses_current_saved_edits_without_provider_or_zotero_access(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Example.md").write_text("A saved finding.")
    class NoZotero:
        def __getattr__(self, name):
            pytest.fail("Export accessed Zotero")
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json", client=NoZotero())
    workspace_id = app.documents.open(root)["id"]
    calls = []
    def export(workspace, destination, **options):
        assert options["documents"] is app.documents
        assert options["workspace_id"] == workspace_id
        calls.append((Path(workspace), destination, options))
        return {"status": "exported" if options["apply"] else "preview", "provider_calls": 0, "jev_enabled": False}
    monkeypatch.setattr(markdown_link_export, "export_markdown_links", export)
    payload = {"workspace_id": workspace_id, "destination": str(tmp_path / "linked"), "degree_cap": 8}
    assert app.handle("POST", "/api/workspace/export-links-preview", payload)["status"] == "preview"
    assert not calls[-1][2]["apply"]
    assert app.handle("POST", "/api/workspace/export-links", {**payload, "preview_identity": "exact"})["status"] == "exported"
    assert calls[-1][2]["preview_identity"] == "exact"
    assert calls[-1][0] == root
    for bad in ({**payload, "workspace_id": "wrong"}, {**payload, "allow_cloud": True}, {**payload, "model": "provider"}):
        with pytest.raises((webapp.RequestError, ValueError)):
            app.handle("POST", "/api/workspace/export-links-preview", bad)
    app.run["status"] = "running"
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/workspace/export-links", {**payload, "preview_identity": "exact"})


def test_export_boundary_errors_are_actionable_local_errors(tmp_path, monkeypatch):
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    workspace_id = app.documents.open(tmp_path)["id"]
    def refused(*args, **kwargs):
        raise ValueError("Notes changed; preview again.")
    monkeypatch.setattr(markdown_link_export, "export_markdown_links", refused)
    with pytest.raises(webapp.RequestError, match="preview again"):
        app.handle("POST", "/api/workspace/export-links", {"workspace_id": workspace_id, "destination": str(tmp_path / "other")})
