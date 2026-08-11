from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.literature import _reasoner_context_char_budget
from auto_zettelkasten.literature import (
    RELATIONSHIP_PROMPT_VERSION as LITERATURE_RELATIONSHIP_PROMPT_VERSION,
)
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.readers import (
    RELATIONSHIP_CANDIDATE_MAX_OUTPUT_TOKENS,
    RELATIONSHIP_MAX_OUTPUT_TOKENS,
    ProviderError,
    _relationship_adjudication_system_prompt,
    _relationship_candidate_system_prompt,
    _validate_relationship_response,
)
from auto_zettelkasten.relationships import (
    RELATIONSHIP_DECISION_CONTRACT,
    RELATIONSHIP_PROMPT_VERSION,
    SYMMETRIC_RELATION_TYPES,
    persist_relationship_registry,
    projected_related_links,
    validate_relationship_decision_rows,
)


def test_relationship_discovery_uses_lean_recall_first_prompt() -> None:
    prompt = _relationship_candidate_system_prompt()

    assert LITERATURE_RELATIONSHIP_PROMPT_VERSION == RELATIONSHIP_PROMPT_VERSION
    assert "Prefer fewer grounded pairs to filling a target" in prompt
    assert "not a published relationship" in prompt
    assert "navigation hypothesis that may be wrong" in prompt
    assert "each endpoint profile independently supplies" in prompt
    assert "must not invent an unstated mediator" in prompt
    assert "remove the family goal and self-check" in prompt
    assert "return no_more_candidates even below" in prompt
    assert "max_inferred_pairs" in prompt
    assert "bridge_job_id" in prompt
    assert "target_candidate_count" in prompt
    assert "left_source_id" in prompt
    assert "right_source_id" in prompt
    assert "comparison_proposition" in prompt
    assert "why_compare" not in prompt
    assert "bridge_family" not in prompt
    assert "evidence_anchor_ids" not in prompt
    assert "job_outcomes" in prompt
    assert "no_more_candidates" in prompt
    assert "Neither status requests automatic continuation" in prompt
    assert RELATIONSHIP_CANDIDATE_MAX_OUTPUT_TOKENS == 64_000
    assert RELATIONSHIP_MAX_OUTPUT_TOKENS == 128_000


def test_candidate_validation_preserves_optional_job_outcomes() -> None:
    assert _validate_relationship_response(
        {
            "candidates": [],
            "job_outcomes": [
                {"bridge_job_id": "job-a", "status": "no_more_candidates"}
            ],
        },
        kind="candidate_selection",
    ) == {
        "candidates": [],
        "job_outcomes": [
            {"bridge_job_id": "job-a", "status": "no_more_candidates"}
        ],
    }


def test_v7_relationship_requires_claim_owned_primary_anchor_per_endpoint() -> None:
    profiles = {source_id: _profile(source_id) for source_id in ("A", "B")}
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        profiles={"left": profiles["A"], "right": profiles["B"]},
        selected_evidence={
            "left": [profiles["A"]["evidence_anchors"][0]],
            "right": [profiles["B"]["evidence_anchors"][0]],
        },
    )
    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "relation_type": "qualifies",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "comparison_proposition": "A narrows B's bounded claim.",
                    "left_endpoint_claim": "Claim A",
                    "left_evidence_anchor_id": "anchor-a",
                    "right_endpoint_claim": "Claim B",
                    "right_evidence_anchor_id": "anchor-b",
                    "reason": "The endpoint claims establish the qualification.",
                    "boundary_or_qualification": "The qualification applies to one case.",
                    "confidence": "high",
                }
            }
        },
        jobs=[job],
        profiles=list(profiles.values()),
    )

    assert result["parked"] == []
    assert result["accepted"][0]["left_endpoint_claim"] == "Claim A"
    assert result["accepted"][0]["left_evidence_anchor_ids"] == ["anchor-a"]
    prompt = _relationship_adjudication_system_prompt()
    assert "source_a_basis" in prompt
    assert "source_b_basis" in prompt
    assert "source_a_anchor_ids" not in prompt


