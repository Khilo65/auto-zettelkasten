from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten import profiles
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.notes import (
    parse_atomic_note,
    render_atomic_note,
    render_limited_note,
    semantic_note_hash as shared_semantic_note_hash,
)
from auto_zettelkasten.profiles import (
    PROFILE_ALGORITHM_VERSION,
    PROFILE_CLASSIFIER_VERSION,
    PROFILE_PROMPT_VERSION,
    ProfileCheckpointError,
    ProfileParseError,
    ProfilePersistenceError,
    build_evidence_profile,
    deterministic_profile,
    load_profile_checkpoint,
    load_profile_sidecar,
    parse_profile_json,
    profile_dependency_fingerprint,
    profile_dependency_payload,
    profile_to_dict,
    semantic_note_hash,
    validate_profile,
    write_profile_checkpoint,
    write_profile_sidecar,
)


def test_profile_versions_are_explicit() -> None:
    assert PROFILE_PROMPT_VERSION == profiles.profile_prompt_version == "1"
    assert PROFILE_CLASSIFIER_VERSION == profiles.profile_classifier_version == "1"
    assert PROFILE_ALGORITHM_VERSION == profiles.profile_algorithm_version == "1"


def test_analytical_note_is_extracted_and_validated_from_committed_markdown() -> None:
    note = _analytical_note()

    profile = deterministic_profile(
        note,
        source_set_id="source-set-1",
        provider="deterministic",
        model="deterministic-v1",
        policy={"max_profile_calls": 20},
    )

    assert isinstance(profile, EvidenceProfile)
    assert profile.note_id == "note-1"
    assert profile.note_hash == shared_semantic_note_hash(note)
    assert profile.source_hash == "a" * 64
    assert profile.source_role == "empirical"
    assert profile.coverage == {
        "note_status": "analytical_atomic_note",
        "source_scope": "full_document",
        "coverage_gate": "passed",
        "full_document": True,
    }
    assert profile.validity["profile_prompt_version"] == "1"
    assert profile.validity["classifier_version"] == "1"
    assert profile.validity["algorithm_version"] == "1"
    assert profile.research_questions
    assert {"participation", "trust"} <= set(profile.concepts)
    assert profile.theories == ["contact theory"]
    assert profile.mechanisms == ["learning"]
    assert profile.methods == ["panel regression"]
    assert profile.cases == ["Case A"]
    assert profile.datasets == profile.data == ["panel survey"]
    assert profile.geography == ["Region A"]
    assert profile.periods == ["2018–2020"]
    assert profile.populations == ["urban participants"]
    assert profile.outcomes == ["institutional trust"]
    assert profile.measures == ["trust scale"]
    assert profile.study_family_id == "doi:10.1234/example"
    assert profile.boundaries == [
        "Can support: An adjusted association between participation and trust.",
        "Cannot support: A causal effect of participation.",
    ]
    assert "The observational design cannot establish causation" in profile.limitations
    assert profile.gaps == ["rural comparison"]
    assert profile.future_research == ["replicate in rural sites"]

    assert len(profile.findings) == 1
    finding = profile.findings[0]
    assert finding.finding_id.startswith("finding-")
    assert finding.finding_type == "statistical"
    assert finding.direction == "positive"
    assert finding.magnitude == "12%"
    assert finding.comparison.startswith("compared with non-participants")
    assert finding.uncertainty == "p < 0.05"
    assert finding.conditions and finding.conditions[0].startswith("Among urban participants")
    assert finding.plain_english_meaning.startswith("Participants reported more trust")
    assert finding.is_statistical is True
    assert "Table 2" in finding.locator
    assert finding.locators == [finding.locator]

    validation = validate_profile(profile)
    assert validation.passed, validation.errors
    assert validation.substantive is True


def test_profile_validation_rejects_unlocated_and_unexplained_statistical_findings() -> None:
    profile = deterministic_profile(_analytical_note())
    finding = profile.findings[0]

    profile.findings = [replace(finding, locator="", locators=[])]
    unlocated = validate_profile(profile)
    assert "finding_0:traceable_locator_required" in unlocated.errors

    profile.findings = [replace(finding, plain_english_meaning="")]
    unexplained = validate_profile(profile)
    assert "finding_0:plain_english_meaning_required_for_statistical_finding" in unexplained.errors


