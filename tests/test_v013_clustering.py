from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten import literature
from auto_zettelkasten.literature import (
    _CheckpointedReasonerCalls,
    _cluster_plan_call_settings,
    _cluster_planning_card,
    _cluster_projection_is_publishable,
    _cluster_relationship_context,
    _global_plan_proposals,
    _project_planned_cluster_neighbors,
    build_literature_report,
    map_overlapping_clusters,
    normalize_evidence_profiles,
)
from auto_zettelkasten.readers import _validate_literature_response
from auto_zettelkasten.relationships import RELATIONSHIP_DECISION_CONTRACT


def _profile(source_id: str, *, partial: bool = False) -> dict:
    coverage = "limited_text" if partial else "full_text"
    status = "limited" if partial else "supported"
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_status": (
            "partial_document_atomic_note" if partial else "analytical_atomic_note"
        ),
        "evidence_eligibility": "substantive_bounded",
        "title": f"Source {source_id}",
        "thesis": f"Thesis for {source_id}",
        "methods": ["comparative analysis"],
        "study_family_id": source_id,
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"{source_id}-anchor-{index}",
                "claim": f"Located proposition {index} for {source_id}.",
                "locator": f"p. {index}",
                "planning_roles": [role],
                "salience_priority": 10 - index,
                "support_envelope": {
                    "coverage": coverage,
                    "support_status": status,
                    "empirical_role": "descriptive",
                    "argument_role": "none",
                },
            }
            for index, role in enumerate(
                (
                    "thesis",
                    "method",
                    "major_finding",
                    "mechanism",
                    "limitation",
                    "literature_position",
                ),
                start=1,
            )
        ],
    }


def test_partial_substantive_profile_contributes_bounded_planning_anchors() -> None:
    normalized = normalize_evidence_profiles([_profile("partial", partial=True)])[0]

    assert normalized["analytical"] is True
    assert normalized["evidence_eligibility"] == "substantive_bounded"
    card = _cluster_planning_card(normalized)
    assert 3 <= len(card["evidence_references"]) <= 5
    assert {row["planning_roles"][0] for row in card["evidence_references"]} == {
        "thesis",
        "method",
        "major_finding",
        "mechanism",
        "limitation",
    }
    assert all(
        row["support_boundary"]["coverage"] == "limited_text"
        for row in card["evidence_references"]
    )


def test_global_plan_uses_only_source_owned_member_and_neighbor_anchors() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    response = {
        "clusters": [
            {
                "cluster_id": "planned-one",
                "title": "One debate",
                "shared_question": "How do the sources address the debate?",
                "coherence_rationale": "Both sources directly address the debate.",
                "members": [
                    {
                        "source_id": "a",
                        "role": "core",
                        "evidence_anchor_ids": ["a-anchor-1"],
                        "membership_reason": "A supplies the thesis.",
                    },
                    {
                        "source_id": "b",
                        "role": "core",
                        "evidence_anchor_ids": ["b-anchor-1"],
                        "membership_reason": "B supplies the thesis.",
                    },
                ],
            }
        ],
        "neighbor_relationships": [
            {
                "left_cluster_id": "planned-one",
                "right_cluster_id": "missing-cluster",
                "relationship": "invalid neighbor",
                "basis_source_ids": ["a"],
                "evidence_anchor_ids": ["a-anchor-1"],
            }
        ],
    }

    proposals, parked, neighbors, unclustered = _global_plan_proposals(
        response, profiles
    )

    assert not parked
    assert not neighbors
    assert not unclustered
    assert proposals[0]["formation_route"] == "global_cluster_plan"
    assert proposals[0]["source_roles"] == {"a": "core", "b": "core"}
    assert {
        row["evidence_anchor_id"]
        for row in proposals[0]["supporting_evidence"]
    } == {"a-anchor-1", "b-anchor-1"}


