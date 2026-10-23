"""Read-only recall must not return Jev decisions bound to changed notes."""
from copy import deepcopy

from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.jev_relationships import (
    apply_jev_decisions, initialize_private_relationship_workspace,
)
from auto_zettelkasten.local_tools import load_local_retrieval
from auto_zettelkasten.relationships import persist_relationship_registry

from test_jev_relationships import evaluate, pair


def _catalogue(workspace):
    write_yaml(workspace / "02_source_memory/indexes/source_catalogue.yml", {
        "sources": [{"source_id": "source-" + side, "note_id": "note-" + side,
                     "title": "Synthetic " + side} for side in "ab"],
    })


def test_recall_excludes_jev_edge_after_note_changes_without_profile_change(pair):
    original, copy, _, _ = pair
    _catalogue(copy)
    initialize_private_relationship_workspace(copy, original_workspace=original)
    applied = apply_jev_decisions(copy, [evaluate(pair)], apply=True)
    assert applied["counts"]["accepted"] == 1
    assert len(load_local_retrieval(copy)["relationships"]) == 1
    profiles = {path: path.read_bytes() for path in (copy / "02_source_memory/profiles").glob("*")}
    original_bytes = {path: path.read_bytes() for path in original.rglob("*") if path.is_file()}
    note = copy / "02_source_memory/notes/a.md"
    note.write_text(note.read_text().replace(
        "Institutions reduce conflict when credible commitments are enforced.",
        "The revised thesis no longer makes that claim.",
    ))
    before = {path: path.read_bytes() for path in copy.rglob("*") if path.is_file()}

    loaded = load_local_retrieval(copy)

    assert loaded["relationships"] == []
    assert loaded["status"]["excluded_relationships"] == 1
    assert all(path.read_bytes() == value for path, value in profiles.items())
    assert all(path.read_bytes() == value for path, value in original_bytes.items())
    assert before == {path: path.read_bytes() for path in copy.rglob("*") if path.is_file()}


def test_recall_preserves_legacy_relationship_without_jev_bindings(pair):
    _, copy, _, _ = pair
    _catalogue(copy)
    prior = {"relation_id": "existing-human", "source_id": "source-a", "target_source_id": "source-b",
             "relation_type": "supports", "provenance": "human", "active": True,
             "decision_status": "accepted", "reason": "Existing accepted evidence."}
    persist_relationship_registry(copy, structural_relations=[deepcopy(prior)])
    note = copy / "02_source_memory/notes/a.md"
    note.write_text(note.read_text() + "\nA changed qualification.\n")

    loaded = load_local_retrieval(copy)

    assert loaded["relationships"] == [{**prior, "freshness": "recorded_unverified"}]
    assert loaded["status"]["excluded_relationships"] == 0
