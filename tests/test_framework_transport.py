import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading

import pytest

from auto_zettelkasten.framework_comparison import ComparisonError, StageBound
from auto_zettelkasten.framework_transport import (
    LocalEmbeddingTransport, MeteredCallLedger, MeteredChatTransport, TransportApproval,
    TransportDiagnosticError, input_token_upper_bound, request_identity, validate_chat_payload,
)


def bound(framework="graphiti", stage="query", **changes):
    return replace(StageBound(framework, stage, 2, 32768, 4096, "0.0000001", "0.0000005",
                             "https://example.org/synthetic-price", "synthetic-pinned"), **changes)


def approval(**changes):
    return replace(TransportApproval("fixture-only", "a" * 64, "b" * 64,
        "https://example.org/v1/chat/completions", "0.03", (("graphiti", "0.02"), ("lightrag", "0.01")),
        (bound(), bound("lightrag", max_calls=1))), **changes)


def payload(text="Synthetic notes, never real private data.", **changes):
    return {"model": "synthetic-pinned", "messages": [{"role": "user", "content": text}],
            "max_completion_tokens": 100, **changes}


def response(**changes):
    return {"model": "synthetic-pinned", "service_tier": "default", "choices": [{"message": {"role": "assistant", "content": "bounded result"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "prompt_tokens_details": {"cache_write_tokens": 0}}, **changes}


def test_default_has_no_approval_or_backend_and_creates_no_store(tmp_path):
    path = tmp_path / "not-created.sqlite3"
    ledger = MeteredCallLedger(path)
    assert ledger.snapshot()["approval_installed"] is False
    assert not path.exists()
    with pytest.raises(ComparisonError, match="no installed"):
        asyncio.run(MeteredChatTransport(ledger)(framework="graphiti", stage="query", request_id="r", payload=payload()))
    approved = MeteredCallLedger(path, approval())
    with pytest.raises(ComparisonError, match="no verified"):
        asyncio.run(MeteredChatTransport(approved)(framework="graphiti", stage="query", request_id="r", payload=payload()))
    assert not approved.snapshot()["calls"]


def test_exact_request_and_endpoint_context_model_binding(tmp_path):
    policy = approval()
    key = request_identity(policy, "graphiti", "query", payload())
    exact = replace(policy, request_hashes=(key,))
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", exact)
    ledger.reserve("one", "graphiti", "query", payload())
    with pytest.raises(ComparisonError, match="allowlist"):
        ledger.reserve("two", "graphiti", "query", payload("changed context"))
    assert key != request_identity(replace(policy, context_sha256="c" * 64), "graphiti", "query", payload())
    assert key != request_identity(replace(policy, endpoint="https://example.org/other"), "graphiti", "query", payload())
    with pytest.raises(ComparisonError, match="cannot be changed"):
        MeteredCallLedger(ledger.path, replace(exact, aggregate_usd="0.04"))


@pytest.mark.parametrize("changes", [
    {"stream": True}, {"n": 2}, {"model": "replacement"}, {"max_completion_tokens": 4097},
    {"max_completion_tokens": True}, {"max_tokens": 1}, {"paid_feature": "unapproved"},
    {"tools": [{"type": "web_search"}]}, {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
    {"service_tier": "auto"}, {"service_tier": "priority"}, {"store": True},
    {"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}},
    {"tools": [{"type": "function", "function": {"name": "f", "prompt_cache_breakpoint": {"mode": "explicit"}}}]},
])
def test_unsafe_payloads_rejected_before_backend(changes):
    with pytest.raises(ComparisonError):
        validate_chat_payload(payload(**changes), bound())


def test_token_bound_is_not_a_claim_of_measured_tokens():
    value = payload("中文")
    measured = input_token_upper_bound(value)
    assert measured > len(json.dumps(value, ensure_ascii=False).encode())
    with pytest.raises(ComparisonError, match="input token bound"):
        validate_chat_payload(payload("x" * 32768), bound())


def test_backend_called_once_settled_and_exact_cache_replayed(tmp_path):
    seen = []
    async def backend(**kwargs):
        seen.append(kwargs)
        return response()
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=backend, cache_responses=True)
    first = asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    cached = asyncio.run(transport(framework="graphiti", stage="query", request_id="new-id", payload=payload()))
    assert len(seen) == 1 and seen[0]["endpoint"] == approval().endpoint
    assert seen[0]["payload"]["service_tier"] == "default"
    assert seen[0]["payload"]["store"] is False
    assert seen[0]["payload"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert first["metering"]["provider_calls"] == 1
    assert first["metering"]["invoice_cost_usd"] is None
    assert cached["metering"]["provider_calls"] == 0
    assert ledger.snapshot()["calls"][0]["status"] == "settled"
    with pytest.raises(ComparisonError, match="cannot be rebound"):
        ledger.reserve("one", "graphiti", "query", payload("another note"))
    with pytest.raises(ComparisonError, match="cannot be rebound"):
        ledger.reserve("new-id", "graphiti", "query", payload("changed after cache replay"))


@pytest.mark.parametrize("recompute_digest", [False, True])
def test_cached_response_revalidated_even_after_tampering(tmp_path, recompute_digest):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: response(), cache_responses=True)
    asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    with sqlite3.connect(ledger.path) as db:
        artifact = json.loads(db.execute("SELECT response_json FROM transport_calls").fetchone()[0])
        artifact["response"]["model"] = "replacement"
        if recompute_digest:
            artifact.pop("sha256")
            wire = json.dumps(artifact, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            artifact["sha256"] = hashlib.sha256(wire.encode()).hexdigest()
        db.execute("UPDATE transport_calls SET response_json=?", (json.dumps(artifact),))
    with pytest.raises(ComparisonError, match="tampered"):
        ledger.reserve("cached", "graphiti", "query", payload())


def test_failure_is_unknown_reserved_and_error_does_not_leak_request_or_key(tmp_path):
    calls = []
    async def backend(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("SECRET-KEY and private prompt should never be shown")
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=backend)
    with pytest.raises(ComparisonError) as error:
        asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert "SECRET" not in str(error.value)
    row = ledger.snapshot()["calls"][0]
    assert row["status"] == "unknown" and row["usage_derived_cost_usd"] is None
    assert Decimal(ledger.snapshot()["exposure_usd"]) == bound().reservation() / 2
    with pytest.raises(ComparisonError, match="no automatic retry"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="two", payload=payload()))
    assert len(calls) == 1


@pytest.mark.parametrize("error", [ComparisonError("SECRET-KEY private request text"),
                                  RuntimeError("SECRET-KEY private request text")])
def test_arbitrary_backend_error_messages_still_do_not_escape(tmp_path, error):
    def backend(**kwargs):
        raise error
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError) as failed:
        asyncio.run(MeteredChatTransport(ledger, backend=backend)(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert "SECRET" not in str(failed.value) and "private" not in str(failed.value)
    assert "reservation retained" in str(failed.value)
    assert ledger.snapshot()["calls"][0]["status"] == "unknown"


def test_typed_diagnostic_uses_allowlisted_reason_not_mutable_exception_args(tmp_path):
    error = TransportDiagnosticError("response_size")
    error.args = ("SECRET-KEY private provider response",)
    def backend(**kwargs):
        raise error
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError, match="response exceeds the approved read limit") as failed:
        asyncio.run(MeteredChatTransport(ledger, backend=backend)(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert "SECRET" not in str(failed.value)
    with pytest.raises(ComparisonError, match="invalid sanitized"):
        TransportDiagnosticError("provider says SECRET-KEY")
    with pytest.raises(ComparisonError, match="invalid sanitized"):
        TransportDiagnosticError("http_status", http_status="400 SECRET")


@pytest.mark.parametrize("result", [
    {"model": "replacement", "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{}]},
    {"model": "synthetic-pinned", "choices": [{}]},
    response(usage={"prompt_tokens": True, "completion_tokens": 1, "prompt_tokens_details": {"cache_write_tokens": 0}}),
    response(usage={"prompt_tokens": 32769, "completion_tokens": 1, "prompt_tokens_details": {"cache_write_tokens": 0}}),
    response(usage={"prompt_tokens": 1, "completion_tokens": 101, "prompt_tokens_details": {"cache_write_tokens": 0}}),
    response(choices=[{}, {}]),
])
def test_invalid_or_excess_provider_usage_never_releases_reservation(tmp_path, result):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: result)
    with pytest.raises(ComparisonError):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert ledger.snapshot()["calls"][0]["status"] == "unknown"


def test_concurrent_call_admission_and_stage_limits(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    def reserve(i):
        try:
            ledger.reserve(str(i), "graphiti", "query", payload(str(i)))
            return True
        except ComparisonError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(16))) == 2
    assert len(ledger.snapshot()["calls"]) == 2
    assert Decimal(ledger.snapshot()["exposure_usd"]) == bound().reservation()


def test_shared_admission_lock_releases_before_parallel_backend_waits(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    held = [False]
    guards, checked, started = [], [], []
    both_started = asyncio.Event()

    @contextmanager
    def guard():
        assert not held[0]
        held[0] = True
        guards.append("enter")
        try:
            yield
        finally:
            held[0] = False
            guards.append("exit")

    def check(stage, reservation):
        assert held[0]
        checked.append((stage.stage, reservation))

    async def backend(**kwargs):
        assert not held[0]
        assert all(row["status"] == "unknown" for row in ledger.snapshot()["calls"])
        started.append(kwargs)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        return response()

    transport = MeteredChatTransport(ledger, backend=backend, admission_guard=guard,
                                    before_reservation=check)

    async def run():
        return await asyncio.wait_for(asyncio.gather(*[
            transport(framework="graphiti", stage="query", request_id=str(i), payload=payload(str(i)))
            for i in range(2)]), timeout=2)

    results = asyncio.run(run())
    assert len(started) == len(checked) == 2
    assert guards == ["enter", "exit"] * 4
    assert all(row["metering"]["status"] == "settled" for row in results)


def test_external_aggregate_admission_counts_unknown_across_concurrent_ledgers(tmp_path):
    ledgers = [MeteredCallLedger(tmp_path / f"meter-{i}.sqlite3", rolling_approval(max_calls=8)) for i in range(2)]
    gate = threading.Lock()
    backend_calls = []

    def check(stage, reservation):
        assert gate.locked()
        exposure = sum((Decimal(ledger.snapshot()["exposure_usd"]) for ledger in ledgers), Decimal(0))
        if exposure + reservation > Decimal("0.006"):
            raise ComparisonError("shared aggregate exhausted")

    def backend(**kwargs):
        backend_calls.append(kwargs)
        raise RuntimeError("synthetic unknown outcome")

    transports = [MeteredChatTransport(ledger, backend=backend, admission_guard=lambda: gate,
                                       before_reservation=check) for ledger in ledgers]

    def attempt(i):
        try:
            asyncio.run(transports[i % 2](framework="graphiti", stage="query", request_id=str(i), payload=payload(str(i))))
        except ComparisonError:
            return
        raise AssertionError("synthetic provider must not settle")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(16)))
    rows = [row for ledger in ledgers for row in ledger.snapshot()["calls"]]
    assert len(rows) == len(backend_calls) == 1 and rows[0]["status"] == "unknown"
    assert sum((Decimal(ledger.snapshot()["exposure_usd"]) for ledger in ledgers), Decimal(0)) <= Decimal("0.006")


def test_peer_contract_hold_blocks_new_dispatch_but_allows_inflight_settlement(tmp_path):
    ledgers = [MeteredCallLedger(tmp_path / f"peer-{i}.sqlite3", approval()) for i in range(2)]
    held = [False]
    started, sent = asyncio.Event(), []
    release_valid = asyncio.Event()

    @contextmanager
    def guard():
        assert not held[0]
        held[0] = True
        try:
            yield
        finally:
            held[0] = False

    original_hold = ledgers[0]._hold
    def publish_hold(reason):
        assert held[0]
        original_hold(reason)
    ledgers[0]._hold = publish_hold

    def check(stage, reservation):
        assert held[0]
        if any(ledger.snapshot()["manual_reconciliation_required"] for ledger in ledgers):
            raise ComparisonError("peer contract hold blocks new dispatch")

    async def invalid_backend(**kwargs):
        sent.append("invalid")
        if len(sent) == 2:
            started.set()
        await started.wait()
        return response(model="unapproved")

    async def valid_backend(**kwargs):
        sent.append("valid")
        if len(sent) == 2:
            started.set()
        await release_valid.wait()
        return response()

    bad = MeteredChatTransport(ledgers[0], backend=invalid_backend, admission_guard=guard,
                               before_reservation=check)
    good = MeteredChatTransport(ledgers[1], backend=valid_backend, admission_guard=guard,
                                before_reservation=check)

    async def run():
        bad_task = asyncio.create_task(bad(framework="graphiti", stage="query", request_id="bad", payload=payload()))
        good_task = asyncio.create_task(good(framework="graphiti", stage="query", request_id="good", payload=payload()))
        with pytest.raises(ComparisonError, match="not pinned"):
            await bad_task
        with pytest.raises(ComparisonError, match="peer contract hold"):
            await good(framework="graphiti", stage="query", request_id="new", payload=payload("new evidence"))
        release_valid.set()
        return await good_task

    result = asyncio.run(asyncio.wait_for(run(), timeout=2))
    assert result["metering"]["status"] == "settled"
    assert sent == ["invalid", "valid"]
    assert ledgers[0].snapshot()["calls"][0]["status"] == "unknown"
    assert len(ledgers[1].snapshot()["calls"]) == 1


def test_cached_replay_has_no_external_reservation_or_repeated_charge(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    gate = threading.Lock()
    limit, checks, sent = [Decimal("0.01")], [], []

    def check(stage, reservation):
        checks.append(reservation)
        if Decimal(ledger.snapshot()["exposure_usd"]) + reservation > limit[0]:
            raise ComparisonError("shared aggregate exhausted")

    def backend(**kwargs):
        sent.append(kwargs)
        return response()

    transport = MeteredChatTransport(ledger, backend=backend, cache_responses=True,
                                    admission_guard=lambda: gate, before_reservation=check)
    asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    limit[0] = Decimal(0)
    replay = asyncio.run(transport(framework="graphiti", stage="query", request_id="replay", payload=payload()))
    assert replay["metering"]["provider_calls"] == 0 and len(checks) == len(sent) == 1
    with pytest.raises(ComparisonError, match="cannot be rebound"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="replay", payload=payload("changed")))
    with pytest.raises(ComparisonError, match="shared aggregate"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="new", payload=payload("fresh")))
    assert len(ledger.snapshot()["calls"]) == len(sent) == 1


def test_invalid_admission_hooks_cannot_start_provider_or_leave_reservations(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError, match="explicit guard"):
        MeteredChatTransport(ledger, before_reservation=lambda *args: None)
    with pytest.raises(ComparisonError, match="context-manager factory"):
        MeteredChatTransport(ledger, admission_guard=False)

    async def asynchronous_check(*args):
        raise AssertionError("must not await an asynchronous spending callback")

    def forbidden(**kwargs):
        raise AssertionError("invalid admission must never send")

    for check in (asynchronous_check, lambda *args: False):
        transport = MeteredChatTransport(ledger, backend=forbidden, admission_guard=threading.Lock,
                                        before_reservation=check)
        with pytest.raises(ComparisonError, match="admission callback"):
            asyncio.run(transport(framework="graphiti", stage="query", request_id="bad", payload=payload()))
        assert not ledger.snapshot()["calls"]


def test_unapproved_stage_and_policy_cannot_spend(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError, match="unapproved framework stage"):
        ledger.reserve("x", "graphiti", "fact_extraction", payload())
    with pytest.raises(ComparisonError, match="sublimits exceed"):
        MeteredCallLedger(tmp_path / "bad.sqlite3", approval(aggregate_usd="0.001"))


def rolling_approval(max_calls=4):
    return approval(aggregate_usd="0.006", framework_caps=(("graphiti", "0.006"),),
                    stages=(bound(max_calls=max_calls),), admission_mode="rolling_call_v1")


def test_default_strict_identity_and_oversubscription_behavior_remain_historical():
    from dataclasses import asdict
    policy = approval()
    old_document = asdict(policy)
    old_document.pop("admission_mode")
    old_hash = hashlib.sha256(json.dumps(old_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert policy.identity() == old_hash
    assert "admission_mode" not in policy.document()
    with pytest.raises(ComparisonError, match="stage limits exceed"):
        replace(rolling_approval(), admission_mode="full_plan_v1").identity()
    assert rolling_approval().document()["admission_mode"] == "rolling_call_v1"


def test_rolling_allows_more_calls_only_after_known_cheap_settlements(tmp_path):
    policy = rolling_approval()
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", policy)
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: response())
    for i in range(4):
        result = asyncio.run(transport(framework="graphiti", stage="query", request_id=str(i), payload=payload(f"unique {i}")))
        assert result["metering"]["status"] == "settled"
        assert Decimal(ledger.snapshot()["exposure_usd"]) <= Decimal("0.006")
    assert len(ledger.snapshot()["calls"]) == 4
    with pytest.raises(ComparisonError, match="stage call"):
        ledger.reserve("over-count", "graphiti", "query", payload("another unique request"))


def test_rolling_unknown_reservations_prevent_concurrent_overspending(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", rolling_approval(max_calls=8))
    def attempt(i):
        try:
            ledger.reserve(str(i), "graphiti", "query", payload(str(i)))
            ledger.dispatched(str(i))
            return True
        except ComparisonError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1
    row = ledger.snapshot()["calls"][0]
    assert row["status"] == "unknown"
    assert Decimal(ledger.snapshot()["exposure_usd"]) <= Decimal("0.006")


def test_rolling_mode_cannot_change_an_installed_receipt(tmp_path):
    path = tmp_path / "strict.sqlite3"
    policy = approval()
    MeteredCallLedger(path, policy)
    with pytest.raises(ComparisonError, match="cannot be changed"):
        MeteredCallLedger(path, replace(policy, admission_mode="rolling_call_v1"))
    with pytest.raises(ComparisonError, match="unknown versioned"):
        replace(policy, admission_mode="unbounded").identity()


def test_concurrent_approval_installation_cannot_open_two_policies(tmp_path):
    path = tmp_path / "meter.sqlite3"
    def install(i):
        try:
            MeteredCallLedger(path, approval(approval_id=f"distinct-{i}"))
            return True
        except ComparisonError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(install, range(4))) == 1


def test_settlement_cannot_swap_rates_or_ignore_requested_output_limit(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    ledger.reserve("x", "graphiti", "query", payload())
    ledger.dispatched("x")
    with pytest.raises(ComparisonError, match="installed policy"):
        ledger.settle("x", bound(input_usd_per_token="0"), response(), cache=False)
    with pytest.raises(ComparisonError, match="token caps"):
        ledger.settle("x", bound(), response(usage={"prompt_tokens": 10, "completion_tokens": 101, "prompt_tokens_details": {"cache_write_tokens": 0}}), cache=False)
    assert ledger.snapshot()["manual_reconciliation_required"]
    with pytest.raises(ComparisonError, match="manual reconciliation"):
        ledger.reserve("y", "graphiti", "query", payload("new text"))


def test_queued_reservation_cannot_dispatch_after_another_call_overruns(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    ledger.reserve("first", "graphiti", "query", payload("first"))
    ledger.reserve("queued", "graphiti", "query", payload("second"))
    ledger.dispatched("first")
    with pytest.raises(ComparisonError, match="token caps"):
        ledger.settle("first", bound(), response(usage={"prompt_tokens": 1_000_000, "completion_tokens": 1, "prompt_tokens_details": {"cache_write_tokens": 0}}), cache=False)
    with pytest.raises(ComparisonError, match="queued call not dispatched"):
        ledger.dispatched("queued")
    assert next(row for row in ledger.snapshot()["calls"] if row["request_id"] == "queued")["status"] == "reserved"


def test_unpriced_provider_service_tier_halts_further_calls(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: response(service_tier="priority"))
    with pytest.raises(ComparisonError, match="service tier"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="x", payload=payload()))
    assert ledger.snapshot()["manual_reconciliation_required"]
    with pytest.raises(ComparisonError, match="manual reconciliation"):
        ledger.reserve("y", "graphiti", "query", payload("new evidence"))


@pytest.mark.parametrize("details", [{"cache_write_tokens": 1}, {"cache_write_tokens": True},
                                     {"cache_write_tokens": None}, "malformed"])
def test_unpriced_cache_writes_or_unverified_details_halt_further_calls(tmp_path, details):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    result = response(usage={"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": details})
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: result)
    with pytest.raises(ComparisonError, match="cache write"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="x", payload=payload()))
    assert ledger.snapshot()["manual_reconciliation_required"]


@pytest.mark.parametrize("details", [None, {}])
def test_optional_cache_counter_absence_uses_immutable_no_cache_contract(tmp_path, details):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    result = response(usage={"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": details})
    transport = MeteredChatTransport(ledger, backend=lambda **kwargs: result)
    answer = asyncio.run(transport(framework="graphiti", stage="query", request_id="x", payload=payload()))
    assert answer["metering"]["status"] == "settled"
    assert answer["metering"]["cache_write_accounting"] == "optional_counter_absent_under_explicit_no_cache_request_contract"
    assert not ledger.snapshot()["manual_reconciliation_required"]


def test_provider_optional_cache_details_can_be_entirely_omitted(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    result = response(usage={"prompt_tokens": 10, "completion_tokens": 1})
    answer = asyncio.run(MeteredChatTransport(ledger, backend=lambda **kwargs: result)(framework="graphiti", stage="query", request_id="x", payload=payload()))
    assert answer["metering"]["status"] == "settled"


def test_no_raw_request_text_in_durable_call_ledger(tmp_path):
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    ledger.reserve("x", "graphiti", "query", payload("Synthetic sensitive text not to persist"))
    assert b"Synthetic sensitive text" not in ledger.path.read_bytes()
    assert ledger.path.stat().st_mode & 0o077 == 0


def test_local_embedding_guards_window_and_exact_identity_before_encode():
    calls = []
    def encode(texts):
        calls.append(texts)
        return [[1., 0.] for _ in texts]
    with pytest.raises(ComparisonError, match="identity"):
        LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "b"}, encode=encode,
                                token_count=len, resource_guard=lambda: None, dimension=2)
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"}, encode=encode,
                                    token_count=len, resource_guard=lambda: None, dimension=2)
    assert asyncio.run(local(["two"])) == [[1., 0.]]
    with pytest.raises(ComparisonError, match="window"):
        asyncio.run(local(["x" * 257]))
    assert len(calls) == 1


