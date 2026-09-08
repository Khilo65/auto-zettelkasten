from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten import literature, profiles
from auto_zettelkasten.models import (
    EvidenceAnchor,
    EvidenceFinding,
    EvidenceProfile,
    _canonicalize_anchor_ids,
)
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
    assert PROFILE_SCHEMA_VERSION == "1.4"
    assert PROFILE_PROMPT_VERSION == profiles.profile_prompt_version == "7"
    assert PROFILE_CLASSIFIER_VERSION == profiles.profile_classifier_version == "3"
    assert PROFILE_ALGORITHM_VERSION == profiles.profile_algorithm_version == "10"
    assert ANCHOR_ALGORITHM_VERSION == "4"
    assert SUPPORT_ENVELOPE_VERSION == "1"
    assert COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION == "8"


def test_actor_position_labels_are_not_misread_as_page_locators() -> None:
    assert profiles._first_locator("Interviewee P3 described mediator legitimacy") == ""
    assert profiles._first_locator("Positions P3 and P5 diverged") == ""
    assert profiles._first_locator("See p. 3 and pp. 5-7") == "p. 3; pp. 5-7"
    assert profiles._first_locator("See pages 12-14") == "pages 12-14"
    assert (
        profiles._first_locator("Compare PDF p. 3 with printed p. 3")
        == "PDF p. 3; p. 3"
    )
    assert (
        profiles._best_matching_source_locator(
            "archival evidence supports the claim",
            "archival evidence: PDF p. 3; unrelated discussion: p. 3",
        )
        == "PDF p. 3"
    )


def test_pdf_page_namespace_survives_typed_consumer_projection() -> None:
    locator = "PDF p. 7; p. 7"
    typed = profiles._source_locator_payloads(
        locator, source_id="source-a", evidence_anchor_id="anchor-a"
    )

    assert [row["value"] for row in typed] == ["PDF p. 7", "p. 7"]
    assert [row["page_start"] for row in typed] == [7, 7]
    assert all(row["locator_type"] == "page" for row in typed)
    assert all(row["source_native"] for row in typed)
    assert all(row["supports_strong_assertion"] for row in typed)

    normalized = literature.normalize_evidence_profiles(
        [
            {
                "source_id": "source-a",
                "note_id": "note-a",
                "note_status": "analytical_atomic_note",
                "evidence_eligibility": "substantive_bounded",
                "evidence_anchors": [
                    {
                        "evidence_anchor_id": "anchor-a",
                        "source_id": "source-a",
                        "claim": "A source-grounded finding.",
                        "locator": locator,
                        "locators": [locator],
                        "source_locators": typed,
                        "support_envelope": {
                            "empirical_role": "descriptive",
                            "argument_role": "none",
                            "coverage": "full_text",
                            "scope": {},
                            "restrictions": [],
                            "support_status": "supported",
                        },
                    }
                ],
            }
        ]
    )[0]
    claim = normalized["claims"][0]
    reference = literature._evidence_ref(claim)

    assert claim["locator"] == locator
    assert reference["locator"] == locator
    assert literature._human_locator_text(reference["locator"]) == locator


def test_methods_ignore_negated_terms_in_mixed_prose() -> None:
    cases = (
        (
            "This article uses interviews but does not report a systematic sample, "
            "survey, experiment, or linear regression.",
            ["interviews"],
        ),
        ("A survey was not conducted.", []),
        ("Survey evidence is absent.", []),
        ("Rather than a survey, the article compares documents.", []),
        (
            "The report has no sample-size information and relies on interviews.",
            ["interviews"],
        ),
        (
            "The article did not use random sampling and instead conducted interviews.",
            ["interviews"],
        ),
        (
            "The article reports results from online surveys. It is not itself a survey.",
            ["survey"],
        ),
        (
            "The summary does not describe respondent selection, survey fieldwork "
            "dates, or weighting.",
            ["survey"],
        ),
        ("The survey was not weighted.", ["survey"]),
        ("The study did not conduct survey fieldwork.", []),
        ("The report never used survey data.", []),
        ("The article lacks survey results.", []),
        ("There are no reliable survey data.", []),
        ("The researchers did not collect survey data.", []),
        ("Survey fieldwork was not conducted.", []),
        ("The report does not describe whether survey fieldwork dates exist.", []),
        (
            "The report does not provide survey fieldwork dates because no survey "
            "was conducted.",
            [],
        ),
    )

    for text, expected in cases:
        assert profiles._methods({"Method and Research Design": text}) == expected


