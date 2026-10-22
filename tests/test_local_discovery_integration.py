from copy import deepcopy

from auto_zettelkasten.api import build_map
from auto_zettelkasten.discovery import scan_document
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.indexes import lean_discovery_projection
from auto_zettelkasten.models import LiteratureMappingPolicy
from auto_zettelkasten.notes import semantic_note_hash
from auto_zettelkasten.pipeline import _cluster_preservation_snapshot, _workspace_graph_inputs
from auto_zettelkasten.retrieval import LocalRetrievalIndex

from test_local_discovery import coverage, identity, item, Parser
from test_v030_relationship_first_pipeline import _RelationshipReasoner, _seed_workspace


def test_actual_doi_free_scanner_record_becomes_a_candidate_and_traceable_recall():
    a = item("ITEM0001", title="Learning institutions")
    b = item("ITEM0002", title="Institutional feedback", creators=[{"lastName": "Jones"}], date="2021")
    library = [{"identity": identity("ITEM0001"), "item": a}, {"identity": identity("ITEM0002"), "item": b}]
    records = []
    for key, source, note, source_item, references in [
        ("ITEM0001", "source-a", "note-a", a, [{"title": "Institutional feedback", "author": "Jones", "year": "2021"}]),
        ("ITEM0002", "source-b", "note-b", b, []),
    ]:
        records.append(scan_document(source_item, identity=identity(key), text="Synthetic article and bibliography.",
                                     coverage=coverage(), source_binding={"zotero_key": key, "source_id": source, "note_id": note},
                                     library_items=library, parser=Parser(references), parser_version="synthetic-structured-v1"))
    assert records[0]["references"][0]["match_status"] == "matched_title_author_year"
    index = LocalRetrievalIndex([
        {"source_id": "source-a", "note_id": "note-a", "zotero_key": "ITEM0001", "title": "Learning institutions", "thesis": "rarequery",
         "source_references": [{"locator": "p. 14", "source_pdf_uri": "zotero://open-pdf/library/items/PDF00001"}]},
        {"source_id": "source-b", "note_id": "note-b", "zotero_key": "ITEM0002", "title": "Institutional feedback"},
    ], records)
    pairs = index.candidate_pairs()
    assert len(pairs) == 1
    assert any(row["signal"] == "direct_reference" for row in pairs[0]["discovery_provenance"])
    recall = index.recall("rarequery", max_notes=1, max_chars=4000)
    assert recall["notes"][0]["source_references"][0]["locator"] == "p. 14"
    assert recall["chars"] <= 4000


def test_public_build_map_receipt_does_not_skip_changed_local_selection(tmp_path, sample_items):
    protected, note_rows = _seed_workspace(tmp_path, sample_items)
    _, profiles = _workspace_graph_inputs(tmp_path, [])
    catalogue = read_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml")
    rows = lean_discovery_projection(profiles, catalogue)
    pair = {"left_source_id": rows[0]["source_id"], "right_source_id": rows[1]["source_id"],
            "left_source_title": rows[0]["title"], "right_source_title": rows[1]["title"],
            "rank": 1, "score": 1.0, "why_compare": "Local evidence.", "discovery_route": "deterministic_local_ranked",
            "discovery_provenance": [{"signal": "shared_reference", "work_id": "doi:10.1234/synthetic"}]}
    reasoner = _RelationshipReasoner()
    args = dict(run_id="local-build", provider="ollama", model="fake-1", reasoner=reasoner,
                literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False))
    source_hashes = {row["note_path"]: semantic_note_hash((tmp_path / row["note_path"]).read_text()) for row in note_rows}
    assert build_map(tmp_path, ranked_candidates=[], **args, mapping_backend="llm").status == "built"
    assert reasoner.candidate_calls == 0
    assert build_map(tmp_path, ranked_candidates=[pair], **args, mapping_backend="llm").status == "built"
    assert reasoner.candidate_calls == 1
    assert build_map(tmp_path, ranked_candidates=[{**pair, "rank": 5, "score": 99.0, "why_compare": "Reordered."}], **args, mapping_backend="llm").status == "built"
    assert reasoner.candidate_calls == 1
    changed = deepcopy(pair)
    changed["discovery_provenance"][0]["work_id"] = "doi:10.1234/changed"
    assert build_map(tmp_path, ranked_candidates=[changed], **args, mapping_backend="llm").status == "built"
    assert reasoner.candidate_calls == 2
    assert _cluster_preservation_snapshot(tmp_path) == protected
    assert {path: semantic_note_hash((tmp_path / path).read_text()) for path in source_hashes} == source_hashes
