from dataclasses import replace
from pathlib import Path

import pytest

from auto_zettelkasten.files import atomic_write_text, read_yaml, write_yaml
from auto_zettelkasten.migration import migrate_v016_metadata
from auto_zettelkasten.literature import reconcile_cluster_registry
from auto_zettelkasten.models import RelationshipDecision, RelationshipPairJob
from auto_zettelkasten.pipeline import (
    _commit_relationship_selection_state,
    _relationship_transport_context,
)
from auto_zettelkasten.readers import (
    ProviderError,
    SECTION_KEYS,
    _normalize_source_bundle_payload,
)
from auto_zettelkasten.relationships import (
    persist_relationship_registry,
    projected_related_links,
    validate_relationship_decision_rows,
)
from auto_zettelkasten.workspace import initialize


def test_identical_atomic_write_preserves_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "state.yml"
    atomic_write_text(path, "value: 1\n")
    before = path.stat()

    atomic_write_text(path, "value: 1\n")

    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_unchanged_cluster_does_not_append_a_lifecycle_event() -> None:
    cluster = {
        "cluster_id": "cluster-a",
        "semantic_identity": "shared question",
        "source_ids": ["A", "B"],
        "revision_hash": "revision-a",
    }
    first = reconcile_cluster_registry([cluster])
    replay = reconcile_cluster_registry([cluster], first)

    assert replay == first


def test_empty_selection_revision_preserves_prior_catalogue_revision(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml"
    )
    write_yaml(
        path,
        {
            "state_schema_version": "3",
            "profile_hashes": {"A": "hash-a"},
            "relationship_memory_hashes": {"A": "memory-a"},
            "reconciled_catalogue_revision": "catalogue-a",
            "catalogue_revision": "catalogue-a",
            "selection_identity": "identity-a",
        },
    )
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    _commit_relationship_selection_state(
        tmp_path,
        {
            "state_path": str(path),
            "selection_identity": "identity-a",
        },
        catalogue_revision="",
    )

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_source_bundle_fills_only_noncritical_missing_sections() -> None:
    payload = {
        "analysis_sections": {
            "thesis": "A bounded thesis.",
            "method_and_research_design": "Comparative analysis.",
            "evidence_and_data": "Two cases.",
            "detailed_findings": "The cases differ.",
        }
    }

    normalized = _normalize_source_bundle_payload(payload)

    assert set(normalized["analysis_sections"]) == set(SECTION_KEYS)
    assert normalized["analysis_sections"]["limitations"].startswith(
        "Not separately returned"
    )
    assert all(
        row["severity"] == "advisory"
        for row in normalized["component_diagnostics"]
    )


def test_source_bundle_rejects_missing_core_analysis() -> None:
    with pytest.raises(ProviderError, match="omitted core content"):
        _normalize_source_bundle_payload(
            {"analysis_sections": {"thesis": "Only a thesis."}}
        )


def test_relationship_packet_deduplicates_shared_source_documents() -> None:
    def job(left: str, right: str) -> RelationshipPairJob:
        return RelationshipPairJob(
            left_source_id=left,
            right_source_id=right,
            profiles={
                "left": {"source_id": left, "dependency_hash": left},
                "right": {"source_id": right, "dependency_hash": right},
            },
            atomic_notes={
                "left": {"source_id": left, "markdown": f"# {left}"},
                "right": {"source_id": right, "markdown": f"# {right}"},
            },
        )

    packet = _relationship_transport_context(
        [job("A", "B"), job("A", "C")],
        decision_contract="relationship-decision-v5",
    )

    assert list(packet["source_documents"]) == ["A", "B", "C"]
    assert len(packet["pair_jobs"]) == 2
    assert all("atomic_notes" not in row for row in packet["pair_jobs"])


def test_pair_context_changes_relationship_job_identity() -> None:
    common = {
        "left_source_id": "A",
        "right_source_id": "B",
        "atomic_notes": {
            "left": {"semantic_hash": "note-a"},
            "right": {"semantic_hash": "note-b"},
        },
    }
    first = RelationshipPairJob(
        **common,
        graph_context={
            "pair_context": {
                "endpoint_profiles": {"A": {"year": "2020"}, "B": {"year": "2021"}}
            }
        },
    )
    changed = RelationshipPairJob(
        **common,
        graph_context={
            "pair_context": {
                "endpoint_profiles": {"A": {"year": "2022"}, "B": {"year": "2021"}}
            }
        },
    )
    downstream_only = RelationshipPairJob(
        **common,
        graph_context={
            "pair_context": {
                "endpoint_profiles": {"A": {"year": "2020"}, "B": {"year": "2021"}}
            },
            "existing_neighbors": [{"relation_id": "machine-output"}],
        },
    )

    assert first.pair_job_id != changed.pair_job_id
    assert first.pair_job_id == downstream_only.pair_job_id


