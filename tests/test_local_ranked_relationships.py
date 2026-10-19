from __future__ import annotations

import json
import socket
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from auto_zettelkasten import pipeline
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.literature import _CheckpointedReasonerCalls
from auto_zettelkasten.models import LiteratureMapRequest, LiteratureMappingPolicy, NavigationPolicy
from auto_zettelkasten.relationships import persist_relationship_registry, profile_content_hash

from test_relationship_pipeline_orchestration import (
    _Calls, _OrdinaryReasoner, _catalogue, _ordinary_candidate, _profile, _write_atomic_note,
)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("local ranked relationship tests must stay offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def candidate(left: str, right: str, **extra: Any) -> dict[str, Any]:
    return {
        "left_source_id": left, "right_source_id": right,
        "left_source_title": f"Source {left}", "right_source_title": f"Source {right}",
        "rank": 1, "score": 2.0, "why_compare": "A local shared-reference signal.",
        "discovery_route": "deterministic_local_ranked",
        "discovery_provenance": [{"signal": "shared_reference", "reference_id": "doi:10.synthetic/shared"}],
        **extra,
    }


def decisions(stage: str, profiles: Any, context: Any) -> dict[str, Any]:
    assert stage == "relationship_candidate_selection"
    assert profiles == []
    assert context["discovery_mode"] == "local_ranked_shortlist"
    assert 0 < len(context["required_pairs"]) <= 8
    ids = {value for pair in context["required_pairs"] for value in pair}
    assert [row["source_id"] for row in context["catalogue"]] == sorted(ids)
    assert len(context["catalogue"]) == len(ids)
    return {"candidates": [
        {**_ordinary_candidate(*pair), "bridge_job_id": job["bridge_job_id"]}
        for pair, job in zip(context["required_pairs"], context["bridge_jobs"], strict=True)
    ]}


def run(workspace: Path, profiles: Any, calls: _Calls, rows: Any, **kwargs: Any) -> dict[str, Any]:
    return pipeline._run_relationship_reasoning(
        workspace, profiles=profiles, source_set={"source_set_type": "collection"},
        catalogue=_catalogue(workspace, profiles), reasoner=kwargs.pop("reasoner", _OrdinaryReasoner()), reasoner_calls=calls,
        request=LiteratureMapRequest(workspace, provider="test-provider", model="test-model",
            literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False)),
        ranked_candidates=rows, **kwargs,
    )


def commit(workspace: Path, result: dict[str, Any]) -> None:
    persist_relationship_registry(workspace, structural_relations=[],
        accepted_relations=result["accepted"], no_relationship_decisions=result["no_relationship"],
        parked_rows=[*result["parked"], *result.get("refresh_pending", [])])
    pipeline._commit_relationship_selection_state(workspace, result,
        catalogue_revision=result["reconciled_catalogue_revision"])


def test_deduplicates_reverse_pairs_and_reuses_rank_only_changes(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    rows = [candidate("A", "B"), candidate("B", "A", rank=7)]
    calls = _Calls(decisions)
    result = run(tmp_path, profiles, calls, rows)
    assert result["relationship_stage_complete"] is True
    assert result["pair_job_count"] == len(result["accepted"]) == len(calls.seen) == 1
    jobs = list((Path(result["job_root"])).glob("*/input.json"))
    basis = json.loads(jobs[0].read_text())["candidate_basis"]
    assert basis == pipeline._local_ranked_candidate_basis([candidate("A", "B")])[("A", "B")]
    assert all(key not in basis[0] for key in ("rank", "score", "why_compare"))
    changed = [candidate("B", "A", rank=2, score=999, why_compare="Different selection text.")]
    replay = run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("rank churn repeated a paid job")), changed)
    assert replay["provider_batch_count"] == 0
    assert replay["selected_candidate_pool_hash"] == result["selected_candidate_pool_hash"]
    assert replay["accepted"][0]["pair_job_id"] == result["accepted"][0]["pair_job_id"]


