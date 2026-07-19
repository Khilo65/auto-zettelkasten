from __future__ import annotations

from typing import Any

from auto_zettelkasten.literature import (
    _proposition_debate_state,
    _quantitative_item_errors,
    _quantitative_text_errors,
    _same_provider_inputs,
    build_coverage_register,
    build_locator_audit,
    build_literature_propositions,
    map_overlapping_clusters,
    normalize_evidence_profiles,
    validate_cluster_synthesis,
)
from auto_zettelkasten.models import (
    ClusterSourceContribution,
    CoverageRegister,
    EvidenceBaseGroup,
    IndependenceAssessment,
    StudyLineage,
)


def _profile(source_id: str, *, evidence_base: str | None = None) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "title": f"Mediation design study {source_id}",
        "note_status": "analytical_atomic_note",
        "study_family_id": f"family-{source_id}",
        "study_lineage": {
            "evidence_base_group_id": evidence_base or f"evidence-base-{source_id}",
            "group_basis": "study_family",
            "independence_status": "independent_evidence_base",
        },
        "coverage": {"full_document": True},
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id}",
                "finding": "Mediation design is positively associated with settlement durability.",
                "topic": "mediation design",
                "outcome": ["settlement durability"],
                "direction": "positive",
                "finding_type": "associational",
                "locator": "p. 10",
                "plain_english_meaning": "Better-designed mediation processes tend to be followed by more durable settlements.",
                "support_envelope": {
                    "empirical_role": "associational",
                    "argument_role": "none",
                    "coverage": "full_text",
                    "support_status": "supported",
                    "scope": {},
                    "restrictions": ["Does not establish a causal effect."],
                },
            }
        ],
    }


def _reference(source_id: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "evidence_anchor_id": f"anchor-{source_id}",
        "locator": "p. 10",
    }


def test_source_specific_contributions_survive_without_becoming_agreement() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b"), _profile("c")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    proposition_id = cluster["proposition_ids"][0]
    all_evidence = [_reference(source_id) for source_id in ("a", "b", "c")]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "synthesis": (
                "Three independent evidence bases address the same mediation-design proposition. "
                "All three report a positive association with settlement durability, while none establishes causality. "
                "The repeated direction is therefore a collection-level consensus about association, not proof that "
                "design choices themselves cause durable settlements. Differences in cases and measures still limit "
                "how far the pattern can be generalized beyond the frozen collection."
            ),
            "debate_state": "mapped_consensus",
            "boundaries": ["The studies cover different cases and measures."],
            "central_findings": [
                {
                    "finding": (
                        "Three independent evidence bases report the same positive association between "
                        "mediation design and settlement durability. In plain English, better-designed "
                        "processes tend to be followed by more durable settlements, but the studies do "
                        "not establish that design itself caused the outcome. Differences in cases and "
                        "measures limit how far this repeated pattern can be generalized."
                    ),
                    "evidence": all_evidence,
                    "proposition_ids": [proposition_id],
                }
            ],
            "agreements": [
                {
                    "agreement": "Study A alone establishes collection-wide agreement.",
                    "evidence": [_reference("a")],
                    "proposition_ids": [proposition_id],
                }
            ],
            "positions": [],
            "contradictions": [],
            "boundary_conditions": [],
            "methodological_fault_lines": [],
            "related_clusters": [],
            "source_roles": [],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert {row["source_id"] for row in result["source_contributions"]} == {"a", "b", "c"}
    assert result["agreements"] == []
    assert any(
        row["reason"] == "comparative_assertion_requires_two_effective_evidence_bases"
        for row in result["rejected_assertions"]
    )
    for contribution in result["source_contributions"]:
        ClusterSourceContribution.from_dict(contribution)


