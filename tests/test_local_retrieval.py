from __future__ import annotations

from collections import Counter
import json

import pytest

from auto_zettelkasten.retrieval import LocalRetrievalIndex


def _row(sid: str, thesis: str = "", collection: str = "one", **kwargs):
    return {"source_id": sid, "title": sid, "thesis": thesis, "collection_keys": [collection], "source_scope": "full_document", "evidence_eligibility": "substantive_bounded", **kwargs}


def _record(sid: str, key: str, *, instance: str = "desktop", library: str = "42", references=(), tags=(), relations=None, coverage=None):
    return {"source_id": sid, "note_id": f"note-{sid}", "identity": {"instance_id": instance, "library_type": "user", "library_id": library, "item_key": key}, "input_fingerprint": "synthetic", "metadata": {"tags": list(tags), "relations": relations or {}}, "coverage": coverage or {"status": "complete", "source_scope": "full_document"}, "references": list(references), "unresolved_count": 0}


def _pairs(rows):
    return {(row["left_source_id"], row["right_source_id"]): row for row in rows}


def test_rare_terms_outweigh_popular_terms_and_fields_are_not_terms():
    rows = [_row("A", "common raremechanism"), _row("B", "common raremechanism"), *[_row(f"C{i}", "common") for i in range(10)]]
    index = LocalRetrievalIndex(rows)
    result = _pairs(index.candidate_pairs(per_source_cap=12))
    assert result[("A", "B")]["score"] > result[("A", "C0")]["score"]
    assert not {"title", "thesis", "null", "facets"} & index.words["A"]
    assert LocalRetrievalIndex([_row("A"), _row("B")]).candidate_pairs() == []


def test_pair_dedup_caps_repeatability_and_material_evidence_excludes_rank():
    rows = [_row(sid, "federalism courts", collection=sid) for sid in "ABCDE"]
    index = LocalRetrievalIndex(rows)
    pairs = index.candidate_pairs(per_source_cap=2, max_pairs=4, fanout=3)
    assert pairs == LocalRetrievalIndex(list(reversed(rows))).candidate_pairs(per_source_cap=2, max_pairs=4, fanout=3)
    assert len(pairs) == len(_pairs(pairs)) <= 4
    assert max(Counter(sid for row in pairs for sid in (row["left_source_id"], row["right_source_id"])).values()) <= 2
    assert [row["rank"] for row in pairs] == list(range(1, len(pairs) + 1))
    assert all(row["left_source_id"] < row["right_source_id"] for row in pairs)
    for pair in pairs:
        assert "score" not in json.dumps(pair["discovery_provenance"])
        assert "rank" not in json.dumps(pair["discovery_provenance"])
    larger = LocalRetrievalIndex([*rows, _row("Z", "unrelated")])
    assert index._pair("A", "B")["discovery_provenance"] == larger._pair("A", "B")["discovery_provenance"]
    assert index._pair("A", "B")["score"] != larger._pair("A", "B")["score"]


def test_popular_hub_postings_and_pair_construction_are_bounded(monkeypatch):
    count = 1500
    rows = [_row(f"S{i:04}", "ubiquitous", "bulk" if i < 750 else f"collection-{i}") for i in range(count)]
    records = [_record(row["source_id"], row["source_id"], tags=["popular", "to-read"], references=[{"work_id": "external:ubiquitous"}]) for row in rows]
    index = LocalRetrievalIndex(rows, records, ignored_tags=["To-Read"], max_posting_size=7)
    assert max(map(len, index.postings.values())) <= 7
    assert index.frequencies[("reference", "external:ubiquitous")] == count
    assert ("tag", "to-read") not in index.postings
    calls = []
    original = index._pair
    monkeypatch.setattr(index, "_pair", lambda *args: (calls.append(args), original(*args))[1])
    pairs = index.candidate_pairs(fanout=5, per_source_cap=2, max_pairs=20)
    assert len(calls) == len(pairs) <= 20
    assert index.last_candidate_stats["scored_pairs"] <= count * 5
    assert index.last_candidate_stats["max_source_fanout"] <= 5


