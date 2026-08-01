from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from auto_zettelkasten import literature
from auto_zettelkasten.literature import (
    DEBATE_STATES,
    GAP_RULES,
    _proposition_debate_state,
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
    map_topic_neighborhoods,
    normalize_evidence_profiles,
    reconcile_cluster_registry,
    search_and_validate_gaps,
)
from auto_zettelkasten.models import (
    CURRENT_ENGINE_VERSION,
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    SupportEnvelope,
)
from auto_zettelkasten.literature import run_literature_map, stable_literature_map_id


def supported_finding(
    source: str,
    topic: str,
    *,
    family: str | None = None,
    direction: str = "positive",
    method: str = "panel regression",
    locator: str = "p. 10",
    outcome: str | None = None,
    boundary_condition: str | None = None,
    evidence_role: str = "associational",
    argument_role: str = "none",
) -> dict[str, Any]:
    mapped_outcome = outcome or f"{topic} outcome"
    empirical_role = (
        evidence_role
        if evidence_role
        in {
            "descriptive",
            "associational",
            "causal",
            "mechanism_evidence",
        }
        else "none"
    )
    return {
        "evidence_anchor_id": f"claim-{source}-{topic.replace(' ', '-')}",
        "claim_id": f"claim-{source}-{topic.replace(' ', '-')}",
        "claim": f"{topic} shapes and predicts {mapped_outcome}.",
        "topic": topic,
        "direction": direction,
        "method": method,
        "data": f"dataset-{source}",
        "case": f"case-{source}",
        "period": f"20{len(source):02d}-2025",
        "outcome": mapped_outcome,
        "locator": locator,
        "uncertainty": "moderate",
        "boundary_condition": boundary_condition
        if boundary_condition is not None
        else f"boundary-{source}",
        "evidence_role": evidence_role,
        "support_envelope": {
            "empirical_role": empirical_role,
            "argument_role": argument_role,
            "coverage": "full_text",
            "scope": {
                "cases": [f"case-{source}"],
                "periods": [f"20{len(source):02d}-2025"],
                "outcomes": [mapped_outcome],
            },
            "restrictions": ["Does not establish causality."],
            "support_status": "supported",
        },
    }


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
                **supported_finding(
                    source,
                    topic,
                    family=family,
                    direction=direction,
                    method=method,
                    locator=locator,
                ),
                "evidence_anchor_id": f"claim-{source}",
                "claim_id": f"claim-{source}",
            }
        ],
        "gap_signals": gap_signals or [],
        "gap_answers": gap_answers or [],
    }


def cluster_for(profiles: list[dict], identity: str = "institutional trust") -> dict:
    result = map_overlapping_clusters(profiles, map_profile_relations(profiles))
    return next(
        row for row in result["clusters"] if row["semantic_identity"] == identity
    )


