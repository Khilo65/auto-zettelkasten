from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import socket

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentError, DocumentIndexChanged, DocumentService
from auto_zettelkasten.note_recall import NoteRecallService
from auto_zettelkasten.notes import GRAPH_START_MARKER, GRAPH_END_MARKER


class Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, *, add_special_tokens, return_offsets_mapping=False, **_):
        offsets = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        result = {"input_ids": list(range(len(offsets) + (2 if add_special_tokens else 0)))}
        if return_offsets_mapping:
            result["offset_mapping"] = offsets
        return result


class Encoder:
    tokenizer = Tokenizer()
    dimension = 3
    identity = {"model_name": "synthetic", "model_sha256": "fixed", "dimension": 3}

    def __init__(self, options):
        self.options = options
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return [[0., 1., 0.] if "orchard" in text else [1., 0., 0.] for text in texts]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("recall must remain offline"))
    return root


@pytest.fixture
def service(tmp_path):
    encoders = []
    def factory(options, cache):
        encoder = Encoder(options); encoders.append(encoder)
        return encoder
    value = NoteRecallService(encoder_factory=factory, override_root=tmp_path / "overrides")
    yield value, encoders
    value.close()


def note(root, name, body, front=""):
    text = ("---\n" + front + "---\n\n" if front else "") + body
    path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    return path


def paths(result):
    return [row["note_path"] for row in result["notes"]]


def test_searches_complete_substantive_body_and_filters_private_navigation_metadata(workspace, service):
    value, encoders = service
    note(workspace, "Deep.md", "Ordinary content. " * 1500 + " buriedneedle at the end.")
    note(workspace, "Abstract.md", "A genuine abstract describes buriedneedle.", "source_scope: abstract_only\n")
    note(workspace, "Metadata.md", "buriedneedle", "source_scope: metadata_only\n")
    note(workspace, "INDEX.md", "buriedneedle")
    note(workspace, "private/Secret.md", "buriedneedle")
    note(workspace, ".hidden/Hidden.md", "buriedneedle")
    note(workspace, "Managed.md", f"Useful content.\n\n{GRAPH_START_MARKER}\nburiedneedle\n{GRAPH_END_MARKER}\n")
    result = value.recall(workspace, "buriedneedle")
    assert set(paths(result)) == {"Deep.md", "Abstract.md"}
    assert all("buriedneedle" in row["text"] for row in result["notes"])
    assert result["coverage"]["excluded_notes"]["metadata_only"] == 1
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["provider_calls"] == 0 and result["jev"]["enabled"] is False
    assert result["status"] == "retrieved"
    assert all(row["coverage"]["excerpt_basis"] == "cleaned_note_text" for row in result["notes"])
    assert not encoders
    assert result["chars"] <= 12_000


def test_search_is_not_limited_to_the_file_panel_and_does_not_consume_graph_cursor(workspace, service, monkeypatch):
    value, _ = service
    for i in range(7):
        note(workspace, f"{i}.md", "sharedsearch")
    monkeypatch.setattr(documents, "MAX_ENTRIES", 2)
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    docs = DocumentService()
    try:
        opened = docs.open(workspace)
        assert opened["truncated"]
        page = docs.index_page(opened["id"])
        assert page["next_cursor"]
        result = value.recall(workspace, "sharedsearch", max_notes=8, documents=docs, workspace_id=opened["id"])
        assert len(result["notes"]) == 7
        assert not result["coverage"]["document_index_partial"]
        next_page = docs.index_page(opened["id"], cursor=page["next_cursor"])
        assert next_page["documents"]
    finally:
        docs.close()


def test_edits_new_files_renames_and_deletions_invalidate_the_corpus(workspace, service):
    value, _ = service
    path = note(workspace, "First.md", "beforeword")
    assert paths(value.recall(workspace, "beforeword")) == ["First.md"]
    path.write_text("afterword")
    assert not paths(value.recall(workspace, "beforeword"))
    assert paths(value.recall(workspace, "afterword")) == ["First.md"]
    target = workspace / "Renamed.md"; path.rename(target)
    note(workspace, "New.md", "afterword")
    assert set(paths(value.recall(workspace, "afterword"))) == {"Renamed.md", "New.md"}
    target.unlink()
    assert paths(value.recall(workspace, "afterword")) == ["New.md"]


