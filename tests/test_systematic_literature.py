from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from auto_zettelkasten.literature import (
    EVIDENCE_DIMENSIONS,
    GAP_RULES,
    build_debate_registry,
    build_evidence_matrices,
    build_literature_map,
    build_literature_report,
    cluster_display_title,
    cluster_note_stem,
    gap_display_title,
    gap_note_stem,
    generate_gap_candidates,
    map_overlapping_clusters,
    map_profile_relations,
    normalize_evidence_profiles,
    reconcile_cluster_registry,
    search_and_validate_gaps,
)
from auto_zettelkasten.models import (
    EvidenceFinding,
    EvidenceProfile,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
)
from auto_zettelkasten.literature import run_literature_map, stable_literature_map_id


def profile(
    source: str,
    *,
    family: str | None = None,
    topic: str = "institutional trust",
    direction: str = "positive",
    method: str = "panel regression",
    locator: str = "p. 10",
    tags: list[str] | None = None,
    status: str = "analytical_atomic_note",
    gap_signals: list[dict] | None = None,
    gap_answers: list[dict] | None = None,
    extra_topics: list[str] | None = None,
) -> dict:
    return {
        "source_id": source,
        "note_id": f"note-{source}",
        "title": f"{topic.title()} in {source.upper()}",
        "note_status": status,
        "study_family_id": family or f"family-{source}",
        "semantic_topics": [topic, *(extra_topics or [])],
        "concepts": [topic],
        "theories": [],
        "mechanisms": [],
        "methods": [method],
        "data": [f"dataset-{source}"],
        "cases": [f"case-{source}"],
        "periods": [f"20{len(source):02d}-2025"],
        "outcomes": [f"{topic} outcome"],
        "limitations": [f"limitation-{source}"],
        "normalized_tags": tags or [],
        "findings": [
            {
                "claim_id": f"claim-{source}",
                "claim": f"{topic} has a {direction} result in {source}.",
                "topic": topic,
                "direction": direction,
                "locator": locator,
                "uncertainty": "moderate",
                "boundary_condition": f"boundary-{source}",
            }
        ],
        "gap_signals": gap_signals or [],
        "gap_answers": gap_answers or [],
    }


def cluster_for(profiles: list[dict], identity: str = "institutional trust") -> dict:
    result = map_overlapping_clusters(profiles, map_profile_relations(profiles))
    return next(row for row in result["clusters"] if row["semantic_identity"] == identity)


def _quality_rationale(candidate: Mapping[str, Any]) -> dict[str, Any]:
    missing = str(candidate.get("precise_missing_evidence") or "")
    topic = str(candidate.get("topic") or "the mapped relationship")
    return {
        "gap_id": candidate["gap_id"],
        "title": missing,
        "gap_statement": missing,
        "rule": candidate["rule"],
        "related_cluster_ids": list(candidate.get("related_cluster_ids", []) or []),
        "generation_explanation": str(candidate.get("generation_explanation") or "The cluster evidence generated this candidate."),
        "observed_pattern": str(candidate.get("observed_pattern") or f"The collection maps an unresolved pattern involving {topic}."),
        "precise_missing_evidence": missing,
        "supporting_evidence": list(candidate.get("supporting_evidence", []) or []),
        "countervailing_evidence": list(candidate.get("countervailing_evidence", []) or []),
        "internal_search_summary": "Every analytical profile in the frozen collection was searched.",
        "closest_prior_explanation": "The closest collection evidence does not make the required comparison.",
        "decision_reasoning": "The candidate survives the obvious-answer and executable-design gates.",
        "evidence_needed": missing,
        "why_matters": str(candidate.get("why_matters") or f"Resolving the puzzle changes inference about {topic}."),
        "contribution": str(candidate.get("contribution") or f"A matched test would distinguish explanations for {topic}."),
        "confidence": "moderate",
        "value_assessment": {
            "puzzle_type": "competing explanations",
            "puzzle": f"Why does the mapped evidence leave competing explanations for {topic}?",
            "strongest_obvious_answer": "The observed difference may simply reflect case composition.",
            "why_obvious_answer_is_inadequate": "The collection does not compare matched cases under common measures.",
            "competing_explanations": ["case selection", "measurement differences"],
            "decision_or_inference_changed": f"It determines whether the mapped relationship involving {topic} is transportable.",
            "information_gain": "moderate",
            "non_obviousness_passed": True,
            "importance_passed": True,
            "rejection_reasons": [],
        },
        "study_design": {
            "design_type": "matched comparative design",
            "research_question": f"Under matched conditions, what explains variation in {topic}?",
            "estimand": "The matched contrast in the specified outcome.",
            "unit_of_analysis": "Study case period",
            "target_population": "Cases represented by the frozen collection",
            "exposure_or_treatment": f"Variation in {topic}",
            "comparator": "Matched cases without the focal exposure",
            "outcomes": [f"Measured outcome for {topic}"],
            "mechanism_measures": ["Observed intermediate mechanism indicators"],
            "identification_or_inference_strategy": "Match cases on pre-exposure covariates and compare within matched sets.",
            "data_route": "Reuse collection-defined measures and assemble comparable case-level observations.",
            "confounders_or_rival_explanations": ["case selection", "measurement differences"],
            "falsification_or_process_tests": ["negative-control outcome", "pre-treatment balance test"],
            "feasibility": "The design is feasible if the mapped source datasets expose common case identifiers.",
            "ethical_constraints": "Use de-identified or public case-level evidence.",
            "validity_risks": ["residual confounding", "cross-study measurement mismatch"],
        },
        "anchors": [],
        "merged_from_gap_ids": [],
        "reframed_from_gap_id": "",
        "priority_tier": "moderate",
    }