def _quality_rationale(candidate: Mapping[str, Any]) -> dict[str, Any]:
    missing = str(candidate.get("precise_missing_evidence") or "")
    topic = str(candidate.get("topic") or "the mapped relationship")
    return {
        "gap_id": candidate["gap_id"],
        "title": missing,
        "gap_statement": missing,
        "rule": candidate["rule"],
        "related_cluster_ids": list(candidate.get("related_cluster_ids", []) or []),
        "proposition_ids": list(candidate.get("proposition_ids", []) or []),
        "proposition_id": str(candidate.get("proposition_id") or ""),
        "originating_cluster_revisions": list(
            candidate.get("originating_cluster_revisions", []) or []
        ),
        "originating_cluster_revision": str(
            candidate.get("originating_cluster_revision") or ""
        ),
        "missing_cell": dict(candidate.get("missing_cell") or {}),
        "generation_explanation": str(
            candidate.get("generation_explanation")
            or "The cluster evidence generated this candidate."
        ),
        "observed_pattern": str(
            candidate.get("observed_pattern")
            or f"The collection maps an unresolved pattern involving {topic}."
        ),
        "precise_missing_evidence": missing,
        "supporting_evidence": list(candidate.get("supporting_evidence", []) or []),
        "countervailing_evidence": list(
            candidate.get("countervailing_evidence", []) or []
        ),
        "internal_search_summary": "Every analytical profile in the frozen collection was searched.",
        "closest_prior_explanation": "The closest collection evidence does not make the required comparison.",
        "decision_reasoning": "The candidate survives the obvious-answer and executable-design gates.",
        "evidence_needed": missing,
        "why_matters": str(
            candidate.get("why_matters")
            or f"Resolving the puzzle changes inference about {topic}."
        ),
        "contribution": str(
            candidate.get("contribution")
            or f"A matched test would distinguish explanations for {topic}."
        ),
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
        "resolution_path": {
            "path_type": "quantitative",
            "question": f"Under matched conditions, what explains variation in {topic}?",
            "evidence_needed": missing,
            "requirements": {
                "estimand": "The matched contrast in the specified outcome.",
                "comparison": "Matched cases with and without the focal exposure.",
                "identification": "Match on pre-exposure covariates and compare within matched sets.",
                "measurement": "Use common case identifiers and collection-defined outcome measures.",
            },
            "feasibility": "Feasible if the mapped datasets expose common case identifiers.",
            "limitations": ["Residual confounding", "Cross-study measurement mismatch"],
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
        if "gap_adjudication_did_not_retain_candidate"
        in (row.get("quality_rejection_reasons") or [])
    ]
    reasoner["gap_rationales"] = [_quality_rationale(row) for row in candidates]
    reasoner.setdefault("rejected_gap_rationales", [])
    return reasoner


def build_quality_report(
    rows: list[dict],
    base_reasoner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return build_literature_report(rows, reasoner=quality_reasoner(rows, base_reasoner))


def with_proposition_lineage(
    candidate: Mapping[str, Any],
    rows: list[dict],
    identity: str = "institutional trust",
) -> dict[str, Any]:
    cluster = cluster_for(rows, identity)
    proposition = cluster["propositions"][0]
    missing = str(candidate.get("precise_missing_evidence") or "")
    return {
        **dict(candidate),
        "related_cluster_ids": [cluster["cluster_id"]],
        "proposition_ids": [proposition["proposition_id"]],
        "proposition_id": proposition["proposition_id"],
        "originating_cluster_revisions": [cluster["revision_hash"]],
        "originating_cluster_revision": cluster["revision_hash"],
        "missing_cell": {
            "kind": str(candidate.get("rule") or "missing_relationship"),
            "description": missing,
        },
    }


def test_typed_untagged_profiles_cluster_semantically_and_tags_are_only_tiebreakers() -> (
    None
):
    typed = [
        EvidenceProfile(
            source_id=f"source-{index}",
            note_id=f"note-{index}",
            study_family_id=f"family-{index}",
            coverage={"status": "full_text"},
            validity={"status": "valid"},
            concepts=["civil resistance"],
            methods=[f"method-{index}"],
            evidence_anchors=[
                EvidenceAnchor(
                    evidence_anchor_id=f"claim-{index}",
                    source_id=f"source-{index}",
                    study_family_id=f"family-{index}",
                    evidence_role="associational",
                    claim="Civil resistance changes participation.",
                    direction="positive",
                    locator=f"p. {index + 1}",
                    support_envelope=SupportEnvelope(
                        empirical_role="associational",
                        coverage="full_text",
                        scope={"outcomes": ["participation"]},
                        restrictions=["Does not alone establish causality."],
                        support_status="supported",
                    ),
                )
            ],
        )
        for index in range(3)
    ]
    normalized = normalize_evidence_profiles(typed)
    mapped = map_overlapping_clusters(normalized, map_profile_relations(normalized))
    assert len(mapped["clusters"]) == 1
    cluster = mapped["clusters"][0]
    assert cluster["status"] == "source_backed_cluster"
    assert cluster["independent_study_family_count"] == 3
    assert cluster["propositions"][0]["comparability"]["passed"] is True
    assert all(
        reference["support_status"] == "supported"
        for reference in cluster["propositions"][0]["evidence"]
    )

    unrelated = [
        profile("a", topic="school finance", tags=["shared-tag"]),
        profile("b", topic="ocean salinity", tags=["shared-tag"]),
    ]
    unrelated_relations = map_profile_relations(unrelated)
    assert unrelated_relations
    assert map_overlapping_clusters(unrelated)["clusters"] == []

    mirrored = [
        {
            **profile("a", topic="school finance"),
            "semantic_topics": ["shared-tag"],
            "normalized_tags": ["shared-tag"],
        },
        {
            **profile("b", topic="ocean salinity"),
            "semantic_topics": ["shared-tag"],
            "normalized_tags": ["shared-tag"],
        },
    ]
    mirrored_relations = map_profile_relations(mirrored)
    assert mirrored_relations
    assert map_overlapping_clusters(mirrored)["clusters"] == []


def test_reasoner_proposal_narrows_invalid_members_without_erasing_valid_cluster() -> (
    None
):
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
                "source_ids": ["c"],
                "supporting_evidence": [
                    {"source_id": "c", "claim_id": "claim-c", "locator": "p. 999"},
                    {"source_id": "d", "claim_id": "claim-d", "locator": "p. 999"},
                ],
            },
        ]
    }
    report = build_literature_report(rows, reasoner=reasoner)
    clusters = report["cluster_registry"]["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["label"] == "Institutional trust mechanisms"
    assert clusters[0]["core_source_ids"] == ["a", "b"]
    rejected = {
        row.get("proposal_id"): row.get("reason")
        for row in report["cluster_registry"]["rejected_proposals"]
    }
    assert rejected == {
        "bad-locator": "no_valid_connected_family_relation",
    }


def test_reasoner_propositions_are_persisted_in_the_map_level_registry() -> None:
    rows = [profile(source) for source in ("a", "b", "c")]
    reasoner = {
        "cluster_proposals": [
            {
                "proposal_id": "proposal-provider-proposition",
                "label": "Institutional trust and participation",
                "semantic_identity": "institutional trust and participation",
                "source_ids": ["a", "b", "c"],
                "source_roles": {source: "core" for source in ("a", "b", "c")},
                "propositions": [
                    {
                        "statement": "Institutional trust predicts participation.",
                        "question": "Does institutional trust predict participation?",
                        "proposition_type": "empirical",
                        "source_ids": ["a", "b", "c"],
                        "evidence": [
                            {
                                "source_id": source,
                                "evidence_anchor_id": f"claim-{source}",
                                "locator": "p. 10",
                            }
                            for source in ("a", "b", "c")
                        ],
                        "comparability": {"passed": True},
                    }
                ],
            }
        ]
    }

    report = build_literature_report(rows, reasoner=reasoner)

    assert report["manifest"]["cluster_count"] == 1
    assert report["manifest"]["proposition_count"] == 1
    assert (
        report["propositions"]
        == report["cluster_registry"]["clusters"][0]["propositions"]
    )


def test_shared_tags_are_nonanalytical_topic_neighborhoods_not_cluster_support() -> (
    None
):
    rows = [
        profile("a", tags=["shared-topic", "one-off-a"]),
        profile("b", tags=["shared-topic", "one-off-b"]),
        profile("c", tags=["unrelated-tag"]),
    ]
    normalized = normalize_evidence_profiles(rows)
    relations = map_profile_relations(normalized)
    neighborhoods = map_topic_neighborhoods(normalized, relations)
    shared = next(
        row
        for row in neighborhoods
        if row["kind"] == "tag" and row["semantic_identity"] == "shared topic"
    )
    assert shared["source_ids"] == ["a", "b"]
    assert shared["analytical_support"] is False
    mapped = map_overlapping_clusters(
        normalized,
        relations,
        topic_neighborhoods=neighborhoods,
    )
    cluster = next(
        row
        for row in mapped["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    assert cluster["shared_normalized_tags"] == []
    assert shared["topic_neighborhood_id"] in cluster["topic_neighborhood_ids"]

    typed = [
        EvidenceProfile(
            source_id=f"typed-{index}",
            note_id=f"typed-note-{index}",
            study_family_id=f"typed-family-{index}",
            coverage={"status": "full_text"},
            validity={"status": "valid"},
            concepts=["civil resistance"],
            features={"zotero_tag_context": ["Shared Topic"]},
            evidence_anchors=[
                EvidenceAnchor(
                    source_id=f"typed-{index}",
                    study_family_id=f"typed-family-{index}",
                    evidence_role="associational",
                    claim="Civil resistance affects participation.",
                    direction="positive",
                    locator=f"p. {index + 1}",
                    support_envelope=SupportEnvelope(
                        empirical_role="associational",
                        coverage="full_text",
                        scope={"outcomes": ["participation"]},
                        restrictions=["Does not alone establish causality."],
                        support_status="supported",
                    ),
                )
            ],
        )
        for index in range(2)
    ]
    typed_rows = normalize_evidence_profiles(typed)
    typed_neighborhoods = map_topic_neighborhoods(
        typed_rows, map_profile_relations(typed_rows)
    )
    typed_tag = next(row for row in typed_neighborhoods if row["kind"] == "tag")
    assert typed_tag["semantic_identity"] == "shared topic"
    assert typed_tag["analytical_support"] is False


def test_human_neighborhoods_are_machine_navigation_with_cluster_backlinks() -> None:
    report = build_literature_report(
        [profile("a"), profile("b", method="comparative case study")]
    )
    cluster = report["cluster_registry"]["clusters"][0]
    exposed_neighborhood_ids = set(cluster["topic_neighborhood_ids"])
    summaries = {
        row["neighborhood_id"]: row
        for row in report["navigation"]["human_neighborhood_summaries"]
    }

    assert exposed_neighborhood_ids
    for neighborhood_id in exposed_neighborhood_ids:
        assert summaries[neighborhood_id]["related_cluster_ids"] == [
            cluster["cluster_id"]
        ]

    assert report["navigation"]["human_neighborhood_summaries"]
    assert all(
        summary["related_cluster_ids"] == [cluster["cluster_id"]]
        for summary in report["navigation"]["human_neighborhood_summaries"]
        if summary["neighborhood_id"] in exposed_neighborhood_ids
    )


def test_profile_exclusion_coverage_and_validity_create_explicit_unclustered_reasons() -> (
    None
):
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
    assert reasons["other"] == "singleton_bounded_literature"


def test_relations_create_neighborhoods_but_not_analytical_clusters() -> None:
    citation_rows = [
        {
            **profile("a", topic="school finance"),
            "zotero_item_key": "ITEMA",
            "zotero_relations": {
                "dc:references": "http://zotero.org/users/local/items/ITEMB"
            },
        },
        {**profile("b", topic="ocean salinity"), "zotero_item_key": "ITEMB"},
    ]
    relation = map_profile_relations(citation_rows)[0]
    assert any(
        row["kind"] == "explicit_zotero_or_citation_relation"
        for row in relation["evidence"]
    )

    finding_rows = [
        profile("a", topic="school finance"),
        profile("b", topic="ocean salinity"),
    ]
    finding_rows[0]["findings"][0]["claim"] = (
        "Elite bargaining constrains local implementation capacity."
    )
    finding_rows[1]["findings"][0]["claim"] = (
        "Elite bargaining constrains implementation choices."
    )
    relation = map_profile_relations(finding_rows)[0]
    assert any(row["kind"] == "structured_findings" for row in relation["evidence"])
    neighborhoods = map_topic_neighborhoods(finding_rows, [relation])
    relation_neighborhood = next(
        row for row in neighborhoods if row["kind"] == "citation_or_relation"
    )
    assert relation_neighborhood["source_ids"] == ["a", "b"]
    assert relation_neighborhood["analytical_support"] is False
    assert (
        map_overlapping_clusters(
            finding_rows,
            [relation],
            topic_neighborhoods=neighborhoods,
        )["clusters"]
        == []
    )

    weak_overlap = [
        profile("weak-a", topic="school finance"),
        profile("weak-b", topic="ocean salinity"),
    ]
    weak_overlap[0]["findings"][0]["claim"] = (
        "An agreement can improve school financing."
    )
    weak_overlap[1]["findings"][0]["claim"] = (
        "An agreement can fail under ocean pressure."
    )
    assert map_profile_relations(weak_overlap) == []


def test_overlap_policy_is_hard_capped_at_three_and_honors_model_field_name() -> None:
    topics = ["alpha mechanism", "beta mechanism", "gamma mechanism", "delta mechanism"]
    rows = [profile("a", topic=topics[0]), profile("b", topic=topics[0])]
    for row in rows:
        source = row["source_id"]
        row["findings"].extend(supported_finding(source, topic) for topic in topics[1:])
    mapped = map_overlapping_clusters(
        rows, policy=LiteratureMappingPolicy(max_memberships=3)
    )
    memberships = [
        cluster["cluster_id"]
        for cluster in mapped["clusters"]
        if "a" in cluster["source_ids"]
    ]
    assert len(memberships) == 3
    assert mapped["max_cluster_memberships"] == 3

    one = map_overlapping_clusters(
        rows, policy=LiteratureMappingPolicy(max_memberships=1)
    )
    assert (
        len([cluster for cluster in one["clusters"] if "a" in cluster["source_ids"]])
        == 1
    )


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
    concentrated = map_overlapping_clusters(only_one_family)
    assert len(concentrated["clusters"]) == 1
    assert concentrated["clusters"][0]["qualification_status"] == (
        "evidence_concentrated_cluster"
    )


def test_source_backed_threshold_policy_changes_status_without_allowing_singletons() -> (
    None
):
    rows = [profile("a"), profile("b"), profile("c")]
    cluster = cluster_for(rows)
    assert cluster["status"] == "source_backed_cluster"
    mapped = map_overlapping_clusters(
        rows, policy=LiteratureMappingPolicy(source_backed_threshold=4)
    )
    target = next(
        row
        for row in mapped["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    assert target["status"] == "emerging_cluster"


def test_research_question_fragments_do_not_form_clusters_and_labels_preserve_phrase_order() -> (
    None
):
    unrelated = [
        {
            **profile("a", topic="school finance"),
            "research_questions": ["Can peace agreements last?"],
        },
        {
            **profile("b", topic="ocean salinity"),
            "research_questions": ["Can peace agreements last?"],
        },
    ]
    assert map_overlapping_clusters(unrelated)["clusters"] == []

    peace_rows = [
        profile("a", topic="peace agreement"),
        profile("b", topic="peace agreement"),
    ]
    peace_cluster = next(
        row
        for row in map_overlapping_clusters(peace_rows)["clusters"]
        if row["semantic_identity"] == "agreement peace"
    )
    assert peace_cluster["label"] == peace_cluster["propositions"][0]["statement"]
    assert peace_cluster["label"].startswith("peace agreement")
    assert not peace_cluster["label"].startswith("agreement peace")


def test_near_identical_nested_topics_do_not_create_duplicate_clusters() -> None:
    rows = [
        profile(source, topic="mediation", extra_topics=["conflict mediation"])
        for source in ("a", "b", "c")
    ]
    neighborhoods = map_topic_neighborhoods(rows, map_profile_relations(rows))
    mapped = map_overlapping_clusters(rows, topic_neighborhoods=neighborhoods)
    identities = {row["semantic_identity"] for row in mapped["clusters"]}
    assert identities == {"mediation"}
    nested = next(
        row for row in neighborhoods if row["semantic_identity"] == "conflict mediation"
    )
    assert nested["analytical_support"] is False
    assert (
        nested["topic_neighborhood_id"]
        in mapped["clusters"][0]["topic_neighborhood_ids"]
    )
    assert mapped["rejected_proposals"] == []


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
    candidate = next(
        row
        for row in mapped["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    assert candidate["status"] == "cluster_candidate"
    assert candidate["qualification_status"] == "source_backed_cluster"
    assert candidate["promoted"] is False
    assert candidate["automation_status"] == "candidate"


def test_proposition_matrix_has_comparable_rows_and_only_complete_locator_records() -> (
    None
):
    rows = [profile("a"), profile("b", method="comparative case study")]
    cluster = cluster_for(rows)
    matrices = build_evidence_matrices(rows, [cluster])
    matrix = matrices[0]
    assert matrix["matrix_version"] == "3"
    assert matrix["proposition_count"] == 1
    assert matrix["admission_passed"] is True
    assert matrix["source_level_metadata_inherited"] is False
    proposition = matrix["propositions"][0]
    assert proposition["comparability"]["passed"] is True
    assert proposition["independent_core_study_family_count"] == 2
    assert set(proposition["cells"]) == {"a", "b"}
    for cell in proposition["cells"].values():
        assert cell["stance_or_finding"]
        assert cell["scope"]["outcome"] == ["institutional trust outcome"]
        assert all(
            set(reference)
            >= {"evidence_anchor_id", "source_id", "locator", "support_status"}
            for reference in cell["evidence"]
        )
        assert all(
            reference["locator"] and reference["support_status"] == "supported"
            for reference in cell["evidence"]
        )

    missing = deepcopy(rows)
    missing[0]["findings"][0]["locator"] = ""
    missing_matrix = build_evidence_matrices(missing, [cluster])[0]
    assert missing_matrix["proposition_count"] == 0
    assert missing_matrix["admission_passed"] is False

    vague = deepcopy(rows)
    vague[0]["findings"][0]["locator"] = "somewhere in the article"
    vague_matrix = build_evidence_matrices(vague, [cluster])[0]
    assert vague_matrix["proposition_count"] == 0
    assert vague_matrix["admission_passed"] is False


def test_all_eight_debate_states_are_declared_and_proposition_cells_drive_classification() -> (
    None
):
    def cell(
        source: str,
        *,
        direction: str = "positive",
        evidence_type: list[str] | None = None,
        boundaries: list[str] | None = None,
        stance: str = "Institutional trust affects participation.",
    ) -> dict[str, Any]:
        return {
            "source_id": source,
            "study_family_id": f"family-{source}",
            "evidence_base_group_id": f"evidence-base-{source}",
            "counted_as_independent": True,
            "stance_or_finding": stance,
            "evidence_type": evidence_type or ["associational"],
            "boundary_conditions": boundaries or [],
            "direction_or_interpretation": [direction],
        }

    cases = {
        "mapped_debate": {
            "a": cell(
                "a",
                direction="positive",
                stance="Higher trust increases participation.",
            ),
            "b": cell(
                "b",
                direction="negative",
                stance="Higher trust decreases participation.",
            ),
        },
        "emerging_convergence": {"a": cell("a"), "b": cell("b")},
        "mixed_evidence": {
            "a": cell("a", direction="mixed"),
            "b": cell(
                "b",
                direction="not_reported",
                stance="A second estimate is inconclusive.",
            ),
        },
        "conditional_relationship": {
            "a": cell("a", boundaries=["urban municipalities"]),
            "b": cell("b", boundaries=["rural districts"]),
        },
        "complementary_positions": {
            "a": cell("a", evidence_type=["associational"]),
            "b": cell("b", evidence_type=["conceptual"]),
        },
        "single_position": {"a": cell("a")},
        "no_debate": {},
    }
    assert DEBATE_STATES == {
        "mapped_debate",
        "mapped_consensus",
        "emerging_convergence",
        "aligned_institutional_guidance",
        "within_program_consistency",
        "mixed_evidence",
        "conditional_relationship",
        "complementary_positions",
        "parallel_literatures",
        "single_position",
        "no_debate",
    }
    for expected, cells in cases.items():
        actual, _ = _proposition_debate_state({"cells": cells})
        assert actual == expected


def test_parallel_literatures_is_aggregated_from_multiple_single_position_propositions(
    monkeypatch,
) -> None:
    matrix = {
        "cluster_id": "cluster-parallel",
        "propositions": [
            {
                "proposition_id": "proposition-a",
                "statement": "Institutional trust shapes participation.",
                "cells": {"a": {"direction_or_interpretation": ["positive"]}},
            },
            {
                "proposition_id": "proposition-b",
                "statement": "Mediator legitimacy shapes compliance.",
                "cells": {"b": {"direction_or_interpretation": ["positive"]}},
            },
        ],
    }
    monkeypatch.setattr(
        "auto_zettelkasten.literature.build_evidence_matrices",
        lambda profiles, clusters: [matrix],
    )
    assessment = build_debate_registry([], [{"cluster_id": "cluster-parallel"}])[
        "assessments"
    ][0]
    assert assessment["classification"] == "parallel_literatures"


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
    assert assessment["classification"] == "mapped_debate"
    cells = assessment["proposition_assessments"][0]["cells"].values()
    positive_families = {
        cell["study_family_id"]
        for cell in cells
        if cell["direction_or_interpretation"] == ["positive"]
    }
    negative_families = {
        cell["study_family_id"]
        for cell in cells
        if cell["direction_or_interpretation"] == ["negative"]
    }
    assert any(
        left != right for left in positive_families for right in negative_families
    )


def test_debate_auto_promotion_can_be_disabled_without_hiding_candidate_assessment() -> (
    None
):
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
    assert candidates["debate_candidates"] == []
    assert len(candidates["assessments"]) == 1
    assessment = candidates["assessments"][0]
    assert assessment["classification"] == "mapped_debate"
    assert assessment["evidence_classification"] == "mapped_debate"
    assert assessment["status"] == "mapped_debate"
    assert assessment["promoted"] is False
    assert assessment["automation_status"] == "mapped"
    assert candidates["debates"] == []
    assert candidates["debate_count"] == 0
    assert candidates["debate_candidate_count"] == 0


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

    mapped = map_overlapping_clusters(rows)
    assert mapped["clusters"] == []
    assert {row["reason"] for row in mapped["unclustered_sources"]} == {
        "singleton_bounded_literature"
    }


def test_opposite_directions_for_different_predictors_are_not_a_debate() -> None:
    rows = [
        profile("a", topic="mediation success"),
        profile("b", topic="mediation success"),
    ]
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

    mapped = map_overlapping_clusters(rows)
    assert mapped["clusters"] == []
    assert {row["reason"] for row in mapped["unclustered_sources"]} == {
        "singleton_bounded_literature"
    }


def test_contradictory_gap_names_the_exact_mapped_proposition() -> None:
    rows = [profile("a", direction="positive"), profile("b", direction="negative")]
    report = build_quality_report(rows)
    gap = next(
        row
        for row in report["gap_registry"]["gaps"]
        if row["rule"] == "contradictory_findings"
    )
    assert "institutional trust" in gap["precise_missing_evidence"]
    assert "matched cases, measures, and periods" in gap["precise_missing_evidence"]
    assert (
        gap["precise_missing_evidence"]
        != "Evidence that resolves the mapped, locator-backed finding directions."
    )


@pytest.mark.parametrize("rule", GAP_RULES)
def test_every_allowed_gap_rule_has_a_deterministic_candidate_and_promotion_path(
    rule: str,
) -> None:
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
    if rule == "cross_cluster_integration":
        rows = [
            profile("a", topic="institutional trust"),
            profile("b", topic="institutional trust", method="comparative case study"),
            profile("c", topic="mediator legitimacy"),
            profile("d", topic="mediator legitimacy", method="field experiment"),
        ]
        clusters = map_overlapping_clusters(rows, map_profile_relations(rows))[
            "clusters"
        ]
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
    assert gap["status"] == "collection_surviving_gap"
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
    assert gap["proposition_ids"]
    assert gap["originating_cluster_revisions"]
    assert gap["missing_cell"]["description"] == gap["precise_missing_evidence"]


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

    assert validated[0]["status"] == "underspecified_gap"
    assert validated[0]["promoted"] is False
    result = validated[0]["rule_results"][0]
    assert result["candidate_valid"] is False
    assert result["rule_admission_errors"] == [
        "contradiction_requires_opposing_comparable_claims"
    ]
    report = build_literature_report(same_direction)
    assert not any(
        row["rule"] == "contradictory_findings"
        for row in report["gap_registry"]["gaps"]
    )
    rejected = next(
        row
        for row in report["gap_registry"]["rejected_candidates"]
        if row["rule"] == "contradictory_findings"
    )
    assert rejected["status"] == "underspecified_gap"
    assert rejected["rule_results"][0]["decision"] == "reject_rule_admission"

    opposing = deepcopy(same_direction)
    opposing[1]["findings"][0]["direction"] = "negative"
    candidates = generate_gap_candidates(
        opposing,
        map_overlapping_clusters(opposing)["clusters"],
        {"debates": []},
    )
    validated, _ = search_and_validate_gaps(candidates, opposing)

    assert validated[0]["status"] == "collection_surviving_gap"
    assert validated[0]["promoted"] is True
    assert validated[0]["rule_results"][0]["rule_specific_admission_passed"] is True


def test_gap_evidence_resolution_is_source_scoped_when_claim_ids_collide() -> None:
    first = profile("a", topic="institutional trust")
    second = profile("b", topic="mediator legitimacy")
    first["findings"][0]["evidence_anchor_id"] = "shared-claim"
    first["findings"][0]["claim_id"] = "shared-claim"
    second["findings"][0]["evidence_anchor_id"] = "shared-claim"
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

    assert [(row["source_id"], row["claim_id"]) for row in evidence] == [
        ("b", "shared-claim")
    ]


def test_zero_gaps_is_a_valid_result_and_reasoner_cannot_introduce_generic_rule() -> (
    None
):
    rows = [
        profile("a", direction="positive", method="panel regression"),
        profile("b", direction="positive", method="case study"),
    ]
    report = build_literature_report(
        rows,
        reasoner={
            "gap_candidates": [
                {
                    "rule": "generic_literature_gap",
                    "topic": "trust",
                    "missing_evidence": "anything",
                }
            ]
        },
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


def test_author_gap_gate_keeps_explicit_research_needs_but_not_plain_limitations() -> (
    None
):
    future_research = "Replicate institutional trust findings with rural populations."
    rows = [
        {
            **profile("a"),
            "gaps": [
                "Results may not generalize beyond the sampled cases.",
                "No empirical evidence tests the mechanism in rural cases.",
            ],
            "future_research": [future_research],
        },
        {**profile("b", method="case study"), "future_research": [future_research]},
    ]
    gaps = build_quality_report(rows)["gap_registry"]["gaps"]
    missing = {row["precise_missing_evidence"] for row in gaps}
    assert "Results may not generalize beyond the sampled cases." not in missing
    assert "No empirical evidence tests the mechanism in rural cases." in missing
    assert future_research in missing


def test_author_gap_observed_pattern_uses_stable_claim_order() -> None:
    row = profile("a")
    later = deepcopy(row["findings"][0])
    earlier = deepcopy(row["findings"][0])
    later.update(
        {
            "evidence_anchor_id": "claim-z",
            "claim_id": "claim-z",
            "claim": "The later claim text.",
            "locator": "p. 12",
        }
    )
    earlier.update(
        {
            "evidence_anchor_id": "claim-a",
            "claim_id": "claim-a",
            "claim": "The earlier claim text.",
            "locator": "p. 11",
        }
    )
    row["findings"] = [
        later,
        earlier,
    ]
    row["future_research"] = [
        "Replicate institutional trust findings in rural populations."
    ]
    normalized = normalize_evidence_profiles([row])

    candidates = generate_gap_candidates(normalized, [], {"debates": []})
    candidate = next(item for item in candidates if item["rule"] == "author_stated_gap")

    assert candidate["observed_pattern"].startswith(
        "The earlier claim text. The later claim text."
    )


@pytest.mark.parametrize(
    ("answer_status", "expected"),
    [("answered", "answered_within_collection"), ("partial", "narrowed_by_collection")],
)
def test_internal_search_rejects_or_narrows_when_answered_elsewhere(
    answer_status: str, expected: str
) -> None:
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
    gap = next(
        row
        for row in report["gap_registry"][registry_name]
        if row["rule"] == "empirical_coverage"
    )
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
    assert gap["status"] == "collection_surviving_gap"
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
        for row in build_literature_report(unlinked_rows)["gap_registry"][
            "rejected_candidates"
        ]
        if row["rule"] == "replication"
    )
    assert unlinked_gap["status"] == "underspecified_gap"
    assert unlinked_gap["promoted"] is False
    assert unlinked_gap["supporting_evidence"] == []
    assert (
        "missing_locator_backed_generation_evidence"
        in unlinked_gap["specificity_errors"]
    )

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
    assert linked_gap["status"] == "collection_surviving_gap"
    assert linked_gap["promoted"] is True
    assert {row["claim_id"] for row in linked_gap["supporting_evidence"]} == {
        "claim-a",
        "claim-b",
    }


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
    gap = next(
        row
        for row in report["gap_registry"]["rejected_candidates"]
        if row["rule"] == "replication"
    )
    assert gap["status"] == "underspecified_gap"
    assert gap["promoted"] is False
    assert "missing_locator_backed_generation_evidence" in gap["specificity_errors"]
    assert all(
        row["rule"] != "empirical_coverage" for row in report["gap_registry"]["gaps"]
    )


def test_closest_prior_and_gap_ranking_are_deterministic() -> None:
    signal = {
        "rule": "boundary_condition",
        "topic": "institutional trust",
        "missing_evidence": "Institutional trust outside urban cases.",
    }
    rows = [
        profile("b", method="case study", gap_signals=[signal]),
        profile("a", gap_signals=[signal]),
    ]
    first = build_quality_report(rows)["gap_registry"]["gaps"]
    second = build_quality_report(list(reversed(rows)))["gap_registry"]["gaps"]
    assert first == second
    gap = first[0]
    assert gap["ranking"]["stable_id"] == gap["gap_id"]
    assert gap["ranking"]["source_count"] == 2
    assert gap["ranking"]["locator_completeness"] == 1.0
    assert all(
        row["prior_id"].startswith("prior-") for row in gap["closest_prior_work"]
    )
    assert all(
        row["overlap_explanation"].startswith("Matched collection terms:")
        for row in gap["closest_prior_work"]
    )


def test_semantic_cluster_id_survives_membership_revision_and_revision_hash_changes() -> (
    None
):
    initial_profiles = [profile("a"), profile("b"), profile("c")]
    initial = build_literature_report(initial_profiles)
    initial_cluster = next(
        row
        for row in initial["cluster_registry"]["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    reordered = build_literature_report(list(reversed(initial_profiles)))
    reordered_cluster = next(
        row
        for row in reordered["cluster_registry"]["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    assert reordered_cluster["cluster_id"] == initial_cluster["cluster_id"]
    assert reordered_cluster["revision_hash"] == initial_cluster["revision_hash"]

    revised = build_literature_report(
        [*initial_profiles, profile("d")], previous_registry=initial["cluster_registry"]
    )
    revised_cluster = next(
        row
        for row in revised["cluster_registry"]["clusters"]
        if row["semantic_identity"] == "institutional trust"
    )
    assert revised_cluster["cluster_id"] == initial_cluster["cluster_id"]
    assert revised_cluster["revision_hash"] != initial_cluster["revision_hash"]
    assert revised_cluster["registry_status"] == "revision"
    assert any(
        row["event"] == "revision" for row in revised["cluster_registry"]["ledger"]
    )
    replay = build_literature_report(
        [*initial_profiles, profile("d")], previous_registry=revised["cluster_registry"]
    )
    assert any(
        row["event"] == "revision" for row in replay["cluster_registry"]["ledger"]
    )
    assert replay["cluster_registry"] == revised["cluster_registry"]


def test_registry_infers_split_merge_supersede_and_retire() -> None:
    previous = {
        "clusters": [
            {
                "cluster_id": "old-split",
                "semantic_identity": "old",
                "source_ids": ["a", "b", "c", "d"],
            },
            {
                "cluster_id": "old-supersede",
                "semantic_identity": "same topic",
                "source_ids": ["e"],
            },
            {
                "cluster_id": "old-retire",
                "semantic_identity": "retired",
                "source_ids": ["z"],
            },
            {
                "cluster_id": "old-merge-a",
                "semantic_identity": "left",
                "source_ids": ["m1"],
            },
            {
                "cluster_id": "old-merge-b",
                "semantic_identity": "right",
                "source_ids": ["m2"],
            },
        ]
    }
    current = [
        {
            "cluster_id": "new-split-a",
            "semantic_identity": "new-a",
            "source_ids": ["a", "b"],
            "revision_hash": "1",
        },
        {
            "cluster_id": "new-split-b",
            "semantic_identity": "new-b",
            "source_ids": ["c", "d"],
            "revision_hash": "2",
        },
        {
            "cluster_id": "new-supersede",
            "semantic_identity": "same topic",
            "source_ids": ["e"],
            "revision_hash": "3",
        },
        {
            "cluster_id": "new-merge",
            "semantic_identity": "combined",
            "source_ids": ["m1", "m2"],
            "revision_hash": "4",
        },
    ]
    events = {
        row["event"] for row in reconcile_cluster_registry(current, previous)["ledger"]
    }
    assert {"split", "merge", "revision", "retire"}.issubset(events)


def test_registry_retains_last_valid_map_on_material_coverage_regression() -> None:
    previous_clusters = [
        {
            "cluster_id": f"old-{index}",
            "semantic_identity": f"topic-{index}",
            "source_ids": [f"s{index}", f"s{index + 1}"],
            "revision_hash": f"old-{index}",
        }
        for index in range(0, 10, 2)
    ]
    registry = reconcile_cluster_registry(
        [
            {
                "cluster_id": "new",
                "semantic_identity": "narrow-topic",
                "source_ids": [f"s{index}" for index in range(7)],
                "revision_hash": "new",
            }
        ],
        {"clusters": previous_clusters},
    )

    assert {row["cluster_id"] for row in registry["clusters"]} == {"new"}
    assert not registry["clusters"][0].get("refresh_pending", False)
    assert {
        row["cluster_id"] for row in registry["retired_clusters"]
    } == {"old-8"}
    assert any(
        row.get("event") == "merge"
        and set(row.get("prior_cluster_ids", []))
        == {"old-0", "old-2", "old-4", "old-6"}
        for row in registry["ledger"]
    )


def test_compatibility_entry_accepts_current_rows_writes_all_outputs_and_has_no_generic_gap(
    tmp_path: Path,
) -> None:
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
    stale_neighborhood = (
        tmp_path
        / "03_literature_synthesis"
        / "maps"
        / stable_literature_map_id(source_set)
        / "Literature Neighborhoods - set-1.md"
    )
    stale_neighborhood.parent.mkdir(parents=True, exist_ok=True)
    stale_neighborhood.write_text("# Stale neighborhood projection\n")
    cluster_map, gap_map, packet, paths = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=notes,
        question="What changes trust?",
        run_id="compatibility",
    )
    assert cluster_map["status"] == "complete_no_analytical_clusters"
    assert cluster_map["clusters"] == []
    assert cluster_map["topic_neighborhoods"] == []
    assert cluster_map["navigation"]["unconfirmed_zotero_tag_count"] == 2
    assert {row["reason"] for row in cluster_map["unclustered_sources"]} == {
        "currently_unclustered"
    }
    assert gap_map["gap_candidates"] == []
    assert gap_map["status"] == "complete_no_qualifying_gaps"
    assert packet["mapper_version"] == CURRENT_ENGINE_VERSION
    assert all(path.exists() for path in paths)
    assert not stale_neighborhood.exists()
    manifest = yaml.safe_load(
        (tmp_path / "03_literature_synthesis" / "manifest.yml").read_text()
    )
    assert set(manifest["artifacts"]) == {
        "manifest",
        "cluster_registry",
        "cluster_ledger",
            "cluster_syntheses",
            "cluster_acquisition_ledger",
        "study_lineage_registry",
        "independence_assessments",
        "cluster_source_contributions",
        "quantitative_comparisons",
        "locator_audit",
        "coverage_register",
        "tag_concept_registry",
        "evidence_matrices",
        "navigation_facets",
        "topic_neighborhoods",
        "subject_tag_registry",
        "subject_tag_assignments",
        "typed_source_relations",
        "navigation_audit",
        "propositions",
        "debate_registry",
        "gap_registry",
        "gap_memory",
        "gap_merge_ledger",
        "internal_search_log",
        "packet",
        "literature_map_markdown",
        "index",
        "canonical_map",
    }
    map_text = Path(manifest["artifacts"]["literature_map_markdown"]).read_text()
    assert "Candidate gap requiring falsification" not in map_text
    for heading in (
        "# Literature Map",
        "## How to use this map",
        "## Literature clusters",
        "## Collection-relative gaps",
        "## Collection coverage",
        "## Navigate",
    ):
        assert heading in map_text
    assert "No coherent multi-source cluster was admitted." in map_text
    assert (
        "No sufficiently specific collection-native candidate was generated" in map_text
    )
    assert "Dependency hash" not in map_text and "fingerprint" not in map_text
    assert "Currently unclustered" in map_text
    assert "remain eligible for future plans" in map_text
    assert "Institutional Trust" in map_text
    cluster_root = tmp_path / "03_literature_synthesis" / "clusters"
    assert list(cluster_root.glob("Cluster - *.md")) == []

    old_cluster_path = cluster_root / "cluster-old-id.md"
    old_gap_path = (
        tmp_path / "03_literature_synthesis" / "gaps" / "candidates" / "gap-old-id.md"
    )
    old_cluster_path.write_text("stale generated projection")
    old_gap_path.write_text("stale generated projection")
    build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=notes,
        question="What changes trust?",
        run_id="compatibility-replay",
    )
    assert old_cluster_path.read_text() == "stale generated projection"
    assert not old_gap_path.exists()

    map_root = Path(packet["map_path"])
    assert map_root.name == stable_literature_map_id(source_set, "What changes trust?")
    for relative in (
        "manifest.yml",
        "cluster_registry.yml",
        "cluster_ledger.yml",
        "cluster_syntheses.yml",
        "evidence_matrices.yml",
        "topic_neighborhoods.yml",
        "propositions.yml",
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
    canonical_manifest = yaml.safe_load((map_root / "manifest.yml").read_text())
    assert (
        Path(canonical_manifest["artifacts"]["literature_map_markdown"]).read_text()
        == map_text
    )
    assert "the collection literature map" in (map_root / "INDEX.md").read_text()


def test_gap_markdown_has_native_tags_and_reciprocal_evidence_links(
    tmp_path: Path,
) -> None:
    base = [
        {
            **profile("a", tags=["shared-topic"]),
            "note_path": "02_source_memory/notes/a.md",
        },
        {
            **profile("b", method="case study", tags=["shared-topic"]),
            "note_path": "02_source_memory/notes/b.md",
        },
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
                    {
                        "finding": (
                            "Both studies estimate the same institutional-trust outcome and report a compatible "
                            "association, so the collection contains an emerging cross-study pattern rather than "
                            "a causal conclusion. In plain English, the result is repeated in two independent "
                            "settings, which makes it more credible inside this collection, but two studies do "
                            "not establish a mature collection-wide conclusion. Differences in design and case coverage also "
                            "limit how far the pattern can be generalized."
                        ),
                        "evidence": evidence,
                    }
                ],
                "boundaries": [
                    "The evidence covers only the two mapped study settings."
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
    gap_path = (
        tmp_path
        / "03_literature_synthesis"
        / "gaps"
        / "candidates"
        / f"{gap_note_stem(gap)}.md"
    )
    text = gap_path.read_text()
    frontmatter = yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n"))
    assert frontmatter["type"] == "literature_gap"
    assert frontmatter["title"] == gap_display_title(gap)
    assert gap["gap_id"] in frontmatter["aliases"]
    assert gap_path.name.startswith("Gap - An independent replication")
    assert gap["gap_id"] in gap_path.name
    assert "concept/institutional-trust" in frontmatter["tags"]
    assert all(not tag.startswith("auto-zettelkasten/") for tag in frontmatter["tags"])
    assert set(frontmatter["sources"]) == {
        "[[a|Institutional Trust in A]]",
        "[[b|Institutional Trust in B]]",
    }
    cluster = next(
        row for row in cluster_map["clusters"] if row["cluster_id"] == cluster_id
    )
    assert cluster["related_gap_ids"] == [gap["gap_id"]]
    assert gap["related_cluster_ids"] == [cluster_id]
    expected_cluster_link = (
        f"[[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]"
    )
    assert frontmatter["related_clusters"] == [expected_cluster_link]
    assert expected_cluster_link in text
    cluster_text = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster_note_stem(cluster)}.md"
    ).read_text()
    cluster_frontmatter = yaml.safe_load(
        cluster_text.split("\n---\n", 1)[0].removeprefix("---\n")
    )
    expected_gap_link = f"[[{gap_note_stem(gap)}|{gap_display_title(gap)}]]"
    assert cluster_frontmatter["related_gaps"] == [expected_gap_link]
    cluster_body = cluster_text.split("\n---\n", 1)[1]
    gap_body = text.split("\n---\n", 1)[1]
    assert "## Collection gaps" in cluster_body
    assert expected_gap_link in cluster_body
    assert "## Boundary, method, and measurement differences" in cluster_body
    assert "## Counterevidence and limits" not in gap_body
    assert "claim-a" not in cluster_body and "claim-b" not in cluster_body
    assert "claim-a" not in gap_body and "claim-b" not in gap_body


def test_human_facing_cluster_and_gap_titles_keep_normal_descriptive_labels() -> None:
    cluster = {
        "cluster_id": "cluster-stable-agent-id",
        "label": "United Nations mediation architecture and institutional support across regional organizations",
    }
    gap = {
        "gap_id": "gap-stable-agent-id",
        "title": (
            "Measurement inconsistency in conflict intensity operationalization blocks synthesis "
            "of mediation success effect sizes"
        ),
    }

    assert "…" not in cluster_display_title(cluster)
    assert "…" not in gap_display_title(gap)
    assert cluster_note_stem(cluster).endswith("[cluster-stable-agent-id]")
    assert gap_note_stem(gap).endswith("[gap-stable-agent-id]")


def test_reasoned_cluster_markdown_explains_findings_debate_and_gap_lineage(
    tmp_path: Path,
) -> None:
    rows = [
        {
            **profile("a", direction="positive"),
            "note_path": "02_source_memory/notes/a.md",
        },
        {
            **profile("b", direction="negative", method="case study"),
            "note_path": "02_source_memory/notes/b.md",
        },
    ]
    cluster_probe = cluster_for(rows)
    cluster_id = cluster_probe["cluster_id"]
    proposition_id = cluster_probe["proposition_ids"][0]
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
                "synthesis": (
                    "## Synthesis\n\nThe two studies address the same trust-participation proposition but report estimates in opposite "
                    "directions. Read together, they do not support a single collection-wide direction; instead, the "
                    "evidence indicates that the relationship is conditional on study setting or design. The panel and "
                    "case-study evidence are comparable enough to expose the disagreement, but not enough to determine "
                    "which methodological or contextual difference accounts for it. The cluster therefore establishes "
                    "a genuine mapped debate while preserving the limits of two study settings."
                ),
                "debate_state": "mapped_debate",
                "supporting_evidence": evidence,
                "central_findings": [
                    {
                        "assertion": (
                            "The two evidence bases estimate the same institutional-trust and participation "
                            "relationship but report opposite directions. In plain English, higher trust aligns "
                            "with more participation in one setting and less participation in the other. This is "
                            "a genuine collection-level disagreement, not a pooled estimate, and the different "
                            "research designs leave the map unable to decide whether the reversal reflects context, "
                            "measurement, or method. The collection therefore supports a mapped disagreement while "
                            "preserving uncertainty about the explanation for it."
                        ),
                        "plain_english_meaning": "In practical terms, higher trust aligns with participation in one study and lower participation in the other.",
                        "evidence": evidence,
                        "proposition_ids": [proposition_id],
                    }
                ],
                "agreements": [],
                "positions": [
                    {
                        "position": "Trust has a positive association with participation.",
                        "evidence": [evidence[0]],
                        "proposition_ids": [proposition_id],
                    },
                    {
                        "position": "Trust has a negative association with participation.",
                        "evidence": [evidence[1]],
                        "proposition_ids": [proposition_id],
                    },
                ],
                "contradictions": [
                    {
                        "proposition": "Direction of the trust-participation relationship",
                        "contradiction": "The estimated direction reverses across the two studies.",
                        "evidence": evidence,
                        "proposition_ids": [proposition_id],
                    }
                ],
                "boundary_conditions": [
                    {
                        "assertion": "The estimates use different study designs.",
                        "evidence": evidence,
                        "proposition_ids": [proposition_id],
                    }
                ],
                "methodological_fault_lines": [
                    {
                        "assertion": "Panel regression versus case-study inference.",
                        "evidence": evidence,
                        "proposition_ids": [proposition_id],
                    }
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
                        "proposition_id": proposition_id,
                        "originating_cluster_revision": cluster_probe["revision_hash"],
                        "supporting_evidence": evidence,
                    }
                ],
            }
        }
    }
    cluster_map, gap_map, packet, _ = build_literature_map(
        tmp_path,
        source_set={"source_set_id": "set-reasoned", "dependency_hash": "dependency"},
        notes=[],
        profiles=rows,
        question=None,
        run_id="reasoned-cluster",
        reasoner=quality_reasoner(rows, reasoner),
    )
    cluster = next(
        row for row in cluster_map["clusters"] if row["cluster_id"] == cluster_id
    )
    gap = next(
        row for row in gap_map["gap_candidates"] if row["rule"] == "boundary_condition"
    )
    cluster_text = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster_note_stem(cluster)}.md"
    ).read_text()
    gap_text = (
        tmp_path
        / "03_literature_synthesis"
        / "gaps"
        / "candidates"
        / f"{gap_note_stem(gap)}.md"
    ).read_text()
    cluster_body = cluster_text.split("\n---\n", 1)[1]
    gap_body = gap_text.split("\n---\n", 1)[1]
    for heading in (
        "## Research question",
        "## Verdict",
        "## Why these studies form a cluster",
        "## Sources and their roles",
        "## What the studies find",
        "## Consensus, disagreement, and uncertainty",
        "## Boundary, method, and measurement differences",
        "## Collection gaps",
        "## Members",
    ):
        assert heading in cluster_body
    assert "### Agreements" not in cluster_body
    assert "In practical terms" in cluster_body
    assert "Panel regression versus case-study inference." in cluster_body
    assert "Literature Neighborhoods" not in cluster_body
    assert "claim-a" not in cluster_body and "claim-b" not in cluster_body
    assert "p. 10" in cluster_body
    assert "## Where the gap came from" in gap_body
    assert "Originating proposition:" in gap_body
    assert "Missing evidence-matrix cell:" in gap_body
    assert "## Why the mapper raised it" in gap_body
    assert "## A route to resolving it" in gap_body
    assert "panel-regression and case-study evidence" in gap_body
    assert "claim-a" not in gap_body and "claim-b" not in gap_body
    assert (
        f"[[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]" in gap_body
    )
    manifest = yaml.safe_load(
        (tmp_path / "03_literature_synthesis" / "manifest.yml").read_text()
    )
    map_text = Path(manifest["artifacts"]["literature_map_markdown"]).read_text()
    assert "## How to use this map" in map_text
    assert "## Collection coverage" in map_text
    assert "## Literature clusters" in map_text
    assert "## Navigate" in map_text
    assert (
        f"### [[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]"
        in map_text
    )
    assert "The two evidence bases estimate the same institutional-trust" in map_text
    assert "**Verdict:** ## Synthesis" not in map_text
    assert "No candidate survived the collection-wide" not in map_text
    assert Path(packet["literature_map_markdown"]).read_text() == map_text
    assert (
        "the collection literature map"
        in (Path(packet["map_path"]) / "INDEX.md").read_text()
    )
    cluster_index_text = (
        tmp_path / "03_literature_synthesis" / "clusters" / "INDEX.md"
    ).read_text()
    assert (
        "The two evidence bases estimate the same institutional-trust"
        in cluster_index_text
    )
    assert "core sources" in cluster_index_text


def test_synthesis_budget_resume_reuses_successful_calls_without_repayment(
    tmp_path: Path,
) -> None:
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
                "synthesis": (
                    "The two sources address the same relationship and provide independent evidence for a bounded "
                    "collection-level consensus. Their findings are comparable because they use the same proposition "
                    "and outcome, while their different methods provide complementary perspectives rather than a "
                    "contradiction. The evidence remains associational, so the cluster does not establish a causal "
                    "effect or generalize beyond the mapped settings. The resulting verdict is that the shared pattern "
                    "is credible inside this collection but still constrained by design and coverage limits."
                ),
                "boundaries": [
                    "The verdict is limited to the two mapped study settings."
                ],
                "debate_state": "mapped_consensus",
                "supporting_evidence": evidence,
                "central_findings": [
                    {
                        "finding": (
                            "The two independent evidence bases report the same bounded association for the mapped "
                            "proposition. In plain English, both sources identify a shared pattern, but neither "
                            "establishes that the relationship is causal. Their different methods add useful context "
                            "without changing the direction of the result, while the represented settings and outcome "
                            "definitions limit any broader generalization beyond this collection."
                        ),
                        "evidence": evidence,
                    }
                ],
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
    with pytest.raises(RuntimeError, match="synthesis_call_budget"):
        build_literature_map(
            tmp_path,
            source_set=source_set,
            notes=[],
            profiles=rows,
            question=None,
            run_id="synthesis-resume",
            request=request,
            reasoner=resumed_reasoner,
        )
    assert resumed_reasoner.calls == []


def test_coverage_repair_recovers_supported_family_and_replays_without_calls(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "coverage-repair-reasoner"
        model = "coverage-repair-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def propose_clusters(self, profiles, request, *, context=None):
            repair_source_ids = list(
                (context or {}).get("coverage_repair_source_ids", []) or []
            )
            if not repair_source_ids:
                self.calls.append("proposal")
                return {
                    "clusters": [
                        {
                            "proposal_id": "unsupported-initial-proposal",
                            "label": "Unsupported umbrella",
                            "semantic_identity": "unsupported umbrella",
                            "shared_question": "Does an unsupported umbrella connect these sources?",
                            "bounded_object": "unsupported umbrella",
                            "source_ids": ["a", "b"],
                            "source_roles": {"a": "core", "b": "core"},
                            "propositions": [
                                {
                                    "proposition_id": "unsupported-proposition",
                                    "statement": "Ocean salinity determines school finance.",
                                    "source_ids": ["a", "b"],
                                    "evidence": [],
                                }
                            ],
                            "family_relations": [],
                        }
                    ]
                }

            self.calls.append("repair")
            evidence = [
                {
                    "source_id": row["source_id"],
                    "evidence_anchor_id": row["claims"][0]["evidence_anchor_id"],
                    "locator": row["claims"][0]["locator"],
                }
                for row in profiles
                if row["source_id"] in repair_source_ids
            ]
            return {
                "clusters": [
                    {
                        "proposal_id": "repaired-trust-family",
                        "label": "Institutional trust and its mapped outcome",
                        "semantic_identity": "institutional trust outcome relationship",
                        "shared_question": "How does institutional trust relate to the mapped outcome?",
                        "bounded_object": "institutional trust outcome relationship",
                        "source_ids": ["a", "b"],
                        "source_roles": {"a": "core", "b": "core"},
                        "propositions": [
                            {
                                "proposition_id": "provider-trust-proposition",
                                "statement": "Institutional trust shapes and predicts institutional trust outcome.",
                                "question": "How does institutional trust relate to its mapped outcome?",
                                "proposition_type": "empirical",
                                "source_ids": ["a", "b"],
                                "evidence": evidence,
                                "comparability": {"passed": True},
                            }
                        ],
                        "family_relations": [],
                    }
                ]
            }

    source_set = {
        "source_set_id": "set-coverage-repair",
        "dependency_hash": "dependency",
    }
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-coverage-repair",
        run_id="coverage-repair",
        provider="ollama",
        model="coverage-repair-v1",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=4),
    )
    reasoner = Reasoner()
    cluster_map, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="coverage-repair",
        request=request,
        reasoner=reasoner,
    )

    assert reasoner.calls == ["proposal", "repair"]
    assert len(cluster_map["clusters"]) == 1
    assert cluster_map["unclustered_sources"] == []
    assert packet["synthesis_call_count"] == 2

    replay = Reasoner()
    _, _, replay_packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="coverage-repair",
        request=request,
        reasoner=replay,
    )
    assert replay.calls == []
    assert replay_packet["synthesis_call_count"] == 2
    assert replay_packet["synthesis_new_call_count"] == 0
    assert replay_packet["synthesis_checkpoint_hit_count"] == 2


