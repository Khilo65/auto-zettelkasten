from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import auto_zettelkasten.expansion as expansion_module
from auto_zettelkasten.api import (
    decide_expansion,
    export_expansion_candidates,
    export_to_obsidian,
    list_expansion_candidates,
    map_accepted_candidates,
    run_expansion,
    run_map,
)
from auto_zettelkasten.expansion import _bounded_candidates, _round_robin_frontier, render_expansion_projection
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.identity import identify_work
from auto_zettelkasten.models import (
    ExpansionCandidate,
    ExpansionDecision,
    ExpansionRequest,
    MapRequest,
    RunReport,
)

from conftest import FakeReader, FakeZotero


def _item(
    key: str,
    title: str,
    doi: str,
    *,
    relations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "date": "2024",
            "DOI": doi,
            "creators": [{"creatorType": "author", "firstName": key, "lastName": "Author"}],
            "relations": dict(relations or {}),
            "tags": [],
        },
    }


def _map(workspace: Path, items: Sequence[Mapping[str, Any]], run_id: str = "seed-map") -> RunReport:
    return run_map(
        MapRequest(workspace, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero([dict(row) for row in items]),
        reader=FakeReader(),
        run_id=run_id,
    )


def _write_candidates(workspace: Path, candidates: Sequence[ExpansionCandidate]) -> None:
    write_yaml(
        workspace / "03_literature_synthesis" / "expansion" / "candidates.yml",
        {
            "engine_version": "0.2.0",
            "artifact_schema_version": "1.1",
            "candidates": [row.to_dict() for row in candidates],
        },
    )
    decisions = []
    for row in candidates:
        if row.state not in {"accepted", "parked", "rejected"}:
            continue
        decisions.append(
            {
                "decision_id": f"expansion-decision-{row.suggestion_id}",
                "suggestion_id": row.suggestion_id,
                "work_id": row.work_id,
                "target_scope": row.target_scope,
                "target_id": row.target_id,
                "previous_state": "proposed",
                "decision": row.state,
                "reason": "synthetic historical decision",
                "actor": "test",
                "expected_version": 0,
                "decision_version": 1,
                "decided_at": "2026-01-01T00:00:00Z",
            }
        )
    write_yaml(
        workspace / "03_literature_synthesis" / "expansion" / "decisions.yml",
        {
            "engine_version": "0.2.0",
            "artifact_schema_version": "1.1",
            "decisions": decisions,
        },
    )


class _CrossTargetIdentityProvider:
    name = "semantic-scholar"
    is_network = True

    def resolve_work(self, work):
        suffix = str(work.get("doi") or "").rsplit("/", 1)[-1].upper()
        return {**dict(work), "provider_ids": {"semantic_scholar": f"SEED-{suffix}"}}

    def citation_neighbors(self, work, *, limit):
        seed_id = str((work.get("provider_ids") or {}).get("semantic_scholar") or "")
        common = {
            "title": "One Canonical Publication",
            "year": "2023",
            "authors": ["Same Author"],
            "relation_type": "cited_by",
        }
        if seed_id.endswith("A"):
            return [{**common, "doi": "10.9000/canonical"}]
        return [{**common, "provider_ids": {"semantic_scholar": "S2-CANONICAL"}}]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def test_work_identity_is_global_but_decisions_remain_target_specific(tmp_path: Path) -> None:
    seeds = [_item("A", "Seed A", "10.9000/a"), _item("B", "Seed B", "10.9000/b")]
    mapped = _map(tmp_path, seeds)
    target_ids = tuple(sorted(str(row["source_id"]) for row in mapped.items))
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=target_ids,
            provider="semantic-scholar",
            allow_network=True,
            run_id="global-identity",
        ),
        client=FakeZotero([]),
        graph_provider=_CrossTargetIdentityProvider(),
    )
    assert report.candidate_count == 2
    assert len({row["work_id"] for row in report.candidates}) == 1
    assert report.candidates[0]["work_id"].startswith("work-doi-")
    assert len({row["suggestion_id"] for row in report.candidates}) == 2

    first, second = sorted(report.candidates, key=lambda row: row["target_id"])
    decide_expansion(
        tmp_path,
        ExpansionDecision(first["suggestion_id"], 0, "accepted", "Relevant for this target"),
    )
    decide_expansion(
        tmp_path,
        ExpansionDecision(second["suggestion_id"], 0, "rejected", "Not relevant for this target"),
    )
    assert {row.state for row in list_expansion_candidates(tmp_path)} == {"accepted", "rejected"}


