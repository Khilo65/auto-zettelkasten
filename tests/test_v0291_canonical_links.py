from pathlib import Path

import yaml

from auto_zettelkasten.literature import (
    _canonical_projection_profiles,
    _cluster_limit_text,
    _cluster_wikilink,
    _navigation_profile_rows,
    _obsidian_note_link,
    _replace_source_keys_in_markdown_body,
    _report_projection_profiles,
    _streamlined_cluster_markdown,
    _write_managed_cluster_projection,
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


def test_final_projection_resolves_stale_zotero_note_paths(tmp_path: Path) -> None:
    notes_root = tmp_path / "02_source_memory" / "notes"
    notes_root.mkdir(parents=True)
    canonical_paths = {
        "source-zotero-9mh7lag9": (
            notes_root / "Paris2004 - At war's end [9mh7lag9].md"
        ),
        "source-zotero-494iyya8": (
            notes_root / "Unknownn.d. - Document Viewer [494iyya8].md"
        ),
        "source-zotero-iwhwd39r": (
            notes_root
            / "Walter2004 - Does Conflict Beget Conflict [iwhwd39r].md"
        ),
    }
    for path in canonical_paths.values():
        path.write_text("", encoding="utf-8")
    (notes_root / "Paris (2004).md").write_text("stale", encoding="utf-8")
    report = {
        "projection_profiles": [
            {
                "source_id": source_id,
                "title": path.stem.split(" - ", 1)[-1].rsplit(" [", 1)[0],
                "note_path": (
                    "02_source_memory/notes/"
                    + {
                        "source-zotero-9mh7lag9": "Paris (2004).md",
                        "source-zotero-494iyya8": "Unknownn.d..md",
                        "source-zotero-iwhwd39r": "Walter (2004).md",
                    }[source_id]
                ),
            }
            for source_id, path in canonical_paths.items()
        ]
    }

    profiles = _canonical_projection_profiles(tmp_path, report)

    assert {
        row["source_id"]: row["note_path"] for row in profiles
    } == {
        source_id: str(path.relative_to(tmp_path))
        for source_id, path in canonical_paths.items()
    }
    links = {_obsidian_note_link(row) for row in profiles}
    assert all("(2004)" not in link for link in links)
    assert "[[Paris2004 - At war's end [9mh7lag9]|At war's end]]" in links


def test_final_projection_omits_ambiguous_or_unknown_wikilink_targets(
    tmp_path: Path,
) -> None:
    notes_root = tmp_path / "02_source_memory" / "notes"
    notes_root.mkdir(parents=True)
    (notes_root / "First [abcd1234].md").write_text("", encoding="utf-8")
    (notes_root / "Second [abcd1234].md").write_text("", encoding="utf-8")
    (notes_root / "Stale target.md").write_text("", encoding="utf-8")
    report = {
        "projection_profiles": [
            {
                "source_id": "source-zotero-abcd1234",
                "title": "Ambiguous",
                "note_path": "02_source_memory/notes/Stale target.md",
            },
            {
                "source_id": "source-zotero-deadbeef",
                "title": "Missing",
                "note_path": "02_source_memory/notes/Missing target.md",
            },
            {
                "source_id": "custom-source",
                "title": "Unknown custom source",
                "note_path": "02_source_memory/notes/Unknown custom.md",
            },
        ]
    }

    profiles = _canonical_projection_profiles(tmp_path, report)

    assert [row["note_path"] for row in profiles] == ["", "", ""]
    assert [_obsidian_note_link(row) for row in profiles] == [
        "Ambiguous",
        "Missing",
        "Unknown custom source",
    ]


def test_cluster_body_uses_final_canonical_profile_links(tmp_path: Path) -> None:
    notes_root = tmp_path / "02_source_memory" / "notes"
    notes_root.mkdir(parents=True)
    paris_path = notes_root / "Paris2004 - At war's end [9mh7lag9].md"
    unknown_path = notes_root / "Unknownn.d. - Document Viewer [494iyya8].md"
    walter_path = (
        notes_root
        / "Walter2004 - Does Conflict Beget Conflict [iwhwd39r].md"
    )
    for path in (paris_path, unknown_path, walter_path):
        path.write_text("", encoding="utf-8")
    profiles = _canonical_projection_profiles(
        tmp_path,
        {
            "projection_profiles": [
                {
                    "source_id": "source-zotero-9mh7lag9",
                    "zotero_item_key": "9mh7lag9",
                    "title": "At war's end",
                    "note_path": "02_source_memory/notes/Paris (2004).md",
                },
                {
                    "source_id": "source-zotero-494iyya8",
                    "zotero_item_key": "494iyya8",
                    "title": "Document Viewer",
                    "note_path": "02_source_memory/notes/Unknownn.d..md",
                },
                {
                    "source_id": "source-zotero-iwhwd39r",
                    "zotero_item_key": "iwhwd39r",
                    "title": "Does Conflict Beget Conflict?",
                    "note_path": "02_source_memory/notes/Walter (2004).md",
                },
            ]
        },
    )
    profile_by_source = {row["source_id"]: row for row in profiles}
    cluster = {
        "cluster_id": "cluster-a",
        "source_ids": list(profile_by_source),
    }
    synthesis = {
        "title": "Cluster A",
        "retained_member_ids": list(profile_by_source),
        "lines_of_inquiry": [
            {
                "title": "Finding",
                "study_findings": [
                    {
                        "source_id": "source-zotero-9mh7lag9",
                        "finding": "A finding.",
                    }
                ],
            }
        ],
        "additional_cited_works_worth_mapping": [
            {
                "cited_work": "External work",
                "attributions": [
                    {"current_source_id": "source-zotero-494iyya8"}
                ],
            }
        ],
        "material_exclusions": [
            {
                "source_id": "source-zotero-iwhwd39r",
                "boundary": "Outside the boundary.",
            }
        ],
    }

    markdown = _streamlined_cluster_markdown(
        cluster,
        synthesis,
        profile_by_source=profile_by_source,
        cluster_by_id={"cluster-a": cluster},
    )

    for canonical_target in (
        "Paris2004 - At war's end [9mh7lag9]",
        "Unknownn.d. - Document Viewer [494iyya8]",
        "Walter2004 - Does Conflict Beget Conflict [iwhwd39r]",
    ):
        assert f"[[{canonical_target}" in markdown
    for stale_target in (
        "[[Paris (2004)",
        "[[Unknownn.d.]]",
        "[[Walter (2004)",
    ):
        assert stale_target not in markdown


def test_managed_cluster_refresh_replaces_stale_related_cluster_targets(
    tmp_path: Path,
) -> None:
    source = {
        "cluster_id": "cluster-source",
        "label": "Source cluster",
        "source_ids": [],
    }
    target = {
        "cluster_id": "cluster-target-stable-id",
        "label": "Current complete target label used by the canonical cluster note",
    }
    stale_link = (
        "[[Cluster - Current complete target label… "
        "[cluster-target-stable-id]|Cluster: Target]]"
    )
    path = tmp_path / "cluster.md"
    path.write_text(
        "---\n"
        "type: literature_cluster\n"
        f"related_clusters:\n- '{stale_link}'\n"
        "user_field: keep-me\n"
        "---\n\n"
        "User preface.\n\n"
        "<!-- auto-zettelkasten:cluster:start -->\n"
        "## Related clusters\n\n"
        f"- {stale_link}\n"
        "<!-- auto-zettelkasten:cluster:end -->\n\n"
        "User appendix.\n",
        encoding="utf-8",
    )
    synthesis = {
        "cluster_contract": "streamlined-full-note-v2",
        "title": "Source cluster",
        "related_clusters": [{"target_cluster_id": target["cluster_id"]}],
    }
    rendered = _streamlined_cluster_markdown(
        source,
        synthesis,
        profile_by_source={},
        cluster_by_id={source["cluster_id"]: source, target["cluster_id"]: target},
    )

    _write_managed_cluster_projection(
        tmp_path,
        path,
        rendered,
        cluster_id=source["cluster_id"],
    )

    text = path.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(
        text.split("\n---\n", 1)[0].removeprefix("---\n")
    )
    canonical_link = _cluster_wikilink(target)
    assert frontmatter["related_clusters"] == [canonical_link]
    assert canonical_link in text
    assert stale_link not in text
    assert frontmatter["user_field"] == "keep-me"
    assert "User preface." in text
    assert "User appendix." in text
    assert not _write_managed_cluster_projection(
        tmp_path,
        path,
        rendered,
        cluster_id=source["cluster_id"],
    )
    assert path.read_text(encoding="utf-8") == text


def test_prose_humanizer_never_rewrites_keyed_wikilinks() -> None:
    profiles = [
        {
            "source_id": "source-zotero-9mh7lag9",
            "zotero_item_key": "9mh7lag9",
            "citation_key": "Paris2004",
        },
        {
            "source_id": "source-zotero-9vncmnst",
            "zotero_item_key": "9vncmnst",
            "citation_key": "Vreeland2008",
        },
        {
            "source_id": "source-zotero-494iyya8",
            "zotero_item_key": "494iyya8",
            "citation_key": "Unknownn.d.",
        },
    ]
    links = (
        "[[Paris2004 - At war's end [9mh7lag9]|Paris]] and "
        "[[Vreeland2008 - Political Institutions [9vncmnst]|Vreeland]] and "
        "[[Unknownn.d. - Document Viewer [494iyya8]|Document Viewer]]"
    )
    markdown = f"---\ntitle: Cluster\n---\n{links}\n9mh7lag9 9vncmnst 494iyya8"

    rendered = _replace_source_keys_in_markdown_body(markdown, profiles)

    assert links in rendered
    assert rendered.endswith("Paris (2004) Vreeland (2008) Unknownn.d.")


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