def test_effective_evidence_bases_control_admission_and_consensus_strength() -> None:
    same_program = [
        _profile("a", evidence_base="program-one"),
        _profile("b", evidence_base="program-one"),
    ]
    concentrated = map_overlapping_clusters(same_program)["clusters"]
    assert len(concentrated) == 1
    assert concentrated[0]["qualification_status"] == "evidence_concentrated_cluster"

    two_base_state, _ = _proposition_debate_state(
        {
            "effective_evidence_base_count": 2,
            "cells": {
                "a": {
                    "source_id": "a",
                    "evidence_base_group_id": "one",
                    "evidence_type": ["associational"],
                    "direction_or_interpretation": ["positive"],
                },
                "b": {
                    "source_id": "b",
                    "evidence_base_group_id": "two",
                    "evidence_type": ["associational"],
                    "direction_or_interpretation": ["positive"],
                },
            },
        }
    )
    three_base_state, _ = _proposition_debate_state(
        {
            "effective_evidence_base_count": 3,
            "cells": {
                key: {
                    "source_id": key,
                    "evidence_base_group_id": key,
                    "evidence_type": ["associational"],
                    "direction_or_interpretation": ["positive"],
                }
                for key in ("a", "b", "c")
            },
        }
    )
    assert two_base_state == "emerging_convergence"
    assert three_base_state == "mapped_consensus"


def test_quantitative_arithmetic_and_generated_note_locators_are_rejected() -> None:
    assert _quantitative_text_errors(
        {
            "technical_context": "The marginal effect is +0.0997.",
            "plain_english_meaning": "The probability rises from 38% to 45%, a 7 percentage point increase.",
        }
    ) == ["decimal_effect_to_percentage_point_mismatch"]
    assert _quantitative_text_errors(
        {
            "technical_result": "The estimate is a 0.25 percentage point decrease; 95% CI not reported.",
        }
    ) == []
    assert _quantitative_text_errors(
        {
            "technical_result": (
                "Overall success was 22%; 56% of disputes were mediated; substantive strategies had 44% "
                "success versus 19% for conciliation, a 25 percentage point difference."
            ),
        }
    ) == []
    assert _quantitative_text_errors(
        {
            "technical_result": (
                "The probability rose by 41.7 percentage points (from 3.6% to 45.3%). "
                "A different subgroup rose by 34.4 pp, while coefficient = 0.601 (p<0.01)."
            ),
        }
    ) == []
    assert _quantitative_text_errors(
        {
            "technical_result": (
                "The probability rose by 40 percentage points (from 3.6% to 45.3%). "
                "A separate estimate was 34.4 pp."
            ),
        }
    ) == ["percentage_point_arithmetic_mismatch"]
    assert _quantitative_text_errors(
        {
            "technical_result": (
                "The coefficient was 0.601 (p<0.01). The predicted probability changed by 14%. "
                "Neither statistic is reported as a marginal effect or percentage-point conversion."
            ),
        }
    ) == []
    assert _quantitative_text_errors(
        {
            "technical_result": "9.97 percentage point increase; 95% CI not reported.",
            "plain_english_meaning": "The chance rises from about 38% to about 45%.",
        }
    ) == ["percentage_point_arithmetic_mismatch"]

    profile = _profile("generated")
    profile["evidence_anchors"][0]["locator"] = "Detailed Findings (1)"
    normalized = normalize_evidence_profiles([profile])
    assert normalized[0]["claims"][0]["locator_complete"] is False
    assert build_literature_propositions(normalized) == []
    audit = build_locator_audit(normalized)
    assert audit["generated_note_heading_count"] == 1
    assert audit["strong_locator_count"] == 0


def test_quantitative_comparisons_require_typed_results_and_explicit_checks() -> None:
    assert _quantitative_item_errors(
        {
            "technical_result": "The reported probability rises from 38% to 45%.",
            "quantitative_comparisons": [{}],
        },
        require_comparable=True,
    ) == [
        "quantitative_arithmetic_not_reproducible",
        "quantitative_estimands_not_comparable",
        "quantitative_outcomes_not_comparable",
        "quantitative_populations_not_comparable",
    ]
    assert _quantitative_item_errors(
        {"finding": "Across sources, success rises from 38% to 45%."},
        require_comparable=True,
    ) == [
        "quantitative_claim_missing_typed_results",
        "quantitative_comparison_requires_two_typed_results",
    ]
    assert _quantitative_item_errors(
        {"finding": "The studies cover 1990 to 2020."},
        require_comparable=True,
    ) == []
    for technical_result in (
        "The odds ratio was 1.45.",
        "The coefficient was 0.23.",
        "The treatment effect was 2.4 units.",
    ):
        assert _quantitative_item_errors(
            {"technical_result": technical_result},
            require_comparable=True,
        ) == [
            "quantitative_claim_missing_typed_results",
            "quantitative_comparison_requires_two_typed_results",
        ]


