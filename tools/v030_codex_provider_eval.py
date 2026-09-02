#!/usr/bin/env python3
"""Run the private, hash-locked v0.30 Codex contract canary cases."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
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

from auto_zettelkasten.files import (  # noqa: E402
    now_iso,
    read_yaml,
    sha256_file,
    sha256_text,
    write_yaml,
)
from auto_zettelkasten.models import LiteratureMapRequest  # noqa: E402
from auto_zettelkasten.readers import (  # noqa: E402
    CODEX_OUTPUT_CONTRACTS,
    CodexReader,
    ProviderInterrupted,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTimeout,
    ProviderTransportError,
    _CODEX_ERROR_ITEM_CATEGORIES,
    _redact_codex_diagnostic,
    codex_contract_identity,
    current_provider_completion,
    reset_provider_completion,
)
from v030_codex_campaign_guard import CodexCampaignGuard  # noqa: E402


CONTRACTS = (
    "source_bundle",
    "evidence_profile",
    "literature_family_plan",
    "relationship_candidate_selection",
    "relationship_adjudication",
    "cluster_plan",
    "cluster_synthesis",
    "chunk_evidence",
    "relationship_shard_selection",
    "bridge_shard_selection",
    "cluster_proposal",
    "gap_adjudication",
)
SOURCE_CONTRACTS = {"source_bundle", "chunk_evidence"}
CONTRACT_METHODS = {
    "source_bundle": "read_source_bundle",
    "evidence_profile": "profile_source",
    "literature_family_plan": "plan_literature_families",
    "relationship_candidate_selection": "select_relationship_candidates",
    "relationship_adjudication": "adjudicate_relationships",
    "cluster_plan": "plan_clusters",
    "cluster_synthesis": "synthesize_cluster",
    "chunk_evidence": "summarize_chunk",
    "relationship_shard_selection": "select_relationship_shards",
    "bridge_shard_selection": "select_relationship_bridge_shards",
    "cluster_proposal": "propose_clusters",
    "gap_adjudication": "detect_gaps",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_EVALUATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_ISOLATION_SUBTYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
CALL_DEADLINE_SECONDS = 600.0
STAGE_DEADLINE_SECONDS = 7_260.0
CONTROLS = {
    "initial_calls": 12,
    "maximum_attempts": 12,
    "retry_limit": 0,
    "concurrency": 1,
    "call_deadline_seconds": CALL_DEADLINE_SECONDS,
    "stage_deadline_seconds": STAGE_DEADLINE_SECONDS,
    "quota_behavior": "pause",
    "attachments": "none",
    "tool_attempt_behavior": "fail_closed",
    "authentication_behavior": "fail_closed",
    "truncation_behavior": "fail_closed",
}


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
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
    if _git_root(resolved) is not None or _inside(resolved, _REPOSITORY_ROOT):
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
        raise RuntimeError("unable to resolve the evaluation code commit")
    if require_clean:
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if status.returncode or status.stdout.strip():
            raise ValueError("contract smoke requires a clean code commit")
    return commit


def _append_ledger(path: Path, row: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(
            descriptor,
            (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _reserve_attempt(path: Path, row: Mapping[str, Any]) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.lseek(descriptor, 0, os.SEEK_SET)
        rows = [
            json.loads(line)
            for line in os.read(descriptor, 1_000_000).decode().splitlines()
        ]
        reservations = [value for value in rows if value.get("record") == "reserved"]
        job_id = str(row.get("job_id") or "")
        if any(str(value.get("job_id") or "") == job_id for value in reservations):
            raise ValueError("contract attempt is already reserved")
        if len(reservations) >= len(CONTRACTS):
            raise RuntimeError("contract smoke attempt ceiling would be exceeded")
        os.lseek(descriptor, 0, os.SEEK_END)
        os.write(
            descriptor,
            (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode(),
        )
        os.fsync(descriptor)
        return len(reservations) + 1
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ledger_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        rows = [
            json.loads(line)
            for line in os.read(descriptor, 1_000_000).decode().splitlines()
        ]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    records: dict[str, dict[str, Any]] = {}
    for value in rows:
        if not isinstance(value, Mapping) or value.get("record") not in {
            "reserved",
            "completed",
        }:
            raise ValueError("contract attempt ledger contains an invalid record")
        job_id = str(value.get("job_id") or "")
        if value["record"] == "reserved":
            if not job_id or job_id in records:
                raise ValueError("contract attempt ledger contains a duplicate job")
            records[job_id] = {
                "attempt_number": len(records) + 1,
                "reserved": dict(value),
            }
        elif (
            not job_id
            or job_id not in records
            or "completed" in records[job_id]
        ):
            raise ValueError("contract attempt ledger contains an invalid completion")
        else:
            records[job_id]["completed"] = dict(value)
    return records


def _reservation_count(path: Path) -> int:
    return len(_ledger_records(path))


def _safe_completion(value: Any) -> dict[str, Any]:
    completion = dict(value) if isinstance(value, Mapping) else {}
    usage = completion.get("usage")
    safe_usage = {}
    if isinstance(usage, Mapping) and all(
        isinstance(usage.get(key), int)
        and not isinstance(usage.get(key), bool)
        and int(usage[key]) >= 0
        for key in ("input_tokens", "output_tokens")
    ):
        safe_usage = {
            str(key): int(count)
            for key, count in usage.items()
            if isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        }
    allowed = {
        "provider",
        "model",
        "reasoning_effort",
        "adapter_protocol",
        "cli_profile",
        "codex_cli_version",
        "contract_id",
        "schema_hash",
        "output_reservation",
        "feature_manifest_hash",
        "finish_reason",
        "max_output_tokens",
    }
    return {
        **{key: completion[key] for key in allowed if key in completion},
        "usage": safe_usage,
    }


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
    if not _safe_completion(completion)["usage"]:
        raise ValueError("provider completion usage is missing")


def _require_arguments(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    arguments = _require_mapping(payload.get("arguments"), label="case arguments")
    unknown = sorted(set(arguments) - allowed)
    missing = sorted(required - set(arguments))
    if unknown:
        raise ValueError(f"unknown case arguments: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing case arguments: {', '.join(missing)}")
    return arguments


def _optional_mapping(value: Any, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _require_mapping(value, label=label)


def _literature_arguments(
    payload: Mapping[str, Any],
    *,
    workspace: Path,
    model: str,
    effort: str,
) -> tuple[list[dict[str, Any]], LiteratureMapRequest, dict[str, Any] | None]:
    arguments = _require_arguments(
        payload,
        allowed={"profiles", "request", "context"},
        required={"profiles", "request"},
    )
    profiles = arguments["profiles"]
    if not isinstance(profiles, list) or any(
        not isinstance(profile, Mapping) for profile in profiles
    ):
        raise ValueError("profiles must be a list of mappings")
    request_values = _require_mapping(arguments["request"], label="request")
    request_values.update(
        {
            "workspace": str(workspace),
            "provider": "codex",
            "model": model,
            "reasoning_effort": effort,
            "allow_cloud": True,
            "provider_concurrency": 1,
            "max_provider_spend_usd": None,
        }
    )
    request = LiteratureMapRequest.from_dict(request_values)
    context = _optional_mapping(arguments.get("context"), label="context")
    return [dict(profile) for profile in profiles], request, context


def dispatch_case(
    reader: Any,
    contract_id: str,
    payload: Mapping[str, Any],
    *,
    workspace: Path,
    model: str,
    effort: str,
) -> Mapping[str, Any]:
    """Dispatch one already-verified private payload to its public reader method."""

    if contract_id == "source_bundle":
        arguments = _require_arguments(
            payload,
            allowed={"text", "metadata", "question"},
            required={"text", "metadata"},
        )
        result = reader.read_source_bundle(
            str(arguments["text"]),
            _require_mapping(arguments["metadata"], label="metadata"),
            question=arguments.get("question"),
        )
    elif contract_id == "evidence_profile":
        arguments = _require_arguments(
            payload,
            allowed={"note", "question", "context"},
            required={"note"},
        )
        result = reader.profile_source(
            _require_mapping(arguments["note"], label="note"),
            question=arguments.get("question"),
            context=_optional_mapping(arguments.get("context"), label="context"),
        )
    elif contract_id == "chunk_evidence":
        arguments = _require_arguments(
            payload,
            allowed={
                "text",
                "metadata",
                "question",
                "chunk_id",
                "locator",
                "max_output_tokens",
                "deadline_seconds",
            },
            required={"text", "metadata"},
        )
        result = reader.summarize_chunk(
            str(arguments["text"]),
            _require_mapping(arguments["metadata"], label="metadata"),
            question=arguments.get("question"),
            chunk_id=str(arguments.get("chunk_id") or ""),
            locator=str(arguments.get("locator") or ""),
            max_output_tokens=arguments.get("max_output_tokens"),
            deadline_seconds=arguments.get("deadline_seconds"),
        )
    else:
        profiles, request, context = _literature_arguments(
            payload, workspace=workspace, model=model, effort=effort
        )
        if contract_id == "literature_family_plan":
            result = reader.plan_literature_families(
                profiles, request, context=context
            )
        elif contract_id == "relationship_candidate_selection":
            result = reader.select_relationship_candidates(
                profiles, request, context=context
            )
        elif contract_id == "relationship_adjudication":
            result = reader.adjudicate_relationships(
                profiles, request, context=context
            )
        elif contract_id == "cluster_plan":
            result = reader.plan_clusters(profiles, request, context=context)
        elif contract_id == "cluster_synthesis":
            result = reader.synthesize_cluster(profiles, request, context=context)
        elif contract_id == "relationship_shard_selection":
            result = reader.select_relationship_shards(
                profiles, request, context=context
            )
        elif contract_id == "bridge_shard_selection":
            result = reader.select_relationship_bridge_shards(
                profiles, request, context=context
            )
        elif contract_id == "cluster_proposal":
            result = reader.propose_clusters(profiles, request, context=context)
        elif contract_id == "gap_adjudication":
            result = reader.detect_gaps(profiles, request, context=context)
        else:
            raise ValueError(f"unsupported contract: {contract_id}")
    if not isinstance(result, Mapping):
        raise ValueError(f"{contract_id} returned a non-mapping response")
    return result


def _validated_cases(
    manifest_path: Path,
    expected_manifest_sha256: str,
    *,
    require_clean: bool,
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    if not _SHA256.fullmatch(expected_manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    manifest_path = _require_private_path(manifest_path, label="manifest")
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    actual_manifest_sha256 = sha256_file(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    manifest = _require_mapping(read_yaml(manifest_path), label="manifest")
    if str(manifest.get("schema_version") or "") != "1":
        raise ValueError("manifest schema_version must be '1'")
    if manifest.get("code_commit") != _git_commit(require_clean=require_clean):
        raise ValueError("manifest code commit mismatch")
    if _require_mapping(manifest.get("controls"), label="manifest controls") != CONTROLS:
        raise ValueError("manifest controls do not match the bounded contract gate")
    rows = manifest.get("cases")
    if not isinstance(rows, list):
        raise ValueError("manifest cases must be a list")
    if set(CONTRACTS) != set(CODEX_OUTPUT_CONTRACTS):
        raise RuntimeError("runner contract inventory does not match Codex contracts")

    root = manifest_path.resolve().parent
    cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    case_ids: set[str] = set()
    for index, raw in enumerate(rows):
        row = _require_mapping(raw, label=f"case {index}")
        case_id = str(row.get("case_id") or "").strip()
        contract_id = str(row.get("contract_id") or "").strip()
        payload_value = row.get("payload")
        payload_sha256 = str(row.get("payload_sha256") or "")
        if not case_id or case_id in case_ids:
            raise ValueError("case IDs must be non-empty and unique")
        if contract_id not in CONTRACTS or contract_id in cases:
            raise ValueError(f"unknown or duplicate contract: {contract_id or '<missing>'}")
        if not isinstance(payload_value, str) or not payload_value.strip():
            raise ValueError(f"{case_id} payload must be a relative path")
        if not _SHA256.fullmatch(payload_sha256):
            raise ValueError(f"{case_id} payload_sha256 is invalid")
        relative = Path(payload_value)
        if relative.is_absolute():
            raise ValueError(f"{case_id} payload must be a relative path")
        payload_path = (root / relative).resolve()
        if root not in payload_path.parents or not payload_path.is_file():
            raise ValueError(f"{case_id} payload is missing or outside the manifest root")
        if sha256_file(payload_path) != payload_sha256:
            raise ValueError(f"{case_id} payload SHA-256 mismatch")
        payload = _require_mapping(read_yaml(payload_path), label=f"{case_id} payload")
        if str(payload.get("contract_id") or "") != contract_id:
            raise ValueError(f"{case_id} payload contract does not match manifest")
        model, effort = (
            ("gpt-5.6-luna", "medium")
            if contract_id in SOURCE_CONTRACTS
            else ("gpt-5.6-terra", "medium")
        )
        if row.get("contract_identity") != codex_contract_identity(
            contract_id, model, effort
        ):
            raise ValueError(f"{case_id} contract identity mismatch")
        cases[contract_id] = (row, payload)
        case_ids.add(case_id)
    missing = sorted(set(CONTRACTS) - set(cases))
    if missing:
        raise ValueError(f"manifest is missing contracts: {', '.join(missing)}")
    if len(rows) != len(CONTRACTS):
        raise ValueError(f"manifest must contain exactly {len(CONTRACTS)} cases")
    return manifest, cases


def _failure_class(error: BaseException) -> str:
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


def run_evaluation(
    *,
    manifest_path: Path,
    manifest_sha256: str,
    workspace: Path,
    authorization_path: Path | None = None,
    authorization_sha256: str | None = None,
    evaluation_id: str | None = None,
    execute: bool = False,
    resume: bool = False,
    require_clean: bool = True,
    reader_factory: Callable[..., Any] = CodexReader,
) -> tuple[Path, dict[str, Any]]:
    """Validate every private input, then run the bounded contract smoke."""

    _verify_runtime_import_root()
    if not execute:
        raise PermissionError("contract smoke requires explicit execute=True authorization")
    if (authorization_path is None) != (authorization_sha256 is None):
        raise ValueError(
            "authorization_path and authorization_sha256 must be provided together"
        )
    if require_clean and authorization_path is None:
        raise ValueError("production contract smoke requires a frozen authorization")
    manifest_path = _require_private_path(manifest_path, label="manifest")
    manifest, cases = _validated_cases(
        manifest_path,
        manifest_sha256,
        require_clean=require_clean,
    )
    manifest_id = str(manifest.get("evaluation_id") or "").strip()
    if evaluation_id is not None and str(evaluation_id).strip() != manifest_id:
        raise ValueError("evaluation_id override must match the frozen manifest")
    selected_id = manifest_id
    if not _EVALUATION_ID.fullmatch(selected_id):
        raise ValueError("evaluation_id must be 1-96 safe filename characters")
    workspace = _require_private_path(workspace, label="workspace")
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.chmod(0o700)
    report_path = (
        workspace
        / "11_state/evaluations/codex-provider"
        / f"{selected_id}.yml"
    )
    ledger_root = manifest_path.parent / ".v030-attempts"
    ledger_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_root.chmod(0o700)
    ledger_path = ledger_root / f"{selected_id}.jsonl"
    ledger_records = _ledger_records(ledger_path)
    if not resume and (report_path.exists() or ledger_records):
        raise ValueError("contract smoke already contains consumed attempts")
    if resume and (not report_path.is_file() or not ledger_records):
        raise ValueError("resume requires an existing report and attempt ledger")

    resume_reason: str | None = None
    if resume:
        previous = _require_mapping(read_yaml(report_path), label="existing report")
        expected = {
            "evaluation_id": selected_id,
            "manifest_sha256": manifest_sha256,
            "code_commit": str(manifest["code_commit"]),
            "maximum_attempts": len(CONTRACTS),
        }
        if any(previous.get(key) != value for key, value in expected.items()):
            raise ValueError("existing report does not match the frozen contract smoke")
        if previous.get("status") != "paused":
            raise ValueError("only a paused contract smoke may resume")
        resume_reason = str(previous.get("paused_by") or "")
        if resume_reason not in {"quota", "timeout", "interruption"}:
            raise ValueError("paused contract smoke has an invalid pause reason")
        reports = [
            _require_mapping(row, label="existing case")
            for row in previous.get("cases", [])
        ]
        reported_contracts = {str(row.get("contract_id") or "") for row in reports}
        if (
            len(reported_contracts) != len(reports)
            or reported_contracts != set(ledger_records)
            or any("completed" not in record for record in ledger_records.values())
        ):
            raise ValueError("existing report does not match the contract attempt ledger")
        for row in reports:
            contract_id = str(row["contract_id"])
            record = ledger_records[contract_id]
            attempt = _require_mapping(row.get("attempts", [{}])[-1], label="attempt")
            completion = record["completed"]
            expected_status = "valid" if row.get("status") == "valid" else "failed"
            if (
                int(attempt.get("attempt") or 0) != record["attempt_number"]
                or attempt.get("status") != completion.get("status")
                or expected_status != completion.get("status")
                or str(attempt.get("failure_class") or "")
                != str(completion.get("failure_class") or "")
                or record["reserved"].get("contract_id") != contract_id
                or record["reserved"].get("manifest_sha256") != manifest_sha256
                or row.get("payload_sha256")
                != str(cases[contract_id][0]["payload_sha256"])
            ):
                raise ValueError(
                    "existing report does not match the contract attempt ledger"
                )
        resume_count = int(previous.get("resume_count") or 0) + 1
    else:
        reports = []
        resume_count = 0

    remaining_contracts = [
        contract_id for contract_id in CONTRACTS if contract_id not in ledger_records
    ]
    if not remaining_contracts:
        raise ValueError("paused contract smoke has no unspent attempts")

    readers: dict[tuple[str, str], Any] = {}
    for contract_id in remaining_contracts:
        model, effort = (
            ("gpt-5.6-luna", "medium")
            if contract_id in SOURCE_CONTRACTS
            else ("gpt-5.6-terra", "medium")
        )
        if (model, effort) in readers:
            continue
        reader = reader_factory(
            model=model,
            reasoning_effort=effort,
            allow_cloud=True,
            request_deadline=CALL_DEADLINE_SECONDS,
        )
        setattr(
            reader,
            "credential_forbidden_roots",
            (_REPOSITORY_ROOT, manifest_path.parent, workspace),
        )
        preflight = getattr(reader, "_ensure_codex_preflight", None)
        if callable(preflight):
            preflight()
        readers[(model, effort)] = reader

    attempt_guard = (
        CodexCampaignGuard.start(
            authorization_path,
            str(authorization_sha256),
            repository_root=_REPOSITORY_ROOT,
            stage="final_head_contract_smoke",
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            evaluation_id=selected_id,
            run_id=selected_id,
            source_attempt_limit=3,
            relationship_attempt_limit=9,
            total_attempt_limit=12,
            resume_reason=resume_reason,
        )
        if authorization_path is not None
        else None
    )
    if attempt_guard is not None:
        for reader in readers.values():
            setattr(reader, "attempt_guard", attempt_guard)

    guard_finished = False
    try:
        retry_count = 0
        tool_attempts = 0
        paused_by = ""
        pending_error: BaseException | None = None
        started_at = time.monotonic()

        for contract_id in remaining_contracts:
            remaining = STAGE_DEADLINE_SECONDS - (time.monotonic() - started_at)
            if remaining <= 0:
                paused_by = "timeout"
                break
            row, payload = cases[contract_id]
            model, effort = (
                ("gpt-5.6-luna", "medium")
                if contract_id in SOURCE_CONTRACTS
                else ("gpt-5.6-terra", "medium")
            )
            reader = readers[(model, effort)]
            setattr(reader, "request_deadline", min(CALL_DEADLINE_SECONDS, remaining))
            attempts: list[dict[str, Any]] = []
            result_sha256 = ""
            for _attempt in (1,):
                attempt_number = _reserve_attempt(
                    ledger_path,
                    {
                        "record": "reserved",
                        "job_id": contract_id,
                        "contract_id": contract_id,
                        "manifest_sha256": manifest_sha256,
                        "created_at": now_iso(),
                    },
                )
                reset_provider_completion()
                attempt_status = "incomplete"
                try:
                    with (
                        attempt_guard.job(contract_id)
                        if attempt_guard is not None
                        else nullcontext()
                    ):
                        result = dispatch_case(
                            reader,
                            contract_id,
                            payload,
                            workspace=workspace,
                            model=model,
                            effort=effort,
                        )
                    completion = current_provider_completion()
                    _validate_completion(
                        completion,
                        contract_id=contract_id,
                        model=model,
                        effort=effort,
                    )
                    usage = _safe_completion(completion)["usage"]
                    result_sha256 = sha256_text(
                        json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
                    )
                    attempts.append(
                        {
                            "attempt": attempt_number,
                            "status": "valid",
                            "completion": _safe_completion(completion),
                            "usage_complete": isinstance(usage, Mapping) and bool(usage),
                        }
                    )
                    attempt_status = "valid"
                    break
                except (KeyboardInterrupt, SystemExit) as error:
                    attempts.append(
                        {
                            "attempt": attempt_number,
                            "status": "failed",
                            "failure_class": "interruption",
                            "error_type": type(error).__name__,
                            "error": _redact_codex_diagnostic(str(error)),
                            "completion": _safe_completion(
                                current_provider_completion()
                            ),
                        }
                    )
                    attempt_status = "failed"
                    paused_by = "interruption"
                    pending_error = error
                    break
                except Exception as error:  # report the typed boundary without losing later cases
                    failure_class = _failure_class(error)
                    completion = _safe_completion(
                        getattr(error, "provider_completion", None)
                        or current_provider_completion()
                    )
                    attempt = {
                        "attempt": attempt_number,
                        "status": "failed",
                        "failure_class": failure_class,
                        "error_type": type(error).__name__,
                        "completion": completion,
                    }
                    if isinstance(error, ProviderIsolationFailure):
                        attempt["isolation_reason"] = _isolation_reason(error)
                    elif error_reason := _error_reason(error):
                        attempt["error_reason"] = error_reason
                    else:
                        attempt["error"] = _redact_codex_diagnostic(str(error))
                    attempts.append(attempt)
                    attempt_status = "failed"
                    if str(attempt.get("isolation_reason") or "").startswith(
                        "disallowed_item:"
                    ):
                        tool_attempts += 1
                    if failure_class in {"quota", "timeout", "interruption"}:
                        paused_by = failure_class
                    break
                finally:
                    _append_ledger(
                        ledger_path,
                        {
                            "record": "completed",
                            "job_id": contract_id,
                            "status": attempt_status,
                            "failure_class": (
                                attempts[-1].get("failure_class", "")
                                if attempts
                                else "interruption"
                            ),
                            "completed_at": now_iso(),
                        },
                    )
            valid_attempts = [attempt for attempt in attempts if attempt["status"] == "valid"]
            reports.append(
                {
                    "case_id": str(row["case_id"]),
                    "contract_id": contract_id,
                    "model": model,
                    "reasoning_effort": effort,
                    "payload_sha256": str(row["payload_sha256"]),
                    "status": "valid" if valid_attempts else "failed",
                    "first_pass_valid": bool(valid_attempts and len(attempts) == 1),
                    "result_sha256": result_sha256,
                    "attempts": attempts,
                }
            )
            if reports[-1]["status"] == "failed":
                break

        first_pass_valid_count = sum(bool(row["first_pass_valid"]) for row in reports)
        usage_complete_count = sum(
            bool(row["attempts"][-1].get("usage_complete"))
            for row in reports
            if row["status"] == "valid"
        )
        failures = [
            attempt
            for row in reports
            for attempt in row.get("attempts", [])
            if attempt.get("status") == "failed"
        ]
        failure_classes = {
            str(attempt.get("failure_class") or "terminal") for attempt in failures
        }
        paused_classes = failure_classes & {"quota", "timeout", "interruption"}
        terminal_failure = bool(failure_classes - {"quota", "timeout", "interruption"})
        if not paused_by and paused_classes:
            paused_by = sorted(paused_classes)[0]
        passed = (
            len(reports) == len(CONTRACTS)
            and all(row["status"] == "valid" for row in reports)
            and first_pass_valid_count == len(CONTRACTS)
            and retry_count == 0
            and tool_attempts == 0
            and usage_complete_count == len(CONTRACTS)
        )
        report = {
            "schema_version": "1",
            "evaluation_id": selected_id,
            "created_at": now_iso(),
            "status": (
                "passed"
                if passed
                else "failed"
                if terminal_failure
                else "paused"
                if paused_by
                else "failed"
            ),
            "manifest_sha256": manifest_sha256,
            "code_commit": str(manifest["code_commit"]),
            "concurrency": 1,
            "call_deadline_seconds": CALL_DEADLINE_SECONDS,
            "stage_deadline_seconds": STAGE_DEADLINE_SECONDS,
            "initial_call_ceiling": len(CONTRACTS),
            "maximum_attempts": len(CONTRACTS),
            "retry_call_ceiling": 0,
            "attempt_count": _reservation_count(ledger_path),
            "retry_count": retry_count,
            "resume_count": resume_count,
            "first_pass_valid_count": first_pass_valid_count,
            "usage_complete_count": usage_complete_count,
            "tool_attempt_count": tool_attempts,
            "paused_by": paused_by,
            "attempt_ledger": str(ledger_path.relative_to(manifest_path.parent)),
            "cases": reports,
        }
        write_yaml(report_path, report)
        report_path.chmod(0o600)
        ledger_path.chmod(0o600)
        if attempt_guard is not None:
            if report["status"] == "passed":
                attempt_guard.finish("passed")
            elif report["status"] == "paused":
                attempt_guard.finish("paused", reason=paused_by)
            else:
                attempt_guard.finish(
                    "failed",
                    reason=(sorted(failure_classes)[0] if failure_classes else "terminal"),
            )
        guard_finished = True
        if pending_error is not None:
            raise pending_error
        return report_path, report
    except BaseException as error:
        if attempt_guard is not None and not guard_finished:
            try:
                failure_class = _failure_class(error)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    attempt_guard.finish("paused", reason="interruption")
                elif failure_class in {"quota", "timeout", "interruption"}:
                    attempt_guard.finish("paused", reason=failure_class)
                else:
                    attempt_guard.finish("failed", reason=failure_class)
            except Exception:
                pass
        if pending_error is not None and error is not pending_error:
            raise pending_error from error
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--evaluation-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; this command spends subscription attempts")
    report_path, report = run_evaluation(
        manifest_path=args.manifest,
        manifest_sha256=args.manifest_sha256,
        authorization_path=args.authorization,
        authorization_sha256=args.authorization_sha256,
        workspace=args.workspace,
        evaluation_id=args.evaluation_id,
        execute=True,
        resume=args.resume,
    )
    print(report_path)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
