from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

import auto_zettelkasten.pipeline as pipeline_module
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import (
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    RelationshipPairJob,
)
from auto_zettelkasten.profiles import profile_to_dict
from auto_zettelkasten.readers import ProviderTransportError
from auto_zettelkasten.relationships import (
    ingest_relationship_decision_batch,
    relationship_decision_key,
    stable_hash,
)
from auto_zettelkasten.pipeline import (
    _allocate_complementary_candidate_quotas,
    _balance_complementary_jobs,
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


class _V6Reasoner(_Reasoner):
    relationship_decision_contract = "relationship-decision-v6"


class _V8Reasoner(_Reasoner):
    relationship_decision_contract = "relationship-decision-v8"


class _BuiltInDeepSeekReasoner(_Reasoner):
    name = "deepseek"
    profile_generation_route = "built_in_reader"


class _CodexReasoner(_Reasoner):
    name = "codex"


class _BridgeRoutedReasoner(_Reasoner):
    def select_relationship_bridge_shards(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        pass


class _V6BridgeRoutedReasoner(_V6Reasoner):
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
        if hasattr(self, "cumulative_provider_calls"):
            self.cumulative_provider_calls += 1
        return self.handler(stage, profiles, context)


def _profile(source_id: str, *, collection: str = "") -> EvidenceProfile:
    return EvidenceProfile(
        profile_schema_version="1.3",  # Explicit legacy fixtures exercise v6/v8 evidence contracts.
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
    left, right = sorted((source_id, target_id))
    return {
        "left_source_id": left,
        "right_source_id": right,
        "comparison_proposition": "The sources address the same proposition.",
        "why_compare": "A full-note comparison may clarify the proposition.",
        "bridge_family": "shared proposition",
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


def _v6_decision(job: Mapping[str, Any]) -> dict[str, Any]:
    pair = dict(job["pair"])
    left = str(pair["left_source_id"])
    right = str(pair["right_source_id"])
    return {
        "decision": "relationship",
        "relation_type": "supports",
        "actor_source_id": left,
        "reference_source_id": right,
        "comparison_proposition": "The works support the same bounded proposition.",
        "reason": "Both sources independently support the proposition.",
        "left_evidence_anchor_ids": [
            job["selected_evidence"]["left"][0]["evidence_anchor_id"]
        ],
        "right_evidence_anchor_ids": [
            job["selected_evidence"]["right"][0]["evidence_anchor_id"]
        ],
        "confidence": "high",
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
            "collections": [
                {
                    "key": literature_id,
                    "parent_key": "",
                    "direct_source_ids": [
                        profile.source_id
                        for profile in profiles
                        if literature_id
                        in set(profile.context.get("collections", []) or [])
                    ],
                    "routing_card": {
                        "title": literature_id.title(),
                        "scope": f"Sources in {literature_id}.",
                    },
                }
                for literature_id in literature_ids
            ],
            "shards": list(shards),
            "virtual_shards": list(shards),
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
    shared_family_plan: Mapping[str, Any] | None = None,
    request: LiteratureMapRequest | None = None,
    frozen_pair_jobs: Sequence[RelationshipPairJob] | None = None,
) -> dict[str, Any]:
    return _run_relationship_reasoning(
        workspace,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue or _catalogue(workspace, profiles),
        reasoner=reasoner or _Reasoner(),
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=request or _request(workspace),
        shared_family_plan=shared_family_plan,
        frozen_pair_jobs=frozen_pair_jobs,
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


@pytest.mark.parametrize("clusters_enabled", [False, True])
def test_unclustered_source_remains_discoverable_and_replays(
    tmp_path: Path, clusters_enabled: bool,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    metadata_only = _profile("E")
    metadata_only.context["note_status"] = "metadata_only"
    profiles.append(metadata_only)
    plan = {
        "lean_index_hash": "lean",
        "literature_families": [{"family_id": "family", "source_ids": ["A", "B", "C"]}],
        "discovery_jobs": [{
            "job_id": "family", "family": "family", "left_source_ids": ["A"],
            "right_source_ids": ["B", "C"], "candidate_quota": 3,
        }],
        "source_dispositions": [{
            "source_id": "D", "disposition": "currently_unclustered", "family_ids": [],
        }],
    }
    original_plan = json.dumps(plan, sort_keys=True)
    request = LiteratureMapRequest(
        workspace=tmp_path, provider="test-provider", model="test-model",
        literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=clusters_enabled),
    )

    def handler(stage, _profiles, context):
        if stage == "relationship_adjudication":
            return {"decisions": [_decision(job) for job in context["pair_jobs"]]}
        candidates = []
        for job in context["bridge_jobs"]:
            endpoints = set(job["left_source_ids"]) | set(job["right_source_ids"])
            if "D" in endpoints:
                assert endpoints == set("ABCD")
                candidates.append({**_candidate("A", "D"), "bridge_job_id": job["bridge_job_id"]})
        return {"candidates": candidates, "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ]}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls, shared_family_plan=plan, request=request)
    assert result["pair_job_count"] == 1
    assert result["relationship_stage_complete"] is True
    assert json.dumps(plan, sort_keys=True) == original_plan
    _commit_relationship_selection_state(
        tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"],
    )
    selection_state = Path(result["state_path"])
    before = (selection_state.read_bytes(), selection_state.stat().st_mtime_ns)
    replay_calls = _Calls(lambda *_args: pytest.fail("unexpected replay call"))
    replay = _run(tmp_path, profiles, replay_calls, shared_family_plan=plan, request=request)
    assert replay["semantic_noop"] is True
    assert replay_calls.seen == []
    assert (selection_state.read_bytes(), selection_state.stat().st_mtime_ns) == before


@pytest.mark.parametrize("context_budget", [1_000_000, 15_000])
def test_unclustered_source_coverage_quota_is_shared_across_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context_budget: int,
) -> None:
    peer_ids = [f"A{index:02}" for index in range(64)]
    profiles = [_profile(source_id) for source_id in [*peer_ids, "Z"]]
    monkeypatch.setattr(pipeline_module, "_relationship_context_char_budget", lambda *_args: context_budget)
    coverage_jobs = []

    def handler(stage, _profiles, context):
        assert stage.endswith("candidate_selection")
        for job in context["bridge_jobs"]:
            if "Z" in job["left_source_ids"] or "Z" in job["right_source_ids"]:
                assert job["left_source_ids"] == ["Z"]
                assert set(job["right_source_ids"]) <= set(peer_ids)
                assert not job["family_source_ids"]
                coverage_jobs.append(job)
        return {"candidates": [], "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ]}

    result = _run(tmp_path, profiles, _Calls(handler), shared_family_plan={
        "lean_index_hash": "lean",
        "literature_families": [{"family_id": "family", "source_ids": peer_ids}],
        "discovery_jobs": [{
            "job_id": "family", "family": "family", "left_source_ids": peer_ids[:1],
            "right_source_ids": peer_ids[1:], "candidate_quota": 1,
        }],
    })
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert coverage_jobs
    if context_budget == 15_000:
        assert len(coverage_jobs) > 1
    assert sum(job["target_candidate_count"] for job in coverage_jobs) <= 24


@pytest.mark.parametrize("clusters_enabled", [False, True])
@pytest.mark.parametrize("select_neighbors", [False, True])
def test_neighbor_families_are_compared_and_neighbor_changes_invalidate_replay(
    tmp_path: Path, clusters_enabled: bool, select_neighbors: bool,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    metadata = _profile("M")
    metadata.context["note_status"] = "metadata_only"
    profiles.append(metadata)
    families = [
        {"family_id": "one", "source_ids": ["A", "B", "M"], "supersedes_family_ids": ["old-one"]},
        {"family_id": "two", "source_ids": ["C", "D"]},
        {"family_id": "three", "source_ids": ["E", "F"]},
    ]
    plan = {
        "lean_index_hash": "lean",
        "literature_families": families,
        "discovery_jobs": [
            {"job_id": family["family_id"], "family": family["family_id"],
             "left_source_ids": family["source_ids"][:1],
             "right_source_ids": family["source_ids"][1:], "candidate_quota": 3}
            for family in families
        ],
        "neighboring_families": [
            {"left_family_id": "old-one", "right_family_id": "two", "reason": "Different measures of one construct."},
            {"left_family_id": "two", "right_family_id": "one", "reason": "The same comparison in reverse."},
        ],
    }
    request = LiteratureMapRequest(
        workspace=tmp_path, provider="test-provider", model="test-model",
        literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=clusters_enabled),
    )
    compared = set()
    neighbor_jobs = []

    def handler(stage, _profiles, context):
        if stage == "relationship_adjudication":
            compared.update(tuple(sorted(job["pair"].values())) for job in context["pair_jobs"])
            return {"decisions": [_decision(job) for job in context["pair_jobs"]]}
        candidates = []
        for job in context["bridge_jobs"]:
            if job["bridge_job_id"].startswith("neighbor-coverage-"):
                assert not job["family_source_ids"]
                neighbor_jobs.append(job)
                if not select_neighbors:
                    continue
            for left in job["left_source_ids"]:
                for right in job["right_source_ids"]:
                    assert "M" not in (left, right)
                    candidates.append({**_candidate(left, right), "bridge_job_id": job["bridge_job_id"]})
        return {"candidates": candidates, "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ]}

    original_plan = json.dumps(plan, sort_keys=True)
    result = _run(tmp_path, profiles, _Calls(handler), shared_family_plan=plan, request=request)
    assert json.dumps(plan, sort_keys=True) == original_plan
    assert compared == {("A", "B"), ("C", "D"), ("E", "F")} | (
        {("A", "C"), ("A", "D"), ("B", "C"), ("B", "D")} if select_neighbors else set()
    )
    assert result["relationship_stage_complete"] is True
    assert len(neighbor_jobs) == 1
    _commit_relationship_selection_state(tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"])
    plan["neighboring_families"].append({"left_family_id": "two", "right_family_id": "three", "reason": "A new bounded comparison."})
    compared.clear()
    changed = _run(tmp_path, profiles, _Calls(handler), shared_family_plan=plan, request=request)
    assert changed["reconciled_catalogue_revision"] != result["reconciled_catalogue_revision"]
    assert compared == ({("C", "E"), ("C", "F"), ("D", "E"), ("D", "F")} if select_neighbors else set())
    assert changed["relationship_stage_complete"] is True
    _commit_relationship_selection_state(tmp_path, changed, catalogue_revision=changed["reconciled_catalogue_revision"])
    state = Path(changed["state_path"])
    before = (state.read_bytes(), state.stat().st_mtime_ns)
    replay = _run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("unexpected replay call")), shared_family_plan=plan, request=request)
    assert replay["semantic_noop"] is True
    assert (state.read_bytes(), state.stat().st_mtime_ns) == before


@pytest.mark.parametrize("context_budget", [1_000_000, 15_000])
def test_neighbor_candidate_quota_stays_bounded_when_packets_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context_budget: int,
) -> None:
    left = [f"L{index:02}" for index in range(32)]
    right = [f"R{index:02}" for index in range(32)]
    profiles = [_profile(source_id) for source_id in [*left, *right]]
    monkeypatch.setattr(pipeline_module, "_relationship_context_char_budget", lambda *_args: context_budget)
    neighbor_jobs = []

    def handler(stage, _profiles, context):
        assert stage.endswith("candidate_selection")
        for job in context["bridge_jobs"]:
            if job["bridge_job_id"].startswith("neighbor-coverage-"):
                assert set(job["left_source_ids"]) <= set(left)
                assert set(job["right_source_ids"]) <= set(right)
                assert not job["family_source_ids"]
                neighbor_jobs.append(job)
        return {"candidates": [], "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ]}

    result = _run(tmp_path, profiles, _Calls(handler), shared_family_plan={
        "lean_index_hash": "lean",
        "literature_families": [
            {"family_id": "left", "source_ids": left},
            {"family_id": "right", "source_ids": right},
        ],
        "discovery_jobs": [
            {"job_id": name, "family": name, "left_source_ids": ids[:1],
             "right_source_ids": ids[1:], "candidate_quota": 1}
            for name, ids in [("left", left), ("right", right)]
        ],
        "neighboring_families": [{"left_family_id": "left", "right_family_id": "right"}],
    })
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert result["pair_job_count"] == 0
    assert neighbor_jobs
    assert sum(job["target_candidate_count"] for job in neighbor_jobs) <= 24
    if context_budget == 15_000:
        assert len(neighbor_jobs) > 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unresolved_neighbor_is_not_silently_reported_covered(tmp_path: Path, ambiguous: bool) -> None:
    families = [
        {"family_id": "one", "label": "ambiguous", "source_ids": ["A", "B"]},
        {"family_id": "two", "label": "ambiguous", "source_ids": ["C", "D"]},
    ]
    with pytest.raises(ValueError, match="unresolved neighboring family reference"):
        _run(tmp_path, [_profile(s) for s in "ABCD"],
             _Calls(lambda *_args: pytest.fail("unresolved routing must stop before discovery")),
             shared_family_plan={
                 "lean_index_hash": "lean", "literature_families": families,
                 "discovery_jobs": [], "neighboring_families": [{
                     "left_family_id": "ambiguous" if ambiguous else "missing",
                     "right_family_id": "two",
                 }],
             })


def test_mutual_unclustered_job_does_not_cover_library_connections(tmp_path: Path) -> None:
    calls = _Calls(lambda _stage, _profiles, context: {
        "candidates": [], "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ],
    })
    result = _run(tmp_path, [_profile(s) for s in "ABCD"], calls, shared_family_plan={
        "lean_index_hash": "lean",
        "literature_families": [{"family_id": "one", "source_ids": ["A", "B"]}],
        "discovery_jobs": [
            {"job_id": "assigned", "family": "one", "left_source_ids": ["A"], "right_source_ids": ["B"]},
            {"job_id": "mutual", "family": "", "left_source_ids": ["C"], "right_source_ids": ["D"]},
        ],
    })
    covered = {(left, right) for _, _, context in calls.seen for job in context["bridge_jobs"]
               for left in job["left_source_ids"] for right in job["right_source_ids"]
               if job["bridge_job_id"].startswith("source-coverage-")}
    assert covered == {("C", "A"), ("C", "B"), ("D", "A"), ("D", "B")}
    assert result["relationship_stage_complete"] is True


@pytest.mark.parametrize(
    "left,right,needs_neighbor_job",
    [("AB", "CD", False), ("CD", "AB", False), ("AC", "BD", True)],
)
def test_neighbor_deduplication_compares_sides_not_endpoint_unions(
    tmp_path: Path, left: str, right: str, needs_neighbor_job: bool,
) -> None:
    calls = _Calls(lambda _stage, _profiles, context: {
        "candidates": [], "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]
        ],
    })
    result = _run(tmp_path, [_profile(source_id) for source_id in "ABCD"], calls, shared_family_plan={
        "lean_index_hash": "lean",
        "literature_families": [
            {"family_id": "one", "source_ids": ["A", "B"]},
            {"family_id": "two", "source_ids": ["C", "D"]},
        ],
        "discovery_jobs": [{
            "job_id": "existing", "family": "", "left_source_ids": list(left),
            "right_source_ids": list(right), "candidate_quota": 4,
            "endpoint_coverage_only": True,
        }],
        "neighboring_families": [{"left_family_id": "one", "right_family_id": "two"}],
    })
    jobs = [job for _, _, context in calls.seen for job in context["bridge_jobs"]
            if job["bridge_job_id"].startswith("neighbor-coverage-")]
    assert bool(jobs) is needs_neighbor_job
    assert result["relationship_stage_complete"] is True


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
            assert all(not profile.evidence_anchors for profile in provider_profiles)
            assert context["max_inferred_pairs"] == 1
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


def test_builtin_global_discovery_uses_one_complete_response(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABC"]

    def handler(stage, _profiles, context):
        if stage == "relationship_candidate_selection":
            assert context["max_inferred_pairs"] == 3
            return {"candidates": [_candidate("A", "B")]}
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_BuiltInDeepSeekReasoner(),
    )

    assert result["pair_job_count"] == 1
    assert result["relationship_stage_complete"] is True
    assert sum(
        stage == "relationship_candidate_selection"
        for stage, _key, _context in calls.seen
    ) == 1


def test_completed_discovery_response_does_not_start_serial_continuation(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABC"]
    candidate_calls = 0

    def handler(stage, _profiles, context):
        nonlocal candidate_calls
        if stage == "relationship_candidate_selection":
            candidate_calls += 1
            if candidate_calls == 1:
                return {"candidates": [_candidate("A", "B")]}
            raise TimeoutError("provider request timed out")
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=_BuiltInDeepSeekReasoner(),
    )

    assert len(result["accepted"]) == 1
    assert result["pair_job_count"] == 1
    assert result["relationship_stage_complete"] is True
    assert result["relationship_retry_on_resume"] is False
    assert candidate_calls == 1


def test_shared_plan_keeps_family_discovery_in_separate_packets(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEFGH"]
    calls = _Calls(
        lambda stage, _profiles, _context: (
            {"candidates": []}
            if stage.endswith("candidate_selection")
            else {"decisions": []}
        )
    )
    families = [
        {
            "family_id": f"family-{index}",
            "source_ids": [left, right],
        }
        for index, (left, right) in enumerate(
            (("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")),
            start=1,
        )
    ]
    jobs = [
        {
            "job_id": f"job-{index}",
            "family": f"family-{index}",
            "left_source_ids": [left],
            "right_source_ids": [right],
            "candidate_quota": 12,
            "requested_collection_pair": (
                ["C1", "C2"] if index > 2 else []
            ),
        }
        for index, (left, right) in enumerate(
            (("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")),
            start=1,
        )
    ]

    _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": families,
            "discovery_jobs": jobs,
        },
    )

    discovery_calls = [
        row for row in calls.seen if row[0].endswith("candidate_selection")
    ]
    assert len(discovery_calls) == 2
    assert [row[2]["discovery_pass"] for row in discovery_calls] == [
        "broad",
        "complement",
    ]
    assert discovery_calls[1][2]["prior_candidate_pairs"] == []


@pytest.mark.parametrize("quota", [1, 2, 6])
def test_complementary_family_breadth_covers_pairs_inside_planner_sides(
    tmp_path: Path, quota: int,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    discovery_passes: list[str] = []

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            discovery_pass = str(context["discovery_pass"])
            discovery_passes.append(discovery_pass)
            job = context["bridge_jobs"][0]
            job_id = str(job["bridge_job_id"])
            assert context["literature_families"][0]["family_id"] == "family-one"
            if discovery_pass == "complement":
                pairs = (("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"))
            else:
                assert set(job["left_source_ids"]) == set("ABCD")
                assert set(job["right_source_ids"]) == set("ABCD")
                assert {
                    tuple(pair) for pair in context["excluded_candidate_pairs"]
                } >= {
                    ("A", "C"),
                    ("A", "D"),
                    ("B", "C"),
                    ("B", "D"),
                }
                pairs = (("A", "B"), ("C", "D"))
            return {
                "candidates": [
                    {**_candidate(left, right, rank=index), "bridge_job_id": job_id}
                    for index, (left, right) in enumerate(pairs, start=1)
                ],
                "job_outcomes": [
                    {"bridge_job_id": job_id, "status": "completed"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {
                    "family_id": "family-one",
                    "label": "Named family",
                    "source_ids": list("ABCD"),
                }
            ],
            "discovery_jobs": [
                {
                    "job_id": "family-job",
                    "family": "Named family",
                    "left_source_ids": ["A", "B"],
                    "right_source_ids": ["C", "D"],
                    "candidate_quota": quota,
                }
            ],
        },
    )

    assert discovery_passes == ["complement", "breadth_completion"]
    assert result["pair_job_count"] == 6
    accounting = next(
        row
        for row in result["relationship_discovery_jobs"]
        if row["bridge_job_id"] == "family-job"
    )
    assert accounting["planner_target_candidates"] == quota
    assert accounting["valid_unique_candidates"] == 6
    assert accounting["breadth_added_unique_candidates"] == 2
    assert accounting["planner_target_met"] is True


@pytest.mark.parametrize(("page_size", "expected_pairs"), [(64, 6), (5, 5)])
def test_small_family_no_more_still_adjudicates_every_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
    expected_pairs: int,
) -> None:
    monkeypatch.setattr(
        pipeline_module, "_RELATIONSHIP_DISCOVERY_PAGE_SIZE", page_size
    )
    profiles = [_profile(source_id) for source_id in "ABCD"]
    discovery_passes: list[str] = []

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            discovery_pass = str(context["discovery_pass"])
            discovery_passes.append(discovery_pass)
            job_id = str(context["bridge_jobs"][0]["bridge_job_id"])
            pairs = (
                (("A", "B"), ("A", "C"), ("A", "D"))
                if discovery_pass == "complement"
                else (("B", "C"), ("B", "D"))
            )
            return {
                "candidates": [
                    {**_candidate(left, right, rank=index), "bridge_job_id": job_id}
                    for index, (left, right) in enumerate(pairs, start=1)
                ],
                "job_outcomes": [
                    {"bridge_job_id": job_id, "status": "no_more_candidates"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {
                "family_id": "family-one",
                "source_ids": list("ABCD"),
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [
            {
                "job_id": "family-job",
                "family": "family-one",
                "left_source_ids": ["A"],
                "right_source_ids": ["B", "C", "D"],
                "candidate_quota": 6,
            }
        ],
    }
    result = _run(
        tmp_path, profiles, _Calls(handler), shared_family_plan=plan
    )

    assert discovery_passes == ["complement", "breadth_completion"]
    assert result["pair_job_count"] == expected_pairs
    accounting = result["relationship_discovery_jobs"][0]
    assert accounting["planner_target_candidates"] == 6
    assert accounting["valid_unique_candidates"] == expected_pairs
    assert accounting.get("deterministic_family_completion_count", 0) == (
        1 if page_size == 64 else 0
    )
    assert accounting["planner_target_met"] is (page_size == 64)
    if page_size == 64:
        _commit_relationship_selection_state(
            tmp_path,
            result,
            catalogue_revision=result["reconciled_catalogue_revision"],
        )
        replay_calls = _Calls(
            lambda *_args: pytest.fail("unexpected replay call")
        )
        replay = _run(
            tmp_path, profiles, replay_calls, shared_family_plan=plan
        )
        assert replay_calls.seen == []
        assert replay["semantic_noop"] is True

        monkeypatch.setattr(
            pipeline_module,
            "_RELATIONSHIP_DISCOVERY_POLICY_VERSION",
            "quota-independent-family-coverage-v302",
        )
        upgraded_calls = _Calls(handler)
        upgraded = _run(
            tmp_path, profiles, upgraded_calls, shared_family_plan=plan
        )
        assert upgraded_calls.seen
        assert upgraded.get("semantic_noop") is not True


def test_codex_discovery_transport_failure_is_not_retried_as_breadth(
    tmp_path: Path,
) -> None:
    def handler(*_args: Any) -> Mapping[str, Any]:
        raise ProviderTransportError("disconnected", transport_kind="codex_cli")

    calls = _Calls(handler)
    with pytest.raises(ProviderTransportError, match="disconnected"):
        _run(
            tmp_path,
            [_profile(source_id) for source_id in "ABCD"],
            calls,
            reasoner=_CodexReasoner(),
            request=LiteratureMapRequest(
                workspace=tmp_path,
                provider="codex",
                model="gpt-5.6-terra",
            ),
            shared_family_plan={
                "lean_index_hash": "lean",
                "literature_families": [
                    {
                        "family_id": "family-one",
                        "source_ids": list("ABCD"),
                        "candidate_cluster": True,
                    }
                ],
                "discovery_jobs": [
                    {
                        "job_id": "family-job",
                        "family": "family-one",
                        "left_source_ids": ["A"],
                        "right_source_ids": ["B", "C", "D"],
                        "candidate_quota": 6,
                    }
                ],
            },
        )

    assert len(calls.seen) == 1


@pytest.mark.parametrize("family_reference,left_ids,right_ids", [
    ("Named family", ["C"], ["D"]),
    ("Other description", ["C"], ["D"]),
    (None, [], []),
    ("Named family", list("ABC"), list("BCD")),
    ("Named family", list("ABCD"), []),
])
def test_shared_discovery_keeps_members_omitted_from_planner_jobs(
    tmp_path: Path, family_reference: str | None, left_ids: list[str], right_ids: list[str],
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    catalogue = _catalogue(tmp_path, profiles)
    plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {
                "family_id": "family-one",
                "label": "Named family",
                "source_ids": list("ABCD"),
                "candidate_cluster": True,
            },
            {
                "family_id": "routing-only",
                "source_ids": list("EF"),
                "candidate_cluster": False,
            },
        ],
        "discovery_jobs": [
            {
                "job_id": "incomplete-job",
                "family": family_reference,
                "left_source_ids": left_ids,
                "right_source_ids": right_ids,
                "candidate_quota": 6,
            }
        ] if family_reference is not None else [],
    }
    considered: set[tuple[str, str]] = set()

    def handler(stage, _profiles, context):
        if not stage.endswith("candidate_selection"):
            return {"decisions": [_decision(job) for job in context["pair_jobs"]]}
        excluded = {tuple(pair) for pair in context["excluded_candidate_pairs"]}
        candidates = []
        outcomes = []
        packet_pairs: set[tuple[str, str]] = set()
        for job in context["bridge_jobs"]:
            pairs = {
                tuple(sorted((left, right)))
                for left in job["left_source_ids"]
                for right in job["right_source_ids"]
                if left != right
            } - excluded
            considered.update(pairs)
            assert all(set(pair) <= set("ABCD") or pair == ("E", "F") for pair in pairs)
            candidates.extend(
                {**_candidate(*pair), "bridge_job_id": job["bridge_job_id"]}
                for pair in sorted(pairs - packet_pairs)
                if pair != ("E", "F")
            )
            packet_pairs.update(pairs)
            outcomes.append({"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"})
        return {"candidates": candidates, "job_outcomes": outcomes}

    result = _run(
        tmp_path, profiles, _Calls(handler), catalogue=catalogue,
        shared_family_plan=plan,
    )
    assert considered == {
        ("A", "B"), ("A", "C"), ("A", "D"), ("B", "C"),
        ("B", "D"), ("C", "D"), ("E", "F"),
    }
    assert result["pair_job_count"] == len(result["accepted"]) == 6
    assert result["relationship_stage_complete"] is True
    assert plan["literature_families"][1]["candidate_cluster"] is False
    _commit_relationship_selection_state(
        tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"],
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }
    replay_calls = _Calls(lambda *_args: pytest.fail("unexpected replay call"))
    replay = _run(
        tmp_path, profiles, replay_calls, catalogue=catalogue,
        shared_family_plan=plan,
    )
    _commit_relationship_selection_state(
        tmp_path, replay, catalogue_revision=replay["reconciled_catalogue_revision"],
    )
    assert replay_calls.seen == []
    assert replay["semantic_noop"] is True
    assert replay["provider_batch_count"] == 0
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }


def test_complementary_breadth_runs_after_cross_pairs_are_resolved(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "relations": [
                {
                    "source_id": left,
                    "target_source_id": right,
                    "relation_type": "cites",
                }
                for left in "AB"
                for right in "CD"
            ]
        },
    )
    discovery_passes: list[str] = []

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            discovery_passes.append(str(context["discovery_pass"]))
            job_id = str(context["bridge_jobs"][0]["bridge_job_id"])
            return {
                "candidates": [
                    {**_candidate("A", "B"), "bridge_job_id": job_id},
                    {**_candidate("C", "C", rank=2), "bridge_job_id": job_id},
                ],
                "job_outcomes": [
                    {"bridge_job_id": job_id, "status": "no_more_candidates"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family", "source_ids": list("ABCD")}
            ],
            "discovery_jobs": [
                {
                    "job_id": "family-job",
                    "family": "family",
                    "left_source_ids": ["A", "B"],
                    "right_source_ids": ["C", "D"],
                    "candidate_quota": 6,
                }
            ],
        },
    )

    assert discovery_passes == ["breadth_completion"]
    accounting = result["relationship_discovery_jobs"][0]
    assert accounting["planner_target_candidates"] == 2
    assert accounting["valid_unique_candidates"] == 1
    assert accounting["planner_target_met"] is False
    assert result["pair_job_count"] == 5
    assert {
        tuple(row["pair"]): row["disposition"]
        for row in result["candidate_dispositions"]
    }[("C", "C")] == "parked_contract_failure"


def test_split_complementary_breadth_routes_each_pair_once(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if (
            context.get("discovery_pass") == "breadth_completion"
            and len(context.get("catalogue", []) or []) > 2
        ):
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)
    breadth_pairs: list[tuple[str, str]] = []

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            job = context["bridge_jobs"][0]
            job_id = str(job["bridge_job_id"])
            if context["discovery_pass"] == "complement":
                pairs = [("A", "C"), ("B", "D")]
            else:
                excluded = {
                    tuple(pair) for pair in context["excluded_candidate_pairs"]
                }
                pairs = sorted(
                    {
                        tuple(sorted((left, right)))
                        for left in job["left_source_ids"]
                        for right in job["right_source_ids"]
                        if left != right and tuple(sorted((left, right))) not in excluded
                    }
                )
                breadth_pairs.extend(pairs)
            return {
                "candidates": [
                    {**_candidate(left, right), "bridge_job_id": job_id}
                    for left, right in pairs
                ],
                "job_outcomes": [
                    {"bridge_job_id": job_id, "status": "completed"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family", "source_ids": list("ABCD")}
            ],
            "discovery_jobs": [
                {
                    "job_id": "family-job",
                    "family": "family",
                    "left_source_ids": ["A", "B"],
                    "right_source_ids": ["C", "D"],
                    "candidate_quota": 6,
                }
            ],
        },
    )

    assert len(breadth_pairs) == len(set(breadth_pairs)) == 4
    assert set(breadth_pairs) == {
        ("A", "B"),
        ("A", "D"),
        ("B", "C"),
        ("C", "D"),
    }
    assert result["pair_job_count"] == 6
    assert result["relationship_discovery_jobs"][0]["planner_target_met"] is True


def test_fully_resolved_requested_job_skips_candidate_discovery(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "relations": [
                {
                    "source_id": left,
                    "target_source_id": right,
                    "relation_type": "cites",
                }
                for left in "AB"
                for right in "CD"
            ]
        },
    )
    calls = _Calls(
        lambda stage, _profiles, context: (
            {"decisions": [_decision(job) for job in context["pair_jobs"]]}
            if stage == "relationship_adjudication"
            else (_ for _ in ()).throw(AssertionError(stage))
        )
    )

    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "requested", "source_ids": list("ABCD")}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested-job",
                    "family": "requested",
                    "left_source_ids": ["A", "B"],
                    "right_source_ids": ["C", "D"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 4,
                }
            ],
        },
    )

    assert all(not stage.endswith("candidate_selection") for stage, _, _ in calls.seen)
    assert result["relationship_discovery_jobs"][0]["status"] == "completed"