def test_invalid_optional_numbers_do_not_erase_supported_qualitative_finding() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    proposition_id = cluster["proposition_ids"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "emerging_convergence",
            "central_findings": [
                {
                    "finding": (
                        "Both independent studies report the same positive association between mediation design and "
                        "settlement durability. This supports an emerging collection-level pattern rather than a causal "
                        "claim: better-designed processes tend to be followed by more durable settlements, but neither "
                        "study establishes that design itself produced the outcome. Differences in cases and measures "
                        "still limit how far the relationship can be generalized."
                    ),
                    "technical_detail": "The effect was +0.0997 and probabilities rose from 38% to 45%, or 12 points.",
                    "plain_english_meaning": "The studies imply a 10-25 percentage point increase.",
                    "evidence": [_reference("a"), _reference("b")],
                    "proposition_ids": [proposition_id],
                }
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    finding = result["central_findings"][0]
    assert finding["quantitative_detail_status"] == "omitted_unvalidated_comparison"
    assert "technical_detail" not in finding
    assert "source-specific figures remain separate" in finding["plain_english_meaning"]
    assert result["quantitative_comparisons"][0]["status"] == "rejected"


def test_duplicate_or_invalid_model_contribution_falls_back_to_source_anchor() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    duplicate_anchor = dict(normalized[0]["claims"][0])
    duplicate_anchor["evidence_anchor_id"] = "anchor-a-legacy-duplicate"
    duplicate_anchor["claim_id"] = "anchor-a-legacy-duplicate"
    duplicate_anchor["locator"] = "p. 11"
    normalized[0]["claims"].append(duplicate_anchor)
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "source_contributions": [
                {
                    "source_id": "a",
                    "finding": "Mediation design is positively associated with settlement durability.",
                    "technical_result": "The effect was +0.0997 but increased from 38% to 45% by 12 percentage points.",
                    "plain_english_meaning": "A model-written numerical gloss.",
                    "evidence": [_reference("a")],
                },
                {
                    "source_id": "a",
                    "finding": "Mediation design is positively associated with settlement durability.",
                    "technical_result": "The effect was +0.0997 but increased from 38% to 45% by 12 percentage points.",
                    "plain_english_meaning": "A duplicate model-written numerical gloss.",
                    "evidence": [_reference("a")],
                },
            ],
        },
        cluster,
        normalized,
    )

    source_a = [row for row in result["source_contributions"] if row["source_id"] == "a"]
    assert len(source_a) == 1
    assert source_a[0]["plain_english_meaning"] == (
        "Better-designed mediation processes tend to be followed by more durable settlements."
    )
    assert any(
        row["section"] == "source_contributions"
        and "percentage_point_arithmetic_mismatch" in row["reason"]
        for row in result["rejected_assertions"]
    )


def test_inconsistent_fallback_anchor_keeps_finding_but_omits_conflicting_numbers() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    anchor = normalized[0]["claims"][0]
    anchor.update(
        {
            "magnitude": "9.97 percentage point increase in probability",
            "comparison": "baseline versus directive strategy",
            "plain_english_meaning": "The probability rises from 38% to 45%.",
        }
    )

    result = validate_cluster_synthesis(
        {"cluster_id": cluster["cluster_id"]},
        cluster,
        normalized,
    )

    contribution = next(row for row in result["source_contributions"] if row["source_id"] == "a")
    assert contribution["finding"] == (
        "Mediation design is positively associated with settlement durability."
    )
    assert contribution["technical_result"] == ""
    assert "could not be reconciled as one comparison" in contribution["plain_english_meaning"]
    assert any(
        row["section"] == "source_contributions"
        and row["reason"] == "percentage_point_arithmetic_mismatch"
        for row in result["rejected_assertions"]
    )
    assert any(
        row["status"] == "rejected"
        and row["reason"] == "percentage_point_arithmetic_mismatch"
        for row in result["quantitative_comparisons"]
    )


def test_gap_checkpoint_ignores_projection_only_synthesis_changes() -> None:
    components = {
        key: f"hash-{key}"
        for key in (
            "stage",
            "key",
            "method",
            "provider",
            "model",
            "source_set_id",
            "profile_dependencies",
            "context",
            "policy",
            "prompt_version",
        )
    }
    checkpoint = {
        "dependency_component_hashes": {**components, "context": "old-full-context"},
        "dependency_context_hashes": {
            "candidates": "same-candidates",
            "internal_search_log": "same-search",
            "cluster_syntheses": "old-projection",
        },
    }
    current_context = {
        "candidates": "same-candidates",
        "internal_search_log": "same-search",
        "cluster_syntheses": "new-projection",
    }

    assert _same_provider_inputs(
        checkpoint,
        {**components, "context": "new-full-context"},
        stage="gap_adjudication",
        current_context_hashes=current_context,
    )
    assert not _same_provider_inputs(
        checkpoint,
        {**components, "context": "new-full-context"},
        stage="gap_adjudication",
        current_context_hashes={**current_context, "candidates": "changed-candidates"},
    )


