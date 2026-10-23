from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import threading

import pytest

from auto_zettelkasten import jev_batches as jb
from auto_zettelkasten import jev_decisions as jd
from auto_zettelkasten.jev_billing import (
    BATCH_BILLING_POLICY, BillingAdmissionError, batch_billing_admission, billing_admission,
)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("batch tests cannot use the network")
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("socket.socket.connect", denied)


def _endpoint(letter):
    text = f"Claim {letter} is conditional on the observed population."
    passage = jd.EvidencePassage(f"passage-{letter}", text,
                                {"note_id": f"note-{letter}", "note_relative_path": f"notes/{letter}.md",
                                 "heading": "Thesis", "original_document_locator_verified": False},
                                "A supplied note paraphrase, not an original quotation.", hashlib.sha256(text.encode()).hexdigest())
    return jd.Endpoint(f"source-{letter}", f"note-{letter}",
                       f"# Source {letter}\n## Thesis\n{text}\n## Limitations\nObservation is not causal proof.",
                       {"source_scope": "note_only", "reason": "original not supplied"},
                       {"status": "unresolved", "warning": "A quotation need not be endorsement."}, (passage,))


def _pairs():
    anchor = _endpoint("a")
    return tuple(jd.DecisionRequest("synthetic-workspace", "synthetic-library", anchor, _endpoint(letter))
                 for letter in ("b", "c", "d"))


def _store(tmp_path):
    return jd.DecisionStore(tmp_path / ".auto-zettelkasten/jev-decisions.sqlite3")


def _config(batches, **overrides):
    values = dict(enabled=True, api_key="synthetic-secret", budget_usd=Decimal("0.05"),
                  data_sharing_confirmed=True, paid_call_approved=True,
                  approved_billing_policy_identity=BATCH_BILLING_POLICY.identity,
                  approved_cache_keys=tuple(jb.batch_cache_key(row) for row in batches), max_tokens=65536)
    return jd.JevConfig(**{**values, **overrides})


def _response(payload, *, choice=None, confidence=1.0):
    answers = {}
    for key, spec in payload["questions"].items():
        selected = choice or next(iter(spec["criteria"]))
        answers[key] = {"type": "choice", "choice": selected,
                        "probabilities": {option: float(option == selected) for option in spec["criteria"]},
                        "confidence": confidence}
    return jd.TransportResponse(200, json.dumps({"model": jd.MODEL_ID, "answers": answers,
                                               "usage": {"input_tokens": 1000, "output_tokens": 60}}).encode())


def test_full_isolated_uses_exact_legacy_payload_and_compact_keeps_semantics():
    pair = _pairs()[0]
    assert jb.build_batch_payload(jb.BatchRequest((pair,))) == jd.build_payload(pair)
    payload = jb.build_batch_payload(jb.BatchRequest((pair,), packet_format="compact"))
    assert payload["questions"] == jd.build_payload(pair)["questions"]
    endpoint = payload["state"]["endpoints"]["A"]
    assert pair.endpoint_a.passages[0].text not in endpoint["note_text"]
    assert "Observation is not causal proof." in endpoint["note_text"]
    assert endpoint["passages"][0]["text"] == pair.endpoint_a.passages[0].text
    assert endpoint["ownership"] == pair.endpoint_a.ownership
    assert endpoint["coverage"] == pair.endpoint_a.coverage
    assert endpoint["passages"][0]["attribution"] == pair.endpoint_a.passages[0].attribution
    assert endpoint["passages"][0]["locator"]["original_document_locator_verified"] is False
    assert "content_sha256" not in endpoint["passages"][0]
    assert "note_relative_path" not in endpoint["passages"][0]["locator"]


@pytest.mark.parametrize("packet_format", ["full", "compact"])
def test_shared_single_and_multi_questions_see_identical_deduplicated_state(packet_format):
    pairs = _pairs()
    one = jb.build_batch_payload(jb.BatchRequest((pairs[0],), pairs, packet_format))
    all_pairs = jb.build_batch_payload(jb.BatchRequest(pairs, pairs, packet_format))
    assert one["state"] == all_pairs["state"]
    assert len(one["state"]["notes"]) == 4
    assert len(one["questions"]) == 1
    assert len(all_pairs["questions"]) == 3
    question = next(iter(one["questions"]))
    assert one["questions"][question] == all_pairs["questions"][question]
    assert len(all_pairs["state"]["comparisons"]) == 3


