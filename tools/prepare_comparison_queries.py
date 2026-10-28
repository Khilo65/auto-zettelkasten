#!/usr/bin/env python3
"""Prepare query vectors with cached MiniLM assets and existing resource guards."""
from __future__ import annotations

import argparse
import ctypes
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_retrieval_comparison import private_output, read_json, write_json
from auto_zettelkasten.local_mapping import LocalMappingOptions, _LocalMiniLM


class _SwapUsage(ctypes.Structure):
    # Darwin's xsw_usage layout. Refuse an unexpected returned size instead of
    # interpreting a different platform/ABI as a safe memory reading.
    _fields_ = [("total", ctypes.c_uint64), ("available", ctypes.c_uint64),
                ("used", ctypes.c_uint64), ("page_size", ctypes.c_uint32),
                ("encrypted", ctypes.c_int32)]


@lru_cache(maxsize=1)
def _sysctl_function():
    # Cache only the function handle, never resource values or their freshness.
    function = ctypes.CDLL(None, use_errno=True).sysctlbyname
    function.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                         ctypes.c_size_t]
    function.restype = ctypes.c_int
    return function


def _native_sysctl(name, value):
    size = ctypes.c_size_t(ctypes.sizeof(value))
    result = _sysctl_function()(name, ctypes.byref(value), ctypes.byref(size), None, 0)
    if result != 0 or size.value != ctypes.sizeof(value):
        raise OSError(ctypes.get_errno(), "native resource reading failed")
    return value


def pressure_level():
    if sys.platform != "darwin":
        return None
    try:
        return int(_native_sysctl(b"kern.memorystatus_vm_pressure_level", ctypes.c_uint32()).value)
    except (OSError, AttributeError):
        # Keep the existing portable command path for unavailable native APIs.
        pass
    try:
        result = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                capture_output=True, text=True, timeout=5, check=True)
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def swap_used_bytes():
    if sys.platform != "darwin":
        return None
    try:
        value = _native_sysctl(b"vm.swapusage", _SwapUsage())
        if value.used > value.total or value.available > value.total:
            raise OSError("invalid native swap reading")
        return int(value.used)
    except (OSError, AttributeError):
        pass
    import re
    try:
        result = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                                text=True, timeout=5, check=True)
        matched = re.search(r"used\s*=\s*([\d.]+)([KMG])", result.stdout)
        return int(float(matched[1]) * {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[matched[2]]) if matched else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def resource_guard(output, initial_swap):
    if pressure_level() != 1:
        raise RuntimeError("memory pressure is not verified normal; encoder remains paused")
    if shutil.disk_usage(output).free < 2 * 1024 ** 3:
        raise RuntimeError("free disk is below the existing 2 GiB experiment guard")
    swap = swap_used_bytes()
    if swap is None or initial_swap is None:
        raise RuntimeError("swap growth cannot be verified")
    if swap - initial_swap > 256 * 1024 ** 2:
        raise RuntimeError("swap grew beyond the existing 256 MiB experiment guard")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    if rss > 2 * 1024 ** 3:
        raise RuntimeError("encoder process exceeded the existing 2 GiB RSS guard")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--vector-manifest", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = private_output(args.output)
    started = time.monotonic()
    initial_swap = swap_used_bytes()
    record = {"provider_calls": 0, "model_downloads": 0, "source_embeddings_changed": False,
              "pressure_level": pressure_level(), "initial_swap_bytes": initial_swap}
    try:
        resource_guard(output, initial_swap)
    except RuntimeError as exc:
        record.update(status="paused_resources", reason=str(exc), encoder_loads=0)
        write_json(output / "status.json", record)
        print(json.dumps(record))
        return 0
    manifest = read_json(args.vector_manifest)
    tasks = read_json(args.tasks)
    tasks = tasks.get("tasks", []) if isinstance(tasks, dict) else tasks
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 1000:
        raise ValueError("tasks must be a nonempty bounded list")
    options = LocalMappingOptions(model_path=args.model_path, batch_size=8, threads=2)
    options.validate()
    encoder = _LocalMiniLM(options, output)
    assets = {key: encoder.identity[key] for key in ("model_name", "model_sha256")}
    if assets != manifest.get("model_assets") or encoder.identity["library_versions"] != manifest.get("libraries"):
        raise ValueError("cached query encoder differs from frozen source vector space")
    if manifest.get("normalization") != "l2" or manifest.get("dimension") != encoder.dimension:
        raise ValueError("source normalization/dimension mismatch")
    ids, queries = [], []
    for task in tasks:
        task_id, query = task.get("task_id", task.get("id")), task.get("query")
        if not isinstance(task_id, str) or not task_id or task_id in ids or not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("invalid query identity or text bound")
        tokens = encoder.tokenizer.encode(query, add_special_tokens=True, truncation=False)
        if len(tokens) > encoder.max_seq_length:
            raise ValueError("query exceeds MiniLM token window; no silent truncation permitted")
        ids.append(task_id)
        queries.append(query)
    encoded = []
    try:
        for offset in range(0, len(queries), 8):
            resource_guard(output, initial_swap)
            vectors = encoder.encode(queries[offset:offset + 8])
            resource_guard(output, initial_swap)
            batch_start = len(encoded)
            for task_id, query, vector in zip(ids[offset:offset + 8], queries[offset:offset + 8], vectors):
                encoded.append({"task_id": task_id, "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                                "vector": vector.tolist(), "encoder_identity": assets,
                                "tokenizer": manifest["tokenizer"], "libraries": manifest["libraries"],
                                "normalization": "l2", "dimension": encoder.dimension})
            write_json(output / f"checkpoint-{offset:04d}.json", encoded[batch_start:])
    except RuntimeError as exc:
        record.update(status="paused_resources", reason=str(exc), completed_queries=len(encoded),
                      encoder_loads=int(encoder.model is not None))
        write_json(output / "status.json", record)
        print(json.dumps(record))
        return 0
    write_json(output / "query-vectors.json", encoded)
    record.update(status="completed", completed_queries=len(encoded), encoder_loads=1,
                  elapsed_seconds=time.monotonic() - started, actual_cost_usd="0")
    write_json(output / "status.json", record)
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
