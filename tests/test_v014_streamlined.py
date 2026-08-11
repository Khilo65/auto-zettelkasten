from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import _compact_cluster_catalogue
from auto_zettelkasten.literature import (
    _CheckpointedReasonerCalls,
    _cluster_writer_member_roles,
    _cluster_synthesis_profile_projection,
    _cluster_markdown,
    _prune_stale_generated_markdown,
    _project_planned_cluster_neighbors,
    _source_notes_with_custody_relations,
    _write_markdown_with_quality_ratchet,
    build_literature_report,
    normalize_evidence_profiles,
    validate_streamlined_cluster_synthesis,
)
from auto_zettelkasten.migration import (
    V014_MIGRATION_ID,
    migrate_v014_schema,
)
from auto_zettelkasten.models import (
    LiteratureMapRequest,
    MapRequest,
    ProcessingPolicy,
    RelationshipPairJob,
)
from auto_zettelkasten.pipeline import _cluster_catalogue_rows, _fingerprint
from auto_zettelkasten.readers import (
    _cluster_plan_system_prompt,
    _cluster_synthesis_system_prompt,
    _relationship_adjudication_system_prompt,
    _validate_streamlined_cluster_response,
)
from auto_zettelkasten.workspace import initialize


def _profile(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_status": "analytical_atomic_note",
        "evidence_eligibility": "substantive_bounded",
        "title": f"Source {source_id}",
        "thesis": f"Thesis for {source_id}.",
        "methods": ["comparative analysis"],
        "study_family_id": source_id,
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id}",
                "source_id": source_id,
                "claim": f"Finding for {source_id}.",
                "locator": "p. 10",
                "planning_roles": ["major_finding"],
                "salience_priority": 10,
                "support_envelope": {
                    "coverage": "full_text",
                    "support_status": "supported",
                    "empirical_role": "descriptive",
                    "argument_role": "none",
                },
            }
        ],
    }


