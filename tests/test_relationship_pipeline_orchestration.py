from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import (
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMapRequest,
    RelationshipPairJob,
)
from auto_zettelkasten.profiles import profile_to_dict
from auto_zettelkasten.relationships import (
    ingest_relationship_decision_batch,
    relationship_decision_key,
    stable_hash,
)
from auto_zettelkasten.pipeline import (
    _cluster_membership_relations,
    _commit_relationship_selection_state,
    _ranked_relationship_candidates,
    _run_relationship_reasoning,
)


class _Reasoner:
    name = "test-provider"
    model = "test-model"
    capabilities = {"capability_identity": "test-capabilities"}

    def select_relationship_candidates(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _RoutedReasoner(_Reasoner):
    context_window_tokens = 35_000

    def select_relationship_shards(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _BridgeRoutedReasoner(_Reasoner):
    def select_relationship_bridge_shards(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass


class _Calls:
    run_id = "relationship-run"

    def __init__(
        self,
        handler: Callable[
            [str, Sequence[Any], Mapping[str, Any]], Mapping[str, Any]
        ],
    ) -> None:
        self.handler = handler
        self.seen: list[tuple[str, str, Mapping[str, Any]]] = []

    def __call__(
        self,
        stage: str,
        key: str,
        _method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.seen.append((stage, key, context))
        return self.handler(stage, profiles, context)


def _profile(source_id: str, *, collection: str = "") -> EvidenceProfile:
    return EvidenceProfile(
        source_id=source_id,
        note_id=f"note-{source_id.lower()}",
        context={
            "note_status": "analytical_atomic_note",
            "title": f"Source {source_id}",
            "collections": [collection] if collection else [],
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


def _candidate(
    source_id: str,
    target_id: str,
    *,
    rank: int = 1,
    cross_literature: bool = False,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_id": target_id,
        "why_relevant": "The sources address the same substantive proposition.",
        "rank": rank,
        "cross_literature": cross_literature,
    }


def _decision(
    job: Mapping[str, Any],
    *,
    malformed: bool = False,
) -> dict[str, Any]:
    pair = dict(job["pair"])
    left = str(pair["left_source_id"])
    right = str(pair["right_source_id"])
    left_anchor_id = str(
        job["selected_evidence"]["left"][0]["evidence_anchor_id"]
    )
    right_anchor_id = str(
        job["selected_evidence"]["right"][0]["evidence_anchor_id"]
    )
    return {
        "pair_job_id": job["pair_job_id"],
        "decision": "relationship",
        "pair": pair,
        "relation_type": "supports",
        "actor_source_id": left,
        "reference_source_id": right,
        "forward_label": "supports",
        "inverse_label": "supported by",
        "comparison_proposition": "The works support the same bounded proposition.",
        "reason": "Both sources independently support the proposition.",
        "left_evidence_anchor_ids": [
            "unknown-anchor" if malformed else left_anchor_id
        ],
        "right_evidence_anchor_ids": [right_anchor_id],
        "confidence": "high",
        "output_contract": "relationship-decision-v4",
    }


def _catalogue(
    workspace: Path,
    profiles: Sequence[EvidenceProfile],
    *,
    shards: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    path = workspace / "02_source_memory" / "indexes" / "source_catalogue.yml"
    literature_ids = sorted(
        {
            str(value)
            for profile in profiles
            for value in profile.context.get("collections", []) or []
            if str(value)
        }
    )
    write_yaml(
        path,
        {
            "literatures": [
                {
                    "literature_id": literature_id,
                    "title": literature_id.title(),
                    "scope": f"Sources in {literature_id}.",
                    "source_count": sum(
                        literature_id
                        in set(profile.context.get("collections", []) or [])
                        for profile in profiles
                    ),
                }
                for literature_id in literature_ids
            ],
            "sources": [
                {
                    "source_id": profile.source_id,
                    "title": f"Source {profile.source_id}",
                    "author": f"Author {profile.source_id}",
                    "year": "2020",
                    "thesis": f"Thesis {profile.source_id}",
                    "method": "Comparative analysis.",
                    "source_scope": "full_document",
                    "collections": list(profile.context.get("collections", [])),
                    "literature_ids": list(
                        profile.context.get("collections", [])
                    ),
                }
                for profile in profiles
            ],
            "shards": list(shards),
        },
    )
    return {"catalogue_path": str(path)}


def _request(workspace: Path) -> LiteratureMapRequest:
    return LiteratureMapRequest(
        workspace=workspace,
        provider="test-provider",
        model="test-model",
    )


def _run(
    workspace: Path,
    profiles: Sequence[EvidenceProfile],
    calls: _Calls,
    *,
    reasoner: _Reasoner | None = None,
    catalogue: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _run_relationship_reasoning(
        workspace,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue or _catalogue(workspace, profiles),
        reasoner=reasoner or _Reasoner(),
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=_request(workspace),
    )


def _write_atomic_note(workspace: Path, source_id: str) -> None:
    root = workspace / "02_source_memory" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"Source {source_id}.md").write_text(
        "---\n"
        f"note_id: note-{source_id.lower()}\n"
        f"source_id: {source_id}\n"
        f"zotero_item_key: {source_id}\n"
        "note_status: analytical_atomic_note\n"
        f"title: Source {source_id}\n"
        "---\n"
        f"# Source {source_id}\n\n"
        "## Thesis\n\n"
        f"Complete semantic argument from {source_id}.\n\n"
        "## Graph Links\n\n"
        "<!-- auto-zettelkasten:graph:start -->\n"
        "- [[Generated neighbor]]\n"
        "<!-- auto-zettelkasten:graph:end -->\n",
        encoding="utf-8",
    )


def test_global_discovery_creates_immutable_pair_job(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    _write_atomic_note(tmp_path, "A")
    _write_atomic_note(tmp_path, "B")

    def handler(
        stage: str,
        provider_profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            assert context["discovery_mode"] == "global"
            assert {profile.source_id for profile in provider_profiles} == {"A", "B"}
            assert all(profile.evidence_anchors for profile in provider_profiles)
            assert context["max_inferred_pairs"] == 72
            assert context["reserved_bridge_fraction"] == 0.4
            assert "literature_positions" in context
            assert "existing_graph_neighbors" not in context
            assert "cluster_summaries" not in context
            return {"candidates": [_candidate("A", "B")]}
        assert stage == "relationship_adjudication"
        jobs = context["pair_jobs"]
        assert len(jobs) == 1
        assert jobs[0]["output_contract"] == "relationship-decision-v4"
        assert all(
            jobs[0]["atomic_notes"][side]["markdown"]
            for side in ("left", "right")
        )
        assert all(
            "Complete semantic argument" in jobs[0]["atomic_notes"][side]["markdown"]
            for side in ("left", "right")
        )
        assert all(
            "Generated neighbor" not in jobs[0]["atomic_notes"][side]["markdown"]
            for side in ("left", "right")
        )
        pair_context = jobs[0]["graph_context"]["pair_context"]
        assert pair_context["canonical_pair"] == {
            "left_source_id": "A",
            "right_source_id": "B",
        }
        assert {
            source_id: row["year"]
            for source_id, row in pair_context["endpoint_profiles"].items()
        } == {"A": "2020", "B": "2020"}
        return {"decisions": [_decision(jobs[0])]}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls)

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_candidate_selection",
        "relationship_adjudication",
    ]
    assert len(result["accepted"]) == 1
    assert result["pair_job_count"] == 1
    job_path = next(
        (
            tmp_path
            / "11_state"
            / "runs"
            / calls.run_id
            / "relationship_jobs"
        ).glob("*/input.json")
    )
    before = job_path.read_bytes()

    replay_calls = _Calls(handler)
    replay = _run(tmp_path, profiles, replay_calls)

    assert [
        stage for stage, _key, _context in replay_calls.seen
    ] == ["relationship_candidate_selection"]
    assert replay["provider_batch_count"] == 0
    assert job_path.read_bytes() == before


def test_candidate_cap_reserves_bridge_slots_and_keeps_model_rank() -> None:
    source_ids = {f"W{index}" for index in range(11)} | {
        f"B{index}" for index in range(11)
    }
    entries = {
        source_id: {
            "source_id": source_id,
            "collections": ["within" if source_id.startswith("W") else source_id],
        }
        for source_id in source_ids
    }
    response = {
        "candidates": [
            _candidate("W0", f"W{index}", rank=index)
            for index in range(1, 11)
        ]
        + [
            _candidate("B0", f"B{index}", rank=index, cross_literature=True)
            for index in range(1, 11)
        ]
    }

    selected = _ranked_relationship_candidates(
        response,
        available_source_ids=source_ids,
        entry_by_source=entries,
        excluded_pairs=set(),
        maximum=10,
        bridge_fraction=0.4,
    )

    assert [(row["source_id"], row["target_id"]) for row in selected] == [
        ("B0", "B1"),
        ("B0", "B2"),
        ("B0", "B3"),
        ("B0", "B4"),
        ("W0", "W1"),
        ("W0", "W2"),
        ("W0", "W3"),
        ("W0", "W4"),
        ("W0", "W5"),
        ("W0", "W6"),
    ]


def test_general_and_bridge_discovery_use_separate_candidate_pools(
    tmp_path: Path,
) -> None:
    profiles = [
        _profile("A", collection="mediation"),
        _profile("B", collection="relapse"),
    ]

    def handler(
        stage: str,
        provider_profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            assert context["discovery_mode"] == "global"
            assert context["max_inferred_pairs"] == 72
            return {"candidates": []}
        assert stage == "relationship_bridge_candidate_selection"
        assert context["discovery_mode"] == "bridge_only"
        assert context["max_inferred_pairs"] == 48
        assert provider_profiles == []
        assert {
            row["literature_id"] for row in context["collection_index_cards"]
        } == {"mediation", "relapse"}
        assert [row["source_id"] for row in context["catalogue"]] == ["A", "B"]
        assert all(
            "evidence_anchors" not in row and "catalogue_entry" not in row
            for row in context["catalogue"]
        )
        return {"candidates": []}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls)

    assert {
        stage for stage, _key, _context in calls.seen
    } == {
        "relationship_candidate_selection",
        "relationship_bridge_candidate_selection",
    }
    assert result["pair_job_count"] == 0


def test_bridge_only_ranking_rejects_same_literature_pairs() -> None:
    response = {
        "candidates": [
            _candidate("A", "B", cross_literature=True),
            _candidate("A", "C", rank=2, cross_literature=True),
        ]
    }
    entries = {
        "A": {"literature_ids": ["one"]},
        "B": {"literature_ids": ["one"]},
        "C": {"literature_ids": ["two"]},
    }

    selected = _ranked_relationship_candidates(
        response,
        available_source_ids=set(entries),
        entry_by_source=entries,
        excluded_pairs=set(),
        maximum=48,
        bridge_fraction=1.0,
        scope="bridge",
    )

    assert [(row["source_id"], row["target_id"]) for row in selected] == [
        ("A", "C")
    ]


def test_bridge_only_ranking_retains_the_first_forty_eight_model_candidates() -> None:
    entries = {
        **{
            f"M{index}": {"literature_ids": ["mediation"]}
            for index in range(50)
        },
        **{
            f"R{index}": {"literature_ids": ["relapse"]}
            for index in range(50)
        },
    }
    selected = _ranked_relationship_candidates(
        {
            "candidates": [
                _candidate(
                    f"M{index}",
                    f"R{index}",
                    rank=index + 1,
                    cross_literature=True,
                )
                for index in range(50)
            ]
        },
        available_source_ids=set(entries),
        entry_by_source=entries,
        excluded_pairs=set(),
        maximum=48,
        bridge_fraction=1.0,
        scope="bridge",
    )

    assert len(selected) == 48
    assert [row["rank"] for row in selected] == list(range(1, 49))


def test_bridge_packet_includes_only_resolved_cross_collection_positions(
    tmp_path: Path,
) -> None:
    profiles = [
        _profile("A", collection="mediation"),
        _profile("B", collection="relapse"),
        _profile("C", collection="mediation"),
    ]
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml",
        {
            "positions": [
                {
                    "literature_position_id": "cross",
                    "current_source_id": "A",
                    "matched_source_id": "B",
                    "engagement": "A cites B.",
                },
                {
                    "literature_position_id": "same",
                    "current_source_id": "A",
                    "matched_source_id": "C",
                    "engagement": "A cites C.",
                },
                {
                    "literature_position_id": "missing",
                    "current_source_id": "A",
                    "matched_source_id": "D",
                    "engagement": "A cites missing D.",
                },
            ]
        },
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_bridge_candidate_selection":
            assert [
                row["literature_position_id"]
                for row in context["literature_positions"]
            ] == ["cross"]
            return {"candidates": []}
        if stage == "relationship_adjudication":
            return {"decisions": [_decision(job) for job in context["pair_jobs"]]}
        return {"candidates": []}

    _run(tmp_path, profiles, _Calls(handler))


def test_more_than_two_literatures_use_existing_bridge_shard_router(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]
    catalogue_path = (
        tmp_path / "02_source_memory" / "indexes" / "source_catalogue.yml"
    )
    write_yaml(
        catalogue_path,
        {
            "sources": [
                {
                    "source_id": source_id,
                    "title": f"Source {source_id}",
                    "literature_ids": [f"lit-{source_id.lower()}"],
                }
                for source_id in ("A", "B", "C")
            ],
            "shards": [
                {
                    "shard_id": f"shard-{source_id.lower()}",
                    "literature_id": f"lit-{source_id.lower()}",
                    "source_ids": [source_id],
                    "routing_card": {"title": f"Literature {source_id}"},
                }
                for source_id in ("A", "B", "C")
            ],
        },
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_bridge_shard_selection":
            return {
                "shard_pairs": [
                    {
                        "left_shard_id": "shard-a",
                        "right_shard_id": "shard-b",
                    }
                ]
            }
        if stage in {
            "relationship_candidate_selection",
            "relationship_bridge_candidate_selection",
        }:
            if stage == "relationship_bridge_candidate_selection":
                assert _profiles == []
                assert [row["source_id"] for row in context["catalogue"]] == [
                    "A",
                    "B",
                ]
                assert {
                    row["literature_id"]
                    for row in context["collection_index_cards"]
                } == {"lit-a", "lit-b"}
            return {"candidates": []}
        raise AssertionError((stage, context))

    calls = _Calls(handler)
    _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_BridgeRoutedReasoner(),
        catalogue={"catalogue_path": str(catalogue_path)},
    )

    assert {
        stage for stage, _key, _context in calls.seen
    } == {
        "relationship_candidate_selection",
        "relationship_bridge_shard_selection",
        "relationship_bridge_candidate_selection",
    }


def test_known_collection_membership_overrides_false_model_bridge_flag() -> None:
    entries = {
        "A": {"source_id": "A", "collections": ["Mediation"]},
        "B": {"source_id": "B", "collections": ["Mediation"]},
        "C": {"source_id": "C", "collections": ["Conflict relapse"]},
    }
    selected = _ranked_relationship_candidates(
        {
            "candidates": [
                _candidate("A", "B", rank=1, cross_literature=True),
                _candidate("A", "C", rank=2),
            ]
        },
        available_source_ids=set(entries),
        entry_by_source=entries,
        excluded_pairs=set(),
        maximum=2,
        bridge_fraction=0.5,
    )

    assert [(row["source_id"], row["target_id"]) for row in selected] == [
        ("A", "C"),
        ("A", "B"),
    ]


def test_endpoint_owned_profile_anchor_need_not_be_preselected() -> None:
    left = _profile("A")
    right = _profile("B")
    extra = EvidenceAnchor(
        evidence_anchor_id="anchor-a-extra",
        source_id="A",
        claim="A second source-owned claim.",
        locator="p. 11",
        support_envelope={
            "support_status": "supported",
            "coverage": "full_text",
        },
    )
    left.evidence_anchors.append(extra)
    job = RelationshipPairJob(
        left_source_id="A",
        right_source_id="B",
        selected_evidence={
            "left": [left.evidence_anchors[0].to_dict()],
            "right": [right.evidence_anchors[0].to_dict()],
        },
    )
    decision = _decision(job.to_dict())
    decision["left_evidence_anchor_ids"] = [extra.evidence_anchor_id]

    result = ingest_relationship_decision_batch(
        {"decisions": [decision]},
        pair_jobs=[job],
        profiles=[left, right],
    )

    assert len(result["accepted"]) == 1
    assert result["parked"] == []


def test_pair_jobs_are_bounded_into_eight_row_transport_batches(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index:02d}") for index in range(13)]]
    candidates = [
        _candidate("A", f"S{index:02d}", rank=index + 1)
        for index in range(13)
    ]
    batch_sizes: list[int] = []

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": candidates}
        jobs = context["pair_jobs"]
        batch_sizes.append(len(jobs))
        return {"decisions": [_decision(job) for job in jobs]}

    result = _run(tmp_path, profiles, _Calls(handler))

    assert sorted(batch_sizes) == [5, 8]
    assert result["provider_batch_count"] == 2
    assert result["pair_job_count"] == 13
    assert len(result["accepted"]) == 13
    batch_files = list(
        (
            tmp_path
            / "11_state"
            / "runs"
            / _Calls.run_id
            / "relationship_batches"
        ).glob("*/batch.yml")
    )
    assert len(batch_files) == 2
    assert {
        read_yaml(path)["status"] for path in batch_files
    } == {"completed"}


def test_malformed_decision_parks_only_its_pair(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B"), _profile("C")]

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    _candidate("A", "B"),
                    _candidate("A", "C", rank=2),
                ]
            }
        jobs = context["pair_jobs"]
        return {
            "decisions": [
                _decision(jobs[0]),
                _decision(jobs[1], malformed=True),
            ]
        }

    result = _run(tmp_path, profiles, _Calls(handler))

    assert len(result["accepted"]) == 1
    assert len(result["parked"]) == 1
    assert result["parked"][0]["reason"] == "left_anchor_not_owned_by_left_source"
    parked_job_id = result["parked"][0]["pair_job_id"]
    parked_status = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / _Calls.run_id
        / "relationship_jobs"
        / parked_job_id
        / "status.yml"
    )
    assert parked_status["status"] == "parked_for_review"


def test_failed_batch_preserves_sibling_batches_and_job_status(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index:02d}") for index in range(13)]]
    call_number = 0

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal call_number
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    _candidate("A", f"S{index:02d}", rank=index + 1)
                    for index in range(13)
                ]
            }
        call_number += 1
        if call_number == 2:
            raise RuntimeError("provider batch failed")
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(tmp_path, profiles, _Calls(handler))

    assert 0 < len(result["accepted"]) < 13
    statuses = [
        read_yaml(path)
        for path in (
            tmp_path
            / "11_state"
            / "runs"
            / _Calls.run_id
            / "relationship_jobs"
        ).glob("*/status.yml")
    ]
    completed = sum(row["status"] == "completed" for row in statuses)
    parked = sum(row["status"] == "parked_for_review" for row in statuses)
    assert completed == len(result["accepted"])
    assert completed + parked == 13


def test_transport_failed_batch_resumes_without_losing_completed_decisions(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index:02d}") for index in range(13)]]
    selection_state = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml"
    )
    write_yaml(
        selection_state,
        {
            "state_schema_version": "3",
            "profile_hashes": {
                profile.source_id: stable_hash(profile_to_dict(profile))
                for profile in profiles
            },
            "relationship_memory_hashes": {},
            "reconciled_catalogue_revision": "prior",
            "catalogue_revision": "prior",
            "selection_identity": "prior-prompt",
        },
    )
    first_batch_number = 0

    def first_handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal first_batch_number
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    _candidate("A", f"S{index:02d}", rank=index + 1)
                    for index in range(13)
                ]
            }
        first_batch_number += 1
        if first_batch_number == 2:
            raise TimeoutError("provider timed out")
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    first = _run(tmp_path, profiles, _Calls(first_handler))

    assert first["relationship_stage_complete"] is False
    assert first["selected_profile_hashes"] == {}
    assert len(first["accepted"]) in {5, 8}
    assert sum(
        bool(row.get("retry_on_resume")) for row in first["parked"]
    ) == 13 - len(first["accepted"])
    assert (
        _commit_relationship_selection_state(
            tmp_path,
            first,
            catalogue_revision=first["reconciled_catalogue_revision"],
        )
        is None
    )
    assert read_yaml(selection_state)["selection_identity"] == "prior-prompt"

    resumed_calls = _Calls(
        lambda stage, _profiles, context: (
            {
                "candidates": [
                    _candidate("A", f"S{index:02d}", rank=index + 1)
                    for index in range(13)
                ]
            }
            if stage == "relationship_candidate_selection"
            else {"decisions": [_decision(job) for job in context["pair_jobs"]]}
        )
    )
    resumed = _run(tmp_path, profiles, resumed_calls)

    assert resumed["relationship_stage_complete"] is True
    assert resumed["accounted_pair_job_count"] == resumed["pair_job_count"] == 13
    assert len(resumed["accepted"]) == 13
    assert [
        stage for stage, _key, _context in resumed_calls.seen
    ].count("relationship_adjudication") == 1