def test_v7_relationship_accepts_unambiguous_plural_anchor_fields() -> None:
    profiles = {source_id: _profile(source_id) for source_id in ("A", "B")}
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        profiles={"left": profiles["A"], "right": profiles["B"]},
        selected_evidence={
            "left": [profiles["A"]["evidence_anchors"][0]],
            "right": [profiles["B"]["evidence_anchors"][0]],
        },
    )

    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "relation_type": "qualifies",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "comparison_proposition": "A narrows B's bounded claim.",
                    "left_endpoint_claim": "Claim A",
                    "left_evidence_anchor_ids": ["anchor-a"],
                    "right_endpoint_claim": "Claim B",
                    "right_evidence_anchor_ids": ["anchor-b"],
                    "reason": "The endpoint claims establish the qualification.",
                    "boundary_or_qualification": "The qualification applies to one case.",
                    "confidence": "high",
                }
            }
        },
        jobs=[job],
        profiles=list(profiles.values()),
    )

    assert result["parked"] == []
    assert result["accepted"][0]["left_evidence_anchor_ids"] == ["anchor-a"]
    assert result["accepted"][0]["contract_warnings"] == [
        "normalized_v7_plural_left_anchors",
        "normalized_v7_plural_right_anchors",
    ]


def test_v7_contextual_decision_shorthand_is_normalized() -> None:
    profiles = {source_id: _profile(source_id) for source_id in ("A", "B")}
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        profiles={"left": profiles["A"], "right": profiles["B"]},
        selected_evidence={
            "left": [profiles["A"]["evidence_anchors"][0]],
            "right": [profiles["B"]["evidence_anchors"][0]],
        },
    )

    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "contextual_connection",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "comparison_proposition": "The works illuminate adjacent stages.",
                    "left_endpoint_claim": "Claim A",
                    "left_evidence_anchor_id": "anchor-a",
                    "right_endpoint_claim": "Claim B",
                    "right_evidence_anchor_id": "anchor-b",
                    "reason": "Joint reading is useful despite distinct propositions.",
                    "boundary_or_qualification": "The outcomes differ.",
                    "confidence": "medium",
                }
            }
        },
        jobs=[job],
        profiles=list(profiles.values()),
    )

    assert result["parked"] == []
    assert result["accepted"][0]["relation_type"] == "contextual_connection"
    assert result["accepted"][0]["contract_warnings"] == [
        "normalized_relation_decision_shorthand"
    ]


def test_v7_direct_relation_decision_shorthand_is_normalized() -> None:
    profiles = {source_id: _profile(source_id) for source_id in ("A", "B")}
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        profiles={"left": profiles["A"], "right": profiles["B"]},
        selected_evidence={
            "left": [profiles["A"]["evidence_anchors"][0]],
            "right": [profiles["B"]["evidence_anchors"][0]],
        },
    )

    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "supports",
                    "relation_type": "supports",
                    "actor_source_id": "A",
                    "reference_source_id": "B",
                    "comparison_proposition": "A supports B's bounded claim.",
                    "left_endpoint_claim": "Claim A",
                    "left_evidence_anchor_id": "anchor-a",
                    "right_endpoint_claim": "Claim B",
                    "right_evidence_anchor_id": "anchor-b",
                    "reason": "Both endpoint claims establish the support.",
                    "boundary_or_qualification": "The evidence is associational.",
                    "confidence": "high",
                }
            }
        },
        jobs=[job],
        profiles=list(profiles.values()),
    )

    assert result["parked"] == []
    assert result["accepted"][0]["relation_type"] == "supports"
    assert result["accepted"][0]["contract_warnings"] == [
        "normalized_relation_decision_shorthand"
    ]