@pytest.mark.parametrize("change", ["reverse_duplicate", "conflicting_note", "missing_pair", "scope", "nonstar", "too_many", "bad_format"])
def test_invalid_context_rejected(change):
    pairs = _pairs()
    batch = jb.BatchRequest(pairs)
    if change == "reverse_duplicate":
        reverse = replace(pairs[0], endpoint_a=pairs[0].endpoint_b, endpoint_b=pairs[0].endpoint_a)
        batch = jb.BatchRequest((pairs[0], reverse))
    elif change == "conflicting_note":
        changed = replace(pairs[1], endpoint_a=replace(pairs[1].endpoint_a, note_text="Different text"))
        batch = jb.BatchRequest((pairs[0], changed))
    elif change == "missing_pair":
        batch = jb.BatchRequest((pairs[1],), (pairs[0],))
    elif change == "scope":
        batch = jb.BatchRequest((pairs[0], replace(pairs[1], library_id="other")))
    elif change == "nonstar":
        batch = jb.BatchRequest((pairs[0], replace(pairs[1], endpoint_a=_endpoint("e"))))
    elif change == "too_many":
        batch = jb.BatchRequest((*pairs, replace(pairs[0], endpoint_b=_endpoint("e"))))
    else:
        batch = replace(batch, packet_format="other")
    with pytest.raises(ValueError):
        jb.build_batch_payload(batch)


def test_batch_billing_is_separate_and_reserves_full_context():
    payload = jb.build_batch_payload(jb.BatchRequest(_pairs()))
    with pytest.raises(BillingAdmissionError, match="exactly one"):
        billing_admission(payload)
    admission = batch_billing_admission(payload)
    assert admission.max_model_usage_usd == Decimal("0.002752512")
    assert admission.to_dict()["wire_bytes_are_token_bound"] is False
    assert "three" in admission.to_dict()["assumptions"][0]
    for headers in [{"TypeSafe-Danger-Zone-Long-Context": "true"}, {"Content-Encoding": "gzip"}]:
        with pytest.raises(BillingAdmissionError):
            batch_billing_admission(payload, headers=headers)
    with pytest.raises(BillingAdmissionError):
        batch_billing_admission({**payload, "model": "jev-latest"})
    with pytest.raises(BillingAdmissionError):
        batch_billing_admission(payload, policy=None)


@pytest.mark.parametrize("policy", [
    replace(BATCH_BILLING_POLICY, input_usd_per_token=Decimal("sNaN")),
    replace(BATCH_BILLING_POLICY, input_usd_per_token=Decimal("0")),
    replace(BATCH_BILLING_POLICY, input_token_ceiling=65536.0),
    replace(BATCH_BILLING_POLICY, max_wire_bytes=65536.0),
    replace(BATCH_BILLING_POLICY, model="jev-latest"),
])
def test_modified_batch_policy_fails_closed(policy):
    with pytest.raises(BillingAdmissionError):
        batch_billing_admission(jb.build_batch_payload(jb.BatchRequest(_pairs())), policy=policy)


def test_shared_question_retains_explicit_option_order():
    pairs = _pairs()
    question = jd.build_payload(pairs[0])["questions"]["relationship"]
    question["criteria"] = dict(reversed(list(question["criteria"].items())))
    changed = replace(pairs[0], questions={"relationship": question})
    batch = jb.BatchRequest((changed,), (changed, *pairs[1:]))
    rendered = next(iter(jb.build_batch_payload(batch)["questions"].values()))
    assert list(rendered["criteria"]) == list(question["criteria"])


def test_default_dry_run_never_dispatches(tmp_path):
    calls = []
    client = jb.JevBatchClient(jd.JevConfig(), _store(tmp_path), transport=lambda *a, **k: calls.append(a))
    result = client.assess(jb.BatchRequest(_pairs()))
    assert result["status"] == "dry_run" and result["provider_calls"] == 0
    assert len(result["payload"]["questions"]) == 3 and calls == []


