"""Ordinary decisions retain useful context without inventing direction."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from auto_zettelkasten import pipeline
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.relationships import ingest_relationship_decision_batch


def _job(contract: str = "relationship-decision-v11") -> RelationshipPairJob:
    return RelationshipPairJob(
        left_source_id="source-a", right_source_id="source-b", output_contract=contract
    )


def _row(job: RelationshipPairJob, **changes: Any) -> dict[str, Any]:
    return {
        "pair_job_id": job.pair_job_id,
        "decision": "relationship",
        "relation_type": "supports",
        "actor_source_id": None,
        "reference_source_id": None,
        "reason": "Identity mechanisms connect individual conflict to institutional persistence.",
        "comparison_proposition": "Identity mechanisms operate at different analytical levels.",
        **changes,
    }


def _ingest(job: RelationshipPairJob, *rows: dict[str, Any], provider: str = "codex"):
    return ingest_relationship_decision_batch(
        {"decisions": list(rows)}, pair_jobs=[job], provider=provider, model="test-model"
    )


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
@pytest.mark.parametrize("relation_type", ["supports", "sequential_relationship"])
@pytest.mark.parametrize("empty", [None, "", " \t"])
def test_explicitly_empty_direction_retains_context_and_rationale(
    provider: str, relation_type: str, empty: Any
) -> None:
    job = _job()
    row = _row(job, relation_type=relation_type, actor_source_id=empty, reference_source_id=empty)
    original = dict(row)
    result = _ingest(job, row, provider=provider)

    assert result["parked"] == []
    accepted, = result["accepted"]
    assert accepted["relation_type"] == "contextual_connection"
    assert accepted["forward_label"] == accepted["inverse_label"] == "is contextually connected to"
    assert accepted["reason"] == row["reason"]
    assert accepted["comparison_proposition"] == row["comparison_proposition"]
    assert f"missing_direction_normalized_to_contextual:{relation_type}" in accepted["contract_warnings"]
    assert accepted["provider"] == provider
    assert row == original


@pytest.mark.parametrize("changes", [
    {"actor_source_id": "source-a"},
    {"actor_source_id": "outside", "reference_source_id": "source-b"},
    {"actor_source_id": "source-a", "reference_source_id": "source-a"},
    {"actor_source_id": 0, "reference_source_id": 0},
    {"actor_source_id": [], "reference_source_id": []},
    {"actor": "outside", "reference": "source-b"},
    {"relation_type": "invented_type"},
    {"pair_job_id": "unknown-job"},
    {"reason": "", "comparison_proposition": ""},
])
def test_context_fallback_does_not_rescue_invalid_decisions(changes: dict[str, Any]) -> None:
    job = _job()
    result = _ingest(job, _row(job, **changes))
    assert result["accepted"] == []
    assert result["parked"]


@pytest.mark.parametrize("missing", ["actor_source_id", "reference_source_id"])
def test_omitted_direction_is_not_explicit_empty_direction(missing: str) -> None:
    job = _job()
    row = _row(job)
    del row[missing]
    result = _ingest(job, row)
    assert result["accepted"] == []
    assert result["parked"]


def test_provided_direction_is_preserved_and_legacy_remains_strict() -> None:
    job = _job()
    directed = _row(job, actor_source_id="source-b", reference_source_id="source-a")
    accepted, = _ingest(job, directed)["accepted"]
    assert accepted["relation_type"] == "supports"
    assert accepted["source_id"] == "source-b"
    assert accepted["target_source_id"] == "source-a"
    assert not any("missing_direction" in warning for warning in accepted.get("contract_warnings", []))
    legacy = _job("relationship-decision-v10")
    result = _ingest(legacy, _row(legacy))
    assert result["accepted"] == []
    assert result["parked"]


def test_duplicate_decisions_and_self_pairs_remain_invalid() -> None:
    job = _job()
    result = _ingest(job, _row(job), _row(job))
    assert result["accepted"] == []
    assert result["parked"]
    with pytest.raises(ValueError, match="two distinct source IDs"):
        RelationshipPairJob(left_source_id="source-a", right_source_id="source-a")


@pytest.fixture(scope="module")
def parser_scope() -> tuple[Any, ast.AST]:
    # Exercise the actual nested transport boundary without running discovery.
    tree = ast.parse(Path(pipeline.__file__).read_text())
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "ordinary_row")
    scope = dict(vars(pipeline))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "ordinary-boundary", "exec"), scope)
    return scope["ordinary_row"], tree


def test_transport_boundary_preserves_nulls_but_rejects_missing_or_malformed_fields(parser_scope) -> None:
    parse, _tree = parser_scope
    job = _job()
    row = _row(job)
    del row["pair_job_id"]
    row.update(left_source_id=job.left_source_id, right_source_id=job.right_source_id)
    assert len(_ingest(job, parse(job, [row]))["accepted"]) == 1
    for missing in ("actor_source_id", "reference_source_id"):
        incomplete = {key: value for key, value in row.items() if key != missing}
        assert parse(job, [incomplete])["decision"] == "invalid_ordinary_decision_shape"
    for value in (0, False, [], {}):
        assert parse(job, [{**row, "actor_source_id": value}])["decision"] == "invalid_ordinary_decision_shape"
    assert parse(job, [{**row, "actor_source_id": "outside"}])["decision"] == "ordinary_decision_unknown_endpoint"
    assert parse(job, [{**row, "actor": "source-a"}])["decision"] == "invalid_ordinary_decision_fields"
    assert parse(job, [row, {**row, "relation_type": "sequential_relationship"}])["decision"] == "conflicting_ordinary_decisions"


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
def test_local_job_cache_identity_changes_with_normalization_policy(parser_scope, provider: str) -> None:
    _parse, tree = parser_scope
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "decision_identity"
                              for target in node.targets))
    identities = []
    for version in ("4", "5"):
        scope = {
            **vars(pipeline), "provider_name": provider, "model_name": "test-model",
            "decision_prompt_version": "unchanged", "decision_contract": "relationship-decision-v11",
            "ordinary_decisions": True, "relationship_policy_identity": "unchanged",
            "RELATIONSHIP_DECISION_NORMALIZATION_VERSION": version,
        }
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), "job-cache-identity", "exec"), scope)
        identities.append(scope["decision_identity"])
    assert identities[0] != identities[1]
