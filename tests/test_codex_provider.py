from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

import auto_zettelkasten.codex_attempt_guard as attempt_guard_module
import auto_zettelkasten.literature as literature_module
import auto_zettelkasten.readers as readers_module
from auto_zettelkasten.api import (
    _provider_check,
    build_map,
    initialize_workspace,
    resume_map,
    run_map,
)
from auto_zettelkasten.codex_attempt_guard import (
    CodexAttemptStateError,
    deny_codex_attempts,
)
from auto_zettelkasten.cli import main
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.models import (
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
    ProcessingPolicy,
)
from auto_zettelkasten.literature import (
    LiteratureSynthesisPartialError,
    _CheckpointedReasonerCalls,
    _PROVIDER_INPUT_DEPENDENCY_COMPONENTS,
    _bounded_provider_futures,
    _provider_worker_count,
    _same_provider_inputs,
)
from auto_zettelkasten.pipeline import (
    _ProfileProviderBudget,
    _apply_reader_policy,
    _read_document,
    _profile_dependency_policy,
    _provider_call_with_transport_retry,
    _source_worker_count,
    _transport_retryable,
    run_pipeline,
)
from auto_zettelkasten.readers import (
    CHUNK_EVIDENCE_KEYS,
    CODEX_CLI_PROFILES,
    CODEX_CONTRACT_RESERVATIONS,
    CODEX_OUTPUT_CONTRACTS,
    CodexReader,
    ProviderError,
    ProviderInterrupted,
    ProviderInvalidSourceBundle,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderTimeout,
    ProviderTransportError,
    ProviderUnsupportedAttachment,
    _OUTPUT_CONTRACT,
    _SOURCE_BUNDLE_ATTACHMENTS,
    _CODEX_TRANSPORT_INSTRUCTIONS,
    _codex_executable,
    _codex_error_item_category,
    _codex_failure,
    _codex_json_schema,
    _codex_pdf_helper_manifest,
    _codex_retry_arguments,
    _codex_tool_feature_arguments,
    _redact_codex_diagnostic,
    cancel_active_provider_responses,
    codex_contract_for_stage,
    codex_contract_identity,
    codex_preflight_status,
    codex_stage_identity,
    current_provider_completion,
)
from conftest import SECTION_KEYS, FakeZotero, fake_codex_preflight


def test_codex_0152_no_retry_config_uses_guarded_builtin_overrides() -> None:
    arguments = _codex_retry_arguments("0.152.1")

    assert 'model_provider="openai"' in arguments
    assert 'openai_base_url="https://chatgpt.com/backend-api/codex"' in arguments
    assert 'chatgpt_base_url="https://chatgpt.com/backend-api/"' in arguments
    assert "model_providers.openai.request_max_retries=0" in arguments
    assert "model_providers.openai.stream_max_retries=0" in arguments


def test_codex_executable_prefers_override_then_companion_then_stock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    override = tmp_path / "override-codex"
    companion = binaries / "auto-zettelkasten-codex"
    stock = binaries / "codex"
    for path in (override, companion, stock):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)

    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.setenv("AUTO_ZETTELKASTEN_CODEX", str(override))
    assert _codex_executable() == override.resolve()

    monkeypatch.delenv("AUTO_ZETTELKASTEN_CODEX")
    assert _codex_executable() == companion.resolve()

    companion.unlink()
    assert _codex_executable() == stock.resolve()


def test_codex_executable_rejects_invalid_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUTO_ZETTELKASTEN_CODEX", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ProviderError, match="executable"):
        _codex_executable()


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
        provider_concurrency=8,
    )
    with pytest.raises(ValueError, match="between 1 and 8"):
        MapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-luna",
            literature_model="gpt-5.6-terra",
            provider_concurrency=9,
        )
    LiteratureMapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        provider_concurrency=8,
    )
    with pytest.raises(ValueError, match="between 1 and 8"):
        LiteratureMapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-terra",
            provider_concurrency=9,
        )


def test_codex_subscription_run_lock_is_exclusive_and_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "codex.lock"
    monkeypatch.setattr(
        attempt_guard_module, "_CODEX_SUBSCRIPTION_RUN_LOCK_PATH", lock_path
    )

    with attempt_guard_module.codex_subscription_run_lock("codex"):
        with pytest.raises(CodexAttemptStateError, match="already active"):
            with attempt_guard_module.codex_subscription_run_lock("codex"):
                pass
    with attempt_guard_module.codex_subscription_run_lock("codex"):
        pass

    lock_path.unlink()
    lock_path.symlink_to(tmp_path / "target")
    with pytest.raises(CodexAttemptStateError, match="unavailable"):
        with attempt_guard_module.codex_subscription_run_lock("codex"):
            pass


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
        "evidence_anchors",
        "analysis_sections",
        "compact_profile",
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
        _codex_failure("Selected model is at capacity. Please try a different model."),
        ProviderQuotaExhausted,
    )
    assert isinstance(
        _codex_failure(
            "image inputs are not supported: /tmp/pytest-500/page.png",
            attachment_paths=("/tmp/pytest-500/page.png",),
        ),
        ProviderUnsupportedAttachment,
    )


def test_codex_image_bundle_without_evidence_is_typed_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts: list[str] = []
    payload = {
        "analysis_sections": {
            key: "No recovered source text is available." for key in SECTION_KEYS
        },
        "compact_profile": {
            "thesis": "No thesis is recoverable.",
            "method_or_knowledge_basis": "",
            "source_genre": "",
            "inferential_design": "",
            "mechanisms": [],
            "outcomes": [],
            "cases": [],
            "populations": [],
            "periods": [],
            "datasets": [],
        },
        "evidence_anchors": [],
        "literature_positions": [],
        "observed_bibliographic_identity": {"title": "", "creators": [], "date": ""},
    }
    def generate(_reader: CodexReader, _system: str, user: str, *_args, **_kwargs):
        prompts.append(user)
        return payload

    monkeypatch.setattr(CodexReader, "_generate_with_reasoning", generate)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)

    with pytest.raises(ProviderInvalidSourceBundle):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
            attachment_paths=[tmp_path / "page.png"],
        )
    assert "attached page images are the inspected source content" in prompts[0]


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


def test_codex_transport_failure_is_raised_without_retry(tmp_path: Path) -> None:
    budget = _ProfileProviderBudget(
        tmp_path / "provider_usage.yml", 1, provider="codex", model="gpt-5.6-luna"
    )
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise ProviderTransportError(
            "stream disconnected before completion",
            transport_kind="codex_cli",
        )

    with pytest.raises(ProviderTransportError, match="stream disconnected"):
        _provider_call_with_transport_retry(
            budget,
            "source_bundle_direct",
            "A",
            "fingerprint",
            operation,
        )

    assert calls == 1
    assert budget.cumulative_calls == 1
    assert budget.attempts[0]["failure_class"] == "transport"


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


