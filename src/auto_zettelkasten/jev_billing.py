"""Pinned standard-context Jev billing admission, without provider access.

The model-usage ceiling is conditional on the provider's published standard
context/pricing contract. It is not an unconditional invoice guarantee. Wire
bytes are bounded locally for operational reasons, not converted into tokens.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
import json
from typing import Any


SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
MODEL_ID = "jev-1.13.0"
TEMPLATE_VERSION = "relationship-decision-v3"
INPUT_USD_PER_TOKEN = Decimal("0.000000042")
# Covers either the decimal or binary reading of the documented 64k ceiling.
STANDARD_INPUT_TOKEN_CEILING = 65_536
# A separate local operational cap; no byte/token equivalence is asserted.
MAX_ADMISSION_WIRE_BYTES = 65_536
MAX_JSON_NESTING = 64


class BillingAdmissionError(ValueError):
    """The prepared request has no supported pinned billing admission policy."""


@dataclass(frozen=True)
class BillingPolicy:
    identity: str
    model: str
    template_version: str
    input_usd_per_token: Decimal
    input_token_ceiling: int
    max_wire_bytes: int
    verified_on: str
    sources: tuple[str, ...]


STANDARD_BILLING_POLICY = BillingPolicy(
    identity="typesafe-jev-1.13.0-standard-context-usd-20261022-v2",
    model=MODEL_ID,
    template_version=TEMPLATE_VERSION,
    input_usd_per_token=INPUT_USD_PER_TOKEN,
    input_token_ceiling=STANDARD_INPUT_TOKEN_CEILING,
    max_wire_bytes=MAX_ADMISSION_WIRE_BYTES,
    verified_on="2026-10-22",
    sources=(
        "https://docs.typesafe.ai/models",
        "https://docs.typesafe.ai/api/system-one",
        "https://docs.typesafe.ai/api/errors",
    ),
)

# A separate contract preserves historical single-pair admission and replay.
BATCH_TEMPLATE_VERSION = "relationship-batch-v2"
BATCH_BILLING_POLICY = replace(
    STANDARD_BILLING_POLICY,
    identity="typesafe-jev-1.13.0-standard-batch-usd-20261022-v2",
    template_version=BATCH_TEMPLATE_VERSION,
)


@dataclass(frozen=True)
class BillingAdmission:
    policy: BillingPolicy
    wire_bytes: int

    @property
    def max_model_usage_usd(self) -> Decimal:
        return Decimal(self.policy.input_token_ceiling) * self.policy.input_usd_per_token

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_identity": self.policy.identity,
            "model": self.policy.model,
            "template_version": self.policy.template_version,
            "currency": "USD",
            "input_usd_per_token": str(self.policy.input_usd_per_token),
            "input_token_ceiling": self.policy.input_token_ceiling,
            "wire_bytes": self.wire_bytes,
            "max_wire_bytes": self.policy.max_wire_bytes,
            "max_model_usage_usd": str(self.max_model_usage_usd),
            "bound_scope": "documented_model_usage",
            "guaranteed_invoice_cap": False,
            "tokenizer_identity": None,
            "wire_bytes_are_token_bound": False,
            "verified_on": self.policy.verified_on,
            "source_urls": list(self.policy.sources),
            "assumptions": [
                ("Pinned jev-1.13.0 standard text endpoint and at most three Choice questions."
                 if self.policy == BATCH_BILLING_POLICY else
                 "Pinned jev-1.13.0 standard text endpoint and one Choice question."),
                "No long-context or other danger-zone header, retries, or fallback model.",
                "Published input-only USD price and standard context contract remain applicable.",
            ],
            "error_billing": "Not explicitly documented; reserve the full ceiling after every dispatched error or unknown outcome.",
            "invoice_exclusions": ["taxes", "account-specific orders or charges", "provider contract violations"],
        }


def billing_admission(
    payload: Mapping[str, Any], *, policy: BillingPolicy | None = STANDARD_BILLING_POLICY,
    endpoint: str = SYSTEM_ONE_URL, headers: Mapping[str, str] | None = None,
    template_version: str = TEMPLATE_VERSION,
) -> BillingAdmission:
    """Validate a bounded request and return its full conditional reservation.

    This function does not approve spending or enable transport. The input-token
    ceiling follows the provider's documented model context rather than a local
    tokenizer estimate; the tighter per-question context only reduces exposure.
    """

    if (type(policy) is not BillingPolicy
            or not isinstance(policy.input_usd_per_token, Decimal)
            or not policy.input_usd_per_token.is_finite()
            or type(policy.input_token_ceiling) is not int
            or type(policy.max_wire_bytes) is not int
            or policy != STANDARD_BILLING_POLICY):
        raise BillingAdmissionError("a supported pinned pricing and context policy is required")
    if endpoint != SYSTEM_ONE_URL:
        raise BillingAdmissionError("billing admission requires the pinned System One endpoint")
    if template_version != policy.template_version:
        raise BillingAdmissionError("billing admission requires the pinned relationship template")
    if headers is not None:
        if not isinstance(headers, Mapping):
            raise BillingAdmissionError("request headers must be a mapping")
        allowed = {"authorization", "content-type", "accept"}
        if any(not isinstance(name, str) or name.casefold() not in allowed for name in headers):
            raise BillingAdmissionError("unsupported headers can change the billing context policy")
    if not isinstance(payload, Mapping) or set(payload) != {"model", "state", "questions"}:
        raise BillingAdmissionError("billing admission supports only standard text System One payloads")
    if payload.get("model") != policy.model:
        raise BillingAdmissionError("billing admission requires the exact pinned Jev model")
    questions = payload.get("questions")
    if not isinstance(questions, Mapping) or list(questions) != ["relationship"]:
        raise BillingAdmissionError("billing admission supports exactly one relationship Choice")
    question = questions["relationship"]
    if (not isinstance(question, Mapping) or set(question) != {"type", "instructions", "criteria"}
            or question.get("type") != "choice" or not isinstance(question.get("instructions"), str)
            or not isinstance(question.get("criteria"), Mapping)
            or not 2 <= len(question["criteria"]) <= 255):
        raise BillingAdmissionError("billing admission requires one bounded standard Choice question")
    for label, root in (("state", payload["state"]), ("instructions", question["instructions"]),
                        ("criteria", question["criteria"])):
        _validate_nesting(root, label)
    try:
        wire_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BillingAdmissionError("billing payload must be finite JSON") from exc
    if wire_bytes > policy.max_wire_bytes:
        raise BillingAdmissionError("prepared Jev request exceeds the local wire-byte admission limit; reduce supplied evidence")
    return BillingAdmission(policy=policy, wire_bytes=wire_bytes)


def batch_billing_admission(
    payload: Mapping[str, Any], *, policy: BillingPolicy | None = BATCH_BILLING_POLICY,
    endpoint: str = SYSTEM_ONE_URL, headers: Mapping[str, str] | None = None,
    template_version: str = BATCH_TEMPLATE_VERSION,
) -> BillingAdmission:
    """Admit a versioned one-to-three-question request at the full context reserve.

    The batch builder additionally validates exact endpoint/option binding.
    Neither wire bytes nor a count of questions is treated as a token bound.
    """
    if (type(policy) is not BillingPolicy
            or not isinstance(policy.input_usd_per_token, Decimal)
            or not policy.input_usd_per_token.is_finite()
            or type(policy.input_token_ceiling) is not int
            or type(policy.max_wire_bytes) is not int
            or policy != BATCH_BILLING_POLICY):
        raise BillingAdmissionError("a supported pinned batch billing policy is required")
    if endpoint != SYSTEM_ONE_URL or template_version != BATCH_TEMPLATE_VERSION:
        raise BillingAdmissionError("batch billing requires the pinned endpoint and template")
    if headers is not None:
        if not isinstance(headers, Mapping) or any(
            not isinstance(name, str) or name.casefold() not in {"authorization", "content-type", "accept"}
            for name in headers
        ):
            raise BillingAdmissionError("unsupported headers can change the billing context policy")
    if (not isinstance(payload, Mapping) or set(payload) != {"model", "state", "questions"}
            or payload.get("model") != MODEL_ID):
        raise BillingAdmissionError("batch billing requires a pinned standard text payload")
    questions = payload.get("questions")
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= 3:
        raise BillingAdmissionError("batch billing supports one to three Choice questions")
    for name, question in questions.items():
        if (not isinstance(name, str) or not name or len(name) > 128
                or not isinstance(question, Mapping) or set(question) != {"type", "instructions", "criteria"}
                or question.get("type") != "choice" or not isinstance(question.get("instructions"), str)
                or not isinstance(question.get("criteria"), Mapping)
                or not 2 <= len(question["criteria"]) <= 255):
            raise BillingAdmissionError("batch billing requires bounded standard Choice questions")
        for label, root in (("instructions", question["instructions"]), ("criteria", question["criteria"])):
            _validate_nesting(root, label)
    _validate_nesting(payload["state"], "state")
    try:
        wire_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BillingAdmissionError("billing payload must be finite JSON") from exc
    if wire_bytes > policy.max_wire_bytes:
        raise BillingAdmissionError("prepared Jev batch exceeds the local wire-byte admission limit; split the batch")
    return BillingAdmission(policy=policy, wire_bytes=wire_bytes)


def _validate_nesting(root: Any, label: str) -> None:
    """Count the root container as level one, conservatively within API limits."""

    containers = (Mapping, list, tuple)
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, containers):
            continue
        if depth > MAX_JSON_NESTING:
            raise BillingAdmissionError(f"{label} exceeds the 64-level JSON nesting admission limit")
        children = node.values() if isinstance(node, Mapping) else node
        stack.extend((child, depth + 1) for child in children if isinstance(child, containers))
