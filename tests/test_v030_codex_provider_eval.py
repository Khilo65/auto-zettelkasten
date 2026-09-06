from __future__ import annotations

import json
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.readers import (
    ProviderError,
    ProviderIsolationFailure,
    ProviderTimeout,
    ProviderTransportError,
)


TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "v030_codex_provider_eval",
    TOOLS / "v030_codex_provider_eval.py",
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_evidence_profiles_use_the_source_model_role() -> None:
    assert "evidence_profile" in runner.SOURCE_CONTRACTS


def _write_manifest(root: Path) -> Path:
    cases = []
    for contract_id in runner.CONTRACTS:
        payload = {
            "contract_id": contract_id,
            "arguments": (
                {
                    "text": "Frozen source text.",
                    "metadata": {"title": "Frozen source"},
                }
                if contract_id == "source_bundle"
                else {
                    "text": "Frozen chunk text.",
                    "metadata": {"title": "Frozen source"},
                    "chunk_id": "chunk-1",
                    "locator": "p. 1",
                }
                if contract_id == "chunk_evidence"
                else {
                    "note": {"profile_prompt": "Return the frozen profile."},
                    "context": {"profile_prompt_version": "6"},
                }
                if contract_id == "evidence_profile"
                else {"profiles": [], "request": {}, "context": {"pair_jobs": [{"pair_job_id": "job-smoke"}]}}
                if contract_id == "relationship_adjudication"
                else {"profiles": [], "request": {}, "context": {}}
            ),
        }
        relative = Path("cases") / f"{contract_id}.yml"
        write_yaml(root / relative, payload)
        cases.append(
            {
                "case_id": f"case-{contract_id}",
                "contract_id": contract_id,
                "payload": relative.as_posix(),
                "payload_sha256": sha256_file(root / relative),
                "contract_identity": runner.codex_contract_identity(
                    contract_id,
                    "gpt-5.6-luna"
                    if contract_id in runner.SOURCE_CONTRACTS
                    else "gpt-5.6-terra",
                    "medium",
                    runner.CODEX_CLI_PROFILE,
                ),
            }
        )
    manifest = root / "manifest.yml"
    write_yaml(
        manifest,
        {
            "schema_version": "1",
            "evaluation_id": "test-run",
            "code_commit": runner._git_commit(require_clean=False),
            "controls": runner.CONTROLS,
            "cases": cases,
        },
    )
    return manifest


class FakeReader:
    def __init__(
        self, calls: list[str], failures: dict[str, list[BaseException]]
    ) -> None:
        self.calls = calls
        self.failures = failures

    def _call(self, contract_id: str) -> dict[str, object]:
        self.calls.append(contract_id)
        failures = self.failures.get(contract_id, [])
        if failures:
            raise failures.pop(0)
        return {"contract_id": contract_id, "valid": True}


def _fake_method(contract_id: str):
    def method(self: FakeReader, *_args, **_kwargs):
        return self._call(contract_id)

    return method


for _contract_id, _method_name in runner.CONTRACT_METHODS.items():
    setattr(FakeReader, _method_name, _fake_method(_contract_id))


def _factory(calls: list[str], failures: dict[str, list[BaseException]]):
    def make(**_kwargs):
        return FakeReader(calls, failures)

    return make


def _recording_guard(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[object, ...]],
) -> list[object]:
    guards: list[object] = []

    class Guard:
        @classmethod
        def start(cls, authorization_path, authorization_sha256, **kwargs):
            guard = cls()
            guards.append(guard)
            events.append(
                (
                    "start",
                    authorization_path,
                    authorization_sha256,
                    kwargs["stage"],
                    kwargs["resume_reason"],
                    kwargs["evaluation_id"],
                    kwargs["run_id"],
                    kwargs["source_attempt_limit"],
                    kwargs["relationship_attempt_limit"],
                    kwargs["total_attempt_limit"],
                )
            )
            return guard

        @contextmanager
        def job(self, job_id):
            events.append(("job", job_id))
            yield

        def finish(self, state, *, reason=""):
            events.append(("finish", state, reason))

    monkeypatch.setattr(runner, "CodexCampaignGuard", Guard)
    return guards


