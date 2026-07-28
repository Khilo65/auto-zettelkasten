from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from auto_zettelkasten import literature
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.literature import (
    _cluster_proposal_partitions,
    build_literature_report,
)
from auto_zettelkasten.models import EvidenceAnchor, EvidenceProfile, LiteratureMapRequest
from auto_zettelkasten.pipeline import _run_relationship_reasoning


class _Reasoner:
    name = "test-provider"
    model = "test-model"

    def select_relationship_shards(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def select_relationship_bridge_shards(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def select_relationship_candidates(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def verify_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _NoVerifierReasoner:
    name = "test-provider"
    model = "test-model"

    def select_relationship_candidates(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _Calls:
    def __init__(
        self,
        handler: Callable[
            [str, Sequence[Any], Mapping[str, Any]], Mapping[str, Any]
        ],
    ) -> None:
        self.handler = handler
        self.seen: list[tuple[str, Sequence[Any], Mapping[str, Any]]] = []

    def __call__(
        self,
        stage: str,
        _key: str,
        _method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.seen.append((stage, profiles, context))
        return self.handler(stage, profiles, context)


def _profile(source_id: str, *, literature: str = "") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id.lower()}",
        "title": f"Source {source_id}",
        "note_status": "analytical_atomic_note",
        "analytical": True,
        "study_family_id": f"family-{source_id}",
        "semantic_topics": ["peace durability"],
        "concepts": ["peace durability"],
        "methods": ["comparative analysis"],
        "mechanisms": ["credible commitment"],
        "outcomes": ["peace duration"],
        "cases": [literature or "civil wars"],
        "findings": [
            {
                "evidence_anchor_id": f"anchor-{source_id.lower()}",
                "claim_id": f"anchor-{source_id.lower()}",
                "claim": f"Substantive claim from {source_id}.",
                "locator": "p. 10",
                "support_envelope": {
                    "empirical_role": "associational",
                    "argument_role": "none",
                    "coverage": "full_text",
                    "support_status": "supported",
                },
            }
        ],
    }


def _relationship_profile(source_id: str) -> EvidenceProfile:
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
                claim=f"Substantive claim from {source_id}.",
                locator="p. 10",
                support_envelope={
                    "empirical_role": "associational",
                    "argument_role": "none",
                    "coverage": "full_text",
                    "support_status": "supported",
                },
            )
        ],
    )


def _catalogue(
    workspace: Path,
    profiles: Sequence[Any],
    *,
    shards: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    rows = [
        dict(profile) if isinstance(profile, Mapping) else profile.to_dict()
        for profile in profiles
    ]
    path = workspace / "02_source_memory" / "indexes" / "source_catalogue.yml"
    write_yaml(
        path,
        {
            "sources": [
                {
                    "source_id": row["source_id"],
                    "title": str(
                        row.get("title")
                        or (row.get("context") or {}).get("title")
                        or row["source_id"]
                    ),
                    "thesis": str(
                        (row.get("findings") or row.get("evidence_anchors"))[0][
                            "claim"
                        ]
                    ),
                }
                for row in rows
            ],
            "shards": list(shards),
        },
    )
    return {
        "catalogue_path": str(path),
        "routing_revision_hash": "catalogue-v2",
    }


def _request(workspace: Path) -> LiteratureMapRequest:
    return LiteratureMapRequest(
        workspace=workspace,
        provider="test-provider",
        model="test-model",
    )


def _candidate(source_id: str, target_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_kind": "source",
        "target_id": target_id,
        "why_relevant": "The sources may address the same bounded proposition.",
        "comparison_unit": "shared proposition",
        "confidence": 0.9,
    }


def _adjudications(
    context: Mapping[str, Any],
    profiles: Sequence[Any] = (),
) -> dict[str, Any]:
    anchor_ids = {
        str(row["source_id"]): str(row["evidence_anchors"][0]["evidence_anchor_id"])
        for profile in profiles
        for row in [
            dict(profile) if isinstance(profile, Mapping) else profile.to_dict()
        ]
    }
    return {
        "decisions": [
            {
                "source_id": pair["source_id"],
                "target_source_id": pair["target_source_id"],
                "status": "accepted",
                "relation_type": "supports",
                "comparison_unit": "shared proposition",
                "reason": "Both sources independently support the same bounded proposition.",
                "source_evidence_anchor_id": anchor_ids.get(
                    str(pair["source_id"]),
                    f"anchor-{pair['source_id'].lower()}",
                ),
                "target_evidence_anchor_id": anchor_ids.get(
                    str(pair["target_source_id"]),
                    f"anchor-{pair['target_source_id'].lower()}",
                ),
                "qualifiers": [],
                "confidence": 0.9,
            }
            for pair in context["pairs"]
        ]
    }


def _verifications(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verifications": [
            {
                **row,
                "status": "confirmed",
                "reason": (
                    "Both located claims independently establish the same bounded "
                    "proposition."
                ),
                "requested_context": [],
            }
            for row in context["preliminary_decisions"]
        ]
    }