def test_v6_keyed_provider_envelope_normalizes_to_rows() -> None:
    normalized = _validate_relationship_response(
        {
            "decisions": {
                "job-a-b": {
                    "decision": "no_relationship",
                    "reason": "The apparent overlap is only topical.",
                    "confidence": 0.8,
                }
            }
        },
        kind="relationship_adjudication",
    )

    assert normalized == {
        "decisions": [
            {
                "pair_job_id": "job-a-b",
                "decision": "no_relationship",
                "reason": "The apparent overlap is only topical.",
                "confidence": 0.8,
            }
        ]
    }


def test_top_level_keyed_provider_envelope_normalizes_to_rows() -> None:
    normalized = _validate_relationship_response(
        {
            "relationship-job-a-b": {
                "decision": "no_relationship",
                "reason": "The apparent overlap is only topical.",
                "confidence": 0.8,
            }
        },
        kind="relationship_adjudication",
    )

    assert normalized["decisions"] == [
        {
            "pair_job_id": "relationship-job-a-b",
            "decision": "no_relationship",
            "reason": "The apparent overlap is only topical.",
            "confidence": 0.8,
        }
    ]


def test_top_level_bare_hex_job_suffixes_normalize_to_full_job_ids() -> None:
    suffix = "a6a74627d57ffabf1cae"

    normalized = _validate_relationship_response(
        {
            suffix: {
                "decision": "no_relationship",
                "reason": "The apparent overlap is only topical.",
                "confidence": 0.8,
            }
        },
        kind="relationship_adjudication",
    )

    assert normalized["decisions"] == [
        {
            "pair_job_id": f"relationship-job-{suffix}",
            "decision": "no_relationship",
            "reason": "The apparent overlap is only topical.",
            "confidence": 0.8,
        }
    ]


def test_top_level_mixed_or_non_hex_job_suffixes_remain_invalid() -> None:
    with pytest.raises(ProviderError, match="must contain a decisions list"):
        _validate_relationship_response(
            {"a6a74627d57ffabf1cae": {}, "not-a-job": {}},
            kind="relationship_adjudication",
        )


def test_v6_keyed_provider_envelope_uses_the_mapping_key_as_job_id() -> None:
    normalized = _validate_relationship_response(
        {
            "decisions": {
                "job-a-b": {
                    "pair_job_id": "wrong-job",
                    "decision": "no_relationship",
                    "reason": "The apparent overlap is only topical.",
                    "confidence": 0.8,
                }
            }
        },
        kind="relationship_adjudication",
    )

    assert normalized["decisions"][0]["pair_job_id"] == "job-a-b"


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


def test_million_token_reasoner_uses_configured_literature_context_budget() -> None:
    reasoner = type(
        "Reasoner",
        (),
        {
            "context_window_tokens": 1_000_000,
            "direct_read_fraction": 0.5,
            "prompt_reserve_tokens": 2_048,
            "capabilities": {"supported_output_tokens": 64_000},
        },
    )()

    budget = _reasoner_context_char_budget(
        reasoner,
        {
            "processing": {"estimated_chars_per_token": 3.5},
            "literature_policy": {"deepseek_packet_context_fraction": 0.8},
        },
    )

    assert budget == 2_201_856


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
        output_contract="relationship-decision-v6",
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
    return {
        "pair_job_id": job.pair_job_id,
        "decision": "relationship",
        "relation_type": relation_type,
        "actor_source_id": actor,
        "reference_source_id": reference,
        "comparison_proposition": "The works address the same bounded claim.",
        "reason": "Related.",
        "left_evidence_anchor_ids": [
            f"anchor-{job.left_source_id.lower()}"
        ],
        "right_evidence_anchor_ids": [
            f"anchor-{job.right_source_id.lower()}"
        ],
        "confidence": "",
    }


def _profiles(*source_ids: str) -> list[dict[str, Any]]:
    return [_profile(source_id) for source_id in source_ids]


def test_complete_v6_decision_accepts_model_direction_without_semantic_veto() -> None:
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


