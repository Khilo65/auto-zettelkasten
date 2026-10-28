import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
from types import SimpleNamespace

import pytest

from auto_zettelkasten.framework_comparison import (
    AggregateSpendLedger, ComparisonError, FRAMEWORKS, NativeFrameworkAdapter,
    NativeHit, StageBound, extract_native_hits, normalize_evidence,
    prepare_framework_plan, validate_corpus,
    preview_stage_bounds,
)


def corpus():
    return [{"source_id": "a", "title": "Synthetic A", "text": "Institutions condition peace.",
             "content_sha256": hashlib.sha256(b"Institutions condition peace.").hexdigest(),
             "coverage": {"status": "abstract"}},
            {"source_id": "b", "title": "Synthetic B", "text": "Institutions condition fiscal competition.",
             "content_sha256": hashlib.sha256(b"Institutions condition fiscal competition.").hexdigest(),
             "coverage": {"status": "substantive_note"}}]


def make_bound(framework="graphiti", **changes):
    bound = StageBound(framework, "query", 1, 100, 10, "0.001", "0.002",
                       "https://example.org/synthetic-pricing", "synthetic-pinned")
    return replace(bound, **changes)


@pytest.mark.parametrize("framework", list(FRAMEWORKS))
def test_preparation_has_no_execution_or_guessed_price(framework, tmp_path):
    plan = prepare_framework_plan(framework, corpus(), experiment_root=tmp_path,
                                  embedding_identity="synthetic-identity")
    assert plan["source_count"] == 2
    assert len(plan["framework"]["commit"]) == 40
    assert plan["provider_calls"] == 0
    assert plan["cost_preview"]["index_reservation_usd"] is None
    assert plan["runtime_verified"] is False
    assert not list(tmp_path.iterdir())


def test_corpus_identity_includes_text_coverage_and_titles():
    rows = corpus()
    _, before = validate_corpus(rows)
    assert before == validate_corpus(list(reversed(rows)))[1]
    rows[0]["coverage"] = {"status": "unknown"}
    assert before != validate_corpus(rows)[1]
    rows[0]["text"] += "stale"
    with pytest.raises(ComparisonError, match="hash mismatch"):
        validate_corpus(rows)
    with pytest.raises(ComparisonError, match="unique"):
        validate_corpus([corpus()[0], corpus()[0]])


def test_grounding_requires_literal_evidence_not_only_source_name():
    rows = corpus()
    identity = validate_corpus(rows)[1]
    packet = normalize_evidence([
        NativeHit("chunk-a", "Institutions condition peace.", ("a",)),
        NativeHit("fact-b", "Institutions always cause peace", ("a",), kind="fact"),
        NativeHit("unknown", "Unsupported speculation", (), kind="summary"),
    ], rows, framework="graphiti", corpus_sha256=identity)
    assert [n["source_id"] for n in packet["notes"]] == ["a"]
    assert [r["grounding_status"] for r in packet["grounding_failures"]] == ["source_trace_only", "untraceable"]
    assert packet["original_publication_verified"] is False
    assert packet["notes"][0]["coverage"]["status"] == "abstract"


def test_invalid_quote_and_wrong_corpus_cannot_be_grounded():
    rows = corpus()
    identity = validate_corpus(rows)[1]
    packet = normalize_evidence([NativeHit("bad", "fact", ("a",), (("a", "invented quotation"),))],
                                rows, framework="lightrag", corpus_sha256=identity)
    assert not packet["notes"]
    assert packet["grounding_failures"][0]["grounding_status"] == "invalid_attribution_or_quote"
    with pytest.raises(ComparisonError, match="identity mismatch"):
        normalize_evidence([], rows, framework="lightrag", corpus_sha256="0" * 64)


def test_ambiguous_exact_passage_does_not_invent_identity():
    rows = corpus()
    rows[1].update(text=rows[0]["text"], content_sha256=rows[0]["content_sha256"])
    packet = normalize_evidence([NativeHit("ambiguous", rows[0]["text"])], rows,
                               framework="hipporag2", corpus_sha256=validate_corpus(rows)[1])
    assert not packet["notes"]
    assert packet["grounding_failures"][0]["grounding_status"] == "untraceable"


def test_mixed_unknown_identity_rejects_even_valid_quote():
    rows = corpus()
    packet = normalize_evidence([NativeHit("mixed", rows[0]["text"], ("a", "unknown"),
                                         (("a", rows[0]["text"]),))], rows,
                                framework="lightrag", corpus_sha256=validate_corpus(rows)[1])
    assert packet["notes"] == []
    assert packet["grounding_failures"] == [{"native_id": "mixed", "grounding_status": "invalid_attribution_or_quote"}]


