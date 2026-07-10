from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.api import (
    decide_expansion,
    list_expansion_candidates,
    run_expansion,
    run_map,
)
from auto_zettelkasten.models import ExpansionDecision, ExpansionRequest, MapRequest


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "expansion_benchmark.json"
SECTION_KEYS = (
    "thesis",
    "method_and_research_design",
    "evidence_and_data",
    "detailed_findings",
    "strengths_and_contributions",
    "methodological_critique",
    "limitations",
    "what_this_source_can_support",
    "what_this_source_cannot_support",
    "locators",
)


@pytest.fixture
def benchmark_corpus() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class BenchmarkReader:
    name = "benchmark-reader"
    model = "benchmark-1"
    is_cloud = False

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        del text, question
        title = str(metadata.get("title") or "synthetic source")
        return {
            key: f"Source-grounded {key.replace('_', ' ')} for {title}; inspected at page 1."
            for key in SECTION_KEYS
        }


class BenchmarkZotero:
    def __init__(self, items: Sequence[Mapping[str, Any]], fulltext: Mapping[str, str] | None = None) -> None:
        self._items = [deepcopy(dict(item)) for item in items]
        self._fulltext = dict(fulltext or {})

    def status(self) -> Mapping[str, Any]:
        return {"status": "available", "read_only": True, "base_url": "fake://benchmark-zotero"}

    def collections(self) -> list[dict[str, Any]]:
        return []

    def selected_collection(self) -> Mapping[str, Any]:
        return {"scope": "library", "key": ""}

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]:
        del scope, collection_key
        return deepcopy(self._items)

    def children(self, item_key: str) -> list[dict[str, Any]]:
        return [
            {
                "key": f"{item_key}PDF",
                "data": {
                    "key": f"{item_key}PDF",
                    "parentItem": item_key,
                    "itemType": "attachment",
                    "contentType": "text/plain",
                    "filename": f"{item_key}.txt",
                },
            }
        ]

    def fulltext(self, item_key: str) -> Mapping[str, Any] | None:
        parent_key = item_key.removesuffix("PDF")
        text = self._fulltext.get(parent_key) or (
            f"Complete inspected synthetic document content for {parent_key}. " * 12
        )
        return {"content": text, "contentType": "text/plain", "indexedChars": len(text), "totalChars": len(text)}

    def file(self, item_key: str) -> tuple[bytes, str] | None:
        del item_key
        return None

    def item(self, item_key: str) -> Mapping[str, Any] | None:
        return next((deepcopy(row) for row in self._items if row.get("key") == item_key), None)


class AcceptEverythingController:
    def review_expansion_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "suggestion_id": row["suggestion_id"],
                "decision": "accepted",
                "decision_reason": "synthetic controller acceptance attempt",
                "actor": "benchmark-controller",
            }
            for row in candidates
        ]


