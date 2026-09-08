#!/usr/bin/env python3
"""Audit frozen relationship packet composition without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.pipeline import (
    _pack_relationship_rows,
    _relationship_context_char_budget,
    _relationship_transport_context,
)
from auto_zettelkasten.readers import (
    _estimate_tokens,
    _relationship_adjudication_system_prompt,
    _relationship_prompt,
)
from auto_zettelkasten.relationships import RELATIONSHIP_DECISION_CONTRACT


CONTEXT_WINDOW_TOKENS = 272_000
PROVIDER_USABLE_CONTEXT_TOKENS = 200_000
PROMPT_RESERVE_TOKENS = 2_048
OUTPUT_RESERVE_TOKENS = 16_384
MAX_PAIR_JOBS = 8
MATERIAL_REDUCTION_TARGET_PERCENT = 25.0
DECISION_CONTRACT = RELATIONSHIP_DECISION_CONTRACT
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _private_report_path(path: Path, workspace: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix not in {".yml", ".yaml"}:
        raise ValueError("report must be a YAML path")
    if (
        _inside(resolved, workspace)
        or _inside(resolved, _REPOSITORY_ROOT)
        or _git_root(resolved) is not None
    ):
        raise ValueError("report must be outside the audited workspace and Git")
    return resolved


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str
    )


def _bytes(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def _measure(value: Any) -> dict[str, int]:
    text = _json(value)
    return {
        "bytes": len(text.encode("utf-8")),
        "estimated_tokens": _estimate_tokens(text),
    }


def _measure_text(value: str) -> dict[str, int]:
    return {
        "bytes": len(value.encode("utf-8")),
        "estimated_tokens": _estimate_tokens(value),
    }


def _note_only_context(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if key != "source_evidence"
    }


def _source_values(
    job: RelationshipPairJob,
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for side, source_id in (
        ("left", job.left_source_id),
        ("right", job.right_source_id),
    ):
        values[source_id] = {
            "atomic_notes": dict(job.atomic_notes.get(side) or {}),
            "profiles": dict(job.profiles.get(side) or {}),
            "evidence": list(job.selected_evidence.get(side) or []),
        }
    return values


def _load_frozen_run(
    workspace: Path, run_id: str
) -> tuple[
    list[RelationshipPairJob],
    list[tuple[str, list[str]]],
    dict[str, Mapping[str, Any]],
    str,
    str,
]:
    run_root = workspace / "11_state" / "runs" / run_id
    job_root = run_root / "relationship_jobs"
    batch_root = run_root / "relationship_batches"
    if not job_root.is_dir() or not batch_root.is_dir():
        raise ValueError("frozen relationship jobs or batches are missing")

    jobs: list[RelationshipPairJob] = []
    for input_path in sorted(job_root.glob("*/input.json")):
        status = read_yaml(input_path.parent / "status.yml", {}) or {}
        if status.get("status") != "completed":
            raise ValueError("every frozen relationship job must be completed")
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("frozen relationship input is unreadable") from exc
        job = RelationshipPairJob.from_dict(payload)
        if job.pair_job_id != input_path.parent.name:
            raise ValueError("relationship job directory does not match its input")
        if job.output_contract != DECISION_CONTRACT:
            raise ValueError(f"audit requires {DECISION_CONTRACT} jobs")
        jobs.append(job)
    if not jobs:
        raise ValueError("no frozen relationship jobs found")

    batches: list[tuple[str, list[str]]] = []
    providers: set[str] = set()
    models: set[str] = set()
    for batch_path in sorted(batch_root.glob("*/batch.yml")):
        batch = read_yaml(batch_path, {}) or {}
        pair_job_ids = [str(value) for value in batch.get("pair_job_ids", [])]
        if batch.get("status") != "completed" or not pair_job_ids:
            raise ValueError("every frozen relationship batch must be completed")
        batch_id = str(batch.get("batch_id") or "")
        if batch_id != batch_path.parent.name:
            raise ValueError("relationship batch directory does not match its input")
        batches.append((batch_id, pair_job_ids))
        providers.add(str(batch.get("provider") or ""))
        models.add(str(batch.get("model") or ""))
    if len(providers) != 1 or len(models) != 1 or "" in providers | models:
        raise ValueError("frozen batches must use one explicit provider and model")

    expected_ids = {job.pair_job_id for job in jobs}
    observed_ids = [job_id for _batch_id, ids in batches for job_id in ids]
    if Counter(observed_ids) != Counter(expected_ids):
        raise ValueError("completed batches must partition frozen jobs exactly once")

    profiles: dict[str, Mapping[str, Any]] = {}
    for profile_path in sorted(
        (workspace / "02_source_memory" / "profiles").glob("*.yml")
    ):
        stored = read_yaml(profile_path, {}) or {}
        profile = (
            dict(stored.get("profile") or {})
            if isinstance(stored, Mapping)
            else {}
        )
        if not profile and isinstance(stored, Mapping):
            profile = dict(stored)
        source_id = str(profile.get("source_id") or "")
        if source_id:
            profiles[source_id] = profile
    source_ids = {
        source_id
        for job in jobs
        for source_id in (job.left_source_id, job.right_source_id)
    }
    if not source_ids <= profiles.keys():
        raise ValueError("full profiles for frozen relationship endpoints are missing")
    return jobs, batches, profiles, providers.pop(), models.pop()


def _component_payloads(context: Mapping[str, Any]) -> dict[str, Any]:
    pair_metadata = []
    candidate_basis = []
    graph_context = []
    for raw in context.get("pair_jobs", []) or []:
        row = dict(raw)
        job_id = str(row.get("pair_job_id") or "")
        candidate_basis.append(
            {"pair_job_id": job_id, "candidate_basis": row.pop("candidate_basis", [])}
        )
        graph_context.append(
            {"pair_job_id": job_id, "graph_context": row.pop("graph_context", {})}
        )
        pair_metadata.append(row)
    return {
        "atomic_notes": dict(context.get("source_documents") or {}),
        "compact_profiles": dict(context.get("source_profiles") or {}),
        "evidence_anchors": dict(context.get("source_evidence") or {}),
        "pair_job_metadata": pair_metadata,
        "candidate_basis": candidate_basis,
        "graph_context": graph_context,
    }


def _prompt_measurement(
    context: Mapping[str, Any], *, run_id: str, provider: str, model: str
) -> dict[str, Any]:
    request = SimpleNamespace(
        source_set_id=run_id,
        provider=provider,
        model=model,
    )
    system_prompt = _relationship_adjudication_system_prompt()
    user_prompt = _relationship_prompt([], request, context)
    input_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
    input_ceiling = (
        PROVIDER_USABLE_CONTEXT_TOKENS
        - PROMPT_RESERVE_TOKENS
        - OUTPUT_RESERVE_TOKENS
    )
    return {
        "system_prompt": _measure_text(system_prompt),
        "user_prompt": _measure_text(user_prompt),
        "estimated_input_tokens": input_tokens,
        "input_token_ceiling": input_ceiling,
        "fits": input_tokens <= input_ceiling,
    }


def _packet_report(
    batch_id: str,
    jobs: Sequence[RelationshipPairJob],
    *,
    run_id: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    context = _relationship_transport_context(
        jobs, decision_contract=DECISION_CONTRACT
    )
    components = _component_payloads(context)
    source_ids = sorted(
        {source_id for job in jobs for source_id in (job.left_source_id, job.right_source_id)}
    )
    expanded = {key: 0 for key in ("atomic_notes", "profiles", "evidence")}
    transported = {
        "atomic_notes": context["source_documents"],
        "profiles": context["source_profiles"],
        "evidence": context.get("source_evidence", {}),
    }
    for job in jobs:
        for source_id in (job.left_source_id, job.right_source_id):
            for key, values in transported.items():
                if source_id in values:
                    expanded[key] += _bytes(values[source_id])
    unique = {
        "atomic_notes": sum(_bytes(value) for value in context["source_documents"].values()),
        "profiles": sum(_bytes(value) for value in context["source_profiles"].values()),
        "evidence": sum(_bytes(value) for value in transported["evidence"].values()),
    }
    return {
        "batch_id": batch_id,
        "pair_job_ids": [job.pair_job_id for job in jobs],
        "pair_job_count": len(jobs),
        "source_occurrences": len(jobs) * 2,
        "unique_source_count": len(source_ids),
        "repeated_source_occurrences": len(jobs) * 2 - len(source_ids),
        "components": {key: _measure(value) for key, value in components.items()},
        "within_packet_source_deduplication": {
            key: {
                "pair_expanded_bytes": expanded[key],
                "packet_unique_bytes": unique[key],
                "saved_bytes": expanded[key] - unique[key],
            }
            for key in expanded
        },
        "complete_v1": {
            "context": _measure(context),
            **_prompt_measurement(
                context, run_id=run_id, provider=provider, model=model
            ),
        },
        "note_only_v1_estimate": {
            "context": _measure(_note_only_context(context)),
            **_prompt_measurement(
                _note_only_context(context),
                run_id=run_id,
                provider=provider,
                model=model,
            ),
        },
    }


def _natural_packets(
    jobs: Sequence[RelationshipPairJob],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    note_only: bool,
) -> list[list[RelationshipPairJob]]:
    reasoner = SimpleNamespace(
        context_window_tokens=CONTEXT_WINDOW_TOKENS,
        prompt_reserve_tokens=PROMPT_RESERVE_TOKENS,
    )

    def context_for(packet: Sequence[RelationshipPairJob]) -> dict[str, Any]:
        context = _relationship_transport_context(
            packet, decision_contract=DECISION_CONTRACT
        )
        return _note_only_context(context) if note_only else context

    return _pack_relationship_rows(
        sorted(jobs, key=lambda job: (job.left_source_id, job.right_source_id)),
        pair_for=lambda job: (job.left_source_id, job.right_source_id),
        profile_by_source=profiles,
        context_for=context_for,
        max_chars=_relationship_context_char_budget(reasoner, None),
        max_rows=MAX_PAIR_JOBS,
    )


def audit(workspace: Path, run_id: str, report_path: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("workspace does not exist")
    report_path = _private_report_path(report_path, workspace)
    jobs, batches, profiles, provider, model = _load_frozen_run(workspace, run_id)
    jobs_by_id = {job.pair_job_id: job for job in jobs}

    fingerprints: dict[tuple[str, str], str] = {}
    global_values: dict[str, dict[str, Any]] = {
        "atomic_notes": {},
        "profiles": {},
        "evidence": {},
    }
    for job in jobs:
        for source_id, values in _source_values(job).items():
            for component, value in values.items():
                digest = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
                key = (source_id, component)
                if key in fingerprints and fingerprints[key] != digest:
                    raise ValueError("repeated source payloads are inconsistent")
                fingerprints[key] = digest
                global_values[component][source_id] = value

    packets = [
        _packet_report(
            batch_id,
            [jobs_by_id[job_id] for job_id in pair_job_ids],
            run_id=run_id,
            provider=provider,
            model=model,
        )
        for batch_id, pair_job_ids in batches
    ]
    complete_packets = _natural_packets(jobs, profiles, note_only=False)
    note_only_packets = _natural_packets(jobs, profiles, note_only=True)
    frozen_memberships = {
        frozenset(pair_job_ids) for _batch_id, pair_job_ids in batches
    }
    reconstructed_memberships = {
        frozenset(job.pair_job_id for job in packet)
        for packet in complete_packets
    }
    if frozen_memberships != reconstructed_memberships:
        raise ValueError("current complete-v1 packing does not reconstruct frozen batches")

    packet_unique_totals = {
        component: sum(
            row["within_packet_source_deduplication"][component]["packet_unique_bytes"]
            for row in packets
        )
        for component in global_values
    }
    global_unique_totals = {
        component: sum(_bytes(value) for value in values.values())
        for component, values in global_values.items()
    }
    complete_tokens = sum(row["complete_v1"]["estimated_input_tokens"] for row in packets)
    note_only_tokens = sum(
        row["note_only_v1_estimate"]["estimated_input_tokens"] for row in packets
    )
    reduction_percent = round(
        (complete_tokens - note_only_tokens) * 100 / complete_tokens, 2
    )
    overall_components = {
        component: {
            "bytes": sum(row["components"][component]["bytes"] for row in packets),
            "estimated_tokens": sum(
                row["components"][component]["estimated_tokens"]
                for row in packets
            ),
        }
        for component in packets[0]["components"]
    }
    report = {
        "audit_schema_version": "1",
        "status": "passed",
        "audit": "v030_relationship_packet_composition",
        "workspace": str(workspace),
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "provider_calls": 0,
        "pair_job_count": len(jobs),
        "frozen_packet_count": len(batches),
        "context_policy": {
            "context_window_tokens": CONTEXT_WINDOW_TOKENS,
            "provider_usable_context_tokens": PROVIDER_USABLE_CONTEXT_TOKENS,
            "prompt_reserve_tokens": PROMPT_RESERVE_TOKENS,
            "output_reserve_tokens": OUTPUT_RESERVE_TOKENS,
            "production_pack_char_ceiling": _relationship_context_char_budget(
                SimpleNamespace(
                    context_window_tokens=CONTEXT_WINDOW_TOKENS,
                    prompt_reserve_tokens=PROMPT_RESERVE_TOKENS,
                ),
                None,
            ),
            "estimated_utf8_bytes_per_token": 3,
            "max_pair_jobs_per_packet": MAX_PAIR_JOBS,
            "prompt_source_set_id": run_id,
            "prompt_source_set_id_basis": (
                "run_id substitution because frozen pair jobs omit the original request value"
            ),
        },
        "fixed_membership": {
            "complete_v1_all_fit": all(row["complete_v1"]["fits"] for row in packets),
            "note_only_v1_all_fit": all(
                row["note_only_v1_estimate"]["fits"] for row in packets
            ),
            "complete_v1_estimated_input_tokens": complete_tokens,
            "note_only_v1_estimated_input_tokens": note_only_tokens,
            "estimated_reduction_percent": reduction_percent,
        },
        "natural_pack_estimate": {
            "complete_v1_packet_count": len(complete_packets),
            "complete_v1_packet_sizes": [len(packet) for packet in complete_packets],
            "note_only_v1_packet_count": len(note_only_packets),
            "note_only_v1_packet_sizes": [len(packet) for packet in note_only_packets],
        },
        "overall_components": overall_components,
        "across_packet_source_repetition": {
            component: {
                "packet_unique_bytes_total": packet_unique_totals[component],
                "global_unique_bytes": global_unique_totals[component],
                "repeated_bytes": packet_unique_totals[component]
                - global_unique_totals[component],
            }
            for component in global_values
        },
        "material_reduction_target_percent": MATERIAL_REDUCTION_TARGET_PERCENT,
        "separate_ab_experiment_justified": reduction_percent
        >= MATERIAL_REDUCTION_TARGET_PERCENT,
        "ab_authorization": "not_granted",
        "transport_changes_implemented": False,
        "packets": packets,
    }
    write_yaml(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.workspace, args.run_id, args.report)
    print(
        json.dumps(
            {
                "status": result["status"],
                "pair_job_count": result["pair_job_count"],
                "frozen_packet_count": result["frozen_packet_count"],
                "provider_calls": result["provider_calls"],
                "report": str(args.report.expanduser().resolve()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