def test_contextual_relationship_has_an_explicit_non_direct_tier() -> None:
    decision = RelationshipDecision(
        decision="relationship",
        left_source_id="A",
        right_source_id="B",
        relation_type="contextual_connection",
        actor_source_id="A",
        reference_source_id="B",
        forward_label="is contextually connected to",
        inverse_label="is contextually connected to",
        comparison_proposition="Together they frame a broader conflict process.",
        reason="They concern adjacent stages but do not test the same proposition.",
        left_evidence_anchor_ids=["anchor-a"],
        right_evidence_anchor_ids=["anchor-b"],
    )

    assert decision.relationship_tier == "contextual"
    with pytest.raises(ValueError, match="tier"):
        replace(decision, relationship_tier="direct")


def test_contextual_relationship_projects_reciprocally(tmp_path: Path) -> None:
    profiles = [
        {
            "source_id": source_id,
            "note_id": f"note-{source_id.lower()}",
            "title": f"Source {source_id}",
            "evidence_anchors": [
                {
                    "evidence_anchor_id": f"anchor-{source_id.lower()}",
                    "source_id": source_id,
                    "claim": f"Claim {source_id}",
                    "locator": "p. 1",
                }
            ],
        }
        for source_id in ("A", "B")
    ]
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        profiles={"left": profiles[0], "right": profiles[1]},
        selected_evidence={
            "left": [profiles[0]["evidence_anchors"][0]],
            "right": [profiles[1]["evidence_anchors"][0]],
        },
    )
    accepted = validate_relationship_decision_rows(
        {
            "decisions": [
                {
                    "pair_job_id": job.pair_job_id,
                    "decision": "relationship",
                    "pair": {
                        "left_source_id": "A",
                        "right_source_id": "B",
                    },
                    "relation_type": "contextual_connection",
                    "relationship_tier": "contextual",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "forward_label": "is contextually connected to",
                    "inverse_label": "is contextually connected to",
                    "comparison_proposition": "The studies concern adjacent stages.",
                    "reason": "Useful context without a shared tested proposition.",
                    "left_evidence_anchor_ids": ["anchor-a"],
                    "right_evidence_anchor_ids": ["anchor-b"],
                    "output_contract": "relationship-decision-v5",
                }
            ]
        },
        jobs=[job],
        profiles=profiles,
    )["accepted"]
    registry = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        accepted_relations=accepted,
    )

    assert accepted[0]["relationship_tier"] == "contextual"
    assert projected_related_links(
        "A", profiles, registry["links"], max_inferred_links=0
    )[0]["primary_relation_type"] == "is contextually connected to"
    assert projected_related_links(
        "B", profiles, registry["links"], max_inferred_links=0
    )[0]["primary_relation_type"] == "is contextually connected to"


def test_v016_migration_is_local_and_idempotent(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config = read_yaml(config_path)
    manifest = read_yaml(manifest_path)
    config.update(
        engine_version="0.15.0",
        artifact_schema_version="1.13",
        prompt_version="10",
    )
    manifest.update(engine_version="0.15.0", artifact_schema_version="1.13")
    write_yaml(config_path, config)
    write_yaml(manifest_path, manifest)
    registry_path = (
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    )
    write_yaml(
        registry_path,
        {
            "registry_schema_version": "5",
            "relations": [
                {
                    "relation_id": "machine-a-b",
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "provenance": "probabilistic_relationship_adjudication",
                    "active": True,
                }
            ],
            "links": [],
        },
    )

    first = migrate_v016_metadata(tmp_path)
    before = (config_path.stat().st_mtime_ns, manifest_path.stat().st_mtime_ns)
    second = migrate_v016_metadata(tmp_path)

    assert first["status"] == "migrated"
    assert first["provider_calls"] == 0
    assert first["source_notes_rewritten"] == 0
    assert second["status"] == "already_current"
    registry = read_yaml(registry_path)
    assert registry["registry_schema_version"] == "6"
    assert registry["relations"][0]["relationship_tier"] == "direct"
    assert registry["relation_counts"] == {"supports": 1}
    assert registry["graph_projection_hash"]
    assert before == (
        config_path.stat().st_mtime_ns,
        manifest_path.stat().st_mtime_ns,
    )
