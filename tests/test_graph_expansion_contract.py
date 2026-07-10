from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.api import (
    build_map,
    decide_expansion,
    list_expansion_candidates,
    map_accepted_candidates,
    resume_expansion,
    run_expansion,
    run_map,
)
from auto_zettelkasten.citations import write_citation_sidecar
from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.models import ExpansionCandidate, ExpansionDecision, ExpansionRequest, MapRequest
from auto_zettelkasten.scholarly import SemanticScholarProvider

from conftest import FakeReader, FakeZotero


def _item(
    key: str,
    title: str,
    doi: str,
    *,
    relations: Mapping[str, Any] | None = None,
    tags: Sequence[str] = (),
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
            "tags": [{"tag": value} for value in tags],
        },
    }


def _map(workspace: Path, items: Sequence[Mapping[str, Any]], run_id: str = "seed-map"):
    return run_map(
        MapRequest(workspace, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero([dict(row) for row in items]),
        reader=FakeReader(),
        run_id=run_id,
    )


def test_reverse_edges_depth_two_cycles_and_budget_are_bounded(tmp_path: Path) -> None:
    a = _item("A", "Seed A", "10.7000/a", relations={"dc:references": "http://zotero/items/B"})
    b = _item(
        "B",
        "Frontier B",
        "10.7000/b",
        relations={
            "dc:references": ["http://zotero/items/C", "http://zotero/items/A"],
            "owl:sameAs": "http://zotero/items/E",
        },
    )
    c = _item("C", "Depth C", "10.7000/c")
    d = _item("D", "Reverse D", "10.7000/d", relations={"dc:references": "http://zotero/items/A"})
    e = _item("E", "Generic Path E", "10.7000/e")
    f = _item("F", "Coupled F", "10.7000/f", relations={"dc:references": "http://zotero/items/B"})
    _map(tmp_path, [a])
    request = ExpansionRequest(
        tmp_path,
        scope="source",
        target_ids=("source-zotero-a",),
        depth=2,
        budget=10,
        run_id="depth-two",
    )
    report = run_expansion(request, client=FakeZotero([a, b, c, d, e, f]))
    by_key = {row["zotero_item_key"]: row for row in report.candidates}
    assert by_key["B"]["primary_relation"] == "cites"
    assert by_key["D"]["primary_relation"] == "cited_by"
    assert by_key["C"]["depth"] == 2
    # A -> B -> C is a citation path, not co-citation. The benchmark below
    # separately covers the converging/diverging path shapes that justify
    # co_cited_with and bibliographic_coupling.
    assert by_key["C"]["primary_relation"] == "citation_path"
    assert by_key["F"]["primary_relation"] == "bibliographic_coupling"
    assert by_key["E"]["primary_relation"] == "citation_path"
    assert by_key["E"]["ranking"]["relation_strength"] == 0.0
    assert "A" not in by_key
    path_observation = next(row for row in by_key["E"]["observations"] if row["depth"] == 2)
    assert path_observation["path_relation_types"] == ["cites", "zotero_related"]

    bounded = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-zotero-a",),
            depth=2,
            budget=2,
            run_id="depth-budget",
        ),
        client=FakeZotero([a, b, c, d, e, f]),
    )
    assert bounded.candidate_count == 2
    assert bounded.truncated is True
    assert all(row["depth"] == 1 for row in bounded.candidates)


