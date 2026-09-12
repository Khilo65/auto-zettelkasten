"""Frozen source custody and real persistence/replay checks without providers."""
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import v030_linking_comparison as comparison
from auto_zettelkasten.indexes import _catalogue_entry, lean_discovery_projection
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.notes import SECTION_HEADINGS, internal_note_text, read_note, semantic_note_hash, write_atomic_note
from auto_zettelkasten.profiles import profile_to_dict
from v030_codex_pdf_eval import _gate_snapshot
from v030_linking_experiment_planner import ExperimentReasonerCalls, experiment_request


def cohort_at(root: Path):
    records = []
    for index in range(2):
        source_id, note_id = f"source-{index}", f"note-{index}"
        title = f"Distinct work {index}"
        metadata = {"note_id": note_id, "source_id": source_id, "title": title,
                    "note_status": "analytical_atomic_note", "source_scope": "full_document",
                    "source_file": "custody/source.pdf", "zotero_item_key": f"ITEM000{index}",
                    "source_coverage": {"gate": "passed"}, "inspected_content_hash": "a" * 64,
                    "content_route": "pypdf_text", "reader_provider": "test", "reader_model": "test",
                    "source_pdf_uri": f"zotero://open-pdf/library/items/PDFTEST{index}",
                    "original_zotero_tags": [], "normalized_tags": [], "related_notes": []}
        analysis = {key: f"Specific source discussion of {heading} (Example, 2025, p. 3)." for key, heading in SECTION_HEADINGS}
        note, valid = write_atomic_note(root, metadata, analysis)
        assert valid.passed
        profile = profile_to_dict(EvidenceProfile(
            source_id=source_id, note_id=note_id, profile_schema_version="1.4",
            context={"title": title, "thesis": analysis["thesis"], "method_or_knowledge_basis": "Comparison",
                     "source_scope": "full_document", "evidence_eligibility": "substantive_bounded"}))
        entry = _catalogue_entry(profile, read_note(note)["frontmatter"])
        description = lean_discovery_projection([profile], {"sources": [entry]})[0]
        sidecar = root / "11_state/note_metadata" / f"{note_id}.yml"
        record = {"source_id": source_id, "note_id": note_id,
                  "inputs": {key: {"path": str(path), "sha256": comparison.digest(path.read_bytes())}
                             for key, path in (("note", note), ("note_metadata", sidecar))},
                  "description": description, "derived_profile": profile,
                  "internal_note_sha256": comparison.digest(internal_note_text(note)),
                  "semantic_note_sha256": semantic_note_hash(internal_note_text(note)),
                  "migration": {"description_changed_fields": []}}
        for field in ("description", "derived_profile"):
            record[field + "_sha256"] = comparison.digest(comparison.canonical(record[field]))
        records.append(record)
    return {"records": records, "descriptions": [r["description"] for r in records]}


class OfflineReader:
    approach = "direct"
    name = "codex"
    model = "gpt-5.6-terra"
    max_records = 2
    reasoning_effort = "max"
    context_window_tokens = 872000

    def direct_request(self, rows, **kwargs):
        return "instructions", comparison.canonical({"rows": rows, **kwargs})

    def request_fits(self, *args):
        return True


