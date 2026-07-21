from __future__ import annotations

from typing import Any

from auto_zettelkasten import literature
from auto_zettelkasten.literature import (
    _cluster_answer_excerpt,
    _cluster_display_question,
    _cluster_display_coherence,
    _cluster_display_source_roles,
    _cluster_display_threads,
    _cluster_projection_is_publishable,
    _fallback_source_contributions,
    _apply_researcher_display_safeguards,
    _human_locator_text,
    _human_projection_text,
    _human_prose_errors,
    _human_technical_result,
    _map_verdict_excerpt,
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


def test_future_observed_evidence_flags_a_bibliographic_identity_conflict() -> None:
    profile = _profile("mismatched-report")
    profile["context"] = {"date": "09/1996"}
    profile["evidence_anchors"][0]["finding"] = (
        "As of 1 August 2000, 25% of positions were vacant, and the 2001 budget "
        "was estimated at $2.582 billion."
    )

    normalized = normalize_evidence_profiles([profile])[0]

    assert normalized["analytical"] is False
    assert normalized["limited"] is True
    assert normalized["bibliographic_identity_status"] == "source_identity_conflict"
    assert "1996" in normalized["exclusion_reason"]
    assert "2000-2001" in normalized["exclusion_reason"]


def test_forward_looking_projection_does_not_create_an_identity_conflict() -> None:
    profile = _profile("forecast")
    profile["context"] = {"date": "2024"}
    profile["evidence_anchors"][0]["finding"] = (
        "The scenario projects conflict risks in 2030 and 2040."
    )

    normalized = normalize_evidence_profiles([profile])[0]

    assert normalized["analytical"] is True
    assert normalized["bibliographic_identity_status"] == "not_flagged"


def _reference(source_id: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "evidence_anchor_id": f"anchor-{source_id}",
        "locator": "p. 10",
    }


def test_atomic_note_reconciliation_rejects_a_stale_term_value_pairing() -> None:
    raw = _profile("a")
    raw["evidence_anchors"][0].update(
        {
            "finding": "Territorial war has a coefficient of 0.114 (p < .05).",
            "topic": "territorial war",
            "magnitude": "0.114",
            "uncertainty": "p < .05",
        }
    )
    profiles = normalize_evidence_profiles([raw])

    mismatch_count = literature._reconcile_profile_anchors_with_atomic_notes(
        profiles,
        [
            {
                "source_id": "a",
                "body": (
                    "Territorial war has a coefficient of 0.351 (p < .1); "
                    "logged war duration has a coefficient of 0.114 (p < .05)."
                ),
            }
        ],
    )

    anchor = profiles[0]["claims"][0]
    assert mismatch_count == 1
    assert anchor["support_status"] == "support_unknown"
    assert anchor["note_numeric_pairing_valid"] is False
    assert literature._anchor_is_synthesis_eligible(anchor) is False


def test_atomic_note_reconciliation_accepts_a_matching_value_among_related_figures() -> None:
    raw = _profile("a")
    raw["evidence_anchors"][0].update(
        {
            "finding": "Landmine victims averaged 2 per day.",
            "topic": "landmine victims",
            "magnitude": "2 per day",
        }
    )
    profiles = normalize_evidence_profiles([raw])

    mismatch_count = literature._reconcile_profile_anchors_with_atomic_notes(
        profiles,
        [
            {
                "source_id": "a",
                "body": (
                    "The source reports several estimates; 40% of landmine victims "
                    "were civilians and landmine victims averaged 2 per day."
                ),
            }
        ],
    )

    anchor = profiles[0]["claims"][0]
    assert mismatch_count == 0
    assert anchor["support_status"] == "supported"
    assert "note_numeric_pairing_valid" not in anchor


def test_atomic_note_reconciliation_ignores_generated_graph_links() -> None:
    raw = _profile("a")
    raw["evidence_anchors"][0].update(
        {
            "finding": "Territorial war has a coefficient of 0.114 (p < .05).",
            "topic": "territorial war",
            "magnitude": "0.114",
            "uncertainty": "p < .05",
        }
    )
    profiles = normalize_evidence_profiles([raw])

    mismatch_count = literature._reconcile_profile_anchors_with_atomic_notes(
        profiles,
        [
            {
                "source_id": "a",
                "body": (
                    "Territorial war has a coefficient of 0.351 (p < .1); "
                    "logged war duration has a coefficient of 0.114 (p < .05).\n\n"
                    "## Graph Links\n"
                    "- [[Cluster - Territorial War 0.114|Territorial war coefficient 0.114]]"
                ),
            }
        ],
    )

    assert mismatch_count == 1
    assert profiles[0]["claims"][0]["support_status"] == "support_unknown"


def test_outcome_centered_family_keeps_exact_propositions_and_context() -> None:
    specifications = {
        "strategy-a": "Directive mediator strategy is associated with mediation success.",
        "strategy-b": "Active mediator strategy is associated with mediation success.",
        "fatality-a": "Higher fatalities are associated with lower mediation success.",
        "fatality-b": "Conflict fatalities are associated with lower mediation success.",
        "occurrence": "War duration predicts whether mediation occurs.",
    }
    raw_profiles = []
    for source_id, finding in specifications.items():
        profile = _profile(source_id)
        profile["title"] = f"Mediation success and occurrence study {source_id}"
        profile["research_questions"] = [
            "What predicts mediation success or whether mediation occurs?"
        ]
        profile["outcomes"] = [
            "mediation occurrence" if source_id == "occurrence" else "mediation success"
        ]
        profile["evidence_anchors"][0].update(
            finding=finding,
            topic="mediation determinants",
            outcome=profile["outcomes"],
            plain_english_meaning=finding,
        )
        raw_profiles.append(profile)
    profiles = normalize_evidence_profiles(raw_profiles)
    proposal = {
        "proposal_id": "proposal-success-determinants",
        "label": "Determinants of Mediation Success",
        "semantic_identity": "mediation success effectiveness determinants",
        "bounded_object": "factors associated with mediation success",
        "shared_question": "Which factors are associated with mediation success?",
        "coherence_rationale": "The sources study determinants of mediation outcomes.",
        "source_ids": list(specifications),
        "source_roles": {source_id: "core" for source_id in specifications},
        "supporting_evidence": [
            {
                "source_id": profile["source_id"],
                "evidence_anchor_id": profile["claims"][0]["evidence_anchor_id"],
                "locator": profile["claims"][0]["locator"],
            }
            for profile in profiles
        ],
        "propositions": [
            {
                "statement": "Mediator strategy is associated with mediation success.",
                "source_ids": ["strategy-a", "strategy-b"],
                "evidence": [_reference("strategy-a"), _reference("strategy-b")],
            },
            {
                "statement": "Fatalities are associated with mediation success.",
                "source_ids": ["fatality-a", "fatality-b"],
                "evidence": [_reference("fatality-a"), _reference("fatality-b")],
            },
        ],
    }

    mapped = map_overlapping_clusters(
        profiles, proposals=[proposal], propositions=[]
    )

    assert len(mapped["clusters"]) == 1
    cluster = mapped["clusters"][0]
    assert set(cluster["core_source_ids"]) == {
        "strategy-a",
        "strategy-b",
        "fatality-a",
        "fatality-b",
    }
    assert cluster["context_source_ids"] == ["occurrence"]
    assert len(cluster["propositions"]) == 2
    assert all(
        "occurrence" not in proposition["source_ids"]
        for proposition in cluster["propositions"]
    )


def test_support_unknown_anchor_cannot_appear_in_researcher_facing_synthesis() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(profiles)["clusters"][0]
    anchor = profiles[0]["claims"][0]
    anchor["support_envelope"]["support_status"] = "support_unknown"
    anchor["support_status"] = "support_unknown"

    synthesis = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "evidence_threads": [
                {
                    "title": "Unverified numerical summary",
                    "summary": "The source reports nine suspensions.",
                    "relationship": "complementary",
                    "source_ids": ["a"],
                    "evidence": [_reference("a")],
                }
            ],
        },
        cluster,
        profiles,
    )

    assert all(
        "nine suspensions" not in str(row.get("summary") or "").casefold()
        for row in synthesis["evidence_threads"]
    )