@pytest.mark.skipif(not documents._SAFE_WRITES, reason="safe overrides require directory handles")
def test_effective_override_is_searchable_without_changing_generated_note(workspace, service, tmp_path):
    value, _ = service
    (workspace / "auto-zettelkasten.yml").write_text("version: 1\n")
    header = "note_id: note-a\nsource_id: source-a\ntitle: Generated\n"
    path = note(workspace, "Note.md", "Canonical upstream text.", header)
    before = path.read_bytes()
    docs = DocumentService(override_root=tmp_path / "custom-overrides")
    try:
        opened = docs.open(workspace)
        doc = docs.read(opened["id"], "Note.md")
        assert not paths(value.recall(workspace, "personalword", documents=docs, workspace_id=opened["id"]))
        docs.save_override(opened["id"], doc["id"], doc["revision"], doc["text"].replace("Canonical upstream text.", "A personalword override."))
        assert paths(value.recall(workspace, "personalword", documents=docs, workspace_id=opened["id"])) == ["Note.md"]
        assert path.read_bytes() == before
    finally:
        docs.close()


def test_explicit_note_indexing_enables_hybrid_and_reuses_warm_encoder_checkpoints(workspace, service):
    value, encoders = service
    a = note(workspace, "A.md", "A peace finding.", "source_id: source-a\n")
    b = note(workspace, "B.md", "An orchard finding.", "source_id: source-b\n")
    original = (a.read_bytes(), b.read_bytes())
    prepared = value.prepare(workspace, model_path="synthetic")
    assert prepared["status"] == "prepared" and prepared["indexed_note_count"] == 2
    assert len(encoders) == 1
    count = len(encoders[0].calls)
    result = value.recall(workspace, "diplomacy", model_path="synthetic", max_notes=1)
    assert paths(result) == ["A.md"]
    assert result["provenance"]["route"] == "full_note_hybrid"
    assert result["coverage"]["semantic"]["status"] == "complete"
    assert result["coverage"]["semantic"]["source_count"] == 2
    assert len(encoders[0].calls) == count + 1
    generation = value._vector_generation
    index = value._search_cache[1]
    value.recall(workspace, "diplomacy", model_path="synthetic")
    assert len(encoders[0].calls) == count + 1
    assert value._vector_generation == generation
    assert value._search_cache[1] is index
    again = value.prepare(workspace, model_path="synthetic")
    assert again["reused_note_count"] == 2 and again["encoded_chunks"] == 0
    assert (a.read_bytes(), b.read_bytes()) == original
    assert not (workspace / ".auto-zettelkasten/mapping").exists()


def test_changed_note_vectors_are_excluded_until_explicit_refresh(workspace, service):
    value, _ = service
    a = note(workspace, "A.md", "peace", "source_id: a\n")
    note(workspace, "B.md", "orchard", "source_id: b\n")
    value.prepare(workspace, model_path="synthetic")
    a.write_text(a.read_text().replace("peace", "changedword"))
    result = value.recall(workspace, "changedword", model_path="synthetic")
    assert "A.md" in paths(result)
    assert result["coverage"]["semantic"]["status"] == "partial"
    assert result["coverage"]["semantic"]["source_count"] == 1
    refreshed = value.prepare(workspace, model_path="synthetic")
    assert refreshed["reused_note_count"] == 1
    assert value.recall(workspace, "changedword", model_path="synthetic")["coverage"]["semantic"]["status"] == "complete"


