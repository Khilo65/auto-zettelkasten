from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from auto_zettelkasten import jev_decisions as jd
from auto_zettelkasten.jev_billing import STANDARD_BILLING_POLICY


@pytest.fixture(autouse=True)
def deny_all_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("synthetic Jev regressions must never open a network connection")

    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("urllib.request.urlopen", denied)


def _passage(passage_id, text, *, page=3):
    return jd.EvidencePassage(
        passage_id, text, {"page": page, "span": {"lines": [1, 2]}},
        "author's own claim", hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _request():
    return jd.DecisionRequest(
        "synthetic-workspace", "synthetic-library",
        jd.Endpoint("source-a", "note-a", "A conditional prediction.",
                    {"scope": "partial", "details": {"sections": ["results"]}},
                    {"kind": "managed", "details": {"owners": ["source-a"]}},
                    (_passage("a-1", "In population P, treatment T improves Y."),)),
        jd.Endpoint("source-b", "note-b", "B limits the prediction in A.",
                    {"scope": "partial"}, {"kind": "managed"},
                    (_passage("b-1", "T improves Y in P only when condition C holds."),)),
        request_options={"review": {"tags": ["synthetic"]}},
    )


def _response(request, *, choice="qualifies_B_to_A__A0_B0"):
    questions = jd.build_payload(request)["questions"]
    selections = {"relationship": choice}
    answers = {
        name: {"type": "choice", "choice": selections[name], "confidence": 0.99,
               "probabilities": {option: int(option == selections[name]) for option in question["criteria"]}}
        for name, question in questions.items()
    }
    return jd.TransportResponse(200, json.dumps({
        "model": "jev-1.13.0", "answers": answers,
        "usage": {"input_tokens": 123, "output_tokens": 16},
    }).encode("utf-8"))


def _client(tmp_path, monkeypatch, transport, *, request=None):
    # Approval here applies only to the explicitly injected synthetic transport.
    request = request or _request()
    return jd.JevDecisionClient(
        jd.JevConfig(enabled=True, api_key="synthetic-test-only", budget_usd=Decimal("1"),
                     data_sharing_confirmed=True, paid_call_approved=True,
                     approved_billing_policy_identity=STANDARD_BILLING_POLICY.identity,
                     approved_cache_keys=(jd.decision_cache_key(request),)),
        jd.DecisionStore(tmp_path / ".auto-zettelkasten" / "jev-decisions.sqlite3"),
        transport=transport,
    )


def test_direction_and_evidence_are_one_self_contained_choice():
    payload = jd.build_payload(_request())
    assert jd.TEMPLATE_VERSION == "relationship-decision-v3"
    assert list(payload["questions"]) == ["relationship"]
    question = payload["questions"]["relationship"]
    assert question["type"] == "choice"
    options = question["criteria"]
    assert set(options) == {
        "supports_A_to_B__A0_B0", "supports_B_to_A__A0_B0",
        "undermines_A_to_B__A0_B0", "undermines_B_to_A__A0_B0",
        "qualifies_A_to_B__A0_B0", "qualifies_B_to_A__A0_B0",
        "extends_A_to_B__A0_B0", "extends_B_to_A__A0_B0",
        "complements__A0_B0", "contrasts__A0_B0",
        "topical_only_or_none", "insufficient_evidence",
    }
    assert "selected primary" not in json.dumps(question).lower()
    assert "primary decision" not in json.dumps(question).lower()
    for key in ("supports_A_to_B__A0_B0", "supports_B_to_A__A0_B0"):
        rendered = json.dumps(options[key])
        assert "a-1" in rendered and "b-1" in rendered
    assert options["supports_A_to_B__A0_B0"] != options["supports_B_to_A__A0_B0"]


def test_memory_rubric_keeps_comparisons_distinct_from_corroboration():
    question = jd.build_payload(_request())["questions"]["relationship"]
    instructions = question["instructions"]
    assert "not only direct corroboration or contradiction" in instructions
    assert "Different populations or outcome definitions can permit such a bounded comparison without mutual validation" in instructions
    assert "Partial coverage does not erase a comparison established by the supplied passages" in instructions
    assert "never to replace missing selected evidence" in instructions
    assert "a citation, example or document-input relationship alone does not establish it" in instructions
    assert "comparable meaning and scope" in question["criteria"]["supports_A_to_B__A0_B0"]
    assert "different populations or periods alone do not suffice" in question["criteria"]["contrasts__A0_B0"]


def test_current_template_rejects_legacy_complement_definition():
    request = _request()
    question = deepcopy(jd.build_payload(request)["questions"]["relationship"])
    question["criteria"]["complements__A0_B0"] = (
        "A and B supply distinct but mutually useful evidence or methods for a specific comparison"
    )
    with pytest.raises(ValueError, match="options must match"):
        jd.build_payload(replace(request, questions={"relationship": question}))


@pytest.mark.parametrize("endpoint", ["endpoint_a", "endpoint_b"])
def test_reserved_none_passage_is_rejected_at_programmatic_boundary(endpoint):
    request = _request()
    original = getattr(request, endpoint)
    invalid = replace(request, **{endpoint: replace(original, passages=(replace(original.passages[0], passage_id="none"),))})
    with pytest.raises(ValueError, match="reserved"):
        jd.build_payload(invalid)


@pytest.mark.parametrize(("field", "value"), [
    ("workspace_id", "wrong-workspace"),
    ("library_id", "wrong-library"),
    ("provider", "wrong-provider"),
    ("cost_usd", "1000000"),
    ("cost_usd", "NaN"),
    ("cost_usd", "Infinity"),
    ("cost_usd", None),
    ("cost_usd", {"wrong": "type"}),
    ("input_tokens", True),
    ("input_tokens", "123"),
    ("output_tokens", -1),
])
def test_corrupted_cache_provenance_is_not_surfaced(tmp_path, monkeypatch, field, value):
    request = _request()
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(1)
        return _response(request)

    client = _client(tmp_path, monkeypatch, transport, request=request)
    first = client.assess(request, dry_run=False)
    assert first["status"] == "review_required"
    with client.store._transaction() as db:
        artifact = json.loads(db.execute("SELECT artifact_json FROM decisions WHERE cache_key=?", (first["cache_key"],)).fetchone()[0])
        artifact["provenance"][field] = value
        db.execute("UPDATE decisions SET artifact_json=? WHERE cache_key=?", (json.dumps(artifact), first["cache_key"]))
    repeat = client.assess(request, dry_run=False)
    assert repeat["status"] == "blocked"
    assert "decision" not in repeat
    assert calls == [1]


def test_exact_usage_cost_survives_cache_round_trip(tmp_path, monkeypatch):
    request = _request()
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request), request=request)
    first = client.assess(request, dry_run=False)
    assert first["cost_usd"] == "0.000005166"
    assert first["decision"]["provenance"]["cost_usd"] == "0.000005166"
    second = client.assess(request, dry_run=False)
    assert second["status"] == "cached_review_required"
    assert second["cost_usd"] == "0"
    assert second["decision"]["provenance"]["cost_usd"] == "0.000005166"


