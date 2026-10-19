from copy import deepcopy

from auto_zettelkasten.indexes import lean_discovery_projection, local_retrieval_projection


def test_local_projection_preserves_explicit_roles_and_traceability_without_changing_legacy():
    profile = {"source_id": "source-a", "note_id": "note-a", "note_hash": "semantic-a",
               "source_scope": "partial_document", "evidence_eligibility": "context_only",
               "methods": ["Panel comparison; controls are adjustments, not established causes."],
               "outcomes": ["participation"],
               "context": {"variable_roles": {"treatments": ["access"], "controls": ["income"],
                                                "mediators": ["trust (proposed)"]},
                           "metadata": {"source_pdf_uri": "zotero://open-pdf/library/items/PDF00001"},
                           "note_path": "02_source_memory/notes/Study.md",
                           "source_references": [{"locator": "p. 12", "text": "A bounded passage."}]}}
    catalogue = {"sources": [{"source_id": "source-a", "title": "A study", "zotero_key": "ITEM0001",
                               "identity": {"zotero_item_keys": ["ALIAS001", "ITEM0001"]}}]}
    before = deepcopy((profile, catalogue))
    legacy = lean_discovery_projection([profile], catalogue)
    result = local_retrieval_projection([profile], catalogue)[0]
    assert result["variable_roles"]["controls"] == ["income"]
    assert result["variable_roles"]["treatments"] == ["access"]
    assert result["facets"]["outcome"] == ["participation"]
    assert "income" not in result["facets"]["outcome"]
    assert result["source_scope"] == "partial_document"
    assert result["evidence_eligibility"] == "context_only"
    assert result["note_path"] == profile["context"]["note_path"]
    assert result["source_references"] == [{"locator": "p. 12", "text": "A bounded passage."},
                                           {"source_pdf_uri": "zotero://open-pdf/library/items/PDF00001"}]
    assert result["note_hash"] == "semantic-a"
    assert result["zotero_item_keys"] == ["ALIAS001", "ITEM0001"]
    assert lean_discovery_projection([profile], catalogue) == legacy
    assert (profile, catalogue) == before


def test_interpretive_work_gets_no_fabricated_roles_or_page_references():
    profiles = [{"source_id": "interpretive", "methods": ["Interpretive reading of institutional meanings."]}]
    result = local_retrieval_projection(profiles, {"sources": [{"source_id": "interpretive"}]})[0]
    assert "variable_roles" not in result
    assert result["source_references"] == []
    assert result["note_id"] == result["note_path"] == ""


def test_only_explicit_role_fields_and_bounded_references_are_projected():
    profile = {"source_id": "source-a", "independent_variables": ["policy"],
               "context": {"variable_roles": {"moderators": ["age"] * 50, "invented": ["ignore"]},
                           "source_references": [{"locator": "x" * 1000}] * 100}}
    result = local_retrieval_projection([profile], {"sources": [{"source_id": "source-a"}]})[0]
    assert result["variable_roles"] == {"independent_variables": ["policy"], "moderators": ["age"]}
    assert len(result["source_references"]) <= 32
    assert len(result["source_references"][0]["locator"]) <= 500
