from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .files import now_iso, read_yaml, sha256_text, write_yaml
from .models import RelationshipDecision, RelationshipPairJob
from .navigation import TYPED_SOURCE_RELATIONS, rank_human_related_links


RELATIONSHIP_PROMPT_VERSION = "9"
RELATIONSHIP_REGISTRY_SCHEMA_VERSION = "6"
RELATIONSHIP_DECISION_SCHEMA_VERSION = "6"
RELATIONSHIP_DECISION_CONTRACT = "relationship-decision-v6"
SUBSTANTIVE_RELATION_TYPES = frozenset(
    {
        "supports",
        "undermines",
        "qualifies",
        "extends",
        "complements",
        "contrasts",
        "rival_explanation",
        "boundary_contrast",
        "methodological_fault_line",
        "sequential_relationship",
        "interpretive_or_normative_disagreement",
        "contextual_connection",
    }
)
RECIPROCAL_RELATION_TYPES = {
    "supports": "supported_by",
    "undermines": "undermined_by",
    "qualifies": "qualified_by",
    "extends": "extended_by",
    "complements": "complements",
    "contrasts": "contrasts",
    "rival_explanation": "rival_explanation",
    "boundary_contrast": "boundary_contrast",
    "methodological_fault_line": "methodological_fault_line",
    "sequential_relationship": "sequential_relationship",
    "interpretive_or_normative_disagreement": "interpretive_or_normative_disagreement",
    "contextual_connection": "contextual_connection",
}
RELATIONSHIP_PROJECTION_LABELS = {
    "supports": ("supports", "supported by"),
    "undermines": ("undermines", "undermined by"),
    "qualifies": ("qualifies", "qualified by"),
    "extends": ("extends", "extended by"),
    "complements": ("complements", "complements"),
    "contrasts": ("contrasts with", "contrasts with"),
    "rival_explanation": ("offers a rival explanation to", "has a rival explanation from"),
    "boundary_contrast": ("contrasts in scope with", "contrasts in scope with"),
    "methodological_fault_line": (
        "differs methodologically from",
        "differs methodologically from",
    ),
    "sequential_relationship": ("precedes in sequence", "follows in sequence"),
    "interpretive_or_normative_disagreement": (
        "disagrees interpretively with",
        "disagrees interpretively with",
    ),
    "contextual_connection": (
        "is contextually connected to",
        "is contextually connected to",
    ),
}
SYMMETRIC_RELATION_TYPES = frozenset(
    {
        "complements",
        "contrasts",
        "boundary_contrast",
        "methodological_fault_line",
        "interpretive_or_normative_disagreement",
        "contextual_connection",
    }
)
_LIMITED_STATUSES = {
    "abstract_only_atomic_note",
    "metadata_only_source_note",
    "partial_document_atomic_note",
    "fulltext_available",
    "limited",
    "limited_context_only",
}


def stable_hash(value: Any) -> str:
    return sha256_text(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def profile_row(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return dict(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise ValueError("relationship profiles must be mappings or dataclass models")


def profile_content_hash(value: Any) -> str:
    return stable_hash(profile_row(value))


def relationship_decision_key(
    source_id: str,
    target_source_id: str,
    source_profile_hash: str,
    target_profile_hash: str,
    *,
    provider: str,
    model: str,
    prompt_version: str = RELATIONSHIP_PROMPT_VERSION,
) -> str:
    profiles = sorted(
        (
            (str(source_id), str(source_profile_hash)),
            (str(target_source_id), str(target_profile_hash)),
        )
    )
    return stable_hash(
        {
            "profiles": profiles,
            "provider": str(provider),
            "model": str(model),
            "prompt_version": str(prompt_version),
        }
    )


def candidate_rows(
    response: Mapping[str, Any],
    *,
    focus_source_ids: Sequence[str],
    available_source_ids: Sequence[str],
    available_cluster_ids: Sequence[str] = (),
    max_per_source: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    focus = set(focus_source_ids)
    available_sources = set(available_source_ids)
    available_clusters = set(available_cluster_ids)
    accepted: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(response.get("candidates", []) or []):
        if not isinstance(raw, Mapping):
            parked.append({"row_index": index, "reason": "candidate_not_mapping"})
            continue
        row = dict(raw)
        source_id = str(row.get("source_id") or "").strip()
        target_kind = str(row.get("target_kind") or "source").strip()
        target_id = str(
            row.get("target_id") or row.get("target_source_id") or ""
        ).strip()
        reasons = []
        if source_id not in focus:
            reasons.append("source_not_in_focus_context")
        if target_kind not in {"source", "cluster"}:
            reasons.append("unsupported_target_kind")
        if target_kind == "source" and target_id not in available_sources:
            reasons.append("target_not_in_context")
        if target_kind == "cluster" and target_id not in available_clusters:
            reasons.append("target_not_in_context")
        if target_kind == "source" and source_id == target_id:
            reasons.append("self_relationship")
        why = str(row.get("why_relevant") or row.get("reason") or "").strip()
        if not why:
            reasons.append("missing_relevance_reason")
        confidence = _confidence(row.get("confidence"))
        if confidence is None:
            reasons.append("invalid_confidence")
        identity = (source_id, target_kind, target_id)
        if identity in seen:
            continue
        seen.add(identity)
        if reasons:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "reason": ",".join(reasons),
                    "raw": row,
                }
            )
            continue
        if counts.get(source_id, 0) >= max_per_source:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "reason": "candidate_limit_reached",
                    "raw": row,
                }
            )
            continue
        counts[source_id] = counts.get(source_id, 0) + 1
        accepted.append(
            {
                "source_id": source_id,
                "target_kind": target_kind,
                "target_id": target_id,
                "why_relevant": why,
                "comparison_unit": str(row.get("comparison_unit") or "").strip(),
                "likely_relation_type": str(
                    row.get("likely_relation_type") or ""
                ).strip(),
                "requested_evidence_depth": str(
                    row.get("requested_evidence_depth") or "profile"
                ).strip(),
                "confidence": confidence,
            }
        )
    return accepted, parked