def test_incremental_discovery_excludes_unchanged_pairs(tmp_path: Path) -> None:
    baseline_profiles = [_profile("A", collection="C1"), _profile("B", collection="C2")]
    baseline = _run(
        tmp_path,
        baseline_profiles,
        _Calls(
            lambda stage, _profiles, _context: (
                {"candidates": []}
                if stage.endswith("candidate_selection")
                else {"decisions": []}
            )
        ),
    )
    _commit_relationship_selection_state(
        tmp_path,
        baseline,
        catalogue_revision=baseline["reconciled_catalogue_revision"],
    )
    profiles = [*baseline_profiles, _profile("C", collection="C1")]

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            return {
                "candidates": [
                    {**_candidate("A", "B"), "bridge_job_id": "requested"},
                    {**_candidate("B", "C", rank=2), "bridge_job_id": "requested"},
                ],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        assert {
            tuple(sorted(job["pair"].values())) for job in context["pair_jobs"]
        } == {("B", "C")}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "incremental",
            "incremental_source_ids": ["C"],
            "literature_families": [
                {"family_id": "requested", "source_ids": ["A", "B", "C"]}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A", "C"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 2,
                }
            ],
        },
    )

    assert result["pair_job_count"] == 1
    dispositions = {
        tuple(row["pair"]): row["disposition"]
        for row in result["candidate_dispositions"]
    }
    assert dispositions[("A", "B")] == "unchanged_pair_out_of_scope"
    assert dispositions[("B", "C")] == "selected_for_adjudication"


