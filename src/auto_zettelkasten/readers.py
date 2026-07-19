from __future__ import annotations

import base64
import email.utils
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from .files import require_loopback_http_url
from .models import (
    ClusterProposal,
    ClusterSynthesis,
    EvidenceProfile,
    GapRationale,
    LiteratureMapRequest,
    SynthesisAssertion,
)


class ProviderError(RuntimeError):
    pass


class CloudPermissionError(ProviderError):
    pass


SECTION_KEYS = (
    "thesis",
    "method_and_research_design",
    "evidence_and_data",
    "detailed_findings",
    "plain_english_interpretation",
    "strengths_and_contributions",
    "methodological_critique",
    "limitations",
    "what_this_source_can_support",
    "what_this_source_cannot_support",
    "locators",
)

CHUNK_EVIDENCE_KEYS = (
    "summary",
    "claims_and_findings",
    "statistical_context",
    "methods_and_data",
    "limitations",
    "locators",
)

DIRECT_READ_CONTEXT_FRACTION = 0.8
DEFAULT_PROMPT_RESERVE_TOKENS = 2_048
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_CHUNK_OUTPUT_TOKENS = 1_024
PROFILE_MAX_OUTPUT_TOKENS = 8_000
LITERATURE_MAX_OUTPUT_TOKENS = 8_000
CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS = 32_000
CLUSTER_SYNTHESIS_MAX_OUTPUT_TOKENS = 16_000
GAP_ADJUDICATION_MAX_OUTPUT_TOKENS = 32_000

MODEL_CONTEXT_WINDOWS: Mapping[tuple[str, str], int] = {
    ("deepseek", "deepseek-v4-flash"): 1_000_000,
    ("gemini", "gemini-2.5-flash"): 1_000_000,
}

PROVIDER_CONTEXT_WINDOW_DEFAULTS: Mapping[str, int] = {
    "deepseek": 128_000,
    "openrouter": 128_000,
    "gemini": 128_000,
    # Ollama model metadata is not available at construction time. This is a
    # deliberately conservative fallback and callers may override it.
    "ollama": 32_768,
}


def provider_from_name(name: str, model: str, *, allow_cloud: bool):
    normalized = name.strip().lower()
    if normalized == "deepseek":
        return DeepSeekReader(model=model, allow_cloud=allow_cloud)
    if normalized == "openrouter":
        return OpenRouterReader(model=model, allow_cloud=allow_cloud)
    if normalized == "gemini":
        return GeminiReader(model=model, allow_cloud=allow_cloud)
    if normalized == "ollama":
        return OllamaReader(model=model)
    raise ValueError(f"unknown reader provider: {name}")


def vision_provider_from_name(name: str, model: str, *, allow_cloud: bool):
    if name.strip().lower() != "gemini":
        raise ValueError("Gemini is the supported document-vision provider")
    return GeminiReader(model=model, allow_cloud=allow_cloud)


