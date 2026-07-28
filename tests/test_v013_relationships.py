from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.literature import _reasoner_context_char_budget
from auto_zettelkasten.literature import (
    RELATIONSHIP_PROMPT_VERSION as LITERATURE_RELATIONSHIP_PROMPT_VERSION,
)
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.readers import (
    RELATIONSHIP_MAX_OUTPUT_TOKENS,
    _relationship_candidate_system_prompt,
)
from auto_zettelkasten.relationships import (
    RELATIONSHIP_DECISION_CONTRACT,
    RELATIONSHIP_PROMPT_VERSION,
    persist_relationship_registry,
    projected_related_links,
    validate_relationship_decision_rows,
)


def test_relationship_discovery_uses_one_bridge_aware_prompt_identity() -> None:
    prompt = _relationship_candidate_system_prompt()

    assert LITERATURE_RELATIONSHIP_PROMPT_VERSION == RELATIONSHIP_PROMPT_VERSION
    assert "globally rank" in prompt
    assert "max_inferred_pairs" in prompt
    assert "cross_literature" in prompt
    assert "left_evidence_anchor_ids" in prompt
    assert "right_evidence_anchor_ids" in prompt
    assert RELATIONSHIP_MAX_OUTPUT_TOKENS == 64_000


def _profile(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id.lower()}",
        "title": f"Source {source_id}",
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id.lower()}",
                "source_id": source_id,
                "proposition": f"Bounded claim from {source_id}",
                "locator": "p. 4",
            }
        ],
    }


def test_million_token_reasoner_uses_measured_half_context_budget() -> None:
    reasoner = type("Reasoner", (), {"context_window_tokens": 1_000_000})()

    budget = _reasoner_context_char_budget(
        reasoner,
        {
            "processing": {"estimated_chars_per_token": 3.5},
            "literature_policy": {"deepseek_packet_context_fraction": 0.8},
        },
    )

    assert budget == 1_680_000


def _job(left: str, right: str) -> RelationshipPairJob:
    profiles = {source_id: _profile(source_id) for source_id in (left, right)}
    return RelationshipPairJob(
        pair_job_id=f"job-{left.lower()}-{right.lower()}",
        catalogue_revision="catalogue-1",
        left_source_id=left,
        right_source_id=right,
        profiles={"left": profiles[left], "right": profiles[right]},
        selected_evidence={
            "left": [profiles[left]["evidence_anchors"][0]],
            "right": [profiles[right]["evidence_anchors"][0]],
        },
    )


def _relationship_row(
    job: RelationshipPairJob,
    *,
    relation_type: str = "supports",
    actor_source_id: str | None = None,
) -> dict[str, Any]:
    actor = actor_source_id or job.left_source_id
    reference = (
        job.right_source_id
        if actor == job.left_source_id
        else job.left_source_id
    )
    labels = {
        "supports": ("supports", "supported by"),
        "extends": ("extends", "extended by"),
    }[relation_type]
    return {
        "pair_job_id": job.pair_job_id,
        "decision": "relationship",
        "pair": {
            "left_source_id": job.left_source_id,
            "right_source_id": job.right_source_id,
        },
        "relation_type": relation_type,
        "actor_source_id": actor,
        "reference_source_id": reference,
        "forward_label": labels[0],
        "inverse_label": labels[1],
        "comparison_proposition": "The works address the same bounded claim.",
        "reason": "Related.",
        "left_evidence_anchor_ids": [
            f"anchor-{job.left_source_id.lower()}"
        ],
        "right_evidence_anchor_ids": [
            f"anchor-{job.right_source_id.lower()}"
        ],
        "confidence": "",
        "output_contract": RELATIONSHIP_DECISION_CONTRACT,
    }


def _profiles(*source_ids: str) -> list[dict[str, Any]]:
    return [_profile(source_id) for source_id in source_ids]