def test_incremental_discovery_resume_retains_affected_set(tmp_path: Path) -> None:
    baseline_profiles = [_profile("A", collection="C1"), _profile("B", collection="C2")]
    baseline = _run(
        tmp_path,
        baseline_profiles,
        _Calls(
            lambda stage, _profiles, _context: (
                {"candidates": []}
                if stage.endswith("candidate_selection")
                else {"decisions": []}
            )
        ),
    )
    _commit_relationship_selection_state(
        tmp_path,
        baseline,
        catalogue_revision=baseline["reconciled_catalogue_revision"],
    )
    profiles = [*baseline_profiles, _profile("C", collection="C1")]
    shared_family_plan = {
        "lean_index_hash": "incremental",
        "incremental_source_ids": ["C"],
        "literature_families": [
            {"family_id": "requested", "source_ids": ["A", "B", "C"]}
        ],
        "discovery_jobs": [
            {
                "job_id": "requested",
                "family": "explicit_requested_collection_comparison",
                "left_source_ids": ["A", "C"],
                "right_source_ids": ["B"],
                "requested_collection_pair": ["C1", "C2"],
                "candidate_quota": 2,
            }
        ],
    }

    first_incremental = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, context: (
                {
                    "candidates": [
                        {**_candidate("B", "C"), "bridge_job_id": "requested"}
                    ],
                    "job_outcomes": [
                        {
                            "bridge_job_id": "requested",
                            "status": "no_more_candidates",
                        }
                    ],
                }
                if stage.endswith("candidate_selection")
                else {"decisions": [_decision(context["pair_jobs"][0])]}
            )
        ),
        shared_family_plan=shared_family_plan,
    )
    first_incremental.update(
        relationship_discovery_status="partial",
        relationship_discovery_incomplete_jobs=["requested"],
        relationship_stage_complete=False,
    )
    _commit_relationship_selection_state(
        tmp_path,
        first_incremental,
        catalogue_revision=first_incremental["reconciled_catalogue_revision"],
    )

    def resume_handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            return {
                "candidates": [
                    {**_candidate("A", "B"), "bridge_job_id": "requested"},
                    {**_candidate("B", "C", rank=2), "bridge_job_id": "requested"},
                ],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    resumed = _run(
        tmp_path,
        profiles,
        _Calls(resume_handler),
        shared_family_plan=shared_family_plan,
    )

    dispositions = {
        tuple(row["pair"]): row["disposition"]
        for row in resumed["candidate_dispositions"]
    }
    assert resumed["pair_job_count"] == 1
    assert dispositions[("A", "B")] == "unchanged_pair_out_of_scope"
    assert dispositions[("B", "C")] == "selected_for_adjudication"


def test_complementary_quota_allocation_covers_every_family_stably() -> None:
    jobs = [
        {
            "bridge_job_id": f"job-{index:02d}",
            "target_candidate_count": 10 + index,
        }
        for index in range(15)
    ]

    allocated = _allocate_complementary_candidate_quotas(jobs, capacity=70)

    assert sum(allocated.values()) == 70
    assert set(allocated) == {row["bridge_job_id"] for row in jobs}
    assert min(allocated.values()) >= 3
    assert allocated == _allocate_complementary_candidate_quotas(
        list(reversed(jobs)), capacity=70
    )


def test_complementary_overflow_repartitions_all_jobs_into_three_packets() -> None:
    jobs = [
        {"bridge_job_id": f"job-{index:02d}", "target_candidate_count": 3}
        for index in range(15)
    ]

    packets = _balance_complementary_jobs(
        jobs,
        measured_sizes={row["bridge_job_id"]: 100 for row in jobs},
        packet_count=3,
    )

    assert len(packets) == 3
    scheduled = [row["bridge_job_id"] for packet in packets for row in packet]
    assert sorted(scheduled) == sorted(row["bridge_job_id"] for row in jobs)
    assert len(scheduled) == len(set(scheduled))


def test_shared_plan_balances_many_complementary_jobs_across_two_packets(
    tmp_path: Path,
) -> None:
    source_ids = [f"S{index:02d}" for index in range(30)]
    profiles = [_profile(source_id) for source_id in source_ids]
    requested_job = {
        "job_id": "requested",
        "family": "explicit_requested_collection_comparison",
        "left_source_ids": source_ids[:15],
        "right_source_ids": source_ids[15:],
        "requested_collection_pair": ["C1", "C2"],
        "candidate_quota": 40,
    }
    complementary_jobs = [
        {
            "job_id": f"family-{index:02d}",
            "family": f"family-{index:02d}",
            "left_source_ids": [source_ids[index]],
            "right_source_ids": [source_ids[index + 15]],
            "candidate_quota": 10,
        }
        for index in range(15)
    ]
    calls = _Calls(
        lambda stage, _profiles, _context: (
            {"candidates": []}
            if stage.endswith("candidate_selection")
            else {"decisions": []}
        )
    )

    _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {
                    "family_id": f"family-{index:02d}",
                    "source_ids": [source_ids[index], source_ids[index + 15]],
                }
                for index in range(15)
            ],
            "discovery_jobs": [requested_job, *complementary_jobs],
        },
    )

    discovery = [
        context
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    ]
    assert [row["discovery_pass"] for row in discovery].count("broad") == 1
    assert next(
        row for row in discovery if row["discovery_pass"] == "broad"
    )["max_inferred_pairs"] == 50
    complement = [
        row for row in discovery if row["discovery_pass"] == "complement"
    ]
    assert len(complement) == 2
    scheduled = [
        job["bridge_job_id"]
        for packet in complement
        for job in packet["bridge_jobs"]
    ]
    assert sorted(scheduled) == sorted(
        row["job_id"] for row in complementary_jobs
    )
    assert len(scheduled) == len(set(scheduled))
    assert sum(
        int(packet["max_inferred_pairs"]) for packet in complement
    ) == 15
    assert all(packet["prior_candidate_pairs"] == [] for packet in complement)


def test_terminal_complement_packet_does_not_discard_successful_graph_work(
    tmp_path: Path,
) -> None:
    source_ids = [f"S{index:02d}" for index in range(16)]
    profiles = [_profile(source_id) for source_id in source_ids]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": source_ids[:8],
            "right_source_ids": source_ids[8:],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        *[
            {
                "job_id": f"family-{index:02d}",
                "family": f"family-{index:02d}",
                "left_source_ids": [source_ids[index]],
                "right_source_ids": [source_ids[index + 8]],
                "candidate_quota": 6,
            }
            for index in range(8)
        ],
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [
                    {
                        **_candidate("S00", "S08"),
                        "bridge_job_id": "requested",
                    }
                ]
            }
        if stage == "relationship_candidate_selection":
            packet_job_ids = {
                row["bridge_job_id"] for row in context["bridge_jobs"]
            }
            if "family-00" in packet_job_ids:
                raise ValueError("malformed complementary packet")
            job = context["bridge_jobs"][0]
            return {
                "candidates": [
                    {
                        **_candidate(
                            job["left_source_ids"][0],
                            job["right_source_ids"][0],
                        ),
                        "bridge_job_id": job["bridge_job_id"],
                    }
                ]
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {
                    "family_id": f"family-{index:02d}",
                    "source_ids": [source_ids[index], source_ids[index + 8]],
                }
                for index in range(8)
            ],
            "discovery_jobs": jobs,
        },
    )

    assert result["relationship_stage_complete"] is False
    assert result["relationship_discovery_status"] == "partial"
    assert "family-00" in result["relationship_discovery_incomplete_jobs"]
    assert result["accounted_pair_job_count"] == result["pair_job_count"]
    assert result["accepted"]

    _commit_relationship_selection_state(
        tmp_path,
        result,
        catalogue_revision=result["reconciled_catalogue_revision"],
    )

    def resume_handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            return {
                "candidates": [
                    {
                        **_candidate(
                            job["left_source_ids"][0],
                            job["right_source_ids"][0],
                        ),
                        "bridge_job_id": job["bridge_job_id"],
                    }
                    for job in context["bridge_jobs"]
                ],
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "completed",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    resume_calls = _Calls(resume_handler)
    resumed = _run(
        tmp_path,
        profiles,
        resume_calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {
                    "family_id": f"family-{index:02d}",
                    "source_ids": [source_ids[index], source_ids[index + 8]],
                }
                for index in range(8)
            ],
            "discovery_jobs": jobs,
        },
    )
    assert resume_calls.seen
    assert not resumed.get("semantic_noop", False)
    assert resumed["relationship_discovery_status"] == "complete"


def test_shared_plan_runs_broad_before_complement_and_passes_prior_pairs(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-cd",
            "family": "family-cd",
            "left_source_ids": ["C"],
            "right_source_ids": ["D"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            assert context["discovery_pass"] == "broad"
            return {
                "candidates": [
                    {**_candidate("A", "B"), "bridge_job_id": "requested"}
                ]
            }
        if stage == "relationship_candidate_selection":
            assert context["discovery_pass"] == "complement"
            assert context["prior_candidate_pairs"] == [("A", "B")]
            return {
                "candidates": [
                    {**_candidate("C", "D"), "bridge_job_id": "family-cd"}
                ]
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]}
            ],
            "discovery_jobs": jobs,
        },
    )

    assert result["pair_job_count"] == 2
    assert [
        context["discovery_pass"]
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    ] == ["broad", "complement"]


def test_missing_candidate_job_id_is_recovered_from_unique_endpoint_sides(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-cd",
            "family": "family-cd",
            "left_source_ids": ["C"],
            "right_source_ids": ["D"],
            "candidate_quota": 12,
        },
        {
            "job_id": "family-ef",
            "family": "family-ef",
            "left_source_ids": ["E"],
            "right_source_ids": ["F"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [_candidate("C", "D"), _candidate("E", "F")],
                "job_outcomes": [
                    {"bridge_job_id": "family-cd", "status": "completed"},
                    {"bridge_job_id": "family-ef", "status": "completed"},
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]},
                {"family_id": "family-ef", "source_ids": ["E", "F"]},
            ],
            "discovery_jobs": jobs,
        },
    )

    assert result["pair_job_count"] == 2
    assert {
        tuple(row["pair"])
        for row in result["candidate_dispositions"]
        if row["disposition"] == "selected_for_adjudication"
    } == {("C", "D"), ("E", "F")}
    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["family-cd"]["valid_unique_candidates"] == 1
    assert accounting["family-ef"]["valid_unique_candidates"] == 1


def test_undercovered_families_share_one_followup_and_are_accounted(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEFGHIJ"]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        *[
            {
                "job_id": f"family-{left.lower()}{right.lower()}",
                "family": f"family-{left.lower()}{right.lower()}",
                "left_source_ids": [left, {"C": "G", "E": "I"}[left]],
                "right_source_ids": [right, {"D": "H", "F": "J"}[right]],
                "candidate_quota": 12,
            }
            for left, right in (("C", "D"), ("E", "F"))
        ],
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            if context["discovery_pass"] == "complement":
                return {
                    "candidates": [],
                    "job_outcomes": [
                        {
                            "bridge_job_id": job["bridge_job_id"],
                            "status": "completed",
                        }
                        for job in context["bridge_jobs"]
                    ],
                }
            assert {row["bridge_job_id"] for row in context["bridge_jobs"]} == {
                "family-cd",
                "family-ef",
            }
            return {
                "candidates": [
                    {**_candidate("C", "D"), "bridge_job_id": "family-cd"},
                    {**_candidate("E", "F"), "bridge_job_id": "family-ef"},
                ],
                "job_outcomes": [
                    {"bridge_job_id": job_id, "status": "completed"}
                    for job_id in ("family-cd", "family-ef")
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    shared_plan = {
        "lean_index_hash": "lean",
        "requested_collection_keys": ["C1", "C2"],
        "literature_families": [
            {"family_id": "family-cd", "source_ids": ["C", "D", "G", "H"]},
            {"family_id": "family-ef", "source_ids": ["E", "F", "I", "J"]},
        ],
        "discovery_jobs": jobs,
    }
    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan=shared_plan,
    )

    assert [
        context["discovery_pass"]
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    ] == [
        "broad",
        "complement",
        "breadth_completion",
    ]
    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["requested"]["planner_target_candidates"] == 1
    assert accounting["requested"]["breadth_completion_status"] == "no_more"
    assert accounting["family-cd"]["status"] == "completed"
    assert accounting["family-ef"]["status"] == "completed"
    assert accounting["family-cd"]["coverage_warning"] == (
        "coverage_shortfall_after_single_breadth_wave"
    )
    assert accounting["family-ef"]["coverage_warning"] == (
        "coverage_shortfall_after_single_breadth_wave"
    )
    assert accounting["family-cd"]["breadth_completion_status"] == "completed"
    assert accounting["family-ef"]["breadth_completion_status"] == "completed"
    assert accounting["family-cd"]["breadth_warning"] == (
        "planner_target_shortfall_after_single_breadth_wave"
    )
    assert result["relationship_discovery_status"] == "complete"
    _commit_relationship_selection_state(
        tmp_path,
        result,
        catalogue_revision=result["reconciled_catalogue_revision"],
    )
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )
    replay = _run(
        tmp_path,
        profiles,
        replay_calls,
        shared_family_plan=shared_plan,
    )
    assert replay_calls.seen == []
    assert replay["semantic_noop"] is True


