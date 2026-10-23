"""Optional, bounded relationship decisions over existing source-memory notes.

This stage never acquires source documents or regenerates notes. Dispatch is
disabled by default and requires an exact request approval manifest. Registry
writes additionally require the explicit private-copy guard in jev_relationships.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .files import write_json
from .workspace import confined_child, resolve_workspace


@dataclass(frozen=True)
class RelationshipMappingOptions:
    backend: str = "off"
    per_source_cap: int = 8
    max_pairs: int = 256
    batch_size: int = 1
    packet_format: str = "full"
    dry_run: bool = True
    apply: bool = False
    max_spend_usd: str = "0"
    approval_manifest: str = ""
    instance_id: str | None = None
    library_id: str = "0"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None = None) -> RelationshipMappingOptions:
        if value is None:
            value = {}
        if not isinstance(value, Mapping) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError("invalid relationship_decisions options")
        result = cls(**dict(value))
        if not isinstance(result.backend, str) or result.backend not in {"off", "jev"}:
            raise ValueError("relationship decision backend must be off or jev")
        for name, upper in (("per_source_cap", 64), ("max_pairs", 256), ("batch_size", 3)):
            number = getattr(result, name)
            if type(number) is not int or not 1 <= number <= upper:
                raise ValueError(f"relationship {name} must be between 1 and {upper}")
        if not isinstance(result.packet_format, str) or result.packet_format not in {"full", "compact"}:
            raise ValueError("relationship packet_format must be full or compact")
        if type(result.dry_run) is not bool or type(result.apply) is not bool:
            raise ValueError("relationship dry_run and apply must be booleans")
        try:
            budget = Decimal(str(result.max_spend_usd))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("relationship spending limit must be an exact decimal") from exc
        if not budget.is_finite() or budget < 0:
            raise ValueError("relationship spending limit must be finite and nonnegative")
        if not isinstance(result.approval_manifest, str):
            raise ValueError("relationship approval manifest must be a path string")
        if result.instance_id is not None and (not isinstance(result.instance_id, str) or not result.instance_id.strip()):
            raise ValueError("relationship instance_id must be a nonempty string")
        if not isinstance(result.library_id, str) or not result.library_id.strip():
            raise ValueError("relationship library_id must be a nonempty string")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "max_spend_usd": str(self.max_spend_usd)}


def _section(text: str, heading: str) -> tuple[str, int] | None:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.fullmatch(r"##\s+" + re.escape(heading) + r"\s*", line.strip(), re.I):
            end = next((i for i in range(index + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])), len(lines))
            first = index + 1
            while first < end and not lines[first].strip():
                first += 1
            content = "".join(lines[first:end]).strip()
            return (content, first + 1) if content else None
    return None


def load_decision_endpoints(workspace: Path | str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Read bounded canonical notes/profiles; select existing sections verbatim."""
    from .jev_decisions import Endpoint, EvidencePassage
    from .local_tools import _ReadBudget
    from .notes import parse_atomic_note
    from .profiles import profile_from_dict, profile_sidecar_path
    from .relationships import profile_content_hash

    root = resolve_workspace(workspace)
    if len(rows) > 8192:
        raise ValueError("too many relationship endpoints")
    budget = _ReadBudget(root, max_files=16384, max_file_bytes=8 * 1024 * 1024, max_total_bytes=128 * 1024 * 1024)
    endpoints, profiles, bindings, excluded = {}, [], {}, []
    for row in rows:
        sid, nid = row.get("source_id"), row.get("note_id")
        if not isinstance(sid, str) or not isinstance(nid, str) or not sid or not nid:
            raise ValueError("relationship endpoint requires canonical source and note identities")
        if sid in endpoints or sid in bindings:
            raise ValueError("duplicate relationship source identity")
        relative = row.get("note_path")
        if not isinstance(relative, str) or not relative:
            excluded.append({"source_id": sid, "reason": "missing_note_path"})
            continue
        path = confined_child(root, relative)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("relationship note must be a bounded canonical file")
        profile_path = profile_sidecar_path(root / "02_source_memory/profiles", nid)
        metadata_path = confined_child(root, "11_state/note_metadata", profile_path.name)
        budget.reserve([profile_path, metadata_path])
        profile_raw = profile_path.read_bytes() if profile_path.is_file() else b""
        wrapper, metadata = budget.read(profile_path), budget.read(metadata_path)
        if not wrapper:
            excluded.append({"source_id": sid, "reason": "missing_profile"})
            continue
        profile = profile_from_dict(wrapper.get("profile", {}))
        if profile_path.read_bytes() != profile_raw:
            raise ValueError("relationship profile changed while preparing evidence")
        if profile.source_id != sid or profile.note_id != nid:
            raise ValueError("relationship canonical profile identity mismatch")
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("relationship note changed beyond its size bound")
        text = raw.decode("utf-8")
        note_hash = hashlib.sha256(raw).hexdigest()
        stored_front = metadata.get("frontmatter", {})
        if not isinstance(stored_front, Mapping):
            raise ValueError("relationship metadata frontmatter must be an object")
        if stored_front and (stored_front.get("source_id") != sid or stored_front.get("note_id") != nid):
            raise ValueError("relationship metadata identity mismatch")
        front, _ = parse_atomic_note(text)
        for field, expected in (("source_id", sid), ("note_id", nid)):
            if front.get(field, stored_front.get(field)) != expected:
                raise ValueError("relationship actual note identity mismatch")
        if metadata.get("note_path") and confined_child(root, metadata["note_path"]) != path:
            raise ValueError("relationship canonical note path mismatch")
        bindings[sid] = {"source_id": sid, "note_id": nid, "note_path": str(path.relative_to(root)),
                         "note_sha256": note_hash, "profile_hash": profile_content_hash(profile),
                         "profile_path": str(profile_path.relative_to(root)),
                         "profile_file_sha256": hashlib.sha256(profile_raw).hexdigest()}
        profiles.append(profile)
        thesis = _section(text, "Thesis")
        if not thesis:
            excluded.append({"source_id": sid, "reason": "missing_substantive_thesis"})
            continue
        parts = []
        for heading in ("Thesis", "Method and Research Design", "Limitations"):
            section = _section(text, heading)
            if section:
                parts.append(f"## {heading}\n{section[0]}")
        title = str(row.get("title") or nid)
        context = f"# {title}\nExisting note summary; no original document is supplied.\n\n" + "\n\n".join(parts)
        if len(context.encode("utf-8")) > 16_000 or len(thesis[0].encode("utf-8")) > 24_000:
            excluded.append({"source_id": sid, "reason": "selected_note_context_exceeds_bound"})
            continue
        coverage = {"source_scope": row.get("source_scope", "note_only"),
                    "evidence_basis": "existing_note_summary", "original_document_locator_verified": False,
                    "evidence_eligibility": profile.evidence_eligibility}
        ownership = {"attribution": "existing_source_note", "original_source_ownership_verified": False}
        for origin in (stored_front, profile.context or {}, profile.validity or {}, front):
            for name in ("ownership_status", "attribution_status", "attribution_warning", "identity_warning",
                         "ownership_warning", "author_attribution_unresolved", "note_source_identity"):
                if name in origin:
                    # A warning in any recorded source must survive; a later
                    # empty field must not erase an earlier attribution alert.
                    if origin[name] or name not in ownership:
                        ownership[name] = origin[name]
        passage = EvidencePassage(
            passage_id=f"{nid}-thesis", text=thesis[0],
            locator={"kind": "note_lines", "note_id": nid, "note_relative_path": str(path.relative_to(root)),
                     "heading": "Thesis", "line_start": thesis[1], "line_end": thesis[1] + len(thesis[0].splitlines()) - 1,
                     "original_document_locator_verified": False},
            attribution="Existing generated note summary; not an original-publication quotation.",
            content_sha256=hashlib.sha256(thesis[0].encode()).hexdigest(),
        )
        endpoint = Endpoint(sid, nid, context, coverage, ownership, (passage,))
        endpoint.validate()
        endpoints[sid] = endpoint
    return {"endpoints": endpoints, "profiles": profiles, "bindings": bindings, "excluded": excluded}