def test_human_projection_repairs_old_narrowing_debris_and_machine_locators() -> None:
    assert (
        _human_projection_text(
            "The suffering prevention aims to is associated with lower."
        )
        == "The suffering prevention aims to reduce."
    )
    assert (
        _human_projection_text("Failure can is associated with resumed fighting.")
        == "Failure may be associated with resumed fighting."
    )
    assert (
        _human_projection_text(
            "The world saw a big is associated with higher in conflict."
        )
        == "The world saw a large increase in conflict."
    )
    assert (
        _human_projection_text(
            "Government-biased mediators is associated with higher the likelihood of settlement."
        )
        == "Government-biased mediators are associated with a higher likelihood of settlement."
    )
    assert (
        _human_projection_text(
            "Both types is associated with higher the probability of talks."
        )
        == "Both types are associated with a higher probability of talks."
    )
    assert (
        _human_projection_text("The combination almost guarantees success.")
        == "The combination corresponds to a very high model-predicted probability of success."
    )
    assert (
        _human_projection_text("The manual describes proven techniques.")
        == "The manual describes practitioner-recommended techniques."
    )
    assert "does not itself test" in _human_projection_text(
        "Mediation works best when mediators are seen as fair, include all relevant groups, "
        "are well-prepared, and follow international norms."
    )
    assert "raise—but do not test" in _human_projection_text(
        "Together, they suggest that legitimacy compensates for capacity deficits."
    )
    assert "borderline at p=0.068" in _human_projection_text(
        "Using directive mediation raises the chance of success from about 38% to about 45%."
    )
    assert (
        _human_technical_result("not_reported; than non-internationalized cases") == ""
    )
    assert _human_locator_text("data") == ""
    assert _human_locator_text("Table 2, p. 16") == "Table 2, p. 16"
    assert _human_prose_errors("The model significantly is associated with success.")
    assert not _human_prose_errors(
        "This is a reported link, not proof of cause: the model increases predicted success."
    )
    assert _human_projection_text("uNGA2020 and hellmüller2022 (finding-1)") == (
        "UNGA (2020) and Hellmüller (2022)"
    )


def test_map_excerpts_stop_at_complete_sentences_and_deduplicate_threads() -> None:
    long_second = (
        "The first result is complete. The second result compares 669 staff with a much larger "
        "organization and continues beyond the display budget without reaching a stable conclusion."
    )
    assert _map_verdict_excerpt(long_second, character_limit=45) == (
        "The first result is complete."
    )
    synthesis = {
        "evidence_threads": [
            {"summary": "One complete finding."},
            {"summary": "One complete finding."},
            {"summary": "A second complete finding."},
        ]
    }
    assert _cluster_answer_excerpt(synthesis) == (
        "One complete finding. A second complete finding."
    )
    assert (
        _map_verdict_excerpt(
            "The U.N. study reports a result. DeRouen et al. explain the model.",
            sentence_limit=1,
        )
        == "The U.N. study reports a result."
    )
    assert (
        _map_verdict_excerpt(
            "One complete finding. Alvarez et al.",
            sentence_limit=2,
        )
        == "One complete finding."
    )
    assert (
        _map_verdict_excerpt(
            "The estimate is positive (coefficient 0.48, p<0.05 in Model 2; Table I).",
            sentence_limit=1,
            character_limit=58,
        )
        == "The estimate is positive."
    )


def test_human_cluster_projection_demotes_off_question_context() -> None:
    cluster = {
        "cluster_id": "cluster-un-support",
        "label": "UN Mediation Support and Institutional Capacity",
        "shared_question": "How does institutional capacity improve mediation effectiveness?",
        "semantic_identity": "UN mediation support infrastructure capacity guidance",
        "bounded_object": "Standby teams, expert rosters, and operational support",
        "source_ids": ["capacity", "demand"],
        "core_source_ids": ["capacity", "demand"],
        "source_roles": [
            {"source_id": "capacity", "role": "core"},
            {"source_id": "demand", "role": "core"},
        ],
        "propositions": [],
    }
    synthesis = {
        "evidence_threads": [
            {
                "title": "Operational support capacity",
                "summary": "The standby team and expert roster provide rapid operational support.",
                "evidence": [{"source_id": "capacity", "locator": "p. 8"}],
            },
            {
                "title": "Global conflict deaths",
                "question": "What conflict trends justify increased mediation capacity?",
                "summary": "Conflict fatalities increased sixfold.",
                "evidence": [{"source_id": "demand", "locator": "p. 2"}],
            },
            {
                "title": "Distinct source contributions to the cluster question",
                "summary": "The sources answer separate questions.",
                "origin": "deterministic_source_contribution_map",
            },
        ],
        "source_contributions": [
            {
                "source_id": "capacity",
                "finding": "The standby team supplies rapid operational support to mediators.",
                "contribution_kind": "unique_cluster_relevant_finding",
            },
            {
                "source_id": "demand",
                "finding": "Conflict fatalities increased sixfold.",
                "contribution_kind": "unique_cluster_relevant_finding",
            },
        ],
    }

    assert _cluster_display_source_roles(cluster, synthesis) == {
        "capacity": "core",
        "demand": "core",
    }
    assert [row["title"] for row in _cluster_display_threads(cluster, synthesis)] == [
        "Operational support capacity"
    ]
    assert _cluster_display_question(cluster, synthesis) == (
        "What does this collection show about UN Mediation Support and Institutional Capacity?"
    )
    assert _cluster_display_coherence(cluster, synthesis) == (
        "These sources are grouped because they centrally address the bounded question: What does this "
        "collection show about UN Mediation Support and Institutional Capacity? Their contributions may "
        "answer different parts of that question; cluster membership alone does not imply agreement or "
        "establish a shared causal effect."
    )


