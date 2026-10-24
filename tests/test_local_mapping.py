from __future__ import annotations

import builtins
from copy import deepcopy
import hashlib
import json
import os
import re

import pytest

from auto_zettelkasten import local_mapping as mapping


def _row(source_id, *, library="42", **values):
    return {"source_id": source_id, "note_id": f"note-{source_id}", "title": source_id,
            "note_path": f"notes/{source_id}.md", "note_hash": "semantic-note-hash",
            "projection_hash": "actual-note-sha256", "profile_hash": "semantic-profile-hash",
            "identity": {"instance_id": "synthetic", "library_type": "user", "library_id": library, "item_key": source_id},
            **values}


def _text(source_id, text):
    return {"source_id": source_id, "note_id": f"note-{source_id}", "text": text,
            "text_origin": "synthetic_full_document", "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "coverage": {"source_scope": "full_document", "status": "succeeded"},
            "ownership_spans": [{"start": 0, "end": len(text), "owner": f"author-{source_id}", "section_kind": "body"}]}


class _Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, *, add_special_tokens, return_offsets_mapping=False, **_):
        offsets = [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]
        result = {"input_ids": list(range(len(offsets) + (2 if add_special_tokens else 0)))}
        if return_offsets_mapping:
            result["offset_mapping"] = offsets
        return result


class _Encoder:
    tokenizer = _Tokenizer()
    dimension = 3
    model_load_seconds = 0.0

    def __init__(self, *, revision="one", fail_after=None):
        self.identity = {"model_name": "synthetic", "model_sha256": revision, "dimension": self.dimension,
                         "library_versions": {"synthetic": "1"}, "device": "cpu"}
        self.calls = []
        self.fail_after = fail_after

    def encode(self, texts):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise mapping.LocalMappingError("synthetic interruption")
        self.calls.append(list(texts))
        return [[1.0 + text.count("court"), 1.0 + text.count("garden"), 1.0 + text.count("war")] for text in texts]


@pytest.fixture
def native_graph():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")


def test_defaults_and_validation_remain_lightweight():
    options = mapping.LocalMappingOptions.from_dict()
    assert options.encoder == "minilm" and options.clustering == "leiden"
    assert options.batch_size == 8 and options.chunk_tokens == 256
    assert mapping.LocalMappingOptions.from_dict(options.to_dict()) == options
    for values in ({"batch_size": 0}, {"batch_size": True}, {"chunk_tokens": 257}, {"clustering": "both"},
                   {"resolution": float("nan")}, {"seed": -1}, {"neighbors": 129}, {"unknown": "value"}):
        with pytest.raises(ValueError):
            mapping.LocalMappingOptions.from_dict(values)
    with pytest.raises(ValueError, match="explicit compatible adapter"):
        mapping.LocalMappingOptions.from_dict({"encoder": "qwen"})


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linked_job_lock_never_overwrites_an_existing_note(tmp_path, kind):
    note = tmp_path / "Source.md"
    original = b"# Original source\r\nThis must remain unchanged.\r\n"
    note.write_bytes(original)
    cache = tmp_path / "derived"
    cache.mkdir()
    lock = cache / "job.lock"
    if kind == "symlink":
        lock.symlink_to(note)
    else:
        os.link(note, lock)
    with pytest.raises(mapping.LocalMappingError, match="lock"):
        mapping.build_local_map([], cache_dir=cache)
    assert note.read_bytes() == original


