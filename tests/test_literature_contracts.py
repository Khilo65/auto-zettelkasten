from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.models import (
    ArtifactManifest,
    ClusterProposal,
    ClusterSynthesis,
    DebateFamily,
    EvidenceAnchor,
    EvidenceFinding,
    EvidenceProfile,
    GapRationale,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    LiteratureProposition,
    MapRequest,
    NavigationPolicy,
    RunReport,
    ResolutionPath,
    SynthesisAssertion,
    StatusReport,
    SupportEnvelope,
    SubjectTag,
    SubjectTagAssignment,
    TopicNeighborhood,
    TypedSourceRelation,
)


def test_literature_mapping_policy_defaults_and_validation() -> None:
    policy = LiteratureMappingPolicy()
    assert policy.to_dict() == {
        "synthesis_enabled": True,
        "require_question": False,
        "auto_promote_clusters": True,
        "auto_promote_debates": True,
        "auto_promote_gaps": True,
        "source_backed_threshold": 3,
        "max_memberships": 3,
        "external_discovery": "disabled",
        "max_profile_calls": 100,
        "max_synthesis_calls": 24,
        "profile_workers": 4,
        "literature_deadline_seconds": 1800.0,
        "deepseek_packet_context_fraction": 0.8,
        "weak_gap_handling": "audit_only",
        "cluster_gap_projection": "inline",
        "require_executable_gap_design": True,
    }
    assert LiteratureMappingPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(ValueError, match="external_discovery"):
        LiteratureMappingPolicy(external_discovery="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source_backed_threshold"):
        LiteratureMappingPolicy.from_dict({"source_backed_threshold": "3"})
    with pytest.raises(ValueError, match="synthesis_enabled"):
        LiteratureMappingPolicy.from_dict({"synthesis_enabled": 1})
    with pytest.raises(ValueError, match="unknown literature_mapping fields"):
        LiteratureMappingPolicy.from_dict({"model_enthusiasm": 1})
    with pytest.raises(ValueError, match="weak_gap_handling"):
        LiteratureMappingPolicy.from_dict({"weak_gap_handling": "markdown"})
    with pytest.raises(ValueError, match="cluster_gap_projection"):
        LiteratureMappingPolicy.from_dict({"cluster_gap_projection": "standalone_section"})
    with pytest.raises(ValueError, match="require_executable_gap_design"):
        LiteratureMappingPolicy.from_dict({"require_executable_gap_design": "true"})


def test_navigation_policy_defaults_round_trip_and_validate_strictly() -> None:
    policy = NavigationPolicy()
    assert policy.to_dict() == {
        "subject_tags_enabled": True,
        "max_candidate_tags_per_source": 24,
        "max_visible_tags_per_source": 6,
        "max_visible_tags_per_cluster_or_gap": 6,
        "min_sources_per_neighborhood": 2,
        "max_visible_neighborhoods": 8,
        "max_collection_neighborhoods": 20,
        "max_inferred_related_note_links": 8,
        "external_ontology": "disabled",
        "automatic_semantic_synonym_merging": False,
    }
    assert NavigationPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(ValueError, match="subject_tags_enabled"):
        NavigationPolicy.from_dict({"subject_tags_enabled": "true"})
    with pytest.raises(ValueError, match="max_candidate_tags_per_source"):
        NavigationPolicy.from_dict({"max_candidate_tags_per_source": True})
    with pytest.raises(ValueError, match="external_ontology"):
        NavigationPolicy.from_dict({"external_ontology": "always"})
    with pytest.raises(ValueError, match="unknown navigation fields"):
        NavigationPolicy.from_dict({"ontology_model": "external"})
    with pytest.raises(ValueError, match="navigation must be a mapping"):
        NavigationPolicy.from_dict([])  # type: ignore[arg-type]


