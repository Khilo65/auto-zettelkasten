"""Synthetic current-evidence tests for every-pass scoped paid admission."""
from copy import deepcopy
import json

import pytest

from auto_zettelkasten.candidate_queue import CandidateQueue, queue_scope
from auto_zettelkasten.mapping_calibration import _digest, source_fingerprint
from auto_zettelkasten.mapping_candidates import CandidateAdmissionError, _evidence_bindings, prepare_mapping_candidates
from auto_zettelkasten.retrieval import LocalRetrievalIndex
from auto_zettelkasten import scoped_calibration as calibration
from test_scoped_calibration import _data, _community_reviews


@pytest.fixture
def current_policy(tmp_path, monkeypatch):
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    ids, vectors, collections, rows, reviews = _data()
    root = tmp_path / "synthetic-memory"
    root.mkdir()
    (root / "notes").mkdir()
    for row in rows:
        row.update(note_path=f"notes/{row['note_id']}.md", profile_hash="synthetic-profile")
        (root / row["note_path"]).write_text("Synthetic research evidence for " + row["source_id"])
    loaded = {"workspace": str(root), "rows": rows, "index": LocalRetrievalIndex(rows),
              "status": {"instance_id": "synthetic", "library_id": "0"},
              "relationships": [], "discovery_records": [], "citation_records": []}
    embedding = {"encoder": {"model": "synthetic-pinned"}, "cleaning_version": "synthetic-v1",
                 "chunking_version": "synthetic-v1", "chunk_tokens": 128, "aggregation": "synthetic"}
    sources = [{"source_id": sid, "note_content_sha256": _digest(sid)} for sid in ids]
    report = calibration.calibrate_scoped_mapping(
        ids, vectors, collections, reviews, embedding_identity=_digest(embedding),
        source_fingerprint=source_fingerprint(sources), projection_rows=rows,
        evidence_bindings=_evidence_bindings(root, rows), clock=lambda: 0.)
    report = calibration.finalize_scoped_calibration(report, _community_reviews(report))
    assert report["status"] == "complete"
    cache = root / ".auto-zettelkasten" / "mapping"
    cache.mkdir(parents=True)
    (cache / "result.json").write_text(json.dumps({"identity": embedding, "sources": sources}))
    path = cache / "scoped-calibration.json"
    path.write_text(json.dumps(report))
    # Admission uses the checksum-validating cache loader. This fixture supplies
    # a synthetic cache snapshot; actual checkpoint corruption is covered by
    # the cache-only refresh integration tests.
    from auto_zettelkasten import mapping_calibration
    monkeypatch.setattr(mapping_calibration, "load_cached_source_vectors", lambda cache: {
        "result": json.loads((cache / "result.json").read_text()), "source_ids": ids, "vectors": vectors})
    neighbors = [{"source_id": left, "target_source_id": right, "score": .9,
                  "provenance": {"embedding_identity": _digest(embedding),
                                 "discovery_scopes": ["within_collection" if collections[left] == collections[right] else "whole_library"],
                                 "shared_collection_keys": collections[left] if collections[left] == collections[right] else []}}
                 for left in ids for right in ids if left != right]
    return root, loaded, report, path, neighbors


def prepare(data, **kwargs):
    root, loaded, _, _, neighbors = data
    return prepare_mapping_candidates(root, neighbors, loaded=loaded, persist_queue=True,
                                      decision_contract={"model": "synthetic-pinned", "template": "synthetic-v1"},
                                      fanout=64, **kwargs)


def test_real_sealed_policy_filters_every_continuation_before_caps(current_policy):
    root, _, report, _, _ = current_policy
    allowed = set(map(tuple, report["admission"]["allowed_pairs"]))
    first = prepare(current_policy, per_source_cap=1, max_pairs=2)
    queue = CandidateQueue(root, queue_scope("synthetic", "0", None))
    assert first["admission"]["status"] == "calibrated"
    assert first["queue"]["outside_policy_unexamined_count"] > 0
    seen = set()
    for _ in range(100):
        prepared = prepare(current_policy, per_source_cap=1, max_pairs=2)
        pairs = {(row["left_source_id"], row["right_source_id"]) for row in prepared["candidates"]}
        assert pairs <= allowed
        assert not pairs & seen
        for row in prepared["candidates"]:
            queue.record(row, "negative")
        queue.finish_pass()
        seen.update(pairs)
        if not pairs:
            break
    assert seen == allowed
    progress = queue.summary(allowed_pairs=list(allowed))
    assert progress["admitted_unexamined_count"] == 0
    assert progress["outside_policy_unexamined_count"] > 0
    assert progress["counts"]["negative"] == len(allowed)
    assert progress["total"] > len(allowed)  # Broad inventory was not deleted or marked negative.