def test_optional_hdbscan_dependency_is_independent(native_graph, monkeypatch):
    real_import = builtins.__import__
    attempts = []

    def guarded(name, *args, **kwargs):
        if name == "hdbscan":
            attempts.append(name)
            raise ImportError("synthetic unavailable optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    communities, neighbors = mapping.cluster_source_vectors(["A", "B"], [[1, 0], [0.9, 0.1]])
    assert communities and neighbors and not attempts
    with pytest.raises(mapping.LocalMappingError, match=r"\[local-mapping-hdbscan\]"):
        mapping._cluster_dependencies("hdbscan")


def test_cache_reuses_unchanged_sources_and_refreshes_map(native_graph, tmp_path):
    cache = tmp_path / "derived"
    rows = [_row("A"), _row("B")]
    sources = {"A": _text("A", "court evidence judges"), "B": _text("B", "garden flowers evidence")}
    original = deepcopy(sources)
    first_encoder = _Encoder()
    first = mapping.build_local_map(rows, cache_dir=cache, text_sources=sources, encoder_factory=lambda _: first_encoder)
    assert first["stats"]["encoded_chunks"] == 2 and len(first_encoder.calls) == 1
    second_encoder = _Encoder()
    second = mapping.build_local_map(rows[::-1], cache_dir=cache, text_sources=sources, encoder_factory=lambda _: second_encoder)
    assert second["stats"]["encoded_chunks"] == 0 and second["stats"]["reused_chunks"] == 2
    assert second_encoder.calls == []
    assert first["communities"] == second["communities"] and first["neighbors"] == second["neighbors"]
    sources["C"] = _text("C", "war evidence troops")
    third = mapping.build_local_map([*rows, _row("C")], cache_dir=cache, text_sources=sources, encoder_factory=lambda _: _Encoder())
    assert third["stats"]["encoded_chunks"] == 1 and third["stats"]["reused_chunks"] == 2
    assert {row["source_id"] for row in third["neighbors"]} == {"A", "B", "C"}
    sources["B"] = _text("B", "garden changed evidence")
    changed = mapping.build_local_map([*rows, _row("C")], cache_dir=cache, text_sources=sources, encoder_factory=lambda _: _Encoder())
    assert changed["stats"]["encoded_chunks"] == 1 and changed["stats"]["reused_chunks"] == 2
    assert original["A"] == sources["A"]
    assert changed["sources"][0]["note_hash"] == "semantic-note-hash"
    assert changed["sources"][0]["projection_hash"] == "actual-note-sha256"
    assert changed["sources"][0]["profile_hash"] == "semantic-profile-hash"


def test_scoped_discovery_reuses_embeddings_but_refreshes_membership_identity(native_graph, tmp_path):
    rows = [_row("A", collection_keys=["one"]), _row("B", collection_keys=["one"]), _row("C", collection_keys=["two"])]
    texts = {sid: _text(sid, "court evidence war") for sid in "ABC"}
    legacy = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=texts, encoder_factory=lambda _: _Encoder())
    encoder = _Encoder()
    scoped = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=texts, options={"discovery_scope": "collection_and_library"}, encoder_factory=lambda _: encoder)
    assert not encoder.calls and scoped["stats"]["encoded_chunks"] == 0
    assert [row["chunk_id"] for row in legacy["chunks"]] == [row["chunk_id"] for row in scoped["chunks"]]
    assert "discovery" not in legacy["identity"] and "discovery" in scoped["identity"]
    assert legacy["identity"]["digest"] != scoped["identity"]["digest"]
    assert all("source_chunk_ids" in row["provenance"] for row in scoped["neighbors"])
    assert all("source_chunk_ids" not in row["provenance"] for row in scoped["candidate_neighbors"])
    memberships = scoped["identity"]["discovery"]["membership_sha256"]
    rows[1]["collection_keys"] = ["two"]
    fresh = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=texts, options={"discovery_scope": "collection_and_library", "tuning_mode": "auto"}, calibration_report={"historical": True}, encoder_factory=lambda _: encoder)
    assert fresh["stats"]["encoded_chunks"] == 0
    assert fresh["identity"]["discovery"]["membership_sha256"] != memberships
    assert fresh["tuning"]["calibration_status"] == "not_applied_discovery_scope_changed"
    assert fresh["tuning"]["status"] != "calibrated"
    assert fresh["scope_discovery_coverage"]["sources"] == 3