def test_limited_note_is_deterministic_context_only_and_never_calls_reasoner() -> None:
    calls: list[str] = []

    def reasoner(prompt: str) -> str:
        calls.append(prompt)
        raise AssertionError("limited notes must not call a reasoner")

    profile = build_evidence_profile(_limited_note(), reasoner_method=reasoner)

    assert calls == []
    assert profile.excluded_from_synthesis is True
    assert profile.findings == []
    assert profile.methods == []
    assert profile.concepts == ["participation"]
    assert profile.source_role == "context_only"
    assert "Only the abstract was available" in profile.exclusion_reason
    assert any(boundary.startswith("Context only:") for boundary in profile.boundaries)
    assert profile.context["metadata"]["title"] == "Limited Source"

    structural = validate_profile(profile, require_substantive=False)
    assert structural.passed, structural.errors
    substantive = validate_profile(profile)
    assert substantive.passed is False
    assert "analytical_full_document_profile_required" in substantive.errors

    analytical_finding = deterministic_profile(_analytical_note()).findings[0]
    profile.findings = [analytical_finding]
    rejected = validate_profile(profile, require_substantive=False)
    assert "limited_profile_contains_substantive_findings" in rejected.errors


def test_non_full_analytical_note_is_context_only_and_never_calls_reasoner() -> None:
    invalid = _with_frontmatter_updates(
        _analytical_note(),
        source_scope="abstract_only",
        source_coverage={"gate": "limited"},
    )
    calls: list[str] = []

    profile = build_evidence_profile(invalid, reasoner_method=lambda prompt: calls.append(prompt))

    assert calls == []
    assert profile.excluded_from_synthesis is True
    assert profile.findings == []
    assert profile.validity["status"] == "excluded_context_only"
    assert validate_profile(profile, require_substantive=False).passed


def test_semantic_hash_reuses_notes_helper_and_ignores_generated_graph_projection() -> None:
    note = _analytical_note()
    projected = _with_generated_graph(note)

    assert semantic_note_hash is shared_semantic_note_hash
    assert semantic_note_hash(note) == semantic_note_hash(projected)
    assert deterministic_profile(note).note_hash == deterministic_profile(projected).note_hash