def test_cross_collection_and_sparse_source_have_independent_route():
    rows = [_row("A", "constitutional design", "major"), _row("B", "constitutional design", "major"), _row("Z", "constitutional design", "minor")]
    index = LocalRetrievalIndex(rows)
    result = index.candidate_pairs(per_source_cap=1, fanout=1, max_pairs=1)
    assert len(result) == 1
    assert "Z" in (result[0]["left_source_id"], result[0]["right_source_id"])


def test_external_shared_references_tags_related_are_separate_provenance():
    refs = [{"work_id": "doi:10.test/external", "match_status": "unresolved", "matched_item_keys": [], "locator": "bibliography p. 90"}]
    records = [_record("A", "KA", references=refs, tags=["specific", "folder"], relations={"dc:relation": ["http://zotero.org/users/42/items/KB"]}), _record("B", "KB", references=refs, tags=["specific", "folder"])]
    index = LocalRetrievalIndex([_row("A"), _row("B")], records, ignored_tags=["folder"])
    result = index.candidate_pairs()
    signals = {row["signal"] for row in result[0]["discovery_provenance"]}
    assert signals == {"shared_reference", "shared_tag", "zotero_related", "source_coverage", "material_evidence_summary"}
    assert len(index.rows) == 2
    assert "direct_reference" not in signals
    assert "doi:10.test/external" in json.dumps(result[0]["discovery_provenance"])
    assert "bibliography p. 90" in json.dumps(result[0]["discovery_provenance"])
    assert records[0]["metadata"]["tags"] == ["specific", "folder"]


def test_direct_reference_resolves_within_exact_library_and_instance_only():
    rows = [_row(sid) for sid in "ABCD"]
    refs = [{"work_id": "work:target", "match_status": "matched", "matched_item_keys": ["TARGET"]}]
    records = [_record("A", "AUTHOR", references=refs), _record("B", "TARGET"), _record("C", "TARGET", library="99"), _record("D", "TARGET", instance="other")]
    index = LocalRetrievalIndex(rows, records)
    assert set(_pairs(index.candidate_pairs())) == {("A", "B")}
    records[0]["references"][0]["match_status"] = "ambiguous"
    assert LocalRetrievalIndex(rows, records).candidate_pairs() == []


def test_scanner_title_author_year_match_creates_direct_reference_candidate():
    reference = {
        "work_id": "title-author-year:synthetic-federalism-2001",
        "identifiers": {},
        "title": "Synthetic Federalism",
        "author": "Example",
        "year": "2001",
        "match_status": "matched_title_author_year",
        "matched_item_keys": ["TARGET"],
        "locator": "bibliography p. 14",
    }
    records = [_record("A", "AUTHOR", references=[reference]), _record("B", "TARGET")]
    pairs = LocalRetrievalIndex([_row("A"), _row("B")], records).candidate_pairs()
    assert set(_pairs(pairs)) == {("A", "B")}
    direct = next(row for row in pairs[0]["discovery_provenance"] if row["signal"] == "direct_reference")
    assert direct["citing_source_id"] == "A"
    assert direct["cited_source_id"] == "B"
    assert direct["match_status"] == "matched_title_author_year"
    assert direct["locator"] == "bibliography p. 14"


def test_declared_target_alias_resolves_citation_to_existing_source():
    target_identity = _record("B", "CANONICAL")["identity"]
    rows = [_row("A", zotero_key="AUTHOR"), _row("B", zotero_key="CANONICAL", zotero_item_keys=["CANONICAL", "ALIAS"], identity=target_identity)]
    refs = [{"work_id": "work:target", "match_status": "matched", "matched_item_keys": ["ALIAS"]}]
    index = LocalRetrievalIndex(rows, [_record("A", "AUTHOR", references=refs)])
    pairs = index.candidate_pairs()
    assert set(_pairs(pairs)) == {("A", "B")}
    direct = next(row for row in pairs[0]["discovery_provenance"] if row["signal"] == "direct_reference")
    assert direct["cited_source_id"] == "B"
    assert direct["target_identity"]["item_key"] == "ALIAS"
    assert set(index.rows) == {"A", "B"}


