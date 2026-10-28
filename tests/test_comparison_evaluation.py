import pytest

from auto_zettelkasten.comparison_evaluation import compare_paired_results, evaluate_evidence, resource_record


def reference():
    return {"task_id": "Q1", "evidence_units": [{"unit_id": "finding", "importance": "essential", "alternatives": [
        {"source_id": "a", "quote": "The intervention had no effect."},
        {"source_id": "b", "quote": "No significant effect was found."}]}],
        "source_judgments": [{"source_id": "a", "label": "essential"}]}


def test_endpoint_recovery_is_not_passage_recovery():
    result = evaluate_evidence({"notes": [{"source_id": "a", "excerpt": "An unrelated finding."}]}, reference())
    assert result["totals"]["essential"] == {"available_units": 1, "sources_found": 1, "passages_found": 0}


def test_alternative_sources_and_whitespace_are_credited_but_not_generated_claims():
    packet = {"notes": [{"source_id": "b", "excerpt": "No significant\n effect was found."},
                        {"source_id": "c", "excerpt": "The intervention had no effect.", "grounding_status": "unmapped"}]}
    result = evaluate_evidence(packet, reference())
    assert result["units"][0]["matched_source_ids"] == ["b"]
    assert result["unreviewed_sources"] == ["b"]


def test_disagreements_remain_unresolved():
    ref = reference()
    ref["source_judgments"].extend([{"source_id": "a", "label": "misleading"}, {"source_id": "a", "label": "essential"}])
    result = evaluate_evidence({"notes": [{"source_id": "a", "text": "The intervention had no effect."}]}, ref)
    assert result["returned_review_labels"]["unresolved"] == 1
    assert result["returned_review_labels"]["misleading"] == 0


def test_legacy_compact_evidence_is_credited_without_loading_full_notes():
    packet = {"notes": [{"source_id": "a", "thesis": "The intervention had no effect.", "method": "A controlled trial."}]}
    assert evaluate_evidence(packet, reference())["totals"]["essential"]["passages_found"] == 1


def test_pairing_requires_same_tasks_and_reference_units():
    a = evaluate_evidence({"notes": []}, reference())
    b = evaluate_evidence({"notes": [{"source_id": "a", "text": "The intervention had no effect."}]}, reference())
    result = compare_paired_results([a], [b])
    assert result["essential_improved_tasks"] == 1
    assert result["rollout"] == "not_authorized"
    with pytest.raises(ValueError, match="same nonempty"):
        compare_paired_results([a], [])
    other = dict(b, units=[dict(b["units"][0], unit_id="other")])
    with pytest.raises(ValueError, match="same reference"):
        compare_paired_results([a], [other])
    changed = reference()
    changed["evidence_units"][0]["alternatives"][0]["quote"] = "A different finding."
    with pytest.raises(ValueError, match="same frozen reference"):
        compare_paired_results([a], [evaluate_evidence({"notes": []}, changed)])


def test_empty_reference_quotes_and_duplicate_ids_are_rejected():
    ref = reference()
    ref["evidence_units"][0]["alternatives"][0]["quote"] = " "
    with pytest.raises(ValueError, match="nonempty quote"):
        evaluate_evidence({"notes": []}, ref)
    ref = reference()
    ref["evidence_units"] *= 2
    with pytest.raises(ValueError, match="unique"):
        evaluate_evidence({"notes": []}, ref)


def test_resource_measurements_do_not_claim_unmeasured_cost_or_research_timer():
    row = resource_record(elapsed_seconds=1.25, peak_rss_bytes=1024, disk_delta_bytes=-4, provider_calls=0, actual_cost_usd=None)
    assert row["actual_cost_usd"] is None
    assert "no research stopwatch" in row["time_role"]
    with pytest.raises(ValueError):
        resource_record(elapsed_seconds=float("nan"), peak_rss_bytes=0, disk_delta_bytes=0, provider_calls=0, actual_cost_usd="0")
    with pytest.raises(ValueError):
        resource_record(elapsed_seconds=0, peak_rss_bytes=0, disk_delta_bytes=0, provider_calls=0, actual_cost_usd="NaN")
