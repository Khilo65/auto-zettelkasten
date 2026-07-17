from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import (
    MIGRATION_ID,
    REVIEW_MIGRATION_ID,
    migrate_literature_map,
    migrate_review_status,
    migrate_workspace,
    review_hash_aliases,
)
from auto_zettelkasten.workspace import initialize


def test_literature_migration_dry_run_is_non_mutating(tmp_path: Path) -> None:
    initialize(tmp_path)
    legacy = tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml"
    write_yaml(legacy, {"clusters": [{"cluster_id": "old"}]})

    result = migrate_literature_map(tmp_path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["source_notes_rewritten"] is False
    assert legacy.exists()
    assert not (tmp_path / "11_state" / "migrations" / f"{MIGRATION_ID}.yml").exists()


def test_literature_migration_archives_once_and_keeps_projection(tmp_path: Path) -> None:
    initialize(tmp_path)
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


def test_schema_1_2_review_cleanup_is_local_archived_and_idempotent(tmp_path: Path) -> None:
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
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.4"
    aliases = review_hash_aliases(tmp_path)
    assert aliases["note-legacy"]["legacy_semantic_hash"]
    assert aliases["note-legacy"]["semantic_hash"]
    rewritten = result["review_status"]["rewritten_files"]
    note_archive = next(row for row in rewritten if row["source"].endswith("Legacy.md"))
    assert (tmp_path / note_archive["archive"]).read_bytes() == before

    replay = migrate_workspace(tmp_path)
    assert replay["review_status"]["status"] == "already_migrated"
    assert replay["gap_quality"]["status"] == "already_migrated"
    assert replay["provider_calls"] == 0


def test_schema_1_3_to_1_4_upgrade_does_not_rewrite_source_notes(tmp_path: Path) -> None:
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
    assert read_yaml(tmp_path / "auto-zettelkasten.yml")["artifact_schema_version"] == "1.4"
    assert read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")[
        "artifact_schema_version"
    ] == "1.4"
