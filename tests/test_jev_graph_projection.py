from __future__ import annotations

import json
from hashlib import sha256
import shutil

import pytest
import yaml

from auto_zettelkasten.documents import DocumentService
from auto_zettelkasten.relationships import RELATIONSHIP_REGISTRY_SCHEMA_VERSION


@pytest.fixture
def workspace(tmp_path):
    notes = tmp_path / "02_source_memory/notes"
    notes.mkdir(parents=True)
    for identity in ("a", "b", "other"):
        (notes / f"{identity}.md").write_text(f"---\nnote_id: note-{identity}\n---\n# {identity}\n\nSynthetic thesis.\n")
    (tmp_path / "auto-zettelkasten.yml").write_text("engine_version: test\n")
    registry = tmp_path / "02_source_memory/indexes/typed_links.yml"
    registry.parent.mkdir(parents=True)
    service = DocumentService(journal_root=tmp_path / "journal")
    yield tmp_path, registry, service
    service.close()


def edge(**updates):
    return {
        "relation_id": "jev:test", "relation_type": "qualifies", "source_note_id": "note-a", "target_note_id": "note-b",
        "active": True, "decision_status": "accepted", "provider": "typesafe", "model": "jev-1.13.0",
        "output_contract": "synthetic-display-v1", "verification_status": "policy_accepted",
        "template_version": "relationship-decision-v2", "policy_version": "jev-private-policy-v1",
        "confidence": 0.91, "top_probability": 0.92, "probability_margin": 0.85,
        "probabilities": {"qualifies_A_to_B__A0_B0": 0.92, "topical_only_or_none": 0.08},
        "evidence_basis": "note_summary",
        "selected_evidence": [{"note_id": "note-a", "passage_id": "a-thesis", "text": "A synthetic condition.",
                               "locator": {"kind": "note_lines", "heading": "Thesis", "line_start": 7,
                                           "note_relative_path": "/private/secret.md", "original_document_locator_verified": False},
                               "attribution": "Existing note summary.", "content_sha256": "a" * 64}],
        "private_rationale": "PRIVATE-RESEARCH", **updates,
    }


def save(registry, rows, **other):
    registry.write_text(yaml.safe_dump({"registry_schema_version": RELATIONSHIP_REGISTRY_SCHEMA_VERSION, "links": rows, **other}, sort_keys=False))


def collect(service, identity):
    result = []
    page = service.index_page(identity)
    while True:
        result.extend(page["relations"])
        if page["complete"]:
            return result, page
        page = service.index_page(identity, page["next_cursor"])


def test_paged_and_snapshot_preserve_bounded_jev_metadata_without_private_tree(workspace):
    root, registry, service = workspace
    row = edge()
    save(registry, [row], private_payload={"credentials": "DO-NOT-PUBLISH"})
    opened = service.open(root)
    snapshot = service.index(opened["id"])
    rows, page = collect(service, opened["id"])
    assert rows == snapshot["relations"]
    projected = rows[0]
    assert projected["confidence"] == 0.91
    assert projected["top_probability"] == 0.92
    assert projected["probabilities"] == row["probabilities"]
    assert projected["verification_status"] == "policy_accepted"
    assert projected["policy_version"] == "jev-private-policy-v1"
    assert projected["selected_evidence"] == [{"path": "02_source_memory/notes/a.md", "passage_id": "a-thesis",
                                             "text": "A synthetic condition.", "attribution": "Existing note summary.",
                                             "locator": {"kind": "note_lines", "heading": "Thesis", "line_start": 7}}]
    serialized = json.dumps(rows)
    assert "PRIVATE-RESEARCH" not in serialized
    assert "DO-NOT-PUBLISH" not in serialized
    assert "content_sha256" not in serialized
    assert "/private/" not in serialized
    assert not page["truncated"]


def test_evidence_must_resolve_to_an_existing_relation_endpoint_and_is_bounded(workspace):
    root, registry, service = workspace
    row = edge(selected_evidence=[{"note_id": "note-other", "text": "Wrong note."},
                                 {"note_id": "note-b", "text": "x" * 12000, "locator": {"line_start": -2}}])
    save(registry, [row])
    opened = service.open(root)
    rows, _ = collect(service, opened["id"])
    assert rows[0]["selected_evidence"] == [{"path": "02_source_memory/notes/b.md", "text": "x" * 1600}]