def test_accepted_tag_neighbors_and_scope_specific_round_robin(tmp_path: Path) -> None:
    a = _item("A", "Seed A", "10.7100/a", relations={"dc:references": "http://zotero/items/B"}, tags=("Exact Shared",))
    d = _item("D", "Seed D", "10.7100/d", relations={"dc:references": "http://zotero/items/B"})
    b = _item("B", "Candidate B", "10.7100/b", tags=("Exact Shared",))
    c = _item("C", "Tag Only C", "10.7100/c", tags=("Exact Shared",))
    mapped = _map(tmp_path, [a, d])
    source_ids = tuple(sorted(str(row["source_id"]) for row in mapped.items))
    report = run_expansion(
        ExpansionRequest(tmp_path, scope="source", target_ids=source_ids, budget=2, run_id="round-robin"),
        client=FakeZotero([a, b, c, d]),
    )
    assert {row["target_id"] for row in report.candidates} == set(source_ids)
    assert len({row["suggestion_id"] for row in report.candidates}) == 2

    tag_report = run_expansion(
        ExpansionRequest(tmp_path, scope="source", target_ids=("source-zotero-a",), budget=10, run_id="tag-neighbor"),
        client=FakeZotero([a, b, c, d]),
    )
    tag_only = next(row for row in tag_report.candidates if row["zotero_item_key"] == "C")
    assert tag_only["primary_relation"] == "accepted_tag_neighbor"
    assert tag_only["ranking"]["relation_strength"] == 0.4


class _RankingProvider:
    name = "semantic-scholar"
    is_network = True

    def resolve_work(self, work: Mapping[str, Any]) -> Mapping[str, Any]:
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work: Mapping[str, Any], *, limit: int):
        return [
            {
                "title": "Low Relevance",
                "year": "2024",
                "authors": ["Low Author"],
                "doi": "10.7200/low",
                "relation_type": "cited_by",
                "provider_relevance": 0.2,
            },
            {
                "title": "High Relevance",
                "year": "2024",
                "authors": ["High Author"],
                "doi": "10.7200/high",
                "relation_type": "cited_by",
                "provider_relevance": 0.9,
            },
        ][:limit]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def test_provider_relevance_affects_deterministic_score(tmp_path: Path) -> None:
    seed = _item("A", "Seed", "10.7200/seed")
    _map(tmp_path, [seed])
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            run_id="provider-ranking",
        ),
        client=FakeZotero([]),
        graph_provider=_RankingProvider(),
    )
    by_title = {row["title"]: row for row in report.candidates}
    assert by_title["High Relevance"]["score"] > by_title["Low Relevance"]["score"]
    assert by_title["Low Relevance"]["ranking"]["provider_relevance"] == 0.2


class _TargetRecommendationProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self) -> None:
        self.recommendation_calls: list[tuple[str, ...]] = []

    def resolve_work(self, work):
        suffix = str(work.get("doi") or "seed").rsplit("/", 1)[-1].upper()
        return {**dict(work), "provider_ids": {"semantic_scholar": f"S2-{suffix}"}}

    def citation_neighbors(self, work, *, limit):
        return []

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        positive = tuple(positive_paper_ids)
        self.recommendation_calls.append(positive)
        suffix = positive[0].removeprefix("S2-").casefold()
        return [
            {
                "title": f"Recommendation {suffix}",
                "year": "2024",
                "authors": [f"{suffix} Author"],
                "doi": f"10.7250/recommendation-{suffix}",
                "provider_ids": {"semantic_scholar": f"REC-{suffix}"},
                "relation_type": "recommended_similar",
            }
        ]

    def drain_attempts(self):
        return []


def test_external_recommendations_remain_scoped_per_target(tmp_path: Path) -> None:
    a = _item("A", "Seed A", "10.7250/a")
    d = _item("D", "Seed D", "10.7250/d")
    mapped = _map(tmp_path, [a, d])
    source_ids = tuple(sorted(str(row["source_id"]) for row in mapped.items))
    provider = _TargetRecommendationProvider()
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=source_ids,
            provider="semantic-scholar",
            allow_network=True,
            run_id="target-recommendations",
        ),
        client=FakeZotero([]),
        graph_provider=provider,
    )
    titles_by_target = {row["target_id"]: row["title"] for row in report.candidates}
    assert titles_by_target["source-zotero-a"] == "Recommendation a"
    assert titles_by_target["source-zotero-d"] == "Recommendation d"
    assert sorted(provider.recommendation_calls) == [("S2-A",), ("S2-D",)]


