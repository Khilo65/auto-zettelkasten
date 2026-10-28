from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import threading
import time

import pytest

from auto_zettelkasten import jev_graph_value as gv
from auto_zettelkasten import jev_decisions as jd
from auto_zettelkasten.jev_billing import (
    BillingAdmissionError, BATCH_BILLING_POLICY, GRAPH_VALUE_BILLING_POLICY,
    GRAPH_VALUE_TEMPLATE_VERSION, billing_admission, batch_billing_admission,
    graph_value_billing_admission,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("graph-value tests must not access a provider")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


def endpoint(letter):
    text = f"Study {letter} finds a conditional association, not causal proof."
    passage = jd.EvidencePassage(
        f"passage-{letter}", text, {"heading": "Thesis", "original_document_locator_verified": False},
        "Saved note paraphrase, not an original publication quotation.", hashlib.sha256(text.encode()).hexdigest(),
    )
    return jd.Endpoint(f"source-{letter}", f"note-{letter}", f"# Study {letter}\n{text}",
                       {"source_scope": "note_only"}, {"status": "owned"}, (passage,))


def request(condition="C", **kw):
    pair = jd.DecisionRequest("synthetic-workspace", "synthetic-library", endpoint("a"), endpoint("b"))
    return gv.GraphValueRequest(pair, (endpoint("c"), endpoint("d")), condition, **kw)


def response(req, graph_value="helpful", *, relation=None, tie_value=False, tie_relation=False):
    payload = gv.build_graph_value_payload(req)
    answers = {}
    for name, question in payload["questions"].items():
        options = list(question["criteria"])
        selected = graph_value if name == "graph_value" else relation or next(x for x in options if x.startswith("complements"))
        probs = {x: float(x == selected) for x in options}
        if (name == "graph_value" and tie_value) or (name == "relationship" and tie_relation):
            other = next(x for x in options if x != selected)
            probs[selected] = probs[other] = .5
        top = max(probs.values()); n = len(probs)
        answers[name] = {"type": "choice", "choice": selected, "probabilities": probs,
                         "confidence": (top - 1/n)/(1-1/n)}
    return {"model": jd.MODEL_ID, "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 50}}


def body(req, *args, **kw):
    return json.dumps(response(req, *args, **kw)).encode()


def artifact(req, *args, **kw):
    return gv.validate_graph_value_response(req, body(req, *args, **kw))


def store(tmp_path):
    return jd.DecisionStore(tmp_path / ".auto-zettelkasten/jev-decisions.sqlite3")


def config(requests, **kw):
    return jd.JevConfig(**{
        "enabled": True, "api_key": "synthetic-only", "data_sharing_confirmed": True,
        "paid_call_approved": True, "budget_usd": Decimal("0.80"), "max_tokens": 65536,
        "approved_billing_policy_identity": GRAPH_VALUE_BILLING_POLICY.identity,
        "approved_cache_keys": tuple(gv.graph_value_cache_key(x) for x in requests), **kw,
    })


def test_three_conditions_isolate_context_and_questions():
    a, b, c = [gv.build_graph_value_payload(request(x)) for x in "ABC"]
    assert a == jd.build_payload(request().pair)
    assert b["state"] == c["state"]
    assert a["questions"]["relationship"] == b["questions"]["relationship"] == c["questions"]["relationship"]
    assert list(a["questions"]) == list(b["questions"]) == ["relationship"]
    assert list(c["questions"]) == ["relationship", "graph_value"]
    assert len(c["state"]["alternative_neighbors"]) == 2
    assert list(c["questions"]["graph_value"]["criteria"]) == list(gv.VALUES)


@pytest.mark.parametrize("change", ["condition", "too_many", "duplicate", "same_source", "same_passage", "bad_tuple"])
def test_invalid_context_rejected(change):
    req = request()
    if change == "condition": req = replace(req, condition="D")
    if change == "too_many": req = replace(req, alternatives=(endpoint("c"), endpoint("d"), endpoint("e")))
    if change == "duplicate": req = replace(req, alternatives=(req.pair.endpoint_a,))
    if change == "same_source": req = replace(req, alternatives=(replace(endpoint("c"), source_id="source-a"),))
    if change == "same_passage": req = replace(req, alternatives=(replace(endpoint("c"), passages=(req.pair.endpoint_a.passages[0],)),))
    if change == "bad_tuple": req = replace(req, alternatives=[endpoint("c")])
    with pytest.raises(ValueError): gv.build_graph_value_payload(req)


def test_experimental_billing_is_separate_and_bounded():
    req = request(); payload = gv.build_graph_value_payload(req)
    admission = gv.graph_value_admission(req, payload)
    assert admission.policy == GRAPH_VALUE_BILLING_POLICY
    assert admission.max_model_usage_usd == Decimal("0.002752512")
    assert admission.max_model_usage_usd * 288 == Decimal("0.792723456")
    assert "one or two" in admission.to_dict()["assumptions"][0]
    with pytest.raises(BillingAdmissionError): billing_admission(payload)
    with pytest.raises(BillingAdmissionError): graph_value_billing_admission(payload, policy=BATCH_BILLING_POLICY)
    with pytest.raises(BillingAdmissionError): batch_billing_admission(payload, policy=GRAPH_VALUE_BILLING_POLICY)
    payload["questions"]["extra_pair"] = payload["questions"]["relationship"]
    with pytest.raises(BillingAdmissionError): graph_value_billing_admission(payload)
    with pytest.raises(BillingAdmissionError): gv.graph_value_admission(req, payload)


@pytest.mark.parametrize("header", ["TypeSafe-Danger-Zone-Long-Context", "x-fallback-model"])
def test_billing_forbids_context_and_model_escape(header):
    with pytest.raises(BillingAdmissionError):
        graph_value_billing_admission(gv.build_graph_value_payload(request()), headers={header: "true"})


def test_exact_cache_and_execution_identity_include_context():
    req = request()
    assert gv.graph_value_cache_key(req) == gv.graph_value_cache_key(request())
    assert gv.graph_value_cache_key(req) != gv.graph_value_cache_key(replace(req, execution_id="fresh-repeat"))
    assert gv.graph_value_cache_key(req) != gv.graph_value_cache_key(replace(req, alternatives=(endpoint("e"),)))
    assert gv.graph_value_cache_key(req) != gv.graph_value_cache_key(replace(req, context_id="different-map"))
    assert len({gv.graph_value_cache_key(request(x)) for x in "ABC"}) == 3
    # Pair-alone A omits alternatives from transport, but preserves the frozen
    # experiment context identity so accidental input mixing cannot be hidden.
    a = request("A")
    assert gv.build_graph_value_payload(a) == gv.build_graph_value_payload(replace(a, alternatives=()))
    assert gv.graph_value_cache_key(a) != gv.graph_value_cache_key(replace(a, alternatives=()))


@pytest.mark.parametrize("winner,graph_visible,recall_visible,priority", [
    ("essential", True, True, 2), ("helpful", True, True, 1),
    ("redundant", False, True, 0), ("misleading", False, False, 0),
    ("uncertain", True, True, 0),
])
def test_winner_only_projection(winner, graph_visible, recall_visible, priority):
    req = request(); result = gv.graph_value_projection(req, artifact(req, winner), current_request=req)
    assert result["status"] == "current" and result["winner"] == winner
    assert result["graph_visible"] == graph_visible
    assert result["default_recall_visible"] == recall_visible
    assert result["priority"] == priority
    assert result["relationship_value_conflict"] == (winner == "misleading")


def test_small_unique_winner_is_not_rejected_by_a_confidence_cutoff():
    req = request(); raw = response(req, "misleading"); answer = raw["answers"]["graph_value"]
    answer["probabilities"] = dict(essential=.20, helpful=.20, redundant=.20, misleading=.21, uncertain=.19)
    answer["confidence"] = (.21 - .2)/.8
    value = gv.validate_graph_value_response(req, json.dumps(raw).encode())
    assert not gv.graph_value_projection(req, value, current_request=req)["graph_visible"]


def test_tie_does_not_create_action_but_independent_relationship_tie_does_not_cancel_misleading():
    req = request()
    value = artifact(req, "misleading", tie_value=True)
    assert gv.graph_value_projection(req, value, current_request=req)["graph_visible"]
    value = artifact(req, "misleading", tie_relation=True)
    assert value["relationship_winner"] is None
    assert not gv.graph_value_projection(req, value, current_request=req)["graph_visible"]


@pytest.mark.parametrize("change", ["no_current", "stale_note", "stale_alternative", "different_context", "tampered_flag", "tampered_response"])
def test_stale_missing_or_forged_evidence_cannot_suppress(change):
    req = request(); value = artifact(req, "misleading"); current = req
    if change == "no_current": current = None
    if change == "stale_note": current = replace(req, pair=replace(req.pair, endpoint_a=replace(req.pair.endpoint_a, note_text="Changed note")))
    if change == "stale_alternative": current = replace(req, alternatives=(endpoint("e"),))
    if change == "different_context": current = replace(req, context_id="another-map")
    if change == "tampered_flag": value["graph_value_winner"] = "helpful"
    if change == "tampered_response": value["response"]["model"] = "jev-latest"
    result = gv.graph_value_projection(req, value, current_request=current)
    assert result["graph_visible"] and result["default_recall_visible"]
    assert result["status"] in {"unreviewed", "stale", "invalid"}


@pytest.mark.parametrize("change", ["model", "unrequested", "not_argmax", "bad_confidence", "bad_evidence_choice"])
def test_response_validation_is_pinned_and_endpoint_bound(change):
    req = request(); raw = response(req)
    if change == "model": raw["model"] = "jev-1.14.0"
    if change == "unrequested": raw["answers"]["alternative_pair"] = raw["answers"]["relationship"]
    if change == "not_argmax": raw["answers"]["graph_value"]["choice"] = "misleading"
    if change == "bad_confidence": raw["answers"]["graph_value"]["confidence"] = 0
    if change == "bad_evidence_choice": raw["answers"]["relationship"]["choice"] = "supports_A_to_C__A0_C0"
    with pytest.raises(jd.JevDecisionError): gv.validate_graph_value_response(req, json.dumps(raw).encode())


def test_projection_preserves_manual_citations_unknown_and_other_pairs_and_originals():
    req = request(); value = artifact(req, "misleading")
    rows = [
        {"id": "semantic", "source": "note-a", "target": "note-b", "type": "semantic_similarity", "derived": True},
        {"id": "registry", "source_id": "source-a", "target_source_id": "source-b", "inferred": True, "relation_type": "supports"},
        {"id": "jev-rendered", "source": "note-a", "target": "note-b", "layer": "relation", "provider": "typesafe", "model": jd.MODEL_ID},
        {"id": "citation", "source": "note-a", "target": "note-b", "layer": "citation", "inferred": True},
        {"id": "manual", "source_id": "source-a", "target_source_id": "source-b", "origin": "manual", "derived": True},
        {"id": "unmarked-legacy", "source": "note-a", "target": "note-b", "layer": "relation"},
        {"id": "other-pair", "source_id": "source-a", "target_source_id": "source-c", "inferred": True},
    ]
    before = json.dumps(rows)
    assessments = [{"request": req, "artifact": value, "current_request": req}]
    for view in ("graph", "recall"):
        projected = gv.project_graph_value_edges(rows, assessments, view=view)
        assert [x["id"] for x in projected["edges"]] == ["citation", "manual", "unmarked-legacy", "other-pair"]
        assert len(projected["hidden"]) == 3
    assert json.dumps(rows) == before
    assert len(gv.project_graph_value_edges(rows, assessments, context_id="another-map")["edges"]) == len(rows)
    assert len(gv.project_graph_value_edges(rows, assessments*2)["edges"]) == len(rows)


def test_redundant_is_graph_only_and_exposure_is_reversible():
    req = request(); value = artifact(req, "redundant")
    rows = [{"source_id": "source-a", "target_source_id": "source-b", "inferred": True}]
    assessments = [{"request": req, "artifact": value, "current_request": req}]
    assert not gv.project_graph_value_edges(rows, assessments)["edges"]
    assert len(gv.project_graph_value_edges(rows, assessments, view="recall")["edges"]) == 1
    assert gv.project_graph_value_edges(rows, [])["edges"] == rows


def test_disabled_dry_run_and_separate_paid_approval_remain_required(tmp_path):
    req = request(); ledger = store(tmp_path)
    client = gv.JevGraphValueClient(jd.JevConfig(), ledger, transport=lambda *a, **k: pytest.fail("disabled must not dispatch"))
    assert client.assess(req)["status"] == "dry_run"
    assert client.assess(req, dry_run=False)["status"] == "blocked"
    client = gv.JevGraphValueClient(config([req], paid_call_approved=False), ledger, transport=lambda *a, **k: pytest.fail("approval missing"))
    assert client.assess(req, dry_run=False)["provider_calls"] == 0
    with ledger._connect() as db: assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_dispatch_cache_and_fresh_execution_without_historical_deletion(tmp_path):
    req = request(); fresh = replace(req, execution_id="new-trial"); ledger = store(tmp_path); calls = []
    def transport(payload, **kw):
        calls.append(payload)
        return jd.TransportResponse(200, body(req))
    client = gv.JevGraphValueClient(config([req, fresh]), ledger, transport=transport)
    one = client.assess(req, dry_run=False)
    assert one["status"] == "review_required" and one["provider_calls"] == 1
    assert client.assess(req, dry_run=False)["status"] == "cached_review_required"
    assert client.assess(fresh, dry_run=False)["provider_calls"] == 1
    assert len(calls) == 2
    with ledger._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_value_decisions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


def test_modified_cached_artifact_is_not_replayed_or_dispatched_twice(tmp_path):
    req = request(); ledger = store(tmp_path)
    client = gv.JevGraphValueClient(config([req]), ledger, transport=lambda *a, **k: jd.TransportResponse(200, body(req)))
    client.assess(req, dry_run=False)
    with ledger._transaction() as db:
        key, raw = db.execute("SELECT cache_key,artifact_json FROM graph_value_decisions").fetchone()
        forged = json.loads(raw); forged["graph_value_winner"] = "misleading"
        db.execute("UPDATE graph_value_decisions SET artifact_json=? WHERE cache_key=?", (json.dumps(forged), key))
    assert client.assess(req, dry_run=False)["status"] == "blocked"


@pytest.mark.parametrize("failure", ["unknown", "http", "model"])
def test_dispatched_unknown_or_invalid_result_keeps_reservation_no_retry(tmp_path, failure):
    req = request(); ledger = store(tmp_path); calls = []
    def transport(*a, **kw):
        calls.append(1)
        if failure == "unknown": raise TimeoutError("unknown")
        if failure == "http": return jd.TransportResponse(429, b"overloaded")
        raw = response(req); raw["model"] = "jev-latest"
        return jd.TransportResponse(200, json.dumps(raw).encode())
    client = gv.JevGraphValueClient(config([req]), ledger, transport=transport)
    result = client.assess(req, dry_run=False)
    assert result["provider_calls"] == 1 and len(calls) == 1
    with ledger._connect() as db:
        status, reserved = db.execute("SELECT status,reserved_usd FROM reservations").fetchone()
        assert status == "uncertain" and Decimal(reserved) == Decimal("0.002752512")
    assert client.assess(req, dry_run=False)["status"] == "blocked" and len(calls) == 1


def test_two_jobs_share_aggregate_budget_and_concurrent_admission(tmp_path):
    a = request(execution_id="training"); b = request(execution_id="held-out"); ledger = store(tmp_path)
    entered = threading.Event(); release = threading.Event(); calls = []
    def transport(*args, **kw):
        calls.append(1); entered.set(); release.wait(5)
        raise TimeoutError("retain reservation")
    # Exact one-request reserve shared across two independent job configurations.
    budget = Decimal("0.002752512")
    first = gv.JevGraphValueClient(config([a], budget_usd=budget), ledger, transport=transport)
    second = gv.JevGraphValueClient(config([b], budget_usd=budget), ledger, transport=transport)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(first.assess, a, dry_run=False)
        assert entered.wait(5)
        other = pool.submit(second.assess, b, dry_run=False).result(5)
        assert other["status"] == "blocked" and other["provider_calls"] == 0
        release.set(); assert pending.result(5)["status"] == "billing_uncertain"
    assert len(calls) == 1
    with ledger._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_job_allowlist_stays_bounded_while_aggregate_budget_can_span_jobs():
    keys = tuple(f"{x:064x}" for x in range(144))
    jd.JevConfig(approved_cache_keys=keys, budget_usd=Decimal("0.80")).validate()
    with pytest.raises(ValueError): jd.JevConfig(approved_cache_keys=tuple(f"{x:064x}" for x in range(288))).validate()


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), .000000042])
def test_invalid_experimental_billing_policy_has_typed_boundary_failure(rate):
    policy = replace(GRAPH_VALUE_BILLING_POLICY, input_usd_per_token=rate)
    with pytest.raises(BillingAdmissionError):
        graph_value_billing_admission(gv.build_graph_value_payload(request()), policy=policy)