def validate_bridge_shard_pairs(
    response: Mapping[str, Any],
    *,
    available_shards: Sequence[Mapping[str, Any]],
    max_pairs: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate bounded cross-literature routing without making semantic judgments."""

    by_id = {
        str(row.get("shard_id") or ""): dict(row)
        for row in available_shards
        if isinstance(row, Mapping) and row.get("shard_id")
    }
    accepted: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(response.get("shard_pairs", []) or []):
        if not isinstance(raw, Mapping):
            parked.append({"row_index": index, "reason": "shard_pair_not_mapping"})
            continue
        row = dict(raw)
        left = str(row.get("left_shard_id") or "").strip()
        right = str(row.get("right_shard_id") or "").strip()
        pair = canonical_pair(left, right)
        confidence = _confidence(row.get("confidence"))
        reason = str(row.get("reason") or "").strip()
        reasons: list[str] = []
        if not left or not right or left == right:
            reasons.append("invalid_shard_pair")
        if left not in by_id or right not in by_id:
            reasons.append("shard_not_in_context")
        if (
            left in by_id
            and right in by_id
            and str(by_id[left].get("literature_id") or "")
            == str(by_id[right].get("literature_id") or "")
        ):
            reasons.append("same_literature_shard_pair")
        if pair in seen:
            reasons.append("duplicate_shard_pair")
        seen.add(pair)
        if confidence is None:
            reasons.append("invalid_confidence")
        if len(reason.split()) < 5:
            reasons.append("bridge_reason_too_vague")
        if len(accepted) >= max_pairs:
            reasons.append("bridge_shard_pair_limit_reached")
        if reasons:
            parked.append(
                {
                    "row_index": index,
                    "left_shard_id": left,
                    "right_shard_id": right,
                    "reason": ",".join(reasons),
                    "raw": row,
                }
            )
            continue
        accepted.append(
            {
                "left_shard_id": left,
                "right_shard_id": right,
                "reason": reason,
                "confidence": confidence,
            }
        )
    return accepted, parked


def _normalize_provider_decision_row(
    row: Mapping[str, Any],
    *,
    job: RelationshipPairJob,
    left_anchors: Mapping[str, Mapping[str, Any]],
    right_anchors: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Fill job-owned v6 structure and normalize anchor ownership."""

    normalized = dict(row)
    contract = str(job.output_contract or "")
    if contract != RELATIONSHIP_DECISION_CONTRACT:
        if str(normalized.get("decision") or "") == "relationship":
            labels = RELATIONSHIP_PROJECTION_LABELS.get(
                str(normalized.get("relation_type") or "")
            )
            if labels:
                normalized["forward_label"], normalized["inverse_label"] = labels
        return normalized, []

    normalized["pair_job_id"] = job.pair_job_id
    normalized["pair"] = {
        "left_source_id": job.left_source_id,
        "right_source_id": job.right_source_id,
    }
    normalized["output_contract"] = RELATIONSHIP_DECISION_CONTRACT
    if str(normalized.get("decision") or "") != "relationship":
        return normalized, []

    relation_type = str(normalized.get("relation_type") or "")
    labels = RELATIONSHIP_PROJECTION_LABELS.get(relation_type)
    if labels:
        normalized["forward_label"], normalized["inverse_label"] = labels
    normalized["relationship_tier"] = (
        "contextual" if relation_type == "contextual_connection" else "direct"
    )
    if relation_type in SYMMETRIC_RELATION_TYPES:
        normalized["actor_source_id"] = job.left_source_id
        normalized["reference_source_id"] = job.right_source_id

    raw_left = normalized.get("left_evidence_anchor_ids", [])
    raw_right = normalized.get("right_evidence_anchor_ids", [])
    if not isinstance(raw_left, list) or not isinstance(raw_right, list):
        return normalized, []

    warnings: list[str] = []
    left_ids: list[str] = []
    right_ids: list[str] = []
    seen: set[str] = set()
    for supplied_side, value in [
        *(("left", anchor_id) for anchor_id in raw_left),
        *(("right", anchor_id) for anchor_id in raw_right),
    ]:
        anchor_id = str(value)
        if not anchor_id or anchor_id in seen:
            continue
        seen.add(anchor_id)
        if anchor_id in left_anchors and anchor_id not in right_anchors:
            left_ids.append(anchor_id)
            if supplied_side != "left":
                warnings.append(
                    f"anchor_repartitioned:{anchor_id}:right_to_left"
                )
        elif anchor_id in right_anchors and anchor_id not in left_anchors:
            right_ids.append(anchor_id)
            if supplied_side != "right":
                warnings.append(
                    f"anchor_repartitioned:{anchor_id}:left_to_right"
                )
        else:
            warnings.append(f"anchor_dropped_unknown:{anchor_id}")
    normalized["left_evidence_anchor_ids"] = left_ids
    normalized["right_evidence_anchor_ids"] = right_ids
    return normalized, warnings


def ingest_relationship_decision_batch(
    response: Mapping[str, Any],
    *,
    pair_jobs: Sequence[RelationshipPairJob | Mapping[str, Any]],
    profiles: Sequence[Any] = (),
    provider: str = "",
    model: str = "",
    reasoner_backend: str = "",
    prompt_version: str = RELATIONSHIP_PROMPT_VERSION,
) -> dict[str, list[dict[str, Any]]]:
    """Validate independent provider decisions without rejudging their semantics."""

    jobs: dict[str, tuple[RelationshipPairJob, dict[str, Any]]] = {}
    for value in pair_jobs:
        job = (
            value
            if isinstance(value, RelationshipPairJob)
            else RelationshipPairJob.from_dict(value)
        )
        if job.pair_job_id in jobs:
            raise ValueError(f"duplicate relationship pair job: {job.pair_job_id}")
        jobs[job.pair_job_id] = (job, job.to_dict())
    profiles_by_source = {
        str(row.get("source_id") or ""): row
        for row in (profile_row(value) for value in profiles)
        if row.get("source_id")
    }

    raw_values = response.get("decisions", []) or []
    if isinstance(raw_values, Mapping):
        raw_rows: list[Any] = [
            {**dict(value), "pair_job_id": str(job_id)}
            if isinstance(value, Mapping)
            else value
            for job_id, value in raw_values.items()
        ]
    elif isinstance(raw_values, Sequence) and not isinstance(
        raw_values, (str, bytes, bytearray)
    ):
        raw_rows = list(raw_values)
    else:
        raw_rows = []
    job_counts: dict[str, int] = {}
    for value in raw_rows:
        if isinstance(value, Mapping):
            job_id = str(value.get("pair_job_id") or "").strip()
            if job_id:
                job_counts[job_id] = job_counts.get(job_id, 0) + 1

    accepted: list[dict[str, Any]] = []
    no_relationship: list[dict[str, Any]] = []
    needs_more_context: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    completed_job_ids: set[str] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            parked.append(
                {
                    "row_index": index,
                    "reason": "decision_v4_not_mapping",
                    "raw": raw,
                }
            )
            continue
        row = dict(raw)
        row_provider = str(row.pop("provider", "") or provider)
        row_model = str(row.pop("model", "") or model)
        row_backend = str(
            row.pop("reasoner_backend", "") or reasoner_backend or row_provider
        )
        job_id = str(row.get("pair_job_id") or "").strip()
        if job_id not in jobs:
            parked.append(
                {
                    "row_index": index,
                    "pair_job_id": job_id,
                    "reason": "pair_job_not_in_batch",
                    "raw": row,
                }
            )
            continue
        if job_counts.get(job_id, 0) != 1:
            parked.append(
                {
                    "row_index": index,
                    "pair_job_id": job_id,
                    "reason": "duplicate_pair_job_decision",
                    "raw": row,
                }
            )
            continue

        job, job_row = jobs[job_id]
        reasons: list[str] = []
        left_anchors = _job_anchor_rows(
            job_row,
            side="left",
            source_id=job.left_source_id,
            profile=profiles_by_source.get(job.left_source_id, {}),
        )
        right_anchors = _job_anchor_rows(
            job_row,
            side="right",
            source_id=job.right_source_id,
            profile=profiles_by_source.get(job.right_source_id, {}),
        )
        row, contract_warnings = _normalize_provider_decision_row(
            row,
            job=job,
            left_anchors=left_anchors,
            right_anchors=right_anchors,
        )
        if (
            str(row.get("decision") or "") == "relationship"
            and (
                not row.get("left_evidence_anchor_ids")
                or not row.get("right_evidence_anchor_ids")
            )
        ):
            parked.append(
                {
                    "row_index": index,
                    "pair_job_id": job_id,
                    "source_id": job.left_source_id,
                    "target_source_id": job.right_source_id,
                    "reason": "missing_endpoint_evidence",
                    "contract_warnings": contract_warnings,
                    "raw": row,
                }
            )
            continue
        try:
            decision = RelationshipDecision.from_dict(row)
        except (TypeError, ValueError) as exc:
            parked.append(
                {
                    "row_index": index,
                    "pair_job_id": job_id,
                    "reason": "invalid_decision_v4_contract",
                    "error": str(exc),
                    "raw": row,
                }
            )
            continue

        if (
            decision.left_source_id != job.left_source_id
            or decision.right_source_id != job.right_source_id
        ):
            reasons.append("decision_pair_does_not_match_job")

        if decision.decision == "relationship":
            if decision.relation_type not in SUBSTANTIVE_RELATION_TYPES:
                reasons.append("unsupported_relation_type")
            if {
                decision.actor_source_id,
                decision.reference_source_id,
            } != {job.left_source_id, job.right_source_id}:
                reasons.append("direction_does_not_use_job_pair")
            if not set(decision.left_evidence_anchor_ids) <= set(left_anchors):
                reasons.append("left_anchor_not_owned_by_left_source")
            if not set(decision.right_evidence_anchor_ids) <= set(right_anchors):
                reasons.append("right_anchor_not_owned_by_right_source")

        if reasons:
            parked.append(
                {
                    "row_index": index,
                    "pair_job_id": job_id,
                    "source_id": job.left_source_id,
                    "target_source_id": job.right_source_id,
                    "reason": ",".join(reasons),
                    "raw": row,
                }
            )
            continue

        completed_job_ids.add(job_id)
        normalized = _normalized_v4_decision(
            decision,
            job=job,
            job_row=job_row,
            left_anchors=left_anchors,
            right_anchors=right_anchors,
            provider=row_provider,
            model=row_model,
            reasoner_backend=row_backend,
            prompt_version=prompt_version,
            profiles_by_source=profiles_by_source,
        )
        if contract_warnings:
            normalized["contract_warnings"] = contract_warnings
        if decision.decision == "relationship":
            accepted.append(normalized)
        elif decision.decision == "no_relationship":
            no_relationship.append(normalized)
        else:
            needs_more_context.append(normalized)

    for job_id, (job, _job_row) in jobs.items():
        if job_id not in completed_job_ids and job_counts.get(job_id, 0) == 0:
            parked.append(
                {
                    "pair_job_id": job_id,
                    "source_id": job.left_source_id,
                    "target_source_id": job.right_source_id,
                    "reason": "missing_decision_for_pair_job",
                }
            )
    return {
        "accepted": accepted,
        "no_relationship": no_relationship,
        "needs_more_context": needs_more_context,
        "parked": parked,
    }


def validate_relationship_decision_rows(
    response: Mapping[str, Any],
    *,
    jobs: Sequence[RelationshipPairJob | Mapping[str, Any]],
    profiles: Sequence[Any],
    provider: str = "",
    model: str = "",
    reasoner_backend: str = "",
    prompt_version: str = RELATIONSHIP_PROMPT_VERSION,
) -> dict[str, list[dict[str, Any]]]:
    return ingest_relationship_decision_batch(
        response,
        pair_jobs=jobs,
        profiles=profiles,
        provider=provider,
        model=model,
        reasoner_backend=reasoner_backend,
        prompt_version=prompt_version,
    )


def validate_decisions(
    response: Mapping[str, Any],
    *,
    offered_pairs: Sequence[tuple[str, str]],
    profiles: Sequence[Any],
) -> dict[str, list[dict[str, Any]]]:
    by_source = {
        str(row.get("source_id") or ""): row
        for row in (profile_row(value) for value in profiles)
        if row.get("source_id")
    }
    offered = {canonical_pair(*pair) for pair in offered_pairs}
    accepted: list[dict[str, Any]] = []
    no_relationship: list[dict[str, Any]] = []
    needs_more_context: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(response.get("decisions", []) or []):
        if not isinstance(raw, Mapping):
            parked.append({"row_index": index, "reason": "decision_not_mapping"})
            continue
        row = dict(raw)
        source_id = str(row.get("source_id") or "").strip()
        target_id = str(
            row.get("target_source_id") or row.get("target_id") or ""
        ).strip()
        pair = canonical_pair(source_id, target_id)
        reasons: list[str] = []
        if pair not in offered:
            reasons.append("pair_not_in_context")
        if not source_id or not target_id or source_id == target_id:
            reasons.append("invalid_pair")
        if source_id not in by_source or target_id not in by_source:
            reasons.append("unknown_source")
        if pair in seen:
            reasons.append("duplicate_pair_decision")
        seen.add(pair)
        status = str(row.get("status") or "").strip()
        if status not in {"accepted", "no_relationship", "needs_more_context"}:
            reasons.append("unsupported_status")
        confidence = _confidence(row.get("confidence"))
        if confidence is None:
            reasons.append("invalid_confidence")
        if reasons:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": ",".join(reasons),
                    "raw": row,
                }
            )
            continue
        hashes = {
            "source_profile_hash": profile_content_hash(by_source[source_id]),
            "target_profile_hash": profile_content_hash(by_source[target_id]),
        }
        if status == "no_relationship":
            no_relationship.append(
                {
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": str(row.get("reason") or "").strip(),
                    "confidence": confidence,
                    "decision_status": "no_relationship",
                    "verification_status": "pending",
                    **hashes,
                }
            )
            continue
        if status == "needs_more_context":
            needs_more_context.append(
                {
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "requested_context": _string_list(
                        row.get("requested_context")
                    ),
                    "reason": str(row.get("reason") or "").strip(),
                    "confidence": confidence,
                    "decision_status": "needs_more_context",
                    "verification_status": "pending",
                    **hashes,
                }
            )
            continue
        relation_type = str(row.get("relation_type") or "").strip()
        if relation_type not in SUBSTANTIVE_RELATION_TYPES:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": "unsupported_relation_type",
                    "raw": row,
                }
            )
            continue
        source_anchor_id = str(
            row.get("source_evidence_anchor_id")
            or _evidence_id(row.get("source_evidence"))
            or ""
        ).strip()
        target_anchor_id = str(
            row.get("target_evidence_anchor_id")
            or _evidence_id(row.get("target_evidence"))
            or ""
        ).strip()
        source_anchor = _anchor(by_source[source_id], source_anchor_id)
        target_anchor = _anchor(by_source[target_id], target_anchor_id)
        evidence_reasons = []
        if source_anchor is None:
            evidence_reasons.append("source_anchor_not_found")
        if target_anchor is None:
            evidence_reasons.append("target_anchor_not_found")
        if source_anchor is not None and not _anchor_is_substantive(source_anchor):
            evidence_reasons.append("source_anchor_not_substantive")
        if target_anchor is not None and not _anchor_is_substantive(target_anchor):
            evidence_reasons.append("target_anchor_not_substantive")
        if _limited_profile(by_source[source_id]) and not _limited_anchor_can_support(
            source_anchor
        ):
            evidence_reasons.append("source_limited_scope_cannot_support_relation")
        if _limited_profile(by_source[target_id]) and not _limited_anchor_can_support(
            target_anchor
        ):
            evidence_reasons.append("target_limited_scope_cannot_support_relation")
        reason = str(row.get("reason") or "").strip()
        if len(reason.split()) < 5:
            evidence_reasons.append("relationship_reason_too_vague")
        from .literature import (
            _anchor_supports_causal_claim,
            _has_unqualified_causal_language,
        )

        if _has_unqualified_causal_language(reason) and not (
            source_anchor is not None
            and target_anchor is not None
            and _anchor_supports_causal_claim(source_anchor)
            and _anchor_supports_causal_claim(target_anchor)
        ):
            evidence_reasons.append("unsupported_causal_upgrade")
        if confidence is not None and confidence < 0.55:
            evidence_reasons.append("low_confidence")
        if evidence_reasons:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": ",".join(evidence_reasons),
                    "raw": row,
                }
            )
            continue
        accepted.append(
            {
                "relation_id": _relation_id(source_id, target_id, relation_type),
                "source_id": source_id,
                "target_source_id": target_id,
                "source_note_id": str(by_source[source_id].get("note_id") or ""),
                "target_note_id": str(by_source[target_id].get("note_id") or ""),
                "relation_type": relation_type,
                "reciprocal_type": RECIPROCAL_RELATION_TYPES[relation_type],
                "comparison_unit": str(row.get("comparison_unit") or "").strip(),
                "proposition_ids": _string_list(row.get("proposition_ids")),
                "reason": reason,
                "source_evidence": _evidence_reference(
                    source_id, source_anchor_id, source_anchor or {}
                ),
                "target_evidence": _evidence_reference(
                    target_id, target_anchor_id, target_anchor or {}
                ),
                "qualifiers": _string_list(row.get("qualifiers")),
                "confidence": confidence,
                "provenance": "probabilistic_relationship_adjudication",
                "verification_status": "pending",
                "model": str(row.get("model") or ""),
                "inferred": True,
                "strength": 110,
                "active": True,
                "decision_status": "accepted",
                **hashes,
            }
        )
    return {
        "accepted": accepted,
        "no_relationship": no_relationship,
        "needs_more_context": needs_more_context,
        "parked": parked,
    }


