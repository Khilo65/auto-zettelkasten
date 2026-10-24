from __future__ import annotations

from copy import deepcopy

import pytest

from auto_zettelkasten import local_mapping as mapping


def _options(**values):
    return mapping.LocalMappingOptions(discovery_scope="collection_and_library", **values)


def _neighbors(result, sid, key="neighbors"):
    return [row for row in result[key] if row["source_id"] == sid]


def test_balanced_display_and_broad_pool_retain_lower_ranked_cross_collection_neighbors():
    pytest.importorskip("numpy")
    ids = [f"A{at:02d}" for at in range(70)] + [f"B{at:02d}" for at in range(70)]
    vectors = [[1, at / 10000] for at in range(70)] + [[.7, .7 + at / 10000] for at in range(70)]
    memberships = {sid: [sid[0]] for sid in ids}
    original = deepcopy((ids, vectors, memberships))
    result = mapping.scoped_source_neighbors(ids, vectors, memberships, options=_options(), embedding_identity="synthetic")
    for key, limit in (("neighbors", 8), ("candidate_neighbors", 64)):
        rows = _neighbors(result, "A00", key)
        assert len(rows) == limit
        assert sum(row["target_source_id"].startswith("A") for row in rows) == limit // 2
        assert sum(row["target_source_id"].startswith("B") for row in rows) == limit // 2
        assert len({(row["source_id"], row["target_source_id"]) for row in result[key]}) == len(result[key])
        assert all(not row["provenance"]["accepted"] and not row["provenance"]["evidence_backed"] for row in rows)
        for row in rows:
            within = row["target_source_id"].startswith("A")
            assert row["provenance"]["discovery_scopes"] == (["within_collection"] if within else ["whole_library"])
            assert row["provenance"]["shared_collection_keys"] == (["A"] if within else [])
    assert (ids, vectors, memberships) == original
    assert result["scope_identity"]["membership_sha256"]
    assert result["coverage"]["sources"] == 140


def test_memberships_and_input_order_are_deterministic_with_overlaps_and_unfiled_sources():
    pytest.importorskip("numpy")
    ids = ["A", "B", "C", "D"]
    vectors = [[1, 0], [1, 0], [1, 0], [1, 0]]
    memberships = {"A": ["x", "x", "y"], "B": ["x", "z"], "C": ["z"], "D": []}
    first = mapping.scoped_source_neighbors(ids, vectors, memberships, options=_options(neighbors=3))
    second = mapping.scoped_source_neighbors(ids[::-1], vectors[::-1], {sid: memberships[sid][::-1] for sid in ids[::-1]}, options=_options(neighbors=3))
    assert first == second
    by_target = {row["target_source_id"]: row for row in _neighbors(first, "A")}
    assert "within_collection" in by_target["B"]["provenance"]["discovery_scopes"]
    assert by_target["B"]["provenance"]["shared_collection_keys"] == ["x"]
    assert all(row["provenance"]["discovery_scopes"] == ["whole_library"] for row in _neighbors(first, "D"))
    assert first["coverage"]["unfiled_sources"] == 1


def test_single_collection_fills_global_capacity_and_merges_duplicate_proposals():
    pytest.importorskip("numpy")
    ids = [f"S{at}" for at in range(10)]
    result = mapping.scoped_source_neighbors(ids, [[1, 0]] * len(ids), {sid: ["only"] for sid in ids})
    rows = _neighbors(result, "S0")
    assert len(rows) == 8
    assert len({row["target_source_id"] for row in rows}) == 8
    assert any(row["provenance"]["discovery_scopes"] == ["whole_library", "within_collection"] for row in rows)
    assert all(row["provenance"]["shared_collection_keys"] == ["only"] for row in rows)


def test_thresholds_never_force_negative_or_weak_bridges_and_small_scopes_remain_valid():
    pytest.importorskip("numpy")
    ids, vectors = ["A", "B", "C"], [[1, 0], [1, .01], [-1, 0]]
    result = mapping.scoped_source_neighbors(ids, vectors, {"A": ["a"], "B": ["a"], "C": ["c"]}, options=_options(min_similarity=.99))
    assert {row["target_source_id"] for row in _neighbors(result, "A")} == {"B"}
    assert not _neighbors(result, "C")
    assert mapping.scoped_source_neighbors(["A"], [[1, 0]], {"A": []})["neighbors"] == []
    assert mapping.scoped_source_neighbors([], [], {})["candidate_neighbors"] == []
    weak = mapping.scoped_source_neighbors(["A", "B"], [[1, 0], [.4, .9]], {"A": ["a"], "B": ["b"]}, options=_options(min_similarity=.8))
    assert not weak["neighbors"] and weak["candidate_neighbors"]


@pytest.mark.parametrize("ids,vectors,memberships", [
    (["A", "A"], [[1, 0], [1, 0]], {"A": []}),
    (["A"], [[1, 0]], {"B": []}),
    (["A"], [[1, 0]], {"A": "collection"}),
    (["A"], [[1, 0]], {"A": ["bad\nkey"]}),
    (["A"], [[1, 0]], {"A": ["x"] * 65}),
    (["A"], [[1, 0], [1, 0]], {"A": []}),
    (["A"], [[float("nan"), 0]], {"A": []}),
    (["A"], [[0, 0]], {"A": []}),
])
def test_scoped_input_boundaries_reject_invalid_ownership_and_vectors(ids, vectors, memberships):
    pytest.importorskip("numpy")
    with pytest.raises((ValueError, mapping.LocalMappingError)):
        mapping.scoped_source_neighbors(ids, vectors, memberships)


def test_runtime_guard_and_source_limit_apply_before_partial_output(monkeypatch):
    pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="max_sources"):
        mapping.scoped_source_neighbors(["A", "B"], [[1, 0], [1, 0]], {}, options=_options(max_sources=1))
    def guard():
        raise mapping.LocalMappingError("synthetic runtime safeguard")
    with pytest.raises(mapping.LocalMappingError, match="safeguard"):
        mapping.scoped_source_neighbors(["A"], [[1, 0]], {}, check_resources=guard)
    times = iter([0, 1])
    monkeypatch.setattr(mapping.time, "monotonic", lambda: next(times))
    with pytest.raises(mapping.LocalMappingError, match="max_wall_seconds"):
        mapping.scoped_source_neighbors(["A"], [[1, 0]], {}, options=_options(max_wall_seconds=.1))


def test_membership_changes_alter_only_the_discovery_identity():
    pytest.importorskip("numpy")
    first = mapping.scoped_source_neighbors(["A", "B"], [[1, 0], [1, 0]], {"A": ["x"], "B": ["x"]})
    second = mapping.scoped_source_neighbors(["A", "B"], [[1, 0], [1, 0]], {"A": ["x"], "B": ["y"]})
    assert first["scope_identity"]["membership_sha256"] != second["scope_identity"]["membership_sha256"]
    assert mapping.LocalMappingOptions().discovery_scope == "library"
    for value in ("collection", None, True):
        with pytest.raises(ValueError, match="discovery_scope"):
            mapping.LocalMappingOptions.from_dict({"discovery_scope": value})
