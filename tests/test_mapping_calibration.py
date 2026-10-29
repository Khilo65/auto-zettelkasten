from copy import deepcopy
from dataclasses import replace
import json

import pytest

from auto_zettelkasten.local_mapping import LocalMappingError, LocalMappingOptions
from auto_zettelkasten import mapping_calibration as calibration


@pytest.fixture
def native_graph():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")


def _data():
    np = pytest.importorskip("numpy")
    ids = [f"source-{number:02d}" for number in range(20)]
    collections = {sid: ["elections" if index < 10 else "mediation"] for index, sid in enumerate(ids)}
    held = set(calibration.stratified_holdout(collections))
    train = [sid for sid in ids if sid not in held]
    vectors = np.asarray([[1., .03 * index, float(index >= 10)] for index in range(20)])
    pairs = [(train[0], train[1], "useful"), (train[0], train[-1], "unsupported"),
             (min(held), train[0], "useful")]
    reviews = [{"pair": [left, right], "label": label, "reviewer_id": reviewer, "score_blinded": True}
               for left, right, label in pairs for reviewer in ("reviewer-a", "reviewer-b")]
    return ids, vectors, collections, reviews


def _calibrate(**kwargs):
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    ids, vectors, collections, reviews = _data()
    return calibration.calibrate_mapping(ids, vectors, collections, reviews,
        embedding_identity="synthetic-model-v1", source_fingerprint="synthetic-evidence-v1", **kwargs)


def test_holdout_whole_works_stratified_stable_and_input_order_independent():
    collections = {f"work-{index}": ["A" if index < 10 else "B"] for index in range(20)}
    held = calibration.stratified_holdout(collections)
    assert len(held) == 4
    assert sum(collections[sid] == ["A"] for sid in held) == 2
    assert calibration.stratified_holdout(dict(reversed(list(collections.items())))) == held
    assert calibration.stratified_holdout({"single": ["A"]}) == []
    with pytest.raises(ValueError):
        calibration.stratified_holdout({"bad": "A"})


def test_evaluates_12_configs_nine_partitions_using_one_vector_search(monkeypatch):
    calls = []
    original = calibration._broad_neighbors
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(calibration, "_broad_neighbors", counted)
    report = _calibrate(clock=lambda: 0.)
    assert calls == [True]
    assert report["status"] == "complete"
    assert len(report["configurations"]) == 12
    assert [row["resolution"] for row in report["community_scales"]] == [.5, 1., 2.]
    assert all(row["seeds"] == [0, 1, 2] for row in report["community_scales"])
    assert report["selected_parameters"]["resolution"] == 1.
    assert report["selected_parameters"]["neighbors"] == 8
    assert "held_out" in report
    ids, _, _, _ = _data()
    assert calibration.admitted_parameters(report, embedding_identity="synthetic-model-v1",
        source_ids=ids, source_fingerprint="synthetic-evidence-v1") == report["selected_parameters"]
    assert report == _calibrate(clock=lambda: 0.)


def test_held_out_reviews_cannot_change_selected_policy_or_training_percentiles():
    ids, vectors, collections, reviews = _data()
    baseline = _calibrate(clock=lambda: 0.)
    held = set(baseline["holdout_source_ids"])
    for review in reviews:
        if set(review["pair"]) & held:
            review["label"] = "unsupported"
    changed = calibration.calibrate_mapping(ids, vectors, collections, reviews,
        embedding_identity="synthetic-model-v1", source_fingerprint="synthetic-evidence-v1", clock=lambda: 0.)
    assert baseline["selected_parameters"] == changed["selected_parameters"]
    assert baseline["configurations"] == changed["configurations"]
    assert baseline["held_out"] != changed["held_out"]