def validate_verifications(
    response: Mapping[str, Any],
    *,
    preliminary_decisions: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
    verifier_provider: str = "",
    verifier_model: str = "",
    prompt_version: str = RELATIONSHIP_PROMPT_VERSION,
) -> dict[str, list[dict[str, Any]]]:
    """Validate independent relationship verification against the offered decisions."""

    preliminary_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in preliminary_decisions:
        source_id = str(raw.get("source_id") or "")
        target_id = str(raw.get("target_source_id") or raw.get("target_id") or "")
        if source_id and target_id and source_id != target_id:
            preliminary_by_pair[canonical_pair(source_id, target_id)] = dict(raw)

    confirmed: list[dict[str, Any]] = []
    corrected: list[dict[str, Any]] = []
    no_relationship: list[dict[str, Any]] = []
    needs_more_context: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    values = response.get("verifications")
    if values is None:
        values = response.get("decisions", [])
    for index, raw in enumerate(values or []):
        if not isinstance(raw, Mapping):
            parked.append({"row_index": index, "reason": "verification_not_mapping"})
            continue
        row = dict(raw)
        source_id = str(row.get("source_id") or "").strip()
        target_id = str(
            row.get("target_source_id") or row.get("target_id") or ""
        ).strip()
        pair = canonical_pair(source_id, target_id)
        preliminary = preliminary_by_pair.get(pair)
        status = str(row.get("status") or "").strip()
        reasons: list[str] = []
        if preliminary is None:
            reasons.append("pair_not_offered_for_verification")
        if not source_id or not target_id or source_id == target_id:
            reasons.append("invalid_pair")
        if pair in seen:
            reasons.append("duplicate_pair_verification")
        seen.add(pair)
        if status not in {
            "confirmed",
            "corrected",
            "no_relationship",
            "needs_more_context",
        }:
            reasons.append("unsupported_verification_status")
        if reasons:
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": ",".join(reasons),
                    "raw": row,
                }
            )
            continue

        assert preliminary is not None
        preliminary_status = str(
            preliminary.get("decision_status")
            or preliminary.get("status")
            or ("accepted" if preliminary.get("relation_type") else "")
        )
        preliminary_relation_type = str(
            preliminary.get("relation_type") or ""
        )
        lineage = {
            "verification_status": status,
            "preliminary_decision_hash": stable_hash(preliminary),
            "preliminary_status": preliminary_status,
            "preliminary_relation_type": preliminary_relation_type,
            "preliminary_reason": str(preliminary.get("reason") or ""),
            "verifier_provider": verifier_provider,
            "verifier_model": verifier_model,
            "verification_prompt_version": str(prompt_version),
            "prompt_version": str(prompt_version),
        }
        if status == "confirmed" and (
            preliminary_status not in {"accepted", "confirmed", "corrected"}
            or source_id != str(preliminary.get("source_id") or "")
            or target_id
            != str(
                preliminary.get("target_source_id")
                or preliminary.get("target_id")
                or ""
            )
            or str(row.get("relation_type") or "") != preliminary_relation_type
        ):
            parked.append(
                {
                    "row_index": index,
                    "source_id": source_id,
                    "target_source_id": target_id,
                    "reason": "confirmed_decision_does_not_match_preliminary",
                    "raw": row,
                }
            )
            continue

        normalized_status = (
            "accepted"
            if status in {"confirmed", "corrected"}
            else status
        )
        validated = validate_decisions(
            {
                "decisions": [
                    {
                        **row,
                        "status": normalized_status,
                        "model": verifier_model,
                    }
                ]
            },
            offered_pairs=[pair],
            profiles=profiles,
        )
        if validated["parked"]:
            parked.extend(
                {
                    **dict(value),
                    "reason": "verification_invalid:"
                    + str(value.get("reason") or ""),
                }
                for value in validated["parked"]
            )
            continue
        if status in {"confirmed", "corrected"}:
            relation = dict(validated["accepted"][0])
            relation.update(
                lineage,
                provenance="probabilistic_relationship_verification",
                model=verifier_model,
                active=True,
                decision_status="accepted",
            )
            (confirmed if status == "confirmed" else corrected).append(relation)
        elif status == "no_relationship":
            decision = dict(validated["no_relationship"][0])
            decision.update(lineage, decision_status="no_relationship")
            no_relationship.append(decision)
        else:
            decision = dict(validated["needs_more_context"][0])
            decision.update(lineage)
            needs_more_context.append(decision)
    return {
        "confirmed": confirmed,
        "corrected": corrected,
        "accepted": [*confirmed, *corrected],
        "no_relationship": no_relationship,
        "needs_more_context": needs_more_context,
        "parked": parked,
    }


