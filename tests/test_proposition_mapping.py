from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping

import pytest

import auto_zettelkasten.literature as literature
from auto_zettelkasten.literature import (
    DEBATE_STATES,
    _cluster_markdown,
    _gap_markdown,
    _gap_quality_errors,
    _gap_rule_admission_errors,
    _gap_specificity_errors,
    _proposition_debate_state,
    build_debate_registry,
    build_evidence_matrices,
    build_literature_propositions,
    map_overlapping_clusters,
    map_profile_relations,
    map_topic_neighborhoods,
    normalize_evidence_profiles,
    search_and_validate_gaps,
    validate_cluster_synthesis,
)
from auto_zettelkasten.models import LiteratureMappingPolicy, ResolutionPath


CANONICAL_GAP_STATUSES = {
    "underspecified_gap",
    "collection_gap_lead",
    "collection_surviving_gap",
    "answered_within_collection",
    "narrowed_by_collection",
}


def _profile(
    source_id: str,
    *,
    topic: str = "women participation",
    outcome: str = "ceasefire durability",
    claim: str | None = None,
    direction: str = "positive",
    family_id: str | None = None,
    empirical_role: str = "associational",
    argument_role: str = "none",
    source_role: str = "analytical_source",
    anchor_id: str | None = None,
    anchor_method: str = "comparative case analysis",
    anchor_data: str = "ceasefire episode records",
    anchor_case: str | None = None,
    source_dimensions: Mapping[str, list[str]] | None = None,
    semantic_topics: list[str] | None = None,
    tags: list[str] | None = None,
    gap_answers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence_role = empirical_role if empirical_role != "none" else argument_role
    locator = f"p. {sum(ord(character) for character in source_id) % 80 + 1}"
    anchor_case = anchor_case or f"negotiation-{source_id}"
    profile: dict[str, Any] = {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "title": f"Evidence from {source_id.upper()}",
        "note_path": f"01_source_notes/Source {source_id.upper()}.md",
        "note_hash": f"hash-{source_id}",
        "note_status": "analytical_atomic_note",
        "study_family_id": family_id or f"family-{source_id}",
        "study_lineage": {
            "study_family_id": family_id or f"family-{source_id}",
            "group_basis": "study_family",
        },
        "source_role": source_role,
        "semantic_topics": list(semantic_topics or [topic]),
        "normalized_tags": list(tags or []),
        "methods": ["source-level review method"],
        "data": ["source-level archive"],
        "cases": [f"source-case-{source_id}"],
        "periods": ["1990-2020"],
        "outcomes": [outcome],
        "gap_answers": list(gap_answers or []),
        "evidence_anchors": [
            {
                "evidence_anchor_id": anchor_id or f"anchor-{source_id}",
                "claim": claim or f"{topic} improves {outcome}.",
                "topic": topic,
                "outcome": outcome,
                "direction": direction,
                "finding_type": evidence_role,
                "evidence_role": evidence_role,
                "method": anchor_method,
                "data": anchor_data,
                "case": anchor_case,
                "period": "2000-2020",
                "locator": locator,
                "support_envelope": {
                    "empirical_role": empirical_role,
                    "argument_role": argument_role,
                    "coverage": "full_text",
                    "scope": {
                        "outcome": [outcome],
                        "case": [anchor_case],
                        "period": ["2000-2020"],
                    },
                    "restrictions": [],
                    "support_status": "supported",
                },
            }
        ],
    }
    profile.update(dict(source_dimensions or {}))
    return profile


def _normalized_stub(source_id: str) -> dict[str, Any]:
    return {
        "_normalized": True,
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "title": f"Source {source_id}",
        "note_path": f"01_source_notes/Source {source_id}.md",
        "note_hash": f"hash-{source_id}",
        "study_family_id": f"family-{source_id}",
        "evidence_base_group_id": f"evidence-base-{source_id}",
        "evidence_base_counted": True,
        "study_lineage": {
            "lineage_id": f"lineage-{source_id}",
            "evidence_base_group_id": f"evidence-base-{source_id}",
            "group_basis": "study_family",
            "counted_as_independent": True,
            "independence_status": "independent_evidence_base",
        },
        "source_role": "analytical_source",
        "analytical": True,
        "limited": False,
        "claims": [],
    }


def _manual_proposition(proposition_id: str, source_ids: list[str]) -> dict[str, Any]:
    return {
        "proposition_id": proposition_id,
        "semantic_identity": proposition_id,
        "statement": f"Shared relationship {proposition_id}",
        "question": f"What establishes {proposition_id}?",
        "proposition_type": "empirical",
        "source_ids": source_ids,
        "study_family_ids": [f"family-{source_id}" for source_id in source_ids],
        "independent_study_family_count": len(source_ids),
        "evidence_base_group_ids": [f"evidence-base-{source_id}" for source_id in source_ids],
        "effective_evidence_base_count": len(source_ids),
        "cells": [],
        "evidence": [],
        "comparability": {"passed": True, "basis": "test fixture"},
    }


def _reference(profile: Mapping[str, Any]) -> dict[str, Any]:
    anchor = profile["claims"][0]
    return {
        "evidence_anchor_id": anchor["evidence_anchor_id"],
        "claim_id": anchor["evidence_anchor_id"],
        "source_id": profile["source_id"],
        "study_family_id": profile["study_family_id"],
        "evidence_base_group_id": profile.get("evidence_base_group_id", ""),
        "counted_as_independent": profile.get("evidence_base_counted", False),
        "independence_status": profile.get("study_lineage", {}).get(
            "independence_status", "independence_uncertain"
        ),
        "locator": anchor["locator"],
        "support_status": anchor["support_status"],
        "empirical_role": anchor["support_envelope"]["empirical_role"],
        "argument_role": anchor["support_envelope"]["argument_role"],
    }


def _reader_visible_markdown(markdown: str) -> str:
    body = markdown.split("\n---\n", 1)[1]
    return re.sub(r"\[\[[^|]+\|([^\]]+)\]\]", r"\1", body)


def test_reference_locator_identity_ignores_space_after_page_marker() -> None:
    profile = normalize_evidence_profiles([_profile("a")])[0]
    reference = _reference(profile)
    reference["locator"] = reference["locator"].replace("p. ", "p.")

    assert literature._reference_matches_profile(reference, profile)


def test_source_level_dimensions_are_not_inherited_by_anchors_or_matrix_cells() -> None:
    source_only = {
        "methods": ["SOURCE ONLY ethnography"],
        "data": ["SOURCE ONLY famine archive"],
        "cases": ["SOURCE ONLY hostage talks"],
        "outcomes": ["SOURCE ONLY food insecurity"],
    }
    rows = [
        _profile(
            source_id,
            anchor_method="event history model",
            anchor_data="ceasefire event records",
            anchor_case=f"peace-process-{source_id}",
            source_dimensions=source_only,
        )
        for source_id in ("a", "b")
    ]
    for row in rows:
        row["evidence_anchors"].append(
            {
                "evidence_anchor_id": f"anchor-monitoring-{row['source_id']}",
                "claim": "Monitoring compliance reduces ceasefire violations.",
                "topic": "monitoring compliance",
                "outcome": "ceasefire violations",
                "direction": "negative",
                "finding_type": "associational",
                "evidence_role": "associational",
                "method": "field monitoring",
                "data": "incident reports",
                "case": f"monitoring-mission-{row['source_id']}",
                "period": "2010-2020",
                "locator": "p. 44",
                "support_envelope": {
                    "empirical_role": "associational",
                    "argument_role": "none",
                    "coverage": "full_text",
                    "scope": {
                        "outcome": ["ceasefire violations"],
                        "case": [f"monitoring-mission-{row['source_id']}"],
                        "period": ["2010-2020"],
                    },
                    "restrictions": [],
                    "support_status": "supported",
                },
            }
        )
    normalized = normalize_evidence_profiles(rows)

    for profile in normalized:
        assert profile["dimensions"]["method"] == source_only["methods"]
        dimensions_by_anchor = {
            anchor["evidence_anchor_id"]: anchor["dimensions"] for anchor in profile["claims"]
        }
        ceasefire_dimensions = dimensions_by_anchor[f"anchor-{profile['source_id']}"]
        monitoring_dimensions = dimensions_by_anchor[f"anchor-monitoring-{profile['source_id']}"]
        assert ceasefire_dimensions["method"] == ["event history model"]
        assert ceasefire_dimensions["data"] == ["ceasefire event records"]
        assert ceasefire_dimensions["case"] == [f"peace-process-{profile['source_id']}"]
        assert ceasefire_dimensions["outcome"] == ["ceasefire durability"]
        assert monitoring_dimensions["method"] == ["field monitoring"]
        assert monitoring_dimensions["data"] == ["incident reports"]
        assert monitoring_dimensions["outcome"] == ["ceasefire violations"]
        assert all(
            "SOURCE ONLY" not in value
            for dimensions in dimensions_by_anchor.values()
            for values in dimensions.values()
            for value in values
        )

    propositions = build_literature_propositions(normalized)
    mapped = map_overlapping_clusters(normalized, propositions=propositions)
    matrices = build_evidence_matrices(normalized, mapped["clusters"])

    assert len(propositions) == len(matrices) == 2
    assert all(matrix["source_level_metadata_inherited"] is False for matrix in matrices)
    assert all(set(matrix["propositions"][0]["cells"]) == {"a", "b"} for matrix in matrices)
    assert "SOURCE ONLY" not in repr(matrices)


def test_topic_neighborhoods_are_navigation_only_and_shared_tags_cannot_cluster() -> None:
    topic_only = [
        {
            **_profile(source_id, semantic_topics=["peace process inclusion"]),
            "evidence_anchors": [],
        }
        for source_id in ("a", "b", "c")
    ]
    normalized_topic_only = normalize_evidence_profiles(topic_only)
    neighborhoods = map_topic_neighborhoods(normalized_topic_only)

    assert any(set(row["source_ids"]) == {"a", "b", "c"} for row in neighborhoods)
    assert all(row["analytical_support"] is False for row in neighborhoods)
    assert build_literature_propositions(normalized_topic_only) == []
    assert map_overlapping_clusters(
        normalized_topic_only,
        propositions=[],
        topic_neighborhoods=neighborhoods,
    )["clusters"] == []

    tags_only = [
        _profile("tag-a", topic="school finance", outcome="graduation rates", tags=["shared-tag"]),
        _profile("tag-b", topic="ocean salinity", outcome="coral bleaching", tags=["shared-tag"]),
        _profile("tag-c", topic="battery chemistry", outcome="charge density", tags=["shared-tag"]),
    ]
    assert map_profile_relations(tags_only) == []
    assert map_overlapping_clusters(tags_only)["clusters"] == []


@pytest.mark.parametrize(
    ("core_count", "expected_status"),
    [(2, "emerging_cluster"), (3, "source_backed_cluster")],
)
def test_cluster_qualification_counts_independent_core_studies_on_one_proposition(
    core_count: int,
    expected_status: str,
) -> None:
    rows = normalize_evidence_profiles([_profile(chr(97 + index)) for index in range(core_count)])
    propositions = build_literature_propositions(rows)
    mapped = map_overlapping_clusters(rows, propositions=propositions)

    assert len(propositions) == 1
    assert len(mapped["clusters"]) == 1
    cluster = mapped["clusters"][0]
    assert cluster["qualification_status"] == expected_status
    assert cluster["independent_study_family_count"] == core_count
    assert cluster["proposition_ids"] == [propositions[0]["proposition_id"]]


def test_context_and_bridge_sources_do_not_count_toward_cluster_thresholds() -> None:
    rows = normalize_evidence_profiles([_profile(source_id) for source_id in ("a", "b", "c", "d")])
    proposition = build_literature_propositions(rows)[0]
    proposal = {
        "proposal_id": "proposal-role-counting",
        "label": "Ceasefire effectiveness",
        "semantic_identity": "ceasefire effectiveness",
        "source_ids": ["a", "b", "c", "d"],
        "source_roles": {"a": "core", "b": "core", "c": "context", "d": "bridge"},
        "propositions": [proposition],
    }
    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[proposition])
    cluster = mapped["clusters"][0]

    assert cluster["qualification_status"] == "emerging_cluster"
    assert cluster["independent_study_family_count"] == 2
    assert cluster["core_source_ids"] == ["a", "b"]
    assert cluster["context_source_ids"] == ["c"]
    assert cluster["bridge_source_ids"] == ["d"]

    one_core = deepcopy(proposal)
    one_core["proposal_id"] = "proposal-one-core"
    one_core["source_roles"] = {"a": "core", "b": "context", "c": "context", "d": "bridge"}
    rejected = map_overlapping_clusters(rows, proposals=[one_core], propositions=[proposition])
    assert rejected["clusters"] == []
    assert rejected["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_three_core_sources_across_two_rows_do_not_fake_a_source_backed_cluster() -> None:
    rows = [_normalized_stub(source_id) for source_id in ("a", "b", "c")]
    propositions = [
        _manual_proposition("proposition-one", ["a", "b"]),
        _manual_proposition("proposition-two", ["b", "c"]),
    ]
    proposal = {
        "proposal_id": "proposal-two-rows",
        "label": "Two related propositions",
        "semantic_identity": "two related propositions",
        "source_ids": ["a", "b", "c"],
        "source_roles": {source_id: "core" for source_id in ("a", "b", "c")},
        "propositions": propositions,
    }

    cluster = map_overlapping_clusters(
        rows,
        proposals=[proposal],
        propositions=propositions,
    )["clusters"][0]

    assert cluster["independent_study_family_count"] == 3
    assert cluster["qualifying_proposition_family_count"] == 2
    assert cluster["qualification_status"] == "emerging_cluster"
    assert cluster["source_backed"] is False


def test_invalid_proposal_anchor_cannot_fall_back_to_source_id_membership() -> None:
    rows = normalize_evidence_profiles([_profile("a"), _profile("b")])
    proposition = build_literature_propositions(rows)[0]
    proposal = {
        "proposal_id": "proposal-invalid-anchor",
        "label": "Mislabeled proposal",
        "semantic_identity": "mislabeled proposal",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "supporting_evidence": [
            {
                "source_id": "a",
                "evidence_anchor_id": rows[0]["claims"][0]["evidence_anchor_id"],
                "locator": "p. 999",
            }
        ],
    }

    mapped = map_overlapping_clusters(
        rows,
        proposals=[proposal],
        propositions=[proposition],
    )

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_provider_proposition_evidence_builds_source_local_matrix_cells() -> None:
    rows = normalize_evidence_profiles([_profile("a"), _profile("b"), _profile("c")])
    proposal = {
        "proposal_id": "proposal-provider-evidence",
        "label": "Ceasefire durability",
        "semantic_identity": "ceasefire durability",
        "source_ids": ["a", "b", "c"],
        "source_roles": {source_id: "core" for source_id in ("a", "b", "c")},
        "supporting_evidence": [
            {**_reference(rows[0]), "locator": "p. 999"},
        ],
        "propositions": [
            {
                "proposition_id": "provider-proposition",
                "statement": "Women participation improves ceasefire durability.",
                "question": "Does participation improve durability?",
                "proposition_type": "empirical",
                "source_ids": ["a", "b", "c"],
                "evidence": [_reference(row) for row in rows],
                "comparability": {"passed": True, "basis": "provider comparison"},
            }
        ],
    }

    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])
    cluster = mapped["clusters"][0]
    matrix = build_evidence_matrices(rows, [cluster])[0]

    assert cluster["qualification_status"] == "source_backed_cluster"
    assert matrix["admission_passed"] is True
    assert set(matrix["propositions"][0]["cells"]) == {"a", "b", "c"}
    assert all(
        cell["scope"]["outcome"] == ["ceasefire durability"]
        for cell in matrix["propositions"][0]["cells"].values()
    )