def test_breadth_completion_runs_once_for_broad_job_with_exclusions(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDE"]
    job = {
        "job_id": "requested",
        "family": "explicit_requested_collection_comparison",
        "left_source_ids": ["A", "C"],
        "right_source_ids": ["B", "D", "E"],
        "requested_collection_pair": ["C1", "C2"],
        "candidate_quota": 4,
    }

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            assert context["discovery_mode"] == "bridge_only"
            if context["discovery_pass"] == "broad":
                return {
                    "candidates": [
                        {**_candidate("A", "B"), "bridge_job_id": "requested"}
                    ],
                    "job_outcomes": [
                        {"bridge_job_id": "requested", "status": "completed"}
                    ],
                }
            assert context["discovery_pass"] == "breadth_completion"
            assert context["breadth_completion_wave"] == 1
            assert context["excluded_candidate_pairs"] == [("A", "B")]
            assert context["max_inferred_pairs"] <= 64
            return {
                "candidates": [
                    {
                        **_candidate(left, right, rank=rank),
                        "bridge_job_id": "requested",
                    }
                    for rank, (left, right) in enumerate(
                        (("A", "D"), ("A", "E"), ("B", "C"), ("C", "D")),
                        start=1,
                    )
                ],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "completed"}
                ],
            }
        return {"decisions": [_decision(row) for row in context["pair_jobs"]]}

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "requested", "source_ids": list("ABCDE")}
            ],
            "discovery_jobs": [job],
        },
    )

    discovery_passes = [
        context["discovery_pass"]
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    ]
    assert discovery_passes == ["broad", "breadth_completion"]
    accounting = result["relationship_discovery_jobs"][0]
    assert accounting["coverage_floor"] == 0
    assert accounting["planner_target_candidates"] == 4
    assert accounting["initial_unique_candidates"] == 1
    assert accounting["breadth_requested_count"] == 3
    assert accounting["breadth_added_unique_candidates"] == 4
    assert accounting["planner_target_met"] is True
    assert accounting["breadth_completion_status"] == "completed"
    assert "breadth_warning" not in accounting


def test_breadth_completion_packs_output_and_runs_packets_concurrently(
    tmp_path: Path,
) -> None:
    first_left = [f"A{index}" for index in range(7)]
    first_right = [f"B{index}" for index in range(6)]
    second_left = [f"C{index}" for index in range(7)]
    second_right = [f"D{index}" for index in range(6)]
    profiles = [
        _profile(source_id)
        for source_id in [
            *first_left,
            *first_right,
            *second_left,
            *second_right,
        ]
    ]
    jobs = [
        {
            "job_id": job_id,
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": left,
            "right_source_ids": right,
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        }
        for job_id, left, right in (
            ("requested-one", first_left, first_right),
            ("requested-two", second_left, second_right),
        )
    ]
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def handler(stage, _profiles, context):
        nonlocal active, peak
        if stage == "relationship_bridge_candidate_selection":
            if context["discovery_pass"] == "broad":
                return {
                    "candidates": [],
                    "job_outcomes": [
                        {
                            "bridge_job_id": job["bridge_job_id"],
                            "status": "completed",
                        }
                        for job in context["bridge_jobs"]
                    ],
                }
            assert context["discovery_pass"] == "breadth_completion"
            assert sum(
                int(job["target_candidate_count"])
                for job in context["bridge_jobs"]
            ) <= 64
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return {
                "candidates": [],
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "completed",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        return {"decisions": []}

    calls = _Calls(handler)
    _run(
        tmp_path,
        profiles,
        calls,
        request=LiteratureMapRequest(
            workspace=tmp_path,
            provider="test-provider",
            model="test-model",
            provider_concurrency=2,
        ),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {
                    "family_id": "requested",
                    "source_ids": [profile.source_id for profile in profiles],
                }
            ],
            "discovery_jobs": jobs,
        },
    )

    breadth_calls = [
        context
        for stage, _key, context in calls.seen
        if stage == "relationship_bridge_candidate_selection"
        and context["discovery_pass"] == "breadth_completion"
    ]
    assert len(breadth_calls) == 2
    assert peak == 2


def test_breadth_completion_apportions_residual_across_split_shards(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if (
            context.get("discovery_pass") == "breadth_completion"
            and len(context.get("catalogue", []) or []) > 2
        ):
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)
    breadth_targets: list[int] = []

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            if context["discovery_pass"] == "breadth_completion":
                breadth_targets.extend(
                    int(job["target_candidate_count"])
                    for job in context["bridge_jobs"]
                )
            return {
                "candidates": [],
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "completed",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        return {"decisions": []}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "split-family", "source_ids": ["C", "D", "E", "F"]}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 1,
                },
                {
                    "job_id": "split-family",
                    "family": "split-family",
                    "left_source_ids": ["C", "D"],
                    "right_source_ids": ["E", "F"],
                    "candidate_quota": 4,
                },
            ],
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert len(breadth_targets) == 4
    assert sum(breadth_targets) == 4
    assert accounting["split-family"]["breadth_requested_count"] == 4


def test_unschedulable_breadth_shard_survives_successful_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if context.get("discovery_pass") != "breadth_completion":
            return original(profiles, context)
        job_ids = {
            job["bridge_job_id"] for job in context.get("bridge_jobs", []) or []
        }
        if "family-bad" in job_ids or len(job_ids) > 1:
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            if context["discovery_pass"] == "complement":
                return {
                    "candidates": [],
                    "job_outcomes": [
                        {
                            "bridge_job_id": job["bridge_job_id"],
                            "status": "completed",
                        }
                        for job in context["bridge_jobs"]
                    ],
                }
            assert {
                job["bridge_job_id"] for job in context["bridge_jobs"]
            } == {"family-good"}
            return {
                "candidates": [
                    {**_candidate("E", "F"), "bridge_job_id": "family-good"}
                ],
                "job_outcomes": [
                    {"bridge_job_id": "family-good", "status": "completed"}
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "family-bad", "source_ids": ["C", "D"]},
                {"family_id": "family-good", "source_ids": ["E", "F"]},
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 1,
                },
                {
                    "job_id": "family-bad",
                    "family": "family-bad",
                    "left_source_ids": ["C"],
                    "right_source_ids": ["D"],
                    "candidate_quota": 1,
                },
                {
                    "job_id": "family-good",
                    "family": "family-good",
                    "left_source_ids": ["E"],
                    "right_source_ids": ["F"],
                    "candidate_quota": 1,
                },
            ],
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["family-bad"]["packet_status"] == "failed"
    assert accounting["family-bad"]["packet_failure_class"] == "context"
    assert accounting["family-bad"]["unschedulable_completion_shards"] == 1
    assert accounting["family-good"]["status"] == "completed"
    assert result["relationship_discovery_incomplete_jobs"] == ["family-bad"]
    assert result["pair_job_count"] == 1


def test_limited_family_side_is_accounted_without_a_call(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    profiles[1].context["note_status"] = "metadata_only_atomic_note"
    jobs = [
        {
            "job_id": "limited",
            "family": "limited",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-cd",
            "family": "family-cd",
            "left_source_ids": ["C"],
            "right_source_ids": ["D"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            assert [row["bridge_job_id"] for row in context["bridge_jobs"]] == [
                "family-cd"
            ]
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "family-cd", "status": "no_more_candidates"}
                ],
            }
        return {"decisions": []}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]}
            ],
            "discovery_jobs": jobs,
        },
    )
    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["limited"]["status"] == "insufficient_analytical_endpoints"


def test_builtin_missing_job_outcomes_settle_with_warning(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-cd",
            "family": "family-cd",
            "left_source_ids": ["C"],
            "right_source_ids": ["D"],
            "candidate_quota": 12,
        },
        {
            "job_id": "family-ef",
            "family": "family-ef",
            "left_source_ids": ["E"],
            "right_source_ids": ["F"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "completed"}
                ],
            }
        return {
            "candidates": [],
            # Deliberately omit family-ef from a partial built-in envelope.
            "job_outcomes": [
                {"bridge_job_id": "family-cd", "status": "completed"}
            ],
        }

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_BuiltInDeepSeekReasoner(),
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]},
                {"family_id": "family-ef", "source_ids": ["E", "F"]},
            ],
            "discovery_jobs": jobs,
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert result["relationship_discovery_incomplete_jobs"] == []
    assert result["relationship_discovery_status"] == "complete"
    assert accounting["family-cd"]["coverage_warning"] == (
        "coverage_shortfall_after_single_breadth_wave"
    )
    assert accounting["family-ef"]["coverage_warning"] == (
        "coverage_shortfall_after_single_breadth_wave"
    )
    assert accounting["family-ef"]["accounting_warning"] == (
        "missing_or_invalid_job_outcomes"
    )
    assert any(
        context.get("discovery_pass") == "breadth_completion"
        for _stage, _key, context in calls.seen
    )


def test_completed_packet_does_not_start_autonomous_continuation(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-cd",
            "family": "family-cd",
            "left_source_ids": ["C"],
            "right_source_ids": ["D"],
            "candidate_quota": 12,
        },
        {
            "job_id": "family-ef",
            "family": "family-ef",
            "left_source_ids": ["E"],
            "right_source_ids": ["F"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    {**_candidate("C", "D"), "bridge_job_id": "family-cd"},
                ],
                "job_outcomes": [
                    {"bridge_job_id": "family-cd", "status": "no_more_candidates"},
                    {"bridge_job_id": "family-ef", "status": "completed"},
                ],
            }
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_BuiltInDeepSeekReasoner(),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]},
                {"family_id": "family-ef", "source_ids": ["E", "F"]},
            ],
            "discovery_jobs": jobs,
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["family-cd"]["status"] == "completed"
    assert accounting["family-cd"]["packet_status"] == "completed"
    assert accounting["family-ef"]["coverage_warning"] == (
        "coverage_shortfall_after_single_breadth_wave"
    )
    assert result["relationship_discovery_incomplete_jobs"] == []
    assert all(
        "discovery_page" not in context
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    )


def test_split_discovery_job_keeps_failed_shard_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if (
            context.get("discovery_pass") == "complement"
            and len(context.get("catalogue", []) or []) > 2
        ):
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            source_ids = {
                row["source_id"] for row in context.get("catalogue", []) or []
            }
            if "D" in source_ids:
                raise ValueError("one split shard failed")
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "split-family", "status": "no_more_candidates"}
                ],
            }
        return {"decisions": []}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=_BuiltInDeepSeekReasoner(),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "split-family", "source_ids": ["C", "D", "E", "F"]}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 40,
                },
                {
                    "job_id": "split-family",
                    "family": "split-family",
                    "left_source_ids": ["C", "D"],
                    "right_source_ids": ["E", "F"],
                    "candidate_quota": 12,
                },
            ],
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["split-family"]["packet_status"] == "failed"
    assert accounting["split-family"]["explicit_no_more_candidates"] is False
    assert result["relationship_discovery_incomplete_jobs"] == ["split-family"]


def test_split_breadth_completion_cannot_hide_failed_shard(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEF"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if (
            context.get("discovery_pass") == "breadth_completion"
            and len(context.get("catalogue", []) or []) > 2
        ):
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)

    def handler(stage, _profiles, context):
        if stage == "relationship_bridge_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "no_more_candidates"}
                ],
            }
        if stage == "relationship_candidate_selection":
            if context.get("discovery_pass") == "complement":
                return {
                    "candidates": [],
                    "job_outcomes": [
                        {"bridge_job_id": "split-family", "status": "completed"}
                    ],
                }
            source_ids = {
                row["source_id"] for row in context.get("catalogue", []) or []
            }
            if "D" in source_ids:
                raise ValueError("one breadth shard failed")
            return {
                "candidates": [],
                "job_outcomes": [
                    {"bridge_job_id": "split-family", "status": "no_more_candidates"}
                ],
            }
        return {"decisions": []}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=_BuiltInDeepSeekReasoner(),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "split-family", "source_ids": ["C", "D", "E", "F"]}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 40,
                },
                {
                    "job_id": "split-family",
                    "family": "split-family",
                    "left_source_ids": ["C", "D"],
                    "right_source_ids": ["E", "F"],
                    "candidate_quota": 12,
                },
            ],
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["split-family"]["packet_status"] == "failed"
    assert result["relationship_discovery_incomplete_jobs"] == ["split-family"]


def test_duplicate_broad_and_family_candidate_preserves_both_provenances(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    jobs = [
        {
            "job_id": "requested",
            "family": "explicit_requested_collection_comparison",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "requested_collection_pair": ["C1", "C2"],
            "candidate_quota": 40,
        },
        {
            "job_id": "family-ab",
            "family": "family-ab",
            "left_source_ids": ["A"],
            "right_source_ids": ["B"],
            "candidate_quota": 12,
        },
    ]

    def handler(stage, _profiles, context):
        if stage.endswith("candidate_selection"):
            job = context["bridge_jobs"][0]
            return {
                "candidates": [
                    {
                        **_candidate("A", "B"),
                        "bridge_job_id": job["bridge_job_id"],
                    }
                ],
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "completed",
                    }
                ],
            }
        job = context["pair_jobs"][0]
        provenance_ids = {
            row["discovery_job_id"]
            for row in job["candidate_basis"][0]["discovery_provenance"]
        }
        assert provenance_ids == {"requested", "family-ab"}
        return {"decisions": [_decision(job)]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        shared_family_plan={
            "lean_index_hash": "lean",
            "requested_collection_keys": ["C1", "C2"],
            "literature_families": [
                {"family_id": "family-ab", "source_ids": ["A", "B"]}
            ],
            "discovery_jobs": jobs,
        },
    )

    assert result["pair_job_count"] == 1
    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["requested"]["dispositions"]["selected_for_adjudication"]
    assert accounting["family-ab"]["dispositions"]["selected_for_adjudication"]