def test_mapping_preserves_originals_pdf_access_reciprocal_links_and_exact_replay(tmp_path):
    cohort = cohort_at(tmp_path / "original")
    originals = _gate_snapshot(tmp_path / "original")
    workspace = tmp_path / "mapping"
    prepared = comparison.prepare_workspace(workspace, cohort)
    cohort["common_descriptions"] = prepared["descriptions"]
    request = experiment_request(workspace, model="gpt-5.6-terra", run_id="replay-test", source_set_id="frozen-two")
    response = {"candidates": [{"left_source_id": "source-0", "right_source_id": "source-1",
                               "left_source_title": "Distinct work 0", "right_source_title": "Distinct work 1",
                               "decision": "relationship", "relation_type": "contextual_connection",
                               "actor_source_id": None, "reference_source_id": None,
                               "reason": "The works connect institutional explanations across scales."}]}
    reader = OfflineReader()
    dispatches = []

    def frozen_call(*args, **kwargs):
        dispatches.append(args)
        return response

    reader.select_direct_candidates = frozen_call
    calls = ExperimentReasonerCalls(workspace, request.run_id, reader, request,
                                     experiment_identity={"version": "test"}, input_char_budget=750000 * 3)
    first = comparison.execute_mapping(workspace, cohort, prepared, reader=reader,
                                       calls=calls, request=request, evidence=tmp_path / "evidence")
    assert first["status"] == "completed_paging"
    assert len(first["accepted"]) == 1
    protected = _gate_snapshot(workspace)
    for index, row in enumerate(prepared["note_rows"]):
        note_path = workspace / row["note_path"]
        assert f"[Open PDF in Zotero](zotero://open-pdf/library/items/PDFTEST{index})" in note_path.read_text()
        related = read_note(note_path)["frontmatter"]["related_notes"]
        assert len(related) == 1 and related[0]["reason"]
        assert related[0]["note_id"] == f"note-{1-index}"
    def forbidden(*args, **kwargs):
        raise AssertionError("provider calls forbidden during replay")

    reader.select_direct_candidates = forbidden
    replay_calls = ExperimentReasonerCalls(workspace, request.run_id, reader, request,
                                            experiment_identity={"version": "test"}, input_char_budget=750000 * 3)
    second = comparison.execute_mapping(workspace, cohort, prepared, reader=reader,
                                        calls=replay_calls, request=request, evidence=tmp_path / "evidence")
    assert replay_calls.provider_calls == 0 and len(dispatches) == 1
    assert second == first
    assert _gate_snapshot(workspace) == protected
    assert _gate_snapshot(tmp_path / "original") == originals
    comparison.verify_cohort(cohort)


def test_cohort_tampering_rejected_before_workspace_creation(tmp_path):
    cohort = cohort_at(tmp_path / "original")
    cohort["records"][0]["derived_profile"]["note_id"] = "wrong-owner"
    with pytest.raises(ValueError, match="hash mismatch"):
        comparison.prepare_workspace(tmp_path / "mapping", cohort)
    assert not (tmp_path / "mapping").exists()


