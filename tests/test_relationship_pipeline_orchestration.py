from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import (
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMapRequest,
)
from auto_zettelkasten.pipeline import (
    _cluster_membership_relations,
    _run_relationship_reasoning,
)
from auto_zettelkasten.profiles import profile_to_dict
from auto_zettelkasten.relationships import (
    relationship_decision_key,
    stable_hash,
)


class _Reasoner:
    name = "test-provider"
    model = "test-model"

    def select_relationship_shards(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def select_relationship_candidates(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def verify_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _Calls:
    def __init__(
        self,
        handler: Callable[
            [str, Sequence[Any], Mapping[str, Any]],
            Mapping[str, Any],
        ],
    ) -> None:
        self.handler = handler
        self.seen: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(
        self,
        stage: str,
        _key: str,
        _method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.seen.append((stage, context))
        return self.handler(stage, profiles, context)


def _profile(source_id: str) -> EvidenceProfile:
    return EvidenceProfile(
        source_id=source_id,
        note_id=f"note-{source_id.lower()}",
        context={
            "note_status": "analytical_atomic_note",
            "title": f"Source {source_id}",
        },
        evidence_anchors=[
            EvidenceAnchor(
                evidence_anchor_id=f"anchor-{source_id.lower()}",
                source_id=source_id,
                claim=f"Substantive claim from {source_id}",
                locator="p. 10",
                support_envelope={
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            )
        ],
    )


def _candidate(source_id: str, target_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_kind": "source",
        "target_id": target_id,
        "why_relevant": "The sources address the same substantive proposition.",
        "confidence": 0.9,
    }


def _accepted_decision(
    source_id: str,
    target_id: str,
    anchors: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_source_id": target_id,
        "status": "accepted",
        "relation_type": "supports",
        "source_evidence_anchor_id": anchors[source_id],
        "target_evidence_anchor_id": anchors[target_id],
        "comparison_unit": "shared proposition",
        "reason": "Both sources independently support the same substantive proposition.",
        "confidence": 0.9,
    }


def _catalogue(
    workspace: Path,
    source_ids: Sequence[str],
    *,
    shards: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    path = workspace / "02_source_memory" / "indexes" / "source_catalogue.yml"
    write_yaml(
        path,
        {
            "sources": [
                {"source_id": source_id, "title": f"Source {source_id}"}
                for source_id in source_ids
            ],
            "shards": list(shards),
        },
    )
    return {
        "catalogue_path": str(path),
        "routing_revision_hash": "catalogue-revision",
    }


def _request(workspace: Path) -> LiteratureMapRequest:
    return LiteratureMapRequest(
        workspace=workspace,
        provider="test-provider",
        model="test-model",
    )


def _run(
    workspace: Path,
    profiles: Sequence[Any],
    catalogue: Mapping[str, Any],
    calls: _Calls,
) -> dict[str, Any]:
    return _run_relationship_reasoning(
        workspace,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue,
        reasoner=_Reasoner(),
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=_request(workspace),
    )


def test_large_catalogue_candidate_context_uses_selected_shards(
    tmp_path: Path,
) -> None:
    profiles = [_profile(f"S{index:03d}") for index in range(251)]
    catalogue = _catalogue(
        tmp_path,
        [profile.source_id for profile in profiles],
        shards=[
            {
                "literature_id": "lit-a",
                "shard_id": "shard-a",
                "source_ids": ["S000", "S001"],
            },
            {
                "literature_id": "lit-b",
                "shard_id": "shard-b",
                "source_ids": [
                    profile.source_id for profile in profiles[2:]
                ],
            },
        ],
    )
    state_path = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml"
    )
    profile_hashes = {
        profile.source_id: stable_hash(profile_to_dict(profile))
        for profile in profiles
    }
    profile_hashes["S000"] = "stale"
    write_yaml(state_path, {"profile_hashes": profile_hashes})

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_shard_selection":
            return {"shard_ids": ["shard-a"]}
        if stage == "relationship_candidate_selection":
            return {"candidates": [_candidate("S000", "S001")]}
        return {
            "decisions": [
                {
                    "source_id": pair["source_id"],
                    "target_source_id": pair["target_source_id"],
                    "status": "no_relationship",
                    "reason": "The apparent overlap is not substantively meaningful.",
                    "confidence": 0.9,
                }
                for pair in context["pairs"]
            ]
        }

    calls = _Calls(handler)
    _run(tmp_path, profiles, catalogue, calls)

    candidate_context = next(
        context
        for stage, context in calls.seen
        if stage == "relationship_candidate_selection"
    )
    assert {
        row["source_id"] for row in candidate_context["catalogue_entries"]
    } == {"S001"}


@pytest.mark.parametrize("failure_stage", ["candidate", "adjudication"])
def test_second_provider_batch_failure_preserves_earlier_results_and_state(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    profiles = [_profile(f"S{index:02d}") for index in range(20)]
    catalogue = _catalogue(
        tmp_path, [profile.source_id for profile in profiles]
    )
    anchors = {
        profile.source_id: profile_to_dict(profile)["evidence_anchors"][0][
            "evidence_anchor_id"
        ]
        for profile in profiles
    }
    stage_counts: dict[str, int] = {}

    def handler(
        stage: str,
        batch_profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        call_number = stage_counts[stage]
        if stage == "relationship_candidate_selection":
            if failure_stage == "candidate" and call_number == 2:
                raise RuntimeError("candidate provider failure")
            focus_ids = [profile.source_id for profile in batch_profiles]
            if call_number == 1:
                if failure_stage == "candidate":
                    return {"candidates": [_candidate("S00", "S01")]}
                return {
                    "candidates": [
                        _candidate(f"S{index:02d}", target)
                        for index in range(12)
                        for target in ("S18", "S19")
                    ]
                }
            assert focus_ids == [f"S{index:02d}" for index in range(12, 20)]
            return {"candidates": [_candidate("S12", "S13")]}
        if stage == "relationship_adjudication":
            if failure_stage == "adjudication" and call_number == 2:
                raise RuntimeError("adjudication provider failure")
            return {
                "decisions": [
                    _accepted_decision(
                        str(pair["source_id"]),
                        str(pair["target_source_id"]),
                        anchors,
                    )
                    for pair in context["pairs"]
                ]
            }
        if stage == "relationship_verification":
            return {
                "verifications": [
                    {
                        **row,
                        "status": "confirmed",
                        "reason": "Both located claims support the same bounded proposition.",
                        "requested_context": [],
                    }
                    for row in context["preliminary_decisions"]
                ]
            }
        raise AssertionError(f"unexpected stage: {stage}")

    result = _run(tmp_path, profiles, catalogue, _Calls(handler))

    assert result["accepted"], result
    assert set(result["selected_profile_hashes"])
    assert set(result["selected_profile_hashes"]) < {
        f"S{index:02d}" for index in range(20)
    }
    assert any(
        row["reason"]
        == (
            "relationship_candidate_selection_failure"
            if failure_stage == "candidate"
            else "relationship_adjudication_failure"
        )
        for row in result["parked"]
    )


def test_matching_pair_decision_suppresses_redundant_adjudication(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    catalogue = _catalogue(tmp_path, ["A", "B"])
    decision_key = relationship_decision_key(
        "A",
        "B",
        stable_hash(profile_to_dict(profiles[0])),
        stable_hash(profile_to_dict(profiles[1])),
        provider="test-provider",
        model="test-model",
    )
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {"links": [], "pair_decisions": [{"decision_key": decision_key}]},
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": [_candidate("A", "B")]}
        raise AssertionError("matching pair decision should skip adjudication")

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, catalogue, calls)

    assert result["accepted"] == []
    assert result["no_relationship"] == []
    assert set(result["selected_profile_hashes"]) == {"A", "B"}
    assert [
        stage for stage, _context in calls.seen
        if stage == "relationship_adjudication"
    ] == []


def test_cluster_membership_relations_are_reciprocal() -> None:
    profile = _profile("A")
    relations = _cluster_membership_relations(
        [{"cluster_id": "cluster-one", "source_ids": ["A"]}],
        [profile],
    )

    assert {row["relation_type"] for row in relations} == {
        "cluster_member",
        "has_member",
    }
    member = next(
        row for row in relations if row["relation_type"] == "cluster_member"
    )
    reciprocal = next(
        row for row in relations if row["relation_type"] == "has_member"
    )
    assert member["source_id"] == reciprocal["target_source_id"] == "A"
    assert member["target_cluster_id"] == reciprocal["source_id"] == "cluster-one"


def test_cluster_candidate_is_retained_for_cluster_proposal_context(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    catalogue = _catalogue(tmp_path, ["A", "B"])
    write_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-one",
                    "label": "Existing debate",
                    "core_source_ids": ["B"],
                }
            ]
        },
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert stage == "relationship_candidate_selection"
        return {
            "candidates": [
                {
                    "source_id": "A",
                    "target_kind": "cluster",
                    "target_id": "cluster-one",
                    "why_relevant": "The source directly addresses the existing debate.",
                    "comparison_unit": "debate boundary",
                    "confidence": 0.9,
                }
            ]
        }

    result = _run(tmp_path, profiles, catalogue, _Calls(handler))

    assert result["cluster_candidates"] == [
        {
            "source_id": "A",
            "target_kind": "cluster",
            "target_id": "cluster-one",
            "why_relevant": "The source directly addresses the existing debate.",
            "comparison_unit": "debate boundary",
            "likely_relation_type": "",
            "requested_evidence_depth": "profile",
            "confidence": 0.9,
        }
    ]
