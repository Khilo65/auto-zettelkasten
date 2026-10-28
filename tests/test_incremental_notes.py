import hashlib
import json
import re
import socket

import pytest

from auto_zettelkasten.documents import DocumentService
from auto_zettelkasten.incremental_notes import IncrementalNoteDiscovery
from auto_zettelkasten.mapping_text import prepare_note_embedding_spans
from auto_zettelkasten.note_recall import NoteRecallService, _corpus, _digest, _sha
from auto_zettelkasten.zotero_annotations import api_to_reader, project_annotations


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
        self.options, self.calls = options, []

    def encode(self, texts):
        self.calls.extend(texts)
        return [[1., 0., 0.] for _ in texts]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("local discovery must stay offline"))
    root = tmp_path / "notes"; root.mkdir()
    docs = DocumentService(override_root=tmp_path / "overrides")
    opened = docs.open(root)
    encoders = []
    def factory(options, _cache):
        encoder = Encoder(options); encoders.append(encoder)
        return encoder
    recall = NoteRecallService(encoder_factory=factory)
    discovery = IncrementalNoteDiscovery(tmp_path / "private", recall=recall)
    yield root, docs, opened["id"], recall, discovery, encoders
    recall.close(); docs.close()


def write(root, sid, body="A substantive shared finding.", collection="one", title=None):
    path = root / f"{sid}.md"
    path.write_text(f"---\nsource_id: {sid}\ntitle: {title or sid}\ncollection_keys: [{collection}]\n---\n\n{body}\n")
    return path


def groups(quote="Distinctquotedneedle finding", comment="Personalneedle perspective", color="#ffd400", version=1):
    record = {"key": "ANNT2345", "version": version, "itemType": "annotation", "parentItem": "PDFK2345",
              "annotationType": "highlight", "annotationText": quote, "annotationComment": comment,
              "annotationPageLabel": "458", "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[0, 0, 10, 20]]}),
              "annotationColor": color, "annotationSortIndex": "00000|000001", "annotationIsExternal": False, "tags": []}
    return [{"scope": "users/0", "attachment_key": "PDFK2345", "title": "Example source", "confirmed": True,
             "annotations": [api_to_reader(record, scope="users/0", attachment_key="PDFK2345")]}]


def test_metadata_changes_preserve_vectors_and_personal_prose_invalidates(setup):
    root, docs, wid, recall, _, encoders = setup
    path = write(root, "a")
    recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    count = len(encoders[0].calls)
    path.write_text(path.read_text().replace("title: a", "title: My personal title\ntags: [changed]"))
    result = recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    assert result["reused_note_count"] == 1 and len(encoders[0].calls) == count
    path.write_text(path.read_text() + "New personal argument.")
    result = recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    assert result["reused_note_count"] == 0 and len(encoders[0].calls) > count


def test_legacy_receipt_reused_without_rewriting_on_metadata_change(setup):
    root, docs, wid, recall, _, _ = setup
    path = write(root, "a")
    recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    receipt = next((root / ".auto-zettelkasten/recall/vectors").glob("*/*.json"))
    record = json.loads(receipt.read_text())
    record.pop("key_version"); record.pop("semantic_sha256"); record.pop("record_sha256")
    record["key"] = _digest({key: record[key] for key in ("embedding_identity", "source_id", "note_path", "note_content_sha256", "content_sha256")})
    record["record_sha256"] = _digest(record)
    old = receipt.with_name(record["key"] + ".json")
    old.write_text(json.dumps(record)); receipt.unlink()
    before = old.read_bytes()
    path.write_text(path.read_text().replace("title: a", "title: New title"))
    result = recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    assert result["reused_note_count"] == 1 and old.read_bytes() == before


