from __future__ import annotations

import io
import http.client
import inspect
import json
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from auto_zettelkasten import readers as readers_module
from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
from auto_zettelkasten.ports import HierarchicalReaderProvider, ReaderProvider
from auto_zettelkasten.readers import (
    CHUNK_EVIDENCE_KEYS,
    SECTION_KEYS,
    CodexReader,
    DeepSeekReader,
    GeminiReader,
    OllamaReader,
    OpenRouterReader,
    ProviderEmptyResponse,
    ProviderError,
    ProviderTransportError,
    _parse_analysis,
    _parse_chunk_evidence,
    _post_json,
    _read_openai_stream_response,
    _chunk_system_prompt,
    _source_bundle_system_prompt,
    _synthesis_prompt,
)


class _Response:
    def __init__(self, payload: dict[str, Any], *, on_read: Callable[[], None] | None = None) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.offset = 0
        self.on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self.on_read:
            self.on_read()
        if self.offset >= len(self.payload):
            return b""
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _ReadOneResponse(_Response):
    def read(self, size: int = -1) -> bytes:  # pragma: no cover - failure proves the wrong transport path
        raise AssertionError("bounded provider reads must use read1 when available")

    def read1(self, size: int = -1) -> bytes:
        return super().read(size)


class _SseResponse:
    def __init__(self, events: list[dict[str, Any] | str], *, on_read: Callable[[], None] | None = None) -> None:
        self.lines = [
            (f"data: {json.dumps(event)}\n\n" if isinstance(event, dict) else f"data: {event}\n\n").encode("utf-8")
            for event in events
        ]
        self.on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def readline(self) -> bytes:
        if self.on_read:
            self.on_read()
        return self.lines.pop(0) if self.lines else b""


class _InterruptedSseResponse(_SseResponse):
    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        raise OSError("connection reset")


def _analysis() -> dict[str, str]:
    return {key: f"Grounded {key}; page 1." for key in SECTION_KEYS}


def _chunk_memo() -> dict[str, str]:
    return {key: f"Compact {key}; section A, page 2." for key in CHUNK_EVIDENCE_KEYS}


def test_additive_fields_are_optional_in_direct_and_chunk_responses() -> None:
    for payload, parser in (
        (_analysis(), _parse_analysis),
        (_chunk_memo(), _parse_chunk_evidence),
    ):
        for key in (
            "key_concepts_and_definitions",
            "source_structure_and_organization",
            "source_visible_bibliographic_identity",
        ):
            payload.pop(key, None)
        assert not {
            "key_concepts_and_definitions",
            "source_structure_and_organization",
            "source_visible_bibliographic_identity",
        } & parser(payload).keys()
        payload.update(
            key_concepts_and_definitions="",
            source_structure_and_organization="",
            source_visible_bibliographic_identity="",
        )
        assert not {
            "key_concepts_and_definitions",
            "source_structure_and_organization",
            "source_visible_bibliographic_identity",
        } & parser(payload).keys()


def test_hierarchical_prompts_preserve_identity_quantity_and_locator_roles() -> None:
    chunk = _chunk_system_prompt()
    synthesis = _synthesis_prompt(
        [
            {
                **_chunk_memo(),
                "source_visible_bibliographic_identity": (
                    "Title: The Making of Israel; date: 1987; "
                    "edition: FIRST EDITION; ISBN: 0-394-55588-x."
                ),
            }
        ],
        {"title": "Supplied Zotero title", "date": "1988"},
        None,
    )

    assert "source_visible_bibliographic_identity" in chunk
    assert "distinguish them from supplied metadata" in chunk
    assert "duration, year, rank, page, or sample label" in chunk
    assert "exact noun, unit, and grammatical role" in chunk
    assert "coverage boundary, not a section boundary" in chunk
    assert "The Making of Israel" in synthesis
    assert "source-visible identity" in synthesis
    assert "earliest supported section start" in synthesis
    assert "preserve the supplied locators exactly without converting or adding a page coordinate" in synthesis


def test_journalism_prompt_separates_interviews_from_nonresponses() -> None:
    prompt = _source_bundle_system_prompt()

    assert "people interviewed" in prompt
    assert "merely contacted" in prompt
    assert "source's actual knowledge basis" in prompt


