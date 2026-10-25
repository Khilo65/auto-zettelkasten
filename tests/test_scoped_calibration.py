from copy import deepcopy
from dataclasses import replace

import pytest

from auto_zettelkasten import scoped_calibration as calibration
from auto_zettelkasten.local_mapping import LocalMappingError, LocalMappingOptions


def _data():
    np = pytest.importorskip("numpy")
    ids = [f"work-{index:02d}" for index in range(24)]
    memberships = {sid: ["collection-a" if index < 12 else "collection-b"] for index, sid in enumerate(ids)}
    held = set(calibration.stratified_holdout(memberships))
    train = [sid for sid in ids if sid not in held]
    vectors = np.asarray([[1., index / 40., float(index >= 12)] for index in range(24)], dtype=np.float32)
    rows = [{"source_id": sid, "note_id": sid, "title": "Shared research terminology",
             "tags": ["common"], "collection_keys": memberships[sid]} for sid in ids]
    pairs = [(train[0], train[1], "useful"), (train[0], train[-1], "unsupported"),
             (min(held), train[0], "useful")]
    reviews = [{"pair": [left, right], "reviewer_id": reviewer, "label": label, "score_blinded": True}
               for left, right, label in pairs for reviewer in ("judge-a", "judge-b")]
    return ids, vectors, memberships, rows, reviews


def _run(*, reviews=None, vectors=None, rows=None, check_resources=None):
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    ids, original_vectors, memberships, original_rows, original_reviews = _data()
    return calibration.calibrate_scoped_mapping(ids, original_vectors if vectors is None else vectors,
        memberships, original_reviews if reviews is None else reviews,
        embedding_identity="synthetic-encoder", source_fingerprint="synthetic-notes",
        projection_rows=original_rows if rows is None else rows,
        evidence_bindings={sid: {"note_sha256": "synthetic-" + sid} for sid in ids},
        check_resources=check_resources, clock=lambda: 0.)


def _community_reviews(report, *, preferred=1.):
    return [{"configuration_id": scale["configuration_id"], "reviewer_id": reviewer,
             "source_ids": report["training_source_ids"], "score_blinded": True,
             **{key: 5 if scale["resolution"] == preferred else 3 for key in
                ("coherence", "separation", "coverage", "granularity_fit")}}
            for scale in report["community_scales"] for reviewer in ("community-a", "community-b")]


def _args(report):
    ids, _, memberships, rows, _ = _data()
    return dict(embedding_identity="synthetic-encoder", source_ids=ids, source_fingerprint="synthetic-notes",
                ranking_inputs_sha256=calibration.ranking_fingerprint(rows), collections=memberships,
                current_evidence_bindings_sha256=report["identity"]["evidence_bindings_sha256"])


def test_runs_twelve_actual_admission_policies_and_requires_semantic_community_review():
    report = _run()
    assert report["status"] == "awaiting_community_review"
    assert len(report["configurations"]) == 12
    assert sum(len(row["partitions"]) for row in report["community_scales"]) == 9
    train = set(report["training_source_ids"])
    for row in report["community_scales"]:
        for partition in row["partitions"]:
            assert {sid for group in partition for sid in group} == train
            assert sum(map(len, partition)) == len(train)
    assert calibration.admitted_scoped_policy(report, **_args(report)) is None
    completed = calibration.finalize_scoped_calibration(report, _community_reviews(report, preferred=2.))
    assert completed["status"] == "complete"
    assert completed["selected_parameters"]["resolution"] == 2.
    assert report["status"] == "awaiting_community_review"  # historical draft preserved
    policy = calibration.admitted_scoped_policy(completed, **_args(completed))
    assert policy["parameters"] == completed["selected_parameters"]
    assert policy["allowed_pairs"] == completed["admission"]["allowed_pairs"]


def test_admission_narrows_the_actual_fused_pool_without_losing_inventory_or_citation_bridge():
    np = pytest.importorskip("numpy")
    ids, _, memberships, rows, _ = _data()
    # Distinct opposing directions: ubiquitous tags and title terms still propose
    # cross-topic pairs, but cannot qualify them for paid decisions.
    vectors = np.asarray([[1., index / 100.] if index < 12 else [-1., index / 100.]
                          for index in range(24)])
    citation = {"source_id": ids[0], "target_source_id": ids[-1], "match_status": "exact_identifier",
                "match_basis": "doi", "locator": "synthetic-reference-1", "freshness": "current_declared_hashes"}
    pool = calibration.scoped_admission_pool(ids, vectors, memberships,
        parameters={"neighbors": 8, "min_similarity": .99}, projection_rows=rows, citation_records=[citation])
    inventory = {calibration._pair(row) for row in pool["inventory"]}
    admitted = {calibration._pair(row) for row in pool["admitted"]}
    semantic = {tuple(sorted((row["source_id"], row["target_source_id"]))) for row in pool["neighbors"]}
    bridge = (ids[0], ids[-1])
    assert bridge in admitted and bridge not in semantic
    assert admitted < inventory
    assert all(pair in semantic or pair == bridge for pair in admitted)
    assert {calibration._pair(row) for row in pool["first_pass"]} <= admitted
    assert any(row["comparison_scope"] == "whole_library" and calibration._pair(row) == bridge for row in pool["first_pass"])
    ambiguous = dict(citation, match_status="ambiguous")
    without_verified = calibration.scoped_admission_pool(ids, vectors, memberships,
        parameters={"neighbors": 8, "min_similarity": .99}, projection_rows=rows, citation_records=[ambiguous])
    assert bridge not in {calibration._pair(row) for row in without_verified["admitted"]}
    stale = dict(citation, freshness="recorded_unverified")
    stale_pool = calibration.scoped_admission_pool(ids, vectors, memberships,
        parameters={"neighbors": 8, "min_similarity": .99}, projection_rows=rows, citation_records=[stale])
    assert bridge not in {calibration._pair(row) for row in stale_pool["admitted"]}