def test_duplicate_provider_rows_park_the_pair_without_caching_an_edge(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": [_candidate("A", "B")]}
        row = _decision(context["pair_jobs"][0])
        return {"decisions": [row, {**row, "reason": "Conflicting duplicate."}]}

    result = _run(tmp_path, profiles, _Calls(handler))

    assert result["accepted"] == []
    assert result["relationship_stage_complete"] is True
    assert [row["reason"] for row in result["parked"]] == [
        "duplicate_pair_job_decision"
    ]
    assert not list(
        (tmp_path / "11_state" / "relationship_jobs").glob("*/result.json")
    )


def test_terminal_discovery_failure_is_replayable_but_does_not_enter_clusters(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    result = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, _context: (
                (_ for _ in ()).throw(ValueError("invalid discovery contract"))
                if stage == "relationship_candidate_selection"
                else {"candidates": []}
            )
        ),
    )

    assert result["relationship_stage_complete"] is False
    assert result["relationship_retry_on_resume"] is False
    assert set(result["selected_profile_hashes"]) == {"A", "B"}
    assert (
        _commit_relationship_selection_state(
            tmp_path,
            result,
            catalogue_revision=result["reconciled_catalogue_revision"],
        )
        is not None
    )
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )
    replay = _run(tmp_path, profiles, replay_calls)
    assert replay_calls.seen == []
    assert replay["relationship_stage_complete"] is False
    assert replay["relationship_retry_on_resume"] is False
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )

    replay = _run(tmp_path, profiles, replay_calls)

    assert replay_calls.seen == []
    assert replay["relationship_stage_complete"] is False
    assert replay["relationship_retry_on_resume"] is False