def test_subject_tag_assignment_and_typed_relation_contracts_round_trip() -> None:
    tag = SubjectTag.from_dict(
        {
            "subject_tag_id": "subject-tag-ceasefire-design",
            "label": "Ceasefire design",
            "slug": "concept/ceasefire-design",
            "facet_type": "concept",
            "original_variants": ["ceasefire design", "Ceasefire Design"],
            "source_ids": ["source-a", "source-b"],
            "study_family_ids": ["family-a", "family-b"],
            "assignment_provenance": ["profile.concepts", "zotero.manual"],
            "relationship_proposals": [
                {"relation": "related_to", "subject_tag_id": "subject-tag-security-arrangements"}
            ],
            "revision_hash": "sha256-tag",
        }
    )
    assignment = SubjectTagAssignment.from_dict(
        {
            "assignment_id": "assignment-a",
            "subject_tag_id": tag.subject_tag_id,
            "source_id": "source-a",
            "note_id": "note-a",
            "facet_type": "concept",
            "original_value": "ceasefire design",
            "provenance": "profile.concepts",
            "reason": "Typed profile concept",
            "confirmed_by_profile": True,
            "visible": True,
        }
    )
    relation = TypedSourceRelation.from_dict(
        {
            "relation_id": "relation-a-b",
            "source_ids": ["source-a", "source-b"],
            "note_ids": ["note-a", "note-b"],
            "relation_type": "same_proposition",
            "reasons": ["Both sources address ceasefire monitoring effectiveness."],
            "subject_tag_ids": [tag.subject_tag_id],
            "proposition_ids": ["proposition-a"],
            "evidence": [{"source_id": "source-a", "locator": "p. 10"}],
            "provenance": "proposition_matrix",
            "inferred": True,
        }
    )

    assert SubjectTag.from_dict(tag.to_dict()) == tag
    assert tag.source_count == 2
    assert tag.independent_source_count == 2
    assert SubjectTagAssignment.from_dict(assignment.to_dict()) == assignment
    assert TypedSourceRelation.from_dict(relation.to_dict()) == relation
    with pytest.raises(ValueError, match="relation_type"):
        TypedSourceRelation.from_dict({"relation_type": "citation_or_relation"})
    with pytest.raises(ValueError, match="inferred"):
        TypedSourceRelation.from_dict({"inferred": "false"})
    with pytest.raises(ValueError, match="facet_type"):
        SubjectTag.from_dict({"facet_type": "system_status"})


def test_cluster_and_gap_reasoning_types_round_trip_strictly() -> None:
    proposal = ClusterProposal.from_dict(
        {
            "proposal_id": "proposal-1",
            "label": "Mediator legitimacy",
            "semantic_identity": "mediator legitimacy",
            "source_ids": ["source-a", "source-b"],
            "supporting_evidence": [{"source_id": "source-a", "claim_id": "claim-a", "locator": "p. 10"}],
            "propositions": [{"proposition_id": "proposition-1", "statement": "Legitimacy matters."}],
            "source_roles": [{"source_id": "source-a", "role": "core"}],
        }
    )
    assert ClusterProposal.from_dict(proposal.to_dict()) == proposal
    assert ClusterProposal.from_dict(
        {
            "source_ids": ["source-a", "source-b", "source-c"],
            "source_roles": {"core": ["source-a", "source-b"], "context": ["source-c"]},
        }
    ).source_roles == [
        {"source_id": "source-c", "role": "context"},
        {"source_id": "source-a", "role": "core"},
        {"source_id": "source-b", "role": "core"},
    ]
    assert ClusterProposal.from_dict(
        {
            "source_ids": ["source-a", "source-b"],
            "source_roles": ["core", "bridge"],
        }
    ).source_roles == [
        {"source_id": "source-a", "role": "core"},
        {"source_id": "source-b", "role": "bridge"},
    ]
    synthesis = ClusterSynthesis.from_dict(
        {
            "cluster_id": "cluster-1",
            "boundaries": ["African civil wars"],
            "central_findings": [{"finding": "Legitimacy matters", "evidence": []}],
            "synthesis_assertions": [
                {
                    "assertion_id": "assertion-1",
                    "cluster_id": "cluster-1",
                    "section": "central_findings",
                    "statement": "Legitimacy matters.",
                    "proposition_ids": ["proposition-1"],
                }
            ],
            "debate_state": "mapped_consensus",
        }
    )
    assert ClusterSynthesis.from_dict(synthesis.to_dict()) == synthesis
    provider_synthesis = ClusterSynthesis.from_dict(
        {
            "cluster_id": "cluster-provider",
            "debate_state": {
                "classification": "mapped_debate",
                "agreements": [{"agreement": "Sources agree on the baseline."}],
            },
        }
    )
    assert provider_synthesis.debate_state == "mapped_debate"
    assert provider_synthesis.agreements == [{"agreement": "Sources agree on the baseline."}]
    rationale = GapRationale.from_dict(
        {
            "gap_id": "gap-1",
            "proposition_id": "proposition-1",
            "originating_cluster_ids": ["cluster-1"],
            "related_cluster_ids": ["cluster-1"],
            "gap_statement": "A bounded gap.",
            "resolution_path": {
                "path_type": "qualitative",
                "question": "When does legitimacy matter?",
                "evidence_needed": "Within-case process evidence.",
                "requirements": {"cases": ["case-a", "case-b"]},
                "feasibility": "moderate",
                "limitations": ["case access"],
            },
        }
    )
    assert GapRationale.from_dict(rationale.to_dict()) == rationale
    with pytest.raises(ValueError, match="unknown cluster proposal fields"):
        ClusterProposal.from_dict({"source_ids": [], "enthusiasm": 1})
    with pytest.raises(ValueError, match="list of strings"):
        GapRationale.from_dict({"related_cluster_ids": "cluster-1"})
    with pytest.raises(ValueError, match="value_assessment must be a mapping"):
        GapRationale.from_dict({"value_assessment": []})
    with pytest.raises(ValueError, match="non_obviousness_passed"):
        GapRationale.from_dict(
            {"value_assessment": {"non_obviousness_passed": "false"}}
        )