def _openai_response(content: dict[str, str], *, fenced: bool = False) -> dict[str, Any]:
    encoded = json.dumps(content)
    if fenced:
        encoded = f"```json\n{encoded}\n```"
    return {"choices": [{"message": {"content": encoded}}]}


def _http_error(status: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "https://provider.invalid",
        status,
        "error",
        headers,
        io.BytesIO(b"provider error"),
    )


def test_builtins_preserve_reader_protocol_and_add_hierarchical_protocol() -> None:
    readers = [
        DeepSeekReader(),
        OpenRouterReader("openai/gpt-4.1-mini"),
        GeminiReader(),
        OllamaReader(),
    ]
    assert all(isinstance(reader, ReaderProvider) for reader in readers)
    assert all(isinstance(reader, HierarchicalReaderProvider) for reader in readers)


def test_hierarchical_method_signatures_match_pipeline_contract() -> None:
    summarize_parameters = list(inspect.signature(DeepSeekReader.summarize_chunk).parameters)
    synthesis_parameters = list(inspect.signature(DeepSeekReader.synthesize_document).parameters)
    read_parameters = list(inspect.signature(DeepSeekReader.read_source).parameters)

    assert read_parameters == ["self", "text", "metadata", "question"]
    assert summarize_parameters == [
        "self",
        "text",
        "metadata",
        "question",
        "chunk_id",
        "locator",
        "max_output_tokens",
        "deadline_seconds",
    ]
    assert synthesis_parameters == [
        "self",
        "chunk_memos",
        "metadata",
        "question",
        "max_output_tokens",
        "deadline_seconds",
    ]


def test_provider_context_capabilities_are_explicit_and_overrideable() -> None:
    deepseek = DeepSeekReader()
    assert deepseek.context_window_tokens == 1_000_000
    assert deepseek.capabilities["context_window_source"] == "model"
    assert OpenRouterReader("unknown/model").context_window_tokens == 128_000
    assert GeminiReader(model="unknown-gemini").context_window_tokens == 128_000

    ollama = OllamaReader()
    assert ollama.context_window_tokens == 32_768
    assert ollama.capabilities["context_window_source"] == "fallback"
    assert OllamaReader(context_window_tokens=65_536).context_window_tokens == 65_536


def test_deepseek_prefers_direct_read_well_beyond_sixty_thousand_characters() -> None:
    reader = DeepSeekReader()
    metadata = {"title": "Large source"}

    assert reader.reading_strategy("x" * 240_000, metadata) == "direct"
    assert reader.reading_strategy("x" * 1_090_074, metadata) == "direct"
    assert reader.reading_strategy("x" * 2_500_000, metadata) == "hierarchical"
    assert 490_000 <= reader.direct_input_token_budget <= 500_000


def test_deepseek_chunk_prompt_parsing_and_per_call_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: list[tuple[dict[str, Any], float]] = []

    def urlopen(request, timeout):
        captured.append((json.loads(request.data), timeout))
        return _Response(_openai_response(_chunk_memo(), fenced=True))

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    reader = DeepSeekReader(
        allow_cloud=True,
        request_deadline=10,
        max_output_tokens=700,
        chunk_output_tokens=250,
    )
    result = reader.summarize_chunk(
        "## Findings\n[Page 42]\nThe reported effect was conditional.",
        {
            "title": "Study",
            "section": "Findings",
            "page_start": 42,
            "page_end": 44,
            "_source_context": {
                "ordinal_to_printed_page": {"42": "32", "43": "33", "44": "34"}
            },
        },
        "What qualifies the finding?",
        chunk_id="chunk-0007",
        locator="Findings, pages 42-44",
        max_output_tokens=2_000,
        deadline_seconds=3,
    )

    assert result == _chunk_memo()
    body, timeout = captured[0]
    assert body["max_tokens"] == 700
    assert timeout == pytest.approx(3, abs=1e-4)
    prompt = body["messages"][1]["content"]
    system_prompt = body["messages"][0]["content"]
    assert "chunk-0007" in prompt
    assert "Findings, pages 42-44" in prompt
    assert '"page_start": 42' in prompt
    assert '"ordinal_to_printed_page": {"42": "32"' in prompt
    assert "COARSE INSPECTED SOURCE CHUNK" in prompt
    assert "key_concepts_and_definitions" in system_prompt
    assert "contiguous verbatim quotation without inserted ellipses" in system_prompt
    assert "source_structure_and_organization" in system_prompt
    assert "source-native headings or chapters" in system_prompt
    assert "physical PDF ordinals" in system_prompt
    assert "Use one page coordinate per citation" in system_prompt


