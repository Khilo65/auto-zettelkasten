from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.api import (
    _provider_check,
    build_map,
    initialize_workspace,
    resume_map,
    run_map,
)
from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
from auto_zettelkasten.cli import main
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.models import (
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
)
from auto_zettelkasten.literature import (
    _PROVIDER_INPUT_DEPENDENCY_COMPONENTS,
    _bounded_provider_futures,
    _provider_worker_count,
    _same_provider_inputs,
)
from auto_zettelkasten.pipeline import (
    _ProfileProviderBudget,
    _profile_dependency_policy,
    _provider_call_with_transport_retry,
    _source_worker_count,
    _transport_retryable,
    run_pipeline,
)
from auto_zettelkasten.readers import (
    CODEX_CLI_PROFILES,
    CODEX_CONTRACT_RESERVATIONS,
    CODEX_OUTPUT_CONTRACTS,
    CodexReader,
    ProviderError,
    ProviderInterrupted,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTimeout,
    ProviderTransportError,
    ProviderUnsupportedAttachment,
    _CODEX_TOOL_FEATURE_ARGUMENTS,
    _CODEX_TRANSPORT_INSTRUCTIONS,
    _codex_error_item_category,
    _codex_failure,
    _codex_json_schema,
    _redact_codex_diagnostic,
    cancel_active_provider_responses,
    codex_contract_for_stage,
    codex_contract_identity,
    codex_preflight_status,
    codex_stage_identity,
)
from conftest import SECTION_KEYS, FakeZotero, fake_codex_preflight


def test_codex_request_roles_and_existing_provider_serialization(tmp_path: Path) -> None:
    existing = MapRequest(tmp_path).to_dict()
    assert "literature_model" not in existing
    assert "reasoning_effort" not in existing

    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        reasoning_effort="medium",
    )
    assert request.to_dict()["literature_model"] == "gpt-5.6-terra"
    with pytest.raises(ValueError, match="requires literature_model"):
        MapRequest(tmp_path, provider="codex", model="gpt-5.6-luna")
    MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
    )
    with pytest.raises(ValueError, match="supported only by Codex"):
        MapRequest(tmp_path, reasoning_effort="medium")
    MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        provider_concurrency=32,
    )
    with pytest.raises(ValueError, match="between 1 and 32"):
        MapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-luna",
            literature_model="gpt-5.6-terra",
            provider_concurrency=33,
        )
    LiteratureMapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        provider_concurrency=32,
    )
    with pytest.raises(ValueError, match="between 1 and 32"):
        LiteratureMapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-terra",
            provider_concurrency=33,
        )


def test_codex_contract_capabilities_and_typed_retry_policy() -> None:
    reader = CodexReader("gpt-5.6-luna")
    assert reader.capabilities["context_window_tokens"] == 272_000
    assert reader.capabilities["max_output_tokens"] == 6_000
    assert reader.capabilities["supported_output_tokens"] == 128_000
    assert set(CODEX_OUTPUT_CONTRACTS) == set(CODEX_CONTRACT_RESERVATIONS)
    assert CODEX_CONTRACT_RESERVATIONS == {
        "source_bundle": 32_768,
        "evidence_profile": 16_384,
        "literature_family_plan": 49_152,
        "relationship_candidate_selection": 16_384,
        "relationship_adjudication": 16_384,
        "cluster_plan": 24_576,
        "cluster_synthesis": 40_960,
        "chunk_evidence": 2_048,
        "relationship_shard_selection": 4_096,
        "bridge_shard_selection": 12_288,
        "cluster_proposal": 24_576,
        "gap_adjudication": 12_288,
    }
    assert CODEX_OUTPUT_CONTRACTS["source_bundle"]["required"] == [
        "analysis_sections",
        "compact_profile",
        "evidence_anchors",
        "literature_positions",
        "observed_bibliographic_identity",
    ]
    assert "source_contributions" not in CODEX_OUTPUT_CONTRACTS[
        "cluster_synthesis"
    ]["required"]
    assert reader._reserved_output_tokens("source_bundle", 1) == 32_768
    assert not _transport_retryable(ProviderTimeout("timed out"))
    assert not _transport_retryable(ProviderQuotaExhausted("quota"))
    assert codex_contract_for_stage("cluster_reconciliation") == "cluster_proposal"
    assert isinstance(_codex_failure("rate limit exceeded"), ProviderTransportError)
    assert isinstance(_codex_failure("HTTP 429"), ProviderTransportError)
    assert isinstance(_codex_failure("503 Service Unavailable"), ProviderTransportError)
    assert isinstance(
        _codex_failure(
            "image inputs are not supported: /tmp/pytest-500/page.png",
            attachment_paths=("/tmp/pytest-500/page.png",),
        ),
        ProviderUnsupportedAttachment,
    )


def test_codex_evidence_profile_contract_remains_dormant_in_production(
    tmp_path: Path,
) -> None:
    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
    )
    _policy, route, identity = _profile_dependency_policy(
        request, CodexReader("gpt-5.6-terra"), analytical=True
    )
    assert route == "deterministic"
    assert identity == "auto_zettelkasten.profiles.deterministic_profile:v1"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProviderQuotaExhausted("quota"), "quota"),
        (ProviderTimeout("timeout"), "timeout"),
        (ProviderInterrupted("interrupted"), "interruption"),
        (ProviderTransportError("transport", transport_kind="test"), "transport"),
    ],
)
def test_codex_source_attempt_ledger_preserves_typed_failure_class(
    tmp_path: Path, failure: ProviderError, expected: str
) -> None:
    budget = _ProfileProviderBudget(
        tmp_path / "provider_usage.yml", 1, provider="codex", model="gpt-5.6-luna"
    )
    attempt_id = budget.reserve("source_bundle_direct", "A", "fingerprint")
    budget.finish(attempt_id, status="failed", failure=failure)
    assert budget.attempts[0]["failure_class"] == expected


