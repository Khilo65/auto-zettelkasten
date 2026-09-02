#!/usr/bin/env python3
"""Run one private, hash-locked Codex throughput calibration stage."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import auto_zettelkasten


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _verify_runtime_import_root() -> None:
    expected = (_REPOSITORY_ROOT / "src" / "auto_zettelkasten").resolve()
    module_file = getattr(auto_zettelkasten, "__file__", None)
    actual = Path(module_file).resolve().parent if module_file else None
    if actual != expected:
        raise RuntimeError(
            "evaluation runner imported auto_zettelkasten outside this "
            "repository's src directory"
        )


_verify_runtime_import_root()

from auto_zettelkasten.codex_attempt_guard import (  # noqa: E402
    PAUSE_REASONS,
    CodexAttemptGuard,
)
from auto_zettelkasten.files import (  # noqa: E402
    now_iso,
    read_yaml,
    sha256_file,
    sha256_text,
    write_yaml,
)
from auto_zettelkasten.models import LiteratureMapRequest  # noqa: E402
from auto_zettelkasten.readers import (  # noqa: E402
    CodexReader,
    ProviderInterrupted,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTimeout,
    ProviderTransportError,
    _CODEX_ERROR_ITEM_CATEGORIES,
    cancel_active_provider_responses,
    codex_contract_identity,
    current_provider_completion,
    reset_provider_completion,
)


STAGES: Mapping[str, Mapping[str, Any]] = {
    "source": {
        "contract_id": "source_bundle",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "levels": (1, 2, 4, 8, 16, 32),
        "maximum_attempts": 70,
    },
    "relationship": {
        "contract_id": "relationship_adjudication",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "levels": (1, 2, 4, 8, 16),
        "maximum_attempts": 38,
    },
}
BASELINE_ATTEMPTS = 8
MINIMUM_GAIN_PERCENT = 15.0
CALL_DEADLINE_SECONDS = 600.0
BASELINE_DEADLINE_SECONDS = 4_860.0
WAVE_DEADLINE_SECONDS = 660.0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_EVALUATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_ISOLATION_SUBTYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

for _config in STAGES.values():
    assert BASELINE_ATTEMPTS + sum(_config["levels"][1:]) == _config["maximum_attempts"]


def _controls(stage: str) -> dict[str, Any]:
    config = STAGES[stage]
    return {
        "initial_calls": BASELINE_ATTEMPTS,
        "maximum_attempts": config["maximum_attempts"],
        "cumulative_calibration_ceiling": 108,
        "retry_limit": 0,
        "levels": list(config["levels"]),
        "call_deadline_seconds": CALL_DEADLINE_SECONDS,
        "baseline_deadline_seconds": BASELINE_DEADLINE_SECONDS,
        "wave_deadline_seconds": WAVE_DEADLINE_SECONDS,
        "minimum_gain_percent": MINIMUM_GAIN_PERCENT,
        "quota_behavior": "pause",
        "resume_behavior": "unfinished_jobs_only",
        "attachments": "none",
        "tool_attempt_behavior": "fail_closed",
        "authentication_behavior": "fail_closed",
        "truncation_behavior": "fail_closed",
    }


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _require_private_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    git_root = _git_root(resolved)
    if git_root is not None or _inside(resolved, _REPOSITORY_ROOT):
        raise ValueError(f"{label} must be outside Git repositories")
    return resolved


def _git_commit(*, require_clean: bool = True) -> str:
    process = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    commit = process.stdout.strip()
    if process.returncode or not _GIT_COMMIT.fullmatch(commit):
        raise RuntimeError("unable to resolve the calibration code commit")
    if require_clean:
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if status.returncode or status.stdout.strip():
            raise ValueError("calibration requires a clean code commit")
    return commit


def _validated_manifest(
    manifest_path: Path,
    expected_sha256: str,
    *,
    require_clean: bool,
) -> tuple[dict[str, Any], dict[str, Any], str, Path]:
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    manifest_path = _require_private_path(manifest_path, label="manifest")
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    if sha256_file(manifest_path) != expected_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    manifest = _mapping(read_yaml(manifest_path), label="manifest")
    if str(manifest.get("schema_version") or "") != "1":
        raise ValueError("manifest schema_version must be '1'")
    if manifest.get("code_commit") != _git_commit(require_clean=require_clean):
        raise ValueError("manifest code commit mismatch")
    stage = str(manifest.get("stage") or "").strip()
    if stage not in STAGES:
        raise ValueError("manifest stage must be source or relationship")
    if _mapping(manifest.get("controls"), label="manifest controls") != _controls(
        stage
    ):
        raise ValueError("manifest controls do not match the bounded calibration")
    config = STAGES[stage]
    if manifest.get("contract_identity") != codex_contract_identity(
        str(config["contract_id"]),
        str(config["model"]),
        str(config["reasoning_effort"]),
    ):
        raise ValueError("manifest contract identity mismatch")
    evaluation_id = str(manifest.get("evaluation_id") or "").strip()
    if not _EVALUATION_ID.fullmatch(evaluation_id):
        raise ValueError("evaluation_id must be 1-96 safe filename characters")
    payload_value = manifest.get("payload")
    payload_sha256 = str(manifest.get("payload_sha256") or "")
    if not isinstance(payload_value, str) or not payload_value.strip():
        raise ValueError("manifest payload must be a relative path")
    if not _SHA256.fullmatch(payload_sha256):
        raise ValueError("manifest payload_sha256 is invalid")
    relative = Path(payload_value)
    if relative.is_absolute():
        raise ValueError("manifest payload must be a relative path")
    root = manifest_path.parent
    payload_path = (root / relative).resolve()
    if not _inside(payload_path, root) or not payload_path.is_file():
        raise ValueError("payload is missing or outside the manifest root")
    if sha256_file(payload_path) != payload_sha256:
        raise ValueError("payload SHA-256 mismatch")
    payload = _mapping(read_yaml(payload_path), label="payload")
    if str(payload.get("contract_id") or "") != STAGES[stage]["contract_id"]:
        raise ValueError("payload contract does not match the selected stage")
    return manifest, payload, stage, manifest_path


def _arguments(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    arguments = _mapping(payload.get("arguments"), label="payload arguments")
    unknown = sorted(set(arguments) - allowed)
    missing = sorted(required - set(arguments))
    if unknown:
        raise ValueError(f"unknown payload arguments: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing payload arguments: {', '.join(missing)}")
    return arguments


def _prepare_dispatch(
    stage: str,
    payload: Mapping[str, Any],
    *,
    workspace: Path,
) -> Callable[[Any], Mapping[str, Any]]:
    config = STAGES[stage]
    if stage == "source":
        arguments = _arguments(
            payload,
            allowed={"text", "metadata", "question"},
            required={"text", "metadata"},
        )
        text = arguments["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("source text must be a non-empty string")
        metadata = _mapping(arguments["metadata"], label="source metadata")
        question = arguments.get("question")
        if question is not None and not isinstance(question, str):
            raise ValueError("source question must be a string or null")

        def dispatch(reader: Any) -> Mapping[str, Any]:
            return reader.read_source_bundle(text, metadata, question=question)

        return dispatch

    arguments = _arguments(
        payload,
        allowed={"profiles", "request", "context"},
        required={"profiles", "request"},
    )
    profiles = arguments["profiles"]
    if not isinstance(profiles, list) or any(
        not isinstance(profile, Mapping) for profile in profiles
    ):
        raise ValueError("relationship profiles must be a list of mappings")
    request_values = _mapping(arguments["request"], label="relationship request")
    policy = _mapping(
        request_values.get("literature_policy", {}),
        label="relationship literature_policy",
    )
    policy["cluster_generation_enabled"] = False
    request_values.update(
        {
            "workspace": str(workspace),
            "provider": "codex",
            "model": config["model"],
            "reasoning_effort": config["reasoning_effort"],
            "allow_cloud": True,
            "provider_concurrency": 1,
            "max_provider_spend_usd": None,
            "literature_policy": policy,
        }
    )
    request = LiteratureMapRequest.from_dict(request_values)
    context_value = arguments.get("context")
    context = (
        None
        if context_value is None
        else _mapping(context_value, label="relationship context")
    )
    frozen_profiles = [dict(profile) for profile in profiles]

    def dispatch(reader: Any) -> Mapping[str, Any]:
        return reader.adjudicate_relationships(
            frozen_profiles,
            request,
            context=context,
        )

    return dispatch


def _failure_class(error: BaseException) -> str:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interruption"
    if isinstance(error, ProviderQuotaExhausted):
        return "quota"
    if isinstance(error, ProviderTimeout):
        return "timeout"
    if isinstance(error, ProviderInterrupted):
        return "interruption"
    if isinstance(error, ProviderIsolationFailure):
        return "isolation"
    if isinstance(error, ProviderTransportError):
        return "transport"
    return "terminal"


def _isolation_reason(error: ProviderIsolationFailure) -> str:
    message = str(error)
    for prefix, reason in (
        ("Codex emitted unexpected event: ", "unexpected_event"),
        ("Codex emitted unexpected item: ", "unexpected_item"),
        ("Codex attempted tool event: ", "disallowed_item"),
    ):
        subtype = message.removeprefix(prefix)
        if subtype != message and _ISOLATION_SUBTYPE.fullmatch(subtype):
            return f"{reason}:{subtype}"
    return "unknown"


def _error_reason(error: BaseException) -> str | None:
    message = str(error)
    prefix = "Codex CLI emitted error item: "
    subtype = message.removeprefix(prefix)
    if subtype != message:
        safe_subtype = (
            subtype if subtype in _CODEX_ERROR_ITEM_CATEGORIES else "unknown"
        )
        return f"error_item:{safe_subtype}"
    return None


def _validate_completion(
    completion: Mapping[str, Any],
    *,
    contract_id: str,
    model: str,
    effort: str,
) -> None:
    expected = {
        **codex_contract_identity(contract_id, model, effort),
        "finish_reason": "turn.completed",
    }
    for key, value in expected.items():
        if completion.get(key) != value:
            raise ValueError(f"provider completion {key} mismatch")
    if not _usage_counts(completion.get("usage")):
        raise ValueError("provider completion usage is missing")


def _safe_completion(value: Any) -> dict[str, Any]:
    completion = dict(value) if isinstance(value, Mapping) else {}
    identity_keys = set(
        codex_contract_identity(
            "source_bundle", "gpt-5.6-luna", "medium"
        )
    ) | {"finish_reason", "codex_cli_version", "max_output_tokens"}
    return {
        **{key: completion[key] for key in identity_keys if key in completion},
        "usage": _usage_counts(completion.get("usage")),
    }


def _append_ledger(path: Path, row: Mapping[str, Any]) -> None:
    data = (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _reserve_attempt(
    path: Path,
    row: Mapping[str, Any],
    *,
    maximum_attempts: int,
) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.lseek(descriptor, 0, os.SEEK_SET)
        rows = [
            json.loads(line)
            for line in os.read(descriptor, 10_000_000).decode().splitlines()
        ]
        reservations = [value for value in rows if value.get("record") == "reserved"]
        job_id = str(row.get("job_id") or "")
        if any(str(value.get("job_id") or "") == job_id for value in reservations):
            raise ValueError("attempt job is already reserved")
        if len(reservations) >= maximum_attempts:
            raise RuntimeError("calibration attempt ceiling would be exceeded")
        os.lseek(descriptor, 0, os.SEEK_END)
        os.write(
            descriptor,
            (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ledger_jobs(
    path: Path,
) -> tuple[int, set[str], set[str], dict[str, dict[str, dict[str, Any]]]]:
    if not path.exists():
        return 0, set(), set(), {}
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        content = os.read(descriptor, 10_000_000).decode()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    jobs: set[str] = set()
    completed: set[str] = set()
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for line in content.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("attempt ledger contains invalid JSONL") from exc
        if not isinstance(row, Mapping) or row.get("record") not in {
            "reserved",
            "completed",
        }:
            raise ValueError("attempt ledger contains an invalid record")
        job_id = str(row.get("job_id") or "")
        if row["record"] == "reserved":
            if not job_id or job_id in jobs:
                raise ValueError("attempt ledger contains an invalid job reservation")
            jobs.add(job_id)
            records[job_id] = {"reserved": dict(row)}
        elif not job_id or job_id not in jobs or job_id in completed:
            raise ValueError("attempt ledger contains an invalid completion")
        else:
            if row.get("attempt_id") != records[job_id]["reserved"].get(
                "attempt_id"
            ):
                raise ValueError("attempt ledger completion identity mismatch")
            completed.add(job_id)
            records[job_id]["completed"] = dict(row)
    return len(jobs), jobs, completed, records


def _usage_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or any(
        not isinstance(value.get(key), int)
        or isinstance(value.get(key), bool)
        or int(value[key]) < 0
        for key in ("input_tokens", "output_tokens")
    ):
        return {}
    return {
        str(key): int(count)
        for key, count in value.items()
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _child_resources() -> tuple[float, int]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    max_rss = int(usage.ru_maxrss)
    if sys.platform != "darwin":
        max_rss *= 1_024
    return float(usage.ru_utime + usage.ru_stime), max_rss


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _run_level(
    *,
    level: int,
    planned_attempts: int,
    slots: Sequence[int],
    attempt_offset: int,
    completion_offset: int,
    config: Mapping[str, Any],
    evaluation_id: str,
    manifest_sha256: str,
    payload_sha256: str,
    ledger_path: Path,
    maximum_attempts: int,
    attempt_guard: CodexAttemptGuard | None,
    reader: Any,
    dispatch: Callable[[Any], Mapping[str, Any]],
    abort_event: threading.Event,
    canceller: Callable[[], int],
) -> tuple[dict[str, Any], list[dict[str, Any]], int, BaseException | None]:
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "completed": completion_offset}
    interruptions: list[BaseException] = []
    deadline = BASELINE_DEADLINE_SECONDS if level == 1 else WAVE_DEADLINE_SECONDS
    started_at = time.monotonic()
    cpu_started, _rss_started = _child_resources()

    def run_attempt(index: int, slot: int) -> dict[str, Any] | None:
        attempt_number = attempt_offset + index + 1
        attempt_id = f"{evaluation_id}:{attempt_number:03d}"
        job_id = f"c{level}:s{slot + 1:03d}"
        with lock:
            if abort_event.is_set():
                return None
            _reserve_attempt(
                ledger_path,
                {
                    "record": "reserved",
                    "attempt_id": attempt_id,
                    "attempt_number": attempt_number,
                    "job_id": job_id,
                    "stage": config["contract_id"],
                    "level": level,
                    "manifest_sha256": manifest_sha256,
                    "payload_sha256": payload_sha256,
                    "created_at": now_iso(),
                },
                maximum_attempts=maximum_attempts,
            )
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        attempt_started_at = time.monotonic()
        reset_provider_completion()
        try:
            if attempt_guard is None:
                result = dispatch(reader)
            else:
                with attempt_guard.job(job_id):
                    result = dispatch(reader)
            if not isinstance(result, Mapping):
                raise ValueError("provider returned a non-mapping response")
            completion = current_provider_completion()
            _validate_completion(
                completion,
                contract_id=str(config["contract_id"]),
                model=str(config["model"]),
                effort=str(config["reasoning_effort"]),
            )
            row = {
                "attempt_number": attempt_number,
                "job_id": job_id,
                "concurrency": level,
                "slot": slot + 1,
                "status": "valid",
                "latency_seconds": round(time.monotonic() - attempt_started_at, 6),
                "result_sha256": sha256_text(
                    json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
                ),
                "usage": _usage_counts(completion.get("usage")),
            }
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            completion = _safe_completion(
                getattr(error, "provider_completion", None)
                or current_provider_completion()
            )
            row = {
                "attempt_number": attempt_number,
                "job_id": job_id,
                "concurrency": level,
                "slot": slot + 1,
                "status": "failed",
                "latency_seconds": round(time.monotonic() - attempt_started_at, 6),
                "failure_class": _failure_class(error),
                "error_type": type(error).__name__,
                "completion": completion,
            }
            if isinstance(error, ProviderIsolationFailure):
                row["isolation_reason"] = _isolation_reason(error)
            elif error_reason := _error_reason(error):
                row["error_reason"] = error_reason
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                interruptions.append(error)
            abort_event.set()
            try:
                canceller()
            except Exception:
                pass
        finally:
            with lock:
                state["active"] -= 1
        with lock:
            state["completed"] += 1
            row["completion_order"] = state["completed"]
            _append_ledger(
                ledger_path,
                {
                    "record": "completed",
                    "attempt_id": attempt_id,
                    "job_id": job_id,
                    "status": row["status"],
                    "failure_class": row.get("failure_class", ""),
                    "completed_at": now_iso(),
                },
            )
        return row

    executor = ThreadPoolExecutor(max_workers=level)
    futures: list[Future[dict[str, Any] | None]] = []
    timed_out = False
    try:
        futures = [
            executor.submit(run_attempt, index, slot)
            for index, slot in enumerate(slots)
        ]
        _done, pending = wait(futures, timeout=deadline)
        if pending:
            timed_out = True
            abort_event.set()
            try:
                canceller()
            except Exception:
                pass
            for future in pending:
                future.cancel()
    except (KeyboardInterrupt, SystemExit) as error:
        interruptions.append(error)
        abort_event.set()
        try:
            canceller()
        except Exception:
            pass
        for future in futures:
            future.cancel()
    except BaseException:
        abort_event.set()
        canceller()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    attempts = [
        row
        for future in futures
        if not future.cancelled() and (row := future.result()) is not None
    ]
    elapsed = max(time.monotonic() - started_at, 1e-9)
    cpu_finished, max_rss_bytes = _child_resources()
    valid = [row for row in attempts if row["status"] == "valid"]
    latencies = [float(row["latency_seconds"]) for row in valid]
    level_report = {
        "concurrency": level,
        "planned_attempts": planned_attempts,
        "submitted_attempts": len(slots),
        "started_attempts": len(attempts),
        "valid_attempts": len(valid),
        "elapsed_seconds": round(elapsed, 6),
        "calls_per_minute": round(len(valid) * 60.0 / elapsed, 6),
        "median_latency_seconds": round(statistics.median(latencies), 6)
        if latencies
        else None,
        "p95_latency_seconds": round(_percentile(latencies, 0.95), 6)
        if latencies
        else None,
        "peak_in_flight": state["peak"],
        "child_cpu_seconds": round(max(0.0, cpu_finished - cpu_started), 6),
        "child_max_rss_bytes": max_rss_bytes,
        "usage": {
            key: sum(int(row["usage"].get(key, 0)) for row in valid)
            for key in sorted({key for row in valid for key in row["usage"]})
        },
        "completion_order": [
            row["attempt_number"]
            for row in sorted(attempts, key=lambda value: value["completion_order"])
        ],
        "deadline_seconds": deadline,
        "timed_out": timed_out,
        "interrupted": bool(interruptions),
        "status": (
            "valid"
            if not timed_out
            and not interruptions
            and len(valid) == len(slots)
            and len(attempts) == len(slots)
            else "failed"
        ),
    }
    return level_report, attempts, state["peak"], (
        interruptions[0] if interruptions else None
    )


def run_calibration(
    *,
    manifest_path: Path,
    manifest_sha256: str,
    output_path: Path,
    authorization_path: Path | None = None,
    authorization_sha256: str | None = None,
    execute: bool = False,
    resume: bool = False,
    require_clean: bool = True,
    reader_factory: Callable[..., Any] = CodexReader,
    canceller: Callable[[], int] = cancel_active_provider_responses,
) -> tuple[Path, dict[str, Any]]:
    """Run a no-retry calibration only after explicit execution authorization."""

    _verify_runtime_import_root()
    if not execute:
        raise PermissionError(
            "calibration requires explicit execute=True authorization"
        )
    if (authorization_path is None) != (authorization_sha256 is None):
        raise ValueError(
            "authorization_path and authorization_sha256 must be provided together"
        )
    if require_clean and authorization_path is None:
        raise ValueError("production calibration requires a frozen authorization")
    manifest, payload, stage, manifest_path = _validated_manifest(
        manifest_path,
        manifest_sha256,
        require_clean=require_clean,
    )
    output_path = _require_private_path(output_path, label="output")
    if output_path.exists() and not resume:
        raise ValueError("output report already exists")
    if resume and not output_path.is_file():
        raise ValueError("resume requires an existing output report")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.parent.chmod(0o700)
    ledger_root = manifest_path.parent / ".v030-attempts"
    ledger_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_root.chmod(0o700)
    ledger_path = ledger_root / f"{manifest['evaluation_id']}.jsonl"
    ledger_path.touch(mode=0o600)
    ledger_path.chmod(0o600)
    reservation_count, reserved_jobs, completed_jobs, ledger_records = _ledger_jobs(
        ledger_path
    )
    if reservation_count and not resume:
        raise ValueError("attempt ledger already contains consumed attempts")

    config = STAGES[stage]
    dispatch = _prepare_dispatch(stage, payload, workspace=output_path.parent)
    abort_event = threading.Event()
    resume_reason: str | None = None

    if resume:
        previous = _mapping(read_yaml(output_path), label="existing report")
        expected = {
            "evaluation_id": str(manifest["evaluation_id"]),
            "stage": stage,
            "manifest_sha256": manifest_sha256,
            "payload_sha256": str(manifest["payload_sha256"]),
            "maximum_attempts": config["maximum_attempts"],
        }
        if any(previous.get(key) != value for key, value in expected.items()):
            raise ValueError("existing report does not match the frozen calibration")
        if previous.get("status") != "paused":
            raise ValueError("only a paused calibration may resume")
        resume_reason = str(previous.get("stop_reason") or "")
        if resume_reason not in PAUSE_REASONS:
            raise ValueError("paused calibration has no typed resume reason")
        level_reports = [
            _mapping(row, label="existing level")
            for row in previous.get("levels", [])
        ]
        attempt_reports = [
            _mapping(row, label="existing attempt")
            for row in previous.get("attempts", [])
        ]
        reported_jobs = {str(row.get("job_id") or "") for row in attempt_reports}
        if (
            int(previous.get("attempt_count") or -1) != reservation_count
            or reported_jobs != completed_jobs
            or "" in reported_jobs
        ):
            raise ValueError("existing report does not match the attempt ledger")
        for row in attempt_reports:
            job_id = str(row["job_id"])
            reservation = ledger_records[job_id]["reserved"]
            completion = ledger_records[job_id].get("completed", {})
            if (
                int(row.get("attempt_number") or 0)
                != int(reservation.get("attempt_number") or -1)
                or row.get("status") != completion.get("status")
                or str(row.get("failure_class") or "")
                != str(completion.get("failure_class") or "")
                or int(row.get("concurrency") or 0)
                != int(reservation.get("level") or -1)
                or reservation.get("manifest_sha256") != manifest_sha256
                or reservation.get("payload_sha256")
                != str(manifest["payload_sha256"])
                or reservation.get("stage") != config["contract_id"]
            ):
                raise ValueError("existing report does not match the attempt ledger")
        if not level_reports:
            raise ValueError("paused report has no level checkpoint")
        level = int(level_reports[-1].get("concurrency") or 0)
        if level not in config["levels"]:
            raise ValueError("paused report has an invalid concurrency level")
        planned = BASELINE_ATTEMPTS if level == 1 else level
        slots = [
            slot
            for slot in range(planned)
            if f"c{level}:s{slot + 1:03d}" not in reserved_jobs
        ]
        if not slots:
            raise ValueError("paused level has no unfinished jobs")
        if reservation_count + len(slots) > config["maximum_attempts"]:
            raise ValueError("unused attempt allowance cannot complete the paused wave")
        levels_to_run = ((level, planned, slots, True),)
        recommended_concurrency = previous.get("recommended_concurrency")
        stop_reason = str(previous.get("stop_reason") or "paused")
        peak_in_flight = int(previous.get("peak_in_flight") or 0)
        resume_count = int(previous.get("resume_count") or 0) + 1
    else:
        level_reports = []
        attempt_reports = []
        levels_to_run = tuple(
            (
                level,
                BASELINE_ATTEMPTS if level == 1 else level,
                list(range(BASELINE_ATTEMPTS if level == 1 else level)),
                False,
            )
            for level in config["levels"]
        )
        recommended_concurrency = None
        stop_reason = "maximum_level_completed"
        peak_in_flight = 0
        resume_count = 0

    reader = reader_factory(
        model=config["model"],
        reasoning_effort=config["reasoning_effort"],
        allow_cloud=True,
        request_deadline=CALL_DEADLINE_SECONDS,
    )
    setattr(
        reader,
        "credential_forbidden_roots",
        (_REPOSITORY_ROOT, manifest_path.parent, output_path.parent),
    )
    preflight = getattr(reader, "_ensure_codex_preflight", None)
    if callable(preflight):
        preflight()

    attempt_guard: CodexAttemptGuard | None = None
    carried_stage_attempt_count = 0
    if authorization_path is not None and authorization_sha256 is not None:
        attempt_guard = CodexAttemptGuard.start(
            authorization_path,
            authorization_sha256,
            repository_root=_REPOSITORY_ROOT,
            stage=(
                "luna_source_calibration"
                if stage == "source"
                else "terra_relationship_calibration"
            ),
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            resume_reason=resume_reason,
        )
        carried_stage_attempt_count = attempt_guard.carried_stage_attempt_count
        setattr(reader, "attempt_guard", attempt_guard)
    stage_attempt_allowance = (
        int(config["maximum_attempts"]) - carried_stage_attempt_count
    )

    try:
        prior_rate: float | None = None
        prior_level: int | None = None
        pending_interruption: BaseException | None = None

        for level, planned, slots, continuation in levels_to_run:
            if reservation_count + len(slots) > stage_attempt_allowance:
                if continuation:
                    raise ValueError(
                        "unused attempt allowance cannot complete the paused wave"
                    )
                stop_reason = "insufficient_attempt_allowance"
                break
            level_report, attempts, peak, interruption = _run_level(
                level=level,
                planned_attempts=planned,
                slots=slots,
                attempt_offset=reservation_count,
                completion_offset=max(
                    (
                        int(row.get("completion_order") or 0)
                        for row in attempt_reports
                    ),
                    default=0,
                ),
                config=config,
                evaluation_id=str(manifest["evaluation_id"]),
                manifest_sha256=manifest_sha256,
                payload_sha256=str(manifest["payload_sha256"]),
                ledger_path=ledger_path,
                maximum_attempts=int(config["maximum_attempts"]),
                attempt_guard=attempt_guard,
                reader=reader,
                dispatch=dispatch,
                abort_event=abort_event,
                canceller=canceller,
            )
            peak_in_flight = max(peak_in_flight, peak)
            pending_interruption = pending_interruption or interruption
            attempt_reports.extend(attempts)
            reservation_count, reserved_jobs, completed_jobs, ledger_records = (
                _ledger_jobs(ledger_path)
            )
            if continuation:
                level_report["continuation"] = True
            level_reports.append(level_report)
            if level_report["status"] != "valid":
                failures = [row for row in attempts if row["status"] == "failed"]
                failure_class = (
                    str(failures[0]["failure_class"])
                    if failures
                    else "interruption"
                    if level_report["interrupted"]
                    else "timeout"
                    if level_report["timed_out"]
                    else "terminal"
                )
                stop_reason = failure_class
                break
            if continuation:
                break
            rate = float(level_report["calls_per_minute"])
            if prior_rate is not None:
                gain = (
                    ((rate / prior_rate) - 1.0) * 100.0
                    if prior_rate > 0
                    else 0.0
                )
                level_report["gain_percent"] = round(gain, 6)
                if gain < MINIMUM_GAIN_PERCENT:
                    recommended_concurrency = prior_level
                    stop_reason = "gain_below_15_percent"
                    break
            else:
                level_report["gain_percent"] = None
            recommended_concurrency = level
            prior_rate = rate
            prior_level = level

        failures = [row for row in attempt_reports if row["status"] == "failed"]
        failure_classes = {
            str(row.get("failure_class") or "terminal") for row in failures
        }
        timed_out = any(bool(row.get("timed_out")) for row in level_reports)
        interrupted = any(bool(row.get("interrupted")) for row in level_reports)
        status = (
            "failed"
            if failure_classes - PAUSE_REASONS
            else "paused"
            if failure_classes or timed_out or interrupted
            else "failed"
            if not level_reports
            else "failed"
            if any(row["status"] != "valid" for row in level_reports)
            else "passed"
        )
        report = {
            "schema_version": "1",
            "evaluation_id": str(manifest["evaluation_id"]),
            "created_at": now_iso(),
            "status": status,
            "stage": stage,
            "contract_id": config["contract_id"],
            "model": config["model"],
            "reasoning_effort": config["reasoning_effort"],
            "manifest_sha256": manifest_sha256,
            "code_commit": str(manifest["code_commit"]),
            "payload_sha256": str(manifest["payload_sha256"]),
            "call_deadline_seconds": CALL_DEADLINE_SECONDS,
            "minimum_gain_percent": MINIMUM_GAIN_PERCENT,
            "maximum_attempts": config["maximum_attempts"],
            "attempt_count": reservation_count,
            "carried_stage_attempt_count": carried_stage_attempt_count,
            "remaining_attempts": stage_attempt_allowance - reservation_count,
            "retry_count": 0,
            "resume_count": resume_count,
            "peak_in_flight": peak_in_flight,
            "recommended_concurrency": recommended_concurrency,
            "stop_reason": stop_reason,
            "levels": level_reports,
            "attempts": attempt_reports,
            "attempt_ledger": str(ledger_path.relative_to(manifest_path.parent)),
        }
        write_yaml(output_path, report)
        output_path.chmod(0o600)
        ledger_path.chmod(0o600)
    except BaseException as error:
        if attempt_guard is not None:
            reason = (
                "interruption"
                if isinstance(error, (KeyboardInterrupt, SystemExit))
                else _failure_class(error)
            )
            try:
                attempt_guard.finish(
                    "paused" if reason in PAUSE_REASONS else "failed",
                    reason=reason,
                )
            except BaseException:
                pass
        raise

    if attempt_guard is not None:
        try:
            if status == "passed":
                attempt_guard.finish("passed")
            elif status == "paused":
                attempt_guard.finish("paused", reason=stop_reason)
            else:
                terminal_reasons = sorted(failure_classes - PAUSE_REASONS)
                attempt_guard.finish(
                    "failed",
                    reason=terminal_reasons[0] if terminal_reasons else "terminal",
                )
        except BaseException:
            if pending_interruption is None:
                raise
    if pending_interruption is not None:
        raise pending_interruption
    return output_path, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; this command spends subscription attempts")
    report_path, report = run_calibration(
        manifest_path=args.manifest,
        manifest_sha256=args.manifest_sha256,
        authorization_path=args.authorization,
        authorization_sha256=args.authorization_sha256,
        output_path=args.output,
        execute=True,
        resume=args.resume,
    )
    print(report_path)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
