"""Bounded Jev pair questions sharing evidence and a durable spend ledger.

The client only proposes relationships. It never discovers pairs, accepts links,
or changes notes. Exact request caching is separate from a caller's eventual
pair-decision reuse. Explicit execution identities permit approved repeated
trials with identical wire bytes without clearing any previous cache or ledger.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import time
from typing import Any

from .jev_billing import (
    BATCH_BILLING_POLICY, BATCH_TEMPLATE_VERSION, BillingAdmissionError,
    BillingPolicy, batch_billing_admission,
)
from .jev_decisions import (
    API_URL, MODEL_ID, DecisionRequest, DecisionStore, Endpoint, JevConfig,
    JevDecisionError, Transport, _actual_cost, _bounded_string, _canonical_json,
    _decision_artifact, _endpoint_payload, _http_transport, _json_copy,
    _relationship_questions, _validate_response, build_payload,
    decision_cache_key, estimate_cost_upper_bound, snapshot_request,
)


BATCH_RESULT_SCHEMA = "jev-batch-result-v2"
MAX_PAIRS = 3
MAX_CONTEXT_NOTES = 4


@dataclass(frozen=True)
class BatchRequest:
    requests: tuple[DecisionRequest, ...]
    context_requests: tuple[DecisionRequest, ...] = ()
    packet_format: str = "full"
    execution_id: str = ""


def _endpoint_key(endpoint: Endpoint) -> tuple[str, str]:
    return endpoint.source_id, endpoint.note_id


def _pair_key(request: DecisionRequest) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((_endpoint_key(request.endpoint_a), _endpoint_key(request.endpoint_b))))


def _question_id(request: DecisionRequest) -> str:
    ordered = [_endpoint_key(request.endpoint_a), _endpoint_key(request.endpoint_b)]
    return "pair_" + hashlib.sha256(_canonical_json(ordered).encode()).hexdigest()[:20]


def _snapshot(batch: BatchRequest) -> BatchRequest:
    if not isinstance(batch, BatchRequest):
        raise ValueError("a BatchRequest is required")
    if not isinstance(batch.requests, tuple) or not 1 <= len(batch.requests) <= MAX_PAIRS:
        raise ValueError("assess one to three pair requests")
    if not isinstance(batch.context_requests, tuple) or len(batch.context_requests) > MAX_PAIRS:
        raise ValueError("context must contain at most three pair requests")
    if not isinstance(batch.packet_format, str) or batch.packet_format not in {"full", "compact"}:
        raise ValueError("packet_format must be full or compact")
    if not isinstance(batch.execution_id, str):
        raise ValueError("execution_id must be a string")
    if batch.execution_id:
        _bounded_string(batch.execution_id, "execution_id", 128)
    requests = tuple(snapshot_request(row) for row in batch.requests)
    context = tuple(snapshot_request(row) for row in (batch.context_requests or batch.requests))
    if len({_pair_key(row) for row in context}) != len(context):
        raise ValueError("context pairs must be distinct, including reversed pairs")
    if len({_pair_key(row) for row in requests}) != len(requests):
        raise ValueError("requested pairs must be distinct")
    scope = (context[0].workspace_id, context[0].library_id)
    if any((row.workspace_id, row.library_id) != scope for row in (*context, *requests)):
        raise ValueError("all pair requests must share workspace and library identity")
    context_keys = {decision_cache_key(row) for row in context}
    if any(decision_cache_key(row) not in context_keys for row in requests):
        raise ValueError("assessed pairs must be unchanged members of the supplied context")
    notes: dict[tuple[str, str], str] = {}
    passage_owners: dict[str, tuple[str, str]] = {}
    for row in context:
        for endpoint in (row.endpoint_a, row.endpoint_b):
            key = _endpoint_key(endpoint)
            value = _canonical_json(_endpoint_payload(endpoint))
            if key in notes and notes[key] != value:
                raise ValueError("a shared note has conflicting text or metadata")
            notes[key] = value
            for passage in endpoint.passages:
                if passage.passage_id in passage_owners and passage_owners[passage.passage_id] != key:
                    raise ValueError("evidence IDs must identify one context note")
                passage_owners[passage.passage_id] = key
    if len(notes) > MAX_CONTEXT_NOTES:
        raise ValueError("shared context supports at most four distinct notes")
    if len(context) > 1:
        shared = set((_endpoint_key(context[0].endpoint_a), _endpoint_key(context[0].endpoint_b)))
        for row in context[1:]:
            shared &= {_endpoint_key(row.endpoint_a), _endpoint_key(row.endpoint_b)}
        if not shared:
            raise ValueError("shared-note batches must have a common endpoint")
    return BatchRequest(requests, context, batch.packet_format, batch.execution_id)


def _compact_endpoint(endpoint: Endpoint) -> dict[str, Any]:
    value = _endpoint_payload(endpoint)
    # Deduplicate exact supplied text only; never summarize, slice or rewrite it.
    for passage in endpoint.passages:
        value["note_text"] = value["note_text"].replace(passage.text, f"[Supplied passage {passage.passage_id}]")
    for passage in value["passages"]:
        passage.pop("content_sha256")
        passage["locator"] = {
            key: item for key, item in passage["locator"].items()
            if key not in {"note_relative_path", "note_id", "source_path", "cache_path", "content_sha256", "source_sha256"}
        }
    return value


def _payload(batch: BatchRequest) -> dict[str, Any]:
    # Historical full isolated requests remain byte-equivalent to build_payload.
    if len(batch.context_requests) == len(batch.requests) == 1:
        payload = build_payload(batch.requests[0])
        if batch.packet_format == "compact":
            payload["state"]["endpoints"] = {
                "A": _compact_endpoint(batch.requests[0].endpoint_a),
                "B": _compact_endpoint(batch.requests[0].endpoint_b),
            }
        return payload
    notes: dict[str, Any] = {}
    names: dict[tuple[str, str], str] = {}
    comparisons: dict[str, Any] = {}
    for row in batch.context_requests:
        endpoints = {}
        for label, endpoint in (("A", row.endpoint_a), ("B", row.endpoint_b)):
            key = _endpoint_key(endpoint)
            if key not in names:
                name = "N" + str(len(names))
                names[key] = name
                notes[name] = _compact_endpoint(endpoint) if batch.packet_format == "compact" else _endpoint_payload(endpoint)
            endpoints[label] = names[key]
        comparisons[_question_id(row)] = {"endpoints": endpoints, "endpoint_order": ["A", "B"]}
    state = {
        "task": "Compare only the pairs named by the requested questions using supplied notes and evidence. Other comparison bindings are context, not requested decisions.",
        "untrusted_source_material_notice": build_payload(batch.context_requests[0])["state"]["untrusted_source_material_notice"],
        "notes": notes, "comparisons": comparisons,
    }
    questions = {}
    for row in batch.requests:
        name = _question_id(row)
        binding = comparisons[name]["endpoints"]
        question = _json_copy((row.questions or _relationship_questions(row))["relationship"])
        question["instructions"] = (
            f"For this question endpoint A is state.notes.{binding['A']} and endpoint B is state.notes.{binding['B']}. "
            "These two records alone supply this comparison's evidence. " + question["instructions"]
        )
        questions[name] = question
    return {"model": MODEL_ID, "state": state, "questions": questions}


def build_batch_payload(batch: BatchRequest) -> dict[str, Any]:
    """Build bounded evidence without dispatching or admitting spending."""
    captured = _snapshot(batch)
    payload = _payload(captured)
    batch_billing_admission(payload)
    return payload


def _identities(batch: BatchRequest, payload: Mapping[str, Any]) -> tuple[str, str, str]:
    payload_hash = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    identity = {
        "workspace_id": batch.requests[0].workspace_id,
        "library_id": batch.requests[0].library_id,
        "model": MODEL_ID, "template_version": BATCH_TEMPLATE_VERSION,
        "packet_format": batch.packet_format,
        "requests": [decision_cache_key(row) for row in batch.requests],
        "context": [decision_cache_key(row) for row in batch.context_requests],
        "payload_sha256": payload_hash,
    }
    exact = hashlib.sha256(_canonical_json(identity).encode()).hexdigest()
    execution = hashlib.sha256(_canonical_json({"exact_request_key": exact, "execution_id": batch.execution_id}).encode()).hexdigest()
    return exact, execution, payload_hash


def batch_cache_key(batch: BatchRequest) -> str:
    captured = _snapshot(batch)
    return _identities(captured, _payload(captured))[1]


def _validate(body: bytes, payload: Mapping[str, Any], *, max_tokens: int) -> dict[str, Any]:
    response, _ = _validate_response(body, expected_questions=payload["questions"], expected_model=MODEL_ID, max_tokens=max_tokens)
    for answer in response["answers"].values():
        probabilities = answer["probabilities"]
        top = max(float(value) for value in probabilities.values())
        if float(probabilities[answer["choice"]]) + 1e-12 < top:
            raise JevDecisionError("provider choice is not a highest-probability option")
        n = len(probabilities)
        confidence = (top - 1 / n) / (1 - 1 / n)
        if abs(float(answer["confidence"]) - confidence) > 0.02 + 1e-12:
            raise JevDecisionError("provider confidence is inconsistent with its probabilities")
    return response


def _artifact(batch: BatchRequest, payload: Mapping[str, Any], response: Mapping[str, Any], *,
              key: str, exact: str, payload_hash: str, cost: Decimal) -> dict[str, Any]:
    decisions = []
    allocated = Decimal("0")
    for index, request in enumerate(batch.requests):
        name = "relationship" if len(batch.context_requests) == len(batch.requests) == 1 else _question_id(request)
        share = cost - allocated if index == len(batch.requests) - 1 else (cost / len(batch.requests)).quantize(Decimal("0.000000001"))
        allocated += share
        pair_response = {**response, "answers": {"relationship": response["answers"][name]}}
        decision = _decision_artifact(request, pair_response, key, payload_hash, share)
        decision["pair_key"] = decision_cache_key(request)
        decision["provenance"].update(
            template_version=BATCH_TEMPLATE_VERSION, source_template_version=request.template_version,
            packet_format=batch.packet_format, question_id=name,
            batch_context_sha256=hashlib.sha256(_canonical_json(payload["state"]).encode()).hexdigest(),
            exact_request_key=exact, execution_id=batch.execution_id,
            pair_request_sha256=hashlib.sha256(_canonical_json(build_payload(request)).encode()).hexdigest(),
            batch_cost_usd=str(cost), cost_allocation="equal_share_of_batch_not_individual_metering",
            source_note_sha256=hashlib.sha256(request.endpoint_a.note_text.encode()).hexdigest(),
            target_note_sha256=hashlib.sha256(request.endpoint_b.note_text.encode()).hexdigest(),
        )
        decisions.append(decision)
    return {
        "schema": BATCH_RESULT_SCHEMA, "status": "review_required", "cache_key": key,
        "exact_request_key": exact, "request_payload_sha256": payload_hash,
        "model": MODEL_ID, "template_version": BATCH_TEMPLATE_VERSION,
        "packet_format": batch.packet_format, "execution_id": batch.execution_id,
        "response": response, "decisions": decisions, "cost_usd": str(cost),
    }


class JevBatchClient:
    def __init__(self, config: JevConfig, store: DecisionStore, *, transport: Transport | None = None,
                 billing_policy: BillingPolicy | None = BATCH_BILLING_POLICY) -> None:
        config.validate()
        if type(config.enabled) is not bool or type(config.data_sharing_confirmed) is not bool:
            raise ValueError("batch enabled and data-sharing confirmation must be explicit booleans")
        self.config, self.store = config, store
        self.transport = transport or _http_transport
        self.billing_policy = billing_policy
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS batch_decisions (
                cache_key TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
                artifact_json TEXT NOT NULL, created_at REAL NOT NULL
            )""")

    def _cached(self, batch: BatchRequest, payload: Mapping[str, Any], key: str, exact: str,
                payload_hash: str, max_tokens: int) -> dict[str, Any] | None:
        with self.store._connect() as db:
            row = db.execute("SELECT payload_sha256, artifact_json FROM batch_decisions WHERE cache_key=?", (key,)).fetchone()
        if row is None or row[0] != payload_hash or len(row[1].encode()) > 1_000_000:
            return None
        try:
            cached = json.loads(row[1])
            response = _validate(_canonical_json(cached["response"]).encode(), payload, max_tokens=max_tokens)
            cost = _actual_cost(response["usage"]["input_tokens"])
            expected = _artifact(batch, payload, response, key=key, exact=exact, payload_hash=payload_hash, cost=cost)
            return cached if _canonical_json(cached) == _canonical_json(expected) else None
        except (KeyError, TypeError, ValueError, RecursionError, JevDecisionError):
            return None

    @staticmethod
    def _cached_result(artifact: Mapping[str, Any]) -> dict[str, Any]:
        return {**dict(artifact), "status": "cached_review_required", "cache_hit": True,
                "provider_calls": 0, "cost_usd": "0"}

    def assess(self, batch: BatchRequest, *, dry_run: bool = True) -> dict[str, Any]:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be an explicit boolean")
        captured = _snapshot(batch)
        payload = _payload(captured)
        exact, key, payload_hash = _identities(captured, payload)
        cached = self._cached(captured, payload, key, exact, payload_hash, self.config.max_tokens)
        if cached is not None:
            return self._cached_result(cached)
        try:
            admission = batch_billing_admission(payload, policy=self.billing_policy, endpoint=API_URL)
        except BillingAdmissionError as exc:
            return {"status": "blocked", "reason": str(exc), "provider_calls": 0, "cache_key": key}
        record = admission.to_dict()
        preview = {"cache_key": key, "exact_request_key": exact, "payload": payload,
                   "billing_admission": record, "cost_estimate": estimate_cost_upper_bound(payload), "provider_calls": 0}
        if dry_run:
            return {"status": "dry_run", "cache_hit": False, **preview}
        checks = [
            (self.config.enabled, "optional Jev decisions are disabled"),
            (self.config.data_sharing_confirmed, "explicit provider data-sharing confirmation is required"),
            (bool(self.config.api_key and self.config.api_key.strip()), "TYPESAFE_API_KEY is required for a live decision"),
            (Decimal(str(self.config.budget_usd)) > 0, "positive explicit Jev spend budget is required"),
            (self.config.paid_call_approved, "explicit human paid-call approval is required"),
            (self.config.approved_billing_policy_identity == admission.policy.identity, "the exact pinned batch billing policy must be explicitly approved"),
            (key in self.config.approved_cache_keys, "this exact execution is not in the approved request allowlist"),
        ]
        for allowed, reason in checks:
            if not allowed:
                return {"status": "blocked", "reason": reason, "cache_key": key, "provider_calls": 0, "billing_admission": record}
        try:
            reservation = self.store.reserve(key, admission.max_model_usage_usd, Decimal(str(self.config.budget_usd)))
        except JevDecisionError as exc:
            cached = self._cached(captured, payload, key, exact, payload_hash, self.config.max_tokens)
            return self._cached_result(cached) if cached is not None else {"status": "blocked", "reason": str(exc), "cache_key": key, "provider_calls": 0}
        settled = False
        actual = None
        try:
            response = self.transport(_canonical_json(payload).encode(), api_key=self.config.api_key, timeout=self.config.timeout_seconds)
            if response.status != 200:
                self.store.mark_uncertain(reservation)
                return {"status": "provider_error", "http_status": response.status, "provider_calls": 1,
                        "cache_key": key, "reason": "provider outcome is not trusted; reservation retained; no retry was attempted"}
            validated = _validate(response.body, payload, max_tokens=min(self.config.max_tokens, admission.policy.input_token_ceiling))
            actual = _actual_cost(validated["usage"]["input_tokens"])
            self.store.settle(reservation, actual)
            settled = True
            artifact = _artifact(captured, payload, validated, key=key, exact=exact, payload_hash=payload_hash, cost=actual)
            serialized = _canonical_json(artifact)
            if len(serialized.encode()) > 1_000_000:
                raise JevDecisionError("batch decision artifact exceeds the cache bound")
            with self.store._transaction() as db:
                db.execute("INSERT OR REPLACE INTO batch_decisions VALUES (?, ?, ?, ?)", (key, payload_hash, serialized, time.time()))
            return {**artifact, "cache_hit": False, "provider_calls": 1}
        except Exception as exc:
            if settled:
                return {"status": "persistence_failed", "cache_key": key, "provider_calls": 1,
                        "cost_usd": str(actual), "accounting_status": "settled",
                        "reason": f"validated batch could not be persisted ({type(exc).__name__})"}
            self.store.mark_uncertain(reservation)
            return {"status": "invalid_response" if isinstance(exc, JevDecisionError) else "billing_uncertain",
                    "cache_key": key, "provider_calls": 1,
                    "reason": str(exc) if isinstance(exc, JevDecisionError) else f"transport outcome unknown ({type(exc).__name__}); reservation retained",
                    "reservation_usd": str(admission.max_model_usage_usd), "billing_admission": record}
