"""Private frozen-note comparison support; no public application entry point."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from auto_zettelkasten.files import atomic_write_text, read_yaml
from auto_zettelkasten.indexes import build_source_catalogue, lean_discovery_projection
from auto_zettelkasten.literature import _persist_typed_source_relation_projection
from auto_zettelkasten.models import NavigationPolicy
from auto_zettelkasten.notes import internal_note_text, read_note, semantic_note_hash, update_note_graph
from auto_zettelkasten.pipeline import _project_atomic_graph
from auto_zettelkasten.relationships import persist_relationship_registry
from auto_zettelkasten.workspace import initialize


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def save(path: Path, value: Any, *, replace: bool = False) -> None:
    text = canonical(value) + "\n"
    if path.exists() and path.read_text() == text:
        return
    if path.exists() and not replace:
        raise ValueError(f"refusing to overwrite frozen evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, text)
    path.chmod(0o600)


def verify_cohort(cohort: Mapping[str, Any]) -> None:
    records = cohort["records"]
    if not records or len({r["source_id"] for r in records}) != len(records):
        raise ValueError("empty cohort or duplicate source identity")
    if len({r["note_id"] for r in records}) != len(records):
        raise ValueError("duplicate note identity")
    if cohort["descriptions"] != [r["description"] for r in records]:
        raise ValueError("cohort descriptions differ from their bound records")
    for row in records:
        for binding in row["inputs"].values():
            path = Path(binding["path"])
            if not path.is_file() or digest(path.read_bytes()) != binding["sha256"]:
                raise ValueError("original input hash mismatch")
        for field in ("description", "derived_profile"):
            if digest(canonical(row[field])) != row[field + "_sha256"]:
                raise ValueError(f"{field} hash mismatch")
            if row[field]["source_id"] != row["source_id"]:
                raise ValueError(f"{field} owner mismatch")
        if row["derived_profile"]["note_id"] != row["note_id"]:
            raise ValueError("derived profile note identity mismatch")
        note_path = Path(row["inputs"]["note"]["path"])
        note = read_note(note_path)
        if any(note["frontmatter"].get(k) != row[k] for k in ("source_id", "note_id")):
            raise ValueError("note identity mismatch")
        if digest(internal_note_text(note_path)) != row["internal_note_sha256"]:
            raise ValueError("canonical note hash mismatch")
        if set(row["migration"]["description_changed_fields"]) - {"thesis"}:
            raise ValueError("profile migration changed frozen discovery content")


def measured_record_capacity(paths: Sequence[Path], allowance: int = 65_536) -> dict[str, Any]:
    """Nearest-rank p90 of all raw saved candidate records, before arm execution."""
    sizes, bindings = [], []
    for path in sorted(paths):
        payload = read_yaml(path, {})
        response = payload.get("response", payload)
        candidates = response.get("candidates", [])
        if not isinstance(candidates, list):
            raise ValueError("saved candidate sample must be a list")
        for row in candidates:
            if not isinstance(row, Mapping):
                raise ValueError("saved candidate sample contains a malformed record")
            sizes.append(math.ceil(len(canonical(row).encode()) / 3))
        bindings.append({"path": str(path), "sha256": digest(path.read_bytes()),
                         "record_count": len(candidates)})
    if not sizes or allowance < 2:
        raise ValueError("a nonempty saved-response sample and completion allowance are required")
    sizes.sort()
    p90 = sizes[math.ceil(0.9 * len(sizes)) - 1]
    return {"estimator": "ceil(UTF8_bytes/3)", "percentile": "nearest-rank p90",
            "sample": bindings, "record_count": len(sizes), "p90_record_tokens": p90,
            "completion_allowance": allowance, "visible_record_budget": allowance // 2,
            "max_records": (allowance // 2) // p90,
            "service_output_cap_supported": False,
            "note": "Equal reservation, not a service-enforced output limit; inspect observed usage."}


def prepare_workspace(workspace: Path, cohort: Mapping[str, Any], *,
                      collection_snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Clone only notes/metadata, with current derived profiles bound separately."""
    if workspace.exists():
        raise ValueError("comparison requires a new isolated workspace")
    verify_cohort(cohort)
    os.umask(0o077)
    initialize(workspace)
    note_rows, profiles = [], []
    for row in cohort["records"]:
        original = Path(row["inputs"]["note"]["path"])
        relative = Path("02_source_memory/notes") / original.name
        target = workspace / relative
        if target.exists():
            raise ValueError("note filename collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        if "note_metadata" in row["inputs"]:
            sidecar = workspace / "11_state/note_metadata" / f"{row['note_id']}.yml"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(row["inputs"]["note_metadata"]["path"], sidecar)
        update_note_graph(target, updates={"related": [], "related_notes": [], "clusters": [],
                                          "cluster_links": [], "cluster_roles": {},
                                          "gaps": [], "gap_links": []},
                          related_links=[], cluster_ids=[])
        if semantic_note_hash(internal_note_text(target)) != row["semantic_note_sha256"]:
            raise ValueError("clearing historical graph changed note analysis")
        profiles.append(row["derived_profile"])
        note_rows.append({**read_note(target)["frontmatter"],
                          "source_id": row["source_id"], "note_id": row["note_id"],
                          "thesis": row["description"]["thesis"], "method": row["description"]["method"],
                          "note_path": str(relative), "semantic_note_sha256": row["semantic_note_sha256"]})
    catalogue = build_source_catalogue(workspace, profiles, note_rows,
                                       collection_snapshot=collection_snapshot, write_cluster_outputs=False)
    projected = lean_discovery_projection(profiles, read_yaml(Path(catalogue["catalogue_path"]), {}))
    by_id = {r["source_id"]: r for r in projected}
    changes = []
    for original in cohort["descriptions"]:
        current = by_id[original["source_id"]]
        changed = {k: {"original": original.get(k), "derived": current.get(k)}
                   for k in original.keys() | current.keys() if original.get(k) != current.get(k)}
        if changed.keys() - {"virtual_topic_ids", "literature_ids", "collection_keys"}:
            raise ValueError("prepared catalogue changed substantive frozen descriptions")
        if changed:
            changes.append({"source_id": original["source_id"], "fields": changed})
    descriptions = [by_id[r["source_id"]] for r in cohort["descriptions"]]
    if "common_descriptions" in cohort and descriptions != cohort["common_descriptions"]:
        raise ValueError("prepared descriptions differ from the common prelive freeze")
    # Derivations are not written over original profile files or mislabeled as
    # previously accepted generation. No source bundle or PDF is required here.
    save(workspace / "COMPARISON_INPUTS.json", {"profiles": profiles, "note_rows": note_rows,
                                               "cohort_sha256": digest(canonical(cohort))})
    return {"profiles": profiles, "note_rows": note_rows, "catalogue": catalogue,
            "descriptions": descriptions, "routing_derivation": changes}


def persist(workspace: Path, prepared: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    registry = persist_relationship_registry(
        workspace, structural_relations=[], accepted_relations=result.get("accepted", []),
        no_relationship_decisions=result.get("no_relationship", []), parked_rows=result.get("parked", []),
    )
    _persist_typed_source_relation_projection(workspace, registry)
    _project_atomic_graph(workspace, note_rows=prepared["note_rows"], profiles=prepared["profiles"],
                          relations=registry.get("relations", []), navigation={},
                          navigation_policy=NavigationPolicy())
    verify_projection(workspace, prepared, registry)
    return registry


def verify_projection(workspace: Path, prepared: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
    notes = {r["source_id"]: r for r in prepared["note_rows"]}
    fronts = {}
    for sid, row in notes.items():
        path = workspace / row["note_path"]
        if semantic_note_hash(internal_note_text(path)) != row["semantic_note_sha256"]:
            raise ValueError("relationship projection changed atomic analysis")
        fronts[sid] = read_note(path)["frontmatter"]
    for relation in registry.get("relations", []):
        if not relation.get("active", True) or relation.get("decision_status") != "accepted":
            continue
        left, right = relation["source_id"], relation["target_source_id"]
        if left == right or left not in notes or right not in notes:
            raise ValueError("invalid persisted destination")
        for source, target in ((left, right), (right, left)):
            if not any(r.get("note_id") == notes[target]["note_id"] and r.get("reason")
                       for r in fronts[source].get("related_notes", [])):
                raise ValueError("missing reciprocal note link or rationale")


def execute_mapping(workspace: Path, cohort: Mapping[str, Any], prepared: Mapping[str, Any],
                    *, reader: Any, calls: Any, request: Any, evidence: Path,
                    max_calls: int = 24, replay: bool = False) -> dict[str, Any]:
    from auto_zettelkasten.profiles import profile_from_dict
    from v030_linking_experiment_direct import run_direct
    from v030_linking_experiment_planner import run_planner

    profiles = [profile_from_dict(p) for p in prepared["profiles"]]
    descriptions = prepared["descriptions"]
    if descriptions != cohort["common_descriptions"]:
        raise ValueError("arm input differs from common frozen descriptions")
    if reader.approach == "planner":
        outcome = run_planner(
            workspace, profiles=profiles, catalogue=prepared["catalogue"],
            source_set={"source_set_id": request.source_set_id,
                        "source_ids": [r["source_id"] for r in descriptions], "source_set_type": "library"},
            note_rows=prepared["note_rows"], reader=reader, request=request, reasoner_calls=calls,
            max_records=reader.max_records, input_char_budget=750_000 * 3,
        )
        result = outcome["relationships"]
        if not replay:
            save(evidence / "FAMILY_PLAN.json", outcome["family_plan"])
    else:
        accumulated = {k: [] for k in ("accepted", "no_relationship", "parked")}

        def fits(rows: Any, exclusions: Any, limit: int) -> bool:
            if limit != reader.max_records:
                raise ValueError("response capacity changed")
            system, user = reader.direct_request(rows, excluded_pairs=exclusions,
                                                  source_set_id=request.source_set_id)
            return reader.request_fits(system, user, "relationship_candidate_selection")

        def call(rows: Any, exclusions: Any, limit: int) -> Mapping[str, Any]:
            context = {"descriptions": rows, "excluded_pairs": exclusions, "max_inferred_pairs": limit}
            key = "direct-" + digest(canonical(context))[:24]
            return calls("relationship_candidate_selection", key, "select_direct_candidates", [], context)

        def preserve_raw(page: str, packet: Any, response: Any) -> None:
            save(evidence / "pages" / f"{page}.json", {"request": packet, "response": response})

        def persist_page(page: str, batch: Any, jobs: Any) -> None:
            save(evidence / "pages" / f"{page}-validated.json",
                 {"result": batch, "jobs": [j.to_dict() for j in jobs]})
            for key in accumulated:
                accumulated[key].extend(batch.get(key, []))
            persist(workspace, prepared, accumulated)

        result = run_direct(descriptions, profiles, call=call, fits=fits, max_records=reader.max_records,
                            preserve_raw=preserve_raw, persist_page=persist_page,
                            provider=reader.name, model=reader.model, max_calls=max_calls)
    persist(workspace, prepared, result)
    return result


def accounting(calls: Any, guard: Any) -> dict[str, Any]:
    usage = read_yaml(calls.usage_path, {}) or {}
    stages: dict[str, dict[str, Any]] = {}
    for attempt in usage.get("attempts", []):
        kind = "planning" if attempt.get("stage") == "literature_family_plan" else "linking"
        row = stages.setdefault(kind, {"logical_attempts": 0, "completed": 0, "failed": 0,
                                       "input_tokens": 0, "output_tokens_including_reasoning": 0,
                                       "reasoning_tokens": 0, "visible_output_tokens": 0,
                                       "attempts_with_unavailable_usage": 0})
        row["logical_attempts"] += 1
        status = attempt.get("status")
        if status in {"completed", "failed"}:
            row[status] += 1
        tokens = (attempt.get("provider_completion") or {}).get("usage") or {}
        values = [tokens.get(k) for k in ("input_tokens", "output_tokens", "reasoning_output_tokens")]
        if any(type(value) is not int or value < 0 for value in values):
            row["attempts_with_unavailable_usage"] += 1
            continue
        input_tokens, output, reasoning = values
        row["input_tokens"] += input_tokens
        row["output_tokens_including_reasoning"] += output
        row["reasoning_tokens"] += reasoning
        row["visible_output_tokens"] += output - reasoning
    reservations = [] if guard is None else [json.loads(line) for line in guard.ledger_path.read_text().splitlines()
                                             if line.strip() and json.loads(line).get("record") == "reserved"]
    if any(row.get("role") != "relationship" or row.get("job_attempt_number") != 1 for row in reservations):
        raise ValueError("source call or automatic retry appeared in campaign ledger")
    return {"graph_calls": len(reservations), "source_calls": 0, "stages": stages,
            "logical_attempts": calls.provider_calls,
            "reused_initial_planner_responses": getattr(calls, "recovered_initial_calls", 0),
            "token_cost_note": "Measured provider tokens; not measured subscription deductions."}


def run_campaign(manifest_path: Path, authorization_path: Path, *, replay: bool = False) -> dict[str, Any]:
    """Run one already-frozen arm. Never launches another campaign or changes its budget."""
    import v030_codex_pdf_eval as base
    from auto_zettelkasten.codex_attempt_guard import deny_codex_attempts
    from auto_zettelkasten.models import LiteratureMappingPolicy
    from v030_codex_campaign_guard import CodexCampaignGuard
    from v030_linking_experiment_planner import ExperimentReasonerCalls, experiment_request
    from v030_linking_experiment_reader import ExperimentCodexReader

    manifest = json.loads(manifest_path.read_text())
    repository = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != repository or ".codex/worktrees" in str(repository):
        raise ValueError("launch comparison only from the feature repository")
    if base._repository_state() != (manifest["code_commit"], False):
        raise ValueError("comparison requires its frozen clean commit")
    for key in ("cohort", "capacity", "prepared", "helper", "offline_acceptance"):
        if digest(Path(manifest[key]).read_bytes()) != manifest[key + "_sha256"]:
            raise ValueError(f"frozen {key} changed")
    offline = json.loads(Path(manifest["offline_acceptance"]).read_text())
    if offline.get("status") != "passed" or offline.get("code_commit") != manifest["code_commit"]:
        raise ValueError("offline prerequisites are not accepted for this code")
    if offline.get("helper_sha256") != manifest["helper_sha256"]:
        raise ValueError("offline helper verification does not match the pinned executable")
    diagnostic = manifest.get("single_call_diagnostic") is True
    recovered_checkpoint = None
    if manifest.get("recovered_initial_checkpoint"):
        if diagnostic or manifest.get("approach") != "planner":
            raise ValueError("recovered initial checkpoint requires a planner continuation")
        plan_path = Path(manifest["recovered_initial_checkpoint"])
        if digest(plan_path.read_bytes()) != manifest["recovered_initial_checkpoint_sha256"]:
            raise ValueError("frozen recovered initial checkpoint changed")
        recovered_checkpoint = read_yaml(plan_path, {})
        if not isinstance(recovered_checkpoint, dict) or not recovered_checkpoint:
            raise ValueError("recovered initial checkpoint is empty or invalid")
    call_limit = 23 if recovered_checkpoint is not None else 1 if diagnostic else 24
    if (manifest["source_attempt_limit"] != 0 or manifest["relationship_attempt_limit"] != call_limit
            or (diagnostic and (manifest.get("approach") != "direct"
                               or manifest.get("model") != "gpt-5.6-luna"))):
        raise ValueError("campaign allowance differs from approved plan")
    if manifest["reasoning_effort"] != "max" or manifest["deadline_seconds"] != 14_400:
        raise ValueError("campaign reasoning or deadline changed")
    cohort = json.loads(Path(manifest["cohort"]).read_text())
    verify_cohort(cohort)
    capacity = json.loads(Path(manifest["capacity"]).read_text())
    prepared = json.loads(Path(manifest["prepared"]).read_text())
    workspace = Path(manifest["workspace"])
    evidence = manifest_path.parent
    if replay:
        if json.loads((evidence / "RUN_RECEIPT.json").read_text())["status"] not in {
            "mechanical_pass_review_pending", "diagnostic_completed_review_pending"
        }:
            raise ValueError("exact replay requires mechanically completed campaign")
    elif (evidence / "RUN_RECEIPT.json").exists():
        raise ValueError("one campaign per arm; no automatic retry")
    if not replay and base._gate_snapshot(workspace) != {
        p: tuple(values) for p, values in manifest["prepared_inventory"].items()
    }:
        raise ValueError("prepared workspace changed before execution")
    os.environ["AUTO_ZETTELKASTEN_CODEX"] = manifest["helper"]
    reader = ExperimentCodexReader(
        manifest["model"], approach=manifest["approach"], max_records=capacity["max_records"],
        capability=manifest["capability"], allow_cloud=True, attempt_guard=None,
        credential_forbidden_roots=(workspace, repository),
    )
    request = experiment_request(
        workspace, model=manifest["model"], run_id=manifest["run_id"],
        source_set_id=manifest["source_set_id"], allow_cloud=True,
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=call_limit,
                                                  literature_deadline_seconds=14_400,
                                                  cluster_generation_enabled=False),
    )
    calls = ExperimentReasonerCalls(workspace, manifest["run_id"], reader, request,
                                    experiment_identity=manifest["experiment_identity"],
                                    input_char_budget=750_000 * 3,
                                    recovered_initial_checkpoint=recovered_checkpoint)
    settings = base.GateSettings(stage=manifest["stage"], case_count=len(cohort["records"]),
                                 source_attempt_limit=0, relationship_attempt_limit=call_limit,
                                 total_attempt_limit=call_limit, stage_deadline_seconds=14_400)
    before = base._gate_snapshot(workspace) if replay else None
    started = time.monotonic()
    guard = None if replay else CodexCampaignGuard.start(
        authorization_path, digest(authorization_path.read_bytes()), repository_root=repository,
        stage=manifest["stage"], manifest_path=manifest_path, manifest_sha256=digest(manifest_path.read_bytes()),
        evaluation_id=manifest["evaluation_id"], run_id=manifest["run_id"],
        source_attempt_limit=0, relationship_attempt_limit=call_limit, total_attempt_limit=call_limit,
    )
    reader.attempt_guard = guard
    try:
        reader.campaign_expires_at = time.monotonic() + settings.stage_deadline_seconds
        with (deny_codex_attempts() if replay else guard.activate()), base._stage_deadline(settings):
            result = execute_mapping(workspace, cohort, prepared, reader=reader, calls=calls,
                                     request=request, evidence=evidence, max_calls=call_limit, replay=replay)
        if not replay:
            save(evidence / "RESULT.json", result)
        successful = (result.get("status") == "completed_paging" if manifest["approach"] == "direct"
                      else result.get("relationship_stage_complete") is True)
        if diagnostic:
            successful = (result.get("status") in {"completed_paging", "incomplete_budget"}
                          and len(result.get("completed_requests", [])) == 1)
        if not successful or result.get("parked") or result.get("needs_more_context"):
            raise ValueError("arm incomplete or failed; preserved result requires diagnosis")
        verify_cohort(cohort)
        if replay:
            if calls.provider_calls != 0 or before != base._gate_snapshot(workspace):
                raise ValueError("replay changed protected outputs or attempted a provider call")
            receipt = {"status": "passed", "provider_calls": 0, "protected_files": len(before)}
            save(evidence / "REPLAY_RECEIPT.json", receipt)
        else:
            receipt = {"status": ("diagnostic_completed_review_pending" if diagnostic
                                  else "mechanical_pass_review_pending"), **accounting(calls, guard),
                       "elapsed_seconds": time.monotonic() - started,
                       "result_sha256": digest((evidence / "RESULT.json").read_bytes())}
            guard.finish("passed")
            save(evidence / "RUN_RECEIPT.json", receipt)
        return receipt
    except BaseException as exc:
        if guard is not None:
            guard.finish("failed", reason=type(exc).__name__)
            save(evidence / "RUN_RECEIPT.json", {"status": "failed", "error": str(exc),
                 "error_type": type(exc).__name__, **accounting(calls, guard),
                 "elapsed_seconds": time.monotonic() - started})
        raise