def test_nested_mutation_during_transport_cannot_change_saved_snapshot(tmp_path, monkeypatch):
    request = _request()
    questions = deepcopy(jd.build_payload(request)["questions"])
    request = replace(request, questions=questions)
    baseline_request = deepcopy(request)
    original_payload = deepcopy(jd.build_payload(request))
    expected_key = jd.decision_cache_key(request)
    synthetic_response = _response(request)
    sent = []

    def transport(body, **_kwargs):
        sent.append(json.loads(body))
        request.endpoint_a.coverage["details"]["sections"].append("fabricated")
        request.endpoint_a.ownership["details"]["owners"].append("foreign-owner")
        request.endpoint_a.passages[0].locator["span"]["lines"][0] = 999
        request.endpoint_a.passages[0].locator["page"] = 999
        request.request_options["review"]["tags"].append("mutated")
        first_question = request.questions[next(iter(request.questions))]
        first_question["criteria"].clear()
        first_question["instructions"] = "mutated after dispatch"
        return synthetic_response

    client = _client(tmp_path, monkeypatch, transport, request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    assert sent == [original_payload]
    assert result["cache_key"] == expected_key
    artifact = result["decision"]
    assert artifact["endpoints"]["A"]["coverage"] == original_payload["state"]["endpoints"]["A"]["coverage"]
    assert artifact["endpoints"]["A"]["ownership"] == original_payload["state"]["endpoints"]["A"]["ownership"]
    assert artifact["selected_evidence"][0]["locator"] == original_payload["state"]["endpoints"]["A"]["passages"][0]["locator"]
    assert artifact["provenance"]["request_payload_sha256"] == hashlib.sha256(
        json.dumps(original_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    repeat = client.assess(baseline_request, dry_run=False)
    assert repeat["status"] == "cached_review_required"
    assert repeat["cache_key"] == expected_key
    assert repeat["decision"] == artifact
    assert sent == [original_payload]


def test_reverse_qualification_and_endpoint_swap_preserve_actor(tmp_path, monkeypatch):
    request = _request()
    first_client = _client(tmp_path / "first", monkeypatch, lambda *_a, **_kw: _response(request), request=request)
    first = first_client.assess(request, dry_run=False)
    assert first["status"] == "review_required"
    assert first["decision"]["relationship"] == {
        "relation_type": "qualifies", "direction": "B_to_A",
        "actor_source_id": "source-b", "reference_source_id": "source-a",
    }
    swapped = replace(request, endpoint_a=request.endpoint_b, endpoint_b=request.endpoint_a)
    second_client = _client(tmp_path / "second", monkeypatch, lambda *_a, **_kw: _response(swapped, choice="qualifies_A_to_B__A0_B0"), request=swapped)
    second = second_client.assess(swapped, dry_run=False)
    assert second["status"] == "review_required"
    assert second["decision"]["relationship"] == {
        "relation_type": "qualifies", "direction": "A_to_B",
        "actor_source_id": "source-b", "reference_source_id": "source-a",
    }
    assert first["cache_key"] != second["cache_key"]


@pytest.mark.parametrize("relation", ["supports", "undermines", "qualifies", "extends"])
def test_reverse_joint_label_resolves_the_actor_from_the_label(tmp_path, monkeypatch, relation):
    request = _request()
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request, choice=f"{relation}_B_to_A__A0_B0"), request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    assert result["decision"]["relationship"] == {
        "relation_type": relation, "direction": "B_to_A",
        "actor_source_id": "source-b", "reference_source_id": "source-a",
    }


def test_single_choice_binds_the_selected_passage_pair(tmp_path, monkeypatch):
    request = _request()
    endpoint_a = replace(request.endpoint_a, passages=(
        request.endpoint_a.passages[0], _passage("a-2", "P contains subgroup S.", page=4),
    ))
    request = replace(request, endpoint_a=endpoint_a)
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request, choice="qualifies_B_to_A__A1_B0"), request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    evidence = result["decision"]["selected_evidence"]
    assert [(row["endpoint"], row["passage_id"], row["question"]) for row in evidence] == [
        ("A", "a-2", "relationship"), ("B", "b-1", "relationship"),
    ]
    assert evidence[0]["text"] == "P contains subgroup S."
    assert evidence[0]["locator"]["page"] == 4


@pytest.mark.parametrize("choice", ["topical_only_or_none", "insufficient_evidence"])
def test_nonrelationship_choices_have_no_active_relation_or_selected_evidence(tmp_path, monkeypatch, choice):
    request = _request()
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request, choice=choice), request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    assert result["decision"]["relationship"] == {
        "relation_type": "", "direction": "unresolved", "actor_source_id": "", "reference_source_id": "",
    }
    assert result["decision"]["selected_evidence"] == []