def test_evidence_budgets_and_deduplication():
    rows = corpus()
    hits = [NativeHit(str(i), row["text"], (row["source_id"],)) for i, row in enumerate(rows * 3)]
    packet = normalize_evidence(hits, rows, framework="lightrag",
                               corpus_sha256=validate_corpus(rows)[1], max_notes=1, max_chars=1000)
    assert len(packet["notes"]) == 1
    assert packet["chars"] <= 1000
    assert packet["truncated"]


def test_stage_bound_uses_explicit_rates_and_output_caps():
    assert make_bound().reservation() == Decimal("0.12")
    with pytest.raises(ComparisonError):
        make_bound(max_calls=-1).reservation()
    with pytest.raises(ComparisonError):
        make_bound(input_usd_per_token="NaN").reservation()
    with pytest.raises(ComparisonError):
        make_bound(stage="unbounded").reservation()


def test_preview_prices_caps_without_authorizing_or_promising_completion():
    preview = preview_stage_bounds([make_bound(), make_bound("lightrag", max_calls=2)])
    assert Decimal(preview["aggregate_reservation_usd"]) == Decimal("0.36")
    assert preview["dispatch_approved"] is False
    assert preview["completion_guaranteed"] is False
    assert preview["stages"][0]["max_input_tokens"] == 100
    with pytest.raises(ComparisonError, match="distinct"):
        preview_stage_bounds([make_bound(), make_bound()])


def test_unknown_outcome_retains_reservation_and_retries_blocked(tmp_path):
    store = AggregateSpendLedger(tmp_path / "spend.sqlite3", approval_id="fixture-only", aggregate_usd="0.24",
                                 framework_caps={"graphiti": "0.24"})
    store.reserve("first", make_bound())
    store.mark_unknown("first")
    assert store.snapshot()["exposure_usd"] == "0.120"
    with pytest.raises(ComparisonError, match="already reserved"):
        store.reserve("first", make_bound())
    store.reserve("second", make_bound())
    with pytest.raises(ComparisonError, match="limit exceeded"):
        store.reserve("third", make_bound())
    store.settle("first", "0.04")
    assert Decimal(store.snapshot()["exposure_usd"]) == Decimal("0.16")
    with pytest.raises(ComparisonError):
        store.settle("second", "1")


def test_ledger_approval_cannot_be_silently_extended(tmp_path):
    path = tmp_path / "spend.sqlite3"
    AggregateSpendLedger(path, approval_id="fixture", aggregate_usd="0.12", framework_caps={"graphiti": "0.12"})
    with pytest.raises(ComparisonError, match="cannot be changed"):
        AggregateSpendLedger(path, approval_id="new-approval", aggregate_usd="1", framework_caps={"graphiti": "1"})


def test_concurrent_aggregate_and_framework_admission(tmp_path):
    store = AggregateSpendLedger(tmp_path / "spend.sqlite3", approval_id="fixture", aggregate_usd="0.12",
                                 framework_caps={"graphiti": "0.12"})
    def attempt(i):
        try:
            store.reserve(str(i), make_bound())
            return True
        except ComparisonError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1
    assert Decimal(store.snapshot()["exposure_usd"]) == Decimal("0.12")


class NativeFixture:
    def __init__(self):
        self.calls = []
    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"native": name}
        return call


def adapter(framework):
    native = NativeFixture()
    result = NativeFrameworkAdapter(framework, installed_commit=FRAMEWORKS[framework].commit,
                                    client=native, corpus_sha256=validate_corpus(corpus())[1])
    return result, native


@pytest.mark.parametrize("framework", list(FRAMEWORKS))
def test_native_operations_default_blocked_before_client_calls(framework):
    bridge, native = adapter(framework)
    with pytest.raises(ComparisonError, match="native dispatch unavailable"):
        asyncio.run(bridge.query("research", mode=FRAMEWORKS[framework].modes[0]))
    with pytest.raises(ComparisonError, match="native dispatch unavailable"):
        asyncio.run(bridge.build_index(corpus=corpus()))
    assert not native.calls


@pytest.mark.parametrize("framework,mode,method,kwargs", [
    ("graphrag", "local", "local_search", {"config": "fixture"}),
    ("graphrag", "global", "global_search", {"config": "fixture"}),
    ("lightrag", "mix", "aquery_data", {"param": SimpleNamespace(mode="mix")}),
    ("hipporag2", "native", "retrieve", {}),
    ("hipporag2", "dense", "retrieve_dpr", {}),
    ("graphiti", "hybrid", "search", {}),
    ("llamaindex", "property_graph", "retrieve", {}),
])
def test_native_query_uses_verified_entrypoint(framework, mode, method, kwargs):
    bridge, native = adapter(framework)
    result = asyncio.run(bridge.offline_replay_query("research", mode=mode, native_kwargs=kwargs,
                                                    recorded_response={"native": method}))
    assert result["native_result"] == {"native": method}
    assert result["native_calls"][0]["method"] == method
    assert result["provider_calls"] == 0
    assert not native.calls