def test_provider_proposition_rejects_adjacent_claims_with_different_outcomes() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile(
                "prevalence",
                outcome="conflict relapse",
                claim="Resource-linked conflicts relapse more often.",
            ),
            _profile(
                "guidance",
                outcome="mediation uptake",
                claim="Mediation is underused in natural-resource disputes.",
            ),
        ]
    )
    proposal = {
        "proposal_id": "proposal-adjacent-resource-claims",
        "label": "Natural resources and mediation",
        "semantic_identity": "natural resources and mediation",
        "source_ids": ["prevalence", "guidance"],
        "source_roles": {"prevalence": "core", "guidance": "core"},
        "propositions": [
            {
                "statement": "Resource-linked conflicts relapse more often and mediation resolves them.",
                "question": "How do resources affect relapse and mediation?",
                "proposition_type": "empirical_claim",
                "source_ids": ["prevalence", "guidance"],
                "evidence": [_reference(row) for row in rows],
                "comparability": {"outcomes": "relapse and mediation uptake"},
            }
        ],
    }

    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_provider_proposition_keeps_only_sources_sharing_the_bounded_outcome() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile(
                "a",
                outcome="mediation success",
                claim="Mediator strategy is associated with mediation success.",
            ),
            _profile(
                "b",
                outcome="successful mediation",
                claim="Directive mediator strategy predicts successful mediation.",
            ),
            _profile("background", outcome="conflict prevalence"),
        ]
    )
    proposal = {
        "proposal_id": "proposal-bounded-outcome",
        "label": "Mediation success",
        "semantic_identity": "mediation success",
        "source_ids": ["a", "b", "background"],
        "source_roles": {source_id: "core" for source_id in ("a", "b", "background")},
        "propositions": [
            {
                "statement": "Mediator strategy is associated with successful mediation.",
                "question": "What predicts mediation success?",
                "proposition_type": "empirical_claim",
                "source_ids": ["a", "b", "background"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    cluster = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])["clusters"][0]

    assert cluster["core_source_ids"] == ["a", "b"]
    assert cluster["qualification_status"] == "emerging_cluster"


def test_provider_proposition_rejects_same_outcome_with_unrelated_predictor() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile(
                "strategy",
                outcome="mediation success",
                claim="Mediator strategy is associated with mediation success.",
            ),
            _profile(
                "baseline",
                outcome="mediation success",
                claim="The baseline probability of successful mediation is 38 percent.",
            ),
        ]
    )
    proposal = {
        "proposal_id": "proposal-strategy-not-baseline",
        "label": "Mediator strategy",
        "semantic_identity": "mediator strategy",
        "source_ids": ["strategy", "baseline"],
        "source_roles": {"strategy": "core", "baseline": "core"},
        "propositions": [
            {
                "statement": "Directive mediator strategy improves mediation success.",
                "question": "Does mediator strategy predict mediation success?",
                "proposition_type": "associational",
                "source_ids": ["strategy", "baseline"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_source_level_anchor_metadata_cannot_make_famine_statistics_support_engagement() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile(
                source_id,
                topic="armed groups strategy",
                outcome="peace process engagement",
                claim="War-induced famine killed 500,000 people and food deliveries totaled 100,000 tons.",
            )
            for source_id in ("a", "b")
        ]
    )
    proposal = {
        "proposal_id": "proposal-irrelevant-statistics",
        "label": "Armed-group engagement",
        "semantic_identity": "armed-group engagement",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "propositions": [
            {
                "statement": "Armed-group engagement requires tailored mediator strategies.",
                "question": "How should mediators engage armed groups?",
                "proposition_type": "practitioner",
                "source_ids": ["a", "b"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_provider_proposition_repairs_a_wrong_reference_from_existing_source_anchors() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile(
                source_id,
                outcome="mediation success",
                claim="The baseline probability of successful mediation is 38 percent.",
            )
            for source_id in ("a", "b")
        ]
    )
    for row in rows:
        direct = deepcopy(row["claims"][0])
        direct["evidence_anchor_id"] = direct["claim_id"] = f"anchor-direct-{row['source_id']}"
        direct["text"] = "Directive mediator strategy is associated with mediation success."
        direct["locator"] = "Table 2, p. 14"
        row["claims"].append(direct)
    proposal = {
        "proposal_id": "proposal-anchor-repair",
        "label": "Mediator strategy",
        "semantic_identity": "mediator strategy",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "propositions": [
            {
                "statement": "Directive mediator strategy is associated with mediation success.",
                "question": "Does mediator strategy predict mediation success?",
                "proposition_type": "associational",
                "source_ids": ["a", "b"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    cluster = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])["clusters"][0]

    proposition = cluster["propositions"][0]
    assert proposition["comparability"]["deterministic_anchor_expansion_source_ids"] == ["a", "b"]
    assert {reference["evidence_anchor_id"] for reference in proposition["evidence"]} == {
        "anchor-direct-a",
        "anchor-direct-b",
    }


def test_provider_proposition_uses_precise_finding_instead_of_same_outcome_anchor() -> None:
    raw_rows = [
        _profile(
            source_id,
            outcome="mediation success",
            claim="Longer mediation duration is negatively associated with mediation success.",
        )
        for source_id in ("a", "b")
    ]
    for row in raw_rows:
        row["findings"] = [
            {
                "finding_id": f"finding-intensity-{row['source_id']}",
                "claim": "Conflict intensity is not significantly associated with mediation success.",
                "finding_type": "association",
                "direction": "null",
                "plain_english_meaning": "Fatality levels do not independently predict success.",
                "outcome": "mediation success",
                "locator": "Table 1, p. 163",
                "locators": ["Table 1, p. 163"],
            }
        ]
    rows = normalize_evidence_profiles(raw_rows)
    proposal = {
        "proposal_id": "proposal-intensity",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "propositions": [
            {
                "statement": "Lower conflict intensity is associated with higher mediation success.",
                "question": "Does conflict intensity predict mediation success?",
                "proposition_type": "associational",
                "source_ids": ["a", "b"],
                # The provider selected true references from the right sources,
                # but they address another predictor of the same outcome.
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    cluster = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])["clusters"][0]
    proposition = cluster["propositions"][0]

    assert proposition["comparability"]["deterministic_anchor_expansion_source_ids"] == ["a", "b"]
    assert {
        reference["evidence_anchor_id"] for reference in proposition["evidence"]
    } == {"finding-intensity-a", "finding-intensity-b"}
    assert proposition["statement"] == "Conflict intensity is associated with mediation success."
    assert all(
        "conflict intensity" in cell["stance_or_finding"].casefold()
        and "mediation duration" not in cell["stance_or_finding"].casefold()
        for cell in proposition["cells"]
    )


def test_provider_proposition_does_not_equate_mediator_strategy_with_experience() -> None:
    raw_a = _profile(
        "a",
        outcome="mediation success",
        claim="Mediator experience is positively associated with mediation success.",
    )
    raw_a["findings"] = [
        {
            "finding_id": "finding-strategy-a",
            "claim": "Directive mediation strategy is associated with mediation success.",
            "finding_type": "association",
            "direction": "positive",
            "plain_english_meaning": "Active strategies correlate with success.",
            "outcome": "mediation success",
            "locator": "Table 2, p. 164",
            "locators": ["Table 2, p. 164"],
        }
    ]
    raw_b = _profile(
        "b",
        outcome="mediation success",
        claim="Mediator strategy is associated with mediation success.",
    )
    rows = normalize_evidence_profiles([raw_a, raw_b])
    proposal = {
        "proposal_id": "proposal-strategy-experience",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "propositions": [
            {
                "statement": (
                    "Directive mediator strategy and greater mediator experience are "
                    "associated with mediation success."
                ),
                "question": "What mediator characteristics predict success?",
                "proposition_type": "associational",
                "source_ids": ["a", "b"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    proposition = map_overlapping_clusters(
        rows,
        proposals=[proposal],
        propositions=[],
    )["clusters"][0]["propositions"][0]

    assert proposition["comparability"]["shared_proposition_subject"] == "strategy"
    assert {
        reference["evidence_anchor_id"] for reference in proposition["evidence"]
    } == {"finding-strategy-a", "anchor-b"}
    assert all(
        "strategy" in cell["stance_or_finding"].casefold()
        and "experience" not in cell["stance_or_finding"].casefold()
        for cell in proposition["cells"]
    )


def test_support_unknown_anchor_cannot_populate_provider_proposition_matrix() -> None:
    raw_rows = [_profile("a"), _profile("b")]
    raw_rows[1]["evidence_anchors"][0]["support_envelope"]["support_status"] = "support_unknown"
    rows = normalize_evidence_profiles(raw_rows)
    proposal = {
        "proposal_id": "proposal-unknown-support",
        "source_ids": ["a", "b"],
        "source_roles": {"a": "core", "b": "core"},
        "propositions": [
            {
                "statement": "An unresolved shared proposition.",
                "source_ids": ["a", "b"],
                "evidence": [_reference(row) for row in rows],
            }
        ],
    }

    mapped = map_overlapping_clusters(rows, proposals=[proposal], propositions=[])

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == "no_valid_multi_source_proposition_row"


def test_repeated_publications_from_one_study_family_do_not_raise_cluster_status() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile("a", family_id="family-a"),
            _profile("b", family_id="family-b"),
            _profile("c", family_id="family-b"),
        ]
    )
    cluster = map_overlapping_clusters(rows)["clusters"][0]

    assert cluster["source_count"] == 3
    assert cluster["independent_study_family_count"] == 2
    assert cluster["qualification_status"] == "emerging_cluster"


def test_analytical_cluster_memberships_are_capped_at_three() -> None:
    profiles = [_normalized_stub("hub"), *[_normalized_stub(f"peer-{index}") for index in range(4)]]
    identities = ("alpha relationship", "beta relationship", "gamma relationship", "delta relationship")
    propositions = [
        _manual_proposition(f"proposition-{index}", ["hub", f"peer-{index}"])
        for index in range(4)
    ]
    proposals = [
        {
            "proposal_id": f"proposal-{index}",
            "label": f"Relationship {index}",
            "semantic_identity": identities[index],
            "source_ids": ["hub", f"peer-{index}"],
            "source_roles": {"hub": "core", f"peer-{index}": "core"},
            "propositions": [propositions[index]],
        }
        for index in range(4)
    ]
    mapped = map_overlapping_clusters(
        profiles,
        proposals=proposals,
        propositions=propositions,
        policy=LiteratureMappingPolicy(max_memberships=3),
    )

    hub_memberships = [cluster for cluster in mapped["clusters"] if "hub" in cluster["source_ids"]]
    assert mapped["max_cluster_memberships"] == 3
    assert len(hub_memberships) == 3
    assert any(row["reason"] == "overlap_policy_removed_proposition_support" for row in mapped["rejected_proposals"])


def test_practitioner_guidance_cannot_be_core_for_empirical_effectiveness() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile("a", empirical_role="associational"),
            _profile("b", empirical_role="causal"),
            _profile(
                "guide",
                empirical_role="none",
                argument_role="practitioner_guidance",
                source_role="practitioner handbook",
            ),
        ]
    )
    proposition = _manual_proposition("proposition-effectiveness", ["a", "b", "guide"])
    proposal = {
        "proposal_id": "proposal-effectiveness",
        "label": "Ceasefire effectiveness",
        "semantic_identity": "ceasefire effectiveness",
        "source_ids": ["a", "b", "guide"],
        "source_roles": {"a": "core", "b": "core", "guide": "core"},
        "propositions": [proposition],
    }
    cluster = map_overlapping_clusters(rows, proposals=[proposal], propositions=[proposition])["clusters"][0]

    assert cluster["core_source_ids"] == ["a", "b"]
    assert cluster["context_source_ids"] == ["guide"]
    assert cluster["independent_study_family_count"] == 2

    one_empirical = deepcopy(proposition)
    one_empirical["source_ids"] = ["a", "guide"]
    rejected = map_overlapping_clusters(
        rows,
        proposals=[{**proposal, "source_ids": ["a", "guide"], "propositions": [one_empirical]}],
        propositions=[one_empirical],
    )
    assert rejected["clusters"] == []


def test_practice_guidance_causal_proposition_is_narrowed_to_an_attributed_claim() -> None:
    profiles = normalize_evidence_profiles(
        [
            _profile("guide-a", empirical_role="descriptive", source_role="practitioner guidance"),
            _profile("guide-b", empirical_role="descriptive", source_role="practitioner guidance"),
        ]
    )
    evidence = [_reference(profile) for profile in profiles]
    proposal = {
        "proposal_id": "proposal-guidance",
        "label": "Inclusive mediation guidance",
        "semantic_identity": "inclusive mediation guidance",
        "source_ids": ["guide-a", "guide-b"],
        "source_roles": {"guide-a": "core", "guide-b": "core"},
        "propositions": [
            {
                "statement": "Inclusion improves agreement durability.",
                "question": "Does inclusion improve durability?",
                "proposition_type": "practice_guidance",
                "evidence": evidence,
                "comparability": {"passed": True},
            }
        ],
    }

    cluster = map_overlapping_clusters(profiles, proposals=[proposal], propositions=[])["clusters"][0]
    proposition = cluster["propositions"][0]

    assert proposition["statement"].startswith(
        "Practice-guidance sources advance the claim that women participation improves"
    )
    assert proposition["original_statement"] == "Inclusion improves agreement durability."
    assert proposition["support_qualification"] == "causal_relationship_not_established"
    assert proposition["proposition_id"].startswith("proposition-")


def test_proposition_matrix_has_rows_and_core_columns_without_cross_product_contamination() -> None:
    source_dimensions = {
        "methods": ["metadata-only famine coding"],
        "data": ["food price metadata"],
        "cases": ["hostage negotiation metadata"],
        "outcomes": ["famine severity metadata"],
    }
    rows = normalize_evidence_profiles(
        [_profile(source_id, source_dimensions=source_dimensions) for source_id in ("a", "b", "context")]
    )
    proposition = build_literature_propositions(rows)[0]
    proposal = {
        "proposal_id": "proposal-matrix",
        "label": "Women participation and ceasefire durability",
        "semantic_identity": "women participation ceasefire durability",
        "source_ids": ["a", "b", "context"],
        "source_roles": {"a": "core", "b": "core", "context": "context"},
        "propositions": [proposition],
    }
    cluster = map_overlapping_clusters(rows, proposals=[proposal], propositions=[proposition])["clusters"][0]
    matrix = build_evidence_matrices(rows, [cluster])[0]

    assert matrix["core_source_ids"] == ["a", "b"]
    assert matrix["proposition_count"] == 1
    matrix_row = matrix["propositions"][0]
    assert matrix_row["proposition_id"] == proposition["proposition_id"]
    assert list(matrix_row["cells"]) == ["a", "b"]
    assert all(cell["scope"]["outcome"] == ["ceasefire durability"] for cell in matrix_row["cells"].values())
    assert not {"famine", "food", "hostage"} & set(re.findall(r"[a-z]+", repr(matrix).casefold()))


def test_evidence_references_keep_stable_anchor_ids_and_equal_legacy_aliases() -> None:
    first = _profile("a", anchor_id=None)
    second = _profile("b", anchor_id=None)
    first["evidence_anchors"][0].pop("evidence_anchor_id")
    second["evidence_anchors"][0].pop("evidence_anchor_id")
    first["evidence_anchors"][0]["finding_id"] = "legacy-anchor-a"
    second["evidence_anchors"][0]["claim_id"] = "legacy-anchor-b"
    normalized = normalize_evidence_profiles([first, second])
    proposition = build_literature_propositions(normalized)[0]

    assert {
        (reference["evidence_anchor_id"], reference["claim_id"])
        for reference in proposition["evidence"]
    } == {
        ("legacy-anchor-a", "legacy-anchor-a"),
        ("legacy-anchor-b", "legacy-anchor-b"),
    }

    generated = _profile("stable")
    generated["evidence_anchors"][0].pop("evidence_anchor_id")
    initial = normalize_evidence_profiles([generated])[0]["claims"][0]
    reworded = deepcopy(generated)
    reworded["evidence_anchors"][0]["claim"] = "Reworded prose with the same source location."
    revised = normalize_evidence_profiles([reworded])[0]["claims"][0]
    assert initial["evidence_anchor_id"] == revised["evidence_anchor_id"]


def test_causal_synthesis_is_rejected_without_causal_or_mechanism_anchors() -> None:
    profiles = normalize_evidence_profiles(
        [
            _profile("descriptive", empirical_role="descriptive"),
            _profile("associational", empirical_role="associational"),
        ]
    )
    cluster = map_overlapping_clusters(profiles)["clusters"][0]
    proposition = cluster["propositions"][0]
    proposed = {
        "cluster_id": cluster["cluster_id"],
        "synthesis": "The evidence establishes a causal effect.",
        "central_findings": [
            {
                "finding": "Women participation causes durable ceasefires.",
                "proposition_id": proposition["proposition_id"],
                "evidence": proposition["evidence"],
            }
        ],
        "supporting_evidence": proposition["evidence"],
    }

    validated = validate_cluster_synthesis(proposed, cluster, profiles)

    assert validated["central_findings"] == []
    assert validated["synthesis"] == ""
    assert validated["status"] == "deterministic_fallback"
    assert validated["rejected_assertions"][0]["reason"] == "causal_wording_without_causal_or_mechanism_anchor"


def test_effectiveness_wording_is_causal_unless_explicitly_attributed() -> None:
    profiles = normalize_evidence_profiles(
        [_profile("a", empirical_role="descriptive"), _profile("b", empirical_role="descriptive")]
    )
    cluster = map_overlapping_clusters(profiles)["clusters"][0]
    proposition = cluster["propositions"][0]
    evidence = proposition["evidence"]

    rejected = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "central_findings": [
                {
                    "finding": "Inclusion improves agreement durability.",
                    "proposition_id": proposition["proposition_id"],
                    "evidence": evidence,
                }
            ],
        },
        cluster,
        profiles,
    )
    admitted = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "central_findings": [
                {
                    "finding": "Guidance sources assert that inclusion improves agreement durability.",
                    "proposition_id": proposition["proposition_id"],
                    "evidence": evidence,
                }
            ],
        },
        cluster,
        profiles,
    )

    assert rejected["central_findings"] == []
    assert admitted["central_findings"][0]["finding"].startswith("Guidance sources assert")


def test_cluster_synthesis_counts_admitted_section_evidence_missing_from_top_summary() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(profiles)["clusters"][0]
    proposition = cluster["propositions"][0]
    evidence = proposition["evidence"]

    validated = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "synthesis": (
                "The mapped sources report the same relationship across two independent study families, so the "
                "collection supports a bounded consensus about the proposition. Both sources contribute directly to "
                "that comparison, although their evidence remains associational and should not be read as a causal "
                "estimate. The verdict is therefore a shared empirical pattern within the mapped settings, with scope "
                "and measurement limits that should be preserved in any downstream use."
            ),
            "boundaries": ["The comparison is limited to the two mapped study settings."],
            "debate_state": "mapped_consensus",
            "supporting_evidence": [evidence[0]],
            "central_findings": [
                {
                    "finding": (
                        "The mapped sources report the same bounded association between participation and "
                        "ceasefire durability across two independent evidence bases. In plain English, both "
                        "sources observe that greater participation accompanies more durable ceasefires, but "
                        "neither establishes causation. The comparison remains limited to their mapped settings "
                        "and outcome measures. This makes the repeated pattern useful for orienting the debate, "
                        "while leaving open whether it travels to other conflicts or measurement strategies."
                    ),
                    "proposition_id": proposition["proposition_id"],
                    "evidence": evidence,
                }
            ],
        },
        cluster,
        profiles,
    )

    assert validated["status"] == "reasoned"
    assert validated["synthesis"].startswith("The mapped sources report the same bounded association")
    assert {row["source_id"] for row in validated["supporting_evidence"]} == {"a", "b"}


def test_cluster_synthesis_with_a_thin_validated_assertion_is_partial() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(profiles)["clusters"][0]
    proposition = cluster["propositions"][0]

    validated = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "synthesis": "",
            "boundaries": ["The evidence is limited to the mapped settings."],
            "debate_state": "mapped_consensus",
            "central_findings": [
                {
                    "finding": "The sources report a comparable relationship.",
                    "proposition_id": proposition["proposition_id"],
                    "evidence": proposition["evidence"],
                }
            ],
        },
        cluster,
        profiles,
    )

    assert validated["status"] == "partial"
    assert validated["quality_status"] == "incomplete"
    assert "verdict_too_thin" in validated["quality_errors"]