def test_methods_use_affirmed_evidence_section_terms() -> None:
    assert profiles._methods(
        {
            "Method and Research Design": "The article reports repeated polling.",
            "Evidence and Data": "The evidence comes from daily opt-in online surveys.",
        }
    ) == ["survey"]
    assert profiles._methods(
        {
            "Method and Research Design": "This is selected journalistic reporting.",
            "Evidence and Data": "The article does not use a survey or systematic sample.",
        }
    ) == []


def test_metadata_refresh_preserves_evidence_and_complete_lineage() -> None:
    note = _analytical_note().replace(
        "## Method and Research Design",
        "## Method and Research Design\n\nInstitutional series: Series A\nInstitutional series: Series B",
    )
    profile = deterministic_profile(note)
    corrected = _with_frontmatter_updates(
        note, title="Corrected title", DOI="10.1234/corrected", doi="10.1234/corrected",
        creators=[{"creatorType": "author", "name": "Correct Institute"}], date="2020",
    )
    refreshed = profiles._refresh_profile_metadata(profile, corrected)
    canonical = deterministic_profile(corrected)
    assert refreshed.study_lineage == canonical.study_lineage
    assert refreshed.study_family_id == canonical.study_family_id
    for before, after in zip(profile.evidence_anchors, refreshed.evidence_anchors, strict=True):
        old, new = before.to_dict(), after.to_dict()
        assert old.pop("evidence_anchor_id") == new.pop("evidence_anchor_id")
        for key in ("study_family_id", "revision_hash"):
            old.pop(key)
            new.pop(key)
        assert old == new
    assert profiles._refresh_profile_metadata(refreshed, corrected) == refreshed
    with pytest.raises(ProfileContractError, match="source_id mismatch"):
        profiles._refresh_profile_metadata(profile, _with_frontmatter_updates(note, source_id="other-source"))


def test_current_profile_algorithm_refreshes_stale_bundle_methods_without_a_call() -> None:
    note = _analytical_note()
    profile = deterministic_profile(note)
    profile.methods = ["Provider-described mixed method", "survey", "panel regression"]
    profile.context["profile_generation_route"] = "source_analysis_bundle"
    profile.validity.update(
        algorithm_version="7",
        committed_note_anchor_augmentation_version=(
            COMMITTED_NOTE_ANCHOR_AUGMENTATION_VERSION
        ),
    )

    refreshed, changed = augment_profile_from_committed_note(
        profile,
        note,
        source_set_id="",
        provider="codex",
        model="gpt-5.6-luna",
    )

    assert changed is True
    assert refreshed.methods == [
        "Provider-described mixed method",
        "panel regression",
        "survey",
    ]
    assert refreshed.validity["algorithm_version"] == "10"


def test_mismatched_plain_english_rows_do_not_shift_between_findings() -> None:
    rows = profiles._extract_findings(
        "- The program started in 2002.\n"
        "- It targeted more than 400,000 combatants.\n"
        "- Outcomes were higher than the comparison group.\n"
        "- Participation was higher than 5%.",
        "- The program operated at substantial scale.",
        "p. 1",
        note_id="note-1",
        populations=[],
        outcomes=[],
    )

    assert [row["plain_english_meaning"] for row in rows] == [
        row["claim"] for row in rows
    ]
    assert rows[1]["comparison"] == "not_reported"
    assert rows[2]["comparison"].startswith("higher than")
    assert rows[3]["comparison"].startswith("higher than 5%")