def test_deepseek_synthesis_returns_pipeline_analysis_and_uses_final_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: list[dict[str, Any]] = []

    def urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _Response(_openai_response(_analysis()))

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    reader = DeepSeekReader(allow_cloud=True, max_output_tokens=900)
    result = reader.synthesize_document(
        [_chunk_memo(), _chunk_memo()],
        {"title": "Study"},
        max_output_tokens=600,
        deadline_seconds=5,
    )

    assert tuple(result) == SECTION_KEYS
    assert result == _analysis()
    assert captured[0]["max_tokens"] == 600
    assert "COARSE CHUNK EVIDENCE" in captured[0]["messages"][1]["content"]
    system_prompt = captured[0]["messages"][0]["content"]
    assert "Plain-English Interpretation" in system_prompt
    assert "what changed, by how much, compared with what" in system_prompt
    assert "naturally rather than as a compulsory checklist" in system_prompt
    assert "statistical_context" in captured[0]["messages"][1]["content"]


@pytest.mark.parametrize("reader_class", [DeepSeekReader, OpenRouterReader, GeminiReader, OllamaReader, CodexReader])
def test_source_bundle_synthesis_admission_is_independent_of_direct_planning(
    monkeypatch: pytest.MonkeyPatch, reader_class,
) -> None:
    model = {OpenRouterReader: "unknown/model", CodexReader: "gpt-5.6-luna"}
    reader = reader_class(
        **({"model": model[reader_class]} if reader_class in model else {}),
        context_window_tokens=272_000, direct_read_fraction=0.5, request_deadline=600,
    )
    original_capabilities = dict(reader.capabilities)
    original_fit = reader._ensure_prompt_fits
    fit_calls, generated = [], []

    class CapturedGeneration(Exception):
        pass

    def fit(system, user, output, **kwargs):
        fit_calls.append((system, user, output, kwargs))
        return original_fit(system, user, output, **kwargs)

    def generate(system, user, output, deadline):
        assert (system, user, output) == fit_calls[-1][:3]
        assert deadline == 600.0
        assert readers_module._OUTPUT_CONTRACT.get() == "source_bundle"
        assert readers_module._REASONING_EFFORT.get() == ("medium" if reader.name == "codex" else "high")
        generated.append((system, user, output))
        raise CapturedGeneration

    monkeypatch.setattr(reader, "_authorize_request", lambda: None)
    monkeypatch.setattr(reader, "_ensure_prompt_fits", fit)
    monkeypatch.setattr(reader, "_generate_text", generate)
    monkeypatch.setattr(readers_module.subprocess, "Popen", lambda *a, **k: pytest.fail("provider subprocess called"))
    monkeypatch.setattr(CodexReader, "_ensure_codex_preflight", lambda *a: pytest.fail("provider preflight called"))
    memo = {"summary": "字"}
    with deny_codex_attempts():
        with pytest.raises(CapturedGeneration):
            reader.synthesize_document_bundle([memo], {}, max_output_tokens=64_000, deadline_seconds=600)
        system, user, output, _ = fit_calls[-1]
        assert output == (32_768 if reader.name == "codex" else min(64_000, reader.capabilities["supported_output_tokens"]))
        usable = min(int(reader.context_window_tokens * 0.8), 200_000) if reader.name == "codex" else int(reader.context_window_tokens * 0.8)
        user_byte_limit = 3 * (usable - output - reader.prompt_reserve_tokens - readers_module._estimate_tokens(system))
        memo["summary"] += "x" * (user_byte_limit - len(user.encode("utf-8")))
        with pytest.raises(CapturedGeneration):
            reader.synthesize_document_bundle([memo], {}, max_output_tokens=64_000, deadline_seconds=600)
        system, user, output, kwargs = fit_calls[-1]
        assert kwargs["context_fraction"] == 0.8
        assert readers_module._estimate_tokens(system) + readers_module._estimate_tokens(user) + output + reader.prompt_reserve_tokens == usable
        assert not reader._prompt_fits(system, user, output)
        memo["summary"] += "x"
        with pytest.raises(ProviderError, match="hierarchical source analysis bundle exceeds"):
            reader.synthesize_document_bundle([memo], {}, max_output_tokens=64_000, deadline_seconds=600)
    assert len(generated) == 2
    assert dict(reader.capabilities) == original_capabilities