def test_codex_source_transport_failure_stops_queue_and_is_terminal(
    tmp_path: Path,
    sample_items: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransportFailureReader:
        name = "codex"
        model = "gpt-5.6-luna"
        is_cloud = True

        def __init__(self) -> None:
            self.calls = 0

        def read_source(self, text, metadata, question=None):
            del text, metadata, question
            self.calls += 1
            raise ProviderTransportError(
                "unexpected 404", transport_kind="codex_cli"
            )

    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        allow_cloud=True,
        parallel=1,
        provider_concurrency=1,
        literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
    )
    reader = TransportFailureReader()
    monkeypatch.setattr(
        "auto_zettelkasten.pipeline.rebuild_map",
        lambda *_args, **_kwargs: pytest.fail(
            "graph stage must not start after terminal source transport"
        ),
    )

    report = run_pipeline(
        request,
        client=FakeZotero(sample_items[:2]),
        reader=reader,
        run_id="terminal-source-transport",
    )

    assert report.status == "partial"
    assert report.pending_count == 1
    assert reader.calls == 1
    failure = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "terminal-source-transport"
        / "items"
        / "ITEMA"
        / "source_failure.yml"
    )
    assert failure["failure_class"] == "transport"
    assert failure["retry_on_resume"] is False
    assert failure["status"] == "parked_for_review"

    resumed = run_pipeline(
        request,
        client=FakeZotero(sample_items[:2]),
        reader=reader,
        run_id="terminal-source-transport",
        resume=True,
    )
    assert resumed.status == "partial"
    assert reader.calls == 1

    (
        tmp_path
        / "11_state"
        / "runs"
        / "terminal-source-transport"
        / "items"
        / "ITEMA"
        / "prepared_result.yml"
    ).unlink()
    crash_resumed = run_pipeline(
        request,
        client=FakeZotero(sample_items[:2]),
        reader=reader,
        run_id="terminal-source-transport",
        resume=True,
    )
    assert crash_resumed.status == "partial"
    assert reader.calls == 1

    single_root = tmp_path / "single"
    single_reader = TransportFailureReader()
    single = run_pipeline(
        MapRequest(
            single_root,
            provider="codex",
            model="gpt-5.6-luna",
            allow_cloud=True,
            parallel=1,
            provider_concurrency=1,
            literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
        ),
        client=FakeZotero(sample_items[:1]),
        reader=single_reader,
        run_id="single-terminal-source-transport",
    )
    assert single.status == "partial"
    assert single_reader.calls == 1


def test_codex_literature_transport_failure_stops_replenishment_and_is_terminal(
    tmp_path: Path,
) -> None:
    class TransportFailureReasoner:
        name = "codex"
        model = "gpt-5.6-terra"
        _preflight = {"version": "0.152.1"}

        def __init__(self) -> None:
            self.calls = 0
            self.quota_stop_event = threading.Event()

        def select_relationship_candidates(self, profiles, request, *, context=None):
            del profiles, request, context
            self.calls += 1
            raise ProviderTransportError(
                "unexpected 404", transport_kind="codex_cli"
            )

    reasoner = TransportFailureReasoner()
    request = LiteratureMapRequest(
        workspace=tmp_path,
        provider="codex",
        model="gpt-5.6-terra",
        allow_cloud=True,
    )
    calls = _CheckpointedReasonerCalls(tmp_path, "terminal-graph", reasoner, request)
    submitted: list[int] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        for future, _job in _bounded_provider_futures(
            executor,
            [0, 1, 2, 3, 4],
            lambda pool, job: (
                submitted.append(job)
                or pool.submit(
                    calls,
                    "relationship_candidate_selection",
                    f"job-{job}",
                    "select_relationship_candidates",
                    [],
                    {},
                )
            ),
            workers=4,
            stop_event=reasoner.quota_stop_event,
        ):
            with pytest.raises(
                (
                    LiteratureSynthesisPartialError,
                    ProviderQuotaExhausted,
                    ProviderTransportError,
                )
            ):
                future.result()

    assert submitted == [0, 1, 2, 3]
    assert 1 <= reasoner.calls <= 4
    failure = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "terminal-graph"
        / "literature"
        / "synthesis"
        / "terminal_transport_failure.yml"
    )
    assert failure["failure_class"] == "transport"
    assert failure["terminal"] is True
    assert failure["retry_on_resume"] is False

    resumed_reasoner = TransportFailureReasoner()
    resumed_calls = _CheckpointedReasonerCalls(
        tmp_path, "terminal-graph", resumed_reasoner, request
    )
    resumed_submitted: list[int] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for future, _job in _bounded_provider_futures(
            executor,
            [0, 5],
            lambda pool, job: (
                resumed_submitted.append(job)
                or pool.submit(
                    resumed_calls,
                    "relationship_candidate_selection",
                    f"job-{job}",
                    "select_relationship_candidates",
                    [],
                    {},
                )
            ),
            workers=4,
            stop_event=resumed_reasoner.quota_stop_event,
        ):
            future.result()

    assert resumed_submitted == []
    assert resumed_reasoner.calls == 0