def quality_reasoner(
    rows: list[dict],
    base_reasoner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reasoner = deepcopy(dict(base_reasoner or {}))
    probe = build_literature_report(rows, reasoner=reasoner)
    candidates = [
        row
        for row in probe["gap_registry"]["rejected_candidates"]
        if row.get("status") == "rejected_gap_quality"
    ]
    reasoner["gap_rationales"] = [_quality_rationale(row) for row in candidates]
    reasoner.setdefault("rejected_gap_rationales", [])
    return reasoner


def build_quality_report(
    rows: list[dict],
    base_reasoner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return build_literature_report(rows, reasoner=quality_reasoner(rows, base_reasoner))


def test_typed_untagged_profiles_cluster_semantically_and_tags_are_only_tiebreakers() -> None:
    typed = [
        EvidenceProfile(
            source_id=f"source-{index}",
            note_id=f"note-{index}",
            study_family_id=f"family-{index}",
            coverage={"status": "full_text"},
            validity={"status": "valid"},
            concepts=["civil resistance"],
            methods=[f"method-{index}"],
            findings=[
                EvidenceFinding(
                    finding_id=f"claim-{index}",
                    claim="Civil resistance changes participation.",
                    direction="positive",
                    outcome="participation",
                    locator=f"p. {index + 1}",
                )
            ],
        )
        for index in range(3)
    ]
    normalized = normalize_evidence_profiles(typed)
    mapped = map_overlapping_clusters(normalized, map_profile_relations(normalized))
    cluster = next(row for row in mapped["clusters"] if row["semantic_identity"] == "civil resistance")
    assert cluster["status"] == "source_backed_cluster"
    assert cluster["independent_study_family_count"] == 3

    unrelated = [
        profile("a", topic="school finance", tags=["shared-tag"]),
        profile("b", topic="ocean salinity", tags=["shared-tag"]),
    ]
    assert map_profile_relations(unrelated) == []
    assert map_overlapping_clusters(unrelated)["clusters"] == []

    mirrored = [
        {**profile("a", topic="school finance"), "semantic_topics": ["shared-tag"], "normalized_tags": ["shared-tag"]},
        {**profile("b", topic="ocean salinity"), "semantic_topics": ["shared-tag"], "normalized_tags": ["shared-tag"]},
    ]
    assert map_profile_relations(mirrored) == []
    assert map_overlapping_clusters(mirrored)["clusters"] == []


def test_partial_reasoner_proposals_enrich_without_suppressing_deterministic_clusters() -> None:
    rows = [
        profile("a", topic="institutional trust"),
        profile("b", topic="institutional trust"),
        profile("c", topic="mediator legitimacy"),
        profile("d", topic="mediator legitimacy"),
        profile("e", topic="institutional trust"),
    ]
    reasoner = {
        "cluster_proposals": [
            {
                "proposal_id": "proposal-trust",
                "label": "Institutional trust mechanisms",
                "semantic_identity": "institutional trust",
                "shared_question": "When does institutional trust affect participation?",
                "coherence_rationale": "The two studies test the same relationship.",
                "source_ids": ["a", "b", "c", "e"],
                "supporting_evidence": [
                    {"source_id": "a", "claim_id": "claim-a", "locator": "p. 10"},
                    {"source_id": "b", "claim_id": "claim-b", "locator": "p. 10"},
                    {"source_id": "c", "claim_id": "claim-c", "locator": "p. 999"},
                ],
            },
            {
                "proposal_id": "bad-locator",
                "label": "Invented cluster",
                "semantic_identity": "invented cluster",
                "source_ids": ["c", "d"],
                "supporting_evidence": [
                    {"source_id": "c", "claim_id": "claim-c", "locator": "p. 999"},
                    {"source_id": "d", "claim_id": "claim-d", "locator": "p. 999"},
                ],
            },
        ]
    }
    report = build_literature_report(rows, reasoner=reasoner)
    identities = {row["semantic_identity"] for row in report["cluster_registry"]["clusters"]}
    assert {"institutional trust", "legitimacy mediator"}.issubset(identities)
    trust = next(row for row in report["cluster_registry"]["clusters"] if row["semantic_identity"] == "institutional trust")
    assert trust["formation_route"] == "reasoner_proposal"
    assert trust["source_ids"] == ["a", "b", "e"]
    assert any(
        row.get("proposal_id") == "proposal-trust" and row.get("action") == "narrow"
        for row in report["cluster_registry"]["rejected_proposals"]
    )
    assert any(row.get("proposal_id") == "bad-locator" for row in report["cluster_registry"]["rejected_proposals"])


def test_cluster_projects_only_reconciled_tags_shared_by_independent_families() -> None:
    rows = [
        profile("a", tags=["shared-topic", "one-off-a"]),
        profile("b", tags=["shared-topic", "one-off-b"]),
        profile("c", tags=["unrelated-tag"]),
    ]
    cluster = cluster_for(rows)
    assert cluster["shared_normalized_tags"] == ["shared-topic"]

    typed = [
        EvidenceProfile(
            source_id=f"typed-{index}",
            note_id=f"typed-note-{index}",
            study_family_id=f"typed-family-{index}",
            coverage={"status": "full_text"},
            validity={"status": "valid"},
            concepts=["civil resistance"],
            features={"zotero_tag_context": ["Shared Topic"]},
        )
        for index in range(2)
    ]
    typed_cluster = cluster_for(normalize_evidence_profiles(typed), "civil resistance")
    assert typed_cluster["shared_normalized_tags"] == ["shared-topic"]


def test_profile_exclusion_coverage_and_validity_create_explicit_unclustered_reasons() -> None:
    rows = [
        profile("a"),
        profile("b"),
        {**profile("limited"), "note_status": "abstract_only_atomic_note"},
        {**profile("excluded"), "excluded_from_synthesis": True},
        {**profile("invalid"), "validity": {"status": "invalid"}},
        profile("other", topic="unrelated coastline"),
    ]
    result = map_overlapping_clusters(rows)
    reasons = {row["source_id"]: row["reason"] for row in result["unclustered_sources"]}
    assert reasons["limited"] == "limited_profile_excluded_from_analytical_clustering"
    assert reasons["excluded"] == "limited_profile_excluded_from_analytical_clustering"
    assert reasons["invalid"] == "limited_profile_excluded_from_analytical_clustering"
    assert reasons["other"] == "no_coherent_multi_family_cluster"


def test_explicit_zotero_and_structured_finding_relations_are_mapped_without_tag_binning() -> None:
    citation_rows = [
        {
            **profile("a", topic="school finance"),
            "zotero_item_key": "ITEMA",
            "zotero_relations": {"dc:references": "http://zotero.org/users/local/items/ITEMB"},
        },
        {**profile("b", topic="ocean salinity"), "zotero_item_key": "ITEMB"},
    ]
    relation = map_profile_relations(citation_rows)[0]
    assert any(row["kind"] == "explicit_zotero_or_citation_relation" for row in relation["evidence"])

    finding_rows = [profile("a", topic="school finance"), profile("b", topic="ocean salinity")]
    finding_rows[0]["findings"][0]["claim"] = "Elite bargaining constrains local implementation capacity."
    finding_rows[1]["findings"][0]["claim"] = "Elite bargaining constrains implementation choices."
    relation = map_profile_relations(finding_rows)[0]
    assert any(row["kind"] == "structured_findings" for row in relation["evidence"])
    related_cluster = map_overlapping_clusters(finding_rows, [relation])["clusters"]
    assert len(related_cluster) == 1
    assert set(related_cluster[0]["source_ids"]) == {"a", "b"}

    weak_overlap = [profile("weak-a", topic="school finance"), profile("weak-b", topic="ocean salinity")]
    weak_overlap[0]["findings"][0]["claim"] = "An agreement can improve school financing."
    weak_overlap[1]["findings"][0]["claim"] = "An agreement can fail under ocean pressure."
    assert map_profile_relations(weak_overlap) == []


def test_overlap_policy_is_hard_capped_at_three_and_honors_model_field_name() -> None:
    topics = ["alpha mechanism", "beta mechanism", "gamma mechanism", "delta mechanism"]
    rows = [profile("a", topic=topics[0], extra_topics=topics[1:]), profile("b", topic=topics[0], extra_topics=topics[1:])]
    mapped = map_overlapping_clusters(rows, policy=LiteratureMappingPolicy(max_memberships=3))
    memberships = [cluster["cluster_id"] for cluster in mapped["clusters"] if "a" in cluster["source_ids"]]
    assert len(memberships) == 3
    assert mapped["max_cluster_memberships"] == 3

    one = map_overlapping_clusters(rows, policy=LiteratureMappingPolicy(max_memberships=1))
    assert len([cluster for cluster in one["clusters"] if "a" in cluster["source_ids"]]) == 1


def test_study_family_dedup_controls_emerging_and_source_backed_status() -> None:
    duplicate_family = [
        profile("a", family="study-one"),
        profile("a-reprint", family="study-one"),
        profile("b", family="study-two"),
    ]
    emerging = cluster_for(duplicate_family)
    assert emerging["source_count"] == 3
    assert emerging["independent_study_family_count"] == 2
    assert emerging["status"] == "emerging_cluster"

    backed = cluster_for([*duplicate_family, profile("c", family="study-three")])
    assert backed["independent_study_family_count"] == 3
    assert backed["status"] == "source_backed_cluster"

    only_one_family = [profile("x", family="one"), profile("x-reprint", family="one")]
    rejected = map_overlapping_clusters(only_one_family)
    assert rejected["clusters"] == []
    assert any(row["reason"] == "insufficient_independent_study_families" for row in rejected["rejected_proposals"])


def test_source_backed_threshold_policy_changes_status_without_allowing_singletons() -> None:
    rows = [profile("a"), profile("b"), profile("c")]
    cluster = cluster_for(rows)
    assert cluster["status"] == "source_backed_cluster"
    mapped = map_overlapping_clusters(rows, policy=LiteratureMappingPolicy(source_backed_threshold=4))
    target = next(row for row in mapped["clusters"] if row["semantic_identity"] == "institutional trust")
    assert target["status"] == "emerging_cluster"


def test_research_question_fragments_do_not_form_clusters_and_labels_preserve_phrase_order() -> None:
    unrelated = [
        {**profile("a", topic="school finance"), "research_questions": ["Can peace agreements last?"]},
        {**profile("b", topic="ocean salinity"), "research_questions": ["Can peace agreements last?"]},
    ]
    assert map_overlapping_clusters(unrelated)["clusters"] == []

    peace_rows = [profile("a", topic="peace agreement"), profile("b", topic="peace agreement")]
    peace_cluster = next(
        row
        for row in map_overlapping_clusters(peace_rows)["clusters"]
        if row["semantic_identity"] == "agreement peace"
    )
    assert peace_cluster["label"] == "peace agreement"


def test_near_identical_nested_topics_do_not_create_duplicate_clusters() -> None:
    rows = [
        profile(source, topic="mediation", extra_topics=["conflict mediation"])
        for source in ("a", "b", "c")
    ]
    mapped = map_overlapping_clusters(rows)
    identities = {row["semantic_identity"] for row in mapped["clusters"]}
    assert identities == {"mediation"}
    assert any(
        row["reason"] == "redundant_semantic_cluster"
        and row["semantic_identity"] == "conflict mediation"
        for row in mapped["rejected_proposals"]
    )


def test_cluster_auto_promotion_can_be_disabled_without_hiding_candidates() -> None:
    rows = [profile("a"), profile("b"), profile("c")]
    promoted = cluster_for(rows)
    assert promoted["status"] == "source_backed_cluster"
    assert promoted["qualification_status"] == "source_backed_cluster"
    assert promoted["promoted"] is True
    assert promoted["automation_status"] == "promoted"

    mapped = map_overlapping_clusters(
        rows,
        policy=LiteratureMappingPolicy(auto_promote_clusters=False),
    )
    candidate = next(row for row in mapped["clusters"] if row["semantic_identity"] == "institutional trust")
    assert candidate["status"] == "cluster_candidate"
    assert candidate["qualification_status"] == "source_backed_cluster"
    assert candidate["promoted"] is False
    assert candidate["automation_status"] == "candidate"


def test_evidence_matrix_has_all_dimensions_and_only_complete_locator_records() -> None:
    rows = [profile("a"), profile("b", method="comparative case study")]
    cluster = cluster_for(rows)
    matrices = build_evidence_matrices(rows, [cluster])
    matrix = matrices[0]
    assert tuple(matrix["dimension_names"]) == EVIDENCE_DIMENSIONS
    assert set(matrix["dimensions"]) == set(EVIDENCE_DIMENSIONS)
    for entries in matrix["dimensions"].values():
        for entry in entries:
            assert all(set(reference) >= {"claim_id", "source_id", "locator"} for reference in entry["evidence"])
            assert all(reference["locator"] for reference in entry["evidence"])

    missing = deepcopy(rows)
    missing[0]["findings"][0]["locator"] = ""
    matrix = build_evidence_matrices(missing, [cluster_for(missing)])[0]
    assert matrix["omitted_unlocated_cell_count"] > 0

    vague = deepcopy(rows)
    vague[0]["findings"][0]["locator"] = "somewhere in the article"
    matrix = build_evidence_matrices(vague, [cluster_for(vague)])[0]
    assert matrix["omitted_unlocated_cell_count"] > 0
    assert all(
        reference["source_id"] != "a"
        for entries in matrix["dimensions"].values()
        for entry in entries
        for reference in entry["evidence"]
    )


@pytest.mark.parametrize(
    ("directions", "expected"),
    [
        (("positive", "negative"), "debate"),
        (("positive", "positive"), "mapped_consensus"),
        (("mixed", "not reported"), "mixed_evidence"),
    ],
)
def test_debate_consensus_and_mixed_classification(directions: tuple[str, str], expected: str) -> None:
    rows = [profile("a", direction=directions[0]), profile("b", direction=directions[1])]
    assessment = build_debate_registry(rows, [cluster_for(rows)])["assessments"][0]
    assert assessment["classification"] == expected
    if expected == "debate":
        assert len(assessment["positions"]) == 2
        assert assessment["contradictions"]
        for section in ("positions", "contradictions", "boundaries", "method_fault_lines"):
            records = assessment[section]
            assert records
            assert "claim_id" in records[0]["evidence"][0]


def test_no_debate_without_two_locator_backed_findings() -> None:
    row = profile("a")
    manual_cluster = {
        "cluster_id": "cluster-manual",
        "source_ids": ["a"],
    }
    assessment = build_debate_registry([row], [manual_cluster])["assessments"][0]
    assert assessment["classification"] == "no_debate"
    assert assessment["positions"] == []


def test_debate_contradictions_always_cite_different_study_families() -> None:
    rows = [
        profile("a", family="family-one", direction="positive"),
        profile("b", family="family-two", direction="positive"),
        profile("c", family="family-one", direction="negative"),
    ]
    assessment = build_debate_registry(rows, [cluster_for(rows)])["assessments"][0]
    assert assessment["classification"] == "debate"
    for contradiction in assessment["contradictions"]:
        assert contradiction["evidence"][0]["study_family_id"] != contradiction["evidence"][1]["study_family_id"]


def test_debate_auto_promotion_can_be_disabled_without_hiding_candidate_assessment() -> None:
    rows = [profile("a", direction="positive"), profile("b", direction="negative")]
    promoted = build_literature_report(rows)["debate_registry"]
    assert promoted["debates"]
    assert all(row["status"] == "mapped_debate" for row in promoted["debates"])
    assert all(row["promoted"] is True for row in promoted["debates"])
    assert all(row["automation_status"] == "promoted" for row in promoted["debates"])
    assert promoted["debate_count"] == len(promoted["debates"])

    candidates = build_literature_report(
        rows,
        policy=LiteratureMappingPolicy(auto_promote_debates=False),
    )["debate_registry"]
    assert candidates["debate_candidates"]
    for assessment in candidates["debate_candidates"]:
        assert assessment["classification"] == "debate_candidate"
        assert assessment["evidence_classification"] == "debate"
        assert assessment["status"] == "debate_candidate"
        assert assessment["promoted"] is False
        assert assessment["automation_status"] == "candidate"
    assert candidates["debates"] == []
    assert candidates["debate_count"] == 0
    assert candidates["debate_candidate_count"] == len(candidates["debate_candidates"])


def test_opposite_directions_for_different_outcomes_are_not_a_debate() -> None:
    rows = [profile("a", direction="positive"), profile("b", direction="negative")]
    rows[0]["findings"][0].update(
        {
            "claim": "Institutional trust increases voter turnout.",
            "topic": "institutional trust",
            "outcome": "voter turnout",
        }
    )
    rows[1]["findings"][0].update(
        {
            "claim": "Institutional trust decreases reported corruption.",
            "topic": "institutional trust",
            "outcome": "reported corruption",
        }
    )

    assessment = build_debate_registry(rows, [cluster_for(rows)])["assessments"][0]
    assert assessment["classification"] in {"no_debate", "mixed_evidence"}
    assert assessment["positions"] == []
    assert assessment["contradictions"] == []


def test_opposite_directions_for_different_predictors_are_not_a_debate() -> None:
    rows = [profile("a", topic="mediation success"), profile("b", topic="mediation success")]
    rows[0]["findings"][0].update(
        {
            "claim": "Third-party mediator impartiality increases mediation success.",
            "topic": "",
            "outcome": "mediation success",
            "direction": "positive",
        }
    )
    rows[1]["findings"][0].update(
        {
            "claim": "Third-party mediator pressure decreases mediation success.",
            "topic": "",
            "outcome": "mediation success",
            "direction": "negative",
        }
    )

    assessment = build_debate_registry(rows, [cluster_for(rows, "mediation success")])["assessments"][0]
    assert assessment["classification"] in {"no_debate", "mixed_evidence"}
    assert assessment["contradictions"] == []


def test_contradictory_gap_names_the_exact_mapped_proposition() -> None:
    rows = [profile("a", direction="positive"), profile("b", direction="negative")]
    report = build_quality_report(rows)
    gap = next(row for row in report["gap_registry"]["gaps"] if row["rule"] == "contradictory_findings")
    assert "institutional trust" in gap["precise_missing_evidence"]
    assert "matched cases, measures, and periods" in gap["precise_missing_evidence"]
    assert gap["precise_missing_evidence"] != "Evidence that resolves the mapped, locator-backed finding directions."


@pytest.mark.parametrize("rule", GAP_RULES)
def test_every_allowed_gap_rule_has_a_deterministic_candidate_and_promotion_path(rule: str) -> None:
    signal = {
        "rule": rule,
        "topic": "institutional trust",
        "missing_evidence": f"Missing evidence for {rule} under rural conditions.",
        "why_matters": f"Why {rule} matters.",
        "contribution": f"Contribution for {rule}.",
    }
    rows = [
        profile("a", method="panel regression", gap_signals=[signal]),
        profile("b", method="comparative case study", gap_signals=[signal]),
    ]
    if rule == "contradictory_findings":
        rows[1]["findings"][0]["direction"] = "negative"
        rows[1]["findings"][0]["claim"] = "institutional trust has a negative result in b."
    if rule == "cross_cluster_integration":
        rows = [
            profile("a", topic="institutional trust"),
            profile("b", topic="institutional trust", method="comparative case study"),
            profile("c", topic="mediator legitimacy"),
            profile("d", topic="mediator legitimacy", method="field experiment"),
        ]
        clusters = map_overlapping_clusters(rows, map_profile_relations(rows))["clusters"]
        signal["topic"] = "institutional trust and mediator legitimacy"
        signal["missing_evidence"] = (
            "Evidence connecting institutional trust to mediator legitimacy under rural conditions."
        )
        signal["related_cluster_ids"] = [row["cluster_id"] for row in clusters]
        signal["supporting_claim_ids"] = ["claim-a", "claim-c"]
        rows[0]["gap_signals"] = [signal]
        rows[2]["gap_signals"] = [signal]
    report = build_quality_report(rows)
    gap = next(row for row in report["gap_registry"]["gaps"] if row["rule"] == rule)
    assert gap["status"] == "mapped_collection_gap"
    assert gap["scope"] == "collection_only"
    assert gap["promoted"] is True
    assert "human_review" not in gap
    assert gap["novelty_claimed"] is False
    metadata = gap["promotion_metadata"]
    assert (metadata["scope"], metadata["promoted"], metadata["novelty_claimed"]) == (
        "collection_only",
        True,
        False,
    )
    assert metadata["rule_results"][0]["collection_search_complete"] is True
    assert metadata["supporting_locators"]
    assert metadata["internal_search"]["results"]
    assert metadata["why_matters"] and metadata["contribution"]


def test_contradictory_gap_requires_opposing_comparable_claims() -> None:
    signal = {
        "rule": "contradictory_findings",
        "topic": "institutional trust",
        "missing_evidence": "Matched evidence adjudicating institutional trust effects under rural conditions.",
    }
    same_direction = [
        profile("a", direction="positive", gap_signals=[signal]),
        profile("b", direction="positive", method="case study", gap_signals=[signal]),
    ]
    candidates = generate_gap_candidates(
        same_direction,
        map_overlapping_clusters(same_direction)["clusters"],
        {"debates": []},
    )
    validated, _ = search_and_validate_gaps(candidates, same_direction)

    assert validated[0]["status"] == "rejected_rule_admission"
    assert validated[0]["promoted"] is False
    result = validated[0]["rule_results"][0]
    assert result["candidate_valid"] is False
    assert result["rule_admission_errors"] == ["contradiction_requires_opposing_comparable_claims"]
    report = build_literature_report(same_direction)
    assert not any(row["rule"] == "contradictory_findings" for row in report["gap_registry"]["gaps"])
    rejected = next(
        row
        for row in report["gap_registry"]["rejected_candidates"]
        if row["rule"] == "contradictory_findings"
    )
    assert rejected["status"] == "rejected_rule_admission"

    opposing = deepcopy(same_direction)
    opposing[1]["findings"][0]["direction"] = "negative"
    opposing[1]["findings"][0]["claim"] = "institutional trust has a negative result in b."
    candidates = generate_gap_candidates(
        opposing,
        map_overlapping_clusters(opposing)["clusters"],
        {"debates": []},
    )
    validated, _ = search_and_validate_gaps(candidates, opposing)

    assert validated[0]["status"] == "mapped_collection_gap"
    assert validated[0]["promoted"] is True
    assert validated[0]["rule_results"][0]["rule_specific_admission_passed"] is True


def test_gap_evidence_resolution_is_source_scoped_when_claim_ids_collide() -> None:
    first = profile("a", topic="institutional trust")
    second = profile("b", topic="mediator legitimacy")
    first["findings"][0]["claim_id"] = "shared-claim"
    second["findings"][0]["claim_id"] = "shared-claim"
    normalized = normalize_evidence_profiles([first, second])
    from auto_zettelkasten.literature import _signal_evidence

    lookup = {
        (claim["source_id"], claim["claim_id"]): claim
        for row in normalized
        for claim in row["claims"]
    }
    evidence = _signal_evidence(
        {
            "supporting_evidence": [
                {"source_id": "b", "claim_id": "shared-claim", "locator": "p. 10"}
            ]
        },
        None,
        lookup,
    )

    assert [(row["source_id"], row["claim_id"]) for row in evidence] == [("b", "shared-claim")]


def test_zero_gaps_is_a_valid_result_and_reasoner_cannot_introduce_generic_rule() -> None:
    rows = [profile("a", direction="positive", method="panel regression"), profile("b", direction="positive", method="case study")]
    report = build_literature_report(
        rows,
        reasoner={"gap_candidates": [{"rule": "generic_literature_gap", "topic": "trust", "missing_evidence": "anything"}]},
    )
    assert report["gap_registry"]["gaps"] == []
    assert report["manifest"]["gap_count"] == 0

    allowed_but_fabricated = build_literature_report(
        rows,
        reasoner={
            "gap_candidates": [
                {
                    "rule": "replication",
                    "topic": "institutional trust",
                    "missing_evidence": "A fabricated reasoner proposal.",
                    "supporting_evidence": [
                        {"claim_id": "invented", "source_id": "a", "locator": "p. 1"},
                        {"claim_id": "invented-2", "source_id": "b", "locator": "p. 2"},
                    ],
                }
            ]
        },
    )["gap_registry"]
    assert allowed_but_fabricated["gaps"] == []
    assert allowed_but_fabricated["rejected_candidates"] == []


def test_author_gap_gate_keeps_explicit_research_needs_but_not_plain_limitations() -> None:
    rows = [
        {
            **profile("a"),
            "gaps": [
                "Results may not generalize beyond the sampled cases.",
                "No empirical evidence tests the mechanism in rural cases.",
            ],
            "future_research": ["Replicate institutional trust findings with rural populations."],
        }
    ]
    gaps = build_quality_report(rows)["gap_registry"]["gaps"]
    missing = {row["precise_missing_evidence"] for row in gaps}
    assert "Results may not generalize beyond the sampled cases." not in missing
    assert "No empirical evidence tests the mechanism in rural cases." not in missing
    assert "Replicate institutional trust findings with rural populations." in missing


def test_author_gap_observed_pattern_uses_stable_claim_order() -> None:
    row = profile("a")
    row["findings"] = [
        {
            "claim_id": "claim-z",
            "claim": "The later claim text.",
            "topic": "institutional trust",
            "direction": "positive",
            "locator": "p. 12",
        },
        {
            "claim_id": "claim-a",
            "claim": "The earlier claim text.",
            "topic": "institutional trust",
            "direction": "positive",
            "locator": "p. 11",
        },
    ]
    row["future_research"] = ["Replicate institutional trust findings in rural populations."]
    normalized = normalize_evidence_profiles([row])

    candidates = generate_gap_candidates(normalized, [], {"debates": []})
    candidate = next(item for item in candidates if item["rule"] == "author_stated_gap")

    assert candidate["observed_pattern"].startswith("The earlier claim text. The later claim text.")


@pytest.mark.parametrize(
    ("answer_status", "expected"),
    [("answered", "rejected_answered_elsewhere"), ("partial", "narrowed_gap_lead")],
)
def test_internal_search_rejects_or_narrows_when_answered_elsewhere(answer_status: str, expected: str) -> None:
    signal = {
        "rule": "empirical_coverage",
        "topic": "institutional trust",
        "missing_evidence": "Rural institutional trust evidence.",
    }
    rows = [
        profile("a", method="panel regression", gap_signals=[signal]),
        profile("b", method="case study", gap_signals=[signal]),
        profile(
            "answer",
            method="field experiment",
            gap_answers=[
                {
                    "rule": "empirical_coverage",
                    "topic": "rural institutional trust",
                    "status": answer_status,
                    "claim_id": "answer-claim",
                    "locator": "p. 44",
                }
            ],
        ),
    ]
    report = build_quality_report(rows)
    registry_name = "rejected_candidates" if answer_status == "answered" else "gaps"
    gap = next(row for row in report["gap_registry"][registry_name] if row["rule"] == "empirical_coverage")
    assert gap["status"] == expected
    assert gap["promoted"] is False
    assert len(gap["internal_search_results"]) == 3
    assert report["internal_search_log"][0]["analytical_profile_count_searched"] == 3
    assert gap["countervailing_evidence"][0]["locator"] == "p. 44"


def test_same_rule_answer_on_unrelated_subject_cannot_reject_gap() -> None:
    signal = {
        "rule": "empirical_coverage",
        "topic": "institutional trust",
        "missing_evidence": "Rural institutional trust evidence.",
    }
    rows = [
        profile("a", method="panel regression", gap_signals=[signal]),
        profile("b", method="case study", gap_signals=[signal]),
        profile(
            "answer",
            topic="ocean salinity",
            method="field experiment",
            gap_answers=[
                {
                    "rule": "empirical_coverage",
                    "topic": "ocean salinity in deep water",
                    "status": "answered",
                    "claim_id": "claim-answer",
                    "locator": "p. 44",
                }
            ],
        ),
    ]
    gap = next(
        row
        for row in build_quality_report(rows)["gap_registry"]["gaps"]
        if row["rule"] == "empirical_coverage"
    )
    assert gap["status"] == "mapped_collection_gap"
    assert gap["countervailing_evidence"] == []


def test_gap_support_requires_explicit_or_semantic_link_to_the_gap_signal() -> None:
    signal = {
        "rule": "replication",
        "topic": "ocean salinity",
        "missing_evidence": "An independent replication of deep-water salinity estimates.",
    }
    unlinked_rows = [
        profile("a", gap_signals=[signal]),
        profile("b", method="case study", gap_signals=[signal]),
    ]
    unlinked_gap = next(
        row
        for row in build_literature_report(unlinked_rows)["gap_registry"]["rejected_candidates"]
        if row["rule"] == "replication"
    )
    assert unlinked_gap["status"] == "underspecified_gap"
    assert unlinked_gap["promoted"] is False
    assert unlinked_gap["supporting_evidence"] == []
    assert "missing_locator_backed_generation_evidence" in unlinked_gap["specificity_errors"]

    explicitly_linked_rows = [
        profile(
            "a",
            topic="ocean salinity",
            gap_signals=[{**signal, "supporting_claim_ids": ["claim-a"]}],
        ),
        profile(
            "b",
            topic="ocean salinity",
            method="case study",
            gap_signals=[{**signal, "supporting_claim_ids": ["claim-b"]}],
        ),
    ]
    linked_gap = next(
        row
        for row in build_quality_report(explicitly_linked_rows)["gap_registry"]["gaps"]
        if row["rule"] == "replication"
    )
    assert linked_gap["status"] == "mapped_collection_gap"
    assert linked_gap["promoted"] is True
    assert {row["claim_id"] for row in linked_gap["supporting_evidence"]} == {"claim-a", "claim-b"}


def test_incomplete_support_stays_gap_lead_and_limited_profile_only_warns() -> None:
    signal = {
        "rule": "replication",
        "topic": "institutional trust",
        "missing_evidence": "An independent replication with full outcome reporting.",
    }
    rows = [
        profile("a", gap_signals=[signal], locator=""),
        profile("b", method="case study"),
        profile(
            "limited",
            status="abstract_only_atomic_note",
            gap_signals=[
                {
                    "rule": "empirical_coverage",
                    "topic": "institutional trust",
                    "missing_evidence": "A limited profile must not originate this candidate.",
                }
            ],
        ),
    ]
    report = build_literature_report(rows)
    gap = next(row for row in report["gap_registry"]["rejected_candidates"] if row["rule"] == "replication")
    assert gap["status"] == "underspecified_gap"
    assert gap["promoted"] is False
    assert "missing_locator_backed_generation_evidence" in gap["specificity_errors"]
    assert all(row["rule"] != "empirical_coverage" for row in report["gap_registry"]["gaps"])


def test_closest_prior_and_gap_ranking_are_deterministic() -> None:
    signal = {
        "rule": "boundary_condition",
        "topic": "institutional trust",
        "missing_evidence": "Institutional trust outside urban cases.",
    }
    rows = [profile("b", method="case study", gap_signals=[signal]), profile("a", gap_signals=[signal])]
    first = build_quality_report(rows)["gap_registry"]["gaps"]
    second = build_quality_report(list(reversed(rows)))["gap_registry"]["gaps"]
    assert first == second
    gap = first[0]
    assert gap["ranking"]["stable_id"] == gap["gap_id"]
    assert gap["ranking"]["source_count"] == 2
    assert gap["ranking"]["locator_completeness"] == 1.0
    assert all(row["prior_id"].startswith("prior-") for row in gap["closest_prior_work"])
    assert all(row["overlap_explanation"].startswith("Matched collection terms:") for row in gap["closest_prior_work"])


def test_semantic_cluster_id_survives_membership_revision_and_revision_hash_changes() -> None:
    initial_profiles = [profile("a"), profile("b"), profile("c")]
    initial = build_literature_report(initial_profiles)
    initial_cluster = next(row for row in initial["cluster_registry"]["clusters"] if row["semantic_identity"] == "institutional trust")
    reordered = build_literature_report(list(reversed(initial_profiles)))
    reordered_cluster = next(row for row in reordered["cluster_registry"]["clusters"] if row["semantic_identity"] == "institutional trust")
    assert reordered_cluster["cluster_id"] == initial_cluster["cluster_id"]
    assert reordered_cluster["revision_hash"] == initial_cluster["revision_hash"]

    revised = build_literature_report([*initial_profiles, profile("d")], previous_registry=initial["cluster_registry"])
    revised_cluster = next(row for row in revised["cluster_registry"]["clusters"] if row["semantic_identity"] == "institutional trust")
    assert revised_cluster["cluster_id"] == initial_cluster["cluster_id"]
    assert revised_cluster["revision_hash"] != initial_cluster["revision_hash"]
    assert revised_cluster["registry_status"] == "revision"
    assert any(row["event"] == "revision" for row in revised["cluster_registry"]["ledger"])
    replay = build_literature_report([*initial_profiles, profile("d")], previous_registry=revised["cluster_registry"])
    assert any(row["event"] == "revision" for row in replay["cluster_registry"]["ledger"])
    assert any(row["event"] == "unchanged" for row in replay["cluster_registry"]["ledger"])


def test_registry_infers_split_merge_supersede_and_retire() -> None:
    previous = {
        "clusters": [
            {"cluster_id": "old-split", "semantic_identity": "old", "source_ids": ["a", "b", "c", "d"]},
            {"cluster_id": "old-supersede", "semantic_identity": "same topic", "source_ids": ["e"]},
            {"cluster_id": "old-retire", "semantic_identity": "retired", "source_ids": ["z"]},
            {"cluster_id": "old-merge-a", "semantic_identity": "left", "source_ids": ["m1"]},
            {"cluster_id": "old-merge-b", "semantic_identity": "right", "source_ids": ["m2"]},
        ]
    }
    current = [
        {"cluster_id": "new-split-a", "semantic_identity": "new-a", "source_ids": ["a", "b"], "revision_hash": "1"},
        {"cluster_id": "new-split-b", "semantic_identity": "new-b", "source_ids": ["c", "d"], "revision_hash": "2"},
        {"cluster_id": "new-supersede", "semantic_identity": "same topic", "source_ids": ["e"], "revision_hash": "3"},
        {"cluster_id": "new-merge", "semantic_identity": "combined", "source_ids": ["m1", "m2"], "revision_hash": "4"},
    ]
    events = {row["event"] for row in reconcile_cluster_registry(current, previous)["ledger"]}
    assert {"split", "merge", "supersede", "retire"}.issubset(events)


def test_compatibility_entry_accepts_current_rows_writes_all_outputs_and_has_no_generic_gap(tmp_path: Path) -> None:
    notes = [
        {
            "source_id": "source-a",
            "note_id": "note-a",
            "note_status": "analytical_atomic_note",
            "title": "Institutional Trust Reform",
            "study_family_id": "family-a",
            "method": "panel regression",
            "normalized_tags": ["shared-topic"],
            "note_path": "02_source_memory/notes/a.md",
        },
        {
            "source_id": "source-b",
            "note_id": "note-b",
            "note_status": "analytical_atomic_note",
            "title": "Institutional Trust Participation",
            "study_family_id": "family-b",
            "method": "comparative case study",
            "normalized_tags": ["shared-topic"],
            "note_path": "02_source_memory/notes/b.md",
        },
    ]
    source_set = {"source_set_id": "set-1", "dependency_hash": "dependency"}
    cluster_map, gap_map, packet, paths = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=notes,
        question="What changes trust?",
        run_id="compatibility",
    )
    assert cluster_map["clusters"]
    assert gap_map["gap_candidates"] == []
    assert gap_map["status"] == "complete_no_qualifying_gaps"
    assert packet["mapper_version"] == "0.5.0"
    assert all(path.exists() for path in paths)
    manifest = yaml.safe_load((tmp_path / "03_literature_synthesis" / "manifest.yml").read_text())
    assert set(manifest["artifacts"]) == {
            "manifest",
            "cluster_registry",
            "cluster_ledger",
            "cluster_syntheses",
            "evidence_matrices",
        "debate_registry",
        "gap_registry",
            "gap_memory",
            "gap_merge_ledger",
        "internal_search_log",
        "packet",
        "index",
        "canonical_map",
    }
    assert "Candidate gap requiring falsification" not in (tmp_path / "03_literature_synthesis" / "INDEX.md").read_text()
    cluster = cluster_map["clusters"][0]
    cluster_path = tmp_path / "03_literature_synthesis" / "clusters" / f"{cluster_note_stem(cluster)}.md"
    cluster_frontmatter = yaml.safe_load(cluster_path.read_text().split("\n---\n", 1)[0].removeprefix("---\n"))
    assert cluster_frontmatter["type"] == "literature_cluster"
    assert cluster_frontmatter["title"] == cluster_display_title(cluster)
    assert cluster["cluster_id"] in cluster_frontmatter["aliases"]
    assert cluster_path.name.startswith("Cluster - Institutional")
    assert cluster["cluster_id"] in cluster_path.name
    assert "auto-zettelkasten/cluster" in cluster_frontmatter["tags"]
    assert "shared-topic" in cluster_frontmatter["tags"]
    assert set(cluster_frontmatter["sources"]) == {
        "[[a|Institutional Trust Reform]]",
        "[[b|Institutional Trust Participation]]",
    }

    old_cluster_path = cluster_path.with_name(f"{cluster['cluster_id']}.md")
    old_gap_path = tmp_path / "03_literature_synthesis" / "gaps" / "candidates" / "gap-old-id.md"
    old_cluster_path.write_text("stale generated projection")
    old_gap_path.write_text("stale generated projection")
    build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=notes,
        question="What changes trust?",
        run_id="compatibility-replay",
    )
    assert not old_cluster_path.exists()
    assert not old_gap_path.exists()
    assert cluster_path.exists()

    map_root = Path(packet["map_path"])
    assert map_root.name == stable_literature_map_id(source_set, "What changes trust?")
    for relative in (
        "manifest.yml",
        "cluster_registry.yml",
        "cluster_ledger.yml",
        "cluster_syntheses.yml",
        "evidence_matrices.yml",
        "debate_registry.yml",
        "gap_registry.yml",
        "gap_memory.yml",
        "gap_merge_ledger.yml",
        "internal_search_log.yml",
        "packet.yml",
        "INDEX.md",
        "clusters/INDEX.md",
        "gaps/INDEX.md",
    ):
        assert (map_root / relative).is_file(), relative