def test_display_role_accepts_a_core_source_through_a_relevant_evidence_thread() -> (
    None
):
    cluster = {
        "label": "Engaging Armed Groups and Mediator Bias",
        "shared_question": "How do mediators engage armed groups and how does bias matter?",
        "source_roles": [
            {"source_id": "practice", "role": "core"},
            {"source_id": "bias", "role": "core"},
        ],
    }
    synthesis = {
        "evidence_threads": [
            {
                "title": "Engaging armed groups",
                "summary": "The practice study maps ways to engage armed groups.",
                "evidence": [{"source_id": "practice", "locator": "p. 10"}],
            }
        ],
        "source_contributions": [
            {
                "source_id": "bias",
                "finding": "Mediator bias is associated with negotiated settlement.",
            }
        ],
    }

    assert _cluster_display_source_roles(cluster, synthesis) == {
        "practice": "core",
        "bias": "core",
    }


def test_projection_never_silently_rewrites_canonical_source_roles() -> None:
    cluster = {
        "label": "Ceasefire Design",
        "shared_question": "What does the collection show about ceasefire design?",
        "source_roles": [
            {"source_id": "design", "role": "core"},
            {"source_id": "fatalities", "role": "core"},
        ],
    }
    synthesis = {
        "evidence_threads": [
            {
                "title": "Ceasefire design and conflict context",
                "summary": "Ceasefire design is discussed alongside conflict trends.",
                "evidence": [
                    {"source_id": "design", "locator": "p. 10"},
                    {"source_id": "fatalities", "locator": "p. 2"},
                ],
            }
        ],
        "source_contributions": [
            {
                "source_id": "design",
                "finding": "The source identifies ceasefire design provisions.",
            },
            {
                "source_id": "fatalities",
                "finding": "Conflict fatalities increased sixfold.",
            },
        ],
    }

    assert _cluster_display_source_roles(cluster, synthesis) == {
        "design": "core",
        "fatalities": "core",
    }


def test_cross_cluster_relation_requires_two_sided_locator_backed_evidence() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = {
        "cluster_id": "cluster-a",
        "label": "Mediation design",
        "shared_question": "How is mediation designed?",
        "source_ids": ["a"],
        "core_source_ids": ["a"],
        "source_roles": [{"source_id": "a", "role": "core"}],
        "propositions": [],
        "family_relations": [],
    }
    target = {
        "cluster_id": "cluster-b",
        "label": "Settlement durability",
        "source_ids": ["b"],
    }
    raw = {
        "cluster_id": "cluster-a",
        "related_clusters": [
            {
                "target_cluster_id": "cluster-b",
                "relation_type": "shared_mechanism",
                "relationship": (
                    "Mediation design connects to settlement durability because both clusters "
                    "examine how process design relates to durable settlements."
                ),
                "current_evidence": [_reference("a")],
                "target_evidence": [_reference("b")],
            }
        ],
    }

    result = validate_cluster_synthesis(
        raw,
        cluster,
        profiles,
        deterministic_debate={"classification": "no_debate"},
        all_clusters=[cluster, target],
    )

    assert result["related_clusters"][0]["target_cluster_id"] == "cluster-b"
    raw["related_clusters"][0]["target_evidence"] = []
    rejected = validate_cluster_synthesis(
        raw,
        cluster,
        profiles,
        deterministic_debate={"classification": "no_debate"},
        all_clusters=[cluster, target],
    )
    assert rejected["related_clusters"] == []


def test_complete_deterministic_synthesis_is_publishable() -> None:
    assert _cluster_projection_is_publishable(
        {"status": "deterministic_fallback", "quality_status": "complete"}
    )
    assert not _cluster_projection_is_publishable(
        {"status": "deterministic_fallback", "quality_status": "incomplete"}
    )


def test_thematic_cluster_gets_complete_source_specific_fallback() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    evidence = [
        {
            "source_id": profile["source_id"],
            "evidence_anchor_id": profile["claims"][0]["evidence_anchor_id"],
            "claim_id": profile["claims"][0]["evidence_anchor_id"],
            "locator": profile["claims"][0]["locator"],
            "evidence_base_group_id": profile["study_lineage"][
                "evidence_base_group_id"
            ],
        }
        for profile in profiles
    ]
    cluster = {
        "cluster_id": "cluster-thematic",
        "label": "Mediation design",
        "shared_question": "What do these sources contribute to mediation design?",
        "coherence_rationale": "Both sources address mediation design.",
        "source_ids": ["a", "b"],
        "core_source_ids": ["a", "b"],
        "source_roles": [
            {"source_id": "a", "role": "core"},
            {"source_id": "b", "role": "core"},
        ],
        "propositions": [],
        "family_relations": [
            {
                "relation_type": "shared_research_problem",
                "evidence": evidence,
            }
        ],
    }

    result = validate_cluster_synthesis(
        {},
        cluster,
        profiles,
        deterministic_debate={"classification": "parallel_literatures"},
    )

    assert result["status"] == "deterministic_fallback"
    assert result["quality_status"] == "complete"
    assert len(result["source_contributions"]) >= 2
    assert result["evidence_threads"][0]["origin"] == (
        "deterministic_source_contribution_map"
    )
    assert "different questions and evidence types" in result["synthesis"]


def test_parallel_single_source_threads_get_one_bounded_collection_answer() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    references = [_reference("a"), _reference("b")]
    cluster = {
        "cluster_id": "cluster-parallel",
        "label": "Mediation design",
        "shared_question": "What do these sources contribute to mediation design?",
        "coherence_rationale": "Both sources address mediation design.",
        "source_ids": ["a", "b"],
        "core_source_ids": ["a", "b"],
        "source_roles": [
            {"source_id": "a", "role": "core"},
            {"source_id": "b", "role": "core"},
        ],
        "propositions": [],
        "family_relations": [
            {
                "relation_type": "shared_research_problem",
                "evidence": references,
            }
        ],
    }
    raw = {
        "cluster_id": "cluster-parallel",
        "debate_state": "parallel_literatures",
        "evidence_threads": [
            {
                "title": "First strand",
                "summary": "Study A maps one part of the design problem.",
                "evidence": [references[0]],
            },
            {
                "title": "Second strand",
                "summary": "Study B maps a different part of the design problem.",
                "evidence": [references[1]],
            },
        ],
        "source_contributions": [
            {
                "source_id": source_id,
                "finding": "Mediation design is positively associated with settlement durability.",
                "evidence": [_reference(source_id)],
            }
            for source_id in ("a", "b")
        ],
    }

    result = validate_cluster_synthesis(
        raw,
        cluster,
        profiles,
        deterministic_debate={"classification": "parallel_literatures"},
    )

    assert result["status"] == "reasoned"
    assert result["quality_status"] == "complete"
    assert any(
        row.get("origin") == "deterministic_source_contribution_map"
        for row in result["evidence_threads"]
    )