class _S2OnlyProvider:
    name = "semantic-scholar"
    is_network = True

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work, *, limit):
        return [
            {
                "title": "Cross Identifier Work",
                "year": "2024",
                "authors": ["Local Author"],
                "provider_ids": {"semantic_scholar": "S2-CROSS"},
                "relation_type": "cited_by",
            }
        ]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def _url_only_local(key: str, url: str) -> dict[str, Any]:
    row = _item(key, "Cross Identifier Work", "")
    row["data"].pop("DOI")
    row["data"]["url"] = url
    row["data"]["creators"] = [{"firstName": "Local", "lastName": "Author"}]
    return row


def test_cross_identifier_unique_match_merges_but_ambiguous_match_stays_unresolved(tmp_path: Path) -> None:
    unique_workspace = tmp_path / "unique"
    seed = _item("A", "Seed", "10.7260/seed")
    _map(unique_workspace, [seed])
    local = _url_only_local("LOCAL", "https://example.org/unique")
    unique = run_expansion(
        ExpansionRequest(
            unique_workspace,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            run_id="cross-id-unique",
        ),
        client=FakeZotero([local]),
        graph_provider=_S2OnlyProvider(),
    )
    assert unique.candidate_count == 1
    assert unique.candidates[0]["work_id"].startswith("work-s2-")
    assert unique.candidates[0]["zotero_item_key"] == "LOCAL"
    assert unique.candidates[0]["actionability"] == "ready"

    ambiguous_workspace = tmp_path / "ambiguous"
    _map(ambiguous_workspace, [seed])
    ambiguous = run_expansion(
        ExpansionRequest(
            ambiguous_workspace,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            run_id="cross-id-ambiguous",
        ),
        client=FakeZotero(
            [
                _url_only_local("L1", "https://example.org/one"),
                _url_only_local("L2", "https://example.org/two"),
            ]
        ),
        graph_provider=_S2OnlyProvider(),
    )
    assert ambiguous.candidate_count == 1
    assert ambiguous.candidates[0]["zotero_item_key"] == ""
    assert ambiguous.candidates[0]["actionability"] == "resolve_identity"


class _JsonResponse:
    status = 200

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def test_semantic_scholar_balances_and_paginates_both_directions() -> None:
    calls: list[tuple[str, int]] = []

    def opener(request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        relation = "citations" if "/citations" in parsed.path else "references"
        offset = int(dict(urllib.parse.parse_qsl(parsed.query)).get("offset", 0))
        calls.append((relation, offset))
        key = "citingPaper" if relation == "citations" else "citedPaper"
        paper = {
            "paperId": f"{relation}-{offset}",
            "title": f"{relation} {offset}",
            "year": 2024,
            "authors": [{"name": f"Author {offset}"}],
            "externalIds": {"DOI": f"10.7300/{relation}-{offset}"},
        }
        return _JsonResponse({"data": [{key: paper}], "next": offset + 1 if offset == 0 else None})

    provider = SemanticScholarProvider(opener=opener, page_size=1)
    rows = provider.citation_neighbors({"provider_ids": {"semantic_scholar": "SEED"}}, limit=4)
    assert [row["relation_type"] for row in rows] == ["cited_by", "cites", "cited_by", "cites"]
    assert calls == [("citations", 0), ("citations", 1), ("references", 0), ("references", 1)]


class _PageProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self, *, fail_second_page: bool) -> None:
        self.fail_second_page = fail_second_page
        self.page_calls: list[tuple[str, str | None]] = []
        self.attempts: list[dict[str, Any]] = []

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work, *, limit):
        raise AssertionError("engine should use the resumable page seam")

    def citation_neighbors_page(self, work, *, relation, cursor, limit):
        self.page_calls.append((relation, cursor))
        self.attempts.append({"provider": self.name, "request_hash": f"{relation}-{cursor}", "status": "succeeded", "attempt": 1})
        if relation == "citations" and cursor == "1" and self.fail_second_page:
            raise RuntimeError("interrupted second page")
        suffix = cursor or "0"
        row = {
            "title": f"{relation} {suffix}",
            "year": "2024",
            "authors": [f"{relation} Author"],
            "doi": f"10.7400/{relation}-{suffix}",
            "relation_type": "cited_by" if relation == "citations" else "cites",
        }
        return {"rows": [row], "next_cursor": "1" if cursor is None else None, "done": cursor is not None}

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        rows = list(self.attempts)
        self.attempts.clear()
        return rows