@pytest.mark.parametrize("location", ["response", "answer", "usage"])
def test_unrequested_provider_narrative_is_rejected(tmp_path, monkeypatch, location):
    request = _request()
    value = json.loads(_response(request).body)
    target = {"response": value, "answer": next(iter(value["answers"].values())), "usage": value["usage"]}[location]
    target["rationale"] = "Unrequested narrative must never enter the proposal."
    response = jd.TransportResponse(200, json.dumps(value).encode("utf-8"))
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: response, request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "invalid_response"
    assert "decision" not in result


@pytest.mark.parametrize("version", ["relationship-decision-v1", "relationship-decision-v2"])
def test_historical_template_is_not_reinterpreted_by_v3_renderer(version):
    with pytest.raises(ValueError, match="template"):
        jd.build_payload(replace(_request(), template_version=version))


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_historical_cached_artifact_is_preserved_without_reinterpretation(tmp_path, monkeypatch, version):
    request = _request()
    calls = []

    def transport(*_a, **_kw):
        calls.append(1)
        return _response(request)

    client = _client(tmp_path, monkeypatch, transport, request=request)
    first = client.assess(request, dry_run=False)
    historical = deepcopy(first["decision"])
    template = f"relationship-decision-{version}"
    historical["schema"] = f"jev-relationship-decision-{version}"
    historical["provenance"]["template_version"] = template
    serialized = json.dumps(historical)
    with client.store._transaction() as db:
        db.execute("UPDATE decisions SET template_version=?, artifact_json=? WHERE cache_key=?",
                   (template, serialized, first["cache_key"]))
    repeat = client.assess(request, dry_run=False)
    assert repeat["status"] == "blocked"
    assert "decision" not in repeat
    with client.store._connect() as db:
        row = db.execute("SELECT template_version, artifact_json FROM decisions WHERE cache_key=?", (first["cache_key"],)).fetchone()
    assert row == (template, serialized)
    assert calls == [1]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_historical_cache_key_is_untouched_by_a_v3_request(tmp_path, monkeypatch, version):
    request = _request()
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request), request=request)
    template = f"relationship-decision-{version}"
    key = f"historical-{version}-key"
    historical = {
        "schema": f"jev-relationship-decision-{version}", "cache_key": key,
        "status": "review_required", "answers": {"primary_decision": {"choice": "supports"}},
        "provenance": {"template_version": template, "cache_key": key},
    }
    serialized = json.dumps(historical)
    with client.store._transaction() as db:
        db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                   (key, "jev-1.13.0", template, "historical-payload", serialized, 1.0))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    assert result["decision"]["schema"] == "jev-relationship-decision-v3"
    assert result["cache_key"] != key
    with client.store._connect() as db:
        assert db.execute("SELECT artifact_json FROM decisions WHERE cache_key=?", (key,)).fetchone() == (serialized,)
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2


