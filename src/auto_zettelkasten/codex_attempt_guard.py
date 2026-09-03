from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Literal, Mapping


TOTAL_ATTEMPT_LIMIT = 134
STAGE_ATTEMPT_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "luna_source_calibration": 70,
        "terra_relationship_calibration": 38,
        "final_head_contract_smoke": 12,
        "final_four_pdf_public_path": 14,
    }
)
PAUSE_REASONS = frozenset({"quota", "timeout", "interruption"})

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LEDGER_LIMIT = 8_000_000
_CODEX_SUBSCRIPTION_RUN_LOCK_PATH = (
    Path(tempfile.gettempdir())
    / f"auto-zettelkasten-codex-subscription-{os.getuid()}.lock"
)


class CodexAttemptGuardError(RuntimeError):
    pass


class CodexAttemptCeilingExceeded(CodexAttemptGuardError):
    pass


class CodexAttemptStateError(CodexAttemptGuardError):
    pass


class CodexAttemptDeny:
    """Captured sentinel used by exact-replay gates."""


_DENY_ATTEMPTS = CodexAttemptDeny()
_ACTIVE_GUARD: ContextVar[CodexAttemptGuard | CodexAttemptDeny | None] = ContextVar(
    "auto_zettelkasten_codex_attempt_guard", default=None
)
_ACTIVE_JOB: ContextVar[tuple[str, str] | None] = ContextVar(
    "auto_zettelkasten_codex_attempt_job", default=None
)


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