def test_literature_proposition_accepts_unambiguous_provider_aliases() -> None:
    proposition = LiteratureProposition.from_dict(
        {
            "statement": "Mediator legitimacy improves agreement durability.",
            "participating_core_sources": ["source-a", "source-b"],
            "source_id": "source-a",
            "evidence_anchor_id": "anchor-a",
            "locator": "pp. 10-12",
            "comparability": "Comparable outcome definitions and populations.",
        }
    )

    assert proposition.source_ids == ["source-a", "source-b"]
    assert proposition.evidence == [
        {
            "source_id": "source-a",
            "evidence_anchor_id": "anchor-a",
            "locator": "pp. 10-12",
        }
    ]
    assert proposition.comparability == {
        "summary": "Comparable outcome definitions and populations."
    }


def test_cluster_proposal_derives_roles_from_proposition_participation_when_provider_uses_source_types() -> None:
    proposal = ClusterProposal.from_dict(
        {
            "source_ids": ["source-a", "source-b", "source-c"],
            "source_roles": ["journal article", "primary empirical study", "full_document"],
            "propositions": [
                {
                    "statement": "A comparable proposition.",
                    "source_ids": ["source-a", "source-b"],
                }
            ],
        }
    )

    assert proposal.source_roles == [
        {"source_id": "source-a", "role": "core"},
        {"source_id": "source-b", "role": "core"},
        {"source_id": "source-c", "role": "context"},
    ]


def test_literature_proposition_rejects_incomplete_or_conflicting_provider_aliases() -> None:
    with pytest.raises(ValueError, match="requires non-empty"):
        LiteratureProposition.from_dict(
            {
                "source_id": "source-a",
                "evidence_anchor_id": "anchor-a",
            }
        )
    with pytest.raises(ValueError, match="conflicting literature proposition source aliases"):
        LiteratureProposition.from_dict(
            {
                "source_ids": ["source-a"],
                "participating_core_sources": ["source-b"],
            }
        )
    with pytest.raises(ValueError, match="conflicting flattened literature proposition evidence"):
        LiteratureProposition.from_dict(
            {
                "source_id": "source-a",
                "evidence_anchor_id": "anchor-a",
                "locator": "p. 11",
                "evidence": [
                    {
                        "source_id": "source-a",
                        "evidence_anchor_id": "anchor-a",
                        "locator": "p. 10",
                    }
                ],
            }
        )


