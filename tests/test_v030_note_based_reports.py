import json

import pytest

from auto_zettelkasten import literature
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.readers import CodexReader, DeepSeekReader


def _profiles():
    return [
        {"source_id": source_id, "note_id": f"note-{source_id}", "title": source_id,
         "note_status": "analytical_atomic_note", "evidence_eligibility": "substantive_bounded",
         "thesis": f"Detailed thesis for {source_id}",
         "evidence_anchors": [{"claim": "Historical inventory must not be read"}]}
        for source_id in ("A", "B")
    ]


class _CurrentReasoner:
    name = "local"
    model = "test"

    def plan_clusters(self, profiles, request, *, context=None):
        return {"clusters": [{"cluster_id": "family", "title": "Family",
                 "semantic_identity": "family", "organizing_mode": "question",
                 "organizing_problem": "What do the studies contribute?",
                 "members": [{"source_id": row["source_id"], "role": "core"} for row in profiles]}],
                "neighbor_relationships": [], "unclustered_sources": []}

    def synthesize_cluster(self, profiles, request, *, context=None):
        ids = [row["source_id"] for row in profiles]
        return {"cluster_contract": "streamlined-full-note-v4",
                "cluster_id": context["cluster"]["cluster_id"], "title": "Family",
                "organizing_problem": "What do the studies contribute?", "status": "accepted",
                "retained_member_ids": ids, "member_roles": {sid: "core" for sid in ids},
                "dropped_members": [], "debate_state": "complementary_positions",
                "bottom_line": "Distinct contributions illuminate the field together.",
                "lines_of_inquiry": [{"title": "Contributions", "synthesis": "Related findings differ in emphasis.",
                    "study_findings": [{"source_id": sid, "finding": f"Specific finding from {sid}",
                        "method_scope": "Comparative analysis", "relation_to_line": "contextualizes"} for sid in ids]}],
                "limits": [], "related_clusters": [], "acquisition_candidate_dispositions": []}


@pytest.mark.parametrize("reader_type", [CodexReader, DeepSeekReader])
@pytest.mark.parametrize("shared_plan", [False, True])
def test_current_report_skips_legacy_analysis_and_replays(tmp_path, monkeypatch, reader_type, shared_plan):
    def forbidden(*args, **kwargs):
        pytest.fail("Current note-based report called retired analytical machinery")

    for name in ("_normalize_claims", "build_independence_records", "build_locator_audit",
                 "map_profile_relations", "build_literature_propositions", "build_evidence_matrices",
                 "build_debate_registry", "generate_gap_candidates", "search_and_validate_gaps",
                 "_apply_researcher_display_safeguards"):
        monkeypatch.setattr(literature, name, forbidden)
    reasoner = reader_type("gpt-5.6-terra") if reader_type is CodexReader else reader_type()
    captured = []

    def completion(self, system, user, **settings):
        payload = json.loads(user)
        captured.append((settings["label"], payload))
        assert "evidence_anchors" not in user
        if settings["label"] == "cluster synthesis":
            assert all(row.get("atomic_note_markdown") for row in payload["profiles"])
            return _CurrentReasoner().synthesize_cluster(
                payload["profiles"], None, context=payload["context"]
            )
        return _CurrentReasoner().plan_clusters(_profiles(), None)

    monkeypatch.setattr(reader_type, "_authorize_request", lambda self: None)
    monkeypatch.setattr(reader_type, "_literature_json_call", completion)
    monkeypatch.setattr(reader_type, "_generate_text", forbidden)
    stages = []
    report = literature.build_literature_report(
        _profiles(), stage_callback=stages.append, reasoner=reasoner, request=LiteratureMapRequest(tmp_path, provider=reasoner.name, model=reasoner.model),
        shared_literature_plan={"literature_families": [{
            "family_id": "family", "label": "Family", "source_ids": ["A", "B"],
            "organizing_problem": "What do the studies contribute?",
            "proposed_roles": {"A": "core", "B": "core"}, "candidate_cluster": True,
        }], "discovery_jobs": [], "neighboring_families": []} if shared_plan else None,
        source_notes=[{"source_id": sid, "note_id": f"note-{sid}", "title": sid,
                       "body": f"Complete substantive atomic note {sid}", "source_scope": "full_document"}
                      for sid in ("A", "B")],
    )
    assert not {"evidence_anchors", "proposition_mapping", "evidence_matrices"} & set(stages)
    assert any(label == "cluster synthesis" for label, _ in captured)
    assert len(report["cluster_registry"]["clusters"]) == 1
    assert all("evidence_anchors" not in row and "claims" not in row for row in report["profiles"])
    assert "evidence_matrices" not in report
    assert report["gap_registry"]["status"] == "not_evaluated"
    assessment = report["debate_registry"]["assessments"][0]
    assert assessment["classification"] == "complementary_positions"
    assert assessment["explanation"] == "Distinct contributions illuminate the field together."
    assert "effective_evidence_base_count" not in assessment
    assert len(next(iter(report["cluster_source_contributions"].values()))) == 2
    # Existing historical artifacts must survive; current manifests must not advertise them.
    root = tmp_path / "03_literature_synthesis"
    root.mkdir(parents=True)
    historical = root / "evidence_matrices.yml"
    historical.write_text("historical: preserved\n")
    arguments = dict(source_set={"source_set_id": "test", "source_ids": ["A", "B"]}, run_id="test", question=None)
    _, paths = literature.persist_literature_report(tmp_path, report, **arguments)
    assert historical.read_text() == "historical: preserved\n"
    assert historical not in paths
    assert "evidence_matrices" not in read_yaml(root / "manifest.yml")["artifacts"]
    assert not (root / "propositions.yml").exists()
    assert "not evaluated" in (root / "gaps" / "INDEX.md").read_text()
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    literature.persist_literature_report(tmp_path, report, **arguments)
    assert before == {path: path.read_bytes() for path in before}


