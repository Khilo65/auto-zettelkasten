from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten import pipeline as pipeline_module
from auto_zettelkasten.api import build_map, run_map
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import build_source_catalogue
from auto_zettelkasten.literature import stable_literature_map_id
from auto_zettelkasten.migration import migrate_workspace
from auto_zettelkasten.models import (
    EvidenceProfile,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
    NavigationPolicy,
)
from auto_zettelkasten.notes import read_note
from auto_zettelkasten.pipeline import (
    _cluster_preservation_snapshot,
    _project_atomic_graph,
    _write_cross_boundary_ledger,
    _workspace_graph_inputs,
)
from auto_zettelkasten.profiles import deterministic_profile, profile_to_dict
from auto_zettelkasten.readers import ProviderTimeout
from auto_zettelkasten.relationships import persist_relationship_registry
from auto_zettelkasten.workspace import initialize

from conftest import FakeReader, FakeZotero


class _RelationshipReasoner:
    name = "relationship-first-test"
    model = "relationship-first-v1"
    is_cloud = False
    ordinary_relationship_decision_contract = "relationship-decision-v11"

    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.profile_calls = 0
        self.candidate_calls = 0
        self.bridge_calls = 0
        self.adjudication_calls = 0
        self.cluster_calls = 0

    def profile_source(
        self,
        note: Mapping[str, Any],
        *,
        question: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        del question, context
        self.profile_calls += 1
        return profile_to_dict(deterministic_profile(str(note["committed_note"])))

    def select_relationship_candidates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        del request
        self.candidate_calls += 1
        if self.timeout:
            raise TimeoutError("synthetic relationship timeout")
        catalogue = (context or {}).get("catalogue") or [profile_to_dict(p) for p in profiles]
        left, right = catalogue[:2]
        return {"candidates": [{
            "left_source_id": left["source_id"],
            "right_source_id": right["source_id"],
            "decision": "relationship",
            "bridge_job_id": str(((context or {}).get("bridge_jobs") or [{}])[0].get("bridge_job_id") or ""),
            "relation_type": "complements",
            "actor_source_id": None,
            "reference_source_id": None,
            "reason": "The sources provide complementary evidence for the bounded proposition.",
        }]}

    def adjudicate_relationships(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.adjudication_calls += 1
        raise AssertionError("Ordinary relationships must not make a second judge call")

    def select_relationship_bridge_shards(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        del profiles, request, context
        self.bridge_calls += 1
        return {"shard_pairs": []}

    def propose_clusters(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.cluster_calls += 1
        return {"clusters": []}

    def map_debates(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.cluster_calls += 1
        return {"assessments": []}

    def detect_gaps(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.cluster_calls += 1
        return {"gaps": []}


class _TerminalRelationshipReasoner(_RelationshipReasoner):
    def select_relationship_candidates(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.candidate_calls += 1
        raise ValueError("synthetic terminal relationship failure")


def _downgrade_engine_metadata(workspace: Path) -> None:
    for path in (
        workspace / "auto-zettelkasten.yml",
        workspace / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload["engine_version"] = "0.29.10"
        write_yaml(path, payload)


def _seed_workspace(
    workspace: Path, sample_items: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = deepcopy(sample_items)
    for item in items:
        item["data"].pop("relations", None)
    run_map(
        MapRequest(
            workspace,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False),
        ),
        client=FakeZotero(items),
        reader=FakeReader(),
        run_id="relationship-first-sources",
    )
    note_rows, profiles = _workspace_graph_inputs(workspace, [])
    assert len(note_rows) == len(profiles) == 2
    source_ids = [str(row["source_id"]) for row in note_rows]
    note_ids = [str(row["note_id"]) for row in note_rows]
    cluster = {
        "cluster_id": "cluster-preserved",
        "display_label": "Preserved cluster",
        "source_ids": source_ids,
        "core_source_ids": source_ids,
        "note_ids": note_ids,
        "source_roles": [
            {"source_id": source_id, "role": "core"} for source_id in source_ids
        ],
    }
    gap = {
        "gap_id": "gap-preserved",
        "display_label": "Preserved gap",
        "status": "collection_gap_lead",
        "supporting_evidence": [
            {"source_id": source_ids[0], "evidence_anchor_id": "preserved-anchor"}
        ],
    }
    build_source_catalogue(workspace, profiles, note_rows, [cluster])
    existing = (
        read_yaml(workspace / "02_source_memory" / "indexes" / "typed_links.yml", {})
        or {}
    )
    membership_rows = [
        {
            "relation_id": "cluster-member-preserved",
            "source_kind": "source",
            "source_id": source_ids[0],
            "source_note_id": note_ids[0],
            "target_kind": "cluster",
            "target_cluster_id": "cluster-preserved",
            "relation_type": "cluster_member",
            "cluster_role": "core",
            "provenance": "preserved_fixture",
            "active": True,
        },
        {
            "relation_id": "cluster-has-member-preserved",
            "source_kind": "cluster",
            "source_id": "cluster-preserved",
            "target_kind": "source",
            "target_source_id": source_ids[0],
            "target_note_id": note_ids[0],
            "relation_type": "has_member",
            "cluster_role": "core",
            "provenance": "preserved_fixture",
            "active": True,
        },
    ]
    typed = persist_relationship_registry(
        workspace,
        structural_relations=[
            *(existing.get("relations", []) or []),
            *membership_rows,
        ],
    )
    _project_atomic_graph(
        workspace,
        note_rows=note_rows,
        profiles=profiles,
        relations=typed.get("links", []) or [],
        navigation={"typed_relations": [], "assignments": []},
        navigation_policy=NavigationPolicy(),
        clusters=[cluster],
        gaps=[gap],
        cluster_scope_note_ids=note_ids,
    )
    synthesis = workspace / "03_literature_synthesis"
    write_yaml(synthesis / "cluster_registry.yml", {"clusters": [cluster]})
    write_yaml(synthesis / "gap_registry.yml", {"gap_candidates": [gap]})
    write_yaml(
        workspace / "02_source_memory" / "indexes" / "gap_candidates.yml",
        {"gap_candidates": [gap]},
    )
    (synthesis / "clusters").mkdir(parents=True, exist_ok=True)
    (synthesis / "gaps" / "candidates").mkdir(parents=True, exist_ok=True)
    (synthesis / "clusters" / "Cluster - preserved.md").write_text(
        "# Preserved cluster\n", encoding="utf-8"
    )
    (synthesis / "gaps" / "candidates" / "Gap - preserved.md").write_text(
        "# Preserved gap\n", encoding="utf-8"
    )
    _downgrade_engine_metadata(workspace)
    migration = migrate_workspace(workspace)
    assert migration["v016"]["status"] == "migrated"
    snapshot = _cluster_preservation_snapshot(workspace)
    assert snapshot["protected_relations"]
    assert snapshot["preserved_clusters"]
    assert any(row["gaps"] for row in snapshot["note_projections"].values())
    return snapshot, note_rows


def _workspace_bytes(workspace: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(workspace): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }


def test_relationship_semantic_noop_keeps_cross_boundary_ledger_exact(
    tmp_path: Path,
) -> None:
    path = _write_cross_boundary_ledger(
        tmp_path,
        family_plan={"plan_path": "plan.yml"},
        relationship_result={"accepted": [{"relation_id": "relationship-preserved"}]},
    )
    before = path.read_bytes()

    replay = _write_cross_boundary_ledger(
        tmp_path,
        family_plan=None,
        relationship_result={"semantic_noop": True},
    )

    assert replay == path
    assert path.read_bytes() == before


def test_clusters_off_preserves_history_runs_relationships_and_replays_exactly(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    protected, note_rows = _seed_workspace(tmp_path, sample_items)
    projection_path = (
        tmp_path / "03_literature_synthesis" / "typed_source_relations.yml"
    )
    write_yaml(projection_path, {"relations": [{"source_id": "stale"}]})
    reasoner = _RelationshipReasoner()
    policy = LiteratureMappingPolicy(cluster_generation_enabled=False)

    first = build_map(
        tmp_path,
        run_id="relationship-first-build",
        provider="ollama",
        model="fake-1",
        literature_policy=policy,
        reasoner=reasoner,
    )

    assert first.status == "built"
    assert first.metadata["migration"]["status"] == "completed"
    assert first.metadata["cluster_map"]["status"] == "clusters_preserved_not_updated"
    assert first.metadata["cluster_map"]["clusters"] == []
    assert first.metadata["cluster_map"]["preserved_clusters"]
    assert reasoner.candidate_calls > 0
    assert reasoner.bridge_calls > 0
    assert reasoner.adjudication_calls == 0
    assert reasoner.cluster_calls == 0
    assert _cluster_preservation_snapshot(tmp_path) == protected
    registry = (
        read_yaml(tmp_path / "02_source_memory" / "indexes" / "typed_links.yml", {})
        or {}
    )
    assert any(
        row.get("relation_type") == "complements" and row.get("active", True)
        for row in registry.get("relations", []) or []
    )
    source_relations = [
        row
        for row in registry.get("links", []) or []
        if row.get("source_kind", "source") == "source"
        and row.get("target_kind", "source") == "source"
        and row.get("active", True)
    ]
    relation_id = next(
        row["relation_id"]
        for row in source_relations
        if row.get("relation_type") == "complements"
    )
    catalogue = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "source_catalogue.yml"
    )
    assert all(
        relation_id in row.get("relationship_ids", [])
        for row in catalogue.get("sources", []) or []
    )
    progress = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "relationship-first-build"
        / "progress.yml"
    )
    assert progress["literature"]["typed_relation_count"] == len(source_relations)
    projection = read_yaml(projection_path)
    assert projection["relations"] == [
        row
        for row in registry.get("links", []) or []
        if row.get("source_kind", "source") == "source"
        and row.get("target_kind", "source") == "source"
        and row.get("active", True)
    ]
    receipt = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "relationship-first-build"
        / "semantic_build_receipt.yml"
    )
    assert receipt["summary"]["relationship_count"] == len(source_relations)
    assert all(
        any(
            row.get("relation_type") == "complements"
            for row in read_note(tmp_path / str(note["note_path"]))["frontmatter"].get(
                "related_notes", []
            )
        )
        for note in note_rows
    )

    calls = (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.bridge_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    )
    bytes_before = _workspace_bytes(tmp_path)
    projection_mtime = projection_path.stat().st_mtime_ns
    replay = build_map(
        tmp_path,
        run_id="relationship-first-build",
        provider="ollama",
        model="fake-1",
        literature_policy=policy,
        reasoner=reasoner,
        resume=True,
    )

    assert replay.status == "built"
    assert (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.bridge_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    ) == calls
    assert _workspace_bytes(tmp_path) == bytes_before
    assert projection_path.stat().st_mtime_ns == projection_mtime
    assert _cluster_preservation_snapshot(tmp_path) == protected


def test_clusters_on_refreshes_post_adjudication_catalogue_and_progress(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    items = deepcopy(sample_items)
    for item in items:
        item["data"]["collections"] = ["COLL1"]
    _seed_workspace(tmp_path, items)
    reasoner = _RelationshipReasoner()

    result = build_map(
        tmp_path,
        run_id="relationship-and-cluster-build",
        provider="ollama",
        model="fake-1",
        literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=True),
        reasoner=reasoner,
    )

    assert result.status == "built"
    registry = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    )
    source_relations = [
        row
        for row in registry.get("links", []) or []
        if row.get("source_kind", "source") == "source"
        and row.get("target_kind", "source") == "source"
        and row.get("active", True)
    ]
    relation_id = next(
        row["relation_id"]
        for row in source_relations
        if row.get("relation_type") == "complements"
    )
    catalogue = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "source_catalogue.yml"
    )
    assert all(
        relation_id in row.get("relationship_ids", [])
        for row in catalogue.get("sources", []) or []
    )
    assert catalogue["clusters"] == []
    assert all(not row.get("cluster_ids") for row in catalogue.get("sources", []) or [])
    assert all(
        not row.get("routing_card", {}).get("active_cluster_ids")
        for row in catalogue.get("collections", []) or []
    )
    master_index = (
        tmp_path / "02_source_memory" / "indexes" / "INDEX.md"
    ).read_text(encoding="utf-8")
    collection_index = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "collections"
        / "COLL1"
        / "INDEX.md"
    ).read_text(encoding="utf-8")
    assert f"Catalogue revision: `{catalogue['revision_hash']}`" in master_index
    assert "cluster-preserved" not in master_index
    assert "cluster-preserved" not in collection_index
    progress = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "relationship-and-cluster-build"
        / "progress.yml"
    )
    assert progress["literature"]["typed_relation_count"] == len(source_relations)


def test_catalogue_projection_tamper_invalidates_receipt_replay(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    items = deepcopy(sample_items)
    for item in items:
        item["data"]["collections"] = ["COLL1"]
    _seed_workspace(tmp_path, items)
    reasoner = _RelationshipReasoner()
    policy = LiteratureMappingPolicy(cluster_generation_enabled=False)
    kwargs = {
        "run_id": "catalogue-artifact-replay",
        "provider": "ollama",
        "model": "fake-1",
        "literature_policy": policy,
        "reasoner": reasoner,
    }

    first = build_map(tmp_path, **kwargs)
    generated_catalogue_paths = {
        path
        for directory in (
            tmp_path / "02_source_memory" / "indexes" / "by_topic",
            tmp_path / "02_source_memory" / "indexes" / "collections",
        )
        for path in directory.rglob("*")
        if path.is_file()
    }
    artifact_paths = {
        tmp_path / str(row["path"])
        for row in first.artifacts
        if row.get("path")
    }
    assert generated_catalogue_paths <= artifact_paths

    target = next(
        path
        for path in generated_catalogue_paths
        if path.name == "relationships-001.md"
    )
    target.write_text("tampered\n", encoding="utf-8")
    calls = (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    )

    replay = build_map(tmp_path, resume=True, **kwargs)

    assert replay.status == "built"
    assert target.read_text(encoding="utf-8") != "tampered\n"
    assert (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    ) == calls


def test_clusters_off_receipt_replay_rejects_changed_protected_state(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    _seed_workspace(tmp_path, sample_items)
    reasoner = _RelationshipReasoner()
    policy = LiteratureMappingPolicy(cluster_generation_enabled=False)
    build_map(
        tmp_path,
        run_id="relationship-first-protected-replay",
        provider="ollama",
        model="fake-1",
        literature_policy=policy,
        reasoner=reasoner,
    )
    calls = (reasoner.candidate_calls, reasoner.adjudication_calls)
    gap_path = (
        tmp_path
        / "03_literature_synthesis"
        / "gaps"
        / "candidates"
        / "Gap - preserved.md"
    )
    gap_path.write_text(
        gap_path.read_text(encoding="utf-8") + "changed\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match="protected_cluster_state_changed:receipt_replay"
    ):
        build_map(
            tmp_path,
            run_id="relationship-first-protected-replay",
            provider="ollama",
            model="fake-1",
            literature_policy=policy,
            reasoner=reasoner,
            resume=True,
        )

    assert (reasoner.candidate_calls, reasoner.adjudication_calls) == calls


def test_fresh_map_family_timeout_preserves_absent_cluster_outputs(
    tmp_path: Path,
    sample_items: list[dict[str, Any]],
    monkeypatch,
) -> None:
    reasoner = _RelationshipReasoner()

    def timed_out(*args: Any, **kwargs: Any) -> None:
        raise ProviderTimeout("synthetic family planning timeout")

    monkeypatch.setattr(pipeline_module, "_plan_literature_families", timed_out)
    result = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=True),
        ),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        literature_reasoner=reasoner,
        run_id="fresh-family-timeout",
    )

    assert result.status == "partial"
    assert result.literature_packet["retry_on_resume"] is True
    assert result.cluster_map["preservation"]["status"] == "verified"
    assert reasoner.candidate_calls == reasoner.cluster_calls == 0
    index_root = tmp_path / "02_source_memory" / "indexes"
    assert not (index_root / "cluster_catalogue.yml").exists()
    assert not (index_root / "CLUSTERS.md").exists()


def test_clusters_off_partial_relationship_stage_preserves_protected_state(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    protected, _ = _seed_workspace(tmp_path, sample_items)
    reasoner = _RelationshipReasoner(timeout=True)

    result = build_map(
        tmp_path,
        run_id="relationship-first-partial",
        provider="ollama",
        model="fake-1",
        literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False),
        reasoner=reasoner,
    )

    assert result.status == "partial"
    assert result.metadata["cluster_map"]["status"] == "clusters_preserved_not_updated"
    assert result.metadata["literature_packet"]["status"] == "partial"
    assert result.metadata["literature_packet"]["retry_on_resume"] is True
    assert reasoner.candidate_calls > 0
    assert reasoner.adjudication_calls == 0
    assert reasoner.cluster_calls == 0
    assert _cluster_preservation_snapshot(tmp_path) == protected


@pytest.mark.parametrize("clusters_enabled", [False, True])
def test_terminal_relationship_failure_stops_before_cluster_synthesis(
    tmp_path: Path,
    sample_items: list[dict[str, Any]],
    clusters_enabled: bool,
) -> None:
    _seed_workspace(tmp_path, sample_items)
    reasoner = _TerminalRelationshipReasoner()
    suffix = "on" if clusters_enabled else "off"
    run_id = f"relationship-first-terminal-{suffix}"
    map_id = stable_literature_map_id(
        {"source_set_id": "source-set-auto-zettelkasten-workspace"}
    )
    map_root = tmp_path / "03_literature_synthesis" / "maps" / map_id
    legacy_registry = read_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml"
    )
    registry_path = map_root / "cluster_registry.yml"
    syntheses_path = map_root / "cluster_syntheses.yml"
    write_yaml(registry_path, legacy_registry)
    write_yaml(
        syntheses_path,
        {
            "syntheses": {
                "cluster-preserved": {
                    "cluster_id": "cluster-preserved",
                    "refresh_pending": False,
                }
            }
        },
    )
    protected = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (registry_path, syntheses_path)
    }
    protected_cluster_state = _cluster_preservation_snapshot(tmp_path)
    policy = LiteratureMappingPolicy(
        cluster_generation_enabled=clusters_enabled
    )

    result = build_map(
        tmp_path,
        run_id=run_id,
        provider="ollama",
        model="fake-1",
        literature_policy=policy,
        reasoner=reasoner,
    )

    assert result.status == "partial"
    assert result.metadata["literature_packet"]["reason"].endswith(
        "terminal_incomplete_relationships"
    )
    assert reasoner.candidate_calls > 0
    assert reasoner.adjudication_calls == 0
    assert reasoner.cluster_calls == 0
    assert all(
        (path.read_bytes(), path.stat().st_mtime_ns) == before
        for path, before in protected.items()
    )
    assert _cluster_preservation_snapshot(tmp_path) == protected_cluster_state
    assert not list(
        (
            tmp_path
            / "11_state"
            / "runs"
            / run_id
            / "literature"
            / "synthesis"
            / "cluster_synthesis"
        ).glob("*.yml")
    )

    candidate_calls = reasoner.candidate_calls
    replay_run_id = f"{run_id}-fresh"
    replay = build_map(
        tmp_path,
        run_id=replay_run_id,
        provider="ollama",
        model="fake-1",
        literature_policy=policy,
        reasoner=reasoner,
    )

    assert replay.status == "partial"
    assert reasoner.candidate_calls == candidate_calls
    assert reasoner.cluster_calls == 0
    assert all(
        (path.read_bytes(), path.stat().st_mtime_ns) == before
        for path, before in protected.items()
    )
    assert _cluster_preservation_snapshot(tmp_path) == protected_cluster_state
    assert not list(
        (
            tmp_path
            / "11_state"
            / "runs"
            / replay_run_id
            / "literature"
            / "synthesis"
            / "cluster_synthesis"
        ).glob("*.yml")
    )


