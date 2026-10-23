from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from decimal import Decimal

import pytest

from auto_zettelkasten import jev_decisions as jd
from auto_zettelkasten.jev_billing import STANDARD_BILLING_POLICY


@pytest.fixture(autouse=True)
def deny_provider_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline Jev tests must never use provider network transport")
    monkeypatch.setattr("urllib.request.urlopen", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("socket.socket.connect", denied)


def _passage(pid: str, text: str) -> jd.EvidencePassage:
    return jd.EvidencePassage(pid, text, {"page": 3}, "author's own claim", hashlib.sha256(text.encode()).hexdigest())


def _request(*, b_text: str = "Policy B reports a different outcome.", b_id: str = "b-1") -> jd.DecisionRequest:
    return jd.DecisionRequest(
        workspace_id="workspace-synthetic", library_id="library-synthetic",
        endpoint_a=jd.Endpoint("source-a", "note-a", "A concise synthetic note.",
                               {"scope": "partial"}, {"owner": "managed"},
                               (_passage("a-1", "Policy A reduced the measured outcome."),)),
        endpoint_b=jd.Endpoint("source-b", "note-b", "B concise synthetic note.",
                               {"scope": "abstract_only"}, {"owner": "managed"},
                               (_passage(b_id, b_text),)),
    )


def _store(tmp_path):
    return jd.DecisionStore(tmp_path / ".auto-zettelkasten" / "jev-decisions.sqlite3")


def _approval(request):
    return {
        "paid_call_approved": True,
        "approved_billing_policy_identity": STANDARD_BILLING_POLICY.identity,
        "approved_cache_keys": (jd.decision_cache_key(request),),
    }


def _response(req: jd.DecisionRequest, *, choice="undermines_A_to_B__A0_B0"):
    questions = jd._relationship_questions(req)
    answers = {}
    for name, spec in questions.items():
        options = list(spec["criteria"])
        selection = choice
        answers[name] = {"type": "choice", "choice": selection,
                         "probabilities": {option: (1.0 if option == selection else 0.0) for option in options},
                         "confidence": 0.99}
    return jd.TransportResponse(200, json.dumps({"model": jd.MODEL_ID, "answers": answers,
                                                "usage": {"input_tokens": 100, "output_tokens": 16}}).encode())


def test_dry_run_default_is_disabled_and_never_calls_transport(tmp_path):
    calls = []
    req = _request()
    client = jd.JevDecisionClient(jd.JevConfig(api_key="ambient-secret"), _store(tmp_path),
                                  transport=lambda *a, **kw: calls.append((a, kw)))
    result = client.assess(req)
    assert result["status"] == "dry_run"
    assert result["provider_calls"] == 0
    assert result["payload"]["model"] == "jev-1.13.0"
    assert list(result["payload"]["questions"]) == ["relationship"]
    assert result["payload"]["state"]["endpoints"]["A"]["passages"][0]["text"] == "Policy A reduced the measured outcome."
    assert calls == []


def test_disabled_zero_budget_and_unapproved_calls_do_not_dispatch(tmp_path):
    calls = []
    req = _request()
    client = jd.JevDecisionClient(jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"),
                                                data_sharing_confirmed=True), _store(tmp_path),
                                  transport=lambda *a, **kw: calls.append((a, kw)))
    result = client.assess(req, dry_run=False)
    assert result["status"] == "blocked"
    assert "approval" in result["reason"]
    assert calls == []
    zero = jd.JevDecisionClient(jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("0"),
                                              data_sharing_confirmed=True), _store(tmp_path / "zero"),
                                transport=lambda *a, **kw: calls.append((a, kw)))
    assert zero.assess(req, dry_run=False)["reason"] == "positive explicit Jev spend budget is required"
    assert calls == []