@pytest.mark.parametrize("reader_class", [DeepSeekReader, OpenRouterReader, GeminiReader, OllamaReader, CodexReader])
def test_hierarchical_bundle_excludes_global_navigation_fragments(
    monkeypatch: pytest.MonkeyPatch, reader_class,
) -> None:
    model = {OpenRouterReader: "unknown/model", CodexReader: "gpt-5.6-luna"}
    reader = reader_class(**({"model": model[reader_class]} if reader_class in model else {}))
    retained = '### Observed findings\nThe comparison was inconclusive. Locator: p. vii.'
    memos = [{"summary": retained, "locators": "p. vii"}]
    fragments = ["9. Unselected numbered proposition", "Table 4. Unselected outcome", "Figure 5. Unselected mechanism"]
    metadata = {"title": "Generic source", "_source_context": {
        "source_id": "generic-source", "source_scope": "partial_document", "unresolved_pages": [9],
        "ordinal_to_printed_page": {"7": "vii"},
        **{key: [{"label": label, "page_ordinal": 9}] for key, label in zip(
            ("heading_spans", "table_spans", "figure_spans"), fragments)},
    }}
    before = json.dumps([memos, metadata])
    default_prompt = readers_module._source_bundle_prompt(retained, metadata, None)
    fit_calls, generated = [], []
    original_fit = reader._ensure_prompt_fits

    class CapturedGeneration(Exception):
        pass

    def fit(system, user, output, **kwargs):
        fit_calls.append((system, user, output))
        return original_fit(system, user, output, **kwargs)

    def generate(system, user, output, deadline):
        assert (system, user, output) == fit_calls[-1]
        generated.append(user)
        raise CapturedGeneration

    monkeypatch.setattr(reader, "_authorize_request", lambda: None)
    monkeypatch.setattr(reader, "_ensure_prompt_fits", fit)
    monkeypatch.setattr(reader, "_generate_text", generate)
    monkeypatch.setattr(readers_module.subprocess, "Popen", lambda *a, **k: pytest.fail("provider subprocess called"))
    monkeypatch.setattr(CodexReader, "_ensure_codex_preflight", lambda *a: pytest.fail("provider preflight called"))
    with deny_codex_attempts():
        with pytest.raises(CapturedGeneration):
            reader.synthesize_document_bundle(memos, metadata)
        hierarchy = generated[-1]
        assert all(fragment not in hierarchy for fragment in fragments)
        assert json.dumps(memos, ensure_ascii=False) in hierarchy
        assert '"ordinal_to_printed_page": {"7": "vii"}' in hierarchy
        assert '"source_id": "generic-source"' in hierarchy
        assert '"unresolved_pages": [9]' in hierarchy
        with pytest.raises(CapturedGeneration):
            reader.read_source_bundle(retained, metadata)
    assert generated[-1] == default_prompt
    assert all(fragment in default_prompt for fragment in fragments)
    assert readers_module._source_bundle_prompt(retained, metadata, None) == default_prompt
    assert json.dumps([memos, metadata]) == before


@pytest.mark.parametrize("provider", ["gemini", "ollama"])
def test_gemini_and_ollama_apply_hierarchical_output_caps(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    captured: list[dict[str, Any]] = []
    if provider == "gemini":
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        reader = GeminiReader(allow_cloud=True, max_output_tokens=500)
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps(_chunk_memo())}]}}]}
    else:
        reader = OllamaReader(max_output_tokens=500)
        response = {"message": {"content": json.dumps(_chunk_memo())}}

    def urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _Response(response)

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    assert reader.summarize_chunk("page-aware chunk", {}, max_output_tokens=200) == _chunk_memo()
    if provider == "gemini":
        assert captured[0]["generationConfig"]["maxOutputTokens"] == 200
    else:
        assert captured[0]["options"]["num_predict"] == 200


