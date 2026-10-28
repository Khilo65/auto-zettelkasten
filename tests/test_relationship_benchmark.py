from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from auto_zettelkasten.jev_decisions import DecisionRequest, Endpoint, EvidencePassage, build_payload
from auto_zettelkasten.relationship_benchmark import (
    benchmark_preview, normalize_choice, pair_identity, prepare_matched_pair,
    select_challenge_pairs, validate_matched_pair,
)


def request():
    def endpoint(sid, text):
        return Endpoint(sid, f"note-{sid}", "Existing note only; original document unverified.",
                        {"scope": "genuine_abstract"}, {"attribution": "unresolved"},
                        (EvidencePassage(f"{sid}-passage", text, {"kind": "saved_note"},
                                         "summary attribution unverified", hashlib.sha256(text.encode()).hexdigest()),))
    return DecisionRequest("synthetic-workspace", "synthetic-library",
                           endpoint("source-a", "A reports an association under condition C."),
                           endpoint("source-b", "B reports no association without condition C."))


def historical():
    rows = []
    for kind, count in (("supports", 11), ("qualifies", 12), ("undermines", 1),
                        ("contextual_connection", 3), ("complements", 3), ("", 5)):
        for i in range(count):
            rows.append({"source_id": f"{kind or 'none'}-{i}", "target_source_id": "other",
                         "relation_type": kind, "decision_status": "accepted" if kind else "no_relationship"})
    return rows


def test_packets_preserve_exact_substantive_task_and_current_option_order():
    result = prepare_matched_pair(request())
    jev = result["payloads"]["jev"]["payload"]
    deep = result["payloads"]["historical_deepseek"]["payload"]
    assert jev == build_payload(request())
    assert json.loads(deep["messages"][1]["content"]) == {"state": jev["state"], "questions": jev["questions"]}
    assert list(jev["questions"]["relationship"]["criteria"]) == result["expected_options"]
    assert result["provider_calls"] == 0
    assert result["paid_execution_approved"] is False
    assert deep["model"] == "deepseek-v4-flash"


@pytest.mark.parametrize("path,value", [
    (("jev_billing_admission", "max_model_usage_usd"), "0"),
    (("payloads", "jev", "wire_bytes"), 1),
    (("payloads", "historical_deepseek", "payload", "model"), "substitute-model"),
])
def test_modified_artifacts_fail_before_cost_preview(path, value):
    result = deepcopy(prepare_matched_pair(request()))
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        benchmark_preview([result])


def test_changed_evidence_and_coverage_invalidate_identity():
    base = request()
    changed = replace(base, endpoint_a=replace(base.endpoint_a, coverage={"scope": "partial"}))
    assert prepare_matched_pair(base)["substantive_task_sha256"] != prepare_matched_pair(changed)["substantive_task_sha256"]
    stale = replace(base, endpoint_a=replace(base.endpoint_a, passages=(replace(base.endpoint_a.passages[0], text="altered"),)))
    with pytest.raises(ValueError, match="hash"):
        prepare_matched_pair(stale)


def test_preview_retains_unknown_historical_cost_and_conditional_reservations():
    preview = benchmark_preview([prepare_matched_pair(request())])
    assert preview["prepared_request_count"] == 2
    assert preview["jev_conditional_model_usage_reservation_usd"] == "0.002752512"
    assert preview["combined_reservation_usd"] is None
    assert preview["deepseek_reservation_usd"] is None
    assert preview["input_token_counts"] is None
    assert preview["wire_bytes_are_tokens"] is False
    with pytest.raises(ValueError, match="repeats"):
        benchmark_preview([prepare_matched_pair(request())] * 2)


def test_selection_is_deterministic_and_never_pads_with_invented_pairs():
    disputed = [{"source_id": f"dispute-{i}", "target_source_id": "other"} for i in range(6)]
    rows = historical()
    forward = select_challenge_pairs(rows, disputed)
    backward = select_challenge_pairs(list(reversed(rows)), list(reversed(disputed)))
    assert forward == backward
    assert len(forward) == 36
    assert len({pair_identity(row) for row in forward}) == 36
    with pytest.raises(ValueError, match="24"):
        select_challenge_pairs([row for row in rows if row["relation_type"] != "undermines"], disputed)
    with pytest.raises(ValueError, match="4"):
        select_challenge_pairs(rows, disputed[:3])


def test_unique_winner_has_no_numerical_confidence_cutoff_and_never_writes_link():
    req = request()
    options = list(build_payload(req)["questions"]["relationship"]["criteria"])
    chosen = "qualifies_A_to_B__A0_B0"
    # A low-probability unique winner is still the winner; no 0.8 cutoff.
    probabilities = {option: .91 / (len(options) - 1) for option in options}
    probabilities[chosen] = .09
    result = normalize_choice(req, chosen, probabilities)
    assert result["direction"] == "A_to_B"
    assert result["accepted"] is False


def test_runtime_options_remain_in_exact_cache_identity():
    plain = prepare_matched_pair(request())
    changed = prepare_matched_pair(replace(request(), request_options={"execution_id": "synthetic-new-run"}))
    assert plain["substantive_task_sha256"] == changed["substantive_task_sha256"]
    assert plain["jev_cache_key"] != changed["jev_cache_key"]
    validate_matched_pair(changed)


def test_ties_and_inconsistent_choices_cannot_be_accepted():
    req = request()
    options = list(build_payload(req)["questions"]["relationship"]["criteria"])
    probs = dict.fromkeys(options, 0.)
    probs[options[0]] = probs[options[1]] = .5
    assert normalize_choice(req, options[0], probs)["status"] == "unresolved_tie"
    probs[options[0]], probs[options[1]] = .6, .4
    with pytest.raises(ValueError, match="disagrees"):
        normalize_choice(req, options[1], probs)
    with pytest.raises(ValueError, match="unknown"):
        normalize_choice(req, "fabricated_evidence_A0_B9")