def test_v6_symmetric_relationship_uses_canonical_registry_direction() -> None:
    job = _job("A", "B")
    row = _relationship_row(job)
    row.update(
        {
            "relation_type": "contextual_connection",
            "comparison_proposition": "The works illuminate adjacent stages.",
            "reason": "Joint reading is useful without a shared tested proposition.",
        }
    )
    row.pop("actor_source_id")
    row.pop("reference_source_id")

    relation = validate_relationship_decision_rows(
        {"decisions": {job.pair_job_id: row}},
        jobs=[job],
        profiles=_profiles("A", "B"),
    )["accepted"][0]

    assert relation["source_id"] == "A"
    assert relation["target_source_id"] == "B"
    assert relation["relationship_tier"] == "contextual"


def test_v8_accepts_short_anchor_aliases_and_canonicalizes_symmetric_pair() -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": RELATIONSHIP_DECISION_CONTRACT,
        }
    )
    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "connections": [
                        {
                            "comparison_proposition": "Joint reading is useful.",
                            "primary_relation_type": "contextual_connection",
                            "actor_source_id": None,
                            "reference_source_id": None,
                            "source_a_basis": "A establishes its bounded claim.",
                            "source_b_basis": "B establishes its bounded claim.",
                            "reason": "The claims illuminate adjacent questions.",
                            "source_a_anchor_ids": ["anchor-a"],
                            "source_b_anchor_ids": ["anchor-b"],
                        }
                    ],
                }
            }
        },
        jobs=[job],
        profiles=_profiles("A", "B"),
    )

    assert result["parked"] == []
    relation = result["accepted"][0]
    assert relation["source_id"] == "A"
    assert relation["target_source_id"] == "B"
    assert relation["left_evidence_anchor_ids"] == ["anchor-a"]
    assert relation["right_evidence_anchor_ids"] == ["anchor-b"]


def test_v8_directional_relationship_without_endpoints_is_parked() -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": RELATIONSHIP_DECISION_CONTRACT,
        }
    )
    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "connections": [
                        {
                            "comparison_proposition": "A bears on B's claim.",
                            "primary_relation_type": "supports",
                            "actor_source_id": None,
                            "reference_source_id": None,
                            "source_a_basis": "A establishes its bounded claim.",
                            "source_b_basis": "B advances the reference claim.",
                            "reason": "The proposed support is directional.",
                        }
                    ],
                }
            }
        },
        jobs=[job],
        profiles=_profiles("A", "B"),
    )

    assert result["accepted"] == []
    assert any(
        "direction must use both pair endpoints"
        in str(row.get("error") or "")
        for row in result["parked"]
    )


@pytest.mark.parametrize("relation_type", sorted(SYMMETRIC_RELATION_TYPES))
def test_v8_all_symmetric_relationships_accept_null_endpoints(
    relation_type: str,
) -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": RELATIONSHIP_DECISION_CONTRACT,
        }
    )
    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "connections": [
                        {
                            "comparison_proposition": "The bounded comparison is useful.",
                            "primary_relation_type": relation_type,
                            "actor_source_id": None,
                            "reference_source_id": None,
                            "source_a_basis": "A establishes its bounded claim.",
                            "source_b_basis": "B establishes its bounded claim.",
                            "reason": "The pair supports the stated comparison.",
                        }
                    ],
                }
            }
        },
        jobs=[job],
        profiles=_profiles("A", "B"),
    )

    assert result["parked"] == []
    assert result["accepted"][0]["relation_type"] == relation_type