def test_relationships_publish_only_after_independent_verification(
    tmp_path: Path,
) -> None:
    profiles = [_relationship_profile("A"), _relationship_profile("B")]
    catalogue = _catalogue(tmp_path, profiles)

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": [_candidate("A", "B")]}
        if stage == "relationship_adjudication":
            return _adjudications(context, _profiles)
        if stage == "relationship_verification":
            return _verifications(context)
        raise AssertionError(stage)

    result = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue,
        reasoner=_Reasoner(),
        reasoner_calls=_Calls(handler),  # type: ignore[arg-type]
        request=_request(tmp_path),
    )
    assert len(result["accepted"]) == 1, result
    assert result["accepted"][0]["verification_status"] == "confirmed"

    no_verifier = _run_relationship_reasoning(
        tmp_path / "without-verifier",
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=_catalogue(tmp_path / "without-verifier", profiles),
        reasoner=_NoVerifierReasoner(),
        reasoner_calls=_Calls(
            lambda stage, _profiles, context: (
                {"candidates": [_candidate("A", "B")]}
                if stage == "relationship_candidate_selection"
                else _adjudications(context, _profiles)
            )
        ),  # type: ignore[arg-type]
        request=_request(tmp_path / "without-verifier"),
    )
    assert no_verifier["accepted"] == []
    assert any(
        row.get("reason") == "relationship_verification_unavailable"
        for row in no_verifier["parked"]
    )
    assert set(no_verifier["selected_profile_hashes"]) == {"A", "B"}
    assert result["selection_identity"] != no_verifier["selection_identity"]


def test_cross_literature_bridge_routing_uses_cards_then_selected_entries(
    tmp_path: Path,
) -> None:
    profiles = [
        _relationship_profile("A"),
        _relationship_profile("B"),
        _relationship_profile("C"),
        _relationship_profile("D"),
    ]
    shards = [
        {
            "shard_id": "mediation",
            "literature_id": "mediation",
            "source_ids": ["A", "B"],
            "routing_card": {
                "shard_id": "mediation",
                "title": "Mediation",
                "source_count": 2,
                "representative_theses": ["Monitoring reduces uncertainty."],
            },
        },
        {
            "shard_id": "relapse",
            "literature_id": "relapse",
            "source_ids": ["C", "D"],
            "routing_card": {
                "shard_id": "relapse",
                "title": "Conflict relapse",
                "source_count": 2,
                "representative_theses": ["Commitment problems predict relapse."],
            },
        },
    ]
    calls: _Calls

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": []}
        if stage == "relationship_bridge_shard_selection":
            assert "catalogue_entries" not in context
            return {
                "shard_pairs": [
                    {
                        "left_shard_id": "mediation",
                        "right_shard_id": "relapse",
                        "reason": "Their theses identify a plausible commitment mechanism bridge.",
                        "confidence": 0.9,
                    }
                ]
            }
        if stage == "relationship_bridge_candidate_selection":
            assert not _profiles
            assert {row["source_id"] for row in context["catalogue_entries"]} == {
                "A",
                "B",
                "C",
                "D",
            }
            return {"candidates": [_candidate("A", "C")]}
        if stage == "relationship_adjudication":
            return _adjudications(context, _profiles)
        if stage == "relationship_verification":
            return _verifications(context)
        raise AssertionError(stage)

    calls = _Calls(handler)
    result = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=_catalogue(tmp_path, profiles, shards=shards),
        reasoner=_Reasoner(),
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )
    assert [(row["source_id"], row["target_source_id"]) for row in result["accepted"]] == [
        ("A", "C")
    ], result