class _ConflictingIdentityProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self, identity_class: str) -> None:
        self.identity_class = identity_class

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work, *, limit):
        shared = {
            "title": "Ambiguous Strong Identity",
            "year": "2022",
            "authors": ["Conflict Author"],
            "relation_type": "cited_by",
        }
        if self.identity_class == "semantic_scholar":
            values = [
                {"provider_ids": {"semantic_scholar": "S2-A"}},
                {"provider_ids": {"semantic_scholar": "S2-B"}},
            ]
        elif self.identity_class == "url":
            values = [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}]
        else:
            values = [{"isbn": "9780306406157"}, {"isbn": "9790306406156"}]
        return [{**shared, **value} for value in values]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


@pytest.mark.parametrize("identity_class", ["semantic_scholar", "url", "isbn"])
def test_conflicting_strong_identifiers_never_auto_merge(
    tmp_path: Path,
    identity_class: str,
) -> None:
    workspace = tmp_path / identity_class
    _map(workspace, [_item("A", "Seed", f"10.9010/{identity_class}")])
    report = run_expansion(
        ExpansionRequest(
            workspace,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            run_id=f"conflict-{identity_class}",
        ),
        client=FakeZotero([]),
        graph_provider=_ConflictingIdentityProvider(identity_class),
    )
    assert report.candidate_count == 2
    assert len({row["work_id"] for row in report.candidates}) == 2
    assert {row["actionability"] for row in report.candidates} == {"resolve_identity"}


def test_portable_cluster_gap_source_and_source_set_associations(tmp_path: Path) -> None:
    a = _item("A", "Seed A", "10.9020/a", relations={"dc:references": "http://zotero/items/X"})
    b = _item("B", "Seed B", "10.9020/b", relations={"dc:references": "http://zotero/items/Y"})
    x = _item("X", "Candidate X", "10.9020/x")
    y = _item("Y", "Candidate Y", "10.9020/y")
    mapped = _map(tmp_path, [a, b])
    by_key = {str(row["zotero_item_key"]): row for row in mapped.items}
    write_yaml(
        tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml",
        {
            "clusters": [
                {"cluster_id": "cluster-a", "source_ids": [by_key["A"]["source_id"]]},
                {
                    "cluster_id": "cluster-b",
                    "representative_sources": [
                        {
                            "source_id": by_key["B"]["source_id"],
                            "note_id": by_key["B"]["note_id"],
                        }
                    ],
                },
            ]
        },
    )
    write_yaml(
        tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml",
        {"gap_candidates": [{"gap_id": "gap-a", "related_clusters": ["cluster-a"]}]},
    )
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "gap_candidates.yml",
        {
            "gap_candidates": [
                {
                    "gap_id": "gap-host",
                    "source_ids": [by_key["A"]["source_id"]],
                    "supporting_clusters": ["cluster-b"],
                }
            ]
        },
    )
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "source_sets" / "set-one.yml",
        {
            "source_set_id": "set-one",
            "source_ids": [by_key["A"]["source_id"]],
            "cluster_ids": ["cluster-a"],
            "gap_ids": ["gap-a"],
        },
    )

    cluster = run_expansion(
        ExpansionRequest(tmp_path, scope="cluster", target_ids=("cluster-a",), run_id="portable-cluster"),
        client=FakeZotero([a, b, x, y]),
    )
    assert cluster.seed_count == 1
    assert cluster.candidates[0]["related_cluster_ids"] == ["cluster-a"]
    assert "gap-a" in cluster.candidates[0]["related_gap_ids"]

    source = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(str(by_key["A"]["source_id"]),),
            run_id="portable-source",
        ),
        client=FakeZotero([a, b, x, y]),
    )
    assert source.candidates[0]["related_cluster_ids"] == ["cluster-a"]
    assert "gap-a" in source.candidates[0]["related_gap_ids"]

    source_set = run_expansion(
        ExpansionRequest(tmp_path, scope="source_set", target_ids=("set-one",), run_id="portable-set"),
        client=FakeZotero([a, b, x, y]),
    )
    assert source_set.candidates[0]["related_cluster_ids"] == ["cluster-a"]
    assert "gap-a" in source_set.candidates[0]["related_gap_ids"]

    gap = run_expansion(
        ExpansionRequest(tmp_path, scope="gap", target_ids=("gap-host",), run_id="portable-gap"),
        client=FakeZotero([a, b, x, y]),
    )
    assert gap.seed_count == 2
    assert {tuple(row["related_cluster_ids"]) for row in gap.candidates} == {
        ("cluster-a",),
        ("cluster-b",),
    }
    assert all("gap-host" in row["related_gap_ids"] for row in gap.candidates)