def test_associational_verdict_language_is_not_erased_as_causal() -> None:
    verdict = (
        "Lower conflict intensity is associated with increased mediation success probabilities. "
        "The studies report this as a correlational pattern, and no causal mechanism is established."
    )

    assert literature._has_unqualified_causal_language(verdict) is False
    assert literature._has_unqualified_causal_language("Directive mediation increases settlement success.") is True


def test_attributed_findings_and_statistical_effect_terms_are_not_causal_overclaims() -> None:
    verdict = (
        "The studies converge on the proposition that local mediation can reduce violence, "
        "but none of the sources provide quantitative testing or causal evidence. "
        "Bercovitch finds no significant effect of elapsed time, while the reported marginal effect "
        "for directive strategy is positive."
    )

    assert literature._has_unqualified_causal_language(verdict) is False


def test_cluster_quality_reports_uncovered_propositions_and_core_sources() -> None:
    errors = literature._cluster_synthesis_quality_errors(
        {
            "synthesis": (
                "The admitted evidence establishes a bounded collection-level pattern across the sources represented in "
                "the first proposition. That pattern remains associational and limited to the mapped settings, so it "
                "cannot establish a causal relationship or answer the second proposition. The verdict is intentionally "
                "restricted to supported comparisons and does not extend to evidence that is absent from the synthesis."
            ),
            "central_findings": [{"finding": "A bounded pattern is present."}],
            "synthesis_assertions": [{"proposition_ids": ["proposition-a"]}],
            "supporting_evidence": [{"source_id": "source-a"}, {"source_id": "source-b"}],
            "boundaries": ["The evidence is limited to the mapped settings."],
            "debate_state": "single_position",
        },
        {
            "proposition_ids": ["proposition-a", "proposition-b"],
            "source_roles": [
                {"source_id": "source-a", "role": "core"},
                {"source_id": "source-b", "role": "core"},
                {"source_id": "source-c", "role": "core"},
            ],
        },
    )

    assert "uncovered_admitted_proposition:proposition-b" in errors
    assert "uncovered_core_source:source-c" in errors