@contextmanager
def codex_subscription_run_lock(provider: str) -> Iterator[None]:
    """Allow one local Auto-Zettelkasten Codex run per user."""

    if provider.casefold() != "codex":
        yield
        return
    # ponytail: one host-local run lock; add account-wide coordination only if
    # cross-host Auto-Zettelkasten concurrency becomes a measured requirement.
    descriptor = -1
    try:
        descriptor = os.open(
            _CODEX_SUBSCRIPTION_RUN_LOCK_PATH,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise CodexAttemptStateError("Codex subscription run lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodexAttemptStateError(
                "another Auto-Zettelkasten Codex subscription run is already active"
            ) from exc
    except CodexAttemptStateError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise CodexAttemptStateError(
            "Codex subscription run lock is unavailable"
        ) from exc

    try:
        yield
    finally:
        try:
            if descriptor >= 0:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


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


def _clean_commit(repository_root: Path) -> str:
    root = repository_root.expanduser().resolve()
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    commit = head.stdout.strip()
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("unable to resolve the live-gate code commit")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if status.returncode or status.stdout.strip():
        raise ValueError("live Codex gates require a clean code commit")
    return commit


def _carried_attempts(
    authorization: Mapping[str, Any],
) -> tuple[int, dict[str, int] | None]:
    has_total = "carried_attempts" in authorization
    has_stages = "carried_stage_attempts" in authorization
    if has_total != has_stages:
        raise ValueError("authorization carried attempt fields must appear together")
    if not has_total:
        return 0, None

    total = authorization["carried_attempts"]
    stages = authorization["carried_stage_attempts"]
    if type(total) is not int or total < 0:
        raise ValueError("authorization carried_attempts must be a nonnegative integer")
    if not isinstance(stages, Mapping) or set(stages) != set(STAGE_ATTEMPT_LIMITS):
        raise ValueError(
            "authorization carried_stage_attempts must contain the exact stage keys"
        )
    counts = dict(stages)
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError(
            "authorization carried stage attempts must be nonnegative integers"
        )
    if sum(counts.values()) != total:
        raise ValueError("authorization carried attempt counts must sum to the total")
    if total > TOTAL_ATTEMPT_LIMIT:
        raise ValueError("authorization carried attempts exceed the total ceiling")
    if any(
        counts[stage] > limit for stage, limit in STAGE_ATTEMPT_LIMITS.items()
    ):
        raise ValueError("authorization carried attempts exceed a stage ceiling")
    return total, counts


def _authorization_header(
    authorization_sha256: str,
    carried_attempts: int,
    carried_stage_attempts: Mapping[str, int] | None,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "authorization_sha256": authorization_sha256,
        "record": "authorization",
        "stage_attempt_limits": dict(STAGE_ATTEMPT_LIMITS),
        "total_attempt_limit": TOTAL_ATTEMPT_LIMIT,
    }
    if carried_stage_attempts is not None:
        header["carried_attempts"] = carried_attempts
        header["carried_stage_attempts"] = dict(carried_stage_attempts)
    return header


def _authorization(
    path: Path,
    expected_sha256: str,
    repository_root: Path,
) -> tuple[dict[str, Any], Path, Path, int, dict[str, int] | None]:
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("authorization_sha256 must be a lowercase SHA-256 digest")
    root = repository_root.expanduser().resolve()
    authorization_path = _private_file(path, root, label="authorization")
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
    if authorization.get("total_attempt_limit") != TOTAL_ATTEMPT_LIMIT:
        raise ValueError("authorization total attempt limit mismatch")
    if authorization.get("stage_attempt_limits") != dict(STAGE_ATTEMPT_LIMITS):
        raise ValueError("authorization stage attempt limits mismatch")
    carried_attempts, carried_stage_attempts = _carried_attempts(authorization)
    authorization_id = str(authorization.get("authorization_id") or "")
    if not _SAFE_ID.fullmatch(authorization_id):
        raise ValueError("authorization_id is invalid")
    ledger_value = authorization.get("ledger")
    if not isinstance(ledger_value, str) or not ledger_value:
        raise ValueError("authorization ledger must be an absolute canonical path")
    ledger_input = Path(ledger_value)
    ledger_path = ledger_input.resolve(strict=False)
    if not ledger_input.is_absolute() or str(ledger_path) != ledger_value:
        raise ValueError("authorization ledger must be an absolute canonical path")
    if _inside(ledger_path, root) or _git_root(ledger_path) is not None:
        raise ValueError("authorization ledger must be outside Git repositories")
    if not ledger_path.parent.is_dir():
        raise ValueError("authorization ledger parent does not exist")
    return (
        authorization,
        authorization_path,
        ledger_path,
        carried_attempts,
        carried_stage_attempts,
    )


def _open_ledger(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("attempt ledger must be a regular file")
    return descriptor


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run_lock(path: Path) -> int:
    lock_path = path.with_name(f"{path.name}.run.lock")
    descriptor = _open_ledger(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise CodexAttemptStateError("Codex live-gate stage is already active") from exc
    return descriptor


def _read_rows(descriptor: int) -> list[dict[str, Any]]:
    size = os.fstat(descriptor).st_size
    if size > _LEDGER_LIMIT:
        raise ValueError("attempt ledger exceeds its byte ceiling")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = os.read(descriptor, _LEDGER_LIMIT + 1).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("attempt ledger contains invalid JSONL") from exc
        if not isinstance(row, Mapping):
            raise ValueError("attempt ledger rows must be JSON objects")
        rows.append(dict(row))
    return rows


def _append(descriptor: int, row: Mapping[str, Any]) -> None:
    payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_END)
    os.write(descriptor, payload)
    os.fsync(descriptor)


def _audit(
    rows: list[dict[str, Any]],
    authorization_sha256: str,
    carried_attempts: int = 0,
    carried_stage_attempts: Mapping[str, int] | None = None,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, tuple[str, str]],
    set[tuple[str, str]],
    dict[str, int],
]:
    carry_authorization: dict[str, Any] = {}
    if carried_stage_attempts is not None or carried_attempts:
        carry_authorization = {
            "carried_attempts": carried_attempts,
            "carried_stage_attempts": carried_stage_attempts,
        }
    carried_attempts, carried_stage_attempts = _carried_attempts(
        carry_authorization
    )
    if not rows or rows[0] != _authorization_header(
        authorization_sha256, carried_attempts, carried_stage_attempts
    ):
        raise ValueError("attempt ledger authorization header mismatch")
    states: dict[str, str] = {}
    reasons: dict[str, str] = {}
    bindings: dict[str, tuple[str, str]] = {}
    run_ids: dict[str, str] = {}
    jobs: set[tuple[str, str]] = set()
    counts = (
        dict(carried_stage_attempts)
        if carried_stage_attempts is not None
        else {stage: 0 for stage in STAGE_ATTEMPT_LIMITS}
    )
    for row in rows[1:]:
        record = row.get("record")
        stage = str(row.get("stage") or "")
        if stage not in STAGE_ATTEMPT_LIMITS:
            raise ValueError("attempt ledger contains an invalid stage")
        if record == "stage_started":
            run_id = str(row.get("run_id") or "")
            manifest_sha256 = str(row.get("manifest_sha256") or "")
            code_commit = str(row.get("code_commit") or "")
            resume_reason = str(row.get("resume_reason") or "")
            if not _SAFE_ID.fullmatch(run_id):
                raise ValueError("attempt ledger contains an invalid run ID")
            binding = (manifest_sha256, code_commit)
            previous = states.get(stage)
            if previous is None:
                if resume_reason:
                    raise ValueError("fresh stage cannot carry a resume reason")
                bindings[stage] = binding
            elif previous != "paused" or resume_reason != reasons.get(stage):
                raise ValueError("attempt ledger contains an invalid stage resume")
            elif binding != bindings[stage]:
                raise ValueError("attempt ledger stage binding changed on resume")
            states[stage] = "running"
            reasons[stage] = ""
            run_ids[stage] = run_id
        elif record == "stage_finished":
            state = str(row.get("state") or "")
            reason = str(row.get("reason") or "")
            if states.get(stage) != "running" or row.get("run_id") != run_ids.get(
                stage
            ):
                raise ValueError("attempt ledger contains an invalid stage finish")
            if state not in {"paused", "failed", "passed"}:
                raise ValueError("attempt ledger contains an invalid stage state")
            if state == "paused" and reason not in PAUSE_REASONS:
                raise ValueError("attempt ledger contains an invalid pause reason")
            if state != "paused" and (not reason if state == "failed" else bool(reason)):
                raise ValueError("attempt ledger contains an invalid finish reason")
            states[stage] = state
            reasons[stage] = reason
        elif record == "reserved":
            job_id = str(row.get("job_id") or "")
            key = (stage, job_id)
            if (
                states.get(stage) != "running"
                or row.get("run_id") != run_ids.get(stage)
                or (row.get("manifest_sha256"), row.get("code_commit"))
                != bindings.get(stage)
                or not _SAFE_ID.fullmatch(job_id)
                or key in jobs
            ):
                raise ValueError("attempt ledger contains an invalid reservation")
            jobs.add(key)
            counts[stage] += 1
        else:
            raise ValueError("attempt ledger contains an unknown record")
    if sum(counts.values()) > TOTAL_ATTEMPT_LIMIT or any(
        counts[stage] > limit for stage, limit in STAGE_ATTEMPT_LIMITS.items()
    ):
        raise ValueError("attempt ledger exceeds the frozen authorization")
    return states, reasons, bindings, jobs, counts


def initialize_codex_attempt_ledger(
    authorization_path: Path,
    authorization_sha256: str,
    *,
    repository_root: Path,
) -> Path:
    _, _, ledger_path, carried_attempts, carried_stage_attempts = _authorization(
        authorization_path, authorization_sha256, repository_root
    )
    descriptor = _open_ledger(
        ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
    )
    try:
        _append(
            descriptor,
            _authorization_header(
                authorization_sha256,
                carried_attempts,
                carried_stage_attempts,
            ),
        )
    finally:
        os.close(descriptor)
    _fsync_parent(ledger_path)
    return ledger_path


@dataclass(frozen=True, slots=True)
class CodexAttemptGuard:
    authorization_sha256: str
    ledger_path: Path
    stage: str
    run_id: str
    manifest_sha256: str
    code_commit: str
    _carried_attempts: int = field(repr=False, compare=False)
    _carried_stage_attempts: Mapping[str, int] | None = field(
        repr=False, compare=False
    )
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
        resume_reason: Literal["quota", "timeout", "interruption"] | None = None,
    ) -> CodexAttemptGuard:
        if stage not in STAGE_ATTEMPT_LIMITS:
            raise ValueError("unknown Codex live-gate stage")
        if not _SHA256.fullmatch(manifest_sha256):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
        root = repository_root.expanduser().resolve()
        (
            _,
            _,
            ledger_path,
            carried_attempts,
            carried_stage_attempts,
        ) = _authorization(
            authorization_path, authorization_sha256, root
        )
        manifest = _private_file(manifest_path, root, label="stage manifest")
        if _sha256(manifest) != manifest_sha256:
            raise ValueError("stage manifest SHA-256 mismatch")
        code_commit = _clean_commit(root)
        run_id = uuid.uuid4().hex
        run_lock_descriptor = _run_lock(ledger_path)
        descriptor = -1
        try:
            descriptor = _open_ledger(ledger_path, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            rows = _read_rows(descriptor)
            states, reasons, bindings, _, _ = _audit(
                rows,
                authorization_sha256,
                carried_attempts,
                carried_stage_attempts,
            )
            unfinished = next(
                (
                    other
                    for other, state in states.items()
                    if other != stage and state in {"running", "paused", "failed"}
                ),
                None,
            )
            if unfinished is not None:
                raise CodexAttemptStateError(
                    f"Codex live-gate stage {unfinished} is unfinished"
                )
            previous = states.get(stage)
            if previous is None:
                if resume_reason is not None:
                    raise CodexAttemptStateError(
                        "fresh stage cannot specify a resume reason"
                    )
            elif previous == "running":
                if resume_reason != "interruption":
                    raise CodexAttemptStateError(
                        "abandoned stage resume requires interruption"
                    )
                if bindings[stage] != (manifest_sha256, code_commit):
                    raise CodexAttemptStateError(
                        "stage manifest or code commit changed before resume"
                    )
                previous_run_id = next(
                    str(row.get("run_id") or "")
                    for row in reversed(rows)
                    if row.get("record") == "stage_started"
                    and row.get("stage") == stage
                )
                _append(
                    descriptor,
                    {
                        "record": "stage_finished",
                        "stage": stage,
                        "run_id": previous_run_id,
                        "state": "paused",
                        "reason": "interruption",
                        "finished_at": _now(),
                    },
                )
            elif (
                previous != "paused"
                or resume_reason is None
                or resume_reason != reasons.get(stage)
            ):
                raise CodexAttemptStateError(
                    "stage resume requires its matching typed pause"
                )
            elif bindings[stage] != (manifest_sha256, code_commit):
                raise CodexAttemptStateError(
                    "stage manifest or code commit changed before resume"
                )
            _append(
                descriptor,
                {
                    "record": "stage_started",
                    "stage": stage,
                    "run_id": run_id,
                    "manifest_sha256": manifest_sha256,
                    "code_commit": code_commit,
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
            stage=stage,
            run_id=run_id,
            manifest_sha256=manifest_sha256,
            code_commit=code_commit,
            _carried_attempts=carried_attempts,
            _carried_stage_attempts=(
                MappingProxyType(carried_stage_attempts)
                if carried_stage_attempts is not None
                else None
            ),
            _run_lock_descriptor=run_lock_descriptor,
        )

    @property
    def carried_stage_attempt_count(self) -> int:
        return int((self._carried_stage_attempts or {}).get(self.stage, 0))

    @contextmanager
    def activate(self) -> Iterator[CodexAttemptGuard]:
        token = _ACTIVE_GUARD.set(self)
        try:
            yield self
        finally:
            _ACTIVE_GUARD.reset(token)

    @contextmanager
    def job(self, job_id: str) -> Iterator[None]:
        if not _SAFE_ID.fullmatch(job_id):
            raise ValueError("Codex attempt job_id is invalid")
        guard_token = _ACTIVE_GUARD.set(self)
        job_token = _ACTIVE_JOB.set((self.run_id, job_id))
        try:
            yield
        finally:
            _ACTIVE_JOB.reset(job_token)
            _ACTIVE_GUARD.reset(guard_token)

    def reserve(self, contract_id: str, job_id: str | None = None) -> str:
        if not _SAFE_ID.fullmatch(contract_id):
            raise ValueError("Codex attempt contract_id is invalid")
        descriptor = _open_ledger(self.ledger_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            rows = _read_rows(descriptor)
            states, _, bindings, jobs, counts = _audit(
                rows,
                self.authorization_sha256,
                self._carried_attempts,
                self._carried_stage_attempts,
            )
            if states.get(self.stage) != "running":
                raise CodexAttemptStateError("Codex live-gate stage is not running")
            if bindings.get(self.stage) != (
                self.manifest_sha256,
                self.code_commit,
            ):
                raise CodexAttemptStateError("Codex live-gate binding changed")
            if counts[self.stage] >= STAGE_ATTEMPT_LIMITS[self.stage]:
                raise CodexAttemptCeilingExceeded(
                    f"{self.stage} attempt ceiling would be exceeded"
                )
            if sum(counts.values()) >= TOTAL_ATTEMPT_LIMIT:
                raise CodexAttemptCeilingExceeded(
                    "cumulative Codex attempt ceiling would be exceeded"
                )
            if job_id is None:
                raise CodexAttemptStateError(
                    "Codex attempts require a stable logical job ID"
                )
            if not _SAFE_ID.fullmatch(job_id) or (self.stage, job_id) in jobs:
                raise CodexAttemptStateError("Codex attempt job is already reserved")
            _append(
                descriptor,
                {
                    "record": "reserved",
                    "stage": self.stage,
                    "run_id": self.run_id,
                    "job_id": job_id,
                    "contract_id": contract_id,
                    "manifest_sha256": self.manifest_sha256,
                    "code_commit": self.code_commit,
                    "attempt_number": sum(counts.values()) + 1,
                    "stage_attempt_number": counts[self.stage] + 1,
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
            raise ValueError("paused stages require quota, timeout, or interruption")
        if state == "passed" and reason:
            raise ValueError("passed stages cannot carry a failure reason")
        if state == "failed" and not _SAFE_ID.fullmatch(reason):
            raise ValueError("failed stages require a coarse failure reason")
        descriptor = _open_ledger(self.ledger_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            rows = _read_rows(descriptor)
            states, _, _, _, _ = _audit(
                rows,
                self.authorization_sha256,
                self._carried_attempts,
                self._carried_stage_attempts,
            )
            if states.get(self.stage) != "running":
                raise CodexAttemptStateError("Codex live-gate stage is not running")
            active_run = next(
                (
                    str(row.get("run_id") or "")
                    for row in reversed(rows)
                    if row.get("record") == "stage_started"
                    and row.get("stage") == self.stage
                ),
                "",
            )
            if active_run != self.run_id:
                raise CodexAttemptStateError("Codex live-gate run is not active")
            _append(
                descriptor,
                {
                    "record": "stage_finished",
                    "stage": self.stage,
                    "run_id": self.run_id,
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


@contextmanager
def deny_codex_attempts() -> Iterator[None]:
    token = _ACTIVE_GUARD.set(_DENY_ATTEMPTS)
    try:
        yield
    finally:
        _ACTIVE_GUARD.reset(token)


def current_codex_attempt_guard() -> CodexAttemptGuard | CodexAttemptDeny | None:
    return _ACTIVE_GUARD.get()


def reserve_codex_attempt(
    guard: CodexAttemptGuard | CodexAttemptDeny | None,
    *,
    contract_id: str,
    job_id: str | None = None,
) -> str | None:
    selected = guard or _ACTIVE_GUARD.get()
    if selected is None:
        return None
    if isinstance(selected, CodexAttemptDeny):
        raise CodexAttemptStateError("Codex provider calls are forbidden")
    active_job = _ACTIVE_JOB.get()
    if active_job is not None and active_job[0] != selected.run_id:
        raise CodexAttemptStateError("Codex attempt job belongs to another stage run")
    return selected.reserve(
        contract_id,
        active_job[1] if active_job is not None else job_id,
    )