def test_deepseek_coverage_audit_uses_one_full_packet_then_all_residual_components() -> (
    None
):
    rows = normalize_evidence_profiles(
        [profile(f"source-{index:02d}") for index in range(20)]
    )
    relations = map_profile_relations(rows)
    neighborhoods = map_topic_neighborhoods(rows, relations)
    clustered = {
        "clusters": [],
        "unclustered_sources": [
            {"source_id": row["source_id"], "reason": "broad_topical_overlap_only"}
            for row in rows
        ],
    }

    class DeepSeekReasoner:
        name = "deepseek"
        model = "deepseek-v4-flash"
        context_window_tokens = 1_000_000

    plan = literature._coverage_audit_plan(
        clustered,
        rows,
        relations,
        neighborhoods,
        reasoner=DeepSeekReasoner(),
        request=LiteratureMapRequest(
            workspace=".", provider="deepseek", model="deepseek-v4-flash"
        ),
    )

    assert plan
    assert all(row["mode"] == "semantic_component" for row in plan)
    assert all(
        row["key"].startswith("collection--coverage-component-")
        for row in plan
    )
    assert {
        source_id
        for packet in plan
        for source_id in packet["focus_source_ids"]
    } == {row["source_id"] for row in rows}
    assert all(2 <= len(row["source_ids"]) <= 32 for row in plan)