def group_pair_requests(requests: Sequence[Any], batch_size: int = 1) -> list[tuple[Any, ...]]:
    """Only group up to three explicitly requested pairs sharing one endpoint."""
    if type(batch_size) is not int or not 1 <= batch_size <= 3:
        raise ValueError("relationship batch size must be between one and three")
    remaining = list(requests)
    result = []
    while remaining:
        first = remaining.pop(0)
        group = [first]
        common = {first.endpoint_a.source_id, first.endpoint_b.source_id}
        for other in remaining[:]:
            shared = common & {other.endpoint_a.source_id, other.endpoint_b.source_id}
            if shared and len(group) < batch_size:
                group.append(other)
                common = shared
                remaining.remove(other)
        result.append(tuple(group))
    return result


def _approved_config(options: RelationshipMappingOptions, policy_identity: str):
    from .jev_decisions import JevConfig
    approval: dict[str, Any] = {}
    if options.approval_manifest:
        path = Path(options.approval_manifest).expanduser()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("relationship approval must be a bounded local manifest")
        approval = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(approval, dict):
            raise ValueError("relationship approval manifest must be an object")
        approved_budget = Decimal(str(approval.get("budget_usd", "0")))
        if not approved_budget.is_finite() or approved_budget < Decimal(str(options.max_spend_usd)):
            raise ValueError("relationship budget exceeds manifest approval")
    return JevConfig(
        enabled=approval.get("enabled") is True, api_key=os.environ.get("TYPESAFE_API_KEY"),
        budget_usd=Decimal(str(options.max_spend_usd)),
        data_sharing_confirmed=approval.get("data_sharing_confirmed") is True,
        paid_call_approved=approval.get("paid_call_approved") is True,
        approved_billing_policy_identity=str(approval.get("approved_billing_policy_identity", "")),
        approved_cache_keys=tuple(approval.get("approved_cache_keys", ())),
        max_tokens=65_536,
    )