def test_synthesis_assertion_accepts_a_singular_proposition_alias() -> None:
    assertion = SynthesisAssertion.from_dict(
        {
            "assertion": "Inclusive constitution-making is recommended.",
            "proposition_id": "proposition-1",
            "evidence": ["source-a anchor-a Section 7, pp. 12-13"],
        }
    )

    assert assertion.statement == "Inclusive constitution-making is recommended."
    assert assertion.proposition_ids == ["proposition-1"]
    assert assertion.evidence == [
        {
            "source_id": "source-a",
            "evidence_anchor_id": "anchor-a",
            "locator": "Section 7, pp. 12-13",
        }
    ]
    anchor_alias = SynthesisAssertion.from_dict(
        {
            "statement": "Inclusive constitution-making is recommended.",
            "evidence_anchors": [
                {"source_id": "source-a", "evidence_anchor_id": "anchor-a", "locator": "p. 12"}
            ],
        }
    )
    assert anchor_alias.evidence[0]["evidence_anchor_id"] == "anchor-a"
    with pytest.raises(ValueError, match="conflicting synthesis assertion proposition aliases"):
        SynthesisAssertion.from_dict(
            {
                "proposition_id": "proposition-1",
                "proposition_ids": ["proposition-2"],
            }
        )


def test_evidence_anchor_and_support_envelope_round_trip_strictly_with_legacy_aliases() -> None:
    envelope = SupportEnvelope.from_dict(
        {
            "empirical_role": "associational",
            "argument_role": "none",
            "coverage": "full_text",
            "scope": {"population": ["urban participants"]},
            "restrictions": ["observational design"],
            "support_status": "supported",
        }
    )
    anchor = EvidenceAnchor(
        source_id="source-1",
        study_family_id="family-1",
        evidence_role="associational",
        claim="Participation is associated with trust.",
        finding_type="statistical",
        magnitude="12%",
        plain_english_meaning="Participants reported more trust.",
        uncertainty="p < 0.05",
        locator="Table 2, p. 14",
        support_envelope=envelope,
    )
    assert SupportEnvelope.from_dict(envelope.to_dict()) == envelope
    assert EvidenceAnchor.from_dict(anchor.to_dict()) == anchor

    legacy = anchor.to_dict()
    legacy_id = legacy.pop("evidence_anchor_id")
    legacy["finding_id"] = legacy_id
    assert EvidenceAnchor.from_dict(legacy).evidence_anchor_id == legacy_id
    legacy["claim_id"] = legacy_id
    assert EvidenceAnchor.from_dict(legacy).evidence_anchor_id == legacy_id

    canonical_with_equal_alias = anchor.to_dict()
    canonical_with_equal_alias["claim_id"] = anchor.evidence_anchor_id
    assert EvidenceAnchor.from_dict(canonical_with_equal_alias) == anchor

    conflicting = anchor.to_dict()
    conflicting.pop("evidence_anchor_id")
    conflicting["finding_id"] = "finding-a"
    conflicting["claim_id"] = "claim-b"
    with pytest.raises(ValueError, match="conflicting evidence anchor aliases"):
        EvidenceAnchor.from_dict(conflicting)

    conflicting_canonical = anchor.to_dict()
    conflicting_canonical["finding_id"] = "other-id"
    with pytest.raises(ValueError, match="conflicting evidence_anchor_id"):
        EvidenceAnchor.from_dict(conflicting_canonical)
    with pytest.raises(ValueError, match="unknown support envelope fields"):
        SupportEnvelope.from_dict({"unexpected": True})
    with pytest.raises(ValueError, match="scope.population"):
        SupportEnvelope.from_dict({"scope": {"population": "urban"}})


