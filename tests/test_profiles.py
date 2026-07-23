from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten import profiles
from auto_zettelkasten.models import EvidenceAnchor, EvidenceFinding, EvidenceProfile
from auto_zettelkasten.notes import (
    parse_atomic_note,
    render_atomic_note,
    render_limited_note,
    semantic_note_hash as shared_semantic_note_hash,
)
from auto_zettelkasten.profiles import (
    COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION,
    PROFILE_ALGORITHM_VERSION,
    ANCHOR_ALGORITHM_VERSION,
    PROFILE_CLASSIFIER_VERSION,
    PROFILE_PROMPT_VERSION,
    PROFILE_SCHEMA_VERSION,
    SUPPORT_ENVELOPE_VERSION,
    ProfileCheckpointError,
    ProfileContractError,
    ProfileParseError,
    ProfilePersistenceError,
    augment_profile_from_committed_note,
    build_evidence_profile,
    deterministic_profile,
    load_profile_checkpoint,
    load_profile_sidecar,
    parse_profile_json,
    profile_dependency_fingerprint,
    profile_dependency_payload,
    profile_from_dict,
    profile_to_dict,
    semantic_note_hash,
    validate_profile,
    write_profile_checkpoint,
    write_profile_sidecar,
    _quantitative_result_payload,
)


def test_profile_versions_are_explicit() -> None:
    assert PROFILE_SCHEMA_VERSION == "1.2"
    assert PROFILE_PROMPT_VERSION == profiles.profile_prompt_version == "6"
    assert PROFILE_CLASSIFIER_VERSION == profiles.profile_classifier_version == "3"
    assert PROFILE_ALGORITHM_VERSION == profiles.profile_algorithm_version == "4"
    assert ANCHOR_ALGORITHM_VERSION == SUPPORT_ENVELOPE_VERSION == "1"
    assert COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION == "8"


def test_actor_position_labels_are_not_misread_as_page_locators() -> None:
    assert profiles._first_locator("Interviewee P3 described mediator legitimacy") == ""
    assert profiles._first_locator("Positions P3 and P5 diverged") == ""
    assert profiles._first_locator("See p. 3 and pp. 5-7") == "p. 3; pp. 5-7"
    assert profiles._first_locator("See pages 12-14") == "pages 12-14"


def test_central_contribution_gets_a_locator_matched_conceptual_anchor() -> None:
    note = _analytical_note()
    note = note.replace(
        "The panel design observes change over time.",
        (
            "Muscular mediation can backfire when coercion threatens vital interests, the target can attack "
            "civilians, and deterrent forces are insufficient."
        ),
    ).replace(
        "Table 2, p. 14.",
        "Muscular Mediation Theory (pp. 166-168); Muscular Mistakes (pp. 173-174).",
    )

    profile = deterministic_profile(note)
    conceptual = [
        anchor
        for anchor in profile.evidence_anchors
        if anchor.support_envelope.argument_role == "conceptual"
    ]

    assert conceptual
    assert any("vital interests" in anchor.claim for anchor in conceptual)
    assert any("pp. 166-168" in anchor.locator for anchor in conceptual)


