"""Offline, reviewed calibration over existing vectors; never a model trainer.

Reports contain derived private evidence and belong in a workspace's private
mapping cache, not in source control. No provider, document or Zotero IO occurs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
import hashlib
import json
import math
import time
from typing import Any

from .local_mapping import LocalMappingError, LocalMappingOptions, _cluster_dependencies, _numpy, _unit_vectors
from .retrieval import LocalRetrievalIndex

CALIBRATION_VERSION = "coverage-first-calibration-v1"
LIMITATIONS = ["AI-reviewed note evidence is not publication-level ground truth.",
               "Held-out works do not select discovery parameters.",
               "A bounded candidate pool cannot guarantee all useful relationships.",
               "Community stability measures repeatability, not substantive correctness."]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def stratified_holdout(collections: Mapping[str, Sequence[str]], *, fraction: float = .2) -> list[str]:
    """Hold out whole works, with deterministic handling of overlapping collections."""
    if not isinstance(collections, Mapping) or not 0 < fraction < 1:
        raise ValueError("collections must map source IDs to collection keys; fraction must lie between zero and one")
    strata: dict[str, list[str]] = defaultdict(list)
    for sid, keys in collections.items():
        if not isinstance(sid, str) or not sid or not isinstance(keys, (list, tuple)) or any(not isinstance(key, str) for key in keys):
            raise ValueError("collection membership requires source IDs and collection-key sequences")
        # One work occupies one stratum even when it belongs to two collections.
        strata[min(keys, default="uncollected")].append(sid)
    result = []
    for key, ids in sorted(strata.items()):
        ids.sort(key=lambda sid: (_digest([CALIBRATION_VERSION, key, sid]), sid))
        count = min(len(ids) - 1, max(1, math.ceil(len(ids) * fraction))) if len(ids) > 1 else 0
        result.extend(ids[:count])
    return sorted(result)


def _reviewed(reviews: Sequence[Mapping[str, Any]], ids: set[str]) -> tuple[dict[tuple[str, str], str], list[dict[str, Any]]]:
    if not isinstance(reviews, (list, tuple)) or len(reviews) > 100_000:
        raise ValueError("reviews must be a bounded sequence")
    grouped: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    material = []
    for row in reviews:
        if not isinstance(row, Mapping):
            raise ValueError("review must be an object")
        pair, reviewer, label = row.get("pair"), row.get("reviewer_id"), row.get("label")
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or pair[0] == pair[1]
                or any(not isinstance(sid, str) or sid not in ids for sid in pair) or not isinstance(reviewer, str) or not reviewer
                or not isinstance(label, str) or label not in {"useful", "unsupported", "uncertain"} or row.get("score_blinded") is not True):
            raise ValueError("reviews require known distinct endpoints, reviewer_id, score_blinded=true and a bounded label")
        key = tuple(sorted(pair))
        if reviewer in grouped[key]:
            raise ValueError("one reviewer cannot count as two independent reviews")
        grouped[key][reviewer] = label
        material.append({"pair": list(key), "reviewer_id": reviewer, "label": label, "score_blinded": True})
    labels = {}
    for key, labels_by_reviewer in grouped.items():
        values = list(labels_by_reviewer.values())
        labels[key] = values[0] if len(values) == 2 and len(set(values)) == 1 else "uncertain"
    return labels, sorted(material, key=lambda row: (row["pair"], row["reviewer_id"]))


def _broad_neighbors(ids, matrix, *, check, max_seconds, clock, training_ids):
    np = _numpy()
    neighbors, training_neighbors = [], []
    started = clock()
    for first in range(0, len(ids), 64):
        check()
        if clock() - started > max_seconds:
            raise LocalMappingError("Calibration reached max_wall_seconds; existing settings retained.")
        scores = matrix[first:first + 64] @ matrix.T
        for offset, values in enumerate(scores):
            index = first + offset
            ranked = sorted((other for other in range(len(ids)) if other != index and values[other] > 0),
                            key=lambda other: (-float(values[other]), ids[other]))[:64]
            neighbors.extend({"source_id": ids[index], "target_source_id": ids[other],
                              "score": min(1., float(values[other]))} for other in ranked)
            if ids[index] in training_ids:
                # Rank training endpoints independently. Held-out vectors must not
                # push a training candidate below the 64-neighbor cutoff.
                train_ranked = sorted((other for other in range(len(ids)) if other != index
                                       and ids[other] in training_ids and values[other] > 0),
                                      key=lambda other: (-float(values[other]), ids[other]))[:64]
                training_neighbors.extend({"source_id": ids[index], "target_source_id": ids[other],
                                           "score": min(1., float(values[other]))} for other in train_ranked)
    return neighbors, training_neighbors


def _filtered(neighbors, count, floor, allowed):
    used = Counter()
    result = []
    for row in neighbors:
        sid, target = row["source_id"], row["target_source_id"]
        if sid in allowed and target in allowed and row["score"] > floor and used[sid] < count:
            result.append(row)
            used[sid] += 1
    return result


def _partition(ids, neighbors, resolution, seed):
    if not ids:
        return []
    positions = {sid: index for index, sid in enumerate(ids)}
    edges = {}
    for row in neighbors:
        key = tuple(sorted((positions[row["source_id"]], positions[row["target_source_id"]])))
        edges[key] = max(edges.get(key, 0.), row["score"])
    if not edges:
        return [[sid] for sid in ids]
    igraph, leiden = _cluster_dependencies("leiden")
    keys = sorted(edges)
    graph = igraph.Graph(n=len(ids), edges=keys, directed=False)
    partition = leiden.find_partition(graph, leiden.RBConfigurationVertexPartition,
                                     weights=[edges[key] for key in keys], seed=seed,
                                     resolution_parameter=resolution, n_iterations=2)
    return sorted([sorted(ids[index] for index in group) for group in partition])


def _agreement(left, right, ids):
    """Adjusted Rand agreement without a second clustering dependency."""
    n = len(ids)
    if n < 2:
        return 1.
    a = {sid: group for group, members in enumerate(left) for sid in members}
    b = {sid: group for group, members in enumerate(right) for sid in members}
    cells = Counter((a[sid], b[sid]) for sid in ids)
    choose = lambda count: count * (count - 1) / 2
    observed = sum(choose(count) for count in cells.values())
    rows = sum(choose(count) for count in Counter(a.values()).values())
    cols = sum(choose(count) for count in Counter(b.values()).values())
    expected = rows * cols / choose(n)
    upper = (rows + cols) / 2
    return 1. if upper == expected else (observed - expected) / (upper - expected)


def _metrics(pairs, labels):
    selected = {tuple(sorted((row["left_source_id"], row["right_source_id"]))) for row in pairs}
    useful = {key for key, label in labels.items() if label == "useful"}
    unsupported = {key for key, label in labels.items() if label == "unsupported"}
    return {"selected_pairs": len(selected), "reviewed_useful": len(useful),
            "useful_recovered": len(useful & selected), "useful_missed": len(useful - selected),
            "unsupported_selected": len(unsupported & selected),
            "unreviewed_or_uncertain_selected": len(selected - useful - unsupported)}


def calibrate_mapping(
    source_ids: Sequence[str], vectors: Any, collections: Mapping[str, Sequence[str]],
    reviews: Sequence[Mapping[str, Any]], *, options: LocalMappingOptions | None = None,
    embedding_identity: str, source_fingerprint: str | None = None, projection_rows: Sequence[Mapping[str, Any]] | None = None,
    discovery_records: Sequence[Mapping[str, Any]] = (),
    citation_records: Sequence[Mapping[str, Any]] = (),
    check_resources: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Screen 12 discovery policies and nine Leiden runs using one vector matrix.

    ``reviews`` has two independently produced, score-blinded rows per pair,
    labelled useful/unsupported/uncertain. Disagreement stays uncertain. Training
    and holdout selectors are separate, so held-out pairs cannot consume training
    admission slots or influence learned similarity percentiles.
    """
    options = options or LocalMappingOptions()
    options.validate()
    if (not isinstance(source_ids, (list, tuple)) or len(source_ids) > options.max_sources
            or any(not isinstance(sid, str) or not sid or len(sid) > 256 for sid in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError("calibration requires unique bounded source IDs")
    if not isinstance(embedding_identity, str) or not embedding_identity:
        raise ValueError("embedding_identity is required")
    if set(collections) != set(source_ids):
        raise ValueError("collection membership must cover exactly the calibration works")
    ids = sorted(source_ids)
    np = _numpy()
    supplied = np.asarray(vectors, dtype=np.float32)
    if supplied.ndim != 2 or supplied.shape[1] < 1:
        raise ValueError("source vectors must be a nonempty-dimensional matrix")
    matrix = _unit_vectors(supplied, len(source_ids), supplied.shape[1])
    positions = {sid: index for index, sid in enumerate(source_ids)}
    matrix = matrix[[positions[sid] for sid in ids]]
    labels, review_material = _reviewed(reviews, set(ids))
    holdout = set(stratified_holdout(collections))
    train = set(ids) - holdout
    train_labels = {key: label for key, label in labels.items() if not set(key) & holdout}
    held_labels = {key: label for key, label in labels.items() if set(key) & holdout}
    rows = list(projection_rows) if projection_rows is not None else [
        {"source_id": sid, "note_id": sid, "title": sid, "collection_keys": list(collections[sid])} for sid in ids]
    if {row.get("source_id") for row in rows} != set(ids) or len(rows) != len(ids):
        raise ValueError("projection_rows must cover exactly the calibration works")
    ranking_identity = ranking_fingerprint(rows, discovery_records, citation_records)
    identity = {"version": CALIBRATION_VERSION, "embedding_identity": embedding_identity,
                "ranking_inputs_sha256": ranking_identity,
                "vectors_sha256": hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest(),
                "source_ids": ids, "source_fingerprint": source_fingerprint,
                "collections": {sid: sorted(collections[sid]) for sid in ids}, "reviews": review_material,
                "limits": {"per_source_cap": 8, "max_pairs": 256, "neighbors": [8, 16, 32, 64],
                           "resolutions": [.5, 1., 2.], "seeds": [0, 1, 2],
                           "max_sources": options.max_sources, "max_wall_seconds": options.max_wall_seconds,
                           "max_cache_bytes": options.max_cache_bytes, "min_free_disk_bytes": options.min_free_disk_bytes}}
    report = {"schema_version": CALIBRATION_VERSION, "calibration_identity": _digest(identity),
              "identity": identity, "embedding_identity": embedding_identity, "status": "incomplete",
              "selected_parameters": None, "holdout_source_ids": sorted(holdout),
              "coverage": {"works": len(ids), "training_works": len(train), "held_out_works": len(holdout),
                           "reviewed_pairs": len(labels), "training_reviewed_pairs": len(train_labels),
                           "held_out_reviewed_pairs": len(held_labels),
                           "uncertain_pairs": sum(label == "uncertain" for label in labels.values())},
              "limitations": list(LIMITATIONS), "configurations": [], "community_scales": []}
    # Gate coverage, not a particular winning outcome. One-sided reference sets
    # cannot distinguish missed useful links from broadly unsupported proposals.
    independent = all(len({row["reviewer_id"] for row in review_material if tuple(row["pair"]) == key}) == 2 for key in labels)
    if not independent or not {"useful", "unsupported"} <= set(train_labels.values()) or not held_labels:
        report["incomplete_reason"] = "requires_two_blind_reviews_and_training_useful_unsupported_plus_held_out_pairs"
        return report
    started = clock()
    def check():
        if check_resources:
            check_resources()
        if clock() - started > options.max_wall_seconds:
            raise LocalMappingError("Calibration reached max_wall_seconds; existing settings retained.")
    def select(neighbors, allowed):
        selected_discovery = [row for row in discovery_records if row["source_id"] in allowed]
        selected_citations = [row for row in citation_records if row["source_id"] in allowed and row["target_source_id"] in allowed]
        index = LocalRetrievalIndex([row for row in rows if row["source_id"] in allowed], selected_discovery,
                                    citation_records=selected_citations)
        return index.fused_candidate_pairs(neighbors, per_source_cap=8, max_pairs=256, fanout=64)
    try:
        check()
        broad, training_broad = _broad_neighbors(ids, matrix, check=check, max_seconds=options.max_wall_seconds, clock=clock, training_ids=train)
        positive_train = [row["score"] for row in training_broad]
        floors = [("disabled", 0.), ("positive_p25", float(np.percentile(positive_train, 25))),
                  ("positive_p50", float(np.percentile(positive_train, 50)))] if positive_train else [("disabled", 0.)]
        if len(floors) != 3:
            report["incomplete_reason"] = "no_positive_training_similarities"
            return report
        for count in (8, 16, 32, 64):
            for label, floor in floors:
                check()
                tick = clock()
                chosen = select(_filtered(training_broad, count, floor, train), train)
                elapsed = clock() - tick
                report["configurations"].append({"neighbors": count, "floor_kind": label, "min_similarity": floor,
                    "processing_seconds": elapsed, "training": _metrics(chosen, train_labels)})
        winner = min(report["configurations"], key=lambda row: (-row["training"]["useful_recovered"],
                     row["training"]["unsupported_selected"], row["processing_seconds"], row["neighbors"], row["floor_kind"]))
        # Held-out evaluation includes all endpoints, but never changes winner.
        held_neighbors = _filtered(broad, winner["neighbors"], winner["min_similarity"], set(ids))
        held_pairs = select(held_neighbors, set(ids))
        report["held_out"] = _metrics([row for row in held_pairs if {row["left_source_id"], row["right_source_id"]} & holdout], held_labels)
        train_neighbors = _filtered(training_broad, winner["neighbors"], winner["min_similarity"], train)
        train_ids = sorted(train)
        for resolution in (.5, 1., 2.):
            partitions = []
            for seed in (0, 1, 2):
                check()
                partitions.append(_partition(train_ids, train_neighbors, resolution, seed))
            stability = sum(_agreement(partitions[a], partitions[b], train_ids) for a, b in ((0, 1), (0, 2), (1, 2))) / 3
            report["community_scales"].append({"scale": { .5: "broad", 1.: "default", 2.: "fine"}[resolution],
                "resolution": resolution, "seeds": [0, 1, 2], "repeatability_adjusted_rand": stability,
                "community_counts": [len(partition) for partition in partitions],
                "hierarchy": False})
        best_scale = min(report["community_scales"], key=lambda row: (-row["repeatability_adjusted_rand"],
                         row["resolution"] != 1., row["resolution"]))
        check()
        report.update(status="complete", selected_parameters={"neighbors": winner["neighbors"],
                      "min_similarity": winner["min_similarity"], "resolution": best_scale["resolution"], "seed": 0},
                      evaluation_gate_passed=True, total_seconds=clock() - started)
    except LocalMappingError as exc:
        report.update(incomplete_reason=str(exc), total_seconds=clock() - started)
    if report["status"] == "complete":
        report["report_sha256"] = _digest(report)
    return report


def admitted_parameters(report: Mapping[str, Any], *, embedding_identity: str,
                        source_ids: Sequence[str], source_fingerprint: str, ranking_inputs_sha256: str | None = None) -> dict[str, Any] | None:
    """Fail closed on incomplete, mutated, foreign-model or malformed reports."""
    try:
        identity = report["identity"]
        payload = {key: value for key, value in report.items() if key != "report_sha256"}
        if report.get("report_sha256") != _digest(payload):
            return None
        if (report.get("schema_version") != CALIBRATION_VERSION or report.get("status") != "complete"
                or report.get("evaluation_gate_passed") is not True
                or report.get("embedding_identity") != embedding_identity
                or identity.get("embedding_identity") != embedding_identity
                or identity.get("source_ids") != sorted(source_ids)
                or identity.get("source_fingerprint") != source_fingerprint
                or ranking_inputs_sha256 is not None and identity.get("ranking_inputs_sha256") != ranking_inputs_sha256
                or report.get("calibration_identity") != _digest(identity)
                or not isinstance(report["coverage"], Mapping) or not isinstance(report["limitations"], list)):
            return None
        values = report["selected_parameters"]
        if not isinstance(values, Mapping) or set(values) != {"neighbors", "min_similarity", "resolution", "seed"}:
            return None
        if values["neighbors"] not in {8, 16, 32, 64} or values["resolution"] not in {.5, 1., 2.} or values["seed"] != 0:
            return None
        replace(LocalMappingOptions(), **values).validate()
        if len(report["configurations"]) != 12 or len(report["community_scales"]) != 3:
            return None
        return dict(values)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def source_fingerprint(sources: Sequence[Mapping[str, Any]]) -> str:
    """Bind a policy evaluation to the same note/profile evidence snapshot."""
    keys = ("source_id", "identity", "note_content_sha256", "projection_hash", "profile_hash",
            "profile_file_sha256", "metadata_file_sha256", "full_text_sha256", "embedding_text_sha256")
    return _digest(sorted(({key: source.get(key) for key in keys} for source in sources), key=lambda row: row["source_id"]))


def load_cached_source_vectors(cache_dir, *, options: LocalMappingOptions | None = None,
                               check_resources: Callable[[], None] | None = None) -> dict[str, Any]:
    """Reconstruct source means from checksummed chunk checkpoints, without an encoder.

    Only generated mapping-cache paths are read. Explicit resource bounds are
    applied before every file read; a corrupt checkpoint fails without rewriting
    the last complete map or silently dropping a source.
    """
    from pathlib import Path
    import re
    from .local_mapping import _digest as mapping_digest
    options = options or LocalMappingOptions()
    options.validate()
    root = Path(cache_dir).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise LocalMappingError("Calibration requires an existing non-symlink mapping cache.")
    started, read_bytes = time.monotonic(), 0
    def read(path, maximum):
        nonlocal read_bytes
        if check_resources:
            check_resources()
        if time.monotonic() - started > options.max_wall_seconds:
            raise LocalMappingError("Reading cached vectors reached max_wall_seconds.")
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
                raise LocalMappingError("Cached calibration input exceeds its bound or uses a symlink.")
            read_bytes += path.stat().st_size
            if read_bytes > options.max_cache_bytes:
                raise LocalMappingError("Reading cached calibration input reached max_cache_bytes.")
            return path.read_bytes()
        except OSError as exc:
            raise LocalMappingError("A cached calibration checkpoint is unavailable.") from exc
    try:
        result = json.loads(read(root / "result.json", min(options.max_cache_bytes, options.max_text_bytes)))
        identity = result["identity"]
        embedding = {key: identity[key] for key in ("encoder", "cleaning_version", "chunking_version", "chunk_tokens", "aggregation")}
        embedding_identity = mapping_digest(embedding)
        dimension = embedding["encoder"]["dimension"]
        sources, chunks = result["sources"], result["chunks"]
        if (type(dimension) is not int or not 1 <= dimension <= 4096 or not isinstance(sources, list)
                or len(sources) > options.max_sources or not isinstance(chunks, list) or len(chunks) > options.max_chunks):
            raise ValueError("invalid mapping dimensions or counts")
        by_chunk = {row["chunk_id"]: row for row in chunks}
        if len(by_chunk) != len(chunks):
            raise ValueError("duplicate chunk identities")
        chunk_dir = root / "chunks" / embedding_identity
        if (root / "chunks").is_symlink() or chunk_dir.is_symlink():
            raise ValueError("linked checkpoint directory")
        np = _numpy()
        ids, vectors = [], []
        all_ids = set()
        for source in sorted(sources, key=lambda row: row["source_id"]):
            sid, chunk_ids = source["source_id"], source["chunk_ids"]
            if not isinstance(sid, str) or not sid or sid in all_ids or not isinstance(chunk_ids, list) or len(set(chunk_ids)) != len(chunk_ids):
                raise ValueError("invalid cached source or chunks")
            all_ids.add(sid)
            total = np.zeros(dimension, dtype=np.float64)
            for chunk_id in chunk_ids:
                if not isinstance(chunk_id, str) or not re.fullmatch(r"[a-f0-9]{64}", chunk_id) or by_chunk[chunk_id]["source_id"] != sid:
                    raise ValueError("cached chunk crosses its source endpoint")
                receipt = json.loads(read(chunk_dir / (chunk_id + ".json"), 16 * 1024))
                binary = read(chunk_dir / (chunk_id + ".f32"), dimension * 4)
                if (receipt.get("chunk_id") != chunk_id or receipt.get("embedding_identity") != embedding_identity
                        or len(binary) != dimension * 4 or receipt.get("vector_sha256") != hashlib.sha256(binary).hexdigest()):
                    raise ValueError("cached vector checksum or identity differs")
                vector = np.frombuffer(binary, dtype="<f4")
                if not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector)) - 1.) > 1e-4:
                    raise ValueError("cached vector must be finite and normalized")
                total += vector
            if chunk_ids:
                norm = float(np.linalg.norm(total))
                if norm <= 1e-12:
                    raise ValueError("source mean has zero norm")
                ids.append(sid)
                vectors.append((total / norm).astype(np.float32))
        matrix = np.stack(vectors) if vectors else np.empty((0, dimension), dtype=np.float32)
        return {"source_ids": ids, "vectors": matrix, "embedding_identity": embedding_identity,
                "source_fingerprint": source_fingerprint(sources), "result": result,
                "stats": {"read_bytes": read_bytes, "read_seconds": time.monotonic() - started,
                          "encoder_loads": 0, "encoded_chunks": 0}}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LocalMappingError("Cached vectors failed calibration validation; existing data was preserved.") from exc