def test_clusters_off_profile_failure_stays_partial(
    tmp_path: Path,
    sample_items: list[dict[str, Any]],
    monkeypatch,
) -> None:
    _seed_workspace(tmp_path, sample_items)
    original = pipeline_module._build_profiles_for_map

    def partial_profiles(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        return {**result, "failure_count": 1}

    monkeypatch.setattr(
        pipeline_module, "_build_profiles_for_map", partial_profiles
    )

    result = build_map(
        tmp_path,
        run_id="relationship-first-profile-partial",
        provider="ollama",
        model="fake-1",
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=False
        ),
        reasoner=_RelationshipReasoner(),
    )

    assert result.status == "partial"
    assert result.metadata["literature_packet"]["status"] == "partial"
    assert (
        result.metadata["literature_map"]["partial_reason"]
        == "literature_profiling_partial:1_profile_failure"
    )


def test_clusters_off_without_relationship_capability_stays_partial(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    _seed_workspace(tmp_path, sample_items)

    result = build_map(
        tmp_path,
        run_id="relationship-first-capability-partial",
        provider="ollama",
        model="fake-1",
        allow_cloud=False,
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=False
        ),
        reasoner=None,
    )

    assert result.status == "partial"
    assert result.metadata["literature_packet"]["status"] == "partial"
    ledger = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "relationship-first-capability-partial"
        / "literature"
        / "relationships"
        / "parked.yml"
    )
    assert ledger["parked"] == [
        {
            "reason": "relationship_reasoner_capability_unavailable",
            "eligible_profile_count": 2,
            "retry_on_resume": False,
        }
    ]