def test_canonical_and_declared_alias_records_keep_one_note_and_record_identity():
    rows = [_row("A", "query", zotero_key="CANONICAL", zotero_item_keys=["CANONICAL", "ALIAS"], identity=_record("A", "CANONICAL")["identity"]), _row("B", zotero_key="TARGET")]
    refs = [{"work_id": "work:target", "match_status": "matched", "matched_item_keys": ["TARGET"]}]
    records = [_record("A", "CANONICAL"), _record("A", "ALIAS", references=refs), _record("B", "TARGET")]
    index = LocalRetrievalIndex(rows, records)
    pairs = index.candidate_pairs()
    assert set(_pairs(pairs)) == {("A", "B")}
    direct = next(row for row in pairs[0]["discovery_provenance"] if row["signal"] == "direct_reference")
    assert direct["source_identity"]["item_key"] == "ALIAS"
    notes = index.recall("query")["notes"]
    assert len(notes) == 1 and notes[0]["note_id"] == "note-A"
    assert notes[0]["provenance"]["identity"]["item_key"] == "CANONICAL"
    assert {row["identity"]["item_key"] for row in notes[0]["coverage"]} == {"CANONICAL", "ALIAS"}
    assert pairs == LocalRetrievalIndex(rows, list(reversed(records))).candidate_pairs()


@pytest.mark.parametrize("change", ["unknown_key", "library", "instance", "second_source"])
def test_catalogue_aliases_reject_unknown_key_cross_scope_and_duplicate_binding(change):
    rows = [_row("A", zotero_key="CANONICAL", zotero_item_keys=["CANONICAL", "ALIAS"], identity=_record("A", "CANONICAL")["identity"])]
    records = [_record("A", "ALIAS")]
    if change == "unknown_key":
        records[0]["identity"]["item_key"] = "UNKNOWN"
    elif change == "library":
        records[0]["identity"]["library_id"] = "99"
    elif change == "instance":
        records[0]["identity"]["instance_id"] = "other"
    else:
        rows.append(_row("B", zotero_key="OTHER", zotero_item_keys=["OTHER", "ALIAS"], identity=_record("B", "OTHER")["identity"]))
    with pytest.raises(ValueError):
        LocalRetrievalIndex(rows, records)


def test_declared_aliases_can_anchor_to_canonical_record_without_projection_scope():
    row = _row("A", "query", zotero_key="CANONICAL", zotero_item_keys=["CANONICAL", "ALIAS"])
    index = LocalRetrievalIndex([row], [_record("A", "ALIAS"), _record("A", "CANONICAL")])
    assert index.recall("query")["notes"][0]["provenance"]["identity"]["item_key"] == "CANONICAL"


def test_related_uri_checks_library_and_does_not_claim_citation():
    records = [_record("A", "AUTHOR", relations={"dc:relation": ["http://zotero.org/users/99/items/TARGET", "https://untrusted.example/users/42/items/TARGET"]}), _record("B", "TARGET"), _record("C", "TARGET", library="99")]
    result = LocalRetrievalIndex([_row(sid) for sid in "ABC"], records).candidate_pairs()
    assert set(_pairs(result)) == {("A", "C")}
    assert "direct_reference" not in json.dumps(result)


@pytest.mark.parametrize("change", ["source", "identity", "key", "duplicate_binding"])
def test_conflicting_scanner_binding_is_rejected(change):
    rows = [_row("A", zotero_key="KEY"), _row("B")]
    records = [_record("A", "KEY")]
    if change == "source":
        records[0]["source_id"] = "unknown"
    elif change == "identity":
        records.append(_record("A", "KEY", instance="other"))
    elif change == "key":
        records[0]["identity"]["item_key"] = "OTHER"
    else:
        records.append(_record("B", "KEY"))
    with pytest.raises(ValueError):
        LocalRetrievalIndex(rows, records)


