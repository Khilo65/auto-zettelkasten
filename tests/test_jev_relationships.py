"""Synthetic deterministic Jev policy and private-copy registry checks."""
from dataclasses import asdict, replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import shutil

import pytest

from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.jev_billing import BATCH_TEMPLATE_VERSION
from auto_zettelkasten.jev_decisions import (
    MODEL_ID, TEMPLATE_VERSION, DecisionRequest, Endpoint, EvidencePassage, _canonical_json,
    _decision_artifact, _relationship_options, build_payload,
)
from auto_zettelkasten.jev_relationships import (
    CURRENT_POLICY_VERSION, LEGACY_POLICY_VERSION, JevAcceptancePolicy, apply_jev_decisions, bind_jev_request,
    evaluate_jev_decision, initialize_private_relationship_workspace, reusable_jev_pairs,
    validate_private_relationship_workspace, jev_relationship_is_fresh,
)
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.profiles import save_profile
from auto_zettelkasten.relationships import persist_relationship_registry, projected_related_links, stable_hash


@pytest.fixture
def pair(tmp_path):
    original = tmp_path / "original"
    for name in ("notes", "profiles", "indexes"):
        (original / "02_source_memory" / name).mkdir(parents=True)
    profiles, endpoints = [], []
    for side, claim in (("a", "Institutions reduce conflict when credible commitments are enforced."),
                        ("b", "Strong institutions can reduce recurrence after an early election.")):
        path = Path("02_source_memory/notes") / f"{side}.md"
        note = f"---\nsource_id: source-{side}\nnote_id: note-{side}\n---\n# Synthetic {side}\n\n## Thesis\n{claim}\n"
        (original / path).write_text(note)
        profile = EvidenceProfile(source_id=f"source-{side}", note_id=f"note-{side}", context={"note_path": str(path)})
        save_profile(original / "02_source_memory/profiles", profile)
        profiles.append(profile)
        passage = EvidencePassage(f"passage-{side}", claim, {"kind": "note_lines", "note_relative_path": str(path), "line_start": 8},
                                  "Existing note summary", sha256(claim.encode()).hexdigest())
        endpoints.append(Endpoint(f"source-{side}", f"note-{side}", note, {"status": "note_only", "original_document_supplied": False},
                                  {"attribution_status": "note-derived; original author attribution unverified"}, (passage,)))
    copy = tmp_path / "private-copy"
    shutil.copytree(original, copy)
    request = DecisionRequest("synthetic-workspace", "synthetic-library", *endpoints)
    return original, copy, request, profiles


def artifact(request, choice="supports_A_to_B__A0_B0", top=0.91):
    options = _relationship_options(request)
    probabilities = {key: (1 - top) / (len(options) - 1) for key in options}
    probabilities[choice] = top
    confidence = round((top - 1 / len(options)) / (1 - 1 / len(options)), 2)
    response = {"model": MODEL_ID, "usage": {"input_tokens": 1000, "output_tokens": 100},
                "answers": {"relationship": {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}}}
    payload_hash = sha256(_canonical_json(build_payload(request)).encode()).hexdigest()
    return _decision_artifact(request, response, "a" * 64, payload_hash, Decimal("0.000042"))


def evaluate(pair, choice="supports_A_to_B__A0_B0", **kwargs):
    original, copy, request, profiles = pair
    bindings = bind_jev_request(copy, request, profiles=profiles)
    return evaluate_jev_decision(artifact(request, choice=choice, **kwargs), request, profiles=profiles, bindings=bindings)


@pytest.mark.parametrize("relation", ["supports", "undermines", "qualifies", "extends", "complements", "contrasts"])
def test_substantive_winner_uses_exact_evidence_and_model_confidence(pair, relation):
    suffix = "__A0_B0" if relation in {"complements", "contrasts"} else "_A_to_B__A0_B0"
    result = evaluate(pair, relation + suffix)
    assert result["status"] == "accepted"
    row = result["record"]
    assert row["relation_type"] == relation
    assert row["verification_status"] == "policy_accepted"
    assert row["source_evidence"]["content_sha256"] == sha256(row["source_evidence"]["text"].encode()).hexdigest()
    assert row["policy_version"] == CURRENT_POLICY_VERSION
    assert row["evidence_basis"] == "note_summary"


