from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import pytest

from auto_zettelkasten.discovery import DiscoveryCache, scan_document
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.local_tools import load_cached_mapping_discovery, load_local_retrieval
from auto_zettelkasten.mapping_candidates import prepare_mapping_candidates
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.profiles import save_profile
from auto_zettelkasten.retrieval import LocalRetrievalIndex


def row(sid: str, thesis: str = "", collection: str = "one") -> dict:
    return {"source_id": sid, "note_id": "note-" + sid, "title": sid, "thesis": thesis,
            "collection_keys": [collection], "source_scope": "full_document", "evidence_eligibility": "substantive_bounded"}


def neighbor(source: str, target: str, score: float = 0.8) -> dict:
    return {"source_id": source, "target_source_id": target, "score": score,
            "provenance": {"embedding_identity": "synthetic-encoder", "source_chunk_ids": [source + "-chunk"],
                           "target_chunk_ids": [target + "-chunk"]}}


def pair_ids(pairs: list[dict]) -> list[tuple[str, str]]:
    return [(pair["left_source_id"], pair["right_source_id"]) for pair in pairs]


def workspace(root: Path, *, scoped: bool = False) -> list[dict]:
    rows = []
    for sid, thesis in (("A", "rareanchor"), ("B", "comparativecase"), ("C", "differentmethod")):
        profile = EvidenceProfile(source_id=sid, note_id="note-" + sid, note_hash=sid.lower() * 64,
                                  context={"thesis": thesis}, methods=[], concepts=[])
        save_profile(root / "02_source_memory/profiles", profile)
        entry = row(sid, thesis, sid)
        entry.update(zotero_key="ITEM000" + str(len(rows) + 1), author="Smith", year="2020")
        if scoped:
            entry["identity"] = {"instance_id": "synthetic-desktop", "library_type": "user", "library_id": "0",
                                 "item_key": entry["zotero_key"]}
        rows.append(entry)
        note_path = root / "notes" / (profile.note_id + ".md")
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("Synthetic source note " + sid)
    write_yaml(root / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    return rows


def cache_references(root: Path, rows: list[dict], *, instance: str = "synthetic-desktop") -> None:
    items = [{"key": entry["zotero_key"], "data": {"key": entry["zotero_key"], "title": entry["title"],
              "creators": [{"lastName": "Smith"}], "date": "2020", "itemType": "journalArticle",
              "DOI": "10.1000/fusion-" + entry["source_id"].lower()}} for entry in rows]
    identities = [{"instance_id": instance, "library_type": "user", "library_id": "0", "item_key": item["key"]} for item in items]
    library = [{"identity": identity, "item": item} for identity, item in zip(identities, items)]
    cache = DiscoveryCache(root / ".auto-zettelkasten/discovery.sqlite3")
    for entry, item, identity in zip(rows, items, identities):
        text = "Synthetic body.\nReferences\nSmith (2020). B. doi:10.1000/fusion-b.\n" if entry["source_id"] == "A" else "Synthetic body."
        scan_document(item, identity=identity, text=text,
                      coverage={"status": "succeeded", "source_scope": "full_document", "coverage_gate": "passed"},
                      source_binding={"source_id": entry["source_id"], "note_id": entry["note_id"], "zotero_item_key": item["key"]},
                      library_items=library, cache=cache)


def test_alternates_semantic_and_local_rankings_without_combining_scores():
    index = LocalRetrievalIndex([row("A", "rareterm"), row("B"), row("C", "rareterm"), row("D")])
    pairs = index.fused_candidate_pairs([neighbor("A", "B", 0.9), neighbor("A", "D", 0.7)],
                                        source_ids=["A"], per_source_cap=3)
    assert pair_ids(pairs) == [("A", "B"), ("A", "C"), ("A", "D")]
    assert pairs[0]["semantic_score"] == 0.9 and pairs[0]["local_score"] == 0
    assert "semantic_score" not in pairs[1] and pairs[1]["local_score"] > 0
    assert all(pair["decision_status"] == "unexamined" for pair in pairs)
    assert [pair["rank"] for pair in pairs] == [1, 2, 3]


def test_inverse_neighbors_are_one_pair_and_material_does_not_include_score():
    index = LocalRetrievalIndex([row("A", "sharedthesis"), row("B", "sharedthesis")])
    pairs = index.fused_candidate_pairs([neighbor("A", "B", 0.6), neighbor("B", "A", 0.9)])
    assert pair_ids(pairs) == [("A", "B")]
    assert pairs[0]["semantic_score"] == 0.9
    assert set(pairs[0]["candidate_routes"]) == {"semantic_similarity", "local_reference_tag_profile"}
    proof = pairs[0]["discovery_provenance"]
    assert any(value["signal"] == "semantic_similarity" for value in proof)
    changed = index.fused_candidate_pairs([neighbor("A", "B", 0.8), neighbor("B", "A", 0.7)])
    assert changed[0]["discovery_provenance"] == proof
    assert "semantic_score" not in json.dumps(proof) and '"score"' not in json.dumps(proof)


def test_resolved_pairs_are_removed_before_fanout_and_endpoint_caps():
    index = LocalRetrievalIndex([row(sid, "sharedthesis") for sid in "ABCD"])
    pairs = index.fused_candidate_pairs([neighbor("A", "B", 0.99), neighbor("A", "C", 0.9)],
                                        source_ids=["A"], resolved_pairs=[("B", "A")],
                                        max_pairs=1, fanout=1, per_source_cap=1)
    assert len(pairs) == 1 and pair_ids(pairs)[0] != ("A", "B")
    assert index.last_candidate_stats["resolved_pair_count"] == 1


def test_sparse_sources_and_cross_community_connections_remain_eligible():
    index = LocalRetrievalIndex([row("A", collection="large"), row("B", collection="large"),
                                 row("Z", collection="small")])
    pairs = index.fused_candidate_pairs([neighbor("A", "B", 0.99), neighbor("A", "Z", 0.5)],
                                        per_source_cap=1, max_pairs=1)
    assert pair_ids(pairs) == [("A", "Z")]
    assert index.last_candidate_stats["unexamined_candidate_pairs"] == 1
    assert index.last_candidate_stats["selection_truncated"] == 1


def test_caps_bound_each_endpoint_and_total_merged_fanout():
    rows = [row(f"S{number:03}", "commonterm", str(number)) for number in range(120)]
    edges = [neighbor(rows[0]["source_id"], other["source_id"], 0.5) for other in rows[1:]]
    index = LocalRetrievalIndex(rows)
    pairs = index.fused_candidate_pairs(edges, per_source_cap=2, max_pairs=15, fanout=5)
    assert len(pairs) <= 15 and len(set(pair_ids(pairs))) == len(pairs)
    assert max(Counter(sid for pair in pair_ids(pairs) for sid in pair).values()) <= 2
    assert index.last_candidate_stats["max_source_fanout"] <= 5
    assert index.last_candidate_stats["scored_pairs"] <= len(rows) * 5
    assert pairs == LocalRetrievalIndex(list(reversed(rows))).fused_candidate_pairs(list(reversed(edges)), per_source_cap=2, max_pairs=15, fanout=5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_invalid_semantic_scores_are_rejected(bad):
    with pytest.raises(ValueError, match="similarity"):
        LocalRetrievalIndex([row("A"), row("B")]).fused_candidate_pairs([neighbor("A", "B", bad)])


def test_missing_profiles_are_explicit_and_unknown_semantic_sources_are_not_invented(tmp_path):
    rows = workspace(tmp_path)
    (tmp_path / "02_source_memory/profiles/note-C.yml").unlink()
    result = prepare_mapping_candidates(tmp_path, [neighbor("A", "C"), neighbor("A", "B")])
    assert pair_ids(result["candidates"]) == [("A", "B")]
    assert result["stats"]["unknown_semantic_endpoints"] == 1
    assert result["status"]["missing_profile_count"] == 1
    assert result["status"]["all_pairs_examined"] is False
    assert result["status"]["provider_calls"] == 0
    assert len(result["rows"]) == len(rows) - 1


def test_cache_enrichment_uses_declared_scope_and_preserves_source_files(tmp_path):
    rows = workspace(tmp_path, scoped=True)
    cache_references(tmp_path, rows)
    before = {str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.rglob("*") if path.is_file()}
    result = prepare_mapping_candidates(tmp_path, [], max_pairs=1)
    assert pair_ids(result["candidates"]) == [("A", "B")]
    proof = result["candidates"][0]["discovery_provenance"]
    assert any(value["signal"] == "direct_reference" for value in proof)
    assert result["status"]["scope_status"] == "declared_source_identity"
    assert result["status"]["cached_document_count"] == 3
    assert result["status"]["reference_coverage"] == "cached_unverified"
    enriched = load_cached_mapping_discovery(tmp_path, rows)
    assert len(enriched["records"]) == 3 and enriched["status"]["acquisition_performed"] is False
    assert {str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.rglob("*") if path.is_file()} == before


def test_cache_scope_is_not_guessed_from_cached_records(tmp_path):
    rows = workspace(tmp_path)
    cache_references(tmp_path, rows)
    result = prepare_mapping_candidates(tmp_path, [])
    assert result["status"]["scope_status"] == "unknown"
    assert result["status"]["cached_document_count"] == 0
    assert all(value["signal"] != "direct_reference" for pair in result["candidates"] for value in pair["discovery_provenance"])
    assert prepare_mapping_candidates(tmp_path, [], instance_id="synthetic-desktop")["status"]["cached_document_count"] == 3
    assert prepare_mapping_candidates(tmp_path, [], instance_id="different-desktop")["status"]["cached_document_count"] == 0


def test_mapping_cache_projection_must_match_canonical_note_identity(tmp_path):
    rows = workspace(tmp_path, scoped=True)
    rows[0]["note_id"] = "some-other-note"
    with pytest.raises(ValueError, match="source/note identity"):
        load_cached_mapping_discovery(tmp_path, rows)


def test_matched_discussed_works_are_candidate_signals_without_another_model_or_scope(tmp_path):
    workspace(tmp_path)
    write_yaml(tmp_path / "02_source_memory/indexes/literature_positions.yml", {"positions": [
        {"literature_position_id": "position-A-B", "current_source_id": "A", "matched_source_id": "B",
         "match_status": "mapped", "locator": "Thesis paragraph 2", "relation_label": "undermines"},
        {"literature_position_id": "position-C-B", "current_source_id": "C", "matched_source_id": "B", "match_status": "mapped"},
    ]})
    result = prepare_mapping_candidates(tmp_path, [])
    pairs = {pair: item for pair, item in zip(pair_ids(result["candidates"]), result["candidates"])}
    assert set(pairs) == {("A", "B"), ("B", "C"), ("A", "C")}
    direct = next(value for value in pairs[("A", "B")]["discovery_provenance"] if value["signal"] == "direct_reference")
    assert direct["literature_position_id"] == "position-A-B" and direct["freshness"] == "recorded_unverified"
    assert any(value["signal"] == "shared_reference" for value in pairs[("A", "C")]["discovery_provenance"])
    assert result["status"]["unverified_discussed_work_count"] == 2
    assert result["status"]["reference_coverage"] == "not_examined"
    assert "undermines" not in json.dumps(result["citation_records"])
    assert all(pair["decision_status"] == "unexamined" for pair in pairs.values())


def test_stale_or_unresolved_discussed_works_do_not_propose_citations(tmp_path):
    workspace(tmp_path)
    write_yaml(tmp_path / "02_source_memory/indexes/literature_positions.yml", {"positions": [
        {"current_source_id": "A", "matched_source_id": "B", "match_status": "mapped", "source_profile_hash": "obsolete"},
        {"current_source_id": "A", "matched_source_id": "C", "match_status": "ambiguous"},
        {"current_source_id": "C", "matched_source_id": "not-in-catalogue", "match_status": "mapped"},
    ]})
    result = prepare_mapping_candidates(tmp_path, [])
    assert result["citation_records"] == []
    assert result["status"]["stale_discussed_work_count"] == 1
    assert result["status"]["unresolved_discussed_work_count"] == 2


def test_recall_preserves_numeric_decision_confidence_and_evidence():
    index = LocalRetrievalIndex([row("A", "queryterm"), row("B")])
    result = index.recall("queryterm", relationships=[{"source_id": "A", "target_source_id": "B", "active": True,
                        "decision_status": "accepted", "relation_type": "qualifies", "confidence": 0.9,
                        "winning_probability": 0.91, "probability_margin": 0.85,
                        "selected_evidence": [{"passage_id": "A1", "text": "A bounded supplied note excerpt."}],
                        "decision_policy": "experimental-private-v1"}], max_notes=2)
    relation = result["relationships"][0]
    assert relation["confidence"] == 0.9 and relation["winning_probability"] == 0.91
    assert relation["probability_margin"] == 0.85
    assert relation["selected_evidence"][0]["passage_id"] == "A1"
    assert relation["decision_policy"] == "experimental-private-v1"


def test_stored_subject_tags_are_candidates_without_scanner_or_new_calls(tmp_path):
    rows = workspace(tmp_path)
    for entry in rows[:2]:
        entry["facets_by_type"] = {"subject": ["Specific shared subject"]}
    write_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    result = prepare_mapping_candidates(tmp_path, [])
    candidate = next(pair for pair in result["candidates"] if (pair["left_source_id"], pair["right_source_id"]) == ("A", "B"))
    assert any(proof["signal"] == "shared_tag" and proof["tag"] == "specific shared subject" for proof in candidate["discovery_provenance"])
    assert result["status"]["cached_document_count"] == 0
    assert result["status"]["provider_calls"] == 0


def test_current_discussed_work_hashes_and_cache_helper_coverage(tmp_path):
    rows = workspace(tmp_path)
    loaded = load_local_retrieval(tmp_path)
    by_source = {value["source_id"]: value for value in loaded["rows"]}
    write_yaml(tmp_path / "02_source_memory/indexes/literature_positions.yml", {"positions": [
        {"current_source_id": "A", "matched_source_id": "B", "match_status": "mapped",
         "source_profile_hash": by_source["A"]["profile_hash"], "target_note_hash": by_source["B"]["note_hash"]},
    ]})
    enriched = load_cached_mapping_discovery(tmp_path, loaded["rows"])
    assert enriched["records"] == []
    assert enriched["citation_records"][0]["freshness"] == "current_declared_hashes"
    assert enriched["status"]["matched_discussed_work_count"] == 1
    assert enriched["status"]["unverified_discussed_work_count"] == 0
    assert enriched["status"]["scope_status"] == "unknown"


def test_ambiguous_declared_cache_scope_stays_unexamined(tmp_path):
    rows = workspace(tmp_path, scoped=True)
    rows[1]["identity"]["instance_id"] = "another-desktop"
    write_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    cache_references(tmp_path, rows)
    result = prepare_mapping_candidates(tmp_path, [])
    assert result["status"]["scope_status"] == "ambiguous"
    assert result["status"]["cached_document_count"] == 0


@pytest.mark.parametrize("options", [{"fanout": 257}, {"per_source_cap": 257}, {"max_pairs": 100001},
                                     {"source_ids": "A"}, {"source_ids": [["A"]]}, {"resolved_pairs": [("A", "A")]}])
def test_fused_selection_rejects_invalid_bounds_and_identities(options):
    with pytest.raises(ValueError):
        LocalRetrievalIndex([row("A"), row("B")]).fused_candidate_pairs([], **options)


def test_prepared_candidates_reuse_loaded_snapshot_without_loading_again(tmp_path, monkeypatch):
    workspace(tmp_path, scoped=True)
    loaded = load_local_retrieval(tmp_path, infer_scope=True, library_id=None)
    monkeypatch.setattr("auto_zettelkasten.mapping_candidates.load_local_retrieval",
                        lambda *args, **kwargs: pytest.fail("canonical profiles were loaded twice"))
    result = prepare_mapping_candidates(tmp_path, [neighbor("A", "B")], loaded=loaded)
    assert ("A", "B") in pair_ids(result["candidates"])
    assert result["rows"] is loaded["rows"]
    assert "index" not in result and "loaded" not in result
    json.dumps(result)


def test_reused_snapshot_rejects_conflicting_scope_or_index_binding(tmp_path):
    workspace(tmp_path, scoped=True)
    loaded = load_local_retrieval(tmp_path, infer_scope=True, library_id=None)
    with pytest.raises(ValueError, match="library scope"):
        prepare_mapping_candidates(tmp_path, [], loaded=loaded, instance_id="different-desktop")
    other = tmp_path / "other-workspace"
    other.mkdir()
    with pytest.raises(ValueError, match="canonical local retrieval snapshot"):
        prepare_mapping_candidates(other, [], loaded=loaded)
    loaded["rows"][0]["note_id"] = "wrong-note"
    with pytest.raises(ValueError, match="canonical retrieval index"):
        prepare_mapping_candidates(tmp_path, [], loaded=loaded)


def test_calibrated_first_allocation_matches_evaluated_selector_then_reaches_broad_pool(tmp_path):
    from auto_zettelkasten.candidate_queue import CandidateQueue, queue_scope
    rows = workspace(tmp_path)
    for entry in rows:
        entry["author"] = entry["source_id"]
    write_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    loaded = load_local_retrieval(tmp_path, infer_scope=True)
    broad = [neighbor("A", "B", .99), neighbor("A", "C", .8)]
    calibrated = [neighbor("A", "C", .8)]
    expected = loaded["index"].fused_candidate_pairs(calibrated, per_source_cap=8, max_pairs=256, fanout=64)
    opts = {"loaded": loaded, "persist_queue": True, "decision_contract": {"test": "v1"},
            "preferred_semantic_neighbors": calibrated, "fanout": 64}
    result = prepare_mapping_candidates(tmp_path, broad, **opts)
    assert pair_ids(result["candidates"]) == pair_ids(expected) == [("A", "C")]
    assert result["queue"]["total"] == 2 and result["queue"]["unfinished"]
    q = CandidateQueue(tmp_path, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), None))
    q.record(result["candidates"][0], "negative")
    q.finish_pass()
    second = prepare_mapping_candidates(tmp_path, broad, **opts)
    assert pair_ids(second["candidates"]) == [("A", "B")]
    assert second["queue"]["counts"]["negative"] == 1


