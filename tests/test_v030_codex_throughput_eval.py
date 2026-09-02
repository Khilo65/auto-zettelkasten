from __future__ import annotations

import importlib.util
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.readers import (
    ProviderError,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTransportError,
)


SPEC = importlib.util.spec_from_file_location(
    "v030_codex_throughput_eval",
    Path(__file__).parents[1] / "tools/v030_codex_throughput_eval.py",
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _manifest(root: Path, *, stage: str = "source") -> Path:
    contract_id = runner.STAGES[stage]["contract_id"]
    payload = {
        "contract_id": contract_id,
        "arguments": (
            {
                "text": "Frozen source text.",
                "metadata": {"title": "Frozen source"},
            }
            if stage == "source"
            else {
                "profiles": [],
                "request": {},
                "context": {"pair_jobs": []},
            }
        ),
    }
    payload_path = root / "payload.yml"
    write_yaml(payload_path, payload)
    manifest_path = root / "manifest.yml"
    write_yaml(
        manifest_path,
        {
            "schema_version": "1",
            "evaluation_id": f"test-{stage}",
            "stage": stage,
            "code_commit": runner._git_commit(require_clean=False),
            "controls": runner._controls(stage),
            "contract_identity": runner.codex_contract_identity(
                contract_id,
                runner.STAGES[stage]["model"],
                runner.STAGES[stage]["reasoning_effort"],
            ),
            "payload": payload_path.name,
            "payload_sha256": sha256_file(payload_path),
        },
    )
    return manifest_path


class FakeSourceReader:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls = 0
        self.preflight_calls = 0
        self.lock = threading.Lock()

    def _ensure_codex_preflight(self) -> None:
        self.preflight_calls += 1

    def read_source_bundle(self, *_args, **_kwargs):
        with self.lock:
            self.calls += 1
        if self.failure is not None:
            raise self.failure
        time.sleep(0.03)
        return {"valid": True}


class FakeAttemptGuard:
    def __init__(
        self,
        events: list[object] | None = None,
        *,
        carried_stage_attempt_count: int = 0,
    ) -> None:
        self.events = events if events is not None else []
        self.jobs: list[str] = []
        self.finishes: list[tuple[str, str]] = []
        self.carried_stage_attempt_count = carried_stage_attempt_count

    @contextmanager
    def job(self, job_id: str):
        self.jobs.append(job_id)
        yield

    def finish(self, state: str, *, reason: str = "") -> None:
        self.finishes.append((state, reason))


def _completion() -> dict[str, object]:
    return {
        **runner.codex_contract_identity(
            "source_bundle", "gpt-5.6-luna", "medium"
        ),
        "finish_reason": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_live_commit_identity_refuses_dirty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(arguments, **_kwargs):
        return runner.subprocess.CompletedProcess(
            arguments,
            0,
            stdout="a" * 40 + "\n"
            if arguments[1] == "rev-parse"
            else " M tools/v030_codex_throughput_eval.py\n",
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="clean code commit"):
        runner._git_commit()


def test_calibration_refuses_code_imported_from_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = False

    def forbidden(**_kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("reader must not be constructed")

    monkeypatch.setattr(
        runner.auto_zettelkasten,
        "__file__",
        str(tmp_path / "other/src/auto_zettelkasten/__init__.py"),
    )
    with pytest.raises(RuntimeError, match="outside this repository's src"):
        runner.run_calibration(
            manifest_path=tmp_path / "manifest.yml",
            manifest_sha256="0" * 64,
            output_path=tmp_path / "report.yml",
            execute=True,
            require_clean=False,
            reader_factory=forbidden,
        )
    assert not constructed


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "10", "output_tokens": 5},
        {"input_tokens": 10},
        {"input_tokens": 10, "output_tokens": True},
    ],
)
def test_completion_requires_exact_numeric_usage(usage: object) -> None:
    completion = {**_completion(), "usage": usage}
    with pytest.raises(ValueError, match="usage is missing"):
        runner._validate_completion(
            completion,
            contract_id="source_bundle",
            model="gpt-5.6-luna",
            effort="medium",
        )


