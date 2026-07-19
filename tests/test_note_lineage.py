from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.notes import parse_atomic_note, semantic_note_hash, source_obsidian_tags, update_note_graph


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
        "Substantive claim.\n", "Substantive claim.\n\n## Graph Links\n\n- cluster: [[cluster-a]]\n"
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
    assert frontmatter["tags"] == ["shared-topic"]
    assert frontmatter["cluster_links"] == ["[[cluster-a]]"]
    assert frontmatter["gap_links"] == ["[[gap-a]]"]
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
    assert wikilink in frontmatter_text


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
    assert "[[A Long Source Title]]" in repaired.split("\n---\n", 1)[0]
    assert "updated_at: original" in repaired
    assert update_note_graph(path, updates, related, []) is False