def test_cache_key_binds_order_text_template_and_option_order():
    req = _request()
    key = jd.decision_cache_key(req)
    swapped = jd.DecisionRequest(req.workspace_id, req.library_id, req.endpoint_b, req.endpoint_a)
    changed = _request(b_text="Policy B's qualified result differs.")
    assert jd.decision_cache_key(swapped) != key
    assert jd.decision_cache_key(changed) != key
    with pytest.raises(ValueError, match="template"):
        jd.decision_cache_key(replace(req, template_version="relationship-decision-v1"))
    body = jd.build_payload(req)
    original = list(body["questions"]["relationship"]["criteria"])
    reversed_criteria = dict(reversed(list(body["questions"]["relationship"]["criteria"].items())))
    questions = dict(body["questions"])
    questions["relationship"] = {**questions["relationship"], "criteria": reversed_criteria}
    altered = jd.DecisionRequest(req.workspace_id, req.library_id, req.endpoint_a, req.endpoint_b, questions=questions)
    assert list(jd.build_payload(altered)["questions"]["relationship"]["criteria"]) == list(reversed(original))
    assert jd.decision_cache_key(altered) != key


def test_mock_response_is_review_artifact_and_repeat_cache_hit(tmp_path, monkeypatch):
    req = _request()
    calls = []

    def transport(body, *, api_key, timeout):
        calls.append((json.loads(body), api_key, timeout))
        return _response(req)

    config = jd.JevConfig(enabled=True, api_key="synthetic-test-key", budget_usd=Decimal("0.01"),
                          data_sharing_confirmed=True, **_approval(req))
    store = _store(tmp_path)
    client = jd.JevDecisionClient(config, store, transport=transport)
    first = client.assess(req, dry_run=False)
    second = client.assess(req, dry_run=False)
    assert first["status"] == "review_required"
    assert first["decision"]["answers"]["relationship"]["choice"] == "undermines_A_to_B__A0_B0"
    assert first["decision"]["relationship"]["relation_type"] == "undermines"
    assert first["decision"]["selected_evidence"][0]["text"] == "Policy A reduced the measured outcome."
    assert first["decision"]["review"] == {"reviewed": False, "accepted": False}
    assert len(first["decision"]["selected_evidence"]) == 2
    assert [row["passage_id"] for row in first["decision"]["selected_evidence"]] == ["a-1", "b-1"]
    assert second["status"] == "cached_review_required"
    assert second["cost_usd"] == "0"
    assert len(calls) == 1
    assert calls[0][1] == "synthetic-test-key"


@pytest.mark.parametrize("choice", [
    "supports_symmetric__A0_B0",
    "supports_A_to_B__A0_B9",
    "undermines_A_to_B__A1_B0",
])
def test_inconsistent_or_invalid_mock_responses_are_not_cached(tmp_path, monkeypatch, choice):
    req = _request()
    client = jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"), data_sharing_confirmed=True,
                     **_approval(req)),
        _store(tmp_path), transport=lambda *a, **kw: _response(req, choice=choice),
    )
    result = client.assess(req, dry_run=False)
    assert result["status"] == "invalid_response"
    assert client.store.get(result["cache_key"], model=jd.MODEL_ID, template_version=req.template_version,
                            payload_sha256=hashlib.sha256(jd._canonical_json(jd.build_payload(req)).encode()).hexdigest()) is None


def test_transport_exception_retains_uncertain_reservation(tmp_path, monkeypatch):
    req = _request()
    client = jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"), data_sharing_confirmed=True,
                     **_approval(req)),
        _store(tmp_path), transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()),
    )
    result = client.assess(req, dry_run=False)
    assert result["status"] == "billing_uncertain"
    with client.store._connect() as db:
        status, reserved = db.execute("SELECT status, reserved_usd FROM reservations WHERE cache_key=?", (result["cache_key"],)).fetchone()
    assert status == "uncertain"
    assert Decimal(reserved) > 0


