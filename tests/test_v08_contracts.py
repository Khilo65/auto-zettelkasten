from __future__ import annotations

from auto_zettelkasten import (
    ARTIFACT_SCHEMA_VERSION,
    ENGINE_VERSION,
    ClusterSourceContribution,
    ClusterProposal,
    ClusterSynthesis,
    CoverageRecord,
    CoverageRegister,
    EvidenceAnchor,
    EvidenceBaseGroup,
    EvidenceThread,
    EvidenceProfile,
    IndependenceAssessment,
    NeighborhoodSummary,
    QuantitativeComparisonValidation,
    QuantitativeResult,
    SourceLocator,
    StudyLineage,
    TagConcept,
)
from auto_zettelkasten.models import LiteratureMapReport, NavigationPolicy


def test_current_versions_and_navigation_defaults() -> None:
    assert ENGINE_VERSION == "0.28.0"
    assert ARTIFACT_SCHEMA_VERSION == "1.19"
    assert NavigationPolicy().max_visible_tags_per_source == 6
    assert NavigationPolicy().max_collection_neighborhoods == 20


def test_profile_1_3_round_trips_typed_locator_quantitative_result_and_lineage() -> (
    None
):
    locator = SourceLocator(
        locator_id="locator-1",
        source_id="source-1",
        evidence_anchor_id="anchor-1",
        locator_type="page_range",
        value="pp. 14-16",
        page_start=14,
        page_end=16,
        source_native=True,
        supports_strong_assertion=True,
    )
    result = QuantitativeResult(
        quantitative_result_id="result-1",
        source_id="source-1",
        evidence_anchor_id="anchor-1",
        statistic="marginal effect",
        estimand_type="average marginal effect",
        outcome_definition="mediation success",
        estimate="+0.0997",
        unit="probability",
        scale="0-1",
        baseline="0.38",
        reference_group="non-directive strategy",
        comparison_group="directive strategy",
        denominator="all eligible mediation attempts",
        sample="N=97",
        uncertainty="p=0.068",
        population="international mediation attempts",
        period="1945-1990",
        model="logit",
        provenance="source_reported",
    )
    anchor = EvidenceAnchor(
        evidence_anchor_id="anchor-1",
        source_id="source-1",
        evidence_role="associational",
        claim="Directive strategy is associated with mediation success.",
        locator="pp. 14-16",
        locators=["pp. 14-16"],
        source_locators=[locator],
        quantitative_result=result,
    )
    lineage = StudyLineage(
        study_lineage_id="lineage-1",
        source_ids=["source-1"],
        authors=["Author A"],
        institutions=["Institute A"],
        datasets=["Mediation dataset"],
        data_sources=["published cases"],
        sampling_frame="mediation attempts",
        unit_of_analysis="mediation attempt",
        populations=["international mediation attempts"],
        periods=["1945-1990"],
        institutional_series="",
        confidence="high",
    )
    profile = EvidenceProfile(
        profile_schema_version="1.1",
        source_id="source-1",
        study_lineage=lineage,
        evidence_anchors=[anchor],
    )

    assert profile.profile_schema_version == "1.3"
    assert profile.to_dict()["profile_schema_version"] == "1.3"
    assert profile.to_dict()["study_lineage"] == lineage.to_dict()
    assert EvidenceAnchor.from_dict(anchor.to_dict()) == anchor
    assert SourceLocator.from_dict(locator.to_dict()) == locator
    assert QuantitativeResult.from_dict(result.to_dict()) == result
    assert StudyLineage.from_dict(lineage.to_dict()) == lineage


def test_invalid_optional_page_coordinates_do_not_discard_source_locator() -> None:
    locator = SourceLocator.from_dict(
        {
            "locator_type": "page_range",
            "value": "pp. 19-18",
            "page_start": 19,
            "page_end": 18,
            "source_native": True,
            "supports_strong_assertion": True,
        }
    )

    assert locator.value == "pp. 19-18"
    assert locator.page_start is None
    assert locator.page_end is None


def test_generated_headings_cannot_masquerade_as_strong_source_locators() -> None:
    try:
        SourceLocator(
            locator_type="generated_heading",
            value="Detailed Findings (1)",
            source_native=True,
            supports_strong_assertion=True,
        )
    except ValueError as exc:
        assert "generated headings" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("generated heading was admitted as source native")