def test_gap_markdown_has_native_tags_and_reciprocal_evidence_links(tmp_path: Path) -> None:
    base = [
        {**profile("a", tags=["shared-topic"]), "note_path": "02_source_memory/notes/a.md"},
        {**profile("b", method="case study", tags=["shared-topic"]), "note_path": "02_source_memory/notes/b.md"},
    ]
    cluster_id = cluster_for(base)["cluster_id"]
    signal = {
        "rule": "replication",
        "topic": "institutional trust",
        "missing_evidence": "An independent replication of institutional trust estimates.",
        "related_cluster_ids": [cluster_id],
    }
    rows = [
        {**base[0], "gap_signals": [{**signal, "supporting_claim_ids": ["claim-a"]}]},
        {**base[1], "gap_signals": [{**signal, "supporting_claim_ids": ["claim-b"]}]},
    ]
    evidence = [
        {"source_id": "a", "claim_id": "claim-a", "locator": "p. 10"},
        {"source_id": "b", "claim_id": "claim-b", "locator": "p. 10"},
    ]
    base_reasoner = {
        "cluster_syntheses": {
            cluster_id: {
                "cluster_id": cluster_id,
                "scope": "Institutional trust estimates",
                "coherence_rationale": "Both sources estimate the same relationship.",
                "synthesis": "The two studies provide comparable estimates.",
                "supporting_evidence": evidence,
                "central_findings": [
                    {"finding": "Both studies estimate institutional trust outcomes.", "evidence": evidence}
                ],
            }
        }
    }
    cluster_map, gap_map, _, _ = build_literature_map(
        tmp_path,
        source_set={"source_set_id": "set-gap-links", "dependency_hash": "dependency"},
        notes=[],
        profiles=rows,
        question=None,
        run_id="gap-links",
        reasoner=quality_reasoner(rows, base_reasoner),
    )
    gap = next(row for row in gap_map["gap_candidates"] if row["rule"] == "replication")
    gap_path = tmp_path / "03_literature_synthesis" / "gaps" / "candidates" / f"{gap_note_stem(gap)}.md"
    text = gap_path.read_text()
    frontmatter = yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n"))
    assert frontmatter["type"] == "literature_gap"
    assert frontmatter["title"] == gap_display_title(gap)
    assert gap["gap_id"] in frontmatter["aliases"]
    assert gap_path.name.startswith("Gap - An independent replication")
    assert gap["gap_id"] in gap_path.name
    assert "auto-zettelkasten/gap" in frontmatter["tags"]
    assert "auto-zettelkasten/gap/replication" in frontmatter["tags"]
    assert "shared-topic" in frontmatter["tags"]
    assert set(frontmatter["sources"]) == {
        "[[a|Institutional Trust in A]]",
        "[[b|Institutional Trust in B]]",
    }
    cluster = next(row for row in cluster_map["clusters"] if row["cluster_id"] == cluster_id)
    assert cluster["related_gap_ids"] == [gap["gap_id"]]
    assert gap["related_cluster_ids"] == [cluster_id]
    expected_cluster_link = f"[[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]"
    assert frontmatter["related_clusters"] == [expected_cluster_link]
    assert expected_cluster_link in text
    cluster_text = (
        tmp_path / "03_literature_synthesis" / "clusters" / f"{cluster_note_stem(cluster)}.md"
    ).read_text()
    cluster_frontmatter = yaml.safe_load(cluster_text.split("\n---\n", 1)[0].removeprefix("---\n"))
    expected_gap_link = f"[[{gap_note_stem(gap)}|{gap_display_title(gap)}]]"
    assert cluster_frontmatter["related_gaps"] == [expected_gap_link]
    linked_callout = (
        f"> [!question] [[{gap_note_stem(gap)}|Research opportunity: "
        f"{gap_display_title(gap).removeprefix('Gap: ')}]]"
    )
    assert linked_callout in cluster_text
    assert "## Gaps Emerging from This Cluster" not in cluster_text


