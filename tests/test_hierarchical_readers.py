from __future__ import annotations

import io
import http.client
import inspect
import json
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from auto_zettelkasten.ports import HierarchicalReaderProvider, ReaderProvider
from auto_zettelkasten.readers import (
    CHUNK_EVIDENCE_KEYS,
    SECTION_KEYS,
    DeepSeekReader,
    GeminiReader,
    OllamaReader,
    OpenRouterReader,
    ProviderError,
    _post_json,
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


def _analysis() -> dict[str, str]:
    return {key: f"Grounded {key}; page 1." for key in SECTION_KEYS}


def _chunk_memo() -> dict[str, str]:
    return {key: f"Compact {key}; section A, page 2." for key in CHUNK_EVIDENCE_KEYS}


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
    assert 790_000 <= reader.direct_input_token_budget <= 800_000


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
        {"title": "Study", "section": "Findings", "page_start": 42, "page_end": 44},
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
    assert "chunk-0007" in prompt
    assert "Findings, pages 42-44" in prompt
    assert '"page_start": 42' in prompt
    assert "COARSE INSPECTED SOURCE CHUNK" in prompt


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


def test_retry_once_for_429_respects_retry_after_and_absolute_deadline(
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

    assert _post_json("https://provider.invalid", {}, timeout=5) == {"ok": True}
    assert attempts[0] == 2
    assert sleeps == [1.0]
    assert timeouts == pytest.approx([5.0, 4.0])


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


def test_network_timeout_is_retried_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="timed out"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 2


def test_5xx_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        if attempts[0] == 1:
            raise _http_error(503)
        return _Response({"ok": True})

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    assert _post_json("https://provider.invalid", {}, timeout=5) == {"ok": True}
    assert attempts[0] == 2


def test_non_timeout_network_error_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="provider unavailable"):
        _post_json("https://provider.invalid", {}, timeout=5)
    assert attempts[0] == 2


def test_incomplete_chunked_response_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]

    def urlopen(request, timeout):
        attempts[0] += 1
        if attempts[0] == 1:
            raise http.client.IncompleteRead(b"")
        return _Response({"ok": True})

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    assert _post_json("https://provider.invalid", {}, timeout=5) == {"ok": True}
    assert attempts[0] == 2


def test_retry_after_cannot_exceed_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = [0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: 0.0)

    def urlopen(request, timeout):
        attempts[0] += 1
        raise _http_error(503, retry_after="10")

    monkeypatch.setattr("auto_zettelkasten.readers.urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match="deadline exceeded"):
        _post_json("https://provider.invalid", {}, timeout=2)
    assert attempts[0] == 1


def test_streamed_response_read_obeys_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: clock[0])

    def advance_clock() -> None:
        clock[0] += 2

    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _Response({"ok": True}, on_read=advance_clock),
    )
    with pytest.raises(ProviderError, match="deadline exceeded"):
        _post_json("https://provider.invalid", {}, timeout=3)


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


def test_deepseek_stream_trickle_cannot_bypass_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    clock = [0.0]
    monkeypatch.setattr("auto_zettelkasten.readers.time.monotonic", lambda: clock[0])

    def advance_clock() -> None:
        clock[0] += 2.0

    monkeypatch.setattr(
        "auto_zettelkasten.readers.urllib.request.urlopen",
        lambda request, timeout: _SseResponse(
            [
                {"choices": [{"delta": {"content": "{"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "}"}, "finish_reason": "stop"}]},
                "[DONE]",
            ],
            on_read=advance_clock,
        ),
    )

    with pytest.raises(ProviderError, match="deadline exceeded"):
        DeepSeekReader(allow_cloud=True, request_deadline=3).read_source("source text", {"title": "Study"})
