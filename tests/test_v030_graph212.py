from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.notes import semantic_note_hash
from test_v030_codex_e2e_eval import runner
from test_v030_release_quality_audit import _completed_review, _workspace, audit_tool


def _graph212(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    rows = []
    baseline = set()
    for index in range(212):
        source_id, note_id = f"source-{index}", f"note-{index}"
        note = workspace / "02_source_memory/notes" / f"{note_id}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"---\nsource_id: {source_id}\nnote_id: {note_id}\n---\nFindings {index}.\n")
        profile = workspace / "02_source_memory/profiles" / f"{note_id}.yml"
        write_yaml(profile, {"profile": {"source_id": source_id, "note_id": note_id}})
        metadata = workspace / "11_state/note_metadata" / f"{note_id}.yml"
        write_yaml(metadata, {"source_id": source_id, "note_id": note_id})
        baseline.update(str(path.relative_to(workspace)) for path in (note, profile, metadata))
        rows.append({
            "source_id": source_id, "note_id": note_id, "phase": "baseline",
            "primary_stratum_id": f"stratum-{index % 20}",
            "deepest_leaf_packet_key": f"packet-{index % 23}",
            "note_path": str(note.relative_to(workspace)),
            "profile_path": str(profile.relative_to(workspace)),
            "semantic_note_sha256": semantic_note_hash(note.read_text()),
            "origin_note_sha256": sha256_file(note), "profile_sha256": sha256_file(profile),
            "bundle_path": "", "bundle_sha256": "",
        })
    for relative in (
        "auto-zettelkasten.yml", "11_state/workspace_manifest.yml",
        "01_custody/zotero/collection_snapshot.yml",
        "02_source_memory/indexes/literature_positions.yml",
        "02_source_memory/indexes/missing_sources.yml",
    ):
        write_yaml(workspace / relative, {})
        baseline.add(relative)
    write_yaml(workspace / "11_state/workspace_manifest.yml", {
        "engine_version": "0.30.0", "artifact_schema_version": "1.20",
        "created_at": "2026-01-01", "workspace": str(workspace),
    })
    monkeypatch.setattr(runner, "_GRAPH500_CONFIG_SHA256", sha256_file(workspace / "auto-zettelkasten.yml"))
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps({
        "source_count": 212, "selected_packet_keys": sorted({row["deepest_leaf_packet_key"] for row in rows}),
        "sources": [{**row, "provenance_bindings": [{"files": {"note": {"sha256": row["origin_note_sha256"]}}}]} for row in rows],
    }))
    monkeypatch.setattr(runner, "_GRAPH212_SELECTION_SHA256", sha256_file(approved))
    selection = {
        "schema_version": "1", "status": "frozen_provider_neutral_slice",
        "never_production_prompt_input": True, "source_count": 212, "sources": rows,
        "sampling": {"selected_packet_count": 23, "selected_packet_keys": sorted({row["deepest_leaf_packet_key"] for row in rows})},
    }
    identity = runner.base.sha256_text(json.dumps(selection, sort_keys=True, ensure_ascii=False))
    selection_path = workspace / "11_state/harness_bakeoff_manifest.yml"
    write_yaml(selection_path, {**selection, "manifest_sha256": identity})
    baseline.add(str(selection_path.relative_to(workspace)))
    settings = runner.base.GateSettings(
        kind="graph_e2e", stage="graph212", case_count=212,
        source_attempt_limit=0, relationship_attempt_limit=233, total_attempt_limit=233,
        document_attempt_limit=1, stage_deadline_seconds=14400, clusters_enabled=True,
    )
    manifest = {
        "workspace": str(workspace), "approved_selection": str(approved),
        "selection_manifest": "11_state/harness_bakeoff_manifest.yml",
        "selection_manifest_sha256": sha256_file(approved),
        "selection_manifest_file_sha256": sha256_file(selection_path),
        "materialized_selection_sha256": identity,
        "baseline_file_count": len(baseline),
        "baseline_sha256": runner._inventory_sha256(workspace, baseline),
        "gate": settings.manifest_binding(),
    }
    path = workspace / "PRIVATE_GRAPH_MANIFEST.json"
    path.write_text(json.dumps(manifest))
    return workspace, path, manifest, settings


def test_graph212_bound_inputs_are_provider_free_and_reject_graph_state(tmp_path, monkeypatch):
    workspace, path, manifest, _ = _graph212(tmp_path, monkeypatch)
    with runner.base.deny_codex_attempts():
        validated, settings = runner._manifest_settings(path, sha256_file(path))
        _, rows, source_set = runner._graph_inputs(validated, settings, require_frozen_notes=True, manifest_path=path)
        assert len(rows) == len(source_set["source_ids"]) == 212
        again = runner._graph_inputs(validated, settings, require_frozen_notes=True, manifest_path=path)
        assert again[2] == source_set
    write_yaml(workspace / "03_literature_synthesis/cluster_registry.yml", {})
    with pytest.raises(ValueError, match="unexpected baseline state"):
        runner._graph_inputs(manifest, settings, require_frozen_notes=True, manifest_path=path)