def test_deepseek_coverage_fit_ignores_machine_only_profile_bulk() -> None:
    rows = normalize_evidence_profiles(
        [profile("compact-a"), profile("compact-b")]
    )
    for row in rows:
        row["machine_only_audit_blob"] = "x" * 2_000_000
    clustered = {
        "clusters": [],
        "unclustered_sources": [
            {"source_id": row["source_id"], "reason": "broad_topical_overlap_only"}
            for row in rows
        ],
    }

    class DeepSeekReasoner:
        name = "deepseek"
        model = "deepseek-v4-flash"
        context_window_tokens = 1_000_000

    plan = literature._coverage_audit_plan(
        clustered,
        rows,
        [],
        [],
        reasoner=DeepSeekReasoner(),
        request=LiteratureMapRequest(
            workspace=".", provider="deepseek", model="deepseek-v4-flash"
        ),
    )

    assert len(plan) == 1
    assert plan[0]["mode"] == "semantic_component"


def test_small_context_coverage_audit_keeps_semantic_peers_together() -> None:
    rows = normalize_evidence_profiles(
        [
            profile("a-first", topic="natural resource mediation"),
            profile("z-last", topic="natural resource mediation", method="case study"),
            profile("middle", topic="track two diplomacy"),
        ]
    )
    relations = map_profile_relations(rows)
    neighborhoods = map_topic_neighborhoods(rows, relations)
    clustered = {
        "clusters": [],
        "unclustered_sources": [
            {"source_id": row["source_id"], "reason": "broad_topical_overlap_only"}
            for row in rows
        ],
    }

    class SmallReasoner:
        name = "ollama"
        model = "small-local"
        context_window_tokens = 8_192

    plan = literature._coverage_audit_plan(
        clustered,
        rows,
        relations,
        neighborhoods,
        reasoner=SmallReasoner(),
        request=LiteratureMapRequest(
            workspace=".", provider="ollama", model="small-local"
        ),
    )

    natural_resource_component = next(
        row
        for row in plan
        if "a-first" in row["focus_source_ids"]
    )
    assert natural_resource_component["mode"] == "semantic_component"
    assert {"a-first", "z-last"}.issubset(
        natural_resource_component["source_ids"]
    )


