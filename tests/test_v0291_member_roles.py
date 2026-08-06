from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from auto_zettelkasten.literature import (
    build_literature_report,
    validate_streamlined_cluster_synthesis,
)
from auto_zettelkasten.pipeline import _cluster_membership_relations
from auto_zettelkasten.models import EvidenceProfile, LiteratureMapRequest
from auto_zettelkasten.notes import update_note_graph
from auto_zettelkasten.readers import (
    _cluster_synthesis_system_prompt,
    _validate_streamlined_cluster_response,
)


def _profile(source_id: str) -> dict[str, Any]:
    anchor = {
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
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_status": "analytical_atomic_note",
        "evidence_eligibility": "substantive_bounded",
        "title": f"Source {source_id}",
        "thesis": f"Thesis for {source_id}.",
        "methods": ["comparative analysis"],
        "study_family_id": source_id,
        "evidence_anchors": [anchor],
        "claims": [anchor],
    }


def _response(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_ids = [str(row["source_id"]) for row in profiles]
    return {
        "cluster_contract": "streamlined-full-note-v2",
        "cluster_id": str(cluster["cluster_id"]),
        "title": str(cluster.get("label") or "Mapped findings"),
        "organizing_mode": "question",
        "organizing_problem": "How do the studies explain the outcome?",
        "status": "accepted",
        "retained_member_ids": source_ids,
        "member_roles": {},
        "dropped_members": [],
        "lines_of_inquiry": [
            {
                "title": "Main comparison",
                "synthesis": "The studies identify related bounded findings.",
                "study_findings": [
                    {
                        "source_id": source_id,
                        "finding": f"{source_id} reports a specific finding.",
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
        "acquisition_candidate_dispositions": [],
    }


def test_writer_roles_survive_validation_and_contributions() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]
    cluster = {
        "cluster_id": "cluster-one",
        "source_ids": ["A", "B", "C"],
        "candidate_roles": {"A": "core", "B": "supporting", "C": "bridge"},
        "source_roles": [
            {"source_id": "A", "role": "core"},
            {"source_id": "B", "role": "core"},
            {"source_id": "C", "role": "core"},
        ],
    }
    response = _response(cluster, profiles)
    response["member_roles"] = {"A": "core", "B": "context", "C": "bridge"}

    normalized = _validate_streamlined_cluster_response(response)
    validated = validate_streamlined_cluster_synthesis(
        normalized, cluster, profiles
    )

    assert validated["member_roles"] == {
        "A": "core",
        "B": "context",
        "C": "bridge",
    }
    assert {
        row["source_id"]: row["cluster_role"]
        for row in validated["source_contributions"]
    } == validated["member_roles"]
    assert validated["quality_errors"] == []


def test_missing_or_invalid_writer_roles_fall_back_without_parking() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]
    cluster = {
        "cluster_id": "cluster-one",
        "source_ids": ["A", "B", "C"],
        "candidate_roles": {"A": "core", "B": "supporting", "C": "bridge"},
        "source_roles": [
            {"source_id": source_id, "role": "core"}
            for source_id in ("A", "B", "C")
        ],
    }
    response = _response(cluster, profiles)
    response["member_roles"] = {"A": "core", "B": "decorative", "UNKNOWN": "core"}

    validated = validate_streamlined_cluster_synthesis(
        _validate_streamlined_cluster_response(response), cluster, profiles
    )

    assert validated["status"] == "reasoned"
    assert validated["member_roles"] == {
        "A": "core",
        "B": "context",
        "C": "bridge",
    }
    assert "member_role_fallback_applied" in validated["quality_warnings"]
    assert "unknown_member_role_ignored" in validated["quality_warnings"]


def test_writer_roles_propagate_through_registry_round_trip() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]

    class Reasoner:
        name = "local"
        model = "one"

        def plan_clusters(self, profiles, request, *, context=None):
            return {
                "clusters": [
                    {
                        "cluster_id": "family",
                        "title": "Family",
                        "semantic_identity": "family",
                        "organizing_mode": "question",
                        "organizing_problem": "What do these studies find?",
                        "members": [
                            {"source_id": "A", "role": "core"},
                            {"source_id": "B", "role": "supporting"},
                            {"source_id": "C", "role": "bridge"},
                        ],
                    }
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }

        def synthesize_cluster(self, projected, request, *, context=None):
            response = _response(context["cluster"], projected)
            response["member_roles"] = {
                "A": "core",
                "B": "context",
                "C": "bridge",
            }
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
    assert cluster["source_roles"] == [
        {"source_id": "A", "role": "core"},
        {"source_id": "B", "role": "context"},
        {"source_id": "C", "role": "bridge"},
    ]
    assert {
        row["source_id"]: row["cluster_role"]
        for row in report["cluster_source_contributions"][cluster["cluster_id"]]
    } == {"A": "core", "B": "context", "C": "bridge"}


def test_cluster_prompt_requires_member_roles_v35() -> None:
    prompt = _cluster_synthesis_system_prompt()
    assert "prompt v35" in prompt
    assert "member_roles must map every retained source_id" in prompt


def test_final_roles_reach_reciprocal_membership_relations() -> None:
    relations = _cluster_membership_relations(
        [
            {
                "cluster_id": "cluster-one",
                "source_ids": ["A", "B", "C"],
                "source_roles": [
                    {"source_id": "A", "role": "core"},
                    {"source_id": "B", "role": "context"},
                    {"source_id": "C", "role": "bridge"},
                ],
            }
        ],
        [
            EvidenceProfile(source_id=source_id, note_id=f"note-{source_id}")
            for source_id in ("A", "B", "C")
        ],
    )

    assert {(row["source_id"], row["cluster_role"]) for row in relations} == {
        ("A", "core"),
        ("B", "context"),
        ("C", "bridge"),
        ("cluster-one", "core"),
        ("cluster-one", "context"),
        ("cluster-one", "bridge"),
    }


def test_member_without_resolved_evidence_is_removed_before_admission() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B")]
    cluster = {
        "cluster_id": "cluster-one",
        "source_ids": ["A", "B"],
        "candidate_roles": {"A": "core", "B": "context"},
    }
    response = _response(cluster, profiles)
    response["lines_of_inquiry"][0]["study_findings"][1]["evidence"] = []

    validated = validate_streamlined_cluster_synthesis(response, cluster, profiles)

    assert validated["retained_member_ids"] == ["A"]
    assert "cluster_requires_two_retained_members" in validated["quality_errors"]


def test_final_role_is_visible_in_atomic_note_membership(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text(
        "---\nnote_id: note-A\nsource_id: A\n---\n## Thesis\n\nFinding.\n",
        encoding="utf-8",
    )

    update_note_graph(
        path,
        {"clusters": ["cluster-one"], "cluster_roles": ["bridge"]},
        [],
        ["cluster-one"],
        cluster_wikilinks={"cluster-one": "[[Cluster One]]"},
        cluster_roles={"cluster-one": "bridge"},
    )

    assert "- member of (bridge): [[Cluster One]]" in path.read_text(encoding="utf-8")
