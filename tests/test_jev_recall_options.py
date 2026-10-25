"""Synthetic recall choices, historical read compatibility and discovery retention."""
from copy import deepcopy
from dataclasses import replace

import pytest

from auto_zettelkasten.jev_billing import BATCH_TEMPLATE_VERSION
from auto_zettelkasten.jev_decisions import TEMPLATE_VERSION
from auto_zettelkasten.jev_relationships import (
    _trusted_decision_record, apply_jev_decisions, bind_jev_request,
    evaluate_jev_decision, initialize_private_relationship_workspace,
    jev_relationship_is_fresh,
)
from auto_zettelkasten.relationships import (
    RELATIONSHIP_PROJECTION_LABELS, persist_relationship_registry, projected_related_links,
)
from test_jev_relationships import artifact, evaluate, pair


@pytest.mark.parametrize("choice,relation,source,target,reciprocal", [
    ("contextual_connection__A0_B0", "contextual_connection", "source-a", "source-b", "contextual_connection"),
    ("methodological_comparison__A0_B0", "methodological_comparison", "source-a", "source-b", "methodological_comparison"),
    ("potential_research_resource_A_to_B__A0_B0", "potential_research_resource", "source-a", "source-b", "has_potential_research_resource"),
    ("potential_research_resource_B_to_A__A0_B0", "potential_research_resource", "source-b", "source-a", "has_potential_research_resource"),
])
def test_recall_winners_persist_with_evidence_and_project_in_both_directions(pair, choice, relation, source, target, reciprocal):
    original, copy, _, profiles = pair
    result = evaluate(pair, choice, top=0.18)
    assert result["status"] == "accepted"
    row = result["record"]
    assert (row["relation_type"], row["source_id"], row["target_source_id"]) == (relation, source, target)
    assert row["reciprocal_type"] == reciprocal
    assert row["relationship_tier"] == "contextual"
    assert row["cluster_evidence_eligible"] is False
    assert len(row["selected_evidence"]) == 2
    assert RELATIONSHIP_PROJECTION_LABELS[relation][0] in row["reason"]
    initialize_private_relationship_workspace(copy, original_workspace=original)
    applied = apply_jev_decisions(copy, [result], apply=True)
    assert applied["counts"]["accepted"] == 1
    assert len(applied["registry"]["links"]) == 1
    assert jev_relationship_is_fresh(copy, applied["registry"]["links"][0])
    outgoing = projected_related_links(source, profiles, applied["registry"]["links"], max_inferred_links=8)
    incoming = projected_related_links(target, profiles, applied["registry"]["links"], max_inferred_links=8)
    assert outgoing[0]["primary_relation_type"] == relation
    assert incoming[0]["primary_relation_type"] == reciprocal