def test_annotations_are_searchable_with_exact_pointers_but_do_not_reembed_source(setup):
    root, docs, wid, recall, _, encoders = setup
    path = write(root, "a")
    recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    count = len(encoders[0].calls)
    snapshot = groups()
    projection = project_annotations(path.read_text(), snapshot)
    path.write_text(projection["text"])
    receipt = {"block_hash": projection["block_hash"], "groups": snapshot}
    recall.annotation_resolver = lambda _root, _path: receipt
    result = recall.recall(root, "Distinctquotedneedle", documents=docs, workspace_id=wid)
    note, = result["notes"]
    pointer, = note["annotation_pointers"]
    assert pointer["page_index"] == 0 and pointer["page_label"] == "458"
    assert pointer["uri"] == "zotero://open-pdf/library/items/PDFK2345?page=1&annotation=ANNT2345"
    assert "Source quotation:" in note["text"] and "Personal comment:" in note["text"]
    result = recall.prepare(root, model_path="synthetic", documents=docs, workspace_id=wid)
    assert result["reused_note_count"] == 1 and len(encoders[0].calls) == count
    assert all("Distinctquotedneedle" not in text for text in encoders[0].calls)
    assert recall.recall(root, "Personalneedle", documents=docs, workspace_id=wid)["notes"]
    bounded = recall.recall(root, "Personalneedle", documents=docs, workspace_id=wid, max_chars=1100)
    assert bounded["chars"] <= 1100


def test_unverified_or_edited_annotation_block_keeps_prose_without_asserting_pointers(setup):
    root, docs, wid, recall, _, _ = setup
    path = write(root, "a")
    projection = project_annotations(path.read_text(), groups())
    edited = projection["text"].replace("Distinctquotedneedle", "Manualchange")
    path.write_text(edited)
    recall.annotation_resolver = lambda *_: {"block_hash": projection["block_hash"], "groups": groups()}
    result = recall.recall(root, "Manualchange", documents=docs, workspace_id=wid)
    assert result["notes"] and not result["notes"][0].get("annotation_pointers")
    cleaned = prepare_note_embedding_spans(edited, annotation_hash=projection["block_hash"])
    assert "Manualchange" in "".join(span["text"] for span in cleaned["spans"])


def test_duplicate_tree_entry_does_not_duplicate_note_corpus(setup):
    root, docs, wid, _, _, _ = setup
    write(root, "a")
    snapshot = docs.recall_corpus(wid)
    snapshot["documents"] *= 2
    rows, _, excluded = _corpus(snapshot)
    assert len(rows) == 1 and excluded["duplicate_path"] == 1


def test_enqueue_coalesces_without_loading_encoder_and_incomplete_inventory_cannot_delete(setup, monkeypatch):
    root, docs, wid, _, discovery, encoders = setup
    path = write(root, "a")
    assert discovery.enqueue(root, documents=docs, workspace_id=wid)["pending_note_count"] == 1
    path.write_text(path.read_text() + "A later draft saved.")
    assert discovery.enqueue(root, documents=docs, workspace_id=wid)["pending_note_count"] == 1
    assert not encoders and not (root / ".auto-zettelkasten").exists()
    monkeypatch.setattr(docs, "recall_corpus", lambda _: {"truncated": True})
    with pytest.raises(ValueError, match="Incomplete"):
        discovery.enqueue(root, documents=docs, workspace_id=wid)


def test_bounded_refresh_bridges_collections_and_suppresses_stale_endpoints(setup):
    pytest.importorskip("numpy")
    root, docs, wid, recall, discovery, encoders = setup
    for sid, collection in (("a", "one"), ("b", "one"), ("c", "two")):
        write(root, sid, collection=collection)
    originals = {path.name: path.read_bytes() for path in root.glob("*.md")}
    options = {"neighbors": 2, "min_similarity": .5, "min_free_disk_bytes": 1024 * 1024}
    first = discovery.refresh(root, documents=docs, workspace_id=wid, model_path="synthetic", max_changed_notes=1, options=options)
    assert first["updated_note_count"] == 1 and first["pending_note_count"] == 2
    final = discovery.refresh(root, documents=docs, workspace_id=wid, model_path="synthetic", options=options)
    # Initial source a only had one compatible vector; explicitly requeue it
    # now that the remaining sources have vectors.
    write(root, "a", "An updated substantive shared finding.", collection="one")
    discovery.enqueue(root, documents=docs, workspace_id=wid)
    discovery.refresh(root, documents=docs, workspace_id=wid, model_path="synthetic", options=options)
    edges = discovery.projection(root, documents=docs, workspace_id=wid)["relationships"]
    assert any({edge["source"], edge["target"]} == {"a.md", "c.md"} for edge in edges)
    assert any("within_collection" in edge["provenance"]["discovery_scopes"] for edge in edges)
    assert all(not edge["accepted"] and edge["derived"] for edge in edges)
    assert all((root / name).read_bytes() == data for name, data in originals.items() if name != "a.md")
    assert not (root / ".auto-zettelkasten/mapping").exists()
    write(root, "c", "A changed claim.", collection="two")
    assert all("c.md" not in (edge["source"], edge["target"]) for edge in discovery.projection(root, documents=docs, workspace_id=wid)["relationships"])
    assert discovery.enqueue(root, documents=docs, workspace_id=wid)["pending_note_count"] >= 2
    assert final["provider_calls"] == 0 and not final["jev"]["enabled"]