def test_explicit_roles_search_preserves_control_role_and_does_not_invent_cause():
    roles = {"controls": ["income baseline"], "moderators": ["regime longevity"], "dependent_variables": ["policy adoption"]}
    index = LocalRetrievalIndex([_row("A", variable_roles=roles), _row("B", "regime longevity"), _row("C", "hermeneutic reading")])
    result = index.recall("controls income", max_notes=1)
    assert [row["source_id"] for row in result["notes"]] == ["A"]
    assert result["notes"][0]["variable_roles"] == roles
    assert "cause" not in index.words["A"]
    assert "variable_roles" not in index.rows["C"]
    assert ("A", "B") in _pairs(index.candidate_pairs())


def test_recall_coverage_source_references_and_no_invented_locator():
    source_ref = {"source_file": "synthetic.pdf", "locator": "pp. 3–4", "page": "3"}
    rows = [_row("A", "federalism", source_scope="partial_document", evidence_eligibility="context_only", source_references=[source_ref], note_path="notes/A.md", note_hash="recorded-hash")]
    record = _record("A", "KA", coverage={"status": "partial", "reason": "first pages only", "source_scope": "partial_document"})
    result = LocalRetrievalIndex(rows, [record]).recall("federalism")
    note = result["notes"][0]
    assert note["source_scope"] == "partial_document"
    assert note["evidence_eligibility"] == "context_only"
    assert note["coverage"][0]["status"] == "partial"
    assert note["source_references"] == [source_ref]
    assert note["note_path"] == "notes/A.md"
    assert note["note_hash"] == "recorded-hash"
    unknown = LocalRetrievalIndex([_row("B", "federalism")]).recall("federalism")["notes"][0]
    assert unknown["coverage"] == {"status": "not_examined"}
    assert "source_references" not in unknown


def test_recall_one_hop_only_accepted_active_relationships_and_budget():
    rows = [_row("A", "rarequery " + "explanation " * 300), *[_row(sid, f"unrelated{sid}") for sid in "BCDE"]]
    relationships = [{"source_id": "A", "target_source_id": "B", "relation_id": "R1", "decision_status": "accepted", "reason": "Useful accepted neighbor", "source_evidence": {"locator": "p. 8"}}, {"source_id": "B", "target_source_id": "C", "decision_status": "accepted"}, {"source_id": "A", "target_source_id": "D", "decision_status": "no_relationship"}, {"source_id": "A", "target_source_id": "E", "decision_status": "accepted", "active": False}]
    index = LocalRetrievalIndex(rows)
    result = index.recall("rarequery", relationships=relationships, max_notes=3, max_chars=3000, neighbor_cap=1)
    assert {row["source_id"] for row in result["notes"]} == {"A", "B"}
    assert len(result["relationships"]) == 1
    assert result["relationships"][0]["source_evidence"]["locator"] == "p. 8"
    assert next(row for row in result["notes"] if row["source_id"] == "B")["recall_route"] == "accepted_neighbor"
    assert result["chars"] == sum(len(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))) for row in [*result["notes"], *result["relationships"]]) <= 3000
    assert index.recall("rarequery", max_chars=1)["notes"] == []
    assert index.recall("doesnotexist")["notes"] == []


def test_recall_does_not_expand_relationship_pending_refresh():
    index = LocalRetrievalIndex([_row("A", "rarequery"), _row("B", "unrelated")])
    relationship = {"source_id": "A", "target_source_id": "B", "decision_status": "accepted", "active": True, "refresh_pending": True}
    result = index.recall("rarequery", relationships=[relationship], max_notes=2)
    assert [row["source_id"] for row in result["notes"]] == ["A"]
    assert result["relationships"] == []