def test_complete_decision_v4_accepts_model_direction_without_semantic_veto() -> None:
    job = _job("A", "B")
    result = validate_relationship_decision_rows(
        {
            "decisions": [
                _relationship_row(
                    job, relation_type="extends", actor_source_id="B"
                )
            ]
        },
        jobs=[job],
        profiles=_profiles("A", "B"),
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert result["parked"] == []
    relation = result["accepted"][0]
    assert relation["source_id"] == "B"
    assert relation["target_source_id"] == "A"
    assert relation["forward_label"] == "extends"
    assert relation["inverse_label"] == "extended by"
    assert relation["reason"] == "Related."
    assert relation["confidence"] == ""


def test_imported_relationship_result_preserves_harness_provenance() -> None:
    job = _job("A", "B")
    row = {
        **_relationship_row(job),
        "reasoner_backend": "codex_harness",
        "provider": "openai",
        "model": "gpt-5.6",
    }

    relation = validate_relationship_decision_rows(
        {"decisions": [row]},
        jobs=[job],
        profiles=_profiles("A", "B"),
    )["accepted"][0]

    assert relation["reasoner_backend"] == "codex_harness"
    assert relation["provider"] == "openai"
    assert relation["model"] == "gpt-5.6"


def test_malformed_decision_rows_are_isolated_from_valid_siblings() -> None:
    jobs = [_job("A", "B"), _job("A", "C"), _job("A", "D")]
    bad_anchor = _relationship_row(jobs[1])
    bad_anchor["left_evidence_anchor_ids"] = ["anchor-c"]
    bad_label = _relationship_row(jobs[2])
    bad_label["inverse_label"] = "supports"

    result = validate_relationship_decision_rows(
        {
            "decisions": [
                _relationship_row(jobs[0]),
                bad_anchor,
                bad_label,
            ]
        },
        jobs=jobs,
        profiles=_profiles("A", "B", "C", "D"),
    )

    assert [row["pair_job_id"] for row in result["accepted"]] == ["job-a-b"]
    assert {
        row["reason"] for row in result["parked"]
    } == {
        "left_anchor_not_owned_by_left_source",
        "relation_labels_do_not_match_type",
    }


def test_schema_four_registry_projects_labels_and_preserves_negative_history(
    tmp_path: Path,
) -> None:
    job = _job("A", "B")
    profiles = _profiles("A", "B")
    accepted = validate_relationship_decision_rows(
        {"decisions": [_relationship_row(job)]},
        jobs=[job],
        profiles=profiles,
    )["accepted"][0]
    human = {
        "relation_id": "human-a-b",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "supports",
        "provenance": "human_authored",
        "active": True,
    }
    first = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        accepted_relations=[accepted],
    )

    from_a = projected_related_links(
        "A", profiles, first["links"], max_inferred_links=0
    )
    from_b = projected_related_links(
        "B", profiles, first["links"], max_inferred_links=0
    )
    assert next(
        row
        for row in from_a
        if row["relation_id"] == accepted["relation_id"]
    )["primary_relation_type"] == "supports"
    assert next(
        row
        for row in from_b
        if row["relation_id"] == accepted["relation_id"]
    )["primary_relation_type"] == "supported by"

    negative_row = {
        "pair_job_id": job.pair_job_id,
        "decision": "no_relationship",
        "pair": {
            "left_source_id": "A",
            "right_source_id": "B",
        },
        "reason": "The apparent overlap is not intellectually substantive.",
        "output_contract": RELATIONSHIP_DECISION_CONTRACT,
    }
    negative = validate_relationship_decision_rows(
        {"decisions": [negative_row]},
        jobs=[job],
        profiles=profiles,
    )["no_relationship"][0]
    result = persist_relationship_registry(
        tmp_path,
        structural_relations=[human],
        no_relationship_decisions=[negative],
        parked_rows=[{"pair_job_id": "job-c-d", "reason": "bad row"}],
    )

    by_id = {row["relation_id"]: row for row in result["relations"]}
    assert by_id[accepted["relation_id"]]["active"] is False
    assert by_id["human-a-b"]["active"] is True
    assert result["parked"] == [
        {"pair_job_id": "job-c-d", "reason": "bad row"}
    ]
    assert {event["event_type"] for event in result["events"]} >= {
        "accepted",
        "no_relationship",
        "parked",
        "retired",
    }


def test_schema_three_visible_machine_edges_become_legacy_review_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility = path.with_name("typed_note_links.yml")
    legacy = {
        "registry_schema_version": "3",
        "relations": [
            {
                "relation_id": "legacy-machine",
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "qualifies",
                "reciprocal_type": "qualified_by",
                "provenance": "probabilistic_relationship_verification",
                "verification_status": "confirmed",
                "active": True,
            }
        ],
        "links": [],
        "pair_decisions": [],
    }
    write_yaml(path, legacy)
    write_yaml(compatibility, legacy)

    result = persist_relationship_registry(tmp_path, structural_relations=[])
    relation = result["relations"][0]

    assert read_yaml(path)["registry_schema_version"] == "4"
    assert relation["active"] is True
    assert relation["decision_status"] == "legacy_review_pending"
    assert relation["cluster_evidence_eligible"] is False
    assert result["links"][0]["relation_id"] == "legacy-machine"
