import json
from types import SimpleNamespace

import pytest

from auto_zettelkasten import pipeline
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import EvidenceProfile, LiteratureMapRequest, LiteratureMappingPolicy
from v030_linking_experiment_planner import (
    ExperimentReasonerCalls, _experimental_function, experiment_request, run_planner,
)


def test_planner_adapter_keeps_production_routing_and_isolates_capacity(tmp_path):
    profiles = [EvidenceProfile(source_id=source_id, note_id=f"note-{source_id}", context={
        "title": source_id, "thesis": f"Thesis {source_id}",
        "note_status": "analytical_atomic_note",
        "method_or_knowledge_basis": "Comparative analysis",
    }) for source_id in "ABCD"]
    catalogue_path = tmp_path / "catalogue.yml"
    write_yaml(catalogue_path, {"sources": [
        {"source_id": p.source_id, "title": p.source_id} for p in profiles
    ]})
    family_plan = {
        "literature_families": [{
            "family_id": "one", "label": "Comparison", "organizing_problem": "A problem",
            "source_ids": list("ABCD"), "proposed_roles": {s: "core" for s in "ABCD"},
        }],
        "discovery_jobs": [{"job_id": "ab", "family": "one",
                            "left_source_ids": ["A"], "right_source_ids": ["B"],
                            "candidate_quota": 1}],
        "neighboring_families": [],
    }
    seen = []

    def forbidden(*args, **kwargs):
        raise AssertionError("no uncheckpointed provider, source generation or clustering")

    reader = SimpleNamespace(
        name="codex", model="gpt-5.6-terra", reasoning_effort="max",
        ordinary_relationship_decision_contract="relationship-decision-v11",
        context_window_tokens=872000, prompt_reserve_tokens=0, capabilities={},
        plan_literature_families=forbidden, select_relationship_candidates=forbidden,
        literature_family_plan_fits=lambda *a, **k: True,
    )

    class Calls:
        run_id = "experiment-test"

        def __call__(self, stage, key, method, supplied, context):
            seen.append((method, supplied, context))
            if method == "plan_literature_families":
                return family_plan
            assert method == "select_relationship_candidates"
            assert supplied == []  # descriptions appear once, in the catalogue
            assert context["max_inferred_pairs"] <= 137
            return {"candidates": [], "job_outcomes": [
                {"bridge_job_id": row["bridge_job_id"], "status": "no_more_candidates"}
                for row in context["bridge_jobs"]
            ]}

    kwargs = dict(
        profiles=profiles, catalogue={"catalogue_path": str(catalogue_path)},
        source_set={"source_set_type": "collection"}, note_rows=[], reader=reader,
        request=LiteratureMapRequest(tmp_path, provider="codex", model=reader.model,
                                     reasoning_effort="max", provider_concurrency=1),
        reasoner_calls=Calls(), max_records=137, input_char_budget=2_200_000,
    )
    original = pipeline._RELATIONSHIP_DISCOVERY_PAGE_SIZE
    result = run_planner(tmp_path, **kwargs)
    assert result["family_plan"]["discovery_jobs"][0]["left_source_ids"] == ["A"]
    assert result["relationships"]["accepted"] == []
    assert result["relationships"]["cluster_candidates"] == []
    assert pipeline._RELATIONSHIP_DISCOVERY_PAGE_SIZE == original
    assert seen[0][0] == "plan_literature_families"
    assert any(row[0] == "select_relationship_candidates" for row in seen)

    with pytest.raises(ValueError, match="positive integers"):
        run_planner(tmp_path, **{**kwargs, "max_records": 0})
    reader.reasoning_effort = "medium"
    with pytest.raises(ValueError, match="maximum reasoning"):
        run_planner(tmp_path, **kwargs)