def test_proposition_assertion_neighborhood_and_resolution_path_contracts_are_strict() -> None:
    proposition = LiteratureProposition.from_dict(
        {
            "proposition_id": "proposition-1",
            "semantic_identity": "participation-trust",
            "statement": "Participation is associated with trust.",
            "question": "What does the collection establish about participation and trust?",
            "proposition_type": "empirical",
            "signature": {"relationship": ["participation", "trust"]},
            "source_ids": ["source-1", "source-2"],
            "study_family_ids": ["family-1", "family-2"],
            "independent_study_family_count": 2,
            "cells": [{"source_id": "source-1"}],
            "evidence": [{"source_id": "source-1", "evidence_anchor_id": "anchor-1"}],
            "comparability": {"passed": True},
        }
    )
    assertion = SynthesisAssertion.from_dict(
        {
            "assertion_id": "assertion-1",
            "item_id": "assertion-1",
            "cluster_id": "cluster-1",
            "section": "central_findings",
            "statement": "The mapped sources support an association.",
            "proposition_ids": ["proposition-1"],
            "evidence": [{"source_id": "source-1", "evidence_anchor_id": "anchor-1"}],
            "support_status": "supported",
            "qualifiers": ["observational"],
        }
    )
    neighborhood = TopicNeighborhood.from_dict(
        {
            "topic_neighborhood_id": "neighborhood-semantic-1",
            "kind": "semantic",
            "semantic_identity": "participation",
            "label": "Participation",
            "source_ids": ["source-1", "source-2"],
            "note_ids": ["note-1", "note-2"],
            "signals": [{"source_id": "source-1", "kind": "semantic", "strength": "strong"}],
            "analytical_support": False,
            "source_count": 2,
        }
    )
    assert LiteratureProposition.from_dict(proposition.to_dict()) == proposition
    assert SynthesisAssertion.from_dict(assertion.to_dict()) == assertion
    assert TopicNeighborhood.from_dict(neighborhood.to_dict()) == neighborhood

    mixed_route = ResolutionPath.from_dict(
        {
            "path_type": "mixed methods",
            "question": "Which coded conditions distinguish outcomes?",
            "evidence_needed": "Comparable cases and a transparent coding scheme.",
            "requirements": {"comparison": "QCA and case tracing"},
            "feasibility": "moderate",
            "limitations": ["coding uncertainty"],
        }
    )
    assert mixed_route.path_type == "methodological"

    path_types = (
        "quantitative",
        "qualitative",
        "historical_interpretive",
        "theoretical",
        "normative",
        "methodological",
        "practitioner",
    )
    for path_type in path_types:
        path = ResolutionPath.from_dict(
            {
                "path_type": path_type,
                "question": "What evidence would resolve the gap?",
                "evidence_needed": "Type-appropriate evidence.",
                "requirements": {"route": path_type},
                "feasibility": "moderate",
                "limitations": ["collection bounded"],
            }
        )
        assert ResolutionPath.from_dict(path.to_dict()) == path

    with pytest.raises(ValueError, match="unknown literature proposition fields"):
        LiteratureProposition.from_dict({"unexpected": True})
    with pytest.raises(ValueError, match="independent_study_family_count"):
        LiteratureProposition.from_dict({"independent_study_family_count": True})
    invalid_path = {
        "path_type": "mixed_methods",
        "question": "Question?",
        "evidence_needed": "Evidence.",
        "requirements": {},
        "feasibility": "unknown",
        "limitations": [],
    }
    with pytest.raises(ValueError, match="path_type"):
        ResolutionPath.from_dict(invalid_path)
    with pytest.raises(ValueError, match="requirements must be a mapping"):
        ResolutionPath.from_dict({**invalid_path, "path_type": "quantitative", "requirements": []})


def test_map_request_round_trips_literature_policy(tmp_path: Path) -> None:
    request = MapRequest(
        tmp_path,
        literature_policy=LiteratureMappingPolicy(
            external_discovery="per_run",
            max_profile_calls=12,
        ),
    )
    assert request.to_dict()["literature_policy"]["external_discovery"] == "per_run"
    assert MapRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="literature_policy must be"):
        MapRequest.from_dict({"workspace": str(tmp_path), "literature_policy": []})
    with pytest.raises(ValueError, match="requires a question"):
        MapRequest(tmp_path, literature_policy=LiteratureMappingPolicy(require_question=True))


def test_map_request_round_trips_navigation_policy_strictly(tmp_path: Path) -> None:
    request = MapRequest(
        tmp_path,
        navigation_policy=NavigationPolicy(
            max_visible_tags_per_source=5,
            max_visible_neighborhoods=4,
        ),
    )
    assert request.to_dict()["navigation_policy"]["max_visible_tags_per_source"] == 5
    assert MapRequest.from_dict(request.to_dict()) == request
    assert MapRequest.from_dict({"workspace": str(tmp_path)}).navigation_policy == NavigationPolicy()
    with pytest.raises(ValueError, match="navigation_policy must be"):
        MapRequest.from_dict({"workspace": str(tmp_path), "navigation_policy": []})
    with pytest.raises(ValueError, match="subject_tags_enabled"):
        MapRequest(
            tmp_path,
            navigation_policy={"subject_tags_enabled": "false"},  # type: ignore[arg-type]
        )