def _debate_cell(
    source_id: str,
    *,
    direction: str = "positive",
    evidence_type: str = "associational",
    boundary: str = "",
    stance: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "study_family_id": f"family-{source_id}",
        "evidence_base_group_id": f"evidence-base-{source_id}",
        "counted_as_independent": True,
        "stance_or_finding": stance or f"distinct finding from {source_id}",
        "evidence_type": [evidence_type],
        "boundary_conditions": [boundary] if boundary else [],
        "direction_or_interpretation": [direction] if direction else [],
        "evidence": [],
    }


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({}, "no_debate"),
        ({"a": _debate_cell("a")}, "single_position"),
        (
            {
                "a": _debate_cell("a", direction="positive", stance="Higher trust increases participation."),
                "b": _debate_cell("b", direction="negative", stance="Higher trust decreases participation."),
            },
            "mapped_debate",
        ),
        (
            {"a": _debate_cell("a", direction="positive"), "b": _debate_cell("b", direction="positive")},
            "emerging_convergence",
        ),
        (
            {"a": _debate_cell("a", direction="mixed"), "b": _debate_cell("b", direction="positive")},
            "mixed_evidence",
        ),
        (
            {
                "a": _debate_cell("a", boundary="high-capacity settings"),
                "b": _debate_cell("b", boundary="low-capacity settings"),
            },
            "conditional_relationship",
        ),
        (
            {
                "a": _debate_cell("a", evidence_type="associational"),
                "b": _debate_cell("b", evidence_type="conceptual"),
            },
            "complementary_positions",
        ),
    ],
)
def test_proposition_cells_cover_seven_debate_states(cells: dict[str, Any], expected: str) -> None:
    state, _ = _proposition_debate_state({"cells": cells})
    assert state == expected