def test_committed_note_anchor_augmentation_repairs_a_sparse_profile_once() -> None:
    note = _analytical_note()
    original = profile_to_dict(deterministic_profile(note))
    sparse = profile_from_dict({**original, "findings": [], "evidence_anchors": []})

    augmented, changed = augment_profile_from_committed_note(
        sparse,
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    assert len(augmented.evidence_anchors) == 1
    assert augmented.validity["committed_note_anchor_count_added"] == 1
    assert augmented.validity["committed_note_anchor_augmentation_version"] == "8"

    replayed, replay_changed = augment_profile_from_committed_note(
        augmented,
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert replay_changed is False
    assert profile_to_dict(replayed) == profile_to_dict(augmented)


def test_complete_reasoner_profile_is_not_padded_with_mechanical_summary_anchors() -> None:
    note = _analytical_note()
    payload = profile_to_dict(deterministic_profile(note))
    template = payload["evidence_anchors"][0]
    anchors = []
    for index in range(8):
        anchor = copy.deepcopy(template)
        anchor.update(
            evidence_anchor_id="",
            revision_hash="",
            claim=f"Reasoner-selected contribution {index + 1} about participation and trust.",
            locator=f"p. {14 + index}",
            locators=[f"p. {14 + index}"],
            source_locators=[],
        )
        anchor["support_envelope"] = {
            **anchor["support_envelope"],
            "coverage": "limited_text",
        }
        anchors.append(anchor)
    payload["evidence_anchors"] = anchors
    payload["validity"].pop("committed_note_anchor_augmentation_version", None)

    augmented, changed = augment_profile_from_committed_note(
        profile_from_dict(payload),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    assert len(augmented.evidence_anchors) == 8
    assert augmented.validity["committed_note_anchor_count_added"] == 0
    assert all(
        anchor.support_envelope.coverage == "full_text"
        for anchor in augmented.evidence_anchors
    )


def test_committed_note_augmentation_downgrades_ambiguous_mechanical_composites() -> (
    None
):
    note = _analytical_note()
    original = profile_to_dict(deterministic_profile(note))
    anchor = dict(original["evidence_anchors"][0])
    anchor["locator"] = "p. 4; p. 12; p. 27; p. 48; p. 74"
    anchor["locators"] = [anchor["locator"]]
    anchor["support_envelope"] = {
        **dict(anchor["support_envelope"]),
        "support_status": "supported",
    }
    mechanical = profile_from_dict(
        {
            **original,
            "evidence_anchors": [anchor],
        }
    )

    augmented, changed = augment_profile_from_committed_note(
        mechanical,
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    composite = next(
        item
        for item in augmented.evidence_anchors
        if item.locator == "p. 4; p. 12; p. 27; p. 48; p. 74"
    )
    assert composite.support_envelope.support_status == "support_unknown"
    assert any(
        "Mechanical composite" in value
        for value in composite.support_envelope.restrictions
    )
    assert composite.revision_hash != anchor["revision_hash"]


def test_v11_profile_is_mechanically_enriched_from_committed_note_without_a_reader() -> (
    None
):
    note = _analytical_note()
    legacy = profile_to_dict(deterministic_profile(note))
    legacy["profile_schema_version"] = "1.1"
    legacy["study_lineage"] = None
    legacy["validity"]["committed_note_anchor_augmentation_version"] = "3"
    for anchor in legacy["evidence_anchors"]:
        anchor["source_locators"] = []
        anchor["quantitative_result"] = None

    upgraded, changed = augment_profile_from_committed_note(
        profile_from_dict(legacy),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    assert upgraded.profile_schema_version == "1.2"
    assert upgraded.study_lineage is not None
    assert upgraded.study_lineage.datasets == ["panel survey"]
    assert len(upgraded.evidence_anchors) == 1
    anchor = upgraded.evidence_anchors[0]
    assert any(locator.supports_strong_assertion for locator in anchor.source_locators)
    assert anchor.quantitative_result is not None
    assert anchor.quantitative_result.estimand_type == "raw_percentage"


def test_committed_note_locator_replaces_bare_generated_section_label() -> None:
    note = _analytical_note()
    legacy = profile_to_dict(deterministic_profile(note))
    legacy["validity"]["committed_note_anchor_augmentation_version"] = "4"
    anchor = legacy["evidence_anchors"][0]
    anchor["locator"] = "method"
    anchor["locators"] = ["method"]
    anchor["source_locators"] = []

    upgraded, changed = augment_profile_from_committed_note(
        profile_from_dict(legacy),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    repaired = upgraded.evidence_anchors[0]
    assert repaired.locator == "Table 2; p. 14"
    assert any(
        locator.supports_strong_assertion for locator in repaired.source_locators
    )


def test_v5_aggregate_document_range_is_not_promoted_as_claim_support() -> None:
    note = _analytical_note().replace(
        "Table 2, p. 14.",
        "Table 2, p. 14; p. 89; p. 310.",
    )
    legacy = profile_to_dict(deterministic_profile(note))
    legacy["validity"]["committed_note_anchor_augmentation_version"] = "5"
    anchor = legacy["evidence_anchors"][0]
    anchor["claim"] = (
        "A provider-supplied claim without a claim-specific locator match."
    )
    anchor["locator"] = "pages 14-310"
    anchor["locators"] = ["pages 14-310"]
    anchor["source_locators"] = [
        {
            "locator_id": "locator-aggregate",
            "source_id": "source-1",
            "evidence_anchor_id": anchor["evidence_anchor_id"],
            "locator_type": "page_range",
            "value": "pages 14-310",
            "page_start": 14,
            "page_end": 310,
            "source_native": True,
            "supports_strong_assertion": True,
        }
    ]
    anchor["support_envelope"] = {
        **dict(anchor["support_envelope"]),
        "support_status": "supported",
    }

    upgraded, changed = augment_profile_from_committed_note(
        profile_from_dict(legacy),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    repaired = next(
        item
        for item in upgraded.evidence_anchors
        if item.claim.startswith("A provider-supplied claim")
    )
    assert repaired.support_envelope.support_status == "support_unknown"
    assert not any(
        locator.supports_strong_assertion for locator in repaired.source_locators
    )
    assert validate_profile(upgraded).passed is True


def test_generated_heading_composite_is_not_source_native_support() -> None:
    note = (
        _analytical_note()
        .replace("(p < 0.05); see Table 2.", "(p < 0.05).")
        .replace("Table 2, p. 14.", "findings; data.")
    )
    legacy = profile_to_dict(deterministic_profile(note))
    legacy["validity"]["committed_note_anchor_augmentation_version"] = "6"
    anchor = legacy["evidence_anchors"][0]
    anchor["locator"] = "findings; data"
    anchor["locators"] = ["findings; data"]
    anchor["source_locators"] = []
    anchor["support_envelope"] = {
        **dict(anchor["support_envelope"]),
        "support_status": "supported",
    }

    upgraded, changed = augment_profile_from_committed_note(
        profile_from_dict(legacy),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    repaired = upgraded.evidence_anchors[0]
    assert repaired.support_envelope.support_status == "support_unknown"
    assert repaired.source_locators
    assert all(
        locator.locator_type == "generated_heading"
        and not locator.source_native
        and not locator.supports_strong_assertion
        for locator in repaired.source_locators
    )
    assert validate_profile(upgraded).passed is True


def test_unreconstructable_legacy_statistic_downgrades_only_its_anchor() -> None:
    note = _analytical_note()
    legacy = profile_to_dict(deterministic_profile(note))
    legacy["profile_schema_version"] = "1.1"
    legacy["validity"]["committed_note_anchor_augmentation_version"] = "3"
    anchor = legacy["evidence_anchors"][0]
    anchor["claim"] = (
        "The source classifies the result as statistical but reports no extractable estimate."
    )
    anchor["magnitude"] = "not_reported"
    anchor["uncertainty"] = "not_reported"
    anchor["quantitative_result"] = None

    upgraded, changed = augment_profile_from_committed_note(
        profile_from_dict(legacy),
        note,
        source_set_id="source-set-1",
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert changed is True
    upgraded_anchor = upgraded.evidence_anchors[0]
    assert upgraded_anchor.quantitative_result is None
    assert upgraded_anchor.support_envelope.support_status == "support_unknown"
    validation = validate_profile(upgraded)
    assert validation.passed
    assert (
        "anchor_0:typed_quantitative_result_unresolved_support_unknown"
        in validation.warnings
    )


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
    assert profile.validity["profile_prompt_version"] == "6"
    assert profile.validity["classifier_version"] == "3"
    assert profile.validity["algorithm_version"] == "4"
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
    assert finding.conditions and finding.conditions[0].startswith(
        "Among urban participants"
    )
    assert finding.plain_english_meaning.startswith("Participants reported more trust")
    assert finding.is_statistical is True
    assert "Table 2" in finding.locator
    assert finding.locators == [finding.locator]
    assert len(profile.evidence_anchors) == 1
    anchor = profile.evidence_anchors[0]
    assert anchor.evidence_anchor_id.startswith("anchor-")
    assert anchor.source_id == profile.source_id
    assert anchor.study_family_id == profile.study_family_id
    assert anchor.evidence_role == "associational"
    assert anchor.support_envelope.empirical_role == "associational"
    assert anchor.support_envelope.coverage == "full_text"
    assert anchor.support_envelope.support_status == "supported"
    assert anchor.source_locators
    assert any(locator.locator_type == "table" for locator in anchor.source_locators)
    assert any(locator.locator_type == "page" for locator in anchor.source_locators)
    assert all(locator.source_native for locator in anchor.source_locators)
    assert anchor.quantitative_result is not None
    assert anchor.quantitative_result.estimand_type == "raw_percentage"
    assert anchor.quantitative_result.reference_group.startswith(
        "compared with non-participants"
    )
    assert profile.study_lineage is not None
    assert profile.study_lineage.authors == ["Researcher"]
    assert "panel survey" in profile.study_lineage.datasets
    assert profile.study_lineage.periods == ["2018–2020"]

    validation = validate_profile(profile)
    assert validation.passed, validation.errors
    assert validation.substantive is True


def test_profile_validation_rejects_unlocated_and_unexplained_statistical_anchors() -> (
    None
):
    profile = deterministic_profile(_analytical_note())
    anchor = profile.evidence_anchors[0]

    profile.evidence_anchors = [
        replace(anchor, locator="", locators=[], source_locators=[])
    ]
    unlocated = validate_profile(profile)
    assert "anchor_0:traceable_locator_required" in unlocated.errors

    profile.evidence_anchors = [replace(anchor, plain_english_meaning="")]
    unexplained = validate_profile(profile)
    assert (
        "anchor_0:plain_english_meaning_required_for_statistical_anchor"
        in unexplained.errors
    )


def test_generated_atomic_note_heading_is_not_source_native_evidence() -> None:
    payload = profile_to_dict(deterministic_profile(_analytical_note()))
    anchor = payload["evidence_anchors"][0]
    anchor["locator"] = "Detailed Findings (1)"
    anchor["locators"] = ["Detailed Findings (1)"]
    anchor["source_locators"] = [
        {
            "locator_id": "locator-generated",
            "source_id": "source-1",
            "evidence_anchor_id": anchor["evidence_anchor_id"],
            "locator_type": "generated_heading",
            "value": "Detailed Findings (1)",
            "page_start": None,
            "page_end": None,
            "source_native": False,
            "supports_strong_assertion": False,
        }
    ]

    validation = validate_profile(profile_from_dict(payload))

    assert "anchor_0:source_native_locator_required" in validation.errors


def test_mechanical_quantitative_typing_keeps_unlike_estimands_separate() -> None:
    observed = _quantitative_result_payload(
        {
            "claim": "The observed success rate was 38%.",
            "magnitude": "38%",
            "uncertainty": "",
        },
        source_id="source-1",
        evidence_anchor_id="anchor-observed",
    )
    predicted = _quantitative_result_payload(
        {
            "claim": "The model-predicted probability was 38% and the marginal effect was +0.0997.",
            "magnitude": "marginal effect +0.0997",
            "uncertainty": "",
        },
        source_id="source-1",
        evidence_anchor_id="anchor-predicted",
    )

    assert observed is not None and observed["estimand_type"] == "observed_rate"
    assert (
        predicted is not None
        and predicted["estimand_type"] == "model_predicted_probability"
    )
    assert predicted["estimate"] == "marginal effect +0.0997"
    assert observed["quantitative_result_id"] != predicted["quantitative_result_id"]


def test_limited_note_is_deterministic_context_only_and_never_calls_reasoner() -> None:
    calls: list[str] = []

    def reasoner(prompt: str) -> str:
        calls.append(prompt)
        raise AssertionError("limited notes must not call a reasoner")

    profile = build_evidence_profile(_limited_note(), reasoner_method=reasoner)

    assert calls == []
    assert profile.excluded_from_synthesis is True
    assert profile.findings == []
    assert profile.evidence_anchors == []
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
    profile.evidence_anchors = deterministic_profile(
        _analytical_note()
    ).evidence_anchors
    rejected = validate_profile(profile, require_substantive=False)
    assert "limited_profile_contains_substantive_findings" in rejected.errors
    assert "limited_profile_contains_substantive_anchors" in rejected.errors


def test_non_full_analytical_note_is_context_only_and_never_calls_reasoner() -> None:
    invalid = _with_frontmatter_updates(
        _analytical_note(),
        source_scope="abstract_only",
        source_coverage={"gate": "limited"},
    )
    calls: list[str] = []

    profile = build_evidence_profile(
        invalid, reasoner_method=lambda prompt: calls.append(prompt)
    )

    assert calls == []
    assert profile.excluded_from_synthesis is True
    assert profile.findings == []
    assert profile.evidence_anchors == []
    assert profile.validity["status"] == "excluded_context_only"
    assert validate_profile(profile, require_substantive=False).passed


def test_semantic_hash_reuses_notes_helper_and_ignores_generated_graph_projection() -> (
    None
):
    note = _analytical_note()
    projected = _with_generated_graph(note)

    assert semantic_note_hash is shared_semantic_note_hash
    assert semantic_note_hash(note) == semantic_note_hash(projected)
    assert (
        deterministic_profile(note).note_hash
        == deterministic_profile(projected).note_hash
    )


def test_profile_sidecar_is_atomic_idempotent_and_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_profile_checkpoint_resumes_only_on_matching_fingerprint_and_corruption_fails(
    tmp_path: Path,
) -> None:
    profile = deterministic_profile(_analytical_note())
    state_dir = tmp_path / "literature-state"

    checkpoint = write_profile_checkpoint(
        state_dir, profile.note_id, "fingerprint-1", profile
    )
    assert (
        load_profile_checkpoint(state_dir, profile.note_id, "fingerprint-1") == profile
    )
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
    assert payload["profile_prompt_version"] == "6"
    assert payload["classifier_version"] == "3"
    assert payload["algorithm_version"] == "4"
    assert payload["profile_schema_version"] == "1.2"
    assert payload["anchor_algorithm_version"] == "1"
    assert payload["support_envelope_version"] == "1"
    assert baseline == profile_dependency_fingerprint(
        _with_generated_graph(note), **kwargs
    )
    assert baseline != profile_dependency_fingerprint(
        note.replace("12%", "13%"), **kwargs
    )
    assert baseline != profile_dependency_fingerprint(
        note, **{**kwargs, "source_set_id": "source-set-2"}
    )
    assert baseline != profile_dependency_fingerprint(
        note, **{**kwargs, "provider": "ollama"}
    )
    assert baseline != profile_dependency_fingerprint(
        note, **{**kwargs, "model": "other-model"}
    )
    assert baseline != profile_dependency_fingerprint(
        note, **{**kwargs, "policy": {"max_profile_calls": 21}}
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_prompt_version="7"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_classifier_version="4"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_algorithm_version="5"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_schema_version="1.0"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, anchor_algorithm_version="2"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, support_envelope_version="2"
    )


def test_anchor_ids_ignore_finding_order_and_claim_prose_but_revision_hash_tracks_content() -> (
    None
):
    first = EvidenceFinding(
        finding_id="legacy-first",
        claim="Participation is associated with greater trust.",
        finding_type="association",
        evidence="Source-native span A.",
        locator="Table 2, p. 14",
    )
    second = EvidenceFinding(
        finding_id="legacy-second",
        claim="The association is weaker in rural cases.",
        finding_type="association",
        evidence="Source-native span B.",
        locator="Table 3, p. 18",
    )

    original = EvidenceProfile(
        source_id="source-1", coverage={"status": "full_text"}, findings=[first, second]
    )
    reordered = EvidenceProfile(
        source_id="source-1", coverage={"status": "full_text"}, findings=[second, first]
    )
    original_by_locator = {
        anchor.locator: anchor for anchor in original.evidence_anchors
    }
    reordered_ids = {
        anchor.locator: anchor.evidence_anchor_id
        for anchor in reordered.evidence_anchors
    }

    assert {
        locator: anchor.evidence_anchor_id
        for locator, anchor in original_by_locator.items()
    } == reordered_ids

    reworded = EvidenceProfile(
        source_id="source-1",
        coverage={"status": "full_text"},
        findings=[
            replace(first, claim="Greater trust is associated with participation."),
            second,
        ],
    )
    reworded_first = next(
        anchor
        for anchor in reworded.evidence_anchors
        if anchor.locator == first.locator
    )
    assert (
        reworded_first.evidence_anchor_id
        == original_by_locator[first.locator].evidence_anchor_id
    )
    assert (
        reworded_first.revision_hash != original_by_locator[first.locator].revision_hash
    )


def test_anchor_locator_collisions_use_stable_source_span_hash_not_position() -> None:
    first = EvidenceFinding(
        finding_id="legacy-first",
        claim="First located result.",
        finding_type="descriptive",
        evidence="Stable source span A.",
        locator="p. 22",
    )
    second = EvidenceFinding(
        finding_id="legacy-second",
        claim="Second located result.",
        finding_type="descriptive",
        evidence="Stable source span B.",
        locator="p. 22",
    )
    forward = EvidenceProfile(
        source_id="source-1", coverage={"status": "full_text"}, findings=[first, second]
    )
    reverse = EvidenceProfile(
        source_id="source-1", coverage={"status": "full_text"}, findings=[second, first]
    )

    forward_ids = {
        anchor.claim: anchor.evidence_anchor_id for anchor in forward.evidence_anchors
    }
    reverse_ids = {
        anchor.claim: anchor.evidence_anchor_id for anchor in reverse.evidence_anchors
    }
    assert forward_ids == reverse_ids
    assert len(set(forward_ids.values())) == 2


def test_profile_hard_limits_anchors_to_24() -> None:
    anchor = EvidenceAnchor(
        source_id="source-1",
        evidence_role="descriptive",
        claim="Located.",
        locator="p. 1",
    )
    with pytest.raises(ValueError, match="more than 24"):
        EvidenceProfile(source_id="source-1", evidence_anchors=[anchor] * 25)


def test_v1_profile_mapping_sidecar_and_checkpoint_upgrade_mechanically(
    tmp_path: Path,
) -> None:
    legacy_profile = EvidenceProfile(
        note_id="legacy-note",
        source_id="legacy-source",
        coverage={"status": "full_text"},
        findings=[
            EvidenceFinding(
                finding_id="legacy-finding",
                claim="A located legacy claim.",
                locator="p. 9",
            )
        ],
    ).to_dict()
    legacy_profile["profile_schema_version"] = "1.0"
    legacy_profile.pop("evidence_anchors")
    legacy_profile["findings"][0]["claim_id"] = legacy_profile["findings"][0].pop(
        "finding_id"
    )

    upgraded = profile_from_dict(legacy_profile)
    assert upgraded.profile_schema_version == "1.2"
    assert upgraded.evidence_anchors[0].evidence_role == "support_unknown"
    assert (
        upgraded.evidence_anchors[0].support_envelope.support_status
        == "support_unknown"
    )
    assert "evidence_anchor_id" in profile_to_dict(upgraded)["evidence_anchors"][0]

    sidecar = tmp_path / "legacy-sidecar.yml"
    sidecar.write_text(
        yaml.safe_dump(
            {"profile_schema_version": "1", "profile": legacy_profile}, sort_keys=False
        ),
        encoding="utf-8",
    )
    assert load_profile_sidecar(sidecar) == upgraded
    assert write_profile_sidecar(sidecar, upgraded) is True
    persisted = yaml.safe_load(sidecar.read_text(encoding="utf-8"))["profile"]
    assert persisted["profile_schema_version"] == "1.2"
    assert "evidence_anchor_id" in persisted["evidence_anchors"][0]

    state_dir = tmp_path / "state"
    checkpoint = write_profile_checkpoint(
        state_dir, upgraded.note_id, "legacy-fingerprint", upgraded
    )
    checkpoint_payload = yaml.safe_load(checkpoint.read_text(encoding="utf-8"))
    checkpoint_payload["profile"] = copy.deepcopy(legacy_profile)
    checkpoint.write_text(
        yaml.safe_dump(checkpoint_payload, sort_keys=False), encoding="utf-8"
    )
    assert (
        load_profile_checkpoint(state_dir, upgraded.note_id, "legacy-fingerprint")
        == upgraded
    )


def test_legacy_anchor_upgrade_does_not_inherit_source_level_scope() -> None:
    profile = EvidenceProfile(
        source_id="source-1",
        coverage={"status": "full_text"},
        populations=["all conflicts in the collection"],
        outcomes=["all mediation outcomes"],
        findings=[
            EvidenceFinding(
                claim="A bounded finding.",
                finding_type="descriptive",
                population="African civil wars",
                outcome="ceasefire durability",
                locator="p. 9",
            ),
            EvidenceFinding(
                claim="A finding without reported scope.",
                finding_type="descriptive",
                locator="p. 10",
            ),
        ],
    )

    anchors = {anchor.claim: anchor for anchor in profile.evidence_anchors}
    assert anchors["A bounded finding."].support_envelope.scope == {
        "populations": ["African civil wars"],
        "outcomes": ["ceasefire durability"],
    }
    assert anchors["A finding without reported scope."].support_envelope.scope == {}


def test_strict_profile_json_parser_rejects_wrappers_duplicates_and_unknown_fields() -> (
    None
):
    profile = deterministic_profile(_analytical_note())
    payload = profile_to_dict(profile)

    restored = parse_profile_json(json.dumps(payload))
    assert restored == profile

    with pytest.raises(ProfileParseError, match="strict JSON"):
        parse_profile_json(f"```json\n{json.dumps(payload)}\n```")
    with pytest.raises(ProfileParseError, match="duplicate JSON field"):
        parse_profile_json(
            '{"profile_schema":"evidence_profile","profile_schema":"other"}'
        )

    unknown_profile = copy.deepcopy(payload)
    unknown_profile["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown profile fields"):
        parse_profile_json(json.dumps(unknown_profile))

    unknown_finding = copy.deepcopy(payload)
    unknown_finding["findings"][0]["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown finding fields"):
        parse_profile_json(json.dumps(unknown_finding))

    unknown_anchor = copy.deepcopy(payload)
    unknown_anchor["evidence_anchors"][0]["unexpected"] = True
    with pytest.raises(ProfileParseError, match="invalid evidence anchor"):
        parse_profile_json(json.dumps(unknown_anchor))

    unknown_envelope = copy.deepcopy(payload)
    unknown_envelope["evidence_anchors"][0]["support_envelope"]["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown support envelope fields"):
        parse_profile_json(json.dumps(unknown_envelope))

    unknown_locator = copy.deepcopy(payload)
    unknown_locator["evidence_anchors"][0]["source_locators"][0]["unexpected"] = True
    with pytest.raises(ProfileParseError, match="unknown source locator fields"):
        parse_profile_json(json.dumps(unknown_locator))

    unknown_quantitative = copy.deepcopy(payload)
    unknown_quantitative["evidence_anchors"][0]["quantitative_result"]["unexpected"] = (
        True
    )
    with pytest.raises(ProfileParseError, match="unknown quantitative result fields"):
        parse_profile_json(json.dumps(unknown_quantitative))

    wrong_type = copy.deepcopy(payload)
    wrong_type["concepts"] = "not-a-list"
    with pytest.raises(ProfileParseError, match="profile.concepts must be a list"):
        parse_profile_json(json.dumps(wrong_type))


def test_optional_reasoner_receives_only_committed_note_without_graph_projection() -> (
    None
):
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
    assert result.evidence_anchors
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
    assert "8-20 synthesis-relevant evidence anchors" in prompts[0]
    assert "24 is a hard maximum" in prompts[0]
    assert "support_envelope" in prompts[0]
    assert "empirical_role is descriptive, associational, causal" in prompts[0]
    assert "argument_role is conceptual, interpretive, normative" in prompts[0]
    assert "support_status describes source attribution" in prompts[0]
    assert "practitioner recommendation can therefore be supported" in prompts[0]
    assert "profile_schema must be evidence_profile" in prompts[0]
    assert "do not pad, invent, or collapse an entire detailed note into one omnibus anchor" in prompts[0].casefold()
    assert "conference, policy, practitioner, or web sources" in prompts[0]
    assert "keep findings empty and study_lineage null" in prompts[0].casefold()
    assert "do not output ids, source_locators" in prompts[0].casefold()
    assert "the engine derives those records after the call" in prompts[0]
    assert "do not use a generated atomic-note heading" in prompts[0].casefold()
    assert "observed rate" in prompts[0]
    assert "do not reread" in prompts[0].casefold()
    assert "## Graph Links" not in prompts[0]
    assert "synthetic source full text sentinel" not in prompts[0]


def test_live_reasoner_normalizes_scalar_anchor_scope_without_weakening_sidecars() -> (
    None
):
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["profile_schema_version"] = PROFILE_PROMPT_VERSION
    proposed["evidence_anchors"][0]["support_envelope"]["scope"] = {
        "unit": "mediation episode",
        "outcomes": ["settlement durability"],
        "year": 1996,
        "comparison_years": [1995, "1997"],
    }

    result = build_evidence_profile(
        note,
        source_set_id="source-set-reasoner",
        provider="deepseek",
        model="deepseek-v4-flash",
        reasoner_method=lambda _prompt: proposed,
    )

    assert result.evidence_anchors[0].support_envelope.scope == {
        "unit": ["mediation episode"],
        "outcomes": ["settlement durability"],
        "year": ["1996"],
        "comparison_years": ["1995", "1997"],
    }

    persisted_scope_error = copy.deepcopy(proposed)
    persisted_scope_error["profile_schema_version"] = PROFILE_SCHEMA_VERSION
    with pytest.raises(ProfileContractError, match="scope.unit must be a list"):
        profile_from_dict(persisted_scope_error)

    persisted_shape_error = copy.deepcopy(proposed)
    persisted_shape_error["evidence_anchors"][0]["support_envelope"]["scope"] = {}
    with pytest.raises(
        ProfileContractError, match="unsupported profile_schema_version"
    ):
        profile_from_dict(persisted_shape_error)


def test_live_reasoner_normalizes_only_unambiguous_profile_shape_aliases() -> None:
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["profile_schema"] = "profile"
    proposed["profile_schema_version"] = "provider-invented"
    proposed["note_status"] = "analytical_atomic_note"
    proposed["geography"] = "Rwanda"
    proposed["boundaries"] = "The source is descriptive."
    proposed["findings"][0]["direction"] = None
    proposed["findings"][0]["quantitative_result"] = {"duplicate": True}
    proposed["evidence_anchors"][0]["source_locators"][0]["locator_type"] = "pages"
    proposed["evidence_anchors"][0]["locators"] = [
        {
            "locator_type": "pages",
            "value": "pp. 13-14",
            "source_native": True,
            "supports_strong_assertion": True,
        }
    ]
    proposed["evidence_anchors"][0]["quantitative_result"] = {
        "provenance": "reported"
    }

    result = build_evidence_profile(
        note,
        source_set_id="source-set-reasoner",
        provider="deepseek",
        model="deepseek-v4-flash",
        reasoner_method=lambda _prompt: proposed,
    )

    assert result.profile_schema == "evidence_profile"
    assert result.geography == ["Rwanda"]
    assert result.boundaries == ["The source is descriptive."]
    assert result.findings == []
    assert result.evidence_anchors[0].source_locators
    assert "pp. 13-14" in result.evidence_anchors[0].locators
    assert result.evidence_anchors[0].quantitative_result is not None
    assert result.evidence_anchors[0].quantitative_result.provenance == "source_reported"


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


def test_committed_note_controls_anchor_coverage_and_practitioner_attribution() -> None:
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    envelope = proposed["evidence_anchors"][0]["support_envelope"]
    envelope.update(
        empirical_role="none",
        argument_role="practitioner_guidance",
        coverage="limited_text",
        support_status="unsupported",
        restrictions=["The recommendation is not an effectiveness evaluation."],
    )

    result = build_evidence_profile(note, reasoner_method=lambda _prompt: proposed)
    controlled = result.evidence_anchors[0].support_envelope

    assert controlled.coverage == "full_text"
    assert controlled.support_status == "supported"
    assert controlled.argument_role == "practitioner_guidance"
    assert controlled.restrictions == [
        "The recommendation is not an effectiveness evaluation."
    ]


def test_reasoner_cannot_return_an_empty_full_document_profile() -> None:
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["findings"] = []
    proposed["evidence_anchors"] = []

    with pytest.raises(
        ProfileParseError, match="analytical_profile_requires_substantive_anchor"
    ):
        build_evidence_profile(note, reasoner_method=lambda _prompt: proposed)


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

    assert result.findings == []
    assert len(result.evidence_anchors) == 1
    assert result.validity["omitted_untraceable_or_uninterpreted_finding_count"] == 0


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
