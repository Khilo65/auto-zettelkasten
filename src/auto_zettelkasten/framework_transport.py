"""Explicit, per-call admission for isolated framework experiments.

This module contains no HTTP client, credentials, environment lookup, or native
framework imports. An orchestrator injects a single-attempt backend after SDK
hook verification. Importing it cannot dispatch inference. An approval receipt
is installed once; ordinary request parameters cannot enable or extend it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import inspect
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlparse

from .framework_comparison import ComparisonError, FRAMEWORKS, StageBound


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _money(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ComparisonError("USD amounts must be decimal strings")
    try:
        result = Decimal(value)
    except Exception:
        raise ComparisonError("invalid USD amount") from None
    if not result.is_finite() or result < 0:
        raise ComparisonError("USD amount must be finite and nonnegative")
    return result


class TransportDiagnosticError(ComparisonError):
    """Only fixed local diagnostics may cross a backend's exception boundary."""

    _MESSAGES = {
        "http_status": "framework HTTPS request failed",
        "redirect": "framework HTTPS redirects are disabled",
        "receipt": "HTTPS backend requires an installed approval and explicit credential",
        "endpoint_receipt": "HTTPS endpoint or approval differs from the installed receipt",
        "endpoint": "only the exact approved HTTPS endpoint is permitted",
        "wire_fields": "metered Standard-tier, no-storage, no-cache-write wire fields are required",
        "wire_window": "wire payload does not fit an approved model/stage window",
        "timeout": "framework HTTPS timeout; dispatched outcome remains unknown",
        "response_endpoint": "framework HTTPS response changed the approved endpoint",
        "response_status": "framework HTTPS response was not a successful JSON completion",
        "response_type": "framework HTTPS response was not JSON",
        "content_length": "framework HTTPS content length is invalid",
        "response_size": "framework HTTPS response exceeds the approved read limit",
        "response_envelope": "framework HTTPS returned an invalid completion envelope",
        "response_json": "framework HTTPS completion JSON is malformed",
        "network": "framework HTTPS connection failed; no retry",
        "tls": "framework HTTPS certificate or TLS validation failed; no retry",
    }

    _ERROR_CODES = frozenset({"invalid_request_error", "invalid_request", "unsupported_parameter", "unsupported_value",
        "invalid_value", "missing_required_parameter", "unknown_parameter", "model_not_found", "invalid_api_key",
        "insufficient_quota", "rate_limit_exceeded", "context_length_exceeded", "server_error", "invalid_prompt",
        "permission_denied", "not_found", "billing_hard_limit_reached"})
    _ERROR_TYPES = frozenset({"invalid_request_error", "authentication_error", "permission_error", "not_found_error",
        "conflict_error", "unprocessable_entity_error", "rate_limit_error", "server_error", "api_error", "timeout_error",
        "insufficient_quota"})
    _ERROR_PARAMETERS = frozenset({"model", "messages", "reasoning_effort", "response_format", "temperature",
        "max_tokens", "max_completion_tokens", "tools", "tool_choice", "parallel_tool_calls", "service_tier",
        "prompt_cache_options", "prompt_cache_options.mode", "prompt_cache_options.ttl", "prompt_cache_key",
        "prompt_cache_retention", "store", "stream", "n", "top_p", "seed", "logprobs", "top_logprobs",
        "presence_penalty", "frequency_penalty", "response_format.type", "response_format.json_schema"})

    @classmethod
    def sanitize_machine_error(cls, value: Mapping[str, Any] | None) -> dict[str, str]:
        if not isinstance(value, Mapping):
            return {}
        result = {}
        for key, allowed in (("code", cls._ERROR_CODES), ("type", cls._ERROR_TYPES), ("param", cls._ERROR_PARAMETERS)):
            item = value.get(key)
            if isinstance(item, str) and len(item) <= 64 and re.fullmatch(r"[a-z][a-z0-9_.]*", item) and item in allowed:
                result[key] = item
        return result

    def __init__(self, code: str, *, http_status: int | None = None, machine_error: Mapping[str, Any] | None = None):
        if code not in self._MESSAGES or (http_status is not None and (code != "http_status" or type(http_status) is not int or not 100 <= http_status <= 599)):
            raise ComparisonError("invalid sanitized transport diagnostic")
        self.code, self.http_status = code, http_status
        self.machine_error = self.sanitize_machine_error(machine_error) if code == "http_status" else {}
        super().__init__(self.safe_message)

    @property
    def safe_message(self) -> str:
        # Do not trust mutable Exception.args or arbitrary backend messages.
        text = self._MESSAGES.get(self.code, "framework transport diagnostic unavailable")
        if self.code == "http_status" and type(self.http_status) is int and 100 <= self.http_status <= 599:
            text += f" (HTTP {self.http_status})"
            safe = self.sanitize_machine_error(self.machine_error)
            if safe:
                text += " [" + ", ".join(f"{key}={safe[key]}" for key in ("type", "code", "param") if key in safe) + "]"
        return text