def test_disagreements_remain_uncertain_and_one_review_cannot_pass_gate():
    ids, vectors, collections, reviews = _data()
    reviews[1]["label"] = "unsupported"
    report = calibration.calibrate_mapping(ids, vectors, collections, reviews, embedding_identity="test")
    assert report["coverage"]["uncertain_pairs"] == 1
    assert report["status"] == "incomplete" and report["selected_parameters"] is None
    assert calibration.calibrate_mapping(ids, vectors, collections, reviews[::2], embedding_identity="test")["status"] == "incomplete"
    with pytest.raises(ValueError, match="two independent"):
        calibration.calibrate_mapping(ids, vectors, collections, [reviews[0], reviews[0]], embedding_identity="test")


def test_resource_interruption_retains_existing_parameters_and_partial_report():
    def stopped():
        raise LocalMappingError("synthetic disk safeguard")
    report = _calibrate(check_resources=stopped)
    assert report["status"] == "incomplete" and report["selected_parameters"] is None
    assert "disk safeguard" in report["incomplete_reason"]


def test_admission_rejects_different_model_scope_evidence_mutation_and_incomplete():
    report = _calibrate(clock=lambda: 0.)
    ids, _, _, _ = _data()
    args = dict(embedding_identity="synthetic-model-v1", source_ids=ids, source_fingerprint="synthetic-evidence-v1")
    for key, value in (("embedding_identity", "other"), ("source_ids", ids[:-1]), ("source_fingerprint", "changed")):
        assert calibration.admitted_parameters(report, **{**args, key: value}) is None
    altered = deepcopy(report)
    altered["selected_parameters"]["neighbors"] = 64
    assert calibration.admitted_parameters(altered, **args) is None
    altered = deepcopy(report)
    altered["status"] = "incomplete"
    assert calibration.admitted_parameters(altered, **args) is None


def test_tuning_mode_preserves_historical_defaults_and_rejects_unknown():
    assert LocalMappingOptions.from_mapping({"neighbors": 16}).tuning_mode == "manual"
    assert LocalMappingOptions.from_mapping({"tuning_mode": "auto"}).tuning_mode == "auto"
    with pytest.raises(ValueError, match="tuning_mode"):
        LocalMappingOptions.from_mapping({"tuning_mode": "train-model"})


def test_cached_vectors_reuse_checkpoints_without_encoder_and_fail_corruption(tmp_path):
    import hashlib
    np = pytest.importorskip("numpy")
    from auto_zettelkasten.local_mapping import _digest
    identity = {"encoder": {"dimension": 2}, "cleaning_version": "synthetic", "chunking_version": "synthetic",
                "chunk_tokens": 32, "aggregation": "mean_l2_chunk_vectors_then_l2"}
    embedding = _digest(identity)
    chunk_id = "a" * 64
    directory = tmp_path / "chunks" / embedding
    directory.mkdir(parents=True)
    binary = np.asarray([.6, .8], dtype="<f4").tobytes()
    (directory / (chunk_id + ".f32")).write_bytes(binary)
    receipt = {"chunk_id": chunk_id, "embedding_identity": embedding, "vector_sha256": hashlib.sha256(binary).hexdigest()}
    (directory / (chunk_id + ".json")).write_text(json.dumps(receipt))
    result = {"identity": identity, "sources": [{"source_id": "A", "chunk_ids": [chunk_id]}],
              "chunks": [{"chunk_id": chunk_id, "source_id": "A"}]}
    (tmp_path / "result.json").write_text(json.dumps(result))
    loaded = calibration.load_cached_source_vectors(tmp_path)
    assert loaded["source_ids"] == ["A"]
    assert np.allclose(loaded["vectors"], [[.6, .8]])
    assert loaded["embedding_identity"] == embedding
    assert loaded["stats"]["encoder_loads"] == 0
    assert loaded["source_fingerprint"] == calibration.source_fingerprint(result["sources"])
    saved = (tmp_path / "result.json").read_bytes()
    (directory / (chunk_id + ".f32")).write_bytes(b"invalid")
    with pytest.raises(LocalMappingError, match="validation"):
        calibration.load_cached_source_vectors(tmp_path)
    assert (tmp_path / "result.json").read_bytes() == saved