def test_custom_direction_labels_do_not_create_a_false_debate() -> None:
    state, explanation = _proposition_debate_state(
        {
            "cells": {
                "a": _debate_cell("a", direction="positive"),
                "b": _debate_cell("b", direction="obstructed perception of stalemate"),
            }
        }
    )

    assert state == "emerging_convergence"
    assert explanation == {"direction": "positive", "effective_evidence_base_count": 2}


def test_parallel_literatures_completes_the_eight_state_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(
        literature,
        "build_evidence_matrices",
        lambda profiles, clusters: [
            {
                "cluster_id": "cluster-parallel",
                "propositions": [
                    {
                        "proposition_id": "proposition-a",
                        "statement": "A",
                        "cells": {"a": _debate_cell("a"), "b": _debate_cell("b")},
                    },
                    {
                        "proposition_id": "proposition-b",
                        "statement": "B",
                        "cells": {"c": _debate_cell("c"), "d": _debate_cell("d")},
                    },
                ],
            }
        ],
    )

    assessment = build_debate_registry([], [{"cluster_id": "cluster-parallel"}])["assessments"][0]
    assert assessment["classification"] == "parallel_literatures"


def test_named_proposition_lineage_admits_opposing_wordings_without_token_bureaucracy() -> None:
    rows = normalize_evidence_profiles(
        [
            _profile("a", claim="Participation predicts durable ceasefires.", direction="positive"),
            _profile("b", claim="Inclusive talks undermine long-term ceasefires.", direction="negative"),
        ]
    )
    references = [_reference(row) for row in rows]
    claim_lookup = {
        (str(claim["source_id"]), str(claim["claim_id"])): claim
        for row in rows
        for claim in row["claims"]
    }
    candidate = {
        "rule": "contradictory_findings",
        "proposition_id": "proposition-shared",
        "proposition_evidence_keys": [
            {
                "source_id": reference["source_id"],
                "evidence_anchor_id": reference["evidence_anchor_id"],
            }
            for reference in references
        ],
    }

    assert _gap_rule_admission_errors(candidate, references, claim_lookup) == []


