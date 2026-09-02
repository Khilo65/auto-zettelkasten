from __future__ import annotations

import importlib.util
from pathlib import Path

from auto_zettelkasten.files import read_yaml, sha256_file, write_json, write_yaml
from auto_zettelkasten.models import RelationshipPairJob


SPEC = importlib.util.spec_from_file_location(
    "v030_relationship_packet_audit",
    Path(__file__).parents[1] / "tools/v030_relationship_packet_audit.py",
)
assert SPEC and SPEC.loader
audit_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_tool)


def _job(left: str, right: str) -> RelationshipPairJob:
    return RelationshipPairJob(
        left_source_id=left,
        right_source_id=right,
        profiles={
            "left": {"source_id": left, "thesis": f"Profile {left}"},
            "right": {"source_id": right, "thesis": f"Profile {right}"},
        },
        atomic_notes={
            "left": {"source_id": left, "markdown": f"Complete note {left}"},
            "right": {"source_id": right, "markdown": f"Complete note {right}"},
        },
        selected_evidence={
            "left": [{"source_id": left, "claim": f"Claim {left}"}],
            "right": [{"source_id": right, "claim": f"Claim {right}"}],
        },
        graph_context={"pair_context": {"left": left, "right": right}},
        candidate_basis=[{"why_compare": f"{left} with {right}"}],
    )


def test_private_packet_audit_is_zero_provider_and_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    run_id = "synthetic-run"
    jobs = [_job("source-a", "source-b"), _job("source-a", "source-c")]
    run_root = workspace / "11_state" / "runs" / run_id
    for job in jobs:
        job_root = run_root / "relationship_jobs" / job.pair_job_id
        write_json(job_root / "input.json", job.to_dict())
        write_yaml(job_root / "status.yml", {"status": "completed"})
    batch_id = "relationship-batch-synthetic"
    write_yaml(
        run_root / "relationship_batches" / batch_id / "batch.yml",
        {
            "batch_id": batch_id,
            "pair_job_ids": [job.pair_job_id for job in jobs],
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "status": "completed",
        },
    )
    for source_id in ("source-a", "source-b", "source-c"):
        write_yaml(
            workspace / "02_source_memory" / "profiles" / f"{source_id}.yml",
            {"source_id": source_id, "thesis": f"Full profile {source_id}"},
        )
    before = {
        path.relative_to(workspace): sha256_file(path)
        for path in workspace.rglob("*")
        if path.is_file()
    }

    report_path = tmp_path / "private-report" / "packet-audit.yml"
    report = audit_tool.audit(workspace, run_id, report_path)

    after = {
        path.relative_to(workspace): sha256_file(path)
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert report["status"] == "passed"
    assert report["provider_calls"] == 0
    assert report["pair_job_count"] == 2
    assert report["frozen_packet_count"] == 1
    assert report["packets"][0]["repeated_source_occurrences"] == 1
    assert report["packets"][0]["within_packet_source_deduplication"][
        "atomic_notes"
    ]["saved_bytes"] > 0
    assert set(report["overall_components"]) == {
        "atomic_notes",
        "candidate_basis",
        "compact_profiles",
        "evidence_anchors",
        "graph_context",
        "pair_job_metadata",
    }
    prompts = report["packets"][0]["complete_v1"]
    assert (
        prompts["system_prompt"]["estimated_tokens"]
        + prompts["user_prompt"]["estimated_tokens"]
        == prompts["estimated_input_tokens"]
    )
    assert report["natural_pack_estimate"]["complete_v1_packet_count"] == 1
    assert report["transport_changes_implemented"] is False
    assert read_yaml(report_path)["provider_calls"] == 0