def test_shared_packet_overflow_preserves_other_family_accounting(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    original = pipeline_module._reasoner_packet_chars

    def measured(profiles, context):
        if context.get("discovery_pass") == "complement":
            return 10**9
        return original(profiles, context)

    monkeypatch.setattr(pipeline_module, "_reasoner_packet_chars", measured)
    calls = _Calls(lambda _stage, _profiles, _context: {"candidates": []})
    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan={
            "lean_index_hash": "lean",
            "literature_families": [
                {"family_id": "family-cd", "source_ids": ["C", "D"]}
            ],
            "discovery_jobs": [
                {
                    "job_id": "requested",
                    "family": "explicit_requested_collection_comparison",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                    "candidate_quota": 40,
                },
                {
                    "job_id": "family-cd",
                    "family": "family-cd",
                    "left_source_ids": ["C"],
                    "right_source_ids": ["D"],
                    "candidate_quota": 12,
                },
            ],
        },
    )

    accounting = {
        row["bridge_job_id"]: row
        for row in result["relationship_discovery_jobs"]
    }
    assert accounting["requested"]["status"] == "completed"
    assert accounting["family-cd"]["status"] == "eligible"
    assert "family-cd" in result["relationship_discovery_incomplete_jobs"]
    assert all(
        context.get("discovery_pass") != "breadth_completion"
        for _stage, _key, context in calls.seen
    )


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


def test_zero_candidate_capacity_defers_every_candidate() -> None:
    dispositions: list[dict[str, Any]] = []

    selected = _ranked_relationship_candidates(
        {"candidates": [_candidate("A", "B")]},
        available_source_ids={"A", "B"},
        entry_by_source={"A": {}, "B": {}},
        excluded_pairs=set(),
        maximum=0,
        bridge_fraction=0.4,
        dispositions=dispositions,
    )

    assert selected == []
    assert dispositions == [
        {"pair": ["A", "B"], "disposition": "deferred_capacity"}
    ]


def test_candidate_ranking_is_fair_across_jobs_and_merges_provenance() -> None:
    entries = {
        source_id: {"collections": ["one" if source_id.startswith("A") else "two"]}
        for source_id in ("A1", "A2", "A3", "B1", "B2")
    }
    candidates = [
        {**_candidate("A1", "B1", rank=1, cross_literature=True), "discovery_job_id": "job-a"},
        {**_candidate("A2", "B1", rank=2, cross_literature=True), "discovery_job_id": "job-a"},
        {**_candidate("A3", "B2", rank=10, cross_literature=True), "discovery_job_id": "job-b"},
        {**_candidate("A1", "B1", rank=1, cross_literature=True), "discovery_job_id": "job-b"},
    ]
    dispositions: list[dict[str, Any]] = []

    selected = _ranked_relationship_candidates(
        {"candidates": candidates},
        available_source_ids=set(entries),
        entry_by_source=entries,
        excluded_pairs=set(),
        maximum=2,
        bridge_fraction=1.0,
        scope="bridge",
        dispositions=dispositions,
        job_floors={"job-a": 1, "job-b": 1},
    )

    assert {(row["source_id"], row["target_id"]) for row in selected} == {
        ("A1", "B1"),
        ("A3", "B2"),
    }
    assert len(selected[0]["discovery_provenance"]) == 2
    assert any(row["disposition"] == "duplicate_merged" for row in dispositions)


def test_v6_keyed_batch_parks_only_an_omitted_pair(tmp_path: Path) -> None:
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
        assert list(context["source_documents"]) == ["A", "B", "C"]
        assert all(job["output_contract"] == "relationship-decision-v6" for job in jobs)
        return {
            "decisions": {
                jobs[0]["pair_job_id"]: _v6_decision(jobs[0]),
            }
        }

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=_V6Reasoner(),
    )

    assert len(result["accepted"]) == 1
    assert len(result["parked"]) == 1
    assert result["parked"][0]["reason"] == "provider_batch_missing_pair_row"
    assert result["accounted_pair_job_count"] == 2


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
        if stage == "relationship_bridge_shard_selection":
            return {
                "shard_pairs": [
                    {
                        "left_shard_id": "collection-mediation",
                        "right_shard_id": "collection-relapse",
                        "bridge_family": "conflict management",
                        "target_candidate_count": 12,
                    }
                ]
            }
        if stage == "relationship_candidate_selection":
            assert context["discovery_mode"] == "global"
            assert context["max_inferred_pairs"] == 48
            return {"candidates": []}
        assert stage == "relationship_bridge_candidate_selection"
        assert context["discovery_mode"] == "bridge_only"
        assert context["max_inferred_pairs"] == 72
        assert len(context["bridge_jobs"]) == 1
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
    result = _run(
        tmp_path, profiles, calls, reasoner=_BridgeRoutedReasoner()
    )

    assert {
        stage for stage, _key, _context in calls.seen
    } == {
        "relationship_bridge_shard_selection",
        "relationship_candidate_selection",
        "relationship_bridge_candidate_selection",
    }
    bridge_contexts = [
        context
        for stage, _key, context in calls.seen
        if stage == "relationship_bridge_candidate_selection"
    ]
    assert len(bridge_contexts) == 1
    assert bridge_contexts[0]["bridge_jobs"][0]["left_shard_id"] == (
        "collection-mediation"
    )
    assert bridge_contexts[0]["bridge_jobs"][0]["right_shard_id"] == (
        "collection-relapse"
    )
    assert result["pair_job_count"] == 0


def test_multi_collection_bridge_packet_preserves_every_routed_pair(
    tmp_path: Path,
) -> None:
    profiles = [
        _profile(source_id, collection=collection)
        for source_id, collection in zip(
            ("A", "B", "C", "D"), ("one", "two", "three", "four"), strict=True
        )
    ]
    shards = [
        {
            "shard_id": f"shard-{collection}",
            "literature_id": collection,
            "source_ids": [source_id],
            "routing_card": {"title": collection},
        }
        for source_id, collection in zip(
            ("A", "B", "C", "D"), ("one", "two", "three", "four"), strict=True
        )
    ]
    catalogue = _catalogue(tmp_path, profiles, shards=shards)

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_bridge_shard_selection":
            return {
                "shard_pairs": [
                        {
                            "left_shard_id": "collection-one",
                            "right_shard_id": "collection-two",
                        },
                        {
                            "left_shard_id": "collection-three",
                            "right_shard_id": "collection-four",
                    },
                ]
            }
        if "candidate_selection" in stage:
            return {"candidates": []}
        return {"decisions": []}

    calls = _Calls(handler)
    _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_V6BridgeRoutedReasoner(),
        catalogue=catalogue,
    )
    contexts = [
        context
        for stage, _key, context in calls.seen
        if stage == "relationship_bridge_candidate_selection"
    ]

    assert len(contexts) == 1
    assert {
        (
            row["left_shard_id"],
            row["right_shard_id"],
        )
        for context in contexts
        for row in context["bridge_jobs"]
        } == {
            ("collection-one", "collection-two"),
            ("collection-three", "collection-four"),
        }


def test_multi_packet_discovery_reserves_actual_call_count(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    profiles = [
        _profile(source_id, collection=collection)
        for source_id, collection in zip(
            ("A", "B", "C", "D"), ("one", "two", "three", "four"), strict=True
        )
    ]
    shards = [
        {
            "shard_id": f"shard-{collection}",
            "literature_id": collection,
            "source_ids": [source_id],
            "routing_card": {"title": collection},
        }
        for source_id, collection in zip(
            ("A", "B", "C", "D"), ("one", "two", "three", "four"), strict=True
        )
    ]
    catalogue = _catalogue(tmp_path, profiles, shards=shards)
    monkeypatch.setattr(
        pipeline_module,
        "_reasoner_packet_chars",
        lambda _profiles, context: (
            10_000_000
            if context.get("discovery_mode") == "bridge_only"
            and len(context.get("catalogue", [])) > 2
            else 1
        ),
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_bridge_shard_selection":
            return {
                "shard_pairs": [
                    {
                            "left_shard_id": "collection-one",
                            "right_shard_id": "collection-two",
                    },
                    {
                            "left_shard_id": "collection-three",
                            "right_shard_id": "collection-four",
                    },
                ]
            }
        if "candidate_selection" in stage:
            return {"candidates": []}
        return {"decisions": []}

    calls = _Calls(handler)
    calls.max_calls = 8
    calls.cumulative_provider_calls = 0
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_V6BridgeRoutedReasoner(),
        catalogue=catalogue,
    )
    candidate_contexts = [
        context
        for stage, _key, context in calls.seen
        if "candidate_selection" in stage
    ]

    assert len(candidate_contexts) == 3
    assert candidate_contexts[0]["max_inferred_pairs"] <= 64
    assert all(
        context["max_inferred_pairs"] <= 96
        for context in candidate_contexts[1:]
    )
    assert calls.cumulative_provider_calls == 5
    assert not any(
        row.get("reason") == "relationship_discovery_budget_conflict"
        for row in result["parked"]
    )


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
                "virtual_shards": [
                    {
                        "shard_id": f"shard-{source_id.lower()}",
                        "topic_id": f"lit-{source_id.lower()}",
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
        output_contract="relationship-decision-v6",
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


@pytest.mark.parametrize(
    ("reasoner_class", "expected_batches"),
    [
        (_V8Reasoner, [(8, 0)]),
        (_V6Reasoner, [(5, 6), (3, 4)]),
    ],
)
def test_relationship_packet_sizing_matches_provider_visible_payload(
    tmp_path: Path,
    reasoner_class: type[_Reasoner],
    expected_batches: list[tuple[int, int]],
) -> None:
    profiles = [_profile("A"), *[_profile(f"S{index}") for index in range(8)]]
    for profile in profiles:
        profile.evidence_anchors = [
            EvidenceAnchor(
                evidence_anchor_id=f"anchor-{profile.source_id}",
                source_id=profile.source_id,
                claim="x" * 30_000,
                locator="p. 1",
                support_envelope={
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            )
        ]
    reasoner = reasoner_class()
    reasoner.context_window_tokens = 272_000
    reasoner.prompt_reserve_tokens = 2_048
    observed_batches: list[tuple[int, int]] = []

    def handler(
        stage: str,
        provider_profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [
                    _candidate("A", f"S{index}", rank=index + 1)
                    for index in range(8)
                ]
            }
        jobs = context["pair_jobs"]
        observed_batches.append((len(jobs), len(provider_profiles)))
        return {"decisions": [_decision(job) for job in jobs]}

    result = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        reasoner=reasoner,
        request=LiteratureMapRequest(
            workspace=tmp_path,
            provider="test-provider",
            model="test-model",
            provider_concurrency=1,
        ),
    )

    assert observed_batches == expected_batches
    assert result["provider_batch_count"] == len(expected_batches)
    assert result["accounted_pair_job_count"] == 8


def test_shared_plan_reserves_cluster_and_gap_calls_before_adjudication(
    tmp_path: Path,
) -> None:
    source_ids = ["A", *[f"S{index:02d}" for index in range(13)]]
    profiles = [_profile(source_id) for source_id in source_ids]
    shared_plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {
                "family_id": "family",
                "source_ids": source_ids,
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [
            {
                "job_id": "family",
                "family": "family",
                "left_source_ids": ["A"],
                "right_source_ids": source_ids[1:],
                "candidate_quota": 13,
            }
        ],
    }
    candidates = [
        _candidate("A", source_id, rank=index)
        for index, source_id in enumerate(source_ids[1:], start=1)
    ]
    batch_sizes: list[int] = []

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": candidates,
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "no_more_candidates",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        jobs = context["pair_jobs"]
        batch_sizes.append(len(jobs))
        return {"decisions": [_decision(job) for job in jobs]}

    calls = _Calls(handler)
    calls.max_calls = 5
    calls.cumulative_provider_calls = 0
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_V8Reasoner(),
        shared_family_plan=shared_plan,
        request=LiteratureMapRequest(
            workspace=tmp_path,
            provider="test-provider",
            model="test-model",
            provider_concurrency=1,
            literature_policy=LiteratureMappingPolicy(
                cluster_generation_enabled=True
            ),
        ),
    )

    assert batch_sizes == [8]
    assert result["pair_job_count"] == 8
    assert sum(
        row["disposition"] == "deferred_capacity"
        for row in result["candidate_dispositions"]
    ) == 5
    assert calls.cumulative_provider_calls == 3


def test_shared_plan_defers_mandatory_pairs_to_preserve_cluster_budget(
    tmp_path: Path,
) -> None:
    source_ids = ["A", *[f"S{index:02d}" for index in range(17)]]
    profiles = [_profile(source_id) for source_id in source_ids]
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "relations": [
                {
                    "relation_id": f"explicit-{source_id}",
                    "source_id": "A",
                    "target_source_id": source_id,
                    "relation_type": "zotero_related",
                    "active": True,
                }
                for source_id in source_ids[1:]
            ]
        },
    )
    shared_plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {
                "family_id": "family",
                "source_ids": source_ids,
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [
            {
                "job_id": "family",
                "family": "family",
                "left_source_ids": ["A"],
                "right_source_ids": source_ids[1:],
                "candidate_quota": 17,
            }
        ],
    }

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": [],
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "no_more_candidates",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        raise AssertionError(f"unexpected adjudication call: {stage}")

    calls = _Calls(handler)
    calls.max_calls = 4
    calls.cumulative_provider_calls = 0
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_V8Reasoner(),
        shared_family_plan=shared_plan,
        request=LiteratureMapRequest(
            workspace=tmp_path,
            provider="test-provider",
            model="test-model",
            provider_concurrency=1,
            literature_policy=LiteratureMappingPolicy(
                cluster_generation_enabled=True
            ),
        ),
    )
    assert result["relationship_stage_complete"] is False
    assert result["relationship_retry_on_resume"] is True
    assert result["parked"]
    assert all(row["status"] == "pending" and row["reason"] == "provider_call_budget_reserved_for_clusters"
               for row in result["parked"])

    assert [stage for stage, _profiles, _context in calls.seen] == [
        "relationship_candidate_selection"
    ]
    assert calls.cumulative_provider_calls == 1


def test_shared_plan_uses_actual_packet_count_for_cluster_budget(
    tmp_path: Path,
) -> None:
    source_ids = ["A", *[f"S{index:02d}" for index in range(8)]]
    profiles = [_profile(source_id) for source_id in source_ids]
    for profile in profiles:
        profile.evidence_anchors = [
            EvidenceAnchor(
                evidence_anchor_id=(
                    f"large-anchor-{profile.source_id}-{anchor_index}"
                ),
                source_id=profile.source_id,
                claim=f"Claim for {profile.source_id}. " + "evidence " * 180,
                locator="p. 10",
                support_envelope={
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            )
            for anchor_index in range(3)
        ]
    shared_plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {
                "family_id": "family",
                "source_ids": source_ids,
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [
            {
                "job_id": "family",
                "family": "family",
                "left_source_ids": ["A"],
                "right_source_ids": source_ids[1:],
                "candidate_quota": 8,
            }
        ],
    }
    candidates = [
        _candidate("A", source_id, rank=index)
        for index, source_id in enumerate(source_ids[1:], start=1)
    ]

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            return {
                "candidates": candidates,
                "job_outcomes": [
                    {
                        "bridge_job_id": job["bridge_job_id"],
                        "status": "no_more_candidates",
                    }
                    for job in context["bridge_jobs"]
                ],
            }
        raise AssertionError(f"unexpected adjudication call: {stage}")

    calls = _Calls(handler)
    calls.max_calls = 5
    calls.cumulative_provider_calls = 0
    reasoner = _V8Reasoner()
    reasoner.context_window_tokens = 10_000
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=reasoner,
        shared_family_plan=shared_plan,
        request=LiteratureMapRequest(
            workspace=tmp_path,
            provider="test-provider",
            model="test-model",
            provider_concurrency=1,
            literature_policy=LiteratureMappingPolicy(
                cluster_generation_enabled=True
            ),
        ),
    )
    assert result["relationship_stage_complete"] is False
    assert result["relationship_retry_on_resume"] is True
    assert result["parked"]
    assert all(row["status"] == "pending" and row["reason"] == "provider_call_budget_reserved_for_clusters"
               for row in result["parked"])

    assert [stage for stage, _profiles, _context in calls.seen] == [
        "relationship_candidate_selection",
        "relationship_candidate_selection",
    ]
    assert calls.cumulative_provider_calls == 2


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