@pytest.mark.parametrize("call_limit, status, count_expected", [
    (24, "failed_call", 2), (1, "incomplete_budget", 1),
])
def test_incomplete_refresh_retains_completed_direct_links(tmp_path, call_limit, status, count_expected):
    cohort = cohort_at(tmp_path / "original")
    workspace = tmp_path / "mapping"
    prepared = comparison.prepare_workspace(workspace, cohort)
    cohort["common_descriptions"] = prepared["descriptions"]
    reader = OfflineReader()
    reader.max_records = 1
    count = 0

    def interrupted_call(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise TimeoutError("synthetic interruption")
        return {"candidates": [{"left_source_id": "source-0", "right_source_id": "source-1",
                               "left_source_title": "Distinct work 0", "right_source_title": "Distinct work 1",
                                "decision": "relationship", "relation_type": "contextual_connection",
                                "actor_source_id": None, "reference_source_id": None,
                                "reason": "A grounded conceptual bridge across the two works."}]}

    result = comparison.execute_mapping(workspace, cohort, prepared, reader=reader,
                                        calls=interrupted_call, request=SimpleNamespace(source_set_id="frozen-two"),
                                        evidence=tmp_path / "evidence", max_calls=call_limit)
    assert result["status"] == status and count == count_expected
    assert len(result["accepted"]) == 1
    for row in prepared["note_rows"]:
        assert len(read_note(workspace / row["note_path"])["frontmatter"]["related_notes"]) == 1


@pytest.mark.parametrize("changed, message", [
    ({"source_attempt_limit": 1}, "allowance"),
    ({"relationship_attempt_limit": 25}, "allowance"),
    ({"relationship_attempt_limit": 1}, "allowance"),
    ({"relationship_attempt_limit": 1, "single_call_diagnostic": True,
      "approach": "planner", "model": "gpt-5.6-luna"}, "allowance"),
    ({"relationship_attempt_limit": 1, "single_call_diagnostic": True,
      "approach": "direct", "model": "gpt-5.6-terra"}, "allowance"),
    ({"reasoning_effort": "medium"}, "reasoning"),
    ({"deadline_seconds": 15000}, "deadline"),
])
def test_manifest_limits_rejected_before_campaign_start(tmp_path, monkeypatch, changed, message):
    import v030_codex_pdf_eval as base
    from v030_codex_campaign_guard import CodexCampaignGuard

    repository = Path(comparison.__file__).resolve().parents[1]
    monkeypatch.chdir(repository)
    monkeypatch.setattr(base, "_repository_state", lambda: ("frozen-commit", False))

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid manifest must not start campaign")

    monkeypatch.setattr(CodexCampaignGuard, "start", forbidden)
    manifest = {"code_commit": "frozen-commit", "source_attempt_limit": 0,
                "relationship_attempt_limit": 24, "reasoning_effort": "max", "deadline_seconds": 14400,
                **changed}
    for field in ("cohort", "capacity", "prepared", "helper", "offline_acceptance"):
        path = tmp_path / f"{field}.json"
        comparison.save(path, {"status": "passed", "code_commit": "frozen-commit",
                               "helper_sha256": manifest.get("helper_sha256")})
        manifest[field] = str(path)
        manifest[field + "_sha256"] = comparison.digest(path.read_bytes())
    path = tmp_path / "manifest.json"
    comparison.save(path, manifest)
    with pytest.raises(ValueError, match=message):
        comparison.run_campaign(path, tmp_path / "unused-authorization.json")


def test_accounting_distinguishes_reservations_reasoning_and_missing_usage(tmp_path):
    from auto_zettelkasten.files import write_yaml

    usage_path, ledger_path = tmp_path / "usage.yml", tmp_path / "ledger.jsonl"
    attempts = [
        {"stage": "literature_family_plan", "status": "completed", "provider_completion": {
            "usage": {"input_tokens": 1000, "output_tokens": 200, "reasoning_output_tokens": 80}}},
        {"stage": "relationship_candidate_selection", "status": "completed", "provider_completion": {
            "usage": {"input_tokens": 2500, "output_tokens": 400, "reasoning_output_tokens": 150}}},
        {"stage": "relationship_candidate_selection", "status": "failed"},
        {"stage": "relationship_candidate_selection", "status": "failed", "provider_completion": {
            "usage": {"input_tokens": 100, "output_tokens": 30}}},
    ]
    write_yaml(usage_path, {"attempts": attempts})
    reservations = [{"record": "reserved", "role": "relationship", "job_attempt_number": 1} for _ in range(3)]
    ledger_path.write_text("\n".join(comparison.canonical(row) for row in [{"record": "header"}, *reservations]))
    calls = SimpleNamespace(usage_path=usage_path, provider_calls=4)
    guard = SimpleNamespace(ledger_path=ledger_path)
    measured = comparison.accounting(calls, guard)
    assert measured["graph_calls"] == 3 and measured["logical_attempts"] == 4
    assert measured["source_calls"] == 0
    planning, linking = measured["stages"]["planning"], measured["stages"]["linking"]
    assert planning["input_tokens"] == 1000
    assert planning["output_tokens_including_reasoning"] == 200
    assert planning["visible_output_tokens"] == 120 and planning["reasoning_tokens"] == 80
    assert linking["input_tokens"] == 2500
    assert linking["output_tokens_including_reasoning"] == 400
    assert linking["visible_output_tokens"] == 250 and linking["reasoning_tokens"] == 150
    assert linking["logical_attempts"] == 3 and linking["completed"] == 1 and linking["failed"] == 2
    assert linking["attempts_with_unavailable_usage"] == 2
    for violation in ({"role": "source"}, {"job_attempt_number": 2}):
        ledger_path.write_text(comparison.canonical({**reservations[0], **violation}))
        with pytest.raises(ValueError, match="source call or automatic retry"):
            comparison.accounting(calls, guard)


@pytest.mark.parametrize("verified_helper", [False, True])
def test_single_call_campaign_preserves_saturated_result_and_replays(tmp_path, monkeypatch, verified_helper):
    import v030_codex_pdf_eval as base
    import v030_linking_experiment_reader as transport
    from v030_codex_campaign_guard import CodexCampaignGuard

    repository = Path(comparison.__file__).resolve().parents[1]
    monkeypatch.chdir(repository)
    monkeypatch.setattr(base, "_repository_state", lambda: ("frozen-commit", False))
    monkeypatch.setenv("AUTO_ZETTELKASTEN_CODEX", "offline-test")
    cohort = cohort_at(tmp_path / "original")
    originals = _gate_snapshot(tmp_path / "original")
    arm, workspace = tmp_path / "diagnostic", tmp_path / "diagnostic/workspace"
    prepared = comparison.prepare_workspace(workspace, cohort)
    cohort["common_descriptions"] = prepared["descriptions"]
    ledger_path = arm / "ledger.jsonl"
    ledger_path.write_text("")
    started, finished, dispatches = [], [], []
    guard = SimpleNamespace(ledger_path=ledger_path, activate=nullcontext,
                            finish=lambda status, **kwargs: finished.append(status))

    def start(*args, **kwargs):
        assert kwargs["source_attempt_limit"] == 0
        assert kwargs["relationship_attempt_limit"] == kwargs["total_attempt_limit"] == 1
        started.append(kwargs)
        return guard

    def make_reader(model, *, max_records, **kwargs):
        reader = OfflineReader()
        reader.model, reader.max_records = model, max_records

        def provider(*args, **kwargs):
            dispatches.append(args)
            assert len(dispatches) == 1, "diagnostic or replay launched a second call"
            assert args[1].literature_policy.max_synthesis_calls == 1
            ledger_path.write_text(comparison.canonical(
                {"record": "reserved", "role": "relationship", "job_attempt_number": 1}) + "\n")
            return {"candidates": [{"left_source_id": "source-0", "right_source_id": "source-1",
                               "left_source_title": "Distinct work 0", "right_source_title": "Distinct work 1",
                                    "decision": "relationship", "relation_type": "contextual_connection",
                                    "actor_source_id": None, "reference_source_id": None,
                                    "reason": "A grounded comparison between institutional explanations."}]}

        reader.select_direct_candidates = provider
        return reader

    monkeypatch.setattr(CodexCampaignGuard, "start", start)
    monkeypatch.setattr(transport, "ExperimentCodexReader", make_reader)
    manifest = {"code_commit": "frozen-commit", "source_attempt_limit": 0,
                "relationship_attempt_limit": 1, "single_call_diagnostic": True,
                "approach": "direct", "model": "gpt-5.6-luna", "reasoning_effort": "max",
                "deadline_seconds": 14400, "workspace": str(workspace), "capability": {},
                "run_id": "diagnostic-one", "evaluation_id": "diagnostic-one",
                "source_set_id": "frozen-two", "stage": "linking_comparison_212",
                "experiment_identity": {"version": "single-call-test"},
                "prepared_inventory": _gate_snapshot(workspace)}
    for field, data in {"cohort": cohort, "prepared": prepared, "capacity": {"max_records": 1},
                        "helper": {"offline": True}, "offline_acceptance": {
                            "status": "passed", "code_commit": "frozen-commit"}}.items():
        path = arm / f"{field}.json"
        if field == "offline_acceptance":
            data["helper_sha256"] = manifest["helper_sha256"] if verified_helper else "0" * 64
        comparison.save(path, data)
        manifest[field], manifest[field + "_sha256"] = str(path), comparison.digest(path.read_bytes())
    manifest_path, authorization = arm / "MANIFEST.json", arm / "AUTHORIZATION.json"
    comparison.save(manifest_path, manifest)
    comparison.save(authorization, {"offline": True})
    if not verified_helper:
        with pytest.raises(ValueError, match="offline helper verification"):
            comparison.run_campaign(manifest_path, authorization)
        assert started == dispatches == []
        return
    receipt = comparison.run_campaign(manifest_path, authorization)
    assert receipt["status"] == "diagnostic_completed_review_pending"
    assert receipt["graph_calls"] == receipt["logical_attempts"] == 1
    result_bytes = (arm / "RESULT.json").read_bytes()
    result = json.loads(result_bytes)
    assert result["status"] == "incomplete_budget" and result["calls"] == 1
    assert result["exhaustive_discovery"] is False
    assert len(result["accepted"]) == len(result["completed_requests"]) == 1
    assert len(list((arm / "pages").glob("*.json"))) == 2  # raw page and validated result
    protected, receipt_bytes = _gate_snapshot(workspace), (arm / "RUN_RECEIPT.json").read_bytes()
    replay = comparison.run_campaign(manifest_path, authorization, replay=True)
    assert replay["status"] == "passed" and replay["provider_calls"] == 0
    assert len(dispatches) == len(started) == 1 and finished == ["passed"]
    assert _gate_snapshot(workspace) == protected
    assert (arm / "RESULT.json").read_bytes() == result_bytes
    assert (arm / "RUN_RECEIPT.json").read_bytes() == receipt_bytes
    assert _gate_snapshot(tmp_path / "original") == originals
    with pytest.raises(ValueError, match="one campaign per arm"):
        comparison.run_campaign(manifest_path, authorization)


def test_planner_mapping_replay_preserves_first_plan_and_protected_outputs(tmp_path):
    from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
    from auto_zettelkasten.models import LiteratureMappingPolicy

    cohort = cohort_at(tmp_path / "original")
    originals = _gate_snapshot(tmp_path / "original")
    workspace, evidence = tmp_path / "mapping", tmp_path / "evidence"
    prepared = comparison.prepare_workspace(workspace, cohort)
    cohort["common_descriptions"] = prepared["descriptions"]
    for profile in prepared["profiles"]:
        profile["context"]["note_status"] = "analytical_atomic_note"
    request = experiment_request(workspace, model="gpt-5.6-terra", run_id="planner-replay", source_set_id="frozen-two",
                                 literature_policy=LiteratureMappingPolicy(cluster_generation_enabled=False))
    dispatches = []

    def plan(*args, **kwargs):
        dispatches.append("plan")
        return {"literature_families": [{"family_id": "one", "label": "Comparison", "organizing_problem": "A shared problem",
                    "source_ids": ["source-0", "source-1"], "proposed_roles": {"source-0": "core", "source-1": "core"},
                    "candidate_cluster": True}],
                "discovery_jobs": [{"job_id": "ab", "family": "one", "left_source_ids": ["source-0"],
                                    "right_source_ids": ["source-1"], "candidate_quota": 1}], "neighboring_families": []}

    def select(*args, **kwargs):
        dispatches.append("link")
        return {"candidates": [{"left_source_id": "source-0", "right_source_id": "source-1",
                    "left_source_title": "Distinct work 0", "right_source_title": "Distinct work 1",
                    "decision": "relationship", "relation_type": "contextual_connection",
                    "actor_source_id": None, "reference_source_id": None,
                    "reason": "The works connect institutional explanations across scales."}],
                "job_outcomes": [{"bridge_job_id": job["bridge_job_id"], "status": "completed"}
                                 for job in kwargs["context"]["bridge_jobs"]]}

    reader = SimpleNamespace(approach="planner", name="codex", model=request.model, reasoning_effort="max",
        max_records=163, context_window_tokens=872000, prompt_reserve_tokens=0, capabilities={},
        ordinary_relationship_decision_contract="relationship-decision-v11", plan_literature_families=plan,
        select_relationship_candidates=select, literature_family_plan_fits=lambda *args, **kwargs: True)

    def calls():
        return ExperimentReasonerCalls(workspace, request.run_id, reader, request,
                                       experiment_identity={"version": "planner-replay"}, input_char_budget=2250000)

    with deny_codex_attempts():
        first = comparison.execute_mapping(workspace, cohort, prepared, reader=reader, calls=calls(),
                                           request=request, evidence=evidence)
    assert len(first["accepted"]) == 1 and dispatches == ["plan", "link"]
    protected, evidence_before = _gate_snapshot(workspace), _gate_snapshot(evidence)
    original_plan = (evidence / "FAMILY_PLAN.json").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("provider calls forbidden during planner replay")

    reader.plan_literature_families = reader.select_relationship_candidates = forbidden
    replay_calls = calls()
    with deny_codex_attempts():
        second = comparison.execute_mapping(workspace, cohort, prepared, reader=reader, calls=replay_calls,
                                            request=request, evidence=evidence, replay=True)
    assert second["relationship_stage_complete"]
    assert replay_calls.provider_calls == 0 and dispatches == ["plan", "link"]
    assert (evidence / "FAMILY_PLAN.json").read_bytes() == original_plan
    assert _gate_snapshot(workspace) == protected and _gate_snapshot(evidence) == evidence_before
    assert _gate_snapshot(tmp_path / "original") == originals