def test_reasoned_thematic_map_is_publishable_when_independence_is_unresolved() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    for profile in profiles:
        profile["evidence_base_counted"] = False
        profile["study_lineage"]["independence_status"] = "independence_uncertain"
        profile["study_lineage"]["counted_as_independent"] = False
        for claim in profile["claims"]:
            claim["evidence_base_counted"] = False
            claim["independence_status"] = "independence_uncertain"
    references = [_reference("a"), _reference("b")]
    cluster = {
        "cluster_id": "cluster-thematic-uncertain",
        "label": "Track One and Track Two Diplomacy",
        "shared_question": "How do official and unofficial diplomatic tracks connect?",
        "coherence_rationale": "Both sources map distinct parts of diplomatic practice.",
        "status": "evidence_concentrated_cluster",
        "source_ids": ["a", "b"],
        "core_source_ids": ["a", "b"],
        "source_roles": [
            {"source_id": "a", "role": "core"},
            {"source_id": "b", "role": "core"},
        ],
        "propositions": [],
        "family_relations": [],
    }
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "parallel_literatures",
            "boundaries": [
                "The publications are conceptual and do not compare effectiveness."
            ],
            "evidence_threads": [
                {
                    "title": "Diplomatic tracks and their roles",
                    "summary": (
                        "The sources distinguish official, unofficial, and hybrid tracks and explain "
                        "how they can connect, while offering no independent effectiveness test."
                    ),
                    "plain_english_meaning": (
                        "They map different diplomatic routes, not proof that one route works best."
                    ),
                    "evidence": references,
                }
            ],
            "source_contributions": [
                {
                    "source_id": source_id,
                    "finding": (
                        "The source explains how one diplomatic track contributes to conflict resolution."
                    ),
                    "plain_english_meaning": (
                        "It describes what this channel can do and where it is limited."
                    ),
                    "evidence": [_reference(source_id)],
                }
                for source_id in ("a", "b")
            ],
        },
        cluster,
        profiles,
        deterministic_debate={"classification": "parallel_literatures"},
    )

    assert result["effective_evidence_base_count"] == 0
    assert result["status"] == "reasoned"
    assert result["quality_status"] == "complete"
    assert _cluster_projection_is_publishable(result)


def test_source_specific_contributions_survive_without_becoming_agreement() -> None:
    normalized = normalize_evidence_profiles(
        [_profile("a"), _profile("b"), _profile("c")]
    )
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
    assert {row["source_id"] for row in result["source_contributions"]} == {
        "a",
        "b",
        "c",
    }
    assert result["agreements"] == []
    assert any(
        row["reason"] == "comparative_assertion_requires_two_effective_evidence_bases"
        for row in result["rejected_assertions"]
    )
    for contribution in result["source_contributions"]:
        ClusterSourceContribution.from_dict(contribution)


def test_validated_source_contributions_build_a_readable_noncomparative_answer() -> (
    None
):
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "parallel_literatures",
            "boundaries": ["The sources answer different parts of the question."],
            "source_contributions": [
                {
                    "source_id": source_id,
                    "finding": (
                        "Mediation design is positively associated with settlement durability."
                    ),
                    "plain_english_meaning": (
                        "Better-designed processes tend to be followed by more durable settlements."
                    ),
                    "evidence": [_reference(source_id)],
                }
                for source_id in ("a", "b")
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert result["evidence_threads"][0]["origin"] == (
        "deterministic_source_contribution_map"
    )
    assert "different questions and evidence types" in result["synthesis"]
    assert "findings remain source-specific" in result["synthesis"]


def test_noncausal_contribution_uses_canonical_anchor_instead_of_model_gloss() -> (
    None
):
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "source_contributions": [
                {
                    "source_id": "a",
                    "finding": "The source recommends inclusive mediation practices.",
                    "plain_english_meaning": (
                        "Including women makes peace agreements more sustainable."
                    ),
                    "evidence": [_reference("a")],
                }
            ],
        },
        cluster,
        normalized,
    )

    contribution = next(
        row for row in result["source_contributions"] if row["source_id"] == "a"
    )
    assert contribution["plain_english_meaning"] == normalized[0]["claims"][0][
        "plain_english_meaning"
    ]


def test_broad_generated_section_locator_cannot_support_synthesis() -> None:
    profile = _profile("a")
    profile["evidence_anchors"][0]["locator"] = "findings"
    normalized = normalize_evidence_profiles([profile])

    assert normalized[0]["claims"][0]["source_locator"]["kind"] == (
        "broad_section_heading"
    )
    assert not literature._anchor_is_synthesis_eligible(normalized[0]["claims"][0])