def test_projection_is_workspace_isolated(setup, tmp_path):
    root, docs, wid, _, discovery, _ = setup
    write(root, "a")
    discovery.enqueue(root, documents=docs, workspace_id=wid)
    other = tmp_path / "other"; other.mkdir()
    with pytest.raises(ValueError, match="does not match"):
        discovery.projection(other, documents=docs, workspace_id=wid)


def test_saved_calibration_is_reused_and_identity_checked(setup):
    root, *_ = setup
    directory = root / ".auto-zettelkasten/mapping"; directory.mkdir(parents=True)
    identity = {"clustering": {"neighbors": 4, "min_similarity": .63, "resolution": .5, "seed": 0}}
    identity["digest"] = _digest(identity)
    path = directory / "result.json"
    path.write_text(json.dumps({"schema_version": "local-semantic-map-v1", "identity": identity, "sources": []}))
    policy, _ = IncrementalNoteDiscovery._policy(root)
    assert policy.neighbors == 4 and policy.min_similarity == .63 and policy.resolution == .5
    identity["clustering"]["neighbors"] = 128
    path.write_text(json.dumps({"schema_version": "local-semantic-map-v1", "identity": identity, "sources": []}))
    with pytest.raises(ValueError, match="identity validation"):
        IncrementalNoteDiscovery._policy(root)


def test_community_refresh_is_explicit_cached_and_tracks_current_revision(setup, monkeypatch):
    pytest.importorskip("numpy")
    from auto_zettelkasten import local_mapping
    root, docs, wid, _, discovery, encoders = setup
    write(root, "a"); write(root, "b")
    options = {"neighbors": 2, "min_similarity": .5, "min_free_disk_bytes": 1024 * 1024}
    with pytest.raises(ValueError, match="pending note discovery"):
        discovery.refresh_communities(root, documents=docs, workspace_id=wid, model_path="synthetic", options=options)
    discovery.refresh(root, documents=docs, workspace_id=wid, model_path="synthetic", options=options)
    calls = len(encoders[0].calls)
    monkeypatch.setattr(local_mapping, "cluster_source_vectors", lambda ids, *_a, **_k:
        ([{"community_id": "synthetic", "source_ids": ids, "algorithm": "leiden"}], []))
    result = discovery.refresh_communities(root, documents=docs, workspace_id=wid, model_path="synthetic", options=options)
    assert result["community_count"] == 1 and len(encoders[0].calls) == calls
    assert not (root / ".auto-zettelkasten/mapping").exists()
    projection = discovery.projection(root, documents=docs, workspace_id=wid)
    assert not projection["community_refresh_pending"]
    assert projection["community_overlay"]["communities"][0]["paths"] == ["a.md", "b.md"]
    path = root / "a.md"; path.write_text(path.read_text().replace("title: a", "title: New title"))
    fresh = discovery.projection(root, documents=docs, workspace_id=wid)["community_overlay"]
    assert fresh["note_revisions"]["a.md"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(path.read_text() + "A new claim.")
    assert discovery.projection(root, documents=docs, workspace_id=wid)["community_overlay"] is None
