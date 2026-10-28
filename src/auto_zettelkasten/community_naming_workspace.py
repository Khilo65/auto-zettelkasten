"""Optional cached community naming; source notes and graph topology stay intact."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .community_labeling import bind_naming_corpus, naming_partitions
from .community_naming import _digest
from .community_naming_pipeline import (
    CommunityNamingCache, DEFAULT_MODEL, prepare_two_stage_naming, validate_final_result,
)
from .documents import DocumentService
from .files import write_json
from .local_mapping import _exclusive_cache
from .mapping import _bounded_json
from .note_recall import _safe_path
from .workspace import resolve_workspace


def _without_labels(mapping):
    result = deepcopy(mapping)
    for rows in [result["communities"], *[view["communities"] for view in result.get("community_scales", [])]]:
        for row in rows:
            for field in ("display_label", "display_label_kind", "naming_identity"):
                row.pop(field, None)
    return result


def project_two_stage_names(mapping, preview, final, provenance, *, scales):
    """Project validated names only onto exact matching copies of a partition."""
    labels = validate_final_result(final, preview)
    expected = {row["community_id"]: sorted(row["source_ids"]) for row in preview["groups"]}
    result = deepcopy(mapping)
    provenance = provenance or {}
    origin = provenance.get("origin")
    if origin not in {"imported_saved", "generated"}:
        raise ValueError("Community names require recorded result provenance.")
    identity = _digest({"evidence_identity": preview["evidence_identity"],
                        "final": labels, "provenance": provenance})
    changed, unresolved = set(), set()

    def update(rows):
        actual = {row["community_id"]: sorted(row["source_ids"]) for row in rows}
        if len(actual) != len(rows) or actual != expected:
            raise ValueError("Community membership changed before label projection.")
        for row in rows:
            title = labels[row["community_id"]]["title"]
            if title is None:
                unresolved.add(row["community_id"])
                continue
            projected = {"display_label": title, "display_label_kind": "two_stage_" + origin,
                         "naming_identity": identity}
            if any(row.get(key) != value for key, value in projected.items()):
                changed.add(row["community_id"])
            row.update(projected)

    if "default" in scales:
        update(result["communities"])
    found = {"default"} if "default" in scales else set()
    for view in result.get("community_scales", []):
        if view["scale"] in scales:
            update(view["communities"])
            found.add(view["scale"])
    if found != set(scales):
        raise ValueError("Requested community scale is unavailable.")
    if _without_labels(result) != _without_labels(mapping):
        raise ValueError("Naming projection must not change graph data.")
    return result, sorted(changed), sorted(unresolved)


def community_names_manifest(
    workspace, *, scale="default", apply=False, cache_path=None,
    override_root=None, documents=None, workspace_id=None, provider="openai", model=DEFAULT_MODEL,
    enabled=False, allow_cloud=False, approval=None, reader=None,
    input_price_per_million=None, output_price_per_million=None, pricing_identity="",
):
    """Preview/reuse names, and optionally publish labels under a fresh snapshot.

    Default operation cannot construct a reader or dispatch a provider request.
    Imported results remain distinguishable from fresh generation. Independent
    graph views with identical memberships share their cached naming result.
    """
    if scale not in {"broad", "default", "fine"} or any(type(flag) is not bool for flag in (apply, enabled, allow_cloud)):
        raise ValueError("Invalid community naming options.")
    root = resolve_workspace(workspace)
    map_path = _safe_path(root, ".auto-zettelkasten/mapping/result.json")
    mapping = _bounded_json(map_path, max_bytes=64 * 1024 * 1024)
    partitions, aliases = naming_partitions(mapping)
    if scale not in aliases:
        raise ValueError("Choose an available saved community scale.")
    canonical = aliases[scale]
    store = Path(cache_path).expanduser() if cache_path else map_path.parent / "community-naming.sqlite3"
    owned = documents is None
    if owned:
        overrides = Path(override_root).expanduser() if override_root else Path.home() / ".config/auto-zettelkasten/workspace-overrides"
        documents = DocumentService(journal_root=store.parent / "community-naming-operations", override_root=overrides)
    try:
        if owned:
            workspace_id = documents.open(root)["id"]
        elif not workspace_id or documents.workspace_path(workspace_id) != root:
            raise ValueError("Supply the current document workspace identity without reopening it.")
        corpus = bind_naming_corpus(mapping, documents.recall_corpus(workspace_id))
        preview = prepare_two_stage_naming(partitions[canonical], corpus, provider=provider, model=model,
                                           partition_identity=canonical)
        cache = CommunityNamingCache(store)
        cached = cache.replay(preview)
        if cached["final"] is None:
            from .community_naming_run import run_two_stage_naming
            if enabled and reader is None:
                if not allow_cloud or approval is None:
                    raise ValueError("Generation requires explicit cloud permission and separate naming approval.")
                if provider != "openai":
                    raise ValueError("Supply an explicitly compatible naming reader for this provider.")
                from .readers import OpenAICommunityNamingReader
                reader = OpenAICommunityNamingReader(model=model, allow_cloud=True)
            if enabled and not allow_cloud and getattr(reader, "is_cloud", True):
                raise ValueError("Cloud naming requires explicit allow_cloud permission.")
            naming = run_two_stage_naming(
                preview, cache, reader=reader, enabled=enabled, approval=approval,
                input_price_per_million=input_price_per_million,
                output_price_per_million=output_price_per_million,
                pricing_identity=pricing_identity, ledger_path=store.with_suffix(".spending.sqlite3"),
            )
            cached = cache.replay(preview)
        else:
            naming = cached
        report = {"status": cached["status"], "scale": scale, "canonical_scale": canonical,
                  "preview": preview, "naming": naming, "cache": cached,
                  "provider_calls": naming.get("provider_calls", 0), "applied": False,
                  "changed_community_ids": [], "jev_calls": 0}
        if not apply or cached["final"] is None:
            return report
        with _exclusive_cache(map_path.parent):
            current = _bounded_json(map_path, max_bytes=64 * 1024 * 1024)
            if current != mapping:
                raise ValueError("Saved graph changed; cached names retained but not applied.")
            fresh = bind_naming_corpus(current, documents.recall_corpus(workspace_id))
            if fresh != corpus:
                raise ValueError("Note evidence changed; cached names retained but not applied.")
            copies = sorted(name for name, value in aliases.items() if value == canonical)
            projected, changed, unresolved = project_two_stage_names(
                mapping, preview, cached["final"], cached["final_provenance"], scales=copies)
            if projected != mapping:
                write_json(map_path, projected)
        report.update(status="applied" if changed else "unchanged", applied=True,
                      applied_scales=copies, changed_community_ids=changed, unresolved_community_ids=unresolved)
        return report
    finally:
        if owned:
            documents.close()