def test_429_is_not_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    sleeps: list[float] = []
    timeouts: list[float] = []
    attempts = [0]

    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def urlopen(request, timeout):
        attempts[0] += 1
        timeouts.append(timeout)
        if attempts[0] == 1:
            raise _http_error(429, retry_after="1")
        return _Response({"ok": True})

    monkeypatch.setattr("auto_zettelkasten.readers.time.sleep", sleep)
    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)

    with pytest.raises(ProviderError, match="HTTP 429"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1
    assert sleeps == []
    assert timeouts == pytest.approx([5.0])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_invalid_and_auth_4xx_are_never_retried(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        raise _http_error(status)

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match=rf"HTTP {status}"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1


def test_network_timeout_is_not_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="timed out"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1


def test_5xx_is_not_retried_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        if attempts[0] == 1:
            raise _http_error(503)
        return _Response({"ok": True})

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="HTTP 503"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1


def test_non_timeout_network_error_is_not_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="provider unavailable"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1


def test_incomplete_chunked_response_is_not_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        if attempts[0] == 1:
            raise http.client.IncompleteRead(b"")
        return _Response({"ok": True})

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="connection interrupted"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 1


def test_retry_after_cannot_exceed_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: 0.0)

    def urlopen(request, timeout):
        attempts[0] += 1
        raise _http_error(503, retry_after="10")

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="HTTP 503"):
        _post_json("https://provider.invalid", {}, timeout=2)
    assert attempts[0] == 1


def test_active_json_response_may_exceed_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: clock[0])

    def advance_clock() -> None:
        clock[0] += 2

    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _Response({"ok": True}, on_read=advance_clock),
    )
    assert _post_json("https://provider.invalid", {}, timeout=3) == {"ok": True}


def test_chunked_provider_frames_use_single_read_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _ReadOneResponse({"ok": True}),
    )

    assert _post_json("https://provider.invalid", {}, timeout=3) == {"ok": True}


def test_deepseek_reports_truncated_finish_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    response = _openai_response(_analysis())
    response["choices"][0]["finish_reason"] = "length"
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _Response(response),
    )

    with pytest.raises(ProviderError, match="finish_reason=length"):
        DeepSeekReader(allow_cloud=True).read_source("source text", {"title": "Study"})


def test_deepseek_uses_streaming_json_and_reassembles_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    encoded = json.dumps(_analysis())
    captured: list[dict[str, Any]] = []

    def urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _SseResponse(
            [
                {
                    "choices": [
                        {
                            "delta": {"reasoning_content": "r" * 200_000, "content": encoded[:50]},
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {"content": encoded[50:]}, "finish_reason": "stop"}]},
                "[DONE]",
            ]
        )

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)

    assert DeepSeekReader(allow_cloud=True).read_source("source text", {"title": "Study"}) == _analysis()
    assert captured[0]["stream"] is True


def test_deepseek_empty_stream_preserves_diagnostics_without_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {
                    "id": "response-1",
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "private reasoning",
                                "content": "",
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "response-1",
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {"delta": {"content": ""}, "finish_reason": "stop"}
                    ],
                },
                {
                    "id": "response-1",
                    "model": "deepseek-v4-flash",
                    "usage": {"completion_tokens": 17},
                    "choices": [],
                },
                "[DONE]",
            ]
        ),
    )

    with pytest.raises(ProviderEmptyResponse) as raised:
        DeepSeekReader(allow_cloud=True).read_source(
            "source text", {"title": "Study"}
        )

    assert raised.value.raw_response == ""
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-1"
    assert completion["usage"] == {"completion_tokens": 17}
    assert completion["finish_reason"] == "stop"
    assert completion["event_count"] == 3
    assert completion["content_fragment_count"] == 0
    assert completion["content_characters"] == 0
    assert completion["reasoning_fragment_count"] == 1
    assert completion["response_bytes"] > 0
    assert len(completion["stream_sha256"]) == 64
    assert len(completion["content_sha256"]) == 64
    assert "private reasoning" not in json.dumps(completion)


def test_reasoning_only_stream_without_terminal_event_is_retryable_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {
                    "id": "response-interrupted",
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "private reasoning",
                                "content": "",
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            ]
        ),
    )

    with pytest.raises(ProviderTransportError) as raised:
        DeepSeekReader(allow_cloud=True).read_source(
            "source text", {"title": "Study"}
        )

    assert raised.value.transport_kind == "premature_stream_end"
    assert raised.value.retryable is True
    assert raised.value.retry_on_resume is True
    assert raised.value.raw_response == ""
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-interrupted"
    assert completion["finish_reason"] == ""
    assert completion["event_count"] == 1
    assert completion["content_fragment_count"] == 0
    assert completion["content_characters"] == 0
    assert completion["reasoning_fragment_count"] == 1
    assert completion["response_bytes"] > 0
    assert "private reasoning" not in json.dumps(completion)