def test_corrupted_vector_is_not_used_and_is_repaired_only_by_prepare(workspace, service):
    value, encoders = service
    note(workspace, "A.md", "peace", "source_id: a\n")
    value.prepare(workspace, model_path="synthetic")
    path = next((workspace / ".auto-zettelkasten/recall/vectors").glob("*/*.json"))
    data = json.loads(path.read_text()); data["vector"] = [0., 1., 0.]; path.write_text(json.dumps(data))
    count = len(encoders[0].calls)
    result = value.recall(workspace, "peace", model_path="synthetic")
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["coverage"]["semantic"]["invalid_vector_count"] == 1
    assert len(encoders[0].calls) == count
    assert value.prepare(workspace, model_path="synthetic")["reused_note_count"] == 0


def test_changed_model_identity_does_not_use_old_vectors(workspace, service):
    value, encoders = service
    note(workspace, "A.md", "peace", "source_id: a\n")
    value.prepare(workspace, model_path="synthetic")
    encoders[0].identity = {**Encoder.identity, "model_sha256": "different"}
    result = value.recall(workspace, "peace", model_path="synthetic")
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["coverage"]["semantic"]["source_count"] == 0


def test_reuses_real_mapping_chunk_format_only_for_identical_note_text(workspace, service, monkeypatch):
    pytest.importorskip("numpy")
    from auto_zettelkasten import local_mapping as mapping
    value, encoders = service
    path = note(workspace, "A.md", "A peace finding.", "source_id: a\nnote_id: note-a\n")
    raw = path.read_text()
    monkeypatch.setattr(mapping, "_cluster_dependencies", lambda *_: (None, None))
    monkeypatch.setattr(mapping, "_communities_from_neighbors", lambda *_a, **_k: [])
    row = {"source_id": "a", "note_id": "note-a", "title": "A", "note_path": "A.md",
           "source_scope": "full_document", "evidence_eligibility": "eligible",
           "identity": {"instance_id": "synthetic", "library_type": "user", "library_id": "0", "item_key": "A"}}
    mapping.build_local_map([row], cache_dir=workspace / ".auto-zettelkasten/mapping",
        options=mapping.LocalMappingOptions(model_path="synthetic", min_free_disk_bytes=1024 * 1024),
        text_sources={"a": {"source_id": "a", "note_id": "note-a", "text": raw, "text_origin": "canonical_note"}},
        encoder_factory=lambda options: Encoder(options))
    result = value.recall(workspace, "diplomacy", model_path="synthetic")
    assert paths(result) == ["A.md"]
    assert result["coverage"]["semantic"]["reason"] == "verified_saved_note_embeddings"
    assert result["coverage"]["semantic"]["source_count"] == 1
    assert len(encoders) == 1 and len(encoders[0].calls) == 1  # query only
    current = value._documents.read(value._workspace_id, "A.md")
    value._documents.save_personal_metadata(value._workspace_id, current["id"], current["revision"], {"display_title": "Personal peace title"})
    renamed = value.recall(workspace, "diplomacy", model_path="synthetic")
    assert renamed["notes"][0]["title"] == "Personal peace title"
    assert renamed["coverage"]["semantic"]["source_count"] == 1
    assert len(encoders[0].calls) == 1
    assert path.read_text() == raw
    path.write_text(raw + "A changed passage.")
    stale = value.recall(workspace, "changed", model_path="synthetic")
    assert paths(stale) == ["A.md"]
    assert stale["provenance"]["route"] == "full_note_lexical"
    assert stale["coverage"]["semantic"]["excluded_sources"]["stale_or_different_note_text"] == 1


def test_pdf_based_mapping_vectors_do_not_impersonate_note_vectors(workspace, service, monkeypatch):
    pytest.importorskip("numpy")
    from auto_zettelkasten import local_mapping as mapping
    value, encoders = service
    note(workspace, "A.md", "A peace note.", "source_id: a\nnote_id: note-a\n")
    monkeypatch.setattr(mapping, "_cluster_dependencies", lambda *_: (None, None))
    monkeypatch.setattr(mapping, "_communities_from_neighbors", lambda *_a, **_k: [])
    row = {"source_id": "a", "note_id": "note-a", "title": "A", "note_path": "A.md"}
    mapping.build_local_map([row], cache_dir=workspace / ".auto-zettelkasten/mapping",
        options=mapping.LocalMappingOptions(model_path="synthetic", min_free_disk_bytes=1024 * 1024),
        text_sources={"a": {"text": "Different original PDF discussion.", "text_origin": "source_pdf"}},
        encoder_factory=lambda options: Encoder(options))
    result = value.recall(workspace, "peace", model_path="synthetic")
    assert result["provenance"]["route"] == "full_note_lexical"
    assert result["coverage"]["semantic"]["source_count"] == 0
    assert not encoders[0].calls