def test_human_facing_cluster_and_gap_titles_keep_normal_descriptive_labels() -> None:
    cluster = {
        "cluster_id": "cluster-stable-agent-id",
        "label": "United Nations mediation architecture and institutional support across regional organizations",
    }
    gap = {
        "gap_id": "gap-stable-agent-id",
        "title": "Causal mechanisms linking inclusion of women and civil society to ceasefire durability",
    }

    assert "…" not in cluster_display_title(cluster)
    assert "…" not in gap_display_title(gap)
    assert cluster_note_stem(cluster).endswith("[cluster-stable-agent-id]")
    assert gap_note_stem(gap).endswith("[gap-stable-agent-id]")


def test_reasoned_cluster_markdown_explains_findings_debate_and_gap_lineage(tmp_path: Path) -> None:
    rows = [
        {**profile("a", direction="positive"), "note_path": "02_source_memory/notes/a.md"},
        {**profile("b", direction="negative", method="case study"), "note_path": "02_source_memory/notes/b.md"},
    ]
    cluster_id = cluster_for(rows)["cluster_id"]
    evidence = [
        {"source_id": "a", "claim_id": "claim-a", "locator": "p. 10"},
        {"source_id": "b", "claim_id": "claim-b", "locator": "p. 10"},
    ]
    reasoner = {
        "cluster_syntheses": {
            cluster_id: {
                "cluster_id": cluster_id,
                "scope": "How institutional trust changes participation outcomes.",
                "boundaries": ["The mapped evidence covers two study settings."],
                "coherence_rationale": "Both studies estimate the same trust-participation relationship.",
                "synthesis": "The studies agree that trust matters, but disagree about the direction under different designs.",
                "supporting_evidence": evidence,
                "central_findings": [
                    {
                        "technical_finding": "The two estimates point in opposite directions.",
                        "plain_english_meaning": "In practical terms, higher trust aligns with participation in one study and lower participation in the other.",
                        "evidence": evidence,
                    }
                ],
                "agreements": [],
                "positions": [
                    {"position": "Trust increases participation.", "evidence": [evidence[0]]},
                    {"position": "Trust decreases participation.", "evidence": [evidence[1]]},
                ],
                "contradictions": [
                    {
                        "proposition": "Direction of the trust-participation relationship",
                        "contradiction": "The estimated direction reverses across the two studies.",
                        "evidence": evidence,
                    }
                ],
                "boundary_conditions": [
                    {"boundary": "The estimates use different study designs.", "evidence": evidence}
                ],
                "methodological_fault_lines": [
                    {"fault_line": "Panel regression versus case-study inference.", "evidence": evidence}
                ],
                "related_clusters": [],
                "source_roles": [
                    {"role": "Positive-direction estimate", "evidence": [evidence[0]]},
                    {"role": "Negative-direction estimate", "evidence": [evidence[1]]},
                ],
                "gap_hypotheses": [
                    {
                        "rule": "boundary_condition",
                        "topic": "institutional trust and participation",
                        "precise_missing_evidence": "A matched test of whether research design changes the direction of the institutional trust-participation relationship.",
                        "observed_pattern": "The direction differs between panel-regression and case-study evidence.",
                        "evidence_needed": "A common population, outcome measure, and period analyzed with both designs.",
                        "why_matters": "The collection cannot tell whether the contradiction is substantive or design-driven.",
                        "contribution": "A matched design would adjudicate the mapped contradiction.",
                        "related_cluster_ids": [cluster_id],
                        "supporting_evidence": evidence,
                    }
                ],
            }
        }
    }
    cluster_map, gap_map, _, _ = build_literature_map(
        tmp_path,
        source_set={"source_set_id": "set-reasoned", "dependency_hash": "dependency"},
        notes=[],
        profiles=rows,
        question=None,
        run_id="reasoned-cluster",
        reasoner=quality_reasoner(rows, reasoner),
    )
    cluster = next(row for row in cluster_map["clusters"] if row["cluster_id"] == cluster_id)
    gap = next(row for row in gap_map["gap_candidates"] if row["rule"] == "boundary_condition")
    cluster_text = (
        tmp_path / "03_literature_synthesis" / "clusters" / f"{cluster_note_stem(cluster)}.md"
    ).read_text()
    gap_text = (
        tmp_path / "03_literature_synthesis" / "gaps" / "candidates" / f"{gap_note_stem(gap)}.md"
    ).read_text()
    for heading in (
        "## Central Findings",
        "## Debate Positions",
        "## Contradictions",
        "## Boundary Conditions",
        "## Methodological and Measurement Fault Lines",
    ):
        assert heading in cluster_text
    assert "## Gaps Emerging from This Cluster" not in cluster_text
    assert "> [!question] [[" in cluster_text
    assert "> **Puzzle:**" in cluster_text
    assert "> **Payoff:**" in cluster_text
    assert "> **Test:**" in cluster_text
    assert "Plain English:" in cluster_text
    assert "Panel regression versus case-study inference." in cluster_text
    assert "`claim-a`" in cluster_text and "p. 10" in cluster_text
    assert "## How the System Identified This Gap" in gap_text
    assert "## Why This Is Not an Obvious Gap" in gap_text
    assert "## Executable Study Design" in gap_text
    assert "panel-regression and case-study evidence" in gap_text
    assert f"[[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]" in gap_text