def test_hash_and_execute_refusals_precede_reader_construction(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "private")
    constructed = False

    def forbidden(**_kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("reader must not be constructed")

    with pytest.raises(PermissionError, match="execute=True"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=tmp_path / "output/report.yml",
            reader_factory=forbidden,
            require_clean=False,
        )
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256="0" * 64,
            output_path=tmp_path / "output/report.yml",
            execute=True,
            reader_factory=forbidden,
            require_clean=False,
        )
    with pytest.raises(ValueError, match="frozen authorization"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=tmp_path / "output/report.yml",
            execute=True,
            reader_factory=forbidden,
        )
    assert not constructed


def test_shared_guard_starts_after_preflight_and_wraps_fixed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    events: list[object] = []
    guard = FakeAttemptGuard(events)
    reader = FakeSourceReader(
        failure=ProviderTransportError("stop", transport_kind="test")
    )
    reader._ensure_codex_preflight = lambda: events.append("preflight")  # type: ignore[method-assign]

    def start(_cls, *_args, **kwargs):
        events.append(("start", kwargs))
        return guard

    monkeypatch.setattr(runner.CodexAttemptGuard, "start", classmethod(start))
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=tmp_path / "authorization.json",
        authorization_sha256="a" * 64,
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=lambda **_kwargs: reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert events[0] == "preflight"
    assert events[1][0] == "start"
    assert events[1][1]["stage"] == "luna_source_calibration"
    assert events[1][1]["resume_reason"] is None
    assert reader.attempt_guard is guard
    assert guard.jobs == ["c1:s001"]
    assert guard.finishes == [("failed", "transport")]
    assert report["status"] == "failed"


def test_timeout_is_a_typed_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path / "private")
    monkeypatch.setattr(
        runner,
        "wait",
        lambda futures, **_kwargs: (set(), set(futures)),
    )
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    monkeypatch.setattr(runner, "current_provider_completion", _completion)

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=lambda **_kwargs: FakeSourceReader(),
        canceller=lambda: 0,
        require_clean=False,
    )

    assert report["status"] == "paused"
    assert report["stop_reason"] == "timeout"
    assert report["levels"][0]["timed_out"] is True


def test_interruption_writes_pause_and_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    output_path = tmp_path / "output/report.yml"
    interruption = KeyboardInterrupt("stop")
    guard = FakeAttemptGuard()

    def finish_then_fail(state: str, *, reason: str = "") -> None:
        guard.finishes.append((state, reason))
        raise RuntimeError("finish failed")

    guard.finish = finish_then_fail  # type: ignore[method-assign]
    monkeypatch.setattr(
        runner.CodexAttemptGuard,
        "start",
        classmethod(lambda _cls, *_args, **_kwargs: guard),
    )
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)

    with pytest.raises(KeyboardInterrupt) as caught:
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            authorization_path=tmp_path / "authorization.json",
            authorization_sha256="a" * 64,
            output_path=output_path,
            execute=True,
            reader_factory=lambda **_kwargs: FakeSourceReader(
                failure=interruption
            ),
            canceller=lambda: 0,
            require_clean=False,
        )

    assert caught.value is interruption
    assert guard.finishes == [("paused", "interruption")]
    report = read_yaml(output_path)
    assert report["status"] == "paused"
    assert report["stop_reason"] == "interruption"


def test_source_calibration_uses_exact_70_attempt_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MINIMUM_GAIN_PERCENT", -1.0)
    manifest = _manifest(tmp_path / "private")
    reader = FakeSourceReader()
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return reader

    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    monkeypatch.setattr(runner, "current_provider_completion", _completion)
    output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=factory,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert report["status"] == "passed"
    assert report["attempt_count"] == 70
    assert report["retry_count"] == 0
    assert [row["concurrency"] for row in report["levels"]] == [1, 2, 4, 8, 16, 32]
    assert reader.calls == 70
    assert reader.preflight_calls == 1
    assert factory_calls == [
        {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "allow_cloud": True,
            "request_deadline": 600.0,
        }
    ]
    ledger = manifest.parent / ".v030-attempts/test-source.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert sum(row["record"] == "reserved" for row in rows) == 70
    assert sum(row["record"] == "completed" for row in rows) == 70
    assert output.stat().st_mode & 0o777 == 0o600
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert report["levels"][0]["usage"] == {
        "input_tokens": 80,
        "output_tokens": 40,
    }
    assert report["levels"][0]["completion_order"] == list(range(1, 9))
    assert report["levels"][0]["child_cpu_seconds"] >= 0
    assert report["levels"][0]["child_max_rss_bytes"] >= 0