def test_cluster_verdict_need_not_enumerate_every_core_source() -> None:
    normalized = normalize_evidence_profiles(
        [_profile("a"), _profile("b"), _profile("c")]
    )
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": [
                "The answer is bounded to the mapped settings and associational evidence."
            ],
            "evidence_threads": [
                {
                    "title": "The main shared research problem",
                    "summary": (
                        "The first two studies show why mediation design and settlement durability belong in the same "
                        "bounded research conversation. Their findings are associational and cover different settings, "
                        "so the cluster answer maps the relationship without claiming that design causes durability. "
                        "The third study's distinct contribution remains visible in the source-by-source findings below "
                        "rather than being forced into this top-level comparison."
                    ),
                    "plain_english_meaning": (
                        "The answer summarizes the main thread; it does not need to repeat every study bullet."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("a"), _reference("b")],
                },
            ],
            "source_contributions": [
                {
                    "source_id": source_id,
                    "finding": (
                        "Mediation design is positively associated with settlement durability."
                    ),
                    "plain_english_meaning": (
                        "Better-designed processes tend to be followed by more durable settlements."
                    ),
                    "evidence": [_reference(source_id)],
                }
                for source_id in ("a", "b", "c")
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert {row["source_id"] for row in result["source_contributions"]} == {
        "a",
        "b",
        "c",
    }
    assert {
        row["source_id"] for row in result["verdict_paragraphs"][0]["evidence"]
    } == {"a", "b"}


def test_evidence_thread_accepts_typed_relationship_with_plain_english_explanation() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": ["The evidence is associational and bounded to the mapped cases."],
            "evidence_threads": [
                {
                    "title": "Two complementary contributions",
                    "summary": (
                        "The studies address the same bounded problem from different angles. "
                        "One examines mediator design while the other examines settlement durability, "
                        "so together they explain the relationship without implying a causal effect. "
                        "Their different measures should remain visible because a shared topic does not "
                        "make the evidence directly comparable. The cluster therefore preserves both "
                        "contributions while limiting its verdict to the connection the sources support."
                    ),
                    "plain_english_meaning": (
                        "The studies fit together, but they do not test the same causal claim."
                    ),
                    "relationship": (
                        "Complementary: one source maps process design and the other maps outcomes."
                    ),
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
            "source_contributions": [
                {
                    "source_id": source_id,
                    "finding": "Mediation design is associated with settlement durability.",
                    "plain_english_meaning": (
                        "The source links process design to how long settlements last."
                    ),
                    "evidence": [_reference(source_id)],
                }
                for source_id in ("a", "b")
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert result["evidence_threads"][0]["relationship_type"] == "complementary"
    assert result["evidence_threads"][0]["relationship"].startswith("Complementary:")


def test_context_and_bridge_sources_can_organize_a_thematic_thread() -> None:
    normalized = normalize_evidence_profiles(
        [_profile("a"), _profile("b"), _profile("bridge")]
    )
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    cluster["core_source_ids"] = ["a", "b"]
    cluster["bridge_source_ids"] = ["bridge"]
    cluster["source_roles"] = [
        {"source_id": "a", "role": "core"},
        {"source_id": "b", "role": "core"},
        {"source_id": "bridge", "role": "bridge"},
    ]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": [
                "The bridge source explains context rather than an independent effect."
            ],
            "evidence_threads": [
                {
                    "title": "Empirical finding and contextual explanation",
                    "summary": (
                        "Study B reports an association between mediation design and durability, while the bridge "
                        "source explains how practitioners interpret that design problem. The two contributions "
                        "belong in one thematic thread but do not establish a shared causal conclusion. This keeps "
                        "the empirical observation separate from the practitioner's explanation and shows why both "
                        "sources are useful without pretending that they test the same proposition."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("b"), _reference("bridge")],
                }
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert {row["title"] for row in result["evidence_threads"]} == {
        "Empirical finding and contextual explanation",
        "Distinct source contributions to the cluster question",
    }
    assert not any(
        row["reason"] == "context_or_bridge_source_cannot_support_verdict_thread"
        for row in result["rejected_assertions"]
    )


def test_universal_direction_summary_is_rejected_when_a_cited_source_declines() -> (
    None
):
    rising = _profile("a")
    rising["evidence_anchors"][0]["finding"] = (
        "Conflict incidence rose during the later period."
    )
    declining = _profile("b")
    declining["evidence_anchors"][0]["finding"] = (
        "Conflict onsets declined, and low-intensity onsets roughly halved."
    )
    profiles = normalize_evidence_profiles(
        [rising, declining]
    )
    anchor_by_key = {
        (row["source_id"], row["claims"][0]["evidence_anchor_id"]): row["claims"][0]
        for row in profiles
    }

    assert literature._universal_direction_conflicts_with_evidence(
        "Both reports show rising conflict levels.",
        [_reference("a"), _reference("b")],
        anchor_by_key,
    )
    assert not literature._universal_direction_conflicts_with_evidence(
        "The reports cover different periods and point in different directions.",
        [_reference("a"), _reference("b")],
        anchor_by_key,
    )


def test_named_source_attribution_requires_that_sources_locator() -> None:
    carnegie = _profile("carnegie")
    carnegie["citation_key"] = "Carnegie2024"
    carnegie["study_lineage"]["authors"] = ["Carnegie"]
    world_bank = _profile("world-bank")
    world_bank["citation_key"] = "WorldBank2024"
    profiles = normalize_evidence_profiles([carnegie, world_bank])
    cluster = map_overlapping_clusters(profiles)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": ["The studies use different evidence."],
            "evidence_threads": [
                {
                    "title": "Mediation design evidence",
                    "summary": (
                        "Carnegie reports that mediation design is positively associated with settlement durability."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("world-bank")],
                }
            ],
        },
        cluster,
        profiles,
    )

    assert any(
        row["reason"]
        == "named_source_attribution_not_supported_by_cited_source"
        for row in result["rejected_assertions"]
    )


def test_universal_summary_requires_every_core_source() -> None:
    profiles = normalize_evidence_profiles(
        [_profile("a"), _profile("b"), _profile("c")]
    )
    cluster = map_overlapping_clusters(profiles)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "mapped_consensus",
            "boundaries": ["The collection contains three core studies."],
            "central_findings": [
                {
                    "finding": "All studies associate mediation design with settlement durability.",
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
        },
        cluster,
        profiles,
    )

    assert any(
        row["reason"] == "universal_claim_missing_core_source_coverage"
        for row in result["rejected_assertions"]
    )


def test_evidence_thread_rejects_any_number_absent_from_cited_anchors() -> None:
    first = _profile("a")
    first["evidence_anchors"][0]["finding"] = (
        "Mediation design is associated with a 10 percent increase in settlement durability."
    )
    second = _profile("b")
    second["evidence_anchors"][0]["finding"] = (
        "Mediation design is associated with a 20 percent increase in settlement durability."
    )
    profiles = normalize_evidence_profiles([first, second])
    cluster = map_overlapping_clusters(profiles)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": ["The estimates differ."],
            "evidence_threads": [
                {
                    "title": "Reported estimates",
                    "summary": (
                        "The studies report 10 percent and 30 percent increases in settlement durability."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
        },
        cluster,
        profiles,
    )

    assert any(
        row["reason"] == "evidence_thread_sentence_not_supported_by_locator"
        for row in result["rejected_assertions"]
    )


def test_shared_located_source_projects_a_reciprocal_cluster_bridge_only() -> None:
    profiles = normalize_evidence_profiles(
        [_profile("shared"), _profile("left"), _profile("right")]
    )
    clusters = [
        {
            "cluster_id": "cluster-left",
            "label": "Local mediation",
            "source_ids": ["shared", "left"],
            "source_roles": [
                {"source_id": "shared", "role": "core"},
                {"source_id": "left", "role": "core"},
            ],
        },
        {
            "cluster_id": "cluster-right",
            "label": "UN mediation architecture",
            "source_ids": ["shared", "right"],
            "source_roles": [
                {"source_id": "shared", "role": "context"},
                {"source_id": "right", "role": "core"},
            ],
        },
    ]
    syntheses = {
        cluster["cluster_id"]: {
            "related_clusters": [],
            "source_contributions": [
                {
                    "source_id": "shared",
                    "finding": (
                        "The shared source supplies a distinct located contribution to "
                        f"{cluster['label']}."
                    ),
                    "evidence": [_reference("shared")],
                }
            ],
        }
        for cluster in clusters
    }

    literature._project_cross_cluster_relationships(
        clusters,
        syntheses,
        profiles,
        typed_relations=[],
    )

    left = syntheses["cluster-left"]["related_clusters"]
    right = syntheses["cluster-right"]["related_clusters"]
    assert [row["target_cluster_id"] for row in left] == ["cluster-right"]
    assert [row["target_cluster_id"] for row in right] == ["cluster-left"]
    assert left[0]["relation_type"] == "shared_source_bridge"
    assert "not evidence that the clusters agree" in left[0]["relationship"]

    for cluster in clusters:
        for role in cluster["source_roles"]:
            if role["source_id"] == "shared":
                role["role"] = "context"
    syntheses = {
        cluster["cluster_id"]: {
            "related_clusters": [],
            "source_contributions": [
                {
                    "source_id": "shared",
                    "finding": "A located contextual contribution.",
                    "evidence": [_reference("shared")],
                }
            ],
        }
        for cluster in clusters
    }
    literature._project_cross_cluster_relationships(
        clusters,
        syntheses,
        profiles,
        typed_relations=[
            {
                "source_id": "left",
                "target_source_id": "right",
                "inferred": True,
                "provenance": "canonical_subject_tag_overlap",
            }
        ],
    )
    assert all(
        not synthesis["related_clusters"] for synthesis in syntheses.values()
    )


def test_cluster_local_gap_hypothesis_cannot_invent_an_unrelated_cluster_backlink() -> (
    None
):
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    proposition_id = cluster["proposition_ids"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": ["The studies are associational."],
            "evidence_threads": [
                {
                    "title": "Mediation design and durability",
                    "summary": (
                        "The studies examine how mediation design is associated with settlement durability. "
                        "They address the same bounded research problem, but their observational evidence does "
                        "not establish that changing process design would itself cause a more durable settlement."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
            "gap_hypotheses": [
                {
                    "rule": "untested_mechanism",
                    "proposition_id": proposition_id,
                    "topic": "mediation design and settlement durability",
                    "precise_missing_evidence": (
                        "Process evidence distinguishing mediator design from case selection."
                    ),
                    "observed_pattern": "The located studies report an association.",
                    "why_matters": "The distinction changes the interpretation of the association.",
                    "contribution": "A mechanism account would separate design from selection.",
                    "related_cluster_ids": [
                        cluster["cluster_id"],
                        "cluster-unrelated",
                    ],
                    "supporting_evidence": [_reference("a"), _reference("b")],
                }
            ],
        },
        cluster,
        normalized,
    )

    assert result["gap_hypotheses"][0]["related_cluster_ids"] == [
        cluster["cluster_id"]
    ]


def test_composite_source_contribution_is_replaced_by_a_narrow_cluster_anchor() -> (
    None
):
    raw_a = _profile("a")
    raw_a["evidence_anchors"][0].update(
        {
            "evidence_anchor_id": "anchor-a-specific",
            "finding": (
                "Mediator diversity is positively associated with mediation success "
                "(coefficient 1.237, standard error 0.590)."
            ),
            "locator": "pp. 130-131, Table 2",
            "magnitude": "coefficient 1.237",
            "uncertainty": "standard error 0.590",
        }
    )
    raw_a["evidence_anchors"].append(
        {
            **raw_a["evidence_anchors"][0],
            "evidence_anchor_id": "anchor-a-composite",
            "finding": (
                "Mediator diversity is positively associated with mediation success, while a selection "
                "diagnostic reports p=.028. "
                + "This composite note summary bundles several separately located model results. " * 14
            ),
            "locator": "pp. 129-131, Tables 1-2",
            "magnitude": "coefficient 1.237",
            "uncertainty": "selection-correlation p=.028",
        }
    )
    profiles = normalize_evidence_profiles([raw_a, _profile("b")])
    cluster = {
        "cluster_id": "cluster-success",
        "label": "Determinants of mediation success",
        "shared_question": "Which factors are associated with mediation success?",
        "source_ids": ["a", "b"],
        "core_source_ids": ["a", "b"],
        "source_roles": [
            {"source_id": "a", "role": "core"},
            {"source_id": "b", "role": "core"},
        ],
        "propositions": [],
        "family_relations": [
            {
                "relation_type": "shared_research_problem",
                "evidence": [
                    {
                        "source_id": "a",
                        "evidence_anchor_id": "anchor-a-specific",
                        "locator": "pp. 130-131, Table 2",
                    },
                    _reference("b"),
                ],
            }
        ],
    }
    result = validate_cluster_synthesis(
        {
            "cluster_id": "cluster-success",
            "debate_state": "complementary_positions",
            "boundaries": ["The studies address different determinants."],
            "evidence_threads": [
                {
                    "title": "Factors associated with mediation success",
                    "summary": (
                        "The studies identify different factors associated with mediation outcomes. "
                        "Their estimates should remain source-specific because they use different models and comparisons."
                    ),
                    "relationship": "complementary",
                    "evidence": [
                        {
                            "source_id": "a",
                            "evidence_anchor_id": "anchor-a-specific",
                            "locator": "pp. 130-131, Table 2",
                        },
                        _reference("b"),
                    ],
                }
            ],
            "source_contributions": [
                {
                    "source_id": "a",
                    "cluster_role": "core",
                    "contribution_kind": "unique_cluster_relevant_finding",
                    "related_proposition_ids": [],
                    "evidence_thread_id": "",
                    "finding": "Diversity predicts success with p=.028.",
                    "technical_result": "coefficient 1.237; p=.028",
                    "plain_english_meaning": "More diverse mediators appear more likely to succeed.",
                    "relation_to_cluster_question": "A determinant of success.",
                    "comparison_status": "single_source",
                    "evidence": [
                        {
                            "source_id": "a",
                            "evidence_anchor_id": "anchor-a-composite",
                            "locator": "pp. 129-131, Tables 1-2",
                        }
                    ],
                }
            ],
        },
        cluster,
        profiles,
    )

    contribution = next(
        row for row in result["source_contributions"] if row["source_id"] == "a"
    )
    assert contribution["evidence"][0]["evidence_anchor_id"] == "anchor-a-specific"
    assert "p=.028" not in contribution["finding"]
    assert "Table 2" in contribution["evidence"][0]["locator"]


def test_organizational_thread_narrows_noncausal_effect_language() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "complementary_positions",
            "boundaries": ["The studies are associational."],
            "evidence_threads": [
                {
                    "title": "Mediation design and durability",
                    "summary": (
                        "The studies report that mediation design increases settlement durability. Their "
                        "observational evidence does not establish a causal effect, and differences in cases and "
                        "measurement limit the comparison. The thread therefore records a recurring association "
                        "inside this collection while keeping the source-specific estimates separate below. In "
                        "plain English, the studies point in a similar direction, but the evidence cannot show that "
                        "changing mediation design by itself would produce a more durable settlement."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
        },
        cluster,
        normalized,
    )

    thread = result["evidence_threads"][0]
    assert result["status"] == "reasoned"
    assert thread["summary"].startswith("The cited sources report that")
    assert "does not by itself establish causation" in thread["summary"]
    assert "mediation design increases settlement durability" in thread["summary"]
    assert not _human_prose_errors(thread["summary"])
    assert thread["causal_language_narrowed"] is True


def test_unadjudicated_gap_language_is_narrowed_to_collection_limitation() -> None:
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]

    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "debate_state": "no_debate",
            "boundaries": ["The collection covers two settings."],
            "evidence_threads": [
                {
                    "title": "Missing comparative coverage",
                    "summary": (
                        "The lack of comparable implementation evidence is a critical research gap, although the "
                        "collection does not yet support promotion of a gap claim. The studies still clarify which "
                        "parts of implementation are documented and which comparison is missing. This is useful as "
                        "a bounded map of the evidence without making a literature-wide novelty claim."
                    ),
                    "relationship": "complementary",
                    "evidence": [_reference("a"), _reference("b")],
                }
            ],
        },
        cluster,
        normalized,
    )

    assert result["status"] == "reasoned"
    assert "critical research gap" not in result["synthesis"]
    assert "important limitation in this collection" in result["synthesis"]


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
    assert (
        _quantitative_text_errors(
            {
                "technical_result": "The estimate is a 0.25 percentage point decrease; 95% CI not reported.",
            }
        )
        == []
    )
    assert (
        _quantitative_text_errors(
            {
                "technical_result": (
                    "Overall success was 22%; 56% of disputes were mediated; substantive strategies had 44% "
                    "success versus 19% for conciliation, a 25 percentage point difference."
                ),
            }
        )
        == []
    )
    assert (
        _quantitative_text_errors(
            {
                "technical_result": (
                    "The probability rose by 41.7 percentage points (from 3.6% to 45.3%). "
                    "A different subgroup rose by 34.4 pp, while coefficient = 0.601 (p<0.01)."
                ),
            }
        )
        == []
    )
    assert _quantitative_text_errors(
        {
            "technical_result": (
                "The probability rose by 40 percentage points (from 3.6% to 45.3%). "
                "A separate estimate was 34.4 pp."
            ),
        }
    ) == ["percentage_point_arithmetic_mismatch"]
    assert (
        _quantitative_text_errors(
            {
                "technical_result": (
                    "The coefficient was 0.601 (p<0.01). The predicted probability changed by 14%. "
                    "Neither statistic is reported as a marginal effect or percentage-point conversion."
                ),
            }
        )
        == []
    )
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
    assert (
        _quantitative_item_errors(
            {"finding": "The studies cover 1990 to 2020."},
            require_comparable=True,
        )
        == []
    )
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

    source_a = [
        row for row in result["source_contributions"] if row["source_id"] == "a"
    ]
    assert len(source_a) == 1
    assert source_a[0]["plain_english_meaning"] == (
        "Better-designed mediation processes tend to be followed by more durable settlements."
    )
    assert source_a[0]["evidence"][0]["locator"] == normalized[0]["claims"][0][
        "locator"
    ]
    assert "12 percentage points" not in source_a[0]["technical_result"]


def test_valid_model_contribution_uses_canonical_anchor_wording_numbers_and_locator() -> (
    None
):
    normalized = normalize_evidence_profiles([_profile("a"), _profile("b")])
    cluster = map_overlapping_clusters(normalized)["clusters"][0]
    result = validate_cluster_synthesis(
        {
            "cluster_id": cluster["cluster_id"],
            "source_contributions": [
                {
                    "source_id": "a",
                    "finding": "A model-written paraphrase that changes the result.",
                    "plain_english_meaning": "A model-written interpretation.",
                    "technical_result": "14% more likely",
                    "evidence": [
                        {
                            **_reference("a"),
                            "locator": "p. 999",
                        }
                    ],
                }
            ],
        },
        cluster,
        normalized,
    )

    contribution = next(
        row for row in result["source_contributions"] if row["source_id"] == "a"
    )
    anchor = normalized[0]["claims"][0]
    assert contribution["finding"] == anchor["text"]
    assert contribution["plain_english_meaning"] == anchor["plain_english_meaning"]
    assert contribution["evidence"][0]["locator"] == anchor["locator"]
    assert contribution["technical_result"] != "14% more likely"


def test_fallback_source_contributions_prefer_specific_anchor_over_composite_summary() -> (
    None
):
    raw = _profile("a")
    raw["evidence_anchors"] = [
        {
            **raw["evidence_anchors"][0],
            "evidence_anchor_id": "anchor-specific",
            "claim": "Prevention saved $33 billion in the neutral scenario.",
            "locator": "p. 4",
            "magnitude": "$33 billion",
            "plain_english_meaning": "The middle scenario still produced large savings.",
        },
        {
            **raw["evidence_anchors"][0],
            "evidence_anchor_id": "anchor-composite",
            "claim": " ".join(["Many unrelated findings and figures."] * 30),
            "locator": "p. 4; p. 14; p. 128",
            "locators": ["p. 4", "p. 14", "p. 128"],
            "plain_english_meaning": "A broad summary mixing several results.",
        },
    ]
    normalized = normalize_evidence_profiles([raw, _profile("b")])
    cluster = {
        "cluster_id": "cluster-prevention",
        "label": "Conflict prevention",
        "semantic_identity": "conflict prevention",
        "shared_question": "What does prevention save?",
        "source_ids": ["a"],
        "source_roles": [{"source_id": "a", "role": "core"}],
        "propositions": [],
        "family_relations": [],
    }

    contributions = _fallback_source_contributions(cluster, normalized)

    source_a_ids = {
        row["evidence"][0]["evidence_anchor_id"]
        for row in contributions
        if row["source_id"] == "a"
    }
    assert "anchor-specific" in source_a_ids
    assert "anchor-composite" not in source_a_ids


def test_display_scope_broadens_cluster_when_core_sources_cover_wider_domains() -> None:
    cluster = {
        "cluster_id": "cluster-success",
        "label": "Mediation Success Determinants in Civil Wars",
        "shared_question": (
            "What determines whether mediation produces settlement in civil wars?"
        ),
        "source_roles": [
            {"source_id": "civil", "role": "core"},
            {"source_id": "disputes", "role": "core"},
            {"source_id": "crises", "role": "core"},
        ],
    }
    profiles = [
        {"source_id": "civil", "populations": ["civil wars"]},
        {"source_id": "disputes", "populations": ["international disputes"]},
        {"source_id": "crises", "populations": ["international crises"]},
    ]

    _apply_researcher_display_safeguards([cluster], profiles)

    assert cluster["display_label"] == "Determinants of Mediation Success"
    assert "civil wars" not in cluster["display_question"].casefold()
    assert "international disputes" in cluster["display_scope_note"]


def test_display_safeguard_recognizes_a_conference_series_from_profile_titles() -> None:
    cluster = {
        "cluster_id": "cluster-conference",
        "label": "Mediation Research Evidence",
        "shared_question": "What makes mediation effective?",
        "bounded_object": "Istanbul Mediation Conference Series",
        "source_roles": [
            {"source_id": "conference-a", "role": "core"},
            {"source_id": "conference-b", "role": "core"},
        ],
    }
    profiles = [
        {"source_id": "conference-a", "title": "Istanbul Mediation Conference Report 2017"},
        {"source_id": "conference-b", "title": "Istanbul Mediation Conference Report 2018"},
    ]

    _apply_researcher_display_safeguards([cluster], profiles)

    assert cluster["display_label"] == (
        "Practitioner Priorities from the Istanbul Mediation Conferences"
    )
    assert cluster["evidence_character"] == "practitioner_guidance"
    assert "conference reports identify" in cluster["display_question"]


def test_inconsistent_fallback_anchor_keeps_finding_but_omits_conflicting_numbers() -> (
    None
):
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

    contribution = next(
        row for row in result["source_contributions"] if row["source_id"] == "a"
    )
    assert contribution["finding"] == (
        "Mediation design is positively associated with settlement durability."
    )
    assert contribution["technical_result"] == ""
    assert (
        "could not be reconciled as one comparison"
        in contribution["plain_english_meaning"]
    )
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


def test_cluster_proposal_checkpoint_tracks_every_provider_visible_family_input() -> (
    None
):
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
                    "validated_note"
                    if index < 65
                    else "limited_note"
                    if index < 73
                    else "exhausted"
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
    assert (
        len(
            [row for row in register["records"] if row["terminal_state"] == "exhausted"]
        )
        == 2
    )
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
        assert all(
            profile["evidence_base_counted"] is False
            for profile in normalized_colliding
        )
        thematic = map_overlapping_clusters(normalized_colliding)["clusters"]
        assert len(thematic) == 1
        assert thematic[0]["qualification_status"] == "evidence_concentrated_cluster"


def test_same_author_and_interview_count_reconcile_shared_fieldwork() -> None:
    shared = [_profile("a"), _profile("b")]
    shared[0]["study_lineage"].update(
        authors=["Sara Example"], data_sources=["41 semi-structured interviews"]
    )
    shared[1]["study_lineage"].update(
        authors=["Sara Example"], data_sources=["Fieldwork based on 41 interviews"]
    )

    normalized = normalize_evidence_profiles(shared)

    assert len({row["evidence_base_group_id"] for row in normalized}) == 1
    assert all(
        "shared_fieldwork:interviews:41" in row["study_lineage"]["group_basis"]
        for row in normalized
    )

    unrelated = [_profile("c"), _profile("d")]
    unrelated[0]["study_lineage"].update(
        authors=["Author One"], data_sources=["41 semi-structured interviews"]
    )
    unrelated[1]["study_lineage"].update(
        authors=["Author Two"], data_sources=["41 semi-structured interviews"]
    )
    normalized_unrelated = normalize_evidence_profiles(unrelated)
    assert len({row["evidence_base_group_id"] for row in normalized_unrelated}) == 2


def test_thread_locator_is_repaired_only_from_a_supporting_source_anchor() -> None:
    raw = _profile("a")
    raw["evidence_anchors"] = [
        {
            **raw["evidence_anchors"][0],
            "evidence_anchor_id": "anchor-correct",
            "finding": "Conflict deaths increased tenfold between 2005 and 2016.",
            "locator": "p. 14",
        },
        {
            **raw["evidence_anchors"][0],
            "evidence_anchor_id": "anchor-wrong",
            "finding": "Physical-integrity protections are associated with 37% fewer protests.",
            "locator": "p. 128",
        },
    ]
    profile = normalize_evidence_profiles([raw])[0]
    item = {
        "summary": "Conflict deaths increased tenfold between 2005 and 2016.",
        "evidence": [
            {
                "source_id": "a",
                "evidence_anchor_id": "anchor-wrong",
                "locator": "p. 128",
            }
        ],
    }

    statement, evidence = literature._reconcile_evidence_thread_support(
        item,
        item["summary"],
        item["evidence"],
        {"a": profile},
    )

    assert statement == item["summary"]
    assert evidence[0]["evidence_anchor_id"] == "anchor-correct"
    assert evidence[0]["locator"] == "p. 14"


def test_independence_sidecars_match_public_contracts() -> None:
    from auto_zettelkasten.literature import build_independence_records

    records = build_independence_records(
        normalize_evidence_profiles([_profile("a"), _profile("b")])
    )
    for row in records["study_lineages"]:
        StudyLineage.from_dict(row)
    for row in records["evidence_base_groups"]:
        EvidenceBaseGroup.from_dict(row)
    for row in records["independence_assessments"]:
        IndependenceAssessment.from_dict(row)


def test_v2_every_core_source_in_an_admitted_thematic_cluster_appears_in_a_valid_evidence_thread() -> (
    None
):
    """V-2 characterization: every core source in an admitted thematic cluster
    is represented in at least one valid EvidenceThread produced by
    validate_cluster_synthesis. This guarantees that thematic admission never
    silently drops a source from the researcher-facing synthesis."""

    profiles = normalize_evidence_profiles(
        [_profile("alpha"), _profile("beta"), _profile("gamma")]
    )
    references = [_reference(profile["source_id"]) for profile in profiles]
    cluster = {
        "cluster_id": "cluster-v2-thematic",
        "label": "Mediation design subliterature",
        "shared_question": (
            "What do these studies contribute to mediation design?"
        ),
        "coherence_rationale": (
            "All three sources address bounded pieces of the same "
            "mediation-design subliterature."
        ),
        "source_ids": ["alpha", "beta", "gamma"],
        "core_source_ids": ["alpha", "beta", "gamma"],
        "source_roles": [
            {"source_id": "alpha", "role": "core"},
            {"source_id": "beta", "role": "core"},
            {"source_id": "gamma", "role": "core"},
        ],
        "propositions": [],
        "family_relations": [
            {
                "relation_type": "shared_research_problem",
                "source_ids": ["alpha", "beta", "gamma"],
                "rationale": (
                    "The sources examine complementary parts of one "
                    "mediation-design subliterature."
                ),
                "evidence": references,
                "comparability": {},
            }
        ],
    }

    result = validate_cluster_synthesis(
        {},
        cluster,
        profiles,
        deterministic_debate={"classification": "parallel_literatures"},
    )

    assert result["status"] in {"deterministic_fallback", "complete"}
    threads = [thread for thread in result.get("evidence_threads", []) or []]
    assert threads, "thematic cluster must produce at least one evidence thread"

    thread_source_ids: set[str] = set()
    for thread in threads:
        assert thread.get("thread_id")
        assert thread.get("title")
        assert thread.get("summary")
        for source_id in thread.get("source_ids", []) or []:
            thread_source_ids.add(str(source_id))
        for evidence_row in thread.get("evidence", []) or []:
            source_id = evidence_row.get("source_id") if isinstance(evidence_row, dict) else None
            if source_id:
                thread_source_ids.add(str(source_id))

    core_source_ids = set(cluster["core_source_ids"])
    missing_core_sources = core_source_ids - thread_source_ids
    assert not missing_core_sources, (
        f"Core sources with no EvidenceThread representation: {sorted(missing_core_sources)}. "
        f"Threads covered: {sorted(thread_source_ids)}."
    )