def test_relationship_first_global_plan_requires_verified_pair_connectivity() -> None:
    profiles = normalize_evidence_profiles(
        [_profile(source_id) for source_id in ("a", "b", "c", "x", "y")]
    )
    response = {
        "clusters": [
            {
                "cluster_id": "connected",
                "title": "Connected measurement family",
                "shared_question": "How is one bounded construct measured?",
                "coherence_rationale": "The studies compare one bounded construct.",
                "members": [
                    {
                        "source_id": source_id,
                        "role": "core",
                        "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                    }
                    for source_id in ("a", "b", "c")
                ],
            },
            {
                "cluster_id": "unsupported",
                "title": "Topical adjacency only",
                "shared_question": "Are these works one family?",
                "coherence_rationale": "The planner grouped the titles.",
                "members": [
                    {
                        "source_id": source_id,
                        "role": "core",
                        "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                    }
                    for source_id in ("x", "y")
                ],
            },
        ]
    }
    accepted = [
        {
            "relation_id": relation_id,
            "source_ids": [left, right],
            "relation_type": "complements",
            "reason": "The pair supports a bounded comparison.",
            "cluster_evidence_eligible": True,
            "evidence": [
                {
                    "source_id": source_id,
                    "evidence_anchor_id": f"{source_id}-anchor-2",
                }
                for source_id in (left, right)
            ],
        }
        for relation_id, left, right in (
            ("relationship-a-b", "a", "b"),
            ("relationship-b-c", "b", "c"),
        )
    ]
    accepted.append(
        {
            "relation_id": "citation-x-y",
            "source_ids": ["x", "y"],
            "relation_type": "cites",
            "cluster_evidence_eligible": False,
        }
    )

    proposals, _, _, _ = _global_plan_proposals(
        response,
        profiles,
        accepted_relationships=accepted,
    )
    mapped = map_overlapping_clusters(profiles, proposals=proposals)

    assert len(mapped["clusters"]) == 1
    assert mapped["clusters"][0]["core_source_ids"] == ["a", "b", "c"]
    assert mapped["clusters"][0]["relation_ids"] == [
        "relationship-a-b",
        "relationship-b-c",
    ]
    assert all(
        len(row["source_ids"]) == 2
        for row in mapped["clusters"][0]["family_relations"]
    )
    assert {
        reference["evidence_anchor_id"]
        for row in mapped["clusters"][0]["family_relations"]
        for reference in row["evidence"]
    } == {"a-anchor-2", "b-anchor-2", "c-anchor-2"}
    assert any(
        row["proposal_id"] == "unsupported"
        and row["reason"] == "no_valid_connected_family_relation"
        for row in mapped["rejected_proposals"]
    )


def test_relationship_first_contextual_edge_does_not_create_core_only_cluster() -> None:
    profiles = normalize_evidence_profiles([_profile("a"), _profile("b")])
    response = {
        "clusters": [
            {
                "cluster_id": "context-only",
                "title": "Adjacent questions",
                "shared_question": "How are these adjacent questions connected?",
                "coherence_rationale": "The sources supply neighboring context.",
                "members": [
                    {
                        "source_id": source_id,
                        "role": "core",
                        "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                    }
                    for source_id in ("a", "b")
                ],
            }
        ]
    }
    accepted = [
        {
            "relation_id": "relationship-a-b",
            "source_ids": ["a", "b"],
            "relation_type": "contextual_connection",
            "reason": "The sources address neighboring stages without a shared finding.",
            "cluster_evidence_eligible": True,
            "evidence": [
                {
                    "source_id": source_id,
                    "evidence_anchor_id": f"{source_id}-anchor-2",
                }
                for source_id in ("a", "b")
            ],
        }
    ]

    proposals, _, _, _ = _global_plan_proposals(
        response,
        profiles,
        accepted_relationships=accepted,
    )
    mapped = map_overlapping_clusters(profiles, proposals=proposals)

    assert mapped["clusters"] == []
    assert mapped["rejected_proposals"][0]["reason"] == (
        "no_valid_connected_family_relation"
    )


def test_relationship_first_contextual_edge_retains_non_core_neighbor() -> None:
    profiles = normalize_evidence_profiles(
        [_profile(source_id) for source_id in ("a", "b", "c")]
    )
    response = {
        "clusters": [
            {
                "cluster_id": "connected-with-context",
                "title": "Bounded core with adjacent context",
                "shared_question": "How is the bounded core qualified?",
                "coherence_rationale": "A and B form the core; C supplies context.",
                "members": [
                    {
                        "source_id": source_id,
                        "role": "bridge" if source_id == "c" else "core",
                        "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                    }
                    for source_id in ("a", "b", "c")
                ],
            }
        ]
    }
    accepted = [
        {
            "relation_id": relation_id,
            "source_ids": [left, right],
            "relation_type": relation_type,
            "reason": "Verified bounded connection.",
            "cluster_evidence_eligible": True,
            "evidence": [
                {
                    "source_id": source_id,
                    "evidence_anchor_id": f"{source_id}-anchor-2",
                }
                for source_id in (left, right)
            ],
        }
        for relation_id, left, right, relation_type in (
            ("relationship-a-b", "a", "b", "complements"),
            ("relationship-b-c", "b", "c", "contextual_connection"),
        )
    ]

    proposals, _, _, _ = _global_plan_proposals(
        response,
        profiles,
        accepted_relationships=accepted,
    )
    mapped = map_overlapping_clusters(profiles, proposals=proposals)

    assert mapped["clusters"][0]["core_source_ids"] == ["a", "b"]
    assert mapped["clusters"][0]["context_source_ids"] == ["c"]
    assert mapped["clusters"][0]["relation_ids"] == ["relationship-a-b"]


def test_relationship_first_cluster_discards_unconnected_auxiliary_member() -> None:
    profiles = normalize_evidence_profiles(
        [_profile(source_id) for source_id in ("a", "b", "c")]
    )
    response = {
        "clusters": [
            {
                "cluster_id": "connected",
                "title": "Connected pair",
                "shared_question": "How is one bounded construct measured?",
                "coherence_rationale": "A and B support the comparison.",
                "members": [
                    {
                        "source_id": source_id,
                        "role": "context" if source_id == "c" else "core",
                        "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                    }
                    for source_id in ("a", "b", "c")
                ],
            }
        ]
    }
    proposals, _, _, _ = _global_plan_proposals(
        response,
        profiles,
        accepted_relationships=[
            {
                "relation_id": "relationship-a-b",
                "source_ids": ["a", "b"],
                "relation_type": "complements",
                "cluster_evidence_eligible": True,
                "evidence": [
                    {
                        "source_id": source_id,
                        "evidence_anchor_id": f"{source_id}-anchor-2",
                    }
                    for source_id in ("a", "b")
                ],
            }
        ],
    )

    mapped = map_overlapping_clusters(profiles, proposals=proposals)

    assert mapped["clusters"][0]["source_ids"] == ["a", "b"]
    assert mapped["clusters"][0]["context_source_ids"] == []
    assert any(
        row["source_id"] == "c" for row in mapped["unclustered_sources"]
    )


def test_member_without_planner_anchor_uses_source_owned_profile_evidence() -> None:
    profiles = normalize_evidence_profiles(
        [_profile("a"), _profile("b"), _profile("c")]
    )
    response = {
        "clusters": [
            {
                "cluster_id": "planned-one",
                "title": "One debate",
                "shared_question": "How do the sources address the debate?",
                "members": [
                    {
                        "source_id": "a",
                        "role": "core",
                        "evidence_anchor_ids": ["a-anchor-1"],
                    },
                    {
                        "source_id": "b",
                        "role": "core",
                        "evidence_anchor_ids": ["b-anchor-1"],
                    },
                    {
                        "source_id": "c",
                        "role": "context",
                        "evidence_anchor_ids": [],
                    },
                ],
            }
        ]
    }

    proposals, parked, _neighbors, unclustered = _global_plan_proposals(
        response, profiles
    )

    assert not parked
    assert proposals[0]["source_ids"] == ["a", "b", "c"]
    assert unclustered == []


def test_cluster_plan_validation_parks_only_the_bad_sibling() -> None:
    result = _validate_literature_response(
        {
            "clusters": [
                {
                    "cluster_id": "valid",
                    "title": "Valid cluster",
                    "shared_question": "What is the relationship?",
                    "members": [
                        {
                            "source_id": "a",
                            "role": "core",
                            "evidence_anchor_ids": ["a-1"],
                            "membership_reason": "Direct evidence.",
                        },
                        {
                            "source_id": "b",
                            "role": "core",
                            "evidence_anchor_ids": ["b-1"],
                            "membership_reason": "Direct evidence.",
                        },
                    ],
                },
                {"cluster_id": "bad", "title": "Missing members"},
            ],
            "neighbor_relationships": [],
            "unclustered_sources": [],
        },
        kind="cluster_plan",
    )

    assert [row["cluster_id"] for row in result["clusters"]] == ["valid"]
    assert result["parked_clusters"] == [
        {"cluster_id": "bad", "reason": "incomplete_cluster_plan_row"}
    ]


class _PlanReasoner:
    name = "deepseek"
    model = "deepseek-v4-flash"
    max_output_tokens = 32_000
    context_window_tokens = 1_000_000
    prompt_reserve_tokens = 2_048

    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> dict:
        return {
            "context_window_tokens": 1_000_000,
            "supported_output_tokens": 64_000,
            "request_deadline_seconds": 600,
            "capability_identity": "deepseek-v4-flash-test",
        }

    def plan_clusters(self, profiles, request, *, context=None):
        self.calls += 1
        return {
            "clusters": [],
            "neighbor_relationships": [],
            "unclustered_sources": [],
        }


def test_global_plan_settings_and_checkpoint_replay_are_capability_bound(
    tmp_path: Path,
) -> None:
    reasoner = _PlanReasoner()
    request = {
        "source_set_id": "set",
        "literature_policy": {
            "max_synthesis_calls": 100,
            "literature_deadline_seconds": 7_200,
        },
    }
    settings = _cluster_plan_call_settings(reasoner, request, card_count=122)
    assert settings["output_tokens"] == 64_000
    assert settings["deadline_seconds"] == 600
    assert (
        _cluster_plan_call_settings(reasoner, request, card_count=24)[
            "output_tokens"
        ]
        == 64_000
    )
    context = {"cluster_plan_settings": settings}

    first = _CheckpointedReasonerCalls(
        tmp_path, "run", reasoner, request
    )
    assert first(
        "cluster_plan", "collection", "plan_clusters", [], context
    )["clusters"] == []
    assert reasoner.calls == 1

    replay = _CheckpointedReasonerCalls(
        tmp_path, "run", reasoner, request
    )
    assert replay(
        "cluster_plan", "collection", "plan_clusters", [], context
    )["clusters"] == []
    assert reasoner.calls == 1
    assert replay.checkpoint_hits == 1


def test_cluster_plan_checkpoint_ignores_machine_projection_feedback(
    tmp_path: Path,
) -> None:
    reasoner = _PlanReasoner()
    request = {
        "source_set_id": "set",
        "literature_policy": {"max_synthesis_calls": 100},
    }
    first = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    first(
        "cluster_plan",
        "collection",
        "plan_clusters",
        [],
        {
            "accepted_relationships": [],
            "literature_positions": [],
            "collection_identity": {"source_set_id": "set"},
            "prior_clusters": [{"cluster_id": "machine-output"}],
            "cluster_plan_mode": "collection",
            "cluster_plan_settings": {
                "input_char_budget": 100_000,
                "serialized_input_chars": 10_000,
            },
        },
    )

    replay = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    replay(
        "cluster_plan",
        "collection",
        "plan_clusters",
        [],
        {
            "accepted_relationships": [],
            "literature_positions": [],
            "collection_identity": {"source_set_id": "set"},
            "cluster_plan_mode": "collection",
            "cluster_plan_settings": {
                "input_char_budget": 100_000,
                "serialized_input_chars": 8_000,
            },
        },
    )

    assert reasoner.calls == 1
    assert replay.checkpoint_hits == 1


def test_global_plan_uses_the_provider_output_limit_before_packetization() -> None:
    class _LimitedPlanReasoner(_PlanReasoner):
        @property
        def capabilities(self) -> dict:
            return {
                "context_window_tokens": 1_000_000,
                "supported_output_tokens": 16_000,
                "request_deadline_seconds": 600,
            }

    settings = _cluster_plan_call_settings(
        _LimitedPlanReasoner(), {}, card_count=122
    )

    assert settings["output_tokens"] == 16_000
    assert settings["input_char_budget"] > 0


def test_legacy_review_pending_relationships_do_not_enter_cluster_context() -> None:
    relationships = [
        {
            "relation_id": "legacy",
            "source_id": "a",
            "target_source_id": "b",
            "active": True,
            "verification_status": "confirmed",
            "legacy_review_pending": True,
        },
        {
            "relation_id": "verified",
            "source_id": "a",
            "target_source_id": "b",
            "active": True,
            "verification_status": "confirmed",
        },
    ]

    assert [
        row["relation_id"]
        for row in _cluster_relationship_context(relationships, {"a", "b"})
    ] == ["verified"]


def test_v9_cluster_context_requires_source_owned_anchor_evidence() -> None:
    base = {
        "source_id": "a",
        "target_source_id": "b",
        "active": True,
        "verification_status": "final",
        "relation_type": "supports",
        "output_contract": RELATIONSHIP_DECISION_CONTRACT,
        "cluster_evidence_eligible": True,
    }
    relationships = [
        {
            **base,
            "relation_id": "claim-only",
            "source_evidence": {"source_id": "a", "claim": "A claim"},
            "target_evidence": {"source_id": "b", "claim": "B claim"},
        },
        {
            **base,
            "relation_id": "owned-anchors",
            "source_evidence": {
                "source_id": "a",
                "evidence_anchor_id": "a-1",
                "claim": "A claim",
            },
            "target_evidence": {
                "source_id": "b",
                "evidence_anchor_id": "b-1",
                "claim": "B claim",
            },
        },
    ]

    context = _cluster_relationship_context(relationships, {"a", "b"})

    assert [row["relation_id"] for row in context] == ["owned-anchors"]
    assert [
        (row["source_id"], row["evidence_anchor_id"])
        for row in context[0]["evidence"]
    ] == [("a", "a-1"), ("b", "b-1")]


def test_planned_cluster_neighbors_project_reciprocally() -> None:
    evidence = [
        {"source_id": "a", "evidence_anchor_id": "a-1", "locator": "p. 1"},
        {"source_id": "b", "evidence_anchor_id": "b-1", "locator": "p. 2"},
    ]
    clusters = [
        {
            "cluster_id": "left",
            "source_ids": ["a"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "right",
                    "relationship": "Different stages of the same problem.",
                    "evidence": evidence,
                }
            ],
        },
        {
            "cluster_id": "right",
            "source_ids": ["b"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "left",
                    "relationship": "Different stages of the same problem.",
                    "evidence": evidence,
                }
            ],
        },
    ]
    syntheses = {"left": {"related_clusters": []}, "right": {"related_clusters": []}}

    _project_planned_cluster_neighbors(clusters, syntheses)

    left = syntheses["left"]["related_clusters"][0]
    right = syntheses["right"]["related_clusters"][0]
    assert left["target_cluster_id"] == "right"
    assert right["target_cluster_id"] == "left"
    assert left["relationship_id"] == right["relationship_id"]


class _GlobalOnlyReasoner:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.synthesis_calls = 0

    @property
    def capabilities(self) -> dict:
        return {
            "context_window_tokens": 1_000_000,
            "supported_output_tokens": 64_000,
            "request_deadline_seconds": 600,
            "capability_identity": "global-only",
        }

    def plan_clusters(self, profiles, request, *, context=None):
        self.plan_calls += 1
        return {
            "clusters": [
                {
                    "cluster_id": "planned-debate",
                    "title": "Planned debate",
                    "semantic_identity": "planned debate",
                    "shared_question": "How do these sources address the debate?",
                    "bounded_object": "planned debate",
                    "coherence_rationale": "Both sources centrally address it.",
                    "members": [
                        {
                            "source_id": source_id,
                            "role": "core",
                            "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                            "membership_reason": "The source directly addresses it.",
                        }
                        for source_id in ("a", "b")
                    ],
                }
            ],
            "neighbor_relationships": [],
            "unclustered_sources": [],
        }

    def propose_clusters(self, *args, **kwargs):
        raise AssertionError("the legacy proposal loop must not run")

    def synthesize_cluster(self, *args, **kwargs):
        self.synthesis_calls += 1
        raise RuntimeError("one cluster failed")


def test_capable_reasoner_bypasses_legacy_loop_and_parks_failed_cluster() -> None:
    reasoner = _GlobalOnlyReasoner()
    report = build_literature_report(
        [_profile("a"), _profile("b")],
        reasoner=reasoner,
        request={"source_set_id": "set", "literature_policy": {}},
        source_set={"source_set_id": "set", "collection_name": "Test"},
    )

    assert reasoner.plan_calls == 1
    assert reasoner.synthesis_calls == 1
    assert len(report["cluster_registry"]["clusters"]) == 1
    synthesis = next(iter(report["cluster_syntheses"].values()))
    assert synthesis["status"] == "partial"
    assert synthesis["parked_for_review"] is True


class _WarningBearingGlobalReasoner(_GlobalOnlyReasoner):
    def synthesize_cluster(self, profiles, request, *, context=None):
        self.synthesis_calls += 1
        cluster = context["cluster"]
        evidence = [
            {
                "source_id": profile["source_id"],
                "evidence_anchor_id": profile["evidence_anchors"][0][
                    "evidence_anchor_id"
                ],
                "locator": profile["evidence_anchors"][0]["locator"],
            }
            for profile in profiles
        ]
        return {
            "cluster_id": cluster["cluster_id"],
            "scope": cluster["shared_question"],
            "boundaries": [],
            "coherence_rationale": "Both sources directly address the bounded question.",
            "synthesis": (
                "Both sources identify a comparable pattern while using distinct cases "
                "and methods. Their agreement supplies a coherent answer to the shared "
                "question. The cluster therefore records a recurring empirical pattern."
            ),
            "debate_state": "qualified_agreement",
            "central_findings": [
                {
                    "finding": "The sources report a comparable bounded pattern.",
                    "proposition_id": cluster["proposition_ids"][0],
                    "evidence": evidence,
                }
            ],
            "source_contributions": [
                {
                    "source_id": profiles[0]["source_id"],
                    "finding": "The first source supplies bounded evidence.",
                    "evidence": [evidence[0]],
                }
            ],
        }


def test_global_plan_publishes_usable_synthesis_with_advisory_quality_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = _WarningBearingGlobalReasoner()
    profiles = [_profile("a"), _profile("b")]
    profiles[0]["sample_id"] = "sample-a"
    profiles[1]["sample_id"] = "sample-b"
    original_validator = literature.validate_cluster_synthesis

    def advisory_validator(*args, **kwargs):
        result = original_validator(*args, **kwargs)
        result.update(
            status="partial",
            quality_status="incomplete",
            quality_errors=["advisory_quality_warning"],
        )
        return result

    monkeypatch.setattr(literature, "validate_cluster_synthesis", advisory_validator)
    report = build_literature_report(
        profiles,
        reasoner=reasoner,
        request={"source_set_id": "set", "literature_policy": {}},
        source_set={"source_set_id": "set", "collection_name": "Test"},
    )

    synthesis = next(iter(report["cluster_syntheses"].values()))
    assert reasoner.synthesis_calls == 1
    assert synthesis["status"] == "reasoned"
    assert synthesis["quality_status"] == "warning"
    assert _cluster_projection_is_publishable(synthesis)
    assert synthesis["quality_warnings"] == ["advisory_quality_warning"]


@pytest.mark.parametrize(
    "fatal_error",
    [
        "retained_member_without_specific_finding",
        "study_finding_cross_owned_evidence",
    ],
)
def test_global_plan_keeps_fatal_membership_synthesis_parked(
    monkeypatch: pytest.MonkeyPatch,
    fatal_error: str,
) -> None:
    reasoner = _WarningBearingGlobalReasoner()
    original_validator = literature.validate_cluster_synthesis

    def fatal_validator(*args, **kwargs):
        result = original_validator(*args, **kwargs)
        result.update(
            status="partial",
            quality_status="incomplete",
            quality_errors=[fatal_error],
            parked_for_review=True,
        )
        return result

    monkeypatch.setattr(literature, "validate_cluster_synthesis", fatal_validator)
    report = build_literature_report(
        [_profile("a"), _profile("b")],
        reasoner=reasoner,
        request={"source_set_id": "set", "literature_policy": {}},
        source_set={"source_set_id": "set", "collection_name": "Test"},
    )

    synthesis = next(iter(report["cluster_syntheses"].values()))
    assert synthesis["status"] == "partial"
    assert synthesis["parked_for_review"] is True
    assert not _cluster_projection_is_publishable(synthesis)


class _ShardedPlanReasoner:
    def __init__(self) -> None:
        self.plan_modes: list[str] = []
        self.packet_sizes: list[int] = []
        self.synthesis_calls = 0

    @property
    def capabilities(self) -> dict:
        return {
            "context_window_tokens": 80_000,
            "supported_output_tokens": 16_000,
            "request_deadline_seconds": 600,
            "capability_identity": "bounded-shard-test",
        }

    def plan_clusters(self, profiles, request, *, context=None):
        mode = str((context or {}).get("cluster_plan_mode") or "")
        self.plan_modes.append(mode)
        self.packet_sizes.append(len(profiles))
        if mode == "bridge":
            return {
                "clusters": [],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }
        source_ids = [
            str(profile.get("source_id") or "")
            for profile in profiles
            if profile.get("source_id")
        ]
        assert len(source_ids) >= 2
        return {
            "clusters": [
                {
                    "cluster_id": "local-family",
                    "title": f"Local family for {source_ids[0]}",
                    "semantic_identity": f"local family {source_ids[0]}",
                    "shared_question": "How do these sources address the question?",
                    "bounded_object": "A bounded local debate",
                    "coherence_rationale": "The first two sources directly address it.",
                    "members": [
                        {
                            "source_id": source_id,
                            "role": "core",
                            "evidence_anchor_ids": [f"{source_id}-anchor-1"],
                            "membership_reason": "The thesis addresses the question.",
                        }
                        for source_id in source_ids[:2]
                    ],
                }
            ],
            "neighbor_relationships": [],
            "unclustered_sources": [
                {
                    "source_id": source_id,
                    "reason": "Not central to this local family.",
                }
                for source_id in source_ids[2:]
            ],
        }

    def synthesize_cluster(self, *args, **kwargs):
        self.synthesis_calls += 1
        raise RuntimeError("isolated synthesis failure")


def test_large_global_plan_falls_back_to_bounded_shards_and_one_family_pass() -> None:
    profiles = [_profile(f"source-{index:02d}") for index in range(30)]
    for profile in profiles:
        for anchor in profile["evidence_anchors"]:
            anchor["claim"] = (
                f"{anchor['claim']} "
                + "Substantive evidence and boundary conditions. " * 24
            )
    shards = [
        {
            "literature_id": f"literature-{index}",
            "shard_id": f"catalogue-{index}",
            "source_ids": [
                f"source-{source_index:02d}"
                for source_index in range(index * 10, (index + 1) * 10)
            ],
        }
        for index in range(3)
    ]
    reasoner = _ShardedPlanReasoner()

    report = build_literature_report(
        profiles,
        reasoner=reasoner,
        request={"source_set_id": "large", "literature_policy": {}},
        source_set={"source_set_id": "large", "collection_name": "Large"},
        catalogue_shards=shards,
    )

    assert "collection" not in reasoner.plan_modes
    assert reasoner.plan_modes[-1] == "bridge"
    assert 2 <= reasoner.plan_modes.count("shard") <= 8
    assert reasoner.plan_modes.count("bridge") == 1
    assert max(reasoner.packet_sizes[:-1]) < len(profiles)
    assert reasoner.synthesis_calls == reasoner.plan_modes.count("shard")
    assert len(report["cluster_registry"]["clusters"]) == reasoner.synthesis_calls
    assert all(
        synthesis["parked_for_review"] is True
        for synthesis in report["cluster_syntheses"].values()
    )


def test_large_global_plan_uses_one_call_when_it_fits_context() -> None:
    class HighContextReasoner(_ShardedPlanReasoner):
        @property
        def capabilities(self) -> dict:
            return {
                "context_window_tokens": 1_000_000,
                "supported_output_tokens": 64_000,
                "request_deadline_seconds": 600,
                "capability_identity": "large-collection-shard-test",
            }

    profiles = [_profile(f"source-{index:03d}") for index in range(121)]
    shard_sizes = [50, 16, 49, 6]
    start = 0
    shards = []
    for index, size in enumerate(shard_sizes):
        shards.append(
            {
                "literature_id": f"literature-{index}",
                "shard_id": f"catalogue-{index}",
                "source_ids": [
                    f"source-{source_index:03d}"
                    for source_index in range(start, start + size)
                ],
            }
        )
        start += size
    reasoner = HighContextReasoner()

    build_literature_report(
        profiles,
        reasoner=reasoner,
        request={"source_set_id": "large", "literature_policy": {}},
        source_set={"source_set_id": "large", "collection_name": "Large"},
        catalogue_shards=shards,
    )

    assert reasoner.plan_modes == ["global"]
    assert reasoner.packet_sizes == [121]
