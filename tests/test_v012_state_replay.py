from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import write_source_set
from auto_zettelkasten.literature import (
    LiteratureSynthesisPartialError,
    _CheckpointedReasonerCalls,
    _preserve_last_valid_clusters_on_refresh_failure,
    _same_provider_inputs,
    cluster_note_stem,
)
from auto_zettelkasten.models import (
    EvidenceAnchor,
    EvidenceProfile,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
)
from auto_zettelkasten.pipeline import (
    _ProfileProviderBudget,
    _compact_relationship_catalogue_entry,
    _commit_relationship_selection_state,
    _relationship_event_id,
    _run_relationship_reasoning,
    _write_relationship_run_ledger,
)


class _CallReasoner:
    name = "test-provider"
    model = "test-model"

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def propose_clusters(
        self,
        _profiles: Sequence[Any],
        _request: Any,
        *,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return (
            {"candidates": []}
            if isinstance(context.get("marker"), int)
            else {"clusters": []}
        )


def _request(workspace: Path, *, max_calls: int = 2) -> LiteratureMapRequest:
    return LiteratureMapRequest(
        workspace=workspace,
        run_id="run",
        provider="test-provider",
        model="test-model",
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=max_calls),
    )


def test_early_refresh_failure_preserves_clusters_idempotently(
    tmp_path: Path,
) -> None:
    map_id = "literature-map-test"
    map_root = tmp_path / "03_literature_synthesis" / "maps" / map_id
    cluster = {
        "cluster_id": "cluster-a",
        "label": "Prior valid cluster",
        "revision_hash": "revision-a",
    }
    write_yaml(
        map_root / "cluster_registry.yml",
        {"clusters": [cluster], "pending_revisions": [], "ledger": []},
    )
    write_yaml(
        map_root / "cluster_syntheses.yml",
        {"syntheses": {"cluster-a": {"status": "reasoned"}}},
    )
    note_path = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster_note_stem(cluster)}.md"
    )
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# Prior valid cluster\n", encoding="utf-8")

    clusters, paths = _preserve_last_valid_clusters_on_refresh_failure(
        tmp_path,
        map_id,
        "literature_synthesis_stage_budget_reached:cluster_proposal",
    )
    first_bytes = {path: path.read_bytes() for path in paths}
    replay_clusters, replay_paths = _preserve_last_valid_clusters_on_refresh_failure(
        tmp_path,
        map_id,
        "literature_synthesis_stage_budget_reached:cluster_proposal",
    )

    assert clusters == replay_clusters
    assert clusters[0]["refresh_pending"] is True
    assert len(read_yaml(map_root / "cluster_registry.yml", {})["ledger"]) == 1
    assert "Cluster refresh pending" in note_path.read_text(encoding="utf-8")
    assert replay_paths == paths
    assert {path: path.read_bytes() for path in replay_paths} == first_bytes


def test_relationship_catalogue_projection_keeps_bounded_graph_navigation() -> None:
    projected = _compact_relationship_catalogue_entry(
        {
            "source_id": "source-zotero-abcd1234",
            "zotero_key": "ABCD1234",
            "title": "A title",
            "author": "An author",
            "year": "2026",
            "thesis": "A thesis",
            "method": "A method",
            "source_scope": "full_document",
            "evidence_coverage": "full_text",
            "facets": ["mediation"],
            "facets_by_type": {"outcome": ["peace duration"]},
            "collections": ["Mediation"],
            "note_link": "[[note]]",
            "profile_hash": "hash",
            "relationship_ids": ["relation-a"] * 100,
            "cluster_ids": ["cluster-a"],
        }
    )

    assert projected["zotero_key"] == "ABCD1234"
    assert projected["thesis"] == "A thesis"
    assert projected["relationship_ids"] == ["relation-a"] * 12
    assert projected["cluster_ids"] == ["cluster-a"]
    assert "profile_hash" not in projected
    assert "note_link" not in projected