def test_local_embedding_memory_guard_blocks_before_inference():
    def stop():
        raise RuntimeError("memory pressure")
    def forbidden(*args):
        raise AssertionError("must not encode")
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"}, encode=forbidden,
                                    token_count=len, resource_guard=stop, dimension=2)
    with pytest.raises(RuntimeError, match="memory"):
        asyncio.run(local(["text"]))


def test_local_memory_warning_blocks_tokenizer_before_encoder():
    calls = []
    def stop():
        calls.append("guard")
        raise RuntimeError("memory pressure warning")
    def forbidden(*args):
        raise AssertionError("neither tokenizer nor encoder may allocate")
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"},
        encode=forbidden, token_count=forbidden, resource_guard=stop, dimension=2)
    with pytest.raises(RuntimeError, match="warning"):
        asyncio.run(local(["text"]))
    assert calls == ["guard"] and local.calls == 0


def test_local_async_guard_false_blocks_tokenizer():
    async def stop():
        return False
    def forbidden(*args):
        raise AssertionError("tokenizer and encoder must not run")
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"},
        encode=forbidden, token_count=forbidden, resource_guard=stop, dimension=2)
    with pytest.raises(ComparisonError, match="tokenization"):
        asyncio.run(local(["text"]))


def test_local_text_character_and_utf8_batch_limits_precede_tokenization():
    def forbidden(*args):
        raise AssertionError("tokenizer and encoder must not run")
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"},
        encode=forbidden, token_count=forbidden, resource_guard=lambda: None, dimension=2)
    with pytest.raises(ComparisonError, match="character input bound"):
        asyncio.run(local(["x" * 2_000_001]))
    # Reuse one string: the test does not allocate 72 MiB of input copies.
    text = "\U0001f642" * 2_000_000
    with pytest.raises(ComparisonError, match="64 MiB"):
        asyncio.run(local([text] * 9))


@pytest.mark.parametrize("vector", [[[0., 0.]], [[1., float("nan")]], [[1., 0., 0.]], [], [[True, False]], [["1", "0"]]])
def test_invalid_local_embeddings_cannot_enter_index(vector):
    local = LocalEmbeddingTransport(identity={"pin": "a"}, expected_identity={"pin": "a"},
        encode=lambda texts: vector, token_count=len, resource_guard=lambda: None, dimension=2)
    with pytest.raises(ComparisonError):
        asyncio.run(local(["text"]))