def _ranked_candidate(
    index: int,
    cluster_id: str,
    *,
    scope: str,
    target_id: str,
) -> ExpansionCandidate:
    suffix = f"{cluster_id[-1]}{index:02d}"
    return ExpansionCandidate(
        work_id=f"work-doi-{suffix}",
        suggestion_id=f"suggestion-{scope}-{target_id}-{suffix}",
        title=f"{cluster_id} candidate {index}",
        target_scope=scope,
        target_id=target_id,
        target_ids=[target_id],
        primary_relation="cites",
        observations=[{"originating_cluster_ids": [cluster_id], "relation_type": "cites"}],
        related_cluster_ids=[cluster_id],
        score=max(0.1, 0.99 - index / 1000),
    )


@pytest.mark.parametrize("scope", ["gap", "source_set"])
def test_cluster_balancing_and_per_cluster_cap_apply_across_scopes(
    tmp_path: Path,
    scope: str,
) -> None:
    target = "gap-both" if scope == "gap" else "set-both"
    candidates = [
        *[_ranked_candidate(index, "cluster-a", scope=scope, target_id=target) for index in range(30)],
        *[_ranked_candidate(index, "cluster-b", scope=scope, target_id=target) for index in range(30)],
    ]
    balanced, truncated = _bounded_candidates(
        candidates,
        ExpansionRequest(tmp_path, scope=scope, target_ids=(target,), budget=10),  # type: ignore[arg-type]
    )
    assert truncated is True
    assert [sum(cluster in row.related_cluster_ids for row in balanced) for cluster in ("cluster-a", "cluster-b")] == [5, 5]

    capped, truncated = _bounded_candidates(
        candidates,
        ExpansionRequest(tmp_path, scope=scope, target_ids=(target,), budget=100),  # type: ignore[arg-type]
    )
    assert truncated is True
    assert len(capped) == 50
    assert [sum(cluster in row.related_cluster_ids for row in capped) for cluster in ("cluster-a", "cluster-b")] == [25, 25]


def test_depth_two_frontier_is_cluster_fair_and_deduplicates_shared_works() -> None:
    rows: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    for index in range(25):
        rows.append(
            (
                "gap-both",
                {
                    "source_id": f"source-a-{index}",
                    "_expansion_origin_cluster_ids": ["cluster-a"],
                },
                {"doi": "10.9030/shared", "work_id": "work-doi-shared"},
            )
        )
    for index in range(9):
        rows.append(
            (
                "gap-both",
                {
                    "source_id": f"source-b-{index}",
                    "_expansion_origin_cluster_ids": ["cluster-b"],
                },
                {"doi": f"10.9030/b-{index}", "work_id": f"work-doi-b-{index}"},
            )
        )
    selected = _round_robin_frontier(rows, 10)
    work_ids = [str(row[2]["work_id"]) for row in selected]
    assert len(work_ids) == len(set(work_ids)) == 10
    shared = next(row for row in selected if row[2]["work_id"] == "work-doi-shared")
    assert len(shared[2]["seed_ids"]) == 25

    balanced_rows = [
        (
            "gap-both",
            {"source_id": f"source-{cluster}-{index}", "_expansion_origin_cluster_ids": [cluster]},
            {"work_id": f"work-doi-{cluster}-{index}"},
        )
        for cluster in ("cluster-a", "cluster-b")
        for index in range(30)
    ]
    balanced = _round_robin_frontier(balanced_rows, 10)
    assert [
        sum(cluster in row[1]["_expansion_origin_cluster_ids"] for row in balanced)
        for cluster in ("cluster-a", "cluster-b")
    ] == [5, 5]