def test_evidence_base_and_cluster_contribution_contracts_round_trip() -> None:
    group = EvidenceBaseGroup(
        evidence_base_group_id="evidence-base-1",
        proposition_id="proposition-1",
        source_ids=["source-1", "source-2"],
        study_lineage_ids=["lineage-1"],
        relationship="institutional_series",
        counted_as_independent=True,
        rationale="Two publications represent one institutional evidence base.",
        overlap_signals=["same institution", "same guidance series"],
    )
    assessment = IndependenceAssessment(
        assessment_id="independence-1",
        proposition_id="proposition-1",
        source_ids=["source-1", "source-2"],
        evidence_base_group_ids=["evidence-base-1"],
        status="institutional_series",
        effective_evidence_base_count=1,
        rationale="Publication count exceeds effective evidence-base count.",
        overlap_signals=["same institutional series"],
        confidence="high",
    )
    contribution = ClusterSourceContribution(
        contribution_id="contribution-1",
        source_id="source-1",
        cluster_role="core",
        contribution_kind="unique_cluster_relevant_finding",
        related_proposition_ids=["proposition-1"],
        evidence_thread_id="thread-1",
        finding="The source identifies one relevant determinant.",
        technical_result="Source-reported estimate.",
        plain_english_meaning="This finding matters but is not a collection-wide agreement.",
        relation_to_cluster_question="Directly addresses the question.",
        comparison_status="single_source",
        evidence=[{"evidence_anchor_id": "anchor-1", "locator": "p. 14"}],
    )
    synthesis = ClusterSynthesis(
        cluster_id="cluster-1",
        evidence_threads=[
            EvidenceThread(
                thread_id="thread-1",
                title="How mediator legitimacy matters",
                question="How does legitimacy affect acceptance?",
                summary="The sources connect legitimacy to acceptance through distinct mechanisms.",
                plain_english_meaning="Legitimacy may make proposals easier for parties to accept.",
                relationship="complementary",
                source_ids=["source-1", "source-2"],
                proposition_ids=["proposition-1"],
                evidence=[{"source_id": "source-1", "locator": "p. 14"}],
            )
        ],
        source_contributions=[contribution],
        evidence_base_groups=[group],
        independence_assessments=[assessment],
        effective_evidence_base_count=1,
        debate_state="single_position",
    )
    proposal = ClusterProposal(
        proposal_id="proposal-1",
        source_ids=["source-1", "source-2"],
        study_lineages=[
            StudyLineage(
                study_lineage_id="lineage-1", source_ids=["source-1", "source-2"]
            )
        ],
        evidence_base_groups=[group],
        independence_assessments=[assessment],
        effective_evidence_base_count=1,
    )

    assert EvidenceBaseGroup.from_dict(group.to_dict()) == group
    assert IndependenceAssessment.from_dict(assessment.to_dict()) == assessment
    assert ClusterSourceContribution.from_dict(contribution.to_dict()) == contribution

    without_thread = contribution.to_dict()
    without_thread["evidence_thread_id"] = None
    assert ClusterSourceContribution.from_dict(without_thread).evidence_thread_id == ""
    assert (
        EvidenceThread.from_dict(synthesis.evidence_threads[0].to_dict())
        == synthesis.evidence_threads[0]
    )
    assert ClusterProposal.from_dict(proposal.to_dict()) == proposal
    assert ClusterSynthesis.from_dict(synthesis.to_dict()) == synthesis


def test_quantitative_comparison_requires_all_checks_for_valid_status() -> None:
    comparison = QuantitativeComparisonValidation(
        comparison_id="comparison-1",
        proposition_id="proposition-1",
        source_ids=["source-1", "source-2"],
        quantitative_result_ids=["result-1", "result-2"],
        status="qualified",
        estimands_comparable=True,
        outcomes_comparable=True,
        populations_comparable=False,
        arithmetic_reproducible=True,
        reason="The studies estimate comparable relationships in different populations.",
        qualifications=["Population scope differs."],
    )
    assert (
        QuantitativeComparisonValidation.from_dict(comparison.to_dict()) == comparison
    )

    try:
        QuantitativeComparisonValidation(status="valid", arithmetic_reproducible=True)
    except ValueError as exc:
        assert "all checks" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("invalid quantitative comparison was accepted")


def test_coverage_tag_and_neighborhood_contracts_round_trip() -> None:
    records = [
        CoverageRecord(
            source_id="source-1",
            title="Included source",
            zotero_key="AAAA1111",
            terminal_state="validated_note",
        ),
        CoverageRecord(
            source_id="source-2",
            title="Parked source",
            zotero_key="BBBB2222",
            terminal_state="parked_for_review",
            exclusion_reason="No adequate representation was available.",
            attempted_route=["indexed text", "attachment extraction"],
            could_affect_existing_cluster=True,
        ),
    ]
    coverage = CoverageRegister(
        source_set_id="source-set-1",
        inventory_count=2,
        counts={
            "validated_note": 1,
            "limited_note": 0,
            "parked_for_review": 1,
            "partial": 0,
            "pending": 0,
        },
        records=records,
        status="complete_with_exclusions",
    )
    tag = TagConcept(
        tag_concept_id="tag-concept-1",
        label="Mediation success",
        slug="mediation-success",
        original_variants=["mediation success", "successful mediation"],
        source_ids=["source-1", "source-2"],
        relations=[
            {"relation_type": "broader_than", "target_tag_concept_id": "tag-concept-2"}
        ],
        graph_active=True,
        activation_reason="Occurs in two analytical sources.",
        revision_hash="revision-1",
    )
    neighborhood = NeighborhoodSummary(
        neighborhood_id="neighborhood-1",
        label="Mediation outcomes",
        why_useful="Retrieves sources comparing definitions of mediation success.",
        source_ids=["source-1", "source-2"],
        effective_evidence_base_count=2,
        related_cluster_ids=["cluster-1"],
        representative_source_ids=["source-1"],
        relationship_reasons=["Shared outcome definition"],
    )

    assert CoverageRegister.from_dict(coverage.to_dict()) == coverage
    assert TagConcept.from_dict(tag.to_dict()) == tag
    assert NeighborhoodSummary.from_dict(neighborhood.to_dict()) == neighborhood


def test_literature_report_exposes_v08_artifact_counts() -> None:
    report = LiteratureMapReport(
        status="ok",
        counts={
            "source_contribution_count": 4,
            "evidence_base_group_count": 2,
            "coverage_record_count": 75,
        },
    )
    assert report.source_contribution_count == 4
    assert report.evidence_base_group_count == 2
    assert report.coverage_record_count == 75
    assert report.engine_version == "0.28.0"
    assert report.artifact_schema_version == "1.19"
