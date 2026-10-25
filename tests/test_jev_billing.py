from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import threading

import pytest

from auto_zettelkasten import jev_decisions as jd
from auto_zettelkasten.jev_billing import (
    BillingAdmissionError,
    MAX_ADMISSION_WIRE_BYTES,
    STANDARD_BILLING_POLICY,
    billing_admission,
)


@pytest.fixture(autouse=True)
def deny_provider_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("billing tests must never open a provider or network connection")

    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.socket.connect_ex", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("urllib.request.urlopen", denied)


def _request(*, suffix="", text_size=80, note_size=80):
    endpoints = []
    for side in ("a", "b"):
        text = (f"Synthetic {side} supplied evidence. " + "x" * text_size)[:text_size]
        passage = jd.EvidencePassage(
            f"{side}-1", text, {"section": "Synthetic findings"}, "author's own claim",
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        endpoints.append(jd.Endpoint(
            f"source-{side}{suffix}", f"note-{side}{suffix}", "n" * note_size,
            {"scope": "note_derived"}, {"owner": "synthetic"}, (passage,),
        ))
    return jd.DecisionRequest("synthetic-workspace", "synthetic-library", *endpoints)


def _store(tmp_path):
    return jd.DecisionStore(tmp_path / ".auto-zettelkasten" / "jev-decisions.sqlite3")


def _approved(request, **changes):
    settings = {
        "enabled": True, "api_key": "synthetic-test-only", "budget_usd": Decimal("0.01"),
        "data_sharing_confirmed": True, "paid_call_approved": True,
        "approved_billing_policy_identity": STANDARD_BILLING_POLICY.identity,
        "approved_cache_keys": (jd.decision_cache_key(request),),
    }
    return jd.JevConfig(**{**settings, **changes})


def _response(request, *, input_tokens=100):
    criteria = jd.build_payload(request)["questions"]["relationship"]["criteria"]
    choice = "supports_A_to_B__A0_B0"
    return jd.TransportResponse(200, json.dumps({
        "model": "jev-1.13.0",
        "answers": {"relationship": {
            "type": "choice", "choice": choice, "confidence": 1.0,
            "probabilities": {option: int(option == choice) for option in criteria},
        }},
        "usage": {"input_tokens": input_tokens, "output_tokens": 8},
    }).encode("utf-8"))


def test_documented_context_reservation_is_exact_and_not_an_invoice_guarantee():
    admission = billing_admission(jd.build_payload(_request()))
    assert admission.policy.model == "jev-1.13.0"
    assert admission.policy.input_usd_per_token == Decimal("0.000000042")
    assert admission.policy.input_token_ceiling == 65_536
    assert admission.max_model_usage_usd == Decimal("0.002752512")
    assert admission.max_model_usage_usd * 6 == Decimal("0.016515072")
    report = admission.to_dict()
    assert report["bound_scope"] == "documented_model_usage"
    assert report["guaranteed_invoice_cap"] is False
    assert report["wire_bytes_are_token_bound"] is False
    assert report["tokenizer_identity"] is None
    assert "Not explicitly documented" in report["error_billing"]
    assert report["currency"] == "USD"


@pytest.mark.parametrize("policy", [
    None,
    replace(STANDARD_BILLING_POLICY, input_usd_per_token=Decimal("0")),
    replace(STANDARD_BILLING_POLICY, input_usd_per_token=Decimal("sNaN")),
    replace(STANDARD_BILLING_POLICY, input_token_ceiling=32_000),
    replace(STANDARD_BILLING_POLICY, input_token_ceiling=65_536.0),
    replace(STANDARD_BILLING_POLICY, model="jev-latest"),
    replace(STANDARD_BILLING_POLICY, verified_on="unverified"),
])
def test_missing_or_modified_pricing_context_policy_fails_closed(policy):
    with pytest.raises(BillingAdmissionError, match="pinned pricing"):
        billing_admission(jd.build_payload(_request()), policy=policy)


@pytest.mark.parametrize("changes", [
    {"model": "jev-preview"},
    {"assets": {}},
    {"questions": {"relationship": {"type": "noul", "instructions": "anything"}}},
])
def test_admission_rejects_unsupported_model_or_payload(changes):
    payload = jd.build_payload(_request())
    payload.update(changes)
    with pytest.raises(BillingAdmissionError):
        billing_admission(payload)


def test_long_context_headers_other_endpoints_and_multiple_questions_are_not_admitted():
    payload = jd.build_payload(_request())
    for headers in ({"TypeSafe-Danger-Zone-Long-Context": "true"}, {"x-other-option": "true"}):
        with pytest.raises(BillingAdmissionError, match="headers"):
            billing_admission(payload, headers=headers)
    with pytest.raises(BillingAdmissionError, match="endpoint"):
        billing_admission(payload, endpoint="https://invalid.example/v1/systemone")
    payload["questions"]["second"] = payload["questions"]["relationship"]
    with pytest.raises(BillingAdmissionError, match="exactly one"):
        billing_admission(payload)


def test_oversized_valid_request_is_rejected_before_reservation_or_transport(tmp_path):
    request = _request(text_size=24_000, note_size=16_000)
    payload = jd.build_payload(request)
    assert len(json.dumps(payload).encode("utf-8")) > MAX_ADMISSION_WIRE_BYTES
    calls = []
    client = jd.JevDecisionClient(_approved(request), _store(tmp_path),
                                  transport=lambda *_a, **_kw: calls.append(1))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert "wire-byte" in result["reason"]
    assert result["provider_calls"] == 0
    assert calls == []
    with client.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize(("changes", "reason"), [
    ({"paid_call_approved": False}, "paid-call approval"),
    ({"approved_billing_policy_identity": ""}, "billing policy"),
    ({"approved_billing_policy_identity": "different-policy"}, "billing policy"),
    ({"approved_cache_keys": ()}, "allowlist"),
    ({"approved_cache_keys": ("0" * 64,)}, "allowlist"),
    ({"budget_usd": Decimal("0")}, "positive explicit"),
])
def test_runtime_approval_gates_make_no_dispatch(tmp_path, changes, reason):
    request = _request()
    calls = []
    client = jd.JevDecisionClient(_approved(request, **changes), _store(tmp_path),
                                  transport=lambda *_a, **_kw: calls.append(1))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert reason in result["reason"]
    assert result["provider_calls"] == 0
    assert calls == []


def test_price_bound_absent_at_runtime_is_blocked_before_dispatch(tmp_path):
    request = _request()
    calls = []
    client = jd.JevDecisionClient(_approved(request), _store(tmp_path), billing_policy=None,
                                  transport=lambda *_a, **_kw: calls.append(1))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert "pinned pricing" in result["reason"]
    assert calls == []


def test_full_context_is_reserved_and_settled_from_validated_usage(tmp_path):
    request = _request()
    store = _store(tmp_path)
    calls = []

    def transport(*_a, **_kw):
        calls.append(1)
        with store._connect() as db:
            assert db.execute("SELECT reserved_usd, status FROM reservations").fetchone() == ("0.002752512", "reserved")
        return _response(request)

    client = jd.JevDecisionClient(_approved(request), store, transport=transport)
    first = client.assess(request, dry_run=False)
    assert first["status"] == "review_required"
    assert first["cost_usd"] == "0.000004200"
    assert jd._BILLING_BOUND_VERIFIED is False  # Runtime approval does not edit the historical diagnostic.
    second = client.assess(request, dry_run=False)
    assert second["status"] == "cached_review_required"
    assert calls == [1]
    with store._connect() as db:
        assert db.execute("SELECT reserved_usd, actual_usd, status FROM reservations").fetchone() == (
            "0.002752512", "0.000004200", "settled",
        )


def test_budget_less_than_full_context_blocks_even_tiny_request(tmp_path):
    request = _request()
    calls = []
    client = jd.JevDecisionClient(_approved(request, budget_usd=Decimal("0.002752511")), _store(tmp_path),
                                  transport=lambda *_a, **_kw: calls.append(1))
    assert client.assess(request, dry_run=False)["status"] == "blocked"
    assert calls == []


@pytest.mark.parametrize("outcome", ["timeout", "429", "500", "oversize_usage"])
def test_unknown_or_error_billing_keeps_full_reservation_without_retry(tmp_path, outcome):
    request = _request()
    calls = []

    def transport(*_a, **_kw):
        calls.append(1)
        if outcome == "timeout":
            raise TimeoutError("synthetic unknown outcome")
        if outcome == "oversize_usage":
            return _response(request, input_tokens=65_537)
        return jd.TransportResponse(int(outcome), b"{}")

    client = jd.JevDecisionClient(_approved(request, max_tokens=65_536), _store(tmp_path), transport=transport)
    first = client.assess(request, dry_run=False)
    assert first["status"] in {"billing_uncertain", "provider_error", "invalid_response"}
    second = client.assess(request, dry_run=False)
    assert second["status"] == "blocked"
    assert calls == [1]
    with client.store._connect() as db:
        assert db.execute("SELECT reserved_usd, actual_usd, status FROM reservations").fetchone() == (
            "0.002752512", None, "uncertain",
        )


def test_full_context_reservations_are_atomic_under_competition(tmp_path):
    store = _store(tmp_path)
    amount = Decimal("0.002752512")
    barrier = threading.Barrier(3)
    admitted = []

    def reserve(key):
        barrier.wait()
        try:
            admitted.append(store.reserve(key, amount, amount))
        except jd.JevDecisionError:
            admitted.append(None)

    workers = [threading.Thread(target=reserve, args=(f"synthetic-{index}",)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert sum(token is not None for token in admitted) == 1


def test_approval_identity_is_bounded_and_no_retries_are_supported():
    for changes in ({"paid_call_approved": "yes"}, {"approved_cache_keys": ("not-a-hash",)},
                    {"approved_cache_keys": ("0" * 64, "0" * 64)}, {"max_retries": 1}):
        with pytest.raises(ValueError):
            _approved(_request(), **changes).validate()
    assert jd.JevConfig().paid_call_approved is False
    assert jd.JevConfig().approved_billing_policy_identity == ""
    assert jd.JevConfig().approved_cache_keys == ()


def test_http_redirects_are_not_followed():
    assert jd._NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://invalid.example") is None


def test_validated_usage_with_artifact_write_failure_reports_settled_cost(tmp_path, monkeypatch):
    request = _request()
    store = _store(tmp_path)
    calls = []

    def transport(*_a, **_kw):
        calls.append(1)
        return _response(request)

    def fail_to_persist(*_a, **_kw):
        raise OSError("synthetic artifact persistence failure")

    monkeypatch.setattr(store, "put", fail_to_persist)
    client = jd.JevDecisionClient(_approved(request), store, transport=transport)
    first = client.assess(request, dry_run=False)
    assert first["status"] == "persistence_failed"
    assert first["cost_usd"] == "0.000004200"
    assert first["accounting_status"] == "settled"
    assert "reservation_usd" not in first
    assert "decision" not in first
    with store._connect() as db:
        assert db.execute("SELECT reserved_usd, actual_usd, status FROM reservations").fetchone() == (
            "0.002752512", "0.000004200", "settled",
        )
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert client.assess(request, dry_run=False)["status"] == "blocked"
    assert calls == [1]


def test_deep_metadata_is_rejected_before_reservation_or_transport(tmp_path):
    request = _request()
    nested = "leaf"
    for _ in range(70):
        nested = {"nested": nested}
    request = replace(request, endpoint_a=replace(request.endpoint_a, coverage={"scope": "note_derived", "details": nested}))
    calls = []
    client = jd.JevDecisionClient(_approved(request), _store(tmp_path),
                                  transport=lambda *_a, **_kw: calls.append(1))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert "nesting" in result["reason"]
    assert result["provider_calls"] == 0
    assert calls == []
    with client.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_nesting_cap_conservatively_counts_root_container_as_first_level():
    payload = jd.build_payload(_request())
    for count in (63, 64):
        nested = "leaf"
        for _ in range(count):
            nested = {"nested": nested}
        payload["state"] = nested
        assert billing_admission(payload).wire_bytes > 0
    payload["state"] = {"extra": payload["state"]}
    with pytest.raises(BillingAdmissionError, match="64-level"):
        billing_admission(payload)


def test_historical_billing_contracts_require_explicit_old_template_and_keep_bounds():
    from auto_zettelkasten.jev_billing import (
        HISTORICAL_STANDARD_BILLING_POLICY_V3, HISTORICAL_BATCH_BILLING_POLICY_V2,
        BATCH_BILLING_POLICY, batch_billing_admission,
    )
    payload = {"model": "jev-1.13.0", "state": {"synthetic": True},
               "questions": {"relationship": {"type": "choice", "instructions": "Choose using synthetic evidence.",
                                               "criteria": {"none": "No connection", "related": "Related"}}}}
    for old, current, admit in [
        (HISTORICAL_STANDARD_BILLING_POLICY_V3, STANDARD_BILLING_POLICY, billing_admission),
        (HISTORICAL_BATCH_BILLING_POLICY_V2, BATCH_BILLING_POLICY, batch_billing_admission),
    ]:
        assert old.identity != current.identity
        assert old.template_version != current.template_version
        assert old.model == current.model == "jev-1.13.0"
        assert old.input_token_ceiling == current.input_token_ceiling == 65_536
        assert old.max_wire_bytes == current.max_wire_bytes == 65_536
        assert old.input_usd_per_token == current.input_usd_per_token == Decimal("0.000000042")
        with pytest.raises(BillingAdmissionError, match="template"):
            admit(payload, policy=old)
        admission = admit(payload, policy=old, template_version=old.template_version)
        assert admission.max_model_usage_usd == Decimal("0.002752512")


def test_previous_billing_approval_does_not_dispatch_new_recall_request(tmp_path):
    from auto_zettelkasten.jev_billing import HISTORICAL_STANDARD_BILLING_POLICY_V3
    request = _request()
    client = jd.JevDecisionClient(
        _approved(request, approved_billing_policy_identity=HISTORICAL_STANDARD_BILLING_POLICY_V3.identity),
        _store(tmp_path), transport=lambda *_a, **_kw: pytest.fail("old approval must not dispatch new rubric"),
    )
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert result["provider_calls"] == 0
    with client.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