def persist_relationship_registry(
    workspace: Path,
    *,
    structural_relations: Sequence[Mapping[str, Any]],
    accepted_relations: Sequence[Mapping[str, Any]] = (),
    no_relationship_decisions: Sequence[Mapping[str, Any]] = (),
    parked_rows: Sequence[Mapping[str, Any]] = (),
    preserve_unmentioned_structural: bool = False,
    orphaned_source_ids: Sequence[str] = (),
    reconcile_machine_prompt_version: str | None = None,
) -> dict[str, Any]:
    path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility_path = (
        workspace / "02_source_memory" / "indexes" / "typed_note_links.yml"
    )
    existing = read_yaml(path, {}) or {}
    existing_rows = (
        existing.get("relations") or existing.get("links") or []
        if isinstance(existing, Mapping)
        else []
    )
    existing_schema = (
        str(existing.get("registry_schema_version") or "")
        if isinstance(existing, Mapping)
        else ""
    )
    migrated_rows: list[dict[str, Any]] = []
    for raw in existing_rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        if (
            existing_schema == "3"
            and _machine_substantive(row)
            and bool(row.get("active", True))
            and _verified_machine_relation(row)
        ):
            row.update(
                decision_status="legacy_review_pending",
                legacy_review_pending=True,
                cluster_evidence_eligible=False,
            )
        elif (
            existing_schema == "5"
            and _machine_substantive(row)
            and bool(row.get("active", True))
        ):
            row["relationship_tier"] = (
                "legacy_unclassified"
                if str(row.get("relation_type") or "") == "complements"
                else "direct"
            )
        elif (
            existing_schema != RELATIONSHIP_REGISTRY_SCHEMA_VERSION
            and _machine_substantive(row)
            and bool(row.get("active", True))
            and not _verified_machine_relation(row)
        ):
            row.update(
                active=False,
                decision_status="legacy_unverified",
                verification_status="legacy_unverified",
                retirement_reason="relationship_prompt_v2_requires_verification",
            )
        migrated_rows.append(row)
    existing_rows = migrated_rows
    final_accepted_relations = [
        dict(row)
        for row in accepted_relations
        if isinstance(row, Mapping)
        and (
            _final_v4_relation(row)
            or _verified_machine_relation(row)
            or str(row.get("provenance") or "").startswith("human")
        )
    ]
    final_no_relationship_decisions = [
        dict(row)
        for row in no_relationship_decisions
        if isinstance(row, Mapping)
        and (
            _final_v4_decision(row, decision="no_relationship")
            or str(row.get("verification_status") or "") == "no_relationship"
        )
    ]
    verified_no_relationship_decisions = [
        dict(row)
        for row in final_no_relationship_decisions
    ]
    retained = [
        dict(row)
        for row in existing_rows
        if isinstance(row, Mapping)
        and (
            str(row.get("relation_type") or "") in SUBSTANTIVE_RELATION_TYPES
            or str(row.get("provenance") or "").startswith("human")
            or not bool(row.get("active", True))
            or preserve_unmentioned_structural
        )
    ]
    rows_by_id = {
        str(row.get("relation_id") or row.get("link_id") or stable_hash(row)): row
        for row in retained
    }
    for raw in structural_relations:
        row = dict(raw)
        row.setdefault("active", True)
        identity = str(
            row.get("relation_id") or row.get("link_id") or stable_hash(row)
        )
        rows_by_id[identity] = row
    if reconcile_machine_prompt_version:
        for row in rows_by_id.values():
            if (
                bool(row.get("active", True))
                and _machine_substantive(row)
                and str(row.get("prompt_version") or "")
                != reconcile_machine_prompt_version
            ):
                row.update(
                    active=False,
                    decision_status="retired",
                    retirement_reason="relationship_prompt_changed",
                    retirement_prompt_version=reconcile_machine_prompt_version,
                )
    orphaned = {str(value) for value in orphaned_source_ids if str(value)}
    if orphaned:
        for row in rows_by_id.values():
            if str(row.get("provenance") or "").startswith("human"):
                continue
            if {
                str(row.get("source_id") or ""),
                str(row.get("target_source_id") or ""),
            } & orphaned:
                row.update(
                    active=False,
                    decision_status="orphaned_source",
                    retirement_reason="source_unavailable_in_zotero_snapshot",
                )

    for decision in verified_no_relationship_decisions:
        pair = canonical_pair(
            str(decision.get("source_id") or ""),
            str(decision.get("target_source_id") or ""),
        )
        for row in rows_by_id.values():
            if not _same_pair(row, pair) or not _machine_substantive(row):
                continue
            row.update(
                active=False,
                decision_status="retired",
                retirement_reason="successfully_readjudicated_no_relationship",
                retirement_decision_hash=str(
                    decision.get("preliminary_decision_hash")
                    or stable_hash(decision)
                ),
            )

    for accepted in final_accepted_relations:
        row = dict(accepted)
        pair = canonical_pair(
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or ""),
        )
        for prior in rows_by_id.values():
            if (
                _same_pair(prior, pair)
                and _machine_substantive(prior)
                and str(prior.get("relation_id") or "")
                != str(row.get("relation_id") or "")
            ):
                prior.update(
                    active=False,
                    decision_status="superseded",
                    retirement_reason="new_accepted_relationship",
                )
        rows_by_id[str(row["relation_id"])] = row

    relations = sorted(
        rows_by_id.values(),
        key=lambda row: (
            str(row.get("source_id") or ""),
            str(row.get("target_source_id") or ""),
            str(row.get("relation_type") or ""),
            str(row.get("relation_id") or row.get("link_id") or ""),
        ),
    )
    links = [
        row
        for row in relations
        if bool(row.get("active", True))
        and (
            not _machine_substantive(row)
            or _publishable_machine_relation(row)
        )
    ]
    prior_decisions = (
        list(existing.get("pair_decisions", []) or [])
        if isinstance(existing, Mapping)
        else []
    )
    pair_decisions = {
        str(row.get("decision_key") or stable_hash(row)): dict(row)
        for row in prior_decisions
        if isinstance(row, Mapping)
    }
    for status, decisions in (
        ("accepted", final_accepted_relations),
        ("no_relationship", verified_no_relationship_decisions),
    ):
        for row in decisions:
            decision_key = relationship_decision_key(
                str(row.get("source_id") or ""),
                str(row.get("target_source_id") or ""),
                str(row.get("source_profile_hash") or ""),
                str(row.get("target_profile_hash") or ""),
                provider=str(row.get("provider") or ""),
                model=str(row.get("model") or ""),
                prompt_version=str(
                    row.get("prompt_version") or RELATIONSHIP_PROMPT_VERSION
                ),
            )
            decision_record = {
                **(
                    dict(row)
                    if str(row.get("output_contract") or "")
                    == RELATIONSHIP_DECISION_CONTRACT
                    else {}
                ),
                "decision_key": decision_key,
                "source_id": str(row.get("source_id") or ""),
                "target_source_id": str(row.get("target_source_id") or ""),
                "source_profile_hash": str(
                    row.get("source_profile_hash") or ""
                ),
                "target_profile_hash": str(
                    row.get("target_profile_hash") or ""
                ),
                "status": status,
                "relation_id": str(row.get("relation_id") or ""),
                "provider": str(row.get("provider") or ""),
                "model": str(row.get("model") or ""),
                "prompt_version": str(
                    row.get("prompt_version") or RELATIONSHIP_PROMPT_VERSION
                ),
                "verification_status": str(
                    row.get("verification_status") or ""
                ),
                "preliminary_decision_hash": str(
                    row.get("preliminary_decision_hash") or ""
                ),
                "verifier_provider": str(row.get("verifier_provider") or ""),
                "verifier_model": str(row.get("verifier_model") or ""),
                "verification_prompt_version": str(
                    row.get("verification_prompt_version")
                    or RELATIONSHIP_PROMPT_VERSION
                ),
            }
            pair_decisions[decision_key] = decision_record

    events = {
        str(row.get("event_id") or ""): dict(row)
        for row in (
            existing.get("events", []) or []
            if isinstance(existing, Mapping)
            else []
        )
        if isinstance(row, Mapping) and row.get("event_id")
    }
    for row in migrated_rows:
        if str(row.get("decision_status") or "") == "legacy_review_pending":
            _merge_registry_event(events, "legacy_review_pending", row)
    for row in final_accepted_relations:
        _merge_registry_event(events, "accepted", row)
    for row in verified_no_relationship_decisions:
        _merge_registry_event(events, "no_relationship", row)
    for row in parked_rows:
        if isinstance(row, Mapping):
            _merge_registry_event(events, "parked", row)
    for row in relations:
        if str(row.get("decision_status") or "") in {
            "retired",
            "superseded",
            "legacy_review_pending",
            "orphaned_source",
        }:
            _merge_registry_event(
                events, str(row.get("decision_status") or ""), row
            )
    event_rows = [events[key] for key in sorted(events)]
    parked = [
        dict(event.get("payload") or {})
        for event in event_rows
        if event.get("event_type") == "parked"
        and isinstance(event.get("payload"), Mapping)
    ]
    semantic = {
        "registry_schema_version": RELATIONSHIP_REGISTRY_SCHEMA_VERSION,
        "relations": relations,
        "links": links,
        "pair_decisions": [
            pair_decisions[key] for key in sorted(pair_decisions)
        ],
        "events": event_rows,
        "parked": parked,
    }
    revision_hash = stable_hash(semantic)
    if (
        isinstance(existing, Mapping)
        and str(existing.get("revision_hash") or "") == revision_hash
        and path.is_file()
        and compatibility_path.is_file()
    ):
        payload = dict(existing)
    else:
        payload = {
            "updated_at": now_iso(),
            **semantic,
            "revision_hash": revision_hash,
            "graph_projection_hash": stable_hash(links),
            "relation_counts": _relation_counts(links),
        }
        write_yaml(path, payload)
        write_yaml(compatibility_path, payload)
    compatibility = read_yaml(compatibility_path, {}) or {}
    if compatibility != payload:
        write_yaml(compatibility_path, payload)
    return {
        "path": str(path),
        "compatibility_path": str(compatibility_path),
        "links": list(payload.get("links", []) or []),
        "relations": list(payload.get("relations", []) or []),
        "pair_decisions": list(payload.get("pair_decisions", []) or []),
        "events": list(payload.get("events", []) or []),
        "parked": list(payload.get("parked", []) or []),
        "link_count": len(payload.get("links", []) or []),
        "relation_counts": dict(payload.get("relation_counts", {}) or {}),
        "graph_projection_hash": str(payload.get("graph_projection_hash") or ""),
        "revision_hash": str(payload.get("revision_hash") or ""),
    }