def test_codex_terminal_transport_wins_over_concurrent_quota_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransportFailureReasoner:
        name = "codex"
        model = "gpt-5.6-terra"
        _preflight = {"version": "0.152.1"}

        def __init__(self) -> None:
            self.calls = 0
            self.quota_stop_event = threading.Event()

        def select_relationship_candidates(self, profiles, request, *, context=None):
            del profiles, request, context
            self.calls += 1
            raise ProviderTransportError(
                "unexpected 404", transport_kind="codex_cli"
            )

    delayed = threading.Event()
    release = threading.Event()
    original_packet_chars = literature_module._reasoner_packet_chars

    def pause_after_terminal_check(profiles, context):
        if context.get("delay_before_reservation"):
            delayed.set()
            assert release.wait(timeout=5)
        return original_packet_chars(profiles, context)

    monkeypatch.setattr(
        literature_module, "_reasoner_packet_chars", pause_after_terminal_check
    )
    reasoner = TransportFailureReasoner()
    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "concurrent-terminal-transport",
        reasoner,
        LiteratureMapRequest(
            workspace=tmp_path,
            provider="codex",
            model="gpt-5.6-terra",
            allow_cloud=True,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sibling = executor.submit(
            calls,
            "relationship_candidate_selection",
            "delayed",
            "select_relationship_candidates",
            [],
            {"delay_before_reservation": True},
        )
        assert delayed.wait(timeout=5)
        with pytest.raises(ProviderTransportError):
            calls(
                "relationship_candidate_selection",
                "failure",
                "select_relationship_candidates",
                [],
                {},
            )
        release.set()
        with pytest.raises(
            LiteratureSynthesisPartialError,
            match="terminal_codex_transport_failure",
        ):
            sibling.result()

    assert reasoner.calls == 1


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


def test_codex_relationship_schema_binds_each_endpoint_anchor_pool() -> None:
    original = json.dumps(CODEX_OUTPUT_CONTRACTS, sort_keys=True)
    pools = {
        "job-a": {"source_a": ["anchor-a"], "source_b": ["anchor-b"]},
        "job-b": {"source_a": ["anchor-a"], "source_b": ["anchor-c"]},
    }
    schema = _codex_json_schema(
        "relationship_adjudication", pair_job_ids=tuple(pools), pair_anchor_ids=pools,
    )
    for job_id, endpoints in pools.items():
        connection = schema["properties"]["decisions"]["properties"][job_id]["properties"]["connections"]["items"]
        for endpoint, anchors in endpoints.items():
            field = connection["properties"][f"{endpoint}_anchor_ids"]
            assert field["minItems"] == 1
            assert field["items"] == {"type": "string", "enum": anchors}
    assert "anchor-c" not in schema["properties"]["decisions"]["properties"]["job-a"]["properties"]["connections"]["items"]["properties"]["source_b_anchor_ids"]["items"]["enum"]
    assert json.dumps(CODEX_OUTPUT_CONTRACTS, sort_keys=True) == original


def test_note_based_relationship_schema_binds_pairs_without_anchor_pools() -> None:
    context = {"pair_jobs": [{"pair_job_id": "job-a",
                              "output_contract": "relationship-decision-v10"}]}
    assert readers_module._codex_relationship_anchor_ids(context) is None
    schema = _codex_json_schema("relationship_adjudication", pair_job_ids=("job-a",))
    assert set(schema["properties"]["decisions"]["required"]) == {"job-a"}
    assert "anchor" not in json.dumps(schema)
    assert "evidence_anchor_id" not in json.dumps(_codex_json_schema("cluster_synthesis"))


def test_codex_relationship_schema_requires_every_requested_pair() -> None:
    pair_ids = tuple(f"relationship-job-{value * 20}" for value in "abc")
    original = json.dumps(CODEX_OUTPUT_CONTRACTS, sort_keys=True)
    schema = _codex_json_schema(
        "relationship_adjudication", pair_job_ids=pair_ids
    )
    decisions = schema["properties"]["decisions"]
    assert decisions["type"] == "object"
    assert decisions["required"] == list(pair_ids)
    assert set(decisions["properties"]) == set(pair_ids)
    assert decisions["additionalProperties"] is False
    for value in decisions["properties"].values():
        assert set(value["required"]) == {"assessment", "confidence", "connections"}
        assert value["additionalProperties"] is False
        assert "anyOf" not in value
        assert value["properties"]["connections"]["type"] == "array"
    assert json.dumps(CODEX_OUTPUT_CONTRACTS, sort_keys=True) == original


@pytest.mark.parametrize("failure", ["", "blank", "non_string", "missing", "extra", "invalid_connections", "invalid_connection", "invalid_confidence"])
def test_codex_relationship_connections_determine_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    pair_id = "relationship-job-a"
    context = {"pair_jobs": [{"pair_job_id": pair_id, "allowed_evidence_anchor_ids": {"source_a": ["anchor-a"], "source_b": ["anchor-b"]}}]}
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    assessment = "The sources do not contribute to one bounded comparison."
    value = {"assessment": assessment, "confidence": "high", "connections": []}
    if failure == "blank":
        value["assessment"] = " "
    elif failure == "non_string":
        value["assessment"] = True
    elif failure == "missing":
        value.pop("connections")
    elif failure == "extra":
        value["decision"] = "no_relationship"
    elif failure == "invalid_connections":
        value["connections"] = {}
    elif failure == "invalid_connection":
        value["connections"] = [None]
    elif failure == "invalid_confidence":
        value["confidence"] = True
    monkeypatch.setattr(reader, "_generate_text", lambda *_: json.dumps({"decisions": {pair_id: value}}))
    with deny_codex_attempts():
        if failure:
            with pytest.raises(ProviderError, match="assessment|connections|confidence") as error:
                reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context=context)
            assert error.value.raw_response == {"decisions": {pair_id: value}}
        else:
            schema = _codex_json_schema("relationship_adjudication", pair_job_ids=(pair_id,))
            bound = schema["properties"]["decisions"]["properties"][pair_id]
            assert list(json.loads(json.dumps(bound, sort_keys=True))["properties"]) == ["assessment", "confidence", "connections"]
            result = reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context=context)
            assert result == {"decisions": [{"decision": "no_relationship", "confidence": "high", "reason": assessment, "pair_job_id": pair_id}]}
    assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == ()
    assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() is None


@pytest.mark.parametrize("failure", ["", "missing", "extra", "duplicate", "array", "malformed", "extra_root"])
def test_codex_relationship_request_keys_are_strict_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    pair_ids = ("relationship-job-a", "relationship-job-b")
    context = {"pair_jobs": [{"pair_job_id": value, "allowed_evidence_anchor_ids": {"source_a": ["anchor-a"], "source_b": ["anchor-b"]}} for value in pair_ids]}
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    decision = {"assessment": "No bounded connection.", "connections": [], "confidence": "high"}

    def generate(*_args):
        assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == pair_ids
        assert "decisions is an object" in readers_module._codex_contract_note("relationship_adjudication")
        values = dict.fromkeys(pair_ids, decision)
        if failure == "missing":
            values.pop(pair_ids[-1])
        elif failure == "extra":
            values["relationship-job-foreign"] = decision
        if failure == "duplicate":
            row = json.dumps(decision)
            return '{"decisions":{"relationship-job-a":' + row + ',"relationship-job-a":' + row + ',"relationship-job-b":' + row + '}}'
        if failure == "malformed":
            return '{"decisions":'
        if failure == "extra_root":
            return json.dumps({"decisions": values, "unexpected": "value"})
        return json.dumps({"decisions": [] if failure == "array" else values})

    monkeypatch.setattr(reader, "_generate_text", generate)
    with deny_codex_attempts():
        if failure:
            with pytest.raises(ProviderError, match="exact pair keys|duplicate JSON keys|valid JSON"):
                reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context=context)
        else:
            result = reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context=context)
            assert [row["pair_job_id"] for row in result["decisions"]] == list(pair_ids)
            assert all(row["reason"] == decision["assessment"] for row in result["decisions"])
    assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == ()
    assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() is None
    assert "decisions is an array" in readers_module._codex_contract_note("relationship_adjudication")


def test_codex_relationship_pair_bindings_are_thread_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    barrier = threading.Barrier(2)

    def generate(*_args):
        before = readers_module._RELATIONSHIP_PAIR_JOB_IDS.get()
        anchors = readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get()
        barrier.wait(timeout=5)
        assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == before
        assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() == anchors
        assert anchors[before[0]]["source_b"] == [f"{before[0]}-anchor"]
        return json.dumps({"decisions": {before[0]: {"assessment": "Distinct scope.", "connections": [], "confidence": "high"}}})

    monkeypatch.setattr(reader, "_generate_text", generate)
    def invoke(pair_id):
        result = reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context={"pair_jobs": [{"pair_job_id": pair_id, "allowed_evidence_anchor_ids": {"source_a": ["anchor-a"], "source_b": [f"{pair_id}-anchor"]}}]})
        assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == ()
        assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() is None
        return result["decisions"][0]["pair_job_id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(invoke, ["job-a", "job-b"])) == ["job-a", "job-b"]


def test_codex_relationship_admission_counts_bound_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    request = LiteratureMapRequest(tmp_path)
    context = {"pair_jobs": [{"pair_job_id": f"job-{index}", "allowed_evidence_anchor_ids": {"source_a": ["anchor-a"], "source_b": [f"anchor-{index}"]}} for index in range(8)]}
    observed = []
    def fits(*_args, **kwargs):
        observed.append(kwargs["extra_input_tokens"])
        return False

    monkeypatch.setattr(reader, "_prompt_fits", fits)
    monkeypatch.setattr(reader, "_generate_text", lambda *_args: pytest.fail("over-budget request must not launch"))
    assert reader.relationship_adjudication_fits([], request, context=context) is False
    with pytest.raises(ProviderError, match="context budget"):
        reader.adjudicate_relationships([], request, context=context)
    assert len(observed) == 2 and observed[0] == observed[1] > 3000
    assert readers_module._RELATIONSHIP_PAIR_JOB_IDS.get() == ()
    assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() is None


@pytest.mark.parametrize("pools", [None, {}, {"source_a": ["anchor-a"]}, {"source_a": [], "source_b": ["anchor-b"]}, {"source_a": [None], "source_b": ["anchor-b"]}])
def test_codex_relationship_missing_evidence_fails_before_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pools: object,
) -> None:
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    context = {"pair_jobs": [{"pair_job_id": "job-a", "allowed_evidence_anchor_ids": pools}]}
    monkeypatch.setattr(reader, "_authorize_request", lambda: pytest.fail("invalid evidence must not authorize"))
    with pytest.raises(ProviderError, match="anchor pools"):
        reader.relationship_adjudication_fits([], LiteratureMapRequest(tmp_path), context=context)
    with pytest.raises(ProviderError, match="anchor pools"):
        reader.adjudicate_relationships([], LiteratureMapRequest(tmp_path), context=context)
    assert readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.get() is None


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