def test_missing_legacy_cluster_toggle_keeps_no_reasoner_build_compatible(
    tmp_path: Path, sample_items: list[dict[str, Any]]
) -> None:
    run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(
                synthesis_enabled=False,
                cluster_generation_enabled=None,
            ),
        ),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="legacy-no-synthesis",
    )

    result = build_map(
        tmp_path,
        run_id="legacy-cluster-build",
        provider="ollama",
        model="fake-1",
        allow_cloud=False,
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=None
        ),
        reasoner=None,
    )

    assert result.status == "built"


def test_run_map_migrates_once_and_passes_the_result_to_rebuild(
    tmp_path: Path,
    sample_items: list[dict[str, Any]],
    monkeypatch,
) -> None:
    original_migrate = pipeline_module.migrate_workspace
    original_rebuild = pipeline_module.rebuild_map
    migrations: list[Mapping[str, Any]] = []
    rebuild_migrations: list[Mapping[str, Any] | None] = []

    def counted_migrate(workspace: Path) -> Mapping[str, Any]:
        result = original_migrate(workspace)
        migrations.append(result)
        return result

    def observed_rebuild(*args: Any, **kwargs: Any) -> dict[str, Any]:
        rebuild_migrations.append(kwargs.get("migration"))
        return original_rebuild(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "migrate_workspace", counted_migrate)
    monkeypatch.setattr(pipeline_module, "rebuild_map", observed_rebuild)

    initialize(tmp_path)
    _downgrade_engine_metadata(tmp_path)

    report = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(
                synthesis_enabled=False,
                cluster_generation_enabled=False,
            ),
        ),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="single-migration",
    )

    assert report.status == "completed"
    assert len(migrations) == 1
    assert migrations[0]["v016"]["status"] == "migrated"
    assert rebuild_migrations == migrations