def test_malformed_value_criteria_fails_admission_without_dispatch():
    payload = gv.build_graph_value_payload(request()); payload["questions"]["graph_value"]["criteria"] = None
    with pytest.raises(BillingAdmissionError): graph_value_billing_admission(payload)


def test_request_is_detached_before_transport_and_cache_binding(tmp_path):
    req = request(); original = request(); ledger = store(tmp_path)
    def transport(data, **kw):
        req.pair.endpoint_a.coverage["scope"] = "mutated after dispatch"
        assert "scope" not in json.loads(data)["state"]["endpoints"]["A"]["coverage"]
        return jd.TransportResponse(200, body(original, "misleading"))
    client = gv.JevGraphValueClient(config([req]), ledger, transport=transport)
    result = client.assess(req, dry_run=False)
    assert result["status"] == "review_required"
    assert gv.graph_value_projection(original, result["artifact"], current_request=original)["winner"] == "misleading"
    assert gv.graph_value_projection(original, result["artifact"], current_request=req)["status"] == "stale"


@pytest.mark.parametrize("edges", [None, "edges", [None], [1]])
def test_invalid_projection_sequence_rejected_at_boundary(edges):
    with pytest.raises(ValueError): gv.project_graph_value_edges(edges, [])


def test_stale_projection_priority_cannot_survive_a_context_change():
    req = request(); data = artifact(req, "essential")
    assessment = {"request": req, "artifact": data, "current_request": req}
    raw = [{"source_id": "source-a", "target_source_id": "source-b", "inferred": True}]
    projected = gv.project_graph_value_edges(raw, [assessment])["edges"]
    assert projected[0]["graph_value_priority"] == 2
    stale = gv.project_graph_value_edges(projected, [assessment], context_id="different-map")
    assert "graph_value_priority" not in stale["edges"][0]
    assert stale["audit"][0]["graph_visible"] and stale["audit"][0]["priority"] == 0