def test_reverse_direction_and_reciprocal_projection(pair):
    _, copy, request, profiles = pair
    result = evaluate(pair, "qualifies_B_to_A__A0_B0")
    assert result["record"]["source_id"] == "source-b"
    assert result["record"]["target_source_id"] == "source-a"
    initialize_private_relationship_workspace(copy, original_workspace=pair[0])
    applied = apply_jev_decisions(copy, [result], apply=True)
    links = projected_related_links("source-a", profiles, applied["registry"]["links"], max_inferred_links=8)
    assert links[0]["primary_relation_type"] == "qualified_by"


@pytest.mark.parametrize("choice,status", [("topical_only_or_none", "no_relationship"), ("insufficient_evidence", "deferred")])
def test_abstention_has_no_selected_evidence(pair, choice, status):
    result = evaluate(pair, choice)
    assert result["status"] == status
    assert result["record"]["selected_evidence"] == []
    assert "relation_id" not in result["record"]


def test_low_confidence_unique_winner_creates_inferred_link(pair):
    result = evaluate(pair, top=0.18)
    assert result["status"] == "accepted"
    assert result["record"]["inferred"] is True
    assert result["record"]["confidence"] < 0.80
    assert result["record"]["probability_margin"] < 0.15


def test_small_positive_winning_margin_is_not_a_cutoff(pair):
    _, copy, request, profiles = pair
    value = artifact(request, top=0.18)
    answer = value["answers"]["relationship"]
    top = answer["choice"]
    second = "topical_only_or_none"
    answer["probabilities"] = {key: (1 - 0.18 - 0.17999999) / (len(answer["probabilities"]) - 2) for key in answer["probabilities"]}
    answer["probabilities"].update({top: 0.18, second: 0.17999999})
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "accepted"
    assert 0 < result["record"]["probability_margin"] < 0.000001


@pytest.mark.parametrize("choice,status", [("topical_only_or_none", "no_relationship"), ("insufficient_evidence", "deferred")])
def test_low_confidence_abstention_winner_does_not_create_link(pair, choice, status):
    result = evaluate(pair, choice=choice, top=0.18)
    assert result["status"] == status
    assert "relation_id" not in result["record"]


def test_historical_thresholds_are_explicit_and_not_reinterpreted(pair):
    _, copy, request, profiles = pair
    value = artifact(request, top=0.18)
    bindings = bind_jev_request(copy, request, profiles=profiles)
    legacy = JevAcceptancePolicy(identity=LEGACY_POLICY_VERSION, confidence_floor=0.80, probability_margin=0.15)
    strict = evaluate_jev_decision(value, request, profiles=profiles, bindings=bindings, policy=legacy)
    current = evaluate_jev_decision(value, request, profiles=profiles, bindings=bindings)
    assert strict["status"] == "deferred" and strict["reasons"] == ["below_confidence_floor"]
    assert strict["record"]["policy_version"] == LEGACY_POLICY_VERSION
    assert current["status"] == "accepted"
    assert current["record"]["relationship_policy_identity"] != strict["record"]["relationship_policy_identity"]
    assert current["decision_key"] != strict["decision_key"]
    with pytest.raises(ValueError, match="unsupported Jev evaluation contract"):
        apply_jev_decisions(copy, [strict])


@pytest.mark.parametrize("change,reason", [
    ("model", "unpinned_provider_or_model"), ("choice", "provider_choice_not_local_winner"),
    ("evidence", "selected_evidence_not_supplied"), ("payload", "pair_request_changed"),
    ("confidence", "confidence_distribution_mismatch"), ("probabilities", "invalid_probabilities"),
])
def test_invalid_provider_data_is_rejected(pair, change, reason):
    _, copy, request, profiles = pair
    value = artifact(request)
    if change == "model":
        value["provenance"]["model"] = "jev-latest"
    elif change == "choice":
        value["answers"]["relationship"]["choice"] = "topical_only_or_none"
    elif change == "evidence":
        value["selected_evidence"][0]["text"] = "Invented evidence."
    elif change == "payload":
        value["provenance"]["request_payload_sha256"] = "0" * 64
    elif change == "confidence":
        value["answers"]["relationship"]["confidence"] = 0.1
    else:
        value["answers"]["relationship"]["probabilities"]["topical_only_or_none"] = float("nan")
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "rejected"
    assert reason in result["reasons"]


