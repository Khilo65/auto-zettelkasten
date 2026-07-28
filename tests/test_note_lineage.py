from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auto_zettelkasten.notes import (
    _assert_source_note_safe_to_replace,
    _write_note_metadata,
    parse_atomic_note,
    render_atomic_note,
    semantic_note_hash,
    source_obsidian_tags,
    update_note_graph,
)


def _note() -> str:
    return """---
note_id: note-1
source_id: source-1
clusters: []
related_notes: []
updated_at: original
---
## Thesis

Substantive claim.
"""


def test_semantic_hash_ignores_generated_graph_projection() -> None:
    before = _note()
    after = before.replace(
        "clusters: []",
        "clusters:\n- cluster-a\ncluster_links:\n- '[[cluster-a]]'\ngaps:\n- gap-a\ngap_links:\n- '[[gap-a]]'\n"
        "tags:\n- auto-zettelkasten/source\n",
    ).replace(
        "Substantive claim.\n",
        "Substantive claim.\n\n## Graph Links\n\n"
        "<!-- auto-zettelkasten:graph:start -->\n"
        "- cluster: [[cluster-a]]\n"
        "<!-- auto-zettelkasten:graph:end -->\n",
    )
    assert semantic_note_hash(before) == semantic_note_hash(after)


def test_semantic_hash_ignores_legacy_review_status_but_not_substantive_edits() -> None:
    legacy = _note().replace(
        "source_id: source-1",
        "source_id: source-1\nhuman_review: not_performed\nengine_version: 0.3.0\nartifact_schema_version: '1.2'",
    ).replace(
        "Substantive claim.\n",
        "Substantive claim.\n\n## Automated Validation\n\nAutomated structure checks passed; human review was not performed.\n",
    )
    cleaned = _note()
    assert semantic_note_hash(legacy) == semantic_note_hash(cleaned)
    assert semantic_note_hash(cleaned) != semantic_note_hash(cleaned.replace("Substantive claim.", "Different claim."))


def test_graph_update_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text(_note(), encoding="utf-8")
    updates = {
        "related_notes": [],
        "clusters": ["cluster-a"],
        "cluster_links": ["[[cluster-a]]"],
        "gaps": ["gap-a"],
        "gap_links": ["[[gap-a]]"],
        "tags": source_obsidian_tags(["Shared Topic"], "analytical_atomic_note"),
        "updated_at": "first-update",
    }
    gap_links = [{"gap_id": "gap-a", "relation_type": "supports_gap_rule"}]
    assert update_note_graph(path, updates, [], ["cluster-a"], gap_links) is True
    first = path.read_bytes()
    assert update_note_graph(path, {**updates, "updated_at": "second-update"}, [], ["cluster-a"], gap_links) is False
    assert path.read_bytes() == first
    frontmatter, body = parse_atomic_note(first.decode())
    assert frontmatter["clusters"] == []
    assert "tags" not in frontmatter
    assert "<!-- auto-zettelkasten:graph:start -->" in body
    assert "<!-- auto-zettelkasten:graph:end -->" in body
    assert "- supports_gap_rule: [[gap-a]]" in body


