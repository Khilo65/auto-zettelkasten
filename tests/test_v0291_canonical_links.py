from auto_zettelkasten.literature import (
    _navigation_profile_rows,
    _obsidian_note_link,
)


def test_empty_profile_path_uses_canonical_note_path_for_wikilinks() -> None:
    profile = {
        "source_id": "source-zotero-9mh7lag9",
        "note_id": "note-paris",
        "note_path": "",
        "title": "At war's end",
    }
    source_note = {
        "source_id": "source-zotero-9mh7lag9",
        "note_id": "note-paris",
        "note_path": (
            "02_source_memory/notes/"
            "Paris2004 - At war's end [9mh7lag9].md"
        ),
        "title": "At war's end",
    }

    hydrated = _navigation_profile_rows([profile], [source_note])[0]

    assert hydrated["note_path"] == source_note["note_path"]
    assert _obsidian_note_link(hydrated) == (
        "[[Paris2004 - At war's end [9mh7lag9]|At war's end]]"
    )


def test_stale_profile_path_is_replaced_and_unknown_target_is_not_linked() -> None:
    profile = {
        "source_id": "source-zotero-9mh7lag9",
        "note_id": "stale-note",
        "note_path": "02_source_memory/notes/Paris (2004).md",
        "title": "At war's end",
    }
    source_note = {
        "source_id": "source-zotero-9mh7lag9",
        "note_id": "note-paris",
        "note_path": "02_source_memory/notes/Paris2004 - At war's end [9mh7lag9].md",
    }

    hydrated = _navigation_profile_rows([profile], [source_note])[0]

    assert hydrated["note_id"] == "note-paris"
    assert hydrated["note_path"] == source_note["note_path"]
    assert "Paris (2004)" not in _obsidian_note_link(hydrated)
    assert _obsidian_note_link({"source_id": "unknown", "title": "Unknown"}) == "Unknown"