def test_canonical_anchor_collision_rebinds_nested_ids() -> None:
    anchors = [
        EvidenceAnchor.from_dict(
            {
                "source_id": "source-a",
                "evidence_role": "associational",
                "claim": f"Claim {index}",
                "locator": "p. 10",
            }
        )
        for index in range(1, 3)
    ]

    canonical = _canonicalize_anchor_ids(anchors)
    ids_before = {anchor.claim: anchor.evidence_anchor_id for anchor in canonical}
    enriched = []
    for index, anchor in enumerate(canonical, start=1):
        payload = anchor.to_dict()
        payload["revision_hash"] = ""
        payload["source_locators"] = [
            {
                "locator_id": f"locator-{index}",
                "source_id": "source-a",
                "evidence_anchor_id": anchor.evidence_anchor_id,
                "locator_type": "page",
                "value": "10",
                "page_start": 10,
                "page_end": 10,
                "source_native": True,
                "supports_strong_assertion": True,
            }
        ]
        payload["quantitative_result"] = {
            "quantitative_result_id": f"result-{index}",
            "source_id": "source-a",
            "evidence_anchor_id": "stale-anchor",
            "statistic": str(index),
            "provenance": "unknown",
        }
        enriched.append(EvidenceAnchor.from_dict(payload))

    canonical = _canonicalize_anchor_ids(enriched)

    assert len(canonical) == 2
    assert len({anchor.evidence_anchor_id for anchor in canonical}) == 2
    assert {anchor.claim: anchor.evidence_anchor_id for anchor in canonical} == ids_before
    assert all(
        locator.evidence_anchor_id == anchor.evidence_anchor_id
        for anchor in canonical
        for locator in anchor.source_locators
    )
    assert all(
        anchor.quantitative_result is not None
        and anchor.quantitative_result.evidence_anchor_id
        == anchor.evidence_anchor_id
        for anchor in canonical
    )


def test_incidental_heading_word_is_not_source_native_support() -> None:
    rows = profiles._source_locator_payloads(
        'Section “Faulty Justifications,” UNRWA discussion',
        source_id="source-a",
        evidence_anchor_id="anchor-a",
    )

    assert [
        (row["locator_type"], row["value"])
        for row in rows
        if row["supports_strong_assertion"]
    ] == [("quote_span", "“Faulty Justifications,”")]


@pytest.mark.parametrize("route", ["deterministic", "reasoner", "source_analysis_bundle"])
def test_current_refresh_preserves_note_features_without_generating_claim_inventory(route):
    note = _analytical_note()
    original = deterministic_profile(note)
    original.context["profile_generation_route"] = route
    before = profile_to_dict(original)
    updated, _ = augment_profile_from_committed_note(
        original, note, source_set_id="test", provider="deepseek", model="deepseek-v4-flash")
    after = profile_to_dict(updated)
    assert "evidence_anchors" not in after and "findings" not in after
    for field in ("source_id", "note_id", "note_hash", "source_role", "coverage", "concepts", "methods", "boundaries"):
        assert after[field] == before[field]
    assert validate_profile(updated).passed






























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
    assert profile.validity["profile_prompt_version"] == "7"
    assert profile.validity["classifier_version"] == "3"
    assert profile.validity["algorithm_version"] == "10"
    assert profile.research_questions
    assert {"participation", "trust"} <= set(profile.concepts)
    assert profile.theories == ["contact theory"]
    assert profile.mechanisms == ["learning"]
    assert profile.methods == ["panel regression", "survey"]
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

    assert profile.findings == []
    assert profile.evidence_anchors == []
    assert profile.study_lineage is not None
    assert profile.study_lineage.authors == ["Researcher"]
    assert "panel survey" in profile.study_lineage.datasets
    assert profile.study_lineage.periods == ["2018–2020"]

    validation = validate_profile(profile)
    assert validation.passed, validation.errors
    assert validation.substantive is True