def test_wrong_pin_and_query_override_rejected():
    with pytest.raises(ComparisonError, match="audited upstream"):
        NativeFrameworkAdapter("graphiti", installed_commit="main", client=None, corpus_sha256="a" * 64)
    bridge, native = adapter("graphiti")
    with pytest.raises(ComparisonError, match="cannot replace"):
        asyncio.run(bridge.offline_replay_query("research", mode="hybrid", native_kwargs={"query": "replacement"}, recorded_response={}))
    assert not native.calls


def test_indexing_binds_actual_documents():
    bridge, native = adapter("lightrag")
    result = asyncio.run(bridge.offline_replay_index(corpus=corpus(), recorded_response={}))
    assert result["native_calls"][0]["kwargs"]["ids"] == ["a", "b"]
    assert result["native_calls"][0]["args"][0] == [r["text"] for r in corpus()]
    assert not native.calls
    rows = corpus()
    rows[0]["coverage"] = {"status": "changed"}
    with pytest.raises(ComparisonError, match="corpus mismatch"):
        asyncio.run(bridge.offline_replay_index(corpus=rows, recorded_response={}))


def test_asserted_approval_booleans_cannot_bypass_missing_transport_instrumentation():
    bridge, native = adapter("graphiti")
    with pytest.raises(ComparisonError, match="native dispatch unavailable"):
        asyncio.run(bridge.query("research", mode="hybrid", dispatch_approved=True,
                                 guarded_transports_verified=True))
    assert not native.calls


def test_graphiti_native_fact_never_masquerades_as_literal_evidence():
    hits = extract_native_hits("graphiti", [SimpleNamespace(uuid="e1", fact="inferred claim", episodes=["episode-a"])],
                               source_bindings={"episode-a": ["a"]})
    assert hits == [NativeHit("e1", "inferred claim", ("a",), kind="fact")]
    packet = normalize_evidence(hits, corpus(), framework="graphiti", corpus_sha256=validate_corpus(corpus())[1])
    assert packet["grounding_failures"][0]["grounding_status"] == "source_trace_only"


def test_graphiti_ingestion_requires_explicit_text_group_and_returned_uuid_binding():
    bridge, native = adapter("graphiti")
    with pytest.raises(ComparisonError, match="group"):
        asyncio.run(bridge.offline_replay_index(corpus=corpus(), recorded_response={},
                                                native_kwargs={"reference_time": "fixed"}))
    kwargs = {"reference_time": "fixed", "group_id": "synthetic-group", "source": "text"}
    with pytest.raises(ComparisonError, match="cannot replace"):
        asyncio.run(bridge.offline_replay_index(corpus=corpus(), recorded_response={},
                                                native_kwargs={**kwargs, "uuid": "preselected"}))
    recorded = asyncio.run(bridge.offline_replay_index(corpus=corpus(), recorded_response={}, native_kwargs=kwargs))
    assert len(recorded["native_calls"]) == 2
    assert not native.calls
    episodes = [SimpleNamespace(uuid="episode-" + r["source_id"], content=r["text"], source="text",
                                group_id="synthetic-group") for r in corpus()]
    bindings = bridge.bind_graphiti_ingestion([SimpleNamespace(episode=e) for e in episodes],
                                              corpus=corpus(), group_id="synthetic-group")
    assert set(bindings) == {"episode-a", "episode-b"}
    async def fetch(driver, ids):
        return [e for e in episodes if e.uuid in ids]
    result = asyncio.run(bridge.ground_graphiti_query(
        [SimpleNamespace(uuid="fact", fact="An unverified generated interpretation.", episodes=["episode-a"])],
        corpus=corpus(), driver=None, episode_bindings=bindings, fetch_episodes=fetch, query="peace"))
    assert result["notes"][0]["source_id"] == "a"
    assert result["notes"][0]["excerpt"] in corpus()[0]["text"]
    assert result["fact_verification"] == "unknown"
    assert result["native_query_temporal_filtering"] == "not_evaluated"
    assert not native.calls


def test_structured_retrieval_normalizers_do_not_read_generated_answers():
    light = extract_native_hits("lightrag", {"status": "success", "data": {"chunks": [
        {"chunk_id": "c1", "content": corpus()[0]["text"]}]}, "answer": "ignore generated answer"},
        source_bindings={"c1": ["a"]})
    graph = extract_native_hits("graphrag", ("ignore generated answer", {"sources": [
        {"id": "c1", "text": corpus()[0]["text"]}]}), source_bindings={"c1": ["a"]})
    assert light == graph