def test_unknown_legacy_confidence_is_not_invented_and_citations_remain_distinct(workspace):
    root, registry, service = workspace
    legacy = {key: value for key, value in edge().items() if key in {"relation_id", "relation_type", "source_note_id", "target_note_id", "active", "decision_status"}}
    citation = {**legacy, "relation_id": "citation:test", "relation_type": "cites"}
    save(registry, [legacy, citation])
    opened = service.open(root)
    rows, _ = collect(service, opened["id"])
    assert {row["type"] for row in rows} == {"qualifies", "cites"}
    assert all("confidence" not in row and "selected_evidence" not in row for row in rows)


@pytest.mark.parametrize("confidence", [True, "0.9", float("inf"), float("nan"), -1, 2])
def test_invalid_confidence_is_omitted_without_zero_default(workspace, confidence):
    root, registry, service = workspace
    save(registry, [edge(confidence=confidence)])
    opened = service.open(root)
    rows, _ = collect(service, opened["id"])
    assert len(rows) == 1 and "confidence" not in rows[0]


def test_public_yaml_aliases_are_not_expanded_into_display_evidence(workspace):
    root, registry, service = workspace
    text = yaml.safe_dump({"registry_schema_version": RELATIONSHIP_REGISTRY_SCHEMA_VERSION,
                           "links": [edge(selected_evidence=[])]}, sort_keys=False)
    text = text.replace("selected_evidence: []", "selected_evidence: &public []")
    registry.write_text(text)
    opened = service.open(root)
    rows, page = collect(service, opened["id"])
    assert len(rows) == 1 and "selected_evidence" not in rows[0]
    assert not page["truncated"]


def accepted_jev_row(root, *, long_passage=False, evaluation=False, winning_probability=1.0):
    from decimal import Decimal
    from auto_zettelkasten.jev_decisions import (
        MODEL_ID, DecisionRequest, Endpoint, EvidencePassage, _canonical_json, _decision_artifact,
        _relationship_options, build_payload,
    )
    from auto_zettelkasten.jev_relationships import bind_jev_request, evaluate_jev_decision
    from auto_zettelkasten.models import EvidenceProfile
    from auto_zettelkasten.profiles import save_profile

    profiles, endpoints = [], []
    for side in ("a", "b"):
        claim = f"{side.upper()} synthetic condition." + (" x" * 4500 if long_passage else "")
        note = f"---\nsource_id: source-{side}\nnote_id: note-{side}\n---\n# {side}\n\n## Thesis\n{claim}\n"
        path = root / f"02_source_memory/notes/{side}.md"
        path.write_text(note)
        profile = EvidenceProfile(source_id=f"source-{side}", note_id=f"note-{side}", context={"note_path": str(path)})
        save_profile(root / "02_source_memory/profiles", profile)
        profiles.append(profile)
        passage = EvidencePassage(f"{side}-thesis", claim, {"kind": "note_lines", "note_relative_path": str(path.relative_to(root)), "heading": "Thesis"},
                                  "Existing note summary", sha256(claim.encode()).hexdigest())
        endpoints.append(Endpoint(f"source-{side}", f"note-{side}", note, {"status": "note_only", "original_document_supplied": False},
                                  {"attribution_status": "note-derived; original author attribution unverified"}, (passage,)))
    request = DecisionRequest("synthetic-workspace", "synthetic-library", *endpoints)
    choice = "qualifies_A_to_B__A0_B0"
    options = _relationship_options(request)
    probabilities = {key: (winning_probability if key == choice else
                           (1 - winning_probability) / (len(options) - 1)) for key in options}
    confidence = max(0.0, (winning_probability - 1 / len(options)) / (1 - 1 / len(options)))
    response = {"model": MODEL_ID, "usage": {"input_tokens": 1000, "output_tokens": 100},
                "answers": {"relationship": {"type": "choice", "choice": choice,
                            "probabilities": probabilities, "confidence": confidence}}}
    payload_hash = sha256(_canonical_json(build_payload(request)).encode()).hexdigest()
    artifact = _decision_artifact(request, response, "a" * 64, payload_hash, Decimal("0.000042"))
    value = evaluate_jev_decision(artifact, request, profiles=profiles, bindings=bind_jev_request(root, request, profiles=profiles))
    assert value["status"] == "accepted"
    return value if evaluation else value["record"]