def test_auto_gate_reuses_embeddings_and_adds_scales_without_touching_notes(native_graph, tmp_path):
    from test_local_mapping import _Encoder, _row, _text
    from auto_zettelkasten.local_mapping import build_local_map
    ids, _, collections, reviews = _data()
    sources = {sid: _text(sid, f"court evidence {sid}") for sid in ids}
    rows = [_row(sid, collection_keys=collections[sid]) for sid in ids]
    first = build_local_map(rows, cache_dir=tmp_path, text_sources=sources, encoder_factory=lambda _: _Encoder())
    loaded = calibration.load_cached_source_vectors(tmp_path)
    report = calibration.calibrate_mapping(loaded["source_ids"], loaded["vectors"], collections, reviews,
        embedding_identity=loaded["embedding_identity"], source_fingerprint=loaded["source_fingerprint"],
        projection_rows=rows, clock=lambda: 0.)
    encoder = _Encoder()
    auto = build_local_map(rows, cache_dir=tmp_path, text_sources=sources,
        encoder_factory=lambda _: encoder, options={"tuning_mode": "auto"}, calibration_report=report)
    assert auto["tuning"]["status"] == "calibrated"
    assert encoder.calls == [] and auto["stats"]["encoded_chunks"] == 0
    assert [row["scale"] for row in auto["community_scales"]] == ["broad", "default", "fine"]
    assert len(auto["candidate_neighbors"]) == len(ids) * (len(ids) - 1)
    assert all("source_chunk_ids" not in row["provenance"] for row in auto["candidate_neighbors"])
    assert all(row["provenance"]["source_chunk_ids"] for row in auto["neighbors"])
    assert all(row["display_label"] for view in auto["community_scales"] for row in view["communities"])
    manual = build_local_map(rows, cache_dir=tmp_path, text_sources=sources,
        encoder_factory=lambda _: _Encoder(), calibration_report=report)
    assert manual["tuning"]["status"] == "manual" and manual["community_scales"] == []
    assert first["identity"]["encoder"] == auto["identity"]["encoder"]
    changed = deepcopy(report)
    changed["selected_parameters"]["neighbors"] = 64
    rejected = build_local_map(rows, cache_dir=tmp_path, text_sources=sources,
        encoder_factory=lambda _: _Encoder(), options={"tuning_mode": "auto"}, calibration_report=changed)
    assert rejected["tuning"]["status"] == "evaluation_gate_not_passed"
    assert rejected["identity"]["clustering"]["neighbors"] == 8


def test_held_out_vectors_cannot_change_training_policy():
    np = pytest.importorskip("numpy")
    ids, vectors, collections, reviews = _data()
    baseline = _calibrate(clock=lambda: 0.)
    for index, sid in enumerate(ids):
        if sid in baseline["holdout_source_ids"]:
            vectors[index] = np.asarray([-.8, .9, -.7])
    changed = calibration.calibrate_mapping(ids, vectors, collections, reviews, embedding_identity="test", clock=lambda: 0.)
    assert changed["configurations"] == baseline["configurations"]
    assert changed["selected_parameters"] == baseline["selected_parameters"]