def test_cluster_proposals_are_partitioned_and_reconciled_without_profiles(
    monkeypatch,
) -> None:
    profiles = [_profile(f"S{index:03d}") for index in range(194)]
    shards = [
        {
            "shard_id": "part-a",
            "literature_id": "literature-a",
            "source_ids": [row["source_id"] for row in profiles],
            "routing_card": {"shard_id": "part-a", "title": "Literature A"},
        }
    ]
    partitions = _cluster_proposal_partitions(profiles, shards, [])
    assert len(partitions) == 8
    assert max(len(row["source_ids"]) for row in partitions) <= 25

    calls: list[tuple[str, list[str]]] = []

    def stage(
        _reasoner,
        _reasoner_call,
        *,
        stage: str,
        profiles: Sequence[Mapping[str, Any]],
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        context = _kwargs.get("context", {})
        assert (
            literature._reasoner_packet_chars(profiles, context)
            <= literature._reasoner_context_char_budget(
                _reasoner, _kwargs.get("request")
            )
        )
        if stage == "cluster_proposal":
            assert not {
                "topic_neighborhoods",
                "study_lineages",
                "evidence_base_groups",
                "independence_assessments",
            } & set(context)
        calls.append(
            (
                stage,
                [str(row.get("source_id") or "") for row in profiles],
            )
        )
        if stage == "cluster_proposal" and profiles:
            source_ids = [
                str(row.get("source_id") or "") for row in profiles[:2]
            ]
            return {
                "clusters": [
                    {
                        "proposal_id": f"proposal-{source_ids[0]}",
                        "label": f"Family {source_ids[0]}",
                        "semantic_identity": f"family {source_ids[0]}",
                        "shared_question": "How do these sources explain peace?",
                        "bounded_object": "Peace durability",
                        "coherence_rationale": "The sources address one bounded question.",
                        "source_ids": source_ids,
                        "source_roles": {
                            source_id: "core" for source_id in source_ids
                        },
                        "supporting_evidence": [],
                        "propositions": [],
                        "family_relations": [],
                    }
                ]
            }
        return {"clusters": []}

    monkeypatch.setattr(literature, "_reasoner_stage", stage)
    monkeypatch.setattr(literature, "_coverage_audit_plan", lambda *_a, **_k: [])
    build_literature_report(
        profiles,
        reasoner=object(),
        request=_request(Path(".")),
        catalogue_shards=shards,
    )

    proposals = [
        source_ids
        for stage_name, source_ids in calls
        if stage_name == "cluster_proposal"
    ]
    assert proposals
    assert max(map(len, proposals)) <= 25
    assert len(proposals) <= 8
    assert not any(len(source_ids) == 194 for source_ids in proposals)
    reconciliation = [
        source_ids
        for stage_name, source_ids in calls
        if stage_name == "cluster_reconciliation"
    ]
    assert reconciliation == [[]]


def test_cluster_partitions_split_by_measured_profile_size() -> None:
    profiles = [_profile(f"L{index:02d}") for index in range(10)]
    for row in profiles:
        row["findings"][0]["claim"] = "evidence " * 600
    shards = [
        {
            "shard_id": "large",
            "literature_id": "literature-a",
            "source_ids": [row["source_id"] for row in profiles],
        }
    ]

    class SmallContext:
        context_window_tokens = 1_000

    partitions = _cluster_proposal_partitions(
        profiles,
        shards,
        [],
        reasoner=SmallContext(),
        request=None,
    )

    assert len(partitions) > 1
    assert max(len(row["source_ids"]) for row in partitions) < 10
