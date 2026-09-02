from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import auto_zettelkasten.readers as reader_module
from auto_zettelkasten.codex_attempt_guard import (
    _ACTIVE_GUARD,
    _ACTIVE_JOB,
    reserve_codex_attempt,
)
from auto_zettelkasten.readers import CodexReader
from conftest import fake_codex_preflight


SPEC = importlib.util.spec_from_file_location(
    "v030_codex_campaign_guard",
    Path(__file__).parents[1] / "tools/v030_codex_campaign_guard.py",
)
assert SPEC and SPEC.loader
guard_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard_module
SPEC.loader.exec_module(guard_module)


def _run(*command: str, cwd: Path) -> str:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _fixture(
    tmp_path: Path,
    *,
    source_limit: int = 1,
    relationship_limit: int = 2,
) -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run("git", "init", "-q", cwd=repository)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repository)
    _run("git", "config", "user.name", "Test", cwd=repository)
    (repository / "tracked.txt").write_text("fixed\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=repository)
    _run("git", "commit", "-qm", "fixture", cwd=repository)
    code_commit = _run("git", "rev-parse", "HEAD", cwd=repository)

    private = tmp_path / "private"
    private.mkdir()
    manifest = private / "manifest.json"
    manifest.write_text('{"schema_version":1}\n', encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    ledger = (private / "attempts.jsonl").resolve()
    authorization = private / "authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authorization_id": "campaign-auth-001",
                "ledger": str(ledger),
                "code_commit": code_commit,
                "manifest_sha256": manifest_sha256,
                "evaluation_id": "strategic-eight",
                "run_id": "run-001",
                "stage": "strategic8",
                "source_attempt_limit": source_limit,
                "relationship_attempt_limit": relationship_limit,
                "total_attempt_limit": source_limit + relationship_limit,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    authorization_sha256 = hashlib.sha256(authorization.read_bytes()).hexdigest()
    guard_module.initialize_codex_campaign_ledger(
        authorization,
        authorization_sha256,
        repository_root=repository,
    )
    return {
        "repository": repository,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "authorization": authorization,
        "authorization_sha256": authorization_sha256,
        "ledger": ledger,
        "source_limit": source_limit,
        "relationship_limit": relationship_limit,
    }


def _start(fixture: dict[str, Any], **overrides: Any) -> Any:
    arguments = {
        "repository_root": fixture["repository"],
        "stage": "strategic8",
        "manifest_path": fixture["manifest"],
        "manifest_sha256": fixture["manifest_sha256"],
        "evaluation_id": "strategic-eight",
        "run_id": "run-001",
        "source_attempt_limit": fixture["source_limit"],
        "relationship_attempt_limit": fixture["relationship_limit"],
        "total_attempt_limit": (
            fixture["source_limit"] + fixture["relationship_limit"]
        ),
        "resume_reason": None,
    }
    arguments.update(overrides)
    return guard_module.CodexCampaignGuard.start(
        fixture["authorization"],
        fixture["authorization_sha256"],
        **arguments,
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_role_and_total_ceilings_are_reserved_before_spawn(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture)
    assert guard.reserve("source_bundle", "source:001") == "source:001"
    with pytest.raises(
        guard_module.CodexCampaignCeilingExceeded, match="source attempt"
    ):
        guard.reserve("source_bundle", "source:002")
    guard.reserve("literature_family_plan", "relationship:001")
    guard.reserve("cluster_synthesis", "relationship:002")
    with pytest.raises(
        guard_module.CodexCampaignCeilingExceeded, match="relationship attempt"
    ):
        guard.reserve("cluster_plan", "relationship:003")
    guard.finish("passed")

    reservations = [
        row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
    ]
    assert [row["role"] for row in reservations] == [
        "source",
        "relationship",
        "relationship",
    ]
    assert [row["total_attempt_number"] for row in reservations] == [1, 2, 3]


def test_all_registered_contracts_and_dynamic_readers_use_guard(
    tmp_path: Path,
) -> None:
    source_contracts = sorted(guard_module.SOURCE_CONTRACTS)
    contracts = sorted(guard_module.RELATIONSHIP_CONTRACTS)
    fixture = _fixture(
        tmp_path,
        source_limit=len(source_contracts),
        relationship_limit=len(contracts),
    )
    guard = _start(fixture)
    for index, contract_id in enumerate(source_contracts, start=1):
        guard.reserve(contract_id, f"source:{index:03d}")
    with guard.activate():
        reader = CodexReader(model="gpt-5.6-terra", allow_cloud=True)
        assert reader.attempt_guard is guard
        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = list(
                executor.map(
                    lambda item: reserve_codex_attempt(
                        reader.attempt_guard,
                        contract_id=item[1],
                        job_id=f"request:{item[0]:03d}",
                    ),
                    enumerate(contracts, start=1),
                )
            )
    assert len(jobs) == len(contracts)
    with pytest.raises(guard_module.CodexCampaignStateError, match="not authorized"):
        guard.reserve("unregistered_contract", "request:unsupported")
    guard.finish("passed")


def test_typed_resume_allows_one_new_attempt_for_the_same_job(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, source_limit=2, relationship_limit=1)
    guard = _start(fixture)
    guard.reserve("source_bundle", "request:stable")
    with pytest.raises(guard_module.CodexCampaignStateError, match="already reserved"):
        guard.reserve("source_bundle", "request:stable")
    with pytest.raises(guard_module.CodexCampaignStateError, match="already reserved"):
        guard.reserve("relationship_adjudication", "request:stable")
    guard.finish("paused", reason="quota")

    resumed = _start(fixture, resume_reason="quota")
    with pytest.raises(guard_module.CodexCampaignStateError, match="changed contracts"):
        resumed.reserve("relationship_adjudication", "request:stable")
    resumed.reserve("source_bundle", "request:stable")
    with pytest.raises(guard_module.CodexCampaignStateError, match="already reserved"):
        resumed.reserve("source_bundle", "request:stable")
    resumed.finish("failed", reason="duplicate_job_verified")
    with pytest.raises(guard_module.CodexCampaignStateError, match="matching typed"):
        _start(fixture, resume_reason="quota")

    reservations = [
        row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
    ]
    assert [row["job_attempt_number"] for row in reservations] == [1, 2]
    assert len({row["session_id"] for row in reservations}) == 2


def test_job_validates_and_resets_active_context(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture)
    previous_guard = object()
    guard_token = _ACTIVE_GUARD.set(previous_guard)
    job_token = _ACTIVE_JOB.set(("outer-run", "outer-job"))
    try:
        with pytest.raises(ValueError, match="job_id is invalid"):
            with guard.job("invalid job"):
                pass
        assert _ACTIVE_GUARD.get() is previous_guard
        assert _ACTIVE_JOB.get() == ("outer-run", "outer-job")

        with pytest.raises(RuntimeError, match="leave job"):
            with guard.job("request:stable"):
                assert _ACTIVE_GUARD.get() is guard
                assert _ACTIVE_JOB.get() == (guard.run_id, "request:stable")
                assert reserve_codex_attempt(None, contract_id="source_bundle") == (
                    "request:stable"
                )
                raise RuntimeError("leave job")
        assert _ACTIVE_GUARD.get() is previous_guard
        assert _ACTIVE_JOB.get() == ("outer-run", "outer-job")
    finally:
        _ACTIVE_JOB.reset(job_token)
        _ACTIVE_GUARD.reset(guard_token)
    guard.finish("failed", reason="context_reset_verified")


def test_hash_binding_and_typed_resume_are_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture)
    with pytest.raises(guard_module.CodexCampaignStateError, match="already active"):
        _start(fixture)
    guard.finish("paused", reason="timeout")

    with pytest.raises(guard_module.CodexCampaignStateError, match="matching typed"):
        _start(fixture, resume_reason="quota")
    with pytest.raises(ValueError, match="arguments do not match"):
        _start(fixture, resume_reason="timeout", evaluation_id="other-evaluation")

    original = fixture["manifest"].read_text(encoding="utf-8")
    fixture["manifest"].write_text(original + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        _start(fixture, resume_reason="timeout")
    fixture["manifest"].write_text(original, encoding="utf-8")

    resumed = _start(fixture, resume_reason="timeout")
    resumed.reserve("source_bundle", "request:after-timeout")
    resumed.finish("passed")
    with pytest.raises(guard_module.CodexCampaignStateError, match="matching typed"):
        _start(fixture)


def test_abandoned_running_session_requires_interruption_resume(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, source_limit=2, relationship_limit=1)
    abandoned = _start(fixture)
    abandoned.reserve("source_bundle", "request:before-crash")
    fcntl.flock(abandoned._run_lock_descriptor, fcntl.LOCK_UN)
    os.close(abandoned._run_lock_descriptor)

    with pytest.raises(
        guard_module.CodexCampaignStateError, match="requires interruption"
    ):
        _start(fixture, resume_reason="quota")
    resumed = _start(fixture, resume_reason="interruption")
    resumed.reserve("source_bundle", "request:before-crash")
    resumed.finish("passed")

    interruption = [
        row
        for row in _rows(fixture["ledger"])
        if row["record"] == "run_finished" and row["state"] == "paused"
    ]
    assert len(interruption) == 1
    assert interruption[0]["reason"] == "interruption"
    reservations = [
        row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
    ]
    assert [row["job_attempt_number"] for row in reservations] == [1, 2]


def test_reader_reserves_before_provider_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, source_limit=1, relationship_limit=0)
    guard = _start(fixture)
    with guard.activate():
        reader = CodexReader(model="gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(
        tmp_path, "/not/executed/codex", {}
    )

    class SpawnObserved(RuntimeError):
        pass

    def fake_popen(*_args: Any, **_kwargs: Any) -> None:
        reservations = [
            row
            for row in _rows(fixture["ledger"])
            if row["record"] == "reserved"
        ]
        assert len(reservations) == 1
        assert reservations[0]["contract_id"] == "source_bundle"
        raise SpawnObserved

    monkeypatch.setattr(reader_module.subprocess, "Popen", fake_popen)
    with pytest.raises(SpawnObserved):
        reader.read_source_bundle("Complete synthetic source.", {"title": "Fixture"})
    guard.finish("failed", reason="spawn_order_verified")