class _MultiSeedRecommendationProvider:
    name = "semantic-scholar"
    is_network = True

    def resolve_work(self, work):
        suffix = str(work.get("doi") or "").rsplit("/", 1)[-1].upper()
        return {**dict(work), "provider_ids": {"semantic_scholar": f"S2-{suffix}"}}

    def citation_neighbors(self, work, *, limit):
        return []

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return [
            {
                "title": "Multi Seed Recommendation",
                "year": "2024",
                "authors": ["Recommendation Author"],
                "doi": "10.9040/recommendation",
                "relation_type": "recommended_similar",
            }
        ]

    def drain_attempts(self):
        return []


def test_recommendation_records_every_seed_and_full_seed_coverage(tmp_path: Path) -> None:
    seeds = [_item("A", "Seed A", "10.9040/a"), _item("B", "Seed B", "10.9040/b")]
    mapped = _map(tmp_path, seeds)
    write_yaml(
        tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-multi",
                    "note_ids": [row["note_id"] for row in mapped.items],
                    "source_ids": [row["source_id"] for row in mapped.items],
                }
            ]
        },
    )
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="cluster",
            target_ids=("cluster-multi",),
            provider="semantic-scholar",
            allow_network=True,
            run_id="multi-seed-recommendation",
        ),
        client=FakeZotero([]),
        graph_provider=_MultiSeedRecommendationProvider(),
    )
    [candidate] = report.candidates
    expected_sources = sorted(str(row["source_id"]) for row in mapped.items)
    assert candidate["related_source_ids"] == expected_sources
    assert candidate["ranking"]["seed_coverage"] == 1.0
    [observation] = candidate["observations"]
    assert observation["seed_source_ids"] == expected_sources
    assert observation["provider_seed_ids"] == ["S2-A", "S2-B"]
    assert candidate["related_cluster_ids"] == ["cluster-multi"]


class _OneSidedPageProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self) -> None:
        self.page_calls: list[tuple[str, str | None, int]] = []

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work, *, limit):
        raise AssertionError("engine must use the resumable page seam")

    def citation_neighbors_page(self, work, *, relation, cursor, limit):
        self.page_calls.append((relation, cursor, limit))
        if relation == "citations":
            return {"rows": [], "next_cursor": None, "done": True}
        start = int(cursor or 0)
        rows = [
            {
                "title": f"Reference {index}",
                "year": "2024",
                "authors": ["Reference Author"],
                "doi": f"10.9050/reference-{index}",
                "relation_type": "cites",
            }
            for index in range(start, start + limit)
        ]
        next_cursor = str(start + limit) if start + limit < 5 else None
        return {"rows": rows, "next_cursor": next_cursor, "done": next_cursor is None}

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def test_engine_resumable_pages_top_up_from_one_sided_graph(tmp_path: Path) -> None:
    _map(tmp_path, [_item("A", "Seed", "10.9050/seed")])
    provider = _OneSidedPageProvider()
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            budget=5,
            run_id="one-sided-pages",
        ),
        client=FakeZotero([]),
        graph_provider=provider,
    )
    assert report.candidate_count == 5
    assert {row["primary_relation"] for row in report.candidates} == {"cites"}
    assert [(relation, limit) for relation, _, limit in provider.page_calls] == [
        ("citations", 3),
        ("references", 2),
        ("references", 3),
    ]