@pytest.mark.parametrize("change", ["note", "metadata", "ranking", "membership", "model", "tamper", "scope", "vectors"])
def test_stale_policy_blocks_without_changing_queue_or_history(current_policy, change, monkeypatch):
    root, loaded, _, path, _ = current_policy
    first = prepare(current_policy)
    queue = CandidateQueue(root, queue_scope("synthetic", "0", None))
    queue.record(first["candidates"][0], "accepted", detail={"historical": True})
    before = queue.summary()
    if change == "note":
        (root / loaded["rows"][0]["note_path"]).write_text("Changed actual evidence")
    elif change == "metadata":
        metadata = root / "11_state/note_metadata"
        metadata.mkdir(parents=True)
        (metadata / (loaded["rows"][0]["note_id"] + ".yml")).write_text("attribution: changed")
    elif change in {"ranking", "membership"}:
        rows = deepcopy(loaded["rows"])
        rows[0]["tags" if change == "ranking" else "collection_keys"] = ["changed"]
        loaded.update(rows=rows, index=LocalRetrievalIndex(rows))
    elif change in {"model", "scope"}:
        saved_path = path.parent / "result.json"
        saved = json.loads(saved_path.read_text())
        if change == "model":
            saved["identity"]["encoder"]["model"] = "foreign-model"
        else:
            saved["sources"].pop()
        saved_path.write_text(json.dumps(saved))
    elif change == "vectors":
        from auto_zettelkasten import mapping_calibration
        original = mapping_calibration.load_cached_source_vectors
        def changed(cache):
            value = original(cache)
            value["vectors"] = value["vectors"].copy()
            value["vectors"][0] = [0., 1., 0.]
            return value
        monkeypatch.setattr(mapping_calibration, "load_cached_source_vectors", changed)
    else:
        report = json.loads(path.read_text())
        report["admission"]["allowed_pairs"] = []
        path.write_text(json.dumps(report))
    with pytest.raises(CandidateAdmissionError, match="admission blocked"):
        prepare(current_policy)
    assert queue.summary() == before


@pytest.mark.parametrize("target", ["scoped-calibration.json", "result.json"])
def test_internal_symlink_cannot_hide_behind_confined_resolution(current_policy, target):
    _, _, _, path, _ = current_policy
    original = path.parent / target
    backup = path.parent / (target + ".saved")
    original.rename(backup)
    original.symlink_to(backup)
    with pytest.raises(CandidateAdmissionError, match="symbolic links"):
        prepare(current_policy)


def test_missing_policy_preserves_legacy_and_nonpersistent_selection_obeys_policy(current_policy):
    root, loaded, report, path, neighbors = current_policy
    selected = prepare_mapping_candidates(root, neighbors, loaded=loaded, fanout=64, per_source_cap=1, max_pairs=2)
    allowed = set(map(tuple, report["admission"]["allowed_pairs"]))
    assert {(row["left_source_id"], row["right_source_id"]) for row in selected["candidates"]} <= allowed
    assert selected["stats"]["outside_policy_pairs"] > 0
    path.unlink()
    legacy = prepare(current_policy)
    assert legacy["admission"] == {"status": "legacy_unfiltered"}
    assert "admitted_counts" not in legacy["queue"]


def test_invalid_admission_blocks_relationship_route_before_client(current_policy, monkeypatch):
    from auto_zettelkasten import jev_batches, local_tools
    from auto_zettelkasten.relationship_mapping import run_relationship_mapping
    root, loaded, _, path, neighbors = current_policy
    path.write_text("{}")
    monkeypatch.setattr(local_tools, "load_local_retrieval", lambda *args, **kwargs: loaded)
    monkeypatch.setattr(jev_batches, "JevBatchClient", lambda *args, **kwargs: pytest.fail("must not create provider client"))
    report = run_relationship_mapping(root, neighbors, options={"backend": "jev", "dry_run": True})
    assert report["status"] == "blocked" and report["provider_calls"] == 0
    assert not (root / ".auto-zettelkasten/candidate-queue.sqlite3").exists()
    assert not (root / ".auto-zettelkasten/jev-decisions.sqlite3").exists()


def test_empty_admission_never_falls_back_to_broad_inventory(current_policy):
    root, _, report, path, _ = current_policy
    report = deepcopy(report)
    report["admission"].update(allowed_pairs=[], allowed_pairs_sha256=_digest([]), admitted_count=0)
    path.write_text(json.dumps(calibration._seal(report)))
    selected = prepare(current_policy)
    assert selected["candidates"] == []
    assert selected["queue"]["total"] > 0
    assert selected["queue"]["admitted_unexamined_count"] == 0
    queue = CandidateQueue(root, queue_scope("synthetic", "0", None))
    queue.finish_pass()
    assert prepare(current_policy)["candidates"] == []


@pytest.mark.parametrize("scoped", [False, True])
def test_queue_filters_before_endpoint_caps_on_later_passes(tmp_path, scoped):
    queue = CandidateQueue(tmp_path, queue_scope("synthetic", "0", None))
    pool = [{"left_source_id": "A", "right_source_id": sid} for sid in "BCD"]
    if scoped:
        for row in pool:
            row.update(comparison_scope="whole_library", discovery_scopes=["whole_library"], shared_collection_keys=[])
    queue.sync(pool, bindings={sid: {"note_sha256": sid} for sid in "ABCD"}, contract={"model": "synthetic"})
    selected = queue.select(per_source_cap=1, max_pairs=1, allowed_pairs=[("A", "C"), ("A", "D")])
    assert selected[0]["right_source_id"] == "C"
    queue.record(selected[0], "negative")
    queue.finish_pass()
    selected = queue.select(per_source_cap=1, max_pairs=1, allowed_pairs=[("A", "C"), ("A", "D")])
    assert selected[0]["right_source_id"] == "D"
    queue.record(selected[0], "negative")
    queue.finish_pass()
    assert queue.select(per_source_cap=1, max_pairs=1, allowed_pairs=[("A", "C"), ("A", "D")]) == []
    assert queue.summary()["unexamined_count"] == 1