def test_profile_sidecar_is_atomic_idempotent_and_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = deterministic_profile(_analytical_note())
    path = tmp_path / "profiles" / "note-1.yml"
    calls: list[Path] = []
    real_atomic_write = profiles.atomic_write_text

    def counting_write(target: Path, text: str) -> None:
        calls.append(target)
        real_atomic_write(target, text)

    monkeypatch.setattr(profiles, "atomic_write_text", counting_write)

    assert write_profile_sidecar(path, profile) is True
    first_bytes = path.read_bytes()
    assert write_profile_sidecar(path, profile) is False
    assert path.read_bytes() == first_bytes
    assert calls == [path]
    assert load_profile_sidecar(path) == profile

    unknown = yaml.safe_load(path.read_text(encoding="utf-8"))
    unknown["unexpected"] = True
    unknown_path = tmp_path / "unknown.yml"
    unknown_path.write_text(yaml.safe_dump(unknown), encoding="utf-8")
    with pytest.raises(ProfilePersistenceError, match="unknown persisted fields"):
        load_profile_sidecar(unknown_path)

    malformed = tmp_path / "malformed.yml"
    malformed.write_text("profile_schema_version: [", encoding="utf-8")
    with pytest.raises(ProfilePersistenceError, match="malformed profile sidecar"):
        load_profile_sidecar(malformed)

    duplicate = tmp_path / "duplicate.yml"
    duplicate.write_text(
        "profile_schema_version: '1'\nprofile_schema_version: '1'\nprofile: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfilePersistenceError, match="duplicate YAML field"):
        load_profile_sidecar(duplicate)


def test_profile_checkpoint_resumes_only_on_matching_fingerprint_and_corruption_fails(tmp_path: Path) -> None:
    profile = deterministic_profile(_analytical_note())
    state_dir = tmp_path / "literature-state"

    checkpoint = write_profile_checkpoint(state_dir, profile.note_id, "fingerprint-1", profile)
    assert load_profile_checkpoint(state_dir, profile.note_id, "fingerprint-1") == profile
    assert load_profile_checkpoint(state_dir, profile.note_id, "fingerprint-2") is None

    corrupt_nested = yaml.safe_load(checkpoint.read_text(encoding="utf-8"))
    corrupt_nested["profile"]["concepts"] = "not-a-list"
    checkpoint.write_text(yaml.safe_dump(corrupt_nested), encoding="utf-8")
    with pytest.raises(ProfileCheckpointError, match="profile.concepts must be a list"):
        load_profile_checkpoint(state_dir, profile.note_id, "different-fingerprint")

    checkpoint.write_text("checkpoint_schema_version: [", encoding="utf-8")
    with pytest.raises(ProfileCheckpointError, match="malformed profile checkpoint"):
        load_profile_checkpoint(state_dir, profile.note_id, "fingerprint-1")


def test_profile_fingerprint_includes_every_declared_dependency() -> None:
    note = _analytical_note()
    kwargs = {
        "source_set_id": "source-set-1",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "policy": {"max_profile_calls": 20},
    }
    baseline = profile_dependency_fingerprint(note, **kwargs)
    payload = profile_dependency_payload(note, **kwargs)

    assert payload["note_semantic_hash"] == shared_semantic_note_hash(note)
    assert payload["profile_prompt_version"] == "1"
    assert payload["classifier_version"] == "1"
    assert payload["algorithm_version"] == "1"
    assert baseline == profile_dependency_fingerprint(_with_generated_graph(note), **kwargs)
    assert baseline != profile_dependency_fingerprint(note.replace("12%", "13%"), **kwargs)
    assert baseline != profile_dependency_fingerprint(note, **{**kwargs, "source_set_id": "source-set-2"})
    assert baseline != profile_dependency_fingerprint(note, **{**kwargs, "provider": "ollama"})
    assert baseline != profile_dependency_fingerprint(note, **{**kwargs, "model": "other-model"})
    assert baseline != profile_dependency_fingerprint(note, **{**kwargs, "policy": {"max_profile_calls": 21}})
    assert baseline != profile_dependency_fingerprint(note, **kwargs, profile_prompt_version="2")
    assert baseline != profile_dependency_fingerprint(note, **kwargs, profile_classifier_version="2")
    assert baseline != profile_dependency_fingerprint(note, **kwargs, profile_algorithm_version="2")


def test_strict_profile_json_parser_rejects_wrappers_duplicates_and_unknown_fields() -> None:
    profile = deterministic_profile(_analytical_note())
    payload = profile_to_dict(profile)

    restored = parse_profile_json(json.dumps(payload))
    assert restored == profile

    with pytest.raises(ProfileParseError, match="strict JSON"):
        parse_profile_json(f"```json\n{json.dumps(payload)}\n```")
    with pytest.raises(ProfileParseError, match="duplicate JSON field"):
        parse_profile_json('{"profile_schema":"evidence_profile","profile_schema":"other"}')

    unknown_profile = copy.deepcopy(payload)
    unknown_profile["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown profile fields"):
        parse_profile_json(json.dumps(unknown_profile))

    unknown_finding = copy.deepcopy(payload)
    unknown_finding["findings"][0]["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown finding fields"):
        parse_profile_json(json.dumps(unknown_finding))

    wrong_type = copy.deepcopy(payload)
    wrong_type["concepts"] = "not-a-list"
    with pytest.raises(ProfileParseError, match="profile.concepts must be a list"):
        parse_profile_json(json.dumps(wrong_type))


def test_optional_reasoner_receives_only_committed_note_without_graph_projection() -> None:
    projected = _with_generated_graph(_analytical_note())
    deterministic = deterministic_profile(projected)
    prompts: list[str] = []

    def reasoner(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(profile_to_dict(deterministic))

    result = build_evidence_profile(
        projected,
        source_set_id="source-set-reasoner",
        provider="deepseek",
        model="deepseek-v4-flash",
        policy={"max_profile_calls": 20},
        reasoner_method=reasoner,
    )

    assert result.findings == deterministic.findings
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"
    assert result.dependency_hash == profile_dependency_fingerprint(
        projected,
        source_set_id="source-set-reasoner",
        provider="deepseek",
        model="deepseek-v4-flash",
        policy={"max_profile_calls": 20},
    )
    assert len(prompts) == 1
    assert "COMMITTED MARKDOWN NOTE" in prompts[0]
    assert "## Graph Links" not in prompts[0]
    assert "synthetic source full text sentinel" not in prompts[0]


def test_committed_note_controls_profile_status_scope_and_identity() -> None:
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["note_id"] = ""
    proposed["source_id"] = ""
    proposed["coverage"] = {
        "note_status": "analytical",
        "source_scope": "model_guess",
        "coverage_gate": "unknown",
    }

    result = build_evidence_profile(note, reasoner_method=lambda prompt: proposed)

    assert result.note_id == "note-1"
    assert result.source_id == "source-1"
    assert result.coverage["note_status"] == "analytical_atomic_note"
    assert result.coverage["source_scope"] == "full_document"
    assert result.coverage["coverage_gate"] == "passed"
    assert result.coverage["full_document"] is True


def test_reasoner_profile_omits_only_findings_without_required_support() -> None:
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["findings"].append(
        {
            **copy.deepcopy(proposed["findings"][0]),
            "finding_id": "untraceable-finding",
            "claim": "A second claim without a usable source location.",
            "locator": "not supplied",
            "locators": [],
        }
    )

    result = build_evidence_profile(note, reasoner_method=lambda prompt: proposed)

    assert len(result.findings) == 1
    assert result.validity["omitted_untraceable_or_uninterpreted_finding_count"] == 1


def _analytical_note() -> str:
    frontmatter = {
        "note_id": "note-1",
        "source_id": "source-1",
        "note_status": "analytical_atomic_note",
        "source_scope": "full_document",
        "source_coverage": {"gate": "passed"},
        "inspected_content_hash": "a" * 64,
        "title": "Participation and Trust",
        "creators": [{"lastName": "Researcher"}],
        "date": "2020",
        "DOI": "10.1234/EXAMPLE",
        "normalized_tags": ["participation"],
        "clusters": [],
        "related_notes": [],
        "updated_at": "2026-07-15T00:00:00+00:00",
    }
    analysis = {
        "thesis": (
            "Research question: Does participation increase trust?\n"
            "Concepts: participation; trust\n"
            "Theory: contact theory\n"
            "Mechanism: learning"
        ),
        "method_and_research_design": (
            "Method: panel regression\n"
            "Population: urban participants\n"
            "Cases: Case A\n"
            "Geography: Region A\n"
            "Period: 2018–2020"
        ),
        "evidence_and_data": (
            "Data source: panel survey\n"
            "Measures: trust scale\n"
            "Outcome: institutional trust"
        ),
        "detailed_findings": (
            "- Among urban participants, participation increased trust by 12% compared with non-participants "
            "(p < 0.05); see Table 2."
        ),
        "plain_english_interpretation": (
            "- Participants reported more trust than non-participants. The reported uncertainty indicates that the "
            "estimate is statistically distinguishable from zero, but it does not establish causation."
        ),
        "strengths_and_contributions": "The panel design observes change over time.",
        "methodological_critique": "The observational design remains vulnerable to residual confounding.",
        "limitations": (
            "- The observational design cannot establish causation.\n"
            "- Author-stated gaps: rural comparison\n"
            "- Future research: replicate in rural sites"
        ),
        "what_this_source_can_support": "An adjusted association between participation and trust.",
        "what_this_source_cannot_support": "A causal effect of participation.",
        "locators": "Table 2, p. 14.",
    }
    return render_atomic_note(frontmatter, analysis)


def _limited_note() -> str:
    frontmatter = {
        "note_id": "limited-note-1",
        "source_id": "limited-source-1",
        "note_status": "abstract_only_atomic_note",
        "source_scope": "abstract_only",
        "source_coverage": {"gate": "limited"},
        "inspected_content_hash": "b" * 64,
        "title": "Limited Source",
        "creators": [{"lastName": "Researcher"}],
        "date": "2021",
        "normalized_tags": ["participation"],
    }
    return render_limited_note(
        frontmatter,
        {
            "abstract": "The abstract mentions participation as context.",
            "scope_limitation": (
                "Only the abstract was available. Do not treat this note as evidence from the full publication."
            ),
        },
    )


def _with_generated_graph(note: str) -> str:
    projected = _with_frontmatter_updates(
        note,
        clusters=["cluster-generated"],
        related_notes=[{"note_id": "note-generated", "relation_type": "related"}],
        updated_at="2026-07-15T01:00:00+00:00",
    )
    frontmatter, body = parse_atomic_note(projected)
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return (
        f"---\n{yaml_text}\n---\n{body.rstrip()}\n\n"
        "## Graph Links\n\n"
        "- related: [[Generated Note]]\n"
        "- cluster: [[cluster-generated]]\n"
    )


def _with_frontmatter_updates(note: str, **updates: object) -> str:
    frontmatter, body = parse_atomic_note(note)
    frontmatter.update(updates)
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_text}\n---\n{body}"
