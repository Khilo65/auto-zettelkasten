#!/usr/bin/env python3
"""Run local retrieval controls on supplied private experiment files.

No provider calls, model loading, framework indexing, or production mutations.
Query vectors must be prepared separately with a verified compatible encoder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_zettelkasten.comparison_evaluation import resource_record
from auto_zettelkasten.retrieval import LocalRetrievalIndex
from auto_zettelkasten.retrieval_comparison import ComparisonIndex, VARIANTS


def read_json(path, limit=32 * 1024 * 1024):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("input file missing or exceeds declared read bound")
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path, limit=64 * 1024 * 1024):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("row file missing or exceeds declared read bound")
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def private_output(path):
    path = Path(path).expanduser().resolve()
    # Experiment artifacts must not land inside ANY Git working tree.
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("comparison results must be outside Git working trees")
    if path.exists():
        raise ValueError("output must be a fresh directory; preserve prior runs")
    path.mkdir(parents=True, mode=0o700)
    return path


def write_json(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def verified_vector_matrix(path, ids, actual, expected):
    """Bind binary rows and their order to the separately frozen expectation."""
    import numpy as np
    digest = expected.get("vectors_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or actual.get("vectors_sha256") != digest:
        raise ValueError("source vector file identity is missing or mismatched")
    with Path(path).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
            raise ValueError("source vector file hash differs from frozen identity")
    if actual.get("source_ids") != ids or expected.get("source_ids") != ids:
        raise ValueError("source vector row ordering differs from frozen identity")
    matrix = np.load(path, mmap_mode="r", allow_pickle=False)
    if not isinstance(ids, list) or matrix.ndim != 2 or len(ids) != matrix.shape[0] or len(set(ids)) != len(ids):
        raise ValueError("source vector shape/identity mismatch")
    return matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=["lexical"])
    parser.add_argument("--vectors")
    parser.add_argument("--vector-ids")
    parser.add_argument("--vector-manifest")
    parser.add_argument("--expected-vector-manifest")
    parser.add_argument("--query-vectors")
    parser.add_argument("--legacy-profiles")
    parser.add_argument("--semantic-edges")
    parser.add_argument("--reference-edges")
    parser.add_argument("--accepted-edges")
    args = parser.parse_args(argv)
    started = time.monotonic()
    corpus = read_rows(args.corpus)
    task_value = read_json(args.tasks)
    tasks = task_value.get("tasks", []) if isinstance(task_value, dict) else task_value
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 1000:
        parser.error("tasks must be a nonempty bounded list")
    task_ids = [row.get("task_id", row.get("id")) for row in tasks]
    if any(not isinstance(task_id, str) or not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        parser.error("tasks need unique nonempty IDs")
    vectors = None
    manifest = expected = None
    if args.vectors:
        if not all((args.vector_ids, args.vector_manifest, args.expected_vector_manifest)):
            parser.error("vectors require IDs and actual/expected identity manifests")
        ids = read_json(args.vector_ids)
        manifest = read_json(args.vector_manifest)
        expected = read_json(args.expected_vector_manifest)
        matrix = verified_vector_matrix(args.vectors, ids, manifest, expected)
        vectors = {sid: matrix[index] for index, sid in enumerate(ids)}
    elif any((args.vector_ids, args.vector_manifest, args.expected_vector_manifest, args.query_vectors)):
        parser.error("vector inputs require --vectors")
    query_rows = read_json(args.query_vectors) if args.query_vectors else []
    if not isinstance(query_rows, list):
        parser.error("query vectors must be a list")
    queries = {}
    for row in query_rows:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str) or row["task_id"] in queries:
            parser.error("query vectors require unique task identities")
        queries[row["task_id"]] = row
    def edges(path):
        value = read_json(path) if path else []
        if isinstance(value, dict):
            value = value.get("edges", value.get("relationships"))
        if not isinstance(value, list):
            raise ValueError("edges must be a list or a declared edges/relationships collection")
        return value
    accepted = edges(args.accepted_edges)
    index = ComparisonIndex(corpus, vectors, vector_manifest=manifest, expected_vector_manifest=expected,
                            semantic_edges=edges(args.semantic_edges), reference_edges=edges(args.reference_edges),
                            accepted_edges=accepted)
    legacy_rows = read_json(args.legacy_profiles) if args.legacy_profiles else None
    if isinstance(legacy_rows, dict):
        legacy_rows = legacy_rows.get("rows")
    if args.legacy_profiles and not isinstance(legacy_rows, list):
        raise ValueError("legacy profiles require an explicit rows collection")
    legacy = LocalRetrievalIndex(legacy_rows) if legacy_rows is not None else None
    output = private_output(args.output)
    results = []
    for task, task_id in zip(tasks, task_ids):
        query = task.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("each task requires a query")
        supplied = queries.get(task_id)
        if supplied:
            if supplied.get("query_sha256") != hashlib.sha256(query.encode()).hexdigest():
                raise ValueError("stale query vector text")
            if supplied.get("encoder_identity") != manifest.get("model_assets"):
                raise ValueError("incompatible query vector model identity")
            # Query extraction differs from source chunk aggregation, but must
            # preserve the same tokenizer, runtime and L2 vector space.
            for name in ("tokenizer", "libraries", "normalization", "dimension"):
                if supplied.get(name) != manifest.get(name):
                    raise ValueError("incompatible query vector " + name)
        for variant in args.variants:
            reason = None
            if variant == "legacy" and legacy is None:
                reason = "legacy profiles not supplied"
            elif variant not in {"legacy", "lexical"} and (vectors is None or supplied is None):
                reason = "verified query/source vectors not supplied; no model was loaded"
            if reason:
                results.append({"task_id": task_id, "variant": variant, "status": "blocked", "reason": reason})
                continue
            began = time.monotonic()
            packet = index.recall(query, query_vector=supplied["vector"] if supplied else None, variant=variant,
                                  legacy_index=legacy, legacy_relationships=accepted)
            results.append({"task_id": task_id, "variant": variant, "status": "retrieved",
                            "elapsed_seconds": time.monotonic() - began, "packet": packet})
    write_json(output / "results.json", results)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    stats = resource_record(elapsed_seconds=time.monotonic() - started, peak_rss_bytes=peak,
                            disk_delta_bytes=(output / "results.json").stat().st_size,
                            provider_calls=0, actual_cost_usd="0")
    stats.update(status="local_controls_only", corpus_sources=len(corpus), tasks=len(tasks),
                 variants=args.variants, completed=sum(row["status"] == "retrieved" for row in results),
                 blocked=sum(row["status"] == "blocked" for row in results),
                 production_rollout=False, original_notes_changed=False)
    write_json(output / "run.json", stats)
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