def test_four_by_four_passages_bound_joint_options_and_keep_symmetry(tmp_path, monkeypatch):
    request = _request()
    endpoint_a = replace(request.endpoint_a, passages=tuple(_passage(f"a-{index}", f"Synthetic A passage {index}.") for index in range(4)))
    endpoint_b = replace(request.endpoint_b, passages=tuple(_passage(f"b-{index}", f"Synthetic B passage {index}.") for index in range(4)))
    request = replace(request, endpoint_a=endpoint_a, endpoint_b=endpoint_b)
    criteria = jd.build_payload(request)["questions"]["relationship"]["criteria"]
    assert len(criteria) == 162  # Ten joint relations per pair, plus two no-evidence outcomes.
    assert len(set(criteria)) == 162
    assert "supports_B_to_A__A3_B3" in criteria
    client = _client(tmp_path, monkeypatch, lambda *_a, **_kw: _response(request, choice="complements__A3_B2"), request=request)
    result = client.assess(request, dry_run=False)
    assert result["status"] == "review_required"
    assert result["decision"]["relationship"] == {
        "relation_type": "complements", "direction": "symmetric", "actor_source_id": "", "reference_source_id": "",
    }
    assert [row["passage_id"] for row in result["decision"]["selected_evidence"]] == ["a-3", "b-2"]


def test_public_payload_snapshot_does_not_alias_nested_request_metadata():
    request = _request()
    request = replace(request, questions=deepcopy(jd.build_payload(request)["questions"]))
    original_questions = deepcopy(request.questions)
    payload = jd.build_payload(request)
    payload["state"]["endpoints"]["A"]["coverage"]["details"]["sections"].append("changed")
    payload["state"]["endpoints"]["A"]["ownership"]["details"]["owners"].append("changed")
    payload["state"]["endpoints"]["A"]["passages"][0]["locator"]["span"]["lines"][0] = 999
    payload["questions"]["relationship"]["criteria"].clear()
    assert request.endpoint_a.coverage["details"]["sections"] == ["results"]
    assert request.endpoint_a.ownership["details"]["owners"] == ["source-a"]
    assert request.endpoint_a.passages[0].locator["span"]["lines"] == [1, 2]
    assert request.questions == original_questions


def test_snapshot_request_detaches_nested_options_and_metadata():
    original = _request()
    snapshot = jd.snapshot_request(original)
    snapshot.request_options["review"]["tags"].append("changed")
    snapshot.endpoint_a.coverage["details"]["sections"].append("changed")
    snapshot.endpoint_a.passages[0].locator["span"]["lines"][0] = 999
    assert original.request_options["review"]["tags"] == ["synthetic"]
    assert original.endpoint_a.coverage["details"]["sections"] == ["results"]
    assert original.endpoint_a.passages[0].locator["span"]["lines"] == [1, 2]


def test_returned_model_alias_is_rejected_and_reservation_is_retained(tmp_path, monkeypatch):
    request = _request()
    value = json.loads(_response(request).body)
    value["model"] = "jev-latest"
    calls = []

    def transport(*_a, **_kw):
        calls.append(1)
        return jd.TransportResponse(200, json.dumps(value).encode("utf-8"))

    client = _client(tmp_path, monkeypatch, transport, request=request)
    first = client.assess(request, dry_run=False)
    assert first["status"] == "invalid_response"
    assert "pinned model" in first["reason"]
    assert client.assess(request, dry_run=False)["status"] == "blocked"
    with client.store._connect() as db:
        assert db.execute("SELECT status FROM reservations WHERE cache_key=?", (first["cache_key"],)).fetchone() == ("uncertain",)
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert calls == [1]