def test_synthesis_budget_resume_reuses_successful_calls_without_repayment(tmp_path: Path) -> None:
    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "checkpoint-reasoner"
        model = "checkpoint-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls.append("proposal")
            return {"clusters": []}

        def synthesize_cluster(self, profiles, request, *, context=None):
            self.calls.append("synthesis")
            cluster = context["cluster"]
            evidence = [
                {
                    "source_id": row["source_id"],
                    "claim_id": row["claims"][0]["claim_id"],
                    "locator": row["claims"][0]["locator"],
                }
                for row in profiles
            ]
            return {
                "cluster_id": cluster["cluster_id"],
                "scope": cluster["shared_question"],
                "coherence_rationale": cluster["coherence_rationale"],
                "synthesis": "The two sources address the same relationship.",
                "supporting_evidence": evidence,
                "central_findings": [{"finding": "The findings are comparable.", "evidence": evidence}],
            }

    source_set = {"source_set_id": "set-checkpoint", "dependency_hash": "dependency"}
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-checkpoint",
        run_id="synthesis-resume",
        provider="ollama",
        model="checkpoint-v1",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=1),
    )
    first_reasoner = Reasoner()
    with pytest.raises(RuntimeError, match="synthesis_call_budget"):
        build_literature_map(
            tmp_path,
            source_set=source_set,
            notes=[],
            profiles=rows,
            question=None,
            run_id="synthesis-resume",
            request=request,
            reasoner=first_reasoner,
        )
    assert first_reasoner.calls == ["proposal"]

    resumed_reasoner = Reasoner()
    _, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="synthesis-resume",
        request=request,
        reasoner=resumed_reasoner,
    )
    assert resumed_reasoner.calls == ["synthesis"]
    assert packet["synthesis_call_count"] == 1
    assert packet["synthesis_checkpoint_hit_count"] == 1

    replay_reasoner = Reasoner()
    _, _, replay_packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="synthesis-resume",
        request=request,
        reasoner=replay_reasoner,
    )
    assert replay_reasoner.calls == []
    assert replay_packet["synthesis_call_count"] == 0
    assert replay_packet["synthesis_checkpoint_hit_count"] == 2