def test_unassessed_writer_never_invents_debate_or_consensus():
    cluster = {"cluster_id": "one", "source_ids": ["A", "B"]}
    result = literature._note_based_debate_registry([cluster], {"one": {
        "status": "reasoned", "quality_status": "complete", "contrast": "Different methods",
        "bottom_line": "Complementary insights", "retained_member_ids": ["A", "B"]}})
    assert result["assessments"][0]["classification"] == "unassessed"
    assert result["debate_count"] == 0
    partial = literature._note_based_debate_registry([cluster], {"one": {
        "status": "partial", "debate_state": "mapped_consensus", "bottom_line": "Agreement"}})
    assert partial["assessments"][0]["automation_status"] == "pending"


def test_pending_refresh_projects_last_accepted_debate():
    accepted = {"status": "reasoned", "quality_status": "complete",
                "debate_state": "complementary_positions", "bottom_line": "Accepted synthesis explanation",
                "retained_member_ids": ["A", "B"], "refresh_pending": True}
    registry = literature._note_based_debate_registry(
        [{"cluster_id": "one", "source_ids": ["A", "B"]}], {"one": accepted})
    row = registry["assessments"][0]
    assert row["classification"] == "complementary_positions"
    assert row["explanation"] == "Accepted synthesis explanation"
    assert row["refresh_pending"] is True


@pytest.mark.parametrize("auto_promote", [True, False])
def test_debate_promotion_policy_preserves_assessment(auto_promote):
    result = literature._note_based_debate_registry(
        [{"cluster_id": "one", "source_ids": ["A", "B"]}],
        {"one": {"status": "reasoned", "quality_status": "complete",
                 "debate_state": "mapped_debate", "bottom_line": "The works disagree on the interpretation.",
                 "retained_member_ids": ["A", "B"]}},
        policy={"auto_promote_debates": auto_promote},
    )
    row = result["assessments"][0]
    assert row["classification"] == "mapped_debate"
    assert row["promoted"] is auto_promote
    assert row["automation_status"] == ("promoted" if auto_promote else "mapped")
    assert result["debate_count"] == int(auto_promote)
    assert result["debates"] == ([row] if auto_promote else [])