@pytest.mark.parametrize("freshness", ["recorded_unverified", "current_profile_hashes"])
def test_recall_preserves_supplied_relationship_freshness(freshness):
    index = LocalRetrievalIndex([_row("A", "rarequery"), _row("B", "unrelated")])
    relationship = {"source_id": "A", "target_source_id": "B", "decision_status": "accepted", "freshness": freshness}
    result = index.recall("rarequery", relationships=[relationship], max_notes=2)
    assert len(result["relationships"]) == 1
    assert result["relationships"][0]["freshness"] == freshness


def test_query_and_input_limits_and_duplicate_projection():
    rows = [_row("A", "alpha beta gamma"), _row("A", "alpha beta gamma")]
    index = LocalRetrievalIndex(rows)
    result = index.recall("alpha beta gamma " * 50, max_query_chars=10, max_query_tokens=1, fanout=1)
    assert len(result["query_terms"]) <= 1
    assert result["truncated"]
    assert len(index.rows) == 1
    for kwargs in ({"max_posting_size": 0}, {"max_sources": 1}):
        with pytest.raises(ValueError):
            LocalRetrievalIndex(rows, **kwargs)
    with pytest.raises(ValueError):
        LocalRetrievalIndex([*rows, _row("A", "conflicting")])
    with pytest.raises(ValueError):
        index.candidate_pairs(source_ids=["unknown"])
    with pytest.raises(ValueError):
        index.recall("query", max_notes=-1)


def test_scanner_record_and_relationship_order_do_not_change_results():
    rows = [_row("A", "query"), _row("B", "query"), _row("C", "query")]
    records = [_record("A", "KA", tags=["topic"]), _record("B", "KB", tags=["topic"]), _record("C", "KC", tags=["topic"])]
    links = [{"source_id": "A", "target_source_id": sid, "decision_status": "accepted"} for sid in "BC"]
    first, second = LocalRetrievalIndex(rows, records), LocalRetrievalIndex(list(reversed(rows)), list(reversed(records)))
    assert first.candidate_pairs() == second.candidate_pairs()
    assert first.recall("query", relationships=links) == second.recall("query", relationships=list(reversed(links)))


def test_profile_hashes_are_preserved_separately_from_material_evidence():
    rows = [_row("A", "shared", profile_hash="profile-a"), _row("B", "shared", profile_hash="profile-b")]
    index = LocalRetrievalIndex(rows)
    pair = index.candidate_pairs()[0]
    assert pair["input_profile_hashes"] == {"A": "profile-a", "B": "profile-b"}
    assert "profile-a" not in json.dumps(pair["discovery_provenance"])
    assert index.recall("shared")["notes"][0]["profile_hash"] == "profile-a"


def test_nested_and_cyclic_values_do_not_stringify_or_walk_unbounded_input():
    nested = {}
    nested["controls"] = nested
    index = LocalRetrievalIndex([_row("A", variable_roles=nested, title={"unexpected": "container"})])
    assert index.rows["A"]["title"] == ""
    assert len(json.dumps(index.rows["A"]["variable_roles"])) < 100
    with pytest.raises(ValueError, match="32 fields"):
        LocalRetrievalIndex([_row("A", facets={str(i): "value" for i in range(33)})])
    with pytest.raises(ValueError, match="record limit"):
        LocalRetrievalIndex([_row("A")], [_record("A", "K")] * 5)


def test_conflicting_projection_and_scanner_note_id_rejected():
    with pytest.raises(ValueError, match="note identity"):
        LocalRetrievalIndex([_row("A", note_id="other-note")], [_record("A", "K")])
    first, second = _record("A", "K"), _record("A", "K")
    second["note_id"] = "other-note"
    with pytest.raises(ValueError, match="note identity"):
        LocalRetrievalIndex([_row("A")], [first, second])


