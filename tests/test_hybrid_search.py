import hashlib
import json
import math

import pytest

from auto_zettelkasten.hybrid_search import HybridSearchIndex, reciprocal_rank_fusion
from auto_zettelkasten.retrieval_comparison import ComparisonIndex


def _row(sid, text, **extra):
    return {"source_id": sid, "title": sid, "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "coverage": {"source_scope": "note_only"}, **extra}


def _ids(packet):
    return [row["source_id"] for row in packet["notes"]]


def test_same_hybrid_ranking_and_evidence_as_common_text_engine():
    corpus = [_row("A", "mediation settlement conflict " * 120),
              _row("B", "election timing violence " * 120),
              _row("C", "mediation election conflict " * 120)]
    vectors = {"A": [1., 0.], "B": [0., 1.], "C": [math.sqrt(.5), math.sqrt(.5)]}
    production = HybridSearchIndex(corpus, vectors).recall("mediation election", query_vector=[1., 0.], max_chars=50_000)
    baseline = ComparisonIndex(corpus, vectors).recall("mediation election", query_vector=[1., 0.], max_chars=50_000)
    assert _ids(production) == _ids(baseline)
    for actual, expected in zip(production["notes"], baseline["notes"]):
        assert {k: v for k, v in actual.items() if k != "recall_route"} == {
            k: v for k, v in expected.items() if k != "recall_route"}
    assert production["relationships"] == []
    assert production["provenance"]["rrf_constant"] == 60
    assert production["provenance"]["candidate_limit_per_route"] == 32


def test_full_note_lexical_match_beyond_profile_token_limits():
    text = "background " * 3000 + "Distinctivequartz explains the mechanism."
    index = HybridSearchIndex([_row("deep", text), _row("other", "unrelated reading")])
    result = index.recall("distinctivequartz")
    assert _ids(result) == ["deep"]
    note = result["notes"][0]
    assert "Distinctivequartz" in note["text"]
    assert note["excerpt_start"] > 20_000
    assert text[note["excerpt_start"]:note["excerpt_end"]] == note["text"]
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["provenance"]["semantic_status"] == "no_compatible_vectors"


def test_semantic_route_finds_paraphrase_without_shared_query_words():
    corpus = [_row("ceasefire", "A negotiated ceasefire can stop fighting."),
              _row("farming", "Agriculture needs reliable rainfall.")]
    vectors = {"ceasefire": [1., 0.], "farming": [0., 1.]}
    result = HybridSearchIndex(corpus, vectors).recall("ending armed confrontation", query_vector=[1., 0.], max_notes=1)
    assert _ids(result) == ["ceasefire"]
    assert result["notes"][0]["recall_route"] == "semantic"
    assert result["provenance"]["route"] == "full_note_hybrid"
    assert result["provenance"]["scores_are_probabilities"] is False


def test_rrf_combines_routes_instead_of_comparing_incommensurate_scores():
    corpus = [_row("A", "needle"), _row("B", "unrelated wording"), _row("C", "needle urgent")]
    vectors = {"A": [0., 1.], "B": [1., 0.], "C": [.8, .6]}
    result = HybridSearchIndex(corpus, vectors).recall("needle urgent", query_vector=[1., 0.])
    assert _ids(result)[0] == "C"
    assert result["notes"][0]["recall_route"] == "hybrid"
    assert next(row for row in result["notes"] if row["source_id"] == "B")["recall_route"] == "semantic"


def test_partial_vectors_keep_unembedded_notes_in_lexical_route():
    corpus = [_row("missing", "needle"), _row("embedded", "different words")]
    result = HybridSearchIndex(corpus, {"embedded": [1., 0.]}).recall("needle", query_vector=[1., 0.])
    assert set(_ids(result)) == {"missing", "embedded"}
    assert result["provenance"]["vector_source_count"] == 1
    assert result["provenance"]["source_count"] == 2


def test_missing_query_vector_is_explicit_lexical_fallback():
    index = HybridSearchIndex([_row("A", "needle")], {"A": [1., 0.]})
    result = index.recall("needle")
    assert _ids(result) == ["A"]
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["provenance"]["semantic_status"] == "query_vector_unavailable"


def test_metadata_is_excluded_while_genuine_abstract_remains():
    corpus = [_row("metadata", "needle", coverage={"source_scope": "metadata-only"}),
              _row("abstract", "needle mechanism explained in this abstract", coverage={"source_scope": "abstract_only"})]
    result = HybridSearchIndex(corpus, {"metadata": [1., 0.], "abstract": [1., 0.]}).recall("needle", query_vector=[1., 0.])
    assert _ids(result) == ["abstract"]
    assert result["notes"][0]["coverage"]["source_scope"] == "abstract_only"
    assert result["provenance"]["excluded_metadata_count"] == 1


def test_deterministic_order_deduplication_and_top32_route_limit():
    corpus = [_row(f"S{i:03}", "same needle") for i in range(80)]
    vectors = {row["source_id"]: [1., 0.] for row in reversed(corpus)}
    first = HybridSearchIndex(corpus, vectors).recall("needle", query_vector=[1., 0.], max_notes=32, max_chars=64_000)
    second = HybridSearchIndex(list(reversed(corpus)), vectors).recall("needle", query_vector=[1., 0.], max_notes=32, max_chars=64_000)
    assert first == second
    assert _ids(first) == [f"S{i:03}" for i in range(32)]
    assert first["truncated"] is True


@pytest.mark.parametrize("budget", [32, 300, 600, 1200, 12_000])
def test_serialized_evidence_budget_includes_paths_unicode_and_never_empty_notes(budget):
    corpus = [_row(str(i), "needle é😀 " * 1000, note_path="notes/" + "long-" * 50 + str(i) + ".md", note_id="note-" + str(i)) for i in range(12)]
    result = HybridSearchIndex(corpus).recall("needle", max_chars=budget)
    size = len(json.dumps({"notes": result["notes"], "relationships": result["relationships"]},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert result["chars"] == size <= budget
    assert len(result["notes"]) <= 8
    for note in result["notes"]:
        assert 0 < len(note["text"]) <= 2000
        original = next(row for row in corpus if row["source_id"] == note["source_id"])
        assert note["text"] == original["text"][note["excerpt_start"]:note["excerpt_end"]]
        assert note["note_path"] == original["note_path"]


def test_empty_corpus_and_no_matches_return_bounded_empty_packets():
    assert HybridSearchIndex([]).recall("query")["notes"] == []
    assert HybridSearchIndex([_row("A", "other")]).recall("query")["notes"] == []
    with pytest.raises(ValueError, match="empty evidence packet"):
        HybridSearchIndex([]).recall("query", max_chars=1)


@pytest.mark.parametrize("query", [None, "", "   ", "bad\x00query", "q" * 2001])
def test_invalid_query_rejected(query):
    with pytest.raises(ValueError, match="query"):
        HybridSearchIndex([]).recall(query)


@pytest.mark.parametrize("kwargs", [{"max_notes": True}, {"max_notes": 0}, {"max_notes": 33},
                                   {"max_chars": False}, {"max_chars": 0}, {"max_chars": 64_001}])
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        HybridSearchIndex([]).recall("query", **kwargs)


@pytest.mark.parametrize("vector", [None, "not-vector", [], [0., 0.], [2., 0.], [float("nan"), 0.],
                                   [float("inf"), 0.], [True, False], {0: 1., 1: 0.}])
def test_invalid_source_vectors_rejected(vector):
    with pytest.raises(ValueError):
        HybridSearchIndex([_row("A", "needle")], {"A": vector})


def test_vector_identity_binding_dimensions_and_query_vector_checks():
    with pytest.raises(ValueError, match="bind"):
        HybridSearchIndex([_row("A", "needle")], {"unknown": [1., 0.]})
    with pytest.raises(ValueError, match="dimensions differ"):
        HybridSearchIndex([_row("A", "needle"), _row("B", "needle")], {"A": [1., 0.], "B": [1., 0., 0.]})
    index = HybridSearchIndex([_row("A", "needle")], {"A": [1., 0.]})
    with pytest.raises(ValueError, match="dimension differs"):
        index.recall("needle", query_vector=[1., 0., 0.])
    with pytest.raises(ValueError, match="finite real"):
        index.recall("needle", query_vector=[float("nan"), 0.])


def test_stale_hash_duplicate_identity_and_invalid_coverage_rejected():
    with pytest.raises(ValueError, match="hash"):
        HybridSearchIndex([_row("A", "needle", content_sha256="wrong")])
    with pytest.raises(ValueError, match="duplicate"):
        HybridSearchIndex([_row("A", "needle"), _row("A", "other")])
    with pytest.raises(ValueError, match="coverage"):
        HybridSearchIndex([_row("A", "needle", coverage=["not", "mapping"])])


def test_corpus_and_vectors_are_copied_not_mutated_or_lazily_read():
    row = _row("A", "needle", coverage={"source_scope": "note_only", "limitations": ["partial"]})
    vector = [1., 0.]
    index = HybridSearchIndex([row], {"A": vector})
    row["coverage"]["limitations"].append("changed")
    row["text"] = "changed"
    vector[0] = 0.
    result = index.recall("needle", query_vector=[1., 0.])
    assert result["notes"][0]["text"] == "needle"
    assert result["notes"][0]["coverage"]["limitations"] == ["partial"]


def test_reciprocal_rank_fusion_preserves_shared_historical_contract():
    assert reciprocal_rank_fusion([["B", "A"], ["A", "B"]]) == [("A", 1 / 61 + 1 / 62), ("B", 1 / 61 + 1 / 62)]
    with pytest.raises(ValueError, match="duplicate"):
        reciprocal_rank_fusion([["A", "A"]])


def test_corpus_source_and_utf8_note_resource_limits():
    with pytest.raises(ValueError, match="source limit"):
        HybridSearchIndex([{}] * 20_001)
    with pytest.raises(ValueError, match="note text byte limit"):
        HybridSearchIndex([_row("A", "é" * 1_100_000)])


def test_corpus_aggregate_byte_limit(monkeypatch):
    import auto_zettelkasten.hybrid_search as search

    monkeypatch.setattr(search, "MAX_CORPUS_BYTES", 15)
    with pytest.raises(ValueError, match="corpus text byte limit"):
        HybridSearchIndex([_row("A", "ten letters"), _row("B", "more text")])
