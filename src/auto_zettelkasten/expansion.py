from __future__ import annotations

import csv
import json
import re
import secrets
import urllib.parse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from .citations import backfill_citation_sidecars
from .files import atomic_write_text, now_iso, read_yaml, sha256_text, write_yaml
from .identity import bibliographic_tuple, identify_work, merge_work_metadata, work_metadata
from .literature import render_cluster_expansion_navigation
from .models import (
    ArtifactManifest,
    ExpansionCandidate,
    ExpansionDecision,
    ExpansionReport,
    ExpansionRequest,
    MapRequest,
    RunReport,
)
from .notes import item_data, item_key, normalize_tag, parse_atomic_note, source_id_for_item
from .pipeline import run_pipeline
from .ports import (
    ControllerPort,
    ExpansionControllerPort,
    ReaderProvider,
    ScholarlyGraphProvider,
    VisionProvider,
    ZoteroClient,
)
from .scholarly import SemanticScholarProvider
from .workspace import (
    artifact_rows,
    assert_compatible,
    confined_child,
    require_schema,
    resolve_workspace,
    run_directory,
    validate_opaque_id,
    migrate,
)
from .zotero import ZoteroLocalClient

CANDIDATES_RELATIVE = Path("03_literature_synthesis/expansion/candidates.yml")
DECISIONS_RELATIVE = Path("03_literature_synthesis/expansion/decisions.yml")
EXPANSION_ROOT_RELATIVE = Path("03_literature_synthesis/expansion")
RELATION_STRENGTH = {
    "cites": 1.0,
    "cited_by": 1.0,
    "zotero_related": 1.0,
    "co_cited_with": 0.8,
    "bibliographic_coupling": 0.8,
    "recommended_similar": 0.6,
    # Two-hop path is suggestion provenance, not an analytical relation, so it
    # deliberately receives no relation-strength credit.
    "citation_path": 0.0,
    "accepted_tag_neighbor": 0.4,
}
RELATION_PRIORITY = tuple(RELATION_STRENGTH)
VALID_STATES = {"proposed", "accepted", "parked", "rejected"}


def migrate_workspace(
    workspace: Path | str,
    *,
    target: str = "1.1",
    target_version: str | None = None,
    dry_run: bool = False,
) -> ArtifactManifest:
    """Public additive migration entry point; target_version is a CLI compatibility alias."""

    return migrate(workspace, target=target_version or target, dry_run=dry_run)