def test_codex_auto_concurrency_uses_role_specific_limits(
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
    assert _source_worker_count(reader, request, 20) == 4
    assert _provider_worker_count(
        LiteratureMapRequest(
            tmp_path,
            provider="codex",
            model="gpt-5.6-terra",
            provider_concurrency="auto",
        ),
        20,
    ) == 1


@pytest.mark.parametrize("cli_profile", ["0.145.0", "0.152.1"])
def test_codex_preflight_uses_one_sanitized_executable_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_profile: str
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
            for model, values in CODEX_CLI_PROFILES[cli_profile]["models"].items()
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
            if name in CODEX_CLI_PROFILES[cli_profile]["tool_features"]
            else value["default"],
        }
        for name, value in CODEX_CLI_PROFILES[cli_profile]["features"].items()
    }
    feature_output = "\n".join(
        f"{name}  {value['maturity']}  {str(value['default']).lower()}"
        for name, value in expected_features.items()
    )

    def run(args: list[str], **kwargs: object) -> SimpleNamespace:
        environment = dict(kwargs["env"])  # type: ignore[arg-type]
        calls.append((args, environment))
        if args[-1] == "--version":
            output = f"codex-cli {cli_profile}"
        elif args[-2:] == ["features", "list"]:
            output = feature_output
        else:
            output = "Logged in using ChatGPT"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    status = codex_preflight_status("gpt-5.6-luna")
    assert status["auth_method"] == "chatgpt"
    assert status["auth_status"] == "authenticated"
    assert status["helper_version"] == ""
    assert status["helper_manifest_valid"] is False
    assert status["pdf_input_file_capability"] is False
    assert status["reasoning_effort_compatibility"] is True
    assert len(calls) == 3
    assert calls[0][1] == calls[2][1]
    feature_environment = dict(calls[1][1])
    feature_environment.pop("CODEX_HOME")
    base_environment = dict(calls[0][1])
    base_codex_home = base_environment.pop("CODEX_HOME", None)
    assert feature_environment == base_environment
    assert calls[1][1]["CODEX_HOME"] != base_codex_home
    assert calls[1][0][1:-2] == [
        *_codex_retry_arguments(cli_profile),
        *_codex_tool_feature_arguments(cli_profile),
    ]
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
                **CODEX_CLI_PROFILES[cli_profile]["models"]["gpt-5.6-luna"],
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


def test_codex_preflight_failure_precedes_attempt_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)

    def reject_preflight() -> dict[str, object]:
        raise ProviderError("Codex CLI preflight failed: unsupported retry override")

    reader._preflight_loader = reject_preflight
    monkeypatch.setattr(
        "auto_zettelkasten.readers.reserve_codex_attempt",
        lambda *_args, **_kwargs: pytest.fail("failed preflight reserved an attempt"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("failed preflight spawned Codex"),
    )
    token = _OUTPUT_CONTRACT.set("source_bundle")
    try:
        with pytest.raises(ProviderError, match="unsupported retry override"):
            reader._generate_text("system", "user", 128, 5)
    finally:
        _OUTPUT_CONTRACT.reset(token)


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
    pdf_payload = base64.b64encode(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n").decode(
        "ascii"
    )
    pdf_data_url = "data:application/pdf;base64," + pdf_payload
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
            pdf_data_url,
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
        pdf_payload,
        pdf_data_url,
    ):
        assert sensitive not in redacted
    assert "data:application/pdf;base64,[REDACTED]" in redacted
    assert "data:application/pdf;base64,[REDACTED]" in str(failure)


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
            "executable": "/synthetic/private/bin/auto-zettelkasten-codex",
            "version": "0.152.1",
            "helper_version": "0.152.1+azpdf1",
            "helper_manifest_valid": True,
            "pdf_input_file_capability": True,
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
    assert status["version"] == "0.152.1"
    assert status["helper_version"] == "0.152.1+azpdf1"
    assert status["helper_manifest_valid"] is True
    assert status["pdf_input_file_capability"] is True
    assert "executable" not in status
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