def test_profile_validation_warns_on_unlocated_and_unexplained_statistical_anchors() -> (
    None
):
    profile = _historical_profile()
    anchor = profile.evidence_anchors[0]

    profile.evidence_anchors = [
        replace(anchor, locator="", locators=[], source_locators=[])
    ]
    unlocated = validate_profile(profile)
    assert unlocated.passed
    assert "anchor_0:traceable_locator_unresolved" in unlocated.warnings

    profile.evidence_anchors = [replace(anchor, plain_english_meaning="")]
    unexplained = validate_profile(profile)
    assert unexplained.passed
    assert (
        "anchor_0:plain_english_meaning_required_for_statistical_anchor"
        in unexplained.warnings
    )



def test_generated_atomic_note_heading_is_not_source_native_evidence() -> None:
    payload = profile_to_dict(_historical_profile())
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

    assert validation.passed
    assert "anchor_0:source_native_locator_unresolved" in validation.warnings



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


def test_evidence_bounded_partial_note_is_substantive() -> None:
    note = _with_frontmatter_updates(
        _analytical_note(),
        note_status="partial_document_atomic_note",
        source_scope="partial_document",
        source_coverage={"gate": "limited"},
        evidence_eligibility="substantive_bounded",
    )

    profile = deterministic_profile(note)
    validation = validate_profile(profile)

    assert profile.evidence_eligibility == "substantive_bounded"
    assert profile.excluded_from_synthesis is False
    assert profile.findings == []
    assert profile.evidence_anchors == []
    assert validation.passed, validation.errors


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
    assert payload["profile_prompt_version"] == "7"
    assert payload["classifier_version"] == "3"
    assert payload["algorithm_version"] == "10"
    assert payload["profile_schema_version"] == "1.4"
    assert "anchor_algorithm_version" not in payload
    assert "support_envelope_version" not in payload
    assert baseline == profile_dependency_fingerprint(
        _with_generated_graph(note), **kwargs
    )
    assert baseline != profile_dependency_fingerprint(
        note.replace("12%", "13%"), **kwargs
    )
    assert baseline == profile_dependency_fingerprint(
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
        note, **kwargs, profile_prompt_version="8"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_classifier_version="4"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_algorithm_version="11"
    )
    assert baseline != profile_dependency_fingerprint(
        note, **kwargs, profile_schema_version="1.0"
    )
    assert baseline == profile_dependency_fingerprint(
        note, **kwargs, anchor_algorithm_version="5"
    )
    assert baseline == profile_dependency_fingerprint(
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
        profile_schema_version="1.3",
        source_id="source-1", coverage={"status": "full_text"}, findings=[first, second]
    )
    reordered = EvidenceProfile(
        profile_schema_version="1.3",
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
        profile_schema_version="1.3",
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
        profile_schema_version="1.3",
        source_id="source-1", coverage={"status": "full_text"}, findings=[first, second]
    )
    reverse = EvidenceProfile(
        profile_schema_version="1.3",
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
        EvidenceProfile(profile_schema_version="1.3", source_id="source-1", evidence_anchors=[anchor] * 25)





def test_v1_profile_mapping_sidecar_and_checkpoint_upgrade_mechanically(
    tmp_path: Path,
) -> None:
    legacy_profile = EvidenceProfile(
        profile_schema_version="1.3",
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
    assert upgraded.profile_schema_version == "1.0"
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
    assert persisted["profile_schema_version"] == "1.0"
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
        profile_schema_version="1.3",
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
    profile = _historical_profile()
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
    assert result.evidence_anchors == []
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
    assert "evidence_anchors" not in prompts[0]
    assert "compact discovery features" in prompts[0]
    assert "do not reread" in prompts[0].casefold()
    assert "## Graph Links" not in prompts[0]
    assert "synthetic source full text sentinel" not in prompts[0]





def test_live_reasoner_normalizes_only_unambiguous_profile_shape_aliases():
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed.update(profile_schema="profile", profile_schema_version="provider-invented",
                    geography="Rwanda", boundaries="The source is descriptive.")
    result = build_evidence_profile(note, reasoner_method=lambda _prompt: proposed)
    assert result.profile_schema == "evidence_profile"
    assert result.profile_schema_version == "1.4"
    assert result.geography == ["Rwanda"]
    assert result.boundaries == ["The source is descriptive."]
    assert result.findings == [] and result.evidence_anchors == []



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





def test_full_document_profile_does_not_require_a_duplicate_claim_inventory():
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["findings"] = []
    proposed["evidence_anchors"] = []
    result = build_evidence_profile(note, reasoner_method=lambda _prompt: proposed)
    assert result.concepts and result.methods
    assert validate_profile(result).passed



def test_current_reasoner_discards_unsolicited_legacy_claim_inventory():
    note = _analytical_note()
    proposed = profile_to_dict(deterministic_profile(note))
    proposed["findings"] = [{"claim": "Do not append this duplicate."}]
    proposed["evidence_anchors"] = [{"claim": "Do not recreate this inventory."}]
    result = build_evidence_profile(note, reasoner_method=lambda _prompt: proposed)
    assert result.findings == [] and result.evidence_anchors == []
    assert result.concepts == proposed["concepts"]



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
        "<!-- auto-zettelkasten:graph:start -->\n"
        "- related: [[Generated Note]]\n"
        "- cluster: [[cluster-generated]]\n"
        "<!-- auto-zettelkasten:graph:end -->\n"
    )


def _with_frontmatter_updates(note: str, **updates: object) -> str:
    frontmatter, body = parse_atomic_note(note)
    frontmatter.update(updates)
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_text}\n---\n{body}"


