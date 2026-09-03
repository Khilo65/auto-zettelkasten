#!/usr/bin/env python3
"""Run the private, hash-locked v0.30 four-PDF Codex gate."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

import auto_zettelkasten
from auto_zettelkasten.api import resume_map, run_map
from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
from auto_zettelkasten.files import (
    now_iso,
    read_yaml,
    safe_filename,
    sha256_file,
    sha256_text,
    write_yaml,
)
from auto_zettelkasten.models import (
    ExtractionPolicy,
    LiteratureMappingPolicy,
    MapRequest,
    ProcessingPolicy,
)
from auto_zettelkasten.notes import read_note, source_id_for_item
from auto_zettelkasten.readers import (
    CodexReader,
    ProviderInterrupted,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTimeout,
    codex_contract_identity,
    codex_source_bundle_attachment_identity,
)
from auto_zettelkasten.relationships import stable_hash
from auto_zettelkasten.workspace import assert_compatible
from v030_codex_campaign_guard import CodexCampaignGuard


SOURCE_MODEL = "gpt-5.6-luna"
RELATIONSHIP_MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "medium"
SOURCE_ATTEMPT_LIMIT = 6
RELATIONSHIP_ATTEMPT_LIMIT = 8
TOTAL_ATTEMPT_LIMIT = SOURCE_ATTEMPT_LIMIT + RELATIONSHIP_ATTEMPT_LIMIT
DOCUMENT_ATTEMPT_LIMIT = 2
STAGE_DEADLINE_SECONDS = 8_460
ATTEMPT_GUARD_STAGE = "final_four_pdf_public_path"
CASE_COUNT = 4
IMAGE_ROUTE = "codex_pdf_page_images"
PDF_INPUT_ROUTE = "codex_pdf_input_file"
TEXT_ROUTE = "pypdf_text"
DIRECT_PDF_CLI_VERSION = "0.152.1"
LEGACY_CODEX_CLI_VERSION = "0.145.0"
FOUR_PDF_ROUTE_ORACLE = (
    TEXT_ROUTE,
    PDF_INPUT_ROUTE,
    TEXT_ROUTE,
    PDF_INPUT_ROUTE,
)
_SUPPORTED_CODEX_CLI_VERSIONS = frozenset(
    {LEGACY_CODEX_CLI_VERSION, DIRECT_PDF_CLI_VERSION}
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_ANSWER_STOPWORDS = frozenset(
    "a an and as at be but by for from in into is it its of on or own than that "
    "the their this to".split()
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_ATTEMPT_LEDGER_NAME = ".v030-codex-pdf-attempt-ledger.json"
_ATTEMPT_LOCK_NAME = ".v030-codex-pdf-attempt-ledger.lock"
_RELATIONSHIP_CONTRACTS = {
    "literature_family_plan",
    "relationship_candidate_selection",
    "relationship_adjudication",
    "relationship_shard_selection",
    "bridge_shard_selection",
}
_SOURCE_CONTRACTS = {"source_bundle", "chunk_evidence", "evidence_profile"}
_CLUSTER_CONTRACTS = {
    "cluster_plan",
    "cluster_proposal",
    "cluster_synthesis",
    "gap_adjudication",
}
_PAUSE_REASONS = frozenset({"quota", "timeout", "interruption"})
_PROVIDER_FREE_MODES = frozenset({"replay", "revalidate"})
_REVALIDATION_ONLY_PATHS = frozenset(
    {
        "tools/v030_codex_pdf_eval.py",
        "tests/test_v030_codex_pdf_eval.py",
        "tools/v030_codex_e2e_eval.py",
        "tests/test_v030_codex_e2e_eval.py",
    }
)


class GateSettings:
    """Evaluation-only controls; the four-PDF defaults remain frozen."""

    __slots__ = (
        "kind",
        "stage",
        "case_count",
        "source_attempt_limit",
        "relationship_attempt_limit",
        "total_attempt_limit",
        "document_attempt_limit",
        "stage_deadline_seconds",
        "clusters_enabled",
        "allow_html",
        "allow_metadata_only",
        "require_private_expectations",
        "require_direct_image_route",
        "require_direct_pdf_route",
        "report_directory",
        "attempt_ledger_name",
        "attempt_lock_name",
    )

    def __init__(
        self,
        *,
        kind: str = "four_pdf",
        stage: str = ATTEMPT_GUARD_STAGE,
        case_count: int = CASE_COUNT,
        source_attempt_limit: int = SOURCE_ATTEMPT_LIMIT,
        relationship_attempt_limit: int = RELATIONSHIP_ATTEMPT_LIMIT,
        total_attempt_limit: int = TOTAL_ATTEMPT_LIMIT,
        document_attempt_limit: int = DOCUMENT_ATTEMPT_LIMIT,
        stage_deadline_seconds: int = STAGE_DEADLINE_SECONDS,
        clusters_enabled: bool = False,
        allow_html: bool = False,
        allow_metadata_only: bool = False,
        require_private_expectations: bool = True,
        require_direct_image_route: bool = True,
        require_direct_pdf_route: bool = False,
        report_directory: str = "codex-pdf",
        attempt_ledger_name: str = _ATTEMPT_LEDGER_NAME,
        attempt_lock_name: str = _ATTEMPT_LOCK_NAME,
    ) -> None:
        values = locals()
        for name in self.__slots__:
            setattr(self, name, values[name])
        if kind not in {"four_pdf", "controlled_pdf", "raw_e2e", "graph_e2e"} or not _SAFE_ID.fullmatch(self.stage):
            raise ValueError("gate kind or stage is invalid")
        for name in (
            "case_count",
            "source_attempt_limit",
            "relationship_attempt_limit",
            "total_attempt_limit",
            "document_attempt_limit",
            "stage_deadline_seconds",
        ):
            value = getattr(self, name)
            minimum = 0 if name in {"source_attempt_limit", "relationship_attempt_limit"} else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"gate {name} must be a positive integer")
        if self.total_attempt_limit != (
            self.source_attempt_limit + self.relationship_attempt_limit
        ):
            raise ValueError("gate total_attempt_limit must equal its role limits")
        for name in (
            "clusters_enabled",
            "allow_html",
            "allow_metadata_only",
            "require_private_expectations",
            "require_direct_image_route",
            "require_direct_pdf_route",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"gate {name} must be a boolean")
        for name in ("report_directory", "attempt_ledger_name", "attempt_lock_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or Path(value).name != value:
                raise ValueError(f"gate {name} must be a filename component")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GateSettings) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def manifest_binding(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "kind": self.kind,
            "stage": self.stage,
            "case_count": self.case_count,
            "source_attempt_limit": self.source_attempt_limit,
            "relationship_attempt_limit": self.relationship_attempt_limit,
            "total_attempt_limit": self.total_attempt_limit,
            "document_attempt_limit": self.document_attempt_limit,
            "stage_deadline_seconds": self.stage_deadline_seconds,
            "cluster_generation_enabled": self.clusters_enabled,
        }


FOUR_PDF_GATE = GateSettings(
    require_direct_image_route=False,
    require_direct_pdf_route=True,
)
CONTROLLED_PDF_GATE = GateSettings(
    kind="controlled_pdf",
    stage="controlled_real_pdf_smoke",
    case_count=1,
    source_attempt_limit=1,
    relationship_attempt_limit=0,
    total_attempt_limit=1,
    document_attempt_limit=1,
    clusters_enabled=False,
    require_direct_image_route=False,
    require_direct_pdf_route=True,
    report_directory="codex-controlled-pdf",
    attempt_ledger_name=".v030-codex-controlled-pdf-attempt-ledger.json",
    attempt_lock_name=".v030-codex-controlled-pdf-attempt-ledger.lock",
)


class _ReplayCodexReader(CodexReader):
    """Codex-compatible replay boundary that cannot start a provider process."""

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        del system_prompt, user_prompt, output_tokens, deadline_seconds
        raise ProviderInterrupted("provider calls are disabled during exact replay")


class _ControlledPdfReader(CodexReader):
    """Exercise raw-PDF transport without changing normal PDF routing."""

    __slots__ = (
        "_controlled_custody_root",
        "_controlled_path",
        "_controlled_sha256",
        "_controlled_size",
        "_controlled_source_id",
        "_controlled_zotero_key",
        "source_question",
    )

    def __init__(
        self,
        model: str,
        *,
        controlled_workspace: Path,
        controlled_case: Mapping[str, Any],
        controlled_question: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self._controlled_custody_root = (
            controlled_workspace / "01_custody" / "files"
        ).resolve()
        self._controlled_path = Path(controlled_case["path"]).resolve()
        self._controlled_sha256 = str(controlled_case["sha256"])
        self._controlled_size = self._controlled_path.stat().st_size
        self._controlled_source_id = source_id_for_item(controlled_case["parent"])
        self._controlled_zotero_key = str(controlled_case["parent"]["key"])
        self.source_question = controlled_question
        if not _inside(self._controlled_path, self._controlled_custody_root):
            raise ProviderIsolationFailure(
                "controlled PDF is outside the verified custody directory"
            )

    def _controlled_attachment(self, metadata: Mapping[str, Any]) -> Path:
        context = metadata.get("_source_context")
        source_file = (
            str(context.get("source_file") or "")
            if isinstance(context, Mapping)
            else ""
        )
        source_path = Path(source_file) if source_file else None
        path = source_path.resolve() if source_path is not None else None
        if (
            not isinstance(context, Mapping)
            or source_path is None
            or not source_path.is_absolute()
            or source_path.is_symlink()
            or path is None
            or path.parent != self._controlled_custody_root
            or not path.is_file()
            or path.suffix.casefold() != ".pdf"
            or path.stat().st_size != self._controlled_size
            or context.get("custody_sha256") != self._controlled_sha256
            or context.get("media_type") != "application/pdf"
            or context.get("route") != TEXT_ROUTE
            or context.get("source_scope") != "full_document"
            or context.get("source_id") != self._controlled_source_id
            or context.get("zotero_key") != self._controlled_zotero_key
            or sha256_file(path) != self._controlled_sha256
        ):
            raise ProviderIsolationFailure(
                "controlled PDF custody metadata does not match the manifest"
            )
        return path

    def _require_pdf_capability(self) -> None:
        status = self.pdf_input_file_status()
        if (
            status.get("version") != DIRECT_PDF_CLI_VERSION
            or status.get("helper_version") != DIRECT_PDF_CLI_VERSION
            or status.get("helper_manifest_valid") is not True
            or status.get("pdf_input_file_capability") is not True
            or not _direct_pdf_helper_identity_valid(
                status.get("_helper_manifest_identity")
            )
        ):
            raise ProviderIsolationFailure(
                "controlled PDF gate requires the verified Codex 0.152.1 helper"
            )

    def should_read_source_bundle_directly(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> bool:
        del text, question
        self._controlled_attachment(metadata)
        self._require_pdf_capability()
        return super().should_read_source_bundle_directly(
            "", metadata, self.source_question
        )

    def read_source_bundle(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        attachment_paths: Sequence[Path | str] = (),
    ) -> Mapping[str, Any]:
        del text, question
        if attachment_paths:
            raise ProviderIsolationFailure(
                "controlled PDF gate received an unexpected attachment"
            )
        path = self._controlled_attachment(metadata)
        self._require_pdf_capability()
        return super().read_source_bundle(
            "", metadata, self.source_question, attachment_paths=(path,)
        )


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _verify_runtime_import_root() -> None:
    expected = (_REPOSITORY_ROOT / "src" / "auto_zettelkasten").resolve()
    module_file = getattr(auto_zettelkasten, "__file__", None)
    actual = Path(module_file).resolve().parent if module_file else None
    if actual != expected:
        raise RuntimeError(
            "evaluation runner imported auto_zettelkasten outside this "
            "repository's src directory"
        )


def _expectation(value: Any, *, label: str) -> dict[str, list[str]]:
    if isinstance(value, list):
        payload = {"all_of": value, "any_of": []}
    elif isinstance(value, Mapping):
        unknown = set(value) - {"all_of", "any_of"}
        if unknown:
            raise ValueError(f"{label} contains unsupported fields")
        payload = {
            "all_of": value.get("all_of", []),
            "any_of": value.get("any_of", []),
        }
    else:
        raise ValueError(f"{label} must be a list or all_of/any_of mapping")
    normalized: dict[str, list[str]] = {}
    for key in ("all_of", "any_of"):
        values = payload[key]
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ValueError(f"{label} {key} must contain non-empty strings")
        normalized[key] = list(dict.fromkeys(item.strip() for item in values))
    if not normalized["all_of"] and not normalized["any_of"]:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _private(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if _git_root(resolved) is not None or _inside(resolved, _REPOSITORY_ROOT):
        raise ValueError(f"{label} must be outside Git repositories")
    return resolved


def _repository_state() -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=_REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=_REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to verify the release worktree") from exc
    return head, bool(status.strip())


def _verify_repository(
    code_commit: str,
    probe: Callable[[], tuple[str, bool]],
) -> None:
    head, dirty = probe()
    if head != code_commit:
        raise ValueError("manifest code_commit does not match git HEAD")
    if dirty:
        raise ValueError("live gate requires a clean release worktree")


def _verify_revalidation_repository(code_commit: str) -> str:
    head, dirty = _repository_state()
    if dirty:
        raise ValueError("revalidation requires a clean release worktree")
    if head == code_commit:
        return head
    try:
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", code_commit, head),
            cwd=_REPOSITORY_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        changed = subprocess.run(
            ("git", "diff", "--name-only", f"{code_commit}..{head}", "--"),
            cwd=_REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to verify the revalidation worktree") from exc
    if ancestor.returncode != 0 or not changed or any(
        path not in _REVALIDATION_ONLY_PATHS for path in changed
    ):
        raise ValueError("revalidation permits evaluation-only changes")
    return head


def _ledger_identity(
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    settings: GateSettings = FOUR_PDF_GATE,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "manifest_sha256": manifest_sha256,
        "evaluation_id": str(manifest["evaluation_id"]),
        "run_id": str(manifest["run_id"]),
        "code_commit": str(manifest["code_commit"]),
        "source_reserved": settings.source_attempt_limit,
        "relationship_reserved": settings.relationship_attempt_limit,
        "total_reserved": settings.total_attempt_limit,
    }


@contextmanager
def _locked_attempt_ledger(
    root: Path, settings: GateSettings = FOUR_PDF_GATE
) -> Iterator[Path]:
    lock_path = root / settings.attempt_lock_name
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield root / settings.attempt_ledger_name
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_attempt_ledger(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("private attempt ledger is unreadable") from exc
    return _mapping(payload, label="private attempt ledger")


def _write_attempt_ledger(path: Path, payload: Mapping[str, Any]) -> None:
    data = (
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_attempt_ledger(
    ledger: Mapping[str, Any], identity: Mapping[str, Any]
) -> None:
    if any(ledger.get(key) != value for key, value in identity.items()):
        raise ValueError("private attempt ledger does not match the locked manifest")


def _count_payload(source_count: int, relationship_count: int) -> dict[str, int]:
    return {
        "source_attempt_count": source_count,
        "relationship_attempt_count": relationship_count,
        "total_attempt_count": source_count + relationship_count,
    }


def _reported_reservation_state(mode: str, fallback: str) -> str:
    if mode == "revalidate":
        return "failed_preserved"
    if mode == "replay":
        return "accepted"
    return fallback


def _count_ceiling_errors(
    source_count: int,
    relationship_count: int,
    settings: GateSettings = FOUR_PDF_GATE,
) -> list[str]:
    errors: list[str] = []
    if source_count > settings.source_attempt_limit:
        errors.append("source_attempt_ceiling_exceeded")
    if relationship_count > settings.relationship_attempt_limit:
        errors.append("relationship_attempt_ceiling_exceeded")
    if source_count + relationship_count > settings.total_attempt_limit:
        errors.append("total_attempt_ceiling_exceeded")
    return errors


def _assert_run_reservation_available(
    root: Path, settings: GateSettings = FOUR_PDF_GATE
) -> None:
    with _locked_attempt_ledger(root, settings) as path:
        if _read_attempt_ledger(path) is not None:
            raise ValueError("the fixed 14-attempt reservation is already consumed")


def _begin_attempt_reservation(
    root: Path,
    identity: Mapping[str, Any],
    *,
    mode: str,
    source_count: int,
    relationship_count: int,
    resume_reason: str | None = None,
    settings: GateSettings = FOUR_PDF_GATE,
) -> None:
    ceiling_errors = _count_ceiling_errors(
        source_count, relationship_count, settings
    )
    if ceiling_errors:
        raise ValueError("saved attempts exceed the authorized gate ceiling")
    with _locked_attempt_ledger(root, settings) as path:
        ledger = _read_attempt_ledger(path)
        if mode == "run":
            if resume_reason is not None:
                raise ValueError("new run cannot carry a resume reason")
            if ledger is not None:
                raise ValueError("the fixed 14-attempt reservation is already consumed")
            if source_count or relationship_count:
                raise ValueError("new run has pre-existing provider attempts")
            ledger = {
                **dict(identity),
                **_count_payload(0, 0),
                "state": "running",
                "created_at": now_iso(),
            }
        else:
            if ledger is None:
                raise ValueError("resume requires the private attempt reservation")
            _validate_attempt_ledger(ledger, identity)
            observed = _count_payload(source_count, relationship_count)
            state = ledger.get("state")
            if state == "paused":
                if resume_reason != ledger.get("pause_reason"):
                    raise ValueError("resume requires its matching typed pause")
                if any(ledger.get(key) != value for key, value in observed.items()):
                    raise ValueError(
                        "resume attempt counts disagree with the private ledger"
                    )
            elif state == "running":
                if resume_reason != "interruption":
                    raise ValueError(
                        "abandoned reservation resume requires interruption"
                    )
                if any(
                    observed[key] < ledger.get(key, -1)
                    for key in (
                        "source_attempt_count",
                        "relationship_attempt_count",
                        "total_attempt_count",
                    )
                ):
                    raise ValueError("resume attempt counts precede the private ledger")
            else:
                raise ValueError("resume requires a paused attempt reservation")
            ledger = {
                **ledger,
                **observed,
                "state": "running",
                "pause_reason": "",
                "resume_reason": resume_reason,
                "resumed_at": now_iso(),
            }
        _write_attempt_ledger(path, ledger)


def _resume_reservation_reason(
    root: Path,
    identity: Mapping[str, Any],
    *,
    source_count: int,
    relationship_count: int,
    settings: GateSettings = FOUR_PDF_GATE,
) -> str:
    ceiling_errors = _count_ceiling_errors(
        source_count, relationship_count, settings
    )
    if ceiling_errors:
        raise ValueError("saved attempts exceed the authorized gate ceiling")
    with _locked_attempt_ledger(root, settings) as path:
        ledger = _read_attempt_ledger(path)
        if ledger is None:
            raise ValueError("resume requires the private attempt reservation")
        _validate_attempt_ledger(ledger, identity)
        observed = _count_payload(source_count, relationship_count)
        state = ledger.get("state")
        if state == "paused":
            reason = str(ledger.get("pause_reason") or "")
            if reason not in _PAUSE_REASONS:
                raise ValueError("paused reservation lacks a typed pause reason")
            if any(ledger.get(key) != value for key, value in observed.items()):
                raise ValueError(
                    "resume attempt counts disagree with the private ledger"
                )
            return reason
        if state == "running":
            if any(
                observed[key] < ledger.get(key, -1)
                for key in (
                    "source_attempt_count",
                    "relationship_attempt_count",
                    "total_attempt_count",
                )
            ):
                raise ValueError("resume attempt counts precede the private ledger")
            return "interruption"
        raise ValueError("resume requires a paused attempt reservation")


def _finish_attempt_reservation(
    root: Path,
    identity: Mapping[str, Any],
    *,
    state: str,
    source_count: int | None = None,
    relationship_count: int | None = None,
    reason: str = "",
    settings: GateSettings = FOUR_PDF_GATE,
) -> None:
    if state not in {"accepted", "paused", "failed"}:
        raise ValueError("invalid attempt reservation state")
    if state == "paused" and reason not in _PAUSE_REASONS:
        raise ValueError("paused reservations require a typed pause reason")
    if state != "paused" and reason:
        raise ValueError("only paused reservations may carry a pause reason")
    with _locked_attempt_ledger(root, settings) as path:
        ledger = _read_attempt_ledger(path)
        if ledger is None:
            raise ValueError("private attempt reservation is missing")
        _validate_attempt_ledger(ledger, identity)
        if ledger.get("state") != "running":
            raise ValueError("private attempt reservation is not running")
        if source_count is not None and relationship_count is not None:
            ledger.update(_count_payload(source_count, relationship_count))
        ledger.update(
            state=state,
            pause_reason=reason if state == "paused" else "",
            updated_at=now_iso(),
        )
        _write_attempt_ledger(path, ledger)


def _verify_accepted_reservation(
    root: Path,
    identity: Mapping[str, Any],
    *,
    source_count: int,
    relationship_count: int,
    settings: GateSettings = FOUR_PDF_GATE,
) -> None:
    if _count_ceiling_errors(source_count, relationship_count, settings):
        raise ValueError("accepted attempts exceed the authorized gate ceiling")
    with _locked_attempt_ledger(root, settings) as path:
        ledger = _read_attempt_ledger(path)
        if ledger is None:
            raise ValueError("replay requires the private attempt reservation")
        _validate_attempt_ledger(ledger, identity)
        if ledger.get("state") != "accepted":
            raise ValueError("replay requires an accepted attempt reservation")
        observed = _count_payload(source_count, relationship_count)
        if any(ledger.get(key) != value for key, value in observed.items()):
            raise ValueError("replay attempt counts disagree with the private ledger")


def _verify_failed_reservation(
    root: Path,
    identity: Mapping[str, Any],
    *,
    source_count: int,
    relationship_count: int,
    settings: GateSettings = FOUR_PDF_GATE,
) -> None:
    if _count_ceiling_errors(source_count, relationship_count, settings):
        raise ValueError("failed attempts exceed the authorized gate ceiling")
    with _locked_attempt_ledger(root, settings) as path:
        ledger = _read_attempt_ledger(path)
        if ledger is None:
            raise ValueError("revalidation requires the private attempt reservation")
        _validate_attempt_ledger(ledger, identity)
        if ledger.get("state") != "failed":
            raise ValueError("revalidation requires a failed attempt reservation")
        observed = _count_payload(source_count, relationship_count)
        if any(ledger.get(key) != value for key, value in observed.items()):
            raise ValueError(
                "revalidation attempt counts disagree with the private ledger"
            )


@contextmanager
def _stage_deadline(settings: GateSettings = FOUR_PDF_GATE) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("the live gate must execute on the main thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise ProviderTimeout("four-PDF stage deadline reached")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, settings.stage_deadline_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _case_path(
    root: Path,
    row: Mapping[str, Any],
    settings: GateSettings = FOUR_PDF_GATE,
) -> Path | None:
    value = row.get("file") if settings.allow_html else None
    if value is None:
        value = row.get("pdf")
    if value is None and isinstance(row.get("destination_name"), str):
        value = f"01_custody/files/{row['destination_name']}"
    if value is None and settings.allow_metadata_only:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("each substantive case requires a relative file path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("case file paths must be relative")
    path = (root / relative).resolve()
    if not _inside(path, root) or not path.is_file():
        raise ValueError("case file is missing or outside the manifest root")
    return path


def _expected_route(
    row: Mapping[str, Any],
    settings: GateSettings = FOUR_PDF_GATE,
) -> tuple[str, list[int]]:
    expected = (
        _mapping(row["expected"], label="case expected")
        if row.get("expected") is not None
        else {}
    )
    route = str(
        expected.get("content_route")
        or row.get("expected_route")
        or row.get("audited_route")
        or ""
    )
    if not settings.allow_html and route not in {
        TEXT_ROUTE,
        IMAGE_ROUTE,
        PDF_INPUT_ROUTE,
    }:
        raise ValueError(
            "case expected route must be pypdf_text, codex_pdf_input_file, "
            "or codex_pdf_page_images"
        )
    if settings.allow_html and (
        not route or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", route)
    ):
        raise ValueError("case expected content route is invalid")
    pages_value = expected.get("selected_pages", row.get("expected_selected_pages", []))
    if not isinstance(pages_value, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in pages_value
    ):
        raise ValueError("case expected selected_pages must be positive integers")
    pages = list(dict.fromkeys(pages_value))
    if pages != sorted(pages) or len(pages) != len(pages_value):
        raise ValueError("case expected selected_pages must be ordered and unique")
    if route == IMAGE_ROUTE and not (1 <= len(pages) <= 16):
        raise ValueError("image cases require one to sixteen selected pages")
    if route != IMAGE_ROUTE and pages:
        raise ValueError("non-image cases cannot select image pages")
    return route, pages


def _zotero_rows(
    row: Mapping[str, Any],
    *,
    case_id: str,
    media_type: str = "application/pdf",
    attachment_required: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    parent = _mapping(row.get("zotero_parent"), label=f"{case_id} zotero_parent")
    if not attachment_required:
        if row.get("zotero_attachment") not in (None, {}):
            raise ValueError(f"{case_id} metadata-only case cannot have an attachment")
        parent_data = _mapping(parent.get("data", parent), label=f"{case_id} parent data")
        parent_key = str(parent.get("key") or parent_data.get("key") or "")
        if not _SAFE_ID.fullmatch(parent_key):
            raise ValueError(f"{case_id} Zotero parent key is invalid")
        if str(parent_data.get("itemType") or "") == "attachment":
            raise ValueError(f"{case_id} parent item cannot be an attachment")
        return (
            {**parent, "key": parent_key, "data": {**parent_data, "key": parent_key}},
            None,
        )
    attachment = _mapping(
        row.get("zotero_attachment"), label=f"{case_id} zotero_attachment"
    )
    parent_data = _mapping(parent.get("data", parent), label=f"{case_id} parent data")
    attachment_data = _mapping(
        attachment.get("data", attachment), label=f"{case_id} attachment data"
    )
    parent_key = str(parent.get("key") or parent_data.get("key") or "")
    attachment_key = str(attachment.get("key") or attachment_data.get("key") or "")
    if not _SAFE_ID.fullmatch(parent_key) or not _SAFE_ID.fullmatch(attachment_key):
        raise ValueError(f"{case_id} Zotero keys are invalid")
    if parent_key == attachment_key:
        raise ValueError(f"{case_id} parent and attachment keys must differ")
    if str(parent_data.get("itemType") or "") == "attachment":
        raise ValueError(f"{case_id} parent item cannot be an attachment")
    if str(attachment_data.get("itemType") or "") != "attachment":
        raise ValueError(f"{case_id} attachment itemType must be attachment")
    if str(attachment_data.get("parentItem") or "") != parent_key:
        raise ValueError(f"{case_id} attachment parentItem mismatch")
    if str(attachment_data.get("contentType") or "") != media_type:
        raise ValueError(f"{case_id} attachment contentType mismatch")
    for payload in (attachment, attachment_data):
        for key in ("path", "local_path", "localPath", "source_file", "sourceFile"):
            value = payload.get(key)
            if isinstance(value, str) and Path(value).expanduser().is_absolute():
                raise ValueError(f"{case_id} attachment cannot expose a local path")
    parent = {**parent, "key": parent_key, "data": {**parent_data, "key": parent_key}}
    attachment = {
        **attachment,
        "key": attachment_key,
        "data": {**attachment_data, "key": attachment_key},
    }
    return parent, attachment


def _validated_manifest(
    manifest_path: Path,
    manifest_sha256: str,
    settings: GateSettings = FOUR_PDF_GATE,
) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    if not _SHA256.fullmatch(manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    manifest_path = _private(manifest_path, label="manifest")
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    if sha256_file(manifest_path) != manifest_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    try:
        manifest = _mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")), label="manifest"
        )
    except json.JSONDecodeError as exc:
        raise ValueError("manifest must be valid JSON") from exc
    if str(manifest.get("schema_version") or "") != "1":
        raise ValueError("manifest schema_version must be 1")
    gate_binding = manifest.get("gate")
    if settings != FOUR_PDF_GATE:
        if gate_binding != settings.manifest_binding():
            raise ValueError("manifest gate controls do not match the active gate")
    elif gate_binding is not None and gate_binding != settings.manifest_binding():
        raise ValueError("four-PDF manifest gate controls are invalid")
    code_commit = str(manifest.get("code_commit") or "")
    if not _GIT_COMMIT.fullmatch(code_commit):
        raise ValueError("manifest code_commit must be a full lowercase git commit")
    evaluation_id = str(manifest.get("evaluation_id") or "")
    run_id = str(manifest.get("run_id") or "")
    if not _SAFE_ID.fullmatch(evaluation_id):
        raise ValueError("evaluation_id must contain only safe filename characters")
    if not _SAFE_ID.fullmatch(run_id):
        raise ValueError("run_id must contain only safe filename characters")
    root = manifest_path.parent.resolve()
    workspace_value = manifest.get("workspace")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise ValueError("manifest workspace is required")
    workspace = Path(workspace_value).expanduser()
    if not workspace.is_absolute():
        raise ValueError("manifest workspace must be an absolute private path")
    workspace = _private(workspace, label="workspace")
    if workspace != root:
        raise ValueError("manifest workspace must be its private manifest directory")
    question = manifest.get("question")
    if question is not None and not isinstance(question, str):
        raise ValueError("manifest question must be a string or null")
    if settings.kind == "controlled_pdf" and not str(question or "").strip():
        raise ValueError("controlled PDF manifest question is required")
    pdf_fallback = manifest.get("pdf_fallback", "none")
    if pdf_fallback not in {"none", "images", "ocr"}:
        raise ValueError("manifest pdf_fallback must be none, images, or ocr")
    rows = manifest.get("cases")
    if not isinstance(rows, list) or len(rows) != settings.case_count:
        raise ValueError(
            f"manifest must contain exactly {settings.case_count} cases"
        )

    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    seen_paths: set[Path] = set()
    for index, value in enumerate(rows):
        row = _mapping(value, label=f"case {index}")
        case_id = str(row.get("case_id") or row.get("case") or "")
        if not _SAFE_ID.fullmatch(case_id) or case_id in seen_ids:
            raise ValueError("case IDs must be safe and unique")
        expected = (
            _mapping(row["expected"], label=f"{case_id} expected")
            if row.get("expected") is not None
            else {}
        )
        terminal_status = str(
            expected.get(
                "terminal_status", row.get("expected_terminal_status", "validated_note")
            )
        )
        if terminal_status not in {"validated_note", "limited_note"}:
            raise ValueError(f"{case_id} expected terminal status is invalid")
        metadata_only = terminal_status == "limited_note"
        if metadata_only and not settings.allow_metadata_only:
            raise ValueError(f"{case_id} metadata-only cases are not allowed")
        media_type = str(
            row.get("media_type")
            or ("application/json" if metadata_only else "application/pdf")
        )
        allowed_media = (
            {"application/pdf", "text/html"}
            if settings.allow_html
            else {"application/pdf"}
        )
        if not metadata_only and media_type not in allowed_media:
            raise ValueError(f"{case_id} media_type is unsupported")
        if metadata_only and media_type != "application/json":
            raise ValueError(f"{case_id} metadata-only media_type must be application/json")
        digest = str(row.get("sha256") or "")
        path = _case_path(root, row, settings)
        if metadata_only:
            if path is not None or digest:
                raise ValueError(f"{case_id} metadata-only case cannot bind a file")
            route, pages = "zotero_metadata", []
        else:
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"{case_id} sha256 is invalid")
            assert path is not None
            if path in seen_paths:
                raise ValueError("case file paths must be unique")
            if sha256_file(path) != digest:
                raise ValueError(f"{case_id} file SHA-256 mismatch")
            route, pages = _expected_route(row, settings)
        expectations = (
            {
                "audited_facts": _expectation(
                    expected.get("audited_facts", row.get("audited_facts")),
                    label=f"{case_id} audited_facts",
                ),
                "audited_locators": _expectation(
                    expected.get(
                        "audited_locators",
                        row.get("audited_locators", row.get("locators")),
                    ),
                    label=f"{case_id} audited_locators",
                ),
                "expected_answers": _expectation(
                    expected.get("expected_answers", row.get("expected_answers")),
                    label=f"{case_id} expected_answers",
                ),
            }
            if settings.require_private_expectations
            else {}
        )
        parent, attachment = _zotero_rows(
            row,
            case_id=case_id,
            media_type=media_type,
            attachment_required=not metadata_only,
        )
        keys = {str(parent["key"])}
        if attachment is not None:
            keys.add(str(attachment["key"]))
        if seen_keys.intersection(keys):
            raise ValueError("Zotero keys must be unique across cases")
        fulltext = row.get("zotero_fulltext")
        if (
            fulltext is not None
            and not isinstance(fulltext, (bool, Mapping))
        ):
            raise ValueError(f"{case_id} zotero_fulltext is invalid")
        if isinstance(fulltext, Mapping) and str(
            fulltext.get("contentType") or media_type
        ) != media_type:
            raise ValueError(f"{case_id} zotero_fulltext contentType mismatch")
        if route in {IMAGE_ROUTE, PDF_INPUT_ROUTE} and media_type != "application/pdf":
            raise ValueError(f"{case_id} attachment route requires application/pdf")
        cluster_expectation = str(row.get("cluster_expectation") or "")
        if cluster_expectation not in {"", "related_candidate", "control"}:
            raise ValueError(f"{case_id} cluster_expectation is invalid")
        cases.append(
            {
                "case_id": case_id,
                "path": path,
                "sha256": digest,
                "media_type": media_type,
                "expected_terminal_status": terminal_status,
                "expected_route": route,
                "expected_selected_pages": pages,
                "expectations": expectations,
                "parent": parent,
                "attachment": attachment,
                "zotero_fulltext": dict(fulltext) if isinstance(fulltext, Mapping) else fulltext,
                "cluster_expectation": cluster_expectation,
            }
        )
        seen_ids.add(case_id)
        seen_keys.update(keys)
        if path is not None:
            seen_paths.add(path)
    if settings.kind == "four_pdf" and tuple(
        str(row["expected_route"]) for row in cases
    ) != FOUR_PDF_ROUTE_ORACLE:
        raise ValueError(
            "four-PDF routes must be pypdf_text, codex_pdf_input_file, "
            "pypdf_text, codex_pdf_input_file in manifest order"
        )
    if (
        settings.kind == "controlled_pdf"
        and cases[0]["expected_route"] != PDF_INPUT_ROUTE
    ):
        raise ValueError("controlled PDF gate requires codex_pdf_input_file")
    if settings.kind == "raw_e2e" and settings.case_count == 8:
        role_counts = Counter(row["cluster_expectation"] for row in cases)
        if role_counts != Counter({"related_candidate": 4, "control": 4}):
            raise ValueError(
                "strategic8 requires four private related candidates and four controls"
            )
    collections = manifest.get("collections", [])
    if not isinstance(collections, list) or any(
        not isinstance(row, Mapping) for row in collections
    ):
        raise ValueError("manifest collections must be a list of mappings")
    manifest["collections"] = [dict(row) for row in collections]
    return manifest, cases, workspace


class ManifestZoteroClient:
    """Read-only Zotero boundary backed only by verified private manifest rows."""

    def __init__(
        self,
        cases: Sequence[Mapping[str, Any]],
        collections: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._cases = [dict(row) for row in cases]
        self._collections = [dict(row) for row in collections]
        self._by_parent = {str(row["parent"]["key"]): row for row in self._cases}
        self._by_attachment = {
            str(row["attachment"]["key"]): row for row in self._cases
            if isinstance(row.get("attachment"), Mapping)
        }

    def status(self) -> Mapping[str, Any]:
        return {
            "status": "available",
            "read_only": True,
            "base_url": "private-manifest",
        }

    def collections(self) -> list[dict[str, Any]]:
        return deepcopy(self._collections)

    def selected_collection(self) -> Mapping[str, Any]:
        return {"scope": "library", "key": "", "name": "Private PDF canary"}

    def inventory(
        self, scope: str, collection_key: str | None = None
    ) -> list[dict[str, Any]]:
        if scope != "library" or collection_key not in {None, ""}:
            raise ValueError("the four-PDF gate supports only its private library")
        return [deepcopy(row["parent"]) for row in self._cases]

    def children(self, item_key: str) -> list[dict[str, Any]]:
        row = self._by_parent.get(item_key)
        return (
            [deepcopy(row["attachment"])]
            if row is not None and isinstance(row.get("attachment"), Mapping)
            else []
        )

    def fulltext(self, item_key: str) -> Mapping[str, Any] | None:
        row = self._by_attachment.get(item_key)
        if row is None:
            return None
        value = row.get("zotero_fulltext")
        if value is True:
            path = Path(row["path"])
            if sha256_file(path) != str(row["sha256"]):
                raise ValueError("private source changed after manifest validation")
            return {
                "content": path.read_text(encoding="utf-8"),
                "contentType": str(row["media_type"]),
            }
        return deepcopy(value) if isinstance(value, Mapping) else None

    def file(self, item_key: str) -> tuple[bytes, str] | None:
        row = self._by_attachment.get(item_key)
        if row is None:
            return None
        path = Path(row["path"])
        if sha256_file(path) != str(row["sha256"]):
            raise ValueError("private source changed after manifest validation")
        return path.read_bytes(), str(row["media_type"])


def _request(
    manifest: Mapping[str, Any],
    workspace: Path,
    settings: GateSettings = FOUR_PDF_GATE,
) -> MapRequest:
    return MapRequest(
        workspace,
        scope="library",
        question=(str(manifest["question"]) if manifest.get("question") else None),
        provider="codex",
        model=SOURCE_MODEL,
        literature_model=RELATIONSHIP_MODEL,
        reasoning_effort=REASONING_EFFORT,
        allow_cloud=True,
        parallel=settings.case_count,
        provider_concurrency="auto",
        retry_terminal_failures=False,
        extraction_policy=ExtractionPolicy(
            ocr="auto", pdf_fallback=str(manifest.get("pdf_fallback", "none"))
        ),
        processing=ProcessingPolicy(
            max_calls_per_document_run=settings.document_attempt_limit,
            request_deadline_seconds=600.0,
        ),
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=settings.clusters_enabled,
            max_profile_calls=settings.source_attempt_limit,
            max_synthesis_calls=settings.relationship_attempt_limit,
            profile_workers=settings.case_count,
        ),
    )


def _report_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        return _mapping(method(), label="run report")
    raise ValueError("run_map returned an invalid report")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            rows.append(_mapping(json.loads(line), label=f"{path.name}:{line_number}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} contains invalid JSONL") from exc
    return rows


def _attempts(workspace: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run_root = workspace / "11_state" / "runs" / run_id / "literature"
    source_path = run_root / "profiles" / "provider_usage.yml"
    source = _mapping(read_yaml(source_path, {}) or {}, label="source provider usage")
    source_rows = [
        dict(row)
        for row in source.get("attempts", []) or []
        if isinstance(row, Mapping)
    ]
    source_events = _read_jsonl(source_path.with_name("provider_events.jsonl"))
    reservations = [row for row in source_events if row.get("event_type") == "reserved"]
    synthesis_path = run_root / "synthesis" / "provider_usage.yml"
    synthesis = _mapping(
        read_yaml(synthesis_path, {}) or {}, label="relationship provider usage"
    )
    synthesis_rows = [
        dict(row)
        for row in synthesis.get("attempts", []) or []
        if isinstance(row, Mapping)
    ]
    return (
        {
            "count": len(source_rows),
            "reported_count": int(source.get("provider_call_count", 0) or 0),
            "reservation_count": len(reservations),
            "rows": source_rows,
            "path": source_path,
            "events_path": source_path.with_name("provider_events.jsonl"),
        },
        {
            "count": len(synthesis_rows),
            "reported_count": int(synthesis.get("provider_call_count", 0) or 0),
            "rows": synthesis_rows,
            "path": synthesis_path,
        },
    )


def _latest_attempt_rows(
    attempts: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    latest: dict[tuple[str, str, str], tuple[int, int, Mapping[str, Any]]] = {}
    for order, row in enumerate(attempts):
        identity = tuple(
            str(row.get(field) or "")
            for field in ("stage", "key", "fingerprint")
        )
        if not all(identity):
            identity = (f"__unkeyed__:{order}", "", "")
        raw_attempt = row.get("attempt")
        attempt = (
            raw_attempt
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 0
        )
        candidate = (attempt, order, row)
        if identity not in latest or candidate[:2] > latest[identity][:2]:
            latest[identity] = candidate
    return [entry[2] for entry in sorted(latest.values(), key=lambda entry: entry[1])]


def _direct_pdf_transport_errors(
    cases: Sequence[Mapping[str, Any]], attempts: Sequence[Mapping[str, Any]]
) -> list[str]:
    expected = Counter(
        str(row["sha256"])
        for row in cases
        if row.get("expected_route") == PDF_INPUT_ROUTE
    )
    if not expected:
        return []
    observed: Counter[str] = Counter()
    invalid = False
    for row in _latest_attempt_rows(attempts):
        if row.get("status") != "completed":
            continue
        completion = row.get("provider_completion")
        if not isinstance(completion, Mapping):
            continue
        transport = completion.get("attachment_transport")
        direct_transport = isinstance(transport, Mapping) and (
            transport.get("adapter_protocol") == "codex-app-server-jsonrpc-v2"
            or "helper_manifest" in transport
        )
        if not direct_transport:
            continue
        hashes = completion.get("attachment_hashes")
        if (
            completion.get("codex_cli_version") != DIRECT_PDF_CLI_VERSION
            or not _direct_pdf_transport_valid(transport)
            or completion.get("attachment_count") != 1
            or not isinstance(hashes, list)
            or len(hashes) != 1
            or not _SHA256.fullmatch(str(hashes[0] or ""))
        ):
            invalid = True
            continue
        observed[str(hashes[0])] += 1
    errors = []
    if invalid:
        errors.append("direct_pdf_transport_invalid")
    if observed != expected:
        errors.append("direct_pdf_transport_mismatch")
    return errors


def _attempt_pause_reason(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "")
    failure_class = str(row.get("failure_class") or "")
    if status in {"failed", "interrupted"} and failure_class in _PAUSE_REASONS:
        return failure_class
    if (
        status in {"failed", "interrupted"}
        and failure_class == "transport"
        and str(row.get("error_type") or "") == "InterruptedProviderAttempt"
        and str(row.get("transport_kind") or "") == "interrupted_process"
        and row.get("retry_on_resume") is True
    ):
        return "interruption"
    return ""


def _completion_error(
    row: Mapping[str, Any],
    *,
    source: bool,
    settings: GateSettings = FOUR_PDF_GATE,
) -> str:
    completion = row.get("provider_completion")
    if not isinstance(completion, Mapping):
        return "provider_completion_missing"
    expected_model = SOURCE_MODEL if source else RELATIONSHIP_MODEL
    if (
        completion.get("provider") != "codex"
        or completion.get("model") != expected_model
    ):
        return "provider_completion_identity_mismatch"
    if completion.get("reasoning_effort") != REASONING_EFFORT:
        return "provider_completion_effort_mismatch"
    if completion.get("finish_reason") != "turn.completed":
        return "provider_completion_finish_mismatch"
    contract = str(completion.get("contract_id") or "")
    allowed_source_contracts = (
        _SOURCE_CONTRACTS if settings.kind == "raw_e2e" else {"source_bundle"}
    )
    if source and contract not in allowed_source_contracts:
        return "source_contract_mismatch"
    allowed_relationship_contracts = _RELATIONSHIP_CONTRACTS | (
        _CLUSTER_CONTRACTS if settings.clusters_enabled else set()
    )
    if not source and contract not in allowed_relationship_contracts:
        return "relationship_contract_mismatch"
    cli_version = str(completion.get("codex_cli_version") or "")
    if cli_version not in _SUPPORTED_CODEX_CLI_VERSIONS:
        return "provider_completion_cli_mismatch"
    expected_identity = codex_contract_identity(
        contract, expected_model, REASONING_EFFORT, cli_version
    )
    if any(completion.get(key) != value for key, value in expected_identity.items()):
        return "provider_completion_identity_mismatch"
    maximum = completion.get("max_output_tokens")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum != expected_identity["output_reservation"]
    ):
        return "provider_completion_output_reservation_mismatch"
    usage = completion.get("usage")
    if not isinstance(usage, Mapping):
        return "provider_usage_missing"
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "provider_usage_invalid"
    return ""


def _profile_source_ids(workspace: Path) -> set[str]:
    source_ids: set[str] = set()
    for path in (workspace / "02_source_memory" / "profiles").glob("*.yml"):
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            continue
        profile = payload.get("profile", payload)
        if isinstance(profile, Mapping) and profile.get("source_id"):
            source_ids.add(str(profile["source_id"]))
    return source_ids


def _image_token_estimate(dimensions: Sequence[tuple[int, int]]) -> int:
    return sum(
        ((((width + 31) // 32) * ((height + 31) // 32)) * 6 + 4) // 5
        for width, height in dimensions
    )


def _preflight_valid(
    value: Any,
    *,
    expected_image_tokens: int | None = None,
    image_tokens_in_document_input: bool = False,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    integer_fields = (
        "document_input_tokens",
        "image_tokens",
        "reasoning_reservation_tokens",
        "output_reservation_tokens",
        "uncertainty_tokens",
        "combined_tokens",
        "ceiling_tokens",
    )
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or int(value[key]) < 0
        for key in integer_fields
    ):
        return False
    document_tokens = int(value["document_input_tokens"])
    image_tokens = int(value["image_tokens"])
    uncertainty = int(value["uncertainty_tokens"])
    combined = int(value["combined_tokens"])
    if expected_image_tokens is not None and image_tokens != expected_image_tokens:
        return False
    if image_tokens_in_document_input:
        for key in ("prompt_text_tokens", "pdf_extracted_text_tokens"):
            if (
                isinstance(value.get(key), bool)
                or not isinstance(value.get(key), int)
                or int(value[key]) < 0
            ):
                return False
        if document_tokens != (
            int(value["prompt_text_tokens"])
            + int(value["pdf_extracted_text_tokens"])
            + image_tokens
        ):
            return False
    return (
        int(value["reasoning_reservation_tokens"]) == 32_768
        and int(value["output_reservation_tokens"]) == 32_768
        and uncertainty == max(16_384, (document_tokens + 3) // 4)
        and combined
        == document_tokens
        + (0 if image_tokens_in_document_input else image_tokens)
        + 32_768
        + 32_768
        + uncertainty
        and int(value["ceiling_tokens"]) == 200_000
        and value.get("admitted") is True
        and combined <= 200_000
    )


def _direct_pdf_helper_identity_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    expected = {
        "manifest_version": 1,
        "upstream_tag": "rust-v0.152.1",
        "upstream_commit": "5adb68a49933ae446bf11935662c83dba55a0804",
        "platform": "macos-arm64",
        "license": "Apache-2.0",
        "notice": "NOTICE",
        "input_file_protocol_revision": "input_file-v1",
    }
    return (
        set(value) == {*expected, "patch_sha256", "binary_sha256", "manifest_sha256"}
        and all(value.get(key) == expected_value for key, expected_value in expected.items())
        and all(
            _SHA256.fullmatch(str(value.get(key) or ""))
            for key in ("patch_sha256", "binary_sha256", "manifest_sha256")
        )
    )


def _direct_pdf_attachment_capability_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    helper = value.get("helper_manifest")
    return _direct_pdf_helper_identity_valid(helper) and dict(value) == (
        codex_source_bundle_attachment_identity(DIRECT_PDF_CLI_VERSION, helper)
    )


def _image_attachment_capability_valid(value: Any) -> bool:
    return any(
        value == codex_source_bundle_attachment_identity(version)
        for version in _SUPPORTED_CODEX_CLI_VERSIONS
    )


def _direct_pdf_transport_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    helper = value.get("helper_manifest")
    return _direct_pdf_helper_identity_valid(helper) and dict(value) == {
        **codex_source_bundle_attachment_identity(DIRECT_PDF_CLI_VERSION, helper),
        "adapter_protocol": "codex-app-server-jsonrpc-v2",
    }


def _probe_dimensions(
    probe: Any, selected_pages: Sequence[int]
) -> tuple[list[tuple[int, int]], bool]:
    if not isinstance(probe, Mapping):
        return [], False
    pages = probe.get("pages")
    if not isinstance(pages, list) or not pages:
        return [], False
    by_number: dict[int, tuple[int, int]] = {}
    suspicious_pages: list[int] = []
    for row in pages:
        if not isinstance(row, Mapping):
            return [], False
        number = row.get("page_number")
        width = row.get("width")
        height = row.get("height")
        counts = (
            row.get("embedded_char_count"),
            row.get("embedded_word_count"),
            row.get("resource_count"),
            row.get("xobject_count"),
            row.get("image_count"),
        )
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height < 1
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in counts
            )
            or not _SHA256.fullmatch(str(row.get("embedded_text_sha256") or ""))
            or not isinstance(row.get("resource_types"), list)
            or any(not isinstance(item, str) for item in row["resource_types"])
            or not isinstance(row.get("text_quality"), str)
            or not isinstance(row.get("suspicious"), bool)
            or not isinstance(row.get("visually_consequential"), bool)
            or not isinstance(row.get("render_candidate"), bool)
        ):
            return [], False
        if number in by_number:
            return [], False
        by_number[number] = (width, height)
        if row["suspicious"]:
            suspicious_pages.append(number)
    page_count = probe.get("page_count")
    custody_bytes = probe.get("custody_byte_count")
    selected = list(selected_pages)
    valid = (
        not isinstance(page_count, bool)
        and isinstance(page_count, int)
        and page_count == len(pages)
        and sorted(by_number) == list(range(1, page_count + 1))
        and not isinstance(custody_bytes, bool)
        and isinstance(custody_bytes, int)
        and custody_bytes > 0
        and probe.get("status") in {"succeeded", "partial"}
        and probe.get("render_candidate_pages") == selected
        and probe.get("suspicious_pages") == suspicious_pages
        and all(
            isinstance(row, Mapping) and row.get("render_candidate") is True
            for row in pages
            if row.get("page_number") in selected
        )
        and all(number in by_number for number in selected)
    )
    return [by_number[number] for number in selected if number in by_number], valid


def _direct_probe_dimensions(probe: Any) -> tuple[list[tuple[int, int]], bool]:
    if not isinstance(probe, Mapping):
        return [], False
    render_candidates = probe.get("render_candidate_pages")
    if not isinstance(render_candidates, list):
        return [], False
    _, valid = _probe_dimensions(probe, render_candidates)
    if not valid:
        return [], False
    pages = probe.get("pages")
    assert isinstance(pages, list)
    return [
        (int(row["width"]), int(row["height"]))
        for row in pages
        if isinstance(row, Mapping)
    ], True


def _rendered_dimensions(
    rendered: Any, selected_pages: Sequence[int]
) -> tuple[list[tuple[int, int]], bool]:
    if not isinstance(rendered, list):
        return [], False
    numbers = [row.get("page_number") for row in rendered if isinstance(row, Mapping)]
    if len(numbers) != len(rendered) or numbers != list(selected_pages):
        return [], False
    dimensions: list[tuple[int, int]] = []
    for row in rendered:
        assert isinstance(row, Mapping)
        width = row.get("width")
        height = row.get("height")
        byte_count = row.get("byte_count")
        if (
            row.get("media_type") != "image/png"
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 0 < width <= 2_048
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 0 < height <= 2_048
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 1
            or not _SHA256.fullmatch(str(row.get("sha256") or ""))
            or not str(row.get("renderer") or "")
            or not str(row.get("renderer_version") or "")
            or row.get("render_policy_version") != "1"
        ):
            return [], False
        dimensions.append((width, height))
    return dimensions, True


def _route_errors(
    workspace: Path,
    run_id: str,
    cases: Sequence[Mapping[str, Any]],
    settings: GateSettings = FOUR_PDF_GATE,
) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    direct_image_routes = 0
    direct_pdf_routes = 0
    custody_root = (workspace / "01_custody" / "files").resolve()
    for row in cases:
        case_id = str(row["case_id"])
        parent_key = str(row["parent"]["key"])
        root = (
            workspace
            / "11_state"
            / "runs"
            / run_id
            / "items"
            / safe_filename(parent_key)
        )
        content = read_yaml(root / "frozen_content.yml", {}) or {}
        if not isinstance(content, Mapping):
            errors.append(f"{case_id}:frozen_content_missing")
            continue
        expected_route = str(row["expected_route"])
        if row.get("expected_terminal_status") == "limited_note":
            if (
                str(content.get("content_route") or "") != expected_route
                or str(content.get("source_scope") or "") != "metadata_only"
            ):
                errors.append(f"{case_id}:metadata_route_mismatch")
            results.append(
                {
                    "case_id": case_id,
                    "expected_route": expected_route,
                    "selected_pages": [],
                    "recovery": "not_applicable",
                }
            )
            continue
        source_reference = str(content.get("source_file") or "")
        expected_content_hash = str(row["sha256"])
        source_reference_valid = True
        if expected_route == "zotero_fulltext":
            source_file = Path(row["path"]).resolve()
            fulltext = row.get("zotero_fulltext")
            fulltext_content = (
                fulltext.get("content")
                if isinstance(fulltext, Mapping)
                else source_file.read_text(encoding="utf-8")
                if fulltext is True
                else None
            )
            attachment = row.get("attachment")
            attachment_key = (
                str(attachment.get("key") or "")
                if isinstance(attachment, Mapping)
                else ""
            )
            source_reference_valid = (
                isinstance(fulltext_content, str)
                and source_reference
                == f"zotero://select/library/items/{attachment_key}"
            )
            if isinstance(fulltext_content, str):
                expected_content_hash = sha256_text(fulltext_content)
        else:
            source_file = Path(source_reference).resolve()
        source_file_size = (
            source_file.stat().st_size
            if _inside(source_file, custody_root) and source_file.is_file()
            else -1
        )
        if (
            not _inside(source_file, custody_root)
            or not source_file.is_file()
            or sha256_file(source_file) != str(row["sha256"])
            or not source_reference_valid
            or str(content.get("content_hash") or "") != expected_content_hash
            or str(content.get("media_type") or "") != str(row["media_type"])
        ):
            errors.append(f"{case_id}:custody_binding_mismatch")
        recovery = "not_applicable"
        selected_pages: list[int] = []
        if settings.kind == "controlled_pdf":
            if (
                expected_route != PDF_INPUT_ROUTE
                or content.get("content_route") != TEXT_ROUTE
                or (root / "document_route.yml").exists()
            ):
                errors.append(f"{case_id}:controlled_pdf_acquisition_mismatch")
            else:
                direct_pdf_routes += 1
        elif expected_route == PDF_INPUT_ROUTE:
            route = read_yaml(root / "document_route.yml", {}) or {}
            identity = (
                route.get("identity_payload") if isinstance(route, Mapping) else None
            )
            if (
                not isinstance(identity, Mapping)
                or identity.get("route") != PDF_INPUT_ROUTE
            ):
                errors.append(f"{case_id}:pdf_input_route_missing")
                identity = {}
            if (
                identity.get("route_version") != "1"
                or Path(str(identity.get("custody_file") or "")).resolve()
                != source_file
                or identity.get("custody_sha256") != str(row["sha256"])
                or identity.get("custody_byte_count") != source_file_size
                or identity.get("file_policy")
                != {
                    "media_type": "application/pdf",
                    "maximum_bytes_exclusive": 50_000_000,
                    "detail": "auto",
                }
                or identity.get("model_profile")
                != {
                    "model": SOURCE_MODEL,
                    "reasoning_effort": REASONING_EFFORT,
                    "cli_version": DIRECT_PDF_CLI_VERSION,
                }
                or identity.get("fallback_policy") != "none"
                or not _direct_pdf_attachment_capability_valid(
                    identity.get("attachment_capability")
                )
                or not isinstance(route, Mapping)
                or route.get("identity") != stable_hash(identity)
            ):
                errors.append(f"{case_id}:route_identity_mismatch")
            probe_dimensions, probe_valid = _direct_probe_dimensions(
                identity.get("probe_evidence")
            )
            if not probe_valid:
                errors.append(f"{case_id}:probe_evidence_invalid")
            probe = identity.get("probe_evidence")
            if (
                not isinstance(probe, Mapping)
                or probe.get("custody_byte_count") != source_file_size
            ):
                errors.append(f"{case_id}:probe_custody_evidence_mismatch")
            if not _preflight_valid(
                identity.get("projected_preflight"),
                expected_image_tokens=(
                    _image_token_estimate(probe_dimensions) if probe_valid else None
                ),
                image_tokens_in_document_input=True,
            ):
                errors.append(f"{case_id}:pdf_input_preflight_not_admitted")
            recovery_row = route.get("recovery") if isinstance(route, Mapping) else None
            recovery = (
                str(recovery_row.get("state") or "")
                if isinstance(recovery_row, Mapping)
                else ""
            )
            if recovery != "not_selected":
                errors.append(f"{case_id}:invalid_recovery_state")
            if (
                str(content.get("content_route") or "") != PDF_INPUT_ROUTE
                or route.get("rendered_images") not in (None, [])
                or route.get("actual_preflight") is not None
            ):
                errors.append(f"{case_id}:route_mismatch")
            else:
                direct_pdf_routes += 1
        elif expected_route != IMAGE_ROUTE:
            actual_route = str(content.get("content_route") or "")
            if settings.kind == "raw_e2e":
                if actual_route != expected_route:
                    errors.append(f"{case_id}:route_mismatch")
            elif actual_route != TEXT_ROUTE and actual_route != expected_route:
                errors.append(f"{case_id}:route_mismatch")
        else:
            route = read_yaml(root / "document_route.yml", {}) or {}
            identity = (
                route.get("identity_payload") if isinstance(route, Mapping) else None
            )
            if (
                not isinstance(identity, Mapping)
                or identity.get("route") != IMAGE_ROUTE
            ):
                errors.append(f"{case_id}:image_route_missing")
                identity = {}
            if (
                identity.get("route_version") != "1"
                or Path(str(identity.get("custody_file") or "")).resolve()
                != source_file
                or identity.get("custody_sha256") != str(row["sha256"])
                or not _image_attachment_capability_valid(
                    identity.get("attachment_capability")
                )
                or not isinstance(route, Mapping)
                or route.get("identity") != stable_hash(identity)
            ):
                errors.append(f"{case_id}:route_identity_mismatch")
            if identity.get("render_policy") != {
                "format": "png",
                "maximum_side": 2_048,
                "maximum_pages": 16,
                "enlargement": False,
            }:
                errors.append(f"{case_id}:render_policy_mismatch")
            selected_pages = list(identity.get("selected_pages", []) or [])
            if selected_pages != list(row["expected_selected_pages"]):
                errors.append(f"{case_id}:selected_pages_mismatch")
            probe_dimensions, probe_valid = _probe_dimensions(
                identity.get("probe_evidence"), selected_pages
            )
            if not probe_valid:
                errors.append(f"{case_id}:probe_evidence_invalid")
            probe = identity.get("probe_evidence")
            if (
                not isinstance(probe, Mapping)
                or probe.get("custody_byte_count") != source_file_size
            ):
                errors.append(f"{case_id}:probe_custody_evidence_mismatch")
            if not _preflight_valid(
                identity.get("projected_preflight"),
                expected_image_tokens=(
                    _image_token_estimate(probe_dimensions) if probe_valid else None
                ),
            ):
                errors.append(f"{case_id}:image_preflight_not_admitted")
            recovery_row = route.get("recovery") if isinstance(route, Mapping) else None
            recovery = (
                str(recovery_row.get("state") or "")
                if isinstance(recovery_row, Mapping)
                else ""
            )
            if recovery not in {"not_selected", "completed"}:
                errors.append(f"{case_id}:invalid_recovery_state")
            if recovery == "not_selected":
                direct_image_routes += 1
                if str(content.get("content_route") or "") != IMAGE_ROUTE:
                    errors.append(f"{case_id}:route_mismatch")
            elif not str(content.get("content_route") or "").endswith(
                "_after_codex_image_recovery"
            ):
                errors.append(f"{case_id}:recovery_route_mismatch")
            rendered = (
                route.get("rendered_images") if isinstance(route, Mapping) else None
            )
            rendered_dimensions, rendered_valid = _rendered_dimensions(
                rendered, selected_pages
            )
            if not rendered_valid:
                errors.append(f"{case_id}:rendered_image_evidence_invalid")
            if not _preflight_valid(
                route.get("actual_preflight") if isinstance(route, Mapping) else None,
                expected_image_tokens=(
                    _image_token_estimate(rendered_dimensions)
                    if rendered_valid
                    else None
                ),
            ):
                errors.append(f"{case_id}:actual_image_preflight_invalid")
        results.append(
            {
                "case_id": case_id,
                "expected_route": expected_route,
                **(
                    {"acquisition_route": TEXT_ROUTE}
                    if settings.kind == "controlled_pdf"
                    else {}
                ),
                "selected_pages": selected_pages,
                "recovery": recovery,
            }
        )
    if settings.require_direct_image_route and direct_image_routes < 1:
        errors.append("no_direct_image_route_completed")
    if settings.require_direct_pdf_route and direct_pdf_routes < 1:
        errors.append("no_direct_pdf_route_completed")
    return errors, results


def _relationship_errors(
    workspace: Path,
    source_ids: set[str],
    *,
    require_accepted: bool = False,
) -> tuple[list[str], bool]:
    errors: list[str] = []
    index_root = workspace / "02_source_memory" / "indexes"
    registry_path = index_root / "typed_links.yml"
    registry = read_yaml(registry_path, None)
    if not registry_path.is_file() or not isinstance(registry, Mapping):
        return ["typed_relationship_registry_missing"], False
    compatibility_path = index_root / "typed_note_links.yml"
    compatibility = read_yaml(compatibility_path, None)
    if (
        not compatibility_path.is_file()
        or not isinstance(compatibility, Mapping)
        or compatibility != registry
    ):
        errors.append("relationship_registry_projection_mismatch")
    rows = [
        dict(row)
        for field in ("relations", "links", "pair_decisions")
        for row in registry.get(field, []) or []
        if isinstance(row, Mapping)
        and str(row.get("source_kind") or "source") == "source"
        and str(row.get("target_kind") or "source") == "source"
    ]
    for row in rows:
        endpoints = {
            str(value)
            for value in (row.get("source_id"), row.get("target_source_id"))
            if str(value or "")
        }
        if not endpoints.issubset(source_ids):
            errors.append("relationship_endpoint_outside_gate")
            break
    for row in registry.get("current_pair_decisions", []) or []:
        if not isinstance(row, Mapping):
            continue
        endpoints = {
            str(value) for value in row.get("source_ids", []) or [] if str(value)
        }
        if not endpoints.issubset(source_ids):
            errors.append("relationship_endpoint_outside_gate")
            break
    accepted = [
        dict(row)
        for row in registry.get("relations", []) or []
        if isinstance(row, Mapping)
        and str(row.get("source_kind") or "source") == "source"
        and str(row.get("target_kind") or "source") == "source"
        and row.get("active", True)
        and str(row.get("decision_status") or "") == "accepted"
        and row.get("source_id") in source_ids
        and row.get("target_source_id") in source_ids
    ]
    state_path = index_root / "relationship_selection_state.yml"
    raw_state = read_yaml(state_path, {})
    state = raw_state if isinstance(raw_state, Mapping) else {}
    registry_has_activity = any(
        registry.get(field)
        for field in (
            "relations",
            "links",
            "pair_decisions",
            "current_pair_decisions",
            "events",
            "parked",
        )
    )
    state_required = (
        len(source_ids) > 1 or registry_has_activity or state_path.exists()
    )
    if state_required and (
        not isinstance(raw_state, Mapping)
        or state.get("relationship_stage_complete") is not True
        or state.get("relationship_discovery_status") != "complete"
        or state.get("relationship_discovery_incomplete_jobs")
    ):
        errors.append("relationship_completeness_accounting_failed")
    selected_pairs = {
        tuple(sorted(str(value) for value in row.get("pair", []) or []))
        for row in state.get("selected_candidates", []) or []
        if isinstance(row, Mapping)
        and len(row.get("pair", []) or []) == 2
    }
    current_pairs = {
        tuple(sorted(str(value) for value in row.get("source_ids", []) or []))
        for row in registry.get("current_pair_decisions", []) or []
        if isinstance(row, Mapping)
        and len(row.get("source_ids", []) or []) == 2
        and row.get("active", True)
    }
    accepted_relation_pairs = {
        tuple(sorted((str(row["source_id"]), str(row["target_source_id"]))))
        for row in accepted
    }
    accepted_decision_pairs = {
        tuple(sorted(str(value) for value in row.get("source_ids", []) or []))
        for row in registry.get("current_pair_decisions", []) or []
        if isinstance(row, Mapping)
        and len(row.get("source_ids", []) or []) == 2
        and row.get("active", True)
        and str(row.get("status") or row.get("decision_status") or "")
        == "accepted"
    }
    if accepted_relation_pairs != accepted_decision_pairs:
        errors.append("relationship_registry_projection_mismatch")
    if selected_pairs != current_pairs and selected_pairs:
        errors.append("selected_relationship_pair_coverage_incomplete")
    requires_adjudication = bool(
        rows
        or registry.get("current_pair_decisions")
        or (state.get("selected_candidates") if isinstance(state, Mapping) else [])
    )
    if not accepted:
        if require_accepted:
            errors.append("accepted_relationship_missing")
        return errors, requires_adjudication
    notes: dict[str, Mapping[str, Any]] = {}
    note_ids: dict[str, str] = {}
    for path in (workspace / "02_source_memory" / "notes").glob("*.md"):
        frontmatter = read_note(path)["frontmatter"]
        if frontmatter.get("source_id"):
            source_id = str(frontmatter["source_id"])
            notes[source_id] = frontmatter
            note_ids[source_id] = str(frontmatter.get("note_id") or "")
    def projected(source_id: str, target_id: str) -> bool:
        target_note_id = note_ids.get(target_id, "")
        if not target_note_id:
            return False
        return any(
            isinstance(row, Mapping) and row.get("note_id") == target_note_id
            for row in notes.get(source_id, {}).get("related_notes", []) or []
        )

    if any(
        not projected(str(row["source_id"]), str(row["target_source_id"]))
        or not projected(str(row["target_source_id"]), str(row["source_id"]))
        for row in accepted
    ):
        errors.append("accepted_relationship_not_reciprocally_projected")
    return errors, True


def _answer_matches(text: str, spans: Sequence[str], expected: str) -> bool:
    normalized = _normalized_text(expected)
    if normalized in text:
        return True
    normalized = re.sub(r"\bcomponents?\b", "part", normalized)
    normalized = re.sub(r"\bgains?\b", "gain", normalized)
    terms = {
        term
        for term in re.findall(r"[a-z0-9]+", normalized)
        if term not in _ANSWER_STOPWORDS
    }
    return len(terms) >= 2 and any(
        all(re.search(rf"\b{re.escape(term)}\b", span) for term in terms)
        for span in (
            re.sub(
                r"\bgains?\b",
                "gain",
                re.sub(r"\bcomponents?\b", "part", value),
            )
            for value in spans
        )
    )


def _locator_matches(text: str, expected: str) -> bool:
    if re.search(rf"(?<![a-z0-9]){re.escape(expected)}(?![a-z0-9])", text):
        return True
    numbered = re.fullmatch(r"(page|clause) (\d+)", expected)
    if numbered is None:
        return False
    unit, value = numbered.group(1), int(numbered.group(2))
    unit_pattern = (
        r"(?:pages?\s+|p{1,2}\.\s*)" if unit == "page" else r"clauses?\s+"
    )
    if re.search(rf"\b{unit_pattern}{value}\b", text):
        return True
    return any(
        int(start) <= value <= int(end)
        for start, end in re.findall(
            rf"\b{unit_pattern}(\d+)\s*[-–—]\s*(\d+)\b", text
        )
    )


def _private_expectation_errors(
    cases: Sequence[Mapping[str, Any]],
    note_paths: Mapping[str, Path],
) -> tuple[list[str], int]:
    errors: list[str] = []
    passed = 0
    for row in cases:
        case_id = str(row["case_id"])
        source_id = source_id_for_item(row["parent"])
        note_path = note_paths.get(source_id)
        if note_path is None:
            errors.append(f"{case_id}:private_expectation_note_missing")
            continue
        body = str(read_note(note_path).get("body") or "")
        text = _normalized_text(body)
        answer_spans = [
            _normalized_text(line) for line in body.splitlines() if line.strip()
        ]
        expectations = row.get("expectations")
        if not isinstance(expectations, Mapping):
            errors.append(f"{case_id}:private_expectations_missing")
            continue
        for label in ("audited_facts", "audited_locators", "expected_answers"):
            specification = expectations.get(label)
            if not isinstance(specification, Mapping):
                errors.append(f"{case_id}:{label}_missing")
                continue
            all_of = [
                _normalized_text(str(value))
                for value in specification.get("all_of", []) or []
            ]
            any_of = [
                _normalized_text(str(value))
                for value in specification.get("any_of", []) or []
            ]
            matches = (
                (lambda value: _answer_matches(text, answer_spans, value))
                if label == "expected_answers"
                else (
                    (lambda value: _locator_matches(text, value))
                    if label == "audited_locators"
                    else (lambda value: value in text)
                )
            )
            if any(not matches(value) for value in all_of) or (
                any_of and not any(matches(value) for value in any_of)
            ):
                errors.append(f"{case_id}:{label}_not_matched")
            else:
                passed += 1
    return errors, passed


def _cluster_errors(
    workspace: Path,
    source_ids: set[str],
    report: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    cluster_map = report.get("cluster_map")
    if not isinstance(cluster_map, Mapping):
        return ["cluster_map_missing"]
    clusters = [
        dict(row)
        for row in cluster_map.get("clusters", []) or []
        if isinstance(row, Mapping)
    ]
    if not clusters or int(report.get("cluster_count", 0) or 0) != len(clusters):
        errors.append("cluster_output_missing")
    member_ids = {
        str(source_id)
        for cluster in clusters
        for source_id in cluster.get("source_ids", []) or []
        if str(source_id)
    }
    unclustered_ids = {
        str(row.get("source_id") or "")
        for row in cluster_map.get("unclustered_sources", []) or []
        if isinstance(row, Mapping) and str(row.get("source_id") or "")
    }
    if not member_ids.issubset(source_ids) or not unclustered_ids.issubset(source_ids):
        errors.append("cluster_endpoint_outside_gate")
    if member_ids & unclustered_ids or member_ids | unclustered_ids != source_ids:
        errors.append("cluster_disposition_accounting_failed")
    if any(cluster.get("refresh_pending") is True for cluster in clusters):
        errors.append("cluster_refresh_pending")
    registry = read_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml", {}
    ) or {}
    if not isinstance(registry, Mapping) or registry.get("pending_revisions"):
        errors.append("cluster_registry_incomplete")
    synthesized = int(
        report.get(
            "synthesized_cluster_count",
            cluster_map.get("synthesized_cluster_count", 0),
        )
        or 0
    )
    if synthesized != len(clusters):
        errors.append("cluster_synthesis_incomplete")
    return errors


def _private_cluster_expectation_errors(
    cases: Sequence[Mapping[str, Any]], report: Mapping[str, Any]
) -> list[str]:
    related = {
        source_id_for_item(row["parent"])
        for row in cases
        if row.get("cluster_expectation") == "related_candidate"
    }
    controls = {
        source_id_for_item(row["parent"])
        for row in cases
        if row.get("cluster_expectation") == "control"
    }
    if not related and not controls:
        return []
    if len(related) < 3 or not controls:
        return ["private_cluster_expectations_incomplete"]
    cluster_map = report.get("cluster_map")
    clusters = cluster_map.get("clusters", []) if isinstance(cluster_map, Mapping) else []
    for row in clusters or []:
        if not isinstance(row, Mapping):
            continue
        members = {str(value) for value in row.get("source_ids", []) or []}
        if len(members & related) >= 3 and not members & controls:
            return []
    return ["private_related_cluster_or_control_separation_failed"]


def _acceptance(
    workspace: Path,
    run_id: str,
    cases: Sequence[Mapping[str, Any]],
    report: Mapping[str, Any],
    settings: GateSettings = FOUR_PDF_GATE,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    validated_count = sum(
        row.get("expected_terminal_status") == "validated_note" for row in cases
    )
    limited_count = settings.case_count - validated_count
    if not str(report.get("status") or "").startswith("completed"):
        errors.append("run_not_completed")
    if int(report.get("inventory_count", 0) or 0) != settings.case_count:
        errors.append("inventory_count_mismatch")
    if int(report.get("validated_note_count", 0) or 0) != validated_count:
        errors.append("validated_note_count_mismatch")
    if int(report.get("limited_note_count", 0) or 0) != limited_count:
        errors.append("limited_note_count_mismatch")
    items = [
        dict(row) for row in report.get("items", []) or [] if isinstance(row, Mapping)
    ]
    note_root = (workspace / "02_source_memory" / "notes").resolve()
    note_paths: dict[str, Path] = {}
    item_paths_valid = True
    expected_statuses = {
        source_id_for_item(case["parent"]): str(case["expected_terminal_status"])
        for case in cases
    }
    for row in items:
        path = (workspace / str(row.get("note_path") or "")).resolve()
        source_id = str(row.get("source_id") or "")
        if (
            row.get("terminal_status") != expected_statuses.get(source_id)
            or not source_id
            or not _inside(path, note_root)
            or not path.is_file()
        ):
            item_paths_valid = False
            continue
        note_paths[source_id] = path
    if len(items) != settings.case_count or not item_paths_valid:
        errors.append("validated_note_inventory_mismatch")
    source_ids = {str(row.get("source_id") or "") for row in items} - {""}
    expected_source_ids = {source_id_for_item(row["parent"]) for row in cases}
    if source_ids != expected_source_ids:
        errors.append("source_endpoint_count_mismatch")
    if _profile_source_ids(workspace) != source_ids:
        errors.append("profile_source_set_mismatch")
    if (
        int(report.get("profile_count", 0) or 0) != settings.case_count
        or int(report.get("profile_valid_count", 0) or 0) != validated_count
        or int(report.get("profile_excluded_count", 0) or 0) != limited_count
    ):
        errors.append("profile_validation_count_mismatch")
    expectation_errors, expectation_checks = (
        _private_expectation_errors(cases, note_paths)
        if settings.require_private_expectations
        else ([], 0)
    )
    errors.extend(expectation_errors)
    route_errors, route_results = _route_errors(
        workspace, run_id, cases, settings
    )
    errors.extend(route_errors)

    cluster_map = report.get("cluster_map")
    gap_map = report.get("gap_map")
    eligible_source_ids = {
        source_id_for_item(row["parent"])
        for row in cases
        if row.get("expected_terminal_status") == "validated_note"
    }
    if settings.clusters_enabled:
        errors.extend(_cluster_errors(workspace, eligible_source_ids, report))
        errors.extend(_private_cluster_expectation_errors(cases, report))
    else:
        if (
            not isinstance(cluster_map, Mapping)
            or cluster_map.get("status") != "clusters_preserved_not_updated"
            or cluster_map.get("clusters")
            or int(report.get("cluster_count", 0) or 0) != 0
        ):
            errors.append("cluster_output_not_disabled")
        if (
            not isinstance(gap_map, Mapping)
            or gap_map.get("status") != "clusters_preserved_not_updated"
            or gap_map.get("gap_candidates")
            or int(report.get("mapped_gap_count", 0) or 0) != 0
        ):
            errors.append("gap_output_not_disabled")
        if list((workspace / "03_literature_synthesis" / "clusters").glob("*.md")):
            errors.append("cluster_note_written")
        if list(
            (workspace / "03_literature_synthesis" / "gaps" / "candidates").glob("*.md")
        ):
            errors.append("gap_note_written")
    relationship_errors, requires_relationship_adjudication = _relationship_errors(
        workspace,
        source_ids,
        require_accepted=settings.kind == "raw_e2e",
    )
    errors.extend(relationship_errors)

    source, relationship = _attempts(workspace, run_id)
    if not (
        source["count"]
        == source["reported_count"]
        == source["reservation_count"]
        == int(report.get("source_provider_call_count", 0) or 0)
    ):
        errors.append("source_attempt_ledger_disagreement")
    if not (
        relationship["count"]
        == relationship["reported_count"]
        == int(report.get("synthesis_call_count", 0) or 0)
        == int(report.get("literature_provider_call_count", 0) or 0)
    ):
        errors.append("relationship_attempt_ledger_disagreement")
    if int(report.get("provider_call_count", 0) or 0) != (
        source["count"] + relationship["count"]
    ):
        errors.append("total_attempt_count_disagreement")
    if source["count"] > settings.source_attempt_limit:
        errors.append("source_attempt_ceiling_exceeded")
    if source["count"] < validated_count:
        errors.append("source_attempts_missing")
    if relationship["count"] > settings.relationship_attempt_limit:
        errors.append("relationship_attempt_ceiling_exceeded")
    if source["count"] + relationship["count"] > settings.total_attempt_limit:
        errors.append("total_attempt_ceiling_exceeded")
    source_call_keys = [str(row.get("key") or "") for row in source["rows"]]
    if any(not key for key in source_call_keys) or any(
        count > settings.document_attempt_limit
        for count in Counter(source_call_keys).values()
    ):
        errors.append("document_attempt_ceiling_exceeded")
    recovered_image_route = any(row["recovery"] == "completed" for row in route_results)
    for row in source["rows"]:
        status = str(row.get("status") or "")
        if status in {"failed", "interrupted"} and not _attempt_pause_reason(row):
            if (
                str(row.get("error_type") or "")
                not in {
                    "ProviderUnsupportedAttachment",
                    "ProviderInvalidSourceBundle",
                }
                or str(row.get("failure_class") or "")
                not in {"", "contract", "semantic_contract", "terminal"}
                or not recovered_image_route
            ):
                errors.append("unapproved_source_attempt_failure")
    for row in _latest_attempt_rows(source["rows"]):
        status = str(row.get("status") or "")
        if _attempt_pause_reason(row):
            errors.append("unfinished_source_attempt")
            continue
        if status in {"failed", "interrupted"}:
            continue
        if status != "completed":
            errors.append("unfinished_source_attempt")
            continue
        error = _completion_error(row, source=True, settings=settings)
        if error:
            errors.append(error)
    errors.extend(_direct_pdf_transport_errors(cases, source["rows"]))
    relationship_contracts: set[str] = set()
    for row in relationship["rows"]:
        if str(row.get("status") or "") in {"failed", "interrupted"} and not (
            _attempt_pause_reason(row)
        ):
            errors.append("unfinished_relationship_attempt")
    for row in _latest_attempt_rows(relationship["rows"]):
        if _attempt_pause_reason(row):
            errors.append("unfinished_relationship_attempt")
            continue
        if str(row.get("status") or "") in {"failed", "interrupted"}:
            continue
        if str(row.get("status") or "") != "completed":
            errors.append("unfinished_relationship_attempt")
            continue
        error = _completion_error(row, source=False, settings=settings)
        if error:
            errors.append(error)
        completion = row.get("provider_completion")
        if isinstance(completion, Mapping):
            relationship_contracts.add(str(completion.get("contract_id") or ""))
    required_relationship_contracts = (
        {"relationship_candidate_selection"}
        if settings.relationship_attempt_limit
        else set()
    )
    if requires_relationship_adjudication and settings.relationship_attempt_limit:
        required_relationship_contracts.add("relationship_adjudication")
    if not required_relationship_contracts.issubset(relationship_contracts):
        errors.append("required_relationship_contracts_missing")
    return sorted(set(errors)), {
        "routes": route_results,
        "source_attempt_count": source["count"],
        "relationship_attempt_count": relationship["count"],
        "total_attempt_count": source["count"] + relationship["count"],
        "private_expectation_check_count": expectation_checks,
    }


def _snapshot_digest(snapshot: Mapping[str, tuple[str, int, int]]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _gate_snapshot(
    workspace: Path,
) -> dict[str, tuple[str, int, int]]:
    return {
        str(path.relative_to(workspace)): (
            sha256_file(path),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in workspace.rglob("*")
        if path.is_file()
    }


def _pause_reason(
    report: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]] = ()
) -> str:
    for reason in ("quota", "timeout", "interruption"):
        if any(
            _attempt_pause_reason(row) == reason
            for row in _latest_attempt_rows(attempts)
        ):
            return reason
    if attempts:
        return ""
    text = json.dumps(report, sort_keys=True, default=str).casefold()
    for reason, markers in (
        (
            "quota",
            (
                "provider_quota_exhausted",
                "providerquotaexhausted",
                "quota is paused",
                "usage limit",
            ),
        ),
        ("timeout", ("provider_timeout", "providertimeout", "deadline reached")),
        (
            "interruption",
            ("provider_interrupted", "providerinterrupted", "interrupted"),
        ),
    ):
        if any(marker in text for marker in markers):
            return reason
    return ""


def _write_report(
    workspace: Path,
    evaluation_id: str,
    mode: str,
    report: Mapping[str, Any],
    settings: GateSettings = FOUR_PDF_GATE,
) -> Path:
    root = workspace / "11_state" / "evaluations" / settings.report_directory
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / f"{safe_filename(evaluation_id)}-{mode}.yml"
    write_yaml(path, dict(report))
    path.chmod(0o600)
    return path


def run_gate(
    *,
    mode: str,
    manifest_path: Path,
    manifest_sha256: str,
    authorization_path: Path | None = None,
    authorization_sha256: str = "",
    execute: bool = False,
    map_runner: Callable[..., Any] = run_map,
    repository_probe: Callable[[], tuple[str, bool]] = _repository_state,
    attempt_guard_factory: Callable[..., Any] = CodexCampaignGuard.start,
    settings: GateSettings = FOUR_PDF_GATE,
    acceptance_hook: Callable[
        [Path, str, Sequence[Mapping[str, Any]], Mapping[str, Any]],
        tuple[Sequence[str], Mapping[str, Any]],
    ]
    | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Prepare, execute, resume, or exactly replay the private four-PDF gate."""

    _verify_runtime_import_root()
    if mode not in {"prepare", "run", "resume", "replay", "revalidate"}:
        raise ValueError("mode must be prepare, run, resume, replay, or revalidate")
    manifest, cases, workspace = _validated_manifest(
        manifest_path, manifest_sha256, settings
    )
    assert_compatible(workspace)
    request = _request(manifest, workspace, settings)
    evaluation_id = str(manifest["evaluation_id"])
    run_id = str(manifest["run_id"])
    code_commit = str(manifest["code_commit"])
    ledger_identity = _ledger_identity(manifest, manifest_sha256, settings)
    base = {
        "report_schema_version": "1",
        "evaluation_id": evaluation_id,
        "mode": mode,
        "manifest_sha256": manifest_sha256,
        "code_commit": code_commit,
        "run_id": run_id,
        "source_model": SOURCE_MODEL,
        "relationship_model": RELATIONSHIP_MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "source_attempt_limit": settings.source_attempt_limit,
        "relationship_attempt_limit": settings.relationship_attempt_limit,
        "total_attempt_limit": settings.total_attempt_limit,
        "document_attempt_limit": settings.document_attempt_limit,
        "stage_deadline_seconds": settings.stage_deadline_seconds,
        "cluster_generation_enabled": settings.clusters_enabled,
        "case_count": len(cases),
    }

    def evaluate(run_report: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
        errors, acceptance = _acceptance(
            workspace, run_id, cases, run_report, settings
        )
        if acceptance_hook is not None:
            hook_errors, hook_acceptance = acceptance_hook(
                workspace, run_id, cases, run_report
            )
            errors = sorted({*errors, *(str(value) for value in hook_errors)})
            acceptance = {**acceptance, **dict(hook_acceptance)}
        return errors, acceptance

    if mode == "prepare":
        report = {**base, "status": "prepared", "created_at": now_iso()}
        return _write_report(workspace, evaluation_id, mode, report, settings), report
    if not execute:
        raise PermissionError("live modes require execute=True")
    evaluator_commit = code_commit
    if mode == "revalidate" and repository_probe is _repository_state:
        evaluator_commit = _verify_revalidation_repository(code_commit)
    else:
        _verify_repository(code_commit, repository_probe)
    if mode == "revalidate":
        base["evaluator_commit"] = evaluator_commit

    run_root = workspace / "11_state" / "runs" / run_id
    if mode == "run":
        _assert_run_reservation_available(workspace, settings)
        if run_root.exists() and any(run_root.iterdir()):
            raise ValueError("run state already exists; use resume or replay")
    if mode in {"resume", *_PROVIDER_FREE_MODES} and not (
        run_root / "inventory.json"
    ).is_file():
        raise ValueError("run state is missing; use run first")

    client = ManifestZoteroClient(cases, manifest.get("collections", []))
    initial_source, initial_relationship = _attempts(workspace, run_id)
    resume_reason = (
        _resume_reservation_reason(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )
        if mode == "resume"
        else None
    )
    live_guard: Any | None = None
    if mode in {"run", "resume"}:
        has_authorization_path = authorization_path is not None
        has_authorization_sha = bool(authorization_sha256)
        if has_authorization_path != has_authorization_sha:
            raise ValueError(
                "authorization_path and authorization_sha256 must be supplied together"
            )
        if not has_authorization_path:
            if repository_probe is _repository_state:
                raise ValueError(
                    "live production gates require authorization_path and "
                    "authorization_sha256"
                )
        else:
            assert authorization_path is not None
            live_guard = attempt_guard_factory(
                authorization_path,
                authorization_sha256,
                repository_root=_REPOSITORY_ROOT,
                stage=settings.stage,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                evaluation_id=evaluation_id,
                run_id=run_id,
                source_attempt_limit=settings.source_attempt_limit,
                relationship_attempt_limit=settings.relationship_attempt_limit,
                total_attempt_limit=settings.total_attempt_limit,
                resume_reason=resume_reason,
            )
        try:
            _begin_attempt_reservation(
                workspace,
                ledger_identity,
                mode=mode,
                source_count=initial_source["count"],
                relationship_count=initial_relationship["count"],
                resume_reason=resume_reason,
                settings=settings,
            )
        except Exception:
            if live_guard is not None:
                live_guard.finish("failed", reason="local_reservation_failed")
            raise
    elif mode == "replay":
        _verify_accepted_reservation(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )
    else:
        _verify_failed_reservation(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )

    before: dict[str, tuple[str, int, int]] | None = None
    before_acceptance: dict[str, Any] | None = None
    if mode in _PROVIDER_FREE_MODES:
        prior = read_yaml(run_root / "run_report.yml", {}) or {}
        if not isinstance(prior, Mapping):
            raise ValueError("completed run report is missing")
        prior_errors, before_acceptance = evaluate(prior)
        if mode == "replay" and prior_errors:
            raise ValueError("replay requires a previously accepted gate")
        before = _gate_snapshot(workspace)

    call_kwargs: dict[str, Any] = {
        "client": client,
        "run_id": run_id,
        "resume": mode in {"resume", *_PROVIDER_FREE_MODES},
    }
    if live_guard is not None and (
        map_runner is run_map or settings.kind != "four_pdf"
    ):
        source_reader: CodexReader = (
            _ControlledPdfReader(
                SOURCE_MODEL,
                allow_cloud=True,
                reasoning_effort=REASONING_EFFORT,
                attempt_guard=live_guard,
                controlled_workspace=workspace,
                controlled_case=cases[0],
                controlled_question=str(manifest["question"]),
            )
            if settings.kind == "controlled_pdf"
            else CodexReader(
                SOURCE_MODEL,
                allow_cloud=True,
                reasoning_effort=REASONING_EFFORT,
                attempt_guard=live_guard,
            )
        )
        call_kwargs.update(
            reader=source_reader,
            literature_reasoner=CodexReader(
                RELATIONSHIP_MODEL,
                allow_cloud=True,
                reasoning_effort=REASONING_EFFORT,
                attempt_guard=live_guard,
            ),
        )
    attempt_context = (
        deny_codex_attempts()
        if mode in _PROVIDER_FREE_MODES
        else live_guard.activate()
        if live_guard is not None
        else nullcontext()
    )
    try:
        with attempt_context:
            if mode in _PROVIDER_FREE_MODES:
                replay_source_reader = _ReplayCodexReader(
                    SOURCE_MODEL,
                    allow_cloud=True,
                    reasoning_effort=REASONING_EFFORT,
                )
                if settings.kind == "controlled_pdf":
                    replay_source_reader.source_question = str(manifest["question"])
                call_kwargs.update(
                    reader=replay_source_reader,
                    literature_reasoner=_ReplayCodexReader(
                        RELATIONSHIP_MODEL,
                        allow_cloud=True,
                        reasoning_effort=REASONING_EFFORT,
                    ),
                )
            with _stage_deadline(settings):
                if mode in _PROVIDER_FREE_MODES and map_runner is run_map:
                    value = resume_map(
                        workspace,
                        run_id,
                        client=client,
                        reader=call_kwargs["reader"],
                        literature_reasoner=call_kwargs["literature_reasoner"],
                    )
                else:
                    value = map_runner(request, **call_kwargs)
    except (
        ProviderQuotaExhausted,
        ProviderTimeout,
        ProviderInterrupted,
        KeyboardInterrupt,
    ) as exc:
        paused_by = (
            "quota"
            if isinstance(exc, ProviderQuotaExhausted)
            else "timeout"
            if isinstance(exc, ProviderTimeout)
            else "interruption"
        )
        source, relationship = _attempts(workspace, run_id)
        ceiling_errors = _count_ceiling_errors(
            source["count"], relationship["count"], settings
        )
        provider_free = mode in _PROVIDER_FREE_MODES
        status = "failed" if provider_free or ceiling_errors else "paused"
        if mode in {"run", "resume"}:
            try:
                _finish_attempt_reservation(
                    workspace,
                    ledger_identity,
                    state="paused" if status == "paused" else "failed",
                    source_count=source["count"],
                    relationship_count=relationship["count"],
                    reason=paused_by if status == "paused" else "",
                    settings=settings,
                )
            except Exception:
                if live_guard is not None:
                    live_guard.finish("failed", reason="local_reservation_failed")
                raise
            if live_guard is not None:
                if status == "paused":
                    live_guard.finish("paused", reason=paused_by)
                else:
                    live_guard.finish("failed", reason="attempt_ceiling_exceeded")
        report = {
            **base,
            "status": status,
            "paused_by": paused_by if status == "paused" else "",
            "error_type": type(exc).__name__,
            "source_attempt_count": source["count"],
            "relationship_attempt_count": relationship["count"],
            "total_attempt_count": source["count"] + relationship["count"],
            "validation_errors": (
                ceiling_errors
                if ceiling_errors
                else ["replay_provider_call_blocked"]
                if provider_free
                else []
            ),
            "attempt_reservation_state": _reported_reservation_state(mode, status),
            "created_at": now_iso(),
        }
        return _write_report(workspace, evaluation_id, mode, report, settings), report
    except Exception as exc:
        try:
            source, relationship = _attempts(workspace, run_id)
            source_count = source["count"]
            relationship_count = relationship["count"]
        except Exception:
            source_count = relationship_count = None
        if mode in {"run", "resume"}:
            try:
                _finish_attempt_reservation(
                    workspace,
                    ledger_identity,
                    state="failed",
                    source_count=source_count,
                    relationship_count=relationship_count,
                    settings=settings,
                )
            except Exception:
                if live_guard is not None:
                    live_guard.finish("failed", reason="local_reservation_failed")
                raise
            if live_guard is not None:
                live_guard.finish("failed", reason="gate_execution_failed")
        count_fields = (
            _count_payload(source_count, relationship_count)
            if source_count is not None and relationship_count is not None
            else {}
        )
        report = {
            **base,
            **count_fields,
            "status": "failed",
            "paused_by": "",
            "error_type": type(exc).__name__,
            "validation_errors": ["gate_execution_failed"],
            "attempt_reservation_state": _reported_reservation_state(mode, "failed"),
            "created_at": now_iso(),
        }
        return _write_report(workspace, evaluation_id, mode, report, settings), report

    try:
        run_report = _report_dict(value)
        errors, acceptance = evaluate(run_report)
        source, relationship = _attempts(workspace, run_id)
    except Exception as exc:
        try:
            source, relationship = _attempts(workspace, run_id)
            source_count = source["count"]
            relationship_count = relationship["count"]
        except Exception:
            source_count = relationship_count = None
        if mode in {"run", "resume"}:
            try:
                _finish_attempt_reservation(
                    workspace,
                    ledger_identity,
                    state="failed",
                    source_count=source_count,
                    relationship_count=relationship_count,
                    settings=settings,
                )
            except Exception:
                if live_guard is not None:
                    live_guard.finish("failed", reason="local_reservation_failed")
                raise
            if live_guard is not None:
                live_guard.finish("failed", reason="gate_validation_failed")
        count_fields = (
            _count_payload(source_count, relationship_count)
            if source_count is not None and relationship_count is not None
            else {}
        )
        report = {
            **base,
            **count_fields,
            "status": "failed",
            "paused_by": "",
            "error_type": type(exc).__name__,
            "validation_errors": ["gate_validation_failed"],
            "attempt_reservation_state": _reported_reservation_state(mode, "failed"),
            "created_at": now_iso(),
        }
        return _write_report(workspace, evaluation_id, mode, report, settings), report
    paused_by = _pause_reason(run_report, [*source["rows"], *relationship["rows"]])
    status = "passed" if not errors else "paused" if paused_by else "failed"
    if _count_ceiling_errors(source["count"], relationship["count"], settings):
        status = "failed"
    if mode in _PROVIDER_FREE_MODES and status == "paused":
        status = "failed"
    report = {
        **base,
        **acceptance,
        "status": status,
        "paused_by": paused_by if status == "paused" else "",
        "validation_errors": errors,
        "run_status": str(run_report.get("status") or ""),
        "attempt_reservation_state": _reported_reservation_state(mode, status),
        "created_at": now_iso(),
    }
    if mode in _PROVIDER_FREE_MODES:
        assert before is not None and before_acceptance is not None
        after = _gate_snapshot(workspace)
        changed = sorted(set(before) ^ set(after)) + sorted(
            path for path in set(before) & set(after) if before[path] != after[path]
        )
        if changed:
            report["status"] = "failed"
            report["validation_errors"] = sorted(
                {*report["validation_errors"], "semantic_replay_changed"}
            )
        if acceptance != before_acceptance:
            report["status"] = "failed"
            report["validation_errors"] = sorted(
                {*report["validation_errors"], "attempt_or_route_replay_changed"}
            )
        report.update(
            semantic_file_count=len(after),
            semantic_snapshot_sha256=_snapshot_digest(after),
            semantic_changed_paths=changed,
            exact_zero_call_replay=(
                report["status"] == "passed"
                and not changed
                and acceptance == before_acceptance
            ),
        )
    else:
        reservation_state = {"passed": "accepted", "paused": "paused"}.get(
            str(report["status"]), "failed"
        )
        try:
            _finish_attempt_reservation(
                workspace,
                ledger_identity,
                state=reservation_state,
                source_count=acceptance["source_attempt_count"],
                relationship_count=acceptance["relationship_attempt_count"],
                reason=paused_by if reservation_state == "paused" else "",
                settings=settings,
            )
        except Exception:
            if live_guard is not None:
                live_guard.finish("failed", reason="local_reservation_failed")
            raise
        report["attempt_reservation_state"] = {
            "passed": "accepted",
            "paused": "paused",
        }.get(str(report["status"]), "failed")
        if live_guard is not None:
            if report["status"] == "passed":
                live_guard.finish("passed")
            elif report["status"] == "paused":
                live_guard.finish("paused", reason=paused_by)
            else:
                live_guard.finish("failed", reason="acceptance_failed")
    return _write_report(workspace, evaluation_id, mode, report, settings), report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("prepare", "run", "resume", "replay", "revalidate")
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument(
        "--controlled-pdf",
        action="store_true",
        help="Run the one-PDF, one-attempt direct subscription smoke gate.",
    )
    parser.add_argument(
        "--authorization",
        type=Path,
        help="Private stage-scoped Codex-attempt authorization (run/resume only).",
    )
    parser.add_argument(
        "--authorization-sha256",
        default="",
        help="Hash lock for --authorization (run/resume only).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required for modes that may invoke Codex.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = CONTROLLED_PDF_GATE if args.controlled_pdf else FOUR_PDF_GATE
    path, report = run_gate(
        mode=args.mode,
        manifest_path=args.manifest,
        manifest_sha256=args.manifest_sha256,
        authorization_path=args.authorization,
        authorization_sha256=args.authorization_sha256,
        execute=args.execute,
        settings=settings,
    )
    print(json.dumps({"report": str(path), **report}, sort_keys=True, default=str))
    return 0 if report["status"] in {"prepared", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
