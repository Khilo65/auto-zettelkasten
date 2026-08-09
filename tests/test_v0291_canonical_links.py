from pathlib import Path

from auto_zettelkasten.literature import (
    _cluster_limit_text,
    _navigation_profile_rows,
    _obsidian_note_link,
    _report_projection_profiles,
    _streamlined_cluster_markdown,
    build_literature_map,
)
from auto_zettelkasten.readers import _validate_streamlined_cluster_response


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


def test_projection_profiles_preserve_public_profiles_and_drive_rendering() -> None:
    analytical = {
        "source_id": "source-a",
        "note_path": "02_source_memory/notes/Stale target.md",
        "title": "Source A",
    }
    canonical = {
        **analytical,
        "note_path": "02_source_memory/notes/Canonical Source A [ABC123].md",
    }
    report = {"profiles": [analytical], "projection_profiles": [canonical]}

    selected = _report_projection_profiles(report)

    assert report["profiles"] == [analytical]
    assert selected == [canonical]
    assert _obsidian_note_link(selected[0]) == (
        "[[Canonical Source A [ABC123]|Source A]]"
    )
    assert _report_projection_profiles({"profiles": [analytical]}) == [analytical]


def test_cluster_limits_render_current_and_legacy_mapping_rows_cleanly() -> None:
    profile = {
        "source_id": "source-a",
        "note_path": "02_source_memory/notes/Canonical Source A [ABC123].md",
        "title": "Source A",
    }
    cluster = {"cluster_id": "cluster-a", "source_ids": ["source-a"]}
    synthesis = {
        "title": "Cluster A",
        "retained_member_ids": ["source-a"],
        "limits": [
            {"limit": "Evidence covers one region."},
            "{'boundary': 'The studies use different outcome measures.'}",
        ],
    }

    markdown = _streamlined_cluster_markdown(
        cluster,
        synthesis,
        profile_by_source={"source-a": profile},
        cluster_by_id={"cluster-a": cluster},
    )

    assert _cluster_limit_text({"reason": "No longitudinal evidence."}) == (
        "No longitudinal evidence."
    )
    assert "- Evidence covers one region." in markdown
    assert "- The studies use different outcome measures." in markdown
    assert "{'limit':" not in markdown
    assert "[[Canonical Source A [ABC123]|Source A]]" in markdown


def test_cluster_limit_mappings_normalize_at_the_provider_boundary() -> None:
    normalized = _validate_streamlined_cluster_response(
        {
            "cluster_id": "cluster-a",
            "title": "Cluster A",
            "organizing_problem": "What does the literature establish?",
            "limits": [
                {"limit": "Evidence covers one region."},
                {"boundary": "Measures are not directly comparable."},
            ],
        }
    )

    assert normalized["limits"] == [
        "Evidence covers one region.",
        "Measures are not directly comparable.",
    ]


def test_report_to_persist_uses_canonical_source_note_paths(tmp_path: Path) -> None:
    profiles = [
        {
            "source_id": "source-a",
            "note_id": "note-a",
            "note_status": "analytical_atomic_note",
            "title": "Source A",
            "study_family_id": "family-a",
            "note_path": "02_source_memory/notes/Stale A.md",
        },
        {
            "source_id": "source-b",
            "note_id": "note-b",
            "note_status": "analytical_atomic_note",
            "title": "Source B",
            "study_family_id": "family-b",
            "note_path": "02_source_memory/notes/Stale B.md",
        },
    ]
    notes = [
        {
            **profiles[0],
            "note_path": "02_source_memory/notes/Canonical Source A [AAA111].md",
        },
        {
            **profiles[1],
            "note_path": "02_source_memory/notes/Canonical Source B [BBB222].md",
        },
    ]

    _cluster_map, _gap_map, _packet, paths = build_literature_map(
        tmp_path,
        source_set={"source_set_id": "canonical-test"},
        notes=notes,
        profiles=profiles,
        question=None,
        run_id="canonical-test",
    )
    rendered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in paths
        if path.suffix == ".md"
    )

    assert "[[Canonical Source A [AAA111]|Source A]]" in rendered
    assert "[[Canonical Source B [BBB222]|Source B]]" in rendered
    assert "[[Stale A" not in rendered
    assert "[[Stale B" not in rendered