@pytest.mark.parametrize("budget_stop", [False, True])
def test_transport_failed_batch_resumes_without_losing_completed_decisions(
    tmp_path: Path, budget_stop: bool,
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
            if budget_stop:
                from auto_zettelkasten.literature import LiteratureSynthesisPartialError
                raise LiteratureSynthesisPartialError("literature_synthesis_call_budget_reached")
            raise TimeoutError("provider timed out")
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    first = _run(tmp_path, profiles, _Calls(first_handler))

    if budget_stop:
        assert all(row["status"] == "pending" and row["reason"] == "provider_call_budget_exhausted"
                   for row in first["parked"])
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
    assert result["relationship_stage_complete"] is False
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
    assert replay["semantic_noop"] is True
    assert replay["pair_job_count"] == 0
    assert replay["provider_batch_count"] == 0


def test_adjudication_prompt_change_reuses_discovery_and_readjudicates(
    tmp_path: Path,
    monkeypatch: Any,
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
    state_path = _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    assert state_path is not None
    state = read_yaml(state_path)
    assert state["state_schema_version"] == "4"
    assert len(state["selected_candidates"]) == 1
    assert state["selected_candidate_pool_hash"] == stable_hash(
        state["selected_candidates"]
    )

    monkeypatch.setattr(pipeline_module, "RELATIONSHIP_PROMPT_VERSION", "16")
    calls = _Calls(
        lambda stage, _profiles, context: {
            "decisions": [_decision(context["pair_jobs"][0])]
        }
    )
    second = _run(tmp_path, profiles, calls)

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_adjudication"
    ]
    assert second["pair_job_count"] == 1
    _commit_relationship_selection_state(
        tmp_path,
        second,
        catalogue_revision=second["reconciled_catalogue_revision"],
    )

    before = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )
    replay = _run(tmp_path, profiles, replay_calls)
    _commit_relationship_selection_state(
        tmp_path,
        replay,
        catalogue_revision=replay["reconciled_catalogue_revision"],
    )

    assert replay_calls.seen == []
    assert state_path.read_bytes() == before
    assert state_path.stat().st_mtime_ns == before_mtime


def test_schema3_selected_dispositions_migrate_without_discovery(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    map_request = LiteratureMapRequest(
        workspace=tmp_path,
        provider="test-provider",
        model="test-model",
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=False
        ),
    )
    first = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, context: (
                {"candidates": [_candidate("A", "B")]}
                if stage == "relationship_candidate_selection"
                else {"decisions": [_decision(context["pair_jobs"][0])]}
            )
        ),
        request=map_request,
    )
    state_path = _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    assert state_path is not None
    state = read_yaml(state_path)
    state["state_schema_version"] = "3"
    state["selection_identity"] = stable_hash(
        {
            "provider": "test-provider",
            "model": "test-model",
            "discovery_prompt_version": pipeline_module.RELATIONSHIP_DISCOVERY_PROMPT_VERSION,
            "adjudication_prompt_version": "14",
            "output_contract": "relationship-decision-v4",
            "decision_normalization_version": pipeline_module.RELATIONSHIP_DECISION_NORMALIZATION_VERSION,
            "policy_identity": stable_hash(LiteratureMappingPolicy().to_dict()),
        }
    )
    state["candidate_dispositions"] = [
        {
            "pair": ["A", "B"],
            "disposition": "selected_for_adjudication",
        }
    ]
    for key in (
        "selected_candidates",
        "selected_candidate_pool_hash",
        "discovery_identity",
        "adjudication_identity",
    ):
        state.pop(key, None)
    write_yaml(state_path, state)

    calls = _Calls(
        lambda stage, _profiles, context: {
            "decisions": [_decision(context["pair_jobs"][0])]
        }
    )
    migrated = _run(
        tmp_path,
        profiles,
        calls,
        request=map_request,
    )

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_adjudication"
    ]
    assert migrated["pair_job_count"] == 1
    assert migrated["selected_candidates"][0]["candidate_basis"][0][
        "provenance"
    ] == "legacy_selected_disposition"


def test_prompt_change_reuses_frozen_negative_pair_for_readjudication(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    profiles = [_profile("A"), _profile("B")]

    def no_relationship(context: Mapping[str, Any]) -> Mapping[str, Any]:
        job = context["pair_jobs"][0]
        return {
            "decisions": [
                {
                    "pair_job_id": job["pair_job_id"],
                    "decision": "no_relationship",
                    "reason": "The overlap is only topical.",
                    "confidence": "high",
                }
            ]
        }

    first = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, context: (
                {"candidates": [_candidate("A", "B")]}
                if stage == "relationship_candidate_selection"
                else no_relationship(context)
            )
        ),
        reasoner=_V8Reasoner(),
    )
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    input_path = next(
        (
            tmp_path
            / "11_state"
            / "runs"
            / "relationship-run"
            / "relationship_jobs"
        ).glob("*/input.json")
    )
    frozen_job = RelationshipPairJob.from_dict(
        json.loads(input_path.read_text(encoding="utf-8"))
    )
    monkeypatch.setattr(pipeline_module, "RELATIONSHIP_PROMPT_VERSION", "16")
    calls = _Calls(
        lambda stage, _profiles, context: no_relationship(context)
    )

    second = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=_V8Reasoner(),
        frozen_pair_jobs=[frozen_job],
    )

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_adjudication"
    ]
    assert second["pair_job_count"] == 1


def test_operational_policy_changes_do_not_invalidate_relationship_state(
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
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    changed_request = LiteratureMapRequest(
        workspace=tmp_path,
        provider="test-provider",
        model="test-model",
        provider_concurrency=9,
        literature_policy=LiteratureMappingPolicy(
            max_synthesis_calls=99,
            profile_workers=12,
            literature_deadline_seconds=9_999,
        ),
    )
    calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected provider call: {stage}")
        )
    )

    replay = _run(tmp_path, profiles, calls, request=changed_request)

    assert calls.seen == []
    assert replay["pair_job_count"] == 0


def test_corrupt_selected_pool_hash_falls_back_to_discovery(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    first = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, context: (
                {"candidates": [_candidate("A", "B")]}
                if stage == "relationship_candidate_selection"
                else {"decisions": [_decision(context["pair_jobs"][0])]}
            )
        ),
    )
    state_path = _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    assert state_path is not None
    state = read_yaml(state_path)
    state["selected_candidate_pool_hash"] = "corrupt"
    write_yaml(state_path, state)
    calls = _Calls(
        lambda stage, _profiles, context: (
            {"candidates": [_candidate("A", "B")]}
            if stage == "relationship_candidate_selection"
            else {"decisions": [_decision(context["pair_jobs"][0])]}
        )
    )

    _run(tmp_path, profiles, calls)

    assert any(
        stage == "relationship_candidate_selection"
        for stage, _key, _context in calls.seen
    )


def test_changed_collection_routing_card_invalidates_discovery(
    tmp_path: Path,
) -> None:
    profiles = [
        _profile("A", collection="mediation"),
        _profile("B", collection="relapse"),
    ]
    catalogue = _catalogue(tmp_path, profiles)

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if "candidate_selection" in stage:
            return {"candidates": []}
        return {"decisions": []}

    first = _run(
        tmp_path,
        profiles,
        _Calls(handler),
        catalogue=catalogue,
    )
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    payload = read_yaml(Path(str(catalogue["catalogue_path"])))
    payload["literatures"][0]["scope"] = "Changed upstream collection scope."
    write_yaml(Path(str(catalogue["catalogue_path"])), payload)

    calls = _Calls(handler)
    _run(tmp_path, profiles, calls, catalogue=catalogue)

    assert any("candidate_selection" in stage for stage, _key, _ in calls.seen)


def test_graph_only_collection_card_changes_do_not_invalidate_discovery(
    tmp_path: Path,
) -> None:
    profiles = [
        _profile("A", collection="mediation"),
        _profile("B", collection="relapse"),
    ]
    catalogue = _catalogue(tmp_path, profiles)
    path = Path(str(catalogue["catalogue_path"]))
    payload = read_yaml(path)
    payload["collections"] = [
        {
            "key": "mediation",
            "parent_key": "",
            "direct_source_ids": ["A"],
            "routing_card": {
                "name": "Mediation",
                "scope": "Mediation studies.",
                "active_cluster_ids": ["cluster-old"],
                "cross_collection_relationship_count": 1,
                "revision_hash": "old",
            },
        }
    ]
    write_yaml(path, payload)

    first = _run(
        tmp_path,
        profiles,
        _Calls(
            lambda stage, _profiles, _context: (
                {"candidates": []}
                if "candidate_selection" in stage
                else {"decisions": []}
            )
        ),
        catalogue=catalogue,
    )
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    payload = read_yaml(path)
    payload["collections"][0]["routing_card"].update(
        active_cluster_ids=["cluster-new"],
        cross_collection_relationship_count=9,
        revision_hash="new",
    )
    write_yaml(path, payload)
    calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected provider call: {stage}")
        )
    )

    replay = _run(tmp_path, profiles, calls, catalogue=catalogue)

    assert calls.seen == []
    assert replay["pair_job_count"] == 0


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
            assert all(not profile.evidence_anchors for profile in provider_profiles)
            return {"candidates": [_candidate("S000", "S001")]}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    calls = _Calls(handler)
    reasoner = _RoutedReasoner()
    reasoner.context_window_tokens = 10_000
    result = _run(
        tmp_path,
        profiles,
        calls,
        reasoner=reasoner,
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
    relationship_policy_identity = stable_hash(
        {"relationship_semantic_policy": "source-owned-bases-v26"}
    )
    decision_key = relationship_decision_key(
        "A",
        "B",
        stable_hash(profile_to_dict(profiles[0])),
        stable_hash(profile_to_dict(profiles[1])),
        provider="test-provider",
        model="test-model",
        policy_identity=relationship_policy_identity,
    )
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "pair_decisions": [
                {
                    "decision_key": decision_key,
                    "pair_job_id": "negative-a-b",
                    "source_id": "A",
                    "target_source_id": "B",
                    "status": "no_relationship",
                    "relationship_policy_identity": relationship_policy_identity,
                }
            ],
            "current_pair_decisions": [
                {
                    "source_ids": ["A", "B"],
                    "status": "no_relationship",
                    "pair_job_id": "negative-a-b",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "provider": "test-provider",
                    "model": "test-model",
                    "input_profile_hashes": {
                        profile.source_id: stable_hash(profile_to_dict(profile))
                        for profile in profiles
                    },
                    "active": True,
                }
            ],
        },
    )
    write_yaml(
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "selection_identity": "older-discovery-prompt",
            "profile_hashes": {
                profile.source_id: stable_hash(profile_to_dict(profile))
                for profile in profiles
            },
            "relationship_memory_hashes": {
                profile.source_id: stable_hash([]) for profile in profiles
            },
        },
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert stage == "relationship_candidate_selection"
        assert context["prior_negative_pairs"] == [["A", "B"]]
        assert context["excluded_candidate_pairs"] == [("A", "B")]
        return {"candidates": [_candidate("A", "B")]}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls)

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_candidate_selection"
    ]
    assert result["pair_job_count"] == 0


def test_current_negative_pairs_reach_initial_shared_packets_and_replay(
    tmp_path: Path,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    relationship_policy_identity = stable_hash(
        {"relationship_semantic_policy": "source-owned-bases-v26"}
    )
    profile_hashes = {
        profile.source_id: stable_hash(profile_to_dict(profile))
        for profile in profiles
    }
    legacy_profile_hashes = {
        profile.source_id: stable_hash(asdict(profile)) for profile in profiles
    }
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "pair_decisions": [
                {
                    "decision_key": relationship_decision_key(
                        "A",
                        "B",
                        legacy_profile_hashes["A"],
                        legacy_profile_hashes["B"],
                        provider="test-provider",
                        model="test-model",
                        policy_identity=relationship_policy_identity,
                    ),
                    "pair_job_id": "negative-a-b",
                    "source_id": "A",
                    "target_source_id": "B",
                    "status": "no_relationship",
                    "relationship_policy_identity": relationship_policy_identity,
                    "provider": "test-provider",
                    "model": "test-model",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "source_profile_hash": legacy_profile_hashes["A"],
                    "target_profile_hash": legacy_profile_hashes["B"],
                }
            ],
            "current_pair_decisions": [
                {
                    "source_ids": ["A", "B"],
                    "status": "no_relationship",
                    "pair_job_id": "negative-a-b",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "provider": "test-provider",
                    "model": "test-model",
                    "input_profile_hashes": {
                        "A": legacy_profile_hashes["A"],
                        "B": legacy_profile_hashes["B"],
                    },
                    "active": True,
                }
            ],
        },
    )
    write_yaml(
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "selection_identity": "older-discovery-prompt",
            "profile_hashes": profile_hashes,
            "relationship_memory_hashes": {
                profile.source_id: stable_hash([]) for profile in profiles
            },
        },
    )
    shared_plan = {
        "lean_index_hash": "lean",
        "requested_collection_keys": ["C1", "C2"],
        "literature_families": [
            {"family_id": "requested", "source_ids": ["A", "B", "C", "D"]},
            {"family_id": "complement", "source_ids": ["A", "B"]},
        ],
        "discovery_jobs": [
            {
                "job_id": "requested",
                "family": "requested",
                "left_source_ids": ["A", "C"],
                "right_source_ids": ["B", "D"],
                "requested_collection_pair": ["C1", "C2"],
                "candidate_quota": 3,
            },
            {
                "job_id": "complement",
                "family": "complement",
                "left_source_ids": ["A"],
                "right_source_ids": ["B"],
                "candidate_quota": 1,
            },
        ],
    }

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_adjudication":
            return {"decisions": [_decision(row) for row in context["pair_jobs"]]}
        assert stage in {
            "relationship_bridge_candidate_selection",
            "relationship_candidate_selection",
        }
        assert context["prior_negative_pairs"] == [["A", "B"]]
        if context["discovery_pass"] == "breadth_completion":
            assert context["excluded_candidate_pairs"] == [
                ("A", "B"),
                ("A", "D"),
            ]
            return {
                "candidates": [
                    {
                        **_candidate("B", "C", rank=1),
                        "bridge_job_id": "requested",
                    },
                    {
                        **_candidate("C", "D", rank=2),
                        "bridge_job_id": "requested",
                    },
                ],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "completed"}
                ],
            }
        assert context["excluded_candidate_pairs"] == [("A", "B")]
        if context["discovery_pass"] == "broad":
            return {
                "candidates": [
                    {
                        **_candidate("A", "D"),
                        "bridge_job_id": "requested",
                    }
                ],
                "job_outcomes": [
                    {"bridge_job_id": "requested", "status": "completed"}
                ],
            }
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

    calls = _Calls(handler)
    result = _run(
        tmp_path,
        profiles,
        calls,
        shared_family_plan=shared_plan,
    )

    assert {
        context["discovery_pass"]
        for stage, _key, context in calls.seen
        if stage.endswith("candidate_selection")
    } == {"broad", "breadth_completion"}
    assert result["pair_job_count"] == 3
    registry_bytes = (
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    ).read_bytes()
    _commit_relationship_selection_state(
        tmp_path,
        result,
        catalogue_revision=result["reconciled_catalogue_revision"],
    )
    replay_calls = _Calls(
        lambda stage, _profiles, _context: (_ for _ in ()).throw(
            AssertionError(f"unexpected replay call: {stage}")
        )
    )
    replay = _run(
        tmp_path,
        profiles,
        replay_calls,
        shared_family_plan=shared_plan,
    )
    assert replay_calls.seen == []
    assert replay["semantic_noop"] is True
    assert (
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    ).read_bytes() == registry_bytes