@pytest.mark.parametrize("label", ["p. 9", "p. 9, Table 3, Figure 4, Chapter 5, Paragraph 6"])
@pytest.mark.parametrize("prefix", ["Quote ", "Heading "])
@pytest.mark.parametrize("suffix", ["", " (PDF page 2)"])
def test_typed_locators_ignore_coordinates_inside_quoted_payload(label, prefix, suffix) -> None:
    rows = profiles._source_locator_payloads(
        prefix + '“' + label + '”' + suffix,
        source_id="source-a", evidence_anchor_id="anchor-a",
    )
    assert [(row["locator_type"], row["value"]) for row in rows if row["locator_type"] != "quote_span"] == (
        [("page", "PDF page 2")] if suffix else []
    )
    assert [row["value"] for row in rows if row["locator_type"] == "quote_span"] == (
        ['“' + label + '”'] if len(label) >= 12 else []
    )


def test_typed_locators_ignore_page_syntax_inside_single_quoted_heading() -> None:
    rows = profiles._source_locator_payloads(
        "Heading 'p. 9 and Table 3' (PDF page 2)",
        source_id="source-a", evidence_anchor_id="anchor-a",
    )
    assert [(row["locator_type"], row["value"]) for row in rows] == [("page", "PDF page 2")]


def _historical_profile():
    """Explicit v1.3 record for strict historical readers, never current generation."""
    payload = profile_to_dict(deterministic_profile(_analytical_note()))
    payload["profile_schema_version"] = "1.3"
    payload["findings"] = [EvidenceFinding(
        claim="Participation increased trust by 12%.", locator="Table 2, p. 14",
        finding_type="statistical", magnitude="12%", plain_english_meaning="Higher reported trust."
    ).to_dict()]
    profile = profile_from_dict(payload)
    return profiles._enrich_profile_v12_records(
        profile, frontmatter={}, sections={})