def test_failed_synthesis_call_leaves_a_resumable_diagnostic_record(tmp_path: Path) -> None:
    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "failing-reasoner"
        model = "failing-v1"
        is_cloud = False

        def propose_clusters(self, profiles, request, *, context=None):
            raise RuntimeError("provider returned malformed cluster JSON")

        def synthesize_cluster(self, profiles, request, *, context=None):
            return {}

    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-failure-record",
        run_id="synthesis-failure-record",
        provider="ollama",
        model="failing-v1",
    )
    with pytest.raises(RuntimeError, match="provider returned malformed cluster JSON"):
        build_literature_map(
            tmp_path,
            source_set={"source_set_id": "set-failure-record", "dependency_hash": "dependency"},
            notes=[],
            profiles=rows,
            question=None,
            run_id="synthesis-failure-record",
            request=request,
            reasoner=Reasoner(),
        )

    failure = yaml.safe_load(
        (
            tmp_path
            / "11_state/runs/synthesis-failure-record/literature/synthesis/cluster_proposal/collection.yml"
        ).read_text()
    )
    assert failure["status"] == "failed"
    assert failure["error"] == {
        "type": "RuntimeError",
        "message": "provider returned malformed cluster JSON",
    }
    assert "response" not in failure


def test_synthesis_checkpoint_history_preserves_paid_successes_across_policy_changes(tmp_path: Path) -> None:
    from auto_zettelkasten.literature import _CheckpointedReasonerCalls

    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "history-reasoner"
        model = "history-v1"
        is_cloud = False

        def __init__(self, response=None, error=None) -> None:
            self.response = response
            self.error = error
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return self.response

    def runner(reasoner, *, max_calls: int):
        request = LiteratureMapRequest(
            workspace=tmp_path,
            source_set_id="set-history",
            run_id="history-run",
            provider="ollama",
            model="history-v1",
            literature_policy=LiteratureMappingPolicy(max_synthesis_calls=max_calls),
        )
        return _CheckpointedReasonerCalls(tmp_path, "history-run", reasoner, request)

    first = Reasoner({"clusters": [{"label": "first"}]})
    assert runner(first, max_calls=1)("cluster_proposal", "collection", "propose_clusters", rows, {})[
        "clusters"
    ][0]["label"] == "first"
    assert first.calls == 1

    failed = Reasoner(error=RuntimeError("temporary provider failure"))
    with pytest.raises(RuntimeError, match="temporary provider failure"):
        runner(failed, max_calls=2)("cluster_proposal", "collection", "propose_clusters", rows, {})
    canonical = yaml.safe_load(
        (tmp_path / "11_state/runs/history-run/literature/synthesis/cluster_proposal/collection.yml").read_text()
    )
    assert canonical["status"] == "completed"
    assert canonical["response"]["clusters"][0]["label"] == "first"
    assert (
        tmp_path / "11_state/runs/history-run/literature/synthesis/failures/cluster_proposal/collection.yml"
    ).is_file()

    second = Reasoner({"clusters": [{"label": "second"}]})
    assert runner(second, max_calls=2)("cluster_proposal", "collection", "propose_clusters", rows, {})[
        "clusters"
    ][0]["label"] == "second"
    assert second.calls == 1

    replay_first = Reasoner(error=AssertionError("historical paid call was repeated"))
    restored = runner(replay_first, max_calls=1)(
        "cluster_proposal", "collection", "propose_clusters", rows, {}
    )
    assert restored["clusters"][0]["label"] == "first"
    assert replay_first.calls == 0