def _streamlined_response(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_ids = [str(row["source_id"]) for row in profiles]
    return {
        "cluster_contract": "streamlined-full-note-v1",
        "cluster_id": str(cluster["cluster_id"]),
        "title": str(cluster.get("label") or "Mapped findings"),
        "organizing_mode": "question",
        "organizing_problem": "How do the studies explain the outcome?",
        "status": "accepted",
        "retained_member_ids": source_ids,
        "dropped_members": [],
        "lines_of_inquiry": [
            {
                "title": "Main comparison",
                "synthesis": "The studies identify related but bounded findings.",
                "study_findings": [
                    {
                        "source_id": source_id,
                        "finding": f"{source_id} reports a source-specific finding.",
                        "method_scope": "Comparative analysis.",
                        "relation_to_line": "supports",
                        "evidence": [
                            {
                                "source_id": source_id,
                                "evidence_anchor_id": f"anchor-{source_id}",
                                "locator": "p. 10",
                            }
                        ],
                    }
                    for source_id in source_ids
                ],
            }
        ],
        "bottom_line": "The findings converge within their stated scopes.",
        "differences": [],
        "limits": ["The studies use different settings."],
        "related_clusters": [],
    }


def test_provider_concurrency_round_trips_and_validates(tmp_path: Path) -> None:
    request = MapRequest(tmp_path, provider_concurrency="auto")
    assert MapRequest.from_dict(request.to_dict()).provider_concurrency == "auto"
    literature = LiteratureMapRequest(tmp_path, provider_concurrency=7)
    assert LiteratureMapRequest.from_dict(
        literature.to_dict()
    ).provider_concurrency == 7


def test_semantic_prompts_allow_neutral_nonmembership_and_bounded_relations() -> None:
    planning = _cluster_plan_system_prompt()
    synthesis = _cluster_synthesis_system_prompt()
    relationships = _relationship_adjudication_system_prompt()

    assert "any number of sources may remain unclustered" in planning.casefold()
    assert "do not create weak clusters or memberships" in planning.casefold()
    assert "Do not stop after the first few families" in planning
    assert "statistical significance" in synthesis
    assert "topical, lexical, or generic" in relationships
    assert "extends requires explicit building on" in relationships
    assert "shared data" in relationships
    assert "Do not infer intellectual direction or support" in relationships
    assert "self-review once" in relationships


def test_mapped_literature_position_projects_explicit_citation(tmp_path: Path) -> None:
    for match_status in ("mapped", "matched"):
        write_yaml(
            tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml",
            {
                "positions": [
                    {
                        "current_source_id": "A",
                        "matched_source_id": "B",
                        "match_status": match_status,
                    }
                ]
            },
        )

        notes = _source_notes_with_custody_relations(
            tmp_path,
            [{"source_id": "A"}, {"source_id": "B"}],
        )
        by_source = {row["source_id"]: row for row in notes}

        assert by_source["A"]["custody_relations"]["cites"] == ["B"]
        assert by_source["B"]["custody_relations"]["cited_by"] == ["A"]


def test_streamlined_cluster_normalizes_harmless_mode_formatting() -> None:
    response = _streamlined_response(
        {"cluster_id": "cluster-one", "label": "Cluster One"},
        [_profile("A"), _profile("B")],
    )
    response["organizing_mode"] = "Historical Problem"

    assert _validate_streamlined_cluster_response(response)[
        "organizing_mode"
    ] == "historical_problem"


def test_streamlined_cluster_normalizes_optional_collection_shapes() -> None:
    response = _streamlined_response(
        {"cluster_id": "cluster-one", "label": "Cluster One"},
        [_profile("A"), _profile("B")],
    )
    response["differences"] = "The studies use different measures."
    response["limits"] = "The cases do not cover every conflict."
    response["related_clusters"] = None
    response["dropped_members"] = None

    normalized = _validate_streamlined_cluster_response(response)

    assert normalized["differences"] == [
        {"difference": "The studies use different measures."}
    ]
    assert normalized["limits"] == ["The cases do not cover every conflict."]
    assert normalized["related_clusters"] == []
    assert normalized["dropped_members"] == []


def test_streamlined_cluster_ignores_harmless_optional_shape_drift() -> None:
    response = _streamlined_response(
        {"cluster_id": "cluster-one", "label": "Cluster One"},
        [_profile("A"), _profile("B")],
    )
    response["organizing_mode"] = "thematic synthesis"
    response["organizing_problem"] = ""
    response["guiding_question"] = "How do these studies fit together?"
    response["split_proposals"] = "No split needed"
    response["missing_member_ids"] = None
    response["dropped_members"] = ["B"]

    normalized = _validate_streamlined_cluster_response(response)

    assert normalized["organizing_mode"] == "thematic_synthesis"
    assert normalized["organizing_problem"] == "How do these studies fit together?"
    assert normalized["split_proposals"] == []
    assert normalized["missing_member_ids"] == []
    assert normalized["dropped_members"] == [{"source_id": "B"}]


def test_streamlined_cluster_filters_nonmember_missing_ids() -> None:
    profiles = [_profile("A"), _profile("B")]
    cluster = {
        "cluster_id": "cluster-one",
        "label": "Cluster One",
        "source_ids": ["A", "B"],
    }
    response = _streamlined_response(cluster, profiles)
    response["missing_member_ids"] = ["A", "external-source-candidate"]
    response["related_clusters"] = [{"cluster_id": "cluster-two"}]

    validated = validate_streamlined_cluster_synthesis(
        response, cluster, profiles
    )

    assert validated["missing_member_ids"] == ["A"]
    assert validated["related_clusters"][0]["target_cluster_id"] == "cluster-two"
    assert "unknown_missing_member_ignored" in validated["quality_warnings"]


def test_streamlined_cluster_rebinds_locator_only_finding_evidence() -> None:
    profiles = [_profile("A"), _profile("B")]
    for profile in profiles:
        profile["claims"] = list(profile["evidence_anchors"])
    cluster = {
        "cluster_id": "cluster-one",
        "label": "Cluster One",
        "source_ids": ["A", "B"],
    }
    response = _streamlined_response(cluster, profiles)
    response["lines_of_inquiry"][0]["study_findings"][0]["evidence"] = "p. 10"
    response["lines_of_inquiry"][0]["study_findings"][1]["evidence"] = "p. 999"

    normalized = _validate_streamlined_cluster_response(response)
    validated = validate_streamlined_cluster_synthesis(
        normalized, cluster, profiles
    )

    assert normalized["lines_of_inquiry"][0]["study_findings"][0][
        "evidence"
    ] == [{"source_id": "A", "locator": "p. 10"}]
    assert validated["status"] == "partial"
    assert validated["quality_errors"] == ["cluster_requires_two_retained_members"]
    assert validated["retained_member_ids"] == ["A"]
    assert set(validated["quality_warnings"]) == {
        "retained_member_without_specific_finding_removed",
        "study_finding_evidence_unresolved",
        "study_finding_requires_source_owned_evidence",
    }
    assert validated["source_contributions"][0]["evidence"][0][
        "evidence_anchor_id"
    ] == "anchor-A"


def test_streamlined_cluster_ignores_only_cross_owned_evidence_rows() -> None:
    profiles = [_profile("A"), _profile("B"), _profile("C")]
    cluster = {"cluster_id": "cluster-one", "source_ids": ["A", "B", "C"]}
    response = _streamlined_response(cluster, profiles)
    findings = response["lines_of_inquiry"][0]["study_findings"]
    findings[0]["evidence"].append(
        {
            "source_id": "B",
            "evidence_anchor_id": "anchor-B",
            "locator": "p. 10",
        }
    )
    findings.append(
        {
            "source_id": "A",
            "finding": "A separate valid source-owned finding.",
            "method_scope": "Single-source analysis.",
            "relation_to_line": "supports",
            "evidence": [
                {
                    "source_id": "A",
                    "evidence_anchor_id": "anchor-A",
                    "locator": "p. 10",
                }
            ],
        }
    )
    findings.append(
        {
            "source_id": "A",
            "finding": "An invalid cross-source comparison.",
            "method_scope": "Comparative analysis.",
            "relation_to_line": "contrasts",
            "evidence": [
                {
                    "source_id": "C",
                    "evidence_anchor_id": "anchor-C",
                    "locator": "p. 10",
                }
            ],
        }
    )

    validated = validate_streamlined_cluster_synthesis(
        response, cluster, profiles
    )

    assert validated["status"] == "reasoned"
    assert validated["retained_member_ids"] == ["A", "B", "C"]
    assert all(
        len(row.get("evidence", []) or []) == 1
        for row in validated["lines_of_inquiry"][0]["study_findings"]
    )
    assert len(validated["lines_of_inquiry"][0]["study_findings"]) == 3
    assert "An invalid cross-source comparison." not in {
        row["finding"]
        for row in validated["lines_of_inquiry"][0]["study_findings"]
    }
    assert "study_finding_evidence_source_mismatch_ignored" in validated[
        "quality_warnings"
    ]


def test_oversized_cluster_is_partitioned_before_writers(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C", "D")]
    writer_sizes: list[int] = []

    class Reasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return len(projected) <= 2

        def plan_clusters(self, projected, request, *, context=None):
            assert not projected
            assert context["cluster_plan_mode"] == "partition"
            return {
                "clusters": [
                    {
                        "cluster_id": "left",
                        "title": "Left child",
                        "semantic_identity": "left child",
                        "organizing_problem": "Left problem",
                        "members": [
                            {"source_id": "A", "role": "core"},
                            {"source_id": "B", "role": "core"},
                        ],
                    },
                    {
                        "cluster_id": "right",
                        "title": "Right child",
                        "semantic_identity": "right child",
                        "organizing_problem": "Right problem",
                        "members": [
                            {"source_id": "C", "role": "core"},
                            {"source_id": "A", "role": "bridge"},
                        ],
                    },
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [
                    {"source_id": "D", "reason": "Outside both bounded questions."}
                ],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            writer_sizes.append(len(projected))
            response = _streamlined_response(context["cluster"], projected)
            response["member_roles"] = {
                row["source_id"]: "core" for row in projected
            }
            return response

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=[
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "source_scope": "full_document",
                "body": f"# {row['title']}\n\nComplete note.",
            }
            for row in profiles
        ],
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "oversized",
                    "label": "Oversized",
                    "organizing_problem": "One broad problem",
                    "source_ids": ["A", "B", "C", "D"],
                    "proposed_roles": {
                        source_id: "core" for source_id in ("A", "B", "C", "D")
                    },
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
        acquisition_ledger_path=tmp_path / "cluster_acquisition_ledger.yml",
    )

    clusters = report["cluster_registry"]["clusters"]
    assert len(clusters) == 2
    assert all(row["formation_route"] == "partitioned_cluster_plan" for row in clusters)
    assert writer_sizes == [2, 2]
    assert all(row["parent_cluster_id"] for row in clusters)
    assert all(
        row["source_backed"]
        == (row["qualification_status"] == "source_backed_cluster")
        for row in clusters
    )
    roles_by_cluster = {
        row["label"]: {
            role["source_id"]: role["role"] for role in row["source_roles"]
        }
        for row in clusters
    }
    assert roles_by_cluster["Left child"]["A"] == "core"
    assert roles_by_cluster["Right child"]["A"] == "bridge"
    assert {
        row["source_id"]
        for row in report["cluster_registry"]["unclustered_sources"]
    } == {"D"}


def test_unchanged_oversized_parent_reuses_published_children(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    plan_calls: list[bool] = []

    class Reasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return len(projected) <= 2

        def plan_clusters(self, projected, request, *, context=None):
            plan_calls.append(True)
            return {
                "clusters": [
                    {
                        "cluster_id": "left",
                        "semantic_identity": "left",
                        "members": [
                            {"source_id": "A", "role": "core"},
                            {"source_id": "B", "role": "core"},
                        ],
                    },
                    {
                        "cluster_id": "right",
                        "semantic_identity": "right",
                        "members": [
                            {"source_id": "C", "role": "core"},
                            {"source_id": "D", "role": "core"},
                        ],
                    },
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            return _streamlined_response(context["cluster"], projected)

    shared_plan = {
        "literature_families": [
            {
                "family_id": "oversized",
                "label": "Oversized",
                "organizing_problem": "One broad problem",
                "source_ids": list("ABCD"),
                "proposed_roles": {source_id: "core" for source_id in "ABCD"},
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [],
        "neighboring_families": [],
    }
    notes = [
        {
            "source_id": row["source_id"],
            "title": row["title"],
            "source_scope": "full_document",
            "body": "Complete note.",
        }
        for row in profiles
    ]
    first = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=notes,
        shared_literature_plan=shared_plan,
    )
    child_ids = {
        row["cluster_id"] for row in first["cluster_registry"]["clusters"]
    }
    plan_calls.clear()
    second = build_literature_report(
        profiles,
        previous_registry={
            **first["cluster_registry"],
            "cluster_syntheses": first["cluster_syntheses"],
        },
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=notes,
        shared_literature_plan=shared_plan,
    )

    assert plan_calls == []
    assert {
        row["cluster_id"] for row in second["cluster_registry"]["clusters"]
    } == child_ids


def test_partition_writer_uses_preserved_planner_role_class() -> None:
    warnings: list[str] = []
    roles = _cluster_writer_member_roles(
        {"member_roles": {"A": "core"}},
        {
            "partition_root_id": "parent",
            "source_roles": [{"source_id": "A", "role": "core"}],
            "candidate_roles": {"A": "bridge"},
        },
        {"A"},
        warnings,
    )

    assert roles == {"A": "bridge"}


def test_partition_checkpoint_revalidation_rejects_old_free_text_roles(
    tmp_path: Path,
) -> None:
    valid_response = {
        "clusters": [
            {
                "cluster_id": "left",
                "title": "Left child",
                "organizing_problem": "Left bounded problem",
                "coherence_rationale": "A and B address the left problem.",
                "members": [
                    {
                        "source_id": "A",
                        "role": "core",
                        "membership_reason": "Direct evidence.",
                    },
                    {
                        "source_id": "B",
                        "role": "context",
                        "membership_reason": "Boundary evidence.",
                    },
                ],
            },
            {
                "cluster_id": "right",
                "title": "Right child",
                "organizing_problem": "Right bounded problem",
                "coherence_rationale": "C and D address the right problem.",
                "members": [
                    {
                        "source_id": "C",
                        "role": "core",
                        "membership_reason": "Direct evidence.",
                    },
                    {
                        "source_id": "D",
                        "role": "context",
                        "membership_reason": "Boundary evidence.",
                    },
                ],
            },
        ],
        "neighbor_relationships": [],
        "unclustered_sources": [],
    }
    old_response = {
        **valid_response,
        "clusters": [
            {
                "cluster_id": "left",
                "title": "Left child",
                "organizing_problem": "Left bounded problem",
                "coherence_rationale": "A and B address the left problem.",
                "members": [
                    {
                        "source_id": "A",
                        "role": "core theoretical model",
                        "membership_reason": "Direct evidence.",
                    },
                    {
                        "source_id": "B",
                        "role": "bridge source",
                        "membership_reason": "Cross-child evidence.",
                    },
                ],
            },
            valid_response["clusters"][1],
        ],
    }
    context = {
        "cluster_plan_mode": "partition",
        "compact_parent_cluster": {
            "cluster_id": "parent",
            "source_ids": ["A", "B", "C", "D"],
        },
        "compact_member_cards": [
            {"source_id": source_id} for source_id in ("A", "B", "C", "D")
        ],
    }

    class Reasoner:
        name = "local"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def plan_clusters(self, profiles, request, *, context=None):
            del profiles, request, context
            self.calls += 1
            return valid_response

    reasoner = Reasoner()
    request = LiteratureMapRequest(tmp_path)
    calls = _CheckpointedReasonerCalls(tmp_path, "partition-run", reasoner, request)
    validated_response = calls(
        "cluster_plan", "partition-parent", "plan_clusters", [], context
    )
    assert [
        member["role"]
        for cluster in validated_response["clusters"]
        for member in cluster["members"]
    ] == ["core", "context", "core", "context"]

    checkpoint_path = (
        tmp_path
        / "11_state"
        / "runs"
        / "partition-run"
        / "literature"
        / "synthesis"
        / "cluster_plan"
        / "partition-parent.yml"
    )
    checkpoint = read_yaml(checkpoint_path)
    checkpoint["fingerprint"] = "old-partition-policy"
    checkpoint["response"] = old_response
    write_yaml(checkpoint_path, checkpoint)
    for path in (tmp_path / "11_state" / "semantic_jobs").rglob("*.yml"):
        path.unlink()

    reasoner.calls = 0
    replay = _CheckpointedReasonerCalls(
        tmp_path, "partition-run", reasoner, request
    )
    assert replay(
        "cluster_plan", "partition-parent", "plan_clusters", [], context
    ) == validated_response
    assert reasoner.calls == 1
    refreshed = read_yaml(checkpoint_path)
    assert "revalidated_from_provider_input_fingerprint" not in refreshed
    assert refreshed["response"] == validated_response


def test_nonshrinking_partition_never_reaches_cluster_writer(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C", "D")]
    writer_called = False

    class Reasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return False

        def plan_clusters(self, projected, request, *, context=None):
            return {
                "clusters": [
                    {
                        "cluster_id": "same",
                        "members": [
                            {"source_id": source_id, "role": "core"}
                            for source_id in ("A", "B", "C", "D")
                        ],
                    }
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            nonlocal writer_called
            writer_called = True
            return {}

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=[
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "source_scope": "full_document",
                "body": f"# {row['title']}\n\nComplete note.",
            }
            for row in profiles
        ],
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "oversized",
                    "label": "Oversized",
                    "organizing_problem": "One broad problem",
                    "source_ids": ["A", "B", "C", "D"],
                    "proposed_roles": {
                        source_id: "core" for source_id in ("A", "B", "C", "D")
                    },
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
    )

    assert not writer_called
    pending = report["cluster_registry"]["pending_revisions"]
    assert len(pending) == 1
    assert "cluster_partition_contract_invalid" in pending[0]["synthesis"][
        "quality_errors"
    ]


def test_partition_replacement_waits_for_every_child_writer(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C", "D")]
    source_notes = [
        {
            "source_id": row["source_id"],
            "title": row["title"],
            "source_scope": "full_document",
            "body": f"# {row['title']}\n\nComplete note.",
        }
        for row in profiles
    ]
    shared_plan = {
        "literature_families": [
            {
                "family_id": "oversized",
                "label": "Oversized",
                "organizing_problem": "One broad problem",
                "source_ids": ["A", "B", "C", "D"],
                "proposed_roles": {
                    source_id: "core" for source_id in ("A", "B", "C", "D")
                },
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [],
        "neighboring_families": [],
    }

    class InitialReasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return True

        def synthesize_cluster(self, projected, request, *, context=None):
            return _streamlined_response(context["cluster"], projected)

    initial = build_literature_report(
        profiles,
        reasoner=InitialReasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=source_notes,
        shared_literature_plan=shared_plan,
    )
    prior_cluster = initial["cluster_registry"]["clusters"][0]

    class PartitionReasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return len(projected) <= 2

        def plan_clusters(self, projected, request, *, context=None):
            return {
                "clusters": [
                    {
                        "cluster_id": "left",
                        "title": "Left child",
                        "members": [
                            {"source_id": "A", "role": "core"},
                            {"source_id": "B", "role": "core"},
                        ],
                    },
                    {
                        "cluster_id": "right",
                        "title": "Right child",
                        "members": [
                            {"source_id": "C", "role": "core"},
                            {"source_id": "D", "role": "core"},
                        ],
                    },
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            if {row["source_id"] for row in projected} == {"C", "D"}:
                return {}
            return _streamlined_response(context["cluster"], projected)

    repaired = build_literature_report(
        profiles,
        previous_registry={
            **initial["cluster_registry"],
            "cluster_syntheses": initial["cluster_syntheses"],
        },
        reasoner=PartitionReasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=source_notes,
        shared_literature_plan=shared_plan,
    )

    assert [
        row["cluster_id"] for row in repaired["cluster_registry"]["clusters"]
    ] == [prior_cluster["cluster_id"]]
    assert repaired["cluster_registry"]["clusters"][0]["refresh_pending"] is True
    assert repaired["cluster_registry"]["pending_revisions"][0][
        "partition_child_ids"
    ]


def test_book_chapters_count_as_one_canonical_work(tmp_path: Path) -> None:
    profiles = [_profile("A"), _profile("B"), _profile("C")]
    for index, profile in enumerate(profiles, start=1):
        profile["source_role"] = "Academic book chapter"
        profile["study_family_id"] = (
            f"doi:10.1017/cbo9780511490866.00{index}"
        )
        profile["evidence_base_counted"] = True
        profile["evidence_base_group_id"] = f"chapter-{index}"
        profile["study_lineage"] = {
            "counted_as_independent": True,
            "evidence_base_group_id": f"chapter-{index}",
            "group_basis": "study_family",
        }

    class Reasoner:
        name = "local"
        model = "test"

        def cluster_synthesis_fits(self, projected, request, *, context=None):
            return True

        def synthesize_cluster(self, projected, request, *, context=None):
            assert context["cluster"]["canonical_work_count"] == 1
            assert context["cluster"]["synthesis_lineage"] == (
                "single_work_multi_component"
            )
            return _streamlined_response(context["cluster"], projected)

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=[
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "source_scope": "full_document",
                "body": f"# {row['title']}\n\nComplete note.",
            }
            for row in profiles
        ],
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "one-book",
                    "label": "One book",
                    "source_ids": ["A", "B", "C"],
                    "proposed_roles": {"A": "core", "B": "core", "C": "core"},
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
    )

    cluster = report["cluster_registry"]["clusters"][0]
    assert cluster["canonical_work_count"] == 1
    assert cluster["independent_study_family_count"] == 1
    assert cluster["qualification_status"] == "evidence_concentrated_cluster"
    assert cluster["status"] == "evidence_concentrated_cluster"
    assert cluster["source_backed"] is False


def test_partition_neighbors_merge_reciprocal_source_owned_evidence() -> None:
    clusters = [
        {
            "cluster_id": "left",
            "source_ids": ["A"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "right",
                    "relationship": "A bounded bridge.",
                    "evidence": [
                        {
                            "source_id": "A",
                            "evidence_anchor_id": "anchor-A",
                            "locator": "p. 10",
                        }
                    ],
                }
            ],
        },
        {
            "cluster_id": "right",
            "source_ids": ["B"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "left",
                    "relationship": "A bounded bridge.",
                    "evidence": [
                        {
                            "source_id": "B",
                            "evidence_anchor_id": "anchor-B",
                            "locator": "p. 11",
                        }
                    ],
                }
            ],
        },
    ]
    syntheses = {
        cluster_id: {"status": "reasoned", "related_clusters": []}
        for cluster_id in ("left", "right")
    }

    _project_planned_cluster_neighbors(clusters, syntheses)

    assert syntheses["left"]["related_clusters"][0]["target_cluster_id"] == "right"
    assert syntheses["right"]["related_clusters"][0]["target_cluster_id"] == "left"

    partitioned = [
        {
            "cluster_id": "child",
            "source_ids": ["A"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "external",
                    "partition_parent_target_id": "parent",
                    "relationship": "An external bridge.",
                    "evidence": [
                        {
                            "source_id": "A",
                            "evidence_anchor_id": "anchor-A",
                            "locator": "p. 10",
                        }
                    ],
                }
            ],
        },
        {
            "cluster_id": "external",
            "source_ids": ["E"],
            "planned_neighbor_relationships": [
                {
                    "target_cluster_id": "parent",
                    "relationship": "An external bridge.",
                    "evidence": [
                        {
                            "source_id": "E",
                            "evidence_anchor_id": "anchor-E",
                            "locator": "p. 12",
                        }
                    ],
                }
            ],
        },
    ]
    partitioned_syntheses = {
        cluster_id: {"status": "reasoned", "related_clusters": []}
        for cluster_id in ("child", "external")
    }

    _project_planned_cluster_neighbors(partitioned, partitioned_syntheses)

    assert partitioned_syntheses["child"]["related_clusters"][0][
        "target_cluster_id"
    ] == "external"
    assert partitioned_syntheses["external"]["related_clusters"][0][
        "target_cluster_id"
    ] == "child"


def test_cluster_catalogue_round_trip_preserves_semantic_fields(
    tmp_path: Path,
) -> None:
    cluster = {
        "cluster_id": "cluster-one",
        "display_label": "Cluster One",
        "display_question": "What belongs together?",
        "bounded_object": "A bounded object.",
        "central_debate": "The studies disagree about the mechanism.",
        "source_ids": ["A", "B"],
        "core_source_ids": ["A", "B"],
        "related_cluster_ids": ["cluster-two"],
        "refresh_pending": False,
    }
    write_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml",
        {"clusters": [cluster]},
    )

    reloaded = _cluster_catalogue_rows(tmp_path)

    assert _compact_cluster_catalogue(reloaded) == _compact_cluster_catalogue(
        [cluster]
    )


def test_unchanged_markdown_projection_preserves_mtime(tmp_path: Path) -> None:
    path = tmp_path / "INDEX.md"

    assert _write_markdown_with_quality_ratchet(
        path, "# Index\n", publishable=True
    )
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    assert not _write_markdown_with_quality_ratchet(
        path, "# Index\n", publishable=True
    )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_processing_deadlines_do_not_change_source_identity(tmp_path: Path) -> None:
    first = MapRequest(
        tmp_path,
        processing=ProcessingPolicy(request_deadline_seconds=60),
    )
    second = MapRequest(
        tmp_path,
        processing=ProcessingPolicy(request_deadline_seconds=600),
    )

    assert _fingerprint(
        "ITEM",
        "content",
        first,
        "deepseek",
        "deepseek-v4-flash",
        "full_document",
    ) == _fingerprint(
        "ITEM",
        "content",
        second,
        "deepseek",
        "deepseek-v4-flash",
        "full_document",
    )


def test_relationship_job_identity_ignores_graph_and_catalogue_outputs() -> None:
    common = {
        "left_source_id": "A",
        "right_source_id": "B",
        "profiles": {
            "left": {"dependency_hash": "left"},
            "right": {"dependency_hash": "right"},
        },
        "atomic_notes": {
            "left": {"semantic_hash": "left-note", "body": "Left."},
            "right": {"semantic_hash": "right-note", "body": "Right."},
        },
    }
    first = RelationshipPairJob(
        **common,
        catalogue_revision="one",
        graph_context={"existing_neighbors": ["old"]},
        candidate_basis=[{"route": "same"}],
    )
    second = RelationshipPairJob(
        **common,
        catalogue_revision="two",
        graph_context={"existing_neighbors": ["new"]},
        candidate_basis=[{"route": "same"}],
    )

    assert first.pair_job_id == second.pair_job_id


def test_cluster_projection_contains_full_semantic_note_without_graph_blocks() -> None:
    profile = normalize_evidence_profiles([_profile("A")])[0]
    projected = _cluster_synthesis_profile_projection(
        profile,
        {"source_ids": ["A"]},
        {
            "source_id": "A",
            "title": "Source A",
            "body": (
                "# Source A\n\n## Thesis\n\nComplete source argument.\n\n"
                "<!-- auto-zettelkasten:graph:start -->\n"
                "## Graph Links\n\n- [[Generated neighbor]]\n"
                "<!-- auto-zettelkasten:graph:end -->\n"
            ),
        },
    )

    assert "Complete source argument." in projected["atomic_note_markdown"]
    assert "Generated neighbor" not in projected["atomic_note_markdown"]
    assert "claims" not in projected
    assert projected["evidence_anchors"] == [
        {
            "evidence_anchor_id": "anchor-A",
            "source_id": "A",
            "claim": "Finding for A.",
            "locator": "p. 10",
            "planning_roles": ["major_finding"],
            "salience_priority": 10,
            "support_envelope": {
                "coverage": "full_text",
                "support_status": "supported",
                "empirical_role": "descriptive",
                "argument_role": "none",
            },
        }
    ]


def test_streamlined_cluster_requires_one_specific_finding_per_member() -> None:
    profiles = normalize_evidence_profiles([_profile("A"), _profile("B")])
    cluster = {"cluster_id": "cluster-one", "source_ids": ["A", "B"]}
    complete = validate_streamlined_cluster_synthesis(
        _streamlined_response(cluster, profiles),
        cluster,
        profiles,
    )
    incomplete_response = _streamlined_response(cluster, profiles)
    incomplete_response["lines_of_inquiry"][0]["study_findings"].pop()
    incomplete = validate_streamlined_cluster_synthesis(
        incomplete_response,
        cluster,
        profiles,
    )

    assert complete["status"] == "reasoned"
    assert incomplete["status"] == "partial"
    assert incomplete["retained_member_ids"] == ["A"]
    assert "cluster_requires_two_retained_members" in incomplete["quality_errors"]
    assert "retained_member_without_specific_finding_removed" in incomplete[
        "quality_warnings"
    ]

    empty_response = _streamlined_response(cluster, profiles)
    empty_response["bottom_line"] = ""
    empty_response["lines_of_inquiry"][0]["study_findings"][0]["finding"] = ""
    empty = validate_streamlined_cluster_synthesis(
        empty_response,
        cluster,
        profiles,
    )
    assert "cluster_requires_bottom_line" in empty["quality_errors"]
    assert "study_finding_requires_finding" in empty["quality_warnings"]


def test_streamlined_cluster_publishes_valid_members_when_one_contribution_is_missing() -> None:
    profiles = normalize_evidence_profiles(
        [_profile("A"), _profile("B"), _profile("C")]
    )
    cluster = {"cluster_id": "cluster-one", "source_ids": ["A", "B", "C"]}
    response = _streamlined_response(cluster, profiles)
    response["lines_of_inquiry"][0]["study_findings"] = [
        row
        for row in response["lines_of_inquiry"][0]["study_findings"]
        if row["source_id"] != "C"
    ]

    result = validate_streamlined_cluster_synthesis(
        response, cluster, profiles
    )

    assert result["status"] == "reasoned"
    assert result["retained_member_ids"] == ["A", "B"]
    assert result["dropped_members"] == [
        {
            "source_id": "C",
            "reason": "writer_omitted_specific_contribution",
        }
    ]
    assert result["quality_errors"] == []


def test_streamlined_markdown_is_source_specific_and_has_no_gap_boilerplate() -> None:
    profiles = normalize_evidence_profiles([_profile("A"), _profile("B")])
    for profile in profiles:
        profile["note_path"] = f"02_source_memory/notes/{profile['source_id']}.md"
    cluster = {
        "cluster_id": "cluster-one",
        "label": "Mapped findings",
        "source_ids": ["A", "B"],
    }
    response = _streamlined_response(cluster, profiles)
    markdown = _cluster_markdown(
        cluster,
        None,
        None,
        synthesis=response,
        profile_by_source={row["source_id"]: row for row in profiles},
        cluster_by_id={"cluster-one": cluster},
    )

    assert "## Bottom line" in markdown
    assert "## Main lines of inquiry" in markdown
    assert "A reports a source-specific finding." in markdown
    assert "B reports a source-specific finding." in markdown
    assert "Gap" not in markdown


def test_cluster_cleanup_removes_only_machine_owned_stale_notes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clusters"
    root.mkdir()
    stale = root / "stale.md"
    stale.write_text(
        "<!-- auto-zettelkasten:cluster:start -->\nGenerated.\n",
        encoding="utf-8",
    )
    human = root / "human.md"
    human.write_text("# Human cluster\n", encoding="utf-8")

    _prune_stale_generated_markdown(root, keep_names=[])

    assert not stale.exists()
    assert human.read_text(encoding="utf-8") == "# Human cluster\n"


def test_semantic_checkpoint_is_reused_across_run_ids(tmp_path: Path) -> None:
    class Reasoner:
        name = "local"
        model = "one"

        def __init__(self) -> None:
            self.calls = 0

        def plan_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            return {
                "clusters": [],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

    reasoner = Reasoner()
    first_request = LiteratureMapRequest(tmp_path, run_id="one")
    first = _CheckpointedReasonerCalls(
        tmp_path, "one", reasoner, first_request
    )
    first("cluster_plan", "collection", "plan_clusters", [], {})
    second_request = LiteratureMapRequest(tmp_path, run_id="two")
    second = _CheckpointedReasonerCalls(
        tmp_path, "two", reasoner, second_request
    )
    second("cluster_plan", "collection", "plan_clusters", [], {})

    assert reasoner.calls == 1
    assert second.provider_calls == 0
    assert second.checkpoint_hits == 1


def test_independent_cluster_writers_run_concurrently_with_full_notes() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C", "D")]
    notes = [
        {
            "source_id": source_id,
            "title": f"Source {source_id}",
            "body": f"# Source {source_id}\n\n## Thesis\n\nFull note {source_id}.",
        }
        for source_id in ("A", "B", "C", "D")
    ]
    barrier = threading.Barrier(2)
    seen_notes: list[set[str]] = []
    seen_plan_context: list[dict[str, Any]] = []
    seen_writer_context: list[dict[str, Any]] = []
    stages: list[tuple[str, dict[str, Any]]] = []

    class Reasoner:
        name = "local"
        model = "one"

        def plan_clusters(self, profiles, request, *, context=None):
            seen_plan_context.append(
                {"profiles": list(profiles), "context": dict(context or {})}
            )
            return {
                "clusters": [
                    {
                        "cluster_id": cluster_id,
                        "title": cluster_id,
                        "semantic_identity": cluster_id,
                        "organizing_mode": "question",
                        "organizing_problem": "What do these studies find?",
                        "members": [
                            {
                                "source_id": source_id,
                                "evidence_anchor_ids": [
                                    f"anchor-{source_id}"
                                ],
                            }
                            for source_id in source_ids
                        ],
                    }
                    for cluster_id, source_ids in (
                        ("first-pair", ("A", "B")),
                        ("second-pair", ("C", "D")),
                    )
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            seen_writer_context.append(dict(context["cluster"]))
            seen_notes.append(
                {
                    str(row.get("atomic_note_markdown") or "")
                    for row in projected
                }
            )
            barrier.wait(timeout=3)
            return _streamlined_response(context["cluster"], projected)

    def stage_callback(stage: str, **values: Any) -> None:
        stages.append((stage, values))

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(
            Path("."),
            provider_concurrency="auto",
        ),
        source_set={
            "source_set_id": "global",
            "collection_memberships": {"A": ["Mediation"]},
            "collection_views": [{"collection_key": "MED"}],
            "rejected_pair_memory": [{"source_id": "A", "target_source_id": "D"}],
        },
        source_notes=notes,
        accepted_relationships=[
            {
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "supports",
                "provenance": "human_curated",
                "active": True,
            }
        ],
        stage_callback=stage_callback,
    )

    assert len(report["cluster_registry"]["clusters"]) == 2
    assert seen_plan_context[0]["profiles"][0]["collections"] == ["Mediation"]
    assert seen_plan_context[0]["context"]["collection_identity"][
        "collection_views"
    ] == [{"collection_key": "MED"}]
    assert seen_plan_context[0]["context"]["rejected_pair_memory"] == [
        {"source_id": "A", "target_source_id": "D"}
    ]
    assert seen_plan_context[0]["context"]["accepted_relationships"][0][
        "provenance"
    ] == "human_curated"
    assert all("status" not in context for context in seen_writer_context)
    assert {context["organizing_mode"] for context in seen_writer_context} == {
        "question"
    }
    assert {
        context["organizing_problem"] for context in seen_writer_context
    } == {"What do these studies find?"}
    assert all(any("Full note" in note for note in packet) for packet in seen_notes)
    assert next(
        values
        for stage, values in stages
        if stage == "cluster_synthesis"
        and "cluster_peak_concurrency" in values
    )["cluster_peak_concurrency"] == 2


def test_cluster_writer_drop_updates_all_projected_membership() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]

    class Reasoner:
        name = "local"
        model = "one"

        def plan_clusters(self, profiles, request, *, context=None):
            return {
                "clusters": [
                    {
                        "cluster_id": "trimmed",
                        "title": "Trimmed",
                        "semantic_identity": "trimmed",
                        "organizing_mode": "question",
                        "organizing_problem": "What belongs?",
                        "members": [
                            {"source_id": source_id}
                            for source_id in ("A", "B", "C")
                        ],
                    }
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            response = _streamlined_response(context["cluster"], projected[:2])
            response["dropped_members"] = [
                {"source_id": "C", "reason": "It addresses another problem."}
            ]
            return response

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_set={"source_set_id": "global"},
        source_notes=[
            {
                "source_id": source_id,
                "body": f"# {source_id}\n\n## Thesis\n\nFull note {source_id}.",
            }
            for source_id in ("A", "B", "C")
        ],
    )

    cluster = report["cluster_registry"]["clusters"][0]
    assert cluster["source_ids"] == ["A", "B"]
    assert cluster["note_ids"] == ["note-A", "note-B"]
    assert cluster["source_roles"] == [
        {"source_id": "A", "role": "core"},
        {"source_id": "B", "role": "core"},
    ]
    assert [
        row["source_id"]
        for row in report["cluster_registry"]["unclustered_sources"]
    ] == ["C"]


def test_global_plan_reuse_and_incremental_family_context() -> None:
    profiles = [_profile("A"), _profile("B")]

    class Reasoner:
        name = "local"
        model = "one"

        def __init__(self) -> None:
            self.plan_contexts: list[dict[str, Any]] = []

        def plan_clusters(self, profiles, request, *, context=None):
            self.plan_contexts.append(dict(context or {}))
            return {
                "clusters": [
                    {
                        "cluster_id": "family",
                        "title": "Family",
                        "semantic_identity": "family",
                        "organizing_mode": "question",
                        "organizing_problem": "What do the studies find?",
                        "members": [
                            {"source_id": str(row["source_id"])}
                            for row in profiles
                        ],
                    }
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            return _streamlined_response(context["cluster"], projected)

    reasoner = Reasoner()
    common = {
        "reasoner": reasoner,
        "request": LiteratureMapRequest(Path(".")),
        "source_set": {"source_set_id": "global"},
    }
    first = build_literature_report(profiles, **common)
    second = build_literature_report(
        profiles,
        previous_registry=first["cluster_registry"],
        **common,
    )
    assert len(reasoner.plan_contexts) == 1

    relationship_changed = build_literature_report(
        profiles,
        previous_registry=second["cluster_registry"],
        accepted_relationships=[
            {
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "supports",
                "provenance": "human_curated",
                "active": True,
            }
        ],
        **common,
    )
    assert len(reasoner.plan_contexts) == 2
    assert "incremental_source_delta" not in reasoner.plan_contexts[-1]

    changed_profiles = [
        dict(
            profiles[0],
            evidence_anchors=[
                {
                    **profiles[0]["evidence_anchors"][0],
                    "claim": "Changed source-specific finding.",
                }
            ],
        ),
        profiles[1],
    ]
    incremental = build_literature_report(
        changed_profiles,
        previous_registry=relationship_changed["cluster_registry"],
        accepted_relationships=[
            {
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "supports",
                "provenance": "human_curated",
                "active": True,
            }
        ],
        **common,
    )
    assert len(reasoner.plan_contexts) == 3
    assert reasoner.plan_contexts[-1]["incremental_source_delta"][
        "changed_source_ids"
    ] == ["A"]
    assert reasoner.plan_contexts[-1]["prior_active_cluster_family_cards"][0][
        "semantic_identity"
    ] == "family"

    build_literature_report(
        changed_profiles,
        previous_registry=incremental["cluster_registry"],
        accepted_relationships=[
            {
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "supports",
                "provenance": "human_curated",
                "active": True,
            }
        ],
        **common,
    )
    assert len(reasoner.plan_contexts) == 3


def test_v014_migration_is_local_and_marks_legacy_clusters(tmp_path: Path) -> None:
    initialize(tmp_path)
    for relative in (
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    ):
        path = tmp_path / relative
        payload = read_yaml(path)
        payload.update(
            engine_version="0.13.0",
            artifact_schema_version="1.12",
        )
        write_yaml(path, payload)
    registry = (
        tmp_path
        / "03_literature_synthesis"
        / "maps"
        / "legacy"
        / "cluster_syntheses.yml"
    )
    write_yaml(
        registry,
        {"syntheses": {"cluster-one": {"status": "reasoned"}}},
    )
    write_yaml(
        registry.parent / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-global",
                    "source_ids": ["A", "B"],
                    "semantic_identity": "global",
                }
            ]
        },
    )
    typed_links = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    write_yaml(
        typed_links,
        {
            "registry_schema_version": "4",
            "relations": [
                {
                    "relation_id": "stale-membership",
                    "source_kind": "source",
                    "source_id": "A",
                    "target_kind": "cluster",
                    "target_cluster_id": "cluster-old",
                    "relation_type": "cluster_member",
                    "active": True,
                }
            ],
        },
    )
    typed_note_links = (
        tmp_path / "02_source_memory" / "indexes" / "typed_note_links.yml"
    )
    write_yaml(
        typed_note_links,
        {
            "registry_schema_version": "4",
            "relations": [
                {
                    "relation_id": "current-membership",
                    "source_kind": "source",
                    "source_id": "B",
                    "target_kind": "cluster",
                    "target_cluster_id": "cluster-global",
                    "relation_type": "cluster_member",
                    "active": True,
                }
            ],
        },
    )
    note = tmp_path / "02_source_memory" / "notes" / "Human.md"
    note.write_text("# Human content\n", encoding="utf-8")
    before = note.read_bytes()

    first = migrate_v014_schema(tmp_path)
    second = migrate_v014_schema(tmp_path)

    assert first["provider_calls"] == 0
    assert first["legacy_cluster_syntheses_marked"] == 1
    assert first["relationship_rows_consolidated"] == 2
    assert first["stale_cluster_memberships_retired"] == 1
    assert first["global_cluster_registry_selected"] == 1
    assert second["status"] == "already_migrated"
    assert note.read_bytes() == before
    assert read_yaml(registry)["syntheses"]["cluster-one"][
        "legacy_projection"
    ] is True
    assert read_yaml(typed_links) == read_yaml(typed_note_links)
    migrated_relations = {
        row["relation_id"]: row for row in read_yaml(typed_links)["relations"]
    }
    assert migrated_relations["stale-membership"]["active"] is False
    assert migrated_relations["current-membership"]["active"] is True
    assert (
        tmp_path
        / "11_state"
        / "migrations"
        / f"{V014_MIGRATION_ID}.yml"
    ).is_file()


def test_v014_migration_merges_disjoint_cluster_registries_by_stable_identity(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for relative in (
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    ):
        path = tmp_path / relative
        payload = read_yaml(path)
        payload.update(engine_version="0.13.0", artifact_schema_version="1.12")
        write_yaml(path, payload)

    maps = tmp_path / "03_literature_synthesis" / "maps"
    write_yaml(
        maps / "broad" / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-shared",
                    "semantic_identity": "Shared debate",
                    "source_ids": ["A", "B"],
                },
                {
                    "cluster_id": "cluster-second",
                    "semantic_identity": "Second debate",
                    "source_ids": ["C", "D"],
                },
            ]
        },
    )
    write_yaml(
        maps / "narrow" / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "duplicate-by-members",
                    "semantic_identity": "Renamed shared debate",
                    "members": [{"source_id": "A"}, {"source_id": "B"}],
                },
                {
                    "cluster_id": "duplicate-by-semantics",
                    "semantic_identity": " shared   debate ",
                    "source_ids": ["A"],
                },
                {
                    "cluster_id": "cluster-disjoint",
                    "semantic_identity": "Disjoint debate",
                    "source_ids": ["E"],
                },
            ]
        },
    )

    result = migrate_v014_schema(tmp_path)

    assert result["global_cluster_registry_selected"] == 1
    merged = read_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml"
    )
    assert {row["cluster_id"] for row in merged["clusters"]} == {
        "cluster-shared",
        "cluster-second",
        "cluster-disjoint",
    }
    assert next(
        row for row in merged["clusters"] if row["cluster_id"] == "cluster-shared"
    )["source_ids"] == ["A", "B"]


def test_v014_migration_unions_disjoint_members_of_same_semantic_cluster(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for relative in (
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    ):
        path = tmp_path / relative
        payload = read_yaml(path)
        payload.update(engine_version="0.13.0", artifact_schema_version="1.12")
        write_yaml(path, payload)
    maps = tmp_path / "03_literature_synthesis" / "maps"
    write_yaml(
        maps / "one" / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-one",
                    "semantic_identity": "Same debate",
                    "source_ids": ["A"],
                }
            ]
        },
    )
    write_yaml(
        maps / "two" / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-two",
                    "semantic_identity": " same   debate ",
                    "source_ids": ["B"],
                }
            ]
        },
    )

    migrate_v014_schema(tmp_path)

    clusters = read_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml"
    )["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["source_ids"] == ["A", "B"]


def test_v014_migration_reconciles_machine_edges_without_stale_active_anchors(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for relative in (
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    ):
        path = tmp_path / relative
        payload = read_yaml(path)
        payload.update(engine_version="0.13.0", artifact_schema_version="1.12")
        write_yaml(path, payload)

    primary = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility = primary.with_name("typed_note_links.yml")
    current = {
        "relation_id": "current-machine-id",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "supports",
        "provenance": "probabilistic_relationship_adjudication_v4",
        "source_evidence_anchor_ids": ["new-a"],
        "target_evidence_anchor_ids": ["new-b"],
        "source_evidence": {"evidence_anchor_id": "new-a"},
        "target_evidence": {"evidence_anchor_id": "new-b"},
        "active": True,
    }
    stale = {
        **current,
        "relation_id": "stale-machine-id",
        "source_evidence_anchor_ids": ["old-a"],
        "target_evidence_anchor_ids": ["old-b"],
        "source_evidence": {"evidence_anchor_id": "old-a"},
        "target_evidence": {"evidence_anchor_id": "old-b"},
    }
    human_one = {
        "relation_id": "human-one",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "supports",
        "provenance": "human_curated",
        "active": True,
    }
    human_two = {**human_one, "relation_id": "human-two"}
    write_yaml(
        primary,
        {"registry_schema_version": "4", "relations": [current, human_one]},
    )
    write_yaml(
        compatibility,
        {"registry_schema_version": "4", "relations": [stale, human_two]},
    )

    result = migrate_v014_schema(tmp_path)

    assert result["relationship_rows_consolidated"] == 3
    migrated = read_yaml(primary)
    machine_links = [
        row
        for row in migrated["links"]
        if not str(row.get("provenance") or "").startswith("human")
    ]
    assert machine_links == [current]
    assert {
        row["relation_id"]
        for row in migrated["relations"]
        if str(row.get("provenance") or "").startswith("human")
    } == {"human-one", "human-two"}


def test_v014_migration_keeps_only_current_probabilistic_decision_per_pair(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    for relative in (
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    ):
        path = tmp_path / relative
        payload = read_yaml(path)
        payload.update(engine_version="0.13.0", artifact_schema_version="1.12")
        write_yaml(path, payload)
    primary = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility = primary.with_name("typed_note_links.yml")
    current = {
        "relation_id": "current",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "supports",
        "provenance": "probabilistic_relationship_adjudication_v4",
        "source_evidence_anchor_ids": ["new-a"],
        "target_evidence_anchor_ids": ["new-b"],
        "active": True,
    }
    stale = {
        **current,
        "relation_id": "stale",
        "relation_type": "qualifies",
        "source_evidence_anchor_ids": ["old-a"],
        "target_evidence_anchor_ids": ["old-b"],
    }
    write_yaml(primary, {"registry_schema_version": "4", "relations": [current]})
    write_yaml(
        compatibility,
        {"registry_schema_version": "4", "relations": [stale]},
    )

    migrate_v014_schema(tmp_path)

    assert read_yaml(primary)["links"] == [current]