@pytest.mark.parametrize(
    "relation_type",
    [
        "supports",
        "undermines",
        "qualifies",
        "extends",
        "rival_explanation",
        "sequential_relationship",
    ],
)
def test_v8_all_directional_relationships_require_endpoints(
    relation_type: str,
) -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": RELATIONSHIP_DECISION_CONTRACT,
        }
    )
    result = validate_relationship_decision_rows(
        {
            "decisions": {
                job.pair_job_id: {
                    "decision": "relationship",
                    "connections": [
                        {
                            "comparison_proposition": "A bears on B's claim.",
                            "primary_relation_type": relation_type,
                            "actor_source_id": None,
                            "reference_source_id": None,
                            "source_a_basis": "A establishes its bounded claim.",
                            "source_b_basis": "B establishes its bounded claim.",
                            "reason": "The proposed relationship is directional.",
                        }
                    ],
                }
            }
        },
        jobs=[job],
        profiles=_profiles("A", "B"),
    )

    assert result["accepted"] == []
    assert result["parked"]


def test_v6_repartitions_known_evidence_anchors_by_owner() -> None:
    job = _job("A", "B")
    row = _relationship_row(job)
    row["left_evidence_anchor_ids"] = ["anchor-b"]
    row["right_evidence_anchor_ids"] = ["anchor-a"]

    relation = validate_relationship_decision_rows(
        {"decisions": {job.pair_job_id: row}},
        jobs=[job],
        profiles=_profiles("A", "B"),
    )["accepted"][0]

    assert relation["left_evidence_anchor_ids"] == ["anchor-a"]
    assert relation["right_evidence_anchor_ids"] == ["anchor-b"]
    assert relation["contract_warnings"] == [
        "anchor_repartitioned:anchor-b:left_to_right",
        "anchor_repartitioned:anchor-a:right_to_left",
    ]


def test_legacy_v5_decision_remains_compatible() -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": "relationship-decision-v5",
        }
    )
    row = {
        **_relationship_row(job),
        "pair": {
            "left_source_id": job.left_source_id,
            "right_source_id": job.right_source_id,
        },
        "relationship_tier": "direct",
        "forward_label": "supports",
        "inverse_label": "supported by",
        "output_contract": "relationship-decision-v5",
    }

    relation = validate_relationship_decision_rows(
        {"decisions": [row]},
        jobs=[job],
        profiles=_profiles("A", "B"),
    )["accepted"][0]

    assert relation["output_contract"] == "relationship-decision-v5"
    assert relation["decision_schema_version"] == "5"


def test_legacy_v5_decision_persists_and_projects(tmp_path: Path) -> None:
    job = RelationshipPairJob.from_dict(
        {
            **_job("A", "B").to_dict(),
            "output_contract": "relationship-decision-v5",
        }
    )
    profiles = _profiles("A", "B")
    accepted = validate_relationship_decision_rows(
        {
            "decisions": [
                {
                    **_relationship_row(job),
                    "pair": {
                        "left_source_id": "A",
                        "right_source_id": "B",
                    },
                    "relationship_tier": "direct",
                    "forward_label": "supports",
                    "inverse_label": "supported by",
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

    assert accepted[0]["active"] is True
    assert projected_related_links(
        "A", profiles, registry["links"], max_inferred_links=0
    )[0]["primary_relation_type"] == "supports"


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
    model_label_noise = _relationship_row(jobs[2])
    model_label_noise["inverse_label"] = "supports"

    result = validate_relationship_decision_rows(
        {
            "decisions": [
                _relationship_row(jobs[0]),
                bad_anchor,
                model_label_noise,
            ]
        },
        jobs=jobs,
        profiles=_profiles("A", "B", "C", "D"),
    )

    assert [row["pair_job_id"] for row in result["accepted"]] == [
        "job-a-b",
        "job-a-d",
    ]
    assert result["accepted"][1]["inverse_label"] == "supported by"
    assert {row["reason"] for row in result["parked"]} == {
        "missing_endpoint_evidence",
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

    assert read_yaml(path)["registry_schema_version"] == "7"
    assert relation["active"] is True
    assert relation["decision_status"] == "legacy_review_pending"
    assert relation["cluster_evidence_eligible"] is False
    assert result["links"][0]["relation_id"] == "legacy-machine"
