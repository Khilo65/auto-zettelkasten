from dataclasses import replace
from pathlib import Path
import time

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