@pytest.mark.parametrize(
    ("field", "replacement", "expected_error"),
    [
        ("proposition_ids", [], "missing_originating_proposition"),
        ("originating_cluster_revisions", [], "missing_originating_cluster_revision"),
        ("missing_cell", {}, "missing_precise_matrix_cell"),
    ],
)
def test_gap_candidate_requires_proposition_cluster_revision_and_missing_cell_lineage(
    field: str,
    replacement: Any,
    expected_error: str,
) -> None:
    candidate = {
        "gap_id": "gap-lineage",
        "rule": "untested_mechanism",
        "topic": "women participation ceasefire durability",
        "precise_missing_evidence": "A mechanism test linking participation to ceasefire durability.",
        "related_cluster_ids": ["cluster-ceasefire"],
        "proposition_ids": ["proposition-ceasefire"],
        "originating_cluster_revisions": ["revision-ceasefire"],
        "missing_cell": {"kind": "mechanism", "description": "Mechanism evidence is absent."},
        "supporting_evidence": [
            {
                "source_id": "a",
                "claim_id": "anchor-a",
                "evidence_anchor_id": "anchor-a",
                "study_family_id": "family-a",
                "locator": "p. 10",
            }
        ],
    }
    assert not {
        "missing_originating_proposition",
        "missing_originating_cluster_revision",
        "missing_precise_matrix_cell",
    } & set(_gap_specificity_errors(candidate))

    broken = deepcopy(candidate)
    broken[field] = replacement
    assert expected_error in _gap_specificity_errors(broken)