def projected_related_links(
    source_id: str,
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]],
    *,
    max_inferred_links: int,
) -> list[dict[str, Any]]:
    profile_rows = [profile_row(value) for value in profiles]
    active = [dict(row) for row in relations if bool(row.get("active", True))]
    structural = [
        row
        for row in active
        if str(row.get("relation_type") or "") in TYPED_SOURCE_RELATIONS
    ]
    projected = rank_human_related_links(
        source_id,
        profile_rows,
        structural,
        max_inferred_links=max_inferred_links,
    )
    by_source = {
        str(row.get("source_id") or ""): row for row in profile_rows
    }
    substantive: list[dict[str, Any]] = []
    for row in active:
        if str(row.get("relation_type") or "") not in SUBSTANTIVE_RELATION_TYPES:
            continue
        if _machine_substantive(row) and not _publishable_machine_relation(row):
            continue
        left = str(row.get("source_id") or "")
        right = str(row.get("target_source_id") or "")
        if source_id == left:
            target = right
            relation_type = str(
                row.get("forward_label") or row.get("relation_type") or ""
            )
        elif source_id == right:
            target = left
            relation_type = str(
                row.get("inverse_label")
                or row.get("reciprocal_type")
                or RECIPROCAL_RELATION_TYPES.get(
                    str(row.get("relation_type") or ""),
                    str(row.get("relation_type") or ""),
                )
            )
        else:
            continue
        if target not in by_source:
            continue
        substantive.append(
            {
                "relation_id": str(
                    row.get("relation_id") or row.get("link_id") or ""
                ),
                "target_source_id": target,
                "target_note_id": str(by_source[target].get("note_id") or ""),
                "target_title": str(by_source[target].get("title") or target),
                "relation_types": [relation_type],
                "primary_relation_type": relation_type,
                "reason": str(row.get("reason") or ""),
                "explicit": False,
                "strength": int(row.get("strength") or 110),
            }
        )
    combined = [*substantive, *projected]
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in combined:
        key = (
            str(row.get("target_note_id") or ""),
            str(row.get("primary_relation_type") or ""),
        )
        deduped.setdefault(key, row)
    return sorted(
        deduped.values(),
        key=lambda row: (
            -int(row.get("strength") or 0),
            str(row.get("target_source_id") or ""),
            str(row.get("primary_relation_type") or ""),
        ),
    )


