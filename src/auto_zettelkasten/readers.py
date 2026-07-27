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
from contextvars import ContextVar
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
DEFAULT_MAX_OUTPUT_TOKENS = 6_000
DEFAULT_CHUNK_OUTPUT_TOKENS = 1_024
PROFILE_MAX_OUTPUT_TOKENS = 16_000
LITERATURE_MAX_OUTPUT_TOKENS = 8_000
CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS = 32_000
CLUSTER_SYNTHESIS_MAX_OUTPUT_TOKENS = 32_000
GAP_ADJUDICATION_MAX_OUTPUT_TOKENS = 32_000
RELATIONSHIP_MAX_OUTPUT_TOKENS = 12_000

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

_REASONING_EFFORT: ContextVar[str | None] = ContextVar(
    "auto_zettelkasten_reasoning_effort", default=None
)


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
        self.transport_attempt_count = (
            int(getattr(self, "transport_attempt_count", 0) or 0) + 1
        )

    def _configure_capabilities(self) -> None:
        context_window, source = _resolve_context_window(
            self.name,
            self.model,
            self.context_window_tokens,
        )
        self.context_window_tokens = context_window
        self.context_window_source = source
        if not 0 < self.direct_read_fraction <= 1:
            raise ValueError(
                "direct_read_fraction must be greater than 0 and at most 1"
            )
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
        return _estimate_tokens(_system_prompt()) + _estimate_tokens(
            _source_prompt(text, metadata, question)
        )

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
        return (
            "direct"
            if self.should_read_directly(text, metadata, question)
            else "hierarchical"
        )

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        self._authorize_request()
        system_prompt = _system_prompt()
        user_prompt = _source_prompt(text, metadata, question)
        self._ensure_prompt_fits(
            system_prompt, user_prompt, self.max_output_tokens, label="source"
        )
        return _parse_analysis(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                self.max_output_tokens,
                self._request_deadline_seconds(),
                reasoning_effort="high",
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
        """Generate profile-prompt-v6 JSON from committed note text only."""

        del (
            question
        )  # A question is a projection lens, not part of the base evidence profile.
        self._authorize_request()
        prompt_version = str((context or {}).get("profile_prompt_version") or "6")
        if prompt_version != "6":
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
        self._ensure_prompt_fits(
            system_prompt, user_prompt, output_tokens, label="evidence profile"
        )
        return _parse_json_object(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                deadline_seconds,
                reasoning_effort="high",
            ),
            label="profile response",
        )

    def select_relationship_shards(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Select bounded catalogue shards without deciding source relationships."""

        self._authorize_request()
        return _validate_relationship_response(
            self._literature_json_call(
                _relationship_shard_system_prompt(),
                _relationship_prompt(profiles, request, context),
                label="relationship shard selection",
                reasoning_effort="high",
                output_tokens=RELATIONSHIP_MAX_OUTPUT_TOKENS,
                list_key="shard_ids",
            ),
            kind="shard_selection",
        )

    def select_relationship_candidates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Select intellectually consequential source or cluster comparisons."""

        self._authorize_request()
        return _validate_relationship_response(
            self._literature_json_call(
                _relationship_candidate_system_prompt(),
                _relationship_prompt(profiles, request, context),
                label="relationship candidate selection",
                reasoning_effort="high",
                output_tokens=RELATIONSHIP_MAX_OUTPUT_TOKENS,
                list_key="candidates",
            ),
            kind="candidate_selection",
        )

    def adjudicate_relationships(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Classify supplied pairs from two-sided evidence without editing notes."""

        self._authorize_request()
        return _validate_relationship_response(
            self._literature_json_call(
                _relationship_adjudication_system_prompt(),
                _relationship_prompt(profiles, request, context),
                label="relationship adjudication",
                reasoning_effort="max",
                output_tokens=RELATIONSHIP_MAX_OUTPUT_TOKENS,
                list_key="decisions",
            ),
            kind="relationship_adjudication",
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
        repair_source_ids = [
            str(value)
            for value in (context or {}).get("coverage_repair_source_ids", []) or []
            if str(value)
        ]
        component_source_ids = [
            str(value)
            for value in (context or {}).get("coverage_component_source_ids", []) or []
            if str(value)
        ]
        packet_source_ids = component_source_ids or repair_source_ids
        if packet_source_ids:
            component_source_id_set = set(packet_source_ids)
            proposal_profiles = [
                profile
                for profile in proposal_profiles
                if str(profile.get("source_id") or "") in component_source_id_set
            ]
        audit_mode = str((context or {}).get("coverage_audit_mode") or "")
        instruction = (
            "Audit thematic coverage across the complete analytical collection. The focus-source list identifies sources that "
            "remain unclustered, but every supplied profile may be used to recover a missing cluster, repair fragmented membership, "
            "or distinguish overlapping clusters. Test recognizable research conversations and evidence bases even when their "
            "studies address different propositions. Exact proposition agreement is not required for thematic cluster formation. "
            "Use the deterministic candidate components as attention hints, checking whether each contains an overlooked coherent "
            "conversation; they are not evidence and must not force admission. Account for every focus source by either returning a "
            "supported cluster proposal or leaving it unproposed when the supplied evidence does not establish a coherent group. "
            "Keep this audit packet compact: use one best locator-backed anchor per core source, at most one proposition and one "
            "family relation per proposed cluster, no repeated findings, and no rationale longer than two sentences. "
            "Never return a singleton cluster. Return only new proposals or complete corrected versions of prior proposals whose "
            "membership changes, and preserve stable semantic identities for corrected proposals."
            if audit_mode == "collection"
            else
            "Audit one semantically connected whole-profile component for missing or fragmented thematic clusters. Focus on the "
            "listed unclustered sources while using supplied neighboring or already-clustered profiles as comparison, context, or "
            "bridge evidence. Exact proposition agreement is not required. Never return a singleton cluster. Return only a genuinely "
            "new proposal or a complete corrected prior proposal whose membership changes; preserve its semantic identity."
            if repair_source_ids
            else "Propose the complete set of coherent, overlapping collection debate families."
        )
        user_prompt = _literature_prompt(
            proposal_profiles,
            request,
            _cluster_proposal_context(context),
            instruction=instruction,
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="cluster proposal",
                reasoning_effort="high",
                output_tokens=(
                    CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS
                    if audit_mode == "collection"
                    else 24_000
                    if repair_source_ids
                    else CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS
                ),
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
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="debate mapping",
                reasoning_effort="high",
            ),
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
        response_budget = dict((context or {}).get("response_budget", {}) or {})
        budget_instruction = ""
        if response_budget:
            budget_instruction = (
                " This is a structurally large cluster. To guarantee complete JSON, use at most "
                f"{int(response_budget.get('max_evidence_threads', 3))} evidence threads, exactly one best "
                "source_contribution per core source, at most "
                f"{int(response_budget.get('max_central_findings', 3))} central findings, at most "
                f"{int(response_budget.get('max_items_per_optional_section', 2))} items in every other optional "
                "narrative array, and at most "
                f"{int(response_budget.get('max_gap_hypotheses', 2))} gap hypotheses. Use empty arrays for unused "
                f"sections and keep the entire response below {int(response_budget.get('max_output_tokens', 4_500))} "
                "output tokens. Do not repeat source contributions in synthesis or evidence-thread summaries."
            )
        instruction = (
            "Repair the prior cluster synthesis so every listed validation requirement passes. "
            "Return a substantive bottom-line verdict that answers the cluster question and covers every admitted "
            "proposition. The source_contributions section—not the verdict itself—must retain each core source's important "
            "cluster-relevant findings. Distinguish association from causation and explain agreements, "
            "differences, weaknesses, and evidentiary limits. Preserve only supplied evidence and locators."
            if repair_requirements
            else (
                "Synthesize this admitted cluster and retain the evidence and inferential logic behind each important source-specific "
                "contribution even without multi-source agreement. Never translate an observational association as something that "
                "'works better', 'helps', 'drives', 'leads to', or 'causes'; preserve associational wording in plain English. Normalize "
                "inverse predictor framings before calling findings inconsistent. For Kuperman's muscular-mediation analysis, include "
                "the Rwanda civil-war and genocide context, the distinct half-million-Tutsi and broader up-to-one-million estimates, "
                "the author-reported 500-fold and 6,000-fold descriptive comparisons, and the process-tracing alternatives and limits. "
                "Emit only specific, rule-bound collection gap hypotheses."
            )
        )
        instruction += budget_instruction
        user_prompt = _literature_prompt(
            profiles,
            request,
            context,
            instruction=instruction,
        )
        user_prompt += (
            "\n\nFINAL SYNTHESIS REQUIREMENTS — apply these after reading every atomic note above:\n"
            "1. Preserve the case, period, actors, observed comparison, method, explanatory logic, and inferential limit "
            "when they are necessary to understand a core source.\n"
            "2. In plain English, observational results remain associations: never say a strategy works better, helps, "
            "drives, leads to, causes, or increases an outcome.\n"
            "3. For Kuperman, the cluster contribution is incomplete unless it names the 1990–1994 Rwanda civil war and "
            "1994 genocide, separates the estimated half-million Tutsi from the broader up-to-one-million Rwandans, "
            "labels the reported 500-fold total and 6,000-fold monthly comparisons as descriptive arithmetic rather "
            "than mediation effects, and explains the process-tracing logic and alternative causes.\n"
            "Return only the requested JSON object."
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="cluster synthesis",
                reasoning_effort="max",
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
        user_prompt += (
            "\n\nFINAL GAP REQUIREMENTS:\n"
            "Retain only a specific, non-obvious collection-native puzzle supported by the supplied evidence. Do not "
            "invent follow-up years, sample sizes, cases, datasets, measures, mechanisms, data structures, or study-design "
            "details. Reject the generic observation that an observational association lacks causal identification unless the "
            "collection reveals a concrete non-obvious puzzle beyond that standard limitation and explains what inference or "
            "decision turns on it. A resolution path states only the kind of evidence needed; it is not a project design. If the supplied "
            "evidence cannot support that specificity, reject the candidate. Return only the requested JSON object."
        )
        return _validate_literature_response(
            self._literature_json_call(
                system_prompt,
                user_prompt,
                label="gap adjudication",
                reasoning_effort="high",
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
        reasoning_effort: str = "high",
        output_tokens: int | None = None,
        list_key: str | None = None,
    ) -> Mapping[str, Any]:
        self.last_literature_response = None
        output_tokens = max(
            self.max_output_tokens, output_tokens or LITERATURE_MAX_OUTPUT_TOKENS
        )
        deadline_seconds = self._request_deadline_seconds()
        self._ensure_prompt_fits(system_prompt, user_prompt, output_tokens, label=label)
        response = _parse_json_object(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                deadline_seconds,
                reasoning_effort=reasoning_effort,
            ),
            label=f"{label} response",
            list_key=list_key,
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
        output_tokens = self._bounded_output_tokens(
            max_output_tokens, default=self._chunk_token_cap
        )
        request_deadline = self._bounded_deadline(deadline_seconds)
        self._ensure_prompt_fits(
            system_prompt, user_prompt, output_tokens, label="coarse chunk"
        )
        return _parse_chunk_evidence(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                request_deadline,
                reasoning_effort="high",
            )
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
        output_tokens = self._bounded_output_tokens(
            max_output_tokens, default=self.max_output_tokens
        )
        request_deadline = self._bounded_deadline(deadline_seconds)
        self._ensure_prompt_fits(
            system_prompt, user_prompt, output_tokens, label="chunk synthesis"
        )
        return _parse_analysis(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                request_deadline,
                reasoning_effort="high",
            )
        )

    def _generate_with_reasoning(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
        *,
        reasoning_effort: str,
    ) -> Any:
        if reasoning_effort not in {"high", "max"}:
            raise ValueError("reasoning_effort must be high or max")
        token = _REASONING_EFFORT.set(reasoning_effort)
        try:
            # Keep the transport method's historical four-argument protocol so
            # existing reader integrations and test doubles remain compatible.
            return self._generate_text(
                system_prompt, user_prompt, output_tokens, deadline_seconds
            )
        finally:
            _REASONING_EFFORT.reset(token)

    def _prompt_fits(
        self, system_prompt: str, user_prompt: str, output_tokens: int
    ) -> bool:
        estimated_input = _estimate_tokens(system_prompt) + _estimate_tokens(
            user_prompt
        )
        reserve = self.prompt_reserve_tokens + output_tokens
        usable = int(int(self.context_window_tokens or 0) * self.direct_read_fraction)
        return estimated_input + reserve <= usable

    def _ensure_prompt_fits(
        self, system_prompt: str, user_prompt: str, output_tokens: int, *, label: str
    ) -> None:
        if not self._prompt_fits(system_prompt, user_prompt, output_tokens):
            raise ProviderError(
                f"{label} exceeds the {self.name} context budget; use coarse hierarchical chunks"
            )

    def _request_deadline_seconds(self) -> float:
        return float(
            self.request_deadline if self.request_deadline is not None else self.timeout
        )

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
    last_literature_response: dict[str, Any] | None = field(
        init=False, default=None, repr=False
    )

    def __post_init__(self) -> None:
        self._configure_capabilities()

    def _authorize_request(self) -> None:
        if not self.allow_cloud:
            raise CloudPermissionError(
                f"{self.name} requires explicit allow_cloud consent"
            )
        if not os.getenv(self.api_key_env):
            raise ProviderError(f"{self.api_key_env} is not configured")

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> Any:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": output_tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.name == "deepseek":
            # DeepSeek V4's OpenAI-compatible API accepts these raw top-level
            # fields. Temperature is intentionally omitted in thinking mode.
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = _REASONING_EFFORT.get() or "high"
        else:
            body["temperature"] = 0
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
                raise ProviderError(
                    f"{self.name} response incomplete: finish_reason={finish_reason}"
                )
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
    last_literature_response: dict[str, Any] | None = field(
        init=False, default=None, repr=False
    )

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
            {
                "inline_data": {
                    "mime_type": media_type,
                    "data": base64.b64encode(document).decode("ascii"),
                }
            },
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
    last_literature_response: dict[str, Any] | None = field(
        init=False, default=None, repr=False
    )

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
        "You create source-faithful atomic notes using Auto-Zettelkasten atomic prompt v8. "
        "Adapt the analysis to the source actually supplied: it may be an academic article or book, a report, policy or legal "
        "document, archival material, conference or meeting record, practitioner guidance, speech, working paper, blog post, "
        "or another evidence-bearing source. Do not force a nonacademic source into an academic-study template. "
        "Return only one JSON object. Do not infer facts absent from the source. "
        f"Every value must be a non-empty string. Required keys: {keys}. "
        "Copy only numbers and numerical comparisons explicitly printed in the supplied source. Do not calculate, derive, correct, "
        "convert, combine, or extrapolate a number, rate, ratio, percentage, fold-change, or illustrative example. If the source's "
        "arithmetic appears inconsistent, report it as the source's claim and flag the uncertainty without solving it. "
        "Identify the source's important findings, arguments, observations, interpretations, or recommendations and explain "
        "the method or knowledge basis behind them. Preserve the case or conflict, actors, population, geography, period, "
        "outcome, and comparison needed to understand each important point. Distinguish what the source observes or reports, "
        "the author's interpretation or argument, and what the method and evidence can actually establish. Use the organization and "
        "language best suited to the actual source; do not force fixed labels or an experimental template onto historical, qualitative, "
        "normative, practitioner, meeting, or other non-experimental material. "
        "Detailed findings must retain the source's exact estimates, units, denominators, sample sizes, uncertainty measures, "
        "technical labels, examples, and qualifications when present. Pair technical detail with a plain-English explanation; "
        "do not replace the figures with a simplification or leave them unexplained. "
        "For every important quantitative finding, explain for a statistically non-specialist what changed, by how much, compared with "
        "what, and what the reported uncertainty does and does not establish. Do this naturally rather than as a compulsory checklist, "
        "and never invent a numerical example or transformation. "
        "When a source uses a ratio or fold-change, identify the numerator, denominator, comparison periods or groups, and any "
        "different population estimates used in the arithmetic. Do not silently substitute a subgroup estimate for a broader "
        "estimate, or present descriptive before-and-after arithmetic as an identified causal effect. For comparative or "
        "process-tracing arguments, explain the sequence, counterfactual logic, and alternative explanations actually considered, "
        "then state the limitations of that inferential strategy. In historically sensitive cases, specify the conflict, place, "
        "dates or phase, actors, and context—including genocide or mass-atrocity context when the source makes it relevant—rather "
        "than using an ambiguous country label. "
        "Apply that distinction inside Detailed Findings and Plain-English Interpretation, not only in the critique. For causal narratives "
        "based on historical comparison, case study, or process tracing, explain the observed events and reported numbers, what the author "
        "argues explains them, the comparison or counterfactual logic, and what the design cannot rule out. Attribute the explanatory step "
        "with phrases such as 'the author argues' unless the design identifies the effect. A source-reported before-and-after fold-change is "
        "descriptive arithmetic, not an estimated causal effect. Do not recalculate or silently replace its denominators, and do not compute "
        "additional deaths, rates, percentages, hypothetical populations, or other examples absent from the source. "
        "For Kuperman's muscular-mediation comparison in particular, keep separate the approximately half-million Tutsi estimate and the "
        "broader Rwandan population figure used in the author's fold-change calculations; identify the civil-war and genocide context; label "
        "the 500-fold and 6,000-fold figures as the author's descriptive arithmetic rather than estimated mediation effects; and explain the "
        "author's comparative process-tracing argument, counterfactual logic, and limitations. "
        "If a baseline, denominator, comparison, or uncertainty measure is absent, say that it is not reported and explain "
        "why that limits interpretation. Never invent a benchmark, call an effect large or small without a stated reference, "
        "treat statistical significance as practical importance, or treat association as causation. "
        "PDF extraction may flatten tables, footnotes, and multi-column layouts. Use surrounding prose to recover conclusions, "
        "but never invent an exact row-column relationship when the extracted structure is genuinely unclear. "
        "Locators must identify pages, sections, headings, or explicit text anchors. "
        "If the source does not report something, say so explicitly instead of inventing it. Before returning, silently reread "
        "the analysis against the supplied source and correct any unsupported attribution, number, context, or causal wording. "
        "Do not add a separate validation or self-review section."
    )


def _profile_system_prompt() -> str:
    return (
        "You are the evidence-profile reader for Auto-Zettelkasten profile prompt v6. "
        "Use only the committed Markdown note supplied by the user. Return exactly one JSON object with no Markdown fences, "
        "commentary, inferred full text, or literature-level cluster, debate, or gap proposals. Extract roughly 8-20 "
        "synthesis-relevant evidence anchors when the note supports that many, never more than 24 and never padded. Anchor "
        "identity must use source identity, a source-native locator or span, and evidence role rather than list position or "
        "paraphrase wording. Do not collapse a detailed note into one omnibus anchor when separate contributions have different "
        "locators or support boundaries. Adapt the evidence to the source rather than forcing every item into an academic-study form. "
        "Each anchor needs a support_envelope distinguishing empirical and argument roles, coverage, scope, and restrictions. "
        "Use exactly these values: empirical_role descriptive, associational, causal, mechanism_evidence, or none; argument_role "
        "conceptual, interpretive, normative, methodological, practitioner_guidance, or none; coverage full_text, limited_text, "
        "abstract, metadata, or unknown. support_status describes attribution to the source, not independent verification: use supported "
        "when the note explicitly shows that the source makes the finding, argument, observation, or recommendation; support_unknown "
        "when attribution is unclear; limited for limited coverage; and unsupported only when the note does not support the attribution. "
        "A supported practitioner recommendation may still carry a restriction that it does not establish effectiveness. Every anchor must "
        "provide traceable locator strings. Return the lean shape supplied by the user: keep findings empty and study_lineage null, "
        "and do not output IDs, typed source_locators, quantitative_result, or persistence metadata because the engine derives them. "
        "Use an empty string rather than null, an array, or an object for every declared string field, and add no keys outside the "
        "supplied lean shape. Generated atomic-note headings cannot support a strong synthesis assertion. Statistical anchors must preserve "
        "in their claim and fields whether a result is an observed rate, predicted probability, coefficient, marginal effect, odds ratio, percentage, "
        "reference groups, denominators, uncertainty, and source-reported versus derived values. Never convert or equate unlike "
        "estimands. Extract one source-local study_lineage record from explicit note evidence: authors or institutions, datasets, "
        "sampling frame, unit of analysis, population, period, publication/version relationships, institutional series, and "
        "overlap signals. Leave unsupported lineage fields empty. Source-level concepts, methods, cases, and outcomes are retrieval "
        "metadata and must not be copied automatically into every anchor. Preserve technical findings, plain-English meanings, "
        "qualifications, and traceable locators exactly to the degree supported by the note."
    )


def _relationship_shard_system_prompt() -> str:
    return (
        "You route relationship discovery for Auto-Zettelkasten relationship prompt v1. "
        "Return exactly one JSON object with a shard_ids array containing only IDs from the supplied shard directory. "
        "Select the smallest set of literature shards needed to find genuinely relevant works for every focus source. "
        "Use the focus thesis, method, facets, existing graph neighbors, and cluster summaries. Include a neighboring "
        "literature when a source plausibly bridges fields. Do not classify relationships and do not infer that shared "
        "keywords, methods, cases, or collections make two works intellectually related."
    )


def _relationship_candidate_system_prompt() -> str:
    return (
        "You select source comparisons for Auto-Zettelkasten relationship prompt v1. Return exactly one JSON object with "
        "a candidates array. Each row must contain source_id, target_kind, target_id, why_relevant, comparison_unit, "
        "likely_relation_type, requested_evidence_depth, and confidence. target_kind is source or cluster. "
        "requested_evidence_depth is profile, atomic_note, or source_passage. confidence is a number from 0 to 1. "
        "Select no more than twelve candidates per focus source and return none when none is worthwhile. Choose works "
        "that may support, undermine, qualify, extend, complement, offer a rival explanation, expose a boundary or "
        "methodological fault line, form a sequence, or express an interpretive disagreement. Shared vocabulary, method, "
        "case, tag, or collection alone is only a retrieval clue. Use only supplied IDs and do not classify the final "
        "relationship."
    )


def _relationship_adjudication_system_prompt() -> str:
    return (
        "You adjudicate source relationships for Auto-Zettelkasten relationship prompt v1. Return exactly one JSON object "
        "with a decisions array containing one row per supplied pair. Each row must contain source_id, target_source_id, "
        "status, relation_type, comparison_unit, reason, source_evidence_anchor_id, target_evidence_anchor_id, qualifiers, "
        "confidence, and requested_context. status is accepted, no_relationship, or needs_more_context. relation_type for "
        "accepted rows is supports, undermines, qualifies, extends, complements, rival_explanation, boundary_contrast, "
        "methodological_fault_line, sequential_relationship, or interpretive_or_normative_disagreement; otherwise use an "
        "empty string. qualifiers and requested_context are arrays of strings, confidence is 0 to 1. Compare the actual "
        "claims, findings, arguments, mechanisms, outcomes, methods, and boundaries represented by the supplied evidence. "
        "An accepted relationship requires a real anchor from each source and a plain-language reason explaining the "
        "intellectual connection. Do not convert association into causation. A shared topic, method, case, dataset, outcome, "
        "tag, or citation alone is no_relationship. Limited sources cannot establish a substantive relationship unless the "
        "supplied limited evidence itself contains both the relevant claim and its boundary. Ask for more context only when "
        "the supplied profiles leave a consequential comparison genuinely ambiguous. Never invent IDs, anchors, locators, "
        "claims, profile hashes, relation IDs, provenance, timestamps, or Markdown."
    )


def _cluster_proposal_system_prompt() -> str:
    return (
        "You are the collection-clustering reasoner for Auto-Zettelkasten cluster prompt v17. "
        "Return exactly one compact JSON object with a clusters array. Inspect the complete analytical source inventory and "
        "propose every defensible, recognizable subliterature needed to map it; do not stop after an arbitrary top-N shortlist. "
        "A cluster is a bounded topic, problem, mechanism, case family, theoretical dispute, institutional practice, or practice question that is central "
        "to several sources. Sources may address different evidence threads and need not test the same proposition. Exact "
        "proposition comparability is assessed inside a cluster and is not required for the cluster to exist. It is correct to "
        "leave an unsupported source out, but an omitted analytical source must genuinely lack a located central connection to a "
        "bounded subliterature. Each cluster must contain only proposal_id, label, semantic_identity, shared_question, "
        "bounded_object, coherence_rationale, source_ids, source_roles, supporting_evidence, propositions, and family_relations. "
        "source_roles must map each source_id to core, context, or bridge. Include no more than four total context or bridge "
        "sources per cluster. supporting_evidence must include at least one exact {source_id, evidence_anchor_id, locator} "
        "reference for every core source showing that the topic is central to that source. Do not output study_lineages, "
        "independence_assessments, evidence_base_groups, or effective_evidence_base_count. "
        "Each family_relation must use {relation_type, source_ids, rationale, comparability, evidence}. relation_type must be "
        "one of shared_research_problem, same_proposition, rival_explanation, complementary_mechanism, boundary_contrast, "
        "methodological_fault_line, sequential_relationship, or interpretive_or_normative_disagreement. Use "
        "shared_research_problem when located anchors establish central membership in one bounded subliterature but do not "
        "support an exact comparison. Evidence must contain a located anchor from every source in the relation, and the "
        "core-source relation graph must be connected. Never propose a singleton cluster: a bounded two-source literature is an "
        "emerging cluster when both sources are centrally connected, even when they offer complementary typologies, theories, cases, or guidance. "
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
        "publication type, coverage, document genre, or evidence type in role. Form clusters around bounded subliteratures and "
        "their research problems—not generic vocabulary, shared methods, geography, tags, citation proximity, or count alone. "
        "A repeated navigation facet is only a candidate signal; confirm centrality from located profile evidence. Prefer "
        "human-recognizable clusters such as Conflict Prevention or Internationalized Civil-War Mediation over proposition "
        "micro-clusters or labels named only conflict, mediation, success, or stability. Merge nested signals such as early "
        "warning into a broader cluster when they are evidence threads inside the same research problem. Every analytical source that coherently belongs "
        "may appear in a proposal and no more than three. Put all proposed members in source_ids. For every proposition, "
        "include at least one directly supporting evidence anchor from every participating core source; do not add an anchor "
        "merely to reach a target count. Practice guidance may be core "
        "for typological, conceptual, or practitioner propositions, but not empirical effectiveness propositions. Use study_lineage "
        "signals when choosing core sources, but leave all independence calculations to deterministic admission. Do not treat "
        "publications, DOIs, or source IDs as independent by default. Shared authors, datasets, sampling frames, populations, "
        "periods, institutional series, or publication/version relationships are overlap signals. Keep labels, "
        "questions, and rationales concise. Copy source_id, evidence_anchor_id, and locator strings verbatim "
        "from the supplied profiles; never abbreviate, normalize, or paraphrase a locator. "
        "Allow useful overlap, but do not assign a source to more than three clusters. Do not use Zotero tags, methods, geography, "
        "or citation proximity as proof. "
        "Before returning, explicitly audit the inventory for paired works, recurring named approaches, institutional series, "
        "mediator-power or foreign-policy cases, track-one/track-two diplomacy, ripeness or escalation theory, and other collection-specific "
        "subliteratures evidenced by at least two sources. These are examples of recognizable forms, not mandatory cluster labels. "
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
        "You are the cluster-synthesis reasoner for Auto-Zettelkasten cluster synthesis prompt v19. Read the complete atomic "
        "notes supplied for this admitted cluster. Return exactly one JSON object containing cluster_id, scope, boundaries, "
        "coherence_rationale, synthesis, evidence_threads, central_findings, agreements, "
        "positions, contradictions, boundary_conditions, methodological_fault_lines, related_clusters, source_roles, "
        "source_contributions, supporting_evidence, synthesis_assertions, debate_state, gap_hypotheses, evidence_base_groups, "
        "independence_assessments, quantitative_comparisons, and effective_evidence_base_count. "
        "evidence_threads is mandatory. Each thread must contain thread_id, title, question, summary, plain_english_meaning, "
        "relationship, source_ids, proposition_ids, and evidence. A thread organizes a real line of inquiry inside the broader "
        "cluster and may contain comparable findings, complementary mechanisms, institutional positions, methodological "
        "approaches, or important source-specific findings. Explain how its sources connect without implying agreement when "
        "their evidence is merely adjacent. Every core source must appear in at least one evidence thread, and the threads together "
        "must cover every principal conceptual, empirical, methodological, or practice position needed to answer the cluster question. "
        "A source-specific bullet cannot substitute for that cluster-level organization. source_contributions is mandatory: retain one to three important cluster-relevant contributions from every core source "
        "and at most one from each context or bridge source. A unique finding remains visible with comparison_status single_source; "
        "it does not need multi-source agreement. Each contribution must state source_id, cluster_role, contribution_kind, "
        "related_proposition_ids, evidence_thread_id, finding, technical_result, plain_english_meaning, relation_to_cluster_question, comparison_status, "
        "and evidence. Core contributions prioritize direct proposition findings, unique relevant findings, boundary evidence, and "
        "methodological contributions. When a source's main contribution depends on a case comparison, historical sequence, or "
        "process-tracing argument, retain the decisive case evidence as well as the abstract theory: identify the conflict or case, "
        "actors, period, observed sequence or comparison, the author's explanatory logic, and the most important alternative explanation "
        "or inferential limit. Use a second contribution from that source when needed to make this logic understandable. Context and "
        "bridge contributions must say context_only and cannot enter the verdict, central "
        "cross-source findings, agreement, contradiction, debate classification, causal-effectiveness claim, or gap promotion. "
        "Every substantive synthesis assertion must use real source-local evidence_anchor_id and locator references. An exact "
        "cross-source comparison must name one or more admitted proposition_ids. A broader family assertion may instead be "
        "supported by supplied family_relations, but must identify it as complementary, sequential, conditional, methodological, "
        "parallel, or interpretive rather than consensus or contradiction. "
        "Descriptive, associational, illustrative, or prescriptive anchors cannot support causal wording. Do not combine "
        "differently defined actors, mechanisms, populations, outcomes, or estimands into consensus, and do not label "
        "incomparable claims as contradictions. The synthesis must answer the cluster question in connected prose rather than "
        "merely listing contributions. Explain what technical findings mean in plain English while retaining "
        "reported figures, comparisons, uncertainty, and qualifications. Plain English must never strengthen the source's inference: "
        "for observational associations say 'is associated with' or 'appears alongside', not 'works', 'helps', 'drives', 'leads to', "
        "or 'causes'. For descriptive before-and-after arithmetic, name the observed periods or groups and attribute any explanatory "
        "claim to the author. Preserve quantitative_result types: observed rates, "
        "model-predicted probabilities, coefficients, marginal effects, odds ratios, and raw percentages are not interchangeable. "
        "Keep every uncertainty statistic attached to the exact estimate or diagnostic it qualifies. A p-value for a selection "
        "correlation, specification test, or different coefficient must never be described as the p-value for the focal result. "
        "Prefer a narrow source-local anchor and its exact locator over a composite note summary whenever both are available. "
        "Separate mediation occurrence or selection, process design, and mediation success or settlement outcomes: evidence about "
        "whether mediation happens cannot by itself answer whether mediation succeeds. Preserve temporal tensions and opposite "
        "directions. Never say all, both, every, or consistently when one cited source reports decline, a null result, a different "
        "period, or an evidentiary qualification. State that tension and explain what changes across periods or evidence designs. "
        "Universal wording must cite every core source it describes. If prose names an author, report, or institution, the evidence "
        "for that exact sentence must include that source; never attach one publication's name to another publication's number. "
        "Every displayed number must occur in the union of the cited source-local anchors for that sentence. "
        "Before treating quantitative findings as disagreement, contradiction, or a measurement gap, restate them using the same "
        "predictor and outcome orientation. 'Higher fatalities are associated with lower success' and 'low fatalities are associated "
        "with higher success' describe the same direction, not opposite results. Continuous, categorical, and dichotomous measures "
        "may be a methodological difference, but that difference alone is not a collection gap unless the located results remain "
        "substantively unresolved after orientation and estimands are aligned. "
        "For each publication, choose the finding that most changes the cluster-level inference. Training counts, web visits, "
        "workshop totals, and other implementation activity belong only when implementation reach is part of the cluster question; "
        "otherwise prefer the source's substantive finding, mechanism, boundary, comparison, or central recommendation. "
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
        "or no_debate as debate_state. The supplied deterministic_debate is authoritative. Explain why strict consensus or "
        "contradiction is not established when the family is instead complementary, conditional, mixed, parallel, or concentrated. "
        "Those explanations must be cluster-specific: name the actual proposition, outcome definition, measure, method, population, "
        "period, evidence-base dependence, or boundary that prevents the strict inference. Generic source-count boilerplate is insufficient. "
        "The input includes an all_clusters catalog with human labels, questions, propositions, bounded objects, and compact evidence. "
        "Use related_clusters only for defensible intellectual relationships to entries in that catalog. Each relationship must use "
        "{target_cluster_id, relation_type, relationship, current_evidence, target_evidence}. relationship must explain a substantive "
        "bridge, contrast, sequence, shared mechanism, or boundary in plain English. current_evidence must cite at least one exact anchor "
        "from this cluster, and target_evidence at least one exact anchor supplied for the target cluster. Shared generic words, a shared "
        "tag, or a context-only source are insufficient. "
        "Do not create a debate when evidence is consensus or incomparable. "
        "Keep the response detailed but bounded so the JSON always finishes. synthesis must be one 120-250 word bottom-line "
        "answer, not a second copy of the whole note. Use at most six evidence threads, each with a summary of at most 180 words. "
        "Keep each source contribution's finding, technical result, and plain-English meaning together under 120 words. Keep "
        "other narrative items under 100 words each and return at most three gap hypotheses. Do not repeat one assertion across "
        "synthesis, evidence_threads, central_findings, and source_contributions: put the detailed support in its best-fitting "
        "structured field and let synthesis state the conclusion. Keep the complete JSON below 7,500 output tokens. For a "
        "source_backed_cluster, supply enough non-repetitive detail for roughly a 900-1,800 word Markdown projection; for an "
        "emerging_cluster, supply enough detail for 600-1,200 words. Organize the analysis by evidence thread and prefer evidence "
        "density over filler. Inspect the mapped propositions and evidence threads for defensible collection-native gap hypotheses. "
        "For the Kuperman muscular-mediation source, a theory-only contribution is incomplete. Include the Rwanda evidence as the "
        "1990-1994 civil-war and 1994 genocide case; distinguish the estimated half-million Tutsi killed from the broader up-to-one-million "
        "Rwandans used in the author's arithmetic; identify the 500-fold total and 6,000-fold monthly comparisons as source-reported "
        "descriptive arithmetic rather than mediation effects; and explain the author's comparative process-tracing logic and its inability "
        "to rule out the RPF offensive, the presidential plane shoot-down, long-standing ethnic conflict, or other alternative causes. "
        "Return an empty gap_hypotheses array when none is specific and worthwhile. Never call something a gap in synthesis, an "
        "evidence thread, or a source contribution unless the same bounded candidate appears in gap_hypotheses for deterministic adjudication. "
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
        "You are the collection-gap adjudicator for Auto-Zettelkasten gap prompt v12. Return exactly one JSON object with "
        "gaps and rejected arrays. Consider only supplied candidates and their deterministic all-collection search results. "
        "You may merge equivalent candidates or perform at most one evidence-constrained reframing of a candidate, but may "
        "not manufacture a new literature gap. A missing test is not itself a worthy gap. Retain a candidate only when it "
        "poses a non-obvious puzzle, survives its strongest obvious answer, changes an inference or decision that matters, "
        "and has a feasible type-sensitive resolution path. Each retained gap must contain gap_id, proposition_id, "
        "originating_proposition_id, originating_cluster_ids, title, gap_statement, rule, related_cluster_ids, "
        "generation_explanation, observed_pattern, precise_missing_evidence, supporting_evidence, countervailing_evidence, "
        "internal_search_summary, closest_prior_explanation, decision_reasoning, evidence_needed, why_matters, contribution, "
        "confidence, value_assessment, resolution_path, anchors, merged_from_gap_ids, reframed_from_gap_id, and priority_tier. "
        "related_cluster_ids must contain only clusters that supplied the originating proposition or locator-backed generating "
        "evidence. Do not add a thematically adjacent cluster merely because the proposed research could interest it. Never call "
        "publications the same dataset, sample, or independent replication unless the supplied study-lineage and evidence-base "
        "records explicitly establish that relationship. When independence is uncertain, say it is uncertain and narrow the gap "
        "to independent-author, independent-data, or later-period replication as the supplied evidence permits. "
        "value_assessment must contain puzzle_type, puzzle, strongest_obvious_answer, why_obvious_answer_is_inadequate, "
        "competing_explanations, decision_or_inference_changed, information_gain, non_obviousness_passed, importance_passed, "
        "and rejection_reasons. resolution_path must contain path_type, question, evidence_needed, requirements, feasibility, "
        "and limitations. Choose path_type from quantitative, qualitative, historical_interpretive, theoretical, normative, "
        "methodological, or practitioner and provide requirements specific to that type. Do not force estimands or experiments "
        "onto the wrong evidence type. Use these exact non-empty requirement keys: quantitative requires estimand, comparison, "
        "identification, and measurement; qualitative requires case_selection, mechanism_evidence, negative_cases, and "
        "process_observations; historical_interpretive requires archives, periodization, source_criticism, and "
        "competing_interpretations; theoretical requires premises, derivation, scope, and model_comparison; normative requires "
        "principles, objections, and application_tests; methodological requires assumptions, diagnostics, benchmarks, and "
        "robustness; practitioner requires implementation_evidence, institutional_context, and bias_checks. Each requirement "
        "must say concretely what would be compared, observed, measured, derived, or checked for this candidate. Do not return "
        "an empty requirements object. This is a route to "
        "discriminating evidence, not a finalized project study design. "
        "Do not invent named cases, datasets, instruments, comparison groups, or identification strategies. Mention one only "
        "when it is explicitly present in the supplied collection evidence; otherwise state the type of comparison or evidence needed. "
        "competing_explanations, rejection_reasons, and resolution-path limitations must be arrays of non-empty strings. "
        "information_gain and priority_tier must each be exactly "
        "high, moderate, or low. Anchors must copy cluster_id, section, and item_id from a supplied cluster item whose evidence "
        "generated the puzzle; omit an anchor rather than inventing one. Evidence references must use supplied source_id, "
        "claim_id, and locator values. Preserve full "
        "reasoning only for retained gaps. Do not enumerate routine rejections: a candidate omitted from both arrays is "
        "deterministically recorded as not retained. Use rejected only when a short model-specific reason adds audit value. "
        "Each rejected item must be compact and contain only gap_id, status, and a specific reason of at most 15 words; "
        "never repeat evidence or candidate prose in rejected. Deduplicate semantically "
        "equivalent candidates aggressively; list their IDs in merged_from_gap_ids on the retained canonical gap. Reject "
        "obvious, low-value, infeasible, vague, collection-answered, unlocated, or unsupported candidates with a concrete "
        "reason. For every plausible retained lead, explain why it does or does not meet the stricter strong-gap threshold; "
        "the deterministic collection checks remain authoritative. The women-or-civil-society inclusion candidate is not useful merely because a causal pathway has not been "
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
        results = (
            candidate.get("internal_search_results") or search.get("results") or []
        )
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
        warnings = (
            candidate.get("warnings") or search.get("limited_profile_warnings") or []
        )
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
                        search.get(
                            "analytical_profile_count_searched", len(result_rows)
                        )
                        or 0
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
        context_value = (
            raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
        )
        claims = []
        for claim in (
            raw.get("evidence_anchors")
            or raw.get("claims")
            or raw.get("findings")
            or []
        ):
            value = (
                claim.to_dict()
                if hasattr(claim, "to_dict")
                else dict(claim)
                if isinstance(claim, Mapping)
                else {}
            )
            claim_id = str(
                value.get("evidence_anchor_id")
                or value.get("claim_id")
                or value.get("finding_id")
                or ""
            )
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
                "analytical": bool(
                    raw.get("analytical", not raw.get("excluded_from_synthesis", False))
                ),
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
    iterable = (
        synthesis_rows.values()
        if isinstance(synthesis_rows, Mapping)
        else synthesis_rows or []
    )
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


def _relationship_prompt(
    profiles: Sequence[EvidenceProfile],
    request: LiteratureMapRequest,
    context: Mapping[str, Any] | None,
) -> str:
    payload = {
        "request": {
            "source_set_id": str(getattr(request, "source_set_id", "") or ""),
            "provider": str(getattr(request, "provider", "") or ""),
            "model": str(getattr(request, "model", "") or ""),
        },
        "focus_profiles": [
            _evidence_profile_payload(profile) for profile in profiles
        ],
        "context": dict(context or {}),
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


def _evidence_profile_payload(
    profile: EvidenceProfile | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(profile, EvidenceProfile):
        return profile.to_dict()
    if isinstance(profile, Mapping):
        return dict(profile)
    raise ProviderError(
        "literature profiles must be EvidenceProfile values or mappings"
    )


def _cluster_proposal_profile(
    profile: EvidenceProfile | Mapping[str, Any],
) -> dict[str, Any]:
    """Compact semantic projection for one coarse collection-clustering call."""

    raw = _evidence_profile_payload(profile)
    context = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
    dimensions = (
        raw.get("dimensions") if isinstance(raw.get("dimensions"), Mapping) else {}
    )

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
            if isinstance(value, Sequence) and not isinstance(
                value, (bytes, bytearray)
            ):
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
    for finding in (
        raw.get("evidence_anchors") or raw.get("findings") or raw.get("claims") or []
    ):
        value = (
            finding.to_dict()
            if hasattr(finding, "to_dict")
            else dict(finding)
            if isinstance(finding, Mapping)
            else {}
        )
        finding_id = str(
            value.get("evidence_anchor_id")
            or value.get("finding_id")
            or value.get("claim_id")
            or ""
        )
        locator = str(value.get("locator") or "")
        if not finding_id or not locator:
            continue
        finding_dimensions = (
            value.get("dimensions")
            if isinstance(value.get("dimensions"), Mapping)
            else {}
        )
        envelope = (
            value.get("support_envelope")
            if isinstance(value.get("support_envelope"), Mapping)
            else {}
        )
        anchors.append(
            {
                "evidence_anchor_id": finding_id,
                "claim": str(value.get("claim") or value.get("text") or ""),
                "direction": str(value.get("direction") or ""),
                "outcome": "; ".join(
                    values(value.get("outcome"), finding_dimensions.get("outcome"))
                ),
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
        "study_family_id": str(
            raw.get("study_family_id") or raw.get("source_id") or ""
        ),
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
                "source_ids": [
                    str(value) for value in relation.get("source_ids", []) or []
                ],
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
    prior_proposals = []
    for proposal in (context or {}).get("prior_proposals", []) or []:
        if not isinstance(proposal, Mapping):
            continue
        prior_proposals.append(
            {
                key: proposal.get(key)
                for key in (
                    "proposal_id",
                    "label",
                    "semantic_identity",
                    "shared_question",
                    "bounded_object",
                    "source_ids",
                    "source_roles",
                    "supporting_evidence",
                    "propositions",
                    "family_relations",
                )
                if proposal.get(key) not in (None, "", [])
            }
        )
    return {
        "relations": rows,
        "propositions": proposition_rows,
        "topic_neighborhoods": neighborhood_rows,
        "navigation_facets": neighborhood_rows,
        "topic_neighborhoods_are_candidate_signals_only": True,
        "coverage_repair_source_ids": [
            str(value)
            for value in (context or {}).get("coverage_repair_source_ids", []) or []
            if str(value)
        ],
        "coverage_focus_source_ids": [
            str(value)
            for value in (context or {}).get("coverage_focus_source_ids", []) or []
            if str(value)
        ],
        "coverage_component_source_ids": [
            str(value)
            for value in (context or {}).get("coverage_component_source_ids", []) or []
            if str(value)
        ],
        "coverage_audit_mode": str(
            (context or {}).get("coverage_audit_mode") or ""
        ),
        "coverage_component_signature": str(
            (context or {}).get("coverage_component_signature") or ""
        ),
        "coverage_candidate_components": [
            {
                "focus_source_ids": [
                    str(value)
                    for value in row.get("focus_source_ids", []) or []
                    if str(value)
                ],
                "source_ids": [
                    str(value)
                    for value in row.get("source_ids", []) or []
                    if str(value)
                ],
            }
            for row in (context or {}).get("coverage_candidate_components", []) or []
            if isinstance(row, Mapping)
        ],
        "current_clusters": [
            {
                key: cluster.get(key)
                for key in (
                    "cluster_id",
                    "label",
                    "semantic_identity",
                    "shared_question",
                    "source_ids",
                    "source_roles",
                )
                if cluster.get(key) not in (None, "", [])
            }
            for cluster in (context or {}).get("current_clusters", []) or []
            if isinstance(cluster, Mapping)
        ],
        "current_unclustered_sources": [
            dict(row)
            for row in (context or {}).get("current_unclustered_sources", []) or []
            if isinstance(row, Mapping)
        ],
        "prior_proposal_identities": [
            str(value)
            for value in (context or {}).get("prior_proposal_identities", []) or []
            if str(value)
        ],
        "prior_proposals": prior_proposals,
        "coverage_repair_attempt": int(
            (context or {}).get("coverage_repair_attempt", 0) or 0
        ),
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
        for key in (
            "title",
            "creators",
            "date",
            "publicationTitle",
            "DOI",
            "url",
            "itemType",
        )
        if metadata.get(key)
    }
    extraction_value = next(
        (
            metadata.get(key)
            for key in (
                "_source_context",
                "source_context",
                "extraction_provenance",
                "extraction",
            )
            if isinstance(metadata.get(key), Mapping)
        ),
        None,
    )
    if isinstance(extraction_value, Mapping):
        compact_extraction = {
            key: extraction_value.get(key)
            for key in (
                "source_type",
                "coverage",
                "source_scope",
                "extraction_route",
                "route",
                "page_count",
                "embedded_text_page_count",
                "ocr_page_count",
                "unresolved_pages",
            )
            if extraction_value.get(key) not in (None, "", [])
        }
        if compact_extraction:
            safe_metadata["extraction"] = compact_extraction
    return f"Metadata: {json.dumps(safe_metadata, ensure_ascii=False)}\nQuestion lens: {question or 'none'}"


def _source_prompt(text: str, metadata: Mapping[str, Any], question: str | None) -> str:
    return (
        f"{_metadata_prompt(metadata, question)}\n\nINSPECTED SOURCE CONTENT:\n{text}"
    )


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


def _parse_json_object(
    value: Any, *, label: str, list_key: str | None = None
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list) and list_key:
        return {list_key: value}
    text = str(value).strip()
    fenced = re.match(
        r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE
    )
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # OpenAI-compatible reasoning providers occasionally wrap an otherwise
        # valid JSON object in a short sentence even when JSON-only output was
        # requested. Recover exactly one embedded object, but keep rejecting
        # ambiguous responses containing multiple JSON objects.
        decoder = json.JSONDecoder()
        recovered: list[dict[str, Any]] = []
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                recovered.append(candidate)
        if len(recovered) != 1:
            raise ProviderError(f"{label} was not valid JSON") from exc
        payload = recovered[0]
    if isinstance(payload, list) and list_key:
        return {list_key: payload}
    if not isinstance(payload, dict):
        raise ProviderError(f"{label} must be a JSON object")
    return payload


def _validate_literature_response(
    payload: Mapping[str, Any], *, kind: str
) -> dict[str, Any]:
    def string_values(value: Any) -> list[str]:
        if isinstance(value, list):
            return [
                item.strip() for item in value if isinstance(item, str) and item.strip()
            ]
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
            return "; ".join(
                item.strip() for item in value if isinstance(item, str) and item.strip()
            )
        return str(value or "").strip()

    try:
        if kind == "cluster_proposal":
            if set(payload) != {"clusters"} or not isinstance(
                payload.get("clusters"), list
            ):
                raise ValueError(
                    "cluster proposal response must contain only a clusters list"
                )
            normalized_clusters: list[dict[str, Any]] = []
            for row in payload["clusters"]:
                if not isinstance(row, Mapping):
                    raise ValueError("cluster proposal rows must be mappings")
                proposal = dict(row)
                # Comparability is explanatory provider prose, not a trusted
                # admission result.  Some OpenAI-compatible providers return
                # a compact string here despite the requested object shape.
                # Preserve that useful assessment without allowing a harmless
                # shape variation to discard the complete paid response.
                family_relations = proposal.get("family_relations")
                if isinstance(family_relations, list):
                    normalized_relations: list[Any] = []
                    for relation in family_relations:
                        if not isinstance(relation, Mapping):
                            normalized_relations.append(relation)
                            continue
                        normalized_relation = dict(relation)
                        comparability = normalized_relation.get("comparability", {})
                        if not isinstance(comparability, Mapping):
                            summary = scalar_text(comparability)
                            normalized_relation["comparability"] = (
                                {"provider_assessment": summary} if summary else {}
                            )
                        normalized_relations.append(normalized_relation)
                    proposal["family_relations"] = normalized_relations
                propositions = proposal.get("propositions")
                if isinstance(propositions, list):
                    normalized_propositions: list[Any] = []
                    for proposition in propositions:
                        if not isinstance(proposition, Mapping):
                            normalized_propositions.append(proposition)
                            continue
                        normalized_proposition = dict(proposition)
                        comparability = normalized_proposition.get("comparability", {})
                        if not isinstance(comparability, Mapping):
                            summary = scalar_text(comparability)
                            normalized_proposition["comparability"] = (
                                {"provider_assessment": summary} if summary else {}
                            )
                        normalized_propositions.append(normalized_proposition)
                    proposal["propositions"] = normalized_propositions
                # Independence is a deterministic admission gate. A model may
                # reference lineage IDs from its context, but it cannot author
                # or override the canonical lineage/group records.
                for derived_field in (
                    "cluster_id",
                    "study_lineages",
                    "evidence_base_groups",
                    "independence_assessments",
                    "effective_evidence_base_count",
                ):
                    proposal.pop(derived_field, None)
                normalized_clusters.append(
                    ClusterProposal.from_dict(proposal).to_dict()
                )
            return {"clusters": normalized_clusters}
        if kind == "cluster_synthesis":
            normalized = dict(payload)
            # DeepSeek sometimes adds a top-level explanation alongside the
            # requested object during a repair. It is noncanonical commentary,
            # not evidence, so discard it instead of wasting the otherwise
            # complete paid synthesis. Unknown substantive fields remain strict.
            normalized.pop("explanation", None)
            debate_explanation = scalar_text(
                normalized.pop("debate_explanation", "")
            )
            if debate_explanation:
                synthesis = scalar_text(normalized.get("synthesis"))
                if debate_explanation.casefold() not in synthesis.casefold():
                    normalized["synthesis"] = " ".join(
                        value for value in (synthesis, debate_explanation) if value
                    )
            if "evidence_threads" not in normalized and isinstance(
                normalized.get("evidentiary_threads"), list
            ):
                # Some OpenAI-compatible models use this harmless synonym
                # despite the requested schema. Normalize the known alias;
                # all other unknown fields remain strict contract failures.
                normalized["evidence_threads"] = normalized.pop("evidentiary_threads")
            # Independence and evidence-base counts are deterministic admission
            # outputs. They may be supplied to the model as context, but model-
            # authored variants must never override the canonical accounting or
            # make an otherwise recoverable paid synthesis fail parsing.
            for derived_field in (
                "evidence_base_groups",
                "independence_assessments",
                "effective_evidence_base_count",
                "quantitative_comparisons",
                "strict_adjudications",
            ):
                normalized.pop(derived_field, None)
            boundary_rows = normalized.get("boundaries")
            structured_boundaries = (
                [dict(row) for row in boundary_rows or [] if isinstance(row, Mapping)]
                if isinstance(boundary_rows, list)
                else []
            )
            normalized["boundaries"] = (
                [
                    text
                    for row in boundary_rows or []
                    if (text := _cluster_boundary_text(row))
                ]
                if isinstance(boundary_rows, list)
                else []
            )
            if structured_boundaries:
                existing_conditions = normalized.get("boundary_conditions")
                normalized["boundary_conditions"] = (
                    list(existing_conditions)
                    if isinstance(existing_conditions, list)
                    else []
                ) + structured_boundaries
            for field_name in (
                "evidence_threads",
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
                    normalized[field_name] = [
                        dict(row) for row in values if isinstance(row, Mapping)
                    ]
                else:
                    normalized[field_name] = []
            for field_name in (
                "evidence_threads",
                "source_contributions",
                "central_findings",
                "agreements",
                "positions",
                "contradictions",
                "boundary_conditions",
                "methodological_fault_lines",
                "related_clusters",
                "source_roles",
                "gap_hypotheses",
            ):
                for row in normalized[field_name]:
                    # A bare anchor ID is not enough to establish source or
                    # locator lineage. Preserve the surrounding paid response,
                    # but discard only malformed evidence references so the
                    # normal support validator can narrow, repair, or reject
                    # the item without accepting invented provenance.
                    for evidence_field in (
                        "evidence",
                        "supporting_evidence",
                        "current_evidence",
                        "target_evidence",
                    ):
                        raw_evidence = row.get(evidence_field)
                        if isinstance(raw_evidence, Mapping):
                            row[evidence_field] = [dict(raw_evidence)]
                        elif isinstance(raw_evidence, list):
                            row[evidence_field] = [
                                dict(reference)
                                for reference in raw_evidence
                                if isinstance(reference, Mapping)
                            ]
                        elif raw_evidence is not None:
                            row[evidence_field] = []
            allowed_contribution_kinds = {
                "direct_proposition_finding",
                "unique_cluster_relevant_finding",
                "boundary_evidence",
                "methodological_context",
                "conceptual_context",
                "bridge_evidence",
            }
            allowed_comparison_statuses = {
                "single_source",
                "supports_shared_pattern",
                "contrasts_with_shared_pattern",
                "context_only",
            }
            normalized_contributions: list[dict[str, Any]] = []
            for raw_contribution in normalized["source_contributions"]:
                contribution = dict(raw_contribution)
                # DeepSeek occasionally emits null for optional prose fields
                # and a single evidence object instead of a one-item list.
                # Normalize only these harmless transport-shape variations;
                # ClusterSourceContribution and downstream anchor resolution
                # remain strict about the retained evidence itself.
                for field_name in (
                    "contribution_id",
                    "source_id",
                    "evidence_thread_id",
                    "finding",
                    "technical_result",
                    "plain_english_meaning",
                    "relation_to_cluster_question",
                ):
                    contribution[field_name] = scalar_text(
                        contribution.get(field_name)
                    )
                contribution["related_proposition_ids"] = string_values(
                    contribution.get("related_proposition_ids")
                )
                raw_evidence = contribution.get("evidence")
                if isinstance(raw_evidence, Mapping):
                    contribution["evidence"] = [dict(raw_evidence)]
                elif raw_evidence is None:
                    contribution["evidence"] = []
                role = str(contribution.get("cluster_role") or "context").casefold()
                if role not in {"core", "context", "bridge"}:
                    role = "context"
                contribution["cluster_role"] = role
                kind_value = str(contribution.get("contribution_kind") or "").casefold()
                if kind_value not in allowed_contribution_kinds:
                    kind_value = (
                        "unique_cluster_relevant_finding"
                        if role == "core"
                        else "bridge_evidence"
                        if role == "bridge"
                        else "conceptual_context"
                    )
                contribution["contribution_kind"] = kind_value
                comparison_status = str(
                    contribution.get("comparison_status") or ""
                ).casefold()
                if comparison_status not in allowed_comparison_statuses:
                    comparison_status = (
                        "single_source" if role == "core" else "context_only"
                    )
                contribution["comparison_status"] = comparison_status
                origin = str(contribution.get("origin") or "reasoner").casefold()
                if origin not in {"reasoner", "deterministic_profile_fallback"}:
                    origin = "reasoner"
                contribution["origin"] = origin
                normalized_contributions.append(contribution)
            normalized["source_contributions"] = normalized_contributions
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
                raise ValueError(
                    "gap adjudication response must contain only gaps and rejected"
                )
            if not isinstance(payload.get("gaps"), list) or not isinstance(
                payload.get("rejected"), list
            ):
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
                row.pop("strict_adjudication", None)
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
                for field_name in (
                    "related_cluster_ids",
                    "originating_cluster_ids",
                    "merged_from_gap_ids",
                ):
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
                assessment["information_gain"] = quality_tier(
                    assessment.get("information_gain")
                )
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
                    if str(key)
                    in {
                        "path_type",
                        "question",
                        "evidence_needed",
                        "requirements",
                        "feasibility",
                        "limitations",
                    }
                }
                if resolution:
                    for field_name in (
                        "path_type",
                        "question",
                        "evidence_needed",
                        "feasibility",
                    ):
                        resolution[field_name] = scalar_text(resolution.get(field_name))
                    resolution["requirements"] = (
                        dict(resolution.get("requirements"))
                        if isinstance(resolution.get("requirements"), Mapping)
                        else {}
                    )
                    resolution["limitations"] = string_values(
                        resolution.get("limitations")
                    )
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
                row["anchors"] = (
                    [
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
                    ]
                    if isinstance(anchor_values, list)
                    else []
                )
                normalized_gaps.append(GapRationale.from_dict(row).to_dict())
            return {
                "gaps": normalized_gaps,
                "rejected": [dict(row) for row in rejected],
            }
        if kind == "debate_mapping":
            if set(payload) != {"assessments"} or not isinstance(
                payload.get("assessments"), list
            ):
                raise ValueError(
                    "debate mapping response must contain only an assessments list"
                )
            if any(not isinstance(row, Mapping) for row in payload["assessments"]):
                raise ValueError("debate assessments must be mappings")
            return {"assessments": [dict(row) for row in payload["assessments"]]}
    except ValueError as exc:
        raise ProviderError(
            f"invalid {kind.replace('_', ' ')} response: {exc}"
        ) from exc
    raise ProviderError(f"unsupported literature response kind: {kind}")


def _validate_relationship_response(
    payload: Mapping[str, Any], *, kind: str
) -> dict[str, Any]:
    """Normalize harmless wrappers while leaving row-level judgment local."""

    if kind == "shard_selection":
        values = payload.get("shard_ids", [])
        if not isinstance(values, list):
            raise ProviderError("relationship shard response must contain a shard_ids list")
        return {
            "shard_ids": [
                str(value).strip() for value in values if str(value).strip()
            ]
        }
    key = "candidates" if kind == "candidate_selection" else "decisions"
    values = payload.get(key)
    if values is None and kind == "relationship_adjudication":
        values = payload.get("relationships")
    if not isinstance(values, list):
        raise ProviderError(f"{kind.replace('_', ' ')} response must contain a {key} list")
    return {key: [dict(value) if isinstance(value, Mapping) else value for value in values]}


def _cluster_boundary_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in (
            "boundary",
            "condition",
            "scope",
            "description",
            "text",
            "label",
            "value",
        ):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _parse_required_mapping(
    value: Any, required_keys: Sequence[str], *, label: str
) -> Mapping[str, Any]:
    payload = _parse_json_object(value, label=label)
    missing = [key for key in required_keys if not str(payload.get(key, "")).strip()]
    if missing:
        raise ProviderError(f"{label} omitted required sections: {', '.join(missing)}")
    return {key: str(payload[key]).strip() for key in required_keys}


def _resolve_context_window(
    provider: str, model: str, configured: int | None
) -> tuple[int, str]:
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


def _read_openai_stream_response(
    response: Any, deadline: float, byte_limit: int
) -> dict[str, Any]:
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
            raise ProviderError(
                "provider response exceeded the configured output bound"
            )
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


def _read_json_response(
    response: Any, deadline: float, byte_limit: int
) -> dict[str, Any]:
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
            raise ProviderError(
                "provider response exceeded the configured output bound"
            )
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
        raise _RequestDeadlineExceeded(
            "provider request deadline exceeded while waiting to retry"
        ) from cause
    time.sleep(delay)