class BenchmarkGraphProvider:
    name = "semantic-scholar"
    is_network = True

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []

    def resolve_work(self, work: Mapping[str, Any]) -> Mapping[str, Any] | None:
        self.calls.append(("resolve", deepcopy(dict(work))))
        self.attempts.append({"provider": self.name, "status": "succeeded", "request_hash": "resolve"})
        return {**dict(work), "provider_ids": {"semantic_scholar": "S2-BENCHMARK-SEED"}}

    def citation_neighbors(
        self,
        work: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(("neighbors", {"work": deepcopy(dict(work)), "limit": limit}))
        self.attempts.append({"provider": self.name, "status": "succeeded", "request_hash": "neighbors"})
        return deepcopy(self.fixture["neighbors"][:limit])

    def recommendations(
        self,
        positive_paper_ids: Sequence[str],
        *,
        negative_paper_ids: Sequence[str] = (),
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(
            (
                "recommendations",
                {
                    "positive_paper_ids": list(positive_paper_ids),
                    "negative_paper_ids": list(negative_paper_ids),
                    "limit": limit,
                },
            )
        )
        self.attempts.append({"provider": self.name, "status": "succeeded", "request_hash": "recommendations"})
        return deepcopy(self.fixture["recommendations"][:limit])

    def drain_attempts(self) -> Sequence[Mapping[str, Any]]:
        rows = deepcopy(self.attempts)
        self.attempts.clear()
        return rows


def _zotero_item(spec: Mapping[str, Any]) -> dict[str, Any]:
    key = str(spec["key"])
    relations: dict[str, Any] = {}
    for predicate, values in dict(spec.get("relations") or {}).items():
        targets = values if isinstance(values, list) else [values]
        relations[str(predicate)] = [f"http://zotero.org/users/local/items/{target}" for target in targets]
    data: dict[str, Any] = {
        "key": key,
        "itemType": "journalArticle",
        "title": str(spec.get("title") or key),
        "date": str(spec.get("year") or "2020"),
        "creators": [
            {
                "creatorType": "author",
                "name": str(spec.get("author") or f"Author {key}"),
            }
        ],
        "tags": [{"tag": str(tag)} for tag in spec.get("tags", [])],
    }
    if spec.get("doi"):
        data["DOI"] = str(spec["doi"])
    if relations:
        data["relations"] = relations
    return {"key": key, "data": data}


def _reference_text(works: Sequence[Mapping[str, Any]]) -> str:
    references = "\n".join(
        f"{index}. Author {row['key']}. ({row.get('year', '2020')}). {row['title']}. doi:{row['doi']}."
        for index, row in enumerate(works, start=1)
    )
    return (
        "This complete synthetic article contains inspected prose used only by the local benchmark. " * 8
        + "\n\nReferences\n"
        + references
    )


def _ambiguous_reference_text(reference: str) -> str:
    return (
        "This complete synthetic article contains inspected prose used only by the local benchmark. " * 8
        + "\n\nReferences\n"
        + reference
    )


def _map_seed_items(
    workspace: Path,
    seed_specs: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    fulltext: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    seeds = [_zotero_item(row) for row in seed_specs]
    report = run_map(
        MapRequest(workspace, provider="ollama", model="benchmark-1", parallel=1),
        client=BenchmarkZotero(seeds, fulltext),
        reader=BenchmarkReader(),
        run_id=run_id,
    )
    assert report.status == "completed"
    assert report.validated_note_count == len(seeds)
    source_ids = {str(row["zotero_item_key"]): str(row["source_id"]) for row in report.items}
    return source_ids, seeds


def _surface_snapshot(workspace: Path) -> dict[str, bytes]:
    roots = (
        workspace / "02_source_memory" / "notes",
        workspace / "02_source_memory" / "indexes" / "typed_links.yml",
        workspace / "02_source_memory" / "indexes" / "typed_note_links.yml",
        workspace / "03_literature_synthesis" / "clusters",
        workspace / "03_literature_synthesis" / "gaps",
        workspace / "03_literature_synthesis" / "closest_prior_work",
        workspace / "03_literature_synthesis" / "packets",
    )
    snapshot: dict[str, bytes] = {}
    for root in roots:
        if root.is_file():
            snapshot[str(root.relative_to(workspace))] = root.read_bytes()
        elif root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    snapshot[str(path.relative_to(workspace))] = path.read_bytes()
    return snapshot


def test_exact_graph_recall_holdout_dedup_and_unaccepted_isolation(
    tmp_path: Path,
    benchmark_corpus: Mapping[str, Any],
) -> None:
    fixture = benchmark_corpus["exact_graph"]
    seed = fixture["seed"]
    fulltext = {str(seed["key"]): _reference_text(fixture["citation_works"])}
    source_ids, seed_items = _map_seed_items(tmp_path, [seed], run_id="benchmark-exact-map", fulltext=fulltext)
    canonical_before = _surface_snapshot(tmp_path)
    inventory = [
        *seed_items,
        *[_zotero_item(row) for row in fixture["relation_works"]],
        *[_zotero_item(row) for row in fixture["citation_works"]],
        *[_zotero_item(row) for row in fixture["distractor_works"]],
    ]
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(source_ids[str(seed["key"])],),
            provider="internal",
            depth=1,
            budget=int(benchmark_corpus["recall_at_k"]),
            run_id="benchmark-exact-expansion",
        ),
        client=BenchmarkZotero(inventory, fulltext),
    )

    assert report.status == "completed"
    assert report.candidate_count == int(benchmark_corpus["recall_at_k"])
    assert report.truncated is True
    assert all(row["state"] == "proposed" for row in report.candidates)

    retrieved_dois = [str(row["doi"]) for row in report.candidates]
    gold = set(fixture["gold_dois"])
    holdout = set(fixture["holdout_positive_dois"])
    exact_recall = len(gold & set(retrieved_dois)) / len(gold)
    recall_at_25 = len(holdout & set(retrieved_dois[:25])) / len(holdout)
    duplicate_suggestion_rate = (
        len(report.candidates) - len({row["suggestion_id"] for row in report.candidates})
    ) / len(report.candidates)

    assert exact_recall == 1.0
    assert recall_at_25 >= float(benchmark_corpus["minimum_holdout_recall"])
    assert duplicate_suggestion_rate == 0.0
    assert len({row["work_id"] for row in report.candidates}) == len(report.candidates)

    parked_id = str(report.candidates[0]["suggestion_id"])
    rejected_id = str(report.candidates[1]["suggestion_id"])
    decide_expansion(
        tmp_path,
        ExpansionDecision(parked_id, 0, "parked", "Retain for later benchmark review"),
    )
    decide_expansion(
        tmp_path,
        ExpansionDecision(rejected_id, 0, "rejected", "Out of scope for benchmark target"),
    )
    states = {row.suggestion_id: row.state for row in list_expansion_candidates(tmp_path)}
    assert states[parked_id] == "parked"
    assert states[rejected_id] == "rejected"
    assert "proposed" in set(states.values())

    canonical_after = _surface_snapshot(tmp_path)
    assert canonical_after == canonical_before
    canonical_text = b"\n".join(canonical_after.values())
    for row in report.candidates:
        assert str(row["suggestion_id"]).encode() not in canonical_text


def test_round_robin_budget_truncation_is_deterministic(
    tmp_path: Path,
    benchmark_corpus: Mapping[str, Any],
) -> None:
    fixture = benchmark_corpus["round_robin"]
    source_ids, seed_items = _map_seed_items(tmp_path, fixture["seeds"], run_id="benchmark-round-map")
    inventory = [*seed_items, *[_zotero_item(row) for row in fixture["works"]]]
    target_ids = tuple(source_ids[str(seed["key"])] for seed in fixture["seeds"])
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=target_ids,
            provider="internal",
            depth=1,
            budget=int(fixture["budget"]),
            run_id="benchmark-round-expansion",
        ),
        client=BenchmarkZotero(inventory),
    )

    ordered_targets = sorted(target_ids)
    assert report.truncated is True
    assert [row["target_id"] for row in report.candidates] == [
        ordered_targets[0],
        ordered_targets[1],
        ordered_targets[0],
        ordered_targets[1],
        ordered_targets[0],
    ]
    assert Counter(row["target_id"] for row in report.candidates) == {
        ordered_targets[0]: 3,
        ordered_targets[1]: 2,
    }

    rerun = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=target_ids,
            provider="internal",
            depth=1,
            budget=int(fixture["budget"]),
            run_id="benchmark-round-rerun",
        ),
        client=BenchmarkZotero(inventory),
    )
    assert [row["suggestion_id"] for row in rerun.candidates] == [
        row["suggestion_id"] for row in report.candidates
    ]


