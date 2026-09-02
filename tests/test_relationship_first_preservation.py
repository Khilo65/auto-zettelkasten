from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import build_source_catalogue
from auto_zettelkasten.relationships import persist_relationship_registry


def _membership_rows() -> list[dict[str, object]]:
    return [
        {
            "relation_id": "cluster-member-source-a-cluster-old",
            "source_kind": "source",
            "source_id": "source-a",
            "source_note_id": "note-a",
            "target_kind": "cluster",
            "target_cluster_id": "cluster-old",
            "relation_type": "cluster_member",
            "cluster_role": "core",
            "provenance": "admitted_cluster_registry",
            "active": True,
        },
        {
            "relation_id": "cluster-has-member-cluster-old-source-a",
            "source_kind": "cluster",
            "source_id": "cluster-old",
            "target_kind": "source",
            "target_source_id": "source-a",
            "target_note_id": "note-a",
            "relation_type": "has_member",
            "cluster_role": "core",
            "provenance": "admitted_cluster_registry",
            "active": True,
        },
    ]


def _note(*, thesis: str) -> dict[str, object]:
    return {
        "source_id": "source-a",
        "note_id": "note-a",
        "title": "Study A",
        "date": "2024",
        "creators": [{"lastName": "Author"}],
        "thesis": thesis,
        "method": "Comparative analysis.",
    }


def test_protected_cluster_memberships_survive_reconciliation_orphans_and_replay(
    tmp_path: Path,
) -> None:
    membership_rows = _membership_rows()
    unrelated = {
        "relation_id": "citation-old",
        "source_id": "source-a",
        "target_source_id": "source-b",
        "relation_type": "cites",
        "active": True,
    }
    seeded = persist_relationship_registry(
        tmp_path,
        structural_relations=[*membership_rows, unrelated],
    )
    protected = [
        dict(row)
        for row in seeded["relations"]
        if row["relation_type"] in {"cluster_member", "has_member"}
    ]

    reconciled = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        protected_structural_relations=[*protected, unrelated],
        reconcile_machine_prompt_version="99",
    )
    assert reconciled["relations"] == protected
    registry = Path(reconciled["path"])
    reconciled_bytes = registry.read_bytes()

    orphaned = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        protected_structural_relations=protected,
        orphaned_source_ids=["source-a"],
        reconcile_machine_prompt_version="99",
    )
    assert orphaned["relations"] == protected
    assert registry.read_bytes() == reconciled_bytes

    replay = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        protected_structural_relations=protected,
        orphaned_source_ids=["source-a"],
        reconcile_machine_prompt_version="99",
    )
    assert replay["revision_hash"] == orphaned["revision_hash"]
    assert registry.read_bytes() == reconciled_bytes


def test_catalogue_refresh_preserves_cluster_fields_and_dedicated_outputs(
    tmp_path: Path,
) -> None:
    profile = {
        "source_id": "source-a",
        "note_id": "note-a",
        "concepts": ["original concept"],
    }
    cluster = {
        "cluster_id": "cluster-old",
        "display_label": "Old cluster",
        "source_ids": ["source-a"],
    }
    initial = build_source_catalogue(tmp_path, [profile], [_note(thesis="Old.")], [cluster])
    catalogue_path = Path(initial["catalogue_path"])
    cluster_catalogue_path = Path(initial["cluster_catalogue_path"])
    cluster_index_path = Path(initial["cluster_index_path"])
    initial_catalogue = read_yaml(catalogue_path)
    dedicated_bytes = {
        cluster_catalogue_path: cluster_catalogue_path.read_bytes(),
        cluster_index_path: cluster_index_path.read_bytes(),
    }
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "links": [
                {
                    "relation_id": "relationship-new",
                    "source_kind": "source",
                    "source_id": "source-a",
                    "target_kind": "source",
                    "target_source_id": "source-b",
                    "relation_type": "supports",
                    "confidence": 0.9,
                    "active": True,
                },
                {
                    "relation_id": "cluster-has-member-cluster-old-source-a",
                    "source_kind": "cluster",
                    "source_id": "cluster-old",
                    "target_kind": "source",
                    "target_source_id": "source-a",
                    "relation_type": "has_member",
                    "confidence": 1.0,
                    "active": True,
                },
            ]
        },
    )

    refreshed = build_source_catalogue(
        tmp_path,
        [{**profile, "concepts": ["updated concept"]}],
        [_note(thesis="Updated.")],
        [{"cluster_id": "cluster-new", "source_ids": ["source-a"]}],
        write_cluster_outputs=False,
    )
    refreshed_catalogue = read_yaml(catalogue_path)
    assert refreshed_catalogue["clusters"] == initial_catalogue["clusters"]
    assert refreshed_catalogue["sources"][0]["cluster_ids"] == ["cluster-old"]
    assert refreshed_catalogue["sources"][0]["relationship_ids"] == [
        "relationship-new"
    ]
    assert refreshed_catalogue["sources"][0]["thesis"] == "Updated."
    assert all(path.read_bytes() == content for path, content in dedicated_bytes.items())
    assert not ({str(cluster_catalogue_path), str(cluster_index_path)} & set(refreshed["changed_paths"]))

    refreshed_bytes = catalogue_path.read_bytes()
    replay = build_source_catalogue(
        tmp_path,
        [{**profile, "concepts": ["updated concept"]}],
        [_note(thesis="Updated.")],
        [{"cluster_id": "cluster-new", "source_ids": ["source-a"]}],
        write_cluster_outputs=False,
    )
    assert replay["changed_paths"] == []
    assert catalogue_path.read_bytes() == refreshed_bytes
    assert all(path.read_bytes() == content for path, content in dedicated_bytes.items())