def test_carried_attempts_stop_before_an_oversized_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MINIMUM_GAIN_PERCENT", -1.0)
    manifest = _manifest(tmp_path / "private")
    reader = FakeSourceReader()
    guard = FakeAttemptGuard(carried_stage_attempt_count=3)
    monkeypatch.setattr(
        runner.CodexAttemptGuard,
        "start",
        classmethod(lambda _cls, *_args, **_kwargs: guard),
    )
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    monkeypatch.setattr(runner, "current_provider_completion", _completion)

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=tmp_path / "authorization.json",
        authorization_sha256="a" * 64,
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=lambda **_kwargs: reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert report["status"] == "passed"
    assert report["stop_reason"] == "insufficient_attempt_allowance"
    assert [row["concurrency"] for row in report["levels"]] == [1, 2, 4, 8, 16]
    assert report["attempt_count"] == reader.calls == 38
    assert report["carried_stage_attempt_count"] == 3
    assert report["remaining_attempts"] == 29
    assert guard.finishes == [("passed", "")]


def test_carried_attempts_that_cannot_fit_the_baseline_fail_without_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    reader = FakeSourceReader()
    guard = FakeAttemptGuard(carried_stage_attempt_count=70)
    monkeypatch.setattr(
        runner.CodexAttemptGuard,
        "start",
        classmethod(lambda _cls, *_args, **_kwargs: guard),
    )

    output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=tmp_path / "authorization.json",
        authorization_sha256="a" * 64,
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=lambda **_kwargs: reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert report["status"] == "failed"
    assert report["stop_reason"] == "insufficient_attempt_allowance"
    assert report["levels"] == []
    assert report["attempt_count"] == reader.calls == 0
    assert report["remaining_attempts"] == 0
    assert guard.finishes == [("failed", "terminal")]
    ledger = manifest.parent / report["attempt_ledger"]
    assert output.is_file() and ledger.is_file() and ledger.read_bytes() == b""


def test_failure_aborts_without_retry_and_consumed_ledger_blocks_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    reader = FakeSourceReader(
        failure=ProviderTransportError("secret diagnostic", transport_kind="test")
    )
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    output_path = tmp_path / "output/report.yml"

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=output_path,
        execute=True,
        reader_factory=lambda **_kwargs: reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert reader.calls == 1
    assert report["status"] == "failed"
    assert report["attempt_count"] == 1
    assert report["retry_count"] == 0
    assert report["stop_reason"] == "transport"
    assert report["attempts"][0]["error_type"] == "ProviderTransportError"
    assert "secret diagnostic" not in output_path.read_text()

    output_path.unlink()
    with pytest.raises(ValueError, match="consumed attempts"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=output_path,
            execute=True,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
            require_clean=False,
        )
    with pytest.raises(ValueError, match="consumed attempts"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=tmp_path / "different/report.yml",
            execute=True,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
            require_clean=False,
        )


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        (
            "Codex emitted unexpected event: turn.delta",
            "unexpected_event:turn.delta",
        ),
        (
            "Codex attempted tool event: command_execution",
            "disallowed_item:command_execution",
        ),
        (
            "Codex attempted tool event: todo_list",
            "disallowed_item:todo_list",
        ),
        (
            "Codex attempted tool event: command_execution "
            "Authorization: Bearer " + "synthetic-isolation-secret",
            "unknown",
        ),
    ],
)
def test_isolation_failure_persists_only_coarse_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_reason: str,
) -> None:
    manifest = _manifest(tmp_path / "private")
    output_path = tmp_path / "output/report.yml"
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=output_path,
        execute=True,
        reader_factory=lambda **_kwargs: FakeSourceReader(
            failure=ProviderIsolationFailure(message)
        ),
        canceller=lambda: 0,
        require_clean=False,
    )

    attempt = report["attempts"][0]
    assert attempt["isolation_reason"] == expected_reason
    assert "error" not in attempt
    serialized = output_path.read_text()
    assert message not in serialized
    assert "Codex attempted tool event" not in serialized
    assert "Codex emitted unexpected event" not in serialized
    assert "synthetic-isolation-secret" not in serialized