def _job_anchor_rows(
    job: Mapping[str, Any],
    *,
    side: str,
    source_id: str,
    profile: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    selected = (
        job.get("selected_evidence")
        if isinstance(job.get("selected_evidence"), Mapping)
        else {}
    )
    values = selected.get(side) or selected.get(source_id) or []
    profile_anchors = {
        _evidence_id(row): row
        for row in (
            dict(value)
            if isinstance(value, Mapping)
            else value.to_dict()
            if callable(getattr(value, "to_dict", None))
            else {}
            for value in (
                profile.get("evidence_anchors")
                or profile.get("claims")
                or profile.get("findings")
                or []
            )
        )
        if _evidence_id(row)
        and str(row.get("source_id") or source_id) == source_id
    }
    # The adjudicator receives the current endpoint profiles as well as this
    # preselected routing subset. The subset helps navigation; it must not make
    # another source-owned anchor that the model actually saw invalid.
    anchors: dict[str, dict[str, Any]] = dict(profile_anchors)
    for value in values:
        row = (
            dict(value)
            if isinstance(value, Mapping)
            else value.to_dict()
            if callable(getattr(value, "to_dict", None))
            else profile_anchors.get(str(value), {})
        )
        anchor_id = _evidence_id(row)
        owner = str(row.get("source_id") or source_id)
        if anchor_id and owner == source_id:
            anchors[anchor_id] = profile_anchors.get(anchor_id, row)
    return anchors


def _job_profile(
    job: Mapping[str, Any], *, side: str, source_id: str
) -> dict[str, Any]:
    profiles = (
        job.get("profiles") if isinstance(job.get("profiles"), Mapping) else {}
    )
    value = profiles.get(side) or profiles.get(source_id) or {}
    try:
        return profile_row(value)
    except ValueError:
        return {}


def _normalized_v4_decision(
    decision: RelationshipDecision,
    *,
    job: RelationshipPairJob,
    job_row: Mapping[str, Any],
    left_anchors: Mapping[str, Mapping[str, Any]],
    right_anchors: Mapping[str, Mapping[str, Any]],
    provider: str,
    model: str,
    reasoner_backend: str,
    prompt_version: str,
    profiles_by_source: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    left_profile = dict(
        profiles_by_source.get(job.left_source_id)
        or _job_profile(
            job_row, side="left", source_id=job.left_source_id
        )
    )
    right_profile = dict(
        profiles_by_source.get(job.right_source_id)
        or _job_profile(
            job_row, side="right", source_id=job.right_source_id
        )
    )
    common = {
        "decision_schema_version": decision.output_contract.rsplit("v", 1)[-1],
        "output_contract": decision.output_contract,
        "pair_job_id": job.pair_job_id,
        "catalogue_revision": job.catalogue_revision,
        "left_source_id": job.left_source_id,
        "right_source_id": job.right_source_id,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "provider": provider,
        "model": model,
        "reasoner_backend": reasoner_backend,
        "prompt_version": str(prompt_version),
        "source_profile_hash": profile_content_hash(left_profile),
        "target_profile_hash": profile_content_hash(right_profile),
        "verification_status": "final",
    }
    if decision.decision != "relationship":
        return {
            **common,
            "source_id": job.left_source_id,
            "target_source_id": job.right_source_id,
            "decision": decision.decision,
            "decision_status": decision.decision,
            "comparison_proposition": decision.comparison_proposition,
            "boundary_or_qualification": decision.boundary_or_qualification,
            "active": False,
        }

    actor_is_left = decision.actor_source_id == job.left_source_id
    source_profile = left_profile if actor_is_left else right_profile
    target_profile = right_profile if actor_is_left else left_profile
    source_anchor_ids = (
        decision.left_evidence_anchor_ids
        if actor_is_left
        else decision.right_evidence_anchor_ids
    )
    target_anchor_ids = (
        decision.right_evidence_anchor_ids
        if actor_is_left
        else decision.left_evidence_anchor_ids
    )
    source_anchors = left_anchors if actor_is_left else right_anchors
    target_anchors = right_anchors if actor_is_left else left_anchors
    source_anchor = source_anchors[source_anchor_ids[0]]
    target_anchor = target_anchors[target_anchor_ids[0]]
    forward_label, inverse_label = RELATIONSHIP_PROJECTION_LABELS[
        decision.relation_type
    ]
    return {
        **common,
        "source_profile_hash": profile_content_hash(source_profile),
        "target_profile_hash": profile_content_hash(target_profile),
        "decision": "relationship",
        "decision_status": "accepted",
        "relation_id": _relation_id(
            decision.actor_source_id,
            decision.reference_source_id,
            decision.relation_type,
        ),
        "source_id": decision.actor_source_id,
        "target_source_id": decision.reference_source_id,
        "source_note_id": str(source_profile.get("note_id") or ""),
        "target_note_id": str(target_profile.get("note_id") or ""),
        "relation_type": decision.relation_type,
        "relationship_tier": decision.relationship_tier,
        "reciprocal_type": RECIPROCAL_RELATION_TYPES[decision.relation_type],
        "forward_label": forward_label,
        "inverse_label": inverse_label,
        "comparison_proposition": decision.comparison_proposition,
        "boundary_or_qualification": decision.boundary_or_qualification,
        "left_evidence_anchor_ids": list(decision.left_evidence_anchor_ids),
        "right_evidence_anchor_ids": list(decision.right_evidence_anchor_ids),
        "source_evidence_anchor_ids": list(source_anchor_ids),
        "target_evidence_anchor_ids": list(target_anchor_ids),
        "source_evidence": _evidence_reference(
            decision.actor_source_id, source_anchor_ids[0], source_anchor
        ),
        "target_evidence": _evidence_reference(
            decision.reference_source_id, target_anchor_ids[0], target_anchor
        ),
        "provenance": (
            "probabilistic_relationship_adjudication_"
            + decision.output_contract.rsplit("-", 1)[-1]
        ),
        "cluster_evidence_eligible": True,
        "inferred": True,
        "strength": 110,
        "active": True,
    }


def canonical_pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))  # type: ignore[return-value]