class _DriftingPageProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self, resolved_id: str, *, interrupt: bool) -> None:
        self.resolved_id = resolved_id
        self.interrupt = interrupt
        self.resolve_calls = 0
        self.page_work_ids: list[str] = []

    def resolve_work(self, work):
        self.resolve_calls += 1
        return {**dict(work), "provider_ids": {"semantic_scholar": self.resolved_id}}

    def citation_neighbors(self, work, *, limit):
        raise AssertionError("engine must use the resumable page seam")

    def citation_neighbors_page(self, work, *, relation, cursor, limit):
        paper_id = str((work.get("provider_ids") or {}).get("semantic_scholar") or "")
        self.page_work_ids.append(paper_id)
        if relation == "references":
            return {"rows": [], "next_cursor": None, "done": True}
        if cursor == "1" and self.interrupt:
            raise RuntimeError("synthetic interruption")
        suffix = cursor or "0"
        return {
            "rows": [
                {
                    "title": f"{paper_id} page {suffix}",
                    "year": "2024",
                    "authors": ["Page Author"],
                    "doi": f"10.9060/{paper_id.casefold()}-{suffix}",
                    "relation_type": "cited_by",
                }
            ],
            "next_cursor": "1" if cursor is None else None,
            "done": cursor is not None,
        }

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def test_resume_reuses_persisted_resolution_and_never_mixes_provider_works(tmp_path: Path) -> None:
    _map(tmp_path, [_item("A", "Seed", "10.9060/seed")])
    request = ExpansionRequest(
        tmp_path,
        scope="source",
        target_ids=("source-zotero-a",),
        provider="semantic-scholar",
        allow_network=True,
        budget=4,
        run_id="resolution-drift",
    )
    first = _DriftingPageProvider("S2-A", interrupt=True)
    interrupted = run_expansion(request, client=FakeZotero([]), graph_provider=first)
    assert interrupted.status == "completed_with_errors"

    second = _DriftingPageProvider("S2-B", interrupt=False)
    resumed = expansion_module.resume_expansion(
        tmp_path,
        "resolution-drift",
        allow_network=True,
        client=FakeZotero([]),
        graph_provider=second,
    )
    assert resumed.status == "completed"
    assert second.resolve_calls == 0
    assert set(second.page_work_ids) == {"S2-A"}
    assert all("s2-a" in row["doi"] for row in resumed.candidates)


class _BarrierProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self, barrier) -> None:
        self.barrier = barrier

    def resolve_work(self, work):
        self.barrier.wait(timeout=10)
        suffix = str(work.get("doi") or "").rsplit("/", 1)[-1].upper()
        return {**dict(work), "provider_ids": {"semantic_scholar": f"SEED-{suffix}"}}

    def citation_neighbors(self, work, *, limit):
        seed_id = str((work.get("provider_ids") or {}).get("semantic_scholar") or "")
        return [
            {
                "title": f"Concurrent candidate {seed_id}",
                "year": "2024",
                "authors": ["Concurrent Author"],
                "doi": f"10.9070/{seed_id.casefold()}",
                "relation_type": "cited_by",
            }
        ]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def _concurrent_expansion_worker(workspace: str, target_id: str, run_id: str, barrier, results) -> None:
    try:
        report = run_expansion(
            ExpansionRequest(
                workspace,
                scope="source",
                target_ids=(target_id,),
                provider="semantic-scholar",
                allow_network=True,
                run_id=run_id,
            ),
            client=FakeZotero([]),
            graph_provider=_BarrierProvider(barrier),
        )
        results.put(("ok", report.candidate_count))
    except Exception as exc:
        results.put((type(exc).__name__, str(exc)))


def test_concurrent_expansion_runs_merge_candidate_registry_without_loss(tmp_path: Path) -> None:
    if "spawn" not in multiprocessing.get_all_start_methods():
        pytest.skip("process-safe registry test requires spawn")
    mapped = _map(
        tmp_path,
        [_item("A", "Seed A", "10.9070/a"), _item("B", "Seed B", "10.9070/b")],
    )
    targets = sorted(str(row["source_id"]) for row in mapped.items)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_expansion_worker,
            args=(str(tmp_path), target_id, f"concurrent-{index}", barrier, results),
        )
        for index, target_id in enumerate(targets)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    results.close()
    assert outcomes == [("ok", 1), ("ok", 1)]
    assert len(list_expansion_candidates(tmp_path)) == 2