def test_gap_checkpoint_dependency_ignores_set_like_sequence_order() -> None:
    from auto_zettelkasten.literature import _checkpoint_dependency_context

    first = {
        "candidates": [
            {"gap_id": "gap-b", "terms": ["regional", "mediation"]},
            {"gap_id": "gap-a", "terms": ["inclusion", "durability"]},
        ]
    }
    reordered = {
        "candidates": [
            {"gap_id": "gap-a", "terms": ["durability", "inclusion"]},
            {"gap_id": "gap-b", "terms": ["mediation", "regional"]},
        ]
    }

    assert _checkpoint_dependency_context(first) != _checkpoint_dependency_context(reordered)
    assert _checkpoint_dependency_context(first, sort_sequences=True) == _checkpoint_dependency_context(
        reordered,
        sort_sequences=True,
    )


def test_gap_adjudication_rejections_are_audit_only_and_retained_rationales_win() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication, normalize_evidence_profiles

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        {
            "gap_id": "gap-retained",
            "rule": "empirical_coverage",
            "topic": "institutional trust",
            "precise_missing_evidence": "Comparable institutional trust evidence outside the observed cases",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        {
            "gap_id": "gap-vague",
            "rule": "author_stated_gap",
            "precise_missing_evidence": "More research",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
    ]
    response = {
        "gaps": [
            {
                **_quality_rationale(candidates[0]),
                "title": "Does the result generalize beyond the observed cases?",
            }
        ],
        "rejected": [
            {"gap_id": "gap-retained", "status": "rejected", "reason": "Duplicate of another retained gap."},
            {"gap_id": "gap-vague", "status": "rejected", "reason": "Vague and missing a bounded relationship."},
        ],
    }

    visible, rejected = _apply_gap_adjudication(candidates, response, rows)

    assert [row["gap_id"] for row in visible] == ["gap-retained"]
    assert visible[0]["rationale_status"] == "reasoned"
    assert [row["gap_id"] for row in rejected] == ["gap-vague"]
    assert rejected[0]["status"] == "underspecified_gap"


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda rationale: rationale["value_assessment"].update(
                non_obviousness_passed=False,
                why_obvious_answer_is_inadequate="",
            ),
            "obvious_answer_not_falsified",
        ),
        (
            lambda rationale: rationale["study_design"].update(
                comparator="",
                identification_or_inference_strategy="process tracing",
                falsification_or_process_tests=[],
            ),
            "missing_study_design_comparator",
        ),
    ],
)
def test_obvious_or_nonexecutable_gaps_are_audit_only(mutation, expected_reason: str) -> None:
    signal = {
        "rule": "untested_mechanism",
        "topic": "women inclusion and ceasefire durability",
        "missing_evidence": (
            "Evidence distinguishing selection into inclusive negotiations from a causal effect "
            "of inclusion on ceasefire durability."
        ),
        "supporting_claim_ids": ["claim-a", "claim-b"],
    }
    rows = [
        profile("a", topic="women inclusion and ceasefire durability", gap_signals=[signal]),
        profile(
            "b",
            topic="women inclusion and ceasefire durability",
            method="comparative case study",
            gap_signals=[signal],
        ),
    ]
    probe = build_literature_report(rows)
    candidate = next(
        row
        for row in probe["gap_registry"]["rejected_candidates"]
        if row.get("status") == "rejected_gap_quality"
    )
    rationale = _quality_rationale(candidate)
    mutation(rationale)
    report = build_literature_report(rows, reasoner={"gap_rationales": [rationale]})

    assert report["gap_registry"]["gaps"] == []
    rejected = next(
        row for row in report["gap_registry"]["rejected_candidates"] if row["gap_id"] == candidate["gap_id"]
    )
    assert rejected["status"] == "rejected_gap_quality"
    assert expected_reason in rejected["quality_rejection_reasons"]


def test_semantic_duplicate_gaps_merge_with_a_stable_audit_ledger() -> None:
    first_signal = {
        "rule": "replication",
        "topic": "institutional trust",
        "missing_evidence": "An independent replication of institutional trust outcome estimates.",
        "supporting_claim_ids": ["claim-a", "claim-b"],
    }
    second_signal = {
        **first_signal,
        "missing_evidence": "A separate study that reproduces institutional trust outcome estimates.",
    }
    rows = [
        profile("a", gap_signals=[first_signal, second_signal]),
        profile("b", method="case study", gap_signals=[first_signal, second_signal]),
    ]
    probe = build_literature_report(rows)
    candidates = sorted(
        (
            row
            for row in probe["gap_registry"]["rejected_candidates"]
            if row.get("status") == "rejected_gap_quality" and row.get("rule") == "replication"
        ),
        key=lambda row: row["gap_id"],
    )
    assert len(candidates) == 2
    rationale = _quality_rationale(candidates[0])
    rationale["merged_from_gap_ids"] = [candidates[1]["gap_id"]]
    report = build_literature_report(rows, reasoner={"gap_rationales": [rationale]})

    assert [row["gap_id"] for row in report["gap_registry"]["gaps"]] == [candidates[0]["gap_id"]]
    assert report["gap_registry"]["merge_ledger"] == [
        {
            "event": "merge",
            "canonical_gap_id": candidates[0]["gap_id"],
            "merged_gap_id": candidates[1]["gap_id"],
            "reason": "same structured exposure-mechanism-outcome-population-setting puzzle",
        }
    ]
    merged = next(
        row
        for row in report["gap_registry"]["rejected_candidates"]
        if row["gap_id"] == candidates[1]["gap_id"]
    )
    assert merged["status"] == "merged_gap"


def test_rejected_gap_does_not_reserve_structured_signature() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        {
            "gap_id": "gap-low-quality",
            "rule": "replication",
            "topic": "institutional trust",
            "precise_missing_evidence": "Replicate institutional trust estimates.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        {
            "gap_id": "gap-valid",
            "rule": "replication",
            "topic": "institutional trust",
            "precise_missing_evidence": "Independently reproduce institutional trust estimates.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
    ]
    low_quality = _quality_rationale(candidates[0])
    low_quality["value_assessment"]["information_gain"] = "low"
    valid = _quality_rationale(candidates[1])

    visible, rejected = _apply_gap_adjudication(
        candidates,
        {"gaps": [low_quality, valid], "rejected": []},
        rows,
    )

    assert [row["gap_id"] for row in visible] == ["gap-valid"]
    assert next(row for row in rejected if row["gap_id"] == "gap-low-quality")[
        "status"
    ] == "rejected_gap_quality"


def test_gap_merge_rejects_structurally_unrelated_candidate() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        {
            "gap_id": "gap-trust",
            "rule": "replication",
            "topic": "institutional trust",
            "precise_missing_evidence": "Replicate institutional trust estimates.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        {
            "gap_id": "gap-salinity",
            "rule": "measurement_or_data",
            "topic": "ocean salinity",
            "precise_missing_evidence": "Validate deep-water salinity sensors.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
    ]
    rationale = _quality_rationale(candidates[0])
    rationale["merged_from_gap_ids"] = ["gap-salinity"]

    visible, rejected = _apply_gap_adjudication(
        candidates,
        {"gaps": [rationale], "rejected": []},
        rows,
    )

    assert visible[0]["merged_from_gap_ids"] == []
    assert visible[0]["merge_events"] == []
    unrelated = next(row for row in rejected if row["gap_id"] == "gap-salinity")
    assert unrelated["status"] == "rejected_gap_quality"