@pytest.mark.parametrize("long_passage", [False, True])
def test_jev_graph_omits_changed_note_even_when_profile_and_registry_are_unchanged(workspace, long_passage):
    root, registry, service = workspace
    row = accepted_jev_row(root, long_passage=long_passage)
    save(registry, [row])
    registry_before = registry.read_bytes()
    profile = root / "02_source_memory/profiles/note-a.yml"
    profile_before = profile.read_bytes()
    opened = service.open(root)
    assert len(service.index(opened["id"])["relations"]) == 1
    rows, _ = collect(service, opened["id"])
    assert len(rows) == 1
    assert len(rows[0]["selected_evidence"][0]["text"]) <= 1600
    assert "bindings" not in rows[0]
    path = root / "02_source_memory/notes/a.md"
    path.write_text(path.read_text().replace("A synthetic condition.", "A changed condition."))
    assert service.index(opened["id"])["relations"] == []
    rows, page = collect(service, opened["id"])
    assert rows == []
    assert any("source evidence changed" in warning for warning in page["warnings"])
    assert profile.read_bytes() == profile_before
    assert registry.read_bytes() == registry_before


def test_persisted_jev_registry_projects_until_source_changes(workspace):
    from auto_zettelkasten.jev_relationships import apply_jev_decisions, initialize_private_relationship_workspace

    root, _, _ = workspace
    evaluation = accepted_jev_row(root, evaluation=True)
    copied = root.parent / (root.name + "-copy")
    shutil.copytree(root, copied)
    initialize_private_relationship_workspace(copied, original_workspace=root)
    result = apply_jev_decisions(copied, [evaluation], apply=True)
    assert result["counts"]["accepted"] == 1
    service = DocumentService(journal_root=root.parent / (root.name + "-journal"))
    try:
        opened = service.open(copied)
        assert len(service.index(opened["id"])["relations"]) == 1
        rows, _ = collect(service, opened["id"])
        assert len(rows) == 1
        note = copied / "02_source_memory/notes/a.md"
        note.write_text(note.read_text().replace("A synthetic condition.", "A changed condition."))
        assert service.index(opened["id"])["relations"] == []
        assert collect(service, opened["id"])[0] == []
    finally:
        service.close()


def test_winner_only_low_confidence_link_reaches_graph_and_recall(workspace):
    from auto_zettelkasten.jev_relationships import apply_jev_decisions, initialize_private_relationship_workspace
    from auto_zettelkasten.local_tools import load_local_retrieval

    root, _, _ = workspace
    evaluation = accepted_jev_row(root, evaluation=True, winning_probability=0.31)
    assert evaluation["record"]["confidence"] < 0.80
    (root / "02_source_memory/indexes/source_catalogue.yml").write_text(yaml.safe_dump({
        "sources": [{"source_id": f"source-{side}", "note_id": f"note-{side}",
                     "title": f"Synthetic {side}"} for side in "ab"],
    }))
    copied = root.parent / (root.name + "-winner-copy")
    shutil.copytree(root, copied)
    initialize_private_relationship_workspace(copied, original_workspace=root)
    assert apply_jev_decisions(copied, [evaluation], apply=True)["counts"]["accepted"] == 1
    service = DocumentService(journal_root=root.parent / (root.name + "-winner-journal"))
    try:
        opened = service.open(copied)
        graph = service.index(opened["id"])["relations"]
        assert len(graph) == 1
        assert graph[0]["policy_version"] == "jev-private-winner-only-v1"
        assert graph[0]["confidence"] == pytest.approx(evaluation["record"]["confidence"])
        assert graph[0]["type"] == "qualifies"
        recall = load_local_retrieval(copied)["relationships"]
        assert len(recall) == 1
        assert recall[0]["policy_version"] == "jev-private-winner-only-v1"
        assert recall[0]["selected_evidence"]
    finally:
        service.close()