def community_resolution_checks(source_ids: Sequence[str], neighbors: Sequence[Mapping[str, Any]], *,
                                check_resources: Callable[[], None] | None = None) -> dict[str, Any]:
    """Bounded, unsupervised local checks for a new library's community view.

    This selects a repeatable resolution only. It does not tune the encoder or
    establish that a suggested connection is useful, and it cannot approve paid
    decisions or typed relationships.
    """
    ids = sorted(source_ids)
    scales = []
    for resolution in (.5, 1., 2.):
        partitions = []
        for seed in (0, 1, 2):
            if check_resources:
                check_resources()
            partitions.append(_partition(ids, neighbors, resolution, seed))
        stability = sum(_agreement(partitions[a], partitions[b], ids) for a, b in ((0, 1), (0, 2), (1, 2))) / 3
        scales.append({"scale": {.5: "broad", 1.: "default", 2.: "fine"}[resolution],
                       "resolution": resolution, "seeds": [0, 1, 2],
                       "repeatability_adjusted_rand": stability,
                       "community_counts": [len(partition) for partition in partitions], "hierarchy": False})
    winner = min(scales, key=lambda row: (-row["repeatability_adjusted_rand"], row["resolution"] != 1., row["resolution"]))
    return {"selected_resolution": winner["resolution"], "selected_seed": 0, "community_scales": scales,
            "limitations": ["Unsupervised repeatability checks do not measure useful relationship recovery.",
                            "This library has no reviewed discovery calibration; existing discovery settings are retained."]}


def ranking_fingerprint(projection_rows, discovery_records=(), citation_records=()) -> str:
    """Bind exact normalized retrieval signals without duplicating them in reports."""
    index = LocalRetrievalIndex(projection_rows, discovery_records, citation_records=citation_records)
    return _digest({"rows": sorted(index.rows.values(), key=lambda row: row["source_id"]),
                    "discovery_records": sorted(discovery_records, key=_digest),
                    "citation_records": sorted(citation_records, key=_digest)})