def test_tie_is_deferred_deterministically(pair):
    _, copy, request, profiles = pair
    value = artifact(request)
    answer = value["answers"]["relationship"]
    answer["probabilities"] = {key: 0 for key in answer["probabilities"]}
    answer["probabilities"].update(topical_only_or_none=0.5, insufficient_evidence=0.5)
    answer["confidence"] = round((0.5 - 1 / len(answer["probabilities"])) / (1 - 1 / len(answer["probabilities"])), 2)
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "deferred" and result["reasons"] == ["tied_winning_options"]


def test_known_unresolved_attribution_defers_even_a_strong_winner(pair):
    original, copy, request, profiles = pair
    endpoint = replace(request.endpoint_a, ownership={"attribution_status": "unresolved_note_record_mismatch"})
    request = replace(request, endpoint_a=endpoint)
    bindings = bind_jev_request(copy, request, profiles=profiles)
    result = evaluate_jev_decision(artifact(request), request, profiles=profiles, bindings=bindings)
    assert result["status"] == "deferred"
    assert result["reasons"] == ["unresolved_attribution"]


def test_preview_does_not_write_and_application_requires_private_marker(pair):
    original, copy, _, _ = pair
    result = evaluate(pair)
    before = {path.relative_to(copy): path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    assert apply_jev_decisions(copy, [result])["counts"]["accepted"] == 1
    assert before == {path.relative_to(copy): path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="private copy"):
        apply_jev_decisions(copy, [result], apply=True)
    with pytest.raises(ValueError, match="separate"):
        initialize_private_relationship_workspace(original, original_workspace=original)


def test_apply_preserves_source_notes_and_unrelated_registry_links(pair):
    original, copy, request, profiles = pair
    prior = {"relation_id": "existing-human", "source_id": "source-x", "target_source_id": "source-y", "relation_type": "supports",
             "provenance": "human", "active": True, "decision_status": "accepted", "reason": "Existing accepted work."}
    persist_relationship_registry(copy, structural_relations=[prior])
    before = {path.relative_to(original): path.read_bytes() for path in original.rglob("*") if path.is_file()}
    note_bytes = {path: path.read_bytes() for path in (copy / "02_source_memory/notes").glob("*.md")}
    initialize_private_relationship_workspace(copy, original_workspace=original)
    applied = apply_jev_decisions(copy, [evaluate(pair)], apply=True)
    assert applied["counts"]["accepted"] == 1
    assert any(row["relation_id"] == "existing-human" and row == prior for row in applied["registry"]["links"])
    assert before == {path.relative_to(original): path.read_bytes() for path in original.rglob("*") if path.is_file()}
    assert all(path.read_bytes() == value for path, value in note_bytes.items())
    public_yaml = Path(applied["registry"]["path"]).read_text()
    assert "&id" not in public_yaml and "*id" not in public_yaml
    assert reusable_jev_pairs(copy, profiles=profiles) == {("source-a", "source-b")}


def test_negative_preserves_existing_non_jev_accepted_relationship(pair):
    original, copy, _, _ = pair
    prior = {"relation_id": "existing-human", "source_id": "source-a", "target_source_id": "source-b", "relation_type": "supports",
             "provenance": "human", "active": True, "decision_status": "accepted", "reason": "Keep this existing accepted evidence."}
    persist_relationship_registry(copy, structural_relations=[prior])
    initialize_private_relationship_workspace(copy, original_workspace=original)
    result = apply_jev_decisions(copy, [evaluate(pair, "topical_only_or_none")], apply=True)
    assert result["counts"]["deferred"] == 1
    assert result["registry"]["links"] == [prior]


def test_stale_note_retires_only_previously_written_jev_edge(pair):
    original, copy, request, profiles = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    value = evaluate(pair)
    apply_jev_decisions(copy, [value], apply=True)
    note = copy / "02_source_memory/notes/a.md"
    note.write_text(note.read_text() + "\nA changed qualification.\n")
    assert reusable_jev_pairs(copy, profiles=profiles) == set()
    result = apply_jev_decisions(copy, [value], apply=True)
    assert result["counts"]["deferred"] == 1
    assert result["registry"]["links"] == []
    assert result["registry"]["relations"][0]["decision_status"] == "stale_evidence"


def test_tampered_policy_output_cannot_be_applied(pair):
    value = evaluate(pair)
    value["record"]["relation_type"] = "undermines"
    with pytest.raises(ValueError, match="modified"):
        apply_jev_decisions(pair[1], [value])


def test_copied_marker_cannot_authorize_a_different_path(pair, tmp_path):
    original, copy, _, _ = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    other = tmp_path / "other-copy"
    shutil.copytree(copy, other)
    with pytest.raises(ValueError, match="path-bound"):
        apply_jev_decisions(other, [evaluate(pair)], apply=True)


def test_git_checkout_cannot_be_marked_private(pair):
    (pair[1] / ".git").mkdir()
    with pytest.raises(ValueError, match="Git"):
        initialize_private_relationship_workspace(pair[1], original_workspace=pair[0])


def test_preflight_scope_guard_is_read_only_and_rejects_original(pair):
    original, copy, _, _ = pair
    with pytest.raises(ValueError, match="private copy"):
        validate_private_relationship_workspace(original)
    initialize_private_relationship_workspace(copy, original_workspace=original)
    before = {path: path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    assert validate_private_relationship_workspace(copy)["workspace"] == str(copy)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_legacy_schema_semantic_rows_are_not_migrated_by_jev_application(pair):
    original, copy, _, _ = pair
    prior = {"relation_id": "legacy-unrelated", "source_id": "source-x", "target_source_id": "source-y", "relation_type": "supports",
             "provenance": "probabilistic_relationship_adjudication", "active": True, "decision_status": "accepted",
             "verification_status": "confirmed", "reason": "Preserve this verified historical accepted relationship."}
    write_yaml(copy / "02_source_memory/indexes/typed_links.yml", {"registry_schema_version": "3", "relations": [prior], "links": [prior]})
    initialize_private_relationship_workspace(copy, original_workspace=original)
    result = apply_jev_decisions(copy, [evaluate(pair)], apply=True)
    assert any(row == prior for row in result["registry"]["relations"])
    assert any(row == prior for row in result["registry"]["links"])


def test_negative_and_deferred_decisions_are_reusable_and_evidence_changes_are_not(pair):
    original, copy, request, profiles = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    apply_jev_decisions(copy, [evaluate(pair, "topical_only_or_none")], apply=True)
    assert reusable_jev_pairs(copy, requests=[request]) == {("source-a", "source-b")}
    changed = replace(request, endpoint_a=replace(request.endpoint_a, note_text=request.endpoint_a.note_text + "New selected context."))
    assert reusable_jev_pairs(copy, requests=[changed]) == set()
    apply_jev_decisions(copy, [evaluate(pair, "insufficient_evidence")], apply=True)
    assert reusable_jev_pairs(copy, profiles=profiles) == {("source-a", "source-b")}


def test_new_batch_pair_hash_accepts_original_evidence(pair):
    _, copy, request, profiles = pair
    value = artifact(request)
    value["provenance"].update(template_version=BATCH_TEMPLATE_VERSION, pair_request_sha256=value["provenance"]["request_payload_sha256"],
                               source_template_version=TEMPLATE_VERSION,
                               request_payload_sha256="b" * 64, batch_context_sha256="c" * 64)
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "accepted"


def test_unrelated_bindings_do_not_change_pair_identity_or_prevent_reuse(pair):
    original, copy, request, profiles = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    bindings = bind_jev_request(copy, request, profiles=profiles)
    expected = evaluate_jev_decision(artifact(request), request, profiles=profiles, bindings=bindings)
    unrelated = copy / "02_source_memory/notes/c.md"
    unrelated.write_text("# An unrelated source note.\n")
    bindings["source-c"] = {"source_id": "source-c", "note_id": "note-c", "note_path": "02_source_memory/notes/c.md",
                            "note_sha256": sha256(unrelated.read_bytes()).hexdigest(), "profile_hash": "c" * 64}
    evaluated = evaluate_jev_decision(artifact(request), request, profiles=profiles, bindings=bindings)
    assert set(evaluated["record"]["bindings"]) == {"source-a", "source-b"}
    assert evaluated["decision_key"] == expected["decision_key"]
    apply_jev_decisions(copy, [evaluated], apply=True)
    unrelated.write_text("# An edited unrelated source note.\n")
    bindings["source-c"]["note_sha256"] = sha256(unrelated.read_bytes()).hexdigest()
    assert reusable_jev_pairs(copy, bindings=bindings) == {("source-a", "source-b")}


def test_reuse_accepts_lean_bindings_without_preloaded_note_bytes(pair, monkeypatch):
    import auto_zettelkasten.jev_relationships as module
    original, copy, request, profiles = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    value = evaluate(pair)
    apply_jev_decisions(copy, [value], apply=True)
    lean = {sid: {key: binding[key] for key in ("source_id", "note_id", "note_path", "profile_hash")}
            for sid, binding in value["record"]["bindings"].items()}
    actual = module._read_note
    reads = []
    monkeypatch.setattr(module, "_read_note", lambda path: reads.append(path) or actual(path))
    assert reusable_jev_pairs(copy, bindings=lean) == {("source-a", "source-b")}
    assert len(reads) == 4
    assert sum(path.parent.name == "notes" for path in reads) == 2
    lean["source-a"]["profile_hash"] = "0" * 64
    reads.clear()
    assert reusable_jev_pairs(copy, bindings=lean) == set()
    assert reads == []


def test_frozen_evaluation_keeps_only_endpoint_profiles(pair):
    _, copy, request, profiles = pair
    unrelated = EvidenceProfile(source_id="source-c", note_id="note-c", context={"thesis": "Unrelated saved profile."})
    value = evaluate_jev_decision(artifact(request), request, profiles=[*profiles, unrelated],
                                  bindings=bind_jev_request(copy, request, profiles=profiles))
    assert {row["source_id"] for row in value["evaluation_input"]["profiles"]} == {"source-a", "source-b"}


@pytest.mark.parametrize("field,value", [
    ("provider", "other-provider"), ("template_version", "relationship-decision-v1"),
    ("prompt_version", "old-prompt"), ("source_template_version", "relationship-decision-v1"),
    ("output_contract", "another-contract"), ("decision_schema_version", "0"),
])
def test_reuse_rejects_untrusted_provider_or_template(pair, field, value):
    from auto_zettelkasten.files import read_yaml
    original, copy, _, _ = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    apply_jev_decisions(copy, [evaluate(pair)], apply=True)
    path = copy / "02_source_memory/indexes/typed_links.yml"
    registry = read_yaml(path)
    assert reusable_jev_pairs(copy) == {("source-a", "source-b")}
    registry["jev_pair_decisions"][0][field] = value
    write_yaml(path, registry)
    assert reusable_jev_pairs(copy) == set()


@pytest.mark.parametrize("template", ["relationship-decision-v1", "relationship-batch-v0"])
def test_evaluation_rejects_obsolete_templates(pair, template):
    _, copy, request, profiles = pair
    value = artifact(request)
    value["provenance"]["template_version"] = template
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "rejected"
    assert result["reasons"] == ["unsupported_decision_template"]


@pytest.mark.parametrize("source_template", [None, "relationship-decision-v1"])
def test_batch_evaluation_requires_explicit_current_source_template(pair, source_template):
    _, copy, request, profiles = pair
    value = artifact(request)
    value["provenance"].update(template_version=BATCH_TEMPLATE_VERSION, source_template_version=source_template)
    result = evaluate_jev_decision(value, request, profiles=profiles, bindings=bind_jev_request(copy, request, profiles=profiles))
    assert result["status"] == "rejected"
    assert result["reasons"] == ["unsupported_decision_template"]


def test_edge_freshness_reads_only_pair_dependencies_without_mutation(pair, monkeypatch):
    import auto_zettelkasten.jev_relationships as module
    _, copy, _, _ = pair
    row = evaluate(pair)["record"]
    before = {path: path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    original_read = module._read_note
    calls = []
    monkeypatch.setattr(module, "_read_note", lambda path: calls.append(path) or original_read(path))
    memo = {}
    assert jev_relationship_is_fresh(copy, row, memo=memo)
    assert jev_relationship_is_fresh(copy, row, memo=memo)
    assert len(calls) == 4
    assert before == {path: path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    note = copy / "02_source_memory/notes/a.md"
    note.write_text(note.read_text().replace("reduce conflict", "increase conflict"))
    assert not jev_relationship_is_fresh(copy, row, memo={})


@pytest.mark.parametrize("change", ["provider", "profile", "evidence_hash", "invented_evidence", "note_id", "path"])
def test_edge_freshness_rejects_stale_or_invalid_evidence(pair, change):
    _, copy, _, _ = pair
    row = evaluate(pair)["record"]
    assert jev_relationship_is_fresh(copy, row)
    if change == "provider":
        row["provider"] = "other"
    elif change == "profile":
        profile = copy / row["bindings"]["source-a"]["profile_path"]
        profile.write_text(profile.read_text() + "\n# Changed profile bytes\n")
    elif change == "evidence_hash":
        row["selected_evidence"][0]["content_sha256"] = "0" * 64
    elif change == "invented_evidence":
        text = "A fabricated passage that is absent from these notes."
        row["selected_evidence"][0].update(text=text, content_sha256=sha256(text.encode()).hexdigest())
    elif change == "note_id":
        row["source_note_id"] = "note-c"
    else:
        row["bindings"]["source-a"]["note_path"] = "../outside.md"
    assert not jev_relationship_is_fresh(copy, row)


@pytest.mark.parametrize("kwargs", [
    {"confidence_floor": 0.79}, {"probability_margin": 0.14}, {"identity": "other"},
    {"identity": LEGACY_POLICY_VERSION},
    {"identity": LEGACY_POLICY_VERSION, "confidence_floor": 0.79, "probability_margin": 0.15},
    {"identity": LEGACY_POLICY_VERSION, "confidence_floor": 0.80, "probability_margin": 0.14},
    {"confidence_floor": float("nan")}, {"probability_margin": True},
])
def test_policy_identity_cannot_silently_change_its_acceptance_rules(kwargs):
    with pytest.raises(ValueError):
        JevAcceptancePolicy(**kwargs).validate()


@pytest.mark.parametrize("template", ["relationship-decision-v2", "relationship-batch-v1"])
def test_historical_accepted_edges_remain_readable_but_are_not_current_pair_decisions(pair, template):
    from auto_zettelkasten.files import read_yaml
    original, copy, request, profiles = pair
    initialize_private_relationship_workspace(copy, original_workspace=original)
    current = evaluate(pair)
    apply_jev_decisions(copy, [current], apply=True)
    path = copy / "02_source_memory/indexes/typed_links.yml"
    registry = read_yaml(path)
    legacy = JevAcceptancePolicy(identity=LEGACY_POLICY_VERSION, confidence_floor=0.80, probability_margin=0.15)
    for field in ("links", "relations", "jev_pair_decisions"):
        for row in registry.get(field, []):
            if row.get("policy_version") == CURRENT_POLICY_VERSION:
                row.update(policy_version=LEGACY_POLICY_VERSION, relationship_policy_identity=stable_hash(asdict(legacy)),
                           template_version=template, source_template_version="relationship-decision-v2",
                           prompt_version=template)
    write_yaml(path, registry)
    historical = registry["jev_pair_decisions"][0]
    before = path.read_bytes()
    assert jev_relationship_is_fresh(copy, historical)
    assert reusable_jev_pairs(copy, profiles=profiles, requests=[request]) == set()
    assert path.read_bytes() == before


@pytest.mark.parametrize("template,source_template", [
    ("relationship-batch-v1", TEMPLATE_VERSION),
    (BATCH_TEMPLATE_VERSION, "relationship-decision-v2"),
])
def test_historical_and_current_batch_source_templates_cannot_be_mixed(pair, template, source_template):
    row = evaluate(pair)["record"]
    row.update(template_version=template, prompt_version=template, source_template_version=source_template)
    assert not jev_relationship_is_fresh(pair[1], row)