def test_committed_selection_state_makes_unchanged_replay_call_free(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]

    def first_handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": [_candidate("A", "B")]}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    first = _run(tmp_path, profiles, _Calls(first_handler))
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision="different-public-catalogue-revision",
    )
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )

    replay = _run(tmp_path, profiles, replay_calls)

    assert replay_calls.seen == []
    assert replay["pair_job_count"] == 0
    assert replay["provider_batch_count"] == 0


def test_changed_literature_position_reopens_pair_without_reusing_decision(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    positions_path = (
        tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml"
    )

    def write_position(engagement: str) -> None:
        write_yaml(
            positions_path,
            {
                "positions": [
                    {
                        "literature_position_id": "position-a-b",
                        "current_source_id": "A",
                        "matched_source_id": "B",
                        "raw_citation": "Source B",
                        "engagement": engagement,
                    }
                ]
            },
        )

    job_ids: list[str] = []

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {"candidates": []}
        job_ids.append(str(context["pair_jobs"][0]["pair_job_id"]))
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    write_position("Source A builds on Source B.")
    first = _run(tmp_path, profiles, _Calls(handler))
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision="catalogue-revision",
    )

    write_position("Source A challenges Source B.")
    second_calls = _Calls(handler)
    second = _run(tmp_path, profiles, second_calls)

    assert [stage for stage, _key, _context in second_calls.seen] == [
        "relationship_candidate_selection",
        "relationship_adjudication",
    ]
    assert len(job_ids) == 2
    assert job_ids[0] != job_ids[1]
    assert second["pair_job_count"] == 1