def test_synthesis_call_ceiling_is_cumulative_across_resume(tmp_path: Path) -> None:
    reasoner = _CallReasoner()
    request = _request(tmp_path)

    first = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    first("cluster_proposal", "one", "propose_clusters", [], {"marker": "one"})
    resumed = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    resumed("cluster_proposal", "two", "propose_clusters", [], {"marker": "two"})
    exhausted = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)

    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_call_budget_reached",
    ):
        exhausted(
            "cluster_proposal",
            "three",
            "propose_clusters",
            [],
            {"marker": "three"},
        )

    assert reasoner.calls == 2
    assert exhausted.provider_calls == 0
    assert exhausted.cumulative_provider_calls == 2
    usage = read_yaml(exhausted.usage_path, {})
    assert usage["provider_call_count"] == 2
    assert usage["stage_call_counts"] == {"cluster_proposal": 2}


def test_unchanged_source_set_replay_is_byte_stable(tmp_path: Path) -> None:
    kwargs = {
        "run_id": "run",
        "scope": "workspace",
        "collection_key": None,
        "items": [{"key": "ITEM1"}],
        "terminal_rows": [
            {
                "inventory_index": 0,
                "zotero_item_key": "ITEM1",
                "source_id": "source-item1",
                "note_id": "note-item1",
                "note_path": "02_source_memory/notes/Item 1.md",
                "terminal_status": "validated_note",
                "fingerprint": "fingerprint",
            }
        ],
        "note_rows": [
            {
                "zotero_item_key": "ITEM1",
                "source_id": "source-item1",
                "note_id": "note-item1",
                "note_path": "02_source_memory/notes/Item 1.md",
            }
        ],
        "source_set_id": "source-set-workspace",
    }

    first = write_source_set(tmp_path, **kwargs)
    paths = [Path(first["path"]), Path(first["latest_path"])]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    replay = write_source_set(tmp_path, **kwargs)

    assert replay["updated_at"] == first["updated_at"]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths
    } == before


def test_oversized_cluster_synthesis_is_rejected_before_provider_call(
    tmp_path: Path,
) -> None:
    reasoner = _CallReasoner()
    reasoner.context_window_tokens = 1_000
    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        _request(tmp_path),
    )

    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_provider_context_budget_exceeded:cluster_synthesis",
    ):
        calls(
            "cluster_synthesis",
            "large",
            "propose_clusters",
            [],
            {"evidence": "x" * 9_000},
        )

    assert reasoner.calls == 0
    assert calls.cumulative_provider_calls == 0
    replay = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        _request(tmp_path),
    )
    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_terminal_failure:cluster_synthesis:large",
    ):
        replay(
            "cluster_synthesis",
            "large",
            "propose_clusters",
            [],
            {"evidence": "x" * 9_000},
        )
    assert reasoner.calls == 0
    assert replay.cumulative_provider_calls == 0


def test_interrupted_attempt_becomes_terminal_without_automatic_retry(
    tmp_path: Path,
) -> None:
    reasoner = _CallReasoner(failure=KeyboardInterrupt())
    request = _request(tmp_path, max_calls=3)

    with pytest.raises(KeyboardInterrupt):
        _CheckpointedReasonerCalls(
            tmp_path, "run", reasoner, request
        )("cluster_proposal", "one", "propose_clusters", [], {})
    terminal = _CheckpointedReasonerCalls(
        tmp_path, "run", reasoner, request
    )
    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_terminal_failure:cluster_proposal:one",
    ):
        terminal("cluster_proposal", "one", "propose_clusters", [], {})

    usage = read_yaml(terminal.usage_path, {})
    assert reasoner.calls == 1
    assert usage["provider_call_count"] == 1
    assert [row["attempt"] for row in usage["attempts"]] == [1]


