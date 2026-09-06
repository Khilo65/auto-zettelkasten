from __future__ import annotations

import json
from pathlib import Path

from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.pipeline import _relationship_transport_context
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
        output_contract="relationship-decision-v8",
    )


def _v9_profile(source_id: str) -> dict[str, object]:
    return {
        **_profile(source_id),
        "evidence_anchors": [
            {
                "source_id": source_id,
                "evidence_anchor_id": f"anchor-{source_id.casefold()}",
                "claim": f"{source_id} owns this exact claim.",
                "locator": f"{source_id} locator",
            }
        ],
    }


def _v9_job() -> RelationshipPairJob:
    left = _v9_profile("A")
    right = _v9_profile("B")
    return RelationshipPairJob(
        pair_job_id="job-ab",
        left_source_id="A",
        right_source_id="B",
        profiles={"left": left, "right": right},
        selected_evidence={
            "left": list(left["evidence_anchors"]),
            "right": list(right["evidence_anchors"]),
        },
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


def test_relationship_packet_scopes_anchor_choices_to_each_pair() -> None:
    job_ab = _v9_job()
    source_c = _v9_profile("C")
    job_ac = RelationshipPairJob(
        left_source_id="A", right_source_id="C",
        profiles={"left": _v9_profile("A"), "right": source_c},
        selected_evidence={
            "left": _v9_profile("A")["evidence_anchors"],
            "right": source_c["evidence_anchors"],
        },
        output_contract=RELATIONSHIP_DECISION_CONTRACT,
    )
    context = _relationship_transport_context(
        [job_ab, job_ac], decision_contract=RELATIONSHIP_DECISION_CONTRACT,
    )
    assert [row["allowed_evidence_anchor_ids"] for row in context["pair_jobs"]] == [
        {"source_a": ["anchor-a"], "source_b": ["anchor-b"]},
        {"source_a": ["anchor-a"], "source_b": ["anchor-c"]},
    ]
    connection = {
        **_connection("Both sources discuss the same reported event.", "contextual_connection"),
        "source_a_anchor_ids": ["anchor-a"],
        "source_b_anchor_ids": ["anchor-c"],
    }
    def ingest():
        return ingest_relationship_decision_batch(
            {"decisions": [{"pair_job_id": job_ab.pair_job_id,
                            "decision": "relationship", "connections": [connection]}]},
            pair_jobs=[job_ab], profiles=[_v9_profile(value) for value in "ABC"],
        )

    assert not ingest()["accepted"]
    assert ingest()["parked"]
    connection["source_b_anchor_ids"] = ["anchor-b"]
    assert len(ingest()["accepted"]) == 1
    assert not ingest()["parked"]


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


def test_v9_repartitions_actor_right_anchors_and_uses_owned_claims() -> None:
    job = _v9_job()
    profiles = [_v9_profile("A"), _v9_profile("B")]
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": "job-ab",
                    "decision": "relationship",
                    "connections": [
                        {
                            "comparison_proposition": "B supports A.",
                            "primary_relation_type": "supports",
                            "secondary_relation_types": [],
                            "actor_source_id": "B",
                            "reference_source_id": "A",
                            # Reproduce the provider's actor/reference interpretation
                            # of A/B. Ownership must come from the anchors, not prose.
                            "source_a_basis": "B's submitted basis.",
                            "source_b_basis": "A's submitted basis.",
                            "source_a_anchor_ids": ["anchor-b"],
                            "source_b_anchor_ids": ["anchor-a"],
                            "reason": "The bounded evidence supports the direction.",
                            "boundary_or_qualification": "Only this proposition.",
                            "confidence": "high",
                        }
                    ],
                }
            ]
        },
        pair_jobs=[job],
        profiles=profiles,
    )

    assert result["parked"] == []
    relation = result["accepted"][0]
    assert relation["source_id"] == "B"
    assert relation["target_source_id"] == "A"
    assert relation["left_endpoint_claim"] == "A owns this exact claim."
    assert relation["right_endpoint_claim"] == "B owns this exact claim."
    assert relation["source_evidence"] == {
        "source_id": "B",
        "evidence_anchor_id": "anchor-b",
        "locator": "B locator",
        "claim": "B owns this exact claim.",
    }
    assert relation["target_evidence"] == {
        "source_id": "A",
        "evidence_anchor_id": "anchor-a",
        "locator": "A locator",
        "claim": "A owns this exact claim.",
    }


def test_v9_parks_relationship_without_owned_endpoint_anchors() -> None:
    connection = _connection("A shared proposition.")
    connection.update(
        {
            "source_a_anchor_ids": [],
            "source_b_anchor_ids": ["unknown-anchor"],
        }
    )
    result = ingest_relationship_decision_batch(
        {
            "decisions": [
                {
                    "pair_job_id": "job-ab",
                    "decision": "relationship",
                    "connections": [connection],
                }
            ]
        },
        pair_jobs=[_v9_job()],
        profiles=[_v9_profile("A"), _v9_profile("B")],
    )

    assert result["accepted"] == []
    assert result["parked"]
    assert "complete semantic record" in str(result["parked"][0].get("error") or "")