def test_coverage_components_do_not_merge_distinct_conversations_through_bridge() -> None:
    rows = normalize_evidence_profiles(
        [
            profile("track-a", topic="track two diplomacy"),
            profile("track-b", topic="track two diplomacy"),
            profile("local-a", topic="local mediation"),
            profile("local-b", topic="local mediation"),
            profile("bridge", topic="mediation practice"),
        ]
    )
    by_source = {row["source_id"]: row for row in rows}
    by_source["track-a"]["concepts"] = ["track two diplomacy", "mediation"]
    by_source["track-b"]["concepts"] = ["track two diplomacy", "mediation"]
    by_source["local-a"]["concepts"] = ["local mediation", "mediation"]
    by_source["local-b"]["concepts"] = ["local mediation", "mediation"]
    by_source["bridge"]["concepts"] = [
        "track two diplomacy",
        "local mediation",
        "mediation",
    ]

    components = literature._coverage_signal_components(
        [row["source_id"] for row in rows], rows, [], []
    )
    source_sets = [set(row["source_ids"]) for row in components]

    assert any({"track-a", "track-b", "bridge"} <= values for values in source_sets)
    assert any({"local-a", "local-b", "bridge"} <= values for values in source_sets)
    assert not any(
        {"track-a", "track-b", "local-a", "local-b"} <= values
        for values in source_sets
    )
    assert set().union(*(set(row["focus_source_ids"]) for row in components)) == {
        row["source_id"] for row in rows
    }


def test_component_proposal_shards_union_same_conversation_members() -> None:
    responses = [
        {
            "clusters": [
                {
                    "proposal_id": "track-shard-a",
                    "label": "Track I and Track II diplomacy",
                    "semantic_identity": "track one track two diplomacy",
                    "source_ids": ["track-a", "bridge"],
                    "source_roles": {"track-a": "core", "bridge": "bridge"},
                    "supporting_evidence": [
                        {"source_id": "track-a", "claim_id": "a", "locator": "p. 1"}
                    ],
                    "propositions": [],
                    "family_relations": [],
                }
            ]
        },
        {
            "clusters": [
                {
                    "proposal_id": "track-shard-b",
                    "label": "Track I and Track II diplomacy",
                    "semantic_identity": "track one track two diplomacy",
                    "source_ids": ["track-b", "bridge"],
                    "source_roles": {"track-b": "core", "bridge": "bridge"},
                    "supporting_evidence": [
                        {"source_id": "track-b", "claim_id": "b", "locator": "p. 2"}
                    ],
                    "propositions": [],
                    "family_relations": [],
                }
            ]
        },
    ]

    proposals = literature._cluster_proposals_from_responses(responses)

    assert len(proposals) == 1
    assert set(proposals[0]["source_ids"]) == {"track-a", "track-b", "bridge"}
    assert proposals[0]["source_roles"] == {
        "track-a": "core",
        "track-b": "core",
        "bridge": "bridge",
    }
    assert {row["source_id"] for row in proposals[0]["supporting_evidence"]} == {
        "track-a",
        "track-b",
    }


def test_later_same_id_coverage_correction_can_remove_weak_members() -> None:
    initial = {
        "proposal_id": "internationalized-civil-war",
        "label": "Internationalized Civil-War Mediation",
        "semantic_identity": "internationalized civil war mediation",
        "source_ids": ["kane", "hellmuller", "unrelated"],
        "source_roles": {"kane": "core", "hellmuller": "core", "unrelated": "core"},
        "supporting_evidence": [],
        "propositions": [],
        "family_relations": [],
    }
    corrected = {
        **initial,
        "source_ids": ["kane", "hellmuller"],
        "source_roles": {"kane": "core", "hellmuller": "core"},
    }

    proposals = literature._cluster_proposals_from_responses(
        [{"clusters": [initial]}, {"clusters": [corrected]}]
    )

    assert len(proposals) == 1
    assert proposals[0]["source_ids"] == ["kane", "hellmuller"]
    assert proposals[0]["source_roles"] == {
        "kane": "core",
        "hellmuller": "core",
    }