def test_transport_failure_gets_one_checkpointed_resume_retry(
    tmp_path: Path,
) -> None:
    reasoner = _CallReasoner(failure=TimeoutError("provider request timed out"))
    request = _request(tmp_path, max_calls=3)

    with pytest.raises(TimeoutError):
        _CheckpointedReasonerCalls(
            tmp_path, "run", reasoner, request
        )("cluster_proposal", "one", "propose_clusters", [], {})
    checkpoint = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "run"
        / "literature"
        / "synthesis"
        / "cluster_proposal"
        / "one.yml",
        {},
    )
    assert checkpoint["terminal"] is False
    assert checkpoint["retry_on_resume"] is True

    reasoner.failure = None
    resumed = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    resumed("cluster_proposal", "one", "propose_clusters", [], {})
    assert reasoner.calls == 2
    assert resumed.cumulative_provider_calls == 2


def test_profile_and_fidelity_share_one_frozen_resume_ceiling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider_usage.yml"
    first = _ProfileProviderBudget(path, 2)
    fidelity = first.reserve("atomic_fidelity", "note-a", "hash-a")
    first.finish(fidelity, status="completed")
    profile = first.reserve("profile_source", "note-a", "hash-b")
    first.finish(profile, status="completed")

    resumed = _ProfileProviderBudget(path, 10)
    assert resumed.max_calls == 2
    assert resumed.cumulative_calls == 2
    with pytest.raises(RuntimeError, match="profile_call_budget_reached"):
        resumed.reserve("profile_source", "note-b", "hash-c")


def test_existing_run_ceiling_cannot_be_raised_on_resume(tmp_path: Path) -> None:
    reasoner = _CallReasoner()
    first = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        _request(tmp_path, max_calls=1),
    )
    first("cluster_proposal", "one", "propose_clusters", [], {})

    resumed = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        _request(tmp_path, max_calls=3),
    )

    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_call_budget_reached",
    ):
        resumed("cluster_proposal", "two", "propose_clusters", [], {})
    assert resumed.max_calls == 1
    assert reasoner.calls == 1


def test_acceptance_stage_reservations_protect_later_work(tmp_path: Path) -> None:
    reasoner = _CallReasoner()
    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        _request(tmp_path, max_calls=100),
    )
    for index in range(30):
        calls(
            "relationship_candidate_selection",
            f"source-{index}",
            "propose_clusters",
            [],
            {"marker": index},
        )

    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_stage_budget_reached:source_discovery",
    ):
        calls(
            "relationship_candidate_selection",
            "source-overflow",
            "propose_clusters",
            [],
            {},
        )
    assert calls.cumulative_provider_calls == 30


def test_terminal_checkpoint_is_zero_call_until_explicit_retry(
    tmp_path: Path,
) -> None:
    reasoner = _CallReasoner(failure=ValueError("unsupported provider configuration"))
    request = _request(tmp_path, max_calls=3)
    first = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    with pytest.raises(ValueError, match="unsupported provider configuration"):
        first("cluster_proposal", "one", "propose_clusters", [], {})

    replay = _CheckpointedReasonerCalls(tmp_path, "run", reasoner, request)
    with pytest.raises(
        LiteratureSynthesisPartialError,
        match="literature_synthesis_terminal_failure",
    ):
        replay("cluster_proposal", "one", "propose_clusters", [], {})
    assert replay.provider_calls == 0
    assert reasoner.calls == 1

    explicit = _CheckpointedReasonerCalls(
        tmp_path,
        "run",
        reasoner,
        request,
        retry_terminal_failures=True,
    )
    with pytest.raises(ValueError, match="unsupported provider configuration"):
        explicit("cluster_proposal", "one", "propose_clusters", [], {})
    assert reasoner.calls == 2


def test_cluster_checkpoint_tracks_relationship_inputs() -> None:
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
        "coverage_repair_source_ids": "same-repair",
        "coverage_focus_source_ids": "same-focus",
        "coverage_component_source_ids": "same-component",
        "coverage_audit_mode": "same-mode",
        "coverage_component_signature": "same-signature",
        "coverage_candidate_components": "same-candidates",
        "current_clusters": "same-clusters",
        "current_unclustered_sources": "same-unclustered",
        "prior_proposal_identities": "same-prior",
        "accepted_relationships": "old-relationships",
    }
    checkpoint = {
        "dependency_component_hashes": {**components, "context": "old-context"},
        "dependency_context_hashes": visible,
    }

    assert not _same_provider_inputs(
        checkpoint,
        {**components, "context": "new-context"},
        stage="cluster_proposal",
        current_context_hashes={
            **visible,
            "accepted_relationships": "new-relationships",
        },
    )