def test_evidence_profile_and_finding_are_serializable() -> None:
    finding = EvidenceFinding(
        finding_id="finding-1",
        claim="Participation increased trust.",
        finding_type="association",
        direction="positive",
        magnitude="small",
        comparison="participants versus non-participants",
        conditions=["after adjustment"],
        plain_english_meaning="Participants reported slightly more trust.",
        is_statistical=True,
        evidence="Reported model estimate.",
        locator="p. 14",
        locators=["p. 14"],
        qualifiers=["observational"],
        confidence="moderate",
    )
    profile = EvidenceProfile(
        profile_id="profile-1",
        note_id="note-1",
        source_id="source-1",
        note_hash="note-sha256",
        source_hash="source-sha256",
        source_role="empirical_test",
        coverage={"status": "full_text"},
        validity={"status": "valid"},
        context={"question": "What changes trust?"},
        concepts=["participation", "trust"],
        theories=["contact theory"],
        mechanisms=["learning"],
        methods=["panel regression"],
        cases=["case-a"],
        datasets=["survey-a"],
        data=["panel survey"],
        geography=["region-a"],
        periods=["2018-2020"],
        populations=["participants"],
        outcomes=["institutional trust"],
        measures=["trust scale"],
        study_family_id="study-family-1",
        findings=[finding],
        limitations=["observational design"],
        boundaries=["urban participants"],
        gaps=["rural comparison"],
        future_research=["replicate in rural sites"],
        provider="deepseek",
        model="deepseek-v4-flash",
        dependency_hash="dependency-sha256",
    )
    payload = profile.to_dict()
    assert payload["profile_schema"] == "evidence_profile"
    assert payload["profile_schema_version"] == "1.3"
    assert payload["findings"] == [finding.to_dict()]
    assert payload["evidence_anchors"][0]["evidence_anchor_id"].startswith("anchor-")
    assert payload["findings"][0]["plain_english_meaning"] == "Participants reported slightly more trust."
    assert payload["source_role"] == "empirical_test"
    assert payload["data"] == ["panel survey"]
    assert payload["future_research"] == ["replicate in rural sites"]

    restored = EvidenceFinding.from_dict(
        {
            "claim": "Participation increased trust.",
            "magnitude": "small",
            "comparison": "participants versus non-participants",
            "conditions": ["after adjustment"],
            "plain_english_meaning": "Participants reported slightly more trust.",
            "is_statistical": True,
            "locator": "p. 14",
        }
    )
    assert restored.locator == "p. 14"
    assert restored.locators == ["p. 14"]
    assert restored.is_statistical is True


def test_literature_request_and_report_are_serializable(tmp_path: Path) -> None:
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="source-set-1",
        run_id="run-1",
        map_id="map-1",
    )
    assert request.question is None
    assert request.to_dict()["workspace"] == str(tmp_path)
    assert LiteratureMapRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="requires a question"):
        LiteratureMapRequest(
            workspace=tmp_path,
            literature_policy=LiteratureMappingPolicy(require_question=True),
        )

    report = LiteratureMapReport(
        status="partial",
        map_id="map-1",
        run_id="run-1",
        source_set_id="source-set-1",
        stage="synthesis",
        counts={"profile_count": 3},
        proposition_count=2,
        topic_neighborhood_count=4,
        artifact_paths={"map": Path("03_literature_synthesis/maps/map-1.yml")},
        partial_reason="synthesis_call_limit",
    )
    payload = report.to_dict()
    assert payload["counts"] == {"profile_count": 3}
    assert payload["proposition_count"] == 2
    assert payload["topic_neighborhood_count"] == 4
    assert payload["artifact_paths"]["map"] == "03_literature_synthesis/maps/map-1.yml"
    assert payload["partial_reason"] == "synthesis_call_limit"
    legacy_count_report = LiteratureMapReport(
        status="ok",
        counts={"proposition_count": 3, "topic_neighborhood_count": 5},
    )
    assert legacy_count_report.proposition_count == 3
    assert legacy_count_report.topic_neighborhood_count == 5

