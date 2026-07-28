from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import (
    VERIFIED_GRAPH_MIGRATION_ID,
    migrate_verified_relationship_graph_schema,
    migrate_workspace,
)
from auto_zettelkasten.workspace import initialize


def _downgrade_to_v011(workspace: Path) -> None:
    for path in (
        workspace / "auto-zettelkasten.yml",
        workspace / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.11.0", artifact_schema_version="1.10")
        write_yaml(path, payload)


def test_v012_migration_is_local_idempotent_and_quarantines_machine_links(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v011(tmp_path)
    note = tmp_path / "02_source_memory" / "notes" / "Source.md"
    profile = tmp_path / "02_source_memory" / "profiles" / "note-source.yml"
    note.write_bytes(b"# Source\n\nHuman annotation.\n")
    profile.write_bytes(b"profile_schema_version: '1.2'\nsource_id: source-a\n")
    preserved = {path: path.read_bytes() for path in (note, profile)}

    machine = {
        "relation_id": "machine-1",
        "source_id": "source-a",
        "target_source_id": "source-b",
        "relation_type": "supports",
        "provenance": "probabilistic_relationship_adjudication",
        "active": True,
        "decision_status": "accepted",
    }
    human = {
        "relation_id": "human-1",
        "source_id": "source-a",
        "target_source_id": "source-c",
        "relation_type": "qualifies",
        "provenance": "human_curated",
        "active": True,
        "decision_status": "accepted",
    }
    structural = {
        "link_id": "structural-1",
        "source_id": "source-a",
        "target_source_id": "source-d",
        "relation_type": "cites",
        "provenance": "zotero_relation",
        "active": True,
    }
    pair_decision = {
        "decision_key": "decision-1",
        "source_id": "source-a",
        "target_source_id": "source-b",
        "status": "accepted",
        "prompt_version": "1",
    }
    registry_path = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility_path = (
        tmp_path / "02_source_memory" / "indexes" / "typed_note_links.yml"
    )
    registry = {
        "registry_schema_version": "2",
        "relations": [machine, human, structural],
        "links": [machine, human, structural],
        "pair_decisions": [pair_decision],
    }
    write_yaml(registry_path, registry)
    write_yaml(compatibility_path, registry)
    registry_before = registry_path.read_bytes()

    dry_run = migrate_verified_relationship_graph_schema(tmp_path, dry_run=True)

    assert dry_run["status"] == "dry_run"
    assert dry_run["provider_calls"] == 0
    assert dry_run["source_documents_reread"] == 0
    assert dry_run["source_notes_rewritten"] == 0
    assert dry_run["profile_files_rewritten"] == 0
    assert dry_run["legacy_relationships_deactivated"] == 1
    assert dry_run["human_relationships_preserved"] == 1
    assert registry_path.read_bytes() == registry_before
    assert all(path.read_bytes() == content for path, content in preserved.items())

    first = migrate_verified_relationship_graph_schema(tmp_path)
    first_registry_bytes = registry_path.read_bytes()
    second = migrate_verified_relationship_graph_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert registry_path.read_bytes() == first_registry_bytes
    assert compatibility_path.read_bytes() == first_registry_bytes
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["engine_version"] == "0.12.0"
    assert (
        read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")[
            "artifact_schema_version"
        ]
        == "1.11"
    )

    migrated = read_yaml(registry_path)
    assert migrated["registry_schema_version"] == "3"
    relations = {
        row.get("relation_id") or row.get("link_id"): row
        for row in migrated["relations"]
    }
    assert relations["machine-1"]["active"] is False
    assert relations["machine-1"]["decision_status"] == "legacy_unverified"
    assert relations["human-1"] == human
    assert relations["structural-1"] == structural
    assert {
        row.get("relation_id") or row.get("link_id") for row in migrated["links"]
    } == {"human-1", "structural-1"}
    assert migrated["pair_decisions"] == [pair_decision]
    assert (
        tmp_path / "11_state" / "migrations" / f"{VERIFIED_GRAPH_MIGRATION_ID}.yml"
    ).is_file()


def test_workspace_migration_includes_v012_without_relationship_registry(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v011(tmp_path)

    result = migrate_workspace(tmp_path)

    assert result["verified_graph"]["status"] == "migrated"
    assert result["verified_graph"]["legacy_relationships_deactivated"] == 0
    assert result["provider_calls"] == 0
    assert (
        read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"]
        == "1.11"
    )