def test_relationship_ledger_is_a_stable_union(tmp_path: Path) -> None:
    first = {
        "parked": [{"reason": "first failure", "source_id": "A"}],
        "accepted": [{"relation_id": "relation-a"}],
        "no_relationship": [
            {
                "source_id": "A",
                "target_source_id": "B",
                "source_profile_hash": "hash-a",
                "target_profile_hash": "hash-b",
                "provider": "provider",
                "model": "model",
            }
        ],
    }
    path = _write_relationship_run_ledger(tmp_path, "run", first)
    _write_relationship_run_ledger(tmp_path, "run", {})
    replay = read_yaml(path, {})

    assert replay["ledger_schema_version"] == "2"
    assert replay["accepted_relation_ids"] == ["relation-a"]
    assert replay["no_relationship_count"] == 1
    assert replay["parked"] == first["parked"]
    assert {row["event_type"] for row in replay["events"]} == {
        "accepted",
        "no_relationship",
        "parked",
    }


class _RelationshipReasoner:
    name = "provider"

    def __init__(self, model: str) -> None:
        self.model = model

    def select_relationship_candidates(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> None:
        return None

class _RelationshipCalls:
    run_id = "relationship-run"

    def __init__(self) -> None:
        self.candidate_calls = 0
        self.candidate_entry_counts: list[int] = []
        self.candidate_profile_types: list[set[str]] = []

    def __call__(
        self,
        stage: str,
        _key: str,
        _method: str,
        _profiles: Sequence[Any],
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if stage == "relationship_candidate_selection":
            self.candidate_calls += 1
            self.candidate_profile_types.append(
                {type(row).__name__ for row in _profiles}
            )
            self.candidate_entry_counts.append(
                len(_context.get("catalogue", []) or [])
            )
            return {"candidates": []}
        return {"decisions": []}


def _profile(source_id: str) -> EvidenceProfile:
    return EvidenceProfile(
        source_id=source_id,
        note_id=f"note-{source_id}",
        context={"note_status": "analytical_atomic_note"},
        evidence_anchors=[
            EvidenceAnchor(
                evidence_anchor_id=f"anchor-{source_id}",
                source_id=source_id,
                claim=f"Claim for {source_id}",
                locator="p. 1",
                support_envelope={
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            )
        ],
    )


def _catalogue(
    tmp_path: Path,
    source_ids: Sequence[str],
    *,
    entry_padding: str = "",
) -> dict[str, str]:
    path = tmp_path / "catalogue.yml"
    write_yaml(
        path,
        {
            "sources": [
                {
                    "source_id": source_id,
                    "title": source_id,
                    "thesis": entry_padding,
                }
                for source_id in source_ids
            ],
            "shards": [
                {
                    "literature_id": "literature",
                    "shard_id": "shard",
                    "source_ids": list(source_ids),
                }
            ],
        },
    )
    return {
        "catalogue_path": str(path),
        "routing_revision_hash": "revision",
    }


def test_global_discovery_uses_the_complete_compact_catalogue_when_safe(
    tmp_path: Path,
) -> None:
    profiles = [_profile(f"S{index:03d}") for index in range(251)]
    calls = _RelationshipCalls()
    reasoner = _RelationshipReasoner("model")
    reasoner.context_window_tokens = 1_000_000
    result = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=_catalogue(
            tmp_path,
            [profile.source_id for profile in profiles],
            entry_padding="x" * 1_000,
        ),
        reasoner=reasoner,
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )

    assert len(result["selected_profile_hashes"]) == 251
    assert calls.candidate_calls == 1
    assert calls.candidate_entry_counts == [251]
    assert calls.candidate_profile_types == [{"EvidenceProfile"}]


def test_195_source_discovery_uses_one_global_call(
    tmp_path: Path,
) -> None:
    profiles = [_profile(f"S{index:03d}") for index in range(195)]
    source_ids = [profile.source_id for profile in profiles]
    catalogue_path = tmp_path / "catalogue-195.yml"
    write_yaml(
        catalogue_path,
        {
            "sources": [
                {"source_id": source_id, "title": source_id}
                for source_id in source_ids
            ],
            "shards": [
                {
                    "literature_id": "mediation",
                    "shard_id": "mediation",
                    "source_ids": source_ids[:75],
                    "routing_card": {"shard_id": "mediation"},
                },
                {
                    "literature_id": "relapse",
                    "shard_id": "relapse",
                    "source_ids": source_ids[75:],
                    "routing_card": {"shard_id": "relapse"},
                },
            ],
        },
    )
    calls = _RelationshipCalls()
    reasoner = _RelationshipReasoner("model")
    reasoner.context_window_tokens = 1_000_000
    result = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue={
            "catalogue_path": str(catalogue_path),
            "routing_revision_hash": "revision",
        },
        reasoner=reasoner,
        reasoner_calls=calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )

    assert len(result["selected_profile_hashes"]) == 195
    assert calls.candidate_calls == 1
    assert calls.candidate_entry_counts == [195]
    assert all(types <= {"EvidenceProfile"} for types in calls.candidate_profile_types)


def test_relationship_batch_events_include_affected_source_ids() -> None:
    first = _relationship_event_id(
        "parked",
        {"reason": "routing_failed", "source_ids": ["A", "B"]},
    )
    second = _relationship_event_id(
        "parked",
        {"reason": "routing_failed", "source_ids": ["C", "D"]},
    )

    assert first != second


def test_selection_identity_change_forces_reselection(tmp_path: Path) -> None:
    profiles = [_profile("A"), _profile("B")]
    catalogue = _catalogue(tmp_path, ["A", "B"])
    first_calls = _RelationshipCalls()
    first = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue,
        reasoner=_RelationshipReasoner("model-a"),
        reasoner_calls=first_calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )

    second_calls = _RelationshipCalls()
    second = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue=catalogue,
        reasoner=_RelationshipReasoner("model-b"),
        reasoner_calls=second_calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )

    assert first_calls.candidate_calls == 1
    assert second_calls.candidate_calls == 1
    assert first["selection_identity"] != second["selection_identity"]