def _usage(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(runner, "reset_provider_completion", lambda: None)

    def completion():
        contract_id = calls[-1]
        model = (
            "gpt-5.6-luna"
            if contract_id in runner.SOURCE_CONTRACTS
            else "gpt-5.6-terra"
        )
        return {
            **runner.codex_contract_identity(
                contract_id, model, "medium", runner.CODEX_CLI_PROFILE
            ),
            **({"request_schema_hash": runner.sha256_text(json.dumps(
                runner._codex_json_schema(contract_id, pair_job_ids=("job-smoke",)),
                sort_keys=True,
            ))} if contract_id == "relationship_adjudication" else {}),
            "codex_cli_version": runner.CODEX_CLI_PROFILE,
            "finish_reason": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(
        runner,
        "current_provider_completion",
        completion,
    )


def test_live_commit_identity_refuses_dirty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(arguments, **_kwargs):
        return runner.subprocess.CompletedProcess(
            arguments,
            0,
            stdout="a" * 40 + "\n"
            if arguments[1] == "rev-parse"
            else " M tools/v030_codex_provider_eval.py\n",
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="clean code commit"):
        runner._git_commit()


def test_evaluation_refuses_code_imported_from_another_checkout(
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
        runner.run_evaluation(
            manifest_path=tmp_path / "manifest.yml",
            manifest_sha256="0" * 64,
            workspace=tmp_path / "workspace",
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
    completion = {
        **runner.codex_contract_identity(
            "source_bundle",
            "gpt-5.6-luna",
            "medium",
            runner.CODEX_CLI_PROFILE,
        ),
        "codex_cli_version": runner.CODEX_CLI_PROFILE,
        "finish_reason": "turn.completed",
        "usage": usage,
    }
    with pytest.raises(ValueError, match="usage is missing"):
        runner._validate_completion(
            completion,
            contract_id="source_bundle",
            model="gpt-5.6-luna",
            effort="medium",
        )


def test_completion_requires_pinned_codex_cli_profile() -> None:
    completion = {
        **runner.codex_contract_identity(
            "source_bundle", "gpt-5.6-luna", "medium", "0.152.1"
        ),
        "codex_cli_version": "0.152.1",
        "finish_reason": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    runner._validate_completion(
        completion,
        contract_id="source_bundle",
        model="gpt-5.6-luna",
        effort="medium",
    )
    legacy = {
        **runner.codex_contract_identity(
            "source_bundle", "gpt-5.6-luna", "medium", "0.145.0"
        ),
        "codex_cli_version": "0.145.0",
        "finish_reason": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    with pytest.raises(ValueError, match="cli_profile mismatch"):
        runner._validate_completion(
            legacy,
            contract_id="source_bundle",
            model="gpt-5.6-luna",
            effort="medium",
        )


@pytest.mark.parametrize("schema_hash", [None, "0" * 64])
def test_completion_rejects_unbound_relationship_schema(schema_hash: str | None) -> None:
    completion = {
        **runner.codex_contract_identity(
            "relationship_adjudication", "gpt-5.6-terra", "medium", runner.CODEX_CLI_PROFILE
        ),
        "codex_cli_version": runner.CODEX_CLI_PROFILE,
        "finish_reason": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    if schema_hash is not None:
        completion["request_schema_hash"] = schema_hash
    with pytest.raises(ValueError, match="request_schema_hash mismatch"):
        runner._validate_completion(
            completion, contract_id="relationship_adjudication",
            model="gpt-5.6-terra", effort="medium", pair_job_ids=("job-smoke",),
        )


def test_hash_refusal_happens_before_reader_construction(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path / "private")
    constructed = False

    def forbidden(**_kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("reader must not be constructed")

    with pytest.raises(PermissionError, match="execute=True"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=tmp_path / "workspace",
            reader_factory=forbidden,
        )
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256="0" * 64,
            workspace=tmp_path / "workspace",
            execute=True,
            require_clean=False,
            reader_factory=forbidden,
        )
    assert not constructed

    first_payload = tmp_path / "private/cases/source_bundle.yml"
    first_payload.write_text(first_payload.read_text() + "# changed\n")
    with pytest.raises(ValueError, match="payload SHA-256 mismatch"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=tmp_path / "workspace",
            execute=True,
            require_clean=False,
            reader_factory=forbidden,
        )
    assert not constructed


def test_production_smoke_requires_frozen_authorization(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path / "private")
    with pytest.raises(ValueError, match="frozen authorization"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=tmp_path / "workspace",
            execute=True,
        )

@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_contract_inventory_refusal_precedes_calls(
    tmp_path: Path, mutation: str
) -> None:
    manifest = _write_manifest(tmp_path / mutation)
    payload = read_yaml(manifest)
    if mutation == "unknown":
        payload["cases"][0]["contract_id"] = "unknown_contract"
    else:
        payload["cases"].pop()
    write_yaml(manifest, payload)

    with pytest.raises(ValueError):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=tmp_path / "workspace",
            execute=True,
            require_clean=False,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
        )


def test_dispatches_all_contracts_and_writes_passing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    events: list[tuple[object, ...]] = []
    guards = _recording_guard(monkeypatch, events)
    reader_configs: list[dict[str, object]] = []
    readers: list[FakeReader] = []
    _usage(monkeypatch, calls)

    def factory(**kwargs):
        reader_configs.append(kwargs)
        reader = FakeReader(calls, {})
        reader._ensure_codex_preflight = lambda: events.append(
            ("preflight", kwargs["model"])
        )
        readers.append(reader)
        return reader

    report_path, report = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=manifest.parent / "authorization.json",
        authorization_sha256="a" * 64,
        workspace=tmp_path / "workspace",
        execute=True,
        require_clean=False,
        reader_factory=factory,
    )

    assert calls == list(runner.CONTRACTS)
    assert [event[0] for event in events[:3]] == [
        "preflight",
        "preflight",
        "start",
    ]
    assert events[2][3:] == (
        "final_head_contract_smoke",
        None,
        "test-run",
        "test-run",
        3,
        9,
        12,
    )
    assert [event[1] for event in events if event[0] == "job"] == list(
        runner.CONTRACTS
    )
    assert events[-1] == ("finish", "passed", "")
    assert all(reader.attempt_guard is guards[0] for reader in readers)
    assert reader_configs == [
        {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "allow_cloud": True,
            "request_deadline": 600.0,
        },
        {
            "model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
            "allow_cloud": True,
            "request_deadline": 600.0,
        },
    ]
    assert report_path == (
        tmp_path
        / "workspace/11_state/evaluations/codex-provider/test-run.yml"
    ).resolve()
    assert read_yaml(report_path) == report
    assert report["status"] == "passed"
    assert report["attempt_count"] == 12
    assert report["maximum_attempts"] == 12
    assert report["retry_call_ceiling"] == 0
    assert report["first_pass_valid_count"] == 12
    assert report["retry_count"] == 0
    ledger = manifest.parent / ".v030-attempts/test-run.jsonl"
    assert sum(
        '"record": "reserved"' in line for line in ledger.read_text().splitlines()
    ) == 12
    assert ledger.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="consumed attempts"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=tmp_path / "different-workspace",
            execute=True,
            require_clean=False,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
        )


@pytest.mark.parametrize(
    ("failure", "paused_by"),
    [
        (
            ProviderTransportError(
                "temporary Authorization: Bearer "
                + "synthetic-authorization-value",
                transport_kind="test",
            ),
            "",
        ),
        (ProviderTimeout("late"), "timeout"),
    ],
)
def test_any_failure_stops_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    paused_by: str,
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    failures = {"source_bundle": [failure]}
    _usage(monkeypatch, calls)

    report_path, report = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        workspace=tmp_path / "workspace",
        execute=True,
        require_clean=False,
        reader_factory=_factory(calls, failures),
    )

    assert calls == ["source_bundle"]
    assert report["status"] == ("paused" if paused_by else "failed")
    assert report["attempt_count"] == 1
    assert report["retry_count"] == 0
    assert report["first_pass_valid_count"] == 0
    assert report["paused_by"] == paused_by
    assert report["cases"][0]["status"] == "failed"
    assert len(report["cases"][0]["attempts"]) == 1
    assert read_yaml(report_path) == report
    assert "synthetic-authorization-value" not in str(report)


@pytest.mark.parametrize(
    ("message", "expected_reason", "expected_tool_attempts"),
    [
        (
            "Codex emitted unexpected event: turn.delta",
            "unexpected_event:turn.delta",
            0,
        ),
        (
            "Codex attempted tool event: command_execution",
            "disallowed_item:command_execution",
            1,
        ),
        (
            "Codex attempted tool event: todo_list",
            "disallowed_item:todo_list",
            1,
        ),
        (
            "Codex attempted tool event: command_execution "
            "Authorization: Bearer " + "synthetic-isolation-secret",
            "unknown",
            0,
        ),
    ],
)
def test_isolation_failure_persists_only_coarse_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_reason: str,
    expected_tool_attempts: int,
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    _usage(monkeypatch, calls)

    report_path, report = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        workspace=tmp_path / "workspace",
        execute=True,
        require_clean=False,
        reader_factory=_factory(
            calls, {"source_bundle": [ProviderIsolationFailure(message)]}
        ),
    )

    attempt = report["cases"][0]["attempts"][0]
    assert attempt["isolation_reason"] == expected_reason
    assert report["tool_attempt_count"] == expected_tool_attempts
    assert "error" not in attempt
    serialized = report_path.read_text()
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
def test_error_item_is_reported_as_coarse_non_tool_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    expected_reason: str,
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    _usage(monkeypatch, calls)

    _path, report = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        workspace=tmp_path / "workspace",
        execute=True,
        require_clean=False,
        reader_factory=_factory(
            calls,
            {
                "source_bundle": [
                    ProviderError(f"Codex CLI emitted error item: {suffix}")
                ]
            },
        ),
    )

    attempt = report["cases"][0]["attempts"][0]
    assert attempt["error_reason"] == expected_reason
    assert report["tool_attempt_count"] == 0
    assert "isolation_reason" not in attempt
    assert "error" not in attempt
    assert "synthetic-secret" not in _path.read_text()


def test_paused_smoke_resumes_only_unspent_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    events: list[tuple[object, ...]] = []
    _recording_guard(monkeypatch, events)
    _usage(monkeypatch, calls)
    workspace = tmp_path / "workspace"
    _path, paused = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=manifest.parent / "authorization.json",
        authorization_sha256="a" * 64,
        workspace=workspace,
        execute=True,
        require_clean=False,
        reader_factory=_factory(
            calls, {"source_bundle": [ProviderTimeout("paused")]}
        ),
    )

    assert paused["status"] == "paused"
    assert paused["attempt_count"] == 1
    _path, resumed = runner.run_evaluation(
        manifest_path=manifest,
        manifest_sha256=sha256_file(manifest),
        authorization_path=manifest.parent / "authorization.json",
        authorization_sha256="a" * 64,
        workspace=workspace,
        execute=True,
        resume=True,
        require_clean=False,
        reader_factory=_factory(calls, {}),
    )

    assert calls == list(runner.CONTRACTS)
    assert resumed["status"] == "paused"
    assert resumed["attempt_count"] == 12
    assert resumed["resume_count"] == 1
    assert resumed["first_pass_valid_count"] == 11
    assert [event[4] for event in events if event[0] == "start"] == [None, "timeout"]
    assert all(
        event[5:] == ("test-run", "test-run", 3, 9, 12)
        for event in events
        if event[0] == "start"
    )
    with pytest.raises(ValueError, match="no unspent attempts"):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            workspace=workspace,
            execute=True,
            resume=True,
            require_clean=False,
            reader_factory=lambda **_kwargs: pytest.fail("unexpected reader"),
        )


def test_keyboard_interrupt_writes_resumable_pause_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _write_manifest(tmp_path / "private")
    calls: list[str] = []
    events: list[tuple[object, ...]] = []
    _recording_guard(monkeypatch, events)
    _usage(monkeypatch, calls)
    workspace = tmp_path / "workspace"

    with pytest.raises(KeyboardInterrupt):
        runner.run_evaluation(
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            authorization_path=manifest.parent / "authorization.json",
            authorization_sha256="a" * 64,
            workspace=workspace,
            execute=True,
            require_clean=False,
            reader_factory=_factory(calls, {"source_bundle": [KeyboardInterrupt()]}),
        )

    report = read_yaml(
        workspace / "11_state/evaluations/codex-provider/test-run.yml"
    )
    assert report["status"] == "paused"
    assert report["paused_by"] == "interruption"
    assert report["attempt_count"] == 1
    assert events[-1] == ("finish", "paused", "interruption")
