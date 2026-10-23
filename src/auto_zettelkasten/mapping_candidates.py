"""Bounded candidate fusion over current memory and already cached citations.

No source reading, provider request, relationship acceptance or graph write takes
place here. A candidate is an unexamined comparison, not a relationship claim.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_tools import load_local_retrieval
from .retrieval import CandidateResourceError, LocalRetrievalIndex


def prepare_mapping_candidates(
    workspace: Path | str, semantic_neighbors: Sequence[Mapping[str, Any]], *,
    per_source_cap: int = 8, max_pairs: int = 256, fanout: int = 32,
    resolved_pairs: Sequence[tuple[str, str]] = (), source_ids: Sequence[str] | None = None,
    instance_id: str | None = None, library_id: str | None = None,
    loaded: Mapping[str, Any] | None = None,
    preferred_semantic_neighbors: Sequence[Mapping[str, Any]] | None = None,
    persist_queue: bool = False, decision_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read canonical memory once and fuse semantic and local per-source ranks.

    Explicit cached-decision exclusions are applied before selection caps. The
    returned statistics concern the bounded candidate pool, never all possible
    library pairs. A caller may reuse ``rows`` when building evidence packets,
    or supply its already loaded canonical retrieval snapshot to avoid rereading
    a large catalogue and all profile sidecars.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    if loaded is None:
        loaded = load_local_retrieval(root, instance_id=instance_id, library_id=library_id, infer_scope=True)
    elif (not isinstance(loaded, Mapping) or not isinstance(loaded.get("index"), LocalRetrievalIndex)
          or loaded.get("workspace") != str(root)
          or not isinstance(loaded.get("status"), Mapping)
          or any(not isinstance(loaded.get(key), (list, tuple)) for key in ("rows", "relationships", "discovery_records", "citation_records"))):
        raise ValueError("loaded candidates require a canonical local retrieval snapshot")
    index = loaded["index"]
    rows = loaded["rows"]
    if (len(rows) != len(index.rows)
            or any(not isinstance(row, Mapping) or not isinstance(row.get("source_id"), str)
                   or row["source_id"] not in index.rows
                   or any(row.get(key, "") != index.rows[row["source_id"]].get(key, "") for key in ("note_id", "profile_hash"))
                   for row in rows)
            or len({row["source_id"] for row in rows}) != len(rows)):
        raise ValueError("loaded candidate rows do not match their canonical retrieval index")
    for key, expected in (("instance_id", instance_id), ("library_id", library_id)):
        if expected is not None and (not isinstance(expected, str) or loaded["status"].get(key) != expected.strip()):
            raise ValueError("loaded candidate snapshot conflicts with requested library scope")
    candidates = index.fused_candidate_pairs(
        semantic_neighbors, per_source_cap=per_source_cap, max_pairs=max_pairs,
        fanout=fanout, source_ids=source_ids, resolved_pairs=() if persist_queue else resolved_pairs, retain_pool=persist_queue,
    )
    stats = dict(index.last_candidate_stats)
    progress = {}
    preferred = None
    if persist_queue:
        from .candidate_queue import CandidateQueue, queue_scope
        from .workspace import confined_child
        from .profiles import profile_sidecar_path
        if not isinstance(decision_contract, Mapping) or not decision_contract:
            raise ValueError("persisted candidates require a versioned decision contract")
        pool = index.last_candidate_pool
        if preferred_semantic_neighbors is not None:
            # Reproduce the evaluated selector for the first paid allocation.
            # Persist its additional local candidates too: alternating a short
            # semantic list can expose local signals absent from broad top-k.
            preferred_rows = index.fused_candidate_pairs(
                preferred_semantic_neighbors, per_source_cap=per_source_cap, max_pairs=max_pairs,
                fanout=fanout, source_ids=source_ids, resolved_pairs=resolved_pairs, retain_pool=True)
            preferred = [(row["left_source_id"], row["right_source_id"]) for row in preferred_rows]
            merged = {}
            for row in [*preferred_rows, *pool, *index.last_candidate_pool]:
                merged.setdefault((row["left_source_id"], row["right_source_id"]), row)
            pool = list(merged.values())
        if len(pool) > 100_000:
            raise CandidateResourceError("candidate pool exceeds the bounded queue; reduce source scope")
        used = {pair[key] for pair in pool for key in ("left_source_id", "right_source_id")}
        bindings, total_bytes = {}, 0
        for row in rows:
            sid = row["source_id"]
            if sid not in used:
                continue
            binding = {key: row.get(key, "") for key in ("source_id", "note_id", "note_path", "profile_hash")}
            # Actual note and attribution metadata changes invalidate affected
            # comparisons even when an old catalogue hash was not refreshed.
            note_path = row.get("note_path")
            paths = {"note_sha256": confined_child(root, note_path) if note_path else None,
                     "metadata_sha256": confined_child(root, "11_state/note_metadata", profile_sidecar_path(root, row["note_id"]).name)}
            for name, path in paths.items():
                if path is None or not path.exists():
                    binding[name] = "missing"
                    continue
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                    raise ValueError("candidate evidence must be a bounded canonical file")
                raw = path.read_bytes()
                total_bytes += len(raw)
                if len(raw) > 1024 * 1024 or total_bytes > 128 * 1024 * 1024:
                    raise CandidateResourceError("candidate evidence read budget exceeded")
                binding[name] = hashlib.sha256(raw).hexdigest()
            bindings[sid] = binding
        queue = CandidateQueue(root, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), source_ids))
        queue.sync(pool, bindings=bindings, contract=decision_contract, resolved_pairs=resolved_pairs)
        candidates = queue.select(per_source_cap=per_source_cap, max_pairs=max_pairs, preferred_pairs=preferred)
        progress = queue.summary()
        stats.update(returned_pairs=len(candidates), resolved_pair_count=len(resolved_pairs),
                     unexamined_candidate_pairs=max(0, progress["unexamined_count"] - len(candidates)),
                     selection_truncated=int(progress["unexamined_count"] > len(candidates)))
    return {"candidates": candidates, "stats": stats, "queue": progress,
            "status": {**loaded["status"],
                       "decision_status": "unexamined", "all_pairs_examined": False,
                       "acquisition_performed": False, "provider_calls": 0},
            "rows": loaded["rows"], "relationships": loaded["relationships"],
            "discovery_records": loaded["discovery_records"], "citation_records": loaded["citation_records"]}