def _confidence(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 1 else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    return list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )


def _evidence_id(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(
        value.get("evidence_anchor_id")
        or value.get("claim_id")
        or value.get("finding_id")
        or ""
    )


def _anchor(profile: Mapping[str, Any], anchor_id: str) -> dict[str, Any] | None:
    if not anchor_id:
        return None
    for value in (
        profile.get("evidence_anchors")
        or profile.get("claims")
        or profile.get("findings")
        or []
    ):
        row = (
            dict(value)
            if isinstance(value, Mapping)
            else value.to_dict()
            if callable(getattr(value, "to_dict", None))
            else {}
        )
        if _evidence_id(row) == anchor_id:
            return row
    return None


def _anchor_is_substantive(anchor: Mapping[str, Any]) -> bool:
    locator = str(anchor.get("locator") or "").strip()
    envelope = (
        anchor.get("support_envelope")
        if isinstance(anchor.get("support_envelope"), Mapping)
        else {}
    )
    return bool(
        locator
        and str(envelope.get("support_status") or "supported")
        in {"supported", "limited"}
    )


def _limited_profile(profile: Mapping[str, Any]) -> bool:
    context = (
        profile.get("context")
        if isinstance(profile.get("context"), Mapping)
        else {}
    )
    status = str(
        context.get("note_status")
        or profile.get("note_status")
        or profile.get("status")
        or ""
    )
    return bool(profile.get("excluded_from_synthesis")) or status in _LIMITED_STATUSES


def _limited_anchor_can_support(anchor: Mapping[str, Any] | None) -> bool:
    if anchor is None:
        return False
    envelope = (
        anchor.get("support_envelope")
        if isinstance(anchor.get("support_envelope"), Mapping)
        else {}
    )
    return bool(
        str(envelope.get("support_status") or "") in {"supported", "limited"}
        and str(envelope.get("coverage") or "")
        in {"full_text", "limited_text", "abstract"}
    )


def _evidence_reference(
    source_id: str, anchor_id: str, anchor: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "evidence_anchor_id": anchor_id,
        "locator": str(anchor.get("locator") or ""),
        "claim": str(
            anchor.get("claim")
            or anchor.get("proposition")
            or anchor.get("text")
            or ""
        ),
    }


def _relation_id(source_id: str, target_id: str, relation_type: str) -> str:
    return (
        "substantive-relation-"
        + stable_hash([source_id, target_id, relation_type])[:16]
    )


def _same_pair(row: Mapping[str, Any], pair: tuple[str, str]) -> bool:
    return canonical_pair(
        str(row.get("source_id") or row.get("source_note_id") or ""),
        str(row.get("target_source_id") or row.get("target_note_id") or ""),
    ) == pair


def _machine_substantive(row: Mapping[str, Any]) -> bool:
    return bool(
        str(row.get("relation_type") or "") in SUBSTANTIVE_RELATION_TYPES
        and str(row.get("provenance") or "").startswith(
            "probabilistic_relationship_"
        )
    )


def _verified_machine_relation(row: Mapping[str, Any]) -> bool:
    return bool(
        _machine_substantive(row)
        and str(row.get("verification_status") or "")
        in {"confirmed", "corrected"}
    )


def _final_v4_relation(row: Mapping[str, Any]) -> bool:
    return bool(
        _machine_substantive(row)
        and (
            (
                str(row.get("output_contract") or "")
                == RELATIONSHIP_DECISION_CONTRACT
                and str(row.get("decision_schema_version") or "")
                == RELATIONSHIP_DECISION_SCHEMA_VERSION
            )
            or (
                (
                    str(row.get("output_contract") or ""),
                    str(row.get("decision_schema_version") or ""),
                )
                in {
                    ("relationship-decision-v4", "4"),
                    ("relationship-decision-v5", "5"),
                }
            )
        )
        and str(row.get("decision_status") or "") == "accepted"
        and str(row.get("verification_status") or "") == "final"
    )


def _final_v4_decision(
    row: Mapping[str, Any], *, decision: str
) -> bool:
    return bool(
        (
            (
                str(row.get("output_contract") or "")
                == RELATIONSHIP_DECISION_CONTRACT
                and str(row.get("decision_schema_version") or "")
                == RELATIONSHIP_DECISION_SCHEMA_VERSION
            )
            or (
                (
                    str(row.get("output_contract") or ""),
                    str(row.get("decision_schema_version") or ""),
                )
                in {
                    ("relationship-decision-v4", "4"),
                    ("relationship-decision-v5", "5"),
                }
            )
        )
        and str(row.get("decision") or row.get("decision_status") or "")
        == decision
        and str(row.get("verification_status") or "") == "final"
    )


def _publishable_machine_relation(row: Mapping[str, Any]) -> bool:
    return (
        _final_v4_relation(row)
        or _verified_machine_relation(row)
        or (
            str(row.get("relationship_tier") or "")
            in {"direct", "legacy_unclassified"}
            and str(row.get("output_contract") or "")
            in {"relationship-decision-v4", "relationship-decision-v5"}
            and str(row.get("verification_status") or "") == "final"
        )
    )


def _merge_registry_event(
    events: dict[str, dict[str, Any]],
    event_type: str,
    row: Mapping[str, Any],
) -> None:
    payload = dict(row)
    pair = canonical_pair(
        str(payload.get("source_id") or payload.get("left_source_id") or ""),
        str(
            payload.get("target_source_id")
            or payload.get("right_source_id")
            or ""
        ),
    )
    identity = {
        "event_type": event_type,
        "pair_job_id": str(payload.get("pair_job_id") or ""),
        "relation_id": str(payload.get("relation_id") or ""),
        "decision_key": str(payload.get("decision_key") or ""),
        "pair": pair,
        "reason": (
            str(payload.get("reason") or "")
            if event_type == "parked"
            else ""
        ),
    }
    event_id = str(payload.get("event_id") or stable_hash(identity))
    prior = events.get(event_id, {})
    history = {
        stable_hash(value): dict(value)
        for value in prior.get("payload_history", []) or []
        if isinstance(value, Mapping)
    }
    prior_payload = prior.get("payload")
    if isinstance(prior_payload, Mapping):
        history[stable_hash(prior_payload)] = dict(prior_payload)
    history[stable_hash(payload)] = payload
    events[event_id] = {
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload,
        "payload_history": [history[key] for key in sorted(history)],
    }


def _relation_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        relation_type = str(row.get("relation_type") or "")
        counts[relation_type] = counts.get(relation_type, 0) + 1
    return dict(sorted(counts.items()))


def _dedupe_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    by_hash = {
        stable_hash(row): dict(row)
        for row in rows
        if isinstance(row, Mapping)
    }
    return [by_hash[key] for key in sorted(by_hash)]
