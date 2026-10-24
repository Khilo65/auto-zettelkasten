"""Refresh discovery from validated saved vectors, without acquiring or encoding text."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import copy
import shutil
import time
from typing import Any, Mapping

from .files import atomic_write_text, now_iso
from .local_mapping import LocalMappingError, LocalMappingOptions, _communities_from_neighbors, _digest, _exclusive_cache, _json, scoped_source_neighbors
from .mapping import _bounded_json
from .mapping_calibration import community_resolution_checks, load_cached_source_vectors
from .mapping_text import load_mapping_inputs
from .workspace import confined_child, resolve_workspace


def _verify_inputs(inputs: Mapping[str, Any]) -> None:
    for filename, expected in inputs["input_stamps"].items():
        path = Path(filename)
        if path.is_symlink() or not path.is_file():
            raise LocalMappingError("Discovery inputs changed; the previous map was preserved.")
        value = path.stat()
        if [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns] != list(expected):
            raise LocalMappingError("Discovery inputs changed; the previous map was preserved.")


def refresh_cached_discovery(
    workspace: Path | str, *, options: Mapping[str, Any] | None = None,
    preview_decisions: bool = False, max_pairs: int = 256, per_source_cap: int = 8,
) -> dict[str, Any]:
    """Rebuild one map over its saved source scope; never regenerate missing vectors.

    Collection keys come from the current canonical catalogue. Missing or stale
    inputs fail before publication. A decision preview is always dry-run, with
    zero spending allowance; this entry point has no paid-dispatch option.
    """
    if type(preview_decisions) is not bool:
        raise ValueError("preview_decisions must be a boolean")
    if type(max_pairs) is not int or not 1 <= max_pairs <= 256 or type(per_source_cap) is not int or not 1 <= per_source_cap <= 64:
        raise ValueError("Discovery previews require bounded pair and endpoint limits.")
    if options is not None and not isinstance(options, Mapping):
        raise ValueError("Discovery refresh options must be an object.")
    settings = LocalMappingOptions.from_dict({"discovery_scope": "collection_and_library", "tuning_mode": "auto", **dict(options or {})})
    if settings.discovery_scope != "collection_and_library":
        raise ValueError("Cached scoped refresh requires collection_and_library discovery.")
    root = resolve_workspace(workspace)
    cache = confined_child(root, ".auto-zettelkasten", "mapping")
    result_path = cache / "result.json"
    started = time.monotonic()

    def check_resources():
        if time.monotonic() - started > settings.max_wall_seconds:
            raise LocalMappingError("Discovery refresh reached max_wall_seconds; the previous map was preserved.")
        if shutil.disk_usage(root).free < settings.min_free_disk_bytes:
            raise LocalMappingError("Discovery refresh reached its free-disk safeguard.")

    with _exclusive_cache(cache):
        check_resources()
        vectors = load_cached_source_vectors(cache, options=settings, check_resources=check_resources)
        saved = vectors["result"]
        sources = saved["sources"]
        source_ids = [row["source_id"] for row in sources]
        from .local_tools import load_local_retrieval
        canonical = load_local_retrieval(root, infer_scope=True)
        inputs = load_mapping_inputs(root, source_ids=source_ids, note_rows=canonical["rows"])
        current = {row["source_id"]: row for row in inputs["projection_rows"]}
        if set(current) != set(source_ids):
            raise LocalMappingError("Some saved sources are unavailable; refresh their evidence before discovery.")
        for source in sources:
            row = current[source["source_id"]]
            text_input = inputs["text_sources"].get(source["source_id"], {})
            if any(source.get(key, "") != row.get(key, "") for key in
                   ("note_id", "note_path", "projection_hash", "metadata_file_sha256", "profile_file_sha256")) or (
                    not source.get("full_text_sha256") or source["full_text_sha256"] != text_input.get("content_sha256")
                    or source.get("input_representation") != text_input.get("text_origin")
                    or source.get("coverage", {}).get("input") != text_input.get("coverage")):
                raise LocalMappingError("Saved embedding evidence is stale; no model was loaded and the previous map was preserved.")
        ids = vectors["source_ids"]
        matrix = vectors["vectors"]
        memberships = {sid: current[sid].get("collection_keys", []) for sid in ids}
        scoped = scoped_source_neighbors(ids, matrix, memberships, options=settings,
                                         embedding_identity=vectors["embedding_identity"], check_resources=check_resources)
        neighbors, candidates = scoped["neighbors"], scoped["candidate_neighbors"]
        by_source = {row["source_id"]: row for row in sources}
        for row in neighbors:
            row["provenance"].update(source_chunk_ids=by_source[row["source_id"]]["chunk_ids"],
                                     target_chunk_ids=by_source[row["target_source_id"]]["chunk_ids"])
        tuning = {"mode": settings.tuning_mode, "status": "manual", "calibration_status": "not_applied_discovery_scope_changed"}
        if settings.tuning_mode == "auto" and settings.clustering == "leiden":
            stability = community_resolution_checks(ids, neighbors, check_resources=check_resources)
            settings = replace(settings, resolution=stability["selected_resolution"], seed=stability["selected_seed"])
            tuning.update(status="default_policy_local_stability_checked", community_checks=stability["community_scales"],
                          limitations=stability["limitations"])
        from .community_naming import deterministic_community_label

        def groups(resolution):
            check_resources()
            values = _communities_from_neighbors(ids, matrix, neighbors, options=replace(settings, resolution=resolution))
            for group in values:
                group["display_label"] = deterministic_community_label([
                    {"source_id": sid, "title": current[sid].get("title") or sid} for sid in group["source_ids"]])
                group["display_label_kind"] = "deterministic_terms"
            return values

        communities = groups(settings.resolution)
        scales = [{"scale": scale, "resolution": resolution,
                   "communities": communities if resolution == settings.resolution else groups(resolution)}
                  for scale, resolution in (("broad", .5), ("default", settings.resolution), ("fine", 2.))] if settings.clustering == "leiden" else []
        result = copy.deepcopy(saved)
        for key in ("experiment", "experiment_provenance"):
            result.pop(key, None)
        identity = result["identity"]
        identity.pop("experiment", None)
        identity["discovery"] = scoped["scope_identity"]
        identity["clustering"].update({key: getattr(settings, key) for key in
                                       ("clustering", "neighbors", "min_similarity", "seed", "resolution", "min_cluster_size")})
        identity.pop("digest", None)
        identity["digest"] = _digest(identity)
        for row in result["sources"]:
            row["collection_keys"] = current[row["source_id"]].get("collection_keys", [])
        result.update(neighbors=neighbors, candidate_neighbors=candidates, communities=communities,
                      community_scales=scales, tuning=tuning, scope_discovery_coverage=scoped["coverage"],
                      derived=True, accepted_relationships=False, relationship_status="derived_suggestions_only",
                      created_at=now_iso(), input_coverage=inputs["coverage"],
                      stats={"sources": len(sources), "included_sources": len(ids), "chunks": len(result["chunks"]),
                             "encoded_chunks": 0, "reused_chunks": len(result["chunks"]), "encoder_loads": 0,
                             "total_seconds": time.monotonic() - started, **vectors["stats"]})
        _verify_inputs(inputs)
        if _bounded_json(result_path, max_bytes=min(settings.max_cache_bytes, settings.max_text_bytes)) != saved:
            raise LocalMappingError("The saved map changed during discovery; no result was published.")
        serialized = _json(result) + "\n"
        if len(serialized.encode()) > min(settings.max_cache_bytes, settings.max_text_bytes):
            raise LocalMappingError("Refreshed discovery map exceeds its cache bound; previous map preserved.")
        check_resources()
        atomic_write_text(result_path, serialized)

    pair_scopes = {}
    for row in neighbors:
        pair = tuple(sorted((row["source_id"], row["target_source_id"])))
        pair_scopes.setdefault(pair, set()).update(row["provenance"]["discovery_scopes"])
    report = {"status": "complete", "map_path": str(result_path), "provider_calls": 0, "cost_usd": "0",
              "scope": "all_sources_in_saved_map", "scope_coverage": scoped["coverage"], "stats": result["stats"],
              "unique_suggestions": len(pair_scopes),
              "scope_suggestions": dict(Counter(scope for scopes in pair_scopes.values() for scope in scopes)),
              "community_counts": {row["scale"]: len(row["communities"]) for row in scales}}
    if preview_decisions:
        from .relationship_mapping import run_relationship_mapping
        preview = run_relationship_mapping(root, candidates, options={"backend": "jev", "dry_run": True,
                                            "apply": False, "max_spend_usd": "0", "max_pairs": max_pairs,
                                            "per_source_cap": per_source_cap})
        admissions = [row.get("billing_admission") for row in preview.get("preview", [])]
        valid = [row for row in admissions if isinstance(row, dict)]
        # Bytes are a planning heuristic, not a provider token count or invoice guarantee.
        from .jev_billing import INPUT_USD_PER_TOKEN
        report["decision_preview"] = {
            "path": str(cache.parent / "relationship-mapping-preview.json"), "candidate_count": preview["candidate_count"],
            "request_count": preview["request_count"], "unavailable_count": len(preview["unavailable"]),
            "admitted_requests": len(valid), "estimated_input_cost_usd": str(sum(
                (Decimal(str(row["wire_bytes"])) * INPUT_USD_PER_TOKEN for row in valid), Decimal(0))),
            "estimated_cost_is_guaranteed": False,
            "model_usage_ceiling_usd": str(sum((Decimal(row["max_model_usage_usd"]) for row in valid), Decimal(0))),
            "ceiling_covers_all_requests": len(valid) == len(admissions), "guaranteed_invoice_cap": False,
            "budget_usd": "0", "paid_dispatch_enabled": False, "provider_calls": preview["provider_calls"],
            "reused_pair_count": preview["reused_pair_count"], "candidate_queue": preview["candidate_queue"],
        }
    return report