def test_codex_cancelled_source_call_is_interruption(tmp_path: Path) -> None:
    budget = _ProfileProviderBudget(
        tmp_path / "provider_usage.yml", 1, provider="codex", model="gpt-5.6-luna"
    )
    with pytest.raises(ProviderInterrupted):
        _provider_call_with_transport_retry(
            budget,
            "source_bundle_direct",
            "A",
            "fingerprint",
            lambda: None,
            cancelled=lambda: True,
        )
    assert budget.cumulative_calls == 0


@pytest.mark.parametrize(
    "failure_type",
    [ProviderQuotaExhausted, ProviderTimeout, ProviderInterrupted],
)
def test_codex_source_pause_resumes_from_frozen_content_without_terminal_checkpoint(
    tmp_path: Path,
    sample_items: list[dict[str, object]],
    failure_type: type[ProviderError],
) -> None:
    class PauseOnceReader:
        name = "codex"
        model = "gpt-5.6-luna"
        is_cloud = True

        def __init__(self) -> None:
            self.calls = 0
            self.pause = True

        def read_source(self, text, metadata, question=None):
            del text, metadata, question
            self.calls += 1
            if self.pause:
                self.pause = False
                raise failure_type("synthetic pause")
            return {
                key: f"Source-grounded {key.replace('_', ' ')}; see page 1."
                for key in SECTION_KEYS
            }

    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        allow_cloud=True,
        parallel=1,
        provider_concurrency=1,
        literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
    )
    reader = PauseOnceReader()

    paused = run_pipeline(
        request,
        client=FakeZotero(sample_items[:1]),
        reader=reader,
        run_id="typed-source-pause",
    )

    item_root = (
        tmp_path
        / "11_state"
        / "runs"
        / "typed-source-pause"
        / "items"
        / "ITEMA"
    )
    assert paused.status == "partial"
    assert paused.pending_count == 1
    assert not (item_root / "prepared_result.yml").exists()
    assert not (item_root / "source_failure.yml").exists()
    usage = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "typed-source-pause"
        / "literature"
        / "profiles"
        / "provider_usage.yml"
    )
    assert usage["attempts"][-1]["failure_class"] == {
        ProviderQuotaExhausted: "quota",
        ProviderTimeout: "timeout",
        ProviderInterrupted: "interruption",
    }[failure_type]

    resumed = run_pipeline(
        request,
        client=FakeZotero(sample_items[:1]),
        reader=reader,
        run_id="typed-source-pause",
        resume=True,
    )

    assert resumed.status == "completed"
    assert resumed.validated_note_count == 1
    assert reader.calls == 2


def test_codex_contract_schemas_are_strict_recursively() -> None:
    def visit(schema: object) -> None:
        assert isinstance(schema, dict)
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            properties = schema.get("properties")
            assert isinstance(properties, dict)
            assert set(schema.get("required", [])) == set(properties)
            for value in properties.values():
                visit(value)
        if schema.get("type") == "array":
            assert "items" in schema
            visit(schema["items"])
        for value in schema.get("anyOf", []):
            visit(value)

    for contract_id in CODEX_OUTPUT_CONTRACTS:
        visit(_codex_json_schema(contract_id))


def test_codex_contract_identity_covers_tool_disable_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = codex_contract_identity("chunk_evidence", "gpt-5.6-luna", "medium")
    profile = CODEX_CLI_PROFILES["0.145.0"]
    monkeypatch.setitem(
        CODEX_CLI_PROFILES,
        "0.145.0",
        {
            **profile,
            "tool_features": frozenset((*profile["tool_features"], "future_tool")),
        },
    )
    after = codex_contract_identity("chunk_evidence", "gpt-5.6-luna", "medium")
    assert before["feature_manifest_hash"] != after["feature_manifest_hash"]


def test_codex_contract_identity_covers_transport_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = codex_contract_identity("chunk_evidence", "gpt-5.6-luna", "medium")
    monkeypatch.setattr(
        "auto_zettelkasten.readers._CODEX_TRANSPORT_INSTRUCTIONS",
        "changed",
    )
    after = codex_contract_identity("chunk_evidence", "gpt-5.6-luna", "medium")
    assert before["transport_instructions_hash"] != after[
        "transport_instructions_hash"
    ]


def test_codex_stage_identity_includes_transitive_contract_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adjudication_before = codex_stage_identity(
        "relationship_adjudication", "gpt-5.6-terra", "medium"
    )
    gaps_before = codex_stage_identity(
        "gap_adjudication", "gpt-5.6-terra", "medium"
    )
    monkeypatch.setitem(
        CODEX_OUTPUT_CONTRACTS,
        "literature_family_plan",
        {**CODEX_OUTPUT_CONTRACTS["literature_family_plan"], "title": "changed"},
    )
    assert codex_stage_identity(
        "relationship_adjudication", "gpt-5.6-terra", "medium"
    ) != adjudication_before
    assert codex_stage_identity(
        "gap_adjudication", "gpt-5.6-terra", "medium"
    ) != gaps_before


def test_provider_response_reuse_requires_same_codex_execution_identity() -> None:
    prior = {
        component: "same" for component in _PROVIDER_INPUT_DEPENDENCY_COMPONENTS
    }
    current = dict(prior)
    current["provider_execution_identity"] = "changed"
    assert not _same_provider_inputs(
        {"dependency_component_hashes": prior},
        current,
        stage="cluster_synthesis",
    )


def test_codex_auto_concurrency_defaults_to_sixteen(
    tmp_path: Path,
) -> None:
    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
    )
    reader = CodexReader("gpt-5.6-luna")
    assert request.provider_concurrency == "auto"
    assert _source_worker_count(reader, request, 20) == 16
    assert _provider_worker_count(
        LiteratureMapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-terra",
            provider_concurrency="auto",
        ),
        20,
    ) == 16


