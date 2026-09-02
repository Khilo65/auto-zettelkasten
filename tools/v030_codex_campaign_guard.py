#!/usr/bin/env python3
"""Evaluation-only, hash-locked Codex campaign attempt guard."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from auto_zettelkasten.codex_attempt_guard import _ACTIVE_GUARD, _ACTIVE_JOB
from auto_zettelkasten.readers import CODEX_OUTPUT_CONTRACTS


PAUSE_REASONS = frozenset({"quota", "timeout", "interruption"})
SOURCE_CONTRACTS = frozenset({"source_bundle", "chunk_evidence", "evidence_profile"})
RELATIONSHIP_CONTRACTS = frozenset(CODEX_OUTPUT_CONTRACTS) - SOURCE_CONTRACTS
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LEDGER_LIMIT = 16_000_000


class CodexCampaignGuardError(RuntimeError):
    pass


class CodexCampaignCeilingExceeded(CodexCampaignGuardError):
    pass


class CodexCampaignStateError(CodexCampaignGuardError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    return next(
        (parent for parent in (candidate, *candidate.parents) if (parent / ".git").exists()),
        None,
    )


def _private_file(path: Path, repository_root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if _inside(resolved, repository_root) or _git_root(resolved) is not None:
        raise ValueError(f"{label} must be outside Git repositories")
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist")
    return resolved


def _private_output(path: Path, repository_root: Path, *, label: str) -> Path:
    original = path.expanduser()
    resolved = original.resolve(strict=False)
    if not original.is_absolute() or str(resolved) != str(original):
        raise ValueError(f"{label} must be an absolute canonical path")
    if _inside(resolved, repository_root) or _git_root(resolved) is not None:
        raise ValueError(f"{label} must be outside Git repositories")
    if not resolved.parent.is_dir():
        raise ValueError(f"{label} parent does not exist")
    return resolved


def _clean_commit(repository_root: Path) -> str:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository_root,
        capture_output=True,
        check=False,
        text=True,
    )
    commit = head.stdout.strip()
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if head.returncode or not _COMMIT.fullmatch(commit):
        raise ValueError("unable to resolve the campaign code commit")
    if status.returncode or status.stdout.strip():
        raise ValueError("Codex campaign gates require a clean code commit")
    return commit


def _positive_limit(value: Any, *, label: str, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _authorization(
    path: Path,
    expected_sha256: str,
    repository_root: Path,
) -> tuple[dict[str, Any], Path]:
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("authorization_sha256 must be a lowercase SHA-256 digest")
    authorization_path = _private_file(path, repository_root, label="authorization")
    if _sha256(authorization_path) != expected_sha256:
        raise ValueError("authorization SHA-256 mismatch")
    try:
        value = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("authorization must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("authorization must be a JSON object")
    authorization = dict(value)
    if authorization.get("schema_version") != 1:
        raise ValueError("authorization schema_version must be 1")
    for name in ("authorization_id", "evaluation_id", "run_id", "stage"):
        field_value = authorization.get(name)
        if not isinstance(field_value, str) or not _SAFE_ID.fullmatch(field_value):
            raise ValueError(f"authorization {name} is invalid")
    code_commit = authorization.get("code_commit")
    if not isinstance(code_commit, str) or not _COMMIT.fullmatch(code_commit):
        raise ValueError("authorization code_commit is invalid")
    manifest_sha256 = authorization.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or not _SHA256.fullmatch(manifest_sha256):
        raise ValueError("authorization manifest_sha256 is invalid")
    source_limit = _positive_limit(
        authorization.get("source_attempt_limit"), label="source_attempt_limit"
    )
    relationship_limit = _positive_limit(
        authorization.get("relationship_attempt_limit"),
        label="relationship_attempt_limit",
    )
    total_limit = _positive_limit(
        authorization.get("total_attempt_limit"),
        label="total_attempt_limit",
        allow_zero=False,
    )
    if source_limit + relationship_limit != total_limit:
        raise ValueError("total_attempt_limit must equal its role limits")
    ledger_value = authorization.get("ledger")
    if not isinstance(ledger_value, str) or not ledger_value:
        raise ValueError("authorization ledger is required")
    ledger_path = _private_output(
        Path(ledger_value), repository_root, label="authorization ledger"
    )
    return authorization, ledger_path


def _binding(authorization: Mapping[str, Any], authorization_sha256: str) -> dict[str, Any]:
    return {
        "authorization_sha256": authorization_sha256,
        "code_commit": authorization["code_commit"],
        "manifest_sha256": authorization["manifest_sha256"],
        "evaluation_id": authorization["evaluation_id"],
        "run_id": authorization["run_id"],
        "stage": authorization["stage"],
        "source_attempt_limit": authorization["source_attempt_limit"],
        "relationship_attempt_limit": authorization["relationship_attempt_limit"],
        "total_attempt_limit": authorization["total_attempt_limit"],
    }


def _open_regular(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("campaign ledger must be a regular file")
    return descriptor


def _append(descriptor: int, row: Mapping[str, Any]) -> None:
    payload = (json.dumps(dict(row), sort_keys=True) + "\n").encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_END)
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]
    os.fsync(descriptor)


def _read_rows(descriptor: int) -> list[dict[str, Any]]:
    size = os.fstat(descriptor).st_size
    if size > _LEDGER_LIMIT:
        raise ValueError("campaign ledger exceeds its byte ceiling")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = os.read(descriptor, _LEDGER_LIMIT + 1).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("campaign ledger contains invalid JSONL") from exc
        if not isinstance(value, Mapping):
            raise ValueError("campaign ledger rows must be JSON objects")
        rows.append(dict(value))
    return rows


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _header(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {"record": "authorization", "schema_version": 1, **dict(binding)}


def initialize_codex_campaign_ledger(
    authorization_path: Path,
    authorization_sha256: str,
    *,
    repository_root: Path,
) -> Path:
    root = repository_root.expanduser().resolve()
    authorization, ledger_path = _authorization(
        authorization_path, authorization_sha256, root
    )
    if _clean_commit(root) != authorization["code_commit"]:
        raise ValueError("authorization code_commit does not match clean Git HEAD")
    descriptor = _open_regular(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        _append(descriptor, _header(_binding(authorization, authorization_sha256)))
    finally:
        os.close(descriptor)
    _fsync_parent(ledger_path)
    return ledger_path


def initialize_codex_campaign(
    manifest_path: Path,
    authorization_path: Path,
    ledger_path: Path,
    *,
    repository_root: Path,
    authorization_id: str,
    evaluation_id: str,
    run_id: str,
    stage: str,
    source_attempt_limit: int,
    relationship_attempt_limit: int,
) -> dict[str, Any]:
    """Create one hash-bound authorization and its count-before-launch ledger."""
    root = repository_root.expanduser().resolve()
    manifest = _private_file(manifest_path, root, label="campaign manifest")
    authorization = _private_output(authorization_path, root, label="campaign authorization")
    ledger = _private_output(ledger_path, root, label="campaign ledger")
    if len({manifest, authorization, ledger}) != 3:
        raise ValueError("campaign manifest, authorization, and ledger must be distinct")
    for label, value in (
        ("authorization_id", authorization_id),
        ("evaluation_id", evaluation_id),
        ("run_id", run_id),
        ("stage", stage),
    ):
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"{label} is invalid")
    source_limit = _positive_limit(source_attempt_limit, label="source_attempt_limit")
    relationship_limit = _positive_limit(
        relationship_attempt_limit, label="relationship_attempt_limit"
    )
    total_limit = source_limit + relationship_limit
    if total_limit <= 0:
        raise ValueError("campaign total attempt limit must be positive")
    payload = {
        "schema_version": 1,
        "authorization_id": authorization_id,
        "ledger": str(ledger),
        "code_commit": _clean_commit(root),
        "manifest_sha256": _sha256(manifest),
        "evaluation_id": evaluation_id,
        "run_id": run_id,
        "stage": stage,
        "source_attempt_limit": source_limit,
        "relationship_attempt_limit": relationship_limit,
        "total_attempt_limit": total_limit,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("ascii")
    authorization_descriptor = ledger_descriptor = -1
    try:
        authorization_descriptor = _open_regular(
            authorization, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
        ledger_descriptor = _open_regular(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        view = memoryview(encoded)
        while view:
            view = view[os.write(authorization_descriptor, view) :]
        os.fsync(authorization_descriptor)
        authorization_sha256 = hashlib.sha256(encoded).hexdigest()
        _append(ledger_descriptor, _header(_binding(payload, authorization_sha256)))
    except Exception:
        for path, descriptor in (
            (authorization, authorization_descriptor),
            (ledger, ledger_descriptor),
        ):
            if descriptor >= 0:
                os.close(descriptor)
                path.unlink(missing_ok=True)
        raise
    os.close(authorization_descriptor)
    os.close(ledger_descriptor)
    os.chmod(authorization, 0o400)
    _fsync_parent(authorization)
    if ledger.parent != authorization.parent:
        _fsync_parent(ledger)
    return {
        **payload,
        "authorization": str(authorization),
        "authorization_sha256": authorization_sha256,
        "manifest": str(manifest),
    }


def _contract_role(contract_id: str) -> Literal["source", "relationship"]:
    if contract_id in SOURCE_CONTRACTS:
        return "source"
    if contract_id in RELATIONSHIP_CONTRACTS:
        return "relationship"
    raise CodexCampaignStateError(
        f"Codex contract is not authorized for this campaign: {contract_id}"
    )


def _audit(
    rows: list[dict[str, Any]], binding: Mapping[str, Any]
) -> tuple[
    str | None,
    str,
    str,
    set[str],
    dict[str, str],
    dict[str, int],
    dict[str, int],
]:
    if not rows or rows[0] != _header(binding):
        raise ValueError("campaign ledger authorization header mismatch")
    state: str | None = None
    reason = ""
    session_id = ""
    session_jobs: set[str] = set()
    job_contracts: dict[str, str] = {}
    job_attempts: dict[str, int] = {}
    counts = {"source": 0, "relationship": 0}
    expected_binding = dict(binding)
    for row in rows[1:]:
        record = row.get("record")
        row_binding = {key: row.get(key) for key in expected_binding}
        if row_binding != expected_binding:
            raise ValueError("campaign ledger binding changed")
        if record == "run_started":
            candidate = str(row.get("session_id") or "")
            resume_reason = str(row.get("resume_reason") or "")
            if not _SAFE_ID.fullmatch(candidate):
                raise ValueError("campaign ledger session ID is invalid")
            if state is None:
                if resume_reason:
                    raise ValueError("fresh campaign run cannot carry a resume reason")
            elif state != "paused" or resume_reason != reason:
                raise ValueError("campaign ledger contains an invalid resume")
            state, reason, session_id = "running", "", candidate
            session_jobs = set()
        elif record == "reserved":
            contract_id = str(row.get("contract_id") or "")
            job_id = str(row.get("job_id") or "")
            role = _contract_role(contract_id)
            if (
                state != "running"
                or row.get("session_id") != session_id
                or not _SAFE_ID.fullmatch(job_id)
                or job_id in session_jobs
                or row.get("role") != role
                or (
                    job_id in job_contracts
                    and job_contracts[job_id] != contract_id
                )
            ):
                raise ValueError("campaign ledger contains an invalid reservation")
            session_jobs.add(job_id)
            job_contracts.setdefault(job_id, contract_id)
            job_attempts[job_id] = job_attempts.get(job_id, 0) + 1
            counts[role] += 1
            if (
                row.get("job_attempt_number") != job_attempts[job_id]
                or row.get("role_attempt_number") != counts[role]
                or row.get("total_attempt_number") != sum(counts.values())
            ):
                raise ValueError("campaign ledger attempt numbering is invalid")
        elif record == "run_finished":
            finished_state = str(row.get("state") or "")
            finished_reason = str(row.get("reason") or "")
            if state != "running" or row.get("session_id") != session_id:
                raise ValueError("campaign ledger contains an invalid finish")
            if finished_state not in {"paused", "failed", "passed"}:
                raise ValueError("campaign ledger finish state is invalid")
            if finished_state == "paused":
                if finished_reason not in PAUSE_REASONS:
                    raise ValueError("campaign ledger pause reason is invalid")
            elif finished_state == "passed":
                if finished_reason:
                    raise ValueError("passed campaign run cannot carry a reason")
            elif not _SAFE_ID.fullmatch(finished_reason):
                raise ValueError("failed campaign run reason is invalid")
            state, reason = finished_state, finished_reason
        else:
            raise ValueError("campaign ledger contains an unknown record")
    if counts["source"] > binding["source_attempt_limit"]:
        raise ValueError("campaign ledger exceeds its source ceiling")
    if counts["relationship"] > binding["relationship_attempt_limit"]:
        raise ValueError("campaign ledger exceeds its relationship ceiling")
    if sum(counts.values()) > binding["total_attempt_limit"]:
        raise ValueError("campaign ledger exceeds its total ceiling")
    return (
        state,
        reason,
        session_id,
        session_jobs,
        job_contracts,
        job_attempts,
        counts,
    )


def _run_lock(path: Path) -> int:
    descriptor = _open_regular(
        path.with_name(f"{path.name}.run.lock"), os.O_RDWR | os.O_CREAT
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise CodexCampaignStateError("Codex campaign run is already active") from exc
    return descriptor


@dataclass(frozen=True, slots=True)
class CodexCampaignGuard:
    authorization_sha256: str
    ledger_path: Path
    code_commit: str
    manifest_sha256: str
    evaluation_id: str
    run_id: str
    stage: str
    source_attempt_limit: int
    relationship_attempt_limit: int
    total_attempt_limit: int
    session_id: str
    _run_lock_descriptor: int = field(repr=False, compare=False)

    @classmethod
    def start(
        cls,
        authorization_path: Path,
        authorization_sha256: str,
        *,
        repository_root: Path,
        stage: str,
        manifest_path: Path,
        manifest_sha256: str,
        evaluation_id: str,
        run_id: str,
        source_attempt_limit: int,
        relationship_attempt_limit: int,
        total_attempt_limit: int,
        resume_reason: Literal["quota", "timeout", "interruption"] | None = None,
    ) -> CodexCampaignGuard:
        root = repository_root.expanduser().resolve()
        authorization, ledger_path = _authorization(
            authorization_path, authorization_sha256, root
        )
        expected = {
            "stage": stage,
            "manifest_sha256": manifest_sha256,
            "evaluation_id": evaluation_id,
            "run_id": run_id,
            "source_attempt_limit": source_attempt_limit,
            "relationship_attempt_limit": relationship_attempt_limit,
            "total_attempt_limit": total_attempt_limit,
        }
        if any(authorization.get(key) != value for key, value in expected.items()):
            raise ValueError("campaign arguments do not match the authorization")
        manifest = _private_file(manifest_path, root, label="campaign manifest")
        if _sha256(manifest) != manifest_sha256:
            raise ValueError("campaign manifest SHA-256 mismatch")
        code_commit = _clean_commit(root)
        if code_commit != authorization["code_commit"]:
            raise ValueError("authorization code_commit does not match clean Git HEAD")
        binding = _binding(authorization, authorization_sha256)
        run_lock_descriptor = _run_lock(ledger_path)
        descriptor = -1
        session_id = uuid.uuid4().hex
        try:
            descriptor = _open_regular(ledger_path, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            rows = _read_rows(descriptor)
            state, reason, previous_session, _, _, _, _ = _audit(rows, binding)
            if state is None:
                if resume_reason is not None:
                    raise CodexCampaignStateError(
                        "fresh campaign run cannot specify a resume reason"
                    )
            elif state == "running":
                if resume_reason != "interruption":
                    raise CodexCampaignStateError(
                        "abandoned campaign run requires interruption resume"
                    )
                _append(
                    descriptor,
                    {
                        **binding,
                        "record": "run_finished",
                        "session_id": previous_session,
                        "state": "paused",
                        "reason": "interruption",
                        "finished_at": _now(),
                    },
                )
            elif state != "paused" or resume_reason != reason:
                raise CodexCampaignStateError(
                    "campaign resume requires its matching typed pause"
                )
            _append(
                descriptor,
                {
                    **binding,
                    "record": "run_started",
                    "session_id": session_id,
                    "resume_reason": resume_reason or "",
                    "started_at": _now(),
                },
            )
        except Exception:
            fcntl.flock(run_lock_descriptor, fcntl.LOCK_UN)
            os.close(run_lock_descriptor)
            raise
        finally:
            if descriptor >= 0:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        return cls(
            authorization_sha256=authorization_sha256,
            ledger_path=ledger_path,
            code_commit=code_commit,
            manifest_sha256=manifest_sha256,
            evaluation_id=evaluation_id,
            run_id=run_id,
            stage=stage,
            source_attempt_limit=source_attempt_limit,
            relationship_attempt_limit=relationship_attempt_limit,
            total_attempt_limit=total_attempt_limit,
            session_id=session_id,
            _run_lock_descriptor=run_lock_descriptor,
        )

    @property
    def _binding(self) -> dict[str, Any]:
        return {
            "authorization_sha256": self.authorization_sha256,
            "code_commit": self.code_commit,
            "manifest_sha256": self.manifest_sha256,
            "evaluation_id": self.evaluation_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "source_attempt_limit": self.source_attempt_limit,
            "relationship_attempt_limit": self.relationship_attempt_limit,
            "total_attempt_limit": self.total_attempt_limit,
        }

    @contextmanager
    def activate(self) -> Iterator[CodexCampaignGuard]:
        token = _ACTIVE_GUARD.set(self)
        try:
            yield self
        finally:
            _ACTIVE_GUARD.reset(token)

    @contextmanager
    def job(self, job_id: str) -> Iterator[None]:
        if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
            raise ValueError("Codex campaign job_id is invalid")
        guard_token = _ACTIVE_GUARD.set(self)
        job_token = _ACTIVE_JOB.set((self.run_id, job_id))
        try:
            yield
        finally:
            _ACTIVE_JOB.reset(job_token)
            _ACTIVE_GUARD.reset(guard_token)

    def reserve(self, contract_id: str, job_id: str | None = None) -> str:
        if not _SAFE_ID.fullmatch(contract_id):
            raise ValueError("Codex campaign contract_id is invalid")
        if job_id is None or not _SAFE_ID.fullmatch(job_id):
            raise CodexCampaignStateError(
                "Codex campaign attempts require a stable logical job ID"
            )
        role = _contract_role(contract_id)
        descriptor = _open_regular(self.ledger_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            (
                state,
                _,
                session_id,
                session_jobs,
                job_contracts,
                job_attempts,
                counts,
            ) = _audit(_read_rows(descriptor), self._binding)
            if state != "running" or session_id != self.session_id:
                raise CodexCampaignStateError("Codex campaign run is not active")
            if job_id in session_jobs:
                raise CodexCampaignStateError(
                    "Codex campaign attempt job is already reserved in this session"
                )
            if job_id in job_contracts and job_contracts[job_id] != contract_id:
                raise CodexCampaignStateError(
                    "Codex campaign attempt job changed contracts across resume"
                )
            if role == "source" and counts[role] >= self.source_attempt_limit:
                raise CodexCampaignCeilingExceeded(
                    "source attempt ceiling would be exceeded"
                )
            if (
                role == "relationship"
                and counts[role] >= self.relationship_attempt_limit
            ):
                raise CodexCampaignCeilingExceeded(
                    "relationship attempt ceiling would be exceeded"
                )
            if sum(counts.values()) >= self.total_attempt_limit:
                raise CodexCampaignCeilingExceeded(
                    "total attempt ceiling would be exceeded"
                )
            counts[role] += 1
            _append(
                descriptor,
                {
                    **self._binding,
                    "record": "reserved",
                    "session_id": self.session_id,
                    "job_id": job_id,
                    "contract_id": contract_id,
                    "role": role,
                    "job_attempt_number": job_attempts.get(job_id, 0) + 1,
                    "role_attempt_number": counts[role],
                    "total_attempt_number": sum(counts.values()),
                    "reserved_at": _now(),
                },
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        return job_id

    def finish(
        self,
        state: Literal["paused", "failed", "passed"],
        *,
        reason: str = "",
    ) -> None:
        if state == "paused" and reason not in PAUSE_REASONS:
            raise ValueError("paused campaign runs require a typed pause reason")
        if state == "passed" and reason:
            raise ValueError("passed campaign runs cannot carry a reason")
        if state == "failed" and not _SAFE_ID.fullmatch(reason):
            raise ValueError("failed campaign runs require a coarse reason")
        descriptor = _open_regular(self.ledger_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current, _, session_id, _, _, _, _ = _audit(
                _read_rows(descriptor), self._binding
            )
            if current != "running" or session_id != self.session_id:
                raise CodexCampaignStateError("Codex campaign run is not active")
            _append(
                descriptor,
                {
                    **self._binding,
                    "record": "run_finished",
                    "session_id": self.session_id,
                    "state": state,
                    "reason": reason,
                    "finished_at": _now(),
                },
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        fcntl.flock(self._run_lock_descriptor, fcntl.LOCK_UN)
        os.close(self._run_lock_descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--source-attempt-limit", type=int, required=True)
    parser.add_argument("--relationship-attempt-limit", type=int, required=True)
    args = parser.parse_args()
    result = initialize_codex_campaign(
        args.manifest,
        args.authorization,
        args.ledger,
        repository_root=args.repository_root,
        authorization_id=args.authorization_id,
        evaluation_id=args.evaluation_id,
        run_id=args.run_id,
        stage=args.stage,
        source_attempt_limit=args.source_attempt_limit,
        relationship_attempt_limit=args.relationship_attempt_limit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