@pytest.mark.parametrize("choice", ["contextual_connection__A0_B0", "methodological_comparison__A0_B0"])
def test_genuine_abstract_legacy_context_flag_does_not_block_supplied_recall_evidence(pair, choice):
    _, copy, request, profiles = pair
    endpoint = replace(request.endpoint_a, coverage={"source_scope": "abstract_only", "evidence_eligibility": "context_only"})
    request = replace(request, endpoint_a=endpoint)
    result = evaluate_jev_decision(artifact(request, choice), request, profiles=profiles,
                                   bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "accepted"


@pytest.mark.parametrize("coverage", [
    {"source_scope": "metadata_only", "evidence_eligibility": "context_only"},
    {"source_scope": "abstract_only", "evidence_eligibility": "metadata_only"},
    {"source_scope": "note_only", "evidence_eligibility": "context_only"},
])
def test_abstract_exception_does_not_admit_metadata_or_unspecified_context(pair, coverage):
    _, copy, request, profiles = pair
    request = replace(request, endpoint_a=replace(request.endpoint_a, coverage=coverage))
    result = evaluate_jev_decision(artifact(request, "contextual_connection__A0_B0"), request,
                                   profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "deferred"
    assert result["reasons"] == ["insufficient_source_coverage"]


@pytest.mark.parametrize("choice,status", [
    ("topical_only_or_none", "no_relationship"), ("insufficient_evidence", "deferred"),
])
def test_abstention_preserves_embedding_storage_and_related_projection(pair, choice, status):
    original, copy, _, profiles = pair
    embedding = {"relation_id": "synthetic-embedding", "source_id": "source-a", "target_source_id": "source-b",
                 "source_note_id": "note-a", "target_note_id": "note-b", "relation_type": "semantic_similarity",
                 "provenance": "local_embedding", "active": True, "decision_status": "discovery",
                 "reason": "A saved local semantic neighbor.", "strength": 60}
    persist_relationship_registry(copy, structural_relations=[embedding])
    initialize_private_relationship_workspace(copy, original_workspace=original)
    before = {p: p.read_bytes() for p in (copy / "02_source_memory/notes").glob("*.md")}
    applied = apply_jev_decisions(copy, [evaluate(pair, choice)], apply=True)
    assert applied["counts"][status] == 1
    assert applied["registry"]["links"] == [embedding]
    assert applied["registry"]["relations"] == [embedding]
    projected = projected_related_links("source-a", profiles, applied["registry"]["links"], max_inferred_links=8)
    assert any("semantic_similarity" in row["relation_types"] for row in projected)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_tie_preserves_embedding_projection(pair):
    original, copy, request, profiles = pair
    embedding = {"relation_id": "synthetic-embedding", "source_id": "source-a", "target_source_id": "source-b",
                 "relation_type": "semantic_similarity", "provenance": "local_embedding", "active": True,
                 "reason": "Saved neighbor.", "strength": 60}
    persist_relationship_registry(copy, structural_relations=[embedding])
    value = artifact(request)
    answer = value["answers"]["relationship"]
    answer["probabilities"] = {key: 0 for key in answer["probabilities"]}
    answer["probabilities"].update(topical_only_or_none=0.5, insufficient_evidence=0.5)
    size = len(answer["probabilities"])
    answer["confidence"] = (0.5 - 1 / size) / (1 - 1 / size)
    result = evaluate_jev_decision(value, request, profiles=profiles,
                                   bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["reasons"] == ["tied_winning_options"]
    initialize_private_relationship_workspace(copy, original_workspace=original)
    applied = apply_jev_decisions(copy, [result], apply=True)
    assert applied["registry"]["links"] == [embedding]
    assert any("semantic_similarity" in row["relation_types"] for row in projected_related_links(
        "source-a", profiles, applied["registry"]["links"], max_inferred_links=8))


@pytest.mark.parametrize("template,source_template", [
    ("relationship-decision-v2", "relationship-decision-v2"),
    ("relationship-decision-v3", "relationship-decision-v3"),
    ("relationship-batch-v1", "relationship-decision-v2"),
    ("relationship-batch-v2", "relationship-decision-v3"),
    (TEMPLATE_VERSION, TEMPLATE_VERSION),
    (BATCH_TEMPLATE_VERSION, TEMPLATE_VERSION),
])
def test_historical_accepted_records_remain_readable_under_recorded_template(pair, template, source_template):
    row = deepcopy(evaluate(pair)["record"])
    row.update(template_version=template, prompt_version=template, source_template_version=source_template)
    assert _trusted_decision_record(row)
    assert jev_relationship_is_fresh(pair[1], row)
    row["source_template_version"] = "relationship-decision-unknown"
    assert not _trusted_decision_record(row)


def test_new_recall_types_cannot_be_presented_as_historical_choices(pair):
    row = deepcopy(evaluate(pair, "contextual_connection__A0_B0")["record"])
    row.update(template_version="relationship-decision-v3", prompt_version="relationship-decision-v3",
               source_template_version="relationship-decision-v3")
    assert not _trusted_decision_record(row)
    assert not jev_relationship_is_fresh(pair[1], row)


def test_old_decision_is_not_reinterpreted_for_current_template(pair):
    _, copy, request, profiles = pair
    value = artifact(request, "contextual_connection__A0_B0")
    value["provenance"]["template_version"] = "relationship-decision-v3"
    value["provenance"]["source_template_version"] = "relationship-decision-v3"
    checked = evaluate_jev_decision(value, request, profiles=profiles,
                                    bindings=bind_jev_request(copy, request, profiles=profiles))
    assert checked["status"] == "rejected"
    assert checked["reasons"] == ["unsupported_decision_template"]
