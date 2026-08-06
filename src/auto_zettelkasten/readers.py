from __future__ import annotations

import base64
import email.utils
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

import yaml

from .fidelity import ANALYSIS_SECTION_KEYS, validate_atomic_replacements
from .files import require_loopback_http_url
from .models import (
    ClusterProposal,
    ClusterSynthesis,
    EvidenceAnchor,
    EvidenceProfile,
    GapRationale,
    LiteratureMapRequest,
    QuantitativeResult,
    SourceAnalysisBundle,
    SynthesisAssertion,
)


class ProviderError(RuntimeError):
    pass


class ProviderTransportError(ProviderError):
    """Retryable provider transport failure with stable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        transport_kind: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.transport_kind = transport_kind
        self.retryable = True
        self.retry_on_resume = True
        self.cause_type = type(cause).__name__ if cause is not None else ""
        self.errno = getattr(cause, "errno", None)


class ProviderEmptyResponse(ProviderError):
    """The provider completed without any visible answer content."""


class CloudPermissionError(ProviderError):
    pass


class _ProviderText(str):
    """Provider text with completion metadata preserved for failure checkpoints."""

    completion: Mapping[str, Any]

    def __new__(
        cls, value: str, completion: Mapping[str, Any] | None = None
    ) -> _ProviderText:
        instance = super().__new__(cls, value)
        instance.completion = dict(completion or {})
        return instance


_LITERATURE_COMPLETION: ContextVar[Mapping[str, Any]] = ContextVar(
    "auto_zettelkasten_literature_completion", default={}
)
_ACTIVE_RESPONSE_LOCK = threading.Lock()
_ACTIVE_RESPONSES: dict[int, Any] = {}


def cancel_active_provider_responses() -> int:
    """Interrupt active reads without contending on urllib's buffered lock."""

    with _ACTIVE_RESPONSE_LOCK:
        responses = list(_ACTIVE_RESPONSES.values())
    for response in responses:
        candidate = getattr(getattr(response, "fp", None), "raw", None)
        socket_value = getattr(candidate, "_sock", None)
        shutdown = getattr(socket_value, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            continue
        try:
            response.close()
        except Exception:
            pass
    return len(responses)


def current_literature_completion() -> dict[str, Any]:
    """Return completion metadata for this thread's latest literature call."""

    return dict(_LITERATURE_COMPLETION.get() or {})


def current_provider_completion() -> dict[str, Any]:
    """Return completion metadata for this thread's latest provider call."""

    return current_literature_completion()


def reset_literature_completion() -> None:
    _LITERATURE_COMPLETION.set({})


def reset_provider_completion() -> None:
    _LITERATURE_COMPLETION.set({})


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

DIRECT_READ_CONTEXT_FRACTION = 0.5
DEFAULT_PROMPT_RESERVE_TOKENS = 2_048
DEFAULT_MAX_OUTPUT_TOKENS = 6_000
DEFAULT_CHUNK_OUTPUT_TOKENS = 1_024
SOURCE_CHUNK_MAX_OUTPUT_TOKENS = 8_000
PROFILE_MAX_OUTPUT_TOKENS = 16_000
SOURCE_BUNDLE_MAX_OUTPUT_TOKENS = 64_000
SOURCE_BUNDLE_PROMPT_VERSION = "6"
SOURCE_BUNDLE_ENVELOPE_CONTRACT = "source-bundle-envelope-v2"
LITERATURE_MAX_OUTPUT_TOKENS = 8_000
CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS = 64_000
GAP_ADJUDICATION_MAX_OUTPUT_TOKENS = 32_000
RELATIONSHIP_ROUTING_MAX_OUTPUT_TOKENS = 8_000
LITERATURE_FAMILY_PLAN_MAX_OUTPUT_TOKENS = 64_000
RELATIONSHIP_CANDIDATE_MAX_OUTPUT_TOKENS = 32_000
RELATIONSHIP_MAX_OUTPUT_TOKENS = 128_000
CLUSTER_SYNTHESIS_MAX_OUTPUT_TOKENS = 128_000
ATOMIC_FIDELITY_MAX_OUTPUT_TOKENS = 8_000

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

_EVIDENCE_ANCHOR_TEXT_FIELDS = frozenset(
    {
        field_name
        for field_name in EvidenceAnchor.__dataclass_fields__
        if field_name
        not in {
            "conditions",
            "locators",
            "source_locators",
            "qualifiers",
            "planning_roles",
            "salience_priority",
            "support_envelope",
            "quantitative_result",
        }
    }
)
_QUANTITATIVE_RESULT_FIELDS = frozenset(QuantitativeResult.__dataclass_fields__)
_GENERATED_QUANTITATIVE_RESULT_FIELDS = frozenset(
    {"quantitative_result_id", "source_id", "evidence_anchor_id"}
)
_SALIENCE_LABELS = {
    "critical": 10,
    "high": 10,
    "primary": 10,
    "medium": 5,
    "moderate": 5,
    "low": 1,
    "minor": 1,
}

DEEPSEEK_V4_FLASH_PRICING = {
    "cache_hit_input_per_million": Decimal("0.0028"),
    "cache_miss_input_per_million": Decimal("0.14"),
    "output_per_million": Decimal("0.28"),
    "source": "https://api-docs.deepseek.com/quick_start/pricing",
    "effective_date": "2026-08-03",
}


def provider_attempt_cost_usd(
    provider: str,
    model: str,
    completion: Mapping[str, Any] | None,
) -> tuple[Decimal, str] | None:
    """Return DeepSeek cost from provider usage, or None for unknown pricing."""

    if provider.casefold() != "deepseek" or model != "deepseek-v4-flash":
        return None
    usage = dict((completion or {}).get("usage", {}) or {})
    if not usage:
        return Decimal("0"), "usage_unavailable"
    hit = Decimal(str(usage.get("prompt_cache_hit_tokens", 0) or 0))
    miss_value = usage.get("prompt_cache_miss_tokens")
    if miss_value is None:
        miss_value = max(
            0,
            int(usage.get("prompt_tokens", 0) or 0) - int(hit),
        )
    miss = Decimal(str(miss_value or 0))
    output = Decimal(
        str(
            usage.get("completion_tokens", usage.get("output_tokens", 0))
            or 0
        )
    )
    million = Decimal("1000000")
    return (
        (
            hit * DEEPSEEK_V4_FLASH_PRICING["cache_hit_input_per_million"]
            + miss * DEEPSEEK_V4_FLASH_PRICING["cache_miss_input_per_million"]
            + output * DEEPSEEK_V4_FLASH_PRICING["output_per_million"]
        )
        / million,
        "provider_reported_usage",
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
    connect_timeout: float
    request_deadline: float | None
    relationship_decision_contract = "relationship-decision-v8"

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
        supported_output_tokens = (
            384_000
            if (self.name, self.model) == ("deepseek", "deepseek-v4-flash")
            else self.max_output_tokens
        )
        capability = {
            "context_window_tokens": int(self.context_window_tokens or 0),
            "context_window_source": self.context_window_source,
            "direct_read_fraction": self.direct_read_fraction,
            "direct_input_token_budget": self.direct_input_token_budget,
            "max_output_tokens": self.max_output_tokens,
            "supported_output_tokens": supported_output_tokens,
            "chunk_output_tokens": self._chunk_token_cap,
            "request_deadline_seconds": self._request_deadline_seconds(),
            "connect_timeout_seconds": float(getattr(self, "connect_timeout", 60.0)),
            "supports_hierarchical_reading": True,
            "supports_family_card_reconciliation": True,
        }
        return {
            **capability,
            "capability_identity": hashlib.sha256(
                json.dumps(
                    {
                        "provider": self.name,
                        "model": self.model,
                        **{
                            key: value
                            for key, value in capability.items()
                            if key
                            not in {
                                "request_deadline_seconds",
                                "connect_timeout_seconds",
                            }
                        },
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
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

    def should_read_source_bundle_directly(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> bool:
        output_tokens = min(
            int(self.capabilities["supported_output_tokens"]),
            SOURCE_BUNDLE_MAX_OUTPUT_TOKENS,
        )
        return self._prompt_fits(
            _source_bundle_system_prompt(),
            _source_bundle_prompt(text, metadata, question),
            output_tokens,
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

    def read_source_bundle(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        """Read one source into a source-owned analysis bundle."""

        self._authorize_request()
        system_prompt = _source_bundle_system_prompt()
        user_prompt = _source_bundle_prompt(text, metadata, question)
        output_tokens = min(
            int(self.capabilities["supported_output_tokens"]),
            SOURCE_BUNDLE_MAX_OUTPUT_TOKENS,
        )
        self._ensure_prompt_fits(
            system_prompt, user_prompt, output_tokens, label="source analysis bundle"
        )
        raw = self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                self._request_deadline_seconds(),
                reasoning_effort="high",
            )
        try:
            return _parse_source_bundle_response(
                raw,
                label="source analysis bundle response",
                expected_identity=_source_bundle_expected_identity(metadata),
            )
        except Exception as exc:
            _preserve_provider_failure(exc, raw)
            raise

    def verify_atomic_claims(
        self,
        analysis: Mapping[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return bounded replacements for locally flagged atomic-note claims."""

        risks = [
            dict(row)
            for row in (context or {}).get("risks", []) or []
            if isinstance(row, Mapping) and str(row.get("risk_id") or "").strip()
        ]
        if not risks:
            return {"replacements": []}
        self._authorize_request()
        system_prompt = _atomic_fidelity_system_prompt()
        user_prompt = _atomic_fidelity_prompt(analysis, context or {}, risks)
        output_tokens = max(self.max_output_tokens, ATOMIC_FIDELITY_MAX_OUTPUT_TOKENS)
        deadline_seconds = self._request_deadline_seconds()
        self._ensure_prompt_fits(
            system_prompt,
            user_prompt,
            output_tokens,
            label="atomic fidelity verification",
        )
        payload = _parse_json_object(
            self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                deadline_seconds,
                reasoning_effort="high",
            ),
            label="atomic fidelity verification response",
        )
        try:
            replacements = validate_atomic_replacements(
                analysis,
                payload,
                allowed_risk_ids=[
                    str(row["risk_id"]) for row in risks
                ],
                discard_invalid=True,
            )
        except ValueError as exc:
            raise ProviderError(f"invalid atomic fidelity response: {exc}") from exc
        return {"replacements": replacements}

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
                output_tokens=RELATIONSHIP_ROUTING_MAX_OUTPUT_TOKENS,
                list_key="shard_ids",
            ),
            kind="shard_selection",
        )

    def literature_family_plan_fits(
        self,
        profiles: Sequence[Mapping[str, Any]],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._prompt_fits(
            _literature_family_plan_system_prompt(),
            _literature_prompt(
                profiles,
                request,
                context,
                instruction="Plan shared, overlapping literature families and bounded discovery jobs.",
            ),
            LITERATURE_FAMILY_PLAN_MAX_OUTPUT_TOKENS,
            context_fraction=0.8,
        )

    def plan_literature_families(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Plan shared relationship and cluster families from the complete lean index."""

        self._authorize_request()
        mode = str((context or {}).get("planning_mode") or "initial_global")
        instruction = (
            "Audit the primary family plan for omitted or underrepresented coherent "
            "families and return additions only."
            if mode == "coverage_completion"
            else "Plan shared, overlapping literature families and bounded discovery jobs."
        )
        return self._literature_json_call(
            _literature_family_plan_system_prompt(),
            _literature_prompt(
                profiles,
                request,
                context,
                instruction=instruction,
            ),
            label="literature family plan",
            reasoning_effort="high",
            output_tokens=LITERATURE_FAMILY_PLAN_MAX_OUTPUT_TOKENS,
        )

    def select_relationship_bridge_shards(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Select a bounded set of cross-literature shard pairs."""

        self._authorize_request()
        return _validate_relationship_response(
            self._literature_json_call(
                _relationship_bridge_shard_system_prompt(),
                _relationship_prompt(profiles, request, context),
                label="relationship bridge shard selection",
                reasoning_effort="high",
                output_tokens=RELATIONSHIP_ROUTING_MAX_OUTPUT_TOKENS,
                list_key="shard_pairs",
            ),
            kind="shard_pair_selection",
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
        raw_response = self._literature_json_call(
            _relationship_candidate_system_prompt(),
            _relationship_prompt(profiles, request, context),
            label="relationship candidate selection",
            reasoning_effort="high",
            output_tokens=RELATIONSHIP_CANDIDATE_MAX_OUTPUT_TOKENS,
            list_key="candidates",
        )
        try:
            return _validate_relationship_response(
                raw_response, kind="candidate_selection"
            )
        except ProviderError as exc:
            exc.raw_response = raw_response
            completion = current_literature_completion()
            if completion:
                exc.provider_completion = dict(completion)
            raise

    def adjudicate_relationships(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return one complete v8 decision for every immutable pair job."""

        self._authorize_request()
        raw_response = self._literature_json_call(
            _relationship_adjudication_system_prompt(),
            _relationship_prompt(profiles, request, context),
            label="relationship adjudication",
            reasoning_effort="max",
            output_tokens=RELATIONSHIP_MAX_OUTPUT_TOKENS,
            list_key="decisions",
        )
        try:
            return _validate_relationship_response(
                raw_response, kind="relationship_adjudication"
            )
        except ProviderError as exc:
            exc.raw_response = raw_response
            completion = current_literature_completion()
            if completion:
                exc.provider_completion = dict(completion)
            raise

    def verify_relationships(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Independently verify tentative relationship decisions."""

        self._authorize_request()
        raw_response = self._literature_json_call(
            _relationship_verification_system_prompt(),
            _relationship_prompt(profiles, request, context),
            label="relationship verification",
            reasoning_effort="max",
            output_tokens=RELATIONSHIP_MAX_OUTPUT_TOKENS,
            list_key="verifications",
        )
        try:
            return _validate_relationship_response(
                raw_response, kind="relationship_verification"
            )
        except ProviderError as exc:
            exc.raw_response = raw_response
            completion = current_literature_completion()
            if completion:
                exc.provider_completion = dict(completion)
            raise

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
                reasoning_effort="medium",
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

    def plan_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Plan the eligible collection in one evidence-referenced call."""

        self._authorize_request()
        settings = dict((context or {}).get("cluster_plan_settings", {}) or {})
        output_tokens = int(
            settings.get("output_tokens") or CLUSTER_PROPOSAL_MAX_OUTPUT_TOKENS
        )
        deadline_seconds = float(
            settings.get("deadline_seconds") or self._request_deadline_seconds()
        )
        mode = str((context or {}).get("cluster_plan_mode") or "collection")
        if mode == "bridge":
            instruction = (
                "The supplied records are compact, evidence-referenced local "
                "families. Identify only genuinely cross-family or "
                "cross-literature clusters that the local plans could not see. "
                "Members must still use the supplied underlying source IDs and "
                "must state why they belong. Do not repeat a local family "
                "as a new cluster. Neighbor relationships may refer to supplied "
                "existing_family_ids as well as new cluster IDs. Before "
                "returning, self-check membership and direction."
            )
        elif mode == "shard":
            instruction = (
                "Create a complete local cluster plan for this bounded catalogue "
                "packet. It is acceptable for sources to remain unclustered in this "
                "map revision; do not force weak memberships. Before returning, check member relevance and "
                "neighbor direction in the same call."
            )
        elif (context or {}).get("incremental_source_delta"):
            instruction = (
                "Update the prior active cluster families for the supplied "
                "changed-source delta. Preserve coherent unaffected families, "
                "place changed sources where the evidence warrants, and revise "
                "only memberships or neighboring relationships that the new "
                "source cards change. A changed source may remain unclustered. "
                "Before returning, check member relevance in the same call."
            )
        else:
            instruction = (
                "Create one complete cluster plan for the supplied eligible "
                "corpus. Find mixed-literature debates as well as coherent "
                "within-literature families. Before returning, check membership, "
                "direction and "
                "neighboring relationships in the same call."
            )
        user_prompt = _literature_prompt(
            profiles,
            request,
            context,
            instruction=instruction,
        )
        return _validate_literature_response(
            self._literature_json_call(
                _cluster_plan_system_prompt(),
                user_prompt,
                label="global cluster plan",
                reasoning_effort="max",
                output_tokens=output_tokens,
                deadline_seconds=deadline_seconds,
            ),
            kind="cluster_plan",
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
        instruction = (
            "Read every complete atomic note in the packet and write the most useful "
            "source-specific synthesis of the proposed cluster. Use the depth needed "
            "by the literature, without decorative sections or generic summaries."
        )
        user_prompt = _literature_prompt(
            profiles,
            request,
            context,
            instruction=instruction,
        )
        supported_output = int(self.capabilities["supported_output_tokens"])
        desired_output = min(CLUSTER_SYNTHESIS_MAX_OUTPUT_TOKENS, supported_output)
        raw_response = self._literature_json_call(
            system_prompt,
            user_prompt,
            label="cluster synthesis",
            reasoning_effort="max",
            output_tokens=desired_output,
            deadline_seconds=min(600.0, self._request_deadline_seconds()),
        )
        try:
            return _validate_literature_response(
                raw_response, kind="cluster_synthesis"
            )
        except ProviderError as exc:
            exc.raw_response = raw_response
            completion = current_literature_completion()
            if completion:
                exc.provider_completion = dict(completion)
            raise

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
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        self.last_literature_response = None
        self.last_literature_completion = {}
        _LITERATURE_COMPLETION.set({})
        output_tokens = int(
            output_tokens
            if output_tokens is not None
            else max(self.max_output_tokens, LITERATURE_MAX_OUTPUT_TOKENS)
        )
        supported_output = int(self.capabilities["supported_output_tokens"])
        if output_tokens > supported_output:
            raise ProviderError(
                f"{label} requests {output_tokens} output tokens but the configured "
                f"model supports {supported_output}"
            )
        deadline_seconds = min(
            float(deadline_seconds or self._request_deadline_seconds()),
            self._request_deadline_seconds(),
        )
        self._ensure_prompt_fits(
            system_prompt,
            user_prompt,
            output_tokens,
            label=label,
            context_fraction=0.8,
        )
        raw = self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                deadline_seconds,
                reasoning_effort=reasoning_effort,
            )
        try:
            response = _parse_json_object(
                raw,
                label=f"{label} response",
                list_key=list_key,
            )
        except Exception as exc:
            _preserve_provider_failure(exc, raw)
            raise
        self.last_literature_completion = dict(
            getattr(raw, "completion", {}) or {}
        )
        _LITERATURE_COMPLETION.set(self.last_literature_completion)
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

    def synthesize_document_bundle(
        self,
        chunk_memos: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        max_output_tokens: int | None = None,
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        """Synthesize hierarchical evidence into the same canonical source bundle."""

        self._authorize_request()
        if not chunk_memos:
            raise ValueError("chunk_memos must not be empty")
        system_prompt = _source_bundle_system_prompt()
        user_prompt = _source_bundle_prompt(
            json.dumps(list(chunk_memos), ensure_ascii=False, default=str),
            metadata,
            question,
        )
        user_prompt = (
            "The inspected content below consists of ordered source-grounded chunk "
            "memos from one oversized document. Synthesize them as one source without "
            "inventing evidence absent from those memos.\n\n" + user_prompt
        )
        output_tokens = min(
            int(
                max_output_tokens
                if max_output_tokens is not None
                else SOURCE_BUNDLE_MAX_OUTPUT_TOKENS
            ),
            int(self.capabilities["supported_output_tokens"]),
            SOURCE_BUNDLE_MAX_OUTPUT_TOKENS,
        )
        if output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        request_deadline = self._bounded_deadline(deadline_seconds)
        self._ensure_prompt_fits(
            system_prompt,
            user_prompt,
            output_tokens,
            label="hierarchical source analysis bundle",
        )
        raw = self._generate_with_reasoning(
                system_prompt,
                user_prompt,
                output_tokens,
                request_deadline,
                reasoning_effort="high",
            )
        try:
            return _parse_source_bundle_response(
                raw,
                label="hierarchical source analysis bundle response",
                expected_identity=_source_bundle_expected_identity(metadata),
            )
        except Exception as exc:
            _preserve_provider_failure(exc, raw)
            raise

    def _generate_with_reasoning(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
        *,
        reasoning_effort: str,
    ) -> Any:
        if reasoning_effort not in {"medium", "high", "max"}:
            raise ValueError("reasoning_effort must be medium, high, or max")
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
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        *,
        context_fraction: float | None = None,
    ) -> bool:
        estimated_input = _estimate_tokens(system_prompt) + _estimate_tokens(
            user_prompt
        )
        reserve = self.prompt_reserve_tokens + output_tokens
        usable = int(
            int(self.context_window_tokens or 0)
            * (
                self.direct_read_fraction
                if context_fraction is None
                else context_fraction
            )
        )
        return estimated_input + reserve <= usable

    def _ensure_prompt_fits(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        *,
        label: str,
        context_fraction: float | None = None,
    ) -> None:
        if not self._prompt_fits(
            system_prompt,
            user_prompt,
            output_tokens,
            context_fraction=context_fraction,
        ):
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
    connect_timeout: float = 60.0
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
        _LITERATURE_COMPLETION.set({})
        try:
            payload = _post_json(
                self.endpoint,
                body,
                headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
                timeout=deadline_seconds,
                connect_timeout=self.connect_timeout,
                response_byte_limit=_stream_response_byte_limit(output_tokens),
                response_reader=_read_openai_stream_response,
            )
        except ProviderError as exc:
            completion = dict(getattr(exc, "provider_completion", {}) or {})
            completion.update(
                {
                    "provider": self.name,
                    "model": completion.get("model") or self.model,
                    "max_output_tokens": output_tokens,
                }
            )
            exc.provider_completion = completion
            raise
        try:
            choice = payload["choices"][0]
            finish_reason = str(choice.get("finish_reason") or "")
            content = str(choice["message"]["content"])
            completion = {
                "provider": self.name,
                "model": self.model,
                "finish_reason": finish_reason or "stop",
                "max_output_tokens": output_tokens,
                "response_id": str(payload.get("id") or ""),
                "usage": dict(payload.get("usage") or {}),
            }
            completion.update(dict(payload.get("_stream_diagnostics") or {}))
            _LITERATURE_COMPLETION.set(completion)
            if not content.strip():
                exc = ProviderEmptyResponse(
                    f"{self.name} returned an empty response"
                )
                _preserve_provider_failure(exc, _ProviderText(content, completion))
                raise exc
            if finish_reason and finish_reason != "stop":
                exc = ProviderError(
                    f"{self.name} response incomplete: finish_reason={finish_reason}"
                )
                _preserve_provider_failure(
                    exc, _ProviderText(content, completion)
                )
                raise exc
            return _ProviderText(content, completion)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} returned an unexpected response") from exc


class DeepSeekReader(_OpenAICompatibleReader):
    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        *,
        allow_cloud: bool = False,
        timeout: float = 180.0,
        connect_timeout: float = 60.0,
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
            connect_timeout=connect_timeout,
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
        connect_timeout: float = 60.0,
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
            connect_timeout=connect_timeout,
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
        )
        try:
            return payload["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("ollama returned an unexpected response") from exc


def _system_prompt() -> str:
    keys = ", ".join(SECTION_KEYS)
    return (
        "You create source-faithful atomic notes using Auto-Zettelkasten atomic prompt v11. "
        "Adapt the analysis to the source actually supplied: it may be an academic article or book, a report, policy or legal "
        "document, archival material, conference or meeting record, practitioner guidance, speech, working paper, blog post, "
        "or another evidence-bearing source. Do not force a nonacademic source into an academic-study template. "
        "Return only one JSON object. Do not infer facts absent from the source. "
        f"Every value must be a non-empty string. Required keys: {keys}. "
        "Always preserve source-reported numbers, their original scale, comparison, reference group, denominator, and uncertainty. "
        "A simple derived explanation is allowed only when every required input is explicit in the source; label it as derived, retain "
        "the original statistic beside it, and never invent a missing baseline, denominator, model quantity, or uncertainty measure. "
        "If the source's arithmetic appears inconsistent or the statistic does not support an intuitive conversion, report that "
        "limitation instead of forcing one. "
        "Identify the source's important findings, arguments, observations, interpretations, or recommendations and explain "
        "the method or knowledge basis behind them. Preserve the case or conflict, actors, population, geography, period, "
        "outcome, and comparison needed to understand each important point. Distinguish what the source observes or reports, "
        "the author's interpretation or argument, and what the method and evidence can actually establish. Use the organization and "
        "language best suited to the actual source; do not force fixed labels or an experimental template onto historical, qualitative, "
        "normative, practitioner, meeting, or other non-experimental material. "
        "Detailed findings must retain the source's exact estimates, units, denominators, sample sizes, uncertainty measures, "
        "technical labels, examples, and qualifications when present. Pair technical detail with a plain-English explanation; "
        "do not replace the figures with a simplification or leave them unexplained. "
        "For the two to four quantitative findings most important to understanding the source, explain for a statistically "
        "non-specialist what changed, by how much, compared with what, and what the reported uncertainty does and does not establish. "
        "Do this naturally rather than as a compulsory checklist. Distinguish percentage points from relative percentage change: "
        "a move from 40% to 31% is 9 percentage points lower and, when useful, 22.5% lower relative to the 40% baseline. Distinguish "
        "odds, hazards, risks, and probabilities. A logit coefficient is not a probability change, an odds ratio is not a probability "
        "ratio, and a hazard ratio is not cumulative risk. Do not convert a coefficient or interaction into a percentage unless the "
        "source reports a marginal effect or predicted probability, or supplies every quantity required for that derivation. A p-value "
        "is not an effect size or the probability that a hypothesis is true; statistical significance is not practical importance. "
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
        "descriptive arithmetic, not an estimated causal effect. Do not silently replace its denominators or invent additional deaths, "
        "rates, percentages, hypothetical populations, or examples absent from the source. "
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


def _source_bundle_system_prompt() -> str:
    keys = ", ".join(SECTION_KEYS)
    return (
        "You are the source-reading reasoner for Auto-Zettelkasten source bundle prompt v6. "
        "Capture the thesis, knowledge basis, important evidence, detailed findings, limitations, literature position, "
        "and distinct contribution. Include the consequential data, examples, historical analogies, mechanisms, nulls, "
        "counterexamples, and qualifications needed to evaluate the argument. Distinguish reported observations, modeled "
        "estimates, author interpretations, recommendations, and your explanation. Stay within the recovered-document scope. "
        "Adapt to the source form: quantitative work retains population, period, sample, unit, variables, baseline, "
        "comparison, estimates, uncertainty, interactions, robustness, and design limits; qualitative and comparative work "
        "retains case selection, evidence, chronology, mechanisms, decisive examples, alternatives, and generalization limits; "
        "historical work retains chronology, sources, analogies, inferences, and boundaries; theoretical or normative work "
        "retains assumptions, logical sequence, propositions, rivals, examples, and scope; institutional or practitioner work "
        "separates evidence and consultations from recommendations and implementation constraints; reviews retain the "
        "organizing debate, important cited positions, evidence bases, unresolved questions, and the author's contribution. "
        "For a book-like source, identify whether it is an authored monograph, edited volume, collected work, chapter or "
        "contribution, partial excerpt, or composition-uncertain. An authored monograph needs book-level analysis and a bounded "
        "chapter breakdown when headings are recoverable. An edited or collected volume needs the editors' framing plus chapter "
        "title, chapter author, thesis, evidence or method, and contribution for consequential recoverable chapters. Keep "
        "book-level and chapter-level claims distinct and never attribute a contributor's claim to an editor or the whole volume. "
        "Use associational wording for observational evidence unless the design and source justify causality. Attribute "
        "qualitative explanatory claims to the author. Never turn a recommendation into a demonstrated result. Preserve every "
        "important source-reported number on its original scale with its estimand, comparison, reference group, denominator, "
        "baseline, uncertainty, and observed range. Keep modeled and observed quantities distinct. A simple derivation is "
        "allowed only when all inputs are explicit; retain the source statistic and label the derivation system_derived. "
        "In plain English, explain the two to four most important findings rather than repeating the abstract. Keep the "
        "technical statistic beside the explanation. A move from 40% to 31% is 9 percentage points lower and, when useful, "
        "22.5% lower relative to the 40% baseline. Keep odds, hazards, risks, and probabilities distinct. Do not convert a "
        "logit coefficient or interaction into a percentage without a reported marginal effect, predicted probability, or "
        "all required inputs. A p-value is not an effect size or the probability that a hypothesis is true. Locators are "
        "approximate navigation aids and must not be invented. "
        f"Return exactly one JSON object for {SOURCE_BUNDLE_ENVELOPE_CONTRACT} with only these top-level fields: "
        "analysis_sections, compact_profile, evidence_anchors, literature_positions, and "
        "observed_bibliographic_identity. "
        f"analysis_sections is an object with readable Markdown strings for these keys: {keys}. "
        "Use a short 'Not applicable to this source form' string only when necessary. compact_profile is an object containing "
        "thesis, method_or_knowledge_basis, source_genre, inferential_design, and bounded arrays for mechanisms, outcomes, "
        "cases, populations, periods, and datasets. evidence_anchors is an array of no more than 24 consequential rows. Each "
        "row uses claim, locator, planning_roles, salience_priority, evidence_role, support_boundary, plain_english_meaning, "
        "uncertainty, and optional quantitative_result. evidence_role should be a short controlled description such as "
        "causal, associational, descriptive, mechanism_evidence, conceptual, methodological, normative, or "
        "practitioner_guidance. quantitative_result may use statistic, estimand_type, outcome_definition, estimate, unit, "
        "scale, baseline, reference_group, comparison_group, denominator, sample, uncertainty, population, period, model, "
        "and provenance. literature_positions contains approximately three to eight important substantively engaged works, "
        "not the whole bibliography. Each row uses raw_citation, author, year, title, identifiers, engagement, "
        "relation_label, and locator. observed_bibliographic_identity is a diagnostic object using title, creators, and date "
        "when visible in the source. Do not return stable IDs, source ownership, scope classification, support-envelope "
        "bookkeeping, library match status, missing-source recommendations, or a self-review object; the engine supplies or "
        "derives those fields locally. Before returning, silently self-review attribution, scope, conspicuous numbers, "
        "statistical scale, and causal wording inside this same call. This is not another model call or a separate note section."
    )


def _source_bundle_prompt(
    text: str,
    metadata: Mapping[str, Any],
    question: str | None,
) -> str:
    context = (
        dict(metadata.get("_source_context") or {})
        if isinstance(metadata.get("_source_context"), Mapping)
        else {}
    )
    stable_context = {
        key: context.get(key)
        for key in (
            "source_id",
            "zotero_key",
            "attachment_key",
            "source_scope",
            "page_count",
            "unresolved_pages",
            "recovered_pages",
            "recovered_page_ratio",
            "content_kind",
            "ordinal_to_printed_page",
            "heading_spans",
            "table_spans",
            "figure_spans",
        )
        if context.get(key) not in (None, "", [], {})
    }
    partial_rule = (
        "The content is incomplete. State the recovered and missing scope prominently, "
        "ground every claim in recovered content, and never claim to represent unseen sections."
        if stable_context.get("source_scope") == "partial_document"
        else "Treat the supplied recovered content according to its declared scope."
    )
    return (
        f"Stable source and extraction context: {json.dumps(stable_context, ensure_ascii=False)}\n"
        f"Question lens: {question or 'none'}\n"
        f"Scope rule: {partial_rule}\n\n"
        f"INSPECTED SOURCE CONTENT:\n{text}"
    )


def _atomic_fidelity_system_prompt() -> str:
    return (
        "You verify only locally flagged claims in an Auto-Zettelkasten atomic "
        "analysis. Return exactly one JSON object containing only a replacements "
        "array. Each replacement must contain exactly section_key, original, "
        "replacement, evidence_locator, and risk_ids. Use only the supplied section "
        "keys and risk IDs. Copy original exactly from the supplied analysis, and "
        "replace only the smallest unique sentence or clause needed to correct a "
        "locator, unsupported number, attribution, or causal overstatement. Preserve "
        "supported meaning and qualifiers. A locator correction must distinguish PDF "
        "ordinal pages from printed page labels when supplied. Never rewrite a whole "
        "note, add Markdown headings, introduce a number absent from the original or "
        "the evidence locator, infer missing text, or repair an unflagged passage. "
        "Return an empty replacements array when the supplied evidence does not "
        "support a safe correction."
    )


def _atomic_fidelity_prompt(
    analysis: Mapping[str, Any],
    context: Mapping[str, Any],
    risks: Sequence[Mapping[str, Any]],
) -> str:
    compact_analysis = {
        key: str(analysis.get(key) or "")
        for key in ANALYSIS_SECTION_KEYS
        if str(analysis.get(key) or "").strip()
    }
    compact_risks = [
        {
            key: row.get(key)
            for key in (
                "risk_id",
                "kind",
                "section_key",
                "claim",
                "locator",
                "reason",
                "details",
            )
            if row.get(key) not in (None, "", [], {})
        }
        for row in risks[:24]
    ]
    passages = []
    remaining_chars = 36_000
    for raw in (context.get("source_passages", []) or [])[:12]:
        if not isinstance(raw, Mapping) or remaining_chars <= 0:
            continue
        text = str(raw.get("text") or "")[: min(6_000, remaining_chars)]
        if not text.strip():
            continue
        passages.append(
            {
                "locator": str(raw.get("locator") or ""),
                "text": text,
            }
        )
        remaining_chars -= len(text)
    envelope = {
        "analysis": compact_analysis,
        "risks": compact_risks,
        "source_passages": passages,
        "source_scope": str(context.get("source_scope") or ""),
        "page_map": dict(context.get("page_map") or {})
        if isinstance(context.get("page_map"), Mapping)
        else {},
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


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
        "You route relationship discovery for Auto-Zettelkasten relationship prompt v2. "
        "Return exactly one JSON object with a shard_ids array containing only IDs from the supplied shard directory. "
        "Select the smallest set of literature shards needed to find genuinely relevant works for every focus source. "
        "Use the focus thesis, method, facets, existing graph neighbors, and cluster summaries. Include a neighboring "
        "literature when a source plausibly bridges fields. Do not classify relationships and do not infer that shared "
        "keywords, methods, cases, or collections make two works intellectually related."
    )


def _relationship_bridge_shard_system_prompt() -> str:
    return (
        "You route cross-literature bridge discovery for Auto-Zettelkasten relationship prompt v10. "
        "Return exactly one JSON object with a shard_pairs array of objects. Each object must contain "
        "left_shard_id, right_shard_id, bridge_family, why_examine, "
        "target_candidate_count, and confidence. Use only IDs from the supplied "
        "routing cards and put different IDs in each pair. Return every worthwhile shard pair that fits, using continuation when needed. Route "
        "plausible shared propositions, mechanisms, outcomes, sequences, debates, applications, and boundaries; "
        "this is recall-oriented navigation, not relationship publication. Do not classify source relationships "
        "or manufacture source pairs. Return an empty array only when no cross-literature comparison is worthwhile."
    )


def _relationship_candidate_system_prompt() -> str:
    return (
        "You retrieve comparisons for Auto-Zettelkasten relationship discovery prompt v16. "
        "Return exactly one JSON object with candidates and job_outcomes arrays. Each candidate "
        "contains only left_source_id, right_source_id, comparison_proposition, "
        "bridge_job_id, and rank. Use only supplied IDs, put the "
        "canonical lexicographically earlier ID on the left, and never repeat a "
        "pair. A candidate is a request for later full-note comparison, not a "
        "published relationship, so optimize recall, coverage, diversity, and "
        "useful navigation. Require a concrete shared proposition, mechanism, "
        "Respect max_inferred_pairs as the per-response page size, not as a total discovery cap. "
        "outcome, debate, sequence, implementation problem, or boundary; shared "
        "vocabulary alone is insufficient. Return as many useful pairs as fit "
        "comfortably in this response. When discovery_mode is bridge_only, return only pairs whose "
        "supplied collection memberships are disjoint. For bridge_only packets, "
        "each candidate must name a supplied bridge_job_id and place one endpoint "
        "on each of that job's source sides. Meet each job's target_candidate_count "
        "when useful. A source may appear in several genuinely useful comparisons. "
        "When discovery_mode is complementary_family_discovery, do not repeat "
        "prior_candidate_pairs and fulfill each supplied family job independently. "
        "For every supplied discovery job, return exactly one job_outcomes "
        "row for every supplied bridge_job_id, using status completed when more useful "
        "comparisons remain for a continuation or no_more_candidates only after exhausting that job's "
        "non-trivial comparisons. A candidate floor is a minimum coverage target, not "
        "a cap; return additional useful candidates when they are non-trivial. "
        "Cover multiple theoretical, mechanistic, empirical, "
        "institutional, implementation, outcome, sequence, and boundary families "
        "rather than stopping after citations or one theme. Full-note "
        "adjudication later decides whether and how the works relate."
    )


def _relationship_adjudication_system_prompt() -> str:
    return (
        "You adjudicate immutable relationship pair jobs for Auto-Zettelkasten "
        "relationship prompt v16 and contract relationship-decision-v8. Read both "
        "complete atomic notes. Return one JSON object whose decisions object is keyed "
        "by every supplied pair_job_id, with no missing or extra keys. Each value is "
        "either {decision:no_relationship, reason, confidence} or "
        "{decision:relationship, connections:[...]}. Return one connection normally "
        "and at most two only when the same pair has two genuinely distinct bounded "
        "propositions. Every connection contains comparison_proposition, "
        "primary_relation_type, secondary_relation_types, actor_source_id, "
        "reference_source_id, source_a_basis, source_b_basis, reason, "
        "boundary_or_qualification, and confidence. First describe each endpoint "
        "independently: source_a_basis must describe only the supplied left_source_id "
        "note and source_b_basis only the supplied right_source_id note. Preserve each "
        "source's constructs, entities and roles, level and unit of analysis, process "
        "position, outcome, scope, method, evidentiary status, and causal strength. "
        "Second, test whether both bases support one sufficiently specific proposition, "
        "question, mechanism, outcome, or explicit intellectual engagement without "
        "widening, recasting, or exchanging either source's meaning. Third, choose a "
        "direct relationship only when that comparison succeeds. Use "
        "contextual_connection when joint reading is useful but the connection requires "
        "an additional interpretive step; identify it explicitly as a joint-reading or "
        "system-level inference rather than either author's finding. Use no_relationship "
        "when overlap is only topical, lexical, or generic. Fourth, choose the narrowest "
        "defensible type. supports is directional: the actor supplies evidence or "
        "argument bearing on a sufficiently specific proposition advanced by the "
        "reference. undermines supplies materially incompatible evidence or argument; "
        "qualifies establishes a meaningful condition, exception, or boundary; extends "
        "requires explicit building on, applying, testing, refining, or generalizing; "
        "rival_explanation advances a competing explanation of the same explanandum; "
        "and sequential_relationship expresses an established intellectual or process "
        "sequence. complements is symmetric and is appropriate for independent "
        "compatible contributions to the same specific problem. contrasts, "
        "boundary_contrast, methodological_fault_line, "
        "interpretive_or_normative_disagreement, and contextual_connection are also "
        "symmetric. Directional types require the supplied endpoint IDs as actor and "
        "reference; symmetric types may return them as null because local code stores "
        "the canonical pair. Do not infer intellectual direction or support from pair "
        "order, chronology, citation, shared data, method, vocabulary, or broad outcome. "
        "A direct relationship requires a sufficiently specific shared proposition. When the works instead illuminate adjacent "
        "units, populations, settings, stages, mechanisms, outcomes, or evidentiary questions, prefer a contextual relationship "
        "and state the boundary precisely. Treat limited notes only within visible scope. Finally, self-review once: the "
        "proposition, type, actor, reference, endpoint bases, rationale, and boundary "
        "must preserve meaning, roles, scope, outcome, causal and evidentiary strength, "
        "source ownership, and any applicable direction. Never write display labels, "
        "Markdown, invented IDs, locators, provenance, or timestamps."
    )


def _relationship_verification_system_prompt() -> str:
    return (
        "You independently verify tentative source-relationship decisions for Auto-Zettelkasten relationship prompt v2. "
        "Return exactly one JSON object with a verifications array containing one row per supplied preliminary decision. "
        "Treat the preliminary decision as a claim to audit, not as evidence. Each row must contain source_id, "
        "target_source_id, status, relation_type, comparison_unit, reason, source_evidence_anchor_id, "
        "target_evidence_anchor_id, qualifiers, confidence, and requested_context. status is confirmed, corrected, "
        "no_relationship, or needs_more_context. Confirmed means the supplied anchors independently establish the same "
        "direction and relation type. Corrected means a substantive relationship exists but its direction, type, reason, "
        "or anchors must change. For confirmed and corrected rows, use one real supplied anchor from each source and one "
        "of: supports, undermines, qualifies, extends, complements, rival_explanation, boundary_contrast, "
        "methodological_fault_line, sequential_relationship, or interpretive_or_normative_disagreement. For other "
        "statuses use an empty relation_type and empty anchor IDs. Compare exact constructs, populations, outcomes, "
        "periods, methods, causal scope, and source attribution. Reject pooled evidence attributed to a subgroup, "
        "invented lineage, strawman qualification, and causal upgrades. Shared topic, case, method, dataset, citation, "
        "or vocabulary alone is no_relationship. Ask for more context only when the supplied profiles cannot resolve a "
        "consequential ambiguity. Never invent IDs, evidence, locators, provenance, timestamps, or Markdown."
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


def _literature_family_plan_system_prompt() -> str:
    return (
        "You are the shared literature-family planner for Auto-Zettelkasten "
        "cluster plan prompt v9. Read the supplied labeled source-index shard jobs. Return one "
        "JSON object with literature_families, discovery_jobs, neighboring_families, and "
        "source_dispositions arrays. A family has family_id, label, "
        "organizing_problem, source_ids, proposed_roles, and candidate_cluster. "
        "Roles are core, supporting, mechanism, boundary, practitioner, or partial. "
        "A discovery job has job_id, family, left_source_ids, right_source_ids, "
        "requested_collection_pair, discovery_goal, and candidate_quota. A "
        "neighbor row has left_family_id, right_family_id, and reason. Each source disposition has source_id, disposition "
        "(assigned, currently_unclustered, or overlap), family_ids, and a concise reason. Account for every source required "
        "by the supplied planning jobs. Use only "
        "supplied source and collection IDs. Families and memberships may overlap. "
        "Inspect the whole inventory and identify specific research problems, "
        "debates, mechanisms, outcomes, sequences, methods, cases, and practice "
        "questions; do not use shared words alone. Plan both within-literature and "
        "cross-literature discovery. Every explicitly requested collection pair "
        "must receive a direct discovery job, while other useful comparisons may be "
        "added. Divide discovery into distinct intellectual families with "
        "non-duplicative candidate targets. Candidate discovery should optimize recall; "
        "the later full-note call decides whether a relationship exists. Citation "
        "and literature-position records are routing signals, not evidence of "
        "agreement. When planning_mode is coverage_completion, preserve the supplied "
        "existing family cards and return only omitted or underrepresented coherent "
        "families and jobs; three empty arrays are valid when none are missing. When "
        "planning_mode is incremental_patch, return only affected "
        "or replacement families and discovery jobs, retain existing family IDs "
        "when their organizing problem still applies, and do not reshuffle "
        "unaffected families. Consider coherent within-collection, mixed-collection, bridge, and neighboring-but-distinct "
        "families; collection membership is routing provenance, not an intellectual boundary. Do not adjudicate relationships, summarize findings, write "
        "clusters, invent IDs, or force every source into a family."
    )


def _cluster_plan_system_prompt() -> str:
    return (
        "You are the global collection-clustering reasoner for Auto-Zettelkasten "
        "cluster plan prompt v5. Return exactly one JSON object containing "
        "clusters and neighbor_relationships. For backward compatibility you may "
        "also return an unclustered_sources array, but local code computes current "
        "non-membership and does not need reasons from you. Each cluster "
        "contains cluster_id, title, semantic_identity, organizing_mode, "
        "organizing_problem, optional guiding_question, optional central_tension, "
        "coherence_rationale, and members. organizing_mode is question, debate, "
        "mechanism, outcome, method, case, historical_problem, or practice_problem. "
        "Each member contains source_id and membership_reason; role and "
        "evidence_anchor_ids are optional descriptive routing metadata and never "
        "determine whether the member may contribute later. Use at least two members. "
        "A neighbor relationship contains left_cluster_id, "
        "right_cluster_id, relationship, basis_source_ids, and evidence_anchor_ids. "
        "Its source IDs must come from the supplied cards; anchor IDs are optional. "
        "Return one record per neighboring pair; local code "
        "projects both directions. Do not summarize every source again. Do not infer coherence "
        "from shared tags, methods, geography, or vocabulary alone. Preserve "
        "differences in construct, outcome, population, period, evidence type, and "
        "causal scope. A citation or verified relationship is a routing signal, not "
        "automatic agreement. Planning chooses membership and organization; it does "
        "not preselect the only evidence the cluster writer may read. Do not invent "
        "IDs, locators, sources, claims, debates, consensus, contradictions, or "
        "intellectual lineage. Produce a comprehensive map, not a showcase sample, "
        "but do not create weak clusters or memberships to maximize coverage. Any "
        "number of sources may remain unclustered in the current map and may fit a "
        "future cluster as the library grows. A source may belong to more than one "
        "genuinely relevant family. Do not stop "
        "after the first few families. Audit the complete source-ID inventory before "
        "returning, look explicitly for evidence-supported mixed-collection families, "
        "and preserve distinct major subliteratures rather than collapsing them into "
        "a few broad themes."
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
        "You are the full-note cluster writer for Auto-Zettelkasten cluster "
        "synthesis prompt v35 and contract streamlined-full-note-v2. Read every supplied atomic_note_markdown before "
        "drafting. Copy cluster_id exactly from context.cluster.cluster_id. Return "
        "exactly one JSON object with cluster_id, status, title, "
        "organizing_mode, organizing_problem, optional guiding_question, optional "
        "central_tension, bottom_line, lines_of_inquiry, differences, limits, "
        "related_clusters, retained_member_ids, member_roles, optional dropped_members, optional "
        "material_exclusions, acquisition_candidate_dispositions, optional "
        "split_proposals, and optional missing_member_ids. Status is accepted or "
        "rejected; it is the writer decision, not a copied planning or registry "
        "status. Each line of inquiry contains title, synthesis, and "
        "study_findings. Each study finding contains source_id, finding, "
        "method_scope, relation_to_line, and evidence, plus technical_result and "
        "plain_english_meaning only when it reports a technical statistic whose "
        "meaning is not already intuitive. Do not add a plain-English duplicate of "
        "ordinary prose or an already clear percentage. relation_to_line is supports, "
        "qualifies, contrasts, extends, applies, or contextualizes. Evidence uses "
        "an array of objects; every object copies source_id, evidence_anchor_id, "
        "and locator exactly from a supplied source-owned anchor. Every retained member must "
        "have at least one specific study finding. member_roles must map every retained source_id to core, context, or bridge: "
        "core directly answers the organizing problem; context supplies supporting, boundary, methodological, practitioner, "
        "or background evidence; bridge materially connects another literature or mechanism. Preserve a supplied role when it "
        "remains accurate, changing it only when the complete notes justify the correction. Drop a source rather than retain "
        "decorative context. Any member may contribute regardless of a prior core, "
        "context, or bridge label. A partial-document member may be retained when its "
        "recovered text supplies a specific theoretical, methodological, contextual, "
        "or boundary contribution. State that contribution and its available-content "
        "boundary, and do not present unavailable findings as empirical support. Do "
        "not remove a central work solely because its contribution is non-empirical. "
        "State what each study actually finds or argues, including the important data, "
        "examples, historical analogies, mechanisms, or recommendations appropriate to "
        "its method; state specific contributions, not generic thematic boilerplate. Preserve a source-reported "
        "technical statistic and its scale in technical_result. When interpretation is "
        "needed, explain its substantive meaning without merely restating it. Distinguish percentage points from "
        "relative percentage change; odds, hazards, risks, and probabilities; and "
        "coefficients from predicted probabilities or marginal effects. Do not turn "
        "an interaction coefficient into a percentage without the required reported "
        "quantities. A p-value is not an effect size or the probability a hypothesis "
        "is true, and statistical significance is not practical importance. If no "
        "intuitive conversion is defensible, explain the direction, comparison, and "
        "uncertainty without inventing one. Preserve null "
        "results, uncertainty, observational, descriptive, "
        "model-based, preliminary, normative, and practitioner limits. Explain "
        "support, qualification, disagreement, complementarity, and sequence only "
        "when the notes establish them. Keep the bottom line within the exact outcomes "
        "the retained studies examine; do not silently generalize onset, duration, "
        "violence, recovery, or settlement findings into recurrence findings. You may refine the title and organizing "
        "problem, reject an incoherent cluster, or propose splits, but cannot "
        "silently add a source whose full note was not supplied. In the same call, "
        "review attribution, direction, raw numbers, percentage-point versus relative "
        "percentage language, statistical scale, membership, and inferential scope "
        "before returning. Prefer named-source attribution for findings, disagreement, "
        "and boundaries. Use all, most, none, consensus, includes, or excludes only "
        "when the final retained-member findings establish the relevant numerator and "
        "denominator. Preserve each source's statistic, scale, comparison, denominator, "
        "and direction; do not create cross-study conversions or treat relative risk, "
        "odds, hazards, probabilities, and percentage points as interchangeable. "
        "Distinguish association, author argument, practitioner recommendation, and "
        "causal evidence. "
        "Use the supplied literature_positions to consider important mapped works as "
        "possible members, but retain them only when their complete note is supplied "
        "and makes a material cluster contribution. An important cited work without a "
        "mapped note is not independent evidence or a member. Return exactly one compact "
        "acquisition_candidate_dispositions row for every supplied "
        "important_unmapped_literature external_source_id. Use decision recommend, "
        "relevant_secondary, or not_relevant_to_cluster. recommend means a priority "
        "addition that would materially improve the cluster's central synthesis, evidence "
        "base, debate, or boundary. relevant_secondary means genuinely useful for "
        "understanding or expanding the cluster but not among the first works to map. "
        "not_relevant_to_cluster remains machine-only for this cluster. why_it_matters is "
        "required only for recommend, and selected_attribution_ids is optional. Do not generate a second "
        "independent recommendation list. Local code restores identity, citation, action "
        "status, and citing-source characterizations. "
        "Return material_exclusions only for intellectually important boundary cases; "
        "you need not explain every unretained candidate. "
        "Do not generate research gaps or administrative diagnostics."
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
            "policy": {
                key: value
                for key, value in getattr(
                    request, "literature_policy"
                ).to_dict().items()
                if key
                not in {
                    "literature_deadline_seconds",
                    "max_profile_calls",
                    "max_synthesis_calls",
                    "profile_workers",
                }
            },
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
            "comparison_collection_keys": list(
                getattr(request, "comparison_collection_keys", []) or []
            ),
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
    context = metadata.get("_source_context")
    partial_rule = ""
    if isinstance(context, Mapping) and context.get("source_scope") == "partial_document":
        partial_rule = (
            "\n\nPARTIAL-SOURCE RULE: The supplied text omits pages listed in the "
            "extraction metadata. Describe only the available pages, qualify every "
            "source-level argument or finding as available-content evidence, and do "
            "not infer the complete thesis, findings, method, or limitations of the "
            "missing document sections."
        )
    return (
        f"{_metadata_prompt(metadata, question)}{partial_rule}"
        f"\n\nINSPECTED SOURCE CONTENT:\n{text}"
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


def _preserve_provider_failure(exc: BaseException, raw: Any) -> None:
    """Attach private diagnostic material without changing provider exceptions."""

    if not hasattr(exc, "raw_response"):
        setattr(exc, "raw_response", str(raw))
    completion = getattr(raw, "completion", None)
    if completion and not hasattr(exc, "provider_completion"):
        setattr(exc, "provider_completion", dict(completion))


def _source_bundle_expected_identity(
    metadata: Mapping[str, Any],
) -> dict[str, str]:
    context = (
        metadata.get("_source_context", {})
        if isinstance(metadata.get("_source_context"), Mapping)
        else {}
    )
    return {
        key: str(context.get(key) or "")
        for key in ("source_id", "zotero_key", "attachment_key")
        if str(context.get(key) or "")
    }


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader for conservative recovery of JSON-shaped output."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed")
        return super().compose_node(parent, index)


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, yaml.nodes.MappingNode):
        raise yaml.YAMLError("expected a mapping")
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        if (
            key_node.tag == "tag:yaml.org,2002:merge"
            or getattr(key_node, "value", None) == "<<"
        ):
            raise yaml.YAMLError("merge keys are not allowed")
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.YAMLError("mapping keys must be hashable") from exc
        if duplicate:
            raise yaml.YAMLError(f"duplicate mapping key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _conservative_json_superset_mapping(text: str) -> dict[str, Any] | None:
    """Recover one JSON-like YAML mapping without accepting YAML graph features."""

    if re.search(r"(?m)^\s*%(?:YAML|TAG)\b", text):
        return None
    try:
        documents = list(yaml.load_all(text, Loader=_UniqueSafeLoader))
    except yaml.YAMLError:
        return None
    if len(documents) != 1 or not isinstance(documents[0], Mapping):
        return None
    return {str(key): value for key, value in documents[0].items()}


def _conservative_json_repair_mapping(text: str) -> dict[str, Any] | None:
    """Repair only lexical JSON defects without guessing structure or content."""

    field_pattern = re.compile(
        r'^(\s*"(?:\\.|[^"\\])*"\s*:\s*)(.*?)(,?)\s*$'
    )
    normalized_lines: list[str] = []
    for line in text.splitlines():
        match = field_pattern.match(line)
        if not match:
            normalized_lines.append(line)
            continue
        prefix, raw_value, comma = match.groups()
        value = raw_value.strip()
        if value and value[:1] not in {'"', "{", "["}:
            try:
                json.loads(value)
            except (json.JSONDecodeError, ValueError):
                if value.endswith('"'):
                    value = value[:-1].rstrip()
                value = json.dumps(value, ensure_ascii=False)
            line = f"{prefix}{value}{comma}"
        normalized_lines.append(line)
    text = "\n".join(normalized_lines)

    escaped: list[str] = []
    in_string = False
    backslashes = 0
    for index, char in enumerate(text):
        if char == "\\":
            escaped.append(char)
            backslashes += 1
            continue
        if char != '"' or backslashes % 2:
            escaped.append(char)
            backslashes = 0
            continue
        backslashes = 0
        if not in_string:
            in_string = True
            escaped.append(char)
            continue
        following = text[index + 1 :].lstrip()[:1]
        if not following or following in {":", ",", "}", "]"}:
            in_string = False
            escaped.append(char)
        else:
            escaped.append('\\"')
    if in_string:
        return None

    def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            "".join(escaped),
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, ValueError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _provider_source_bundle_payload(
    payload: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, str],
    local_recovery_reason: str = "",
) -> dict[str, Any]:
    """Normalize the lean provider envelope into the existing internal bundle."""

    normalized = dict(payload)
    identity = (
        dict(normalized.get("source_identity") or {})
        if isinstance(normalized.get("source_identity"), Mapping)
        else {}
    )
    for key, expected in expected_identity.items():
        returned = str(identity.get(key) or "")
        if returned and returned.casefold() != expected.casefold():
            raise ProviderError(f"source_identity.{key} does not match requested source")
    identity.update(expected_identity)
    normalized["bundle_schema_version"] = "1"
    normalized["source_identity"] = identity
    for field_name in (
        "observed_bibliographic_identity",
        "scope_assessment",
        "compact_profile",
    ):
        if not isinstance(normalized.get(field_name), Mapping):
            normalized[field_name] = {}
    normalized["self_review"] = {}
    normalized["missing_source_recommendations"] = []

    source_id = str(expected_identity.get("source_id") or "")
    anchors: list[Any] = []
    for value in normalized.get("evidence_anchors", []) or []:
        if not isinstance(value, Mapping):
            anchors.append(value)
            continue
        row = _normalize_provider_evidence_anchor(
            value,
            expected_source_id=source_id,
            discard_generated_ids=True,
        )
        role = str(row.get("evidence_role") or "").strip().casefold()
        row["support_envelope"] = {
            "empirical_role": (
                "causal"
                if role == "causal"
                else "associational"
                if any(token in role for token in ("associat", "regression"))
                else "descriptive"
                if any(token in role for token in ("descript", "quantitative"))
                else "mechanism_evidence"
                if "mechanism" in role
                else "none"
            ),
            "argument_role": (
                "methodological"
                if "method" in role
                else "normative"
                if "normative" in role
                else "practitioner_guidance"
                if any(token in role for token in ("pract", "guidance"))
                else "conceptual"
                if any(token in role for token in ("concept", "theor", "argument"))
                else "none"
            ),
            "coverage": "unknown",
            "restrictions": (
                [str(row.get("support_boundary"))]
                if str(row.get("support_boundary") or "").strip()
                else []
            ),
            "support_status": "supported",
        }
        anchors.append(row)
    normalized["evidence_anchors"] = anchors

    positions: list[Any] = []
    for value in normalized.get("literature_positions", []) or []:
        if not isinstance(value, Mapping):
            positions.append(value)
            continue
        row = dict(value)
        row.pop("literature_position_id", None)
        row["current_source_id"] = source_id
        if row.get("year") is not None:
            row["year"] = str(row["year"])
        identifiers = row.get("identifiers")
        if isinstance(identifiers, Mapping):
            row["identifiers"] = {
                str(key): str(item)
                for key, item in identifiers.items()
                if item not in (None, "")
            }
        elif str(identifiers or "").strip():
            row["identifiers"] = {"other": str(identifiers).strip()}
        else:
            row["identifiers"] = {}
        row.setdefault("author", "")
        row.setdefault("title", "")
        row.setdefault("relation_label", "")
        row.setdefault("locator", "")
        row.setdefault("matched_source_id", "")
        row.setdefault("provenance", "explicit")
        positions.append(row)
    normalized["literature_positions"] = positions

    diagnostics = [
        dict(row)
        for row in normalized.get("component_diagnostics", []) or []
        if isinstance(row, Mapping)
    ]
    if local_recovery_reason:
        diagnostics.append(
            {
                "component": "provider_envelope",
                "reason": local_recovery_reason,
                "severity": "advisory",
                "contract": SOURCE_BUNDLE_ENVELOPE_CONTRACT,
            }
        )
    normalized["component_diagnostics"] = diagnostics
    return normalized


def _normalize_provider_evidence_anchor(
    value: Mapping[str, Any],
    *,
    expected_source_id: str,
    discard_generated_ids: bool = False,
) -> dict[str, Any]:
    """Coerce mechanical provider shapes without changing evidence meaning."""

    row = dict(value)
    returned_owner = str(row.get("source_id") or "")
    if returned_owner and returned_owner != expected_source_id:
        raise ValueError(
            "evidence_anchors.source_id does not match requested source"
        )
    row["source_id"] = expected_source_id
    if discard_generated_ids:
        row.pop("evidence_anchor_id", None)
        row.pop("finding_id", None)
        row.pop("claim_id", None)

    for field_name in _EVIDENCE_ANCHOR_TEXT_FIELDS:
        if field_name not in row:
            continue
        field_value = row[field_name]
        if field_value is None:
            row[field_name] = ""
        elif isinstance(field_value, (str, int, float, bool)):
            row[field_name] = str(field_value)

    for field_name in ("conditions", "locators", "qualifiers", "planning_roles"):
        field_value = row.get(field_name)
        if field_value is None:
            row[field_name] = []
        elif not isinstance(field_value, (Mapping, list, tuple, set)):
            row[field_name] = [str(field_value)]

    salience = row.get("salience_priority", 0)
    if isinstance(salience, bool):
        row["salience_priority"] = 0
    elif isinstance(salience, (int, float)):
        row["salience_priority"] = max(0, int(salience))
    elif isinstance(salience, str):
        normalized_salience = salience.strip().casefold()
        try:
            row["salience_priority"] = max(0, int(float(normalized_salience)))
        except ValueError:
            row["salience_priority"] = _SALIENCE_LABELS.get(
                normalized_salience, 0
            )
    else:
        row["salience_priority"] = 0

    quantitative = row.get("quantitative_result")
    if isinstance(quantitative, Mapping):
        normalized_quantitative: dict[str, str] = {}
        for key, field_value in quantitative.items():
            field_name = str(key)
            if (
                field_name not in _QUANTITATIVE_RESULT_FIELDS
                or field_name in _GENERATED_QUANTITATIVE_RESULT_FIELDS
                or isinstance(field_value, (Mapping, list, tuple, set))
            ):
                continue
            normalized_quantitative[field_name] = (
                "" if field_value is None else str(field_value)
            )
        if normalized_quantitative:
            if normalized_quantitative.get("provenance") not in {
                "source_reported",
                "system_derived",
                "unknown",
            }:
                normalized_quantitative["provenance"] = "source_reported"
            row["quantitative_result"] = normalized_quantitative
        else:
            row.pop("quantitative_result", None)
    elif quantitative not in (None, ""):
        row.pop("quantitative_result", None)
    return row


def _parse_source_bundle_response(
    value: Any,
    *,
    label: str,
    expected_identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Recover exactly one complete, source-owned bundle from a response."""

    candidates: list[Mapping[str, Any]] = []
    recovered_candidate_reasons: dict[int, str] = {}
    if isinstance(value, Mapping):
        candidates.append(value)
    elif isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], Mapping):
            candidates.append(value[0])
    else:
        text = str(value).strip()
        fenced = re.match(
            r"^```(?:json)?\s*(.*?)\s*```$",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            text = fenced.group(1)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            index = 0
            while (start := text.find("{", index)) >= 0:
                try:
                    candidate, end = decoder.raw_decode(text, start)
                except json.JSONDecodeError:
                    index = start + 1
                    continue
                if isinstance(candidate, Mapping):
                    candidates.append(candidate)
                elif (
                    isinstance(candidate, list)
                    and len(candidate) == 1
                    and isinstance(candidate[0], Mapping)
                ):
                    candidates.append(candidate[0])
                index = max(start + 1, end)
            recovered = _conservative_json_superset_mapping(text)
            if recovered is not None:
                candidates.append(recovered)
                recovered_candidate_reasons[id(recovered)] = (
                    "conservative_json_superset_recovery"
                )
            repaired = _conservative_json_repair_mapping(text)
            if repaired is not None:
                candidates.append(repaired)
                recovered_candidate_reasons[id(repaired)] = (
                    "conservative_json_lexical_recovery"
                )
        else:
            if isinstance(payload, Mapping):
                candidates.append(payload)
            elif (
                isinstance(payload, list)
                and len(payload) == 1
                and isinstance(payload[0], Mapping)
            ):
                candidates.append(payload[0])

    expected = {
        str(key): str(item)
        for key, item in (expected_identity or {}).items()
        if str(item)
    }
    valid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if set(candidate) == {SOURCE_BUNDLE_ENVELOPE_CONTRACT} and isinstance(
            candidate.get(SOURCE_BUNDLE_ENVELOPE_CONTRACT), Mapping
        ):
            candidate = candidate[SOURCE_BUNDLE_ENVELOPE_CONTRACT]
        try:
            provider_payload = _provider_source_bundle_payload(
                candidate,
                expected_identity=expected,
                local_recovery_reason=recovered_candidate_reasons.get(
                    id(candidate), ""
                ),
            )
            normalized = _normalize_source_bundle_payload(provider_payload)
            bundle = SourceAnalysisBundle.from_dict(normalized).to_dict()
            identity = dict(bundle.get("source_identity") or {})
            if any(
                str(identity.get(key) or "")
                and str(identity.get(key) or "").casefold()
                != expected_value.casefold()
                for key, expected_value in expected.items()
            ):
                continue
            expected_source_id = expected.get("source_id", "")
            if expected_source_id and any(
                str(row.get(owner_field) or "")
                and str(row.get(owner_field) or "") != expected_source_id
                for field_name, owner_field in (
                    ("evidence_anchors", "source_id"),
                    ("literature_positions", "current_source_id"),
                )
                for row in bundle.get(field_name, []) or []
                if isinstance(row, Mapping)
            ):
                continue
        except (TypeError, ValueError, ProviderError):
            continue
        identity_key = json.dumps(
            _recovered_bundle_identity(bundle),
            sort_keys=True,
            ensure_ascii=False,
        )
        if identity_key not in seen:
            seen.add(identity_key)
            valid.append(bundle)
    if len(valid) == 1:
        return valid[0]
    if len(valid) > 1:
        dominant = [
            candidate
            for candidate in valid
            if all(
                candidate == other
                or _recovered_bundle_dominates(candidate, other)
                for other in valid
            )
        ]
        if len(dominant) == 1:
            return dominant[0]
        raise ProviderError(f"{label} contained multiple valid source bundles")
    raise ProviderError(f"{label} contained no complete source-owned bundle")


def _recovered_bundle_dominates(left: Any, right: Any) -> bool:
    """Prefer a recovery that only completes otherwise identical text fields."""

    generated = {
        "component_diagnostics",
        "evidence_anchor_id",
        "literature_position_id",
        "quantitative_result_id",
        "revision_hash",
    }
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_values = {key: value for key, value in left.items() if key not in generated}
        right_values = {
            key: value for key, value in right.items() if key not in generated
        }
        return left_values.keys() == right_values.keys() and all(
            left_values[key] == right_values[key]
            or _recovered_bundle_dominates(left_values[key], right_values[key])
            for key in left_values
        ) and left_values != right_values
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            left_value == right_value
            or _recovered_bundle_dominates(left_value, right_value)
            for left_value, right_value in zip(left, right, strict=True)
        ) and left != right
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and len(left) > len(right)
        and left.startswith(right)
    )


def _recovered_bundle_identity(value: Any) -> Any:
    """Remove parser-generated provenance when comparing recovered bundles."""

    ignored = {
        "component_diagnostics",
        "evidence_anchor_id",
        "literature_position_id",
        "quantitative_result_id",
        "revision_hash",
    }
    if isinstance(value, Mapping):
        return {
            key: _recovered_bundle_identity(item)
            for key, item in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [_recovered_bundle_identity(item) for item in value]
    return value


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
        # Recover a common otherwise-valid model response where JavaScript-style
        # call punctuation appears between a completed string value and JSON
        # delimiter. The repaired object still passes the normal stage contract.
        repaired_text = re.sub(r'(?<=")\s*\);(?=\s*[,}])', "", text)
        if repaired_text != text:
            try:
                payload = json.loads(repaired_text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return payload
        # OpenAI-compatible reasoning providers occasionally wrap an otherwise
        # valid JSON object in a short sentence even when JSON-only output was
        # requested. Recover exactly one embedded object, but keep rejecting
        # ambiguous responses containing multiple JSON objects.
        decoder = json.JSONDecoder()
        recovered: list[dict[str, Any]] = []
        index = 0
        while (start := text.find("{", index)) >= 0:
            try:
                candidate, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                index = start + 1
                continue
            if isinstance(candidate, dict):
                recovered.append(candidate)
            index = max(start + 1, end)
        if len(recovered) != 1:
            raise ProviderError(f"{label} was not valid JSON") from exc
        payload = recovered[0]
    if isinstance(payload, list):
        if list_key:
            return {list_key: payload}
        if len(payload) == 1 and isinstance(payload[0], Mapping):
            return dict(payload[0])
    if not isinstance(payload, dict):
        raise ProviderError(f"{label} must be a JSON object")
    return payload


def _normalize_source_bundle_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep complete one-shot analyses usable when presentation fields are omitted."""

    normalized = dict(payload)
    sections = normalized.get("analysis_sections")
    if not isinstance(sections, Mapping):
        raise ProviderError(
            "source analysis bundle response omitted usable analysis_sections"
        )
    sections = {
        str(key): _source_bundle_text(value)
        for key, value in sections.items()
        if _source_bundle_text(value)
    }
    profile = (
        dict(normalized.get("compact_profile") or {})
        if isinstance(normalized.get("compact_profile"), Mapping)
        else {}
    )
    anchors = [
        row
        for row in normalized.get("evidence_anchors", []) or []
        if isinstance(row, Mapping)
    ]
    has_core = {
        "thesis": bool(sections.get("thesis") or profile.get("thesis")),
        "method_and_research_design": bool(
            sections.get("method_and_research_design")
            or profile.get("method_or_knowledge_basis")
            or profile.get("method")
        ),
        "evidence_and_data": bool(sections.get("evidence_and_data") or anchors),
        "detailed_findings": bool(
            sections.get("detailed_findings")
            or any(
                "major_finding" in (row.get("planning_roles") or [])
                or str(row.get("claim") or "").strip()
                for row in anchors
            )
        ),
    }
    missing_core = [key for key, present in has_core.items() if not present]
    if missing_core:
        raise ProviderError(
            "source analysis bundle response omitted core content: "
            + ", ".join(missing_core)
        )
    missing_sections = [key for key in SECTION_KEYS if not sections.get(key)]
    for key in missing_sections:
        sections[key] = "Not separately returned by the source reader."
    normalized["analysis_sections"] = sections
    if missing_sections:
        diagnostics = [
            dict(row)
            for row in normalized.get("component_diagnostics", []) or []
            if isinstance(row, Mapping)
        ]
        diagnostics.extend(
            {
                "component": "analysis_sections",
                "field": key,
                "reason": "noncritical_section_not_separately_returned",
                "severity": "advisory",
            }
            for key in missing_sections
        )
        normalized["component_diagnostics"] = diagnostics
    return normalized


def _source_bundle_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return "\n".join(
            f"- {_source_bundle_text(item)}"
            for item in value
            if _source_bundle_text(item)
        )
    if isinstance(value, Mapping):
        return "\n".join(
            f"- {key}: {_source_bundle_text(item)}"
            for key, item in value.items()
            if _source_bundle_text(item)
        )
    return str(value).strip() if value is not None else ""


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
        if kind == "cluster_plan":
            clusters = payload.get("clusters")
            neighbors = payload.get("neighbor_relationships", [])
            unclustered = payload.get("unclustered_sources", [])
            if not isinstance(clusters, list):
                raise ValueError("cluster plan must contain a clusters list")
            if not isinstance(neighbors, list) or not isinstance(unclustered, list):
                raise ValueError(
                    "cluster plan neighbors and unclustered sources must be lists"
                )
            valid_clusters: list[dict[str, Any]] = []
            parked_clusters: list[dict[str, Any]] = [
                dict(row)
                for row in payload.get("parked_clusters", []) or []
                if isinstance(row, Mapping)
            ]
            for index, raw_cluster in enumerate(clusters):
                if not isinstance(raw_cluster, Mapping):
                    parked_clusters.append(
                        {
                            "cluster_id": f"row-{index}",
                            "reason": "cluster_row_not_mapping",
                        }
                    )
                    continue
                cluster = dict(raw_cluster)
                cluster_id = scalar_text(cluster.get("cluster_id"))
                title = scalar_text(cluster.get("title") or cluster.get("label"))
                organizing_mode = scalar_text(
                    cluster.get("organizing_mode") or "question"
                )
                organizing_problem = scalar_text(
                    cluster.get("organizing_problem")
                    or cluster.get("bounded_object")
                    or cluster.get("shared_question")
                )
                question = scalar_text(
                    cluster.get("guiding_question")
                    or cluster.get("shared_question")
                )
                members = cluster.get("members")
                valid_members = (
                    [
                        {
                            "source_id": scalar_text(member.get("source_id")),
                            "role": (
                                scalar_text(member.get("role")).casefold()
                                or "member"
                            ),
                            "evidence_anchor_ids": string_values(
                                member.get("evidence_anchor_ids")
                            ),
                            "membership_reason": scalar_text(
                                member.get("membership_reason")
                            ),
                        }
                        for member in members
                        if isinstance(member, Mapping)
                    ]
                    if isinstance(members, list)
                    else []
                )
                if (
                    not cluster_id
                    or not title
                    or not organizing_problem
                    or organizing_mode
                    not in {
                        "question",
                        "debate",
                        "mechanism",
                        "outcome",
                        "method",
                        "case",
                        "historical_problem",
                        "practice_problem",
                    }
                    or len(valid_members) != len(members or [])
                    or len(valid_members) < 2
                    or any(not row["source_id"] for row in valid_members)
                ):
                    parked_clusters.append(
                        {
                            "cluster_id": cluster_id or f"row-{index}",
                            "reason": "incomplete_cluster_plan_row",
                        }
                    )
                    continue
                valid_clusters.append(
                    {
                        "cluster_id": cluster_id,
                        "title": title,
                        "semantic_identity": scalar_text(
                            cluster.get("semantic_identity")
                        ),
                        "organizing_mode": organizing_mode,
                        "organizing_problem": organizing_problem,
                        "guiding_question": question,
                        "central_tension": scalar_text(
                            cluster.get("central_tension")
                        ),
                        "shared_question": question,
                        "bounded_object": organizing_problem,
                        "coherence_rationale": scalar_text(
                            cluster.get("coherence_rationale")
                        ),
                        "members": valid_members,
                    }
                )
            valid_neighbors = [
                {
                    "left_cluster_id": scalar_text(row.get("left_cluster_id")),
                    "right_cluster_id": scalar_text(row.get("right_cluster_id")),
                    "relationship": scalar_text(row.get("relationship")),
                    "basis_source_ids": string_values(row.get("basis_source_ids")),
                    "evidence_anchor_ids": string_values(
                        row.get("evidence_anchor_ids")
                    ),
                }
                for row in neighbors
                if isinstance(row, Mapping)
                and scalar_text(row.get("left_cluster_id"))
                and scalar_text(row.get("right_cluster_id"))
                and scalar_text(row.get("relationship"))
            ]
            valid_unclustered = [
                (
                    {
                        "source_id": scalar_text(row.get("source_id")),
                        "reason": scalar_text(row.get("reason")),
                    }
                    if isinstance(row, Mapping)
                    else {"source_id": scalar_text(row), "reason": ""}
                )
                for row in unclustered
                if (
                    scalar_text(row.get("source_id"))
                    if isinstance(row, Mapping)
                    else scalar_text(row)
                )
            ]
            return {
                "clusters": valid_clusters,
                "neighbor_relationships": valid_neighbors,
                "unclustered_sources": valid_unclustered,
                "parked_clusters": parked_clusters,
            }
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
            if "lines_of_inquiry" in payload or "bottom_line" in payload:
                return _validate_streamlined_cluster_response(payload)
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


def _validate_streamlined_cluster_response(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    organizing_mode = (
        str(payload.get("organizing_mode") or "question")
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )
    organizing_problem = str(
        payload.get("organizing_problem")
        or payload.get("guiding_question")
        or payload.get("central_tension")
        or payload.get("title")
        or ""
    ).strip()
    result = {
        "cluster_contract": "streamlined-full-note-v2",
        "cluster_id": str(payload.get("cluster_id") or "").strip(),
        "status": str(payload.get("status") or "accepted").strip().casefold(),
        "title": str(payload.get("title") or organizing_problem).strip(),
        "organizing_mode": organizing_mode,
        "organizing_problem": organizing_problem,
        "guiding_question": str(payload.get("guiding_question") or "").strip(),
        "central_tension": str(payload.get("central_tension") or "").strip(),
        "bottom_line": str(payload.get("bottom_line") or "").strip(),
    }
    if (
        not result["cluster_id"]
        or not result["title"]
        or not result["organizing_problem"]
        or result["status"] not in {"accepted", "rejected"}
    ):
        raise ProviderError(
            "invalid streamlined cluster response identity or organizing mode"
        )

    def mapping_rows(name: str) -> list[dict[str, Any]]:
        value = payload.get(name, [])
        if value is None or isinstance(value, str):
            return []
        if isinstance(value, Mapping):
            return [dict(value)]
        if not isinstance(value, list):
            raise ProviderError(
                f"streamlined cluster response {name} must be a list of objects"
            )
        return [dict(row) for row in value if isinstance(row, Mapping)]

    def narrative_rows(name: str, field: str) -> list[dict[str, Any]]:
        value = payload.get(name, [])
        if value is None or value == "":
            return []
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, str):
            return [{field: value.strip()}] if value.strip() else []
        if not isinstance(value, list):
            raise ProviderError(
                f"streamlined cluster response {name} must be a list"
            )
        return [
            dict(row)
            if isinstance(row, Mapping)
            else {field: str(row).strip()}
            for row in value
            if isinstance(row, Mapping) or str(row).strip()
        ]

    lines = mapping_rows("lines_of_inquiry")
    normalized_lines: list[dict[str, Any]] = []
    for line in lines:
        findings = line.get("study_findings", [])
        if not isinstance(findings, list) or any(
            not isinstance(row, Mapping) for row in findings
        ):
            raise ProviderError(
                "streamlined cluster study_findings must be a list of objects"
            )
        normalized_findings: list[dict[str, Any]] = []
        for row in findings:
            finding = dict(row)
            source_id = str(finding.get("source_id") or "").strip()
            evidence = finding.get("evidence", [])
            if evidence is None or evidence == "":
                evidence = []
            elif isinstance(evidence, (str, Mapping)):
                evidence = [evidence]
            if not isinstance(evidence, list):
                raise ProviderError(
                    "streamlined cluster finding evidence must be a list"
                )
            finding["evidence"] = [
                dict(reference)
                if isinstance(reference, Mapping)
                else {
                    "source_id": source_id,
                    "locator": str(reference).strip(),
                }
                for reference in evidence
                if isinstance(reference, Mapping) or str(reference).strip()
            ]
            normalized_findings.append(finding)
        normalized_lines.append(
            {
                "title": str(line.get("title") or "").strip(),
                "synthesis": str(line.get("synthesis") or "").strip(),
                "study_findings": normalized_findings,
            }
        )
    retained = payload.get("retained_member_ids", [])
    missing = payload.get("missing_member_ids", [])
    if retained is None:
        retained = []
    if missing is None:
        missing = []
    if not isinstance(retained, list) or not isinstance(missing, list):
        raise ProviderError(
            "streamlined cluster member IDs must be lists"
        )
    dropped_members = payload.get("dropped_members", [])
    if isinstance(dropped_members, list):
        dropped_members = [
            dict(value)
            if isinstance(value, Mapping)
            else {"source_id": str(value).strip()}
            for value in dropped_members
            if isinstance(value, Mapping) or str(value).strip()
        ]
    limits = payload.get("limits", [])
    if isinstance(limits, str):
        limits = [limits]
    elif limits is None:
        limits = []
    if not isinstance(limits, list):
        raise ProviderError("streamlined cluster response limits must be a list")
    member_roles = payload.get("member_roles", {})
    if isinstance(member_roles, Mapping):
        member_roles = {
            str(source_id).strip(): str(role).strip().casefold()
            for source_id, role in member_roles.items()
            if str(source_id).strip()
        }
    elif isinstance(member_roles, list):
        member_roles = [
            dict(row) for row in member_roles if isinstance(row, Mapping)
        ]
    return {
        **result,
        "lines_of_inquiry": normalized_lines,
        "differences": narrative_rows("differences", "difference"),
        "limits": [
            str(value).strip()
            for value in limits
            if str(value).strip()
        ],
        "related_clusters": mapping_rows("related_clusters"),
        "retained_member_ids": [
            str(value).strip() for value in retained if str(value).strip()
        ],
        "member_roles": member_roles,
        "dropped_members": (
            dropped_members
            if isinstance(dropped_members, list)
            else mapping_rows("dropped_members")
        ),
        "material_exclusions": mapping_rows("material_exclusions"),
        "acquisition_candidate_dispositions": mapping_rows(
            "acquisition_candidate_dispositions"
        ),
        # Legacy built-in responses remain inspectable during lazy migration.
        "important_cited_works_not_yet_mapped": mapping_rows(
            "important_cited_works_not_yet_mapped"
        ),
        "split_proposals": mapping_rows("split_proposals"),
        "missing_member_ids": [
            str(value).strip() for value in missing if str(value).strip()
        ],
    }


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
    if kind == "shard_pair_selection":
        values = payload.get("shard_pairs")
        if not isinstance(values, list):
            raise ProviderError(
                "relationship bridge shard response must contain a shard_pairs list"
            )
        return {
            "shard_pairs": [
                dict(value) if isinstance(value, Mapping) else value
                for value in values
            ]
        }
    key = (
        "candidates"
        if kind == "candidate_selection"
        else "verifications"
        if kind == "relationship_verification"
        else "decisions"
    )
    values = payload.get(key)
    if values is None and kind == "relationship_adjudication":
        values = payload.get("relationships")
        if (
            values is None
            and payload
            and all(
                str(job_id).startswith("relationship-job-")
                and isinstance(value, Mapping)
                for job_id, value in payload.items()
            )
        ):
            values = payload
        elif (
            values is None
            and payload
            and all(
                re.fullmatch(r"[0-9a-fA-F]{20}", str(job_id))
                and isinstance(value, Mapping)
                for job_id, value in payload.items()
            )
        ):
            values = {
                f"relationship-job-{job_id}": value
                for job_id, value in payload.items()
            }
    if values is None and kind == "relationship_verification":
        values = payload.get("decisions")
    if kind == "relationship_adjudication" and isinstance(values, Mapping):
        return {
            "decisions": [
                {**dict(value), "pair_job_id": str(job_id)}
                if isinstance(value, Mapping)
                else value
                for job_id, value in values.items()
            ]
        }
    if not isinstance(values, list):
        raise ProviderError(f"{kind.replace('_', ' ')} response must contain a {key} list")
    result = {
        key: [dict(value) if isinstance(value, Mapping) else value for value in values]
    }
    if kind == "candidate_selection" and "job_outcomes" in payload:
        outcomes = payload["job_outcomes"]
        if isinstance(outcomes, list):
            result["job_outcomes"] = [
                dict(value) if isinstance(value, Mapping) else value
                for value in outcomes
            ]
        else:
            result["job_outcomes"] = []
            result["accounting_warning"] = "invalid_job_outcomes_shape"
    return result


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


def _post_json(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    connect_timeout: float = 60.0,
    response_byte_limit: int = 2 * 1024 * 1024,
    max_attempts: int = 1,
    on_attempt: Callable[[], None] | None = None,
    response_reader: Callable[[Any, float, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if response_byte_limit <= 0:
        raise ValueError("response_byte_limit must be positive")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
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
    for attempt in range(max_attempts):
        if on_attempt is not None:
            on_attempt()
        try:
            response = urllib.request.urlopen(
                request, timeout=min(timeout, connect_timeout)
            )
            _set_response_read_timeout(response, timeout)
            with _ACTIVE_RESPONSE_LOCK:
                _ACTIVE_RESPONSES[id(response)] = response
            try:
                reader = response_reader or _read_json_response
                return reader(response, timeout, response_byte_limit)
            finally:
                with _ACTIVE_RESPONSE_LOCK:
                    _ACTIVE_RESPONSES.pop(id(response), None)
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_seconds(exc.headers)
            try:
                detail = exc.read(512).decode("utf-8", errors="replace")
            finally:
                exc.close()
            if attempt + 1 < max_attempts and (
                exc.code == 429 or 500 <= exc.code <= 599
            ):
                if retry_after > 0:
                    time.sleep(retry_after)
                continue
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise ProviderTransportError(
                    f"provider HTTP {exc.code}: {detail}",
                    transport_kind="http_retryable",
                    cause=exc,
                ) from exc
            raise ProviderError(f"provider HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt + 1 < max_attempts:
                continue
            if _is_network_timeout(exc.reason):
                raise ProviderTransportError(
                    "provider connection timed out",
                    transport_kind="connection_timeout",
                    cause=exc,
                ) from exc
            raise ProviderTransportError(
                f"provider unavailable: {exc.reason}",
                transport_kind="network_unavailable",
                cause=exc,
            ) from exc
        except http.client.HTTPException as exc:
            if attempt + 1 < max_attempts:
                continue
            raise ProviderTransportError(
                "provider connection interrupted",
                transport_kind="connection_interrupted",
                cause=exc,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt + 1 < max_attempts:
                continue
            raise ProviderTransportError(
                "provider connection timed out",
                transport_kind="connection_timeout",
                cause=exc,
            ) from exc
    raise AssertionError("provider attempt loop exhausted unexpectedly")


def _set_response_read_timeout(response: Any, timeout: float) -> None:
    """Switch urllib's connection timeout to the stream inactivity timeout."""

    candidate = getattr(getattr(response, "fp", None), "raw", None)
    socket_value = getattr(candidate, "_sock", None)
    setter = getattr(socket_value, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _read_openai_stream_response(
    response: Any, idle_timeout: float, byte_limit: int
) -> dict[str, Any]:
    """Read OpenAI-compatible SSE output with a progress-based idle timeout."""

    if not callable(getattr(response, "readline", None)):
        # Test doubles and a few compatible gateways may still return one JSON
        # object even when stream=true. Preserve that compatible fallback.
        return _read_json_response(response, idle_timeout, byte_limit)

    content: list[str] = []
    finish_reason = ""
    total = 0
    event_count = 0
    content_fragment_count = 0
    reasoning_fragment_count = 0
    response_id = ""
    model = ""
    usage: dict[str, Any] = {}
    stream_hash = hashlib.sha256()
    last_activity = time.monotonic()
    saw_terminal = False

    def diagnostics(visible_content: str) -> dict[str, Any]:
        return {
            "response_id": response_id,
            "usage": usage,
            "finish_reason": finish_reason,
            "model": model,
            "event_count": event_count,
            "response_bytes": total,
            "content_fragment_count": content_fragment_count,
            "content_characters": len(visible_content),
            "reasoning_fragment_count": reasoning_fragment_count,
            "stream_sha256": stream_hash.hexdigest(),
            "content_sha256": hashlib.sha256(
                visible_content.encode("utf-8")
            ).hexdigest(),
        }

    def preserve_stream_failure(error: ProviderError) -> ProviderError:
        visible_content = "".join(content)
        _preserve_provider_failure(
            error, _ProviderText(visible_content, diagnostics(visible_content))
        )
        return error

    while True:
        try:
            remaining = idle_timeout - (time.monotonic() - last_activity)
            if remaining <= 0:
                raise ProviderTransportError(
                    "provider stream idle timeout exceeded",
                    transport_kind="idle_timeout",
                )
            _set_stream_timeout(response, remaining)
            line = response.readline()
            if time.monotonic() - last_activity >= idle_timeout:
                raise ProviderTransportError(
                    "provider stream idle timeout exceeded",
                    transport_kind="idle_timeout",
                )
        except ProviderTransportError as exc:
            preserve_stream_failure(exc)
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise preserve_stream_failure(
                ProviderTransportError(
                    "provider stream idle timeout exceeded",
                    transport_kind="idle_timeout",
                    cause=exc,
                )
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise preserve_stream_failure(
                ProviderTransportError(
                    "provider stream read failed",
                    transport_kind="interrupted_stream",
                    cause=exc,
                )
            ) from exc
        if not line:
            break
        total += len(line)
        stream_hash.update(line)
        if total > byte_limit:
            raise preserve_stream_failure(
                ProviderError(
                    "provider response exceeded the configured output bound"
                )
            )
        try:
            decoded = line.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            error = preserve_stream_failure(
                ProviderError("provider stream contained invalid UTF-8")
            )
            error.provider_completion["malformed_event_sha256"] = hashlib.sha256(
                line
            ).hexdigest()
            raise error from exc
        if not decoded or decoded.startswith(":") or not decoded.startswith("data:"):
            continue
        data = decoded[5:].strip()
        if data == "[DONE]":
            saw_terminal = True
            last_activity = time.monotonic()
            break
        event_count += 1
        try:
            event = json.loads(data)
            if not isinstance(event, Mapping):
                raise TypeError("stream event must be an object")
            choices = event.get("choices")
            if choices == [] and isinstance(event.get("usage"), Mapping):
                if event.get("id"):
                    response_id = str(event["id"])
                if event.get("model"):
                    model = str(event["model"])
                usage = dict(event["usage"])
                last_activity = time.monotonic()
                continue
            choice = choices[0]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            error = ProviderError("provider stream contained an invalid event")
            visible_content = "".join(content)
            completion = diagnostics(visible_content)
            completion["malformed_event_sha256"] = hashlib.sha256(
                data.encode("utf-8")
            ).hexdigest()
            if "reasoning_content" not in data:
                completion["malformed_event_excerpt"] = data[:512]
            _preserve_provider_failure(
                error, _ProviderText(visible_content, completion)
            )
            raise error from exc
        if event.get("id"):
            response_id = str(event["id"])
        if event.get("model"):
            model = str(event["model"])
        if isinstance(event.get("usage"), Mapping):
            usage = dict(event["usage"])
        delta = choice.get("delta") or choice.get("message") or {}
        piece = delta.get("content") if isinstance(delta, Mapping) else None
        if isinstance(piece, str) and piece:
            content.append(piece)
            content_fragment_count += 1
            last_activity = time.monotonic()
        reasoning_piece = (
            delta.get("reasoning_content") if isinstance(delta, Mapping) else None
        )
        if isinstance(reasoning_piece, str) and reasoning_piece:
            reasoning_fragment_count += 1
            last_activity = time.monotonic()
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
            saw_terminal = True
            last_activity = time.monotonic()
    visible_content = "".join(content)
    stream_diagnostics = diagnostics(visible_content)
    if not saw_terminal:
        error = ProviderTransportError(
            "provider stream ended before a terminal event",
            transport_kind="premature_eof",
        )
        _preserve_provider_failure(
            error, _ProviderText(visible_content, stream_diagnostics)
        )
        raise error
    if not visible_content.strip():
        error = ProviderEmptyResponse("provider stream ended without response content")
        _preserve_provider_failure(
            error, _ProviderText(visible_content, stream_diagnostics)
        )
        raise error
    return {
        "id": response_id,
        "model": model,
        "usage": usage,
        "_stream_diagnostics": stream_diagnostics,
        "choices": [
            {
                "finish_reason": finish_reason or "stop",
                "message": {"content": visible_content},
            }
        ]
    }


def _read_json_response(
    response: Any, idle_timeout: float, byte_limit: int
) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read1", response.read)
    last_activity = time.monotonic()
    while True:
        remaining = idle_timeout - (time.monotonic() - last_activity)
        if remaining <= 0:
            raise ProviderTransportError(
                "provider response idle timeout exceeded",
                transport_kind="idle_timeout",
            )
        _set_stream_timeout(response, remaining)
        # HTTPResponse.read(size) may internally wait for `size` bytes while a
        # chunked provider trickles smaller frames, effectively renewing the
        # socket timeout inside one Python call. read1 returns after one
        # underlying read so the monotonic deadline is checked between frames.
        try:
            chunk = read(min(65_536, byte_limit + 1 - total))
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderTransportError(
                "provider response idle timeout exceeded",
                transport_kind="idle_timeout",
                cause=exc,
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderTransportError(
                "provider response read failed",
                transport_kind="interrupted_stream",
                cause=exc,
            ) from exc
        if time.monotonic() - last_activity >= idle_timeout:
            raise ProviderTransportError(
                "provider response idle timeout exceeded",
                transport_kind="idle_timeout",
            )
        if not chunk:
            break
        last_activity = time.monotonic()
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