def test_output_capacity_clone_does_not_mutate_shared_globals():
    adapted = _experimental_function(pipeline._run_relationship_reasoning,
                                     max_records=137, input_char_budget=2_200_000)
    assert adapted.__globals__["_RELATIONSHIP_DISCOVERY_PAGE_SIZE"] == 137
    assert adapted.__globals__["_relationship_context_char_budget"](None, None) == 2_200_000
    assert adapted.__kwdefaults__ == pipeline._run_relationship_reasoning.__kwdefaults__
    assert pipeline._RELATIONSHIP_DISCOVERY_PAGE_SIZE == 64


def test_experiment_checkpoints_replay_and_preserve_truncated_failure(tmp_path):
    request = experiment_request(tmp_path, model="gpt-5.6-luna", run_id="experiment",
                                 literature_policy=LiteratureMappingPolicy(max_synthesis_calls=24))
    assert request.to_dict()["model"] == "gpt-5.6-luna"
    count = 0

    def select(profiles, request, *, context):
        nonlocal count
        count += 1
        assert context["linking_experiment_identity"] == {"version": "frozen-test"}
        if context.get("fail"):
            error = ValueError("truncated output")
            error.raw_response = '{"candidates": []'
            error.provider_completion = {"finish_reason": "length"}
            raise error
        return {"candidates": [], "job_outcomes": []}

    reader = SimpleNamespace(name="codex", model=request.model, reasoning_effort="max",
                             context_window_tokens=1, select_relationship_candidates=select,
                             select_direct_candidates=select)
    calls = ExperimentReasonerCalls(tmp_path, "experiment", reader, request,
                                    experiment_identity={"version": "frozen-test"},
                                    input_char_budget=100_000)
    context = {"catalogue": [{"source_id": "A", "thesis": "x" * 9000}]}
    expected = calls("relationship_candidate_selection", "one", "select_direct_candidates", [], context)
    checkpoint = calls.root / "relationship_candidate_selection" / "one.yml"
    original = checkpoint.read_bytes()
    assert calls("relationship_candidate_selection", "one", "select_direct_candidates", [], context) == expected
    assert checkpoint.read_bytes() == original
    assert count == 1
    with pytest.raises(ValueError, match="truncated output"):
        calls("relationship_candidate_selection", "failure", "select_relationship_candidates", [], {"fail": True})
    assert count == 2
    failure = calls.root / "relationship_candidate_selection" / "failure.yml"
    assert 'finish_reason: length' in failure.read_text()
    assert 'status: failed' in failure.read_text()
    assert calls._experiment_call.__globals__["_recover_candidate_prefix"]("anything", {}) is None
    assert checkpoint.read_bytes() == original


def test_experiment_uses_actual_cli_configuration_with_fake_helper(tmp_path, monkeypatch):
    from conftest import fake_codex_preflight
    from test_codex_provider import _fake_codex
    from auto_zettelkasten import readers
    from v030_linking_experiment_reader import ExperimentCodexReader

    executable, capture = tmp_path / "fake-codex", tmp_path / "capture.json"
    _fake_codex(executable, capture)
    executable.write_text(executable.read_text().replace(
        '"output_tokens": 1', '"output_tokens": 10, "reasoning_output_tokens": 5'))
    capability = {"slug": "gpt-5.6-terra", "max_context_window": 872000,
                  "effective_context_window_percent": 95,
                  "supported_reasoning_levels": [{"effort": "max"}]}
    reader = ExperimentCodexReader("gpt-5.6-terra", approach="direct", max_records=201,
                                  capability=capability, allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable, {})
    monkeypatch.setattr(readers, "_codex_model_catalog", lambda env: {reader.model: capability})
    result = reader._generate_with_reasoning("system", "user", 65536, 5,
                                            reasoning_effort="max",
                                            output_contract="relationship_candidate_selection")
    actual = json.loads(capture.read_text())
    assert 'model_reasoning_effort="max"' in actual["argv"]
    assert "model_context_window=872000" in actual["argv"]
    assert set(json.loads(actual["output_schema"])["properties"]) == {"candidates"}
    assert result.completion["usage"] == {"input_tokens": 1, "output_tokens": 10,
                                          "reasoning_output_tokens": 5}
    assert result.completion["reasoning_effort"] == "max"
    assert result.completion["max_output_tokens"] == 65536
