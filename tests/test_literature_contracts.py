from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.models import (
    ArtifactManifest,
    ClusterProposal,
    ClusterSynthesis,
    EvidenceFinding,
    EvidenceProfile,
    GapRationale,
    LiteratureMapReport,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
    RunReport,
    StatusReport,
)


def test_literature_mapping_policy_defaults_and_validation() -> None:
    policy = LiteratureMappingPolicy()
    assert policy.to_dict() == {
        "synthesis_enabled": True,
        "require_question": False,
        "auto_promote_clusters": True,
        "auto_promote_debates": True,
        "auto_promote_gaps": True,
        "source_backed_threshold": 3,
        "max_memberships": 3,
        "external_discovery": "disabled",
        "max_profile_calls": 100,
        "max_synthesis_calls": 24,
        "profile_workers": 4,
        "literature_deadline_seconds": 1800.0,
        "deepseek_packet_context_fraction": 0.8,
        "weak_gap_handling": "audit_only",
        "cluster_gap_projection": "inline",
        "require_executable_gap_design": True,
    }
    assert LiteratureMappingPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(ValueError, match="external_discovery"):
        LiteratureMappingPolicy(external_discovery="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source_backed_threshold"):
        LiteratureMappingPolicy.from_dict({"source_backed_threshold": "3"})
    with pytest.raises(ValueError, match="synthesis_enabled"):
        LiteratureMappingPolicy.from_dict({"synthesis_enabled": 1})
    with pytest.raises(ValueError, match="unknown literature_mapping fields"):
        LiteratureMappingPolicy.from_dict({"model_enthusiasm": 1})
    with pytest.raises(ValueError, match="weak_gap_handling"):
        LiteratureMappingPolicy.from_dict({"weak_gap_handling": "markdown"})
    with pytest.raises(ValueError, match="cluster_gap_projection"):
        LiteratureMappingPolicy.from_dict({"cluster_gap_projection": "standalone_section"})
    with pytest.raises(ValueError, match="require_executable_gap_design"):
        LiteratureMappingPolicy.from_dict({"require_executable_gap_design": "true"})


def test_cluster_and_gap_reasoning_types_round_trip_strictly() -> None:
    proposal = ClusterProposal.from_dict(
        {
            "proposal_id": "proposal-1",
            "label": "Mediator legitimacy",
            "semantic_identity": "mediator legitimacy",
            "source_ids": ["source-a", "source-b"],
            "supporting_evidence": [{"source_id": "source-a", "claim_id": "claim-a", "locator": "p. 10"}],
        }
    )
    assert ClusterProposal.from_dict(proposal.to_dict()) == proposal
    synthesis = ClusterSynthesis.from_dict(
        {
            "cluster_id": "cluster-1",
            "boundaries": ["African civil wars"],
            "central_findings": [{"finding": "Legitimacy matters", "evidence": []}],
        }
    )
    assert ClusterSynthesis.from_dict(synthesis.to_dict()) == synthesis
    rationale = GapRationale.from_dict(
        {"gap_id": "gap-1", "related_cluster_ids": ["cluster-1"], "gap_statement": "A bounded gap."}
    )
    assert GapRationale.from_dict(rationale.to_dict()) == rationale
    with pytest.raises(ValueError, match="unknown cluster proposal fields"):
        ClusterProposal.from_dict({"source_ids": [], "enthusiasm": 1})
    with pytest.raises(ValueError, match="list of strings"):
        GapRationale.from_dict({"related_cluster_ids": "cluster-1"})
    with pytest.raises(ValueError, match="value_assessment must be a mapping"):
        GapRationale.from_dict({"value_assessment": []})
    with pytest.raises(ValueError, match="non_obviousness_passed"):
        GapRationale.from_dict(
            {"value_assessment": {"non_obviousness_passed": "false"}}
        )


def test_map_request_round_trips_literature_policy(tmp_path: Path) -> None:
    request = MapRequest(
        tmp_path,
        literature_policy=LiteratureMappingPolicy(
            external_discovery="per_run",
            max_profile_calls=12,
        ),
    )
    assert request.to_dict()["literature_policy"]["external_discovery"] == "per_run"
    assert MapRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="literature_policy must be"):
        MapRequest.from_dict({"workspace": str(tmp_path), "literature_policy": []})
    with pytest.raises(ValueError, match="requires a question"):
        MapRequest(tmp_path, literature_policy=LiteratureMappingPolicy(require_question=True))