def test_v33_is_compact_domain_neutral_source_owned_and_complete() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "relationship prompt v33" in prompt
    assert "allowed_evidence_anchor_ids" in prompt
    assert "another source's IDs are never interchangeable" in prompt
    assert "contract relationship-decision-v9" in prompt
    assert "source_a_basis describes only the supplied left_source_id" in prompt
    assert "source_b_basis only the supplied right_source_id" in prompt
    assert "whole work versus chapter, excerpt, or component" in prompt
    assert "vocabulary, or pair order alone" in prompt
    assert "supports means the actor supplies evidence or argument" in prompt
    assert "undermines means the actor supplies materially incompatible" in prompt
    assert "qualifies means the actor establishes a condition" in prompt
    assert "sequential_relationship means the actor precedes the reference" in prompt
    assert "contextual_connection are symmetric" in prompt
    assert "directional types require exact supplied endpoints as actor/reference" in prompt
    assert "ACTOR [relation type] REFERENCE" in prompt
    assert "exact evidence-anchor IDs owned by that endpoint" in prompt
    assert "the evidence source normally supports the dependent work" in prompt
    assert "does not support the evidence source merely by relying on it" in prompt
    assert "every ID appears exactly once" in prompt
    assert "use no_relationship rather than omitting a pair" in prompt
    assert len(prompt) <= 5_500
    assert not any(
        name in prompt.casefold()
        for name in ("svensson", "mediation", "civil war", "peacekeeping")
    )


def test_contextual_comparison_does_not_require_a_causal_bridge() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "State the boundary" in prompt
    assert "nor invalidates a contextual comparison" in prompt
    assert "successive institutional stage" in prompt
    assert "prove a cross-stage causal chain" in prompt


def test_contextual_comparison_connects_argument_to_measured_perception_without_causality() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "an argued communication or legitimation process alongside a measured perception outcome" in prompt
    assert "measure exposure, establish that the process caused the outcome" in prompt


def test_measurement_fault_line_includes_simple_indicator_vs_composite_index() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "a simple indicator versus a composite index" in prompt
    assert "different measures of the same bounded phenomenon as a connection, not a rejection" in prompt


def test_method_comparison_does_not_require_matching_case_results() -> None:
    prompt = " ".join(_relationship_adjudication_system_prompt().split())

    assert "one work is a general framework and the other a case result" in prompt
    assert "Missing same-case results blocks corroboration, not methodological or contextual comparison" in prompt
    assert "constructs, populations and timing" in prompt
    assert "Across disciplines or methods, seek unconventional but source-grounded connections" in prompt
    assert "overlap is merely topical, lexical, or generic" in prompt


def test_method_comparison_has_no_shared_proposition_or_author_bridge_prerequisite() -> None:
    prompt = " ".join(_relationship_adjudication_system_prompt().split())

    assert "choose the tier before the subtype" not in prompt
    assert "Direct: the same sufficiently specific proposition" not in prompt
    assert "explicitly establishes an intellectual bridge" not in prompt
    assert "method changes what can be supported" in prompt
    assert "Use the narrowest subtype" in prompt


def test_contextual_comparison_uses_contributions_not_cross_source_proof() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "source-specific contributions to one concrete problem, practice, mechanism, or outcome" in prompt
    assert "Sources need not compare works, link events" in prompt
    assert "Unproven causality neither erases" in prompt
    assert "a source lacks a substantive contribution to that comparison" in prompt


def test_relationship_bases_keep_analytical_cautions_owned_by_the_note() -> None:
    prompt = _relationship_adjudication_system_prompt()

    assert "Distinguish source assertions from analytical cautions in the notes" in prompt
    assert "attribute those cautions to the note or system, not the source" in prompt


def test_adjudication_checks_narrower_connections_before_rejecting_a_pair() -> None:
    prompt = " ".join(_relationship_adjudication_system_prompt().casefold().split())

    for requirement in (
        "candidate comparison is a hypothesis",
        "before no_relationship",
        "narrower contextual",
        "shared reported result",
        "attributed process",
        "causal proof",
        "independent corroboration",
        "neither erases an explicit author argument nor invalidates a contextual comparison",
    ):
        assert requirement in prompt
    assert "no bounded connection survives" in prompt
    assert "reason must represent both complete notes" in prompt
    assert "why the strongest narrower alternative fails" in prompt
    assert "for rejections, verify whole-note scope" in prompt
    assert "not merely direct equivalence" in prompt


def test_adjudication_scopes_absence_claims_to_supplied_summary_evidence() -> None:
    prompt = " ".join(_relationship_adjudication_system_prompt().casefold().split())

    for requirement in (
        "notes are summaries: silence is not evidence of source absence",
        "unless a note explicitly establishes absence",
        "anchor lists are selected evidence, not exhaustive source summaries",
        "reread the full note even when no anchor states the fact",
        "not supplied in the note",
        "apply this to reasons and qualifications",
        "a shared causal outcome or comparable scores are not required",
        "missing methodological detail limits that comparison",
        "does not erase the supplied measurement object",
    ):
        assert requirement in prompt


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

    settled = persist_relationship_registry(
        tmp_path,
        structural_relations=[],
        accepted_relations=result["accepted"],
        parked_rows=[
            {
                "pair_job_id": "older-job-ab",
                "source_id": "A",
                "target_source_id": "B",
                "reason": "historical_malformed_attempt",
            }
        ],
    )
    assert settled["current_pair_decisions"][0]["refresh_pending"] is False


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
