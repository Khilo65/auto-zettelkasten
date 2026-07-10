from __future__ import annotations

from pathlib import Path

import auto_zettelkasten.api as api


def test_expansion_types_and_ports_are_public() -> None:
    expected = {
        "ExpansionRequest",
        "ExpansionCandidate",
        "ExpansionDecision",
        "ExpansionReport",
        "ScholarlyGraphProvider",
        "ExpansionControllerPort",
    }
    assert expected <= set(api.__all__)
    assert all(getattr(api, name) is not None for name in expected)


def test_rebuild_typed_links_uses_committed_notes_only(tmp_path: Path, monkeypatch) -> None:
    api.initialize_workspace(tmp_path)
    notes = [{"note_id": "note-1", "source_id": "source-1", "normalized_tags": ["tag"]}]
    captured: dict[str, object] = {}

    monkeypatch.setattr(api, "all_workspace_note_rows", lambda workspace: notes)

    def fake_build(workspace: Path, note_rows):
        captured.update(workspace=workspace, note_rows=note_rows)
        return {"path": str(workspace / "typed_links.yml"), "links": [], "link_count": 0}

    monkeypatch.setattr(api, "_build_typed_links", fake_build)

    result = api.rebuild_typed_links(tmp_path)

    assert captured == {"workspace": tmp_path.resolve(), "note_rows": notes}
    assert result["link_count"] == 0