def test_large_catalogue_routes_to_selected_shards_before_discovery(
    tmp_path: Path,
) -> None:
    profiles = [_profile(f"S{index:03d}") for index in range(40)]
    shards = [
        {
            "shard_id": "shard-selected",
            "literature_id": "selected",
            "source_ids": ["S000", "S001"],
            "routing_card": {
                "title": "Selected literature",
                "representative_theses": ["A relevant argument."],
            },
        },
        {
            "shard_id": "shard-other",
            "literature_id": "other",
            "source_ids": [f"S{index:03d}" for index in range(2, 40)],
            "routing_card": {
                "title": "Other literature",
                "representative_theses": ["Other arguments."],
            },
        },
    ]
    catalogue = _catalogue(tmp_path, profiles, shards=shards)

    def handler(
        stage: str,
        provider_profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_shard_selection":
            assert not provider_profiles
            return {"shard_ids": ["shard-selected"]}
        if stage == "relationship_candidate_selection":
            assert context["discovery_mode"] == "routed_shards"
            assert {
                row["source_id"] for row in context["catalogue"]
            } == {"S000", "S001"}
            assert {
                profile.source_id for profile in provider_profiles
            } == {"S000", "S001"}
            assert all(profile.evidence_anchors for profile in provider_profiles)
            return {"candidates": [_candidate("S000", "S001")]}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_RoutedReasoner(),
        catalogue=catalogue,
    )

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_shard_selection",
        "relationship_candidate_selection",
        "relationship_adjudication",
    ]
    assert len(result["accepted"]) == 1