def test_coverage_audit_partitions_large_subliterature_without_losing_sources(
    tmp_path: Path,
) -> None:
    rows = [profile(f"source-{index:02d}") for index in range(26)]

    class Reasoner:
        name = "component-coverage-repair-reasoner"
        model = "component-coverage-repair-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.repair_batches: list[list[str]] = []

        def propose_clusters(self, profiles, request, *, context=None):
            repair_source_ids = list(
                (context or {}).get("coverage_repair_source_ids", []) or []
            )
            if not repair_source_ids:
                source_ids = [row["source_id"] for row in profiles]
                return {
                    "clusters": [
                        {
                            "proposal_id": "unsupported-initial-family",
                            "label": "Unsupported initial family",
                            "semantic_identity": "unsupported initial family",
                            "shared_question": "Does an unsupported topic connect every source?",
                            "bounded_object": "unsupported initial topic",
                            "source_ids": source_ids,
                            "source_roles": {
                                source_id: "core" for source_id in source_ids
                            },
                            "propositions": [
                                {
                                    "proposition_id": "unsupported-initial-proposition",
                                    "statement": "Ocean salinity determines school finance.",
                                    "source_ids": source_ids,
                                    "evidence": [],
                                }
                            ],
                            "family_relations": [],
                        }
                    ]
                }

            self.repair_batches.append(repair_source_ids)
            batch_number = len(self.repair_batches)
            evidence = [
                {
                    "source_id": row["source_id"],
                    "evidence_anchor_id": row["claims"][0]["evidence_anchor_id"],
                    "locator": row["claims"][0]["locator"],
                }
                for row in profiles
                if row["source_id"] in repair_source_ids
            ]
            return {
                "clusters": [
                    {
                        "proposal_id": f"repair-batch-{batch_number}",
                        "label": f"Institutional trust evidence family {batch_number}",
                        "semantic_identity": f"institutional trust family {batch_number}",
                        "shared_question": "How does institutional trust relate to the mapped outcome?",
                        "bounded_object": "institutional trust outcome relationship",
                        "source_ids": repair_source_ids,
                        "source_roles": {
                            source_id: "core" for source_id in repair_source_ids
                        },
                        "propositions": [
                            {
                                "proposition_id": f"repair-proposition-{batch_number}",
                                "statement": "Institutional trust predicts the mapped outcome.",
                                "question": "How does institutional trust relate to the mapped outcome?",
                                "proposition_type": "empirical",
                                "source_ids": repair_source_ids,
                                "evidence": evidence,
                                "comparability": {"passed": True},
                            }
                        ],
                        "family_relations": [],
                    }
                ]
            }

    source_set = {
        "source_set_id": "set-batched-coverage-repair",
        "dependency_hash": "dependency",
    }
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-batched-coverage-repair",
        run_id="batched-coverage-repair",
        provider="ollama",
        model="batched-coverage-repair-v1",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=40),
    )
    reasoner = Reasoner()

    build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="batched-coverage-repair",
        request=request,
        reasoner=reasoner,
    )

    assert reasoner.repair_batches
    assert max(map(len, reasoner.repair_batches)) < 26
    assert {
        source_id
        for batch in reasoner.repair_batches
        for source_id in batch
    } == {
        f"source-{index:02d}" for index in range(26)
    }