def test_cycles_are_safe_and_depth_two_records_derived_relations(
    tmp_path: Path,
    benchmark_corpus: Mapping[str, Any],
) -> None:
    fixture = benchmark_corpus["depth_graph"]
    seed = fixture["seed"]
    source_ids, seed_items = _map_seed_items(tmp_path, [seed], run_id="benchmark-depth-map")
    inventory = [*seed_items, *[_zotero_item(row) for row in fixture["works"]]]
    target_id = source_ids[str(seed["key"])]

    depth_one = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(target_id,),
            provider="internal",
            depth=1,
            budget=20,
            run_id="benchmark-depth-one",
        ),
        client=BenchmarkZotero(inventory),
    )
    assert {row["doi"] for row in depth_one.candidates} == set(fixture["depth_one_dois"])

    depth_two = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(target_id,),
            provider="internal",
            depth=2,
            budget=20,
            run_id="benchmark-depth-two",
        ),
        client=BenchmarkZotero(inventory),
    )
    assert depth_two.candidate_count == 4
    assert len({row["suggestion_id"] for row in depth_two.candidates}) == 4
    assert str(seed["doi"]) not in {row["doi"] for row in depth_two.candidates}
    by_doi = {str(row["doi"]): row for row in depth_two.candidates}
    for doi, relation in fixture["depth_two_relations"].items():
        candidate = by_doi[doi]
        assert candidate["depth"] == 2
        assert relation in {row["relation_type"] for row in candidate["observations"]}
        assert any(row.get("path_work_ids") for row in candidate["observations"])