@pytest.mark.parametrize("change", ["model", "token_window", "library", "owner", "cleaning"])
def test_identity_changes_invalidate_only_affected_cache(native_graph, tmp_path, monkeypatch, change):
    rows = [_row("A"), _row("B")]
    sources = {sid: _text(sid, "court evidence") for sid in "AB"}
    mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    options = {}
    encoder = _Encoder()
    if change == "model":
        encoder = _Encoder(revision="two")
    elif change == "token_window":
        options["chunk_tokens"] = 128
    elif change == "library":
        rows[0] = _row("A", library="99")
    elif change == "cleaning":
        monkeypatch.setattr("auto_zettelkasten.mapping_text.CLEANING_VERSION", "synthetic-next-cleaning-version")
    else:
        sources["A"]["ownership_spans"][0]["owner"] = "different-author"
    result = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, options=options, encoder_factory=lambda _: encoder)
    assert result["stats"]["encoded_chunks"] == (2 if change in {"model", "token_window", "cleaning"} else 1)


def test_cleaned_embedding_retains_full_reference_text_and_exact_ownership(native_graph, tmp_path):
    text = "# Introduction\nCourt evidence explains judging.\n# References\nSynthetic 2001. Full citation.\n"
    source = {"text": text, "text_origin": "synthetic_full_document", "coverage": {"status": "partial"}}
    encoder = _Encoder()
    result = mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"A": source}, encoder_factory=lambda _: encoder)
    encoded = " ".join(piece for batch in encoder.calls for piece in batch)
    assert "Court evidence" in encoded and "Full citation" not in encoded
    saved = result["sources"][0]
    assert (tmp_path / saved["text_cache_file"]).read_text() == text
    assert saved["coverage"]["cleaning"]["ownership_status"] == "unresolved"
    for chunk in result["chunks"]:
        assert chunk["owner"] == ""  # A book's catalogue owner is not an inferred chapter author.
        assert hashlib.sha256(text[chunk["start"]:chunk["end"]].encode()).hexdigest() == chunk["text_sha256"]
    assert source["text"] == text
    assert result["relationship_status"] == "derived_suggestions_only"


def test_token_partition_covers_all_content_without_sampling(native_graph, tmp_path):
    words = [f"word{index}" for index in range(1_000)]
    text = " ".join(words)
    encoder = _Encoder()
    result = mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"A": _text("A", text)},
                                     options={"chunk_tokens": 16, "batch_size": 3}, encoder_factory=lambda _: encoder)
    encoded_words = [word for batch in encoder.calls for piece in batch for word in piece.split()]
    assert encoded_words == words
    assert all(len(batch) <= 3 for batch in encoder.calls)
    assert result["stats"]["encoded_chunks"] == 72
    assert all(len(piece.split()) + 2 <= 16 for batch in encoder.calls for piece in batch)


def test_interruption_preserves_last_result_and_resumes_committed_rows(native_graph, tmp_path):
    original_rows = [_row("A")]
    original_sources = {"A": _text("A", "court evidence")}
    mapping.build_local_map(original_rows, cache_dir=tmp_path, text_sources=original_sources, encoder_factory=lambda _: _Encoder())
    saved = (tmp_path / "result.json").read_bytes()
    rows = [*original_rows, _row("B"), _row("C")]
    sources = {**original_sources, "B": _text("B", "garden evidence"), "C": _text("C", "war evidence")}
    with pytest.raises(mapping.LocalMappingError, match="interruption"):
        mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, options={"batch_size": 1}, encoder_factory=lambda _: _Encoder(fail_after=1))
    assert (tmp_path / "result.json").read_bytes() == saved
    assert (tmp_path / "job.lock").is_file()  # The released OS-lock inode is reusable after failure.
    encoder = _Encoder()
    resumed = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, options={"batch_size": 1}, encoder_factory=lambda _: encoder)
    assert resumed["stats"]["reused_chunks"] == 2 and resumed["stats"]["encoded_chunks"] == 1