def test_reasoning_only_stream_with_done_envelope_is_completed_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {
                    "id": "response-complete",
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "delta": {"reasoning_content": "private reasoning"},
                            "finish_reason": None,
                        }
                    ],
                },
                "[DONE]",
            ]
        ),
    )

    with pytest.raises(ProviderEmptyResponse) as raised:
        DeepSeekReader(allow_cloud=True).read_source(
            "source text", {"title": "Study"}
        )

    assert raised.value.raw_response == ""
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-complete"
    assert completion["finish_reason"] == ""
    assert completion["reasoning_fragment_count"] == 1
    assert completion["content_characters"] == 0
    assert "transport_kind" not in completion


def test_deepseek_malformed_stream_event_preserves_bounded_transport_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    malformed = "not-json-" + "x" * 1_000
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {"choices": [{"delta": {"content": "{"}}]},
                malformed,
            ]
        ),
    )

    with pytest.raises(ProviderError, match="invalid event") as raised:
        DeepSeekReader(allow_cloud=True).read_source(
            "source text", {"title": "Study"}
        )

    assert raised.value.raw_response == "{"
    completion = raised.value.provider_completion
    assert completion["event_count"] == 2
    assert len(completion["malformed_event_excerpt"]) == 512
    assert len(completion["malformed_event_sha256"]) == 64


def test_stream_byte_limit_preserves_accumulated_diagnostics() -> None:
    response = _SseResponse(
        [
            {
                "id": "response-1",
                "choices": [{"delta": {"content": "{"}}],
            },
            {"choices": [{"delta": {"content": "x" * 1_000}}]},
        ]
    )
    byte_limit = len(response.lines[0]) + 10

    with pytest.raises(ProviderError, match="configured output bound") as raised:
        _read_openai_stream_response(response, 10**12, byte_limit)

    assert raised.value.raw_response == "{"
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-1"
    assert completion["event_count"] == 1
    assert completion["content_fragment_count"] == 1
    assert completion["response_bytes"] > byte_limit


def test_stream_invalid_utf8_preserves_safe_diagnostics_without_excerpt() -> None:
    response = _SseResponse(
        [
            {
                "id": "response-1",
                "choices": [{"delta": {"content": "{"}}],
            }
        ]
    )
    response.lines.append(b"data: \xff\n\n")

    with pytest.raises(ProviderError, match="invalid UTF-8") as raised:
        _read_openai_stream_response(response, 10**12, 10_000)

    assert raised.value.raw_response == "{"
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-1"
    assert completion["event_count"] == 1
    assert len(completion["malformed_event_sha256"]) == 64
    assert "malformed_event_excerpt" not in completion


def test_interrupted_stream_preserves_accumulated_safe_diagnostics() -> None:
    response = _InterruptedSseResponse(
        [
            {
                "id": "response-1",
                "choices": [{"delta": {"content": "{"}}],
            }
        ]
    )

    with pytest.raises(ProviderError, match="stream read failed") as raised:
        _read_openai_stream_response(response, 10**12, 10_000)

    assert raised.value.raw_response == "{"
    completion = raised.value.provider_completion
    assert completion["response_id"] == "response-1"
    assert completion["event_count"] == 1
    assert completion["content_fragment_count"] == 1


def test_deepseek_active_stream_may_exceed_total_wall_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    clock = [0.0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: clock[0])

    def advance_clock() -> None:
        clock[0] += 2.0

    serialized = json.dumps(_analysis())
    midpoint = len(serialized) // 2
    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {
                    "choices": [
                        {
                            "delta": {"content": serialized[:midpoint]},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {"content": serialized[midpoint:]},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "[DONE]",
            ],
            on_read=advance_clock,
        ),
    )

    result = DeepSeekReader(allow_cloud=True, request_deadline=3).read_source(
        "source text", {"title": "Study"}
    )
    assert result["thesis"]
