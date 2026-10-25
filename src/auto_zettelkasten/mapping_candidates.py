"""Bounded candidate fusion over current memory and already cached citations.

No source reading, provider request, relationship acceptance or graph write takes
place here. A candidate is an unexamined comparison, not a relationship claim.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_tools import load_local_retrieval
from .local_mapping import LocalMappingError
from .retrieval import CandidateResourceError, LocalRetrievalIndex


class CandidateAdmissionError(ValueError):
    """A saved admission policy cannot safely authorize current comparisons."""


def _canonical_file(root, *parts):
    from .workspace import confined_child
    raw = root.joinpath(*parts)
    if any(path.is_symlink() for path in (raw, *raw.parents) if path != root and path.is_relative_to(root)):
        raise ValueError("candidate evidence and admission caches cannot use symbolic links")
    return confined_child(root, *parts)


def _evidence_bindings(root, rows, used=None):
    from .profiles import profile_sidecar_path
    bindings, total_bytes = {}, 0
    for row in rows:
        sid = row["source_id"]
        if used is not None and sid not in used:
            continue
        binding = {key: row.get(key, "") for key in ("source_id", "note_id", "note_path", "profile_hash")}
        note_path = row.get("note_path")
        paths = {"note_sha256": _canonical_file(root, note_path) if note_path else None,
                 "metadata_sha256": _canonical_file(root, "11_state/note_metadata", profile_sidecar_path(root, row["note_id"]).name)}
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
    return bindings


def _scoped_admission(root, loaded):
    """Validate current local evidence before touching a queue or paid client."""
    try:
        path = root / ".auto-zettelkasten" / "mapping" / "scoped-calibration.json"
        if not path.exists() and not path.is_symlink():
            return None, None
        path = _canonical_file(root, ".auto-zettelkasten", "mapping", "scoped-calibration.json")
        from .mapping_calibration import _digest, ranking_fingerprint, source_fingerprint, load_cached_source_vectors
        from .scoped_calibration import admitted_scoped_policy, source_vectors_fingerprint

        def read(path):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("admission cache must be a bounded ordinary file")
            raw = path.read_bytes()
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("admission cache exceeds its read bound")
            return json.loads(raw)

        report = read(path)
        saved = read(_canonical_file(root, ".auto-zettelkasten", "mapping", "result.json"))
        cached = load_cached_source_vectors(path.parent)
        if cached["result"] != saved:
            raise ValueError("cached vectors changed during admission")
        if (not isinstance(saved, Mapping) or not isinstance(saved.get("identity"), Mapping)
                or not isinstance(saved.get("sources"), list)
                or any(not isinstance(row, Mapping) or not isinstance(row.get("source_id"), str)
                       for row in saved["sources"])):
            raise ValueError("cached embedding result has invalid source identity data")
        rows = loaded["rows"]
        ids = sorted(row["source_id"] for row in rows)
        if sorted(row["source_id"] for row in saved["sources"]) != ids:
            raise ValueError("cached embedding scope differs from current source scope")
        embedding = {key: saved["identity"][key] for key in
                     ("encoder", "cleaning_version", "chunking_version", "chunk_tokens", "aggregation")}
        bindings = _evidence_bindings(root, rows)
        policy = admitted_scoped_policy(
            report, embedding_identity=_digest(embedding), source_ids=ids,
            source_fingerprint=source_fingerprint(saved["sources"]),
            ranking_inputs_sha256=ranking_fingerprint(rows, loaded["discovery_records"], loaded["citation_records"]),
            collections={row["source_id"]: row.get("collection_keys", []) for row in rows},
            current_evidence_bindings_sha256=_digest(bindings),
            current_vectors_sha256=source_vectors_fingerprint(cached["source_ids"], cached["vectors"]))
        if policy is None:
            raise ValueError("scoped calibration is invalid, incomplete or stale for current evidence and rankings")
        from .candidate_queue import _allowed_pairs
        _allowed_pairs(policy["allowed_pairs"])
        return policy, bindings
    except (OSError, ValueError, TypeError, KeyError, ImportError, LocalMappingError) as exc:
        raise CandidateAdmissionError(f"Scoped candidate admission blocked: {exc}. Recalibrate current saved inputs before paid selection.") from exc


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
    admission, bindings = _scoped_admission(root, loaded)
    allowed = None if admission is None else admission["allowed_pairs"]
    candidates = index.fused_candidate_pairs(
        semantic_neighbors, per_source_cap=per_source_cap, max_pairs=max_pairs,
        fanout=fanout, source_ids=source_ids, resolved_pairs=() if persist_queue else resolved_pairs,
        retain_pool=persist_queue or admission is not None,
    )
    stats = dict(index.last_candidate_stats)
    progress = {}
    preferred = None
    if persist_queue:
        from .candidate_queue import CandidateQueue, queue_scope
        if not isinstance(decision_contract, Mapping) or not decision_contract:
            raise ValueError("persisted candidates require a versioned decision contract")
        pool = index.last_candidate_pool
        if preferred_semantic_neighbors is not None and admission is None:
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
        if bindings is None:
            bindings = _evidence_bindings(root, rows, used)
        queue = CandidateQueue(root, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), source_ids))
        queue.sync(pool, bindings=bindings, contract=decision_contract, resolved_pairs=resolved_pairs)
        candidates = queue.select(per_source_cap=per_source_cap, max_pairs=max_pairs, preferred_pairs=preferred, allowed_pairs=allowed)
        progress = queue.summary(allowed_pairs=allowed)
        stats.update(returned_pairs=len(candidates), resolved_pair_count=len(resolved_pairs),
                     unexamined_candidate_pairs=max(0, progress["unexamined_count"] - len(candidates)),
                     selection_truncated=int(progress["unexamined_count"] > len(candidates)))
        if stats.get("scope_balanced"):
            stats.update(selected_within_collection=sum(row.get("comparison_scope") == "within_collection" for row in candidates),
                         selected_whole_library=sum(row.get("comparison_scope") == "whole_library" for row in candidates))
    elif admission is not None:
        from .retrieval import _select_scope_balanced
        permitted = set(map(tuple, allowed))
        pool = [row for row in index.last_candidate_pool if (row["left_source_id"], row["right_source_id"]) in permitted]
        if stats.get("scope_balanced"):
            candidates = _select_scope_balanced(lambda: iter(pool), per_source_cap=per_source_cap, max_pairs=max_pairs)
        else:
            candidates, counts = [], Counter()
            for row in pool:
                pair = row["left_source_id"], row["right_source_id"]
                if any(counts[sid] >= per_source_cap for sid in pair):
                    continue
                candidates.append({**row, "rank": len(candidates) + 1})
                counts.update(pair)
                if len(candidates) >= max_pairs:
                    break
        stats.update(returned_pairs=len(candidates), unexamined_admitted_pairs=max(0, len(pool) - len(candidates)),
                     outside_policy_pairs=len(index.last_candidate_pool) - len(pool))
    admission_status = {"status": "legacy_unfiltered"} if admission is None else {
        "status": "calibrated", "policy_identity": admission["policy_identity"],
        "allowed_pair_count": len(allowed), "admitted_selected": len(candidates),
        "unexamined_admitted_pairs": max(0, progress.get("admitted_unexamined_count", len(pool)) - len(candidates)),
        "outside_policy_unexamined_pairs": progress.get("outside_policy_unexamined_count", stats.get("outside_policy_pairs", 0))}
    if admission is not None:
        stats.update(admission_status)
    return {"candidates": candidates, "stats": stats, "queue": progress, "admission": admission_status,
            "_allowed_pairs": allowed,
            "status": {**loaded["status"],
                       "decision_status": "unexamined", "all_pairs_examined": False,
                       "acquisition_performed": False, "provider_calls": 0},
            "rows": loaded["rows"], "relationships": loaded["relationships"],
            "discovery_records": loaded["discovery_records"], "citation_records": loaded["citation_records"]}
