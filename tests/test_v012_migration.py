from __future__ import annotations

import json
from pathlib import Path

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import (
    V013_MIGRATION_ID,
    VERIFIED_GRAPH_MIGRATION_ID,
    migrate_v013_schema,
    migrate_verified_relationship_graph_schema,
    migrate_workspace,
)
from auto_zettelkasten.models import SourceAnalysisBundle
from auto_zettelkasten.notes import render_atomic_note
from auto_zettelkasten.workspace import initialize
from conftest import SECTION_KEYS


def _downgrade_to_v011(workspace: Path) -> None:
    for path in (
        workspace / "auto-zettelkasten.yml",
        workspace / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.11.0", artifact_schema_version="1.10")
        write_yaml(path, payload)


def _downgrade_to_v012(workspace: Path) -> None:
    for path in (
        workspace / "auto-zettelkasten.yml",
        workspace / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.12.0", artifact_schema_version="1.11")
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
        == "1.20"
    )


def test_v013_migration_preserves_visible_legacy_edges_and_marks_them_pending(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v012(tmp_path)
    note = tmp_path / "02_source_memory" / "notes" / "Source.md"
    note.write_bytes(b"# Source\n\nHuman prose stays byte-identical.\n")
    before = note.read_bytes()
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
    registry_path = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    write_yaml(
        registry_path,
        {
            "registry_schema_version": "3",
            "relations": [machine, human],
            "links": [machine, human],
        },
    )

    dry_run = migrate_v013_schema(tmp_path, dry_run=True)
    assert dry_run["provider_calls"] == 0
    assert dry_run["source_documents_reread"] == 0
    assert dry_run["source_notes_rewritten"] == 0
    assert dry_run["legacy_relationships_pending"] == 1
    assert note.read_bytes() == before

    first = migrate_v013_schema(tmp_path)
    second = migrate_v013_schema(tmp_path)

    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert note.read_bytes() == before
    migrated = read_yaml(registry_path)
    assert migrated["registry_schema_version"] == "4"
    relations = {row["relation_id"]: row for row in migrated["relations"]}
    assert relations["machine-1"]["active"] is True
    assert relations["machine-1"]["decision_status"] == "legacy_review_pending"
    assert relations["machine-1"]["cluster_eligible"] is False
    assert relations["human-1"] == human
    assert {row["relation_id"] for row in migrated["links"]} == {
        "machine-1",
        "human-1",
    }
    assert (
        tmp_path / "11_state" / "migrations" / f"{V013_MIGRATION_ID}.yml"
    ).is_file()


def test_v013_migration_wraps_partial_note_profile_without_rewriting_them(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v012(tmp_path)
    note_path = tmp_path / "02_source_memory" / "notes" / "Partial.md"
    frontmatter = _legacy_note_frontmatter(
        source_id="source-partial",
        note_id="note-partial",
        zotero_key="PARTIAL",
        source_scope="partial_document",
        note_status="partial_document_atomic_note",
    )
    note_path.write_text(
        render_atomic_note(
            frontmatter,
            {
                key: f"Recovered-content {key} is bounded to PDF pages 1-39."
                for key in SECTION_KEYS
            },
        ),
        encoding="utf-8",
    )
    profile_path = (
        tmp_path / "02_source_memory" / "profiles" / "note-partial.yml"
    )
    write_yaml(
        profile_path,
        {
            "profile_schema_version": "1",
            "profile": {
                "profile_schema_version": "1.2",
                "source_id": "source-partial",
                "note_id": "note-partial",
                "excluded_from_synthesis": True,
                "coverage": {
                    "source_scope": "partial_document",
                    "coverage_gate": "limited",
                },
                "methods": ["comparative analysis"],
                "mechanisms": ["monitoring"],
                "evidence_anchors": [
                    {
                        "evidence_anchor_id": "anchor-partial",
                        "source_id": "source-partial",
                        "claim": "Monitoring shapes implementation.",
                        "locator": "p. 12",
                    }
                ],
            },
        },
    )
    preserved = {
        note_path: note_path.read_bytes(),
        profile_path: profile_path.read_bytes(),
    }

    first = migrate_v013_schema(tmp_path)
    bundle_path = (
        tmp_path / "02_source_memory" / "bundles" / "source-partial.yml"
    )
    first_bundle = bundle_path.read_bytes()
    second = migrate_v013_schema(tmp_path)

    assert first["legacy_source_bundles_created"] == 1
    assert first["partial_documents_promoted"] == 1
    assert first["profile_files_rewritten"] == 0
    assert first["source_notes_rewritten"] == 0
    assert first["provider_calls"] == first["zotero_calls"] == 0
    assert second["status"] == "already_migrated"
    assert bundle_path.read_bytes() == first_bundle
    assert all(path.read_bytes() == content for path, content in preserved.items())
    sidecar = read_yaml(bundle_path)
    bundle = SourceAnalysisBundle.from_dict(sidecar["bundle"])
    assert sidecar["bundle_origin"] == "legacy_source_analysis_bundle"
    assert sidecar["source_content_hash"] == "a" * 64
    assert sidecar["note_semantic_hash"]
    assert sidecar["legacy_profile_semantic_hash"]
    assert bundle.scope_assessment["evidence_eligibility"] == "substantive_bounded"
    assert bundle.analysis_sections["thesis"]
    assert bundle.literature_positions == []


def test_v013_migration_parks_conflicting_profile_variants(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v012(tmp_path)
    note_root = tmp_path / "02_source_memory" / "notes"
    profile_root = tmp_path / "02_source_memory" / "profiles"
    preserved: dict[Path, bytes] = {}
    for suffix, claim in (("a", "Monitoring supports peace."), ("b", "Monitoring has no effect.")):
        note_id = f"note-{suffix}"
        note_path = note_root / f"Variant-{suffix}.md"
        note_path.write_text(
            render_atomic_note(
                _legacy_note_frontmatter(
                    source_id="source-conflict",
                    note_id=note_id,
                    zotero_key="CONFLICT",
                ),
                {
                    key: claim if key == "thesis" else f"{key}: {claim} See p. 1."
                    for key in SECTION_KEYS
                },
            ),
            encoding="utf-8",
        )
        profile_path = profile_root / f"{note_id}.yml"
        write_yaml(
            profile_path,
            {
                "profile_schema_version": "1",
                "profile": {
                    "profile_schema_version": "1.2",
                    "source_id": "source-conflict",
                    "note_id": note_id,
                    "coverage": {
                        "source_scope": "full_document",
                        "coverage_gate": "passed",
                    },
                    "findings": [{"finding_id": f"finding-{suffix}", "claim": claim}],
                },
            },
        )
        preserved[note_path] = note_path.read_bytes()
        preserved[profile_path] = profile_path.read_bytes()

    result = migrate_v013_schema(tmp_path)

    assert result["legacy_source_bundles_created"] == 0
    assert result["legacy_source_bundle_conflicts"] == 1
    assert not (
        tmp_path / "02_source_memory" / "bundles" / "source-conflict.yml"
    ).exists()
    variants = sorted(
        (
            tmp_path
            / "02_source_memory"
            / "bundles"
            / "legacy_variants"
            / "source-conflict"
        ).glob("*.yml")
    )
    assert len(variants) == 2
    conflicts = read_yaml(
        tmp_path
        / "11_state"
        / "migrations"
        / "v013_source_bundle_conflicts.yml"
    )
    assert conflicts["conflicts"][0]["status"] == "parked_for_review"
    assert len(conflicts["conflicts"][0]["variants"]) == 2
    assert all(path.read_bytes() == content for path, content in preserved.items())


def test_v013_migration_recovers_hash_bound_fidelity_parked_draft(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    _downgrade_to_v012(tmp_path)
    run_root = tmp_path / "11_state" / "runs" / "legacy-run"
    item = {
        "key": "DRAFT1",
        "data": {
            "key": "DRAFT1",
            "title": "Recovered draft",
            "itemType": "journalArticle",
        },
    }
    (run_root / "inventory.json").parent.mkdir(parents=True, exist_ok=True)
    (run_root / "inventory.json").write_text(
        json.dumps([item]), encoding="utf-8"
    )
    item_root = run_root / "items" / "DRAFT1"
    item_root.mkdir(parents=True)
    source_hash = "b" * 64
    (item_root / "source.txt").write_text(
        "Frozen source text is intentionally not reread by migration.",
        encoding="utf-8",
    )
    write_yaml(
        item_root / "frozen_content.yml",
        {
            "checkpoint_version": "1",
            "text_hash": source_hash,
            "source_scope": "full_document",
            "source_coverage": "passed",
        },
    )
    analysis = {
        key: f"Usable recovered {key}; see p. 1."
        for key in SECTION_KEYS
    }
    analysis_hash = _analysis_hash(analysis)
    write_yaml(
        item_root / "direct.yml",
        {
            "identity": {"document_hash": source_hash},
            "analysis": analysis,
        },
    )
    write_yaml(
        item_root / "atomic_fidelity.yml",
        {
            "status": "failed",
            "reason": "atomic_fidelity_risks_unresolved",
            "identity": {
                "source_hash": source_hash,
                "analysis_hash": analysis_hash,
            },
            "risks": [{"risk_id": "locator-warning"}],
        },
    )

    result = migrate_v013_schema(tmp_path)

    assert result["fidelity_drafts_recovered"] == 1
    assert result["legacy_source_bundles_created"] == 1
    assert result["provider_calls"] == result["zotero_calls"] == 0
    assert list((tmp_path / "02_source_memory" / "notes").glob("*.md")) == []
    sidecar = read_yaml(
        tmp_path
        / "02_source_memory"
        / "bundles"
        / "source-zotero-draft1.yml"
    )
    assert sidecar["migration_status"] == "recovered_fidelity_parked_draft"
    assert sidecar["source_content_hash"] == source_hash
    bundle = SourceAnalysisBundle.from_dict(sidecar["bundle"])
    assert bundle.analysis_sections == analysis
    assert bundle.self_review["advisory_warnings"] == [
        {"risk_id": "locator-warning"}
    ]


def _legacy_note_frontmatter(
    *,
    source_id: str,
    note_id: str,
    zotero_key: str,
    source_scope: str = "full_document",
    note_status: str = "analytical_atomic_note",
) -> dict[str, object]:
    return {
        "note_id": note_id,
        "source_id": source_id,
        "note_status": note_status,
        "zotero_item_key": zotero_key,
        "source_file": f"{zotero_key}.pdf",
        "inspected_content_hash": "a" * 64,
        "content_route": "pypdf_text",
        "reader_provider": "deepseek",
        "reader_model": "deepseek-v4-flash",
        "original_zotero_tags": [],
        "normalized_tags": [],
        "related_notes": [],
        "source_scope": source_scope,
        "source_coverage": {
            "gate": "limited" if source_scope == "partial_document" else "passed"
        },
        "coverage_metrics": {
            "page_count": 40,
            "recovered_pages": list(range(1, 40)),
            "unresolved_pages": [40],
            "recovered_page_ratio": 0.975,
        },
        "title": f"Legacy {zotero_key}",
    }


def _analysis_hash(analysis: dict[str, str]) -> str:
    from hashlib import sha256

    return sha256(
        json.dumps(
            analysis,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