def test_cluster_proposal_checkpoint_tracks_every_provider_visible_family_input() -> None:
    components = {
        key: f"hash-{key}"
        for key in (
            "stage",
            "key",
            "method",
            "provider",
            "model",
            "source_set_id",
            "profile_dependencies",
            "context",
            "policy",
            "prompt_version",
        )
    }
    visible = {
        "propositions": "same-propositions",
        "relations": "same-relations",
        "topic_neighborhoods": "same-neighborhoods",
        "coverage_repair_source_ids": "same-repair-sources",
        "prior_proposal_identities": "same-prior-proposals",
    }
    checkpoint = {
        "dependency_component_hashes": {**components, "context": "old-full-context"},
        "dependency_context_hashes": visible,
    }

    assert _same_provider_inputs(
        checkpoint,
        {**components, "context": "new-full-context"},
        stage="cluster_proposal",
        current_context_hashes=visible,
    )
    for changed_component in visible:
        assert not _same_provider_inputs(
            checkpoint,
            {**components, "context": "new-full-context"},
            stage="cluster_proposal",
            current_context_hashes={
                **visible,
                changed_component: f"changed-{changed_component}",
            },
        )

def test_coverage_register_accounts_for_all_75_frozen_items() -> None:
    source_set = {
        "rows": [
            {
                "inventory_index": index,
                "zotero_item_key": f"item-{index}",
                "source_id": f"source-{index}" if index < 73 else "",
                "note_id": f"note-{index}" if index < 73 else "",
                "terminal_status": (
                    "validated_note" if index < 65 else "limited_note" if index < 73 else "exhausted"
                ),
            }
            for index in range(75)
        ]
    }
    profiles = [
        {
            "source_id": f"source-{index}",
            "note_id": f"note-{index}",
            "analytical": index < 65,
            "limited": index >= 65,
        }
        for index in range(73)
    ]
    register = build_coverage_register(profiles, source_set=source_set)
    assert register["counts"] == {
        "validated_note": 65,
        "limited_note": 8,
        "exhausted": 2,
        "partial": 0,
        "pending": 0,
    }
    assert register["inventory_count"] == 75
    assert register["status"] == "complete_with_exclusions"
    assert len([row for row in register["records"] if row["terminal_state"] == "exhausted"]) == 2
    CoverageRegister.from_dict(register)


def test_unknown_or_publication_only_lineage_does_not_count_as_independent() -> None:
    profiles = [_profile("a"), _profile("b")]
    for profile in profiles:
        profile["study_lineage"] = None
        profile["study_family_id"] = f"doi:10.1234/{profile['source_id']}"
    normalized = normalize_evidence_profiles(profiles)
    assert all(profile["evidence_base_counted"] is False for profile in normalized)
    concentrated = map_overlapping_clusters(normalized)["clusters"]
    assert len(concentrated) == 1
    assert concentrated[0]["qualification_status"] == "evidence_concentrated_cluster"

    for publication_identity in ("doi:10.1234/shared", "title:shared publication"):
        colliding = [_profile("a"), _profile("b")]
        for profile in colliding:
            profile["study_lineage"] = None
            profile["study_family_id"] = publication_identity
        normalized_colliding = normalize_evidence_profiles(colliding)
        assert all(profile["evidence_base_counted"] is False for profile in normalized_colliding)
        assert map_overlapping_clusters(normalized_colliding)["clusters"] == []


def test_independence_sidecars_match_public_contracts() -> None:
    from auto_zettelkasten.literature import build_independence_records

    records = build_independence_records(normalize_evidence_profiles([_profile("a"), _profile("b")]))
    for row in records["study_lineages"]:
        StudyLineage.from_dict(row)
    for row in records["evidence_base_groups"]:
        EvidenceBaseGroup.from_dict(row)
    for row in records["independence_assessments"]:
        IndependenceAssessment.from_dict(row)
