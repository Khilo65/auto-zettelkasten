"""Bounded direct-link comparison: supplied-note exposure, existing decision contracts."""
from __future__ import annotations

import json
from itertools import combinations
from typing import Any, Callable, Mapping, Sequence

from auto_zettelkasten.models import RelationshipPairJob
from auto_zettelkasten.pipeline import _relationship_evidence_projection
from auto_zettelkasten.profiles import profile_to_dict
from auto_zettelkasten.relationships import (
    ORDINARY_RELATIONSHIP_DECISION_CONTRACT,
    RELATIONSHIP_DISCOVERY_PROMPT_VERSION,
    canonical_pair,
    relationship_source_identity_error,
    stable_hash,
    validate_relationship_decision_rows,
)

BUCKETS = ("accepted", "no_relationship", "needs_more_context", "parked")
FIELDS = {"left_source_id", "right_source_id", "left_source_title", "right_source_title", "decision", "relation_type",
          "actor_source_id", "reference_source_id", "reason"}


def partition_descriptions(descriptions: Sequence[Mapping[str, Any]], *,
                           fits: Callable, max_records: int) -> list[list[dict[str, Any]]]:
    """Balance intact rows by serialized size; measure every two-block request."""
    rows = [dict(row) for row in descriptions]
    ids = [row.get("source_id") for row in rows]
    if any(not isinstance(value, str) or not value.strip() for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("descriptions require distinct exact source IDs")
    if not rows or fits(rows, [], max_records):
        return [rows] if rows else []
    weighted = sorted(enumerate(rows), key=lambda pair: (
        -len(json.dumps(pair[1], ensure_ascii=False, sort_keys=True).encode()), pair[0]))
    # ponytail: exhaustive block exposure is quadratic; this experiment measures its ceiling.
    for count in range(2, len(rows) + 1):
        blocks: list[list[tuple[int, dict[str, Any]]]] = [[] for _ in range(count)]
        sizes = [0] * count
        for index, row in weighted:
            target = min(range(count), key=lambda number: (sizes[number], number))
            blocks[target].append((index, row))
            sizes[target] += len(json.dumps(row, ensure_ascii=False, sort_keys=True).encode())
        result = [[row for _, row in sorted(block)] for block in blocks]
        if all(fits(left + right, [], max_records) for left, right in combinations(result, 2)):
            return result
    raise ValueError("two intact descriptions cannot fit the complete request")


def adapt_response(response: Mapping[str, Any], *, descriptions: Sequence[Mapping[str, Any]],
                   profiles: Sequence[Any], excluded_pairs: Sequence[Sequence[str]] = (),
                   provider: str = "", model: str = "",
                   require_source_titles: bool = True) -> tuple[dict[str, list], list[RelationshipPairJob]]:
    """Translate compact records without rejudging or introducing evidence requirements."""
    if not isinstance(response, Mapping) or set(response) != {"candidates"} or not isinstance(response["candidates"], list):
        raise ValueError("malformed direct response envelope")
    descriptions_by_id = {row["source_id"]: row for row in descriptions}
    profiles_by_id = {profile_to_dict(profile)["source_id"]: profile for profile in profiles}
    if not set(descriptions_by_id) <= profiles_by_id.keys():
        raise ValueError("supplied description has no frozen profile")
    excluded = {canonical_pair(*pair) for pair in excluded_pairs}
    result = {key: [] for key in BUCKETS}
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    source_titles = {source_id: row.get("title") for source_id, row in descriptions_by_id.items()}
    # Explicit compatibility for archived v4 responses; current execution always requires titles.
    fields = FIELDS if require_source_titles else FIELDS - {"left_source_title", "right_source_title"}
    for index, raw in enumerate(response["candidates"]):
        valid = isinstance(raw, Mapping) and set(raw) == fields
        if valid:
            valid = all(isinstance(raw[key], str) for key in fields - {"actor_source_id", "reference_source_id"})
            valid &= all(raw[key] is None or isinstance(raw[key], str) for key in ("actor_source_id", "reference_source_id"))
        if not valid:
            result["parked"].append({"row_index": index, "reason": "invalid_direct_record_shape", "raw": raw})
            continue
        pair = canonical_pair(raw["left_source_id"], raw["right_source_id"])
        if pair[0] == pair[1] or not set(pair) <= descriptions_by_id.keys():
            result["parked"].append({"row_index": index, "reason": "pair_not_in_supplied_notes", "raw": raw})
        elif pair in excluded:
            result["parked"].append({"row_index": index, "reason": "excluded_pair_returned", "raw": raw})
        elif require_source_titles and (error := relationship_source_identity_error(raw, source_titles)):
            result["parked"].append({"row_index": index, "reason": error, "raw": raw})
        else:
            by_pair.setdefault(pair, []).append(dict(raw))
    jobs, decisions = [], []
    for pair, candidates in by_pair.items():
        meanings = {stable_hash({key: row[key] for key in fields - {"left_source_id", "right_source_id", "left_source_title", "right_source_title"}}) for row in candidates}
        if len(meanings) != 1:
            result["parked"].append({"reason": "conflicting_duplicate_pair", "pair": list(pair), "raw": candidates})
            continue
        raw = candidates[0]
        job = RelationshipPairJob(
            catalogue_revision=stable_hash(descriptions), left_source_id=pair[0], right_source_id=pair[1],
            profiles={side: profile_to_dict(_relationship_evidence_projection(
                profiles_by_id[source_id], descriptions_by_id[source_id], include_anchors=False))
                for side, source_id in zip(("left", "right"), pair, strict=True)},
            candidate_basis=[raw], output_contract=ORDINARY_RELATIONSHIP_DECISION_CONTRACT)
        jobs.append(job)
        decisions.append({"pair_job_id": job.pair_job_id, "decision": raw["decision"],
                          "relation_type": raw["relation_type"], "actor_source_id": raw["actor_source_id"],
                          "reference_source_id": raw["reference_source_id"],
                          "comparison_proposition": raw["reason"], "reason": raw["reason"]})
    validated = validate_relationship_decision_rows(
        {"decisions": decisions}, jobs=jobs, profiles=profiles, provider=provider, model=model,
        reasoner_backend=provider, prompt_version=RELATIONSHIP_DISCOVERY_PROMPT_VERSION)
    for key in BUCKETS:
        result[key].extend(validated[key])
    return result, jobs


def run_direct(descriptions: Sequence[Mapping[str, Any]], profiles: Sequence[Any], *,
               call: Callable, fits: Callable, max_records: int,
               preserve_raw: Callable, persist_page: Callable,
               max_calls: int = 24, provider: str = "", model: str = "") -> dict[str, Any]:
    """Callbacks own transport/raw custody and durable persistence; no provider retries."""
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise ValueError("max_records must be a positive frozen integer")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or not 0 <= max_calls <= 24:
        raise ValueError("direct campaign permits at most 24 calls")
    blocks = partition_descriptions(descriptions, fits=fits, max_records=max_records)
    schedule = [(0,)] if len(blocks) == 1 else list(combinations(range(len(blocks)), 2))
    result: dict[str, Any] = {key: [] for key in BUCKETS}
    result.update(status="completed_paging", exhaustive_discovery=False, calls=0, jobs=[],
                  blocks=[[row["source_id"] for row in block] for block in blocks],
                  schedule=[list(item) for item in schedule], completed_requests=[])
    completed: set[tuple[str, str]] = set()
    for packet_index, block_indices in enumerate(schedule):
        packet = [row for block_index in block_indices for row in blocks[block_index]]
        ids = {row["source_id"] for row in packet}
        page = 0
        while True:
            exclusions = [list(pair) for pair in sorted(completed) if set(pair) <= ids]
            if result["calls"] >= max_calls:
                result["status"] = "incomplete_budget"
                return result
            if not fits(packet, exclusions, max_records):
                result["status"] = "incomplete_context"
                return result
            page_id = f"direct-{packet_index:04d}-{page:04d}"
            request = {"descriptions": packet, "excluded_pairs": exclusions, "max_records": max_records}
            result["calls"] += 1
            try:
                response = call(packet, exclusions, max_records)
            except Exception as exc:
                result.update(status="failed_call", error=f"{type(exc).__name__}: {exc}")
                return result
            preserve_raw(page_id, request, response)
            try:
                if isinstance(response, Mapping) and isinstance(response.get("candidates"), list) and len(response["candidates"]) > max_records:
                    raise ValueError("response exceeds frozen record capacity")
                batch, jobs = adapt_response(response, descriptions=packet, profiles=profiles,
                                             excluded_pairs=exclusions, provider=provider, model=model)
            except (TypeError, ValueError) as exc:
                result.update(status="failed_response", error=str(exc))
                return result
            persist_page(page_id, batch, jobs)
            for key in BUCKETS:
                result[key].extend(batch[key])
            result["jobs"].extend(job.to_dict() for job in jobs)
            valid_ids = {row["pair_job_id"] for key in ("accepted", "no_relationship") for row in batch[key]}
            completed.update((job.left_source_id, job.right_source_id) for job in jobs if job.pair_job_id in valid_ids)
            result["completed_requests"].append(page_id)
            if batch["parked"] or batch["needs_more_context"]:
                result["status"] = "failed_response"
                return result
            if len(response["candidates"]) < max_records:
                break
            if not valid_ids:
                result["status"] = "incomplete_no_progress"
                return result
            page += 1
    return result