def test_changed_endpoint_does_not_reuse_legacy_negative_hashes(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    relationship_policy_identity = stable_hash(
        {"relationship_semantic_policy": "source-owned-bases-v26"}
    )
    legacy_hashes = {
        profile.source_id: stable_hash(asdict(profile)) for profile in profiles
    }
    profiles[0].context["thesis"] = "Materially changed endpoint evidence."
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "pair_decisions": [
                {
                    "decision_key": relationship_decision_key(
                        "A",
                        "B",
                        legacy_hashes["A"],
                        legacy_hashes["B"],
                        provider="test-provider",
                        model="test-model",
                        policy_identity=relationship_policy_identity,
                    ),
                    "pair_job_id": "negative-a-b",
                    "source_id": "A",
                    "target_source_id": "B",
                    "status": "no_relationship",
                    "relationship_policy_identity": relationship_policy_identity,
                    "provider": "test-provider",
                    "model": "test-model",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "source_profile_hash": legacy_hashes["A"],
                    "target_profile_hash": legacy_hashes["B"],
                }
            ],
            "current_pair_decisions": [
                {
                    "source_ids": ["A", "B"],
                    "status": "no_relationship",
                    "pair_job_id": "negative-a-b",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "provider": "test-provider",
                    "model": "test-model",
                    "input_profile_hashes": legacy_hashes,
                    "active": True,
                }
            ],
        },
    )

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            assert context["prior_negative_pairs"] == []
            assert context["excluded_candidate_pairs"] == []
            return {"candidates": [_candidate("A", "B")]}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    result = _run(tmp_path, profiles, _Calls(handler))

    assert result["pair_job_count"] == 1
    stored = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml", {}
    )
    assert stored["current_pair_decisions"][0]["input_profile_hashes"] == (
        legacy_hashes
    )


def test_changed_relationship_prompt_reconsiders_current_negative(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    relationship_policy_identity = stable_hash(
        {"relationship_semantic_policy": "source-owned-bases-v26"}
    )
    profile_hashes = {
        profile.source_id: stable_hash(profile_to_dict(profile))
        for profile in profiles
    }
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "pair_decisions": [
                {
                    "decision_key": relationship_decision_key(
                        "A",
                        "B",
                        profile_hashes["A"],
                        profile_hashes["B"],
                        provider="test-provider",
                        model="test-model",
                        policy_identity=relationship_policy_identity,
                    ),
                    "pair_job_id": "negative-a-b",
                    "source_id": "A",
                    "target_source_id": "B",
                    "status": "no_relationship",
                    "relationship_policy_identity": relationship_policy_identity,
                }
            ],
            "current_pair_decisions": [
                {
                    "source_ids": ["A", "B"],
                    "status": "no_relationship",
                    "pair_job_id": "negative-a-b",
                    "prompt_version": pipeline_module.RELATIONSHIP_PROMPT_VERSION,
                    "provider": "test-provider",
                    "model": "test-model",
                    "input_profile_hashes": profile_hashes,
                    "active": True,
                }
            ],
        },
    )
    write_yaml(
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "selection_identity": "older-discovery-prompt",
            "profile_hashes": profile_hashes,
            "relationship_memory_hashes": {
                profile.source_id: stable_hash([]) for profile in profiles
            },
        },
    )
    monkeypatch.setattr(pipeline_module, "RELATIONSHIP_PROMPT_VERSION", "changed")

    def handler(
        stage: str,
        _profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            assert context["prior_negative_pairs"] == []
            assert context["excluded_candidate_pairs"] == []
            return {"candidates": [_candidate("A", "B")]}
        return {"decisions": [_decision(context["pair_jobs"][0])]}

    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls)

    assert [stage for stage, _key, _context in calls.seen] == [
        "relationship_candidate_selection",
        "relationship_adjudication",
    ]
    assert result["pair_job_count"] == 1


def test_only_active_current_relationship_satisfies_a_discovered_pair(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    for active, expected_jobs in ((True, 0), (False, 1)):
        workspace = tmp_path / ("active" if active else "inactive")
        write_yaml(
            workspace
            / "02_source_memory"
            / "indexes"
            / "typed_links.yml",
            {
                "relations": [
                    {
                        "relation_id": "current-relation",
                        "source_id": "A",
                        "target_source_id": "B",
                            "relation_type": "supports",
                            "active": active,
                        },
                        *(
                            [
                                {
                                    "relation_id": "citation-relation",
                                    "source_id": "A",
                                    "target_source_id": "B",
                                    "relation_type": "cites",
                                    "active": True,
                                }
                            ]
                            if active
                            else []
                        ),
                    ],
                "current_pair_decisions": [
                    {
                        "source_ids": ["A", "B"],
                        "status": "accepted",
                        "relation_ids": ["current-relation"],
                        "provider": "test-provider",
                        "model": "test-model",
                        "input_profile_hashes": {
                            profile.source_id: stable_hash(asdict(profile))
                            for profile in profiles
                        },
                    }
                ],
            },
        )

        def handler(stage, _profiles, context):
            if stage == "relationship_candidate_selection":
                return {"candidates": [_candidate("A", "B")]}
            return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

        result = _run(workspace, profiles, _Calls(handler))

        assert result["pair_job_count"] == expected_jobs
        if not active:
            assert any(
                row.get("reconsideration") == "inactive_or_retired_reconsidered"
                for row in result["candidate_dispositions"]
            )
            assert len(result["accepted"]) == 1


def test_changed_endpoint_and_refresh_pending_refresh_active_relationships(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B"), _profile("C")]
    old_hashes = {
        profile.source_id: stable_hash(profile_to_dict(profile))
        for profile in profiles
    }
    profiles[0].context["thesis"] = "A materially changed thesis"
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        {
            "relations": [
                {
                    "relation_id": "relation-ab",
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "active": True,
                },
                {
                    "relation_id": "relation-bc",
                    "source_id": "B",
                    "target_source_id": "C",
                    "relation_type": "supports",
                    "active": True,
                },
            ],
            "current_pair_decisions": [
                {
                    "source_ids": list(pair),
                    "status": "accepted",
                    "relation_ids": [relation_id],
                    "provider": "test-provider",
                    "model": "test-model",
                    "refresh_pending": relation_id == "relation-bc",
                    "input_profile_hashes": {
                        source_id: old_hashes[source_id] for source_id in pair
                    },
                }
                for pair, relation_id in (
                    (("A", "B"), "relation-ab"),
                    (("B", "C"), "relation-bc"),
                )
            ],
        },
    )

    def handler(stage, _profiles, context):
        if stage == "relationship_candidate_selection":
            return {"candidates": []}
        assert [job["source_ids"] for job in context["pair_jobs"]] == [
            ["A", "B"],
            ["B", "C"],
        ]
        return {"decisions": [_decision(job) for job in context["pair_jobs"]]}

    result = _run(tmp_path, profiles, _Calls(handler))

    assert result["pair_job_count"] == 2


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


class _OrdinaryReasoner(_Reasoner):
    ordinary_relationship_decision_contract = "relationship-decision-v11"
    relationship_decision_contract = "relationship-decision-v10"


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mixed_direction", [False, True])
def test_ordinary_distinct_connections_persist_and_replay(tmp_path: Path, provider: str, reverse: bool, mixed_direction: bool) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    rows = [{**_ordinary_candidate("A", "B"), "relation_type": kind, "reason": reason}
            for kind, reason in (
                ("complements", "The accounts supply complementary measures of reputation."),
                ("methodological_fault_line", "The surveys differ in sampling and measurement scope."),
            )]
    if mixed_direction:
        rows[0].update(relation_type="supports", actor_source_id="B", reference_source_id="A")
        rows[1]["relation_type"] = "contextual_connection"
    if reverse:
        rows.reverse()
    calls = _Calls(lambda stage, *_args: {"candidates": rows}
                   if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    reasoner = _OrdinaryReasoner()
    reasoner.name = provider
    result = _run(tmp_path, profiles, calls, reasoner=reasoner)
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert {row["relation_type"]: row["reason"] for row in result["accepted"]} == {
        row["relation_type"]: row["reason"] for row in rows}
    assert len(calls.seen) == 1
    for source_id in "AB":
        links = pipeline_module.projected_related_links(source_id, profiles, result["accepted"], max_inferred_links=10)
        assert len(links) == 2
    _commit_relationship_selection_state(tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"])
    replay = _run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("replay provider call")), reasoner=reasoner)
    assert replay["semantic_noop"] is True


def test_ordinary_partial_connection_envelope_cannot_complete_pair(tmp_path: Path) -> None:
    rows = [{**_ordinary_candidate("A", "B"), "relation_type": "complements"},
            {**_ordinary_candidate("A", "B"), "relation_type": "methodological_fault_line", "reason": ""}]
    calls = _Calls(lambda stage, *_args: {"candidates": rows}
                   if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    result = _run(tmp_path, [_profile(source_id) for source_id in "AB"], calls, reasoner=_OrdinaryReasoner())
    assert result["relationship_stage_complete"] is False
    assert result["parked"]
    assert not list((tmp_path / "11_state/relationship_jobs").glob("*/result.json"))
    assert len(calls.seen) == 1


@pytest.mark.parametrize("kind", ["third_role", "duplicate_bad_member", "negative", "directed", "missing_direction"])
def test_ordinary_multiple_roles_keep_conflicts_and_overflow_blocking(tmp_path: Path, kind: str) -> None:
    rows = [{**_ordinary_candidate("A", "B"), "relation_type": "complements"},
            {**_ordinary_candidate("A", "B"), "relation_type": "methodological_fault_line"}]
    if kind == "third_role":
        rows.append(_ordinary_candidate("A", "B"))
    elif kind == "duplicate_bad_member":
        rows.append({**rows[1], "reason": ""})
    elif kind == "negative":
        rows[1] = _ordinary_candidate("A", "B", "no_relationship")
    elif kind == "missing_direction":
        rows[0].update(relation_type="supports", actor_source_id="A")
    else:
        rows = [{**rows[0], "relation_type": "supports", "actor_source_id": actor,
                 "reference_source_id": reference} for actor, reference in [("A", "B"), ("B", "A")]]
    calls = _Calls(lambda stage, *_args: {"candidates": rows}
                   if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    result = _run(tmp_path, [_profile(source_id) for source_id in "AB"], calls, reasoner=_OrdinaryReasoner())
    assert result["relationship_stage_complete"] is False
    assert result["parked"]
    assert not list((tmp_path / "11_state/relationship_jobs").glob("*/result.json"))
    assert len(calls.seen) == 1


def _ordinary_candidate(left: str, right: str, decision: str = "relationship") -> dict[str, Any]:
    return {
        "left_source_id": left, "right_source_id": right, "decision": decision,
        "relation_type": "contextual_connection" if decision == "relationship" else "",
        "actor_source_id": None, "reference_source_id": None,
        "reason": "The notes connect institutional choices with implementation outcomes.",
        "rank": 1,
    }


@pytest.mark.parametrize("decision", ["relationship", "no_relationship"])
@pytest.mark.parametrize("frozen", [False, True])
def test_ordinary_linking_finishes_during_discovery_and_replays(tmp_path: Path, decision: str, frozen: bool) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    calls = _Calls(lambda stage, _profiles, _context: {
        "candidates": [_ordinary_candidate("A", "B", decision)],
    } if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    result = _run(tmp_path, profiles, calls, reasoner=_OrdinaryReasoner())
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert len(result["accepted"] if decision == "relationship" else result["no_relationship"]) == 1
    assert len(calls.seen) == 1
    assert result["provider_batch_count"] == 0
    job_inputs = list((tmp_path / "11_state" / "runs" / calls.run_id / "relationship_jobs").glob("*/input.json"))
    assert len(job_inputs) == 1
    job = json.loads(job_inputs[0].read_text())
    assert job["selected_evidence"] == {}
    assert not any(job["atomic_notes"].values())
    assert job["output_contract"] == "relationship-decision-v11"
    _commit_relationship_selection_state(tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"])
    replay = _run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("replay provider call")), reasoner=_OrdinaryReasoner(),
                  frozen_pair_jobs=[RelationshipPairJob.from_dict(job)] if frozen else None)
    assert replay["semantic_noop"] is True


@pytest.mark.parametrize("kind", ["conflict", "missing_decision"])
def test_ordinary_malformed_decisions_do_not_trigger_another_judgment(tmp_path: Path, kind: str) -> None:
    row = _ordinary_candidate("A", "B")
    if kind == "missing_decision":
        row.pop("decision")
    rows = [row] + ([_ordinary_candidate("A", "B", "no_relationship")] if kind == "conflict" else [])
    calls = _Calls(lambda stage, _profiles, _context: {"candidates": rows}
                   if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    result = _run(tmp_path, [_profile(source_id) for source_id in "AB"], calls, reasoner=_OrdinaryReasoner())
    assert result["accepted"] == []
    assert result["parked"]
    assert result["relationship_stage_complete"] is False
    assert len(calls.seen) == 1


@pytest.mark.parametrize("omit", [False, True])
def test_ordinary_mandatory_pair_uses_compact_decision_and_preserves_omission(tmp_path: Path, omit: bool) -> None:
    write_yaml(tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml", {
        "positions": [{"current_source_id": "A", "matched_source_id": "B",
                       "engagement": "Explicit source citation.", "literature_position_id": "position-ab"}],
    })
    def handler(stage, provider_profiles, context):
        assert stage == "relationship_candidate_selection"
        assert provider_profiles == []
        assert context["required_pairs"] == [["A", "B"]]
        assert "atomic_notes" not in context and "source_evidence" not in context
        row = _ordinary_candidate("A", "B")
        row["bridge_job_id"] = context["bridge_jobs"][0]["bridge_job_id"]
        return {"candidates": [] if omit else [row]}
    calls = _Calls(handler)
    result = _run(tmp_path, [_profile(source_id) for source_id in "AB"], calls, reasoner=_OrdinaryReasoner())
    assert len(calls.seen) == 1
    assert result["provider_batch_count"] == 1
    assert bool(result["accepted"]) is not omit
    assert bool(result["parked"]) is omit
    assert result["no_relationship"] == []


@pytest.mark.parametrize("prior_contract,prior_prompt", [
    ("relationship-decision-v10", "35"), ("relationship-decision-v10", "21"),
])
def test_ordinary_refreshes_prior_full_note_decisions(tmp_path: Path, prior_contract: str, prior_prompt: str) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    write_yaml(tmp_path / "02_source_memory" / "indexes" / "typed_links.yml", {
        "relations": [{"relation_id": "old", "source_id": "A", "target_source_id": "B",
                       "relation_type": "contextual_connection", "active": True,
                       "output_contract": prior_contract}],
        "current_pair_decisions": [{"source_ids": ["A", "B"], "status": "accepted",
            "relation_ids": ["old"], "provider": "test-provider", "model": "test-model",
            "prompt_version": prior_prompt,
            "input_profile_hashes": {p.source_id: stable_hash(profile_to_dict(p)) for p in profiles}}],
    })
    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection"
        assert context["required_pairs"] == [["A", "B"]]
        return {"candidates": [{**_ordinary_candidate("A", "B"),
            "bridge_job_id": context["bridge_jobs"][0]["bridge_job_id"]}]}
    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls, reasoner=_OrdinaryReasoner())
    assert len(calls.seen) == 1
    assert result["accepted"][0]["output_contract"] == "relationship-decision-v11"
    assert result["accepted"][0]["prompt_version"] == pipeline_module.RELATIONSHIP_DISCOVERY_PROMPT_VERSION


def test_ordinary_acceptance_does_not_require_an_unused_second_call_budget(tmp_path: Path) -> None:
    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection"
        job_id = context["bridge_jobs"][0]["bridge_job_id"]
        return {"candidates": [{**_ordinary_candidate("A", "B"), "bridge_job_id": job_id}],
                "job_outcomes": [{"bridge_job_id": job_id, "status": "no_more_candidates"}]}
    calls = _Calls(handler)
    calls.max_calls = 1
    calls.cumulative_provider_calls = 0
    result = _run(tmp_path, [_profile(s) for s in "AB"], calls, reasoner=_OrdinaryReasoner(),
        request=LiteratureMapRequest(tmp_path, provider="test-provider", model="test-model",
            literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False)),
        shared_family_plan={"lean_index_hash": "lean",
            "literature_families": [{"family_id": "one", "source_ids": ["A", "B"]}],
            "discovery_jobs": [{"job_id": "one", "family": "one",
                "left_source_ids": ["A"], "right_source_ids": ["B"], "candidate_quota": 1}]})
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert len(result["accepted"]) == 1
    assert calls.cumulative_provider_calls == 1
    assert result["candidate_dispositions"][0]["disposition"] == "accepted"


def test_cluster_reserve_preserves_completed_ordinary_links(tmp_path: Path) -> None:
    write_yaml(tmp_path / "02_source_memory/indexes/literature_positions.yml", {
        "positions": [{"current_source_id": "A", "matched_source_id": "C",
                       "engagement": "Explicit citation.", "literature_position_id": "ac"}],
    })

    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection"
        assert "required_pairs" not in context
        job_id = context["bridge_jobs"][0]["bridge_job_id"]
        return {"candidates": [{**_ordinary_candidate("A", "B"), "bridge_job_id": job_id}],
                "job_outcomes": [{"bridge_job_id": job_id, "status": "no_more_candidates"}]}

    calls = _Calls(handler)
    calls.max_calls = 2
    calls.cumulative_provider_calls = 0
    result = _run(tmp_path, [_profile(s) for s in "ABC"], calls, reasoner=_OrdinaryReasoner(),
        request=LiteratureMapRequest(tmp_path, provider="test-provider", model="test-model",
            literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=True)),
        shared_family_plan={"lean_index_hash": "lean",
            "literature_families": [{"family_id": "one", "source_ids": ["A", "B"]}],
            "discovery_jobs": [{"job_id": "one", "family": "one",
                "left_source_ids": ["A"], "right_source_ids": ["B"], "candidate_quota": 1}]})
    assert len(result["accepted"]) == 1
    assert result["relationship_stage_complete"] is False
    assert result["relationship_retry_on_resume"] is True
    assert calls.cumulative_provider_calls == 2
    assert len(result["parked"]) == 1
    assert result["parked"][0]["status"] == "pending"
    assert result["parked"][0]["reason"] == "provider_call_budget_reserved_for_clusters"