def test_coverage_repair_history_remains_available_to_a_resumed_frozen_run(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "history-reasoner"
        model = "history-v1"

    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="frozen-set",
        run_id="history-run",
        provider="ollama",
        model="history-v1",
    )
    calls = literature._CheckpointedReasonerCalls(
        tmp_path,
        "history-run",
        Reasoner(),
        request,
    )
    current = calls.root / "cluster_proposal" / "collection--coverage-repair.yml"
    historical = (
        calls.root
        / "history"
        / "cluster_proposal"
        / "collection--coverage-repair"
        / "prior.yml"
    )
    current.parent.mkdir(parents=True, exist_ok=True)
    historical.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        yaml.safe_dump(
            {
                "status": "completed",
                "provider": "history-reasoner",
                "model": "history-v1",
                "response": {
                    "clusters": [
                        {"semantic_identity": "current", "source_ids": ["c", "d"]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    historical.write_text(
        yaml.safe_dump(
            {
                "status": "completed",
                "provider": "history-reasoner",
                "model": "history-v1",
                "response": {
                    "clusters": [
                        {"semantic_identity": "prior", "source_ids": ["a", "b"]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    responses = calls.completed_responses(
        "cluster_proposal", "collection--coverage-repair"
    )
    proposals = literature._cluster_proposals_from_responses(responses)

    assert {row["semantic_identity"] for row in proposals} == {"current", "prior"}


def test_coverage_repair_history_rejects_stale_profile_dependencies(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "history-reasoner"
        model = "history-v1"

    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="frozen-set",
        run_id="history-dependency-run",
        provider="ollama",
        model="history-v1",
    )
    calls = literature._CheckpointedReasonerCalls(
        tmp_path,
        "history-dependency-run",
        Reasoner(),
        request,
    )
    current = calls.root / "cluster_proposal" / "collection--coverage-repair.yml"
    historical = (
        calls.root
        / "history"
        / "cluster_proposal"
        / "collection--coverage-repair"
        / "prior.yml"
    )
    current.parent.mkdir(parents=True, exist_ok=True)
    historical.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "status": "completed",
        "provider": "history-reasoner",
        "model": "history-v1",
    }
    current.write_text(
        yaml.safe_dump(
            {
                **base,
                "dependency_component_hashes": {
                    "profile_dependencies": "current-profiles",
                    "prompt_version": "same-prompt",
                    "context": "current-repair-context",
                },
                "response": {
                    "clusters": [
                        {"semantic_identity": "current", "source_ids": ["c", "d"]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    historical.write_text(
        yaml.safe_dump(
            {
                **base,
                "dependency_component_hashes": {
                    "profile_dependencies": "stale-profiles",
                    "prompt_version": "same-prompt",
                    "context": "prior-repair-context",
                },
                "response": {
                    "clusters": [
                        {"semantic_identity": "stale", "source_ids": ["a", "b"]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    responses = calls.completed_responses(
        "cluster_proposal", "collection--coverage-repair"
    )
    proposals = literature._cluster_proposals_from_responses(responses)

    assert {row["semantic_identity"] for row in proposals} == {"current"}


def test_large_clusters_have_no_artificial_response_budget() -> None:
    assert not hasattr(literature, "_cluster_synthesis_response_budget")


def test_cluster_proposal_checkpoint_invalidates_when_profile_eligibility_changes(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b"), profile("c")]

    class Reasoner:
        name = "profile-content-reasoner"
        model = "profile-content-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            return {"clusters": []}

    source_set = {
        "source_set_id": "set-profile-content",
        "dependency_hash": "dependency",
    }
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-profile-content",
        run_id="profile-content-invalidation",
        provider="ollama",
        model="profile-content-v1",
    )
    first_reasoner = Reasoner()
    build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="profile-content-invalidation",
        request=request,
        reasoner=first_reasoner,
    )
    assert first_reasoner.calls == 1

    # The evidence-anchor revision and note dependency are unchanged, but the
    # profile is no longer eligible for analytical synthesis. Reusing the old
    # proposal would silently reason over the wrong evidence set.
    changed_rows = [dict(row) for row in rows]
    changed_rows[0]["excluded_from_synthesis"] = True
    changed_rows[0]["exclusion_reason"] = (
        "profile_or_note_validation_failed:legacy_gate"
    )
    second_reasoner = Reasoner()
    _, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=changed_rows,
        question=None,
        run_id="profile-content-invalidation",
        request=request,
        reasoner=second_reasoner,
    )

    assert second_reasoner.calls == 1
    assert packet["synthesis_call_count"] == 2
    assert packet["synthesis_new_call_count"] == 1
    assert packet["synthesis_checkpoint_hit_count"] == 0


def test_incomplete_cluster_verdict_gets_one_checkpointed_repair_call(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "repair-reasoner"
        model = "repair-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls.append("proposal")
            return {"clusters": []}

        def synthesize_cluster(self, profiles, request, *, context=None):
            is_repair = bool((context or {}).get("repair_requirements"))
            self.calls.append("repair" if is_repair else "synthesis")
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
                "synthesis": (
                    "The two sources address the same proposition and support a bounded collection-level consensus. "
                    "Their independent evidence is comparable across the mapped outcome, while the different methods "
                    "provide complementary perspectives rather than a contradiction. The relationship remains "
                    "associational and cannot establish a causal effect beyond the represented settings. The cluster "
                    "therefore supports the shared pattern while preserving clear design and coverage limits."
                    if is_repair
                    else ""
                ),
                "boundaries": [
                    "The verdict is limited to the two represented settings."
                ],
                "debate_state": "mapped_consensus",
                "supporting_evidence": evidence,
                "central_findings": [
                    {
                        "finding": (
                            "The two independent evidence bases report the same bounded association for the mapped "
                            "proposition. In plain English, both sources identify a shared pattern, but neither "
                            "establishes that the relationship is causal. Their different methods add useful context "
                            "without changing the direction of the result, while the represented settings and outcome "
                            "definitions limit any broader generalization beyond this collection."
                            if is_repair
                            else "The findings are comparable."
                        ),
                        "evidence": evidence,
                    }
                ],
            }

    source_set = {"source_set_id": "set-repair", "dependency_hash": "dependency"}
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-repair",
        run_id="synthesis-repair",
        provider="ollama",
        model="repair-v1",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=3),
    )
    reasoner = Reasoner()
    cluster_map, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="synthesis-repair",
        request=request,
        reasoner=reasoner,
    )

    assert reasoner.calls == ["proposal", "synthesis", "repair"]
    assert packet["synthesis_call_count"] == 3
    assert packet["status"] == "complete"
    synthesis = next(iter(cluster_map["cluster_syntheses"].values()))
    assert synthesis["status"] == "reasoned"
    assert synthesis["repair_attempted"] is True

    replay = Reasoner()
    _, _, replay_packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="synthesis-repair",
        request=request,
        reasoner=replay,
    )
    assert replay.calls == []
    assert replay_packet["synthesis_call_count"] == 3
    assert replay_packet["synthesis_new_call_count"] == 0
    assert replay_packet["synthesis_checkpoint_hit_count"] == 3


def test_failed_quality_repair_stays_partial_and_does_not_publish_a_thin_cluster(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "thin-repair-reasoner"
        model = "thin-repair-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls.append("proposal")
            return {"clusters": []}

        def synthesize_cluster(self, profiles, request, *, context=None):
            self.calls.append(
                "repair" if (context or {}).get("repair_requirements") else "synthesis"
            )
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
                "central_findings": [
                    {
                        "finding": "The sources report a comparable association.",
                        "evidence": evidence,
                    }
                ],
            }

    source_set = {"source_set_id": "set-thin-repair", "dependency_hash": "dependency"}
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-thin-repair",
        run_id="thin-repair",
        provider="ollama",
        model="thin-repair-v1",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=3),
    )
    reasoner = Reasoner()
    cluster_map, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="thin-repair",
        request=request,
        reasoner=reasoner,
    )

    synthesis = next(iter(cluster_map["cluster_syntheses"].values()))
    assert reasoner.calls == ["proposal", "synthesis", "repair"]
    assert packet["status"] == "partial"
    assert synthesis["status"] == "partial"
    assert synthesis["quality_status"] == "incomplete"
    assert synthesis["repair_attempted"] is True
    assert "verdict_too_thin" in synthesis["quality_errors"]
    assert (
        list((tmp_path / "03_literature_synthesis" / "clusters").glob("Cluster - *.md"))
        == []
    )

    replay = Reasoner()
    _, _, replay_packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="thin-repair",
        request=request,
        reasoner=replay,
    )
    assert replay.calls == []
    assert replay_packet["status"] == "partial"
    assert replay_packet["synthesis_checkpoint_hit_count"] == 3


def test_partial_remap_preserves_the_last_published_cluster_markdown(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b", method="case study")]
    cluster = cluster_for(rows)
    cluster_id = cluster["cluster_id"]
    proposition_id = cluster["proposition_ids"][0]
    evidence = [
        {
            "source_id": row["source_id"],
            "claim_id": row["findings"][0]["claim_id"],
            "locator": "p. 10",
        }
        for row in rows
    ]
    complete_reasoner = {
        "cluster_syntheses": {
            cluster_id: {
                "cluster_id": cluster_id,
                "coherence_rationale": "Both studies address one bounded relationship.",
                "boundaries": ["The evidence is limited to the two mapped settings."],
                "central_findings": [
                    {
                        "finding": (
                            "The two independent studies report a compatible association for the same bounded "
                            "institutional-trust outcome. In plain English, the repeated direction makes the pattern "
                            "more credible within this collection, while the study designs still do not establish "
                            "causation. Because only two settings are represented, the finding should not be treated "
                            "as universal or as a mature conclusion beyond the frozen source set."
                        ),
                        "proposition_ids": [proposition_id],
                        "evidence": evidence,
                    }
                ],
                "supporting_evidence": evidence,
            }
        }
    }
    source_set = {"source_set_id": "set-quality-ratchet", "dependency_hash": "same"}
    first_map, _, _, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="quality-ratchet-good",
        reasoner=complete_reasoner,
    )
    synthesis = first_map["cluster_syntheses"][cluster_id]
    assert synthesis["status"] == "reasoned"
    path = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster_note_stem(cluster)}.md"
    )
    published = path.read_text()

    partial_reasoner = {
        "cluster_syntheses": {
            cluster_id: {
                "cluster_id": cluster_id,
                "central_findings": [
                    {"finding": "A thin replacement.", "evidence": evidence}
                ],
            }
        }
    }
    second_map, _, packet, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="quality-ratchet-partial",
        reasoner=partial_reasoner,
    )

    refreshed_synthesis = second_map["cluster_syntheses"][cluster_id]
    assert refreshed_synthesis["status"] == "reasoned"
    assert refreshed_synthesis["refresh_pending"] is True
    assert (
        refreshed_synthesis["central_findings"]
        == synthesis["central_findings"]
    )
    assert packet["status"] == "partial"
    pending = path.read_text()
    assert next(
        row
        for row in second_map["clusters"]
        if row["cluster_id"] == cluster_id
    )["refresh_pending"] is True
    assert pending != published
    assert "Cluster refresh pending" in pending
    assert "The two independent studies report a compatible association" in pending
    build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="quality-ratchet-partial-replay",
        reasoner=partial_reasoner,
    )
    assert path.read_text() == pending
    assert published.split("---", 2)[-1].strip() in pending
    recovered_map, _, _, _ = build_literature_map(
        tmp_path,
        source_set=source_set,
        notes=[],
        profiles=rows,
        question=None,
        run_id="quality-ratchet-recovered",
        reasoner=complete_reasoner,
    )
    assert recovered_map["clusters"][0]["refresh_pending"] is False
    assert "Cluster refresh pending" not in path.read_text()


def test_cluster_projection_explains_machine_assessments_and_failed_gap_gates() -> None:
    cluster = {
        "cluster_id": "cluster-readable",
        "label": "Readable Mediation Findings",
        "shared_question": "What does the collection show about mediation timing and success?",
        "core_source_ids": ["source-a", "source-b"],
        "source_ids": ["source-a", "source-b"],
        "source_roles": [
            {"source_id": "source-a", "role": "core"},
            {"source_id": "source-b", "role": "core"},
        ],
        "representative_sources": [],
    }
    synthesis = {
        "status": "reasoned",
        "quality_status": "complete",
        "evidence_threads": [
            {
                "thread_id": "thread-1",
                "title": "Mediation timing and success",
                "summary": "Two publications report the same bounded association between mediation timing and success.",
                "plain_english_meaning": "The pattern repeats, but causation is not established.",
                "evidence": [
                    {"source_id": "source-a", "locator": "p. 10"},
                    {"source_id": "source-b", "locator": "p. 14"},
                ],
            }
        ],
        "source_contributions": [],
    }
    debate = {
        "classification": "within_program_consistency",
        "proposition_assessments": [
            {
                "statement": "Mediation timing is associated with success.",
                "state": "within_program_consistency",
                "explanation": {"publication_count": 3},
            },
            {
                "statement": "Mediator strategy is associated with success.",
                "state": "emerging_convergence",
                "explanation": {
                    "effective_evidence_base_count": 2,
                    "shared_terms": ["1945", "1995", "mediation"],
                },
            },
        ],
    }
    rejected_gap = {
        "gap_statement": "Whether conflict type changes the intensity-success relationship.",
        "quality_rejection_reasons": [
            "missing_resolution_path_comparison",
            "missing_resolution_path_estimand",
            "missing_resolution_path_identification",
        ],
    }

    text = literature._cluster_markdown(
        cluster,
        None,
        debate,
        rejected_gap_candidates=[rejected_gap],
        synthesis=synthesis,
    )

    assert "3 publications point in the same direction" in text
    assert "2 independent evidence bases point in the same direction" in text
    assert "1945 1995" not in text
    assert "comparison group, estimand, identification strategy" in text
    assert "This gap is retained" not in text
    assert "**In plain English:**" in text


def test_failed_synthesis_call_leaves_a_resumable_diagnostic_record(
    tmp_path: Path,
) -> None:
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
            source_set={
                "source_set_id": "set-failure-record",
                "dependency_hash": "dependency",
            },
            notes=[],
            profiles=rows,
            question=None,
            run_id="synthesis-failure-record",
            request=request,
            reasoner=Reasoner(),
        )

    failure_paths = sorted(
        (
            tmp_path
            / "11_state/runs/synthesis-failure-record/literature/synthesis/cluster_proposal"
        ).glob("*.yml")
    )
    assert len(failure_paths) == 1
    failure = yaml.safe_load(failure_paths[0].read_text())
    assert failure["status"] == "failed"
    assert failure["error"] == {
        "type": "RuntimeError",
        "message": "provider returned malformed cluster JSON",
    }
    assert "response" not in failure


def test_failed_concurrent_call_does_not_capture_shared_reasoner_response(
    tmp_path: Path,
) -> None:
    from auto_zettelkasten.literature import _CheckpointedReasonerCalls

    rows = [profile("a"), profile("b", method="case study")]
    raw_response = {
        "clusters": [
            {
                "proposal_id": "proposal-1",
                "label": "Mediator legitimacy",
                "source_ids": ["source-a", "source-b"],
                "source_roles": ["core", "core"],
                "propositions": [
                    {
                        "statement": "Mediator legitimacy shapes agreement durability.",
                        "participating_core_sources": ["source-a", "source-b"],
                        "source_id": "source-a",
                        "evidence_anchor_id": "anchor-a",
                        "locator": "p. 10",
                    }
                ],
            }
        ]
    }

    class FailedAfterTransportReasoner:
        name = "recoverable-reasoner"
        model = "recoverable-v1"
        last_literature_response = raw_response

        def propose_clusters(self, profiles, request, *, context=None):
            raise ValueError("local response adapter rejected provider aliases")

    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-raw-recovery",
        run_id="raw-recovery",
        provider="ollama",
        model="recoverable-v1",
    )
    first = _CheckpointedReasonerCalls(
        tmp_path,
        "raw-recovery",
        FailedAfterTransportReasoner(),
        request,
    )
    with pytest.raises(ValueError, match="adapter rejected"):
        first("cluster_proposal", "collection", "propose_clusters", rows, {})

    checkpoint_path = (
        tmp_path
        / "11_state/runs/raw-recovery/literature/synthesis/cluster_proposal/collection.yml"
    )
    failure = yaml.safe_load(checkpoint_path.read_text())
    assert failure["status"] == "failed"
    assert "raw_response" not in failure


def test_successful_paid_response_is_revalidated_after_local_algorithm_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_zettelkasten import literature
    from auto_zettelkasten.literature import _CheckpointedReasonerCalls

    rows = [profile("a"), profile("b", method="case study")]

    class Reasoner:
        name = "local-revalidation-reasoner"
        model = "local-revalidation-v1"
        is_cloud = False

        def __init__(self) -> None:
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            return {"clusters": []}

    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="set-local-revalidation",
        run_id="local-revalidation",
        provider="ollama",
        model="local-revalidation-v1",
    )
    first_reasoner = Reasoner()
    first = _CheckpointedReasonerCalls(
        tmp_path, "local-revalidation", first_reasoner, request
    )
    assert first("cluster_proposal", "collection", "propose_clusters", rows, {}) == {
        "clusters": []
    }
    assert first_reasoner.calls == 1

    monkeypatch.setattr(literature, "PROPOSITION_ALGORITHM_VERSION", "local-repair")
    replay_reasoner = Reasoner()
    replay = _CheckpointedReasonerCalls(
        tmp_path, "local-revalidation", replay_reasoner, request
    )
    assert replay("cluster_proposal", "collection", "propose_clusters", rows, {}) == {
        "clusters": []
    }
    assert replay_reasoner.calls == 0
    assert replay.provider_calls == 0
    assert replay.checkpoint_hits == 1


def test_synthesis_checkpoint_history_preserves_paid_successes_across_policy_changes(
    tmp_path: Path,
) -> None:
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

    def runner(reasoner, *, threshold: int):
        request = LiteratureMapRequest(
            workspace=tmp_path,
            source_set_id="set-history",
            run_id="history-run",
            provider="ollama",
            model="history-v1",
            literature_policy=LiteratureMappingPolicy(
                max_synthesis_calls=3,
                source_backed_threshold=threshold,
            ),
        )
        return _CheckpointedReasonerCalls(tmp_path, "history-run", reasoner, request)

    first = Reasoner({"clusters": [{"label": "first"}]})
    assert (
        runner(first, threshold=2)(
            "cluster_proposal", "collection", "propose_clusters", rows, {}
        )["clusters"][0]["label"]
        == "first"
    )
    assert first.calls == 1

    failed = Reasoner(error=RuntimeError("temporary provider failure"))
    with pytest.raises(RuntimeError, match="temporary provider failure"):
        runner(failed, threshold=3)(
            "cluster_proposal", "collection", "propose_clusters", rows, {}
        )
    canonical = yaml.safe_load(
        (
            tmp_path
            / "11_state/runs/history-run/literature/synthesis/cluster_proposal/collection.yml"
        ).read_text()
    )
    assert canonical["status"] == "completed"
    assert canonical["response"]["clusters"][0]["label"] == "first"
    assert (
        tmp_path
        / "11_state/runs/history-run/literature/synthesis/failures/cluster_proposal/collection.yml"
    ).is_file()

    second = Reasoner({"clusters": [{"label": "second"}]})
    assert (
        runner(second, threshold=4)(
            "cluster_proposal", "collection", "propose_clusters", rows, {}
        )["clusters"][0]["label"]
        == "second"
    )
    assert second.calls == 1

    replay_first = Reasoner(error=AssertionError("historical paid call was repeated"))
    restored = runner(replay_first, threshold=2)(
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

    assert _checkpoint_dependency_context(first) != _checkpoint_dependency_context(
        reordered
    )
    assert _checkpoint_dependency_context(
        first, sort_sequences=True
    ) == _checkpoint_dependency_context(
        reordered,
        sort_sequences=True,
    )


def test_gap_adjudication_rejections_are_audit_only_and_retained_rationales_win() -> (
    None
):
    from auto_zettelkasten.literature import (
        _apply_gap_adjudication,
        normalize_evidence_profiles,
    )

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        with_proposition_lineage(candidate, rows)
        for candidate in [
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
    ]
    response = {
        "gaps": [
            {
                **_quality_rationale(candidates[0]),
                "title": "Does the result generalize beyond the observed cases?",
            }
        ],
        "rejected": [
            {
                "gap_id": "gap-retained",
                "status": "rejected",
                "reason": "Duplicate of another retained gap.",
            },
            {
                "gap_id": "gap-vague",
                "status": "rejected",
                "reason": "Vague and missing a bounded relationship.",
            },
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
    ],
)
def test_obvious_gaps_are_audit_only(mutation, expected_reason: str) -> None:
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
        profile(
            "a", topic="women inclusion and ceasefire durability", gap_signals=[signal]
        ),
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
        if "gap_adjudication_did_not_retain_candidate"
        in (row.get("quality_rejection_reasons") or [])
    )
    rationale = _quality_rationale(candidate)
    mutation(rationale)
    report = build_literature_report(rows, reasoner={"gap_rationales": [rationale]})

    assert report["gap_registry"]["gaps"] == []
    rejected = next(
        row
        for row in report["gap_registry"]["rejected_candidates"]
        if row["gap_id"] == candidate["gap_id"]
    )
    assert rejected["status"] == "underspecified_gap"
    assert expected_reason in rejected["quality_rejection_reasons"]


def test_non_obvious_gap_with_incomplete_resolution_path_remains_visible_as_lead() -> (
    None
):
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
        profile(
            "a", topic="women inclusion and ceasefire durability", gap_signals=[signal]
        ),
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
        if "gap_adjudication_did_not_retain_candidate"
        in (row.get("quality_rejection_reasons") or [])
    )
    rationale = _quality_rationale(candidate)
    rationale["value_assessment"]["puzzle_type"] = "quantitative"
    rationale["resolution_path"]["requirements"]["comparison"] = ""

    report = build_literature_report(rows, reasoner={"gap_rationales": [rationale]})

    lead = next(
        row
        for row in report["gap_registry"]["gaps"]
        if row["gap_id"] == candidate["gap_id"]
    )
    assert lead["status"] == "collection_gap_lead"
    assert lead["promoted"] is False
    assert lead["quality_gate_passed"] is False
    assert "missing_resolution_path_comparison" in lead["quality_warnings"]
    value_check = next(
        check
        for check in lead["strict_adjudication"]["checks"]
        if "non-obvious and consequential" in check["requirement"]
    )
    assert value_check["passed"] is True
    resolution_check = next(
        check
        for check in lead["strict_adjudication"]["checks"]
        if "type-sensitive path" in check["requirement"]
    )
    assert resolution_check["passed"] is False


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
            if row.get("rule") == "replication"
            and "gap_adjudication_did_not_retain_candidate"
            in (row.get("quality_rejection_reasons") or [])
        ),
        key=lambda row: row["gap_id"],
    )
    assert len(candidates) == 2
    rationale = _quality_rationale(candidates[0])
    rationale["merged_from_gap_ids"] = [candidates[1]["gap_id"]]
    report = build_literature_report(rows, reasoner={"gap_rationales": [rationale]})

    assert [row["gap_id"] for row in report["gap_registry"]["gaps"]] == [
        candidates[0]["gap_id"]
    ]
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
    assert merged["status"] == "underspecified_gap"
    assert merged["adjudication_reason"].startswith(
        f"Merged into {candidates[0]['gap_id']}"
    )


def test_rejected_gap_does_not_reserve_structured_signature() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidates = [
        with_proposition_lineage(candidate, rows)
        for candidate in [
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
    low_quality_rejection = next(
        row for row in rejected if row["gap_id"] == "gap-low-quality"
    )
    assert low_quality_rejection["status"] == "underspecified_gap"
    assert (
        "insufficient_information_gain"
        in low_quality_rejection["quality_rejection_reasons"]
    )


def test_gap_merge_rejects_structurally_unrelated_candidate() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles(
        [
            profile("a"),
            profile("b", method="case study"),
            profile("c", topic="ocean salinity"),
            profile("d", topic="ocean salinity", method="field experiment"),
        ]
    )
    evidence_by_source = {
        row["source_id"]: {
            "source_id": row["source_id"],
            "claim_id": row["claims"][0]["claim_id"],
            "locator": row["claims"][0]["locator"],
        }
        for row in rows
    }
    candidates = [
        with_proposition_lineage(
            {
                "gap_id": "gap-trust",
                "rule": "replication",
                "topic": "institutional trust",
                "precise_missing_evidence": "Replicate institutional trust estimates.",
                "supporting_evidence": [evidence_by_source["a"]],
                "rule_results": [{"analytical_profile_count_searched": 4}],
            },
            rows,
            "institutional trust",
        ),
        with_proposition_lineage(
            {
                "gap_id": "gap-salinity",
                "rule": "measurement_or_data",
                "topic": "ocean salinity",
                "precise_missing_evidence": "Validate deep-water salinity sensors.",
                "supporting_evidence": [evidence_by_source["c"]],
                "rule_results": [{"analytical_profile_count_searched": 4}],
            },
            rows,
            "ocean salinity",
        ),
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
    assert unrelated["status"] == "underspecified_gap"
    assert (
        "gap_adjudication_did_not_retain_candidate"
        in unrelated["quality_rejection_reasons"]
    )


def test_gap_reframing_can_change_rule_when_topic_and_claim_evidence_match() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
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

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
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
    assert (
        "reframing_not_evidence_constrained"
        in trust_rejection["quality_rejection_reasons"]
    )


def test_completed_checkpoint_normalizes_resolution_path_scalar_and_list_fields() -> (
    None
):
    from auto_zettelkasten.literature import _apply_gap_adjudication
    from auto_zettelkasten.readers import _validate_literature_response

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
    evidence = [
        {
            "source_id": rows[0]["source_id"],
            "claim_id": rows[0]["claims"][0]["claim_id"],
            "locator": rows[0]["claims"][0]["locator"],
        }
    ]
    candidate = with_proposition_lineage(
        {
            "gap_id": "gap-checkpoint-shape",
            "rule": "boundary_condition",
            "topic": "institutional trust",
            "precise_missing_evidence": "Compare institutional trust across regional settings.",
            "supporting_evidence": evidence,
            "rule_results": [{"analytical_profile_count_searched": 2}],
        },
        rows,
    )
    rationale = _quality_rationale(candidate)
    question = rationale["resolution_path"]["question"]
    rationale["resolution_path"]["question"] = [question]
    rationale["resolution_path"]["limitations"] = "Residual confounding"
    response = _validate_literature_response(
        {"gaps": [rationale], "rejected": []},
        kind="gap_adjudication",
    )

    visible, _ = _apply_gap_adjudication(
        [candidate],
        response,
        rows,
    )

    assert visible[0]["resolution_path"]["question"] == question
    assert visible[0]["resolution_path"]["limitations"] == ["Residual confounding"]


def test_multiple_valid_inline_anchors_in_one_cluster_are_preserved() -> None:
    from auto_zettelkasten.literature import _apply_gap_adjudication

    rows = normalize_evidence_profiles(
        [profile("a"), profile("b", method="case study")]
    )
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
        profile(
            "a", topic="women inclusion and ceasefire durability", gap_signals=[signal]
        ),
        profile(
            "b",
            topic="women inclusion and ceasefire durability",
            method="case study",
            gap_signals=[signal],
        ),
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
    assert any(
        row["status"] == "underspecified_gap" for row in registry["rejected_candidates"]
    )


def test_public_run_entry_returns_report_and_map_id_ignores_run_timestamp(
    tmp_path: Path,
) -> None:
    rows = [profile("a"), profile("b", method="case study")]
    source_set = {"source_set_id": "set-semantic", "dependency_hash": "same-dependency"}
    first = run_literature_map(
        LiteratureMapRequest(
            workspace=tmp_path, source_set_id="set-semantic", run_id="run-20260715"
        ),
        profiles=rows,
        source_set=source_set,
    )
    second = run_literature_map(
        LiteratureMapRequest(
            workspace=tmp_path, source_set_id="set-semantic", run_id="run-20990101"
        ),
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


def test_cluster_lifecycle_history_is_isolated_per_canonical_map(
    tmp_path: Path,
) -> None:
    request_a = LiteratureMapRequest(
        workspace=tmp_path, source_set_id="set-a", run_id="a-one"
    )
    request_b = LiteratureMapRequest(
        workspace=tmp_path, source_set_id="set-b", run_id="b-one"
    )
    source_set_a = {"source_set_id": "set-a", "dependency_hash": "dependency-a"}
    source_set_b = {"source_set_id": "set-b", "dependency_hash": "dependency-b"}
    rows_a = [
        profile("a1", topic="institutional trust"),
        profile("a2", topic="institutional trust"),
    ]
    rows_b = [
        profile("b1", topic="ocean salinity"),
        profile("b2", topic="ocean salinity"),
    ]
    first_a = run_literature_map(request_a, profiles=rows_a, source_set=source_set_a)
    mapped_b = run_literature_map(request_b, profiles=rows_b, source_set=source_set_b)
    replay_a = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id="set-a", run_id="a-two"),
        profiles=list(reversed(rows_a)),
        source_set=source_set_a,
    )
    assert first_a.map_id == replay_a.map_id != mapped_b.map_id
    registry_a = yaml.safe_load(
        (tmp_path / replay_a.artifact_paths["cluster_registry"]).read_text()
    )
    registry_b = yaml.safe_load(
        (tmp_path / mapped_b.artifact_paths["cluster_registry"]).read_text()
    )
    b_ids = {row["cluster_id"] for row in registry_b["clusters"]}
    assert not any(cluster_id in repr(registry_a["ledger"]) for cluster_id in b_ids)


def test_stage_callback_fires_before_each_v06_systematic_stage() -> None:
    stages: list[str] = []
    build_literature_report([profile("a"), profile("b")], stage_callback=stages.append)
    assert stages == [
        "evidence_anchors",
        "relation_mapping",
        "topic_neighborhoods",
        "proposition_mapping",
        "clustering",
        "evidence_matrices",
        "support_validation",
        "cluster_synthesis",
        "debate_mapping",
        "gap_detection",
        "internal_falsification",
        "resolution_paths",
        "projection",
    ]


def test_question_is_only_a_projection_lens_and_never_changes_map_identity() -> None:
    source_set = {"source_set_id": "set-semantic", "dependency_hash": "same-dependency"}
    assert stable_literature_map_id(source_set, None) == stable_literature_map_id(
        source_set, "What changes trust?"
    )
    assert stable_literature_map_id(
        source_set, "What changes trust?"
    ) == stable_literature_map_id(
        source_set,
        "Where does participation matter?",
    )


def test_v4_recognizable_subliteratures_cluster_or_receive_specific_adjudication() -> (
    None
):
    """V-4 characterization: every promoted topic_neighborhood of an
    analytical facet type (concept, outcome, subject, case, tag) either
    maps to at least one admitted thematic cluster via
    cluster.topic_neighborhood_ids, or has a persisted, human-readable
    rejection reason explaining why it remains retrieval-only.

    Neighborhoods of inherently retrieval-only facet types (method, period,
    citation_or_relation) are excluded because they exist purely as
    navigation aids and are never analytical candidates."""

    analytical_facet_types = {"concept", "outcome", "subject", "case", "tag"}

    rows = [
        profile("alpha", topic="institutional trust"),
        profile("beta", topic="institutional trust", method="case study"),
        profile("gamma", topic="civic engagement"),
        profile("delta", topic="civic engagement", method="experiment"),
    ]

    report = build_literature_report(rows)

    promoted_neighborhoods = [
        neighborhood
        for neighborhood in report["navigation"].get("topic_neighborhoods", []) or []
        if int(neighborhood.get("source_count", 0) or 0) >= 2
        and len(neighborhood.get("source_ids", []) or []) >= 2
        and str(neighborhood.get("facet_type") or "") in analytical_facet_types
    ]
    assert promoted_neighborhoods, (
        "fixture must produce at least one promoted analytical-facet "
        "neighborhood with >=2 analytical sources for the V-4 "
        "characterization to be meaningful"
    )

    admitted_clusters = [
        cluster for cluster in report["cluster_registry"].get("clusters", []) or []
    ]
    cluster_neighborhood_coverage: dict[str, set[str]] = {}
    for cluster in admitted_clusters:
        for neighborhood_id in cluster.get("topic_neighborhood_ids", []) or []:
            cluster_neighborhood_coverage.setdefault(
                str(neighborhood_id), set()
            ).add(str(cluster["cluster_id"]))

    unclustered_rows = list(
        report["cluster_registry"].get("unclustered_sources", []) or []
    )
    assert all(
        row.get("reason") != "no_connected_debate_family_proposal"
        and str(row.get("reason_detail") or "").strip()
        for row in unclustered_rows
    )
    unclustered_reasons: dict[str, str] = {
        str(row.get("source_id") or ""): str(row.get("reason") or "")
        for row in unclustered_rows
    }
    rejected_proposals = report["cluster_registry"].get("rejected_proposals", []) or []
    rejected_neighborhood_notes: dict[str, str] = {}
    for rejected in rejected_proposals:
        semantic_identity = str(rejected.get("semantic_identity") or "")
        reason = str(rejected.get("reason") or "")
        if semantic_identity and reason:
            rejected_neighborhood_notes.setdefault(semantic_identity, reason)

    gaps: list[str] = []
    for neighborhood in promoted_neighborhoods:
        neighborhood_id = str(neighborhood.get("topic_neighborhood_id") or "")
        semantic_identity = str(neighborhood.get("semantic_identity") or "")
        source_ids = [str(s) for s in neighborhood.get("source_ids", []) or []]
        facet_type = str(neighborhood.get("facet_type") or "")

        if neighborhood_id in cluster_neighborhood_coverage:
            continue

        rejection_reasons = [
            unclustered_reasons.get(source_id, "") for source_id in source_ids
        ]
        rejection_reasons = [reason for reason in rejection_reasons if reason]
        rejected_identity_note = rejected_neighborhood_notes.get(semantic_identity, "")

        if rejection_reasons or rejected_identity_note:
            continue

        gaps.append(
            f"neighborhood {neighborhood_id!r} "
            f"(facet_type={facet_type!r}, "
            f"semantic_identity={semantic_identity!r}, "
            f"source_ids={source_ids}) maps to no admitted cluster and has "
            f"no persisted rejection reason"
        )

    assert not gaps, (
        "Promoted analytical-facet neighborhoods with no admitted cluster and "
        "no rejection reason:\n- "
        + "\n- ".join(gaps)
    )


def test_literature_map_counts_only_analytical_unclustered_sources() -> None:
    report = {
        "manifest": {
            "coverage_inventory_count": 2,
            "analytical_profile_count": 1,
            "limited_profile_count": 1,
            "coverage_exhausted_count": 0,
            "coverage_partial_count": 0,
            "coverage_pending_count": 0,
        },
        "profiles": [
            {
                "source_id": "analytical",
                "note_id": "note-analytical",
                "title": "Analytical source",
                "note_path": "01_source_notes/Analytical source.md",
                "analytical": True,
                "limited": False,
            },
            {
                "source_id": "limited",
                "note_id": "note-limited",
                "title": "Limited source",
                "note_path": "01_source_notes/Limited source.md",
                "analytical": False,
                "limited": True,
            },
        ],
        "cluster_registry": {
            "clusters": [],
            "unclustered_sources": [
                {
                    "source_id": "analytical",
                    "reason": "no_valid_connected_family_relation",
                    "reason_detail": "no_valid_connected_family_relation",
                },
                {
                    "source_id": "limited",
                    "reason": "limited_source_coverage",
                    "reason_detail": "Limited source coverage.",
                },
            ],
        },
        "cluster_syntheses": {},
        "gap_registry": {"gaps": [], "rejected_candidates": []},
    }

    markdown = literature._literature_map_markdown(
        report,
        {"source_set_id": "set-map-count", "collection_name": "Map count"},
        map_id="map-count",
    )

    assert "Analytical sources outside clusters: 1" in markdown
    assert "These sources have no active cluster membership in this map revision" in markdown
    assert "[[Analytical source]]" in markdown
    assert "no_valid_connected_family_relation" not in markdown
    assert "[[01_source_notes/Limited source" not in markdown
