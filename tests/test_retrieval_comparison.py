from __future__ import annotations

import copy
import hashlib
import json
import math

import pytest

from auto_zettelkasten.retrieval import LocalRetrievalIndex
from auto_zettelkasten.retrieval_comparison import (
    ComparisonIndex, reciprocal_rank_fusion, validate_vector_compatibility,
)


def row(sid, text="mediation evidence", **kwargs):
    return {"source_id": sid, "title": sid, "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "coverage": {"status": "complete"}, **kwargs}


def vectors(rows):
    return {raw["source_id"]: [1.0, 0.0] for raw in rows}


def edge(left, right, **kwargs):
    return {"source_id": left, "target_source_id": right, **kwargs}


def manifest(rows):
    return {"model_assets": "asset-hash", "tokenizer": "tokenizer-hash",
            "libraries": {"encoder": "synthetic-1"}, "cleaner_version": "synthetic-cleaner",
            "chunking_version": "synthetic-chunks", "aggregation": "mean",
            "normalization": "l2", "dimension": 2,
            "source_hashes": {raw["source_id"]: raw["content_sha256"] for raw in rows}}


def test_common_text_search_reaches_evidence_beyond_compact_profiles_and_is_deterministic():
    rows = [row("A", "institution " * 800 + "rareclaim"), row("B", "institution"), row("C", "unrelated")]
    a = ComparisonIndex(rows).recall("rareclaim", variant="lexical")
    b = ComparisonIndex(list(reversed(rows))).recall("rareclaim", variant="lexical")
    assert a == b
    assert [note["source_id"] for note in a["notes"]] == ["A"]
    assert "rareclaim" in a["notes"][0]["text"]
    assert a["notes"][0]["excerpt_start"] > 0


def test_dense_semantic_control_can_find_notes_without_word_overlap():
    rows = [row("A", "mediation bargain"), row("B", "insects habitats")]
    result = ComparisonIndex(rows, {"A": [0.0, 1.0], "B": [1.0, 0.0]}).recall(
        "conflict settlement", query_vector=[0.0, 1.0], variant="dense")
    assert result["notes"][0]["source_id"] == "A"
    assert result["provenance"]["scores_are_probabilities"] is False


def test_rrf_combines_ranks_with_stable_identity_ties_and_no_raw_score_mixing():
    assert reciprocal_rank_fusion([["B", "A"], ["A", "B"]]) == [
        ("A", 1 / 61 + 1 / 62), ("B", 1 / 61 + 1 / 62)]
    with pytest.raises(ValueError, match="duplicate"):
        reciprocal_rank_fusion([["A", "A"]])


def test_hybrid_limits_each_direct_route_to_32_candidates():
    rows = [row(f"S{i:03}", "mediation") for i in range(100)]
    result = ComparisonIndex(rows, vectors(rows)).recall("mediation", query_vector=[1, 0], variant="hybrid", max_notes=128, max_chars=100_000)
    assert len(result["notes"]) == 32
    assert result["truncated"] is True


def test_graph_expansion_keeps_cross_community_neighbor_and_query_orders_candidates():
    rows = [row(f"A{i}", "mediation") for i in range(6)] + [row("Znear", "settlement"), row("Bfar", "unrelated")]
    vecs = {raw["source_id"]: [1, 0] for raw in rows}
    vecs["Znear"], vecs["Bfar"] = [0.8, 0.6], [-1, 0]
    index = ComparisonIndex(rows, vecs, semantic_edges=[edge("A0", "Znear", score=0.8), edge("A0", "Bfar", score=0.9)])
    result = index.recall("mediation", query_vector=[1, 0], variant="graph", max_notes=7)
    notes = result["notes"]
    assert notes[5]["source_id"] == "Znear"
    assert notes[5]["recall_route"] == "graph_neighbor"
    assert all(link["layer"] == "semantic" for link in result["relationships"])


def test_typed_and_untyped_have_identical_endpoints_claim_evidence_and_excerpt_budgets():
    text_a = "Mediation results depend on consent. " * 200
    text_b = "Consent may be negotiated rather than fixed. " * 200
    rows = [row("A", text_a), row("B", text_b)]
    relation = edge("B", "A", status="accepted", relation_type="qualifies",
                    reason="B qualifies A because of consent",
                    source_content_sha256=rows[1]["content_sha256"],
                    target_content_sha256=rows[0]["content_sha256"],
                    evidence=[{"source_id": "B", "text": "Consent may be negotiated rather than fixed."}])
    index = ComparisonIndex(rows, vectors(rows), accepted_edges=[relation])
    masked = index.recall("consent", query_vector=[1, 0], variant="untyped", max_chars=2300)
    typed = index.recall("consent", query_vector=[1, 0], variant="typed", max_chars=2300)
    assert masked["notes"] == typed["notes"]
    assert masked["relationships"][0]["evidence"] == typed["relationships"][0]["evidence"]
    assert masked["relationships"][0]["source_ids"] == ["A", "B"]
    assert masked["relationships"][0]["label"] is None
    assert "qualifies" not in json.dumps(masked)
    assert "reason" not in json.dumps(typed)
    assert typed["relationships"][0]["label"] == {"source_id": "B", "target_source_id": "A", "relation_type": "qualifies"}
    assert typed["chars"] <= 2300 and masked["chars"] <= 2300
    assert masked["provenance"]["reserved_chars"] == typed["provenance"]["reserved_chars"]


def test_reference_origin_does_not_invent_verified_citation_and_accepted_not_in_graph_control():
    rows = [row("A"), row("B")]
    index = ComparisonIndex(rows, vectors(rows), reference_edges=[edge("A", "B", reference_origin="discussed_work_memory")],
                            accepted_edges=[edge("A", "B", status="accepted", relation_type="supports")])
    result = index.recall("mediation", query_vector=[1, 0], variant="graph")
    assert [link["layer"] for link in result["relationships"]] == ["reference"]
    assert result["relationships"][0]["reference_origin"] == "discussed_work_memory"


@pytest.mark.parametrize("change", ["duplicate", "hash", "metadata", "unknown_edge", "stale_edge", "fake_quote", "other_quote_endpoint"])
def test_input_boundaries_reject_fabrication_and_stale_sources(change):
    rows = [row("A"), row("B")]
    edges = []
    if change == "duplicate":
        rows.append(rows[0])
    elif change == "hash":
        rows[0]["content_sha256"] = "0" * 64
    elif change == "metadata":
        rows[0]["coverage"] = {"source_scope": "metadata_only"}
    elif change == "unknown_edge":
        edges = [edge("A", "outside")]
    elif change == "stale_edge":
        edges = [edge("A", "B", source_content_sha256="0" * 64)]
    elif change == "fake_quote":
        edges = [edge("A", "B", evidence=[{"source_id": "A", "text": "imaginary evidence"}])]
    elif change == "other_quote_endpoint":
        edges = [edge("A", "B", evidence=[{"source_id": "outside", "text": "mediation"}])]
    with pytest.raises(ValueError):
        ComparisonIndex(rows, semantic_edges=edges)


@pytest.mark.parametrize("value", [[0, 0], [math.nan, 0], [math.inf, 0], [True, 0], [2, 0]])
def test_invalid_vectors_are_rejected(value):
    with pytest.raises(ValueError):
        ComparisonIndex([row("A")], {"A": value})


def test_missing_source_or_query_vectors_and_dimension_mismatches_fail_explicitly():
    with pytest.raises(ValueError, match="exactly"):
        ComparisonIndex([row("A"), row("B")], {"A": [1, 0]})
    index = ComparisonIndex([row("A")], {"A": [1, 0]})
    with pytest.raises(ValueError, match="requires"):
        index.recall("mediation", variant="dense")
    with pytest.raises(ValueError, match="dimension"):
        index.recall("mediation", query_vector=[1, 0, 0], variant="dense")
    with pytest.raises(ValueError, match="query"):
        index.recall("q" * 2001, query_vector=[1, 0], variant="dense")


@pytest.mark.parametrize("field", ["model_assets", "tokenizer", "libraries", "cleaner_version", "chunking_version", "aggregation", "normalization", "source_hashes"])
def test_equal_dimensions_do_not_establish_vector_compatibility(field):
    rows = [row("A")]
    actual, expected = manifest(rows), manifest(rows)
    actual[field] = "incompatible"
    with pytest.raises(ValueError):
        validate_vector_compatibility(actual, expected, {"A": rows[0]["content_sha256"]})


def test_manifest_and_input_copies_are_not_mutated():
    rows = [row("A")]
    original = copy.deepcopy(rows)
    identity = manifest(rows)
    index = ComparisonIndex(rows, vectors(rows), vector_manifest=identity, expected_vector_manifest=identity)
    result = index.recall("mediation", query_vector=[1, 0])
    assert result["provenance"]["vector_identity_verified"] is True
    result["notes"][0]["coverage"]["status"] = "changed caller copy"
    assert rows == original
    assert index.rows["A"]["coverage"]["status"] == "complete"


def test_inactive_pending_and_rejected_edges_do_not_become_graph_links():
    rows = [row("A"), row("B")]
    accepted = [edge("A", "B", status="rejected", relation_type="supports"),
                edge("A", "B", status="accepted", relation_type="supports", active=False),
                edge("A", "B", status="accepted", relation_type="supports", refresh_pending=True)]
    result = ComparisonIndex(rows, vectors(rows), accepted_edges=accepted).recall("mediation", query_vector=[1, 0], variant="typed")
    assert result["relationships"] == []


def test_legacy_variant_matches_unchanged_production_route():
    rows = [row("A")]
    legacy = LocalRetrievalIndex([{"source_id": "A", "title": "A", "thesis": "mediation"}])
    expected = legacy.recall("mediation", max_notes=8, max_chars=12000)
    result = ComparisonIndex(rows).recall("mediation", variant="legacy", legacy_index=legacy)
    assert {key: result[key] for key in expected} == expected


def test_legacy_route_cannot_silently_search_a_different_corpus():
    legacy = LocalRetrievalIndex([{"source_id": "outside", "title": "mediation"}])
    with pytest.raises(ValueError, match="outside the comparison corpus"):
        ComparisonIndex([row("A")]).recall("mediation", variant="legacy", legacy_index=legacy)


def test_edge_budget_cannot_force_packet_overflow_or_label_ablation_drift():
    rows = [row("A"), row("B")]
    accepted = [edge("A", "B", status="accepted", relation_type="qualifies")]
    index = ComparisonIndex(rows, vectors(rows), accepted_edges=accepted)
    for budget in (32, 200, 500, 1200):
        a = index.recall("mediation", query_vector=[1, 0], variant="untyped", max_chars=budget)
        b = index.recall("mediation", query_vector=[1, 0], variant="typed", max_chars=budget)
        assert a["notes"] == b["notes"]
        assert a["chars"] <= budget and b["chars"] <= budget
