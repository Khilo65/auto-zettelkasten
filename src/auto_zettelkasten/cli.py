from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .api import (
    build_map,
    doctor,
    export_to_obsidian,
    get_status,
    initialize_workspace,
    inventory,
    list_collections,
    resume_map,
    run_map,
)
from .models import (
    ExtractionPolicy,
    LiteratureMappingPolicy,
    MapRequest,
    NavigationPolicy,
    ProcessingPolicy,
)
from .migration import migrate_workspace
from .workspace import load_config

DEFAULT_MODELS = {
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-2.5-flash",
    "ollama": "llama3.2",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-zettelkasten", description="Map Zotero libraries into atomic notes and literature graphs.")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Initialize a standalone workspace.")
    init_parser.add_argument("workspace", type=Path)
    init_parser.add_argument("--overwrite", action="store_true")

    doctor_parser = commands.add_parser("doctor", help="Check local prerequisites and privacy configuration.")
    doctor_parser.add_argument("--workspace", type=Path, required=True)

    zotero_parser = commands.add_parser("zotero", help="Inspect Zotero Desktop through its read-only local API.")
    zotero_commands = zotero_parser.add_subparsers(dest="zotero_command", required=True)
    zotero_commands.add_parser("collections", help="List all Zotero collections.")
    inventory_parser = zotero_commands.add_parser("inventory", help="Inventory a Zotero scope without reading documents.")
    inventory_parser.add_argument("--workspace", type=Path, required=True)
    inventory_parser.add_argument("--run-id", required=True)
    inventory_parser.add_argument("--scope", choices=("library", "collection", "selected"), default="library")
    inventory_parser.add_argument("--collection", default="")
    inventory_parser.add_argument("--limit", type=int, default=0)

    map_parser = commands.add_parser("map", help="Exhaustively attempt a Zotero library or collection.")
    map_parser.add_argument("--workspace", type=Path, required=True)
    map_parser.add_argument("--scope", choices=("library", "collection", "selected"), default=None)
    map_parser.add_argument("--collection", default="")
    map_parser.add_argument("--question", default="")
    map_parser.add_argument("--provider", choices=("deepseek", "openrouter", "gemini", "ollama"), default=None)
    map_parser.add_argument("--model", default=None)
    map_parser.add_argument("--allow-cloud", action="store_true", default=None)
    map_parser.add_argument("--parallel", type=int, default=None)
    map_parser.add_argument("--limit", type=int, default=None)
    map_parser.add_argument("--run-id", default="")
    map_parser.add_argument("--ocr", choices=("auto", "off", "required"), default=None)
    map_parser.add_argument(
        "--ocr-language",
        action="append",
        default=None,
        help="Tesseract language code; repeat for additional installed languages.",
    )
    map_parser.add_argument("--direct-read-char-limit", type=int, default=None)
    map_parser.add_argument("--chunk-char-limit", type=int, default=None)
    map_parser.add_argument("--max-total-chunks", type=int, default=None)
    map_parser.add_argument("--max-document-calls", type=int, default=None)
    map_parser.add_argument("--request-deadline-seconds", type=float, default=None)
    map_parser.add_argument("--document-deadline-seconds", type=float, default=None)
    map_parser.add_argument("--chunk-output-tokens", type=int, default=None)
    map_parser.add_argument("--synthesis-output-tokens", type=int, default=None)
    map_parser.add_argument("--context-window-fraction", type=float, default=None)
    map_parser.add_argument("--estimated-chars-per-token", type=float, default=None)
    _add_literature_policy_arguments(map_parser)
    _add_navigation_policy_arguments(map_parser)

    resume_parser = commands.add_parser("resume", help="Resume an interrupted or partially terminal run.")
    resume_parser.add_argument("--workspace", type=Path, required=True)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--retry-terminal-literature", action="store_true")

    status_parser = commands.add_parser("status", help="Show workspace or run status.")
    status_parser.add_argument("--workspace", type=Path, required=True)
    status_parser.add_argument("--run-id", default="")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    build_parser_command = commands.add_parser("build-map", help="Rebuild typed links, clusters, gaps, and indexes from validated notes.")
    build_parser_command.add_argument("--workspace", type=Path, required=True)
    build_parser_command.add_argument("--run-id", default="")
    build_parser_command.add_argument("--source-set", default="")
    build_parser_command.add_argument("--question", default="")
    build_parser_command.add_argument("--provider", choices=("deepseek", "openrouter", "gemini", "ollama"), default=None)
    build_parser_command.add_argument("--model", default=None)
    build_parser_command.add_argument("--allow-cloud", action="store_true", default=None)
    build_parser_command.add_argument("--resume", action="store_true")
    build_parser_command.add_argument("--retry-terminal-literature", action="store_true")
    _add_literature_policy_arguments(build_parser_command)
    _add_navigation_policy_arguments(build_parser_command)

    migrate_parser = commands.add_parser("migrate", help="Archive legacy generated maps for the current artifact schema.")
    migrate_parser.add_argument("--workspace", type=Path, required=True)
    migrate_parser.add_argument("--dry-run", action="store_true")

    export_parser = commands.add_parser("export", help="Export generated Markdown projections.")
    export_commands = export_parser.add_subparsers(dest="export_command", required=True)
    obsidian_parser = export_commands.add_parser("obsidian", help="Export an Obsidian-ready vault projection.")
    obsidian_parser.add_argument("--workspace", type=Path, required=True)
    obsidian_parser.add_argument("--vault", type=Path)
    obsidian_parser.add_argument("--folder", default="Auto-Zettelkasten")
    obsidian_parser.add_argument("--project-folder", default="")
    obsidian_parser.add_argument("--replace", action="store_true")
    obsidian_parser.add_argument("--new-vault", action="store_true")
    obsidian_parser.add_argument("--dry-run", action="store_true")
    obsidian_parser.add_argument("--no-record-link", action="store_false", dest="record_link")
    obsidian_parser.set_defaults(record_link=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_workspace(args.workspace, overwrite=args.overwrite).to_dict()
        elif args.command == "doctor":
            result = doctor(args.workspace).to_dict()
        elif args.command == "zotero" and args.zotero_command == "collections":
            result = {"status": "ok", "collections": list_collections()}
        elif args.command == "zotero" and args.zotero_command == "inventory":
            result = inventory(args.workspace, args.run_id, args.scope, args.collection, args.limit)
        elif args.command == "map":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = str(config.get("model") or "") if provider == configured_provider else ""
            model = args.model or configured_model or DEFAULT_MODELS.get(provider, "")
            extraction_config = (
                config.get("extraction", {})
                if isinstance(config.get("extraction", {}), dict)
                else {}
            )
            request = MapRequest(
                workspace=args.workspace,
                scope=args.scope or str(config.get("scope") or "library"),
                collection_key=args.collection or None,
                question=args.question or None,
                provider=provider,
                model=model,
                allow_cloud=args.allow_cloud is True,
                parallel=args.parallel if args.parallel is not None else int(config.get("parallel", 4)),
                limit=args.limit if args.limit is not None else 0,
                extraction_version=str(extraction_config.get("version") or "2"),
                prompt_version=str(config.get("prompt_version") or "9"),
                extraction_policy=_extraction_policy(args, config),
                processing=_processing_policy(args, config),
                literature_policy=_literature_policy(args, config),
                navigation_policy=_navigation_policy(args, config),
            )
            result = run_map(request, run_id=args.run_id or None).to_dict()
        elif args.command == "resume":
            result = resume_map(
                args.workspace,
                args.run_id,
                retry_terminal_failures=args.retry_terminal_literature,
            ).to_dict()
        elif args.command == "status":
            report = get_status(args.workspace, args.run_id or None)
            result = report.to_dict()
            if not args.as_json:
                print(_status_text(result))
                return _exit_code(result)
        elif args.command == "build-map":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = str(config.get("model") or "") if provider == configured_provider else ""
            model = args.model or configured_model or DEFAULT_MODELS.get(provider, "")
            result = build_map(
                args.workspace,
                run_id=args.run_id or None,
                source_set=args.source_set or None,
                question=args.question or None,
                provider=provider,
                model=model,
                allow_cloud=args.allow_cloud is True,
                literature_policy=_literature_policy(args, config),
                navigation_policy=_navigation_policy(args, config),
                resume=args.resume,
                retry_terminal_failures=args.retry_terminal_literature,
            ).to_dict()
        elif args.command == "migrate":
            result = migrate_workspace(args.workspace, dry_run=args.dry_run)
        elif args.command == "export" and args.export_command == "obsidian":
            config = load_config(args.workspace)
            obsidian = config.get("obsidian", {}) if isinstance(config.get("obsidian", {}), dict) else {}
            vault = args.vault or (Path(str(obsidian.get("vault"))).expanduser() if obsidian.get("vault") else None)
            if vault is None:
                raise ValueError("--vault is required when auto-zettelkasten.yml has no obsidian.vault")
            result = export_to_obsidian(
                args.workspace,
                vault,
                folder=args.folder,
                project_folder=args.project_folder,
                replace=args.replace,
                new_vault=args.new_vault,
                dry_run=args.dry_run,
                record_link=args.record_link,
            ).to_dict()
        else:  # pragma: no cover - argparse enforces reachable commands
            parser.error("unsupported command")
            return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return _exit_code(result)


def _status_text(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    progress = (payload.get("checks", {}) or {}).get("progress", {})
    return (
        f"Status: {payload.get('status', 'unknown')}\n"
        f"Run: {payload.get('run_id') or 'none'}\n"
        f"Stage: {progress.get('stage') or 'none'}\n"
        f"Inventory: {counts.get('inventory_count', 0)}\n"
        f"Validated notes: {counts.get('validated_note_count', 0)}\n"
        f"Limited notes: {counts.get('limited_note_count', 0)}\n"
        f"Exhausted: {counts.get('exhausted_count', 0)}\n"
        f"Partial: {counts.get('partial_count', 0)}\n"
        f"Pending: {counts.get('pending_count', 0)}\n"
        f"Profiles: {counts.get('profile_count', 0)}\n"
        f"Excluded profiles: {counts.get('profile_excluded_count', 0)}\n"
        f"Unclustered sources: {counts.get('unclustered_count', 0)}\n"
        f"Topic neighborhoods: {counts.get('topic_neighborhood_count', 0)}\n"
        f"Subject tags: {counts.get('subject_tag_count', 0)}\n"
        f"Typed source relations: {counts.get('typed_relation_count', 0)}\n"
        f"Propositions: {counts.get('proposition_count', 0)}\n"
        f"Effective evidence bases: {counts.get('evidence_base_group_count', 0)}\n"
        f"Clusters: {counts.get('cluster_count', 0)}\n"
        f"Synthesized clusters: {counts.get('synthesized_cluster_count', 0)}\n"
        f"Source contributions: {counts.get('cluster_source_contribution_count', 0)}\n"
        f"Debates: {counts.get('debate_count', 0)}\n"
        f"Collection-surviving gaps: {counts.get('mapped_gap_count', 0)}\n"
        f"Gap leads: {counts.get('gap_lead_count', 0)}\n"
        f"Rejected underspecified gaps: {counts.get('rejected_underspecified_gap_count', 0)}\n"
        f"Rejected quantitative comparisons: {counts.get('rejected_quantitative_comparison_count', 0)}\n"
        f"Rejected generated locators: {counts.get('rejected_generated_locator_count', 0)}\n"
        f"Mapped inventory coverage: {counts.get('coverage_inventory_count', 0)}\n"
        f"Synthesis calls: {counts.get('synthesis_call_count', 0)}\n"
        f"Synthesis checkpoint hits: {counts.get('synthesis_checkpoint_hit_count', 0)}\n"
        f"Provider calls: {counts.get('provider_call_count', 0)}\n"
        f"Checkpoint hits: {counts.get('checkpoint_hit_count', 0)}"
    )


def _processing_policy(args: argparse.Namespace, config: dict[str, Any]) -> ProcessingPolicy:
    configured = config.get("processing", {}) if isinstance(config.get("processing", {}), dict) else {}
    defaults = ProcessingPolicy.from_dict(configured)
    values = {
        "direct_read_char_limit": args.direct_read_char_limit,
        "chunk_char_limit": args.chunk_char_limit,
        "max_total_chunks": args.max_total_chunks,
        "max_calls_per_document_run": args.max_document_calls,
        "request_deadline_seconds": args.request_deadline_seconds,
        "document_deadline_seconds": args.document_deadline_seconds,
        "chunk_output_tokens": args.chunk_output_tokens,
        "synthesis_output_tokens": args.synthesis_output_tokens,
        "context_window_fraction": args.context_window_fraction,
        "estimated_chars_per_token": args.estimated_chars_per_token,
    }
    payload = {field: getattr(defaults, field) for field in defaults.__dataclass_fields__}
    payload.update({key: value for key, value in values.items() if value is not None})
    return ProcessingPolicy.from_dict(payload)


def _extraction_policy(
    args: argparse.Namespace, config: dict[str, Any]
) -> ExtractionPolicy:
    configured = (
        config.get("extraction", {})
        if isinstance(config.get("extraction", {}), dict)
        else {}
    )
    defaults = ExtractionPolicy.from_dict(configured)
    return ExtractionPolicy(
        ocr=args.ocr if args.ocr is not None else defaults.ocr,
        languages=tuple(args.ocr_language or defaults.languages),
    )


def _add_literature_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--synthesis", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-question", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--auto-promote-clusters", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--auto-promote-debates", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--auto-promote-gaps", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--cluster-threshold", type=int, default=None)
    parser.add_argument("--max-memberships", type=int, default=None)
    parser.add_argument("--external-discovery", choices=("disabled", "per_run", "always"), default=None)
    parser.add_argument("--max-profile-calls", type=int, default=None)
    parser.add_argument("--max-synthesis-calls", type=int, default=None)
    parser.add_argument("--profile-workers", type=int, default=None)
    parser.add_argument("--literature-deadline-seconds", type=float, default=None)
    parser.add_argument("--literature-context-fraction", type=float, default=None)
    parser.add_argument("--weak-gap-handling", choices=("audit_only",), default=None)
    parser.add_argument("--cluster-gap-projection", choices=("inline",), default=None)
    parser.add_argument("--require-executable-gap-design", action=argparse.BooleanOptionalAction, default=None)


def _literature_policy(args: argparse.Namespace, config: dict[str, Any]) -> LiteratureMappingPolicy:
    configured = config.get("literature_mapping", {}) if isinstance(config.get("literature_mapping", {}), dict) else {}
    defaults = LiteratureMappingPolicy.from_dict(configured)
    overrides = {
        "synthesis_enabled": getattr(args, "synthesis", None),
        "require_question": getattr(args, "require_question", None),
        "auto_promote_clusters": getattr(args, "auto_promote_clusters", None),
        "auto_promote_debates": getattr(args, "auto_promote_debates", None),
        "auto_promote_gaps": getattr(args, "auto_promote_gaps", None),
        "source_backed_threshold": getattr(args, "cluster_threshold", None),
        "max_memberships": getattr(args, "max_memberships", None),
        "external_discovery": getattr(args, "external_discovery", None),
        "max_profile_calls": getattr(args, "max_profile_calls", None),
        "max_synthesis_calls": getattr(args, "max_synthesis_calls", None),
        "profile_workers": getattr(args, "profile_workers", None),
        "literature_deadline_seconds": getattr(args, "literature_deadline_seconds", None),
        "deepseek_packet_context_fraction": getattr(args, "literature_context_fraction", None),
        "weak_gap_handling": getattr(args, "weak_gap_handling", None),
        "cluster_gap_projection": getattr(args, "cluster_gap_projection", None),
        "require_executable_gap_design": getattr(args, "require_executable_gap_design", None),
    }
    payload = defaults.to_dict()
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return LiteratureMappingPolicy.from_dict(payload)


def _add_navigation_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subject-tags", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--max-candidate-tags-per-source", type=int, default=None)
    parser.add_argument("--max-visible-tags-per-source", type=int, default=None)
    parser.add_argument("--max-visible-tags-per-cluster-or-gap", type=int, default=None)
    parser.add_argument("--min-sources-per-neighborhood", type=int, default=None)
    parser.add_argument("--max-visible-neighborhoods", type=int, default=None)
    parser.add_argument("--max-inferred-related-note-links", type=int, default=None)
    parser.add_argument(
        "--automatic-semantic-synonym-merging",
        action=argparse.BooleanOptionalAction,
        default=None,
    )