def test_gap_reframing_can_change_rule_when_topic_and_claim_evidence_match() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        {
            "gap_id": "gap-boundary",
            "rule": "boundary_condition",
            "topic": "regional institutional trust effects",
            "precise_missing_evidence": "Compare institutional trust effects across regional settings.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        {
            "gap_id": "gap-author",
            "rule": "author_stated_gap",
            "topic": "regional institutional trust effects",
            "precise_missing_evidence": "The author requests regional comparison of institutional trust effects.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
    ]
    rationale = _quality_rationale(candidates[0])
    rationale["reframed_from_gap_id"] = "gap-author"

    visible, _ = _apply_gap_adjudication(
        candidates,
        {"gaps": [rationale], "rejected": []},
        rows,
    )

    assert [row["gap_id"] for row in visible] == ["gap-boundary"]
    assert visible[0]["reframed_from_gap_id"] == "gap-author"


def test_gap_reframing_rejects_unrelated_evidence() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence_a = {
        "source_id": rows[0]["source_id"],
        "claim_id": rows[0]["claims"][0]["claim_id"],
        "locator": rows[0]["claims"][0]["locator"],
    }
    evidence_b = {
        "source_id": rows[1]["source_id"],
        "claim_id": rows[1]["claims"][0]["claim_id"],
        "locator": rows[1]["claims"][0]["locator"],
    }
    candidates = [
        {
            "gap_id": "gap-trust",
            "rule": "boundary_condition",
            "topic": "institutional trust",
            "precise_missing_evidence": "Compare institutional trust across settings.",
            "supporting_evidence": [evidence_a],
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        {
            "gap_id": "gap-other",
            "rule": "author_stated_gap",
            "topic": "ocean salinity",
            "precise_missing_evidence": "Validate deep-water salinity sensors.",
            "supporting_evidence": [evidence_b],
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
    ]
    rationale = _quality_rationale(candidates[0])
    rationale["reframed_from_gap_id"] = "gap-other"

    visible, rejected = _apply_gap_adjudication(
        candidates,
        {"gaps": [rationale], "rejected": []},
        rows,
    )

    assert visible == []
    trust_rejection = next(row for row in rejected if row["gap_id"] == "gap-trust")
    assert "reframing_not_evidence_constrained" in trust_rejection["quality_rejection_reasons"]


def test_completed_checkpoint_cleans_stringified_single_value_scalar() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidate = {
        "gap_id": "gap-checkpoint-shape",
        "rule": "boundary_condition",
        "topic": "institutional trust",
        "precise_missing_evidence": "Compare institutional trust across regional settings.",
        "supporting_evidence": evidence,
        "rule_results": [{"analytical_profile_count_searched": 2}],
    }
    rationale = _quality_rationale(candidate)
    rationale["study_design"]["ethical_constraints"] = "['standard research ethics']"

    visible, _ = _apply_gap_adjudication(
        [candidate],
        {"gaps": [rationale], "rejected": []},
        rows,
    )

    assert visible[0]["study_design"]["ethical_constraints"] == "standard research ethics"


def test_multiple_valid_inline_anchors_in_one_cluster_are_preserved() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles([profile("a"), profile("b", method="case study")])
    evidence_a = {
        "source_id": rows[0]["source_id"],
        "claim_id": rows[0]["claims"][0]["claim_id"],
        "locator": rows[0]["claims"][0]["locator"],
    }
    evidence_b = {
        "source_id": rows[1]["source_id"],
        "claim_id": rows[1]["claims"][0]["claim_id"],
        "locator": rows[1]["claims"][0]["locator"],
    }
    candidate = {
        "gap_id": "gap-two-anchors",
        "rule": "contradictory_findings",
        "topic": "institutional trust",
        "precise_missing_evidence": "Adjudicate two institutional trust findings.",
        "related_cluster_ids": ["cluster-trust"],
        "supporting_evidence": [evidence_a, evidence_b],
        "rule_results": [{"analytical_profile_count_searched": 2}],
    }
    rationale = _quality_rationale(candidate)
    rationale["anchors"] = [
        {
            "cluster_id": "cluster-trust",
            "section": "contradictions",
            "item_id": "cluster-item-contradictions-a",
        },
        {
            "cluster_id": "cluster-trust",
            "section": "contradictions",
            "item_id": "cluster-item-contradictions-b",
        },
    ]
    syntheses = {
        "cluster-trust": {
            "contradictions": [
                {"item_id": "cluster-item-contradictions-a", "evidence": [evidence_a]},
                {"item_id": "cluster-item-contradictions-b", "evidence": [evidence_b]},
            ]
        }
    }

    visible, _ = _apply_gap_adjudication(
        [candidate],
        {"gaps": [rationale], "rejected": []},
        rows,
        syntheses,
    )

    assert [anchor["item_id"] for anchor in visible[0]["anchors"]] == [
        "cluster-item-contradictions-a",
        "cluster-item-contradictions-b",
    ]


def test_audit_only_gap_creates_no_markdown_projection(tmp_path: Path) -> None:
    signal = {
        "rule": "untested_mechanism",
        "topic": "women inclusion and ceasefire durability",
        "missing_evidence": "A causal pathway test for women inclusion and ceasefire durability.",
        "supporting_claim_ids": ["claim-a", "claim-b"],
    }
    rows = [
        profile("a", topic="women inclusion and ceasefire durability", gap_signals=[signal]),
        profile("b", topic="women inclusion and ceasefire durability", method="case study", gap_signals=[signal]),
    ]
    build_literature_map(
        tmp_path,
        source_set={"source_set_id": "audit-only", "dependency_hash": "audit-only"},
        notes=[],
        profiles=rows,
        question=None,
        run_id="audit-only",
    )

    projected = tmp_path / "03_literature_synthesis" / "gaps" / "candidates"
    assert list(projected.glob("Gap - *.md")) == []
    registry = yaml.safe_load(
        (tmp_path / "03_literature_synthesis" / "gap_registry.yml").read_text()
    )
    assert any(row["status"] == "rejected_gap_quality" for row in registry["rejected_candidates"])


def test_public_run_entry_returns_report_and_map_id_ignores_run_timestamp(tmp_path: Path) -> None:
    rows = [profile("a"), profile("b", method="case study")]
    source_set = {"source_set_id": "set-semantic", "dependency_hash": "same-dependency"}
    first = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id="set-semantic", run_id="run-20260715"),
        profiles=rows,
        source_set=source_set,
    )
    second = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id="set-semantic", run_id="run-20990101"),
        profiles=list(reversed(rows)),
        source_set=source_set,
    )
    assert isinstance(first, LiteratureMapReport)
    assert first.status == second.status == "completed"
    assert first.map_id == second.map_id == stable_literature_map_id(source_set)
    assert first.run_id != second.run_id
    assert first.artifact_paths["manifest"] == second.artifact_paths["manifest"]
    assert (tmp_path / first.artifact_paths["manifest"]).is_file()

    changed = stable_literature_map_id({**source_set, "dependency_hash": "changed"})
    assert changed == first.map_id


def test_cluster_lifecycle_history_is_isolated_per_canonical_map(tmp_path: Path) -> None:
    request_a = LiteratureMapRequest(workspace=tmp_path, source_set_id="set-a", run_id="a-one")
    request_b = LiteratureMapRequest(workspace=tmp_path, source_set_id="set-b", run_id="b-one")
    source_set_a = {"source_set_id": "set-a", "dependency_hash": "dependency-a"}
    source_set_b = {"source_set_id": "set-b", "dependency_hash": "dependency-b"}
    rows_a = [profile("a1", topic="institutional trust"), profile("a2", topic="institutional trust")]
    rows_b = [profile("b1", topic="ocean salinity"), profile("b2", topic="ocean salinity")]
    first_a = run_literature_map(request_a, profiles=rows_a, source_set=source_set_a)
    mapped_b = run_literature_map(request_b, profiles=rows_b, source_set=source_set_b)
    replay_a = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id="set-a", run_id="a-two"),
        profiles=list(reversed(rows_a)),
        source_set=source_set_a,
    )
    assert first_a.map_id == replay_a.map_id != mapped_b.map_id
    registry_a = yaml.safe_load((tmp_path / replay_a.artifact_paths["cluster_registry"]).read_text())
    registry_b = yaml.safe_load((tmp_path / mapped_b.artifact_paths["cluster_registry"]).read_text())
    b_ids = {row["cluster_id"] for row in registry_b["clusters"]}
    assert not any(cluster_id in repr(registry_a["ledger"]) for cluster_id in b_ids)


def test_stage_callback_fires_before_each_systematic_stage() -> None:
    stages: list[str] = []
    build_literature_report([profile("a"), profile("b")], stage_callback=stages.append)
    assert stages == [
        "relation_mapping",
        "clustering",
        "evidence_matrices",
        "cluster_synthesis",
        "debate_mapping",
        "gap_detection",
        "internal_falsification",
        "projection",
    ]


def test_question_is_only_a_projection_lens_and_never_changes_map_identity() -> None:
    source_set = {"source_set_id": "set-semantic", "dependency_hash": "same-dependency"}
    assert stable_literature_map_id(source_set, None) == stable_literature_map_id(source_set, "What changes trust?")
    assert stable_literature_map_id(source_set, "What changes trust?") == stable_literature_map_id(
        source_set,
        "Where does participation matter?",
    )