@pytest.mark.parametrize("field,value", [
    ("source_attempt_limit", 1), ("relationship_attempt_limit", 234),
    ("total_attempt_limit", 234), ("case_count", 250), ("stage_deadline_seconds", 14401),
    ("stage", "graph500"), ("cluster_generation_enabled", False),
])
def test_graph212_does_not_relax_campaign_controls(tmp_path, monkeypatch, field, value):
    _, path, manifest, _ = _graph212(tmp_path, monkeypatch)
    manifest["gate"][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        runner._manifest_settings(path, sha256_file(path))


def test_graph212_rejects_changed_note_profile_and_approved_identity(tmp_path, monkeypatch):
    workspace, path, manifest, settings = _graph212(tmp_path, monkeypatch)
    approved = Path(manifest["approved_selection"])
    original = approved.read_bytes()
    approved.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="approved external selection"):
        runner._graph_inputs(manifest, settings, require_frozen_notes=True, manifest_path=path)
    approved.write_bytes(original)
    profile = workspace / "02_source_memory/profiles/note-0.yml"
    original = profile.read_bytes()
    profile.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="profile SHA-256"):
        runner._graph_inputs(manifest, settings, require_frozen_notes=False, manifest_path=path)
    profile.write_bytes(original)
    (workspace / "02_source_memory/notes/note-0.md").write_text("Different source content")
    with pytest.raises(ValueError, match="semantic note SHA-256"):
        runner._graph_inputs(manifest, settings, require_frozen_notes=False, manifest_path=path)


def test_omission_sample_small_then212_is_balanced_stable_and_separate():
    artifact = {"path": "typed.yml", "path_scope": "workspace", "sha256": "a" * 64}
    small = {f"source-{i}": "one" for i in range(4)}
    accepted = [{"left_source_id": "source-0", "right_source_id": "source-1", "decision_status": "accepted"}]
    supplement = audit_tool._omission_supplement(small, accepted, artifact)
    assert supplement["population_count"] == len(supplement["rows"]) == 5
    strata = {f"source-{i:03d}": "large-stratum" if i < 63 else f"stratum-{i % 19}" for i in range(212)}
    large = audit_tool._omission_supplement(strata, accepted, artifact)
    assert large == audit_tool._omission_supplement(dict(reversed(list(strata.items()))), accepted, artifact)
    assert len(large["rows"]) == 100
    within = [row for row in large["rows"] if len({strata[source] for source in row["payload"]["source_ids"]}) == 1]
    assert len(within) == 50
    assert any(all(strata[source] == "large-stratum" for source in row["payload"]["source_ids"]) for row in within)


def test_stratified212_omissions_do_not_change_link_scores_but_material_failures_block(tmp_path):
    workspace = _workspace(tmp_path, source_count=212, accepted_count=40, negative_count=20, cluster_count=4)
    packet_path = tmp_path / "packet.yml"
    packet = audit_tool.prepare(workspace, "stratified212", packet_path)
    assert packet["source_count"] == 212
    assert "omitted_pair" not in {row["kind"] for row in packet["rows"]}
    assert len(packet["omission_review"]["rows"]) == 100
    assert packet["sampling_counts"]["relationships"]["mandatory"] == 40
    all_rows = [*packet["rows"], *packet["omission_review"]["rows"]]
    assert len(audit_tool._verify_packet(workspace, packet)) == len(all_rows)
    review = _completed_review(packet_path, {**packet, "rows": all_rows})
    omission_ids = {row["review_id"] for row in packet["omission_review"]["rows"]}
    for judgment in review["judgments"]:
        if judgment["review_id"] in omission_ids:
            judgment.update(material_error=False, clear_useful_connection_missed=True, systematic_failure=False)
            judgment.pop("judgment_sha256")
            judgment["judgment_sha256"] = audit_tool._digest(judgment)
    review_path, score_path = tmp_path / "review.yml", tmp_path / "score.yml"
    write_yaml(review_path, review)
    first = audit_tool.score(workspace, packet_path, review_path, score_path)
    assert first["status"] == "passed"
    assert first["metrics"]["relationship_correctness"]["reviewed"] == 40
    assert first["metrics"]["overall_pass_rate"] == 1
    judgment = next(row for row in review["judgments"] if row["review_id"] in omission_ids)
    judgment["systematic_failure"] = True
    judgment.pop("judgment_sha256")
    judgment["judgment_sha256"] = audit_tool._digest(judgment)
    write_yaml(review_path, review)
    second = audit_tool.score(workspace, packet_path, review_path, tmp_path / "failed.yml")
    assert second["status"] == "failed"
    assert second["metrics"]["relationship_correctness"] == first["metrics"]["relationship_correctness"]
    assert not second["checks"]["no_material_or_systematic_omission_failure"]
    stale = deepcopy(packet)
    stale["omission_review"]["rows"].pop()
    stale.pop("packet_identity")
    stale["packet_identity"] = audit_tool._digest(stale)
    with pytest.raises(ValueError, match="omission review supplement"):
        audit_tool._verify_packet(workspace, stale)


def test_graph212_materialization_cannot_substitute_new_sources(tmp_path, monkeypatch):
    workspace, path, manifest, settings = _graph212(tmp_path, monkeypatch)
    selection_path = workspace / "11_state/harness_bakeoff_manifest.yml"
    selection = read_yaml(selection_path)
    selection["sources"][0]["primary_stratum_id"] = "different"
    selection.pop("manifest_sha256")
    identity = runner.base.sha256_text(json.dumps(selection, sort_keys=True, ensure_ascii=False))
    write_yaml(selection_path, {**selection, "manifest_sha256": identity})
    manifest["materialized_selection_sha256"] = identity
    manifest["selection_manifest_file_sha256"] = sha256_file(selection_path)
    with pytest.raises(ValueError, match="differ from approved"):
        runner._graph_inputs(manifest, settings, require_frozen_notes=False, manifest_path=path)