def test_reuses_accepted_and_negative_decisions_and_keeps_material_changes(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABCD"]
    rows = [candidate("A", "B"), candidate("A", "C")]
    def handler(stage: Any, provider_profiles: Any, context: Any) -> dict[str, Any]:
        result = decisions(stage, provider_profiles, context)
        result["candidates"][1].update(decision="no_relationship", relation_type="")
        return result
    first = run(tmp_path, profiles, _Calls(handler), rows)
    commit(tmp_path, first)
    replay = run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("settled pair recharged")), rows)
    assert replay["pair_job_count"] == 0
    assert {row["disposition"] for row in replay["candidate_dispositions"]} == {"already_visible", "current_no_relationship"}
    commit(tmp_path, replay)
    empty = run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("empty shortlist call")), [])
    commit(tmp_path, empty)
    changed = [candidate("A", "B", discovery_provenance=[{"signal": "direct_reference", "reference_id": "new"}]), rows[1]]
    calls = _Calls(decisions)
    refreshed = run(tmp_path, profiles, calls, changed)
    assert len(calls.seen) == 1
    assert calls.seen[0][2]["required_pairs"] == [["A", "B"]]
    assert refreshed["accepted"][0]["pair_job_id"] != first["accepted"][0]["pair_job_id"]


def test_popular_citation_and_family_pools_cannot_refill_local_shortlist(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEFGH"]
    pairs = list(combinations("ABCDEFGH", 2))
    write_yaml(tmp_path / "02_source_memory/indexes/typed_links.yml", {"relations": [
        {"source_id": left, "target_source_id": right, "relation_type": "cites"}
        for left, right in pairs
    ]})
    write_yaml(tmp_path / "02_source_memory/indexes/literature_positions.yml", {"positions": [
        {"current_source_id": left, "matched_source_id": right, "engagement": "Citation."}
        for left, right in pairs
    ]})
    plan = {"literature_families": [{"family_id": "popular", "source_ids": list("ABCDEFGH"), "candidate_cluster": True}],
        "discovery_jobs": [{"job_id": "all", "family": "popular", "left_source_ids": list("ABCDEFGH"),
                            "right_source_ids": list("ABCDEFGH"), "candidate_quota": 64}]}
    calls = _Calls(decisions)
    result = run(tmp_path, profiles, calls, [candidate("A", "B")], shared_family_plan=plan)
    assert result["pair_job_count"] == len(calls.seen) == 1
    assert calls.seen[0][2]["required_pairs"] == [["A", "B"]]
    assert result["cluster_candidates"] == []
    coverage = result["relationship_discovery_jobs"][0]
    assert coverage["scope"] == "selected_pairs_only"
    assert coverage["source_dispositions"][-1] == {"source_id": "H", "status": "not_examined"}


def test_eight_pair_batches_shrink_to_context_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = [_profile(source_id) for source_id in "ABCDEFGHIJKLMNO"]
    rows = [candidate("A", source_id) for source_id in "BCDEFGHIJKLMNO"]
    calls = _Calls(decisions)
    result = run(tmp_path, profiles, calls, rows)
    assert len(result["accepted"]) == 14
    assert sorted(len(context["required_pairs"]) for _, _, context in calls.seen) == [6, 8]
    original = pipeline._reasoner_packet_chars
    def packet_chars(profiles: Any, context: Any) -> int:
        return len(context["required_pairs"]) * 100 if "required_pairs" in context else original(profiles, context)
    monkeypatch.setattr(pipeline, "_reasoner_packet_chars", packet_chars)
    monkeypatch.setattr(pipeline, "_relationship_context_char_budget", lambda *_args: 350)
    changed = [{**row, "discovery_provenance": [{"signal": "shared_tag", "tag": "new"}]} for row in rows]
    smaller = _Calls(decisions)
    second = run(tmp_path, profiles, smaller, changed)
    assert len(second["accepted"]) == 14
    assert max(len(context["required_pairs"]) for _, _, context in smaller.seen) == 3


@pytest.mark.parametrize("kind", ["missing", "malformed", "scope"])
def test_retry_sends_only_unresolved_pair(tmp_path: Path, kind: str) -> None:
    profiles = [_profile(source_id) for source_id in "ABC"]
    rows = [candidate("A", "B"), candidate("A", "C")]
    def handler(stage: Any, provider_profiles: Any, context: Any) -> dict[str, Any]:
        result = decisions(stage, provider_profiles, context)
        if kind == "missing":
            result["candidates"].pop()
        elif kind == "scope":
            result["candidates"][1]["bridge_job_id"] = "wrong-job"
        else:
            result["candidates"][1]["reason"] = ""
        return result
    first = run(tmp_path, profiles, _Calls(handler), rows)
    assert first["relationship_stage_complete"] is False
    assert first["relationship_retry_on_resume"] is True
    assert len(first["accepted"]) == len(first["parked"]) == 1
    completed = list((tmp_path / "11_state/relationship_jobs").glob("*/result.json"))
    assert len(completed) == 1
    before = completed[0].read_bytes()
    calls = _Calls(decisions)
    second = run(tmp_path, profiles, calls, rows)
    assert second["relationship_stage_complete"] is True
    assert len(second["accepted"]) == 2
    assert calls.seen[0][2]["required_pairs"] == [["A", "C"]]
    assert len(calls.seen) == 1
    assert completed[0].read_bytes() == before


@pytest.mark.parametrize("extra", [
    {"decision": "relationship"}, {"left_source_id": "unknown"},
    {"right_source_id": "A"}, {"left_source_title": "Wrong title"},
    {"input_profile_hashes": {"A": "0" * 64}}, {"rank": False}, {"score": float("nan")},
    {"discovery_route": "provider"}, {"discovery_provenance": []},
])
def test_invalid_input_never_reaches_a_provider(tmp_path: Path, extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        run(tmp_path, [_profile(source_id) for source_id in "AB"],
            _Calls(lambda *_args: pytest.fail("invalid candidate reached provider")), [candidate("A", "B", **extra)])


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_single_failed_pair_bypasses_only_its_failed_provider_receipt(tmp_path: Path, kind: str) -> None:
    class Reasoner(_OrdinaryReasoner):
        call_count = 0

        def select_relationship_candidates(self, profiles: Any, _request: Any, *, context: Any) -> Any:
            self.call_count += 1
            result = decisions("relationship_candidate_selection", profiles, context)
            if self.call_count == 1:
                if kind == "missing":
                    result["candidates"] = []
                else:
                    result["candidates"][0]["reason"] = ""
            return result

    reasoner = Reasoner()
    calls = _CheckpointedReasonerCalls(tmp_path, "real-checkpoint", reasoner, LiteratureMapRequest(tmp_path))
    profiles = [_profile(source_id) for source_id in "AB"]
    first = run(tmp_path, profiles, calls, [candidate("A", "B")], reasoner=reasoner)
    assert first["relationship_stage_complete"] is False
    second = run(tmp_path, profiles, calls, [candidate("A", "B")], reasoner=reasoner)
    assert second["relationship_stage_complete"] is True
    assert reasoner.call_count == 2
    third = run(tmp_path, profiles, calls, [candidate("A", "B")], reasoner=reasoner)
    assert third["relationship_stage_complete"] is True
    assert reasoner.call_count == 2


@pytest.mark.parametrize("corrupt", [b"[]", b"null", b"\xff"])
@pytest.mark.parametrize("with_sibling", [False, True])
def test_corrupt_pair_cache_recovers_through_real_checkpoint(
    tmp_path: Path, corrupt: bytes, with_sibling: bool,
) -> None:
    class Reasoner(_OrdinaryReasoner):
        def __init__(self) -> None:
            self.packets: list[list[list[str]]] = []

        def select_relationship_candidates(self, profiles: Any, _request: Any, *, context: Any) -> Any:
            self.packets.append(context["required_pairs"])
            return decisions("relationship_candidate_selection", profiles, context)

    reasoner = Reasoner()
    calls = _CheckpointedReasonerCalls(tmp_path, "cache-corruption", reasoner, LiteratureMapRequest(tmp_path))
    profiles = [_profile(source_id) for source_id in ("ABC" if with_sibling else "AB")]
    rows = [candidate("A", "B"), *([candidate("A", "C")] if with_sibling else [])]
    first = run(tmp_path, profiles, calls, rows, reasoner=reasoner)
    assert first["relationship_stage_complete"] is True
    corrupted_job_id = next(row["pair_job_id"] for row in first["accepted"] if row["target_source_id"] == "B")
    roots = [Path(first["job_root"]), tmp_path / "11_state/relationship_jobs"]
    siblings = {path: path.read_bytes() for root in roots for path in root.glob("*/result.json")
                if path.parent.name != corrupted_job_id}
    for root in roots:
        (root / corrupted_job_id / "result.json").write_bytes(corrupt)

    recovered = run(tmp_path, profiles, calls, rows, reasoner=reasoner)
    assert recovered["relationship_stage_complete"] is True
    assert recovered["parked"] == []
    assert len(recovered["accepted"]) == len(rows)
    assert len(reasoner.packets) == (2 if with_sibling else 1)
    if with_sibling:
        assert reasoner.packets[-1] == [["A", "B"]]
    else:
        assert calls.checkpoint_hits == 1
    for root in roots:
        assert isinstance(json.loads((root / corrupted_job_id / "result.json").read_text()), dict)
    assert {path: path.read_bytes() for path in siblings} == siblings

    replay = run(tmp_path, profiles, calls, rows, reasoner=reasoner)
    assert replay["relationship_stage_complete"] is True
    assert len(reasoner.packets) == (2 if with_sibling else 1)


def test_bound_and_valid_endpoint_hashes(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    with pytest.raises(ValueError, match="256"):
        pipeline._local_ranked_candidate_basis([candidate("A", "B")] * 257)
    row = candidate("A", "B", input_profile_hashes={profile.source_id: profile_content_hash(profile) for profile in profiles})
    result = run(tmp_path, profiles, _Calls(decisions), [row])
    assert result["relationship_stage_complete"] is True
    assert "input_profile_hashes" not in result["selected_candidates"][0]["candidate_basis"][0]


@pytest.mark.parametrize("kind", ["cycle", "depth", "size", "nodes", "id", "title"])
def test_material_inputs_are_bounded_before_hashing(kind: str) -> None:
    row = candidate("A", "B")
    if kind == "cycle":
        row["discovery_provenance"][0]["cycle"] = row
    elif kind == "depth":
        nested: Any = "leaf"
        for _ in range(1_000):
            nested = {"child": nested}
        row["discovery_provenance"] = [nested]
    elif kind == "size":
        row["discovery_provenance"][0]["text"] = "x" * 1_048_577
    elif kind == "nodes":
        row["discovery_provenance"][0]["terms"] = [""] * 65_537
    elif kind == "id":
        row["left_source_id"] = "A" * 257
    else:
        row["left_source_title"] = "T" * 2_001
    with pytest.raises(ValueError):
        pipeline._local_ranked_candidate_basis([row])


def test_empty_shortlist_and_ineligible_sources_have_distinct_coverage(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABC"]
    empty = run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("empty shortlist called provider")), [])
    assert empty["relationship_stage_complete"] is True
    assert empty["pair_job_count"] == 0
    assert empty["no_relationship"] == []
    assert empty["relationship_discovery_jobs"][0]["shortlist_status"] == "examined_no_candidates"
    profiles[1].evidence_eligibility = "context_only"
    # Reuse the synthetic catalogue while supplying a coverage-limited profile.
    result = pipeline._run_relationship_reasoning(tmp_path, profiles=profiles,
        source_set={}, catalogue=_catalogue(tmp_path, profiles), reasoner=_OrdinaryReasoner(),
        reasoner_calls=_Calls(lambda *_args: pytest.fail("metadata-only comparison")),
        request=LiteratureMapRequest(tmp_path), ranked_candidates=[candidate("A", "B")])
    assert result["relationship_stage_complete"] is False
    assert result["no_relationship"] == []
    assert result["parked"][0]["status"] == "insufficient_evidence"


def test_missing_local_decision_capability_is_blocked_for_selected_pairs(tmp_path: Path) -> None:
    reasoner = _OrdinaryReasoner()
    reasoner.select_relationship_candidates = None
    result = run(tmp_path, [_profile(source_id) for source_id in "ABCD"],
        _Calls(lambda *_args: pytest.fail("missing capability call")), [candidate("A", "B")], reasoner=reasoner)
    assert result["relationship_stage_complete"] is False
    assert result["relationship_discovery_status"] == "blocked"
    assert result["pair_job_count"] == 1


def test_stale_active_pair_outside_shortlist_stays_refresh_pending(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "ABC"]
    first = run(tmp_path, profiles, _Calls(decisions), [candidate("A", "B")])
    commit(tmp_path, first)
    profiles[0].context["thesis"] = "A changed source claim."
    second = run(tmp_path, profiles, _Calls(lambda *_args: pytest.fail("outside shortlist refresh")), [])
    assert second["relationship_stage_complete"] is True
    assert second["refresh_pending"][0]["status"] == "not_examined"
    commit(tmp_path, second)
    current = read_yaml(tmp_path / "02_source_memory/indexes/typed_links.yml")["current_pair_decisions"]
    assert current[0]["refresh_pending"] is True


def test_source_content_and_private_overrides_survive_projection(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    for source_id in "AB":
        _write_atomic_note(tmp_path, source_id)
    analysis = tmp_path / "02_source_memory/profiles/synthetic-source-analysis.json"
    override = tmp_path.parent / f"{tmp_path.name}-personal-overrides/synthetic.json"
    for path in (analysis, override):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"personal_or_source_content":"preserve exactly"}\n')
    protected = {path: path.read_bytes() for path in (analysis, override)}
    note_paths = [tmp_path / f"02_source_memory/notes/Source {source_id}.md" for source_id in "AB"]
    semantic_before = [pipeline.semantic_note_hash(path.read_text()) for path in note_paths]
    result = run(tmp_path, profiles, _Calls(decisions), [candidate("A", "B")])
    commit(tmp_path, result)
    pipeline._project_atomic_graph(tmp_path,
        note_rows=[{"source_id": source_id, "note_path": str(path), "note_id": f"note-{source_id.lower()}"}
                   for source_id, path in zip("AB", note_paths, strict=True)],
        profiles=profiles, relations=result["accepted"], navigation={"typed_relations": [], "assignments": []},
        navigation_policy=NavigationPolicy())
    assert [pipeline.semantic_note_hash(path.read_text()) for path in note_paths] == semantic_before
    assert {path: path.read_bytes() for path in protected} == protected
    assert all("contextual" in path.read_text() for path in note_paths)


def test_none_preserves_legacy_discovery_and_identity(tmp_path: Path) -> None:
    profiles = [_profile(source_id) for source_id in "AB"]
    calls = _Calls(lambda stage, _profiles, context: {
        "candidates": [_ordinary_candidate("A", "B")]
    } if stage == "relationship_candidate_selection" and "required_pairs" not in context
      else pytest.fail("legacy route changed"))
    result = run(tmp_path, profiles, calls, None)
    assert result["relationship_stage_complete"] is True
    assert "relationship_scope" not in result
    assert result["discovery_identity"] == pipeline._relationship_discovery_identity("test-provider", "test-model")
    assert result["provider_batch_count"] == 0
    assert len(calls.seen) == 1