def test_fake_semantic_provider_yields_recommendations_and_graph_observations(
    tmp_path: Path,
    benchmark_corpus: Mapping[str, Any],
) -> None:
    fixture = benchmark_corpus["semantic_graph"]
    seed = fixture["seed"]
    source_ids, _ = _map_seed_items(tmp_path, [seed], run_id="benchmark-semantic-map")
    provider = BenchmarkGraphProvider(fixture)
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(source_ids[str(seed["key"])],),
            provider="semantic-scholar",
            depth=1,
            budget=10,
            allow_network=True,
            run_id="benchmark-semantic-expansion",
        ),
        client=BenchmarkZotero([]),
        graph_provider=provider,
    )

    assert report.status == "completed"
    by_doi = {str(row["doi"]): row for row in report.candidates}
    assert {"resolve", "neighbors", "recommendations"} == {name for name, _ in provider.calls}
    assert by_doi["10.7777/semantic-co-cited"]["primary_relation"] == "co_cited_with"
    assert by_doi["10.7777/semantic-coupling"]["primary_relation"] == "bibliographic_coupling"
    assert by_doi["10.7777/semantic-recommendation"]["primary_relation"] == "recommended_similar"
    assert all(row["provider"] == "semantic-scholar" for row in report.candidates)


def test_ambiguous_identity_cannot_be_auto_accepted_or_linked(
    tmp_path: Path,
    benchmark_corpus: Mapping[str, Any],
) -> None:
    fixture = benchmark_corpus["ambiguous_identity"]
    seed = fixture["seed"]
    fulltext = {str(seed["key"]): _ambiguous_reference_text(str(fixture["reference"]))}
    source_ids, seed_items = _map_seed_items(tmp_path, [seed], run_id="benchmark-ambiguous-map", fulltext=fulltext)
    canonical_before = _surface_snapshot(tmp_path)
    inventory = [*seed_items, *[_zotero_item(row) for row in fixture["works"]]]
    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(source_ids[str(seed["key"])],),
            provider="internal",
            depth=1,
            budget=10,
            run_id="benchmark-ambiguous-expansion",
        ),
        client=BenchmarkZotero(inventory, fulltext),
        controller=AcceptEverythingController(),
    )

    assert report.candidate_count == 1
    [candidate] = report.candidates
    assert candidate["actionability"] == "resolve_identity"
    assert candidate["state"] == "parked"
    assert candidate["decision_version"] == 1
    with pytest.raises(ValueError, match="unresolved candidates cannot be accepted"):
        decide_expansion(
            tmp_path,
            ExpansionDecision(
                str(candidate["suggestion_id"]),
                1,
                "accepted",
                "Synthetic attempt to bypass identity resolution",
            ),
        )

    assert _surface_snapshot(tmp_path) == canonical_before
    typed_links = (tmp_path / "02_source_memory" / "indexes" / "typed_links.yml").read_text(encoding="utf-8")
    assert str(candidate["suggestion_id"]) not in typed_links
    assert str(candidate["work_id"]) not in typed_links
