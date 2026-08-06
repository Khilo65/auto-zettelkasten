from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import EvidenceProfile, LiteratureMapRequest
from auto_zettelkasten.pipeline import (
    _commit_relationship_selection_state,
    _run_relationship_reasoning,
)


class _Reasoner:
    name = "test-provider"
    model = "test-model"
    capabilities = {"capability_identity": "test-capabilities"}

    def select_relationship_candidates(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def select_relationship_bridge_shards(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _Calls:
    run_id = "v0291-discovery"

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.seen: list[str] = []

    def __call__(
        self,
        stage: str,
        _key: str,
        _method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.seen.append(stage)
        return self.handler(stage, profiles, context)


def _profile(source_id: str, collection: str) -> EvidenceProfile:
    return EvidenceProfile(
        source_id=source_id,
        note_id=f"note-{source_id.lower()}",
        context={
            "note_status": "analytical_atomic_note",
            "title": f"Source {source_id}",
            "collections": [collection],
        },
    )


def _catalogue(
    workspace: Path, profiles: Sequence[EvidenceProfile]
) -> dict[str, str]:
    path = workspace / "02_source_memory" / "indexes" / "source_catalogue.yml"
    collections = sorted(
        {
            collection
            for profile in profiles
            for collection in profile.context.get("collections", []) or []
        }
    )
    write_yaml(
        path,
        {
            "literatures": [
                {
                    "literature_id": collection,
                    "title": collection,
                    "source_count": sum(
                        collection in profile.context.get("collections", [])
                        for profile in profiles
                    ),
                }
                for collection in collections
            ],
            "collections": [
                {
                    "key": collection,
                    "direct_source_ids": [
                        profile.source_id
                        for profile in profiles
                        if collection in profile.context.get("collections", [])
                    ],
                    "routing_card": {"title": collection},
                }
                for collection in collections
            ],
            "sources": [
                {
                    "source_id": profile.source_id,
                    "title": profile.context["title"],
                    "collections": profile.context["collections"],
                    "literature_ids": profile.context["collections"],
                }
                for profile in profiles
            ],
        },
    )
    return {"catalogue_path": str(path)}


def _shared_plan(*, include_complement: bool = False) -> dict[str, Any]:
    jobs = [
        {
            "job_id": "requested",
            "family": "requested",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["one", "two"],
            "candidate_quota": 12,
        }
    ]
    families = [{"family_id": "requested", "source_ids": ["A", "B"]}]
    if include_complement:
        jobs.append(
            {
                "job_id": "complement",
                "family": "complement",
                "left_source_ids": ["A"],
                "right_source_ids": ["C"],
                "candidate_quota": 6,
            }
        )
        families.append(
            {"family_id": "complement", "source_ids": ["A", "C"]}
        )
    return {
        "lean_index_hash": "lean",
        "literature_families": families,
        "discovery_jobs": jobs,
    }


def _run(
    workspace: Path,
    profiles: Sequence[EvidenceProfile],
    calls: _Calls,
    *,
    shared_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return _run_relationship_reasoning(
        workspace,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=_catalogue(workspace, profiles),
        reasoner=_Reasoner(),
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=LiteratureMapRequest(
            workspace=workspace,
            provider="test-provider",
            model="test-model",
        ),
        shared_family_plan=shared_plan,
    )


def test_shared_plan_skips_legacy_router_and_settles(tmp_path: Path) -> None:
    profiles = [_profile("A", "one"), _profile("B", "two")]

    def handler(
        stage: str, _profiles: Sequence[Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        assert stage == "relationship_bridge_candidate_selection"
        return {
            "candidates": [],
            "job_outcomes": [
                {
                    "bridge_job_id": context["bridge_jobs"][0]["bridge_job_id"],
                    "status": "no_more_candidates",
                }
            ],
        }

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls, shared_plan=_shared_plan())

    assert "relationship_bridge_shard_selection" not in calls.seen
    assert result["relationship_discovery_status"] == "complete"
    assert result["relationship_discovery_incomplete_jobs"] == []
    assert result["relationship_stage_complete"] is True


def test_legacy_router_remains_the_fallback(tmp_path: Path) -> None:
    profiles = [_profile("A", "one"), _profile("B", "two")]

    def handler(
        stage: str, _profiles: Sequence[Any], _context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if stage == "relationship_bridge_shard_selection":
            return {
                "shard_pairs": [
                    {
                        "left_shard_id": "collection-one",
                        "right_shard_id": "collection-two",
                    }
                ]
            }
        return {"candidates": []}

    calls = _Calls(handler)
    _run(tmp_path, profiles, calls, shared_plan=None)

    assert "relationship_bridge_shard_selection" in calls.seen


def test_named_shared_failure_remains_partial(tmp_path: Path) -> None:
    profiles = [
        _profile("A", "one"),
        _profile("B", "two"),
        _profile("C", "three"),
    ]

    def handler(
        stage: str, _profiles: Sequence[Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if context.get("discovery_pass") == "complement":
            raise ValueError("terminal complementary failure")
        return {
            "candidates": [],
            "job_outcomes": [
                {
                    "bridge_job_id": row["bridge_job_id"],
                    "status": "no_more_candidates",
                }
                for row in context["bridge_jobs"]
            ],
        }

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_plan=_shared_plan(include_complement=True),
    )

    assert result["relationship_discovery_status"] == "partial"
    assert result["relationship_discovery_incomplete_jobs"] == ["complement"]
    assert result["relationship_stage_complete"] is True


def test_settled_shared_state_reconciles_old_false_partial_locally(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A", "one"), _profile("B", "two")]

    def handler(
        _stage: str, _profiles: Sequence[Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            "candidates": [],
            "job_outcomes": [
                {
                    "bridge_job_id": context["bridge_jobs"][0]["bridge_job_id"],
                    "status": "no_more_candidates",
                }
            ],
        }

    first = _run(tmp_path, profiles, _Calls(handler), shared_plan=_shared_plan())
    state_path = _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    assert state_path is not None
    state = read_yaml(state_path, {})
    state["relationship_discovery_status"] = "partial"
    state["relationship_discovery_incomplete_jobs"] = []
    write_yaml(state_path, state)

    replay_calls = _Calls(
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("semantic replay made a provider call")
        )
    )
    replay = _run(
        tmp_path,
        profiles,
        replay_calls,
        shared_plan=_shared_plan(),
    )

    assert replay["semantic_noop"] is True
    assert replay["relationship_discovery_status"] == "complete"
    assert replay["relationship_discovery_incomplete_jobs"] == []
    assert replay_calls.seen == []