@pytest.mark.parametrize("override", [
    {"enabled": False}, {"data_sharing_confirmed": False}, {"paid_call_approved": False},
    {"budget_usd": Decimal("0")}, {"api_key": None}, {"approved_cache_keys": ()},
    {"approved_billing_policy_identity": "old-single-policy"},
])
def test_live_dispatch_needs_all_approvals(tmp_path, override):
    batch = jb.BatchRequest(_pairs())
    calls = []
    client = jb.JevBatchClient(_config([batch], **override), _store(tmp_path), transport=lambda *a, **k: calls.append(a))
    assert client.assess(batch, dry_run=False)["status"] == "blocked"
    assert calls == []


def test_batch_artifacts_bind_each_pair_evidence_and_account_cost_once(tmp_path):
    batch = jb.BatchRequest(_pairs())
    payload = jb.build_batch_payload(batch)
    calls = []
    def transport(body, **kwargs):
        calls.append(body)
        return _response(json.loads(body), choice="qualifies_A_to_B__A0_B0")
    client = jb.JevBatchClient(_config([batch]), _store(tmp_path), transport=transport)
    result = client.assess(batch, dry_run=False)
    assert result["status"] == "review_required" and result["provider_calls"] == 1
    assert len(result["decisions"]) == 3
    assert Decimal(result["cost_usd"]) == Decimal("0.000042")
    assert sum(Decimal(row["provenance"]["cost_usd"]) for row in result["decisions"]) == Decimal(result["cost_usd"])
    for decision, pair in zip(result["decisions"], batch.requests):
        assert decision["endpoints"]["A"]["source_id"] == pair.endpoint_a.source_id
        assert decision["endpoints"]["B"]["source_id"] == pair.endpoint_b.source_id
        assert [row["passage_id"] for row in decision["selected_evidence"]] == [pair.endpoint_a.passages[0].passage_id, pair.endpoint_b.passages[0].passage_id]
        assert decision["selected_evidence"][0]["content_sha256"] == pair.endpoint_a.passages[0].content_sha256
        assert decision["provenance"]["pair_request_sha256"] == hashlib.sha256(jd._canonical_json(jd.build_payload(pair)).encode()).hexdigest()
        assert decision["review"] == {"reviewed": False, "accepted": False}
    assert json.loads(calls[0]) == payload
    replay = client.assess(batch, dry_run=False)
    assert replay["cache_hit"] is True and replay["provider_calls"] == 0 and replay["cost_usd"] == "0"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["extra_answer", "missing_answer", "wrong_winner", "confidence", "model", "nan"])
def test_invalid_batch_results_retain_reserve_and_never_cache(tmp_path, change):
    batch = jb.BatchRequest(_pairs())
    response = json.loads(_response(jb.build_batch_payload(batch)).body)
    name = next(iter(response["answers"]))
    if change == "extra_answer":
        response["answers"]["unrequested_pair"] = response["answers"][name]
    elif change == "missing_answer":
        response["answers"].pop(name)
    elif change == "wrong_winner":
        response["answers"][name]["choice"] = "insufficient_evidence"
    elif change == "confidence":
        response["answers"][name]["confidence"] = 0.1
    elif change == "model":
        response["model"] = "jev-1.14.0"
    else:
        response["answers"][name]["probabilities"]["insufficient_evidence"] = float("nan")
    store = _store(tmp_path)
    client = jb.JevBatchClient(_config([batch]), store,
                               transport=lambda *a, **k: jd.TransportResponse(200, json.dumps(response).encode()))
    result = client.assess(batch, dry_run=False)
    assert result["status"] == "invalid_response"
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM batch_decisions").fetchone()[0] == 0
        assert db.execute("SELECT status FROM reservations").fetchone()[0] == "uncertain"
    assert client.assess(batch, dry_run=False)["status"] == "blocked"


def test_ties_and_rounded_confidence_are_preserved_for_later_policy(tmp_path):
    batch = jb.BatchRequest((_pairs()[0],))
    response = json.loads(_response(jb.build_batch_payload(batch)).body)
    answer = response["answers"]["relationship"]
    answer["probabilities"] = {key: 0.0 for key in answer["probabilities"]}
    answer["probabilities"].update(topical_only_or_none=0.5, insufficient_evidence=0.5)
    answer["confidence"] = 0.45
    client = jb.JevBatchClient(_config([batch]), _store(tmp_path),
                               transport=lambda *a, **k: jd.TransportResponse(200, json.dumps(response).encode()))
    assert client.assess(batch, dry_run=False)["status"] == "review_required"


