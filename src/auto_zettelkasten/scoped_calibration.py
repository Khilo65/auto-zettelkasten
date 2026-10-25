"""Reviewed, offline calibration of collection/library candidate admission.

Only supplied vectors and compact records are used. Reports, review material and
partitions are private derived data; this module performs no IO or model calls.
The broad discovery inventory is preserved independently of paid admission.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
import hashlib
import math
import time
from typing import Any

from .local_mapping import LocalMappingError, LocalMappingOptions, _numpy, _unit_vectors, scoped_source_neighbors
from .mapping_calibration import (
    LIMITATIONS, _agreement, _digest, _metrics, _partition, _reviewed,
    ranking_fingerprint, stratified_holdout,
)
from .retrieval import CandidateResourceError, LocalRetrievalIndex, _MATCHED, _select_scope_balanced

SCOPED_CALIBRATION_VERSION = "collection-library-calibration-v1"
ADMISSION_VERSION = "scoped-semantic-or-verified-direct-v1"
_COUNTS = (8, 16, 32, 64)
_RESOLUTIONS = (.5, 1., 2.)
_QUALITY = ("coherence", "separation", "coverage", "granularity_fit")


def _seal(report):
    report.pop("report_sha256", None)
    report["report_sha256"] = _digest(report)
    return report


def _sealed(report):
    return isinstance(report, Mapping) and report.get("report_sha256") == _digest(
        {key: value for key, value in report.items() if key != "report_sha256"})


def _memberships(source_ids, collections):
    if not isinstance(collections, Mapping) or set(collections) != set(source_ids):
        raise ValueError("collection membership must cover exactly the scoped works")
    result = {}
    for sid in sorted(source_ids):
        keys = collections[sid]
        if (not isinstance(keys, (list, tuple)) or len(keys) > 64
                or any(not isinstance(key, str) or not key for key in keys)):
            raise ValueError("collection keys must be bounded nonempty strings")
        result[sid] = sorted(set(keys))
    return result


def _inputs(source_ids, vectors, collections, projection_rows, options):
    options.validate()
    if (not isinstance(source_ids, (list, tuple)) or not source_ids or len(source_ids) > options.max_sources
            or any(not isinstance(sid, str) or not sid or len(sid) > 256 for sid in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError("scoped calibration requires unique bounded source IDs")
    ids = sorted(source_ids)
    memberships = _memberships(ids, collections)
    np = _numpy()
    supplied = np.asarray(vectors, dtype=np.float32)
    if supplied.ndim != 2 or supplied.shape[1] < 1 or supplied.nbytes > options.max_cache_bytes:
        raise ValueError("source vectors require a bounded nonempty-dimensional matrix")
    matrix = _unit_vectors(supplied, len(source_ids), supplied.shape[1])
    positions = {sid: index for index, sid in enumerate(source_ids)}
    matrix = matrix[[positions[sid] for sid in ids]]
    rows = list(projection_rows)
    if (len(rows) != len(ids) or any(not isinstance(row, Mapping) for row in rows)
            or {row.get("source_id") for row in rows} != set(ids)):
        raise ValueError("projection_rows must cover exactly the scoped works")
    # Ranking and neighbor scopes must describe the same collection membership.
    index = LocalRetrievalIndex(rows)
    if any(sorted(set(index.rows[sid]["collection_keys"])) != memberships[sid] for sid in ids):
        raise ValueError("projection and vector collection membership disagree")
    return ids, matrix, memberships, rows


def _parameters(parameters):
    count, floor = parameters.get("neighbors"), parameters.get("min_similarity")
    if (isinstance(count, bool) or count not in _COUNTS or isinstance(floor, bool)
            or not isinstance(floor, (int, float)) or not math.isfinite(floor) or not 0 <= floor <= 1):
        raise ValueError("admission requires a tested neighbor count and finite similarity floor")
    return int(count), float(floor)


def _pool(ids, matrix, collections, rows, discovery_records, citation_records, parameters,
          embedding_identity, options, check):
    count, floor = _parameters(parameters)
    check()
    broad = scoped_source_neighbors(ids, matrix, collections, embedding_identity=embedding_identity,
        options=replace(options, discovery_scope="collection_and_library", neighbors=64, min_similarity=0.),
        check_resources=check)
    neighbors = scoped_source_neighbors(ids, matrix, collections, embedding_identity=embedding_identity,
        options=replace(options, discovery_scope="collection_and_library", neighbors=count, min_similarity=floor),
        check_resources=check)["neighbors"]
    index = LocalRetrievalIndex(rows, discovery_records, citation_records=citation_records)
    index.fused_candidate_pairs(broad["neighbors"], per_source_cap=8, max_pairs=256,
                                fanout=64, source_ids=ids, retain_pool=True)
    inventory = index.last_candidate_pool
    semantic = {tuple(sorted((row["source_id"], row["target_source_id"]))) for row in neighbors}
    # Raw validated records retain endpoint identity even when rendered provenance
    # samples are truncated. Generic tags/terms cannot reinflate paid admission.
    direct = {tuple(sorted(pair)) for pair, material in index.direct.items()
              if any(_verified_direct(row) for row in material)}
    admitted = [row for row in inventory if _pair(row) in semantic or _pair(row) in direct]
    first = _select_scope_balanced(lambda: iter(admitted), per_source_cap=8, max_pairs=256)
    check()
    return {"inventory": inventory, "admitted": admitted, "first_pass": first, "neighbors": neighbors,
            "policy": {"version": ADMISSION_VERSION, "neighbors": count, "min_similarity": floor,
                       "inventory_neighbors": 64, "inventory_floor": 0., "per_source_cap": 8,
                       "max_pairs": 256, "nonsemantic_bypass": "verified_direct_reference_or_zotero_related"}}


def _pair(row):
    return tuple(sorted((row["left_source_id"], row["right_source_id"])))


def _verified_direct(row):
    if row.get("signal") == "zotero_related":
        return True  # LocalRetrievalIndex binds both full Zotero identities.
    if row.get("signal") != "direct_reference" or row.get("match_status") not in _MATCHED | {"mapped"}:
        return False
    if "freshness" in row:
        # Discussed-work memory can be endpoint-matched but historically
        # unverified. It remains an inventory hint until hashes are checked.
        return row["freshness"] in {"current_declared_hashes", "current_profile_hashes"}
    return bool(row.get("source_identity") and row.get("target_identity"))


def scoped_admission_pool(source_ids, vectors, collections, *, parameters, projection_rows,
                          discovery_records=(), citation_records=(), embedding_identity="",
                          options=None, check_resources=None):
    """Return the broad fused inventory and its actual paid-admission subset.

    ``neighbors`` and ``min_similarity`` qualify semantic pairs; known direct
    references/Related-item identities may bypass this gate. This is discovery,
    not acceptance of a research relationship. All queue passes must use the
    admitted set, not the broad inventory.
    """
    options = options or LocalMappingOptions()
    ids, matrix, memberships, rows = _inputs(source_ids, vectors, collections, projection_rows, options)
    return _pool(ids, matrix, memberships, rows, discovery_records, citation_records, parameters,
                 embedding_identity, options, check_resources or (lambda: None))


def _scores(rows, labels, memberships):
    result = {"all": _metrics(rows, labels)}
    for scope in ("within_collection", "whole_library"):
        selected = [row for row in rows if row["comparison_scope"] == scope]
        reviewed = {pair: label for pair, label in labels.items()
                    if bool(set(memberships[pair[0]]) & set(memberships[pair[1]])) == (scope == "within_collection")}
        result[scope] = _metrics(selected, reviewed)
    return result


def calibrate_scoped_mapping(source_ids, vectors, collections, reviews, *, embedding_identity,
                             source_fingerprint, projection_rows, discovery_records=(), citation_records=(),
                             evidence_bindings=None, options=None, check_resources=None, clock=time.monotonic):
    """Freeze discovery settings using training works; await community review.

    Every reviewed pair needs exactly two independent score-blind reviews.
    Disagreement remains uncertain. Held-out vectors and metadata enter retrieval
    only after the training winner is frozen. Semantic community reviews are a
    separate required stage, not inferred from stability or graph appearance.
    """
    options = options or LocalMappingOptions()
    ids, matrix, memberships, rows = _inputs(source_ids, vectors, collections, projection_rows, options)
    if not isinstance(embedding_identity, str) or not embedding_identity or not isinstance(source_fingerprint, str) or not source_fingerprint:
        raise ValueError("embedding and source evidence fingerprints are required")
    if evidence_bindings is not None and (not isinstance(evidence_bindings, Mapping) or set(evidence_bindings) != set(ids)):
        raise ValueError("evidence bindings must cover exactly the scoped works")
    labels, material = _reviewed(reviews, set(ids))
    held = set(stratified_holdout(memberships))
    train_ids = sorted(set(ids) - held)
    train_labels = {pair: label for pair, label in labels.items() if not set(pair) & held}
    held_labels = {pair: label for pair, label in labels.items() if set(pair) & held}
    identity = {"version": SCOPED_CALIBRATION_VERSION, "embedding_identity": embedding_identity,
                "source_ids": ids, "source_fingerprint": source_fingerprint,
                "ranking_inputs_sha256": ranking_fingerprint(rows, discovery_records, citation_records),
                "vectors_sha256": hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest(),
                "collections": memberships, "reviews": material,
                "evidence_bindings_sha256": _digest(evidence_bindings) if evidence_bindings is not None else None,
                "admission_version": ADMISSION_VERSION,
                "limits": {"per_source_cap": 8, "max_pairs": 256, "counts": list(_COUNTS),
                           "resolutions": list(_RESOLUTIONS), "seeds": [0, 1, 2],
                           "max_sources": options.max_sources, "max_wall_seconds": options.max_wall_seconds,
                           "max_cache_bytes": options.max_cache_bytes}}
    report = {"schema_version": SCOPED_CALIBRATION_VERSION, "identity": identity,
              "calibration_identity": _digest(identity), "status": "incomplete", "selected_parameters": None,
              "training_source_ids": train_ids, "holdout_source_ids": sorted(held),
              "configurations": [], "community_scales": [],
              "coverage": {"works": len(ids), "training_works": len(train_ids), "held_out_works": len(held),
                           "reviewed_pairs": len(labels), "training_reviewed_pairs": len(train_labels),
                           "held_out_reviewed_pairs": len(held_labels),
                           "uncertain_pairs": sum(label == "uncertain" for label in labels.values())},
              "limitations": list(LIMITATIONS) + [
                  "Nonsemantic admission bypass is limited to validated direct references and Related-item identities; shared tags/terms alone are not sufficient.",
                  "The bounded fused inventory may omit eligible pairs; inventory and admission are reported separately.",
                  "Review-based tuning is a small screening exercise, not calibrated relationship probability or proof of complete coverage."]}
    counts = Counter(tuple(row["pair"]) for row in material)
    if not counts or any(value != 2 for value in counts.values()) or not {"useful", "unsupported"} <= set(train_labels.values()) or not held_labels:
        report["incomplete_reason"] = "requires_two_blind_reviews_and_training_useful_unsupported_plus_held_out_pairs"
        return _seal(report)
    started = clock()
    def check():
        if check_resources:
            check_resources()
        if clock() - started > options.max_wall_seconds:
            raise LocalMappingError("Scoped calibration reached max_wall_seconds; settings retained.")
    try:
        check()
        positions = {sid: index for index, sid in enumerate(ids)}
        train_matrix = matrix[[positions[sid] for sid in train_ids]]
        train_memberships = {sid: memberships[sid] for sid in train_ids}
        train_rows = [row for row in rows if row["source_id"] in train_memberships]
        train_discovery = [row for row in discovery_records if row["source_id"] in train_memberships]
        train_citations = [row for row in citation_records if row["source_id"] in train_memberships and row["target_source_id"] in train_memberships]
        training_broad = scoped_source_neighbors(train_ids, train_matrix, train_memberships,
            options=replace(options, discovery_scope="collection_and_library", neighbors=64, min_similarity=0.),
            embedding_identity=embedding_identity, check_resources=check)["neighbors"]
        positive = [row["score"] for row in training_broad]
        if not positive:
            report["incomplete_reason"] = "no_positive_training_similarities"
            return _seal(report)
        np = _numpy()
        floors = [("disabled", 0.), ("positive_p25", float(np.percentile(positive, 25))),
                  ("positive_p50", float(np.percentile(positive, 50)))]
        report["training_similarity_floors"] = dict(floors)
        report["training_ranking_inputs_sha256"] = ranking_fingerprint(train_rows, train_discovery, train_citations)
        winning_pool, winning_key, best_index = None, None, None
        for count in _COUNTS:
            for kind, floor in floors:
                before = clock()
                parameters = {"neighbors": count, "min_similarity": floor}
                pool = _pool(train_ids, train_matrix, train_memberships, train_rows, train_discovery,
                             train_citations, parameters, embedding_identity, options, check)
                configuration = {"neighbors": count, "floor_kind": kind, "min_similarity": floor,
                                 "first_pass": _scores(pool["first_pass"], train_labels, train_memberships),
                                 "full_admitted_pool": _scores(pool["admitted"], train_labels, train_memberships),
                                 "inventory_count": len(pool["inventory"]),
                                 "processing_seconds": max(0., clock() - before)}
                report["configurations"].append(configuration)
                candidate_index = len(report["configurations"]) - 1
                candidate_key = (-configuration["first_pass"]["all"]["useful_recovered"],
                                 configuration["first_pass"]["all"]["unsupported_selected"],
                                 len(pool["admitted"]), candidate_index)
                if winning_key is None or candidate_key < winning_key:
                    winning_key, winning_pool, best_index = candidate_key, pool, candidate_index
        # No timing measurement participates in selection. First-pass coverage is
        # the objective; a larger full pool must earn its additional admissions.
        best = report["configurations"][best_index]
        selected = {"neighbors": best["neighbors"], "min_similarity": best["min_similarity"], "resolution": None, "seed": 0}
        report["selected_parameters"] = selected
        report["selected_configuration_index"] = best_index
        report["selection_rule"] = "first_pass_useful_desc_unsupported_asc_full_pool_count_asc_stable_grid_order"
        # Only now introduce held-out works. This cannot change the winner.
        full = _pool(ids, matrix, memberships, rows, discovery_records, citation_records,
                     selected, embedding_identity, options, check)
        allowed = sorted(_pair(row) for row in full["admitted"])
        report["admission"] = {"policy": full["policy"], "allowed_pairs": [list(pair) for pair in allowed],
                               "allowed_pairs_sha256": _digest(allowed), "inventory_count": len(full["inventory"]),
                               "admitted_count": len(allowed), "first_pass_count": len(full["first_pass"]),
                               "first_pass_pairs": [list(_pair(row)) for row in full["first_pass"]]}
        report["full_scope"] = {"inventory": _scores(full["inventory"], labels, memberships),
                                "full_admitted_pool": _scores(full["admitted"], labels, memberships),
                                "first_pass": _scores(full["first_pass"], labels, memberships)}
        report["held_out"] = {"full_admitted_pool": _scores(full["admitted"], held_labels, memberships),
                              "first_pass": _scores(full["first_pass"], held_labels, memberships)}
        training_neighbors = winning_pool["neighbors"]
        for resolution in _RESOLUTIONS:
            partitions = []
            for seed in (0, 1, 2):
                check()
                partitions.append(_partition(train_ids, training_neighbors, resolution, seed))
            stability = sum(_agreement(partitions[left], partitions[right], train_ids)
                            for left, right in ((0, 1), (0, 2), (1, 2))) / 3
            report["community_scales"].append({"configuration_id": f"res-{resolution}", "resolution": resolution,
                "seeds": [0, 1, 2], "partitions": partitions, "stability": stability,
                "community_counts": [len(partition) for partition in partitions],
                "largest_community_fractions": [max(map(len, partition)) / len(train_ids) for partition in partitions],
                "singleton_fractions": [sum(len(group) == 1 for group in partition) / len(train_ids) for partition in partitions]})
        report["status"] = "awaiting_community_review"
        report["processing_seconds"] = max(0., clock() - started)
        return _seal(report)
    except (LocalMappingError, CandidateResourceError) as exc:
        report["status"] = "incomplete"
        report["selected_parameters"] = None
        report["incomplete_reason"] = str(exc)
        return _seal(report)


def finalize_scoped_calibration(report, community_reviews):
    """Select resolution from two blind semantic assessments per training cut.

    Scores 1..5 cover coherence, separation, coverage of minority/outlier themes,
    and useful granularity. They are human/agent assessments, not gold labels.
    """
    if not _sealed(report) or report.get("status") != "awaiting_community_review":
        raise ValueError("finalization requires an unchanged pending calibration report")
    if not isinstance(community_reviews, (list, tuple)) or len(community_reviews) != 6:
        raise ValueError("two independent community reviews are required for each resolution")
    train = report["training_source_ids"]
    grouped = {row["configuration_id"]: {} for row in report["community_scales"]}
    material = []
    for review in community_reviews:
        if not isinstance(review, Mapping):
            raise ValueError("community review must be an object")
        config, reviewer = review.get("configuration_id"), review.get("reviewer_id")
        if (config not in grouped or not isinstance(reviewer, str) or not reviewer
                or reviewer in grouped[config] or review.get("score_blinded") is not True
                or not isinstance(review.get("source_ids"), (list, tuple))
                or sorted(review["source_ids"]) != train):
            raise ValueError("community reviews require independent blinded reviewers and exact training works")
        quality = {key: review.get(key) for key in _QUALITY}
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or not 1 <= value <= 5 for value in quality.values()):
            raise ValueError("community quality scores must be finite values from 1 to 5")
        grouped[config][reviewer] = sum(quality.values()) / len(_QUALITY)
        material.append({"configuration_id": config, "reviewer_id": reviewer, "source_ids": train,
                         "score_blinded": True, **quality})
    if any(len(values) != 2 for values in grouped.values()):
        raise ValueError("two independent community reviews are required for each resolution")
    result = deepcopy(report)
    for scale in result["community_scales"]:
        scale["reviewed_quality"] = sum(grouped[scale["configuration_id"]].values()) / 2
    chosen = min(result["community_scales"], key=lambda scale: (
        -scale["reviewed_quality"], -scale["stability"], scale["resolution"] != 1., scale["resolution"]))
    result["selected_parameters"]["resolution"] = chosen["resolution"]
    result["community_reviews"] = sorted(material, key=lambda row: (row["configuration_id"], row["reviewer_id"]))
    result["community_selection_rule"] = "semantic_quality_desc_stability_desc_resolution_1_then_numeric"
    result["evaluation_gate_passed"] = True
    result["status"] = "complete"
    return _seal(result)


def source_vectors_fingerprint(source_ids, vectors):
    """Bind the normalized, sorted source vectors used for calibration."""
    np = _numpy()
    supplied = np.asarray(vectors, dtype=np.float32)
    if supplied.ndim != 2 or len(source_ids) != len(set(source_ids)):
        raise ValueError("source vectors require unique identities and a matrix")
    matrix = _unit_vectors(supplied, len(source_ids), supplied.shape[1])
    positions = {sid: index for index, sid in enumerate(source_ids)}
    matrix = matrix[[positions[sid] for sid in sorted(source_ids)]]
    return hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest()


def admitted_scoped_policy(report, *, embedding_identity, source_ids, source_fingerprint,
                           ranking_inputs_sha256, collections, current_evidence_bindings_sha256=None,
                           current_vectors_sha256=None):
    """Fail closed on incomplete, tampered, or stale runtime calibration inputs."""
    try:
        if (not _sealed(report) or report.get("schema_version") != SCOPED_CALIBRATION_VERSION
                or report.get("status") != "complete" or report.get("evaluation_gate_passed") is not True):
            return None
        identity = report["identity"]
        if (report["calibration_identity"] != _digest(identity)
                or identity["embedding_identity"] != embedding_identity
                or identity["source_ids"] != sorted(source_ids)
                or identity["source_fingerprint"] != source_fingerprint
                or not ranking_inputs_sha256 or identity["ranking_inputs_sha256"] != ranking_inputs_sha256
                or identity["collections"] != _memberships(source_ids, collections)
                or identity["admission_version"] != ADMISSION_VERSION):
            return None
        if current_evidence_bindings_sha256 is not None and (
                not current_evidence_bindings_sha256
                or identity.get("evidence_bindings_sha256") != current_evidence_bindings_sha256):
            return None
        if current_vectors_sha256 is not None and (
                not current_vectors_sha256 or identity.get("vectors_sha256") != current_vectors_sha256):
            return None
        parameters = report["selected_parameters"]
        _parameters(parameters)
        if (parameters.get("resolution") not in _RESOLUTIONS or parameters.get("seed") != 0
                or len(report["configurations"]) != 12 or len(report["community_scales"]) != 3
                or len(report["community_reviews"]) != 6):
            return None
        pairs = report["admission"]["allowed_pairs"]
        known = set(source_ids)
        if (not isinstance(pairs, list) or len(pairs) > 100_000
                or any(not isinstance(pair, list) or len(pair) != 2 or pair[0] >= pair[1]
                       or not set(pair) <= known for pair in pairs)
                or sorted(pairs) != pairs or len({tuple(pair) for pair in pairs}) != len(pairs)
                or report["admission"]["allowed_pairs_sha256"] != _digest(pairs)):
            return None
        return {"parameters": dict(parameters), "allowed_pairs": deepcopy(pairs),
                "policy_identity": report["report_sha256"], "calibration_identity": report["calibration_identity"]}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
