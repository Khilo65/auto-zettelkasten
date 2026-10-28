import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from auto_zettelkasten.framework_comparison import ComparisonError, validate_corpus
from auto_zettelkasten.graphiti_grounding import EpisodeBindingRegistry, bind_ingested_episodes, hydrate_graphiti_evidence, make_episode_binding, research_search_kwargs


def corpus():
    values = [("a", "The trial found no survival benefit. Its participants were adults."),
              ("b", "The earlier trial found a survival benefit among children.")]
    return [{"source_id": sid, "title": sid.upper(), "text": text,
             "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "coverage": {"source_scope": "abstract_only"}} for sid, text in values]


def setup():
    rows = corpus()
    _, digest = validate_corpus(rows)
    bindings = {f"e-{row['source_id']}": make_episode_binding(f"e-{row['source_id']}", row["source_id"],
                corpus=rows, corpus_sha256=digest, group_id="experiment") for row in rows}
    episodes = {f"e-{row['source_id']}": SimpleNamespace(uuid=f"e-{row['source_id']}", group_id="experiment",
                content=row["text"], source=SimpleNamespace(value="text"), episode_metadata=None) for row in rows}
    return rows, digest, bindings, episodes


def run(edges, *, mutate=None, **kwargs):
    rows, digest, bindings, episodes = setup()
    if mutate:
        mutate(bindings, episodes)
    calls = []
    async def fetch(driver, ids):
        calls.append(ids)
        return [episodes[uid] for uid in ids if uid in episodes]
    result = asyncio.run(hydrate_graphiti_evidence(edges, driver=object(), bindings=bindings,
                         corpus=rows, corpus_sha256=digest, fetch_episodes=fetch, **kwargs))
    return result, calls


def edge(uuid="f", episodes=("e-a",), **fields):
    return SimpleNamespace(uuid=uuid, fact="The treatment always improves survival.", episodes=list(episodes), **fields)


def test_original_note_is_hydrated_but_generated_fact_is_never_verified():
    result, calls = run([edge()], query="survival")
    assert calls == [["e-a"]]
    assert result["notes"][0]["excerpt"] == corpus()[0]["text"]
    assert result["notes"][0]["source_retrieval_status"] == "original_note_retrieved"
    assert result["notes"][0]["fact_verification"] == "unknown"
    assert result["native_records"][0]["generated_fact"] not in result["notes"][0]["excerpt"]
    assert result["provider_calls"] == 0
    assert result["original_publication_verified"] is False


def test_mixed_unknown_binding_rejects_entire_fact_before_any_read():
    result, calls = run([edge(episodes=("e-a", "unknown"))])
    assert calls == [] and result["notes"] == []
    assert result["native_records"][0]["grounding_status"] == "invalid_episode_binding"


@pytest.mark.parametrize("field,value", [("group_id", "other"), ("content", "Fabricated content."),
                                        ("source", "json"), ("episode_metadata", {"source_id": "a"})])
def test_mixed_invalid_hydration_never_launders_valid_subset(field, value):
    def mutate(bindings, episodes):
        setattr(episodes["e-b"], field, value)
    result, _ = run([edge(episodes=("e-a", "e-b"))], mutate=mutate)
    assert result["notes"] == []
    assert result["grounding_failures"][0]["grounding_status"] == "invalid_hydrated_episode"


def test_missing_episode_is_rejected_without_partial_admission():
    result, _ = run([edge(episodes=("e-a", "e-b"))], mutate=lambda bindings, episodes: episodes.pop("e-b"))
    assert result["notes"] == []
    assert "missing_hydrated_episode" in result["grounding_failures"][0]["failures"]


def test_stale_and_wrong_corpus_bindings_reject_before_lookup():
    for field in ("content_sha256", "corpus_sha256"):
        def mutate(bindings, episodes):
            bindings["e-a"] = replace(bindings["e-a"], **{field: "f" * 64})
        result, calls = run([edge()], mutate=mutate)
        assert not calls and not result["notes"]


def test_lookup_cannot_return_extra_or_duplicate_episode():
    rows, digest, bindings, episodes = setup()
    for values in ([episodes["e-a"], episodes["e-b"]], [episodes["e-a"], episodes["e-a"]]):
        async def fetch(driver, ids):
            return values
        with pytest.raises(ComparisonError, match="unrequested or duplicate"):
            asyncio.run(hydrate_graphiti_evidence([edge()], driver=None, bindings=bindings,
                        corpus=rows, corpus_sha256=digest, fetch_episodes=fetch))


def test_temporal_flags_do_not_discard_conflicting_scholarly_notes():
    result, _ = run([edge("old", ("e-b",), invalid_at="2025-01-01"), edge("new", ("e-a",))])
    assert {row["source_id"] for row in result["notes"]} == {"a", "b"}
    assert result["native_temporal_claims_filtered"] is False
    assert result["native_query_temporal_filtering"] == "not_evaluated"
    assert result["native_records"][0]["native_temporal_flags"]["invalid_at"] == "2025-01-01"


def test_an_invalid_hit_does_not_suppress_an_independently_valid_one():
    result, _ = run([edge("invalid", ("e-b", "unknown")), edge("valid", ("e-a",))])
    assert [row["source_id"] for row in result["notes"]] == ["a"]
    assert len(result["grounding_failures"]) == 1


def test_budgets_include_metadata_and_keep_literal_offsets():
    result, _ = run([edge("one", ("e-a",)), edge("two", ("e-b",))], max_notes=1, max_chars=750)
    assert len(result["notes"]) <= 1
    assert result["chars"] <= 750
    assert result["chars"] == len(json.dumps(result["notes"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert result["truncated"]
    source = {row["source_id"]: row["text"] for row in corpus()}
    for row in result["notes"]:
        assert source[row["source_id"]][row["excerpt_start"]:row["excerpt_end"]] == row["excerpt"]


def test_identity_and_empty_reference_validation():
    rows, digest, bindings, episodes = setup()
    with pytest.raises(ComparisonError, match="corpus identity"):
        asyncio.run(hydrate_graphiti_evidence([], driver=None, bindings=bindings, corpus=rows, corpus_sha256="x"))
    with pytest.raises(ComparisonError, match="duplicate Graphiti fact"):
        run([edge(), edge()])
    result, calls = run([edge(episodes=())])
    assert not calls and not result["notes"]


def test_default_reader_uses_actual_episodic_node_class_without_inference(monkeypatch):
    import sys
    from auto_zettelkasten.graphiti_grounding import fetch_graphiti_episodes
    calls = []
    class Node:
        @classmethod
        async def get_by_uuids(cls, driver, uuids):
            calls.append((driver, uuids))
            return []
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", SimpleNamespace(EpisodicNode=Node))
    driver = object()
    assert asyncio.run(fetch_graphiti_episodes(driver, ["e-a"])) == []
    assert calls == [(driver, ["e-a"])]


def test_actual_ingestion_result_uuid_is_bound_instead_of_assuming_new_uuid():
    rows, digest, _, episodes = setup()
    registry = EpisodeBindingRegistry(rows, corpus_sha256=digest, group_id="experiment")
    result = SimpleNamespace(episode=episodes["e-a"])
    bound = registry.record_ingestion(result, "a")
    assert bound.episode_uuid == "e-a"
    assert registry.bindings == {"e-a": bound}
    with pytest.raises(ComparisonError, match="differs from"):
        registry.record_ingestion(result, "b")
    episodes["e-b"].uuid = "e-a"
    with pytest.raises(ComparisonError, match="rebound"):
        registry.record_ingestion(SimpleNamespace(episode=episodes["e-b"]), "b")


def test_explicit_research_filter_has_no_temporal_suppression_but_no_guarantee_of_native_completeness():
    options = research_search_kwargs("experiment", filters_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert options["group_ids"] == ["experiment"]
    assert options["num_results"] == 32
    assert vars(options["search_filter"]) == dict(valid_at=None, invalid_at=None, created_at=None, expired_at=None)
    with pytest.raises(ComparisonError, match="temporal filters"):
        research_search_kwargs("experiment", filters_factory=lambda **kwargs: SimpleNamespace(**dict(kwargs, invalid_at="today")))


def test_failed_episode_read_does_not_retry_or_fall_back():
    rows, digest, bindings, _ = setup()
    calls = []
    async def fail(driver, ids):
        calls.append(ids)
        raise RuntimeError("private database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(hydrate_graphiti_evidence([edge()], driver=None, bindings=bindings,
                    corpus=rows, corpus_sha256=digest, fetch_episodes=fail))
    assert calls == [["e-a"]]


def test_bulk_binding_is_complete_order_bound_and_all_or_nothing():
    rows, digest, _, episodes = setup()
    results = [SimpleNamespace(episode=episodes[uid]) for uid in ("e-a", "e-b")]
    bound = bind_ingested_episodes(results, source_ids=["a", "b"], corpus=rows,
                                   corpus_sha256=digest, group_id="experiment")
    assert {key: value.source_id for key, value in bound.items()} == {"e-a": "a", "e-b": "b"}
    for malformed, ids in ((results[:1], ["a"]), (results, ["b", "a"]), (results, ["a", "a"])):
        with pytest.raises(ComparisonError):
            bind_ingested_episodes(malformed, source_ids=ids, corpus=rows,
                                    corpus_sha256=digest, group_id="experiment")
    episodes["e-b"].episode_metadata = {"source_id": "a"}
    with pytest.raises(ComparisonError, match="metadata mismatch"):
        bind_ingested_episodes(results, source_ids=["a", "b"], corpus=rows,
                                corpus_sha256=digest, group_id="experiment")