def test_gap_mapper_emits_only_the_five_canonical_statuses() -> None:
    answer_rows = [
        {
            "gap_id": "gap-answered",
            "status": "answered",
            "source_id": "a",
            "evidence_anchor_id": "anchor-a",
            "locator": "p. 18",
        },
        {
            "gap_id": "gap-narrowed",
            "status": "partial",
            "source_id": "a",
            "evidence_anchor_id": "anchor-a",
            "locator": "p. 18",
        },
    ]
    profiles = normalize_evidence_profiles(
        [_profile("a", gap_answers=answer_rows), _profile("b")]
    )
    references = [_reference(profile) for profile in profiles]

    def candidate(gap_id: str, supporting: list[dict[str, Any]], *, rule: str = "replication") -> dict[str, Any]:
        return {
            "gap_id": gap_id,
            "rule": rule,
            "topic": "women participation ceasefire durability",
            "precise_missing_evidence": "Comparable evidence resolving women participation and ceasefire durability.",
            "supporting_evidence": supporting,
            "why_matters": "The answer changes the collection-level inference.",
            "contribution": "The comparison resolves the mapped omission.",
        }

    candidates = [
        candidate("gap-underspecified", references, rule="contradictory_findings"),
        candidate("gap-lead", references[:1]),
        candidate("gap-surviving", references),
        candidate("gap-answered", references),
        candidate("gap-narrowed", references),
    ]
    validated, _ = search_and_validate_gaps(candidates, profiles)

    statuses = {row["status"] for row in validated}
    assert statuses == CANONICAL_GAP_STATUSES
    assert not statuses & {
        "mapped_collection_gap",
        "gap_lead",
        "narrowed_gap_lead",
        "rejected_rule_admission",
        "rejected_gap_quality",
        "rejected_answered_elsewhere",
    }


NON_QUANTITATIVE_RESOLUTION_REQUIREMENTS = {
    "qualitative": {
        "case_selection": "Most-likely and least-likely ceasefire cases",
        "mechanism_evidence": "Process observations of participant influence",
        "negative_cases": "Failed ceasefires without participant influence",
        "process_observations": "Meeting records and participant interviews",
    },
    "historical_interpretive": {
        "archives": "Negotiation records and diplomatic archives",
        "periodization": "Pre-agreement, bargaining, and implementation periods",
        "source_criticism": "Triangulate official and participant records",
        "competing_interpretations": "Selection and institutional-capacity accounts",
    },
    "theoretical": {
        "premises": "Participation changes information and legitimacy",
        "derivation": "Derive observable implications for durability",
        "scope": "Negotiated ceasefires with implementation institutions",
        "model_comparison": "Compare information and legitimacy mechanisms",
    },
    "normative": {
        "principles": "Equal political standing and affected interests",
        "objections": "Urgency and representation objections",
        "application_tests": "Apply principles to contrasting negotiations",
    },
    "methodological": {
        "assumptions": "Comparable coding and temporal ordering",
        "diagnostics": "Inter-coder and timing diagnostics",
        "benchmarks": "Hand-coded expert benchmark",
        "robustness": "Alternative inclusion and durability definitions",
    },
    "practitioner": {
        "implementation_evidence": "Facilitator records and implementation logs",
        "institutional_context": "Negotiation mandate and access constraints",
        "bias_checks": "Independent participant and observer accounts",
    },
}


@pytest.mark.parametrize("path_type", sorted(NON_QUANTITATIVE_RESOLUTION_REQUIREMENTS))
def test_gap_quality_gate_accepts_non_quantitative_resolution_paths(path_type: str) -> None:
    resolution_path = {
        "path_type": path_type,
        "question": "How could this mapped proposition be resolved?",
        "evidence_needed": "New proposition-specific evidence from comparable cases.",
        "requirements": NON_QUANTITATIVE_RESOLUTION_REQUIREMENTS[path_type],
        "feasibility": "The collection identifies accessible evidence routes.",
        "limitations": ["The conclusion remains collection scoped."],
    }
    assert ResolutionPath.from_dict(resolution_path).path_type == path_type

    gap = {
        "value_assessment": {
            "puzzle_type": "competing explanations",
            "puzzle": "Why does participation appear linked to ceasefire durability?",
            "strongest_obvious_answer": "Selection into inclusive negotiations explains the association.",
            "why_obvious_answer_is_inadequate": "The mapped evidence does not adjudicate selection and influence.",
            "competing_explanations": ["selection", "institutional capacity"],
            "decision_or_inference_changed": "Resolution changes whether participation is treated as consequential.",
            "information_gain": "moderate",
            "non_obviousness_passed": True,
            "importance_passed": True,
            "rejection_reasons": [],
        },
        "resolution_path": resolution_path,
    }
    assert _gap_quality_errors(gap, require_design=True) == []


