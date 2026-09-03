from __future__ import annotations

import hashlib
import json
import fcntl
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from auto_zettelkasten.codex_attempt_guard import (
    STAGE_ATTEMPT_LIMITS,
    TOTAL_ATTEMPT_LIMIT,
    CodexAttemptCeilingExceeded,
    CodexAttemptGuard,
    CodexAttemptStateError,
    deny_codex_attempts,
    initialize_codex_attempt_ledger,
    reserve_codex_attempt,
)
from auto_zettelkasten.readers import CodexReader, ProviderTransportError
from conftest import fake_codex_preflight


def _run(*command: str, cwd: Path) -> str:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _fixture(
    tmp_path: Path,
    *,
    authorization_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    _run("git", "init", "-q", cwd=repository)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repository)
    _run("git", "config", "user.name", "Test", cwd=repository)
    (repository / "tracked.txt").write_text("fixed\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=repository)
    _run("git", "commit", "-qm", "fixture", cwd=repository)

    private = tmp_path / "private"
    private.mkdir()
    manifest = private / "manifest.json"
    manifest.write_text('{"schema_version":1}\n', encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    ledger = (private / "attempts.jsonl").resolve()
    authorization = private / "authorization.json"
    authorization_value = {
        "schema_version": 1,
        "authorization_id": "v030-release-20260831",
        "ledger": str(ledger),
        "total_attempt_limit": TOTAL_ATTEMPT_LIMIT,
        "stage_attempt_limits": dict(STAGE_ATTEMPT_LIMITS),
    }
    if authorization_overrides is not None:
        authorization_value.update(authorization_overrides)
    authorization.write_text(
        json.dumps(authorization_value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    authorization_sha256 = hashlib.sha256(authorization.read_bytes()).hexdigest()
    initialize_codex_attempt_ledger(
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
    }


def _start(fixture: dict[str, Any], stage: str, **kwargs: Any) -> CodexAttemptGuard:
    return CodexAttemptGuard.start(
        fixture["authorization"],
        fixture["authorization_sha256"],
        repository_root=fixture["repository"],
        stage=stage,
        manifest_path=fixture["manifest"],
        manifest_sha256=fixture["manifest_sha256"],
        **kwargs,
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_typed_resume_consumes_orphans_and_rejects_job_reuse(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture, "luna_source_calibration")
    with guard.job("source:001"):
        assert reserve_codex_attempt(None, contract_id="source_bundle") == "source:001"
    guard.finish("paused", reason="quota")

    with pytest.raises(CodexAttemptStateError, match="matching typed pause"):
        _start(
            fixture,
            "luna_source_calibration",
            resume_reason="timeout",
        )
    resumed = _start(
        fixture,
        "luna_source_calibration",
        resume_reason="quota",
    )
    with resumed.job("source:001"), pytest.raises(
        CodexAttemptStateError, match="already reserved"
    ):
        reserve_codex_attempt(None, contract_id="source_bundle")
    with resumed.job("source:002"):
        reserve_codex_attempt(None, contract_id="source_bundle")
    resumed.finish("passed")

    reservations = [row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"]
    assert [row["job_id"] for row in reservations] == ["source:001", "source:002"]
    with pytest.raises(CodexAttemptStateError, match="matching typed pause"):
        _start(fixture, "luna_source_calibration")


def test_exact_stage_and_cumulative_attempt_caps(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for stage, limit in STAGE_ATTEMPT_LIMITS.items():
        guard = _start(fixture, stage)
        for index in range(limit):
            guard.reserve("source_bundle", f"job:{index + 1:03d}")
        with pytest.raises(CodexAttemptCeilingExceeded):
            guard.reserve("source_bundle", "one-too-many")
        guard.finish("passed")

    reservations = [row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"]
    assert len(reservations) == TOTAL_ATTEMPT_LIMIT == sum(
        STAGE_ATTEMPT_LIMITS.values()
    )


def test_carried_attempt_reduces_stage_and_cumulative_caps(tmp_path: Path) -> None:
    carried_stages = {stage: 0 for stage in STAGE_ATTEMPT_LIMITS}
    carried_stages["luna_source_calibration"] = 1
    fixture = _fixture(
        tmp_path,
        authorization_overrides={
            "carried_attempts": 1,
            "carried_stage_attempts": carried_stages,
        },
    )
    assert _rows(fixture["ledger"])[0] == {
        "authorization_sha256": fixture["authorization_sha256"],
        "carried_attempts": 1,
        "carried_stage_attempts": carried_stages,
        "record": "authorization",
        "stage_attempt_limits": dict(STAGE_ATTEMPT_LIMITS),
        "total_attempt_limit": TOTAL_ATTEMPT_LIMIT,
    }

    for stage, limit in STAGE_ATTEMPT_LIMITS.items():
        guard = _start(fixture, stage)
        assert guard.carried_stage_attempt_count == carried_stages[stage]
        available = limit - carried_stages[stage]
        for index in range(available):
            guard.reserve("source_bundle", f"job:{index + 1:03d}")
        with pytest.raises(CodexAttemptCeilingExceeded):
            guard.reserve("source_bundle", "one-too-many")
        guard.finish("passed")

    reservations = [
        row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
    ]
    assert len(reservations) == 133
    assert reservations[0]["attempt_number"] == 2
    assert reservations[0]["stage_attempt_number"] == 2
    assert reservations[-1]["attempt_number"] == TOTAL_ATTEMPT_LIMIT


@pytest.mark.parametrize(
    "overrides",
    [
        {"carried_attempts": 1},
        {
            "carried_stage_attempts": {
                stage: 0 for stage in STAGE_ATTEMPT_LIMITS
            }
        },
        {
            "carried_attempts": -1,
            "carried_stage_attempts": {
                stage: 0 for stage in STAGE_ATTEMPT_LIMITS
            },
        },
        {
            "carried_attempts": 0,
            "carried_stage_attempts": {
                "luna_source_calibration": 0,
            },
        },
        {
            "carried_attempts": 1,
            "carried_stage_attempts": {
                stage: 0 for stage in STAGE_ATTEMPT_LIMITS
            },
        },
        {
            "carried_attempts": 71,
            "carried_stage_attempts": {
                stage: (71 if stage == "luna_source_calibration" else 0)
                for stage in STAGE_ATTEMPT_LIMITS
            },
        },
        {
            "carried_attempts": 135,
            "carried_stage_attempts": {
                **dict(STAGE_ATTEMPT_LIMITS),
                "luna_source_calibration": 71,
            },
        },
    ],
)
def test_malformed_carried_attempts_are_rejected(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="carried"):
        _fixture(tmp_path, authorization_overrides=overrides)


def test_carried_attempt_header_is_hash_locked(tmp_path: Path) -> None:
    carried_stages = {stage: 0 for stage in STAGE_ATTEMPT_LIMITS}
    carried_stages["luna_source_calibration"] = 1
    fixture = _fixture(
        tmp_path,
        authorization_overrides={
            "carried_attempts": 1,
            "carried_stage_attempts": carried_stages,
        },
    )
    rows = _rows(fixture["ledger"])
    rows[0]["carried_attempts"] = 0
    fixture["ledger"].write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="authorization header mismatch"):
        _start(fixture, "luna_source_calibration")


def test_guard_is_captured_by_reader_and_safe_across_threads(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture, "final_head_contract_smoke")
    with guard.activate():
        reader = CodexReader(model="gpt-5.6-luna", allow_cloud=True)
    assert reader.attempt_guard is guard

    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(
            executor.map(
                lambda index: reserve_codex_attempt(
                    reader.attempt_guard,
                    contract_id="source_bundle",
                    job_id=f"thread:{index + 1:03d}",
                ),
                range(4),
            )
        )
    assert sorted(jobs) == [
        "thread:001",
        "thread:002",
        "thread:003",
        "thread:004",
    ]
    guard.finish("passed")
    assert CodexReader(model="gpt-5.6-luna", allow_cloud=True).attempt_guard is None


def test_authorization_allows_only_one_active_stage(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    source = _start(fixture, "luna_source_calibration")
    with pytest.raises(CodexAttemptStateError, match="already active"):
        _start(fixture, "terra_relationship_calibration")
    source.finish("passed")
    relationship = _start(fixture, "terra_relationship_calibration")
    relationship.finish("passed")


def test_reader_reserves_immediately_before_popen_and_keeps_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    guard = _start(fixture, "final_head_contract_smoke")
    reader = CodexReader(
        model="gpt-5.6-luna",
        allow_cloud=True,
        attempt_guard=guard,
    )
    reader._preflight = fake_codex_preflight(
        tmp_path,
        "/usr/bin/false",
        {"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    calls = 0

    def fail_after_reservation(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        reservations = [
            row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
        ]
        assert reservations[-1]["job_id"] == "contract:001"
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(
        "auto_zettelkasten.readers.subprocess.Popen", fail_after_reservation
    )
    with guard.job("contract:001"), pytest.raises(
        ProviderTransportError, match="Codex CLI process could not start"
    ):
        reader.read_source_bundle("A complete source.", {"title": "Fixture"})
    with guard.job("contract:001"), pytest.raises(
        CodexAttemptStateError, match="already reserved"
    ):
        reader.read_source_bundle("A complete source.", {"title": "Fixture"})
    assert calls == 1


def test_default_request_identity_blocks_retry_but_allows_distinct_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    first = _start(fixture, "final_four_pdf_public_path")
    reader = CodexReader(
        model="gpt-5.6-luna",
        allow_cloud=True,
        attempt_guard=first,
    )
    reader._preflight = fake_codex_preflight(
        tmp_path,
        "/usr/bin/false",
        {"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    calls = 0

    def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise OSError("synthetic spawn failure")

    with monkeypatch.context() as patch:
        patch.setattr("auto_zettelkasten.readers.subprocess.Popen", fail_spawn)
        with pytest.raises(
            ProviderTransportError, match="Codex CLI process could not start"
        ):
            reader.read_source_bundle(
                "A complete source.",
                {"title": "Fixture"},
                attachment_paths=(image,),
            )
    first.finish("paused", reason="timeout")

    resumed = _start(
        fixture,
        "final_four_pdf_public_path",
        resume_reason="timeout",
    )
    resumed_reader = CodexReader(
        model="gpt-5.6-luna",
        allow_cloud=True,
        attempt_guard=resumed,
    )
    resumed_reader._preflight = reader._preflight
    with monkeypatch.context() as patch:
        patch.setattr("auto_zettelkasten.readers.subprocess.Popen", fail_spawn)
        with pytest.raises(CodexAttemptStateError, match="already reserved"):
            resumed_reader.read_source_bundle(
                "A complete source.",
                {"title": "Fixture"},
                attachment_paths=(image,),
            )
        with pytest.raises(
            ProviderTransportError, match="Codex CLI process could not start"
        ):
            resumed_reader.read_source_bundle(
                "Recovered local text.", {"title": "Fixture"}
            )
    assert calls == 2
    reservations = [
        row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"
    ]
    assert len(reservations) == 2
    assert len({row["job_id"] for row in reservations}) == 2
    assert all(row["job_id"].startswith("request:") for row in reservations)
    resumed.finish("failed", reason="retry_blocked")


def test_exact_replay_deny_is_captured_and_blocks_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deny_codex_attempts():
        reader = CodexReader(model="gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(
        tmp_path,
        "/usr/bin/false",
        {"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    monkeypatch.setattr(
        "auto_zettelkasten.readers.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("Popen must not run during replay"),
    )
    with pytest.raises(CodexAttemptStateError, match="calls are forbidden"):
        reader.read_source_bundle("A complete source.", {"title": "Fixture"})


def test_frozen_authorization_and_clean_commit_are_required(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["authorization"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authorization SHA-256 mismatch"):
        _start(fixture, "final_head_contract_smoke")

    fixture = _fixture(tmp_path / "second")
    (fixture["repository"] / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean code commit"):
        _start(fixture, "final_head_contract_smoke")


def test_only_interruption_can_adopt_an_abandoned_stage(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    abandoned = _start(fixture, "final_four_pdf_public_path")
    abandoned.reserve("source_bundle", "pdf:001")
    fcntl.flock(abandoned._run_lock_descriptor, fcntl.LOCK_UN)
    os.close(abandoned._run_lock_descriptor)

    with pytest.raises(CodexAttemptStateError, match="requires interruption"):
        _start(
            fixture,
            "final_four_pdf_public_path",
            resume_reason="quota",
        )
    resumed = _start(
        fixture,
        "final_four_pdf_public_path",
        resume_reason="interruption",
    )
    resumed.reserve("source_bundle", "pdf:002")
    resumed.finish("passed")
    reservations = [row for row in _rows(fixture["ledger"]) if row["record"] == "reserved"]
    assert [row["job_id"] for row in reservations] == ["pdf:001", "pdf:002"]


def test_private_boundaries_reject_unrelated_git_repositories(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    other_repository = tmp_path / "other-repository"
    other_repository.mkdir()
    _run("git", "init", "-q", cwd=other_repository)
    manifest = other_repository / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside Git repositories"):
        CodexAttemptGuard.start(
            fixture["authorization"],
            fixture["authorization_sha256"],
            repository_root=fixture["repository"],
            stage="final_head_contract_smoke",
            manifest_path=manifest,
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        )

    authorization = tmp_path / "private" / "other-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authorization_id": "other",
                "ledger": str((other_repository / "attempts.jsonl").resolve()),
                "total_attempt_limit": TOTAL_ATTEMPT_LIMIT,
                "stage_attempt_limits": dict(STAGE_ATTEMPT_LIMITS),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside Git repositories"):
        initialize_codex_attempt_ledger(
            authorization,
            hashlib.sha256(authorization.read_bytes()).hexdigest(),
            repository_root=fixture["repository"],
        )
