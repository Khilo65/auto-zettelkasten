from __future__ import annotations

import json
from pathlib import Path

from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.readers import _relationship_adjudication_system_prompt
from auto_zettelkasten.relationships import (
    RELATIONSHIP_DECISION_CONTRACT,
    ingest_relationship_decision_batch,
    persist_relationship_registry,
    projected_related_links,
)


def _profile(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id.lower()}",
        "title": f"Source {source_id}",
        "evidence_anchors": [],
    }


def _job() -> RelationshipPairJob:
    return RelationshipPairJob(
        pair_job_id="job-ab",
        left_source_id="A",
        right_source_id="B",
        profiles={"left": _profile("A"), "right": _profile("B")},
        output_contract=RELATIONSHIP_DECISION_CONTRACT,
    )


def _connection(
    proposition: str,
    relation_type: str = "supports",
) -> dict[str, object]:
    return {
        "proposition": proposition,
        "primary_relation_type": relation_type,
        "secondary_relation_types": ["complements"],
        "actor_source_id": "A",
        "reference_source_id": "B",
        "source_a_basis": ["A reports evidence for the proposition."],
        "source_b_basis": "B states the proposition.",
        "reason": "The two source-specific bases establish this connection.",
        "confidence": 0.8,
    }


def test_v8_salvages_valid_connections_and_keeps_anchors_optional() -> None:
    invalid = _connection("A second proposition.", "qualifies")
    invalid["source_b_basis"] = ""
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": "job-ab",
                    "decision": "relationship",
                    "connections": [
                        _connection("A shared proposition."),
                        invalid,
                    ],
                }
            ]
        },
        pair_jobs=[_job()],
        profiles=[_profile("A"), _profile("B")],
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert len(result["accepted"]) == 1
    assert len(result["parked"]) == 1
    accepted = result["accepted"][0]
    assert accepted["comparison_proposition"] == "A shared proposition."
    assert accepted["source_evidence"]["claim"].startswith("A reports")
    assert accepted["target_evidence"]["claim"].startswith("B states")
    assert accepted["source_evidence_anchor_ids"] == []
    assert accepted["secondary_relation_types"] == ["complements"]
    assert accepted["connection_id"].startswith("relationship-connection-")


def test_v14_defines_ordered_taxonomy_and_endpoint_basis_ownership() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "relationship prompt v14" in prompt
    assert "source_a_basis always describes the supplied left_source_id note" in prompt
    assert "source_b_basis always describes the supplied right_source_id note" in prompt
    assert "Choose the primary type in this order" in prompt
    assert "same sufficiently specific proposition" in prompt
    assert "adjacent mechanisms, outcomes, stages" in prompt


def test_v8_accepts_prose_wrapped_singleton_and_relation_shorthand() -> None:
    payload = {
        "decisions": [
            {
                "pair_job_id": "job-ab",
                "decision": "supports",
                **_connection("A shared proposition."),
            }
        ]
    }
    result = ingest_relationship_decision_batch(
        {"decisions": "result follows:\n" + json.dumps(payload)},
        pair_jobs=[_job()],
        profiles=[_profile("A"), _profile("B")],
    )

    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["relation_type"] == "supports"


def test_v8_no_relationship_is_a_complete_pair_decision() -> None:
    result = ingest_relationship_decision_batch(
        {
            "decisions": {
                "job-ab": {
                    "decision": "no_relationship",
                    "rationale": "The apparent overlap is only terminological.",
                }
            }
        },
        pair_jobs=[_job()],
        profiles=[_profile("A"), _profile("B")],
    )

    assert result["accepted"] == []
    assert result["parked"] == []
    assert result["no_relationship"][0]["decision_status"] == "no_relationship"


def test_legacy_v7_endpoint_bases_remain_valid_without_anchor_ids() -> None:
    job = RelationshipPairJob(
        pair_job_id="legacy-job-ab",
        left_source_id="A",
        right_source_id="B",
        output_contract="relationship-decision-v7",
    )
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": job.pair_job_id,
                    "decision": "relationship",
                    "relation_type": "supports",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "comparison_proposition": "A shared proposition.",
                    "left_endpoint_claim": "A reports supporting evidence.",
                    "right_endpoint_claim": "B states the proposition.",
                    "reason": "The endpoint claims establish support.",
                    "confidence": "high",
                }
            ]
        },
        pair_jobs=[job],
        profiles=[_profile("A"), _profile("B")],
    )

    assert result["parked"] == []
    assert result["accepted"][0]["source_evidence_anchor_ids"] == []