def test_interrupted_provider_pages_resume_from_persisted_cursor(tmp_path: Path) -> None:
    seed = _item("A", "Seed", "10.7400/seed")
    _map(tmp_path, [seed])
    request = ExpansionRequest(
        tmp_path,
        scope="source",
        target_ids=("source-zotero-a",),
        provider="semantic-scholar",
        allow_network=True,
        budget=4,
        run_id="paged-run",
    )
    interrupted_provider = _PageProvider(fail_second_page=True)
    interrupted = run_expansion(request, client=FakeZotero([]), graph_provider=interrupted_provider)
    assert interrupted.status == "completed_with_errors"
    cache = read_yaml(tmp_path / "11_state" / "runs" / "paged-run" / "provider_results.yml")
    assert any(row.get("kind") == "citation_page" and row.get("cursor") == "0" for row in cache["calls"].values())

    resumed_provider = _PageProvider(fail_second_page=False)
    resumed = resume_expansion(
        tmp_path,
        "paged-run",
        allow_network=True,
        client=FakeZotero([]),
        graph_provider=resumed_provider,
    )
    assert resumed.status == "completed"
    assert ("citations", None) not in resumed_provider.page_calls
    assert ("citations", "1") in resumed_provider.page_calls
    attempts = read_yaml(tmp_path / "11_state" / "runs" / "paged-run" / "provider_attempts.yml")["attempts"]
    assert {row["request_hash"] for row in attempts} >= {"citations-None", "citations-1"}


class _ExternalProvider:
    name = "semantic-scholar"
    is_network = True

    def resolve_work(self, work):
        return {**dict(work), "provider_ids": {"semantic_scholar": "SEED"}}

    def citation_neighbors(self, work, *, limit):
        return [
            {
                "title": "Later Local Work",
                "year": "2024",
                "authors": ["E Author"],
                "doi": "10.7500/later",
                "provider_ids": {"semantic_scholar": "LATER"},
                "relation_type": "cited_by",
            }
        ]

    def recommendations(self, positive_paper_ids, *, negative_paper_ids=(), limit=10):
        return []

    def drain_attempts(self):
        return []


def test_external_candidate_added_to_zotero_maps_with_terminal_and_manifest_accounting(tmp_path: Path) -> None:
    seed = _item("A", "Seed", "10.7500/seed")
    _map(tmp_path, [seed])
    expansion = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-zotero-a",),
            provider="semantic-scholar",
            allow_network=True,
            run_id="later-local",
        ),
        client=FakeZotero([]),
        graph_provider=_ExternalProvider(),
    )
    candidate = expansion.candidates[0]
    assert candidate["zotero_item_key"] == ""
    decide_expansion(
        tmp_path,
        ExpansionDecision(candidate["suggestion_id"], 0, "accepted", "Acquire after local import"),
    )

    local = _item("E", "Later Local Work", "10.7500/later")
    mapped = map_accepted_candidates(
        tmp_path,
        suggestion_ids=(candidate["suggestion_id"],),
        client=FakeZotero([local]),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
        run_id="focused-later-local",
    )
    assert mapped.inventory_count == mapped.terminal_count == 1
    assert mapped.source_set["upstream_scope"]["kind"] == "graph_expansion"
    assert mapped.source_set["originating_suggestion_ids"] == [candidate["suggestion_id"]]
    current = next(row for row in list_expansion_candidates(tmp_path) if row.suggestion_id == candidate["suggestion_id"])
    assert current.zotero_item_key == "E"
    assert current.fulfillment == "mapped"
    source_set_path = Path(mapped.source_set["path"])
    manifest_row = next(
        row
        for row in mapped.artifact_manifest.artifacts
        if row["path"] == str(source_set_path.relative_to(tmp_path))
    )
    assert manifest_row["sha256"] == sha256_file(source_set_path)