def run_expansion(
    request: ExpansionRequest,
    *,
    client: ZoteroClient | None = None,
    graph_provider: ScholarlyGraphProvider | None = None,
    controller: ExpansionControllerPort | None = None,
) -> ExpansionReport:
    root = resolve_workspace(request.workspace)
    assert_compatible(root)
    require_schema(root, "1.1", operation="graph expansion")
    run_id = request.run_id or _new_expansion_run_id()
    validate_opaque_id(run_id, field="run_id")
    request = replace(request, run_id=run_id)
    run_dir = run_directory(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    saved_request = {
        **request.to_dict(),
        "workspace": str(root),
        "run_id": run_id,
        "engine_version": ENGINE_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
    }
    prior_request = read_yaml(run_dir / "expansion_request.yml", {}) or {}
    if prior_request and _request_identity(prior_request) != _request_identity(saved_request):
        raise ValueError(f"run_id {run_id} already belongs to a different expansion request")
    write_yaml(run_dir / "expansion_request.yml", saved_request)

    zotero = client or ZoteroLocalClient()
    note_rows = _workspace_notes(root)
    targets, seed_errors = _resolve_targets(root, request, note_rows)
    if not targets:
        return _write_blocked_report(root, run_id, request, seed_errors or [{"reason": "no_expansion_seeds"}])

    # Sidecars are local canonical derivatives and are backfilled only when their
    # inspected-content hash can be reproduced exactly.
    seed_source_ids = sorted({str(seed.get("source_id") or "") for rows in targets.values() for seed in rows if seed.get("source_id")})
    try:
        backfill_citation_sidecars(root, source_ids=seed_source_ids, client=zotero)
    except Exception:
        # Missing Zotero/local files should not suppress already persisted graph evidence.
        pass

    local_items = _local_zotero_items(root, zotero)
    local_by_key = {item_key(row): row for row in local_items if item_key(row)}
    mapped_work_ids = {identify_work(row)[0] for row in note_rows}
    existing = _load_candidates(root)
    existing_by_scope_work = {
        (row.target_scope, row.target_id, row.work_id): row for row in existing
    }
    target_associations = _target_associations(root, request, targets)
    observations: dict[tuple[str, str, str], dict[str, Any]] = {}
    existing_work_count = 0
    local_frontier: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []

    for target_id, seeds in targets.items():
        for seed in seeds:
            for row in _local_neighbors(root, seed, local_items, local_by_key, note_rows):
                work_id, actionability = identify_work(row)
                if row.get("local_zotero_item"):
                    local_frontier.append((target_id, seed, row))
                if work_id in mapped_work_ids:
                    existing_work_count += 1
                    continue
                _accumulate_observation(
                    observations,
                    request=request,
                    target_id=target_id,
                    seed=seed,
                    row={**row, "work_id": work_id, "actionability": actionability},
                    provider="internal",
                    depth=1,
                )

    if request.depth == 2:
        for target_id, origin_seed, frontier in _round_robin_frontier(local_frontier, request.budget):
            frontier_item = frontier.get("local_zotero_item")
            if not isinstance(frontier_item, Mapping):
                continue
            frontier_seed = {
                **work_metadata(frontier_item),
                "source_id": source_id_for_item(frontier_item),
                "zotero_item_key": item_key(frontier_item),
                "zotero_relations": item_data(frontier_item).get("relations", {}),
            }
            frontier_work_id = identify_work(frontier_item)[0]
            first_relation = str(frontier.get("relation_type") or "zotero_related")
            for row in _local_neighbors(root, frontier_seed, local_items, local_by_key, note_rows):
                work_id, actionability = identify_work(row)
                if work_id in mapped_work_ids or work_id == identify_work(origin_seed)[0]:
                    continue
                _accumulate_observation(
                    observations,
                    request=request,
                    target_id=target_id,
                    seed=origin_seed,
                    row={
                        **row,
                        "work_id": work_id,
                        "actionability": actionability,
                        "relation_type": _two_hop_relation(first_relation, str(row.get("relation_type") or "zotero_related")),
                        "provenance": "bounded_internal_two_hop_path",
                        "path_work_ids": [frontier_work_id, work_id],
                        "path_relation_types": [first_relation, str(row.get("relation_type") or "zotero_related")],
                        "seed_ids": list(frontier.get("seed_ids", [])),
                        "originating_cluster_ids": list(frontier.get("originating_cluster_ids", [])),
                    },
                    provider="internal",
                    depth=2,
                )

    prior_attempt_payload = read_yaml(run_dir / "provider_attempts.yml", {}) or {}
    attempts: list[dict[str, Any]] = [
        dict(row) for row in prior_attempt_payload.get("attempts", []) if isinstance(row, Mapping)
    ]
    if request.provider == "semantic-scholar":
        if not request.allow_network:
            raise ValueError("semantic-scholar requires fresh --allow-network consent")
        provider = graph_provider or SemanticScholarProvider()
        try:
            _collect_external_neighbors(
                root,
                run_dir,
                request,
                targets,
                observations,
                mapped_work_ids,
                provider,
            )
        except Exception as exc:
            seed_errors.append({"reason": f"graph_provider:{type(exc).__name__}:{exc}"})
        finally:
            attempts = _merge_attempts(attempts, [dict(row) for row in provider.drain_attempts()])

    observations = _reconcile_observation_identities(observations, local_items, existing)

    candidates = _materialize_candidates(
        request,
        targets,
        observations,
        existing_by_scope_work,
        local_items,
        target_associations,
    )
    selected, truncated = _bounded_candidates(candidates, request)
    selected_ids = {row.suggestion_id for row in selected}
    with _registry_lock(root):
        latest = _load_candidates(root)
        latest = _canonicalize_existing_candidates(latest, selected)
        latest_by_id = {row.suggestion_id: row for row in latest}
        selected = [
            _preserve_candidate_state(row, latest_by_id[row.suggestion_id])
            if row.suggestion_id in latest_by_id
            else row
            for row in selected
        ]
        merged = [row for row in latest if row.suggestion_id not in selected_ids]
        merged.extend(selected)
        _write_candidates(root, merged)

    if controller is not None:
        new_ids = {row.suggestion_id for row in selected if row.suggestion_id not in {old.suggestion_id for old in existing}}
        _apply_controller_reviews(root, [row for row in selected if row.suggestion_id in new_ids], controller)
        merged = _load_candidates(root)
        by_id = {row.suggestion_id: row for row in merged}
        selected = [by_id[row.suggestion_id] for row in selected]

    render_paths = render_expansion_projection(root)
    write_yaml(
        run_dir / "provider_attempts.yml",
        {
            "run_id": run_id,
            "engine_version": ENGINE_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "attempts": attempts,
        },
    )
    state_counts = _state_counts(selected)
    manifest = ArtifactManifest(
        status="built",
        workspace=root,
        run_id=run_id,
        created_at=now_iso(),
        artifacts=artifact_rows(
            root,
            [
                root / CANDIDATES_RELATIVE,
                root / DECISIONS_RELATIVE,
                run_dir / "expansion_request.yml",
                run_dir / "provider_attempts.yml",
                run_dir / "provider_results.yml",
                *render_paths,
            ],
        ),
        metadata={"scope": request.scope, "target_ids": list(request.target_ids), "candidate_count": len(selected)},
    )
    report = ExpansionReport(
        status="completed" if not seed_errors else "completed_with_errors",
        workspace=root,
        run_id=run_id,
        scope=request.scope,
        target_ids=list(request.target_ids),
        provider=request.provider,
        seed_count=sum(len(rows) for rows in targets.values()),
        candidate_count=len(selected),
        proposed_count=state_counts["proposed"],
        accepted_count=state_counts["accepted"],
        parked_count=state_counts["parked"],
        rejected_count=state_counts["rejected"],
        unresolved_count=sum(1 for row in selected if row.actionability == "resolve_identity"),
        existing_work_count=existing_work_count,
        truncated=truncated,
        candidates=[row.to_dict() for row in selected],
        attempts=attempts,
        errors=seed_errors,
        artifact_manifest=manifest,
    )
    write_yaml(run_dir / "expansion_report.yml", report.to_dict())
    return report


def resume_expansion(
    workspace: Path | str,
    run_id: str,
    *,
    allow_network: bool = False,
    client: ZoteroClient | None = None,
    graph_provider: ScholarlyGraphProvider | None = None,
    controller: ExpansionControllerPort | None = None,
) -> ExpansionReport:
    root = resolve_workspace(workspace)
    payload = read_yaml(run_directory(root, run_id) / "expansion_request.yml", {}) or {}
    if not payload:
        return ExpansionReport(
            status="blocked",
            workspace=root,
            run_id=run_id,
            errors=[{"reason": "expansion_request_not_found"}],
        )
    payload["workspace"] = str(root)
    payload["run_id"] = run_id
    # Consent is deliberately not inherited from the saved request.
    request = ExpansionRequest.from_dict(payload, allow_network=allow_network)
    return run_expansion(request, client=client, graph_provider=graph_provider, controller=controller)


def list_expansion_candidates(
    workspace: Path | str,
    *,
    state: str | None = None,
) -> list[ExpansionCandidate]:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    if state is not None and state not in VALID_STATES:
        raise ValueError("state must be proposed, accepted, parked, or rejected")
    rows = _load_candidates(root)
    return [row for row in rows if state is None or row.state == state]


def decide_expansion(
    workspace: Path | str,
    decision: ExpansionDecision,
    *,
    controller: ExpansionControllerPort | None = None,
) -> ExpansionCandidate:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    require_schema(root, "1.1", operation="expansion decisions")
    normalized = decision
    if controller is not None:
        candidates = _load_candidates(root)
        matches = [row for row in candidates if row.suggestion_id == decision.suggestion_id]
        if len(matches) != 1:
            raise ValueError("suggestion_id must identify exactly one expansion candidate")
        candidate = matches[0]
        if candidate.decision_version != decision.expected_version:
            raise RuntimeError(
                f"stale expansion decision: expected version {decision.expected_version}, current version {candidate.decision_version}"
            )
        normalized = _controller_decision(candidate, decision, controller)
    with _registry_lock(root):
        updated = _persist_decision(root, normalized)
    render_expansion_projection(root)
    return updated


def _persist_decision(root: Path, decision: ExpansionDecision) -> ExpansionCandidate:
    candidates = _load_candidates(root)
    matches = [row for row in candidates if row.suggestion_id == decision.suggestion_id]
    if len(matches) != 1:
        raise ValueError("suggestion_id must identify exactly one expansion candidate")
    candidate = matches[0]
    if candidate.decision_version != decision.expected_version:
        raise RuntimeError(
            f"stale expansion decision: expected version {decision.expected_version}, current version {candidate.decision_version}"
        )
    if decision.decision == "accepted" and candidate.actionability == "resolve_identity":
        raise ValueError("unresolved candidates cannot be accepted")
    normalized = decision
    decided_at = normalized.decided_at or now_iso()
    updated = ExpansionCandidate.from_dict(
        {
            **candidate.to_dict(),
            "state": normalized.decision,
            "decision_version": candidate.decision_version + 1,
            "updated_at": decided_at,
        }
    )
    _write_candidates(root, [updated if row.suggestion_id == updated.suggestion_id else row for row in candidates])
    history = _load_decision_rows(root)
    history.append(
        {
            "decision_id": f"expansion-decision-{sha256_text(updated.suggestion_id + '|' + str(updated.decision_version))[:16]}",
            "suggestion_id": updated.suggestion_id,
            "work_id": updated.work_id,
            "target_scope": updated.target_scope,
            "target_id": updated.target_id,
            "previous_state": candidate.state,
            "decision": normalized.decision,
            "reason": normalized.reason,
            "actor": normalized.actor,
            "expected_version": normalized.expected_version,
            "decision_version": updated.decision_version,
            "decided_at": decided_at,
        }
    )
    _write_decisions(root, history)
    return updated


def map_accepted_candidates(
    workspace: Path | str,
    *,
    suggestion_ids: Sequence[str] = (),
    client: ZoteroClient | None = None,
    reader: ReaderProvider | None = None,
    vision: VisionProvider | None = None,
    controller: ControllerPort | None = None,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    allow_cloud: bool = False,
    parallel: int = 4,
    question: str | None = None,
    run_id: str | None = None,
) -> RunReport:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    require_schema(root, "1.1", operation="mapping accepted expansion candidates")
    wanted = {str(value) for value in suggestion_ids if value}
    all_candidates = _load_candidates(root)
    selected = [row for row in all_candidates if row.state == "accepted" and (not wanted or row.suggestion_id in wanted)]
    if wanted - {row.suggestion_id for row in selected}:
        missing = sorted(wanted - {row.suggestion_id for row in selected})
        raise ValueError(f"suggestions are missing or not accepted: {', '.join(missing)}")
    unresolved = sorted(row.suggestion_id for row in selected if row.actionability == "resolve_identity")
    if unresolved:
        raise ValueError(
            "accepted suggestions require resolved identity before mapping: "
            + ", ".join(unresolved)
        )
    zotero = client or ZoteroLocalClient()
    local: list[tuple[ExpansionCandidate, dict[str, Any]]] = []
    seen_keys: set[str] = set()
    inventory_for_reconciliation: list[Mapping[str, Any]] = []
    if any(not row.zotero_item_key for row in selected):
        try:
            inventory_for_reconciliation = list(zotero.inventory("library"))
        except Exception:
            inventory_for_reconciliation = []
    reconciled_candidates: dict[str, ExpansionCandidate] = {}
    resolvable_ids: set[str] = set()
    identity_blocked_ids: set[str] = set()
    for candidate in selected:
        if not candidate.zotero_item_key and inventory_for_reconciliation:
            matches = [row for row in inventory_for_reconciliation if identify_work(row)[0] == candidate.work_id]
            if not matches and all(bibliographic_tuple(candidate.to_dict())):
                tuple_matches = [
                    row
                    for row in inventory_for_reconciliation
                    if bibliographic_tuple(row) == bibliographic_tuple(candidate.to_dict())
                ]
                matches = [row for row in tuple_matches if _candidate_item_compatible(candidate, row)]
                if tuple_matches and not matches:
                    identity_blocked_ids.add(candidate.suggestion_id)
            if len(matches) == 1:
                matched = matches[0]
                reconciled_metadata = merge_work_metadata(candidate.to_dict(), work_metadata(matched))
                candidate = ExpansionCandidate.from_dict(
                    {
                        **candidate.to_dict(),
                        **reconciled_metadata,
                        "local_zotero_item": dict(matched),
                        "zotero_item_key": item_key(matched),
                        "updated_at": now_iso(),
                    }
                )
                reconciled_candidates[candidate.suggestion_id] = candidate
        if not candidate.zotero_item_key:
            continue
        item: Mapping[str, Any] | None = None
        exact_lookup = getattr(zotero, "item", None)
        if callable(exact_lookup):
            try:
                item = exact_lookup(candidate.zotero_item_key)
            except Exception:
                item = None
        elif candidate.local_zotero_item:
            item = candidate.local_zotero_item
        if item and not _candidate_item_compatible(candidate, item):
            identity_blocked_ids.add(candidate.suggestion_id)
            continue
        if not item:
            item = {
                "key": candidate.zotero_item_key,
                "data": {
                    "key": candidate.zotero_item_key,
                    "title": candidate.title,
                    "date": candidate.year,
                    "DOI": candidate.doi,
                    "url": candidate.url,
                    "creators": [{"name": author} for author in candidate.authors],
                },
            }
        resolvable_ids.add(candidate.suggestion_id)
        key = item_key(item)
        if key and key not in seen_keys:
            seen_keys.add(key)
            local.append((candidate, dict(item)))

    effective_run_id = run_id or _new_expansion_map_run_id()
    if local:
        focused_client = _FocusedZoteroClient(zotero, [item for _, item in local])
        request = MapRequest(
            workspace=root,
            scope="library",
            question=question,
            provider=provider,
            model=model,
            allow_cloud=allow_cloud,
            parallel=parallel,
        )
        report = run_pipeline(
            request,
            client=focused_client,
            reader=reader,
            vision=vision,
            controller=controller,
            run_id=effective_run_id,
        )
    else:
        report = RunReport(status="completed_no_local_candidates", workspace=root, run_id=effective_run_id)
        run_dir = run_directory(root, effective_run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_yaml(run_dir / "run_report.yml", report.to_dict())

    terminal_by_key = {str(row.get("zotero_item_key") or ""): row for row in report.items}
    selected_by_id = {row.suggestion_id: row for row in selected}
    local_selected_ids = resolvable_ids
    with _registry_lock(root):
        current_rows = _load_candidates(root)
        updated_rows: list[ExpansionCandidate] = []
        for row in current_rows:
            snapshot = selected_by_id.get(row.suggestion_id)
            if snapshot is None:
                updated_rows.append(row)
                continue
            if row.work_id != snapshot.work_id or row.decision_version != snapshot.decision_version:
                updated_rows.append(row)
                continue
            if row.suggestion_id in reconciled_candidates:
                reconciled = reconciled_candidates[row.suggestion_id]
                row = ExpansionCandidate.from_dict(
                    {
                        **row.to_dict(),
                        "zotero_item_key": reconciled.zotero_item_key,
                        "local_zotero_item": reconciled.local_zotero_item,
                    }
                )
            terminal = terminal_by_key.get(row.zotero_item_key, {})
            fulfillment = row.fulfillment
            if terminal.get("terminal_status") == "validated_note":
                fulfillment = "mapped"
            elif terminal.get("terminal_status") == "exhausted":
                fulfillment = "exhausted"
            elif row.suggestion_id in identity_blocked_ids:
                fulfillment = "blocked"
            elif row.suggestion_id in local_selected_ids and report.status == "blocked":
                fulfillment = "blocked"
            updated_rows.append(
                ExpansionCandidate.from_dict(
                    {**row.to_dict(), "fulfillment": fulfillment, "updated_at": now_iso()}
                )
            )
        _write_candidates(root, updated_rows)
    _mark_graph_source_set(root, report, sorted(local_selected_ids))
    render_expansion_projection(root)
    return report


def export_expansion_candidates(
    workspace: Path | str,
    output: Path | str,
    *,
    state: str = "accepted",
    format: str = "bibtex",
    output_format: str | None = None,
) -> ArtifactManifest:
    root = resolve_workspace(workspace)
    assert_compatible(root)
    require_schema(root, "1.1", operation="expansion export")
    if state != "accepted":
        raise ValueError("BibTeX/RIS expansion export is restricted to accepted candidates")
    export_format = (output_format or format).casefold()
    if export_format not in {"bibtex", "ris"}:
        raise ValueError("format must be bibtex or ris")
    candidates = [row for row in _load_candidates(root) if row.state == state]
    unresolved = sorted(row.suggestion_id for row in candidates if row.actionability == "resolve_identity")
    if unresolved:
        raise ValueError(
            "accepted suggestions require resolved identity before export: "
            + ", ".join(unresolved)
        )
    unique_by_work: dict[str, ExpansionCandidate] = {}
    for candidate in sorted(candidates, key=lambda row: (row.work_id, row.target_id, row.suggestion_id)):
        unique_by_work.setdefault(candidate.work_id, candidate)
    export_rows = list(unique_by_work.values())
    path = Path(output).expanduser().resolve()
    text = _render_bibtex(export_rows) if export_format == "bibtex" else _render_ris(export_rows)
    atomic_write_text(path, text)
    exported_snapshots = {
        row.suggestion_id: (row.work_id, row.decision_version)
        for row in candidates
    }
    with _registry_lock(root):
        all_rows = _load_candidates(root)
        _write_candidates(
            root,
            [
                ExpansionCandidate.from_dict(
                    {**row.to_dict(), "fulfillment": "exported", "updated_at": now_iso()}
                )
                if row.suggestion_id in exported_snapshots
                and exported_snapshots[row.suggestion_id] == (row.work_id, row.decision_version)
                and row.fulfillment == "not_started"
                else row
                for row in all_rows
            ],
        )
    render_expansion_projection(root)
    return ArtifactManifest(
        status="exported",
        workspace=root,
        artifacts=artifact_rows(path.parent, [path]),
        created_at=now_iso(),
        metadata={
            "format": export_format,
            "state": state,
            "candidate_count": len(export_rows),
            "suggestion_count": len(candidates),
            "output": str(path),
        },
    )


def render_expansion_projection(workspace: Path) -> list[Path]:
    candidates = _load_candidates(workspace)
    root = workspace / EXPANSION_ROOT_RELATIVE
    candidate_root = root / "candidates"
    candidate_root.mkdir(parents=True, exist_ok=True)
    expected: set[Path] = set()
    for candidate in candidates:
        path = candidate_root / f"{candidate.suggestion_id}.md"
        atomic_write_text(path, _render_candidate_markdown(candidate))
        expected.add(path)
    for old in candidate_root.glob("*.md"):
        if old not in expected:
            old.unlink()
    index = root / "INDEX.md"
    lines = ["# Expansion Inbox", "", "Generated review surface. Canonical decisions live in the expansion registries.", ""]
    for state in ("proposed", "accepted", "parked", "rejected"):
        lines.extend([f"## {state.title()}", ""])
        rows = [row for row in candidates if row.state == state]
        lines.extend(
            [
                f"- [[{row.suggestion_id}]] — {_safe_markdown_title(row.title or row.work_id)} "
                f"({row.primary_relation}, {row.score:.3f})"
                for row in rows
            ]
            or ["No candidates."]
        )
        lines.append("")
    atomic_write_text(index, "\n".join(lines).rstrip() + "\n")
    cluster_paths = render_cluster_expansion_navigation(workspace)
    return [index, *sorted(expected), *cluster_paths]


def _resolve_targets(
    root: Path,
    request: ExpansionRequest,
    notes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_note = {str(row.get("note_id") or ""): dict(row) for row in notes}
    by_source = {str(row.get("source_id") or ""): dict(row) for row in notes}
    by_zotero = {str(row.get("zotero_item_key") or ""): dict(row) for row in notes}
    cluster_payload = read_yaml(root / "03_literature_synthesis" / "clusters" / "clusters.yml", {}) or {}
    cluster_rows = cluster_payload.get("clusters", []) if isinstance(cluster_payload, Mapping) else []
    clusters_by_id = {
        str(row.get("cluster_id") or ""): row
        for row in cluster_rows
        if isinstance(row, Mapping) and row.get("cluster_id")
    }
    targets: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for target_id in request.target_ids:
        seeds: list[dict[str, Any]] = []
        if request.scope == "source":
            row = by_source.get(target_id) or by_note.get(target_id) or by_zotero.get(target_id)
            if row:
                seeds = [row]
        elif request.scope == "source_set":
            payload = read_yaml(root / "02_source_memory" / "indexes" / "source_sets" / f"{target_id}.yml", {}) or {}
            seed_ids = payload.get("note_ids", []) if isinstance(payload.get("note_ids"), list) else []
            source_ids = payload.get("source_ids", []) if isinstance(payload.get("source_ids"), list) else []
            seeds = [by_note[str(value)] for value in seed_ids if str(value) in by_note]
            seeds.extend(by_source[str(value)] for value in source_ids if str(value) in by_source)
        elif request.scope == "cluster":
            cluster = clusters_by_id.get(target_id, {})
            seeds = _record_seeds(cluster, by_note=by_note, by_source=by_source)
        elif request.scope == "gap":
            payloads = [
                read_yaml(root / "03_literature_synthesis" / "gaps" / "gaps.yml", {}) or {},
                read_yaml(root / "02_source_memory" / "indexes" / "gap_candidates.yml", {}) or {},
            ]
            rows = []
            for payload in payloads:
                if not isinstance(payload, Mapping):
                    continue
                for field in ("gap_candidates", "candidates", "gaps"):
                    if isinstance(payload.get(field), list):
                        rows.extend(payload[field])
                        break
            gap = next((row for row in rows if isinstance(row, Mapping) and str(row.get("gap_id")) == target_id), {})
            seeds = _record_seeds(gap, by_note=by_note, by_source=by_source)
            cluster_ids = {
                str(value)
                for field in ("supporting_clusters", "related_clusters")
                for value in (gap.get(field, []) if isinstance(gap.get(field), list) else [])
            }
            for cluster_id in sorted(cluster_ids):
                seeds.extend(
                    _record_seeds(
                        clusters_by_id.get(cluster_id, {}),
                        by_note=by_note,
                        by_source=by_source,
                    )
                )
        deduped: dict[str, dict[str, Any]] = {}
        for row in seeds:
            seed_ids = {
                str(value)
                for value in (row.get("note_id"), row.get("source_id"))
                if value
            }
            origin_cluster_ids = {
                cluster_id
                for cluster_id, cluster in clusters_by_id.items()
                if seed_ids & _record_member_ids(cluster)
            }
            if request.scope == "cluster":
                origin_cluster_ids.add(target_id)
            key = str(row.get("note_id") or row.get("source_id"))
            annotated = {
                **dict(row),
                "_expansion_origin_cluster_ids": sorted(origin_cluster_ids),
            }
            if key in deduped:
                annotated["_expansion_origin_cluster_ids"] = sorted(
                    set(deduped[key].get("_expansion_origin_cluster_ids", []))
                    | origin_cluster_ids
                )
            deduped[key] = annotated
        if deduped:
            targets[target_id] = sorted(deduped.values(), key=lambda row: str(row.get("note_id") or ""))
        else:
            errors.append({"target_id": target_id, "reason": "expansion_target_not_found_or_empty"})
    return targets, errors


def _record_seeds(
    record: Mapping[str, Any],
    *,
    by_note: Mapping[str, dict[str, Any]],
    by_source: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for value in record.get("note_ids", []) if isinstance(record.get("note_ids"), list) else []:
        if str(value) in by_note:
            seeds.append(by_note[str(value)])
    for field in (
        "source_ids",
        "representative_sources",
        "supporting_sources",
        "supporting_source_ids",
        "closest_prior_work",
    ):
        values = record.get(field, []) if isinstance(record.get(field), list) else []
        for value in values:
            if isinstance(value, Mapping):
                note_id = str(value.get("note_id") or "")
                source_id = str(value.get("source_id") or value.get("id") or "")
            else:
                note_id = ""
                source_id = str(value)
            if note_id in by_note:
                seeds.append(by_note[note_id])
            elif source_id in by_source:
                seeds.append(by_source[source_id])
    return seeds


def _target_associations(
    root: Path,
    request: ExpansionRequest,
    targets: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, tuple[list[str], list[str]]]:
    """Resolve canonical cluster/gap provenance for every expansion target."""

    cluster_payload = read_yaml(root / "03_literature_synthesis" / "clusters" / "clusters.yml", {}) or {}
    cluster_rows = cluster_payload.get("clusters", []) if isinstance(cluster_payload, Mapping) else []
    clusters = {
        str(row.get("cluster_id")): row
        for row in cluster_rows
        if isinstance(row, Mapping) and row.get("cluster_id")
    }
    gaps: dict[str, Mapping[str, Any]] = {}
    for path in (
        root / "03_literature_synthesis" / "gaps" / "gaps.yml",
        root / "02_source_memory" / "indexes" / "gap_candidates.yml",
    ):
        payload = read_yaml(path, {}) or {}
        if not isinstance(payload, Mapping):
            continue
        for field in ("gap_candidates", "candidates", "gaps"):
            rows = payload.get(field)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, Mapping) and row.get("gap_id"):
                    gaps.setdefault(str(row["gap_id"]), row)
            break

    cluster_members = {
        cluster_id: _record_member_ids(row)
        for cluster_id, row in clusters.items()
    }
    gap_clusters = {
        gap_id: _record_id_values(row, "supporting_clusters", "related_clusters")
        for gap_id, row in gaps.items()
    }
    gap_members = {
        gap_id: _record_member_ids(row)
        for gap_id, row in gaps.items()
    }
    associations: dict[str, tuple[list[str], list[str]]] = {}
    for target_id, seeds in targets.items():
        seed_ids = {
            str(value)
            for seed in seeds
            for value in (seed.get("source_id"), seed.get("note_id"))
            if value
        }
        cluster_ids = {
            cluster_id
            for cluster_id, member_ids in cluster_members.items()
            if seed_ids & member_ids
        }
        gap_ids = {
            gap_id
            for gap_id, member_ids in gap_members.items()
            if seed_ids & member_ids
        }
        if request.scope == "source_set":
            payload = read_yaml(
                root / "02_source_memory" / "indexes" / "source_sets" / f"{target_id}.yml",
                {},
            ) or {}
            if isinstance(payload, Mapping):
                cluster_ids.update(_record_id_values(payload, "cluster_ids"))
                gap_ids.update(_record_id_values(payload, "gap_ids"))
        elif request.scope == "cluster":
            cluster_ids.add(target_id)
        elif request.scope == "gap":
            gap_ids.add(target_id)
            cluster_ids.update(gap_clusters.get(target_id, set()))

        # A gap is associated with a target whenever it directly names one of
        # its sources or depends on one of its canonical clusters.
        gap_ids.update(
            gap_id
            for gap_id, related_clusters in gap_clusters.items()
            if cluster_ids & related_clusters
        )
        associations[target_id] = (sorted(cluster_ids), sorted(gap_ids))
    return associations


def _record_member_ids(record: Mapping[str, Any]) -> set[str]:
    values = _record_id_values(record, "note_ids", "source_ids", "supporting_source_ids")
    for field in ("representative_sources", "supporting_sources", "closest_prior_work"):
        rows = record.get(field, []) if isinstance(record.get(field), list) else []
        for row in rows:
            if isinstance(row, Mapping):
                values.update(
                    str(value)
                    for value in (row.get("note_id"), row.get("source_id"), row.get("id"))
                    if value
                )
            elif row:
                values.add(str(row))
    return values


def _record_id_values(record: Mapping[str, Any], *fields: str) -> set[str]:
    values: set[str] = set()
    for field in fields:
        rows = record.get(field, []) if isinstance(record.get(field), list) else []
        for row in rows:
            if isinstance(row, Mapping):
                value = row.get("cluster_id") or row.get("gap_id") or row.get("id")
            else:
                value = row
            if value:
                values.add(str(value))
    return values


def _workspace_notes(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "02_source_memory" / "notes").glob("*.md")):
        try:
            front, _ = parse_atomic_note(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not front.get("note_id") or not front.get("source_id"):
            continue
        metadata = work_metadata(front)
        rows.append(
            {
                **metadata,
                **dict(front),
                "note_path": str(path.relative_to(root)),
                "local_zotero_item": {
                    "key": str(front.get("zotero_item_key") or ""),
                    "data": {
                        "key": str(front.get("zotero_item_key") or ""),
                        "title": str(front.get("title") or ""),
                        "date": str(front.get("date") or ""),
                        "DOI": str(front.get("doi") or ""),
                        "url": str(front.get("url") or ""),
                        "creators": front.get("creators", []),
                        "relations": front.get("zotero_relations", {}),
                        "tags": [{"tag": tag} for tag in front.get("original_zotero_tags", []) or []],
                    },
                },
            }
        )
    return rows


def _local_zotero_items(root: Path, client: ZoteroClient) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "01_custody" / "zotero" / "inventory").glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping) and item_key(row):
                by_key[item_key(row)] = dict(row)
    try:
        for row in client.inventory("library"):
            if isinstance(row, Mapping) and item_key(row):
                by_key[item_key(row)] = dict(row)
    except Exception:
        pass
    return [by_key[key] for key in sorted(by_key)]


def _local_neighbors(
    root: Path,
    seed: Mapping[str, Any],
    local_items: Sequence[Mapping[str, Any]],
    local_by_key: Mapping[str, Mapping[str, Any]],
    note_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seed_key = str(seed.get("zotero_item_key") or "")
    seed_source_id = str(seed.get("source_id") or "")
    relations = seed.get("zotero_relations", {}) if isinstance(seed.get("zotero_relations"), Mapping) else {}
    for predicate, values in relations.items():
        relation_type = _zotero_relation_type(str(predicate))
        for value in values if isinstance(values, list) else [values]:
            key = str(value or "").rstrip("/").rsplit("/", 1)[-1]
            target = local_by_key.get(key)
            if target:
                rows.append({**work_metadata(target), "local_zotero_item": dict(target), "relation_type": relation_type, "seed_source_id": seed_source_id})
    for candidate_item in local_items:
        candidate_key = item_key(candidate_item)
        if not candidate_key or candidate_key == seed_key:
            continue
        candidate_relations = item_data(candidate_item).get("relations", {})
        if not isinstance(candidate_relations, Mapping):
            continue
        for predicate, values in candidate_relations.items():
            targets = {
                str(value or "").rstrip("/").rsplit("/", 1)[-1]
                for value in (values if isinstance(values, list) else [values])
            }
            if seed_key and seed_key in targets:
                rows.append(
                    {
                        **work_metadata(candidate_item),
                        "local_zotero_item": dict(candidate_item),
                        "relation_type": _reverse_relation(_zotero_relation_type(str(predicate))),
                        "seed_source_id": seed_source_id,
                        "provenance": "reverse_exact_zotero_item_relation",
                    }
                )
    try:
        safe_seed_source_id = validate_opaque_id(seed_source_id, field="source_id")
        sidecar_path = confined_child(root / "01_custody" / "citation_leads", f"{safe_seed_source_id}.yml")
    except ValueError:
        sidecar_path = root / "01_custody" / "citation_leads" / "invalid-source-id.yml"
    sidecar = read_yaml(sidecar_path, {}) or {}
    for reference in sidecar.get("references", []) if isinstance(sidecar.get("references"), list) else []:
        if not isinstance(reference, Mapping):
            continue
        matched = _match_local(reference, local_items)
        rows.append(
            {
                **work_metadata(matched or reference),
                **({"local_zotero_item": dict(matched)} if matched else {}),
                "relation_type": "cites",
                "seed_source_id": seed_source_id,
                "provenance": "citation_sidecar",
            }
        )
    # Existing custody relations are graph leads but mapped endpoints are later suppressed.
    registry = root / "01_custody" / "source_relation_registry.csv"
    if registry.exists():
        with registry.open("r", encoding="utf-8", newline="") as handle:
            for relation in csv.DictReader(handle):
                if str(relation.get("source_id") or "") == seed_source_id:
                    target = next((row for row in note_rows if str(row.get("source_id")) == str(relation.get("related_source_id"))), None)
                    if target:
                        rows.append(
                            {
                                **work_metadata(target),
                                "relation_type": _candidate_relation(str(relation.get("relation_type") or "zotero_related")),
                                "seed_source_id": seed_source_id,
                            }
                        )
                elif str(relation.get("related_source_id") or "") == seed_source_id:
                    target = next((row for row in note_rows if str(row.get("source_id")) == str(relation.get("source_id"))), None)
                    if target:
                        rows.append(
                            {
                                **work_metadata(target),
                                "relation_type": _reverse_relation(
                                    _candidate_relation(str(relation.get("relation_type") or "zotero_related"))
                                ),
                                "seed_source_id": seed_source_id,
                                "provenance": "reverse_source_relation_registry",
                            }
                        )
    seed_work_id = identify_work(seed)[0]
    for path in sorted((root / "01_custody" / "citation_leads").glob("*.yml")):
        candidate_sidecar = read_yaml(path, {}) or {}
        if str(candidate_sidecar.get("source_id") or "") == seed_source_id:
            continue
        referenced_ids = {
            str(reference.get("work_id") or identify_work(reference)[0])
            for reference in candidate_sidecar.get("references", [])
            if isinstance(reference, Mapping)
        }
        if seed_work_id not in referenced_ids:
            continue
        target_key = str(candidate_sidecar.get("zotero_item_key") or "")
        target = local_by_key.get(target_key)
        if target:
            rows.append(
                {
                    **work_metadata(target),
                    "local_zotero_item": dict(target),
                    "relation_type": "cited_by",
                    "seed_source_id": seed_source_id,
                    "provenance": "reverse_citation_sidecar",
                }
            )
    tag_registry = read_yaml(root / "02_source_memory" / "indexes" / "tag_registry.yml", {}) or {}
    seed_note_id = str(seed.get("note_id") or "")
    accepted_tags = {
        str(row.get("normalized_tag"))
        for row in tag_registry.get("tags", [])
        if isinstance(row, Mapping) and seed_note_id in {str(value) for value in row.get("note_ids", []) or []}
    }
    if accepted_tags:
        for target in local_items:
            if item_key(target) == seed_key:
                continue
            tags = {
                normalize_tag(str(tag.get("tag") if isinstance(tag, Mapping) else tag))
                for tag in item_data(target).get("tags", [])
            }
            if accepted_tags & tags:
                rows.append(
                    {
                        **work_metadata(target),
                        "local_zotero_item": dict(target),
                        "relation_type": "accepted_tag_neighbor",
                        "seed_source_id": seed_source_id,
                        "shared_tags": sorted(accepted_tags & tags),
                    }
                )
    # Bibliographic coupling is available whenever both deterministic sidecars exist.
    seed_refs = {str(row.get("work_id") or identify_work(row)[0]) for row in sidecar.get("references", []) if isinstance(row, Mapping)}
    if seed_refs:
        for path in sorted((root / "01_custody" / "citation_leads").glob("*.yml")):
            candidate_sidecar = read_yaml(path, {}) or {}
            if str(candidate_sidecar.get("source_id") or "") == seed_source_id:
                continue
            candidate_refs = {
                str(row.get("work_id") or identify_work(row)[0])
                for row in candidate_sidecar.get("references", [])
                if isinstance(row, Mapping)
            }
            shared = sorted(seed_refs & candidate_refs)
            if not shared:
                continue
            target_key = str(candidate_sidecar.get("zotero_item_key") or "")
            target = local_by_key.get(target_key)
            if target:
                rows.append(
                    {
                        **work_metadata(target),
                        "local_zotero_item": dict(target),
                        "relation_type": "bibliographic_coupling",
                        "seed_source_id": seed_source_id,
                        "shared_reference_ids": shared,
                    }
                )
    return rows


def _collect_external_neighbors(
    root: Path,
    run_dir: Path,
    request: ExpansionRequest,
    targets: Mapping[str, Sequence[Mapping[str, Any]]],
    observations: dict[tuple[str, str, str], dict[str, Any]],
    mapped_work_ids: set[str],
    provider: ScholarlyGraphProvider,
) -> None:
    cache_path = run_dir / "provider_results.yml"
    cache = read_yaml(cache_path, {}) or {}
    calls = dict(cache.get("calls", {}) or {})
    positive_ids_by_target: dict[str, list[str]] = defaultdict(list)
    depth_two_frontier: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    neighbor_budget, recommendation_budget, depth_two_budget = _external_channel_budgets(
        request.budget,
        request.depth,
    )
    seed_entries: list[tuple[str, Mapping[str, Any]]] = []
    for seed_index in range(max((len(seeds) for seeds in targets.values()), default=0)):
        for target_id in sorted(targets):
            if seed_index < len(targets[target_id]):
                seed_entries.append((target_id, targets[target_id][seed_index]))
    resolution_limit = min(len(seed_entries), neighbor_budget + recommendation_budget)
    resolved_seeds: list[
        tuple[str, Mapping[str, Any], str, Mapping[str, Any], Mapping[str, Any], str]
    ] = []
    for target_id, seed in seed_entries[:resolution_limit]:
        outbound = _outbound_metadata(seed)
        call_id = sha256_text("neighbors|" + json.dumps(outbound, sort_keys=True))
        cached = calls.get(call_id)
        if isinstance(cached, Mapping) and cached.get("resolution_persisted") is True:
            resolved = dict(cached.get("resolved", {}) or {})
            provider_input = dict(cached.get("provider_input", {}) or outbound)
            resolved_work_id = str(cached.get("resolved_work_id") or identify_work(resolved or provider_input)[0])
        else:
            resolved = provider.resolve_work(outbound) or {}
            provider_input = outbound
            if not resolved and seed.get("title"):
                provider_input = _fallback_outbound_metadata(seed)
                resolved = provider.resolve_work(provider_input) or {}
            resolved_work_id = identify_work(resolved or provider_input)[0]
            calls[call_id] = {
                "kind": "neighbor_resolution",
                "resolved": dict(resolved),
                "provider_input": dict(provider_input),
                "resolved_work_id": resolved_work_id,
                "resolution_persisted": True,
                "completed": False,
            }
            _write_provider_cache(cache_path, request.run_id, calls)
        provider_id = str((resolved.get("provider_ids") or {}).get("semantic_scholar") or "") if isinstance(resolved, Mapping) else ""
        if provider_id:
            positive_ids_by_target[target_id].append(provider_id)
        resolved_seeds.append(
            (target_id, seed, call_id, provider_input, resolved, resolved_work_id)
        )
    recommendation_targets = [
        (target_id, positive_ids)
        for target_id, positive_ids in sorted(positive_ids_by_target.items())
        if positive_ids
    ]
    recommendation_remaining = recommendation_budget
    for target_index, (target_id, positive_ids) in enumerate(recommendation_targets):
        if recommendation_remaining <= 0:
            break
        targets_left = len(recommendation_targets) - target_index
        target_budget = max(1, recommendation_remaining // max(1, targets_left))
        if positive_ids:
            normalized_positive_ids = sorted(set(positive_ids))
            call_id = sha256_text("recommendations|" + "|".join(normalized_positive_ids))
            cached = calls.get(call_id)
            if isinstance(cached, Mapping) and isinstance(cached.get("rows"), list):
                rows = cached["rows"]
            else:
                rows = list(provider.recommendations(normalized_positive_ids, limit=target_budget))
                calls[call_id] = {"rows": [dict(row) for row in rows], "completed": True}
                _write_provider_cache(cache_path, request.run_id, calls)
            rows = [dict(row) for row in rows if isinstance(row, Mapping)][:target_budget]
            recommendation_remaining -= len(rows)
            resolved_target_seeds = [
                resolved_seed
                for resolved_target_id, resolved_seed, *_ in resolved_seeds
                if resolved_target_id == target_id
            ]
            seed = resolved_target_seeds[0]
            recommendation_seed_ids = sorted(
                {
                    str(value)
                    for target_seed in resolved_target_seeds
                    for value in (
                        target_seed.get("source_id")
                        or target_seed.get("note_id")
                        or target_seed.get("zotero_item_key"),
                    )
                    if value
                }
            )
            recommendation_source_ids = sorted(
                {
                    str(target_seed.get("source_id"))
                    for target_seed in resolved_target_seeds
                    if target_seed.get("source_id")
                }
            )
            recommendation_cluster_ids = sorted(
                {
                    str(cluster_id)
                    for target_seed in resolved_target_seeds
                    for cluster_id in target_seed.get("_expansion_origin_cluster_ids", [])
                    if cluster_id
                }
            )
            for row in rows:
                work_id, actionability = identify_work(row)
                if work_id not in mapped_work_ids:
                    _accumulate_observation(
                        observations,
                        request=request,
                        target_id=target_id,
                        seed=seed,
                        row={
                            **dict(row),
                            "work_id": work_id,
                            "actionability": actionability,
                            "relation_type": "recommended_similar",
                            "seed_ids": recommendation_seed_ids,
                            "seed_source_ids": recommendation_source_ids,
                            "provider_seed_ids": normalized_positive_ids,
                            "originating_cluster_ids": recommendation_cluster_ids,
                        },
                        provider=provider.name,
                        depth=1,
                    )
    neighbor_remaining = neighbor_budget + recommendation_remaining
    for seed_index, (
        target_id,
        seed,
        call_id,
        provider_input,
        resolved,
        resolved_work_id,
    ) in enumerate(resolved_seeds):
        if neighbor_remaining <= 0:
            break
        seeds_left = len(resolved_seeds) - seed_index
        seed_budget = min(100, max(1, neighbor_remaining // max(1, seeds_left)))
        cached = calls.get(call_id)
        if (
            isinstance(cached, Mapping)
            and cached.get("completed") is True
            and isinstance(cached.get("rows"), list)
        ):
            rows = cached["rows"]
        else:
            rows = _resumable_provider_neighbors(
                provider,
                resolved or provider_input,
                limit=seed_budget,
                calls=calls,
                cache_path=cache_path,
                run_id=request.run_id,
                call_prefix=sha256_text(f"{call_id}|{resolved_work_id}"),
                resolved_work_id=resolved_work_id,
            )
            calls[call_id] = {
                **dict(calls[call_id]),
                "rows": [dict(row) for row in rows],
                "completed": True,
            }
            _write_provider_cache(cache_path, request.run_id, calls)
        rows = [dict(row) for row in rows if isinstance(row, Mapping)][:seed_budget]
        neighbor_remaining -= len(rows)
        for row in rows:
            work_id, actionability = identify_work(row)
            normalized = {**dict(row), "work_id": work_id, "actionability": actionability}
            depth_two_frontier.append((target_id, seed, normalized))
            if work_id in mapped_work_ids:
                continue
            _accumulate_observation(
                observations,
                request=request,
                target_id=target_id,
                seed=seed,
                row=normalized,
                provider=provider.name,
                depth=1,
            )
    depth_two_remaining = depth_two_budget + neighbor_remaining
    if request.depth == 2 and depth_two_remaining > 0:
        frontier_rows = list(
            _round_robin_frontier(
                depth_two_frontier,
                min(25, depth_two_remaining),
            )
        )
        for frontier_index, (target_id, seed, frontier) in enumerate(frontier_rows):
            if depth_two_remaining <= 0:
                break
            frontiers_left = len(frontier_rows) - frontier_index
            frontier_budget = min(25, max(1, depth_two_remaining // max(1, frontiers_left)))
            outbound = _outbound_metadata(frontier)
            frontier_work_id = str(frontier.get("work_id") or identify_work(frontier)[0])
            first_relation = str(frontier.get("relation_type") or "zotero_related")
            call_id = sha256_text("depth2|" + json.dumps(outbound, sort_keys=True))
            cached = calls.get(call_id)
            if isinstance(cached, Mapping) and isinstance(cached.get("rows"), list):
                rows = cached["rows"]
            else:
                rows = _resumable_provider_neighbors(
                    provider,
                    outbound,
                    limit=frontier_budget,
                    calls=calls,
                    cache_path=cache_path,
                    run_id=request.run_id,
                    call_prefix=call_id,
                )
                calls[call_id] = {"rows": [dict(row) for row in rows], "completed": True}
                _write_provider_cache(cache_path, request.run_id, calls)
            rows = [dict(row) for row in rows if isinstance(row, Mapping)][:frontier_budget]
            depth_two_remaining -= len(rows)
            for row in rows:
                work_id, actionability = identify_work(row)
                if work_id not in mapped_work_ids:
                    _accumulate_observation(
                        observations,
                        request=request,
                        target_id=target_id,
                        seed=seed,
                        row={
                            **dict(row),
                            "work_id": work_id,
                            "actionability": actionability,
                            "relation_type": _two_hop_relation(first_relation, str(row.get("relation_type") or "zotero_related")),
                            "provider_path_relation": str(row.get("relation_type") or ""),
                            "path_work_ids": [frontier_work_id, work_id],
                            "path_relation_types": [first_relation, str(row.get("relation_type") or "zotero_related")],
                            "provenance": "bounded_external_two_hop_path",
                            "seed_ids": list(frontier.get("seed_ids", [])),
                            "originating_cluster_ids": list(frontier.get("originating_cluster_ids", [])),
                        },
                        provider=provider.name,
                        depth=2,
                    )


def _external_channel_budgets(total: int, depth: int) -> tuple[int, int, int]:
    """Reserve one request-wide result budget across enabled graph channels."""

    if total <= 1:
        return max(0, total), 0, 0
    if depth != 2:
        neighbors = max(1, (total * 2) // 3)
        return neighbors, total - neighbors, 0
    if total == 2:
        return 1, 1, 0
    neighbors = max(1, total // 2)
    recommendations = max(1, (total - neighbors) // 2)
    return neighbors, recommendations, total - neighbors - recommendations


def _accumulate_observation(
    observations: dict[tuple[str, str, str], dict[str, Any]],
    *,
    request: ExpansionRequest,
    target_id: str,
    seed: Mapping[str, Any],
    row: Mapping[str, Any],
    provider: str,
    depth: int,
) -> None:
    work_id = str(row.get("work_id") or identify_work(row)[0])
    relation = _candidate_relation(str(row.get("relation_type") or "zotero_related"))
    key = (request.scope, target_id, work_id)
    present = observations.setdefault(
        key,
        {
            "metadata": work_metadata(row),
            "actionability": str(row.get("actionability") or identify_work(row)[1]),
            "observations": [],
            "seed_ids": set(),
            "providers": set(),
            "min_depth": depth,
        },
    )
    present["metadata"] = merge_work_metadata(present["metadata"], work_metadata(row))
    if row.get("local_zotero_item"):
        present["metadata"]["local_zotero_item"] = dict(row["local_zotero_item"])
        present["metadata"]["zotero_item_key"] = item_key(row["local_zotero_item"])
    seed_id = str(seed.get("source_id") or seed.get("note_id") or seed.get("zotero_item_key") or "")
    row_seed_ids = {
        str(value)
        for value in (row.get("seed_ids", []) if isinstance(row.get("seed_ids"), list) else [])
        if value
    }
    originating_cluster_ids = row.get("originating_cluster_ids")
    if not isinstance(originating_cluster_ids, list):
        originating_cluster_ids = seed.get("_expansion_origin_cluster_ids", [])
    observation = {
        "relation_type": relation,
        "seed_source_id": str(seed.get("source_id") or ""),
        "seed_note_id": str(seed.get("note_id") or ""),
        "provider": provider,
        "depth": depth,
        "provenance": str(row.get("provenance") or ("scholarly_graph_provider" if provider != "internal" else "local_graph")),
        "provider_relevance": _bounded_float(row.get("provider_relevance", 1.0)),
        "originating_cluster_ids": sorted(
            {str(value) for value in originating_cluster_ids if value}
        ),
    }
    for optional in (
        "shared_tags",
        "shared_reference_ids",
        "path_work_ids",
        "path_relation_types",
        "provider_path_relation",
        "seed_source_ids",
        "provider_seed_ids",
    ):
        if row.get(optional):
            observation[optional] = list(row[optional]) if isinstance(row[optional], (list, tuple, set)) else row[optional]
    identity = json.dumps(observation, sort_keys=True)
    if identity not in {json.dumps(value, sort_keys=True) for value in present["observations"]}:
        present["observations"].append(observation)
    if seed_id:
        present["seed_ids"].add(seed_id)
    present["seed_ids"].update(row_seed_ids)
    present["providers"].add(provider)
    present["min_depth"] = min(int(present["min_depth"]), depth)


def _materialize_candidates(
    request: ExpansionRequest,
    targets: Mapping[str, Sequence[Mapping[str, Any]]],
    observations: Mapping[tuple[str, str, str], Mapping[str, Any]],
    existing: Mapping[tuple[str, str, str], ExpansionCandidate],
    local_items: Sequence[Mapping[str, Any]],
    target_associations: Mapping[str, tuple[Sequence[str], Sequence[str]]],
) -> list[ExpansionCandidate]:
    candidates: list[ExpansionCandidate] = []
    identity_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    local_by_work: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in local_items:
        metadata = work_metadata(item)
        local_by_work[identify_work(item)[0]].append(item)
        key = bibliographic_tuple(metadata)
        if key[0]:
            identity_counts[key] += 1
    for (scope, target_id, work_id), value in observations.items():
        metadata = dict(value["metadata"])
        local_matches = local_by_work.get(work_id, [])
        if len(local_matches) == 1:
            metadata = merge_work_metadata(metadata, work_metadata(local_matches[0]))
            metadata["local_zotero_item"] = dict(local_matches[0])
            metadata["zotero_item_key"] = item_key(local_matches[0])
        rows = sorted(value["observations"], key=lambda row: (int(row.get("depth", 1)), -RELATION_STRENGTH.get(str(row.get("relation_type")), 0.0), str(row.get("seed_source_id"))))
        relation_types = {str(row.get("relation_type")) for row in rows}
        seed_ids = sorted(value["seed_ids"])
        primary = max(
            relation_types,
            key=lambda relation: (
                RELATION_STRENGTH.get(relation, 0.0),
                -RELATION_PRIORITY.index(relation) if relation in RELATION_PRIORITY else -99,
                relation,
            ),
        )
        present = existing.get((scope, target_id, work_id))
        if present is None:
            identity_key = bibliographic_tuple(metadata)
            matching_existing = [
                candidate
                for (candidate_scope, candidate_target, _), candidate in existing.items()
                if candidate_scope == scope
                and candidate_target == target_id
                and all(identity_key)
                and bibliographic_tuple(candidate.to_dict()) == identity_key
            ]
            if len(matching_existing) == 1:
                present = matching_existing[0]
        if present:
            suggestion_id = present.suggestion_id
            primary = present.primary_relation
        else:
            suggestion_id = f"suggestion-{sha256_text('|'.join((work_id, scope, target_id, primary)))[:20]}"
        actionability = str(value.get("actionability") or "resolve_identity")
        identity_key = bibliographic_tuple(metadata)
        if work_id.startswith("work-title-"):
            actionability = "ready" if identity_counts.get(identity_key, 0) == 1 else "resolve_identity"
        seed_count = max(1, len(targets.get(target_id, ())))
        ranking = _ranking(metadata, rows, len(seed_ids), seed_count, int(value["min_depth"]), request.scope)
        now = now_iso()
        target_cluster_ids, related_gap_ids = target_associations.get(target_id, ((), ()))
        observation_cluster_ids = sorted(
            {
                str(cluster_id)
                for row in rows
                for cluster_id in row.get("originating_cluster_ids", [])
                if cluster_id
            }
        )
        related_cluster_ids = observation_cluster_ids or list(target_cluster_ids)
        candidates.append(
            ExpansionCandidate(
                work_id=work_id,
                suggestion_id=suggestion_id,
                title=str(metadata.get("title") or ""),
                year=str(metadata.get("year") or ""),
                authors=list(metadata.get("authors") or []),
                doi=str(metadata.get("doi") or ""),
                url=str(metadata.get("url") or ""),
                isbn=str(metadata.get("isbn") or ""),
                provider_ids=dict(metadata.get("provider_ids") or {}),
                zotero_item_key=str(metadata.get("zotero_item_key") or ""),
                local_zotero_item=dict(metadata.get("local_zotero_item") or {}),
                target_scope=scope,
                target_id=target_id,
                target_ids=[target_id],
                primary_relation=primary,
                observations=rows,
                related_source_ids=seed_ids,
                related_cluster_ids=list(related_cluster_ids),
                related_gap_ids=list(related_gap_ids),
                ranking=ranking,
                score=ranking["score"],
                depth=int(value["min_depth"]),
                provider="semantic-scholar" if "semantic-scholar" in value["providers"] else "internal",
                actionability=actionability,
                state=present.state if present else "proposed",
                fulfillment=present.fulfillment if present else "not_started",
                decision_version=present.decision_version if present else 0,
                created_at=present.created_at if present else now,
                updated_at=now,
            )
        )
    return sorted(candidates, key=lambda row: (row.target_id, -row.score, row.work_id))


def _reconcile_observation_identities(
    observations: Mapping[tuple[str, str, str], Mapping[str, Any]],
    local_items: Sequence[Mapping[str, Any]],
    existing: Sequence[ExpansionCandidate],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Canonicalize one publication globally while retaining target-specific suggestions."""

    groups: dict[
        tuple[str, str, str],
        list[tuple[tuple[str, str, str], Mapping[str, Any]]],
    ] = defaultdict(list)
    passthrough: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, value in observations.items():
        identity = bibliographic_tuple(value.get("metadata", {}))
        if all(identity):
            groups[identity].append((key, value))
        else:
            passthrough[key] = _copy_observation_value(value)

    local_by_tuple: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in local_items:
        identity = bibliographic_tuple(item)
        if all(identity):
            local_by_tuple[identity].append(item)
    existing_by_tuple: dict[tuple[str, str, str], list[ExpansionCandidate]] = defaultdict(list)
    for candidate in existing:
        identity = bibliographic_tuple(candidate.to_dict())
        if all(identity):
            existing_by_tuple[identity].append(candidate)

    for identity, entries in groups.items():
        local_matches = local_by_tuple.get(identity, [])
        existing_matches = existing_by_tuple.get(identity, [])
        metadata_rows = [dict(value.get("metadata", {})) for _, value in entries]
        metadata_rows.extend(work_metadata(candidate.to_dict()) for candidate in existing_matches)
        metadata_rows.extend(work_metadata(item) for item in local_matches)
        observed_work_ids = [key[2] for key, _ in entries]
        observed_work_ids.extend(candidate.work_id for candidate in existing_matches)
        observed_work_ids.extend(identify_work(item)[0] for item in local_matches)
        if _strong_identity_conflict(metadata_rows, observed_work_ids, local_matches):
            for key, value in entries:
                copied = _copy_observation_value(value)
                copied["actionability"] = "resolve_identity"
                passthrough[key] = copied
            continue

        identity_rows: list[tuple[str, Mapping[str, Any]]] = [
            (key[2], value.get("metadata", {})) for key, value in entries
        ]
        identity_rows.extend(
            (candidate.work_id, work_metadata(candidate.to_dict()))
            for candidate in existing_matches
        )
        if local_matches:
            identity_rows.append((identify_work(local_matches[0])[0], work_metadata(local_matches[0])))
        identity_rows.extend(
            (identify_work(metadata)[0], metadata)
            for metadata in metadata_rows
        )
        chosen_work_id = min(
            (work_id for work_id, _ in identity_rows),
            key=_work_identity_priority,
        )
        global_metadata: dict[str, Any] = {}
        for _, metadata in sorted(
            identity_rows,
            key=lambda row: (_work_identity_priority(row[0]), json.dumps(dict(row[1]), sort_keys=True)),
        ):
            global_metadata = merge_work_metadata(global_metadata, metadata)

        entries_by_target: dict[
            tuple[str, str],
            list[tuple[tuple[str, str, str], Mapping[str, Any]]],
        ] = defaultdict(list)
        for key, value in entries:
            entries_by_target[(key[0], key[1])].append((key, value))
        for (scope, target_id), target_entries in entries_by_target.items():
            merged = _copy_observation_value(target_entries[0][1])
            for _, value in target_entries[1:]:
                merged["metadata"] = merge_work_metadata(merged["metadata"], value.get("metadata", {}))
                merged["observations"] = _unique_mappings(
                    [*merged["observations"], *value.get("observations", [])]
                )
                merged["seed_ids"].update(value.get("seed_ids", set()))
                merged["providers"].update(value.get("providers", set()))
                merged["min_depth"] = min(int(merged["min_depth"]), int(value.get("min_depth", 1)))
                if value.get("actionability") == "ready":
                    merged["actionability"] = "ready"
            merged["metadata"] = merge_work_metadata(global_metadata, merged["metadata"])
            if local_matches:
                merged["metadata"]["local_zotero_item"] = dict(local_matches[0])
                merged["metadata"]["zotero_item_key"] = item_key(local_matches[0])
            if not chosen_work_id.startswith(("work-title-", "work-unresolved-")):
                merged["actionability"] = "ready"
            passthrough[(scope, target_id, chosen_work_id)] = merged
    return passthrough


def _copy_observation_value(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "metadata": dict(value.get("metadata", {})),
        "actionability": str(value.get("actionability") or "resolve_identity"),
        "observations": [dict(row) for row in value.get("observations", []) if isinstance(row, Mapping)],
        "seed_ids": set(value.get("seed_ids", set())),
        "providers": set(value.get("providers", set())),
        "min_depth": int(value.get("min_depth", 1)),
    }


def _preserve_candidate_state(
    discovered: ExpansionCandidate,
    current: ExpansionCandidate,
) -> ExpansionCandidate:
    return ExpansionCandidate.from_dict(
        {
            **discovered.to_dict(),
            "suggestion_id": current.suggestion_id,
            "state": current.state,
            "fulfillment": current.fulfillment,
            "decision_version": current.decision_version,
            "created_at": current.created_at or discovered.created_at,
        }
    )


def _canonicalize_existing_candidates(
    existing: Sequence[ExpansionCandidate],
    discovered: Sequence[ExpansionCandidate],
) -> list[ExpansionCandidate]:
    canonical_by_tuple: dict[tuple[str, str, str], ExpansionCandidate] = {}
    conflicted: set[tuple[str, str, str]] = set()
    for candidate in discovered:
        identity = bibliographic_tuple(candidate.to_dict())
        if not all(identity) or candidate.actionability != "ready":
            continue
        present = canonical_by_tuple.get(identity)
        if present is None:
            canonical_by_tuple[identity] = candidate
        elif present.work_id != candidate.work_id:
            conflicted.add(identity)

    updated: list[ExpansionCandidate] = []
    for candidate in existing:
        identity = bibliographic_tuple(candidate.to_dict())
        canonical = canonical_by_tuple.get(identity)
        if canonical is None or identity in conflicted:
            updated.append(candidate)
            continue
        if _strong_identity_conflict(
            [work_metadata(candidate.to_dict()), work_metadata(canonical.to_dict())],
            [candidate.work_id, canonical.work_id],
            [],
        ):
            updated.append(candidate)
            continue
        metadata = merge_work_metadata(canonical.to_dict(), candidate.to_dict())
        updated.append(
            ExpansionCandidate.from_dict(
                {
                    **candidate.to_dict(),
                    "work_id": canonical.work_id,
                    "title": metadata.get("title", candidate.title),
                    "year": metadata.get("year", candidate.year),
                    "authors": metadata.get("authors", candidate.authors),
                    "doi": metadata.get("doi", candidate.doi),
                    "url": metadata.get("url", candidate.url),
                    "isbn": metadata.get("isbn", candidate.isbn),
                    "provider_ids": metadata.get("provider_ids", candidate.provider_ids),
                    "zotero_item_key": metadata.get("zotero_item_key", candidate.zotero_item_key),
                    "local_zotero_item": metadata.get("local_zotero_item", candidate.local_zotero_item),
                }
            )
        )
    return updated


def _strong_identity_conflict(
    metadata_rows: Sequence[Mapping[str, Any]],
    work_ids: Sequence[str],
    local_matches: Sequence[Mapping[str, Any]],
) -> bool:
    if len({item_key(item) for item in local_matches if item_key(item)}) > 1:
        return True
    for field in ("doi", "url", "isbn"):
        if len({str(row.get(field)) for row in metadata_rows if row.get(field)}) > 1:
            return True
    provider_values: dict[str, set[str]] = defaultdict(set)
    for row in metadata_rows:
        providers = row.get("provider_ids", {})
        if isinstance(providers, Mapping):
            for provider, value in providers.items():
                if value:
                    provider_values[str(provider)].add(str(value))
    if any(len(values) > 1 for values in provider_values.values()):
        return True
    for prefix in ("work-doi-", "work-s2-", "work-url-", "work-isbn-"):
        if len({work_id for work_id in work_ids if work_id.startswith(prefix)}) > 1:
            return True
    return False


def _work_identity_priority(work_id: str) -> tuple[int, str]:
    prefixes = ("work-doi-", "work-s2-", "work-url-", "work-isbn-", "work-title-", "work-unresolved-")
    return (next((index for index, prefix in enumerate(prefixes) if work_id.startswith(prefix)), len(prefixes)), work_id)


def _unique_mappings(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = dict(row)
        identity = json.dumps(value, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def _ranking(
    metadata: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    covered_seeds: int,
    seed_count: int,
    depth: int,
    scope: str,
) -> dict[str, float]:
    relation = max((RELATION_STRENGTH.get(str(row.get("relation_type")), 0.0) for row in observations), default=0.0)
    coverage = min(1.0, covered_seeds / max(seed_count, 1))
    provider_relevance = max((_bounded_float(row.get("provider_relevance", 1.0)) for row in observations), default=1.0)
    target_relevance = 1.0 if scope in {"cluster", "gap"} else 0.8
    local = 1.0 if metadata.get("zotero_item_key") else 0.0
    complete = sum(bool(metadata.get(field)) for field in ("title", "year", "authors", "doi", "url")) / 5
    depth_multiplier = 1.0 if depth == 1 else 0.75
    base = 0.30 * relation + 0.20 * coverage + 0.20 * provider_relevance + 0.15 * target_relevance + 0.10 * local + 0.05 * complete
    return {
        "relation_strength": round(relation, 6),
        "seed_coverage": round(coverage, 6),
        "provider_relevance": round(provider_relevance, 6),
        "target_relevance": round(target_relevance, 6),
        "local_zotero_availability": round(local, 6),
        "metadata_completeness": round(complete, 6),
        "depth_multiplier": depth_multiplier,
        "score": round(base * depth_multiplier, 6),
    }


def _round_robin_frontier(
    rows: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    limit: int,
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    ordered_rows = sorted(
        rows,
        key=lambda value: (
            value[0],
            str(value[2].get("work_id") or identify_work(value[2])[0]),
            str(value[1].get("source_id") or value[1].get("note_id") or ""),
        ),
    )
    for target_id, seed, frontier in ordered_rows:
        work_id = str(frontier.get("work_id") or identify_work(frontier)[0])
        identity = "|".join((target_id, work_id))
        entry = grouped.setdefault(
            identity,
            {
                "target_id": target_id,
                "seed": dict(seed),
                "frontier": dict(frontier),
                "seed_ids": set(),
                "origins": set(),
            },
        )
        seed_id = str(seed.get("source_id") or seed.get("note_id") or seed.get("zotero_item_key") or "")
        if seed_id:
            entry["seed_ids"].add(seed_id)
        entry["origins"].update(
            str(value)
            for value in seed.get("_expansion_origin_cluster_ids", [])
            if value
        )

    buckets: dict[
        tuple[str, str],
        list[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ] = defaultdict(list)
    origins_by_identity: dict[str, tuple[str, ...]] = {}
    for identity, entry in grouped.items():
        target_id = str(entry["target_id"])
        origins = tuple(sorted(entry["origins"]))
        seed = {
            **dict(entry["seed"]),
            "_expansion_origin_cluster_ids": list(origins),
        }
        frontier = {
            **dict(entry["frontier"]),
            "seed_ids": sorted(entry["seed_ids"]),
            "originating_cluster_ids": list(origins),
        }
        row = (target_id, seed, frontier)
        origins_by_identity[identity] = origins
        for bucket in origins or (f"target:{target_id}",):
            buckets[(target_id, bucket)].append(row)
    for key, bucket_rows in buckets.items():
        buckets[key] = sorted(
            bucket_rows,
            key=lambda value: (
                str(value[2].get("work_id") or identify_work(value[2])[0]),
                str(value[1].get("source_id") or value[1].get("note_id") or ""),
            ),
        )

    selected: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    selected_ids: set[str] = set()
    cluster_counts: dict[str, int] = defaultdict(int)
    cursors: dict[tuple[str, str], int] = defaultdict(int)
    bucket_keys = sorted(buckets)
    while len(selected) < limit:
        added = False
        for bucket_key in bucket_keys:
            bucket = buckets[bucket_key]
            while cursors[bucket_key] < len(bucket):
                row = bucket[cursors[bucket_key]]
                cursors[bucket_key] += 1
                target_id, _, frontier = row
                work_id = str(frontier.get("work_id") or identify_work(frontier)[0])
                identity = "|".join((target_id, work_id))
                if identity in selected_ids:
                    continue
                origins = origins_by_identity[identity]
                if origins and any(cluster_counts[cluster_id] >= 25 for cluster_id in origins):
                    continue
                selected.append(row)
                selected_ids.add(identity)
                for cluster_id in origins:
                    cluster_counts[cluster_id] += 1
                added = True
                break
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


def _bounded_candidates(candidates: Sequence[ExpansionCandidate], request: ExpansionRequest) -> tuple[list[ExpansionCandidate], bool]:
    buckets: dict[tuple[str, str], list[ExpansionCandidate]] = defaultdict(list)
    origins_by_suggestion: dict[str, tuple[str, ...]] = {}
    for row in candidates:
        origins = tuple(
            sorted(
                {
                    str(cluster_id)
                    for observation in row.observations
                    for cluster_id in observation.get("originating_cluster_ids", [])
                    if cluster_id
                }
            )
        )
        if not origins and row.target_scope == "cluster":
            origins = (row.target_id,)
        origins_by_suggestion[row.suggestion_id] = origins
        for bucket in origins or (f"target:{row.target_id}",):
            buckets[(row.target_id, bucket)].append(row)
    for key, bucket_rows in buckets.items():
        buckets[key] = sorted(bucket_rows, key=lambda row: (-row.score, row.work_id, row.suggestion_id))

    selected: list[ExpansionCandidate] = []
    selected_ids: set[str] = set()
    cluster_counts: dict[str, int] = defaultdict(int)
    cursors: dict[tuple[str, str], int] = defaultdict(int)
    bucket_keys = sorted(buckets)
    while len(selected) < request.budget:
        added = False
        for bucket_key in bucket_keys:
            bucket = buckets[bucket_key]
            while cursors[bucket_key] < len(bucket):
                candidate = bucket[cursors[bucket_key]]
                cursors[bucket_key] += 1
                if candidate.suggestion_id in selected_ids:
                    continue
                origins = origins_by_suggestion[candidate.suggestion_id]
                if origins and any(cluster_counts[cluster_id] >= 25 for cluster_id in origins):
                    continue
                selected.append(candidate)
                selected_ids.add(candidate.suggestion_id)
                for cluster_id in origins:
                    cluster_counts[cluster_id] += 1
                added = True
                break
            if len(selected) >= request.budget:
                break
        if not added:
            break
    return selected, len(selected) < len(candidates)


def _apply_controller_reviews(root: Path, candidates: Sequence[ExpansionCandidate], controller: ExpansionControllerPort) -> None:
    if not candidates:
        return
    try:
        provided = [dict(row) for row in controller.review_expansion_candidates([row.to_dict() for row in candidates])]
    except Exception:
        provided = []
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in provided:
        if row.get("suggestion_id"):
            by_id[str(row["suggestion_id"])].append(row)
    for candidate in candidates:
        matches = by_id.get(candidate.suggestion_id, [])
        expected_matches = (
            len(matches) == 1
            and matches[0].get("decision") in {"accepted", "parked", "rejected"}
            and _controller_version_matches(matches[0], candidate.decision_version)
        )
        if expected_matches:
            row = matches[0]
            decision = str(row["decision"])
            reason = str(row.get("decision_reason") or row.get("reason") or "controller_review")
            actor = str(row.get("actor") or "controller")
        else:
            decision = "parked"
            reason = "controller_returned_no_unique_valid_decision"
            actor = "controller"
        if decision == "accepted" and candidate.actionability == "resolve_identity":
            decision = "parked"
            reason = "controller_cannot_accept_unresolved_identity"
        decide_expansion(
            root,
            ExpansionDecision(candidate.suggestion_id, candidate.decision_version, decision, reason, actor),  # type: ignore[arg-type]
        )


def _controller_decision(
    candidate: ExpansionCandidate,
    requested: ExpansionDecision,
    controller: ExpansionControllerPort,
) -> ExpansionDecision:
    payload = {**candidate.to_dict(), "requested_decision": requested.to_dict()}
    try:
        rows = [dict(row) for row in controller.review_expansion_candidates([payload])]
    except Exception:
        rows = []
    matches = [row for row in rows if str(row.get("suggestion_id") or "") == candidate.suggestion_id]
    if (
        len(matches) == 1
        and matches[0].get("decision") in {"accepted", "parked", "rejected"}
        and _controller_version_matches(matches[0], candidate.decision_version)
    ):
        row = matches[0]
        decision = str(row["decision"])
        reason = str(row.get("decision_reason") or row.get("reason") or "controller_review")
        if decision == "accepted" and candidate.actionability == "resolve_identity":
            decision = "parked"
            reason = "controller_cannot_accept_unresolved_identity"
        return ExpansionDecision(
            candidate.suggestion_id,
            candidate.decision_version,
            decision,  # type: ignore[arg-type]
            reason,
            str(row.get("actor") or "controller"),
        )
    return ExpansionDecision(
        candidate.suggestion_id,
        candidate.decision_version,
        "parked",
        "controller_returned_no_unique_valid_decision",
        "controller",
    )


def _write_candidates(root: Path, candidates: Sequence[ExpansionCandidate]) -> None:
    path = root / CANDIDATES_RELATIVE
    write_yaml(
        path,
        {
            "engine_version": ENGINE_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "updated_at": now_iso(),
            "candidates": [row.to_dict() for row in sorted(candidates, key=lambda value: value.suggestion_id)],
        },
    )


def _load_candidates(root: Path) -> list[ExpansionCandidate]:
    payload = _read_registry(root / CANDIDATES_RELATIVE, "candidates")
    rows = payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else []
    candidates = [ExpansionCandidate.from_dict(row) for row in rows if isinstance(row, Mapping)]
    latest: dict[str, dict[str, Any]] = {}
    for decision in _load_decision_rows(root):
        suggestion_id = str(decision.get("suggestion_id") or "")
        try:
            version = int(decision.get("decision_version", 0))
        except (TypeError, ValueError):
            continue
        if decision.get("decision") not in {"accepted", "parked", "rejected"}:
            continue
        if not suggestion_id or version < 1:
            continue
        if version > int(latest.get(suggestion_id, {}).get("decision_version", 0)):
            latest[suggestion_id] = dict(decision)
    derived: list[ExpansionCandidate] = []
    for candidate in candidates:
        decision = latest.get(candidate.suggestion_id)
        state = str(decision.get("decision")) if decision else "proposed"
        version = int(decision.get("decision_version", 0)) if decision else 0
        derived.append(
            ExpansionCandidate.from_dict(
                {**candidate.to_dict(), "state": state, "decision_version": version}
            )
        )
    return derived


def _load_decision_rows(root: Path) -> list[dict[str, Any]]:
    payload = _read_registry(root / DECISIONS_RELATIVE, "decisions")
    rows = payload.get("decisions", []) if isinstance(payload.get("decisions"), list) else []
    decisions = [dict(row) for row in rows if isinstance(row, Mapping)]
    by_suggestion: dict[str, list[int]] = defaultdict(list)
    decision_ids: set[str] = set()
    for row in decisions:
        suggestion_id = str(row.get("suggestion_id") or "")
        decision_id = str(row.get("decision_id") or "")
        if not suggestion_id or row.get("decision") not in {"accepted", "parked", "rejected"}:
            raise RuntimeError("expansion decision ledger contains a malformed decision")
        try:
            version = int(row.get("decision_version"))
            expected = int(row.get("expected_version"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("expansion decision ledger contains an invalid version") from exc
        if version < 1 or expected != version - 1:
            raise RuntimeError("expansion decision ledger contains a stale or skipped version")
        if decision_id and decision_id in decision_ids:
            raise RuntimeError("expansion decision ledger contains a duplicate decision_id")
        if decision_id:
            decision_ids.add(decision_id)
        by_suggestion[suggestion_id].append(version)
    for versions in by_suggestion.values():
        ordered = sorted(versions)
        if ordered != list(range(1, max(ordered) + 1)):
            raise RuntimeError("expansion decision ledger contains a missing or duplicate version")
    return decisions


def _read_registry(path: Path, field: str) -> dict[str, Any]:
    if not path.exists():
        return {field: []}
    payload = read_yaml(path, {}) or {}
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{path.name} must contain a mapping")
    schema = str(payload.get("artifact_schema_version") or "")
    if schema and schema != "1.1":
        raise RuntimeError(f"{path.name} uses unsupported artifact schema {schema}")
    rows = payload.get(field, [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{path.name} field {field} must be a list")
    return dict(payload)


def _write_decisions(root: Path, decisions: Sequence[Mapping[str, Any]]) -> None:
    write_yaml(
        root / DECISIONS_RELATIVE,
        {
            "engine_version": ENGINE_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "updated_at": now_iso(),
            "decisions": [dict(row) for row in decisions],
        },
    )


def _render_candidate_markdown(candidate: ExpansionCandidate) -> str:
    front = {
        "generated": True,
        "canonical_state": "03_literature_synthesis/expansion/candidates.yml",
        "suggestion_id": candidate.suggestion_id,
        "work_id": candidate.work_id,
        "state": candidate.state,
        "fulfillment": candidate.fulfillment,
        "decision_version": candidate.decision_version,
        "target_scope": candidate.target_scope,
        "target_id": candidate.target_id,
        "primary_relation": candidate.primary_relation,
        "score": candidate.score,
        "related_source_ids": candidate.related_source_ids,
        "related_cluster_ids": candidate.related_cluster_ids,
        "related_gap_ids": candidate.related_gap_ids,
    }
    import yaml

    links: list[str] = []
    if candidate.doi:
        links.append(f"- DOI: [{candidate.doi}](https://doi.org/{candidate.doi})")
    if candidate.url:
        safe_url = _safe_external_url(candidate.url)
        if safe_url:
            links.append(f"- URL: [External URL]({safe_url})")
    observations = [
        f"- `{row.get('relation_type')}` from `{row.get('seed_source_id') or row.get('seed_source_ids') or 'provider'}` via `{row.get('provenance')}`"
        for row in candidate.observations
    ]
    return (
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + f"# {_safe_markdown_title(candidate.title or candidate.work_id)}\n\n"
        + f"- State: **{candidate.state}**\n"
        + f"- Actionability: `{candidate.actionability}`\n"
        + f"- Score: `{candidate.score:.6f}`\n"
        + ("\n".join(links) + "\n" if links else "")
        + "\n## Why this was suggested\n\n"
        + ("\n".join(observations) if observations else "No observations recorded.")
        + "\n\nThis is a generated suggestion, not inspected evidence or a novelty claim.\n"
    )


def _mark_graph_source_set(root: Path, report: RunReport, suggestion_ids: Sequence[str]) -> None:
    path_value = report.source_set.get("path") if isinstance(report.source_set, Mapping) else ""
    if not path_value:
        return
    path = Path(str(path_value))
    payload = read_yaml(path, {}) or {}
    payload.update(
        {
            "source_set_type": "citation_followup_batch",
            "upstream_scope": {"kind": "graph_expansion", "id": report.run_id, "suggestion_ids": list(suggestion_ids)},
            "originating_suggestion_ids": list(suggestion_ids),
            "updated_at": now_iso(),
        }
    )
    write_yaml(path, payload)
    payload["path"] = str(path)
    report.source_set = payload
    if report.artifact_manifest is not None:
        report.artifact_manifest.metadata["source_set"] = payload
        manifest_paths: list[Path] = []
        for row in report.artifact_manifest.artifacts:
            value = Path(str(row.get("path") or ""))
            if value:
                manifest_paths.append(value if value.is_absolute() else root / value)
        if path not in manifest_paths:
            manifest_paths.append(path)
        report.artifact_manifest.artifacts = artifact_rows(root, manifest_paths)
        write_yaml(run_directory(root, report.run_id) / "artifact_manifest.yml", report.artifact_manifest.to_dict())
    run_dir = run_directory(root, report.run_id)
    write_yaml(run_dir / "run_report.yml", report.to_dict())


def _render_bibtex(candidates: Sequence[ExpansionCandidate]) -> str:
    blocks: list[str] = []
    for row in candidates:
        fields = {
            "title": row.title,
            "author": " and ".join(row.authors),
            "year": row.year,
            "doi": row.doi,
            "url": row.url,
        }
        lines = [f"@article{{az_{row.work_id.rsplit('-', 1)[-1]},"]
        lines.extend(f"  {key} = {{{_bib_escape(value)}}}," for key, value in fields.items() if value)
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _render_ris(candidates: Sequence[ExpansionCandidate]) -> str:
    lines: list[str] = []
    for row in candidates:
        lines.extend(["TY  - JOUR", f"TI  - {row.title}"])
        lines.extend(f"AU  - {author}" for author in row.authors)
        if row.year:
            lines.append(f"PY  - {row.year}")
        if row.doi:
            lines.append(f"DO  - {row.doi}")
        if row.url:
            lines.append(f"UR  - {row.url}")
        lines.extend(["ER  -", ""])
    return "\n".join(lines)


def _match_local(reference: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    reference_id, _ = identify_work(reference)
    exact = [item for item in items if identify_work(item)[0] == reference_id]
    if len(exact) == 1:
        return exact[0]
    ref = work_metadata(reference)
    title_key = bibliographic_tuple(ref)
    matches = []
    for item in items:
        data = work_metadata(item)
        key = bibliographic_tuple(data)
        if title_key[0] and key == title_key:
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _candidate_item_compatible(
    candidate: ExpansionCandidate,
    item: Mapping[str, Any],
) -> bool:
    candidate_metadata = work_metadata(candidate.to_dict())
    item_metadata = work_metadata(item)
    item_work_id = identify_work(item)[0]
    if _strong_identity_conflict(
        [candidate_metadata, item_metadata],
        [candidate.work_id, item_work_id],
        [],
    ):
        return False
    if candidate.work_id == item_work_id:
        return True
    for field in ("doi", "url", "isbn"):
        if candidate_metadata.get(field) and candidate_metadata.get(field) == item_metadata.get(field):
            return True
    candidate_providers = candidate_metadata.get("provider_ids", {})
    item_providers = item_metadata.get("provider_ids", {})
    if isinstance(candidate_providers, Mapping) and isinstance(item_providers, Mapping):
        if any(
            candidate_providers.get(provider) == value
            for provider, value in item_providers.items()
            if value and candidate_providers.get(provider)
        ):
            return True
    candidate_tuple = bibliographic_tuple(candidate_metadata)
    return all(candidate_tuple) and candidate_tuple == bibliographic_tuple(item_metadata)


def _safe_external_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value).strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@-._~!$&'*+,;=")
    query = urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&;,:@/?-._~!$'*+")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, query, ""))


def _outbound_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = work_metadata(value)
    if (metadata.get("provider_ids") or {}).get("semantic_scholar"):
        return {"provider_ids": {"semantic_scholar": metadata["provider_ids"]["semantic_scholar"]}}
    if metadata.get("doi"):
        return {"doi": metadata["doi"]}
    if metadata.get("url"):
        return {"url": metadata["url"]}
    if metadata.get("isbn"):
        return {"isbn": metadata["isbn"]}
    return _fallback_outbound_metadata(metadata)


def _fallback_outbound_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = work_metadata(value)
    return {
        key: metadata[key]
        for key in ("title", "authors", "year")
        if metadata.get(key)
    }


def _zotero_relation_type(predicate: str) -> str:
    normalized = predicate.casefold()
    if "isreferencedby" in normalized or "cited_by" in normalized:
        return "cited_by"
    if "references" in normalized or "cites" in normalized:
        return "cites"
    return "zotero_related"


def _reverse_relation(relation: str) -> str:
    if relation == "cites":
        return "cited_by"
    if relation == "cited_by":
        return "cites"
    return relation


def _candidate_relation(relation: str) -> str:
    return relation if relation in RELATION_STRENGTH else "zotero_related"


def _two_hop_relation(first: str, second: str) -> str:
    if first == "cites" and second == "cited_by":
        return "bibliographic_coupling"
    if first == "cited_by" and second == "cites":
        return "co_cited_with"
    return "citation_path"


def _request_identity(payload: Mapping[str, Any]) -> str:
    relevant = {key: payload.get(key) for key in ("scope", "target_ids", "provider", "depth", "budget")}
    return sha256_text(json.dumps(relevant, sort_keys=True))


def _write_provider_cache(path: Path, run_id: str | None, calls: Mapping[str, Any]) -> None:
    write_yaml(
        path,
        {
            "run_id": run_id or "",
            "engine_version": ENGINE_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "calls": dict(calls),
        },
    )


def _resumable_provider_neighbors(
    provider: ScholarlyGraphProvider,
    work: Mapping[str, Any],
    *,
    limit: int,
    calls: dict[str, Any],
    cache_path: Path,
    run_id: str | None,
    call_prefix: str,
    resolved_work_id: str | None = None,
) -> list[dict[str, Any]]:
    page_method = getattr(provider, "citation_neighbors_page", None)
    if not callable(page_method):
        return [dict(row) for row in provider.citation_neighbors(work, limit=limit)]
    routes = ("citations", "references")
    route_results: dict[str, list[dict[str, Any]]] = {route: [] for route in routes}
    route_cursors: dict[str, str | None] = {route: None for route in routes}
    route_done = {route: False for route in routes}
    route_pages = {route: 0 for route in routes}
    bound_work_id = resolved_work_id or identify_work(work)[0]

    def fill_route(relation: str, target_count: int) -> None:
        rows = route_results[relation]
        while len(rows) < target_count and not route_done[relation] and route_pages[relation] < 100:
            cursor = route_cursors[relation]
            page_limit = min(100, target_count - len(rows))
            page_id = sha256_text(
                "|".join((call_prefix, bound_work_id, relation, cursor or "0", str(page_limit)))
            )
            cached = calls.get(page_id)
            if (
                isinstance(cached, Mapping)
                and cached.get("kind") == "citation_page"
                and cached.get("resolved_work_id") == bound_work_id
            ):
                page = cached
            else:
                page = dict(
                    page_method(
                        work,
                        relation=relation,
                        cursor=cursor,
                        limit=page_limit,
                    )
                )
                calls[page_id] = {
                    "kind": "citation_page",
                    "resolved_work_id": bound_work_id,
                    "relation": relation,
                    "cursor": cursor or "0",
                    "limit": page_limit,
                    "rows": [dict(row) for row in page.get("rows", []) if isinstance(row, Mapping)],
                    "next_cursor": page.get("next_cursor"),
                    "done": bool(page.get("done")),
                    "completed": True,
                }
                _write_provider_cache(cache_path, run_id, calls)
                page = calls[page_id]
            remaining = target_count - len(rows)
            page_rows = [dict(row) for row in page.get("rows", []) if isinstance(row, Mapping)]
            rows.extend(page_rows[:remaining])
            next_cursor = str(page.get("next_cursor")) if page.get("next_cursor") is not None else None
            route_cursors[relation] = next_cursor
            route_pages[relation] += 1
            route_done[relation] = bool(page.get("done")) or next_cursor is None or next_cursor == cursor

    initial_targets = {
        "citations": (limit + 1) // 2,
        "references": limit // 2,
    }
    for route in routes:
        fill_route(route, initial_targets[route])

    while sum(len(rows) for rows in route_results.values()) < limit:
        before = sum(len(rows) for rows in route_results.values())
        for route in routes:
            if route_done[route]:
                continue
            remaining = limit - sum(len(rows) for rows in route_results.values())
            fill_route(route, len(route_results[route]) + remaining)
            if sum(len(rows) for rows in route_results.values()) >= limit:
                break
        if sum(len(rows) for rows in route_results.values()) == before:
            break

    interleaved: list[dict[str, Any]] = []
    for index in range(max((len(route_results[route]) for route in routes), default=0)):
        for route in routes:
            if index < len(route_results[route]):
                interleaved.append(route_results[route][index])
    return interleaved[:limit]


def _merge_attempts(
    existing: Sequence[Mapping[str, Any]],
    additions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*existing, *additions]:
        payload = dict(row)
        identity = json.dumps(
            {
                key: payload.get(key)
                for key in ("provider", "endpoint", "method", "request_hash", "response_hash", "status", "attempt")
            },
            sort_keys=True,
        )
        if identity not in seen:
            seen.add(identity)
            merged.append(payload)
    return merged


def _state_counts(candidates: Sequence[ExpansionCandidate]) -> dict[str, int]:
    return {state: sum(1 for row in candidates if row.state == state) for state in VALID_STATES}


def _write_blocked_report(
    root: Path,
    run_id: str,
    request: ExpansionRequest,
    errors: Sequence[Mapping[str, Any]],
) -> ExpansionReport:
    report = ExpansionReport(
        status="blocked",
        workspace=root,
        run_id=run_id,
        scope=request.scope,
        target_ids=list(request.target_ids),
        provider=request.provider,
        errors=[dict(row) for row in errors],
    )
    write_yaml(run_directory(root, run_id) / "expansion_report.yml", report.to_dict())
    return report


def _bib_escape(value: str) -> str:
    return value.replace("\\", "\\textbackslash{} ").replace("{", "\\{").replace("}", "\\}")


def _safe_markdown_title(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    text = text.replace("[[", "[").replace("]]", "]").replace("|", "-")
    return text[:500] or "Untitled expansion candidate"


def _bounded_float(value: Any) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _controller_version_matches(row: Mapping[str, Any], expected: int) -> bool:
    if "expected_version" not in row:
        return False
    try:
        return int(row["expected_version"]) == expected
    except (TypeError, ValueError):
        return False


@contextmanager
def _registry_lock(root: Path):
    """Serialize all read-modify-write operations on expansion candidates."""

    lock_path = root / "11_state" / "expansion-candidates.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows has no fcntl
            yield
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_expansion_run_id() -> str:
    return f"expand-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _new_expansion_map_run_id() -> str:
    return f"expand-map-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


class _FocusedZoteroClient:
    """Read-only client view whose inventory is exactly the accepted frontier."""

    def __init__(self, delegate: ZoteroClient, items: Sequence[Mapping[str, Any]]) -> None:
        self.delegate = delegate
        self.items = [dict(row) for row in items]

    def status(self) -> Mapping[str, Any]:
        return self.delegate.status()

    def collections(self) -> list[dict[str, Any]]:
        return self.delegate.collections()

    def selected_collection(self) -> Mapping[str, Any]:
        return self.delegate.selected_collection()

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]:
        del scope, collection_key
        return list(self.items)

    def children(self, item_key_value: str) -> list[dict[str, Any]]:
        return self.delegate.children(item_key_value)

    def fulltext(self, item_key_value: str) -> Mapping[str, Any] | None:
        return self.delegate.fulltext(item_key_value)

    def file(self, item_key_value: str) -> tuple[bytes, str] | None:
        return self.delegate.file(item_key_value)