def test_evidence_profile_and_finding_are_serializable() -> None:
    finding = EvidenceFinding(
        finding_id="finding-1",
        claim="Participation increased trust.",
        finding_type="association",
        direction="positive",
        magnitude="small",
        comparison="participants versus non-participants",
        conditions=["after adjustment"],
        plain_english_meaning="Participants reported slightly more trust.",
        is_statistical=True,
        evidence="Reported model estimate.",
        locator="p. 14",
        locators=["p. 14"],
        qualifiers=["observational"],
        confidence="moderate",
    )
    profile = EvidenceProfile(
        profile_id="profile-1",
        note_id="note-1",
        source_id="source-1",
        note_hash="note-sha256",
        source_hash="source-sha256",
        source_role="empirical_test",
        coverage={"status": "full_text"},
        validity={"status": "valid"},
        context={"question": "What changes trust?"},
        concepts=["participation", "trust"],
        theories=["contact theory"],
        mechanisms=["learning"],
        methods=["panel regression"],
        cases=["case-a"],
        datasets=["survey-a"],
        data=["panel survey"],
        geography=["region-a"],
        periods=["2018-2020"],
        populations=["participants"],
        outcomes=["institutional trust"],
        measures=["trust scale"],
        study_family_id="study-family-1",
        findings=[finding],
        limitations=["observational design"],
        boundaries=["urban participants"],
        gaps=["rural comparison"],
        future_research=["replicate in rural sites"],
        provider="deepseek",
        model="deepseek-v4-flash",
        dependency_hash="dependency-sha256",
    )
    payload = profile.to_dict()
    assert payload["profile_schema"] == "evidence_profile"
    assert payload["profile_schema_version"] == "1.0"
    assert payload["findings"] == [finding.to_dict()]
    assert payload["findings"][0]["plain_english_meaning"] == "Participants reported slightly more trust."
    assert payload["source_role"] == "empirical_test"
    assert payload["data"] == ["panel survey"]
    assert payload["future_research"] == ["replicate in rural sites"]

    restored = EvidenceFinding.from_dict(
        {
            "claim": "Participation increased trust.",
            "magnitude": "small",
            "comparison": "participants versus non-participants",
            "conditions": ["after adjustment"],
            "plain_english_meaning": "Participants reported slightly more trust.",
            "is_statistical": True,
            "locator": "p. 14",
        }
    )
    assert restored.locator == "p. 14"
    assert restored.locators == ["p. 14"]
    assert restored.is_statistical is True


def test_literature_request_and_report_are_serializable(tmp_path: Path) -> None:
    request = LiteratureMapRequest(
        workspace=tmp_path,
        source_set_id="source-set-1",
        run_id="run-1",
        map_id="map-1",
    )
    assert request.question is None
    assert request.to_dict()["workspace"] == str(tmp_path)
    assert LiteratureMapRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="requires a question"):
        LiteratureMapRequest(
            workspace=tmp_path,
            literature_policy=LiteratureMappingPolicy(require_question=True),
        )

    report = LiteratureMapReport(
        status="partial",
        map_id="map-1",
        run_id="run-1",
        source_set_id="source-set-1",
        stage="synthesis",
        counts={"profile_count": 3},
        artifact_paths={"map": Path("03_literature_synthesis/maps/map-1.yml")},
        partial_reason="synthesis_call_limit",
    )
    payload = report.to_dict()
    assert payload["counts"] == {"profile_count": 3}
    assert payload["artifact_paths"]["map"] == "03_literature_synthesis/maps/map-1.yml"
    assert payload["partial_reason"] == "synthesis_call_limit"


def test_all_report_models_default_to_engine_0_3_schema_1_2(tmp_path: Path) -> None:
    run_report = RunReport(status="ok", workspace=tmp_path, run_id="run-1")
    reports = (
        ArtifactManifest(status="ok", workspace=tmp_path),
        run_report,
        StatusReport(status="ok", workspace=tmp_path),
        LiteratureMapReport(status="ok"),
    )
    assert {(report.engine_version, report.artifact_schema_version) for report in reports} == {
        ("0.5.0", "1.4")
    }
    assert run_report.literature_map == {}
    assert run_report.literature_report == {}
    assert {
        run_report.profile_count,
        run_report.unclustered_count,
        run_report.cluster_count,
        run_report.debate_count,
        run_report.mapped_gap_count,
        run_report.gap_lead_count,
    } == {0}