@pytest.mark.parametrize("usage", [
    {"input_tokens": 10},
    {"input_tokens": -1, "output_tokens": 0},
    {"input_tokens": 9_000, "output_tokens": 0},
])
def test_usage_must_be_complete_nonnegative_and_within_configured_limit(tmp_path, monkeypatch, usage):
    req = _request()
    response = json.loads(_response(req).body)
    response["usage"] = usage
    client = jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"), data_sharing_confirmed=True,
                     max_tokens=8_000, **_approval(req)),
        _store(tmp_path), transport=lambda *a, **kw: jd.TransportResponse(200, json.dumps(response).encode()),
    )
    assert client.assess(req, dry_run=False)["status"] == "invalid_response"


def test_corrupt_cache_artifact_is_rejected_and_settled_reservation_blocks_rebill(tmp_path, monkeypatch):
    req = _request()
    store = _store(tmp_path)
    client = jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"), data_sharing_confirmed=True,
                     **_approval(req)),
        store, transport=lambda *a, **kw: _response(req),
    )
    result = client.assess(req, dry_run=False)
    payload_hash = hashlib.sha256(jd._canonical_json(jd.build_payload(req)).encode()).hexdigest()
    with store._transaction() as db:
        db.execute("UPDATE decisions SET artifact_json=? WHERE cache_key=?", ('{"status":"review_required"}', result["cache_key"]))
    assert store.get(result["cache_key"], model=jd.MODEL_ID, template_version=req.template_version,
                     payload_sha256=payload_hash) is None
    assert client.assess(req, dry_run=False)["status"] == "blocked"


def test_tampered_cached_evidence_is_not_returned_for_review(tmp_path, monkeypatch):
    req = _request()
    store = _store(tmp_path)
    client = jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic", budget_usd=Decimal("1"), data_sharing_confirmed=True,
                     **_approval(req)),
        store, transport=lambda *a, **kw: _response(req),
    )
    result = client.assess(req, dry_run=False)
    with store._transaction() as db:
        artifact = json.loads(db.execute(
            "SELECT artifact_json FROM decisions WHERE cache_key=?", (result["cache_key"],)
        ).fetchone()[0])
        artifact["selected_evidence"][0]["text"] = "forged text"
        db.execute(
            "UPDATE decisions SET artifact_json=? WHERE cache_key=?",
            (json.dumps(artifact), result["cache_key"]),
        )
    repeat = client.assess(req, dry_run=False)
    assert repeat["status"] == "blocked"
    assert "cached" in repeat["reason"]


def test_coverage_change_invalidates_cache_identity():
    req = _request()
    changed_a = replace(req.endpoint_a, coverage={"scope": "full_document"})
    changed = replace(req, endpoint_a=changed_a)
    assert jd.decision_cache_key(changed) != jd.decision_cache_key(req)


def test_decimal_budget_reservations_are_atomic_under_competition(tmp_path):
    store = _store(tmp_path)
    barrier = threading.Barrier(3)
    results = []

    def reserve(key):
        barrier.wait()
        try:
            results.append(store.reserve(key, Decimal("0.60"), Decimal("1.00")))
        except jd.JevDecisionError:
            results.append(None)

    workers = [threading.Thread(target=reserve, args=(f"key-{index}",)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert sum(item is not None for item in results) == 1


def test_invalid_pins_and_unhashed_evidence_fail_before_transport(tmp_path):
    with pytest.raises(ValueError, match="pinned"):
        jd.JevConfig(model="jev-latest").validate()
    req = _request()
    bad_passage = replace(req.endpoint_a.passages[0], content_sha256="0" * 64)
    broken = jd.Endpoint(req.endpoint_a.source_id, req.endpoint_a.note_id, req.endpoint_a.note_text,
                         req.endpoint_a.coverage, req.endpoint_a.ownership,
                         (bad_passage,))
    invalid = jd.DecisionRequest(req.workspace_id, req.library_id, broken, req.endpoint_b)
    with pytest.raises(ValueError, match="hash"):
        jd.build_payload(invalid)