def test_all_report_models_default_to_current_versions(tmp_path: Path) -> None:
    run_report = RunReport(status="ok", workspace=tmp_path, run_id="run-1")
    reports = (
        ArtifactManifest(status="ok", workspace=tmp_path),
        run_report,
        StatusReport(status="ok", workspace=tmp_path),
        LiteratureMapReport(status="ok"),
    )
    assert {(report.engine_version, report.artifact_schema_version) for report in reports} == {
        ("0.21.0", "1.16")
    }
    assert run_report.literature_map == {}
    assert run_report.literature_report == {}
    assert {
        run_report.profile_count,
        run_report.proposition_count,
        run_report.topic_neighborhood_count,
        run_report.unclustered_count,
        run_report.cluster_count,
        run_report.debate_count,
        run_report.mapped_gap_count,
        run_report.gap_lead_count,
    } == {0}

def test_debate_family_and_embedded_relations_round_trip_strictly() -> None:
    relation = {
        "relation_type": "complementary_mechanism",
        "source_ids": ["source-a", "source-b"],
        "rationale": "The studies explain different stages of the same mediation process.",
        "evidence": [
            {
                "source_id": "source-a",
                "evidence_anchor_id": "anchor-a",
                "locator": "p. 10",
            },
            {
                "source_id": "source-b",
                "evidence_anchor_id": "anchor-b",
                "locator": "p. 20",
            },
        ],
        "comparability": {"direct": False, "reason": "Different outcomes."},
    }
    family = DebateFamily(
        cluster_id="cluster-mediation-design",
        label="Mediation design and implementation",
        semantic_identity="mediation design and implementation",
        shared_question="How do mediation design choices shape implementation?",
        bounded_object="mediation design and implementation",
        coherence_rationale="The sources address connected stages of one process.",
        source_ids=["source-a", "source-b"],
        core_source_ids=["source-a", "source-b"],
        source_roles=[
            {"source_id": "source-a", "role": "core"},
            {"source_id": "source-b", "role": "core"},
        ],
        family_relations=[relation],
        qualification_status="emerging_cluster",
        admission_status="admitted",
        effective_evidence_base_count=2,
        revision_hash="revision-1",
    )
    assert DebateFamily.from_dict(family.to_dict()) == family

    proposal = ClusterProposal.from_dict(
        {
            "proposal_id": "proposal-1",
            "label": family.label,
            "semantic_identity": family.semantic_identity,
            "shared_question": family.shared_question,
            "bounded_object": family.bounded_object,
            "coherence_rationale": family.coherence_rationale,
            "source_ids": family.source_ids,
            "source_roles": {"source-a": "core", "source-b": "core"},
            "family_relations": [relation],
            "propositions": [],
        }
    )
    assert ClusterProposal.from_dict(proposal.to_dict()) == proposal
    assert proposal.bounded_object == family.bounded_object
    with pytest.raises(ValueError, match="relation_type is invalid"):
        ClusterProposal.from_dict(
            {
                **proposal.to_dict(),
                "family_relations": [{**relation, "relation_type": "same_tag"}],
            }
        )
    with pytest.raises(ValueError, match="must reference family source_ids"):
        DebateFamily.from_dict(
            {
                **family.to_dict(),
                "family_relations": [{**relation, "source_ids": ["source-a", "source-c"]}],
            }
        )

def test_strict_adjudications_are_embedded_and_validated() -> None:
    adjudication = {
        "kind": "consensus",
        "candidate": "A shared proposition",
        "decision": "not_established",
        "checks": [
            {
                "requirement": "Three independent evidence bases",
                "passed": False,
                "explanation": "Only two are present.",
            }
        ],
        "explanation": "The evidence shows convergence, not mature consensus.",
        "what_would_change": "A third independent study could change the classification.",
        "proposition_ids": ["proposition-1"],
        "related_cluster_ids": ["cluster-1"],
        "evidence": [],
    }
    synthesis = ClusterSynthesis(strict_adjudications=[adjudication])
    assert ClusterSynthesis.from_dict(synthesis.to_dict()) == synthesis

    gap_adjudication = {**adjudication, "kind": "strong_gap"}
    gap = GapRationale(strict_adjudication=gap_adjudication)
    assert GapRationale.from_dict(gap.to_dict()) == gap

    with pytest.raises(ValueError, match="decision is invalid"):
        ClusterSynthesis(strict_adjudications=[{**adjudication, "decision": "maybe"}])