@dataclass(frozen=True)
class TransportApproval:
    """A trusted orchestrator's previously authorized, immutable receipt.

    Construction does not itself constitute human authorization. The parent
    workflow must obtain that separately and keep the installed receipt outside
    the untrusted framework process. No per-call boolean can replace it.
    """

    approval_id: str
    corpus_sha256: str
    context_sha256: str
    endpoint: str
    aggregate_usd: str
    framework_caps: tuple[tuple[str, str], ...]
    stages: tuple[StageBound, ...]
    request_hashes: tuple[str, ...] | None = None
    response_models: tuple[str, ...] = ()
    service_tier: str = "default"
    admission_mode: str = "full_plan_v1"

    def document(self) -> dict[str, Any]:
        """Preserve pre-mode strict receipt/request identities byte-for-byte."""
        value = asdict(self)
        if self.admission_mode == "full_plan_v1":
            value.pop("admission_mode")
        return value

    def identity(self) -> str:
        if not isinstance(self.approval_id, str) or not self.approval_id.strip() or len(self.approval_id) > 256:
            raise ComparisonError("explicit approval identity is required")
        for value in (self.corpus_sha256, self.context_sha256):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ComparisonError("approval requires frozen corpus and execution context hashes")
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ComparisonError("approval requires an explicit HTTPS endpoint without credentials")
        if not isinstance(self.framework_caps, tuple) or not isinstance(self.stages, tuple) or not self.stages:
            raise ComparisonError("approval limits must be immutable tuples")
        caps = dict(self.framework_caps)
        if len(caps) != len(self.framework_caps) or not caps or any(key not in FRAMEWORKS for key in caps):
            raise ComparisonError("invalid framework sublimits")
        if sum((_money(value) for value in caps.values()), Decimal(0)) > _money(self.aggregate_usd):
            raise ComparisonError("framework sublimits exceed aggregate")
        if self.admission_mode not in {"full_plan_v1", "rolling_call_v1"}:
            raise ComparisonError("unknown versioned call admission mode")
        seen = set()
        planned = {key: Decimal(0) for key in caps}
        for bound in self.stages:
            if not isinstance(bound, StageBound) or (bound.framework, bound.stage) in seen or bound.framework not in caps:
                raise ComparisonError("distinct approved stages are required")
            seen.add((bound.framework, bound.stage))
            planned[bound.framework] += bound.reservation()
            if bound.max_calls < 1 or bound.max_input_tokens_per_call < 1 or bound.max_output_tokens_per_call < 1:
                raise ComparisonError("chat stages require positive call/input/output limits")
        if self.admission_mode == "full_plan_v1" and any(planned[key] > _money(caps[key]) for key in caps):
            raise ComparisonError("stage limits exceed framework sublimit")
        if self.request_hashes is not None and (not isinstance(self.request_hashes, tuple) or not self.request_hashes
                or any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in self.request_hashes)):
            raise ComparisonError("approved exact requests require SHA-256 identities")
        if not isinstance(self.response_models, tuple) or any(not isinstance(x, str) or not x for x in self.response_models):
            raise ComparisonError("response model identities must be immutable")
        if self.service_tier != "default":
            raise ComparisonError("this experiment is priced only for explicit Standard/default service tier")
        return _sha(_json(self.document()))