class _ExactMissingZotero(FakeZotero):
    def item(self, item_key: str):
        return None


def test_missing_exact_zotero_item_exhausts_instead_of_using_stale_embedded_metadata(tmp_path: Path) -> None:
    a = _item("A", "Seed", "10.7550/a", relations={"dc:references": "http://zotero/items/B"})
    b = _item("B", "Deleted candidate", "10.7550/b")
    _map(tmp_path, [a])
    expansion = run_expansion(
        ExpansionRequest(tmp_path, scope="source", target_ids=("source-zotero-a",), run_id="deleted-item"),
        client=FakeZotero([a, b]),
    )
    candidate = next(row for row in expansion.candidates if row["zotero_item_key"] == "B")
    decide_expansion(
        tmp_path,
        ExpansionDecision(candidate["suggestion_id"], 0, "accepted", "Test missing exact item"),
    )
    mapped = map_accepted_candidates(
        tmp_path,
        suggestion_ids=(candidate["suggestion_id"],),
        client=_ExactMissingZotero([], missing={"B"}),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
        run_id="deleted-item-map",
    )
    assert mapped.inventory_count == mapped.terminal_count == mapped.exhausted_count == 1
    current = next(row for row in list_expansion_candidates(tmp_path) if row.suggestion_id == candidate["suggestion_id"])
    assert current.fulfillment == "exhausted"


@pytest.mark.parametrize(
    ("state", "actionability"),
    [
        ("proposed", "ready"),
        ("parked", "ready"),
        ("rejected", "ready"),
        ("proposed", "resolve_identity"),
        ("accepted", "ready"),
    ],
)
def test_typed_relations_reconcile_sidecars_without_candidate_leakage(
    tmp_path: Path,
    state: str,
    actionability: str,
) -> None:
    a = _item("A", "A", "10.7600/a")
    b = _item("B", "B", "10.7600/b")
    c = _item("C", "C", "10.7600/c")
    mapped = _map(tmp_path, [a, b, c])
    by_key = {row["zotero_item_key"]: row for row in mapped.items}
    write_citation_sidecar(
        tmp_path,
        item=a,
        source_id=by_key["A"]["source_id"],
        text=(
            "References\n"
            "1. B Author. 2024. B. https://doi.org/10.7600/b\n"
            "2. C Author. 2024. C. https://doi.org/10.7600/c\n"
            "3. X Author. 2024. X. https://doi.org/10.7600/x"
        ),
        content_hash="a" * 64,
        source_file="synthetic-a",
    )
    write_citation_sidecar(
        tmp_path,
        item=b,
        source_id=by_key["B"]["source_id"],
        text="References\n1. X Author. 2024. X. https://doi.org/10.7600/x",
        content_hash="b" * 64,
        source_file="synthetic-b",
    )
    candidate = ExpansionCandidate(
        work_id="work-doi-deadbeef",
        suggestion_id=f"suggestion-isolation-{state}-{actionability}",
        title="Uninspected suggestion",
        target_scope="source",
        target_id=by_key["A"]["source_id"],
        target_ids=[by_key["A"]["source_id"]],
        primary_relation="cites",
        score=0.5,
        state=state,
        actionability=actionability,
    )
    write_yaml(
        tmp_path / "03_literature_synthesis" / "expansion" / "candidates.yml",
        {"artifact_schema_version": "1.1", "candidates": [candidate.to_dict()]},
    )
    build_map(tmp_path, run_id="sidecar-rebuild")
    typed = read_yaml(tmp_path / "02_source_memory" / "indexes" / "typed_links.yml")["links"]
    relation_types = {row["relation_type"] for row in typed}
    assert {"cites", "co_cited_with", "bibliographic_coupling"} <= relation_types
    protected_paths = [
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml",
        tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml",
        tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml",
        tmp_path / "03_literature_synthesis" / "packets" / "literature-packet-sidecar-rebuild.yml",
    ]
    for path in protected_paths:
        if path.exists():
            assert candidate.suggestion_id not in path.read_text(encoding="utf-8")