def test_codex_preflight_uses_one_sanitized_executable_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_executable",
        lambda _environment=None: executable,
    )
    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_model_catalog",
        lambda _environment=None: {
            model: {
                **values,
                "supported_reasoning_levels": [
                    {"effort": effort}
                    for effort in values["reasoning_efforts"]
                ],
            }
            for model, values in CODEX_CLI_PROFILES["0.145.0"]["models"].items()
        },
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    sensitive_names = {
        "OPENAI_API_KEY": "synthetic-openai-key",
        "ACCESS_TOKEN": "synthetic-access-token",
        "REFRESH_TOKEN": "synthetic-refresh-token",
        "CHATGPT_ACCOUNT_ID": "synthetic-account",
        "HTTP_PROXY": "http://synthetic-proxy.invalid",
    }
    for name, value in sensitive_names.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[list[str], dict[str, str]]] = []
    expected_features = {
        name: {
            **value,
            "default": False
            if name in CODEX_CLI_PROFILES["0.145.0"]["tool_features"]
            else value["default"],
        }
        for name, value in CODEX_CLI_PROFILES["0.145.0"]["features"].items()
    }
    feature_output = "\n".join(
        f"{name}  {value['maturity']}  {str(value['default']).lower()}"
        for name, value in expected_features.items()
    )

    def run(args: list[str], **kwargs: object) -> SimpleNamespace:
        environment = dict(kwargs["env"])  # type: ignore[arg-type]
        calls.append((args, environment))
        if args[-1] == "--version":
            output = "codex-cli 0.145.0"
        elif args[-2:] == ["features", "list"]:
            output = feature_output
        else:
            output = "Logged in using ChatGPT"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    status = codex_preflight_status("gpt-5.6-luna")
    assert status["auth_method"] == "chatgpt"
    assert status["auth_status"] == "authenticated"
    assert status["reasoning_effort_compatibility"] is True
    assert len(calls) == 3
    assert calls[0][1] == calls[2][1]
    feature_environment = dict(calls[1][1])
    feature_environment.pop("CODEX_HOME")
    base_environment = dict(calls[0][1])
    base_codex_home = base_environment.pop("CODEX_HOME", None)
    assert feature_environment == base_environment
    assert calls[1][1]["CODEX_HOME"] != base_codex_home
    assert calls[1][0][1:-2] == list(_CODEX_TOOL_FEATURE_ARGUMENTS)
    assert all(args[0] == str(executable) for args, _ in calls)
    assert all("DEEPSEEK_API_KEY" not in environment for _, environment in calls)
    assert all(
        not sensitive_names.keys() & environment.keys()
        for _, environment in calls
    )
    feature_output += "\nfuture_tool  stable  true"
    with pytest.raises(ProviderIsolationFailure, match="feature manifest mismatch"):
        codex_preflight_status("gpt-5.6-luna")

    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_model_catalog",
        lambda _environment=None: {
            "gpt-5.6-luna": {
                **CODEX_CLI_PROFILES["0.145.0"]["models"]["gpt-5.6-luna"],
                "supported_reasoning_levels": [{"effort": "high"}],
            }
        },
    )
    feature_output = "\n".join(
        f"{name}  {value['maturity']}  {str(value['default']).lower()}"
        for name, value in expected_features.items()
    )
    with pytest.raises(ProviderError, match="does not support medium effort"):
        codex_preflight_status("gpt-5.6-luna")


def test_codex_rejects_credential_root_inside_workspace_before_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(workspace / ".codex"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Codex CLI must not run"),
    )

    with pytest.raises(ProviderIsolationFailure, match="protected project"):
        codex_preflight_status(
            "gpt-5.6-luna",
            forbidden_credential_roots=(workspace,),
        )


def test_codex_rejects_credential_root_inside_an_unrelated_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "other-repository"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(repository / "private-codex-state"))

    with pytest.raises(ProviderIsolationFailure, match="protected project"):
        codex_preflight_status("gpt-5.6-luna")


