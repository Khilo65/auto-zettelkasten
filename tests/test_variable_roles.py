from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.indexes import build_source_catalogue, lean_discovery_projection
from auto_zettelkasten.models import EvidenceProfile, LiteratureMapRequest, MapRequest
from auto_zettelkasten.notes import read_note, render_atomic_note
from auto_zettelkasten.profiles import load_profile, save_profile
from auto_zettelkasten.readers import (
    COMPACT_VARIABLE_ROLE_INSTRUCTION, VARIABLE_ROLE_INSTRUCTION,
    VARIABLE_ROLE_LINKING_INSTRUCTION, CodexReader, DeepSeekReader,
    _chunk_system_prompt, _literature_family_plan_system_prompt,
    _profile_system_prompt, _relationship_candidate_system_prompt,
    _source_bundle_system_prompt, _system_prompt,
)


@pytest.mark.parametrize("builder", [_system_prompt, _source_bundle_system_prompt])
def test_source_instructions_preserve_roles_without_forcing_a_design(builder):
    assert VARIABLE_ROLE_INSTRUCTION in builder()
    assert "does not establish an effect" in VARIABLE_ROLE_INSTRUCTION
    assert "without a compulsory variable checklist" in VARIABLE_ROLE_INSTRUCTION
    assert "design choices and established findings" in _chunk_system_prompt()
    assert COMPACT_VARIABLE_ROLE_INSTRUCTION in _source_bundle_system_prompt()
    assert COMPACT_VARIABLE_ROLE_INSTRUCTION in _profile_system_prompt()
    for prompt in (_literature_family_plan_system_prompt(), _relationship_candidate_system_prompt()):
        assert VARIABLE_ROLE_LINKING_INSTRUCTION in prompt


def _capture_requests(monkeypatch, reader, descriptions, workspace):
    captured = []

    def call(self, system, user, **kwargs):
        captured.append((system, json.loads(user)))
        return {"candidates": [], "job_outcomes": []}

    monkeypatch.setattr(type(reader), "_authorize_request", lambda self: None)
    monkeypatch.setattr(type(reader), "_literature_json_call", call)
    request = LiteratureMapRequest(workspace, provider="ollama", model="test")
    with deny_codex_attempts():
        reader.plan_literature_families(descriptions, request)
        reader.select_relationship_candidates(descriptions, request)
    assert len(captured) == 2
    return [captured[0][1]["profiles"][0], captured[1][1]["focus_profiles"][0]]


def test_standalone_profile_preserves_all_method_roles_in_actual_requests(tmp_path, monkeypatch):
    methods = ["Observational panel design.", "Treatment: access; control: income; moderator: age."]
    profile = EvidenceProfile(
        source_id="source-a", note_id="note-a", methods=methods,
        outcomes=["participation"], mechanisms=["trust as proposed mediator"],
        context={"title": "A study", "thesis": "Access and participation."},
    )
    save_profile(tmp_path / "profiles", profile)
    restored = load_profile(tmp_path / "profiles", "note-a")
    catalogue = build_source_catalogue(tmp_path, [restored], [])
    descriptions = lean_discovery_projection([restored], read_yaml(Path(catalogue["catalogue_path"])))
    for supplied in _capture_requests(monkeypatch, DeepSeekReader(), descriptions, tmp_path):
        for method in methods:
            assert method in supplied["method"]
        assert supplied["facets"]["outcome"] == ["participation"]


@pytest.mark.parametrize("reader_type", [CodexReader, DeepSeekReader])
@pytest.mark.parametrize("method,mechanisms,outcomes", [
    ("Outcome: participation; treatment: access; mediator: trust; moderator: age; control: income.",
     ["trust as proposed mediator"], ["participation"]),
    ("Comparative case study; sequence: access then participation; rival explanation: income.",
     ["trust as proposed mechanism"], ["participation"]),
    ("Interpretive reading of institutional meanings.", [], []),
])
def test_bundle_roles_reach_note_profile_and_both_model_requests(
    tmp_path, monkeypatch, reader_type, method, mechanisms, outcomes,
):
    from conftest import FakeZotero
    from test_v013_source_bundle import BundleReader, _bundle_payload
    from auto_zettelkasten.api import run_map
    from auto_zettelkasten.files import read_yaml

    payload = _bundle_payload()
    payload["analysis_sections"]["method_and_research_design"] = method
    payload["compact_profile"].update(method_or_knowledge_basis=method,
                                      mechanisms=mechanisms, outcomes=outcomes)
    calls = []

    def generate(self, system, user, *args, **kwargs):
        calls.append(system)
        return json.dumps(payload)

    monkeypatch.setattr(reader_type, "_authorize_request", lambda self: None)
    monkeypatch.setattr(reader_type, "_generate_with_reasoning", generate)
    reader = reader_type(model="gpt-5.6-luna" if reader_type is CodexReader else "deepseek-chat")
    with deny_codex_attempts():
        result = reader.read_source_bundle(method, {"_source_context": {
            "source_id": "source-zotero-A1", "zotero_key": "A1",
        }})
    assert calls == [_source_bundle_system_prompt()]
    assert method in render_atomic_note({"title": "Study"}, result["analysis_sections"])

    class SavedReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            result = super().read_source_bundle(text, metadata, question)
            result["analysis_sections"]["method_and_research_design"] = method
            result["compact_profile"].update(payload["compact_profile"])
            return result

    item = {"key": "A1", "data": {"key": "A1", "title": "Study", "itemType": "journalArticle"}}
    with deny_codex_attempts():
        report = run_map(MapRequest(tmp_path, provider="ollama", model="test"),
                         client=FakeZotero([item]), reader=SavedReader(), run_id="roles")
    assert report.validated_note_count == 1
    note = read_note(tmp_path / report.items[0]["note_path"])
    assert method in (tmp_path / report.items[0]["note_path"]).read_text()
    profile = read_yaml(next((tmp_path / "02_source_memory/profiles").glob("*.yml")))["profile"]
    assert "evidence_anchors" not in profile
    assert profile["context"]["method_or_knowledge_basis"] == method
    catalogue = build_source_catalogue(tmp_path, [profile], [note["frontmatter"]])
    descriptions = lean_discovery_projection([profile], read_yaml(Path(catalogue["catalogue_path"])))
    for supplied in _capture_requests(monkeypatch, reader, descriptions, tmp_path):
        assert supplied["method"] == method
        assert supplied["facets"].get("outcome", []) == outcomes
        assert supplied["facets"].get("mechanism", []) == mechanisms