def test_ordinary_negative_remains_current_when_another_source_arrives(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    first = _run(tmp_path, profiles, _Calls(lambda *_args: {
        "candidates": [_ordinary_candidate("A", "B", "no_relationship")]}), reasoner=_OrdinaryReasoner())
    pipeline_module.persist_relationship_registry(tmp_path, structural_relations=[], accepted_relations=[],
        no_relationship_decisions=first["no_relationship"], parked_rows=[])
    _commit_relationship_selection_state(tmp_path, first, catalogue_revision=first["reconciled_catalogue_revision"])
    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection"
        assert context["prior_negative_pairs"] == [["A", "B"]]
        return {"candidates": [_ordinary_candidate("A", "C")]}
    calls = _Calls(handler)
    result = _run(tmp_path, [*profiles, _profile("C")], calls, reasoner=_OrdinaryReasoner())
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert len(calls.seen) == 1
    assert result["accepted"][0]["target_source_id"] == "C"


@pytest.mark.parametrize("decision,extra", [
    ("no_relationship", {"secondary_relation_types": ["supports"]}),
    ("no_relationship", {"connections": [{"primary_relation_type": "supports"}]}),
    ("relationship", {"source_a_anchor_ids": ["unknown-anchor"]}),
    ("relationship", {"actor_source_id": "unknown-source"}),
    ("no_relationship", {"reason": "   "}),
    ("no_relationship", {"reason": True}),
])
def test_ordinary_rejects_hidden_active_fields_before_projection(tmp_path: Path, decision: str, extra: dict[str, Any]) -> None:
    calls = _Calls(lambda stage, _profiles, _context: {
        "candidates": [{**_ordinary_candidate("A", "B", decision), **extra}],
    } if stage == "relationship_candidate_selection" else pytest.fail("extra judgment"))
    result = _run(tmp_path, [_profile(source_id) for source_id in "AB"], calls, reasoner=_OrdinaryReasoner())
    assert result["accepted"] == []
    assert result["no_relationship"] == []
    assert result["parked"]
    assert len(calls.seen) == 1


@pytest.mark.parametrize("overlapping_sides", [False, True])
@pytest.mark.parametrize("omit_completion", [False, True])
def test_ordinary_small_family_completes_below_quota_without_rejudging(
    tmp_path: Path, overlapping_sides: bool, omit_completion: bool,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    plan = {
        "lean_index_hash": "lean",
        "literature_families": [{"family_id": "family", "label": "Declared family",
                                 "source_ids": list("ABCD"), "candidate_cluster": True}],
        "discovery_jobs": [{"job_id": "family-job", "family": "Declared family",
                            "left_source_ids": list("ABCD" if overlapping_sides else "AB"),
                            "right_source_ids": list("ABCD" if overlapping_sides else "CD"),
                            "candidate_quota": 4}],
    }
    def handler(stage, provider_profiles, context):
        assert stage == "relationship_candidate_selection"
        assert provider_profiles == []
        if "required_pairs" in context:
            assert provider_profiles == []
            assert context["required_pairs"] == [["B", "D"], ["C", "D"]]
            candidates = [] if omit_completion else [
                {**_ordinary_candidate(*pair, "no_relationship"),
                 "bridge_job_id": job["bridge_job_id"]}
                for pair, job in zip(context["required_pairs"], context["bridge_jobs"], strict=True)
            ]
            return {"candidates": candidates}
        assert {row["source_id"] for row in context["catalogue"]} == set("ABCD")
        job = context["bridge_jobs"][0]
        assert set(job["left_source_ids"]) == set(job["right_source_ids"]) == set("ABCD")
        return {"candidates": [
            {**_ordinary_candidate(left, right), "bridge_job_id": job["bridge_job_id"]}
            for left, right in [("A", "B"), ("A", "C"), ("A", "D"), ("B", "C")]
        ], "job_outcomes": [{"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}]}
    calls = _Calls(handler)
    result = _run(tmp_path, profiles, calls, reasoner=_OrdinaryReasoner(), shared_family_plan=plan)
    assert len(calls.seen) == 2, result["parked"]
    assert len(result["accepted"]) == 4
    assert len(result["no_relationship"]) == (0 if omit_completion else 2)
    assert bool(result["parked"]) is omit_completion
    assert result["pair_job_count"] == 6
    if not omit_completion:
        _commit_relationship_selection_state(tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"])
        replay = _run(tmp_path, profiles, _Calls(lambda *_: pytest.fail("resolved decision regenerated")),
                      reasoner=_OrdinaryReasoner(), shared_family_plan=plan)
        assert replay["semantic_noop"] is True


@pytest.mark.parametrize("scope", ["unknown", "ambiguous", "mixed_family", "explicit", "endpoint_only"])
def test_ordinary_family_normalization_preserves_hard_scopes(tmp_path: Path, scope: str) -> None:
    family = {"family_id": "family", "label": "Family", "source_ids": list("ABCD"),
              "candidate_cluster": True}
    job = {"job_id": "scoped", "family": "Family", "left_source_ids": list("AB"),
           "right_source_ids": list("CD"), "candidate_quota": 1}
    families = [family]
    if scope == "unknown":
        job["family"] = "unknown"
    elif scope == "ambiguous":
        families.append({**family, "family_id": "second"})
    elif scope == "mixed_family":
        family["source_ids"] = list("ABC")
    elif scope == "explicit":
        job["requested_collection_pair"] = ["C1", "C2"]
    else:
        job["endpoint_coverage_only"] = True
    observed = []
    def handler(stage, _profiles, context):
        assert stage.endswith("candidate_selection")
        if "required_pairs" in context:
            return {"candidates": [
                {**_ordinary_candidate(*pair, "no_relationship"), "bridge_job_id": row["bridge_job_id"]}
                for pair, row in zip(context["required_pairs"], context["bridge_jobs"], strict=True)
            ]}
        candidates = []
        for row in context["bridge_jobs"]:
            if row["bridge_job_id"] == "scoped":
                observed.append(row)
                assert row["left_source_ids"] == list("AB")
                assert row["right_source_ids"] == list("CD")
                candidates.extend({**_ordinary_candidate(*pair), "bridge_job_id": "scoped"}
                                  for pair in [("A", "B"), ("A", "C")])
        return {"candidates": candidates, "job_outcomes": [
            {"bridge_job_id": row["bridge_job_id"], "status": "no_more_candidates"}
            for row in context["bridge_jobs"]
        ]}
    result = _run(tmp_path, [_profile(source_id) for source_id in "ABCD"], _Calls(handler),
                  reasoner=_OrdinaryReasoner(), shared_family_plan={
                      "lean_index_hash": "lean", "literature_families": families, "discovery_jobs": [job]})
    assert observed
    assert [(row["source_id"], row["target_source_id"]) for row in result["accepted"]] == [("A", "C")]


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("already_covered", [False, True])
def test_cross_family_job_does_not_hide_internal_pairs(
    tmp_path: Path, provider: str, reverse: bool, already_covered: bool,
) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    sides = [list("AC"), list("BD")] if already_covered else [list("AB"), list("CD")]
    if reverse:
        sides.reverse()
    plan = {
        "lean_index_hash": "lean",
        "literature_families": [
            {"family_id": members, "source_ids": list(members)} for members in ["AB", "CD"]
        ],
        "discovery_jobs": [{"job_id": "cross", "family": "bridge",
                            "left_source_ids": sides[0], "right_source_ids": sides[1],
                            "candidate_quota": 4}],
    }
    observed = set()
    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection", "no extra judgment"
        candidates = []
        for job in context["bridge_jobs"]:
            left, right = set(job["left_source_ids"]), set(job["right_source_ids"])
            if job["bridge_job_id"] == "cross":
                assert [sorted(left), sorted(right)] == sides
                pairs = [("A", "B" if already_covered else "C", "relationship")]
            else:
                assert left == right
                observed.add(tuple(sorted(left)))
                pairs = [(*sorted(left), "relationship" if left == set("AB") else "no_relationship")]
            candidates.extend({**_ordinary_candidate(a, b, decision), "bridge_job_id": job["bridge_job_id"]}
                              for a, b, decision in pairs)
        return {"candidates": candidates, "job_outcomes": [
            {"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}
            for job in context["bridge_jobs"]]}
    calls = _Calls(handler)
    reasoner = _OrdinaryReasoner()
    reasoner.name = provider
    result = _run(tmp_path, profiles, calls, reasoner=reasoner, shared_family_plan=plan)
    assert observed == (set() if already_covered else {("A", "B"), ("C", "D")})
    assert result["relationship_stage_complete"] is True, result["parked"]
    assert {(row["source_id"], row["target_source_id"]) for row in result["accepted"]} == (
        {("A", "B")} if already_covered else {("A", "B"), ("A", "C")})
    assert len(result["no_relationship"]) == (0 if already_covered else 1)
    _commit_relationship_selection_state(tmp_path, result, catalogue_revision=result["reconciled_catalogue_revision"])
    replay = _run(tmp_path, profiles, _Calls(lambda *_: pytest.fail("resolved decision regenerated")),
                  reasoner=reasoner, shared_family_plan=plan)
    assert replay["semantic_noop"] is True


def test_ordinary_family_completion_keeps_full_page_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_RELATIONSHIP_DISCOVERY_PAGE_SIZE", 5)
    def handler(stage, _profiles, context):
        assert stage == "relationship_candidate_selection"
        assert "required_pairs" not in context
        job = context["bridge_jobs"][0]
        return {"candidates": [{**_ordinary_candidate("A", "B"), "bridge_job_id": job["bridge_job_id"]}],
                "job_outcomes": [{"bridge_job_id": job["bridge_job_id"], "status": "no_more_candidates"}]}
    calls = _Calls(handler)
    result = _run(tmp_path, [_profile(source_id) for source_id in "ABCD"], calls,
                  reasoner=_OrdinaryReasoner(), shared_family_plan={
                      "lean_index_hash": "lean",
                      "literature_families": [{"family_id": "family", "source_ids": list("ABCD"),
                                               "candidate_cluster": True}],
                      "discovery_jobs": [{"job_id": "family", "family": "family", "candidate_quota": 1,
                                          "left_source_ids": list("AB"), "right_source_ids": list("CD")}],
                  })
    assert len(calls.seen) == 1
    assert len(result["accepted"]) == 1
    assert result["no_relationship"] == []
    assert not any(row.get("deterministic_family_completion_count") for row in result["relationship_discovery_jobs"])