def _accepted_candidate(
    suggestion_id: str,
    *,
    target_id: str,
    work_id: str | None = None,
    doi: str = "10.9080/accepted",
    zotero_key: str = "",
    actionability: str = "ready",
) -> ExpansionCandidate:
    local_item = _item(zotero_key, "Accepted Work", doi) if zotero_key else {}
    canonical_work_id = work_id or identify_work(
        {
            "title": "Accepted Work",
            "year": "2024",
            "authors": ["Accepted Author"],
            "doi": doi,
        }
    )[0]
    return ExpansionCandidate(
        work_id=canonical_work_id,
        suggestion_id=suggestion_id,
        title="Accepted Work",
        year="2024",
        authors=["Accepted Author"],
        doi=doi,
        zotero_item_key=zotero_key,
        local_zotero_item=local_item,
        target_scope="source",
        target_id=target_id,
        target_ids=[target_id],
        primary_relation="cites",
        observations=[{"relation_type": "cites", "originating_cluster_ids": []}],
        related_source_ids=[target_id],
        score=0.8,
        actionability=actionability,
        state="accepted",
    )


class _ExactItemClient(FakeZotero):
    def __init__(self, exact_item: Mapping[str, Any], inventory_items: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__([dict(row) for row in inventory_items])
        self.exact_item = dict(exact_item)

    def item(self, item_key: str):
        return dict(self.exact_item) if item_key == str(self.exact_item.get("key")) else None


def test_map_accepted_fails_closed_when_zotero_key_now_names_another_work(tmp_path: Path) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9080/seed")])
    candidate = _accepted_candidate(
        "suggestion-key-drift",
        target_id="source-zotero-seed",
        zotero_key="B",
    )
    _write_candidates(tmp_path, [candidate])
    replacement = _item("B", "Accepted Work", "10.9080/replacement")
    replacement["data"]["creators"] = [{"name": "Accepted Author"}]
    report = map_accepted_candidates(
        tmp_path,
        suggestion_ids=(candidate.suggestion_id,),
        client=_ExactItemClient(replacement),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
        run_id="key-drift-map",
    )
    assert report.status == "completed_no_local_candidates"
    [current] = list_expansion_candidates(tmp_path)
    assert current.fulfillment == "blocked"
    assert not any(
        "10.9080/replacement" in path.read_text(encoding="utf-8")
        for path in (tmp_path / "02_source_memory" / "notes").glob("*.md")
    )


def test_later_local_tuple_match_with_conflicting_doi_is_blocked(tmp_path: Path) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9081/seed")])
    candidate = _accepted_candidate(
        "suggestion-later-conflict",
        target_id="source-zotero-seed",
        doi="10.9081/original",
    )
    _write_candidates(tmp_path, [candidate])
    conflicting = _item("LATER", "Accepted Work", "10.9081/conflict")
    conflicting["data"]["creators"] = [{"name": "Accepted Author"}]
    report = map_accepted_candidates(
        tmp_path,
        suggestion_ids=(candidate.suggestion_id,),
        client=FakeZotero([conflicting]),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
        run_id="later-conflict-map",
    )
    assert report.status == "completed_no_local_candidates"
    [current] = list_expansion_candidates(tmp_path)
    assert current.zotero_item_key == ""
    assert current.fulfillment == "blocked"


def test_unresolved_historical_acceptance_cannot_map_or_export(tmp_path: Path) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9082/seed")])
    candidate = _accepted_candidate(
        "suggestion-unresolved-accepted",
        target_id="source-zotero-seed",
        actionability="resolve_identity",
    )
    _write_candidates(tmp_path, [candidate])
    with pytest.raises(ValueError, match="resolved identity before mapping"):
        map_accepted_candidates(
            tmp_path,
            suggestion_ids=(candidate.suggestion_id,),
            client=FakeZotero([]),
            reader=FakeReader(),
            provider="ollama",
            model="fake-1",
        )
    with pytest.raises(ValueError, match="resolved identity before export"):
        export_expansion_candidates(tmp_path, tmp_path / "unresolved.bib")


def test_export_deduplicates_work_but_marks_every_scope_suggestion(tmp_path: Path) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9083/seed")])
    candidates = [
        _accepted_candidate(
            "suggestion-export-a",
            target_id="source-a",
            work_id="work-doi-shared-export",
            doi="10.9083/shared",
        ),
        _accepted_candidate(
            "suggestion-export-b",
            target_id="source-b",
            work_id="work-doi-shared-export",
            doi="10.9083/shared",
        ),
    ]
    _write_candidates(tmp_path, candidates)
    output = tmp_path / "accepted.bib"
    manifest = export_expansion_candidates(tmp_path, output)
    assert output.read_text(encoding="utf-8").count("@article{") == 1
    assert manifest.metadata["candidate_count"] == 1
    assert manifest.metadata["suggestion_count"] == 2
    assert {row.fulfillment for row in list_expansion_candidates(tmp_path)} == {"exported"}


def test_map_fulfillment_patch_preserves_candidate_added_during_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9084/seed")])
    selected = _accepted_candidate(
        "suggestion-map-race",
        target_id="source-zotero-seed",
        zotero_key="B",
        doi="10.9084/selected",
    )
    concurrent = ExpansionCandidate(
        work_id="work-doi-concurrent-addition",
        suggestion_id="suggestion-concurrent-addition",
        title="Concurrent Addition",
        target_scope="source",
        target_id="source-other",
        target_ids=["source-other"],
        primary_relation="cites",
        score=0.5,
    )
    _write_candidates(tmp_path, [selected])

    def fake_run_pipeline(*_args, **kwargs):
        _write_candidates(tmp_path, [selected, concurrent])
        return RunReport(
            status="completed",
            workspace=tmp_path,
            run_id=str(kwargs["run_id"]),
            inventory_count=1,
            validated_note_count=1,
            items=[{"zotero_item_key": "B", "terminal_status": "validated_note"}],
        )

    monkeypatch.setattr(expansion_module, "run_pipeline", fake_run_pipeline)
    map_accepted_candidates(
        tmp_path,
        suggestion_ids=(selected.suggestion_id,),
        client=FakeZotero([]),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
        run_id="map-race",
    )
    current = {row.suggestion_id: row for row in list_expansion_candidates(tmp_path)}
    assert set(current) == {selected.suggestion_id, concurrent.suggestion_id}
    assert current[selected.suggestion_id].fulfillment == "mapped"


def test_untrusted_candidate_url_cannot_inject_obsidian_wikilinks(tmp_path: Path) -> None:
    _map(tmp_path, [_item("SEED", "Seed", "10.9085/seed")])
    malicious = ExpansionCandidate(
        work_id="work-url-malicious",
        suggestion_id="suggestion-malicious-url",
        title="Unsafe [[Missing]] | Candidate",
        url="https://example.org/paper) [[Injected]]x",
        target_scope="source",
        target_id="source-zotero-seed",
        target_ids=["source-zotero-seed"],
        primary_relation="cites",
        score=0.5,
    )
    _write_candidates(tmp_path, [malicious])
    render_expansion_projection(tmp_path)
    canonical_index = (
        tmp_path / "03_literature_synthesis" / "expansion" / "INDEX.md"
    ).read_text(encoding="utf-8")
    assert "[[Missing]]" not in canonical_index
    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)
    assert exported.status == "exported"
    assert exported.metadata["missing_wikilink_count"] == 0
    projected = (
        Path(exported.metadata["export_root"])
        / "Expansion"
        / "Candidates"
        / "suggestion-malicious-url.md"
    ).read_text(encoding="utf-8")
    assert "[[Injected]]" not in projected
    assert "[External URL]" in projected
    assert "%5B%5BInjected%5D%5D" in projected