def request_identity(approval: TransportApproval, framework: str, stage: str, payload: Mapping[str, Any]) -> str:
    """Identity includes batch context and endpoint, not merely pair IDs."""
    approval.identity()
    execution = approval.document()
    # The allowlist cannot participate in its own request-key derivation.
    # It still participates in the installed durable policy identity.
    execution.pop("request_hashes")
    canonical_payload = dict(payload)
    canonical_payload.setdefault("service_tier", approval.service_tier)
    canonical_payload.setdefault("store", False)
    canonical_payload.setdefault("prompt_cache_options", {"mode": "explicit", "ttl": "30m"})
    return _sha(_json({"policy": _sha(_json(execution)), "framework": framework, "stage": stage,
                       "endpoint": approval.endpoint, "payload": canonical_payload}))


def input_token_upper_bound(payload: Mapping[str, Any]) -> int:
    """Conservative text byte allowance plus explicit chat framing overhead.

    This is an admission bound, NOT a tokenizer measurement. Reserve the entire
    approved input window even for shorter requests. Unknown content modalities,
    hosted tools and streaming are refused rather than priced as plain text.
    """
    return len(_json(payload).encode("utf-8")) + 512 * len(payload["messages"]) + 1024


def validate_chat_payload(payload: Mapping[str, Any], bound: StageBound) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, Mapping):
        raise ComparisonError("chat payload must be an object")
    allowed = {"model", "messages", "max_completion_tokens", "max_tokens", "stream", "n", "temperature",
               "top_p", "seed", "response_format", "tools", "tool_choice", "parallel_tool_calls", "reasoning_effort", "service_tier", "store", "prompt_cache_options"}
    if set(payload) - allowed:
        raise ComparisonError("unapproved chat request fields")
    value = json.loads(_json(payload))
    value.setdefault("service_tier", "default")
    value.setdefault("store", False)
    value.setdefault("prompt_cache_options", {"mode": "explicit", "ttl": "30m"})
    if value["service_tier"] != "default" or value["store"] is not False:
        raise ComparisonError("only explicit Standard/default tier with provider storage disabled is approved")
    if value["prompt_cache_options"] != {"mode": "explicit", "ttl": "30m"}:
        raise ComparisonError("controlled pricing disables implicit prompt cache writes")
    def has_breakpoint(item):
        if isinstance(item, dict):
            return "prompt_cache_breakpoint" in item or any(has_breakpoint(v) for v in item.values())
        return isinstance(item, list) and any(has_breakpoint(v) for v in item)
    if has_breakpoint(value):
        raise ComparisonError("explicit cache breakpoints are not approved in the uncached control")
    if value.get("model") != bound.model:
        raise ComparisonError("model differs from approved stage")
    if value.get("stream", False) is not False or value.get("n", 1) != 1:
        raise ComparisonError("streaming and multiple completions are disabled")
    if "max_tokens" in value and "max_completion_tokens" in value:
        raise ComparisonError("only one output token limit is allowed")
    limit = value.get("max_completion_tokens", value.get("max_tokens"))
    if type(limit) is not int or not 1 <= limit <= bound.max_output_tokens_per_call:
        raise ComparisonError("explicit bounded output tokens are required")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise ComparisonError("bounded chat messages are required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ComparisonError("invalid chat role")
        if set(message) - {"role", "content", "name", "tool_call_id", "tool_calls"}:
            raise ComparisonError("unapproved message fields")
        if not isinstance(message.get("content"), str):
            raise ComparisonError("only text note evidence is permitted")
    tools = value.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 64 or any(not isinstance(t, dict) or t.get("type") != "function" for t in tools):
        raise ComparisonError("only bounded function schemas are permitted, never hosted tools")
    upper = input_token_upper_bound(value)
    if upper > bound.max_input_tokens_per_call:
        raise ComparisonError("conservative input token bound exceeds approved window")
    return value, upper


class MeteredCallLedger:
    """Atomic per-call limits and exact-response cache, without storing prompts.

    Only hashes/usage are durable by default. Optional response caching is
    explicitly private; neither secret keys nor raw request text is recorded.
    A failed/cancelled/ambiguous dispatched call remains unknown and reserved.
    """

    def __init__(self, path: Path, approval: TransportApproval | None = None):
        self.path = Path(path)
        self.approval = approval
        self.policy_identity = approval.identity() if approval else None
        if approval is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS transport_policy (id INTEGER PRIMARY KEY, identity TEXT NOT NULL, policy_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS transport_calls (request_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, framework TEXT NOT NULL, stage TEXT NOT NULL, reservation TEXT NOT NULL, settled TEXT, status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, response_json TEXT, input_bound INTEGER NOT NULL, output_limit INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS transport_holds (id INTEGER PRIMARY KEY, reason TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS transport_request_bindings (request_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL)")
            if {r[1] for r in db.execute("PRAGMA table_info(transport_calls)")} != {"request_id", "cache_key", "framework", "stage", "reservation", "settled", "status", "input_tokens", "output_tokens", "response_json", "input_bound", "output_limit"}:
                raise ComparisonError("experimental ledger schema differs; preserve it and use a fresh ledger")
            old = db.execute("SELECT identity FROM transport_policy WHERE id=1").fetchone()
            if old and old[0] != self.policy_identity:
                raise ComparisonError("installed transport approval cannot be changed")
            db.execute("INSERT OR IGNORE INTO transport_policy VALUES (1, ?, ?)", (self.policy_identity, _json(approval.document())))
        os.chmod(self.path, 0o600)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def bound(self, framework: str, stage: str) -> StageBound:
        if self.approval is None:
            raise ComparisonError("no installed paid transport approval; dispatch disabled")
        # The frozen dataclass and persisted policy must still agree.
        if self.approval.identity() != self.policy_identity:
            raise ComparisonError("transport policy changed")
        for bound in self.approval.stages:
            if (bound.framework, bound.stage) == (framework, stage):
                return bound
        raise ComparisonError("unapproved framework stage")

    def reserve(self, request_id: str, framework: str, stage: str, payload: Mapping[str, Any], *,
                before_reservation: Callable[[StageBound, Decimal], None] | None = None) -> dict[str, Any]:
        if before_reservation is not None and not callable(before_reservation):
            raise ComparisonError("spending admission callback must be callable")
        bound = self.bound(framework, stage)
        value, input_upper = validate_chat_payload(payload, bound)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ComparisonError("stable bounded request identity is required")
        key = request_identity(self.approval, framework, stage, value)
        if self.approval.request_hashes is not None and key not in self.approval.request_hashes:
            raise ComparisonError("request is not in the exact approved allowlist")
        reservation = bound.reservation() / bound.max_calls
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT identity FROM transport_policy WHERE id=1").fetchone() != (self.policy_identity,):
                raise ComparisonError("durable transport approval changed")
            if db.execute("SELECT 1 FROM transport_holds").fetchone():
                raise ComparisonError("transport held for manual reconciliation; no further dispatch")
            old_id = db.execute("SELECT cache_key FROM transport_request_bindings WHERE request_id=?", (request_id,)).fetchone()
            if old_id is None:
                old_id = db.execute("SELECT cache_key FROM transport_calls WHERE request_id=?", (request_id,)).fetchone()
            if old_id and old_id[0] != key:
                raise ComparisonError("request identity cannot be rebound to changed evidence")
            cached = db.execute("SELECT status, response_json, input_tokens, output_tokens, settled, output_limit FROM transport_calls WHERE cache_key=?", (key,)).fetchone()
            if cached:
                if cached[0] == "settled" and cached[1] is not None:
                    response = self._cached_response(cached, key, bound)
                    # A replay still binds its workflow ID. Reusing that ID for
                    # changed evidence later cannot turn it into a fresh call.
                    db.execute("INSERT OR IGNORE INTO transport_request_bindings VALUES (?, ?)", (request_id, key))
                    return {"status": "cached", "cache_key": key, "response": response, "provider_calls": 0}
                raise ComparisonError("request already dispatched/reserved; no automatic retry")
            rows = db.execute("SELECT framework, stage, reservation, settled, status FROM transport_calls").fetchall()
            def exposure(row):
                return _money(row[3] if row[4] == "settled" else row[2])
            aggregate = sum((exposure(row) for row in rows), Decimal(0))
            subtotal = sum((exposure(row) for row in rows if row[0] == framework), Decimal(0))
            stage_total = sum((exposure(row) for row in rows if row[:2] == (framework, stage)), Decimal(0))
            stage_calls = sum(row[:2] == (framework, stage) for row in rows)
            caps = dict(self.approval.framework_caps)
            if stage_calls >= bound.max_calls or stage_total + reservation > bound.reservation():
                raise ComparisonError("stage call or spending limit exhausted")
            if aggregate + reservation > _money(self.approval.aggregate_usd) or subtotal + reservation > _money(caps[framework]):
                raise ComparisonError("aggregate or framework spending limit exhausted")
            # A trusted orchestrator may enforce a tighter aggregate shared by
            # several ledgers. Its cross-ledger lock must surround this entire
            # admission transaction. Exact cached replays never reach this hook.
            if before_reservation is not None:
                checked = before_reservation(bound, reservation)
                if inspect.isawaitable(checked):
                    if inspect.iscoroutine(checked):
                        checked.close()
                    raise ComparisonError("spending admission callback must be synchronous")
                if checked is not None:
                    raise ComparisonError("spending admission callback must raise on denial and return None")
            db.execute("INSERT INTO transport_calls VALUES (?, ?, ?, ?, ?, NULL, 'reserved', NULL, NULL, NULL, ?, ?)",
                       (request_id, key, framework, stage, str(reservation), input_upper,
                        value.get("max_completion_tokens", value.get("max_tokens"))))
            db.execute("INSERT INTO transport_request_bindings VALUES (?, ?)", (request_id, key))
        return {"status": "reserved", "cache_key": key, "reservation_usd": str(reservation),
                "input_token_upper_bound": input_upper, "input_bound_method": "utf8_wire_bytes_plus_chat_overhead_not_tokenizer_count",
                "payload": value, "provider_calls": 0}

    def dispatched(self, request_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM transport_holds").fetchone():
                raise ComparisonError("transport held for manual reconciliation; queued call not dispatched")
            result = db.execute("UPDATE transport_calls SET status='unknown' WHERE request_id=? AND status='reserved'", (request_id,))
            if result.rowcount != 1:
                raise ComparisonError("only one reserved call may be dispatched")

    def _hold(self, reason: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO transport_holds VALUES (1, ?)", (reason,))

    def _cached_response(self, row: tuple[Any, ...], key: str, bound: StageBound) -> dict[str, Any]:
        try:
            artifact = json.loads(row[1])
            response = artifact["response"]
            expected_digest = _sha(_json({"policy_identity": self.policy_identity, "cache_key": key, "response": response}))
            if artifact.get("policy_identity") != self.policy_identity or artifact.get("cache_key") != key or artifact.get("sha256") != expected_digest:
                raise ValueError("cache identity mismatch")
            input_tokens, output_tokens, amount = self._response_usage(response, bound, row[5])
            if (input_tokens, output_tokens, amount) != (row[2], row[3], _money(row[4])):
                raise ValueError("cached usage differs from settled ledger")
            return response
        except (ValueError, TypeError, KeyError, ComparisonError):
            raise ComparisonError("cached response is stale, invalid or tampered; no dispatch") from None

    def _response_usage(self, response: Mapping[str, Any], bound: StageBound, output_limit: int) -> tuple[int, int, Decimal]:
        if not isinstance(response, Mapping):
            raise ComparisonError("provider returned an invalid response; reservation retained")
        allowed_models = self.approval.response_models or (bound.model,)
        if response.get("model") not in allowed_models:
            raise ComparisonError("provider response model is not pinned; reservation retained")
        if response.get("service_tier") != self.approval.service_tier:
            raise ComparisonError("provider response service tier is not Standard/default; reservation retained")
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise ComparisonError("provider usage missing; reservation retained")
        details = usage.get("prompt_tokens_details")
        # Every admitted request has the immutable explicit/no-breakpoint cache
        # contract, which the API documents as disabling prompt caching. Its
        # optional usage counters may therefore be absent; that is not a claim
        # of a measured zero. Any explicit contrary/malformed count still halts.
        if details is not None and not isinstance(details, Mapping):
            raise ComparisonError("provider cache write usage is malformed; reservation retained")
        if isinstance(details, Mapping) and "cache_write_tokens" in details and (type(details["cache_write_tokens"]) is not int or details["cache_write_tokens"] != 0):
            raise ComparisonError("provider cache write usage contradicts the no-cache contract; reservation retained")
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
            raise ComparisonError("provider token usage invalid; reservation retained")
        if input_tokens > bound.max_input_tokens_per_call or output_tokens > output_limit:
            raise ComparisonError("provider exceeded approved token caps; reservation retained for manual reconciliation")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise ComparisonError("provider response choices invalid; reservation retained")
        message = choices[0].get("message")
        if not isinstance(message, Mapping) or (not isinstance(message.get("content"), str) and not isinstance(message.get("tool_calls"), list)):
            raise ComparisonError("provider response has no completion message; reservation retained")
        amount = input_tokens * _money(bound.input_usd_per_token) + output_tokens * _money(bound.output_usd_per_token)
        return input_tokens, output_tokens, amount

    def settle(self, request_id: str, bound: StageBound, response: Mapping[str, Any], *, cache: bool) -> dict[str, Any]:
        if bound != self.bound(bound.framework, bound.stage):
            raise ComparisonError("settlement stage differs from installed policy")
        with self._db() as db:
            reserved = db.execute("SELECT output_limit FROM transport_calls WHERE request_id=?", (request_id,)).fetchone()
        if not reserved:
            raise ComparisonError("settlement requires existing request reservation")
        try:
            input_tokens, output_tokens, amount = self._response_usage(response, bound, reserved[0])
        except ComparisonError as exc:
            if "not pinned" in str(exc) or "exceeded approved token caps" in str(exc) or "service tier" in str(exc) or "cache write" in str(exc):
                self._hold("provider model, pricing or token contract violated; actual cost requires manual reconciliation")
            raise
        serialized = _json(response)
        if len(serialized.encode()) > 2_000_000:
            raise ComparisonError("response exceeds private cache bound; reservation retained")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT framework, stage, reservation, status, cache_key FROM transport_calls WHERE request_id=?", (request_id,)).fetchone()
            if not row or row[:2] != (bound.framework, bound.stage) or row[3] != "unknown" or amount > _money(row[2]):
                raise ComparisonError("settlement mismatched or repeated; reservation retained")
            artifact = {"policy_identity": self.policy_identity, "cache_key": row[4], "response": response}
            artifact["sha256"] = _sha(_json(artifact))
            db.execute("UPDATE transport_calls SET status='settled', settled=?, input_tokens=?, output_tokens=?, response_json=? WHERE request_id=?",
                       (str(amount), input_tokens, output_tokens, _json(artifact) if cache else None, request_id))
        return {"status": "settled", "usage_derived_cost_usd": str(amount), "invoice_cost_usd": None,
                "input_tokens": input_tokens, "output_tokens": output_tokens, "provider_calls": 1,
                "cache_write_accounting": "reported_zero" if isinstance(response["usage"].get("prompt_tokens_details"), Mapping) and "cache_write_tokens" in response["usage"]["prompt_tokens_details"] else "optional_counter_absent_under_explicit_no_cache_request_contract"}

    def snapshot(self) -> dict[str, Any]:
        if self.approval is None:
            return {"approval_installed": False, "calls": [], "exposure_usd": "0"}
        with self._db() as db:
            rows = db.execute("SELECT request_id, framework, stage, reservation, settled, status, input_tokens, output_tokens FROM transport_calls ORDER BY request_id").fetchall()
            holds = [r[0] for r in db.execute("SELECT reason FROM transport_holds")]
        return {"approval_installed": True, "policy_identity": self.policy_identity,
                "manual_reconciliation_required": bool(holds), "holds": holds,
                "exposure_usd": str(sum((_money(row[4] if row[5] == "settled" else row[3]) for row in rows), Decimal(0))),
                "calls": [{"request_id": r[0], "framework": r[1], "stage": r[2], "reservation_usd": r[3],
                           "usage_derived_cost_usd": r[4], "status": r[5], "input_tokens": r[6], "output_tokens": r[7]} for r in rows]}


class MeteredChatTransport:
    """One-attempt async backend seam; no per-request enable flag or fallback.

    An optional shared guard locks only admission and settlement bookkeeping,
    never the provider wait. It must permit settlement of already dispatched
    calls even after a peer fails; new-dispatch checks belong in the synchronous
    ``before_reservation`` callback. The orchestrator owns concurrency limits.
    """

    def __init__(self, ledger: MeteredCallLedger, *, backend: Callable[..., Any] | None = None, cache_responses: bool = False,
                 admission_guard: Callable[[], Any] | None = None,
                 before_reservation: Callable[[StageBound, Decimal], None] | None = None):
        if admission_guard is not None and not callable(admission_guard):
            raise ComparisonError("spending admission guard must be an explicit context-manager factory")
        if before_reservation is not None and not callable(before_reservation):
            raise ComparisonError("spending admission callback must be callable")
        if before_reservation is not None and admission_guard is None:
            raise ComparisonError("cross-ledger spending admission requires an explicit guard")
        self.ledger, self.backend, self.cache_responses = ledger, backend, cache_responses
        self.admission_guard, self.before_reservation = admission_guard, before_reservation

    async def __call__(self, *, framework: str, stage: str, request_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        bound = self.ledger.bound(framework, stage)
        if self.backend is None:
            raise ComparisonError("no verified single-attempt provider backend installed")
        if hasattr(self.backend, "approval_identity") and self.backend.approval_identity != self.ledger.policy_identity:
            raise ComparisonError("provider backend approval differs from metering ledger; no reservation or dispatch")
        # Keep shared aggregate admission and reservation atomic, but release
        # that lock before waiting for the network. Every concurrent request is
        # already fully reserved and marked unknown before its backend starts.
        with self.admission_guard() if self.admission_guard is not None else nullcontext():
            if self.before_reservation is None:
                admission = self.ledger.reserve(request_id, framework, stage, payload)
            else:
                admission = self.ledger.reserve(request_id, framework, stage, payload,
                                                 before_reservation=self.before_reservation)
            if admission["status"] == "cached":
                return {"response": admission["response"], "metering": {"status": "cached", "provider_calls": 0, "usage_derived_cost_usd": "0"}}
            self.ledger.dispatched(request_id)
        try:
            response = self.backend(endpoint=self.ledger.approval.endpoint, payload=admission["payload"])
            if inspect.isawaitable(response):
                response = await response
        except TransportDiagnosticError as exc:
            raise ComparisonError(f"{exc.safe_message}; reservation retained") from None
        except Exception as exc:
            raise ComparisonError(f"provider attempt failed ({type(exc).__name__}); reservation retained") from None
        if hasattr(response, "model_dump"):
            response = response.model_dump(mode="json")
        # Publish validated costs or a ledger's contract hold under the same
        # shared lock before another ledger's aggregate admission can proceed.
        with self.admission_guard() if self.admission_guard is not None else nullcontext():
            metering = self.ledger.settle(request_id, bound, response, cache=self.cache_responses)
        return {"response": response, "metering": metering}


class LocalEmbeddingTransport:
    """Local-only injection seam with explicit window and resource checks.

    The supplied encoder is already verified and cached; this class never loads
    or downloads it. Long derived descriptions must be chunked by the caller's
    declared representation policy, never silently truncated here.
    """

    def __init__(self, *, identity: Mapping[str, Any], expected_identity: Mapping[str, Any],
                 encode: Callable[..., Any], token_count: Callable[[str], int],
                 resource_guard: Callable[[], Any], dimension: int = 384, max_tokens: int = 256):
        if _json(identity) != _json(expected_identity) or not identity or type(dimension) is not int or dimension < 1:
            raise ComparisonError("local encoder identity/dimension mismatch")
        if not all(callable(x) for x in (encode, token_count, resource_guard)) or type(max_tokens) is not int or max_tokens < 1:
            raise ComparisonError("explicit encoder, tokenizer and resource guard are required")
        self.identity = json.loads(_json(identity))
        self.encode, self.token_count, self.resource_guard = encode, token_count, resource_guard
        self.dimension, self.max_tokens = dimension, max_tokens
        self.calls = 0

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, (str, bytes)) or not 1 <= len(texts) <= 32:
            raise ComparisonError("local embedding batch must contain 1–32 texts")
        # Tokenizers can allocate substantial memory too. The first guard must
        # run before UTF-8 admission and tokenization, not only before encode.
        result = self.resource_guard()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise ComparisonError("local resource guard did not admit tokenization")
        frozen = list(texts)
        text_bytes = 0
        for text in frozen:
            if not isinstance(text, str) or not text.strip():
                raise ComparisonError("local embedding inputs must be substantive text")
            if len(text) > 2_000_000:
                raise ComparisonError("local embedding text exceeds the 2000000-character input bound")
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > 64 * 1024 * 1024:
                raise ComparisonError("local embedding batch exceeds the 64 MiB UTF-8 input bound")
        for text in frozen:
            count = self.token_count(text)
            if type(count) is not int or not 1 <= count <= self.max_tokens:
                raise ComparisonError("local embedding token window exceeded; no silent truncation")
        result = self.resource_guard()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise ComparisonError("local resource guard did not admit inference")
        self.calls += 1
        vectors = self.encode(frozen)
        if inspect.isawaitable(vectors):
            vectors = await vectors
        result = self.resource_guard()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise ComparisonError("local resource guard did not admit embedding results")
        if len(vectors) != len(frozen):
            raise ComparisonError("local encoder returned wrong batch length")
        output = []
        for vector in vectors:
            if any(isinstance(v, bool) or not isinstance(v, Real) for v in vector):
                raise ComparisonError("local encoder vectors must contain real non-boolean values")
            values = [float(v) for v in vector]
            if len(values) != self.dimension or any(not math.isfinite(v) for v in values) or not math.isclose(sum(v * v for v in values), 1.0, rel_tol=1e-4, abs_tol=1e-4):
                raise ComparisonError("local encoder returned nonfinite, wrong-size or nonnormalized vectors")
            output.append(values)
        return output
