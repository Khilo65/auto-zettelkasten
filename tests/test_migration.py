from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import (
    DEBATE_FAMILY_MIGRATION_ID,
    GAP_QUALITY_MIGRATION_ID,
    MANAGED_GRAPH_MIGRATION_ID,
    MIGRATION_ID,
    NAVIGATION_MIGRATION_ID,
    PROPOSITION_ANCHOR_MIGRATION_ID,
    RESEARCHER_GRADE_MIGRATION_ID,
    REVIEW_MIGRATION_ID,
    THEMATIC_CLUSTER_MIGRATION_ID,
    migrate_debate_family_schema,
    migrate_gap_quality_schema,
    migrate_literature_map,
    migrate_managed_graph_schema,
    migrate_navigation_projection_schema,
    migrate_proposition_anchor_schema,
    migrate_review_status,
    migrate_researcher_grade_schema,
    migrate_thematic_cluster_schema,
    migrate_workspace,
    review_hash_aliases,
)
from auto_zettelkasten.workspace import initialize


def test_schema_1_6_researcher_grade_migration_archives_only_current_projections(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.7.0", artifact_schema_version="1.6")
        write_yaml(path, payload)

    note = tmp_path / "02_source_memory" / "notes" / "Source.md"
    profile = tmp_path / "02_source_memory" / "profiles" / "note-source.yml"
    historical = tmp_path / "03_literature_synthesis" / "maps" / "old-map" / "manifest.yml"
    projection = tmp_path / "03_literature_synthesis" / "clusters" / "Cluster - Old.md"
    for path, content in (
        (note, b"# Source\n"),
        (profile, b"profile_schema_version: '1.1'\n"),
        (historical, b"artifact_schema_version: '1.6'\n"),
        (projection, b"# Old projection\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    preserved = {path: path.read_bytes() for path in (note, profile, historical, projection)}

    dry_run = migrate_researcher_grade_schema(tmp_path, dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert all(path.read_bytes() == content for path, content in preserved.items())

    first = migrate_researcher_grade_schema(tmp_path)
    second = migrate_researcher_grade_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert first["provider_calls"] == 0
    assert first["source_documents_reread"] == 0
    assert first["source_notes_rewritten"] == 0
    assert first["profile_files_rewritten"] == 0
    assert all(path.read_bytes() == content for path, content in preserved.items())
    archived_sources = {row["source"] for row in first["archived_files"]}
    assert str(projection.relative_to(tmp_path)) in archived_sources
    assert str(historical.relative_to(tmp_path)) not in archived_sources
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["engine_version"] == "0.8.0"
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.7"
    assert (tmp_path / "11_state" / "migrations" / f"{RESEARCHER_GRADE_MIGRATION_ID}.yml").is_file()


def test_schema_1_7_debate_family_migration_is_local_and_idempotent(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.8.0", artifact_schema_version="1.7")
        write_yaml(path, payload)
    note = tmp_path / "02_source_memory" / "notes" / "Source.md"
    profile = tmp_path / "02_source_memory" / "profiles" / "note-source.yml"
    projection = tmp_path / "03_literature_synthesis" / "clusters" / "Cluster - Old.md"
    historical = tmp_path / "03_literature_synthesis" / "maps" / "old-map" / "manifest.yml"
    for path, content in (
        (note, b"# Source\n"),
        (profile, b"profile_schema_version: '1.2'\n"),
        (projection, b"# Old cluster\n"),
        (historical, b"artifact_schema_version: '1.7'\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    preserved = {path: path.read_bytes() for path in (note, profile, projection, historical)}

    dry_run = migrate_debate_family_schema(tmp_path, dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert all(path.read_bytes() == content for path, content in preserved.items())

    first = migrate_debate_family_schema(tmp_path)
    second = migrate_debate_family_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert first["provider_calls"] == 0
    assert first["source_documents_reread"] == 0
    assert first["source_notes_rewritten"] == 0
    assert first["profile_files_rewritten"] == 0
    assert all(path.read_bytes() == content for path, content in preserved.items())
    archived_sources = {row["source"] for row in first["archived_files"]}
    assert str(projection.relative_to(tmp_path)) in archived_sources
    assert str(historical.relative_to(tmp_path)) not in archived_sources
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["engine_version"] == "0.9.0"
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.8"
    assert (tmp_path / "11_state" / "migrations" / f"{DEBATE_FAMILY_MIGRATION_ID}.yml").is_file()


def test_schema_1_8_thematic_cluster_migration_retires_only_mutable_markdown(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.9.0", artifact_schema_version="1.8")
        write_yaml(path, payload)

    note = tmp_path / "02_source_memory" / "notes" / "Source.md"
    profile = tmp_path / "02_source_memory" / "profiles" / "note-source.yml"
    historical_map = (
        tmp_path
        / "03_literature_synthesis"
        / "maps"
        / "old-map"
        / "Literature Map - Historical.md"
    )
    machine_sidecar = tmp_path / "03_literature_synthesis" / "cluster_registry.yml"
    projections = (
        tmp_path / "03_literature_synthesis" / "Literature Map - Mediation [map-old].md",
        tmp_path / "03_literature_synthesis" / "Literature Neighborhoods - Mediation.md",
        tmp_path / "03_literature_synthesis" / "INDEX.md",
        tmp_path / "03_literature_synthesis" / "clusters" / "Cluster - Old.md",
        tmp_path / "03_literature_synthesis" / "clusters" / "INDEX.md",
        tmp_path / "03_literature_synthesis" / "gaps" / "Gap - Old.md",
        tmp_path / "03_literature_synthesis" / "gaps" / "INDEX.md",
    )
    for path, content in (
        (note, b"# Source\n"),
        (profile, b"profile_schema_version: '1.2'\n"),
        (historical_map, b"# Immutable historical map\n"),
        (machine_sidecar, b"clusters: []\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for path in projections:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"# Generated {path.stem}\n".encode())
    preserved = {
        path: path.read_bytes() for path in (note, profile, historical_map, machine_sidecar)
    }
    projection_bytes = {path: path.read_bytes() for path in projections}

    dry_run = migrate_thematic_cluster_schema(tmp_path, dry_run=True)

    assert dry_run["status"] == "dry_run"
    assert dry_run["provider_calls"] == 0
    assert dry_run["source_documents_reread"] == 0
    assert dry_run["source_notes_rewritten"] == 0
    assert dry_run["profile_files_rewritten"] == 0
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert all(path.read_bytes() == content for path, content in projection_bytes.items())
    assert not (
        tmp_path / "11_state" / "migrations" / f"{THEMATIC_CLUSTER_MIGRATION_ID}.yml"
    ).exists()

    first = migrate_thematic_cluster_schema(tmp_path)

    assert first["status"] == "migrated"
    assert {row["source"] for row in first["archived_files"]} == {
        str(path.relative_to(tmp_path)) for path in projections
    }
    assert all(not path.exists() for path in projections)
    assert all(
        (tmp_path / row["archive"]).read_bytes()
        == projection_bytes[tmp_path / row["source"]]
        for row in first["archived_files"]
    )
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["engine_version"] == "0.10.0"
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.9"

    regenerated = projections[0]
    regenerated.write_bytes(b"# New thematic map\n")
    second = migrate_thematic_cluster_schema(tmp_path)

    assert second["status"] == "already_migrated"
    assert regenerated.read_bytes() == b"# New thematic map\n"
    assert all(path.read_bytes() == content for path, content in preserved.items())


def test_schema_1_9_managed_graph_migration_only_rewrites_versions(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    version_paths = (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    )
    for path in version_paths:
        payload = read_yaml(path)
        payload.update(engine_version="0.10.0", artifact_schema_version="1.9")
        write_yaml(path, payload)

    preserved_paths = (
        tmp_path / "01_custody" / "files" / "source.pdf",
        tmp_path / "02_source_memory" / "notes" / "Source.md",
        tmp_path / "02_source_memory" / "profiles" / "note-source.yml",
        tmp_path / "03_literature_synthesis" / "maps" / "map-1" / "manifest.yml",
    )
    for path, content in zip(
        preserved_paths,
        (b"source", b"# Source\n", b"profile: {}\n", b"map_id: map-1\n"),
        strict=True,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    preserved = {path: path.read_bytes() for path in preserved_paths}

    dry_run = migrate_managed_graph_schema(tmp_path, dry_run=True)

    assert dry_run["status"] == "dry_run"
    assert {row["source"] for row in dry_run["rewritten_files"]} == {
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    }
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert read_yaml(version_paths[0])["artifact_schema_version"] == "1.9"
    assert not (
        tmp_path / "11_state" / "migrations" / f"{MANAGED_GRAPH_MIGRATION_ID}.yml"
    ).exists()

    first = migrate_managed_graph_schema(tmp_path)
    second = migrate_managed_graph_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert first["provider_calls"] == 0
    assert first["source_documents_reread"] == 0
    assert first["source_notes_rewritten"] == 0
    assert first["profile_files_rewritten"] == 0
    assert all(path.read_bytes() == content for path, content in preserved.items())
    for path in version_paths:
        payload = read_yaml(path)
        assert payload["engine_version"] == "0.11.0"
        assert payload["artifact_schema_version"] == "1.10"


def test_literature_migration_dry_run_is_non_mutating(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.2.0", artifact_schema_version="1.1")
        write_yaml(path, payload)
    legacy = tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml"
    write_yaml(legacy, {"clusters": [{"cluster_id": "old"}]})

    result = migrate_literature_map(tmp_path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["source_notes_rewritten"] is False
    assert legacy.exists()
    assert not (tmp_path / "11_state" / "migrations" / f"{MIGRATION_ID}.yml").exists()


def test_literature_migration_archives_once_and_keeps_projection(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.2.0", artifact_schema_version="1.1")
        write_yaml(path, payload)
    legacy = tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml"
    write_yaml(legacy, {"gap_candidates": [{"gap_id": "old"}]})

    first = migrate_literature_map(tmp_path)
    second = migrate_literature_map(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert legacy.exists()
    archived = [row for row in first["archived_files"] if row["source"].endswith("gaps.yml")]
    assert len(archived) == 1
    assert (tmp_path / archived[0]["archive"]).exists()


def test_malformed_migration_marker_fails_explicitly(tmp_path: Path) -> None:
    initialize(tmp_path)
    marker = tmp_path / "11_state" / "migrations" / f"{MIGRATION_ID}.yml"
    write_yaml(
        marker,
        {
            "migration_id": MIGRATION_ID,
            "migration_version": "1",
            "status": "completed",
            "archive_directory": "",
            "archived_files": [],
            "completed_at": "now",
            "unexpected": True,
        },
    )

    with pytest.raises(ValueError, match="unknown literature-map migration fields"):
        migrate_literature_map(tmp_path)


def test_schema_1_2_review_cleanup_is_local_archived_and_idempotent(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    config.update(engine_version="0.3.0", artifact_schema_version="1.2")
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    manifest = read_yaml(manifest_path)
    manifest.update(engine_version="0.3.0", artifact_schema_version="1.2")
    write_yaml(manifest_path, manifest)
    note_path = tmp_path / "02_source_memory" / "notes" / "Legacy.md"
    note_path.write_text(
        """---
note_id: note-legacy
source_id: source-legacy
human_review: not_performed
engine_version: 0.3.0
artifact_schema_version: '1.2'
---
# Legacy

## Thesis

The substantive analysis remains unchanged.

## Automated Validation

Automated structure checks passed. No substantive human verification was performed.
""",
        encoding="utf-8",
    )
    before = note_path.read_bytes()

    dry_run = migrate_review_status(tmp_path, dry_run=True)
    assert dry_run["provider_calls"] == 0
    assert note_path.read_bytes() == before
    assert not (tmp_path / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml").exists()

    result = migrate_workspace(tmp_path)
    assert result["provider_calls"] == 0
    cleaned = note_path.read_text(encoding="utf-8")
    assert "human_review" not in cleaned
    assert "Automated Validation" not in cleaned
    assert "The substantive analysis remains unchanged." in cleaned
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.20"
    aliases = review_hash_aliases(tmp_path)
    assert aliases["note-legacy"]["legacy_semantic_hash"]
    assert aliases["note-legacy"]["semantic_hash"]
    rewritten = result["review_status"]["rewritten_files"]
    note_archive = next(row for row in rewritten if row["source"].endswith("Legacy.md"))
    assert (tmp_path / note_archive["archive"]).read_bytes() == before

    replay = migrate_workspace(tmp_path)
    assert replay["review_status"]["status"] == "already_migrated"
    assert replay["gap_quality"]["status"] == "not_applicable"
    assert replay["proposition_anchors"]["status"] == "already_migrated"
    assert replay["provider_calls"] == 0


def test_schema_1_3_to_1_5_upgrade_does_not_rewrite_source_notes(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload["engine_version"] = "0.4.0"
        payload["artifact_schema_version"] = "1.3"
        write_yaml(path, payload)
    note_path = tmp_path / "02_source_memory" / "notes" / "Current.md"
    note_path.write_text(
        """---
note_id: note-current
source_id: source-current
engine_version: 0.4.0
artifact_schema_version: '1.3'
---
# Current

## Thesis

Keep these bytes exactly.
""",
        encoding="utf-8",
    )
    before = note_path.read_bytes()

    result = migrate_workspace(tmp_path)

    assert result["provider_calls"] == 0
    assert note_path.read_bytes() == before
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.20"
    assert read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")["artifact_schema_version"] == "1.20"


def test_schema_1_4_to_1_5_dry_run_is_fully_non_mutating(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.5.0", artifact_schema_version="1.4")
        write_yaml(path, payload)

    note_path = tmp_path / "02_source_memory" / "notes" / "Current.md"
    note_path.write_bytes(b"---\nnote_id: note-current\n---\n\n# Keep these exact bytes.\n")
    profile_path = tmp_path / "02_source_memory" / "profiles" / "note-current.yml"
    profile_path.write_bytes(b"profile_schema_version: '1'\nprofile: {note_id: note-current}\n")
    historical_map = tmp_path / "03_literature_synthesis" / "maps" / "map-1" / "manifest.yml"
    historical_map.parent.mkdir(parents=True)
    historical_map.write_bytes(b"artifact_schema_version: '1.4'\nmap_id: map-1\n")
    compatibility_projection = tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml"
    compatibility_projection.write_bytes(b"clusters: [{cluster_id: current}]\n")
    custody_file = tmp_path / "01_custody" / "files" / "source.pdf"
    custody_file.write_bytes(b"source-document-bytes")
    before = {
        path: path.read_bytes()
        for path in (
            tmp_path / "auto-zettelkasten.yml",
            tmp_path / "11_state" / "workspace_manifest.yml",
            note_path,
            profile_path,
            historical_map,
            compatibility_projection,
            custody_file,
        )
    }
    real_open = Path.open

    def reject_source_document_reads(path: Path, *args, **kwargs):
        if path == custody_file:
            raise AssertionError("migration reread a source document")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_source_document_reads)

    result = migrate_workspace(tmp_path, dry_run=True)
    monkeypatch.undo()

    assert result["status"] == "dry_run"
    assert result["provider_calls"] == 0
    assert result["proposition_anchors"]["status"] == "dry_run"
    assert result["proposition_anchors"]["source_documents_reread"] == 0
    assert result["proposition_anchors"]["source_notes_rewritten"] == 0
    assert result["proposition_anchors"]["profile_files_rewritten"] == 0
    assert result["proposition_anchors"]["profile_upgrade"] == "lazy_on_read"
    assert result["proposition_anchors"]["archived_files"] == []
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (
        tmp_path / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml"
    ).exists()


def test_schema_1_4_to_1_5_apply_is_local_byte_preserving_and_idempotent(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.5.0", artifact_schema_version="1.4")
        write_yaml(path, payload)

    note_path = tmp_path / "02_source_memory" / "notes" / "Current.md"
    note_path.write_bytes(b"---\nnote_id: note-current\nreview_status: pending\n---\n\n# Keep bytes.\n")
    profile_path = tmp_path / "02_source_memory" / "profiles" / "note-current.yml"
    profile_path.write_bytes(b"profile_schema_version: '1'\nprofile: {note_id: note-current}\n")
    historical_map = tmp_path / "03_literature_synthesis" / "maps" / "map-1" / "manifest.yml"
    historical_map.parent.mkdir(parents=True)
    historical_map.write_bytes(b"artifact_schema_version: '1.4'\nmap_id: map-1\n")
    compatibility_projection = tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml"
    compatibility_projection.write_bytes(b"gap_candidates: [{gap_id: current}]\n")
    preserved = {
        path: path.read_bytes() for path in (note_path, profile_path, historical_map, compatibility_projection)
    }

    first = migrate_workspace(tmp_path)
    marker = tmp_path / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml"
    archive_directories = sorted((tmp_path / "11_state" / "legacy_maps").iterdir())
    second = migrate_workspace(tmp_path)

    assert first["provider_calls"] == 0
    assert first["literature_map"]["status"] == "not_applicable"
    assert first["review_status"]["status"] == "not_applicable"
    assert first["gap_quality"]["status"] == "not_applicable"
    assert first["proposition_anchors"]["status"] == "migrated"
    assert first["proposition_anchors"]["profile_upgrade"] == "lazy_on_read"
    assert first["proposition_anchors"]["archived_files"] == []
    assert marker.is_file()
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["engine_version"] == "0.29.6"
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["prompt_version"] == "11"
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.20"
    assert read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")["engine_version"] == "0.29.6"
    assert read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")["artifact_schema_version"] == "1.20"
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert second["proposition_anchors"]["status"] == "already_migrated"
    assert second["navigation"]["status"] == "already_migrated"
    assert second["v015"]["status"] == "not_applicable"
    assert sorted((tmp_path / "11_state" / "legacy_maps").iterdir()) == archive_directories
    assert all(path.read_bytes() == content for path, content in preserved.items())


def test_schema_1_5_is_never_downgraded_through_old_review_or_gap_migrations(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.6.0", artifact_schema_version="1.5")
        write_yaml(path, payload)
    note_path = tmp_path / "02_source_memory" / "notes" / "Current.md"
    note_path.write_bytes(b"---\nreview_status: pending\n---\n\n# Keep bytes.\n")
    before = note_path.read_bytes()

    review = migrate_review_status(tmp_path)
    gap = migrate_gap_quality_schema(tmp_path)

    assert review["status"] == "not_applicable"
    assert gap["status"] == "not_applicable"
    assert note_path.read_bytes() == before
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.5"
    assert read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")[
        "artifact_schema_version"
    ] == "1.5"
    assert not (tmp_path / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml").exists()
    assert not (tmp_path / "11_state" / "migrations" / f"{GAP_QUALITY_MIGRATION_ID}.yml").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unexpected", True, "unknown proposition-anchor migration fields"),
        ("profile_upgrade", "eager_rewrite", "malformed proposition-anchor migration marker"),
    ],
)
def test_proposition_anchor_marker_rejects_unknown_and_malformed_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    initialize(tmp_path)
    marker = tmp_path / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml"
    payload = {
        "migration_id": PROPOSITION_ANCHOR_MIGRATION_ID,
        "migration_version": "1",
        "status": "completed",
        "target_engine_version": "0.6.0",
        "target_artifact_schema_version": "1.5",
        "rewritten_files": [],
        "archived_files": [],
        "provider_calls": 0,
        "source_documents_reread": 0,
        "source_notes_rewritten": 0,
        "profile_files_rewritten": 0,
        "profile_upgrade": "lazy_on_read",
        "completed_at": "now",
    }
    payload[field] = value
    write_yaml(marker, payload)

    with pytest.raises(ValueError, match=message):
        migrate_proposition_anchor_schema(tmp_path)


def test_migrate_workspace_preflights_malformed_markers_before_any_write(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.3.0", artifact_schema_version="1.2")
        write_yaml(path, payload)
    note_path = tmp_path / "02_source_memory" / "notes" / "Legacy.md"
    note_path.write_bytes(b"---\nreview_status: pending\n---\n\n# Keep bytes.\n")
    gap_marker = tmp_path / "11_state" / "migrations" / f"{GAP_QUALITY_MIGRATION_ID}.yml"
    write_yaml(
        gap_marker,
        {
            "migration_id": GAP_QUALITY_MIGRATION_ID,
            "migration_version": "1",
            "status": "completed",
            "archive_directory": "",
            "rewritten_files": [],
            "provider_calls": 0,
            "completed_at": "now",
            "unexpected": True,
        },
    )
    before = {
        path: path.read_bytes()
        for path in (
            tmp_path / "auto-zettelkasten.yml",
            tmp_path / "11_state" / "workspace_manifest.yml",
            note_path,
        )
    }

    with pytest.raises(ValueError, match="unknown gap-quality migration fields"):
        migrate_workspace(tmp_path)

    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (tmp_path / "11_state" / "migrations" / f"{REVIEW_MIGRATION_ID}.yml").exists()


def test_completed_proposition_anchor_marker_cannot_mask_an_unmigrated_workspace(tmp_path: Path) -> None:
    initialize(tmp_path)
    for path in (
        tmp_path / "auto-zettelkasten.yml",
        tmp_path / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.5.0", artifact_schema_version="1.4")
        write_yaml(path, payload)
    marker = tmp_path / "11_state" / "migrations" / f"{PROPOSITION_ANCHOR_MIGRATION_ID}.yml"
    write_yaml(
        marker,
        {
            "migration_id": PROPOSITION_ANCHOR_MIGRATION_ID,
            "migration_version": "1",
            "status": "completed",
            "target_engine_version": "0.6.0",
            "target_artifact_schema_version": "1.5",
            "rewritten_files": [],
            "archived_files": [],
            "provider_calls": 0,
            "source_documents_reread": 0,
            "source_notes_rewritten": 0,
            "profile_files_rewritten": 0,
            "profile_upgrade": "lazy_on_read",
            "completed_at": "now",
        },
    )
    before = {
        path: path.read_bytes()
        for path in (
            tmp_path / "auto-zettelkasten.yml",
            tmp_path / "11_state" / "workspace_manifest.yml",
        )
    }

    with pytest.raises(ValueError, match="marker disagrees with workspace schema"):
        migrate_workspace(tmp_path)

    assert all(path.read_bytes() == content for path, content in before.items())


def test_schema_1_5_navigation_migration_archives_only_graph_state_and_is_idempotent(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config = read_yaml(config_path)
    config.pop("navigation", None)
    config.update(engine_version="0.6.0", artifact_schema_version="1.5")
    write_yaml(config_path, config)
    manifest = read_yaml(manifest_path)
    manifest.update(engine_version="0.6.0", artifact_schema_version="1.5")
    write_yaml(manifest_path, manifest)

    note_path = tmp_path / "02_source_memory" / "notes" / "Preserved.md"
    note_path.write_bytes(b"---\nnote_id: note-preserved\n---\n\n# Preserve exactly.\n")
    profile_path = tmp_path / "02_source_memory" / "profiles" / "note-preserved.yml"
    profile_path.write_bytes(b"profile_schema_version: '1.1'\nprofile: {note_id: note-preserved}\n")
    legacy_tag_path = tmp_path / "02_source_memory" / "indexes" / "tag_registry.yml"
    legacy_neighborhood_path = tmp_path / "03_literature_synthesis" / "topic_neighborhoods.yml"
    write_yaml(legacy_tag_path, {"tags": [{"normalized_tag": "shared-topic"}]})
    write_yaml(legacy_neighborhood_path, {"topic_neighborhoods": [{"kind": "tag"}]})
    preserved = {note_path: note_path.read_bytes(), profile_path: profile_path.read_bytes()}

    dry_run = migrate_navigation_projection_schema(tmp_path, dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert dry_run["provider_calls"] == 0
    assert dry_run["source_documents_reread"] == 0
    assert dry_run["source_notes_rewritten"] == 0
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert not (tmp_path / "11_state" / "migrations" / f"{NAVIGATION_MIGRATION_ID}.yml").exists()

    first = migrate_navigation_projection_schema(tmp_path)
    second = migrate_navigation_projection_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert read_yaml(config_path)["engine_version"] == "0.7.0"
    assert read_yaml(config_path)["artifact_schema_version"] == "1.6"
    assert read_yaml(config_path)["navigation"]["subject_tags_enabled"] is True
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert len(first["archived_files"]) == 2
    assert all((tmp_path / row["archive"]).is_file() for row in first["archived_files"])