def test_mid_query_edit_cannot_return_stale_evidence(workspace, service):
    value, encoders = service
    path = note(workspace, "A.md", "peace", "source_id: a\n")
    value.prepare(workspace, model_path="synthetic")
    original = encoders[0].encode
    def change(texts):
        result = original(texts)
        path.write_text(path.read_text().replace("peace", "newword"))
        return result
    encoders[0].encode = change
    with pytest.raises(DocumentIndexChanged):
        value.recall(workspace, "diplomacy", model_path="synthetic")


def test_scope_checks_both_outer_and_nested_declared_identities(workspace, service):
    value, _ = service
    note(workspace, "Wrong.md", "scopeword", "library_id: '99'\nidentity: {instance_id: desktop}\n")
    note(workspace, "Right.md", "scopeword", "library_id: '0'\nidentity: {instance_id: desktop}\n")
    assert paths(value.recall(workspace, "scopeword", library_id="0")) == ["Right.md"]


def test_warm_queries_reuse_cleaned_corpus_and_search_index(workspace, service, monkeypatch):
    from auto_zettelkasten import note_recall
    value, _ = service
    note(workspace, "A.md", "firstword secondword")
    assert paths(value.recall(workspace, "firstword")) == ["A.md"]
    index = value._search_cache[1]
    monkeypatch.setattr(note_recall, "prepare_embedding_spans", lambda *_a, **_k: pytest.fail("unchanged notes must not be cleaned again"))
    assert paths(value.recall(workspace, "secondword")) == ["A.md"]
    assert value._search_cache[1] is index


def test_saved_accepted_relationships_fit_the_packet_and_do_not_expand_results(workspace, service):
    value, _ = service
    (workspace / "auto-zettelkasten.yml").write_text("version: 1\n")
    for sid in ("a", "b", "c"):
        note(workspace, f"{sid}.md", "sharedfinding" if sid != "c" else "unrelated",
             f"note_id: note-{sid}\nsource_id: source-{sid}\n")
    registry = workspace / "02_source_memory/indexes/typed_links.yml"; registry.parent.mkdir(parents=True)
    rows = [{"relation_id": "saved", "relation_type": "supports", "source_note_id": "note-a", "target_note_id": "note-b",
             "active": True, "decision_status": "accepted", "confidence": .72},
            {"relation_id": "outside", "relation_type": "cites", "source_note_id": "note-a", "target_note_id": "note-c"}]
    registry.write_text(json.dumps({"registry_schema_version": "7", "links": rows}))
    original = registry.read_bytes()
    result = value.recall(workspace, "sharedfinding", max_chars=1500)
    assert set(paths(result)) == {"a.md", "b.md"}
    assert [row["id"] for row in result["relationships"]] == ["saved"]
    assert result["relationships"][0]["confidence"] == .72
    assert result["chars"] == len(json.dumps({k: result[k] for k in ("notes", "relationships")},
                                            ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 1500
    assert registry.read_bytes() == original


def test_long_default_packet_reserves_saved_label_confidence_and_literal_evidence(workspace, service):
    value, _ = service
    (workspace / "auto-zettelkasten.yml").write_text("version: 1\n")
    for i in range(8):
        note(workspace, f"{i}.md", "sharedfinding " + "substantive content " * 1500,
             f"note_id: note-{i}\nsource_id: source-{i}\n")
    registry = workspace / "02_source_memory/indexes/typed_links.yml"; registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"registry_schema_version": "7", "links": [{"relation_id": "saved",
        "relation_type": "qualifies", "source_note_id": "note-0", "target_note_id": "note-1",
        "active": True, "decision_status": "accepted", "confidence": .72,
        "selected_evidence": [{"note_id": "note-0", "text": "sharedfinding"}]}]}))
    result = value.recall(workspace, "sharedfinding")
    assert len(result["notes"]) == 8
    edge, = result["relationships"]
    assert edge["type"] == "qualifies" and edge["confidence"] == .72
    assert edge["selected_evidence"] == [{"path": "0.md", "text": "sharedfinding"}]
    assert 11_000 < result["chars"] <= 12_000
    assert result["provider_calls"] == result["jev"]["provider_calls"] == 0