def test_human_markdown_omits_empty_audit_and_raw_id_text_while_linking_sources_and_gaps() -> None:
    source = {
        "source_id": "source-raw-a",
        "note_id": "note-raw-a",
        "title": "Source A",
        "note_path": "01_source_notes/Source A.md",
        "study_family_id": "family-a",
        "normalized_tags": [],
        "claims": [
            {
                "evidence_anchor_id": "anchor-raw-a",
                "claim_id": "anchor-raw-a",
                "text": "Women participation is associated with ceasefire durability.",
            }
        ],
    }
    proposition = {
        "proposition_id": "proposition-raw-ceasefire",
        "statement": "Women participation is associated with ceasefire durability.",
        "question": "Does participation predict durability?",
        "evidence": [
            {
                "source_id": "source-raw-a",
                "evidence_anchor_id": "anchor-raw-a",
                "claim_id": "anchor-raw-a",
                "locator": "p. 12",
            }
        ],
    }
    cluster = {
        "cluster_id": "cluster-raw-ceasefire",
        "label": "Participation and ceasefire durability",
        "shared_question": "Does participation predict durability?",
        "status": "emerging_cluster",
        "qualification_status": "emerging_cluster",
        "revision_hash": "revision-raw-ceasefire",
        "proposition_ids": [proposition["proposition_id"]],
        "propositions": [proposition],
        "topic_neighborhood_ids": [],
        "source_roles": [{"source_id": "source-raw-a", "role": "core"}],
        "representative_sources": [source],
        "shared_normalized_tags": [],
    }
    gap = {
        "gap_id": "gap-raw-ceasefire",
        "title": "Mechanism linking participation to durability",
        "gap_statement": "The collection does not identify the intervening mechanism.",
        "precise_missing_evidence": "A mechanism test for participation and durability.",
        "rule": "untested_mechanism",
        "status": "collection_surviving_gap",
        "promoted": True,
        "proposition_ids": [proposition["proposition_id"]],
        "originating_cluster_revisions": [cluster["revision_hash"]],
        "related_cluster_ids": [cluster["cluster_id"]],
        "missing_cell": {"description": "Mechanism evidence is absent."},
        "supporting_evidence": proposition["evidence"],
        "countervailing_evidence": [],
        "warnings": [],
        "quality_rejection_reasons": ["AUDIT SECRET"],
        "internal_search_results": [{"status": "AUDIT SECRET"}],
        "structured_signature": "AUDIT SECRET",
    }
    cluster_text = _cluster_markdown(
        cluster,
        None,
        {"classification": "no_debate"},
        [gap],
        synthesis={
            "synthesis": (
                "Internationalization changes outcomes "
                "(proposition-raw-ceasefire; Kane2022, anchor-raw-a)."
            ),
            "central_findings": [{"finding": "", "evidence": proposition["evidence"]}],
            "rejected_assertions": [{"reason": "AUDIT SECRET"}],
        },
        profile_by_source={"source-raw-a": source},
    )
    gap_text = _gap_markdown(
        gap,
        profile_by_source={"source-raw-a": source},
        cluster_by_id={cluster["cluster_id"]: cluster},
    )

    assert "[[Source A]]" in cluster_text and "[[Source A]]" in gap_text
    assert "Kane2022" not in cluster_text
    assert "[[Gap - " in cluster_text
    assert "[[Cluster - " in gap_text
    for text in (cluster_text, gap_text):
        visible = _reader_visible_markdown(text)
        assert "AUDIT SECRET" not in text
        assert "- None" not in visible
        assert "None specified" not in visible
        assert "anchor-raw-a" not in visible
        assert "source-raw-a" not in visible
        assert "gap-raw-ceasefire" not in visible
        assert "cluster-raw-ceasefire" not in visible
    assert "## Findings and interpretation" not in cluster_text
    assert "## Why findings differ" not in cluster_text


def test_partial_cluster_renders_a_question_without_claiming_a_verdict() -> None:
    cluster = {
        "cluster_id": "cluster-question-only",
        "label": "Question-only cluster",
        "shared_question": "What does the mapped evidence establish?",
        "status": "emerging_cluster",
        "revision_hash": "revision-question-only",
        "proposition_ids": [],
        "propositions": [],
        "topic_neighborhood_ids": [],
        "source_roles": [],
        "representative_sources": [],
    }

    text = _cluster_markdown(
        cluster,
        None,
        {"classification": "no_debate"},
        synthesis={"status": "partial", "synthesis": ""},
    )

    assert "## Cluster question" in text
    assert "## Question and verdict" not in text


def test_ceasefire_mapping_rejects_broad_inclusion_and_irrelevant_metadata_conflation() -> None:
    irrelevant_source_metadata = {
        "methods": ["famine early-warning coding"],
        "data": ["food-access records"],
        "cases": ["hostage-release negotiations"],
        "outcomes": ["famine mortality", "food insecurity", "hostage release"],
    }
    rows = [
        _profile(
            source_id,
            topic="women participation",
            outcome="ceasefire durability",
            claim="Women participation improves ceasefire durability.",
            semantic_topics=["inclusion"],
            source_dimensions=irrelevant_source_metadata,
        )
        for source_id in ("ceasefire-a", "ceasefire-b")
    ]
    rows.extend(
        _profile(
            source_id,
            topic="humanitarian access",
            outcome="famine mortality",
            claim="Humanitarian access reduces famine mortality.",
            semantic_topics=["inclusion"],
        )
        for source_id in ("famine-a", "famine-b")
    )
    rows.append(
        _profile(
            "hostage-a",
            topic="hostage release",
            outcome="negotiation timing",
            claim="Hostage release accelerates negotiation timing.",
            semantic_topics=["inclusion"],
        )
    )
    normalized = normalize_evidence_profiles(rows)
    neighborhoods = map_topic_neighborhoods(normalized)
    propositions = build_literature_propositions(normalized)
    ceasefire = next(row for row in propositions if "Women participation" in row["statement"])
    famine = next(row for row in propositions if "Humanitarian access" in row["statement"])

    assert set(ceasefire["source_ids"]) == {"ceasefire-a", "ceasefire-b"}
    assert not {"famine", "food", "hostage"} & set(re.findall(r"[a-z]+", repr(ceasefire).casefold()))
    inclusion_neighborhood = next(row for row in neighborhoods if row["semantic_identity"] == "inclusion")
    assert set(inclusion_neighborhood["source_ids"]) == {
        "ceasefire-a",
        "ceasefire-b",
        "famine-a",
        "famine-b",
        "hostage-a",
    }

    broad_proposal = {
        "proposal_id": "proposal-broad-inclusion",
        "label": "Inclusion",
        "semantic_identity": "inclusion",
        "shared_question": "What does inclusion do?",
        "source_ids": [row["source_id"] for row in normalized],
        "source_roles": {row["source_id"]: "core" for row in normalized},
        "propositions": [],
    }
    mapped = map_overlapping_clusters(
        normalized,
        proposals=[broad_proposal],
        propositions=propositions,
        topic_neighborhoods=neighborhoods,
    )

    assert not any(
        {ceasefire["proposition_id"], famine["proposition_id"]}
        <= set(cluster["proposition_ids"])
        for cluster in mapped["clusters"]
    )
    assert not any(
        {"ceasefire-a", "ceasefire-b"} < set(cluster["source_ids"])
        and {"famine-a", "famine-b"} < set(cluster["source_ids"])
        for cluster in mapped["clusters"]
    )