def test_registry_keeps_two_connections_under_one_effective_pair_decision(
    tmp_path: Path,
) -> None:
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": "job-ab",
                    "decision": "relationship",
                    "connections": [
                        _connection("A shared proposition."),
                        _connection("A distinct proposition.", "qualifies"),
                    ],
                }
            ]
        },
        pair_jobs=[_job()],
        profiles=[_profile("A"), _profile("B")],
    )
    registry = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        accepted_relations=result["accepted"],
    )

    assert len(registry["links"]) == 2
    assert len(registry["pair_decisions"]) == 2
    assert len(registry["current_pair_decisions"]) == 1
    current = registry["current_pair_decisions"][0]
    assert current["status"] == "accepted"
    assert len(current["relation_ids"]) == 2

    replay_with_parked_attempt = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        parked_rows=[
            {
                "pair_job_id": "new-job-ab",
                "source_id": "A",
                "target_source_id": "B",
                "reason": "malformed_new_attempt",
            }
        ],
    )
    assert len(replay_with_parked_attempt["links"]) == 2
    refreshed = replay_with_parked_attempt["current_pair_decisions"][0]
    assert refreshed["relation_ids"] == current["relation_ids"]
    assert refreshed["refresh_pending"] is True


def test_projection_keeps_same_type_connections_with_distinct_propositions() -> None:
    profiles = [_profile("A"), _profile("B")]
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": "job-ab",
                    "decision": "relationship",
                    "connections": [
                        _connection("A shared proposition."),
                        _connection("A distinct proposition."),
                    ],
                }
            ]
        },
        pair_jobs=[_job()],
        profiles=profiles,
    )

    projected = projected_related_links(
        "A",
        profiles,
        result["accepted"],
        max_inferred_links=0,
    )

    assert len(projected) == 2
    assert len({row["relation_id"] for row in projected}) == 2
    assert len({row["connection_id"] for row in projected}) == 2


def test_schema6_migration_keeps_one_provisional_effective_decision(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "02_source_memory" / "indexes"
    old_rows = [
        {
            "relation_id": f"old-{relation_type}",
            "source_id": "A",
            "target_source_id": "B",
            "relation_type": relation_type,
            "provenance": "probabilistic_relationship_adjudication_v7",
            "decision_status": "accepted",
            "verification_status": "final",
            "output_contract": "relationship-decision-v7",
            "decision_schema_version": "7",
            "reason": "A complete old decision.",
            "active": True,
        }
        for relation_type in ("supports", "contrasts")
    ]
    write_yaml(
        index_dir / "typed_links.yml",
        {
            "registry_schema_version": "6",
            "relations": old_rows,
            "links": old_rows,
        },
    )

    migrated = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        reconcile_machine_prompt_version="12",
    )

    active_machine = [
        row
        for row in migrated["links"]
        if str(row.get("provenance", "")).startswith(
            "probabilistic_relationship_"
        )
    ]
    assert len(active_machine) == 1
    assert active_machine[0]["decision_status"] == "reconciliation_pending"
    assert len(migrated["current_pair_decisions"]) == 1
    current = migrated["current_pair_decisions"][0]
    assert current["source_ids"] == ["A", "B"]
    assert current["status"] == "reconciliation_pending"
    assert current["relation_ids"] == [active_machine[0]["relation_id"]]
    assert current["reconciliation_pending"] is True

    replay = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        reconcile_machine_prompt_version="12",
    )
    replay_machine = [
        row
        for row in replay["links"]
        if str(row.get("provenance", "")).startswith(
            "probabilistic_relationship_"
        )
    ]
    assert [row["relation_id"] for row in replay_machine] == [
        active_machine[0]["relation_id"]
    ]
    assert replay_machine[0]["active"] is True