def test_empty_or_unaffordable_relations_do_not_exceed_the_packet_budget(workspace, service):
    value, _ = service
    note(workspace, "A.md", "word")
    result = value.recall(workspace, "word", max_chars=64)
    assert not result["notes"] and result["chars"] <= 64


@pytest.mark.parametrize("canonical_path", ["02_source_memory/notes/Canonical.md", "Other.md"])
def test_unselected_canonical_or_duplicate_identity_cannot_be_stolen_by_selected_copy(workspace, service, canonical_path):
    value, _ = service
    (workspace / "auto-zettelkasten.yml").write_text("version: 1\n")
    note(workspace, canonical_path, "no matching text", "note_id: note-a\nsource_id: source-original\n")
    note(workspace, "Copy.md", "sharedfinding", "note_id: note-a\nsource_id: source-copy\n")
    note(workspace, "Target.md", "sharedfinding", "note_id: note-b\nsource_id: source-b\n")
    registry = workspace / "02_source_memory/indexes/typed_links.yml"; registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"registry_schema_version": "7", "links": [{"relation_id": "canonical-only",
        "relation_type": "supports", "source_note_id": "note-a", "target_note_id": "note-b",
        "active": True, "decision_status": "accepted"}]}))
    result = value.recall(workspace, "sharedfinding")
    assert set(paths(result)) == {"Copy.md", "Target.md"}
    assert result["relationships"] == []


def test_symlinked_notes_and_derived_cache_never_escape_workspace(workspace, service, tmp_path):
    value, _ = service
    outside = tmp_path / "Outside.md"; outside.write_text("secretword")
    (workspace / "Linked.md").symlink_to(outside)
    note(workspace, "Public.md", "publicword")
    assert not paths(value.recall(workspace, "secretword"))
    (workspace / ".auto-zettelkasten").symlink_to(tmp_path)
    result = value.recall(workspace, "publicword", model_path="synthetic")
    assert paths(result) == ["Public.md"]
    assert result["coverage"]["semantic"]["status"] == "unavailable"
    with pytest.raises(ValueError, match="symbolic|inside"):
        value.prepare(workspace, model_path="synthetic")


def test_partial_prepare_checkpoints_are_usable_and_resume_without_reencoding(workspace, service):
    value, encoders = service
    for i in range(3):
        note(workspace, f"{i}.md", "peace", f"source_id: source-{i}\n")
    def stop_after_one(event):
        if event["completed"] == 1:
            raise RuntimeError("local_recall_wall_limit")
    result = value.prepare(workspace, model_path="synthetic", progress=stop_after_one)
    assert result["status"] == "partial" and result["indexed_note_count"] == 1
    recall = value.recall(workspace, "peace", model_path="synthetic")
    assert recall["coverage"]["semantic"]["source_count"] == 1
    resumed = value.prepare(workspace, model_path="synthetic")
    assert resumed["reused_note_count"] == 1 and resumed["indexed_note_count"] == 3


@pytest.mark.parametrize("query,options", [("", {}), ("x" * 2001, {}), ("x", {"max_notes": True}),
    ("x", {"max_chars": 63}), ("x", {"library_id": 0}), ("x", {"model_path": None})])
def test_invalid_query_and_resource_parameters_rejected_before_reads(workspace, service, query, options):
    value, _ = service
    with pytest.raises(ValueError):
        value.recall(workspace, query, **options)