def test_bad_checkpoint_and_changed_input_prevent_result_commit(native_graph, tmp_path):
    rows, sources = [_row("A")], {"A": _text("A", "court evidence")}
    result = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    saved = (tmp_path / "result.json").read_bytes()

    def changed(_):
        raise mapping.LocalMappingError("input changed")

    with pytest.raises(mapping.LocalMappingError, match="input changed"):
        mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder(), before_commit=changed)
    assert (tmp_path / "result.json").read_bytes() == saved
    binary = next((tmp_path / "chunks").rglob("*.f32"))
    binary.write_bytes(bytes(len(binary.read_bytes())))
    with pytest.raises(mapping.LocalMappingError, match="Invalid mapping checkpoint"):
        mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    assert (tmp_path / "result.json").read_bytes() == saved


def test_tampered_full_text_cache_is_not_reused(native_graph, tmp_path):
    rows, sources = [_row("A")], {"A": _text("A", "court evidence")}
    result = mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    saved = (tmp_path / "result.json").read_bytes()
    text_cache = tmp_path / result["sources"][0]["text_cache_file"]
    text_cache.write_text("court tampered")  # Same-size mutation also needs an actual hash check.
    with pytest.raises(mapping.LocalMappingError, match="full-text cache failed"):
        mapping.build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    assert (tmp_path / "result.json").read_bytes() == saved


def test_scope_missing_text_limits_and_no_fallback_provider(native_graph, tmp_path):
    called = []
    empty = mapping.build_local_map([_row("A")], cache_dir=tmp_path, encoder_factory=lambda _: called.append(True))
    assert not called and empty["stats"]["included_sources"] == 0
    assert empty["sources"][0]["coverage"]["status"] == "unavailable"
    with pytest.raises(ValueError, match="catalogue source IDs"):
        mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"B": _text("B", "test")})
    with pytest.raises(ValueError, match="ownership differs"):
        mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"A": _text("B", "test")})
    with pytest.raises(mapping.LocalMappingError, match="does not download|never downloads"):
        mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"A": _text("A", "test")})
    with pytest.raises(mapping.LocalMappingError, match="max_text_bytes"):
        mapping.build_local_map([_row("A")], cache_dir=tmp_path, text_sources={"A": _text("A", "too much text")}, options={"max_text_bytes": 2})


def test_exact_seeded_clusters_are_input_order_independent(native_graph):
    ids = ["A", "B", "C", "D"]
    vectors = [[1, 0, 0], [0.98, 0.02, 0], [0, 1, 0], [0.01, 0.99, 0]]
    options = mapping.LocalMappingOptions(neighbors=1, min_similarity=0.5, seed=7)
    result = mapping.cluster_source_vectors(ids, vectors, options=options)
    reversed_result = mapping.cluster_source_vectors(ids[::-1], vectors[::-1], options=options)
    assert result == reversed_result
    assert {tuple(row["source_ids"]) for row in result[0]} == {("A", "B"), ("C", "D")}
    assert len(result[1]) == 4
    assert all(not row["provenance"]["accepted"] and not row["provenance"]["evidence_backed"] for row in result[1])


def test_active_cache_lock_and_symbolic_links_are_rejected(native_graph, tmp_path):
    with mapping._exclusive_cache(tmp_path):
        with pytest.raises(mapping.LocalMappingError, match="owns this cache"):
            mapping.build_local_map([], cache_dir=tmp_path)
    target = tmp_path / "source-note.txt"
    target.write_text("unchanged source")
    (tmp_path / "unsafe").symlink_to(target)
    with pytest.raises(mapping.LocalMappingError, match="symbolic links"):
        mapping.build_local_map([], cache_dir=tmp_path)
    assert target.read_text() == "unchanged source"