def _navigation_policy(args: argparse.Namespace, config: dict[str, Any]) -> NavigationPolicy:
    configured = config.get("navigation", {}) if isinstance(config.get("navigation", {}), dict) else {}
    defaults = NavigationPolicy.from_dict(configured)
    overrides = {
        "subject_tags_enabled": getattr(args, "subject_tags", None),
        "max_candidate_tags_per_source": getattr(args, "max_candidate_tags_per_source", None),
        "max_visible_tags_per_source": getattr(args, "max_visible_tags_per_source", None),
        "max_visible_tags_per_cluster_or_gap": getattr(args, "max_visible_tags_per_cluster_or_gap", None),
        "min_sources_per_neighborhood": getattr(args, "min_sources_per_neighborhood", None),
        "max_visible_neighborhoods": getattr(args, "max_visible_neighborhoods", None),
        "max_inferred_related_note_links": getattr(args, "max_inferred_related_note_links", None),
        "automatic_semantic_synonym_merging": getattr(args, "automatic_semantic_synonym_merging", None),
    }
    payload = defaults.to_dict()
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return NavigationPolicy.from_dict(payload)


def _exit_code(payload: dict[str, Any]) -> int:
    status = str(payload.get("status") or "")
    if status == "partial":
        return 3
    return 2 if status in {"blocked", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