def test_codex_rejects_temporary_call_directory_under_credential_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential_root = tmp_path / "synthetic-codex-state"
    call_root = credential_root / "temporary-call"
    call_root.mkdir(parents=True)

    class FixedTemporaryDirectory:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> str:
            return str(call_root)

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "auto_zettelkasten.readers.tempfile.TemporaryDirectory",
        FixedTemporaryDirectory,
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex process must not start"),
    )
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = {
        "executable": "/synthetic/codex",
        "_environment": {"PATH": "/synthetic/bin"},
        "_credential_root": str(credential_root),
    }

    with pytest.raises(ProviderIsolationFailure, match="temporary call"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


@pytest.mark.parametrize(("name", "linked"), [("auth.json", True), ("models_cache.json", False)])
def test_codex_rejects_unisolatable_child_state_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    linked: bool,
) -> None:
    preflight = fake_codex_preflight(tmp_path, "/synthetic/codex")
    source = Path(str(preflight["_credential_root"])) / name
    source.unlink()
    if linked:
        target = tmp_path / "outside-auth.json"
        target.write_text("{}\n", encoding="utf-8")
        source.symlink_to(target)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex process must not start"),
    )
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = preflight

    with pytest.raises(ProviderIsolationFailure, match="could not be isolated"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_diagnostics_redact_credentials_and_account_identity(
    tmp_path: Path,
) -> None:
    credential_root = tmp_path / "synthetic-codex-home"
    message = "\n".join(
        (
            "quota exhausted",
            "Authorization: Bearer " + "synthetic-authorization-value",
            "Cookie: session=synthetic-cookie-value",
            "account_id=synthetic-account-id",
            "workspace-id: synthetic-workspace-id",
            "synthetic-person@example.invalid",
            str(credential_root / "auth.json"),
            "sk-" + "SYNTHETICINVALID0000",
            "eyJsyntheticA." + "eyJsyntheticB.syntheticC",
        )
    )

    redacted = _redact_codex_diagnostic(message, credential_root)
    failure = _codex_failure(message, credential_root)

    assert isinstance(failure, ProviderQuotaExhausted)
    assert str(failure) == redacted
    for sensitive in (
        "synthetic-authorization-value",
        "synthetic-cookie-value",
        "synthetic-account-id",
        "synthetic-workspace-id",
        "synthetic-person",
        str(credential_root),
        "sk-" + "SYNTHETICINVALID0000",
        "eyJ" + "syntheticA",
    ):
        assert sensitive not in redacted


def test_codex_doctor_checks_both_requested_model_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def preflight(
        model: str, effort: str, additional_models: tuple[str, ...]
    ) -> dict[str, object]:
        captured.update(
            model=model, effort=effort, additional_models=additional_models
        )
        return {
            "status": "configured",
            "provider": "codex",
            "quota": "unknown",
            "auth_method": "chatgpt",
            "auth_status": "authenticated",
            "account_email": "synthetic-person@example.invalid",
            "_credential_root": "/synthetic/private/root",
            "_environment": {"HOME": "/synthetic/private"},
        }

    monkeypatch.setattr("auto_zettelkasten.api.codex_preflight_status", preflight)
    status = _provider_check(
        "codex",
        "gpt-5.6-luna",
        {
            "literature_model": "gpt-5.6-terra",
            "reasoning_effort": "high",
        },
    )
    assert status["status"] == "configured"
    assert status["quota"] == "unknown"
    assert status["auth_status"] == "authenticated"
    assert "account_email" not in status
    assert "credential" not in json.dumps(status)
    assert "synthetic-person" not in json.dumps(status)
    assert captured == {
        "model": "gpt-5.6-luna",
        "effort": "high",
        "additional_models": ("gpt-5.6-terra",),
    }
    assert _provider_check(
        "codex",
        "gpt-5.6-luna",
        {"literature_mapping": {"synthesis_enabled": False}},
    )["status"] == "configured"


@pytest.mark.parametrize(
    "reason",
    [
        "unsupported Codex CLI version: 0.144.0",
        "Codex CLI must be logged in using ChatGPT",
    ],
)
def test_codex_doctor_reports_version_and_auth_failures(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ProviderError(reason)

    monkeypatch.setattr("auto_zettelkasten.api.codex_preflight_status", fail)
    status = _provider_check(
        "codex",
        "gpt-5.6-luna",
        {"literature_model": "gpt-5.6-terra"},
    )
    assert status["status"] == "unavailable"
    assert reason in status["reason"]


def test_combined_codex_replay_without_new_calls_skips_live_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def preflight(
        model: str,
        _effort: str,
        additional_models: tuple[str, ...],
        *,
        forbidden_credential_roots: tuple[Path, ...],
    ) -> dict[str, object]:
        assert forbidden_credential_roots[0] == tmp_path.resolve()
        assert forbidden_credential_roots[1].is_relative_to(
            tmp_path / "11_state" / "runs"
        )
        calls.append((model, additional_models))
        return {"executable": "/unused/codex"}

    monkeypatch.setattr("auto_zettelkasten.pipeline.codex_preflight_status", preflight)
    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        allow_cloud=True,
    )
    source_reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    literature_reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    report = run_pipeline(
        request,
        client=FakeZotero([]),
        reader=source_reader,
        literature_reasoner=literature_reader,
    )
    assert report.status == "completed"
    assert calls == []
    source_reader._ensure_codex_preflight()
    literature_reader._ensure_codex_preflight()
    assert calls == [("gpt-5.6-luna", ("gpt-5.6-terra",))]


def test_unsupported_codex_public_method_fails_before_cli_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("preflight must not run"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex process must not start"),
    )
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    with pytest.raises(ProviderError, match="registered output contract"):
        reader.map_debates(
            [],
            LiteratureMapRequest(
                tmp_path,
                provider="codex",
                model="gpt-5.6-terra",
            ),
        )


def test_codex_cli_forwards_explicit_source_literature_and_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    initialize_workspace(tmp_path)
    captured: dict[str, object] = {}

    def run_map(request: MapRequest, **_kwargs: object) -> SimpleNamespace:
        captured["request"] = request
        return SimpleNamespace(to_dict=lambda: {"status": "completed"})

    monkeypatch.setattr("auto_zettelkasten.cli.run_map", run_map)
    assert (
        main(
            [
                "map",
                "--workspace",
                str(tmp_path),
                "--provider",
                "codex",
                "--model",
                "gpt-5.6-luna",
                "--literature-model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "high",
                "--allow-cloud",
            ]
        )
        == 0
    )
    capsys.readouterr()
    request = captured["request"]
    assert isinstance(request, MapRequest)
    assert request.model == "gpt-5.6-luna"
    assert request.literature_model == "gpt-5.6-terra"
    assert request.reasoning_effort == "high"


def _fake_codex(
    path: Path,
    capture_path: Path,
    *,
    item_type: str = "agent_message",
    item_message: object = "stream lagged; dropped 1 events",
    event_type: str = "item.completed",
    sleep_seconds: float = 0,
    followup_item_type: str = "agent_message",
    null_item: bool = False,
    mutate_codex_home: bool = False,
    mutate_codex_auth: bool = False,
) -> None:
    response_text = (
        '{"summary":"ok","claims_and_findings":"ok",'
        '"statistical_context":"","methods_and_data":"ok",'
        '"limitations":"","locators":[]}'
    )
    item = {"type": item_type}
    if item_type == "error":
        item["message"] = item_message
    else:
        item["text"] = response_text
    event_item = None if null_item else item
    body = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
instruction_config = next(value for value in sys.argv if value.startswith("model_instructions_file="))
instruction_path = Path(json.loads(instruction_config.split("=", 1)[1]))
codex_home = Path(os.environ["CODEX_HOME"])
Path({str(capture_path)!r}).write_text(json.dumps({{"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir()), "codex_home": str(codex_home), "codex_home_entries": sorted(path.name for path in codex_home.iterdir()), "codex_home_modes": {{path.name: path.stat().st_mode & 0o777 for path in codex_home.iterdir()}}, "model_instructions_path": str(instruction_path), "model_instructions": instruction_path.read_text(), "model_instructions_mode": instruction_path.stat().st_mode & 0o777}}))
if {mutate_codex_home!r}:
    (codex_home / "models_cache.json").write_text("child mutation")
if {mutate_codex_auth!r}:
    (codex_home / "auth.json").write_text("child mutation")
time.sleep({sleep_seconds!r})
print(json.dumps({{"type": "thread.started"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}), flush=True)
print(json.dumps({{"type": {event_type!r}, "item": {event_item!r}}}), flush=True)
if {item_type!r} == "error":
    print(json.dumps({{"type": "item.completed", "item": {{"type": {followup_item_type!r}, "text": {response_text!r}}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}), flush=True)
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_codex_transport_is_sanitized_schema_bound_and_tool_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, mutate_codex_home=True)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(
        tmp_path, executable, {"PATH": "preflight-snapshot"}
    )
    credential_root = Path(str(reader._preflight["_credential_root"]))
    (credential_root / "config.toml").write_text("private = true\n", encoding="utf-8")
    system_skills = credential_root / "skills" / ".system"
    system_skills.mkdir(parents=True)
    (system_skills / ".codex-system-skills.marker").write_text(
        "private marker\n", encoding="utf-8"
    )

    def shared_state() -> dict[str, tuple[bytes, int, int]]:
        return {
            str(path.relative_to(credential_root)): (
                path.read_bytes(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in credential_root.rglob("*")
            if path.is_file()
        }

    before = shared_state()
    monkeypatch.setenv("PATH", "changed-after-preflight")
    value = reader._generate_with_reasoning(
        "private-system-instructions",
        "private-user-source",
        2_048,
        5,
        reasoning_effort="high",
        output_contract="chunk_evidence",
    )
    assert json.loads(value)["summary"] == "ok"
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert "--output-schema" in captured["argv"]
    assert 'forced_login_method="chatgpt"' in captured["argv"]
    assert "skills.bundled.enabled=false" in captured["argv"]
    assert "skills.include_instructions=false" in captured["argv"]
    assert "features.code_mode_host=false" in captured["argv"]
    assert captured["argv"][-len(_CODEX_TOOL_FEATURE_ARGUMENTS):] == list(
        _CODEX_TOOL_FEATURE_ARGUMENTS
    )
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"]["PATH"] == "preflight-snapshot"
    assert captured["codex_home_entries"] == ["auth.json", "models_cache.json"]
    assert captured["codex_home_modes"] == {
        "auth.json": 0o600,
        "models_cache.json": 0o600,
    }
    assert captured["env"]["CODEX_HOME"] == captured["codex_home"]
    assert Path(captured["env"]["HOME"]) / ".codex" == Path(
        captured["codex_home"]
    )
    assert Path(captured["codex_home"]) != credential_root
    assert not Path(captured["codex_home"]).exists()
    assert before == shared_state()
    assert captured["cwd_entries"] == []
    assert captured["model_instructions"] == _CODEX_TRANSPORT_INSTRUCTIONS
    assert captured["model_instructions_mode"] == 0o600
    assert "private-system-instructions" not in captured["model_instructions"]
    assert "private-user-source" not in captured["model_instructions"]
    instructions_path = Path(captured["model_instructions_path"])
    assert instructions_path.parent != Path(captured["cwd"])
    schema_path = Path(
        captured["argv"][captured["argv"].index("--output-schema") + 1]
    )
    assert schema_path.parent != Path(captured["cwd"])


@pytest.mark.parametrize(
    ("auth", "reason"),
    [
        (
            '{"auth_mode":"chatgpt","tokens":'
            '{"access_token":"e30.eyJleHAiOjF9."}}\n',
            "expires before",
        ),
        (
            '{"auth_mode":"chatgpt","tokens":{"access_token":1}}\n',
            "authentication is unusable",
        ),
    ],
)
def test_codex_rejects_unusable_child_auth_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auth: str,
    reason: str,
) -> None:
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, "/synthetic/codex")
    credential_root = Path(str(reader._preflight["_credential_root"]))
    (credential_root / "auth.json").write_text(
        auth,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex process must not start"),
    )

    with pytest.raises(ProviderError, match=reason):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_rejects_child_auth_rotation(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, mutate_codex_auth=True)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)

    with pytest.raises(ProviderIsolationFailure, match="authentication state"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


@pytest.mark.parametrize(
    "item_type",
    [
        "collab_tool_call",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "todo_list",
        "web_search",
        "computer_use",
        "image_generation",
        "tool_call",
    ],
)
def test_codex_rejects_every_tool_event_category(
    item_type: str, tmp_path: Path
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, item_type=item_type)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderIsolationFailure, match="tool event"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


@pytest.mark.parametrize(
    "message",
    [
        (
            "Skill descriptions were shortened to fit the skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        ),
        (
            "Skill descriptions were shortened to fit the 2% skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        ),
    ],
)
def test_codex_accepts_nonfatal_error_item_without_reporting_a_tool(
    tmp_path: Path, message: str
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(
        executable,
        capture,
        item_type="error",
        item_message=message,
    )
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    value = reader._generate_with_reasoning(
        "system",
        "user",
        2_048,
        5,
        reasoning_effort="medium",
        output_contract="chunk_evidence",
    )
    assert json.loads(value)["summary"] == "ok"


@pytest.mark.parametrize(
    "message",
    [
        "in-process app-server event stream lagged; dropped 1 events",
        "Model rerouted: gpt-5.6-luna -> another-model",
        "unrecognized private warning",
    ],
)
def test_codex_keeps_other_error_items_fail_closed(
    tmp_path: Path, message: str
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, item_type="error", item_message=message)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderError, match="CLI emitted error item"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_skill_warning_does_not_hide_a_following_tool_item(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(
        executable,
        capture,
        item_type="error",
        item_message=(
            "Skill descriptions were shortened to fit the skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        ),
        followup_item_type="command_execution",
    )
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderIsolationFailure, match="tool event"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_rejects_non_mapping_item_event(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, null_item=True)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderIsolationFailure, match="malformed item"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Skill descriptions were shortened to fit the skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest.",
            "skills_context_budget",
        ),
        ("Exceeded skills context budget; omitted 3 skills.", "unknown"),
        (
            "Model metadata for `gpt-test` not found. Defaulting to fallback metadata.",
            "model_catalog_fallback",
        ),
        (
            "Falling back from WebSockets to HTTPS transport.",
            "transport_fallback",
        ),
        ("unrecognized private warning", "unknown"),
    ],
)
def test_codex_error_items_use_only_fixed_categories(
    message: str, expected: str
) -> None:
    assert _codex_error_item_category(message) == expected


@pytest.mark.parametrize(
    ("event_type", "item_message"),
    [
        ("item.started", "stream lagged"),
        ("item.updated", "stream lagged"),
        ("item.completed", None),
    ],
)
def test_codex_rejects_error_item_outside_exact_completed_shape(
    tmp_path: Path,
    event_type: str,
    item_message: object,
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(
        executable,
        capture,
        item_type="error",
        item_message=item_message,
        event_type=event_type,
    )
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderIsolationFailure, match="unexpected item: error"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_rejects_unexpected_non_tool_item(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, item_type="synthetic_notice")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(
        ProviderIsolationFailure, match="unexpected item: synthetic_notice"
    ):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_rejects_unknown_lifecycle_event(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, event_type="turn.delta")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderIsolationFailure, match="unexpected event"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_unknown_contract_fails_before_process_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex process must not start"),
    )
    with pytest.raises(ProviderError, match="registered output contract"):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            5,
            reasoning_effort="medium",
        )


def test_codex_timeout_is_typed_and_not_immediately_retried(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, sleep_seconds=1)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with pytest.raises(ProviderTimeout):
        reader._generate_with_reasoning(
            "system",
            "user",
            2_048,
            0.05,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


def test_codex_external_cancellation_is_typed(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, sleep_seconds=5)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            reader._generate_with_reasoning,
            "system",
            "user",
            2_048,
            10,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )
        deadline = time.monotonic() + 2
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert capture.exists()
        assert cancel_active_provider_responses() == 1
        with pytest.raises(ProviderInterrupted):
            future.result()


def test_bounded_provider_submission_stops_replenishing_after_quota() -> None:
    stop = threading.Event()
    submitted: list[int] = []

    def run(value: int) -> int:
        if value == 1:
            stop.set()
        else:
            stop.wait(timeout=1)
        return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future, _ in _bounded_provider_futures(
                executor,
                list(range(8)),
                lambda pool, value: (
                    submitted.append(value) or pool.submit(run, value)
                ),
                workers=2,
                stop_event=stop,
            )
        ]
    assert sorted(results) == [0, 1]
    assert submitted == [0, 1]


def test_standalone_codex_build_map_installs_one_lazy_quota_stop_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initialize_workspace(tmp_path)
    reasoner = CodexReader("gpt-5.6-terra", allow_cloud=True)
    submitted: list[int] = []

    def fake_rebuild_map(*_args: object, **kwargs: object) -> None:
        actual = kwargs["reasoner"]
        assert actual is reasoner
        assert reasoner._preflight is None
        assert reasoner._preflight_loader is None
        stop = reasoner.quota_stop_event
        assert isinstance(stop, threading.Event)

        def run(value: int) -> int:
            if value == 1:
                stop.set()
            else:
                stop.wait(timeout=1)
            return value

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result()
                for future, _ in _bounded_provider_futures(
                    executor,
                    list(range(8)),
                    lambda pool, value: (
                        submitted.append(value) or pool.submit(run, value)
                    ),
                    workers=2,
                    stop_event=stop,
                )
            ]
        assert sorted(results) == [0, 1]
        raise ProviderQuotaExhausted("synthetic quota")

    monkeypatch.setattr("auto_zettelkasten.api.rebuild_map", fake_rebuild_map)

    with pytest.raises(ProviderQuotaExhausted, match="synthetic quota"):
        build_map(
            tmp_path,
            run_id="standalone-quota-stop",
            provider="codex",
            model="gpt-5.6-terra",
            allow_cloud=True,
            provider_concurrency=2,
            reasoner=reasoner,
        )

    assert submitted == [0, 1]


def _fake_public_map_codex(path: Path, calls_path: Path) -> None:
    body = f'''#!{sys.executable}
import json, re, sys
from pathlib import Path

schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
schema = json.loads(schema_path.read_text(encoding="utf-8"))
prompt = sys.stdin.read()
user_text = prompt.split("USER INPUT:\\n", 1)[-1]
try:
    user = json.loads(user_text)
except json.JSONDecodeError:
    user = {{}}
properties = tuple(sorted(schema["properties"]))
contracts = {{
    ("analysis_sections", "compact_profile", "evidence_anchors", "literature_positions", "observed_bibliographic_identity"): "source_bundle",
    ("discovery_jobs", "literature_families", "neighboring_families", "source_dispositions"): "literature_family_plan",
    ("candidates", "job_outcomes"): "relationship_candidate_selection",
    ("decisions",): "relationship_adjudication",
    ("shard_ids",): "relationship_shard_selection",
    ("shard_pairs",): "bridge_shard_selection",
}}
if "bottom_line" in properties and "lines_of_inquiry" in properties:
    contract = "cluster_synthesis"
else:
    contract = contracts[properties]

def source_ids(value):
    return sorted(set(re.findall(r"source-zotero-[A-Za-z0-9_-]+", json.dumps(value))))

ids = source_ids(user)
if contract == "source_bundle":
    payload = {{
        "analysis_sections": {{key: "Source-grounded analysis; see p. 1." for key in schema["properties"]["analysis_sections"]["properties"]}},
        "compact_profile": {{
            "thesis": "Institutions shape implementation outcomes.",
            "method_or_knowledge_basis": "Synthetic document analysis.",
            "source_genre": "journal article",
            "inferential_design": "descriptive",
            "mechanisms": ["institutional implementation"],
            "outcomes": ["implementation outcomes"],
            "cases": [], "populations": [], "periods": [], "datasets": [],
        }},
        "evidence_anchors": [{{
            "claim": "The source links institutions with implementation outcomes.",
            "locator": "p. 1",
            "planning_roles": ["finding"],
            "salience_priority": 10,
            "evidence_role": "descriptive",
            "support_boundary": "Synthetic fixture scope.",
            "plain_english_meaning": "Institutional context matters for implementation.",
            "uncertainty": "The fixture supports only this bounded claim.",
            "quantitative_result": None,
        }}],
        "literature_positions": [],
        "observed_bibliographic_identity": {{"title": "", "creators": [], "date": ""}},
    }}
elif contract == "literature_family_plan":
    if user.get("context", {{}}).get("planning_mode") == "coverage_completion":
        payload = {{"literature_families": [], "discovery_jobs": [], "neighboring_families": [], "source_dispositions": []}}
    else:
        payload = {{
            "literature_families": [{{
                "family_id": "family-institutions",
                "label": "Institutions and implementation",
                "organizing_problem": "How institutions shape implementation outcomes.",
                "source_ids": ids,
                "proposed_roles": [{{"source_id": value, "role": "core"}} for value in ids],
                "candidate_cluster": True,
            }}],
            "discovery_jobs": [{{
                "job_id": "family-job-institutions",
                "family": "family-institutions",
                "left_source_ids": ids[:1],
                "right_source_ids": ids[1:2],
                "requested_collection_pair": [],
                "discovery_goal": "Compare the bounded institutional claims.",
                "candidate_quota": 1,
            }}],
            "neighboring_families": [],
            "source_dispositions": [{{
                "source_id": value,
                "disposition": "assigned",
                "family_ids": ["family-institutions"],
                "reason": "The source addresses the bounded organizing problem.",
            }} for value in ids],
        }}
elif contract == "relationship_candidate_selection":
    jobs = user.get("context", {{}}).get("bridge_jobs", [])
    candidates = []
    for rank, job in enumerate(jobs, 1):
        pair = sorted([job["left_source_ids"][0], job["right_source_ids"][0]])
        candidates.append({{
            "left_source_id": pair[0],
            "right_source_id": pair[1],
            "comparison_proposition": "Both sources address institutional implementation outcomes.",
            "bridge_job_id": job["bridge_job_id"],
            "rank": rank,
        }})
    if not jobs and len(ids) >= 2:
        candidates.append({{
            "left_source_id": ids[0],
            "right_source_id": ids[1],
            "comparison_proposition": "Both sources address institutional implementation outcomes.",
            "bridge_job_id": "",
            "rank": 1,
        }})
    payload = {{
        "candidates": candidates,
        "job_outcomes": [{{"bridge_job_id": job["bridge_job_id"], "status": "completed"}} for job in jobs],
    }}
elif contract == "relationship_shard_selection":
    payload = {{"shard_ids": []}}
elif contract == "bridge_shard_selection":
    payload = {{"shard_pairs": []}}
elif contract == "relationship_adjudication":
    jobs = user.get("context", {{}}).get("pair_jobs", [])
    payload = {{"decisions": [{{
        "pair_job_id": job["pair_job_id"],
        "decision": "relationship",
        "connections": [{{
            "comparison_proposition": "Both sources address institutional implementation outcomes.",
            "primary_relation_type": "complements",
            "secondary_relation_types": [],
            "actor_source_id": None,
            "reference_source_id": None,
            "source_a_basis": "The left source describes institutional implementation.",
            "source_b_basis": "The right source describes implementation outcomes.",
            "reason": "The sources contribute complementary bounded evidence.",
            "boundary_or_qualification": "Limited to the synthetic fixture scope.",
            "confidence": "high",
        }}],
    }} for job in jobs]}}
elif contract == "cluster_synthesis":
    cluster = user.get("context", {{}}).get("cluster", {{}})
    member_ids = sorted(cluster.get("source_ids", []))
    evidence = {{}}

    def collect_evidence(value):
        if isinstance(value, dict):
            source_id = value.get("source_id")
            anchor_id = value.get("evidence_anchor_id") or value.get("claim_id")
            locator = value.get("locator")
            if source_id in member_ids and anchor_id and locator:
                evidence.setdefault(source_id, {{
                    "source_id": source_id,
                    "evidence_anchor_id": anchor_id,
                    "locator": locator,
                }})
            for child in value.values():
                collect_evidence(child)
        elif isinstance(value, list):
            for child in value:
                collect_evidence(child)

    collect_evidence(user.get("profiles", []))
    payload = {{
        "cluster_id": cluster["cluster_id"],
        "status": "accepted",
        "title": "Institutions and implementation outcomes",
        "organizing_mode": "question",
        "organizing_problem": "How institutions shape implementation outcomes.",
        "guiding_question": "How do institutions shape implementation outcomes?",
        "central_tension": "Institutional design and practical implementation may diverge.",
        "bottom_line": "Both sources show that institutions shape implementation outcomes within the synthetic fixture.",
        "lines_of_inquiry": [{{
            "title": "Institutional implementation",
            "synthesis": "The two sources provide complementary evidence about institutional implementation.",
            "study_findings": [{{
                "source_id": source_id,
                "finding": "This source links institutions with implementation outcomes.",
                "method_scope": "Synthetic document analysis.",
                "relation_to_line": "supports",
                "evidence": [evidence[source_id]],
                "technical_result": "",
                "plain_english_meaning": "",
            }} for source_id in member_ids],
        }}],
        "differences": [{{"difference": "The sources examine distinct synthetic records."}}],
        "limits": ["The conclusion is limited to the synthetic fixture."],
        "related_clusters": [],
        "retained_member_ids": member_ids,
        "member_roles": [
            {{"source_id": source_id, "role": "core"}}
            for source_id in member_ids
        ],
        "dropped_members": [],
        "material_exclusions": [],
        "acquisition_candidate_dispositions": [],
        "split_proposals": [],
        "missing_member_ids": [],
    }}

with Path({str(calls_path)!r}).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"contract": contract, "model": sys.argv[sys.argv.index("-m") + 1]}}) + "\\n")
print(json.dumps({{"type": "thread.started"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps(payload)}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}), flush=True)
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_public_codex_map_runs_relationships_and_replays_without_calls_or_semantic_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_items: list[dict[str, object]],
) -> None:
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    executable = provider_root / "codex"
    calls_path = provider_root / "calls.jsonl"
    _fake_public_map_codex(executable, calls_path)
    workspace = tmp_path / "workspace"
    preflight_calls: list[tuple[str, tuple[str, ...]]] = []

    def preflight(
        model: str,
        _effort: str,
        additional_models: tuple[str, ...],
        *,
        forbidden_credential_roots: tuple[Path, ...],
    ) -> dict[str, object]:
        assert forbidden_credential_roots == (
            workspace.resolve(),
            workspace.resolve() / "11_state" / "runs" / "public-codex-map",
        )
        preflight_calls.append((model, additional_models))
        return fake_codex_preflight(
            tmp_path,
            executable,
            {"PATH": os.environ["PATH"]},
        )

    monkeypatch.setattr("auto_zettelkasten.pipeline.codex_preflight_status", preflight)
    request = MapRequest(
        workspace,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        reasoning_effort="medium",
        allow_cloud=True,
        parallel=2,
        provider_concurrency=2,
        literature_policy=LiteratureMappingPolicy(
            cluster_generation_enabled=True
        ),
    )
    items = json.loads(json.dumps(sample_items))
    items[0]["data"].pop("relations", None)
    first = run_map(
        request,
        client=FakeZotero(items),
        run_id="public-codex-map",
    )

    assert first.status == "completed"
    assert first.validated_note_count == 2
    assert preflight_calls == [("gpt-5.6-luna", ("gpt-5.6-terra",))]
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert {
        contract: sum(row["contract"] == contract for row in calls)
        for contract in {row["contract"] for row in calls}
    } == {
        "source_bundle": 2,
        "relationship_candidate_selection": 1,
        "relationship_adjudication": 1,
        "literature_family_plan": 1,
        "cluster_synthesis": 1,
    }
    assert {row["model"] for row in calls if row["contract"] == "source_bundle"} == {"gpt-5.6-luna"}
    assert any(row["contract"] == "literature_family_plan" for row in calls)
    assert any(row["contract"] == "relationship_candidate_selection" for row in calls)
    assert any(row["contract"] == "relationship_adjudication" for row in calls)
    assert any(row["contract"] == "cluster_synthesis" for row in calls)
    registry = read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    )
    accepted = next(
        row for row in registry["links"] if row["relation_type"] == "complements"
    )
    notes = sorted((workspace / "02_source_memory" / "notes").glob("*.md"))
    assert len(notes) == 2
    assert all("complements" in note.read_text(encoding="utf-8") for note in notes)
    cluster_registry = read_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml"
    )
    assert cluster_registry["pending_revisions"] == []
    assert len(cluster_registry["clusters"]) == 1
    cluster = cluster_registry["clusters"][0]
    assert cluster["formation_route"] == "global_cluster_plan"
    assert cluster["relationship_first_admission"] is True
    assert cluster["family_admission_passed"] is True
    assert accepted["relation_id"] in cluster["relation_ids"]
    syntheses = read_yaml(
        workspace / "03_literature_synthesis" / "cluster_syntheses.yml"
    )["syntheses"]
    synthesis = syntheses[cluster["cluster_id"]]
    assert synthesis["status"] == "reasoned"
    assert synthesis["quality_status"] == "complete"
    assert synthesis["parked_for_review"] is False
    assert synthesis["retained_member_ids"] == cluster["source_ids"]

    before_calls = calls_path.read_bytes()
    run_root = workspace / "11_state" / "runs" / "public-codex-map"
    semantic_roots = (
        workspace / "02_source_memory",
        workspace / "03_literature_synthesis",
        workspace / "11_state" / "relationship_jobs",
        workspace / "11_state" / "semantic_jobs",
        run_root / "literature",
        run_root / "relationship_batches",
        run_root / "relationship_jobs",
    )
    semantic_paths = (
        run_root / "artifact_manifest.yml",
        run_root / "source_replay_receipt.yml",
    )

    def semantic_snapshot() -> dict[Path, tuple[bytes, int]]:
        paths = {
            path
            for root in semantic_roots
            for path in root.rglob("*")
            if path.is_file() and path.name != "cross_boundary_ledger.yml"
        }
        paths.update(path for path in semantic_paths if path.is_file())
        return {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in paths
        }

    before_files = semantic_snapshot()
    with deny_codex_attempts():
        replay = resume_map(
            workspace,
            "public-codex-map",
            client=FakeZotero(items),
        )

    assert replay.status == "completed"
    assert replay.source_set["source_set_id"] == first.source_set["source_set_id"]
    assert replay.source_set["dependency_hash"] == first.source_set["dependency_hash"]
    assert preflight_calls == [("gpt-5.6-luna", ("gpt-5.6-terra",))]
    assert calls_path.read_bytes() == before_calls
    after_files = semantic_snapshot()
    changed = [
        (path.relative_to(workspace), before_files.get(path), after_files.get(path))
        for path in sorted(set(before_files) | set(after_files))
        if before_files.get(path) != after_files.get(path)
    ]
    assert not changed, "\n".join(
        f"{path}: {before[1] if before else None} -> {after[1] if after else None}"
        for path, before, after in changed
    )
