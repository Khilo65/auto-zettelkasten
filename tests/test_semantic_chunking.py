"""Synthetic production chunking invariants for the derived semantic view."""
from copy import deepcopy
import hashlib

import pytest

from auto_zettelkasten.local_mapping import _semantic_chunks
from auto_zettelkasten.mapping_text import prepare_embedding_spans
from test_local_mapping import _Encoder, _Tokenizer, _row


def chunks(text, ownership=()):
    prepared = prepare_embedding_spans(text, ownership)
    return _semantic_chunks(text, prepared["spans"], _Tokenizer(), 256)


def test_rendered_equivalent_links_do_not_fragment_or_overweight_labels():
    plain = "## Thesis\nCourts do not reduce conflict without institutional safeguards.\n"
    linked = "## Thesis\n[[Hidden/unrelated|Courts]] do not reduce [conflict](https://example.invalid/irrelevant) without [[Other/path|institutional safeguards]].\n"
    changed_destination = linked.replace("Hidden/unrelated", "Quantum/mechanics").replace("example.invalid/irrelevant", "elsewhere.invalid/different").replace("Other/path", "Alien/topic")
    expected = [row["text"] for row in chunks(plain)]
    assert expected == [row["text"] for row in chunks(linked)] == [row["text"] for row in chunks(changed_destination)]
    assert len(expected) == 1
    assert "not" in expected[0] and "without" in expected[0]
    for text in (plain, linked, changed_destination):
        for row in chunks(text):
            assert "".join(text[part["start"]:part["end"]] for part in row["source_spans"]) == row["text"]
            assert row["start"] == row["source_spans"][0]["start"]
            assert row["end"] == row["source_spans"][-1]["end"]


def test_visible_content_changes_remain_semantic_changes():
    left = "## Thesis\n[[Hidden|Courts]] reduce conflict.\n"
    right = left.replace("Courts", "Gardens")
    assert [row["text"] for row in chunks(left)] != [row["text"] for row in chunks(right)]


def test_groups_do_not_cross_ownership_or_excluded_sections():
    text = "First argument. Second argument.\n## References\nAn external citation.\n## Findings\nA different finding.\n"
    cut = text.index("Second")
    owned = [{"start": 0, "end": cut, "owner": "Author A"}, {"start": cut, "end": len(text), "owner": "Author B"}]
    result = chunks(text, owned)
    assert [row["owner"] for row in result][:2] == ["Author A", "Author B"]
    assert all("An external citation" not in row["text"] for row in result)
    assert not any("Second argument" in row["text"] and "A different finding" in row["text"] for row in result)


def test_invalid_source_map_is_rejected():
    span = {"start": 0, "end": 5, "text": "wrong", "embedding_group": 0}
    with pytest.raises(ValueError, match="exact source"):
        _semantic_chunks("hello world", [span], _Tokenizer(), 256)


def test_equivalent_markup_reuses_actual_production_chunk_vectors(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    from auto_zettelkasten.local_mapping import build_local_map

    texts = ["## Thesis\nCourt evidence does not establish causation.\n",
             "## Thesis\n[[Invisible|Court]] evidence does not establish [causation](https://invalid.example/hidden).\n"]
    rows = [_row("A")]
    supplied = {"A": {"source_id": "A", "note_id": "note-A", "text": texts[0], "text_origin": "synthetic_note"}}
    original = deepcopy(supplied)
    first_encoder = _Encoder()
    first = build_local_map(rows, cache_dir=tmp_path, text_sources=supplied, encoder_factory=lambda _: first_encoder)
    supplied["A"]["text"] = texts[1]
    second_encoder = _Encoder()
    second = build_local_map(rows, cache_dir=tmp_path, text_sources=supplied, encoder_factory=lambda _: second_encoder)
    assert first["stats"]["encoded_chunks"] == 1
    assert second["stats"]["reused_chunks"] == 1 and not second_encoder.calls
    assert [row["chunk_id"] for row in first["chunks"]] == [row["chunk_id"] for row in second["chunks"]]
    chunk = second["chunks"][0]
    selected = "".join(texts[1][part["start"]:part["end"]] for part in chunk["source_spans"])
    assert hashlib.sha256(selected.encode()).hexdigest() == chunk["text_sha256"]
    assert original["A"]["text"] == texts[0]
