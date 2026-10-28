"""Naming-only source adapter tests; every transport is synthetic."""
from copy import deepcopy
import hashlib
import json

import pytest

import auto_zettelkasten.readers as readers
from auto_zettelkasten.community_naming_pipeline import prepare_two_stage_naming, finish_reconciliation


def local_job():
    text = "Mediation depends on institutional legitimacy."
    preview = prepare_two_stage_naming([{"community_id": "g", "source_ids": ["s"]}],
        [{"source_id": "s", "title": "Legitimacy and mediation", "text": text,
          "content_sha256": hashlib.sha256(text.encode()).hexdigest()}])
    return preview, preview["local_jobs"]["g"]


def response():
    return {"model": "gpt-6-luna", "service_tier": "default",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"labels": [
                {"community_id": "g", "title": "Mediation legitimacy", "scope": "Legitimacy in mediation."}]})}}]}


def test_constructor_has_no_environment_access_or_transport(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("credential access or dispatch during construction")
    monkeypatch.setattr(readers, "_post_json", forbidden)
    with monkeypatch.context() as patch:
        patch.setattr(readers.os._Environ, "get", forbidden)
        value = readers.OpenAICommunityNamingReader()
    assert value.name == "openai" and value.model == "gpt-6-luna"
    assert value.allow_cloud is False


@pytest.mark.parametrize("stage", ["local", "reconciliation"])
def test_wire_matches_exact_frozen_stage_and_is_single_attempt(monkeypatch, stage):
    preview, job = local_job()
    if stage == "reconciliation":
        job = finish_reconciliation(preview, {"g": {"community_id": "g", "title": "Mediation legitimacy",
                                                   "scope": "Legitimacy in mediation."}})
    captured = []
    def fake(endpoint, body, **kwargs):
        captured.append((endpoint, body, kwargs))
        return response()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-naming-key")
    monkeypatch.setattr(readers, "_post_json", fake)
    reader = readers.OpenAICommunityNamingReader(allow_cloud=True)
    payload = job["request_payload"]
    actual = reader.name_community_stage(payload["messages"][0]["content"], payload["messages"][1]["content"],
                                         stage=stage, output_tokens=job["output_token_limit"])
    assert "labels" in actual
    assert len(captured) == 1
    endpoint, body, options = captured[0]
    assert endpoint == "https://api.openai.com/v1/chat/completions"
    assert body == payload
    assert options["max_attempts"] == 1
    assert callable(options["request_opener"])
    assert any(type(handler).__name__ == "_NoRedirect"
               for handler in options["request_opener"].__self__.handlers)
    assert options["headers"] == {"Authorization": "Bearer synthetic-naming-key"}
    assert body["stream"] is False and body["store"] is False
    assert body["reasoning_effort"] == "none"
    assert "temperature" not in body and "max_tokens" not in body


@pytest.mark.parametrize("stage,cap", [("invalid", 384), ("local", 1024), ("reconciliation", 384), ("local", True)])
def test_invalid_stage_or_output_cap_fails_before_key_or_dispatch(monkeypatch, stage, cap):
    monkeypatch.setattr(readers, "_post_json", lambda *_a, **_k: pytest.fail("provider reached"))
    with pytest.raises(readers.ProviderError, match="contract"):
        readers.OpenAICommunityNamingReader(allow_cloud=True).name_community_stage("system", "{}", stage=stage, output_tokens=cap)


def test_cloud_permission_and_key_are_separate_gates(monkeypatch):
    monkeypatch.setattr(readers, "_post_json", lambda *_a, **_k: pytest.fail("provider reached"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(readers.CloudPermissionError):
        readers.OpenAICommunityNamingReader().name_community_stage("system", "{}", stage="local", output_tokens=384)
    with pytest.raises(readers.ProviderError, match="not configured"):
        readers.OpenAICommunityNamingReader(allow_cloud=True).name_community_stage("system", "{}", stage="local", output_tokens=384)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 121])
def test_invalid_timeout_rejected(value):
    with pytest.raises(ValueError, match="timeout"):
        readers.OpenAICommunityNamingReader(timeout=value)


def test_model_upgrade_is_not_implicit():
    with pytest.raises(ValueError, match="pinned model"):
        readers.OpenAICommunityNamingReader(model="gpt-6-sol")


@pytest.mark.parametrize("kind", ["model", "tier", "tokens", "negative-input", "cache-write", "truncated", "missing", "json"])
def test_bad_provider_envelope_never_passes_stage_boundary(monkeypatch, kind):
    result = deepcopy(response())
    if kind == "model":
        result["model"] = "other-model"
    elif kind == "tier":
        result["service_tier"] = "fast"
    elif kind == "tokens":
        result["usage"]["completion_tokens"] = 385
    elif kind == "negative-input":
        result["usage"]["prompt_tokens"] = -1
    elif kind == "cache-write":
        result["usage"]["prompt_tokens_details"]["cache_write_tokens"] = 1
    elif kind == "truncated":
        result["choices"][0]["finish_reason"] = "length"
    elif kind == "missing":
        result["choices"] = []
    else:
        result["choices"][0]["message"]["content"] = "not JSON"
    calls = []
    def fake(*args, **kwargs):
        calls.append(1)
        return result
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-naming-key")
    monkeypatch.setattr(readers, "_post_json", fake)
    with pytest.raises(readers.ProviderError):
        readers.OpenAICommunityNamingReader(allow_cloud=True).name_community_stage("system", "{}", stage="local", output_tokens=384)
    assert len(calls) == 1


def test_provider_error_cannot_echo_a_credential(monkeypatch):
    def failed(*args, **kwargs):
        raise readers.ProviderError("synthetic-secret-marker")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-naming-key")
    monkeypatch.setattr(readers, "_post_json", failed)
    with pytest.raises(readers.ProviderError, match="no automatic retry") as error:
        readers.OpenAICommunityNamingReader(allow_cloud=True).name_community_stage("system", "{}", stage="local", output_tokens=384)
    assert "synthetic-secret-marker" not in str(error.value)
    assert error.value.__cause__ is None


def test_oversized_request_fails_without_credential_or_provider(monkeypatch):
    monkeypatch.setattr(readers, "_post_json", lambda *_a, **_k: pytest.fail("provider reached"))
    with pytest.raises(readers.ProviderError, match="context allowance"):
        readers.OpenAICommunityNamingReader(allow_cloud=True).name_community_stage("system", "x" * 131_072, stage="local", output_tokens=384)


def test_historical_source_reader_does_not_silently_change_effort_or_route(monkeypatch):
    monkeypatch.setattr(readers.CodexReader, "_authorize_request", lambda *_: pytest.fail("provider preflight"))
    with pytest.raises(readers.ProviderError, match="compatible"):
        readers.CodexReader(model="gpt-6-luna").name_community_stage("system", "{}", stage="local", output_tokens=384)
    assert readers.CODEX_OUTPUT_CONTRACTS["community_name"]["properties"]["title"]["type"] == "string"
    assert readers.CODEX_CONTRACT_RESERVATIONS["community_name"] == 128
    assert "community_name_local_v1" not in readers.CODEX_OUTPUT_CONTRACTS
    assert "community_name_local_v1" not in readers.CODEX_CONTRACT_RESERVATIONS
    assert readers.TWO_STAGE_COMMUNITY_NAMING_CONTRACTS["community_name_local_v1"]["properties"]["labels"]["items"]["properties"]["scope"]["type"] == ["string", "null"]
