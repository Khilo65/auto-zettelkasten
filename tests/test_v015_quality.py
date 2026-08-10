from __future__ import annotations

from auto_zettelkasten.literature import (
    CLUSTER_PLAN_PROMPT_VERSION,
    _reconcile_final_unclustered_sources,
    _streamlined_cluster_markdown,
    validate_streamlined_cluster_synthesis,
)
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import migrate_v015_metadata
from auto_zettelkasten.readers import (
    _cluster_plan_system_prompt,
    _source_bundle_system_prompt,
    _validate_literature_response,
)
from auto_zettelkasten.workspace import initialize


def _profile(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_path": f"02_source_memory/notes/{source_id}.md",
        "title": f"Study {source_id}",
        "analytical": True,
        "limited": False,
        "evidence_eligibility": "substantive_bounded",
        "claims": [
            {
                "source_id": source_id,
                "evidence_anchor_id": f"anchor-{source_id}",
                "claim_id": f"anchor-{source_id}",
                "text": f"Finding from {source_id}",
                "locator": "p. 10",
                "support_status": "supported",
                "support_envelope": {
                    "empirical_role": "associational",
                    "argument_role": "none",
                    "coverage": "full_text",
                    "scope": "reported model",
                    "restrictions": ["observational design"],
                    "support_status": "supported",
                },
            }
        ],
    }


def test_source_bundle_prompt_uses_one_shot_statistical_interpretation() -> None:
    prompt = _source_bundle_system_prompt()

    assert "source bundle prompt v6" in prompt
    assert "9 percentage points lower" in prompt
    assert "22.5% lower relative" in prompt
    assert "odds, hazards, risks, and probabilities distinct" in prompt
    assert "Do not convert a logit coefficient or interaction" in prompt
    assert "p-value is not an effect size" in prompt
    assert "Do not return stable IDs" in prompt
    assert "or a self-review object" in prompt
    assert "another model call" in prompt
    assert "without calculating new ones" not in prompt


def test_cluster_plan_can_omit_unclustered_reasons() -> None:
    prompt = _cluster_plan_system_prompt()
    parsed = _validate_literature_response(
        {"clusters": [], "neighbor_relationships": []},
        kind="cluster_plan",
    )

    assert f"cluster plan prompt v{CLUSTER_PLAN_PROMPT_VERSION}" in prompt
    assert "COHERENCE RULE" in prompt
    assert "one bounded research problem" in prompt
    assert "practitioner guidance alone" in prompt
    assert "does not need reasons from you" in prompt
    assert parsed["unclustered_sources"] == []


def test_streamlined_cluster_preserves_technical_and_plain_english_results() -> None:
    profiles = [_profile("A"), _profile("B")]
    cluster = {
        "cluster_id": "cluster-duration",
        "label": "Civil War Duration",
        "semantic_identity": "civil war duration",
        "source_ids": ["A", "B"],
    }
    response = {
        "cluster_contract": "streamlined-full-note-v1",
        "cluster_id": "cluster-duration",
        "status": "accepted",
        "title": "Civil War Duration",
        "organizing_mode": "outcome",
        "organizing_problem": "What is associated with war duration?",
        "bottom_line": "The studies identify conditional associations.",
        "retained_member_ids": ["A", "B"],
        "dropped_members": [],
        "differences": [],
        "limits": ["The estimates are observational."],
        "related_clusters": [],
        "lines_of_inquiry": [
            {
                "title": "Operational conditions",
                "synthesis": "The association varies across operational conditions.",
                "study_findings": [
                    {
                        "source_id": source_id,
                        "finding": (
                            "The reported probability declines from 40% to 31%."
                        ),
                        "method_scope": "Observational regression.",
                        "technical_result": "40% versus 31%; p<.05.",
                        "plain_english_meaning": (
                            "That is 9 percentage points lower, or 22.5% lower "
                            "relative to the 40% baseline."
                        ),
                        "relation_to_line": "supports",
                        "evidence": [
                            {
                                "source_id": source_id,
                                "evidence_anchor_id": f"anchor-{source_id}",
                                "locator": "p. 10",
                            }
                        ],
                    }
                    for source_id in ("A", "B")
                ],
            }
        ],
    }

    validated = validate_streamlined_cluster_synthesis(
        response,
        cluster,
        profiles,
    )
    markdown = _streamlined_cluster_markdown(
        cluster,
        validated,
        profile_by_source={
            str(profile["source_id"]): profile for profile in profiles
        },
        cluster_by_id={"cluster-duration": cluster},
    )

    contribution = validated["source_contributions"][0]
    assert contribution["technical_result"] == "40% versus 31%; p<.05."
    assert contribution["plain_english_meaning"].startswith(
        "That is 9 percentage points lower"
    )
    assert "Technical result: 40% versus 31%; p<.05." in markdown
    assert "In plain English: That is 9 percentage points lower" in markdown


def test_unclustered_snapshot_is_neutral_and_excludes_limited_profiles() -> None:
    profiles = [
        _profile("clustered"),
        _profile("available-later"),
        {
            **_profile("limited"),
            "limited": True,
            "evidence_eligibility": "context_only",
        },
    ]

    rows = _reconcile_final_unclustered_sources(
        profiles,
        [{"cluster_id": "cluster-a", "source_ids": ["clustered"]}],
        [
            {
                "source_id": "available-later",
                "reason": "no_admitted_thematic_cluster",
            }
        ],
        [
            {
                "source_ids": ["available-later"],
                "reason": "proposed_cluster_failed_evidence_matrix",
            }
        ],
    )

    assert rows == [
        {
            "source_id": "available-later",
            "note_id": "note-available-later",
            "reason": "currently_unclustered",
            "reason_detail": (
                "This source has no active cluster membership in the current "
                "map and remains eligible for future cluster plans."
            ),
        }
    ]


def test_v015_metadata_migration_is_local_and_idempotent(tmp_path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config = read_yaml(config_path)
    manifest = read_yaml(manifest_path)
    config.update(
        engine_version="0.14.0",
        artifact_schema_version="1.13",
        prompt_version="9",
    )
    manifest["engine_version"] = "0.14.0"
    manifest["artifact_schema_version"] = "1.13"
    write_yaml(config_path, config)
    write_yaml(manifest_path, manifest)
    note = tmp_path / "02_source_memory" / "notes" / "human.md"
    note.write_text("# Human note\n", encoding="utf-8")

    first = migrate_v015_metadata(tmp_path)
    second = migrate_v015_metadata(tmp_path)

    assert first["status"] == "migrated"
    assert first["provider_calls"] == 0
    assert first["source_notes_rewritten"] == 0
    assert second["status"] == "already_current"
    assert read_yaml(config_path)["engine_version"] == "0.15.0"
    assert read_yaml(config_path)["prompt_version"] == "10"
    assert read_yaml(manifest_path)["engine_version"] == "0.15.0"
    assert note.read_text(encoding="utf-8") == "# Human note\n"