def test_execution_id_repeats_wire_without_clearing_cache_or_ledger(tmp_path):
    first = jb.BatchRequest(_pairs(), execution_id="trial-1")
    second = replace(first, execution_id="trial-2")
    calls = []
    def transport(body, **kwargs):
        calls.append(body)
        return _response(json.loads(body))
    store = _store(tmp_path)
    client = jb.JevBatchClient(_config([first, second]), store, transport=transport)
    one = client.assess(first, dry_run=False)
    two = client.assess(second, dry_run=False)
    assert one["cache_key"] != two["cache_key"]
    assert one["exact_request_key"] == two["exact_request_key"]
    assert calls[0] == calls[1]
    assert client.assess(first, dry_run=False)["cache_hit"] is True
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 2


def test_changed_context_changes_exact_cache_and_mutated_cache_cannot_rebill(tmp_path):
    pairs = _pairs()
    first = jb.BatchRequest((pairs[0],), pairs)
    changed = replace(pairs[2], endpoint_b=replace(pairs[2].endpoint_b, note_text="A changed contextual note."))
    assert jb.batch_cache_key(first) != jb.batch_cache_key(replace(first, context_requests=(*pairs[:2], changed)))
    store = _store(tmp_path)
    calls = []
    def transport(body, **kwargs):
        calls.append(body)
        return _response(json.loads(body))
    client = jb.JevBatchClient(_config([first]), store, transport=transport)
    result = client.assess(first, dry_run=False)
    with store._transaction() as db:
        artifact = json.loads(db.execute("SELECT artifact_json FROM batch_decisions").fetchone()[0])
        artifact["decisions"][0]["endpoints"]["A"]["source_id"] = "fabricated"
        db.execute("UPDATE batch_decisions SET artifact_json=?", (json.dumps(artifact),))
    assert client.assess(first, dry_run=False)["status"] == "blocked"
    assert len(calls) == 1


def test_concurrent_calls_share_aggregate_budget_and_deduplicate_execution(tmp_path):
    first = jb.BatchRequest((_pairs()[0],), execution_id="one")
    second = replace(first, execution_id="two")
    start = threading.Barrier(2)
    calls = []
    def transport(body, **kwargs):
        calls.append(body)
        raise TimeoutError("synthetic transport uncertainty")
    config = _config([first, second], budget_usd=Decimal("0.003"))
    client = jb.JevBatchClient(config, _store(tmp_path), transport=transport)
    def run(batch):
        start.wait()
        return client.assess(batch, dry_run=False)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [first, second]))
    assert len(calls) == 1
    assert sorted(row["status"] for row in results) == ["billing_uncertain", "blocked"]


def test_single_and_batch_clients_share_the_same_spending_reservations(tmp_path):
    pair = _pairs()[0]
    store = _store(tmp_path)
    store.reserve(jd.decision_cache_key(pair), Decimal("0.002752512"), Decimal("0.003"))
    batch = jb.BatchRequest((pair,))
    client = jb.JevBatchClient(_config([batch], budget_usd=Decimal("0.003")), store,
                               transport=lambda *a, **k: pytest.fail("budget must block dispatch"))
    assert client.assess(batch, dry_run=False)["status"] == "blocked"


def test_caller_mutation_after_snapshot_does_not_change_evidence(tmp_path):
    batch = jb.BatchRequest(_pairs())
    original = batch.requests[0].endpoint_a.passages[0].content_sha256
    def transport(body, **kwargs):
        batch.requests[0].endpoint_a.coverage["source_scope"] = "fabricated"
        batch.requests[0].endpoint_a.passages[0].locator["heading"] = "fabricated"
        return _response(json.loads(body), choice="supports_A_to_B__A0_B0")
    client = jb.JevBatchClient(_config([batch]), _store(tmp_path), transport=transport)
    result = client.assess(batch, dry_run=False)
    assert result["decisions"][0]["endpoints"]["A"]["coverage"]["source_scope"] == "note_only"
    assert result["decisions"][0]["selected_evidence"][0]["locator"]["heading"] == "Thesis"
    assert result["decisions"][0]["selected_evidence"][0]["content_sha256"] == original