def test_unknown_coverage_and_limited_query_postings_report_honest_limits():
    rows = [{"source_id": f"S{i}", "thesis": "popular"} for i in range(10)]
    index = LocalRetrievalIndex(rows, max_posting_size=2)
    result = index.recall("popular", max_notes=8)
    assert result["truncated"]
    assert len(result["notes"]) == 2
    assert all(note["source_scope"] == "unknown" and note["evidence_eligibility"] == "unavailable" for note in result["notes"])


def test_recall_reuses_tag_and_reference_postings_without_changing_text_evidence():
    records = [_record("A", "KA", tags=["urban governance"], references=[{"work_id": "doi:10.test/external", "locator": "bibliography p. 8"}])]
    index = LocalRetrievalIndex([_row("A")], records)
    tag_note = index.recall("urban governance")["notes"][0]
    assert tag_note["matched_signals"] == [{"signal": "tag", "value": "urban governance"}]
    assert "urban" not in index.words["A"]
    ref_note = index.recall("doi:10.test/external")["notes"][0]
    assert ref_note["discovery_references"][0]["locator"] == "bibliography p. 8"


def test_reference_heavy_shortlist_fits_pipeline_and_cli_material_budgets():
    from auto_zettelkasten.pipeline import _local_ranked_candidate_basis

    rows = [_row(f"S{i:02}", "shared federalism") for i in range(12)]
    references = [{"work_id": f"work:shared-{i:03}", "title": "A lengthy synthetic reference title " * 6, "author": "Example author", "year": "2001", "locator": f"bibliography p. {i + 1}"} for i in range(64)]
    records = [_record(row["source_id"], row["source_id"], references=references) for row in rows]
    index = LocalRetrievalIndex(rows, records)
    candidates = index.candidate_pairs()
    assert 40 <= len(candidates) <= 48
    assert len(_local_ranked_candidate_basis(candidates)) == len(candidates)
    assert len(json.dumps(candidates).encode("utf-8")) < 4 * 1024 * 1024
    assert all(len(json.dumps(row["discovery_provenance"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 2048 for row in candidates)
    summary = candidates[0]["discovery_provenance"][0]
    assert summary["signal_counts"]["shared_reference"] == 64
    assert summary["omitted_signal_count"] > 0
    assert "lengthy synthetic reference title" not in json.dumps(candidates)
    assert index.references["S00"]["work:shared-063"]["title"] == references[-1]["title"].strip()
    # A complete default shortlist also remains inside aggregate preflight bounds.
    expanded = LocalRetrievalIndex([_row(f"N{i:02}", f"shared federalism cluster{i // 10}") for i in range(70)], [_record(f"N{i:02}", f"N{i:02}", references=references) for i in range(70)]).candidate_pairs()
    assert len(expanded) == 256
    assert len(_local_ranked_candidate_basis(expanded)) == 256


def test_unsampled_reference_changes_material_fingerprint_without_rank_changes():
    references = [{"work_id": f"work:shared-{i:03}", "title": "Synthetic reference", "locator": f"bibliography p. {i}"} for i in range(64)]
    rows = [_row("A"), _row("B")]
    first = LocalRetrievalIndex(rows, [_record("A", "KA", references=references), _record("B", "KB", references=references)]).candidate_pairs()[0]
    changed = [{**reference, **({"title": "Changed bibliographic evidence"} if i == 63 else {})} for i, reference in enumerate(references)]
    second = LocalRetrievalIndex(rows, [_record("A", "KA", references=changed), _record("B", "KB", references=references)]).candidate_pairs()[0]
    assert first["score"] == second["score"]
    assert first["rank"] == second["rank"]
    assert first["discovery_provenance"][0]["material_sha256"] != second["discovery_provenance"][0]["material_sha256"]


def test_all_shared_references_contribute_to_score_despite_sample_and_query_caps():
    references = [{"work_id": f"work:{i:03}"} for i in range(32)]
    index = LocalRetrievalIndex([_row("A"), _row("B")], [_record("A", "KA", references=references), _record("B", "KB", references=references)], max_signals_per_source=2)
    assert index.candidate_pairs()[0]["score"] == 32 * 4
