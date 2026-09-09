"""Current source/profile contracts share note-based behavior across providers."""
from __future__ import annotations

import json

import pytest

from auto_zettelkasten import profiles, readers
from auto_zettelkasten.models import EvidenceAnchor, EvidenceFinding, EvidenceProfile, SourceAnalysisBundle
from auto_zettelkasten.notes import render_atomic_note


def _note() -> str:
    return render_atomic_note(
        {"note_id": "n1", "source_id": "s1", "title": "Participation",
         "note_status": "analytical_atomic_note", "source_scope": "full_document",
         "source_coverage": {"gate": "passed"}},
        {key: ("Participation was associated with 12% higher trust (Table 2, p. 14)."
               if key in {"evidence_and_data", "detailed_findings"} else "Source-grounded discussion.")
         for key in readers.REQUIRED_SECTION_KEYS},
    )


def _bundle() -> dict:
    return {
        "analysis_sections": {key: "Source-grounded discussion." for key in readers.REQUIRED_SECTION_KEYS},
        "compact_profile": {"thesis": "Participation matters."},
        "literature_positions": [],
        "observed_bibliographic_identity": {"title": "Participation", "creators": [], "date": ""},
    }


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
def test_provider_routes_use_same_current_source_contract(monkeypatch, provider):
    reader = (readers.CodexReader("gpt-5.6-luna", allow_cloud=True) if provider == "codex"
              else readers.DeepSeekReader(allow_cloud=True))
    monkeypatch.setattr(reader, "_authorize_request", lambda: None)
    calls = []

    def generate(system, user, *args, **kwargs):
        calls.append((system, user, kwargs))
        return json.dumps(_bundle())

    monkeypatch.setattr(reader, "_generate_with_reasoning", generate)
    result = reader.read_source_bundle("Original source text.", {"_source_context": {"source_id": "s1"}})
    assert len(calls) == 1
    assert "(Author, Date, p. N)" in calls[0][0]
    assert calls[0][2]["output_contract"] == "source_bundle"
    assert "evidence_anchors" not in calls[0][0] + calls[0][1]
    assert "FINAL QUANTITATIVE COPY GATE" not in calls[0][1]
    assert result["bundle_schema_version"] == "2"
    assert result["source_identity"]["source_id"] == "s1"
    assert "evidence_anchors" not in result
    assert result["analysis_sections"]["detailed_findings"] == "Source-grounded discussion."


def test_current_source_requires_note_content_not_anchor_substitutes():
    payload = _bundle()
    del payload["analysis_sections"]["evidence_and_data"]
    payload["evidence_anchors"] = [{"claim": "A result", "locator": "p. 1"}]
    with pytest.raises(readers.ProviderError):
        readers._parse_source_bundle_response(payload, label="test", expected_identity={"source_id": "s1"})
    assert "evidence_anchors" not in readers.CODEX_OUTPUT_CONTRACTS["source_bundle"]["properties"]
    assert "evidence_anchors" not in readers.CODEX_OUTPUT_CONTRACTS["evidence_profile"]["properties"]
    assert "findings" not in readers.CODEX_OUTPUT_CONTRACTS["evidence_profile"]["properties"]