class _CapabilityAwareReader:
    profile_generation_route = "built_in_reader"
    name: str
    model: str
    context_window_tokens: int | None
    context_window_source: str
    direct_read_fraction: float
    prompt_reserve_tokens: int
    max_output_tokens: int
    chunk_output_tokens: int
    timeout: float
    request_deadline: float | None

    def _record_transport_attempt(self) -> None:
        self.transport_attempt_count = int(getattr(self, "transport_attempt_count", 0) or 0) + 1

    def _configure_capabilities(self) -> None:
        context_window, source = _resolve_context_window(
            self.name,
            self.model,
            self.context_window_tokens,
        )
        self.context_window_tokens = context_window
        self.context_window_source = source
        if not 0 < self.direct_read_fraction <= 1:
            raise ValueError("direct_read_fraction must be greater than 0 and at most 1")
        if self.prompt_reserve_tokens < 0:
            raise ValueError("prompt_reserve_tokens must be non-negative")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.chunk_output_tokens <= 0:
            raise ValueError("chunk_output_tokens must be positive")
        if self._request_deadline_seconds() <= 0:
            raise ValueError("request_deadline must be positive")

    @property
    def capabilities(self) -> Mapping[str, Any]:
        return {
            "context_window_tokens": int(self.context_window_tokens or 0),
            "context_window_source": self.context_window_source,
            "direct_read_fraction": self.direct_read_fraction,
            "direct_input_token_budget": self.direct_input_token_budget,
            "max_output_tokens": self.max_output_tokens,
            "chunk_output_tokens": self._chunk_token_cap,
            "request_deadline_seconds": self._request_deadline_seconds(),
            "supports_hierarchical_reading": True,
        }

    @property
    def direct_input_token_budget(self) -> int:
        usable = int(int(self.context_window_tokens or 0) * self.direct_read_fraction)
        return max(0, usable - self.prompt_reserve_tokens - self.max_output_tokens)

    @property
    def _chunk_token_cap(self) -> int:
        return min(self.chunk_output_tokens, self.max_output_tokens)

    def estimate_input_tokens(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> int:
        return _estimate_tokens(_system_prompt()) + _estimate_tokens(_source_prompt(text, metadata, question))

    def should_read_directly(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> bool:
        return self._prompt_fits(
            _system_prompt(),
            _source_prompt(text, metadata, question),
            self.max_output_tokens,
        )

    def can_read_directly(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> bool:
        return self.should_read_directly(text, metadata, question)

    def reading_strategy(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> str:
        return "direct" if self.should_read_directly(text, metadata, question) else "hierarchical"

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _system_prompt()
        user_prompt = _source_prompt(text, metadata, question)
        self._ensure_prompt_fits(system_prompt, user_prompt, self.max_output_tokens, label="source")
        return _parse_analysis(
            self._generate_text(
                system_prompt,
                user_prompt,
                self.max_output_tokens,
                self._request_deadline_seconds(),
            )
        )

    @property
    def profile_reasoner_identity(self) -> str:
        return f"{type(self).__module__}.{type(self).__qualname__}:{self.name}:{self.model}"

    def profile_source(
        self,
        note: Mapping[str, Any],
        *,
        question: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Generate profile-prompt-v3 JSON from committed note text only."""

        del question  # A question is a projection lens, not part of the base evidence profile.
        self._authorize_request()
        prompt_version = str((context or {}).get("profile_prompt_version") or "3")
        if prompt_version != "3":
            raise ProviderError(f"unsupported profile prompt version: {prompt_version}")
        user_prompt = str(note.get("profile_prompt") or "").strip()
        if not user_prompt:
            raise ProviderError("profile_source requires a profile_prompt")
        system_prompt = _profile_system_prompt()
        # Evidence profiles contain many typed fields and can be materially
        # larger than the final atomic-note synthesis. Reusing the 3,000-token
        # note cap caused valid profile JSON to be truncated mid-object.
        output_tokens = max(self.max_output_tokens, PROFILE_MAX_OUTPUT_TOKENS)
        deadline_seconds = self._request_deadline_seconds()
        self._ensure_prompt_fits(system_prompt, user_prompt, output_tokens, label="evidence profile")
        return _parse_json_object(
            self._generate_text(system_prompt, user_prompt, output_tokens, deadline_seconds),
            label="profile response",
        )

    def propose_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _cluster_proposal_system_prompt()
        proposal_profiles = []
        for profile in profiles:
            raw_profile = _evidence_profile_payload(profile)
            if bool(raw_profile.get("excluded_from_synthesis", False)):
                continue
            if "analytical" in raw_profile and not bool(raw_profile.get("analytical")):
                continue
            proposal_profiles.append(_cluster_proposal_profile(raw_profile))
        user_prompt = _literature_prompt(
            proposal_profiles,
            request,
            _cluster_proposal_context(context),
            instruction="Propose a small set of coherent, overlapping collection clusters.",
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="cluster proposal",
                output_tokens=CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS,
            ),
            kind="cluster_proposal",
        )

    def map_debates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _debate_system_prompt()
        user_prompt = _literature_prompt(
            profiles,
            request,
            context,
            instruction="Map comparable positions, agreements, contradictions, and boundary conditions.",
        )
        return _validate_literature_response(
            self._literature_json_call(system_prompt, user_prompt, label="debate mapping"),
            kind="debate_mapping",
        )

    def synthesize_cluster(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _cluster_synthesis_system_prompt()
        repair_requirements = list((context or {}).get("repair_requirements", []) or [])
        instruction = (
            "Repair the prior cluster synthesis so every listed validation requirement passes. "
            "Return a substantive bottom-line verdict that answers the cluster question, covers every admitted "
            "proposition and core source, retains each source's important cluster-relevant contributions, distinguishes association from causation, and explains agreements, "
            "differences, weaknesses, and evidentiary limits. Preserve only supplied evidence and locators."
            if repair_requirements
            else "Synthesize this admitted cluster, retain important source-specific contributions even without multi-source agreement, and emit specific rule-bound gap hypotheses in the same response."
        )
        user_prompt = _literature_prompt(
            profiles,
            request,
            context,
            instruction=instruction,
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="cluster synthesis",
                output_tokens=CLUSTER_SYNTHESIS_MAX_OUTPUT_TOKENS,
            ),
            kind="cluster_synthesis",
        )

    def detect_gaps(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _gap_adjudication_system_prompt()
        user_prompt = _gap_adjudication_prompt(
            profiles,
            request,
            context,
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="gap adjudication",
                output_tokens=GAP_ADJUDICATION_MAX_OUTPUT_TOKENS,
            ),
            kind="gap_adjudication",
        )

    def _literature_json_call(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        label: str,
        output_tokens: int | None = None,
    ) -> Mapping[str, Any]:
        self.last_literature_response = None
        output_tokens = max(self.max_output_tokens, output_tokens or LITERATURE_MAX_OUTPUT_TOKENS)
        deadline_seconds = self._request_deadline_seconds()
        self._ensure_prompt_fits(system_prompt, user_prompt, output_tokens, label=label)
        response = _parse_json_object(
            self._generate_text(system_prompt, user_prompt, output_tokens, deadline_seconds),
            label=f"{label} response",
        )
        self.last_literature_response = response
        return response

    def summarize_chunk(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        chunk_id: str = "",
        locator: str = "",
        max_output_tokens: int | None = None,
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _chunk_system_prompt()
        user_prompt = _chunk_prompt(text, metadata, question, chunk_id, locator)
        output_tokens = self._bounded_output_tokens(max_output_tokens, default=self._chunk_token_cap)
        request_deadline = self._bounded_deadline(deadline_seconds)
        self._ensure_prompt_fits(system_prompt, user_prompt, output_tokens, label="coarse chunk")
        return _parse_chunk_evidence(
            self._generate_text(system_prompt, user_prompt, output_tokens, request_deadline)
        )

    def synthesize_document(
        self,
        chunk_memos: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        max_output_tokens: int | None = None,
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        if not chunk_memos:
            raise ValueError("chunk_memos must not be empty")
        system_prompt = _system_prompt()
        user_prompt = _synthesis_prompt(chunk_memos, metadata, question)
        output_tokens = self._bounded_output_tokens(max_output_tokens, default=self.max_output_tokens)
        request_deadline = self._bounded_deadline(deadline_seconds)
        self._ensure_prompt_fits(system_prompt, user_prompt, output_tokens, label="chunk synthesis")
        return _parse_analysis(
            self._generate_text(system_prompt, user_prompt, output_tokens, request_deadline)
        )

    def _prompt_fits(self, system_prompt: str, user_prompt: str, output_tokens: int) -> bool:
        estimated_input = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
        reserve = self.prompt_reserve_tokens + output_tokens
        usable = int(int(self.context_window_tokens or 0) * self.direct_read_fraction)
        return estimated_input + reserve <= usable

    def _ensure_prompt_fits(self, system_prompt: str, user_prompt: str, output_tokens: int, *, label: str) -> None:
        if not self._prompt_fits(system_prompt, user_prompt, output_tokens):
            raise ProviderError(
                f"{label} exceeds the {self.name} context budget; use coarse hierarchical chunks"
            )

    def _request_deadline_seconds(self) -> float:
        return float(self.request_deadline if self.request_deadline is not None else self.timeout)

    def _bounded_output_tokens(self, requested: int | None, *, default: int) -> int:
        if requested is None:
            return default
        if requested <= 0:
            raise ValueError("max_output_tokens must be positive")
        return min(requested, self.max_output_tokens)

    def _bounded_deadline(self, requested: float | None) -> float:
        configured = self._request_deadline_seconds()
        if requested is None:
            return configured
        if requested <= 0:
            raise ValueError("deadline_seconds must be positive")
        return min(float(requested), configured)

    def _authorize_request(self) -> None:
        raise NotImplementedError

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        raise NotImplementedError


@dataclass(slots=True)
class _OpenAICompatibleReader(_CapabilityAwareReader):
    name: str
    model: str
    endpoint: str
    api_key_env: str
    allow_cloud: bool
    is_cloud: bool = True
    timeout: float = 180.0
    request_deadline: float | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    chunk_output_tokens: int = DEFAULT_CHUNK_OUTPUT_TOKENS
    context_window_tokens: int | None = None
    direct_read_fraction: float = DIRECT_READ_CONTEXT_FRACTION
    prompt_reserve_tokens: int = DEFAULT_PROMPT_RESERVE_TOKENS
    context_window_source: str = field(init=False, default="")
    last_literature_response: dict[str, Any] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._configure_capabilities()

    def _authorize_request(self) -> None:
        if not self.allow_cloud:
            raise CloudPermissionError(f"{self.name} requires explicit allow_cloud consent")
        if not os.getenv(self.api_key_env):
            raise ProviderError(f"{self.api_key_env} is not configured")

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": output_tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        payload = _post_json(
            self.endpoint,
            body,
            headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
            timeout=deadline_seconds,
            response_byte_limit=_stream_response_byte_limit(output_tokens),
            on_attempt=self._record_transport_attempt,
            response_reader=_read_openai_stream_response,
        )
        try:
            choice = payload["choices"][0]
            finish_reason = str(choice.get("finish_reason") or "")
            if finish_reason and finish_reason != "stop":
                raise ProviderError(f"{self.name} response incomplete: finish_reason={finish_reason}")
            return choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} returned an unexpected response") from exc


class DeepSeekReader(_OpenAICompatibleReader):
    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        *,
        allow_cloud: bool = False,
        timeout: float = 180.0,
        request_deadline: float | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        chunk_output_tokens: int = DEFAULT_CHUNK_OUTPUT_TOKENS,
        context_window_tokens: int | None = None,
        direct_read_fraction: float = DIRECT_READ_CONTEXT_FRACTION,
        prompt_reserve_tokens: int = DEFAULT_PROMPT_RESERVE_TOKENS,
    ) -> None:
        super().__init__(
            name="deepseek",
            model=model,
            endpoint="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
            allow_cloud=allow_cloud,
            timeout=timeout,
            request_deadline=request_deadline,
            max_output_tokens=max_output_tokens,
            chunk_output_tokens=chunk_output_tokens,
            context_window_tokens=context_window_tokens,
            direct_read_fraction=direct_read_fraction,
            prompt_reserve_tokens=prompt_reserve_tokens,
        )


class OpenRouterReader(_OpenAICompatibleReader):
    def __init__(
        self,
        model: str,
        *,
        allow_cloud: bool = False,
        timeout: float = 180.0,
        request_deadline: float | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        chunk_output_tokens: int = DEFAULT_CHUNK_OUTPUT_TOKENS,
        context_window_tokens: int | None = None,
        direct_read_fraction: float = DIRECT_READ_CONTEXT_FRACTION,
        prompt_reserve_tokens: int = DEFAULT_PROMPT_RESERVE_TOKENS,
    ) -> None:
        super().__init__(
            name="openrouter",
            model=model,
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            api_key_env="OPENROUTER_API_KEY",
            allow_cloud=allow_cloud,
            timeout=timeout,
            request_deadline=request_deadline,
            max_output_tokens=max_output_tokens,
            chunk_output_tokens=chunk_output_tokens,
            context_window_tokens=context_window_tokens,
            direct_read_fraction=direct_read_fraction,
            prompt_reserve_tokens=prompt_reserve_tokens,
        )


@dataclass(slots=True)
class GeminiReader(_CapabilityAwareReader):
    model: str = "gemini-2.5-flash"
    allow_cloud: bool = False
    name: str = "gemini"
    is_cloud: bool = True
    timeout: float = 180.0
    request_deadline: float | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    chunk_output_tokens: int = DEFAULT_CHUNK_OUTPUT_TOKENS
    context_window_tokens: int | None = None
    direct_read_fraction: float = DIRECT_READ_CONTEXT_FRACTION
    prompt_reserve_tokens: int = DEFAULT_PROMPT_RESERVE_TOKENS
    context_window_source: str = field(init=False, default="")
    last_literature_response: dict[str, Any] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._configure_capabilities()

    def inspect_document(
        self,
        document: bytes,
        media_type: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        parts = [
            {"text": _system_prompt() + "\n\n" + _metadata_prompt(metadata, question)},
            {"inline_data": {"mime_type": media_type, "data": base64.b64encode(document).decode("ascii")}},
        ]
        return _parse_analysis(self._generate(parts, self.max_output_tokens))

    def _authorize_request(self) -> None:
        if not self.allow_cloud:
            raise CloudPermissionError("gemini requires explicit allow_cloud consent")
        if not os.getenv("GEMINI_API_KEY"):
            raise ProviderError("GEMINI_API_KEY is not configured")

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        return self._generate(
            [{"text": system_prompt + "\n\n" + user_prompt}],
            output_tokens,
            deadline_seconds,
        )

    def _generate(
        self,
        parts: list[dict[str, Any]],
        output_tokens: int,
        deadline_seconds: float | None = None,
    ) -> Any:
        key = os.environ["GEMINI_API_KEY"]
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model, safe='')}:generateContent?key={urllib.parse.quote(key, safe='')}"
        )
        payload = _post_json(
            endpoint,
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": output_tokens,
                    "responseMimeType": "application/json",
                },
            },
            timeout=deadline_seconds or self._request_deadline_seconds(),
            response_byte_limit=_response_byte_limit(output_tokens),
            on_attempt=self._record_transport_attempt,
        )
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("gemini returned an unexpected response") from exc


@dataclass(slots=True)
class OllamaReader(_CapabilityAwareReader):
    model: str = "llama3.2"
    base_url: str = "http://127.0.0.1:11434"
    name: str = "ollama"
    is_cloud: bool = False
    timeout: float = 300.0
    request_deadline: float | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    chunk_output_tokens: int = DEFAULT_CHUNK_OUTPUT_TOKENS
    context_window_tokens: int | None = None
    direct_read_fraction: float = DIRECT_READ_CONTEXT_FRACTION
    prompt_reserve_tokens: int = DEFAULT_PROMPT_RESERVE_TOKENS
    context_window_source: str = field(init=False, default="")
    last_literature_response: dict[str, Any] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        require_loopback_http_url(self.base_url, label="Ollama base_url")
        self._configure_capabilities()

    def _authorize_request(self) -> None:
        return None

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        payload = _post_json(
            f"{self.base_url.rstrip('/')}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0, "num_predict": output_tokens},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=deadline_seconds,
            response_byte_limit=_response_byte_limit(output_tokens),
            on_attempt=self._record_transport_attempt,
        )
        try:
            return payload["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("ollama returned an unexpected response") from exc


def _system_prompt() -> str:
    keys = ", ".join(SECTION_KEYS)
    return (
        "You create source-faithful atomic notes from inspected document content. "
        "Return only one JSON object. Do not infer facts absent from the source. "
        f"Every value must be a non-empty string. Required keys: {keys}. "
        "Detailed findings must retain the source's exact estimates, units, sample sizes, uncertainty measures, "
        "technical labels, examples, and qualifications when present. Do not replace those figures with a simplification. "
        "Plain-English interpretation must then explain every important quantitative finding for a statistically "
        "non-specialist using these explicit labels: Direction, Magnitude, Reference point, Uncertainty, and Practical meaning. "
        "State what increased or decreased; how much in intuitive units; compared with which control, category, baseline, "
        "period, or no-effect value; what confidence intervals, p-values, or other uncertainty do and do not establish; "
        "and what the result would look like in an ordinary example. Convert relative changes to natural frequencies such "
        "as per 100 or per 1,000 only when the source supplies the denominator or baseline needed to do so. "
        "If a baseline, denominator, comparison, or uncertainty measure is absent, say that it is not reported and explain "
        "why that limits interpretation. Never invent a benchmark, call an effect large or small without a stated reference, "
        "treat statistical significance as practical importance, or treat association as causation. "
        "Locators must identify pages, sections, headings, or explicit text anchors. "
        "If the source does not report something, say so explicitly instead of inventing it."
    )


def _profile_system_prompt() -> str:
    return (
        "You are the evidence-profile reader for Auto-Zettelkasten profile prompt v3. "
        "Use only the committed Markdown note supplied by the user. Return exactly one JSON object with no Markdown fences, "
        "commentary, inferred full text, or literature-level cluster, debate, or gap proposals. Extract roughly 8-20 "
        "synthesis-relevant evidence anchors when the note supports that many, never more than 24 and never padded. Anchor "
        "identity must use source identity, a source-native locator or span, and evidence role rather than list position or "
        "paraphrase wording. Each anchor needs a support_envelope distinguishing empirical and argument roles, coverage, "
        "scope, and restrictions. Every anchor must classify source_locators. Generated atomic-note headings use locator_type "
        "generated_heading, are not source-native, and cannot support a strong synthesis assertion. Statistical anchors must preserve a typed quantitative_result that "
        "distinguishes observed rates, predicted probabilities, coefficients, marginal effects, odds ratios, percentages, "
        "reference groups, denominators, uncertainty, and source-reported versus derived values. Never convert or equate unlike "
        "estimands. Extract one source-local study_lineage record from explicit note evidence: authors or institutions, datasets, "
        "sampling frame, unit of analysis, population, period, publication/version relationships, institutional series, and "
        "overlap signals. Leave unsupported lineage fields empty. Source-level concepts, methods, cases, and outcomes are retrieval "
        "metadata and must not be copied automatically into every anchor. Preserve technical findings, plain-English meanings, "
        "qualifications, and traceable locators exactly to the degree supported by the note."
    )


def _cluster_proposal_system_prompt() -> str:
    return (
        "You are the collection-clustering reasoner for Auto-Zettelkasten cluster prompt v12. "
        "Return exactly one compact JSON object with a clusters array. Propose at most eight of the strongest analytical "
        "clusters in the collection. It is correct to leave an unsupported or non-comparable source out; the deterministic "
        "mapper records it as unclustered or as a topic neighbor. Each cluster must contain only proposal_id, label, "
        "semantic_identity, shared_question, coherence_rationale, source_ids, source_roles, and propositions. source_roles "
        "must be a compact object mapping each source_id to core, context, or bridge. Include no more than four total context "
        "or bridge sources per cluster. Do not output supporting_evidence, study_lineages, independence_assessments, "
        "evidence_base_groups, or effective_evidence_base_count; deterministic admission computes those fields. "
        "Each proposition must use this canonical shape: {proposition_id, semantic_identity, statement, question, "
        "proposition_type, source_ids, evidence, comparability}; source_ids is the array of participating core source IDs, "
        "and evidence is an array of {source_id, evidence_anchor_id, locator} objects. Never flatten those evidence fields "
        "onto the proposition object. Use one to three propositions per cluster. comparability must be a compact object stating "
        "whether the same bounded outcome, population, period, concept, and evidence type are compared. Every core source in "
        "a proposition must directly address the same bounded relationship and outcome. Do not "
        "bundle adjacent claims with 'and', 'thereby', or a broad umbrella statement unless at least two independent core "
        "sources support each claimed relationship. If sources address different outcomes or different parts of an argument, "
        "emit separate propositions and mark only genuinely useful non-comparable sources context or bridge. Context and bridge "
        "sources may explain a concept, method, institutional setting, "
        "boundary, or neighboring proposition, but may not broaden the cluster question, verdict, outcome, population, or causal "
        "claim. Only core sources count toward admission. The role field must use only core, context, or bridge; never place "
        "publication type, coverage, document genre, or evidence type in role. Form clusters around comparable propositions or disputes—not generic vocabulary, "
        "tags, broad subject matter, citation proximity, or count. "
        "Prefer broad debate families over "
        "micro-clusters named only conflict, mediation, success, or stability. Every analytical source that coherently belongs "
        "may appear in a proposal and no more than three. Put all proposed members in source_ids. For every proposition, "
        "include at least one directly supporting evidence anchor from every participating core source; do not add an anchor "
        "merely to reach a target count. Practice guidance may be core "
        "for typological, conceptual, or practitioner propositions, but not empirical effectiveness propositions. Use study_lineage "
        "signals when choosing core sources, but leave all independence calculations to deterministic admission. Do not treat "
        "publications, DOIs, or source IDs as independent by default. Shared authors, datasets, sampling frames, populations, "
        "periods, institutional series, or publication/version relationships are overlap signals. Keep labels, "
        "questions, and rationales concise. Copy source_id, evidence_anchor_id, and locator strings verbatim "
        "from the supplied profiles; never abbreviate, normalize, or paraphrase a locator. "
        "Allow useful overlap, but do not assign a source to more than three clusters. Do not use Zotero tags as proof. "
        "Do not invent source IDs, claims, locators, publications, clusters, or gaps. Limited profiles may provide context "
        "but cannot establish substantive cluster coherence."
    )


def _debate_system_prompt() -> str:
    return (
        "You are the proposition-aware debate-mapping reasoner for Auto-Zettelkasten debate prompt v2. Return exactly one JSON object "
        "with an assessments array. Compare only claims that address the same proposition, outcome, and relevant reference "
        "point. Use exactly one of mapped_debate, mapped_consensus, mixed_evidence, conditional_relationship, "
        "complementary_positions, parallel_literatures, single_position, or no_debate. Every position, agreement, "
        "contradiction, boundary condition, and methodological fault line must contain evidence references using real "
        "source_id, claim_id, and locator values supplied in the input. Do not infer disagreement from different outcomes, "
        "predictors, populations, or periods, and do not invent evidence."
    )


def _cluster_synthesis_system_prompt() -> str:
    return (
        "You are the cluster-synthesis reasoner for Auto-Zettelkasten cluster synthesis prompt v6. Return exactly one JSON "
        "object containing cluster_id, scope, boundaries, coherence_rationale, synthesis, central_findings, agreements, "
        "positions, contradictions, boundary_conditions, methodological_fault_lines, related_clusters, source_roles, "
        "source_contributions, supporting_evidence, synthesis_assertions, debate_state, gap_hypotheses, evidence_base_groups, "
        "independence_assessments, quantitative_comparisons, and effective_evidence_base_count. "
        "source_contributions is mandatory: retain one to three important cluster-relevant contributions from every core source "
        "and at most one from each context or bridge source. A unique finding remains visible with comparison_status single_source; "
        "it does not need multi-source agreement. Each contribution must state source_id, cluster_role, contribution_kind, "
        "related_proposition_ids, finding, technical_result, plain_english_meaning, relation_to_cluster_question, comparison_status, "
        "and evidence. Core contributions prioritize direct proposition findings, unique relevant findings, boundary evidence, and "
        "methodological contributions. Context and bridge contributions must say context_only and cannot enter the verdict, central "
        "cross-source findings, agreement, contradiction, debate classification, causal-effectiveness claim, or gap promotion. "
        "Every substantive synthesis assertion "
        "must name one or more admitted proposition_ids and use real source-local evidence_anchor_id and locator references. "
        "Descriptive, associational, illustrative, or prescriptive anchors cannot support causal wording. Do not combine "
        "differently defined actors, mechanisms, populations, outcomes, or estimands into consensus, and do not label "
        "incomparable claims as contradictions. Explain what technical findings mean in plain English while retaining "
        "reported figures, comparisons, uncertainty, and qualifications. Preserve quantitative_result types: observed rates, "
        "model-predicted probabilities, coefficients, marginal effects, odds ratios, and raw percentages are not interchangeable. "
        "Do not reproduce arithmetic unless it is source-reported or directly calculable from compatible supplied values. "
        "Every evidence reference used in the verdict, comparison, agreement, contradiction, debate, or gap must resolve to a typed "
        "source-native locator with supports_strong_assertion true; generated note headings do not qualify. Boundaries must be an array of concise strings. "
        "Central findings and every other substantive narrative section must be arrays of objects, never arrays of strings; "
        "each object must contain its narrative under finding, agreement, position, contradiction, boundary, fault_line, "
        "relationship, or role as appropriate, plus an evidence array of real source_id, claim_id, and locator references. "
        "Each central finding must explain the cross-source conclusion in two to four concrete sentences: what the sources find, "
        "what the finding means in plain English, and the important evidentiary limit or reference point. Central finding objects "
        "should also include technical_detail and plain_english_meaning when the source supports them. The synthesis prose must "
        "contain no claim absent from those evidence-bearing narrative objects. The human verdict will be projected assertion by "
        "assertion from those objects, so a detached supporting_evidence list cannot support extra prose. Agreements and emerging "
        "convergence require at least two proposition-comparable effective "
        "evidence bases. mapped_consensus requires at least three. Publications from one study, overlapping evidence base, or "
        "institutional series may show within-program consistency or aligned institutional guidance, but not scholarly consensus. "
        "Use only mapped_consensus, emerging_convergence, aligned_institutional_guidance, within_program_consistency, "
        "conditional_relationship, mapped_debate, mixed_evidence, complementary_positions, parallel_literatures, single_position, "
        "or no_debate as debate_state. Do not create a debate when evidence is consensus or incomparable. "
        "For a source_backed_cluster, supply enough non-repetitive detail for a 1,200-2,500 word Markdown projection; for "
        "an emerging_cluster, supply enough detail for 600-1,200 words. Prefer evidence density over filler. "
        "Gap hypotheses must use one of these rules: contradictory_findings, untested_mechanism, empirical_coverage, "
        "methodological_concentration, measurement_or_data, boundary_condition, replication, cross_cluster_integration, "
        "author_stated_gap. Each hypothesis must originate from a named proposition, contradiction, boundary condition, or "
        "precise matrix cell and specify proposition_id, topic, precise_missing_evidence, observed_pattern, why_matters, "
        "contribution, related_cluster_ids, and supporting_evidence. Reject generic phrases such as unobserved factors, "
        "more research is needed, limited data, or future studies unless the exact relationship, outcome, bounded context, "
        "and evidence needed are stated from supplied evidence. Do not invent sources, claims, locators, or findings."
    )


def _gap_adjudication_system_prompt() -> str:
    return (
        "You are the collection-gap adjudicator for Auto-Zettelkasten gap prompt v6. Return exactly one JSON object with "
        "gaps and rejected arrays. Consider only supplied candidates and their deterministic all-collection search results. "
        "You may merge equivalent candidates or perform at most one evidence-constrained reframing of a candidate, but may "
        "not manufacture a new literature gap. A missing test is not itself a worthy gap. Retain a candidate only when it "
        "poses a non-obvious puzzle, survives its strongest obvious answer, changes an inference or decision that matters, "
        "and has a feasible type-sensitive resolution path. Each retained gap must contain gap_id, proposition_id, "
        "originating_proposition_id, originating_cluster_ids, title, gap_statement, rule, related_cluster_ids, "
        "generation_explanation, observed_pattern, precise_missing_evidence, supporting_evidence, countervailing_evidence, "
        "internal_search_summary, closest_prior_explanation, decision_reasoning, evidence_needed, why_matters, contribution, "
        "confidence, value_assessment, resolution_path, anchors, merged_from_gap_ids, reframed_from_gap_id, and priority_tier. "
        "value_assessment must contain puzzle_type, puzzle, strongest_obvious_answer, why_obvious_answer_is_inadequate, "
        "competing_explanations, decision_or_inference_changed, information_gain, non_obviousness_passed, importance_passed, "
        "and rejection_reasons. resolution_path must contain path_type, question, evidence_needed, requirements, feasibility, "
        "and limitations. Choose path_type from quantitative, qualitative, historical_interpretive, theoretical, normative, "
        "methodological, or practitioner and provide requirements specific to that type. Do not force estimands or experiments "
        "onto qualitative, historical, theoretical, normative, methodological, or practitioner work. This is a route to "
        "discriminating evidence, not a finalized project study design. competing_explanations, rejection_reasons, and "
        "resolution-path limitations must be arrays of non-empty strings. information_gain and priority_tier must each be exactly "
        "high, moderate, or low. Anchors must copy cluster_id, section, and item_id from a supplied cluster item whose evidence "
        "generated the puzzle; omit an anchor rather than inventing one. Evidence references must use supplied source_id, "
        "claim_id, and locator values. Preserve full "
        "reasoning only for retained gaps. Do not enumerate routine rejections: a candidate omitted from both arrays is "
        "deterministically recorded as not retained. Use rejected only when a short model-specific reason adds audit value. "
        "Each rejected item must be compact and contain only gap_id, status, and a specific reason of at most 15 words; "
        "never repeat evidence or candidate prose in rejected. Deduplicate semantically "
        "equivalent candidates aggressively; list their IDs in merged_from_gap_ids on the retained canonical gap. Reject "
        "obvious, low-value, infeasible, vague, collection-answered, unlocated, or unsupported candidates with a concrete "
        "reason. The women-or-civil-society inclusion candidate is not useful merely because a causal pathway has not been "
        "tested; it must distinguish selection from causal effects and specify a feasible identifying comparison. All "
        "conclusions are scoped to "
        "the frozen collection; never claim novelty across the published literature."
    )


def _gap_adjudication_prompt(
    profiles: Sequence[EvidenceProfile | Mapping[str, Any]],
    request: LiteratureMapRequest,
    context: Mapping[str, Any] | None,
) -> str:
    """Keep the final all-collection pass coarse without repeating full profiles and search logs."""

    raw_context = dict(context or {})
    search_by_gap = {
        str(row.get("gap_id") or ""): row
        for row in raw_context.get("internal_search_log", []) or []
        if isinstance(row, Mapping) and str(row.get("gap_id") or "")
    }
    compact_candidates: list[dict[str, Any]] = []
    referenced_claims: dict[str, set[str]] = {}
    referenced_sources: set[str] = set()
    for raw_candidate in raw_context.get("candidates", []) or []:
        if not isinstance(raw_candidate, Mapping):
            continue
        candidate = dict(raw_candidate)
        gap_id = str(candidate.get("gap_id") or "")
        search = search_by_gap.get(gap_id, {})
        results = candidate.get("internal_search_results") or search.get("results") or []
        result_rows = [dict(row) for row in results if isinstance(row, Mapping)]
        result_counts: dict[str, int] = {}
        for row in result_rows:
            status = str(row.get("status") or "unknown")
            result_counts[status] = result_counts.get(status, 0) + 1
        top_results = sorted(
            result_rows,
            key=lambda row: (
                0 if "answer" in str(row.get("status") or "") else 1,
                -len(row.get("semantic_overlap", []) or []),
                str(row.get("source_id") or ""),
            ),
        )[:8]
        evidence_rows = [
            dict(row)
            for key in ("supporting_evidence", "countervailing_evidence")
            for row in candidate.get(key, []) or []
            if isinstance(row, Mapping)
        ]
        for row in evidence_rows:
            source_id = str(row.get("source_id") or "")
            claim_id = str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
            if source_id:
                referenced_sources.add(source_id)
            if source_id and claim_id:
                referenced_claims.setdefault(source_id, set()).add(claim_id)
        closest_prior = []
        for row in candidate.get("closest_prior_work", []) or []:
            if not isinstance(row, Mapping):
                continue
            source_id = str(row.get("source_id") or "")
            if source_id:
                referenced_sources.add(source_id)
            closest_prior.append(
                {
                    key: row.get(key)
                    for key in (
                        "prior_id",
                        "source_id",
                        "title",
                        "study_family_id",
                        "confidence",
                        "semantic_overlap",
                        "overlap_explanation",
                    )
                    if row.get(key) not in (None, "", [])
                }
            )
        warnings = candidate.get("warnings") or search.get("limited_profile_warnings") or []
        for warning in warnings:
            if isinstance(warning, Mapping) and str(warning.get("source_id") or ""):
                referenced_sources.add(str(warning["source_id"]))
        compact_candidates.append(
            {
                key: candidate.get(key)
                for key in (
                    "gap_id",
                    "rule",
                    "topic",
                    "proposition_id",
                    "proposition_ids",
                    "originating_cluster_revisions",
                    "missing_cell",
                    "precise_missing_evidence",
                    "related_cluster_ids",
                    "observed_pattern",
                    "generation_explanation",
                    "evidence_needed",
                    "why_matters",
                    "contribution",
                    "status",
                    "scope",
                    "rule_results",
                    "supporting_evidence",
                    "countervailing_evidence",
                    "internal_search_terms",
                )
                if candidate.get(key) not in (None, "", [])
            }
            | {
                "internal_search": {
                    "complete": bool(search.get("complete", True)),
                    "analytical_profile_count_searched": int(
                        search.get("analytical_profile_count_searched", len(result_rows)) or 0
                    ),
                    "result_counts": dict(sorted(result_counts.items())),
                    "top_semantic_matches": top_results,
                },
                "closest_prior_work": closest_prior[:5],
                "limited_source_warnings": list(warnings),
            }
        )

    evidence_catalog: list[dict[str, Any]] = []
    for profile in profiles:
        raw = _evidence_profile_payload(profile)
        source_id = str(raw.get("source_id") or "")
        if source_id not in referenced_sources and source_id not in referenced_claims:
            continue
        context_value = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
        claims = []
        for claim in raw.get("evidence_anchors") or raw.get("claims") or raw.get("findings") or []:
            value = claim.to_dict() if hasattr(claim, "to_dict") else dict(claim) if isinstance(claim, Mapping) else {}
            claim_id = str(value.get("evidence_anchor_id") or value.get("claim_id") or value.get("finding_id") or "")
            if claim_id not in referenced_claims.get(source_id, set()):
                continue
            claims.append(
                {
                    "evidence_anchor_id": claim_id,
                    "text": str(value.get("text") or value.get("claim") or ""),
                    "locator": str(value.get("locator") or ""),
                    "direction": str(value.get("direction") or ""),
                    "dimensions": value.get("dimensions", {}),
                    "support_envelope": value.get("support_envelope", {}),
                    "boundary_condition": str(value.get("boundary_condition") or ""),
                }
            )
        evidence_catalog.append(
            {
                "source_id": source_id,
                "title": str(raw.get("title") or context_value.get("title") or ""),
                "study_family_id": str(raw.get("study_family_id") or source_id),
                "analytical": bool(raw.get("analytical", not raw.get("excluded_from_synthesis", False))),
                "claims": claims,
            }
        )

    compact_clusters = [
        {
            key: row.get(key)
            for key in (
                "cluster_id",
                "revision_hash",
                "label",
                "shared_question",
                "status",
                "source_ids",
                "core_source_ids",
                "proposition_ids",
                "propositions",
            )
            if row.get(key) not in (None, "", [])
        }
        for row in raw_context.get("clusters", []) or []
        if isinstance(row, Mapping)
    ]
    compact_syntheses = []
    synthesis_rows = raw_context.get("cluster_syntheses", {})
    iterable = synthesis_rows.values() if isinstance(synthesis_rows, Mapping) else synthesis_rows or []
    for row in iterable:
        if not isinstance(row, Mapping):
            continue
        compact_syntheses.append(
            {
                key: row.get(key)
                for key in (
                    "cluster_id",
                    "scope",
                    "coherence_rationale",
                    "central_findings",
                    "agreements",
                    "positions",
                    "contradictions",
                    "boundary_conditions",
                    "methodological_fault_lines",
                    "related_clusters",
                    "source_roles",
                    "synthesis_assertions",
                    "debate_state",
                    "gap_hypotheses",
                )
                if row.get(key) not in (None, "", [])
            }
        )
    payload = {
        "instruction": "Adjudicate only the supplied, internally searched collection-gap candidates.",
        "request": {
            "source_set_id": str(getattr(request, "source_set_id", "") or ""),
            "map_id": str(getattr(request, "map_id", "") or ""),
            "provider": str(getattr(request, "provider", "") or ""),
            "model": str(getattr(request, "model", "") or ""),
            "policy": getattr(request, "literature_policy").to_dict(),
        },
        "clusters": compact_clusters,
        "cluster_evidence": compact_syntheses,
        "evidence_catalog": evidence_catalog,
        "candidates": compact_candidates,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _literature_prompt(
    profiles: Sequence[EvidenceProfile],
    request: LiteratureMapRequest,
    context: Mapping[str, Any] | None,
    *,
    instruction: str,
) -> str:
    def serializable(value: Any) -> Any:
        if isinstance(value, EvidenceProfile):
            return value.to_dict()
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, Mapping):
            return {str(key): serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [serializable(item) for item in value]
        return value

    payload = {
        "instruction": instruction,
        "request": {
            "source_set_id": str(getattr(request, "source_set_id", "") or ""),
            "map_id": str(getattr(request, "map_id", "") or ""),
            "provider": str(getattr(request, "provider", "") or ""),
            "model": str(getattr(request, "model", "") or ""),
            "policy": getattr(request, "literature_policy").to_dict(),
        },
        "profiles": [serializable(profile) for profile in profiles],
        "context": serializable(dict(context or {})),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _evidence_profile_payload(profile: EvidenceProfile | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(profile, EvidenceProfile):
        return profile.to_dict()
    if isinstance(profile, Mapping):
        return dict(profile)
    raise ProviderError("literature profiles must be EvidenceProfile values or mappings")


def _cluster_proposal_profile(profile: EvidenceProfile | Mapping[str, Any]) -> dict[str, Any]:
    """Compact semantic projection for one coarse collection-clustering call."""

    raw = _evidence_profile_payload(profile)
    context = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
    dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), Mapping) else {}

    def values(*candidates: Any) -> list[str]:
        rows: list[str] = []

        def collect(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                clean = value.strip()
                if clean and clean not in rows:
                    rows.append(clean)
                return
            if isinstance(value, Mapping):
                for child in value.values():
                    collect(child)
                return
            if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
                for child in value:
                    collect(child)
                return
            collect(str(value))

        for candidate in candidates:
            collect(candidate)
        return rows

    topic_labels = raw.get("semantic_topic_labels")
    topic_scores = raw.get("semantic_topic_scores")
    normalized_topics = (
        list(topic_labels.values())
        if isinstance(topic_labels, Mapping) and topic_labels
        else list(topic_scores)
        if isinstance(topic_scores, Mapping)
        else []
    )
    anchors = []
    for finding in raw.get("evidence_anchors") or raw.get("findings") or raw.get("claims") or []:
        value = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding) if isinstance(finding, Mapping) else {}
        finding_id = str(value.get("evidence_anchor_id") or value.get("finding_id") or value.get("claim_id") or "")
        locator = str(value.get("locator") or "")
        if not finding_id or not locator:
            continue
        finding_dimensions = value.get("dimensions") if isinstance(value.get("dimensions"), Mapping) else {}
        envelope = value.get("support_envelope") if isinstance(value.get("support_envelope"), Mapping) else {}
        anchors.append(
            {
                "evidence_anchor_id": finding_id,
                "claim": str(value.get("claim") or value.get("text") or ""),
                "direction": str(value.get("direction") or ""),
                "outcome": "; ".join(values(value.get("outcome"), finding_dimensions.get("outcome"))),
                "conditions": values(
                    value.get("conditions"),
                    value.get("boundaries"),
                    value.get("boundary_condition"),
                ),
                "plain_english_meaning": str(value.get("plain_english_meaning") or ""),
                "locator": locator,
                "source_locators": [
                    dict(row)
                    for row in value.get("source_locators", []) or []
                    if isinstance(row, Mapping)
                ],
                "quantitative_result": (
                    dict(value["quantitative_result"])
                    if isinstance(value.get("quantitative_result"), Mapping)
                    else None
                ),
                "support_envelope": dict(envelope),
            }
        )
    return {
        "source_id": str(raw.get("source_id") or ""),
        "note_id": str(raw.get("note_id") or ""),
        "title": str(raw.get("title") or context.get("title") or ""),
        "study_family_id": str(raw.get("study_family_id") or raw.get("source_id") or ""),
        "study_lineage": (
            dict(raw["study_lineage"])
            if isinstance(raw.get("study_lineage"), Mapping)
            else None
        ),
        "source_role": str(raw.get("source_role") or ""),
        "research_questions": values(raw.get("research_questions")),
        "concepts": values(raw.get("concepts"), normalized_topics),
        "theories": values(raw.get("theories"), dimensions.get("theory")),
        "mechanisms": values(raw.get("mechanisms"), dimensions.get("mechanism")),
        "methods": values(raw.get("methods"), dimensions.get("method")),
        "data": values(raw.get("data"), dimensions.get("data")),
        "cases": values(raw.get("cases"), dimensions.get("case")),
        "geography": values(raw.get("geography")),
        "periods": values(raw.get("periods"), dimensions.get("period")),
        "populations": values(raw.get("populations")),
        "outcomes": values(raw.get("outcomes"), dimensions.get("outcome")),
        "evidence_anchors": anchors,
    }


def _cluster_proposal_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    rows = []
    for relation in (context or {}).get("relations", []) or []:
        if not isinstance(relation, Mapping):
            continue
        rows.append(
            {
                "relation_id": str(relation.get("relation_id") or ""),
                "source_ids": [str(value) for value in relation.get("source_ids", []) or []],
                "confidence": relation.get("confidence"),
                "evidence_kinds": sorted(
                    {
                        str(item.get("kind") or "")
                        for item in relation.get("evidence", []) or []
                        if isinstance(item, Mapping) and str(item.get("kind") or "")
                    }
                ),
            }
        )
    proposition_rows = []
    for proposition in (context or {}).get("propositions", []) or []:
        if not isinstance(proposition, Mapping):
            continue
        proposition_rows.append(
            {
                key: proposition.get(key)
                for key in (
                    "proposition_id",
                    "statement",
                    "question",
                    "proposition_type",
                    "source_ids",
                    "study_family_ids",
                    "evidence",
                    "comparability",
                )
                if proposition.get(key) not in (None, "", [])
            }
        )
    neighborhood_rows = []
    for neighborhood in (context or {}).get("topic_neighborhoods", []) or []:
        if not isinstance(neighborhood, Mapping):
            continue
        neighborhood_rows.append(
            {
                key: neighborhood.get(key)
                for key in ("topic_neighborhood_id", "kind", "label", "source_ids")
                if neighborhood.get(key) not in (None, "", [])
            }
        )
    return {
        "relations": rows,
        "propositions": proposition_rows,
        "topic_neighborhoods": neighborhood_rows,
        "topic_neighborhoods_are_navigation_only": True,
    }


def _chunk_system_prompt() -> str:
    keys = ", ".join(CHUNK_EVIDENCE_KEYS)
    return (
        "You extract compact, source-faithful evidence from one coarse document chunk. "
        "Return only one JSON object and do not infer facts absent from the chunk. "
        f"Every value must be a non-empty string. Required keys: {keys}. "
        "Preserve concrete claims, methods, data, qualifications, and contradictions, but avoid prose repetition. "
        "Statistical context must retain exact estimates and units plus any sample size, denominator, baseline, comparison "
        "group, reference category, uncertainty measure, significance statement, and caveat needed for later plain-English explanation. "
        "If the chunk contains no quantitative result, say so briefly in statistical_context. "
        "Keep page markers, section headings, and explicit text anchors in locators. "
        "When a field is not reported in this chunk, say so briefly."
    )


def _metadata_prompt(metadata: Mapping[str, Any], question: str | None) -> str:
    safe_metadata = {
        key: metadata.get(key)
        for key in ("title", "creators", "date", "publicationTitle", "DOI", "url", "itemType")
        if metadata.get(key)
    }
    return f"Metadata: {json.dumps(safe_metadata, ensure_ascii=False)}\nQuestion lens: {question or 'none'}"


def _source_prompt(text: str, metadata: Mapping[str, Any], question: str | None) -> str:
    return f"{_metadata_prompt(metadata, question)}\n\nINSPECTED SOURCE CONTENT:\n{text}"


def _chunk_prompt(
    text: str,
    metadata: Mapping[str, Any],
    question: str | None,
    chunk_id: str,
    locator: str,
) -> str:
    scope = {
        key: metadata.get(key)
        for key in ("section", "heading", "page", "page_start", "page_end", "locator")
        if metadata.get(key) is not None
    }
    return (
        f"{_metadata_prompt(metadata, question)}\n"
        f"Coarse chunk id: {chunk_id or 'unspecified'}\n"
        f"Caller-provided locator: {locator or 'unspecified'}\n"
        f"Caller-provided section/page scope: {json.dumps(scope, ensure_ascii=False)}\n\n"
        "COARSE INSPECTED SOURCE CHUNK:\n"
        f"{text}"
    )


def _synthesis_prompt(
    chunk_memos: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    question: str | None,
) -> str:
    allowed_keys = set(CHUNK_EVIDENCE_KEYS) | set(SECTION_KEYS)
    compact_evidence: list[dict[str, str]] = []
    for index, evidence in enumerate(chunk_memos, start=1):
        compact = {
            key: str(value).strip()
            for key, value in evidence.items()
            if key in allowed_keys and str(value).strip()
        }
        if not compact:
            raise ValueError(f"chunk_memos item {index} has no usable evidence")
        compact_evidence.append(compact)
    return (
        f"{_metadata_prompt(metadata, question)}\n\n"
        "Synthesize the following ordered coarse chunk evidence into one source-level analysis. "
        "Resolve repetition, retain disagreements and qualifications, and preserve all useful locators. "
        "Keep exact technical figures in detailed_findings and use statistical_context to produce the separately labeled "
        "plain_english_interpretation required by the system instructions. "
        "Do not claim that a chunk summary proves anything beyond its supplied evidence.\n\n"
        f"COARSE CHUNK EVIDENCE:\n{json.dumps(compact_evidence, ensure_ascii=False)}"
    )


def _parse_analysis(value: Any) -> Mapping[str, Any]:
    return _parse_required_mapping(value, SECTION_KEYS, label="reader response")


def _parse_chunk_evidence(value: Any) -> Mapping[str, Any]:
    return _parse_required_mapping(value, CHUNK_EVIDENCE_KEYS, label="chunk response")


def _parse_json_object(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value).strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{label} was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"{label} must be a JSON object")
    return payload


def _validate_literature_response(payload: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    def string_values(value: Any) -> list[str]:
        if isinstance(value, list):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def quality_tier(value: Any) -> str:
        label = str(value or "").strip().casefold()
        if "high" in label:
            return "high"
        if "moderate" in label or "medium" in label:
            return "moderate"
        if "low" in label:
            return "low"
        return ""

    def scalar_text(value: Any) -> str:
        if isinstance(value, list):
            return "; ".join(item.strip() for item in value if isinstance(item, str) and item.strip())
        return str(value or "").strip()

    try:
        if kind == "cluster_proposal":
            if set(payload) != {"clusters"} or not isinstance(payload.get("clusters"), list):
                raise ValueError("cluster proposal response must contain only a clusters list")
            normalized_clusters: list[dict[str, Any]] = []
            for row in payload["clusters"]:
                if not isinstance(row, Mapping):
                    raise ValueError("cluster proposal rows must be mappings")
                proposal = dict(row)
                # Independence is a deterministic admission gate. A model may
                # reference lineage IDs from its context, but it cannot author
                # or override the canonical lineage/group records.
                for derived_field in (
                    "study_lineages",
                    "evidence_base_groups",
                    "independence_assessments",
                    "effective_evidence_base_count",
                ):
                    proposal.pop(derived_field, None)
                normalized_clusters.append(ClusterProposal.from_dict(proposal).to_dict())
            return {"clusters": normalized_clusters}
        if kind == "cluster_synthesis":
            normalized = dict(payload)
            # Independence and evidence-base counts are deterministic admission
            # outputs. They may be supplied to the model as context, but model-
            # authored variants must never override the canonical accounting or
            # make an otherwise recoverable paid synthesis fail parsing.
            for derived_field in (
                "evidence_base_groups",
                "independence_assessments",
                "effective_evidence_base_count",
                "quantitative_comparisons",
            ):
                normalized.pop(derived_field, None)
            boundary_rows = normalized.get("boundaries")
            structured_boundaries = [dict(row) for row in boundary_rows or [] if isinstance(row, Mapping)] \
                if isinstance(boundary_rows, list) else []
            normalized["boundaries"] = [
                text
                for row in boundary_rows or []
                if (text := _cluster_boundary_text(row))
            ] if isinstance(boundary_rows, list) else []
            if structured_boundaries:
                existing_conditions = normalized.get("boundary_conditions")
                normalized["boundary_conditions"] = (
                    list(existing_conditions) if isinstance(existing_conditions, list) else []
                ) + structured_boundaries
            for field_name in (
                "source_contributions",
                "central_findings",
                "agreements",
                "positions",
                "contradictions",
                "boundary_conditions",
                "methodological_fault_lines",
                "related_clusters",
                "source_roles",
                "supporting_evidence",
                "synthesis_assertions",
                "gap_hypotheses",
                "evidence_base_groups",
                "independence_assessments",
                "quantitative_comparisons",
            ):
                values = normalized.get(field_name)
                if isinstance(values, list):
                    # Model-generated prose without its required evidence
                    # object is unsupported. Drop that individual item; the
                    # strict public type and downstream evidence resolver still
                    # validate every retained object.
                    normalized[field_name] = [dict(row) for row in values if isinstance(row, Mapping)]
                else:
                    normalized[field_name] = []
            valid_assertions: list[dict[str, Any]] = []
            for row in normalized["synthesis_assertions"]:
                try:
                    valid_assertions.append(SynthesisAssertion.from_dict(row).to_dict())
                except (TypeError, ValueError):
                    # Assertions duplicate the evidence-bearing narrative
                    # sections. A malformed optional projection must not erase
                    # otherwise valid central findings; downstream validation
                    # rebuilds map-local assertions only from retained section
                    # items with resolvable structured evidence.
                    continue
            normalized["synthesis_assertions"] = valid_assertions
            return ClusterSynthesis.from_dict(normalized).to_dict()
        if kind == "gap_adjudication":
            if set(payload) != {"gaps", "rejected"}:
                raise ValueError("gap adjudication response must contain only gaps and rejected")
            if not isinstance(payload.get("gaps"), list) or not isinstance(payload.get("rejected"), list):
                raise ValueError("gap adjudication gaps and rejected must be lists")
            rejected = payload["rejected"]
            if any(not isinstance(row, Mapping) for row in rejected):
                raise ValueError("gap adjudication rejected records must be mappings")
            normalized_gaps: list[dict[str, Any]] = []
            for raw_row in payload["gaps"]:
                if not isinstance(raw_row, Mapping):
                    raise ValueError("gap adjudication gap records must be mappings")
                row = {
                    str(key): value
                    for key, value in raw_row.items()
                    if str(key) in GapRationale.__dataclass_fields__
                }
                for field_name in (
                    "gap_id",
                    "proposition_id",
                    "originating_proposition_id",
                    "title",
                    "gap_statement",
                    "rule",
                    "generation_explanation",
                    "observed_pattern",
                    "precise_missing_evidence",
                    "internal_search_summary",
                    "closest_prior_explanation",
                    "decision_reasoning",
                    "evidence_needed",
                    "why_matters",
                    "contribution",
                    "confidence",
                    "reframed_from_gap_id",
                ):
                    row[field_name] = scalar_text(row.get(field_name))
                for field_name in ("supporting_evidence", "countervailing_evidence"):
                    values = row.get(field_name)
                    row[field_name] = (
                        [dict(value) for value in values if isinstance(value, Mapping)]
                        if isinstance(values, list)
                        else []
                    )
                for field_name in ("related_cluster_ids", "originating_cluster_ids", "merged_from_gap_ids"):
                    row[field_name] = string_values(row.get(field_name))
                row["priority_tier"] = quality_tier(row.get("priority_tier"))
                assessment = row.get("value_assessment")
                assessment = dict(assessment) if isinstance(assessment, Mapping) else {}
                assessment_aliases = {
                    "obvious_answer": "strongest_obvious_answer",
                    "why_obvious_answer_inadequate": "why_obvious_answer_is_inadequate",
                    "what_changes": "decision_or_inference_changed",
                }
                for alias, canonical in assessment_aliases.items():
                    if canonical not in assessment and alias in assessment:
                        assessment[canonical] = assessment[alias]
                assessment = {
                    str(key): value
                    for key, value in assessment.items()
                    if str(key)
                    in {
                        "puzzle_type",
                        "puzzle",
                        "strongest_obvious_answer",
                        "why_obvious_answer_is_inadequate",
                        "competing_explanations",
                        "decision_or_inference_changed",
                        "information_gain",
                        "non_obviousness_passed",
                        "importance_passed",
                        "rejection_reasons",
                    }
                }
                for field_name in (
                    "puzzle_type",
                    "puzzle",
                    "strongest_obvious_answer",
                    "why_obvious_answer_is_inadequate",
                    "decision_or_inference_changed",
                ):
                    assessment[field_name] = scalar_text(assessment.get(field_name))
                assessment["information_gain"] = quality_tier(assessment.get("information_gain"))
                for field_name in ("competing_explanations", "rejection_reasons"):
                    assessment[field_name] = string_values(assessment.get(field_name))
                for field_name in ("non_obviousness_passed", "importance_passed"):
                    if not isinstance(assessment.get(field_name), bool):
                        assessment[field_name] = False
                row["value_assessment"] = assessment
                resolution = row.get("resolution_path")
                resolution = dict(resolution) if isinstance(resolution, Mapping) else {}
                resolution_aliases = {
                    "type": "path_type",
                    "research_question": "question",
                    "needed_evidence": "evidence_needed",
                    "constraints": "limitations",
                }
                for alias, canonical in resolution_aliases.items():
                    if canonical not in resolution and alias in resolution:
                        resolution[canonical] = resolution[alias]
                resolution = {
                    str(key): value
                    for key, value in resolution.items()
                    if str(key) in {"path_type", "question", "evidence_needed", "requirements", "feasibility", "limitations"}
                }
                if resolution:
                    for field_name in ("path_type", "question", "evidence_needed", "feasibility"):
                        resolution[field_name] = scalar_text(resolution.get(field_name))
                    resolution["requirements"] = (
                        dict(resolution.get("requirements"))
                        if isinstance(resolution.get("requirements"), Mapping)
                        else {}
                    )
                    resolution["limitations"] = string_values(resolution.get("limitations"))
                    row["resolution_path"] = resolution
                design = row.get("study_design")
                design = dict(design) if isinstance(design, Mapping) else {}
                design_aliases = {
                    "identification_or_inference_stategy": "identification_or_inference_strategy",
                    "identification_strategy": "identification_or_inference_strategy",
                    "comparison": "comparator",
                    "exposure": "exposure_or_treatment",
                    "confounders": "confounders_or_rival_explanations",
                    "falsification_tests": "falsification_or_process_tests",
                }
                for alias, canonical in design_aliases.items():
                    if canonical not in design and alias in design:
                        design[canonical] = design[alias]
                design = {
                    str(key): value
                    for key, value in design.items()
                    if str(key)
                    in {
                        "design_type",
                        "research_question",
                        "estimand",
                        "unit_of_analysis",
                        "target_population",
                        "exposure_or_treatment",
                        "comparator",
                        "outcomes",
                        "mechanism_measures",
                        "identification_or_inference_strategy",
                        "data_route",
                        "confounders_or_rival_explanations",
                        "falsification_or_process_tests",
                        "feasibility",
                        "ethical_constraints",
                        "validity_risks",
                    }
                }
                for field_name in (
                    "design_type",
                    "research_question",
                    "estimand",
                    "unit_of_analysis",
                    "target_population",
                    "exposure_or_treatment",
                    "comparator",
                    "identification_or_inference_strategy",
                    "data_route",
                    "feasibility",
                    "ethical_constraints",
                ):
                    design[field_name] = scalar_text(design.get(field_name))
                for field_name in (
                    "outcomes",
                    "mechanism_measures",
                    "confounders_or_rival_explanations",
                    "falsification_or_process_tests",
                    "validity_risks",
                ):
                    design[field_name] = string_values(design.get(field_name))
                row["study_design"] = design
                anchor_values = row.get("anchors")
                row["anchors"] = [
                    dict(value)
                    for value in anchor_values or []
                    if isinstance(value, Mapping)
                    and str(value.get("cluster_id") or "")
                    and str(value.get("item_id") or "")
                    and str(value.get("section") or "")
                    in {
                        "central_findings",
                        "agreements",
                        "positions",
                        "contradictions",
                        "boundary_conditions",
                        "methodological_fault_lines",
                        "related_clusters",
                        "source_roles",
                    }
                ] if isinstance(anchor_values, list) else []
                normalized_gaps.append(GapRationale.from_dict(row).to_dict())
            return {
                "gaps": normalized_gaps,
                "rejected": [dict(row) for row in rejected],
            }
        if kind == "debate_mapping":
            if set(payload) != {"assessments"} or not isinstance(payload.get("assessments"), list):
                raise ValueError("debate mapping response must contain only an assessments list")
            if any(not isinstance(row, Mapping) for row in payload["assessments"]):
                raise ValueError("debate assessments must be mappings")
            return {"assessments": [dict(row) for row in payload["assessments"]]}
    except ValueError as exc:
        raise ProviderError(f"invalid {kind.replace('_', ' ')} response: {exc}") from exc
    raise ProviderError(f"unsupported literature response kind: {kind}")


def _cluster_boundary_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("boundary", "condition", "scope", "description", "text", "label", "value"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _parse_required_mapping(value: Any, required_keys: Sequence[str], *, label: str) -> Mapping[str, Any]:
    payload = _parse_json_object(value, label=label)
    missing = [key for key in required_keys if not str(payload.get(key, "")).strip()]
    if missing:
        raise ProviderError(f"{label} omitted required sections: {', '.join(missing)}")
    return {key: str(payload[key]).strip() for key in required_keys}


def _resolve_context_window(provider: str, model: str, configured: int | None) -> tuple[int, str]:
    if configured is not None:
        if configured <= 0:
            raise ValueError("context_window_tokens must be positive")
        return configured, "configured"
    normalized_model = model.strip().casefold().replace("_", "-")
    model_window = MODEL_CONTEXT_WINDOWS.get((provider, normalized_model))
    if model_window is not None:
        return model_window, "model"
    try:
        default = PROVIDER_CONTEXT_WINDOW_DEFAULTS[provider]
    except KeyError as exc:
        raise ValueError(f"no context-window default for provider: {provider}") from exc
    return default, "fallback" if provider == "ollama" else "provider_default"


def _estimate_tokens(value: str) -> int:
    """Conservative tokenizer-free estimate that remains safe for multilingual text."""

    return max(1, (len(value.encode("utf-8")) + 2) // 3)


def _response_byte_limit(output_tokens: int) -> int:
    # The API envelope adds overhead around generated text. This bounds broken
    # or malicious responses while leaving ample room for UTF-8 JSON encoding.
    return 65_536 + output_tokens * 32


def _stream_response_byte_limit(output_tokens: int) -> int:
    # OpenAI-compatible reasoning models may stream reasoning_content in the
    # transport envelope even though only bounded content is retained. Keep a
    # finite allowance for that envelope without weakening the model output cap.
    return max(8 * 1024 * 1024, 65_536 + output_tokens * 512)


class _RequestDeadlineExceeded(ProviderError):
    pass


def _post_json(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    response_byte_limit: int = 2 * 1024 * 1024,
    on_attempt: Callable[[], None] | None = None,
    response_reader: Callable[[Any, float, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if response_byte_limit <= 0:
        raise ValueError("response_byte_limit must be positive")
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "auto-zettelkasten/0.7.0",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        if on_attempt is not None:
            on_attempt()
        remaining = _remaining_seconds(deadline)
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                reader = response_reader or _read_json_response
                return reader(response, deadline, response_byte_limit)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_seconds(exc.headers)
            try:
                detail = exc.read(512).decode("utf-8", errors="replace")
            finally:
                exc.close()
            _remaining_seconds(deadline)
            if attempt == 0 and (exc.code == 429 or 500 <= exc.code <= 599):
                _wait_for_retry(retry_after, deadline, cause=exc)
                continue
            raise ProviderError(f"provider HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt == 0:
                continue
            if _is_network_timeout(exc.reason):
                raise ProviderError("provider request timed out") from exc
            raise ProviderError(f"provider unavailable: {exc.reason}") from exc
        except http.client.HTTPException as exc:
            if attempt == 0:
                continue
            raise ProviderError("provider connection interrupted") from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt == 0:
                continue
            raise ProviderError("provider request timed out") from exc
    raise AssertionError("provider retry loop exhausted unexpectedly")


def _read_openai_stream_response(response: Any, deadline: float, byte_limit: int) -> dict[str, Any]:
    """Read OpenAI-compatible SSE output while enforcing one absolute deadline."""

    if not callable(getattr(response, "readline", None)):
        # Test doubles and a few compatible gateways may still return one JSON
        # object even when stream=true. Preserve that compatible fallback.
        return _read_json_response(response, deadline, byte_limit)

    content: list[str] = []
    finish_reason = ""
    total = 0
    while True:
        remaining = _remaining_seconds(deadline)
        _set_stream_timeout(response, remaining)
        line = response.readline()
        _remaining_seconds(deadline)
        if not line:
            break
        total += len(line)
        if total > byte_limit:
            raise ProviderError("provider response exceeded the configured output bound")
        decoded = line.decode("utf-8", errors="strict").strip()
        if not decoded or decoded.startswith(":") or not decoded.startswith("data:"):
            continue
        data = decoded[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
            choice = event["choices"][0]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("provider stream contained an invalid event") from exc
        delta = choice.get("delta") or choice.get("message") or {}
        piece = delta.get("content") if isinstance(delta, Mapping) else None
        if isinstance(piece, str):
            content.append(piece)
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    if not content:
        raise ProviderError("provider stream ended without response content")
    return {
        "choices": [
            {
                "finish_reason": finish_reason or "stop",
                "message": {"content": "".join(content)},
            }
        ]
    }


def _read_json_response(response: Any, deadline: float, byte_limit: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read1", response.read)
    while True:
        remaining = _remaining_seconds(deadline)
        _set_stream_timeout(response, remaining)
        # HTTPResponse.read(size) may internally wait for `size` bytes while a
        # chunked provider trickles smaller frames, effectively renewing the
        # socket timeout inside one Python call. read1 returns after one
        # underlying read so the monotonic deadline is checked between frames.
        chunk = read(min(65_536, byte_limit + 1 - total))
        _remaining_seconds(deadline)
        if not chunk:
            break
        total += len(chunk)
        if total > byte_limit:
            raise ProviderError("provider response exceeded the configured output bound")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("provider response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("provider response must be a JSON object")
    return value


def _set_stream_timeout(response: Any, remaining: float) -> None:
    try:
        response.fp.raw._sock.settimeout(remaining)
    except (AttributeError, OSError):
        # urllib does not expose the socket uniformly across Python versions or
        # mocked transports. The open timeout and monotonic checks still apply.
        return


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RequestDeadlineExceeded("provider request deadline exceeded")
    return remaining


def _is_network_timeout(reason: Any) -> bool:
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    normalized = str(reason).casefold()
    return "timed out" in normalized or "timeout" in normalized


def _retry_after_seconds(headers: Any) -> float:
    if not headers:
        return 0.0
    value = headers.get("Retry-After")
    if value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _wait_for_retry(delay: float, deadline: float, *, cause: BaseException) -> None:
    if delay <= 0:
        return
    remaining = _remaining_seconds(deadline)
    if delay >= remaining:
        raise _RequestDeadlineExceeded("provider request deadline exceeded while waiting to retry") from cause
    time.sleep(delay)
