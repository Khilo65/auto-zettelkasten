"""Provider-blocked checks for the private comparison transport."""
import json
import time

import pytest

from auto_zettelkasten import readers as r
from v030_linking_experiment_reader import (
    LINK_CONTRACT,
    LINKING_OUTPUT_ALLOWANCE,
    PLAN_CONTRACT,
    ExperimentCodexReader,
)


def reader(approach="direct", **kwargs):
    return ExperimentCodexReader(
        "gpt-5.6-terra", approach=approach, max_records=201,
        capability={"slug": "gpt-5.6-terra", "max_context_window": 872000,
                    "effective_context_window_percent": 95,
                    "supported_reasoning_levels": [{"effort": "max"}]},
        allow_cloud=True, **kwargs,
    )


def test_default_transport_unchanged_and_private_contract_identity():
    normal = r.CodexReader("gpt-5.6-terra")
    assert normal._request_deadline_seconds() == 600
    assert normal._codex_configuration_arguments() == ()
    assert normal._codex_request_schema(LINK_CONTRACT) == r._codex_json_schema(LINK_CONTRACT)
    assert normal._codex_execution_identity(LINK_CONTRACT, "max", "0.152.1") == r.codex_contract_identity(
        LINK_CONTRACT, normal.model, "max", "0.152.1")
    direct, planner = reader(), reader("planner")
    assert planner._codex_request_schema(LINK_CONTRACT) == normal._codex_request_schema(LINK_CONTRACT)
    assert planner._codex_request_schema(PLAN_CONTRACT) == normal._codex_request_schema(PLAN_CONTRACT)
    props = direct._codex_request_schema(LINK_CONTRACT)["properties"]
    assert set(props) == {"candidates"}
    assert set(props["candidates"]["items"]["required"]) == {
        "left_source_id", "right_source_id", "left_source_title", "right_source_title", "decision", "relation_type",
        "actor_source_id", "reference_source_id", "reason"}
    identity = direct._codex_execution_identity(LINK_CONTRACT, "max", "0.152.1")
    assert identity["output_reservation"] == LINKING_OUTPUT_ALLOWANCE
    assert identity["schema_hash"] != normal._codex_execution_identity(LINK_CONTRACT, "max", "0.152.1")["schema_hash"]
    assert identity["service_output_cap_supported"] is False
    assert direct._codex_configuration_arguments() == (
        "-c", "model_context_window=872000", "-c", "model_providers.openai.stream_idle_timeout_ms=14400000")
    assert direct._request_deadline_seconds() == 14400


def test_exact_wire_budget_counts_schema_exclusions_and_utf8():
    item = reader()
    system, user = item.direct_request([{"source_id": "a", "thesis": "界" * 700000}])
    assert item.request_fits(system, user, LINK_CONTRACT)
    estimate = item.estimate_request_input(system, user, LINK_CONTRACT)
    assert estimate > r._estimate_tokens(user)
    user += "x" * ((750000 - estimate) * 3)
    assert item.estimate_request_input(system, user, LINK_CONTRACT) == 750000
    assert item.request_fits(system, user, LINK_CONTRACT)
    assert not item.request_fits(system, user + "xxx", LINK_CONTRACT)
    assert not r.CodexReader("gpt-5.6-terra")._prompt_fits(system, user, LINKING_OUTPUT_ALLOWANCE)
    assert not item.request_fits(system, user, LINK_CONTRACT, 128000)
    with pytest.raises(ValueError, match="unique"):
        item.direct_request([{"source_id": "a"}, {"source_id": "a"}])
    with pytest.raises(r.ProviderError, match="outside"):
        item._reserved_output_tokens("source_bundle", 1)