def run_relationship_mapping(
    workspace: Path | str, semantic_neighbors: Sequence[Mapping[str, Any]], *,
    options: Mapping[str, Any] | RelationshipMappingOptions | None = None,
    source_ids: Sequence[str] | None = None,
    preferred_semantic_neighbors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prepare or execute the optional stage, independently of the mapping backend."""
    settings = options if isinstance(options, RelationshipMappingOptions) else RelationshipMappingOptions.from_dict(options)
    if settings.backend == "off":
        return {"status": "disabled", "provider_calls": 0, "accepted_count": 0}
    from .jev_batches import BatchRequest, JevBatchClient, batch_cache_key
    from .jev_decisions import DecisionRequest, DecisionStore, MODEL_ID, TEMPLATE_VERSION
    from .jev_relationships import JevAcceptancePolicy
    from .candidate_queue import CandidateQueue, queue_scope
    from .jev_relationships import (apply_jev_decisions, evaluate_jev_decision, reusable_jev_pairs,
                                    validate_private_relationship_workspace)
    from .mapping_candidates import prepare_mapping_candidates
    from .local_tools import load_local_retrieval

    root = resolve_workspace(workspace)
    if settings.apply and not settings.dry_run:
        validate_private_relationship_workspace(root)
    if not settings.dry_run and (settings.batch_size != 1 or settings.packet_format != "full"):
        return {"status": "blocked", "reason": "Experimental packet formats require separate evaluation; this mapping route uses full isolated pairs.",
                "provider_calls": 0, "accepted_count": 0}
    loaded = load_local_retrieval(root, instance_id=settings.instance_id, library_id=settings.library_id, infer_scope=True)
    # Reuse checks read only saved dependencies. Prepare rich note evidence only
    # after bounded candidate selection, rather than rereading the whole library.
    lean_bindings = {row["source_id"]: {key: row.get(key, "") for key in
                                      ("source_id", "note_id", "note_path", "profile_hash")}
                     for row in loaded["rows"]}
    resolved = reusable_jev_pairs(root, bindings=lean_bindings)
    from .relationships import canonical_pair
    resolved.update(canonical_pair(row["source_id"], row["target_source_id"])
                    for row in loaded["relationships"])
    from .retrieval import CandidateResourceError
    try:
        prepared = prepare_mapping_candidates(
            root, semantic_neighbors, per_source_cap=settings.per_source_cap, max_pairs=settings.max_pairs,
            source_ids=source_ids, resolved_pairs=resolved, instance_id=settings.instance_id, library_id=settings.library_id,
            loaded=loaded, fanout=64, persist_queue=True, preferred_semantic_neighbors=preferred_semantic_neighbors,
            decision_contract={"model": MODEL_ID, "template": TEMPLATE_VERSION,
                               "policy": asdict(JevAcceptancePolicy()), "packet_format": settings.packet_format,
                               "batch_size": settings.batch_size},
        )
    except CandidateResourceError as exc:
        return {"schema": "relationship-mapping-v1", "backend": "jev", "status": "partial",
                "coverage_partial": True, "reason": str(exc), "provider_calls": 0,
                "candidate_count": 0, "accepted_count": 0,
                "candidate_queue": {"unfinished": True, "all_pairs_examined": False,
                                    "existing_progress_preserved": True}}
    queue = CandidateQueue(root, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), source_ids))
    selected_by_pair = {(pair["left_source_id"], pair["right_source_id"]): pair for pair in prepared["candidates"]}
    selected_ids = {pair[key] for pair in prepared["candidates"] for key in ("left_source_id", "right_source_id")}
    endpoints = load_decision_endpoints(root, [row for row in loaded["rows"] if row["source_id"] in selected_ids])
    requests, unavailable = [], list(endpoints["excluded"])
    workspace_id = "workspace-" + hashlib.sha256(str(root).encode()).hexdigest()[:24]
    for pair in prepared["candidates"]:
        left, right = pair["left_source_id"], pair["right_source_id"]
        if left not in endpoints["endpoints"] or right not in endpoints["endpoints"]:
            unavailable.append({"left_source_id": left, "right_source_id": right, "reason": "missing_substantive_note_context"})
            if not settings.dry_run:
                queue.record(pair, "deferred", detail={"reason": "missing_substantive_note_context"})
            continue
        requests.append(DecisionRequest(workspace_id, settings.library_id,
                                        endpoints["endpoints"][left], endpoints["endpoints"][right]))
    batches = [BatchRequest(tuple(group), packet_format=settings.packet_format)
               for group in group_pair_requests(requests, settings.batch_size)]
    cache = confined_child(root, ".auto-zettelkasten")
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink():
        raise ValueError("relationship cache cannot be a symbolic link")
    # The API performs all spending admission; this stage never handles secrets
    # beyond passing the conventional process environment to its client.
    from .jev_batches import BATCH_BILLING_POLICY
    config = _approved_config(settings, BATCH_BILLING_POLICY.identity)
    store = DecisionStore(confined_child(cache, "jev-decisions.sqlite3"))
    client = JevBatchClient(config, store)
    previews, evaluations, results = [], [], []
    for batch in batches:
        preview = client.assess(batch, dry_run=True)
        previews.append({"cache_key": batch_cache_key(batch), "request": preview.get("payload"),
                         "billing_admission": preview.get("billing_admission"), "status": preview["status"]})
        result = preview if settings.dry_run else client.assess(batch, dry_run=False)
        results.append(result)
        by_pair = {(request.endpoint_a.source_id, request.endpoint_b.source_id): request for request in batch.requests}
        evaluated_pairs = set()
        for artifact in result.get("decisions", []):
            pair = (artifact["endpoints"]["A"]["source_id"], artifact["endpoints"]["B"]["source_id"])
            if pair not in by_pair:
                raise ValueError("Jev returned an unrequested relationship")
            evaluation = evaluate_jev_decision(artifact, by_pair[pair], profiles=endpoints["profiles"], bindings=endpoints["bindings"])
            evaluations.append(evaluation)
            evaluated_pairs.add(tuple(sorted(pair)))
            if not settings.dry_run:
                queue_status = {"accepted": "accepted", "no_relationship": "negative", "deferred": "deferred"}.get(evaluation["status"], "deferred")
                queue.record(selected_by_pair[tuple(sorted(pair))], queue_status,
                             detail={"decision_key": evaluation["decision_key"], "reasons": evaluation.get("reasons", []),
                                     "cache_key": batch_cache_key(batch)})
        if not settings.dry_run:
            for pair in by_pair:
                key = tuple(sorted(pair))
                if key not in evaluated_pairs:
                    # Never retry a dispatched unknown outcome automatically.
                    # Non-dispatched blocked/budget work stays in the queue.
                    status = "unknown_dispatch" if result.get("provider_calls", 0) else "unexamined"
                    queue.record(selected_by_pair[key], status, detail={"dispatch_status": result["status"], "cache_key": batch_cache_key(batch)})
    applied = apply_jev_decisions(root, evaluations, apply=settings.apply and not settings.dry_run)
    if not settings.dry_run:
        queue.finish_pass()
    statuses = [result["status"] for result in results]
    with store._connect() as db:
        reservations = db.execute("SELECT reserved_usd, actual_usd, status FROM reservations").fetchall()
    settled = sum((Decimal(actual) for _, actual, state in reservations if state == "settled" and actual is not None), Decimal(0))
    retained = sum((Decimal(reserved) for reserved, _, state in reservations if state != "settled"), Decimal(0))
    failures = {"blocked", "invalid_response", "billing_uncertain", "provider_error", "disabled", "persistence_failed"}
    progress = queue.summary()
    partial = progress["unfinished"] or bool(unavailable) or prepared["status"].get("profile_load_status") == "partial" or any(s in failures for s in statuses)
    report = {"schema": "relationship-mapping-v1", "backend": "jev",
              "status": "dry_run" if settings.dry_run else "partial" if partial else "completed",
              "coverage_partial": partial,
              "provider_calls": sum(result.get("provider_calls", 0) for result in results),
              "cost_usd": str(sum((Decimal(str(result.get("cost_usd", "0"))) for result in results), Decimal(0))),
              "max_spend_usd": settings.max_spend_usd, "candidate_count": len(prepared["candidates"]),
              "ledger_settled_usd": str(settled), "ledger_retained_reservations_usd": str(retained),
              "request_count": len(batches), "reused_pair_count": len(resolved),
              "candidate_stats": prepared["stats"], "coverage": prepared["status"], "candidate_queue": progress,
              "unavailable": unavailable, "preview": previews, "application": applied,
              "accepted_count": applied.get("counts", {}).get("accepted", 0) if applied.get("applied") else 0,
              "eligible_count": applied.get("counts", {}).get("accepted", 0),
              "evaluations": evaluations, "results": results}
    path = confined_child(cache, "relationship-mapping-preview.json" if settings.dry_run else "relationship-mapping-result.json")
    write_json(path, report)
    report["path"] = path
    return report
