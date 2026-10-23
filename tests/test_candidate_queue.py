from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from auto_zettelkasten.candidate_queue import CandidateQueue, queue_scope


def queue(root):
    return CandidateQueue(root, queue_scope("synthetic", "0", None))


def candidates():
    return [{"left_source_id": "A", "right_source_id": target, "semantic_score": 0.9 - n / 100,
             "discovery_provenance": [{"signal": "semantic_similarity", "embedding_identity": "synthetic"}]}
            for n, target in enumerate("BCDEFGHIJKLM")]


def bindings():
    return {sid: {"note_sha256": hashlib.sha256(sid.encode()).hexdigest(), "profile_hash": sid} for sid in "ABCDEFGHIJKLM"}


def test_continuation_reaches_beyond_first_endpoint_allocation_and_preserves_provenance(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    first = q.select(per_source_cap=8, max_pairs=256)
    assert len(first) == 8
    assert q.summary()["unexamined_count"] == 12
    assert q.select(per_source_cap=8, max_pairs=256) == first  # preview cannot advance
    for index, row in enumerate(first):
        q.record(row, "accepted" if index == 0 else "negative")
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    second = q.select(per_source_cap=8, max_pairs=256)
    assert len(second) == 4
    assert set(row["right_source_id"] for row in first).isdisjoint(row["right_source_id"] for row in second)
    assert second[0]["discovery_provenance"][0]["signal"] == "semantic_similarity"
    assert q.summary()["counts"] == {"accepted": 1, "negative": 7, "unexamined": 4}
    assert q.summary()["unfinished"] is True


def test_ranking_alone_reuses_and_changed_evidence_only_invalidates_affected_pairs(tmp_path):
    q = queue(tmp_path)
    pool = candidates()
    q.sync(pool, bindings=bindings(), contract={"policy": "v1"})
    first = q.select(per_source_cap=8, max_pairs=1)[0]
    q.record(first, "negative")
    reordered = list(reversed([{**row, "semantic_score": 0.5} for row in pool]))
    q.sync(reordered, bindings=bindings(), contract={"policy": "v1"})
    assert q.summary()["counts"]["negative"] == 1
    altered = bindings()
    altered[first["right_source_id"]]["note_sha256"] = "changed"
    q.sync(reordered, bindings=altered, contract={"policy": "v1"})
    assert q.summary()["counts"] == {"unexamined": 12}
    assert q.summary()["historical_count"] == 1


def test_contract_changes_keep_history_and_unknown_dispatch_never_retries(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    first = q.select(per_source_cap=8, max_pairs=1)[0]
    q.record(first, "unknown_dispatch")
    assert first["right_source_id"] not in {row["right_source_id"] for row in q.select(per_source_cap=20, max_pairs=256)}
    assert q.summary()["unfinished"]
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v2"})
    assert q.summary()["counts"] == {"unexamined": 12}


def test_endpoint_caps_reset_each_pass_and_failed_nondispatched_work_rotates(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    first = q.select(per_source_cap=1, max_pairs=256)
    q.record(first[0], "unexamined", detail={"status": "budget_exhausted"})
    second = q.select(per_source_cap=1, max_pairs=256)
    assert second[0]["right_source_id"] != first[0]["right_source_id"]
    assert max(Counter(sid for row in second for sid in (row["left_source_id"], row["right_source_id"])).values()) == 1


def test_scopes_do_not_leak_and_removed_candidates_remain_historical(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    scoped = CandidateQueue(tmp_path, queue_scope("synthetic", "0", []))
    scoped.sync([], bindings={}, contract={"policy": "v1"})
    assert scoped.select(per_source_cap=8, max_pairs=256) == []
    q.sync([], bindings={}, contract={"policy": "v1"})
    assert q.summary()["total"] == 0 and q.summary()["historical_count"] == 12


def test_concurrent_progress_writes_and_invalid_binding(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    selected = q.select(per_source_cap=20, max_pairs=256)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda row: q.record(row, "deferred"), selected))
    assert q.summary()["counts"] == {"deferred": 12}
    with pytest.raises(ValueError, match="current queued evidence"):
        q.record({**selected[0], "queue_evidence_identity": "fabricated"}, "accepted")


def test_symlink_cache_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".auto-zettelkasten").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        queue(tmp_path)


def test_low_disk_does_not_discard_saved_progress(tmp_path, monkeypatch):
    from collections import namedtuple
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    before = q.summary()
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("auto_zettelkasten.candidate_queue.shutil.disk_usage", lambda _: usage(100, 99, 1))
    with pytest.raises(ValueError, match="free disk"):
        q.sync([], bindings={}, contract={"policy": "v1"})
    assert q.summary() == before


def test_external_resolution_excluded_before_caps_and_removal_restores_candidate(tmp_path):
    q = queue(tmp_path)
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"}, resolved_pairs=[("B", "A")])
    assert q.select(per_source_cap=1, max_pairs=1)[0]["right_source_id"] == "C"
    assert q.summary()["counts"]["resolved"] == 1
    q.sync(candidates(), bindings=bindings(), contract={"policy": "v1"})
    assert q.select(per_source_cap=1, max_pairs=1)[0]["right_source_id"] == "B"
