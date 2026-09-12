"""Declared work identity must agree with its destination on both provider routes."""
import json
from types import SimpleNamespace

import pytest

from auto_zettelkasten import readers as r
from auto_zettelkasten.pipeline import _ranked_relationship_candidates
from auto_zettelkasten.relationships import projected_related_links
from test_v030_linking_experiment_direct import adapt_response, candidate, inputs


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
@pytest.mark.parametrize("failure", ["wrong_valid_id", "swapped_titles", "missing_title"])
def test_provider_guard_preserves_bad_row_and_ordinary_ingestion_excludes_it(monkeypatch, provider, failure):
    rows, _ = inputs()
    good = candidate()
    bad = candidate("source-2", "source-3")
    if failure == "wrong_valid_id":
        bad["right_source_id"] = "source-7"
    elif failure == "swapped_titles":
        bad["left_source_title"], bad["right_source_title"] = bad["right_source_title"], bad["left_source_title"]
    else:
        del bad["right_source_title"]
    original = {"candidates": [good, bad], "job_outcomes": []}
    reader = r.CodexReader("gpt-5.6-terra", allow_cloud=True) if provider == "codex" else r.DeepSeekReader(allow_cloud=True)
    calls = []

    def transport(system, user, *args):
        calls.append(json.loads(user))
        assert "exact supplied title alongside its ID" in system
        return r._ProviderText(json.dumps(original), {"response_id": "offline"})

    monkeypatch.setattr(reader, "_generate_text", transport)
    response = reader.select_relationship_candidates([], SimpleNamespace(), context={"catalogue": rows})
    assert len(calls) == 1 and calls[0]["context"]["catalogue"] == rows
    assert response["candidates"][0] == good
    assert response["candidates"][1]["_candidate_disposition"] == "parked_contract_failure"
    assert all(response["candidates"][1][key] == value for key, value in bad.items())
    dispositions = []
    ranked = _ranked_relationship_candidates(response, available_source_ids={row["source_id"] for row in rows},
        entry_by_source={row["source_id"]: row for row in rows}, excluded_pairs=set(), maximum=20,
        bridge_fraction=0, dispositions=dispositions)
    assert len(ranked) == 1 and ranked[0]["target_id"] == "source-1"
    assert any(row["disposition"] == "parked_contract_failure" for row in dispositions)
    # A cache/alternate caller carrying new fields cannot bypass ordinary admission either.
    ranked = _ranked_relationship_candidates(original, available_source_ids={row["source_id"] for row in rows},
        entry_by_source={row["source_id"]: row for row in rows}, excluded_pairs=set(), maximum=20,
        bridge_fraction=0)
    assert len(ranked) == 1


@pytest.mark.parametrize("count", [8, 40, 212])
def test_correct_selected_note_ids_project_reciprocally_at_small_and_large_sizes(count):
    rows, profiles = inputs(count)
    final = f"source-{count - 1}"
    batch, jobs = adapt_response({"candidates": [candidate("source-0", final)]}, descriptions=rows, profiles=profiles)
    assert not batch["parked"] and len(jobs) == 1
    forward = projected_related_links("source-0", profiles, batch["accepted"], max_inferred_links=0)
    reverse = projected_related_links(final, profiles, batch["accepted"], max_inferred_links=0)
    assert forward[0]["target_note_id"] == f"note-{count - 1}"
    assert reverse[0]["target_note_id"] == "note-0"


def test_conflicting_input_identity_stops_before_provider(monkeypatch):
    reader = r.CodexReader("gpt-5.6-terra", allow_cloud=True)
    monkeypatch.setattr(reader, "_generate_text", lambda *args: pytest.fail("provider called"))
    with pytest.raises(r.ProviderError, match="conflicting supplied"):
        reader.select_relationship_candidates([], SimpleNamespace(), context={"catalogue": [
            {"source_id": "a", "title": "First"}, {"source_id": "a", "title": "Different"}]})