def test_pair_batches_split_on_measured_context_size(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index}") for index in range(5)]]
    for profile in profiles:
        profile.evidence_anchors = [
            EvidenceAnchor(
                evidence_anchor_id=f"large-anchor-{profile.source_id}",
                source_id=profile.source_id,
                claim=f"Claim for {profile.source_id}. " + "evidence " * 180,
                locator="p. 10",
                support_envelope={
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            )
        ]
    reasoner = _RoutedReasoner()
    reasoner.context_window_tokens = 10_000
    batch_sizes: list[int] = []

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    _candidate("A", f"S{index}", rank=index + 1)
                    for index in range(5)
                ]
            }
        jobs = context["pair_jobs"]
        batch_sizes.append(len(jobs))
        return {"decisions": [_decision(job) for job in jobs]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=reasoner,
    )

    assert len(batch_sizes) > 1
    assert sum(batch_sizes) == 5
    assert len(result["accepted"]) == 5


def test_mandatory_pairs_fail_preflight_before_any_provider_call(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index}") for index in range(9)]]
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "relations": [
                {
                    "relation_id": f"explicit-{index}",
                    "source_id": "A",
                    "target_source_id": f"S{index}",
                    "relation_type": "zotero_related",
                    "active": True,
                }
                for index in range(9)
            ]
        },
    )
    calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected provider call: {stage}")
        )
    )
    calls.max_calls = 1
    calls.cumulative_provider_calls = 0

    result = _run(tmp_path, profiles, calls)

    assert calls.seen == []
    assert len(result["parked"]) == 9
    assert {
        row["reason"] for row in result["parked"]
    } == {"mandatory_relationship_budget_conflict"}


def test_unchanged_negative_pair_memory_skips_readjudication(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
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
        {
            "pair_decisions": [
                {
                    "decision_key": decision_key,
                    "source_id": "A",
                    "target_source_id": "B",
                    "status": "no_relationship",
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
        return {"candidates": [_candidate("A", "B")]}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls)

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_candidate_selection"
    ]
    assert result["pair_job_count"] == 0
    assert result["accepted"] == []
    assert result["no_relationship"] == []


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