def test_training_top64_not_cut_off_by_held_out_neighbors(native_graph):
    np = pytest.importorskip("numpy")
    ids = [f"source-{number:03d}" for number in range(100)]
    collections = {sid: ["A" if index < 50 else "B"] for index, sid in enumerate(ids)}
    held = set(calibration.stratified_holdout(collections))
    train = [sid for sid in ids if sid not in held]
    rng = np.random.default_rng(5)
    vectors = rng.uniform(.1, 1., (100, 8))
    pairs = [(train[0], train[-1], "useful"), (train[2], train[3], "unsupported"),
             (min(held), train[0], "useful")]
    reviews = [{"pair": [left, right], "label": label, "reviewer_id": reviewer, "score_blinded": True}
               for left, right, label in pairs for reviewer in ("reviewer-a", "reviewer-b")]
    first = calibration.calibrate_mapping(ids, vectors, collections, reviews, embedding_identity="synthetic", clock=lambda: 0.)
    for index, sid in enumerate(ids):
        if sid in held:
            vectors[index] = vectors[ids.index(train[0])]
    changed = calibration.calibrate_mapping(ids, vectors, collections, reviews, embedding_identity="synthetic", clock=lambda: 0.)
    assert first["status"] == changed["status"] == "complete"
    assert first["configurations"] == changed["configurations"]
    assert first["selected_parameters"] == changed["selected_parameters"]


@pytest.mark.parametrize("field,value", [("source_ids", [["bad"]]), ("reviews", [None])])
def test_boundary_validation_is_explicit(field, value):
    ids, vectors, collections, reviews = _data()
    kwargs = dict(source_ids=ids, vectors=vectors, collections=collections, reviews=reviews, embedding_identity="synthetic")
    kwargs[field] = value
    with pytest.raises(ValueError):
        calibration.calibrate_mapping(**kwargs)


def test_new_auto_library_checks_resolution_without_review_or_model_refit(native_graph, tmp_path):
    from test_local_mapping import _Encoder, _row, _text
    from auto_zettelkasten.local_mapping import build_local_map
    encoder = _Encoder()
    result = build_local_map([_row("A"), _row("B")], cache_dir=tmp_path,
        text_sources={"A": _text("A", "court evidence"), "B": _text("B", "garden evidence")},
        encoder_factory=lambda _: encoder, options={"tuning_mode": "auto"})
    assert len(encoder.calls) == 1
    assert result["tuning"]["status"] == "default_policy_local_stability_checked"
    assert len(result["tuning"]["community_checks"]) == 3
    assert result["tuning"]["selected_parameters"]["neighbors"] == 8
    assert result["tuning"]["selected_parameters"]["min_similarity"] == 0
    assert len(result["community_scales"]) == 3
    assert "reviewed discovery calibration" in " ".join(result["tuning"]["limitations"])


def test_citation_signals_bound_to_allowed_split_and_report_identity(native_graph, monkeypatch):
    ids, vectors, collections, reviews = _data()
    held = calibration.stratified_holdout(collections)
    train = [sid for sid in ids if sid not in held]
    citations = [{"source_id": train[0], "target_source_id": train[-1], "match_status": "mapped"},
                 {"source_id": held[0], "target_source_id": train[1], "match_status": "mapped"}]
    seen = []
    original = calibration.LocalRetrievalIndex
    def index(rows, discovery_records=(), **kwargs):
        seen.append((set(row["source_id"] for row in rows), list(kwargs.get("citation_records", []))))
        return original(rows, discovery_records, **kwargs)
    monkeypatch.setattr(calibration, "LocalRetrievalIndex", index)
    report = calibration.calibrate_mapping(ids, vectors, collections, reviews,
        embedding_identity="synthetic", citation_records=citations, clock=lambda: 0.)
    assert report["status"] == "complete"
    # One fingerprint validation over all works, 12 independent training selectors,
    # then a held-out evaluation against all endpoints.
    assert len(seen) == 14
    assert all({row["source_id"], row["target_source_id"]} <= allowed for allowed, records in seen for row in records)
    assert all(records for allowed, records in seen)
    assert all(len(records) == 1 for allowed, records in seen if len(allowed) < len(ids))
    baseline = _calibrate(clock=lambda: 0.)
    assert report["identity"]["ranking_inputs_sha256"] != baseline["identity"]["ranking_inputs_sha256"]
    assert report["calibration_identity"] != baseline["calibration_identity"]
