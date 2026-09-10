"""Provider-blocked direct exposure, normalization and bounded paging checks."""
from itertools import combinations

import pytest

from auto_zettelkasten.models import EvidenceProfile
from v030_linking_experiment_direct import adapt_response, partition_descriptions, run_direct


def inputs(count=8):
    rows = [{"source_id": f"source-{i}", "title": f"Work {i}", "thesis": "x" * (20 + i)} for i in range(count)]
    profiles = [EvidenceProfile(source_id=row["source_id"], note_id=f"note-{i}", context={"title": row["title"]})
                for i, row in enumerate(rows)]
    return rows, profiles


def candidate(left="source-0", right="source-1", **changes):
    return {"left_source_id": left, "right_source_id": right, "decision": "relationship",
            "relation_type": "contextual_connection", "actor_source_id": None,
            "reference_source_id": None, "reason": "The works connect institutions across distinct scales.", **changes}


def runner(responses, *, count=8, max_records=2, max_calls=24, fits=None):
    rows, profiles = inputs(count)
    events, requests = [], []
    answers = iter(responses)

    def call(packet, excluded, limit):
        requests.append((packet, excluded, limit))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    result = run_direct(rows, profiles, call=call, fits=fits or (lambda *args: True),
                        max_records=max_records, max_calls=max_calls,
                        preserve_raw=lambda *args: events.append(("raw", args)),
                        persist_page=lambda *args: events.append(("persist", args)))
    return result, events, requests


def test_eight_supplied_notes_can_link_without_family_job_eligibility():
    rows, profiles = inputs()
    # A connection between two supplied notes does not need a planner's bridge ID.
    batch, jobs = adapt_response({"candidates": [candidate("source-0", "source-7")]},
                                 descriptions=rows, profiles=profiles)
    assert len(batch["accepted"]) == 1 and not batch["parked"]
    assert jobs[0].selected_evidence == {}
    accepted = batch["accepted"][0]
    assert accepted["source_note_id"] == "note-0"
    assert accepted["target_note_id"] == "note-7"
    assert accepted["forward_label"] == accepted["inverse_label"]
    assert accepted["left_evidence_anchor_ids"] == accepted["right_evidence_anchor_ids"] == []
    assert accepted["source_evidence"]["claim"] == accepted["target_evidence"]["claim"] == ""


@pytest.mark.parametrize("change", [
    {"right_source_id": "not-supplied"}, {"right_source_id": "source-0"},
    {"actor_source_id": "source-4"}, {"actor_source_id": "source-0"},
    {"actor_source_id": []}, {"relation_type": "invented"}, {"rank": 1},
])
def test_invalid_records_are_parked(change):
    rows, profiles = inputs()
    batch, _ = adapt_response({"candidates": [candidate(**change)]}, descriptions=rows, profiles=profiles)
    assert batch["parked"] and not batch["accepted"]


def test_direction_normalization_reuses_production_and_keeps_original_type():
    rows, profiles = inputs()
    batch, _ = adapt_response({"candidates": [candidate(relation_type="extends")]}, descriptions=rows, profiles=profiles)
    assert batch["accepted"][0]["relation_type"] == "contextual_connection"
    assert "missing_direction_normalized_to_contextual:extends" in batch["accepted"][0]["contract_warnings"]
    directed = candidate(relation_type="extends", actor_source_id="source-1", reference_source_id="source-0")
    batch, _ = adapt_response({"candidates": [directed]}, descriptions=rows, profiles=profiles)
    assert batch["accepted"][0]["relation_type"] == "extends"
    assert batch["accepted"][0]["source_id"] == "source-1"


def test_missing_direction_duplicate_conflict_and_completed_exclusions():
    rows, profiles = inputs()
    missing = candidate()
    del missing["actor_source_id"]
    for records, excluded in [([missing], []), ([candidate(), candidate(relation_type="contrasts")], []),
                              ([candidate()], [["source-1", "source-0"]])]:
        batch, _ = adapt_response({"candidates": records}, descriptions=rows, profiles=profiles, excluded_pairs=excluded)
        assert batch["parked"] and not batch["accepted"]
    batch, jobs = adapt_response({"candidates": [candidate(), candidate()]}, descriptions=rows, profiles=profiles)
    assert len(batch["accepted"]) == len(jobs) == 1


def test_partition_preserves_intact_rows_and_every_pair_opportunity():
    rows, _ = inputs(12)
    def fits(packet, excluded, limit):
        return len(packet) <= 6
    blocks = partition_descriptions(rows, fits=fits, max_records=2)
    assert len(blocks) == 4
    assert sorted(row["source_id"] for block in blocks for row in block) == sorted(row["source_id"] for row in rows)
    exposed = {tuple(sorted((a["source_id"], b["source_id"])))
               for left, right in combinations(blocks, 2) for a, b in combinations(left + right, 2)}
    assert len(exposed) == 12 * 11 // 2
    assert all(row in rows for block in blocks for row in block)
    with pytest.raises(ValueError, match="two intact"):
        partition_descriptions(rows, fits=lambda packet, *args: len(packet) < 2, max_records=2)
    with pytest.raises(ValueError, match="distinct"):
        partition_descriptions(rows + rows[:1], fits=fits, max_records=2)


def test_saturated_page_continues_with_exclusions_and_short_page_is_not_exhaustive():
    result, events, requests = runner([
        {"candidates": [candidate(), candidate("source-0", "source-2")]},
        {"candidates": [candidate("source-1", "source-2")]},
    ])
    assert result["status"] == "completed_paging" and result["calls"] == 2
    assert result["exhaustive_discovery"] is False
    assert requests[1][1] == [["source-0", "source-1"], ["source-0", "source-2"]]
    assert [kind for kind, _ in events] == ["raw", "persist", "raw", "persist"]


@pytest.mark.parametrize("second, status", [(ValueError("interrupted"), "failed_call"),
                                             ({"candidates": "truncated"}, "failed_response")])
def test_prior_results_survive_failure_without_retry(second, status):
    result, events, _ = runner([{"candidates": [candidate()]}, second], max_records=1)
    assert result["status"] == status and result["calls"] == 2
    assert len(result["accepted"]) == 1
    assert sum(kind == "persist" for kind, _ in events) == 1


def test_budget_and_exclusion_growth_stop_without_losing_work():
    result, _, _ = runner([{"candidates": [candidate()]}], max_records=1, max_calls=1)
    assert result["status"] == "incomplete_budget" and len(result["accepted"]) == 1
    result, _, _ = runner([{"candidates": [candidate()]}], max_records=1,
                          fits=lambda packet, exclusions, limit: not exclusions)
    assert result["status"] == "incomplete_context" and len(result["accepted"]) == 1


def test_no_relationship_completed_and_oversized_response_preserved_first():
    no_link = candidate(decision="no_relationship", relation_type="")
    result, _, _ = runner([{"candidates": [no_link]}, {"candidates": []}], max_records=1)
    assert result["status"] == "completed_paging" and len(result["no_relationship"]) == 1
    result, events, _ = runner([{"candidates": [candidate(), candidate("source-1", "source-2")]}], max_records=1)
    assert result["status"] == "failed_response"
    assert [kind for kind, _ in events] == ["raw"]