def test_catalogue_content_change_forces_one_global_reselection(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    catalogue_path = tmp_path / "catalogue.yml"
    write_yaml(
        catalogue_path,
        {
            "sources": [
                {"source_id": "A", "title": "A"},
                {"source_id": "B", "title": "B"},
            ],
            "shards": [
                {
                    "literature_id": "left",
                    "shard_id": "left",
                    "source_ids": ["A"],
                },
                {
                    "literature_id": "right",
                    "shard_id": "right",
                    "source_ids": ["B"],
                },
            ],
        },
    )
    reasoner = _RelationshipReasoner("model")
    first_calls = _RelationshipCalls()
    first = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue={
            "catalogue_path": str(catalogue_path),
            "routing_revision_hash": "revision-one",
        },
        reasoner=reasoner,
        reasoner_calls=first_calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )
    _commit_relationship_selection_state(
        tmp_path,
        first,
        catalogue_revision=first["reconciled_catalogue_revision"],
    )
    payload = read_yaml(catalogue_path, {})
    payload["sources"][0]["title"] = "Changed A"
    write_yaml(catalogue_path, payload)

    second_calls = _RelationshipCalls()
    second = _run_relationship_reasoning(
        tmp_path,
        profiles=profiles,
        source_set={"source_set_type": "collection"},
        catalogue={
            "catalogue_path": str(catalogue_path),
            "routing_revision_hash": "revision-two",
        },
        reasoner=reasoner,
        reasoner_calls=second_calls,  # type: ignore[arg-type]
        request=_request(tmp_path),
    )

    assert second_calls.candidate_calls == 1
    assert set(second["selected_profile_hashes"]) == {"A", "B"}
    assert second["reconciled_catalogue_revision"] != first[
        "reconciled_catalogue_revision"
    ]