class _RequestWideBudgetProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self) -> None:
        self.resolve_calls = 0
        self.returned_rows = 0

    def resolve_work(self, work):
        self.resolve_calls += 1
        suffix = str(work.get("doi") or self.resolve_calls).rsplit("/", 1)[-1]
        return {**dict(work), "provider_ids": {"semantic_scholar": f"S2-{suffix}"}}

    def citation_neighbors(self, work, *, limit):
        seed_id = str((work.get("provider_ids") or {}).get("semantic_scholar") or "seed")
        rows = [
            {
                "title": f"Budget candidate {seed_id} {index}",
                "year": "2025",
                "authors": ["Budget Author"],
                "doi": f"10.9086/{seed_id.casefold()}-{index}",
                "relation_type": "cited_by",
            }
            for index in range(limit)
        ]
        self.returned_rows += len(rows)
        return rows

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        self.returned_rows += limit
        return [
            {
                "title": f"Budget recommendation {index}",
                "year": "2025",
                "authors": ["Budget Author"],
                "doi": f"10.9086/recommendation-{index}",
                "relation_type": "recommended_similar",
            }
            for index in range(limit)
        ]

    def drain_attempts(self):
        return []


def test_external_provider_budget_is_request_wide_across_many_seeds(tmp_path: Path) -> None:
    seeds = [
        _item(f"S{index}", f"Seed {index}", f"10.9086/seed-{index}")
        for index in range(12)
    ]
    mapped = _map(tmp_path, seeds)
    write_yaml(
        tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml",
        {
            "clusters": [
                {
                    "cluster_id": "cluster-provider-budget",
                    "note_ids": [row["note_id"] for row in mapped.items],
                    "source_ids": [row["source_id"] for row in mapped.items],
                }
            ]
        },
    )
    provider = _RequestWideBudgetProvider()

    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="cluster",
            target_ids=("cluster-provider-budget",),
            provider="semantic-scholar",
            allow_network=True,
            budget=5,
            run_id="request-wide-provider-budget",
        ),
        client=FakeZotero([]),
        graph_provider=provider,
    )

    assert provider.returned_rows == 5
    assert 0 < provider.resolve_calls <= 5
    assert report.candidate_count == 5