def test_heldout_vectors_metadata_and_reviews_do_not_influence_training_ranks_or_selection():
    ids, vectors, memberships, rows, reviews = _data()
    baseline = _run()
    held = set(baseline["holdout_source_ids"])
    for index, sid in enumerate(ids):
        if sid in held:
            vectors[index] = [0., 0., 1.]
            rows[index]["title"] = "Entirely different held-out material"
            rows[index]["tags"] = ["changed"]
    for row in reviews:
        if set(row["pair"]) & held:
            row["label"] = "unsupported"
    changed = _run(vectors=vectors, rows=rows, reviews=reviews)
    assert changed["selected_parameters"] == baseline["selected_parameters"]
    assert changed["configurations"] == baseline["configurations"]
    assert changed["training_similarity_floors"] == baseline["training_similarity_floors"]
    assert changed["training_ranking_inputs_sha256"] == baseline["training_ranking_inputs_sha256"]
    assert changed["community_scales"] == baseline["community_scales"]
    assert changed["identity"] != baseline["identity"]
    assert changed["held_out"] != baseline["held_out"]


def test_missing_second_reviews_or_negative_controls_prevents_admission():
    _, _, _, _, reviews = _data()
    assert _run(reviews=reviews[::2])["status"] == "incomplete"
    for row in reviews:
        if row["label"] == "unsupported":
            row["label"] = "useful"
    report = _run(reviews=reviews)
    assert report["status"] == "incomplete"
    assert report["selected_parameters"] is None
    reviews = _data()[-1]
    reviews[1]["label"] = "unsupported"
    assert _run(reviews=reviews)["coverage"]["uncertain_pairs"] == 1


def test_runtime_rejects_tampering_and_every_stale_binding():
    report = _run()
    report = calibration.finalize_scoped_calibration(report, _community_reviews(report))
    args = _args(report)
    for key, value in (("embedding_identity", "another-encoder"), ("source_fingerprint", "edited-note"),
                       ("ranking_inputs_sha256", "changed-tag"), ("current_evidence_bindings_sha256", "edited-bytes"),
                       ("current_evidence_bindings_sha256", ""), ("source_ids", args["source_ids"][:-1]),
                       ("collections", {sid: ["different"] for sid in args["source_ids"]})):
        assert calibration.admitted_scoped_policy(report, **{**args, key: value}) is None
    for key in ("status", "selected_parameters", "admission"):
        changed = deepcopy(report)
        changed[key] = "tampered"
        assert calibration.admitted_scoped_policy(changed, **args) is None
    changed = deepcopy(report)
    del changed["identity"]["evidence_bindings_sha256"]
    calibration._seal(changed)
    assert calibration.admitted_scoped_policy(changed, **args) is None


@pytest.mark.parametrize("mutation", ["duplicate_reviewer", "heldout", "not_blind", "nan", "missing_metric"])
def test_community_quality_requires_two_independent_complete_blind_training_assessments(mutation):
    report = _run()
    reviews = _community_reviews(report)
    if mutation == "duplicate_reviewer":
        reviews[1]["reviewer_id"] = reviews[0]["reviewer_id"]
    elif mutation == "heldout":
        reviews[0]["source_ids"] = report["training_source_ids"] + report["holdout_source_ids"][:1]
    elif mutation == "not_blind":
        reviews[0]["score_blinded"] = False
    elif mutation == "nan":
        reviews[0]["coverage"] = float("nan")
    else:
        del reviews[0]["granularity_fit"]
    with pytest.raises(ValueError):
        calibration.finalize_scoped_calibration(report, reviews)


def test_resource_stop_retains_old_settings_and_boundaries_validate_inputs():
    def stopped():
        raise LocalMappingError("synthetic memory safeguard")
    report = _run(check_resources=stopped)
    assert report["status"] == "incomplete" and report["selected_parameters"] is None
    assert "memory safeguard" in report["incomplete_reason"]
    ids, vectors, memberships, rows, _ = _data()
    with pytest.raises(ValueError, match="tested neighbor"):
        calibration.scoped_admission_pool(ids, vectors, memberships,
            parameters={"neighbors": 9, "min_similarity": .5}, projection_rows=rows)
    with pytest.raises(ValueError, match="membership disagree"):
        calibration.scoped_admission_pool(ids, vectors, memberships,
            parameters={"neighbors": 8, "min_similarity": .5}, projection_rows=[dict(row, collection_keys=[]) for row in rows])
    with pytest.raises(ValueError, match="bounded source IDs"):
        calibration.scoped_admission_pool(ids, vectors, memberships,
            parameters={"neighbors": 8, "min_similarity": .5}, projection_rows=rows,
            options=replace(LocalMappingOptions(), max_sources=2))


def test_same_inputs_choose_same_policy_without_timing_tiebreaks():
    left = _run()
    right = _run()
    assert left == right
    assert left["selection_rule"] == "first_pass_useful_desc_unsupported_asc_full_pool_count_asc_stable_grid_order"


def test_inventory_bound_interrupts_without_activating_partially_selected_settings(monkeypatch):
    def exhausted(*args, **kwargs):
        raise calibration.CandidateResourceError("synthetic inventory bound")
    monkeypatch.setattr(calibration.LocalRetrievalIndex, "fused_candidate_pairs", exhausted)
    report = _run()
    assert report["status"] == "incomplete" and report["selected_parameters"] is None
    assert "inventory bound" in report["incomplete_reason"]