def install_fake_transport(monkeypatch, item, output_tokens):
    item._preflight = {"_environment": {}, "version": "0.152.1"}
    monkeypatch.setattr(r, "_codex_model_catalog", lambda env: {item.model: item.catalog_capability})

    def fake(self, system, user, output, deadline):
        assert r._REASONING_EFFORT.get() == "max"
        assert output == LINKING_OUTPUT_ALLOWANCE
        assert 0 < deadline <= 14400
        return r._ProviderText('{"candidates": []}', {
            **self._codex_execution_identity(LINK_CONTRACT, "max", "0.152.1"),
            "usage": {"output_tokens": output_tokens, "reasoning_output_tokens": 123},
        })

    monkeypatch.setattr(r.CodexReader, "_generate_text", fake)


def test_max_effort_reservation_and_receipt_without_provider(monkeypatch):
    item = reader()
    install_fake_transport(monkeypatch, item, 500)
    assert item.select_direct_links([{"source_id": "a", "thesis": "evidence"}]) == {"candidates": []}
    completion = item.last_literature_completion
    assert completion["reasoning_effort"] == "max"
    assert completion["output_reservation"] == 65536
    assert completion["estimated_complete_input_tokens"] > 2048
    assert completion["configuration_arguments"] == [
        "-c", "model_context_window=872000", "-c", "model_providers.openai.stream_idle_timeout_ms=14400000"]
    assert completion["effective_child_deadline_seconds"] == 14400
    assert r._REASONING_EFFORT.get() is None


def test_child_uses_remaining_campaign_time_and_expiry_prevents_dispatch(monkeypatch):
    item = reader()
    install_fake_transport(monkeypatch, item, 500)
    item.campaign_expires_at = time.monotonic() + 1800
    item.select_direct_links([{"source_id": "a"}])
    assert 1790 < item.last_literature_completion["effective_child_deadline_seconds"] <= 1800
    item.campaign_expires_at = time.monotonic() - 1
    monkeypatch.setattr(r.CodexReader, "_generate_text", lambda *a: pytest.fail("expired dispatch"))
    with pytest.raises(r.ProviderTimeout, match="campaign deadline"):
        item.select_direct_links([{"source_id": "a"}])


def test_allowance_overrun_preserves_completed_result_without_retry(monkeypatch):
    item = reader()
    install_fake_transport(monkeypatch, item, 65537)
    with pytest.raises(r.ProviderError, match="allowance") as caught:
        item.select_direct_links([{"source_id": "a"}])
    assert caught.value.raw_response == '{"candidates": []}'
    assert caught.value.provider_completion["usage"]["output_tokens"] == 65537


def test_api_adapter_retains_lower_context_and_real_output_cap(monkeypatch):
    api = r.DeepSeekReader(allow_cloud=True, context_window_tokens=128000)
    assert not api._prompt_fits("s", "x" * 750000, 65536)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-not-a-key")
    bodies = []

    def fake_post(endpoint, body, **kwargs):
        bodies.append(body)
        return {"choices": [{"message": {"content": '{"candidates": []}'}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 30}}

    monkeypatch.setattr(r, "_post_json", fake_post)
    result = api._generate_with_reasoning("s", "u", 65536, 10, reasoning_effort="max")
    assert json.loads(result) == {"candidates": []}
    assert bodies[0]["max_tokens"] == 65536
    assert bodies[0]["reasoning_effort"] == "max"


def test_noncomparison_methods_fail_before_transport(monkeypatch):
    item = reader()
    monkeypatch.setattr(r.CodexReader, "_generate_text", lambda *a: pytest.fail("provider contacted"))
    for name in ("read", "read_document", "read_chunk", "read_source", "read_source_bundle",
                 "summarize_chunk", "synthesize_document", "synthesize_document_bundle", "profile_source",
                 "verify_atomic_claims", "adjudicate_relationships", "verify_relationships",
                 "propose_clusters", "plan_clusters", "synthesize_cluster", "map_debates", "detect_gaps"):
        with pytest.raises(r.ProviderError, match="outside"):
            getattr(item, name)()