class _ChannelReservationProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self) -> None:
        self.returned_rows = 0
        self.first_hop_calls = 0
        self.recommendation_calls = 0
        self.depth_two_calls = 0

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "S2-CHANNEL-SEED"}}

    def citation_neighbors(self, work, *, limit):
        doi = str(work.get("doi") or "")
        if doi == "10.9087/channel-seed":
            self.first_hop_calls += 1
            rows = [
                {
                    "title": f"Channel frontier {index}",
                    "year": "2025",
                    "authors": ["Channel Author"],
                    "doi": f"10.9087/channel-frontier-{index}",
                    "provider_ids": {"semantic_scholar": f"S2-CHANNEL-FRONTIER-{index}"},
                    "relation_type": "cited_by",
                }
                for index in range(limit)
            ]
        else:
            self.depth_two_calls += 1
            suffix = doi.rsplit("-", 1)[-1] or "unknown"
            rows = [
                {
                    "title": f"Channel depth two {suffix} {index}",
                    "year": "2024",
                    "authors": ["Channel Author"],
                    "doi": f"10.9087/channel-depth-{suffix}-{index}",
                    "relation_type": "references",
                }
                for index in range(limit)
            ]
        self.returned_rows += len(rows)
        return rows

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        self.recommendation_calls += 1
        rows = [
            {
                "title": f"Channel recommendation {index}",
                "year": "2026",
                "authors": ["Channel Author"],
                "doi": f"10.9087/channel-recommendation-{index}",
                "relation_type": "recommended_similar",
            }
            for index in range(limit)
        ]
        self.returned_rows += len(rows)
        return rows

    def drain_attempts(self):
        return []


def test_external_budget_reserves_recommendation_and_depth_two_channels(tmp_path: Path) -> None:
    seed = _item("CHANNEL", "Channel Seed", "10.9087/channel-seed")
    mapped = _map(tmp_path, [seed])
    provider = _ChannelReservationProvider()

    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(str(mapped.items[0]["source_id"]),),
            provider="semantic-scholar",
            allow_network=True,
            depth=2,
            budget=5,
            run_id="channel-reservation",
        ),
        client=FakeZotero([]),
        graph_provider=provider,
    )

    assert provider.returned_rows == 5
    assert provider.first_hop_calls == 1
    assert provider.recommendation_calls == 1
    assert provider.depth_two_calls >= 1
    assert 0 < report.candidate_count <= 5
    assert any(
        observation["depth"] == 2
        for candidate in report.candidates
        for observation in candidate["observations"]
    )