def test_codex_doctor_coarsens_unexpected_preflight_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    private_path = str(tmp_path / ".codex" / "auth.json")

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError(f"cannot inspect {private_path}")

    monkeypatch.setattr("auto_zettelkasten.api.codex_preflight_status", fail)
    status = _provider_check(
        "codex",
        "gpt-5.6-luna",
        {"literature_model": "gpt-5.6-terra"},
    )

    assert status["reason"] == "OSError: Codex preflight failed"
    assert private_path not in json.dumps(status)


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
schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
codex_home = Path(os.environ["CODEX_HOME"])
Path({str(capture_path)!r}).write_text(json.dumps({{"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir()), "codex_home": str(codex_home), "codex_home_entries": sorted(path.name for path in codex_home.iterdir()), "codex_home_modes": {{path.name: path.stat().st_mode & 0o777 for path in codex_home.iterdir()}}, "model_instructions_path": str(instruction_path), "model_instructions": instruction_path.read_text(), "model_instructions_mode": instruction_path.stat().st_mode & 0o777, "output_schema": schema_path.read_text()}}))
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


def _valid_source_bundle_payload() -> dict[str, object]:
    return {
        "analysis_sections": {
            key: "Source-grounded analysis from the attached PDF; see p. 1."
            for key in SECTION_KEYS
        },
        "compact_profile": {
            "thesis": "The attached source supports a bounded claim.",
            "method_or_knowledge_basis": "Document analysis.",
            "source_genre": "report",
            "inferential_design": "descriptive",
            "mechanisms": [],
            "outcomes": [],
            "cases": [],
            "populations": [],
            "periods": [],
            "datasets": [],
        },
        "evidence_anchors": [
            {
                "claim": "The attached source supports the bounded claim.",
                "locator": "p. 1",
                "planning_roles": ["finding"],
                "salience_priority": 10,
                "evidence_role": "descriptive",
                "support_boundary": "The attached PDF only.",
                "plain_english_meaning": "The source supports the claim.",
                "uncertainty": "No external evidence was considered.",
                "quantitative_result": None,
            }
        ],
        "literature_positions": [],
        "observed_bibliographic_identity": {
            "title": "",
            "creators": [],
            "date": "",
        },
    }


@pytest.mark.parametrize(
    ("text", "legacy_checkpoint"),
    [("A" * 909_689, False), ("字" * 450_000, False), ("A" * 909_689, True)],
    ids=["ascii", "multibyte", "completed-legacy-split"],
)
def test_codex_hierarchical_chunks_fit_exact_envelopes_without_losing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, legacy_checkpoint: bool,
) -> None:
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True, reasoning_effort="medium")
    reader._preflight = {"version": "0.152.1"}
    request = MapRequest(tmp_path, processing=ProcessingPolicy())
    metadata = {"_source_context": {"source_id": "source-zotero-a1", "zotero_key": "A1"}}
    _apply_reader_policy(reader, request.processing)
    chunks = []
    contracts = []
    summarize = reader.summarize_chunk

    def record_chunk(text, *args, **kwargs):
        chunks.append(text)
        return summarize(text, *args, **kwargs)

    def generate(system, user, output_tokens, deadline):
        assert legacy_checkpoint or reader._prompt_fits(system, user, output_tokens)
        contract = _OUTPUT_CONTRACT.get()
        contracts.append(contract)
        payload = (
            {key: "Source-grounded chunk evidence." for key in CHUNK_EVIDENCE_KEYS}
            if contract == "chunk_evidence"
            else _valid_source_bundle_payload()
        )
        return json.dumps(payload)

    monkeypatch.setattr(reader, "summarize_chunk", record_chunk)
    monkeypatch.setattr(reader, "_generate_text", generate)
    monkeypatch.setattr(
        reader, "_ensure_codex_preflight",
        lambda: pytest.fail("no helper preflight is allowed in this test"),
    )
    if legacy_checkpoint:
        # Model an already completed pre-fit-check checkpoint, never a live request.
        monkeypatch.setattr(reader, "chunk_evidence_fits", lambda *args, **kwargs: True)
        monkeypatch.setattr(reader, "_ensure_prompt_fits", lambda *args, **kwargs: None)
    with deny_codex_attempts():
        result, route, reason = _read_document(
            reader, text, metadata, None, request=request,
            checkpoint_root=tmp_path / "checkpoints",
        )

    assert route == "codex_hierarchical_text"
    assert reason == f"hierarchical_source_read:{len(chunks)}"
    assert len(chunks) > 1
    assert "".join(chunk.split("\n", 1)[1] for chunk in chunks) == text
    assert contracts == ["chunk_evidence"] * len(chunks) + ["source_bundle"]
    before = {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*") if p.is_file()
    }
    monkeypatch.setattr(reader, "_generate_text", lambda *args, **kwargs: pytest.fail("replay launched a call"))
    if legacy_checkpoint:
        monkeypatch.setattr(reader, "chunk_evidence_fits", lambda *args, **kwargs: pytest.fail("completed checkpoint was replanned"))
    with deny_codex_attempts():
        assert _read_document(
            reader, text, metadata, None, request=request,
            checkpoint_root=tmp_path / "checkpoints",
        ) == (result, route, reason)
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("framed_boundary", [False, True], ids=["oversized", "framed-boundary"])
def test_codex_chunk_envelope_that_cannot_fit_fails_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framed_boundary: bool,
) -> None:
    import auto_zettelkasten.pipeline as pipeline_module

    reader = CodexReader("gpt-5.6-luna", allow_cloud=True, reasoning_effort="medium")
    reader._preflight = {"version": "0.152.1"}
    if framed_boundary:
        reader.context_window_tokens = 12_000
    request = MapRequest(tmp_path, processing=ProcessingPolicy())
    _apply_reader_policy(reader, request.processing)
    monkeypatch.setattr(reader, "_generate_text", lambda *args, **kwargs: pytest.fail("provider called for an impossible envelope"))
    monkeypatch.setattr(reader, "_ensure_codex_preflight", lambda: pytest.fail("preflight called for an impossible envelope"))
    split_document = pipeline_module._split_document
    splits = 0

    def bounded_split(*args, **kwargs):
        nonlocal splits
        splits += 1
        assert splits <= 2, "impossible framed envelope repeatedly split the document"
        return split_document(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "_split_document", bounded_split)
    question = "Q" * 334 if framed_boundary else "Question " * 75_000
    text = "A" * (10_000 if framed_boundary else 500_000)
    if framed_boundary:
        assert reader.chunk_evidence_fits(
            "", {}, question, chunk_id="chunk-0001", locator="document chunk 1/1",
            max_output_tokens=request.processing.chunk_output_tokens,
        )

    with deny_codex_attempts(), pytest.raises(ProviderError, match="chunk envelope exceeds"):
        _read_document(
            reader, text, {}, question,
            request=request, checkpoint_root=tmp_path / "checkpoints",
        )
    assert not list(tmp_path.rglob("*.yml"))


def _fake_codex_app_server(
    path: Path,
    capture_path: Path,
    *,
    mode: str = "success",
    mutate_auth: bool = False,
) -> None:
    payload = json.dumps(_valid_source_bundle_payload(), sort_keys=True)
    body = """#!__PYTHON__
import json, os, sys, time
from pathlib import Path

capture = Path(__CAPTURE__)
mode = __MODE__
messages = []
file_data = ""
for line in sys.stdin:
    if mode == "malformed" and not messages:
        print("not-json", flush=True)
        continue
    message = json.loads(line)
    messages.append(message)
    capture.write_text(json.dumps({"argv": sys.argv, "messages": messages}))
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {}}), flush=True)
    elif method == "thread/start":
        provider = "other" if mode == "wrong_provider" else "openai"
        print(json.dumps({"id": request_id, "result": {
            "model": "gpt-5.6-luna",
            "modelProvider": provider,
            "thread": {
                "id": "thread-1",
                "ephemeral": True,
                "modelProvider": provider,
            },
        }}), flush=True)
        if mode == "no_read":
            time.sleep(10)
    elif method == "thread/inject_items":
        file_data = message["params"]["items"][0]["content"][0]["file_data"]
        if mode == "reject":
            print(json.dumps({"id": request_id, "error": {"message": "unsupported input_file attachment"}}), flush=True)
        else:
            print(json.dumps({"id": request_id, "result": {}}), flush=True)
    elif method == "turn/start":
        if mode == "timeout":
            time.sleep(10)
            continue
        print(json.dumps({"id": request_id, "result": {"turn": {"id": "turn-1"}}}), flush=True)
        if mode in {"code_mode_warning", "code_mode_warning_tool", "unknown_warning"}:
            warning = (
                "Code Mode is unavailable because code-mode host is disabled. "
                "Code mode will fail closed; enable `features.code_mode_host` and "
                "install `codex-code-mode-host`."
                if mode != "unknown_warning"
                else "unrecognized private warning"
            )
            print(json.dumps({"method": "warning", "params": {
                "threadId": "thread-1",
                "message": warning,
            }}), flush=True)
        if mode in {"tool", "code_mode_warning_tool"}:
            print(json.dumps({"method": "item/completed", "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "item-1", "type": "commandExecution"},
            }}), flush=True)
            continue
        if mode == "reroute":
            print(json.dumps({"method": "model/rerouted", "params": {"fromModel": "gpt-5.6-luna", "toModel": "other"}}), flush=True)
            continue
        if mode == "server_request":
            print(json.dumps({"id": 99, "method": "item/tool/call", "params": {}}), flush=True)
            continue
        if mode == "raw_leak":
            print(json.dumps({"method": "thread/updated", "params": {"file_data": file_data}}), flush=True)
            continue
        if __MUTATE_AUTH__:
            (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text("mutated")
        event_thread = "thread-other" if mode == "wrong_thread" else "thread-1"
        event_turn = "turn-other" if mode == "wrong_turn" else "turn-1"
        print(json.dumps({"method": "thread/tokenUsage/updated", "params": {
            "threadId": event_thread,
            "turnId": event_turn,
            "tokenUsage": {"total": {
                "inputTokens": 12,
                "cachedInputTokens": 3,
                "cacheWriteInputTokens": 2,
                "outputTokens": 7,
                "reasoningOutputTokens": 1,
            }},
        }}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {
            "threadId": event_thread,
            "turnId": event_turn,
            "item": {"id": "item-1", "type": "agentMessage", "text": __PAYLOAD__},
        }}), flush=True)
        status = "mystery" if mode == "unknown_terminal" else "completed"
        print(json.dumps({"method": "turn/completed", "params": {
            "threadId": event_thread,
            "turn": {"id": event_turn, "status": status, "error": None},
        }}), flush=True)
        if mode == "post_terminal_tool":
            print(json.dumps({"method": "item/completed", "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "item-2", "type": "commandExecution"},
            }}), flush=True)
"""
    body = (
        body.replace("__PYTHON__", sys.executable)
        .replace("__CAPTURE__", repr(str(capture_path)))
        .replace("__MODE__", repr(mode))
        .replace("__MUTATE_AUTH__", repr(mutate_auth))
        .replace("__PAYLOAD__", repr(payload))
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_pdf_preflight(
    tmp_path: Path,
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    patch_hash = "b" * 64
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest_path = executable.with_name(executable.name + ".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "upstream_tag": "rust-v0.152.1",
                "upstream_commit": "5adb68a49933ae446bf11935662c83dba55a0804",
                "platform": "macos-arm64",
                "license": "Apache-2.0",
                "notice": "NOTICE",
                "input_file_protocol_revision": "input_file-v1",
                "patch_sha256": patch_hash,
                "binary_sha256": binary_hash,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "auto_zettelkasten.readers._CODEX_PDF_HELPER_TRUST",
        {"macos-arm64": frozenset({(patch_hash, binary_hash)})},
    )
    monkeypatch.setattr("auto_zettelkasten.readers.platform.system", lambda: "Darwin")
    monkeypatch.setattr("auto_zettelkasten.readers.platform.machine", lambda: "arm64")
    manifest = _codex_pdf_helper_manifest(executable)
    assert manifest is not None
    status = fake_codex_preflight(tmp_path, executable)
    status.update(
        version="0.152.1",
        helper_version="0.152.1+azpdf1",
        helper_manifest_valid=True,
        pdf_input_file_capability=True,
        _helper_manifest_identity=manifest,
    )
    return status


def test_codex_pdf_app_server_sends_exact_ordered_file_text_and_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture)
    pdf = tmp_path / "source.pdf"
    pdf_bytes = b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n"
    pdf.write_bytes(pdf_bytes)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    result = reader.read_source_bundle(
        "",
        {
            "_source_context": {
                "source_id": "source-zotero-A1",
                "zotero_key": "A1",
            }
        },
        attachment_paths=[pdf],
    )

    assert result["evidence_anchors"][0]["locator"] == "p. 1"
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["argv"][1] == "app-server"
    assert 'model_provider="openai"' in captured["argv"]
    assert 'openai_base_url="https://chatgpt.com/backend-api/codex"' in captured["argv"]
    assert 'chatgpt_base_url="https://chatgpt.com/backend-api/"' in captured["argv"]
    assert "model_providers.openai.request_max_retries=0" in captured["argv"]
    assert "model_providers.openai.stream_max_retries=0" in captured["argv"]
    requests = [row for row in captured["messages"] if row.get("id") is not None]
    assert [row["method"] for row in requests] == [
        "initialize",
        "thread/start",
        "thread/inject_items",
        "turn/start",
    ]
    thread = requests[1]["params"]
    assert thread["model"] == "gpt-5.6-luna"
    assert thread["ephemeral"] is True
    injected = requests[2]["params"]["items"]
    assert len(injected) == 1
    assert injected[0]["role"] == "user"
    assert [item["type"] for item in injected[0]["content"]] == [
        "input_file",
        "input_text",
    ]
    file_item = injected[0]["content"][0]
    assert file_item == {
        "type": "input_file",
        "filename": "source.pdf",
        "file_data": "data:application/pdf;base64,"
        + base64.b64encode(pdf_bytes).decode("ascii"),
        "detail": "auto",
    }
    assert injected[0]["content"][1]["type"] == "input_text"
    assert injected[0]["content"][1]["text"].startswith(
        "Follow the supplied output schema. Do not use tools."
    )
    turn = requests[3]["params"]
    assert turn["input"] == []
    assert turn["model"] == "gpt-5.6-luna"
    assert turn["effort"] == "medium"
    assert turn["approvalPolicy"] == "never"
    assert turn["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert turn["outputSchema"]["additionalProperties"] is False
    assert next(iter(turn["outputSchema"]["properties"])) == "evidence_anchors"
    assert current_provider_completion()["usage"] == {
        "input_tokens": 12,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 2,
        "output_tokens": 7,
        "reasoning_output_tokens": 1,
    }


def test_codex_pdf_app_server_accepts_only_the_expected_code_mode_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")

    def invoke(mode: str) -> dict[str, object]:
        _fake_codex_app_server(executable, capture, mode=mode)
        reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
        reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)
        return dict(
            reader.read_source_bundle(
                "",
                {"_source_context": {"source_id": "source-zotero-A1"}},
                attachment_paths=[pdf],
            )
        )

    assert invoke("code_mode_warning")["evidence_anchors"]
    with pytest.raises(ProviderIsolationFailure, match="warning"):
        invoke("unknown_warning")
    with pytest.raises(ProviderIsolationFailure, match="tool"):
        invoke("code_mode_warning_tool")


def test_codex_pdf_rejects_unverified_helper_before_process_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, tmp_path / "codex")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("unverified helper was started"),
    )

    with pytest.raises(ProviderUnsupportedAttachment, match="companion|helper"):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )


def test_codex_pdf_reserves_attempt_before_app_server_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    with deny_codex_attempts(), pytest.raises(
        CodexAttemptStateError, match="calls are forbidden"
    ):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )
    assert not capture.exists()


@pytest.mark.parametrize(
    ("mode", "failure_type", "match"),
    [
        ("tool", ProviderIsolationFailure, "tool"),
        ("reroute", ProviderIsolationFailure, "rerout"),
        ("server_request", ProviderIsolationFailure, "server request"),
        ("malformed", ProviderIsolationFailure, "invalid JSON"),
        ("unknown_terminal", ProviderIsolationFailure, "terminal"),
        ("raw_leak", ProviderIsolationFailure, "raw (?:document|PDF)"),
        ("wrong_provider", ProviderIsolationFailure, "model or provider"),
        ("wrong_thread", ProviderIsolationFailure, "another thread"),
        ("wrong_turn", ProviderIsolationFailure, "another turn"),
        ("post_terminal_tool", ProviderIsolationFailure, "after turn completion"),
    ],
)
def test_codex_pdf_app_server_fails_closed_on_protocol_violations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure_type: type[Exception],
    match: str,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mode=mode)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    with pytest.raises(failure_type, match=match) as raised:
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )
    diagnostic = str(raised.value)
    assert "private-pdf-sentinel" not in diagnostic
    assert base64.b64encode(pdf.read_bytes()).decode("ascii") not in diagnostic