def test_graph_frontmatter_does_not_wrap_long_obsidian_wikilinks(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text(_note(), encoding="utf-8")
    target = "A Very Long Source Title " * 8
    wikilink = f"[[{target.strip()}]]"
    related = [{"note_id": "note-2", "relation_type": "same_concept", "target_stem": target.strip()}]
    update_note_graph(
        path,
        {"related_notes": [{"note_id": "note-2", "relation_type": "same_concept", "wikilink": wikilink}]},
        related,
        [],
    )
    frontmatter_text = path.read_text().split("\n---\n", 1)[0]
    assert wikilink not in frontmatter_text
    assert wikilink in path.read_text().split("## Graph Links", 1)[1]


def test_graph_update_repairs_wrapped_yaml_link_once_without_timestamp_churn(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text(
        _note().replace(
            "related_notes: []",
            "related_notes:\n- note_id: note-2\n  relation_type: same_concept\n  wikilink: '[[A Long\n    Source Title]]'",
        ).replace(
            "Substantive claim.\n",
            "Substantive claim.\n\n## Graph Links\n\n- same_concept: [[A Long Source Title]]\n",
        ),
        encoding="utf-8",
    )
    related = [{"note_id": "note-2", "relation_type": "same_concept", "target_stem": "A Long Source Title"}]
    updates = {
        "related_notes": [
            {"note_id": "note-2", "relation_type": "same_concept", "wikilink": "[[A Long Source Title]]"}
        ],
        "updated_at": "must-not-replace-original",
    }
    assert update_note_graph(path, updates, related, []) is True
    repaired = path.read_text()
    assert "'[[A Long\n    Source Title]]'" in repaired.split("\n---\n", 1)[0]
    assert "[[A Long Source Title]]" in repaired.split("## Graph Links", 1)[1]
    assert "updated_at: original" in repaired
    assert update_note_graph(path, updates, related, []) is False


def test_graph_update_migrates_only_the_legacy_graph_section(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    legacy = _note().replace(
        "Substantive claim.\n",
        "Substantive claim.\n\n"
        "## Graph Links\n\n"
        "- same_concept: [[Old Target]]\n\n"
        "## Researcher Relationships\n\n"
        "- My interpretation must survive.\n",
    )
    path.write_text(legacy, encoding="utf-8")
    before_hash = semantic_note_hash(legacy)
    related = [
        {
            "note_id": "note-2",
            "relation_type": "supports",
            "target_stem": "New Target",
        }
    ]

    assert update_note_graph(path, {"related_notes": []}, related, []) is True
    migrated = path.read_text(encoding="utf-8")
    assert semantic_note_hash(migrated) == before_hash
    assert "- same_concept: [[Old Target]]" in migrated
    assert "- supports: [[New Target]]" in migrated
    assert "## Researcher Relationships\n\n- My interpretation must survive." in migrated
    assert migrated.count("<!-- auto-zettelkasten:graph:start -->") == 1
    assert migrated.count("<!-- auto-zettelkasten:graph:end -->") == 1

    first = path.read_bytes()
    assert update_note_graph(path, {"related_notes": []}, related, []) is False
    assert path.read_bytes() == first


def test_graph_update_rejects_semantic_frontmatter_changes_before_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.md"
    path.write_text(_note(), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="graph projection changed semantic note content"):
        update_note_graph(path, {"source_id": "different-source"}, [], [])

    assert path.read_bytes() == before


def test_graph_update_rejects_ambiguous_managed_markers_without_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.md"
    path.write_text(
        _note()
        + "\n<!-- auto-zettelkasten:graph:start -->\n"
        + "<!-- auto-zettelkasten:graph:start -->\n"
        + "<!-- auto-zettelkasten:graph:end -->\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(ValueError, match="ambiguous_managed_graph_block"):
        update_note_graph(path, {"related_notes": []}, [], [])

    assert path.read_bytes() == before


def test_relation_id_is_rendered_without_rewriting_frontmatter(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.md"
    original = _note()
    path.write_text(original, encoding="utf-8")

    update_note_graph(
        path,
        {"related_notes": []},
        [
            {
                "relation_id": "relation-shared",
                "relation_type": "supports",
                "target_stem": "Target",
            }
        ],
        [],
    )

    updated = path.read_text()
    assert updated.split("\n---\n", 1)[0] == original.split("\n---\n", 1)[0]
    assert "<!-- relation_id: relation-shared -->" in updated


def test_changed_source_note_with_human_content_is_not_replaceable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path
    path = workspace / "02_source_memory" / "notes" / "note.md"
    path.parent.mkdir(parents=True)
    original = _note()
    path.write_text(original, encoding="utf-8")
    _write_note_metadata(
        workspace,
        path,
        {"note_id": "note-1", "source_id": "source-1"},
        machine_text=original,
    )
    path.write_text(original + "\nHuman annotation.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unmanaged_changes"):
        _assert_source_note_safe_to_replace(workspace, path)


def test_machine_bundle_note_remains_replaceable_after_graph_projection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path
    path = workspace / "02_source_memory" / "notes" / "note.md"
    path.parent.mkdir(parents=True)
    frontmatter = {
        "note_id": "note-1",
        "source_id": "source-1",
        "title": "Source",
        "clusters": [],
        "related_notes": [],
        "updated_at": "original",
    }
    analysis = {"thesis": "Substantive claim."}
    original = render_atomic_note(frontmatter, analysis)
    path.write_text(original, encoding="utf-8")
    _write_note_metadata(workspace, path, frontmatter, machine_text=original)
    bundle_path = workspace / "02_source_memory" / "bundles" / "source-1.yml"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_text(
        yaml.safe_dump({"bundle": {"analysis_sections": analysis}}),
        encoding="utf-8",
    )

    update_note_graph(
        path,
        {"related_notes": ["[[Other Source]]"]},
        [],
        [],
    )

    _assert_source_note_safe_to_replace(workspace, path)
    path.write_text(path.read_text(encoding="utf-8") + "\nHuman annotation.\n")
    with pytest.raises(ValueError, match="unmanaged_changes"):
        _assert_source_note_safe_to_replace(workspace, path)