def test_current_profiles_never_extract_or_enrich_anchors(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy claim extraction must not run")

    monkeypatch.setattr(profiles, "_extract_findings", forbidden)
    monkeypatch.setattr(profiles, "_extract_central_argument_findings", forbidden)
    monkeypatch.setattr(profiles, "_enrich_profile_v12_records", forbidden)
    note = _note()
    mechanical = profiles.deterministic_profile(note)
    refreshed, _ = profiles.augment_profile_from_committed_note(
        mechanical, note, source_set_id="test", provider="deterministic", model="test")
    for profile in (mechanical, refreshed):
        result = profiles.profile_to_dict(profile)
        assert result["profile_schema_version"] == "1.4"
        assert "findings" not in result and "evidence_anchors" not in result
        assert profiles.validate_profile(profile, require_substantive=True).passed
    prompt = profiles.build_profile_prompt(note)
    assert "evidence_anchors" not in prompt
    assert "support_envelope" not in prompt
    assert "12%" in prompt
    current = profiles.profile_dependency_payload(note, source_set_id="test", provider="test", model="test", policy={})
    assert "anchor_algorithm_version" not in current


def test_explicit_historical_models_retain_anchors_without_promoting_version():
    anchor = EvidenceAnchor(source_id="s1", claim="Original claim", locator="p. 1")
    old_bundle = SourceAnalysisBundle(bundle_schema_version="1", source_identity={"source_id": "s1"},
                                     analysis_sections={"thesis": "Original thesis"}, evidence_anchors=[anchor])
    assert SourceAnalysisBundle.from_dict(old_bundle.to_dict()).to_dict() == old_bundle.to_dict()
    old_profile = EvidenceProfile(profile_schema_version="1.3", source_id="s1", evidence_anchors=[anchor])
    assert profiles.profile_to_dict(profiles.profile_from_dict(old_profile.to_dict())) == old_profile.to_dict()
    current = EvidenceProfile(source_id="s1", findings=[EvidenceFinding(claim="A finding")], evidence_anchors=[anchor])
    assert current.evidence_anchors == [] and current.findings == []
    assert "evidence_anchors" not in current.to_dict()


def test_synthesis_v4_keeps_debate_decision_in_existing_call():
    properties = readers.CODEX_OUTPUT_CONTRACTS["cluster_synthesis"]["properties"]
    assert "complementary_positions" in properties["debate_state"]["enum"]
    assert "debate_explanation" not in properties
    result = readers._validate_streamlined_cluster_response({
        "cluster_id": "c1", "title": "Trust", "organizing_problem": "Trust",
        "bottom_line": "The works are complementary.", "debate_state": "complementary_positions",
    })
    assert result["cluster_contract"] == "streamlined-full-note-v4"
    assert result["debate_state"] == "complementary_positions"
    with pytest.raises(readers.ProviderError, match="debate_state"):
        readers._validate_streamlined_cluster_response({"debate_state": "invented"})


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
def test_standalone_profile_routes_cannot_reintroduce_legacy_inventory(monkeypatch, provider):
    reader = (readers.CodexReader("gpt-5.6-luna", allow_cloud=True) if provider == "codex"
              else readers.DeepSeekReader(allow_cloud=True))
    monkeypatch.setattr(reader, "_authorize_request", lambda: None)
    response = {
        "concepts": ["participation", "trust"], "methods": ["panel regression"],
        "findings": [{"claim": "A duplicate finding."}],
        "evidence_anchors": [{"claim": "A duplicate finding.", "locator": "p. 14"}],
        "profile_schema_version": "1.3",
    }
    calls = []

    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(response)

    monkeypatch.setattr(reader, "_generate_with_reasoning", generate)
    result = reader.profile_source({"profile_prompt": profiles.build_profile_prompt(_note())})
    assert len(calls) == 1
    assert calls[0][1]["output_contract"] == "evidence_profile"
    assert result["profile_schema_version"] == "1.4"
    assert result["concepts"] == response["concepts"]
    assert "findings" not in result and "evidence_anchors" not in result


def test_image_backed_note_needs_substantive_note_content_not_anchors(monkeypatch, tmp_path):
    reader = readers.CodexReader("gpt-5.6-luna", allow_cloud=True)
    monkeypatch.setattr(reader, "_authorize_request", lambda: None)
    monkeypatch.setattr(reader, "_generate_with_reasoning", lambda *a, **k: json.dumps(_bundle()))
    result = reader.read_source_bundle("", {"_source_context": {"source_id": "s1"}},
                                      attachment_paths=[tmp_path / "page.png"])
    assert result["analysis_sections"]["evidence_and_data"] == "Source-grounded discussion."
    assert "evidence_anchors" not in result


def test_current_coarse_profile_projection_cannot_restore_legacy_claims():
    raw = {"profile_schema_version": "1.4", "source_id": "s1", "concepts": ["trust"],
           "evidence_anchors": [{"evidence_anchor_id": "old", "claim": "Old claim", "locator": "p. 1"}]}
    projected = readers._cluster_proposal_profile(raw)
    assert projected["concepts"] == ["trust"]
    assert "evidence_anchors" not in projected


def test_legacy_bundle_core_recovery_does_not_relax_current_source_contract():
    payload = {"bundle_schema_version": "1", "source_identity": {"source_id": "s1"},
               "analysis_sections": {"thesis": "Original thesis", "method_and_research_design": "Original method"},
               "evidence_anchors": [{"claim": "Original finding", "locator": "p. 1"}]}
    legacy = readers._normalize_source_bundle_payload(payload)
    assert legacy["analysis_sections"]["thesis"] == "Original thesis"
    assert legacy["evidence_anchors"] == payload["evidence_anchors"]
    with pytest.raises(readers.ProviderError, match="core content"):
        readers._normalize_source_bundle_payload({**payload, "bundle_schema_version": "2"})
    with pytest.raises(readers.ProviderError, match="no complete"):
        readers._parse_source_bundle_response(payload, label="fresh response", expected_identity={"source_id": "s1"})


def test_legacy_intake_caps_do_not_make_anchor_inventory_part_of_current_contract():
    payload = {"bundle_schema_version": "1", "evidence_anchors": [{"claim": "old"}] * 25}
    with pytest.raises(ValueError, match="evidence_anchors"):
        readers._validate_source_bundle_row_limits(payload)
    readers._validate_source_bundle_row_limits({**payload, "bundle_schema_version": "2"})