def test_codex_pdf_app_server_types_preinference_attachment_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mode="reject")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    with pytest.raises(ProviderUnsupportedAttachment, match="unsupported"):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )


def test_codex_pdf_app_server_rejects_child_auth_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mutate_auth=True)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    with pytest.raises(ProviderIsolationFailure, match="authentication state"):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )


def test_codex_pdf_app_server_timeout_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mode="timeout")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    token = _SOURCE_BUNDLE_ATTACHMENTS.set((pdf,))
    try:
        with pytest.raises(ProviderTimeout):
            reader._generate_with_reasoning(
                "system",
                "user",
                2_048,
                0.05,
                reasoning_effort="medium",
                output_contract="source_bundle",
            )
    finally:
        _SOURCE_BUNDLE_ATTACHMENTS.reset(token)


def test_codex_pdf_app_server_write_timeout_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mode="no_read")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 2_000_000 + b"\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    token = _SOURCE_BUNDLE_ATTACHMENTS.set((pdf,))
    try:
        with pytest.raises(ProviderTimeout):
            reader._generate_with_reasoning(
                "system",
                "user",
                2_048,
                0.1,
                reasoning_effort="medium",
                output_contract="source_bundle",
            )
    finally:
        _SOURCE_BUNDLE_ATTACHMENTS.reset(token)


def test_codex_pdf_app_server_worker_start_failure_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)
    real_start = threading.Thread.start
    starts = 0

    def fail_second_start(worker: threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("synthetic worker failure")
        real_start(worker)

    monkeypatch.setattr(threading.Thread, "start", fail_second_start)
    with pytest.raises(ProviderTransportError, match="worker could not start"):
        reader.read_source_bundle(
            "",
            {"_source_context": {"source_id": "source-zotero-A1"}},
            attachment_paths=[pdf],
        )
    assert cancel_active_provider_responses() == 0


def test_codex_pdf_app_server_external_cancellation_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    capture = tmp_path / "capture.json"
    _fake_codex_app_server(executable, capture, mode="timeout")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\nprivate-pdf-sentinel\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = _fake_pdf_preflight(tmp_path, executable, monkeypatch)

    def invoke() -> object:
        token = _SOURCE_BUNDLE_ATTACHMENTS.set((pdf,))
        try:
            return reader._generate_with_reasoning(
                "system",
                "user",
                2_048,
                10,
                reasoning_effort="medium",
                output_contract="source_bundle",
            )
        finally:
            _SOURCE_BUNDLE_ATTACHMENTS.reset(token)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoke)
        deadline = time.monotonic() + 2
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert capture.exists()
        assert cancel_active_provider_responses() == 1
        with pytest.raises(ProviderInterrupted):
            future.result()


def test_codex_relationship_completion_binds_emitted_pair_schema(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture)
    reader = CodexReader("gpt-5.6-terra", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable, {})
    pair_ids = ("job-a", "job-b")
    pools = {key: {"source_a": ["anchor-a"], "source_b": [f"{key}-anchor"]} for key in pair_ids}
    token = readers_module._RELATIONSHIP_PAIR_JOB_IDS.set(pair_ids)
    anchor_token = readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.set(pools)
    try:
        value = reader._generate_with_reasoning(
            "system", "user", 2_048, 5, reasoning_effort="medium",
            output_contract="relationship_adjudication",
        )
    finally:
        readers_module._RELATIONSHIP_PAIR_ANCHOR_IDS.reset(anchor_token)
        readers_module._RELATIONSHIP_PAIR_JOB_IDS.reset(token)
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert json.loads(captured["output_schema"]) == _codex_json_schema(
        "relationship_adjudication", pair_job_ids=pair_ids, pair_anchor_ids=pools,
    )
    assert value.completion["request_schema_hash"] == hashlib.sha256(
        captured["output_schema"].encode("utf-8")
    ).hexdigest()
    assert value.completion["request_schema_policy"] == "required-pair-keys-connections-v4"


@pytest.mark.parametrize("contract_id", sorted(CODEX_OUTPUT_CONTRACTS))
def test_codex_transport_is_sanitized_schema_bound_and_tool_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_id: str,
) -> None:
    executable = tmp_path / "codex"
    capture = tmp_path / "capture.json"
    _fake_codex(executable, capture, mutate_codex_home=True)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(
        tmp_path, executable, {"PATH": "preflight-snapshot"}
    )
    reader._preflight["version"] = "0.152.1"
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
        output_contract=contract_id,
    )
    assert json.loads(value)["summary"] == "ok"
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["output_schema"] == json.dumps(
        _codex_json_schema(contract_id), sort_keys=contract_id != "source_bundle"
    )
    assert hashlib.sha256(captured["output_schema"].encode()).hexdigest() == (
        codex_contract_identity(contract_id, reader.model, "high", "0.152.1")["schema_hash"]
    )
    if contract_id == "source_bundle":
        assert next(iter(json.loads(captured["output_schema"])["properties"])) == "evidence_anchors"
        fields = json.loads(captured["output_schema"])["properties"]
        assert fields["evidence_anchors"]["maxItems"] == 24
        assert fields["literature_positions"]["maxItems"] == 8
    assert "--output-schema" in captured["argv"]
    assert 'forced_login_method="chatgpt"' in captured["argv"]
    assert "skills.bundled.enabled=false" in captured["argv"]
    assert "skills.include_instructions=false" in captured["argv"]
    assert "features.code_mode_host=false" in captured["argv"]
    assert captured["argv"][-len(_codex_tool_feature_arguments("0.152.1")):] == list(
        _codex_tool_feature_arguments("0.152.1")
    )
    assert 'model_provider="openai"' in captured["argv"]
    assert 'openai_base_url="https://chatgpt.com/backend-api/codex"' in captured["argv"]
    assert 'chatgpt_base_url="https://chatgpt.com/backend-api/"' in captured["argv"]
    assert "model_providers.openai.request_max_retries=0" in captured["argv"]
    assert "model_providers.openai.stream_max_retries=0" in captured["argv"]
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