@pytest.mark.parametrize(
    ("suffix", "expected_reason"),
    [
        ("stream_loss", "error_item:stream_loss"),
        ("synthetic-secret", "error_item:unknown"),
    ],
)
def test_error_item_persists_only_coarse_non_tool_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    expected_reason: str,
) -> None:
    manifest = _manifest(tmp_path / "private")
    output_path = tmp_path / "output/report.yml"
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=output_path,
        execute=True,
        reader_factory=lambda **_kwargs: FakeSourceReader(
            failure=ProviderError(f"Codex CLI emitted error item: {suffix}")
        ),
        canceller=lambda: 0,
        require_clean=False,
    )

    attempt = report["attempts"][0]
    assert attempt["error_reason"] == expected_reason
    assert "isolation_reason" not in attempt
    assert "error" not in attempt
    assert "synthetic-secret" not in output_path.read_text()


def test_quota_resume_submits_only_unreserved_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    output_path = tmp_path / "output/report.yml"
    authorization_path = tmp_path / "authorization.json"
    paused_reader = FakeSourceReader(failure=ProviderQuotaExhausted("paused"))
    starts: list[dict[str, object]] = []
    guards = [FakeAttemptGuard(), FakeAttemptGuard()]

    def start(_cls, *_args, **kwargs):
        starts.append(kwargs)
        return guards[len(starts) - 1]

    monkeypatch.setattr(runner.CodexAttemptGuard, "start", classmethod(start))
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)

    _output, paused = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=authorization_path,
        authorization_sha256="a" * 64,
        output_path=output_path,
        execute=True,
        reader_factory=lambda **_kwargs: paused_reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert paused["status"] == "paused"
    assert paused["attempt_count"] == 1
    resumed_reader = FakeSourceReader()
    monkeypatch.setattr(runner, "current_provider_completion", _completion)
    _output, resumed = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=authorization_path,
        authorization_sha256="a" * 64,
        output_path=output_path,
        execute=True,
        resume=True,
        reader_factory=lambda **_kwargs: resumed_reader,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert resumed["status"] == "paused"
    assert resumed["attempt_count"] == 8
    assert resumed["resume_count"] == 1
    assert resumed_reader.calls == 7
    assert len({row["job_id"] for row in resumed["attempts"]}) == 8
    assert [row["resume_reason"] for row in starts] == [None, "quota"]
    assert guards[0].finishes == [("paused", "quota")]
    assert guards[1].finishes == [("paused", "quota")]
    with pytest.raises(ValueError, match="no unfinished jobs"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=output_path,
            execute=True,
            resume=True,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
            require_clean=False,
        )


def test_resume_refuses_report_ledger_disagreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    output_path = tmp_path / "output/report.yml"
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)
    runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        output_path=output_path,
        execute=True,
        reader_factory=lambda **_kwargs: FakeSourceReader(
            failure=ProviderQuotaExhausted("paused")
        ),
        canceller=lambda: 0,
        require_clean=False,
    )
    report = read_yaml(output_path)
    report["attempts"][0]["failure_class"] = "timeout"
    write_yaml(output_path, report)

    with pytest.raises(ValueError, match="does not match the attempt ledger"):
        runner.run_calibration(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            output_path=output_path,
            execute=True,
            resume=True,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
            canceller=lambda: 0,
            require_clean=False,
        )


def test_relationship_stage_uses_terra_medium_public_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private", stage="relationship")
    calls: list[tuple[object, object, object]] = []
    factory_kwargs: dict[str, object] = {}
    starts: list[dict[str, object]] = []

    class FakeRelationshipReader:
        def adjudicate_relationships(self, profiles, request, *, context=None):
            calls.append((profiles, request, context))
            raise ProviderTransportError("stop", transport_kind="test")

    def factory(**kwargs):
        factory_kwargs.update(kwargs)
        return FakeRelationshipReader()

    monkeypatch.setattr(
        runner.CodexAttemptGuard,
        "start",
        classmethod(
            lambda _cls, *_args, **kwargs: (
                starts.append(kwargs) or FakeAttemptGuard()
            )
        ),
    )

    _output, report = runner.run_calibration(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=tmp_path / "authorization.json",
        authorization_sha256="a" * 64,
        output_path=tmp_path / "output/report.yml",
        execute=True,
        reader_factory=factory,
        canceller=lambda: 0,
        require_clean=False,
    )

    assert factory_kwargs["model"] == "gpt-5.6-terra"
    assert factory_kwargs["reasoning_effort"] == "medium"
    assert starts[0]["stage"] == "terra_relationship_calibration"
    assert report["maximum_attempts"] == 38
    assert report["attempt_count"] == 1
    assert len(calls) == 1
    assert calls[0][1].literature_policy.cluster_generation_enabled is False