def test_empty_calibrated_shortlist_does_not_drop_broad_candidates(tmp_path):
    from auto_zettelkasten.candidate_queue import CandidateQueue, queue_scope
    rows = workspace(tmp_path)
    for entry in rows:
        entry["author"] = entry["source_id"]
    write_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    loaded = load_local_retrieval(tmp_path, infer_scope=True)
    opts = {"loaded": loaded, "persist_queue": True, "decision_contract": {"test": "v1"},
            "preferred_semantic_neighbors": [], "fanout": 64}
    broad = [neighbor("A", "B", .99)]
    result = prepare_mapping_candidates(tmp_path, broad, **opts)
    assert result["candidates"] == [] and result["queue"]["unexamined_count"] == 1
    q = CandidateQueue(tmp_path, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), None))
    q.finish_pass()
    assert pair_ids(prepare_mapping_candidates(tmp_path, broad, **opts)["candidates"]) == [("A", "B")]


def test_default_recall_reserves_typed_edge_and_its_endpoints_before_long_excerpts():
    records = [row(f"S{index}", "sharedquery " + ("Long evidence with quoted \\\"passages\\\". " * 200)) for index in range(10)]
    for record in records:
        record["method"] = "Detailed methodology. " * 100
    index = LocalRetrievalIndex(records)
    edge = {"source_id": "S0", "target_source_id": "S9", "active": True,
            "decision_status": "accepted", "relation_type": "qualifies", "confidence": .73,
            "winning_probability": .61, "probability_margin": .12,
            "selected_evidence": [{"passage_id": "S0-thesis", "text": "Bounded supplied evidence. " * 25}],
            "decision_policy": "synthetic-winner-policy"}
    result = index.recall("sharedquery", relationships=[edge])
    assert len(result["relationships"]) == 1
    assert {"S0", "S9"} <= {record["source_id"] for record in result["notes"]}
    relation = result["relationships"][0]
    assert relation["confidence"] == .73 and relation["winning_probability"] == .61
    assert relation["selected_evidence"][0]["passage_id"] == "S0-thesis"
    assert len(result["notes"]) <= 8 and result["chars"] <= 12000
    encoded_chars = sum(len(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
                        for value in [*result["notes"], *result["relationships"]])
    assert encoded_chars == result["chars"]


def test_recall_does_not_force_unaffordable_edge_or_unbounded_endpoint_metadata():
    records = [row("A", "queryterm"), row("B")]
    index = LocalRetrievalIndex(records)
    result = index.recall("queryterm", max_chars=1000, relationships=[
        {"source_id": "A", "target_source_id": "B", "decision_status": "accepted", "relation_type": "qualifies",
         "selected_evidence": [{"passage_id": "A-thesis", "text": "Evidence " * 1000}]}])
    assert result["chars"] <= 1000
    assert result["truncated"]