@pytest.mark.parametrize("contract_id", sorted(CODEX_OUTPUT_CONTRACTS))
def test_codex_only_source_bundle_identity_tracks_property_order(
    monkeypatch: pytest.MonkeyPatch, contract_id: str,
) -> None:
    before = codex_contract_identity(contract_id, "gpt-5.6-luna", "medium")
    contract = dict(CODEX_OUTPUT_CONTRACTS[contract_id])
    contract["properties"] = dict(reversed(list(contract["properties"].items())))
    monkeypatch.setitem(CODEX_OUTPUT_CONTRACTS, contract_id, contract)
    after = codex_contract_identity(contract_id, "gpt-5.6-luna", "medium")

    assert (before != after) is (contract_id == "source_bundle")


def test_codex_early_stdin_close_is_typed_transport_failure(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\nimport os, time\nos.close(0)\ntime.sleep(2)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable, {})
    reader._preflight["version"] = "0.152.1"

    with pytest.raises(ProviderTransportError, match="CLI transport failed"):
        reader._generate_with_reasoning(
            "system",
            "user" * 300_000,
            2_048,
            5,
            reasoning_effort="medium",
            output_contract="chunk_evidence",
        )


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
        (
            "Code Mode is unavailable because code-mode host is disabled. "
            "Code mode will fail closed; enable `features.code_mode_host` and install "
            "`codex-code-mode-host`."
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
        (
            "Code Mode is unavailable because code-mode host is disabled. "
            "Code mode will fail closed; enable `features.code_mode_host` and install "
            "`codex-code-mode-host`.",
            "code_mode_disabled",
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


def _fake_public_map_codex(path: Path, calls_path: Path) -> Path:
    exec_path = path.with_name(path.name + "-exec")
    app_server_path = path.with_name(path.name + "-app-server")
    app_server_capture = path.with_name(path.name + "-app-server.json")
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
            "decision": "relationship",
            "relation_type": "complements",
            "actor_source_id": None,
            "reference_source_id": None,
            "reason": "The sources contribute complementary institutional implementation evidence.",
            "bridge_job_id": job["bridge_job_id"],
            "rank": rank,
        }})
    if not jobs and len(ids) >= 2:
        candidates.append({{
            "left_source_id": ids[0],
            "right_source_id": ids[1],
            "decision": "relationship",
            "relation_type": "complements",
            "actor_source_id": None,
            "reference_source_id": None,
            "reason": "The sources contribute complementary institutional implementation evidence.",
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
    if schema["properties"]["decisions"]["type"] == "object":
        payload["decisions"] = {{
            row["pair_job_id"]: {{
                "assessment": "The sources contribute complementary bounded evidence.",
                "connections": row["connections"],
                "confidence": "high",
            }}
            for row in payload["decisions"]
        }}
elif contract == "cluster_synthesis":
    cluster = user.get("context", {{}}).get("cluster", {{}})
    member_ids = sorted(cluster.get("source_ids", []))
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
    exec_path.write_text(body, encoding="utf-8")
    exec_path.chmod(0o755)
    _fake_codex_app_server(app_server_path, app_server_capture)
    wrapper = f'''#!{sys.executable}
import os, sys

target = {str(app_server_path)!r} if sys.argv[1:2] == ["app-server"] else {str(exec_path)!r}
os.execv(target, [target, *sys.argv[1:]])
'''
    path.write_text(wrapper, encoding="utf-8")
    path.chmod(0o755)
    return app_server_capture


def _synthetic_blank_pdf(title: str) -> bytes:
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    writer.write(stream)
    return stream.getvalue()


def test_public_codex_map_runs_relationships_and_replays_without_calls_or_semantic_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_items: list[dict[str, object]],
) -> None:
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    executable = provider_root / "codex"
    calls_path = provider_root / "calls.jsonl"
    app_server_capture = _fake_public_map_codex(executable, calls_path)
    workspace = tmp_path / "workspace"
    preflight_calls: list[tuple[str, tuple[str, ...]]] = []
    pdf_bytes = _synthetic_blank_pdf("Institutions and Reform")

    class MixedSourceZotero(FakeZotero):
        def children(self, item_key: str) -> list[dict[str, object]]:
            if item_key != "ITEMA":
                return super().children(item_key)
            self.children_calls += 1
            return [
                {
                    "key": "ITEMAPDF",
                    "data": {
                        "key": "ITEMAPDF",
                        "parentItem": "ITEMA",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": "Institutions and Reform.pdf",
                        "title": "Full Text PDF",
                    },
                }
            ]

        def fulltext(self, item_key: str) -> dict[str, object] | None:
            if item_key != "ITEMAPDF":
                return super().fulltext(item_key)
            self.fulltext_calls += 1
            return None

        def file(self, item_key: str) -> tuple[bytes, str] | None:
            if item_key != "ITEMAPDF":
                return super().file(item_key)
            self.file_calls += 1
            return pdf_bytes, "application/pdf"

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
        return _fake_pdf_preflight(tmp_path, executable, monkeypatch)

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
        client=MixedSourceZotero(items),
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
        "source_bundle": 1,
        "relationship_candidate_selection": 1,
        "literature_family_plan": 1,
        "cluster_synthesis": 1,
    }
    assert {row["model"] for row in calls if row["contract"] == "source_bundle"} == {"gpt-5.6-luna"}
    assert any(row["contract"] == "literature_family_plan" for row in calls)
    assert any(row["contract"] == "relationship_candidate_selection" for row in calls)
    assert not any(row["contract"] == "relationship_adjudication" for row in calls)
    assert any(row["contract"] == "cluster_synthesis" for row in calls)
    captured = json.loads(app_server_capture.read_text(encoding="utf-8"))
    requests = [row for row in captured["messages"] if row.get("id") is not None]
    assert [row["method"] for row in requests] == [
        "initialize",
        "thread/start",
        "thread/inject_items",
        "turn/start",
    ]
    assert requests[1]["params"]["model"] == "gpt-5.6-luna"
    input_file = requests[2]["params"]["items"][0]["content"][0]
    assert input_file["type"] == "input_file"
    assert input_file["filename"] == "ITEMAPDF.pdf"
    assert base64.b64decode(input_file["file_data"].split(",", 1)[1]) == pdf_bytes
    route = read_yaml(
        workspace
        / "11_state"
        / "runs"
        / "public-codex-map"
        / "items"
        / "ITEMA"
        / "document_route.yml"
    )
    assert route["identity_payload"]["route"] == "codex_pdf_input_file"
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
    before_app_server_capture = app_server_capture.read_bytes()
    run_root = workspace / "11_state" / "runs" / "public-codex-map"
    semantic_roots = (
        workspace / "02_source_memory",
        workspace / "03_literature_synthesis",
        workspace / "11_state" / "relationship_jobs",
        workspace / "11_state" / "semantic_jobs",
        run_root / "items",
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
            client=MixedSourceZotero(items),
        )

    assert replay.status == "completed"
    assert replay.source_set["source_set_id"] == first.source_set["source_set_id"]
    assert replay.source_set["dependency_hash"] == first.source_set["dependency_hash"]
    assert preflight_calls == [("gpt-5.6-luna", ("gpt-5.6-terra",))]
    assert calls_path.read_bytes() == before_calls
    assert app_server_capture.read_bytes() == before_app_server_capture
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
