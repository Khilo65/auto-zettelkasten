from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .api import (
    build_map,
    doctor,
    estimate_cost,
    export_to_obsidian,
    get_status,
    initialize_workspace,
    inventory,
    list_collections,
    recall,
    prepare_recall,
    resume_map,
    run_map,
    sync_zotero,
)
from .models import (
    CURRENT_ATOMIC_PROMPT_VERSION,
    DEFAULT_CODEX_SOURCE_MODEL,
    ExtractionPolicy,
    LiteratureMappingPolicy,
    MapRequest,
    NavigationPolicy,
    ProcessingPolicy,
)
from .migration import migrate_workspace
from .workspace import load_config

DEFAULT_MODELS = {
    "codex": DEFAULT_CODEX_SOURCE_MODEL,
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-2.5-flash",
    "ollama": "llama3.2",
}
PROVIDER_CHOICES = ("deepseek", "openrouter", "gemini", "ollama", "codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-zettelkasten", description="Map Zotero libraries into atomic notes and literature graphs.")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="Open the local mapping interface.")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser.")

    commands.add_parser("desktop", help="Open the native macOS or Windows app.")

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
    map_parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    map_parser.add_argument("--model", default=None)
    map_parser.add_argument("--literature-model", default=None)
    map_parser.add_argument(
        "--reasoning-effort", choices=("medium", "high", "max"), default=None
    )
    map_parser.add_argument("--allow-cloud", action="store_true", default=None)
    map_parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Maximum concurrent local Zotero, extraction, and OCR operations.",
    )
    map_parser.add_argument(
        "--provider-concurrency",
        default=None,
        help="Maximum concurrent provider jobs, or 'auto' for the provider-specific cap.",
    )
    map_parser.add_argument("--max-provider-spend-usd", default=None)
    map_parser.add_argument("--limit", type=int, default=None)
    map_parser.add_argument("--run-id", default="")
    map_parser.add_argument("--ocr", choices=("auto", "off", "required"), default=None)
    map_parser.add_argument(
        "--pdf-fallback", choices=("none", "images", "ocr"), default=None
    )
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
    map_parser.add_argument("--connect-timeout-seconds", type=float, default=None)
    map_parser.add_argument(
        "--request-idle-timeout-seconds",
        "--request-deadline-seconds",
        dest="request_deadline_seconds",
        type=float,
        default=None,
        help=(
            "Maximum seconds without a meaningful provider stream event; "
            "--request-deadline-seconds is a deprecated alias."
        ),
    )
    map_parser.add_argument("--document-deadline-seconds", type=float, default=None)
    map_parser.add_argument("--chunk-output-tokens", type=int, default=None)
    map_parser.add_argument("--synthesis-output-tokens", type=int, default=None)
    map_parser.add_argument("--context-window-fraction", type=float, default=None)
    map_parser.add_argument("--estimated-chars-per-token", type=float, default=None)
    _add_literature_policy_arguments(map_parser)
    _add_local_mapping_arguments(map_parser)
    _add_navigation_policy_arguments(map_parser)

    sync_parser = commands.add_parser(
        "sync",
        help="Process new and changed Zotero records through the resumable mapper.",
    )
    sync_parser.add_argument("--workspace", type=Path, required=True)
    sync_parser.add_argument(
        "--scope", choices=("library", "collection", "selected"), default=None
    )
    sync_parser.add_argument("--collection", default="")
    sync_parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
    )
    sync_parser.add_argument("--model", default=None)
    sync_parser.add_argument("--literature-model", default=None)
    sync_parser.add_argument(
        "--reasoning-effort", choices=("medium", "high", "max"), default=None
    )
    sync_parser.add_argument("--allow-cloud", action="store_true", default=None)
    sync_parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Maximum concurrent local Zotero, extraction, and OCR operations.",
    )
    sync_parser.add_argument(
        "--provider-concurrency",
        default=None,
        help="Maximum concurrent provider jobs, or 'auto' for the provider-specific cap.",
    )
    sync_parser.add_argument("--max-provider-spend-usd", default=None)
    sync_parser.add_argument("--run-id", default="")
    sync_parser.add_argument(
        "--pdf-fallback", choices=("none", "images", "ocr"), default=None
    )
    _add_literature_policy_arguments(sync_parser)
    _add_local_mapping_arguments(sync_parser)
    _add_navigation_policy_arguments(sync_parser)

    resume_parser = commands.add_parser("resume", help="Resume an interrupted or partially terminal run.")
    resume_parser.add_argument("--workspace", type=Path, required=True)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--retry-terminal-literature", action="store_true")
    resume_parser.add_argument("--max-provider-spend-usd", default=None)

    status_parser = commands.add_parser("status", help="Show workspace or run status.")
    status_parser.add_argument("--workspace", type=Path, required=True)
    status_parser.add_argument("--run-id", default="")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    estimate_parser = commands.add_parser(
        "estimate", help="Estimate source and graph cost without provider calls."
    )
    estimate_parser.add_argument("--workspace", type=Path, required=True)
    estimate_parser.add_argument(
        "--scope", choices=("library", "collection", "selected"), default="library"
    )
    estimate_parser.add_argument("--collection", default="")
    estimate_parser.add_argument(
        "--graph-only",
        action="store_true",
        help="Estimate graph work from committed notes without inventorying live Zotero.",
    )
    estimate_parser.add_argument(
        "--provider", choices=PROVIDER_CHOICES, default=None
    )
    estimate_parser.add_argument("--model", default=None)
    estimate_parser.add_argument(
        "--synthesis", action=argparse.BooleanOptionalAction, default=None
    )
    estimate_parser.add_argument(
        "--clusters", action=argparse.BooleanOptionalAction, default=None
    )
    _add_local_mapping_arguments(estimate_parser)

    build_parser_command = commands.add_parser("build-map", help="Rebuild typed links, clusters, gaps, and indexes from validated notes.")
    build_parser_command.add_argument("--workspace", type=Path, required=True)
    build_parser_command.add_argument("--run-id", default="")
    build_parser_command.add_argument("--source-set", default="")
    build_parser_command.add_argument(
        "--compare-collection",
        action="append",
        default=[],
        dest="comparison_collection_keys",
        help="Zotero collection key to compare directly; repeat for each collection.",
    )
    build_parser_command.add_argument("--question", default="")
    build_parser_command.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    build_parser_command.add_argument("--model", default=None)
    build_parser_command.add_argument("--literature-model", default=None)
    build_parser_command.add_argument(
        "--reasoning-effort", choices=("medium", "high", "max"), default=None
    )
    build_parser_command.add_argument("--allow-cloud", action="store_true", default=None)
    build_parser_command.add_argument(
        "--provider-concurrency",
        default=None,
        help="Maximum concurrent provider jobs, or 'auto' for the provider-specific cap.",
    )
    build_parser_command.add_argument("--max-provider-spend-usd", default=None)
    build_parser_command.add_argument("--resume", action="store_true")
    build_parser_command.add_argument("--retry-terminal-literature", action="store_true")
    build_parser_command.add_argument("--local-candidates", type=Path, help="Use a prepared local shortlist instead of provider discovery.")
    _add_literature_policy_arguments(build_parser_command)
    _add_local_mapping_arguments(build_parser_command)
    _add_navigation_policy_arguments(build_parser_command)

    scan_parser = commands.add_parser("local-discovery", help="Inspect a bounded Zotero selection locally without provider calls.")
    scan_parser.add_argument("--workspace", type=Path, required=True)
    scan_parser.add_argument("--instance-id", required=True, help="Stable identity for this Zotero installation.")
    selection = scan_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--item", action="append", dest="item_keys", default=[])
    selection.add_argument("--collection", default="")
    scan_parser.add_argument("--start", type=int, default=0)
    scan_parser.add_argument("--limit", type=int, default=32)

    for name, help_text in (("candidates", "Prepare a bounded local relationship shortlist without provider calls."),
                            ("recall", "Retrieve bounded source context locally without provider calls.")):
        local_parser = commands.add_parser(name, help=help_text)
        local_parser.add_argument("--workspace", type=Path, required=True)
        local_parser.add_argument("--instance-id", default=None)
        local_parser.add_argument("--library-id", default="0" if name == "candidates" else None)
        if name == "candidates":
            local_parser.add_argument("--max-pairs", type=int, default=256)
            local_parser.add_argument("--per-source-cap", type=int, default=8)
        else:
            local_parser.add_argument("--query", required=True)
            local_parser.add_argument("--max-notes", type=int, default=8)
            local_parser.add_argument("--max-chars", type=int, default=12000)
            local_parser.add_argument("--model-path", default="", help="Existing local MiniLM directory; never downloaded.")
            local_parser.add_argument("--profile-only", action="store_true", help="Use the legacy compact-profile recall route.")

    recall_index_parser = commands.add_parser("index-recall", help="Build the local semantic search cache from existing notes, without provider calls.")
    recall_index_parser.add_argument("--workspace", type=Path, required=True)
    recall_index_parser.add_argument("--model-path", default="", help="Existing local MiniLM directory; never downloaded.")

    jev_parser = commands.add_parser(
        "jev-review",
        help="Validate and render one bounded Jev relationship packet for local review (dry-run only).",
    )
    jev_parser.add_argument("--workspace", type=Path, required=True)
    jev_parser.add_argument("--input", type=Path, required=True, dest="pair_input")

    calibration_parser = commands.add_parser("calibrate-map", help="Evaluate saved vectors against private, independently reviewed pairs offline.")
    calibration_parser.add_argument("--workspace", type=Path, required=True)
    calibration_parser.add_argument("--reviews", type=Path, required=True)
    discovery_parser = commands.add_parser("refresh-discovery", help="Refresh collection and library discovery from saved embeddings, without loading a model.")
    discovery_parser.add_argument("--workspace", type=Path, required=True)
    discovery_parser.add_argument("--neighbors", type=int, default=8)
    discovery_parser.add_argument("--preview-decisions", action="store_true", help="Prepare an offline Jev cost preview; paid dispatch stays disabled.")
    discovery_parser.add_argument("--max-pairs", type=int, default=256)
    discovery_parser.add_argument("--per-source-cap", type=int, default=8)
    naming_parser = commands.add_parser("community-name", help="Preview a bounded community title task; dispatch defaults off.")
    naming_parser.add_argument("--workspace", type=Path, required=True)
    naming_parser.add_argument("--community-id", required=True)
    naming_parser.add_argument("--scale", choices=("broad", "default", "fine"), default="default")
    naming_parser.add_argument("--execute", action="store_true")
    naming_parser.add_argument("--allow-cloud", action="store_true")
    naming_parser.add_argument("--approval", type=Path)
    naming_parser.add_argument("--input-price-per-million")
    naming_parser.add_argument("--output-price-per-million")
    naming_parser.add_argument("--pricing-identity", default="")

    names_parser = commands.add_parser("community-names", help="Preview or reuse cached two-stage community names; paid generation defaults off.")
    names_parser.add_argument("--workspace", type=Path, required=True)
    names_parser.add_argument("--scale", choices=("broad", "default", "fine"), default="default")
    names_parser.add_argument("--apply", action="store_true", help="Apply only complete, current cached names to matching graph views.")
    names_parser.add_argument("--cache", type=Path)
    names_parser.add_argument("--override-root", type=Path)
    names_parser.add_argument("--provider", default="openai")
    names_parser.add_argument("--model", default="gpt-6-luna")
    names_parser.add_argument("--execute", action="store_true", help="Generate missing names only with separate exact naming approval.")
    names_parser.add_argument("--allow-cloud", action="store_true")
    names_parser.add_argument("--approval", type=Path)
    names_parser.add_argument("--input-price-per-million")
    names_parser.add_argument("--output-price-per-million")
    names_parser.add_argument("--pricing-identity", default="")

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
        if args.command == "desktop":
            from .desktop import main as desktop_main

            return desktop_main([])
        elif args.command == "serve":
            from .webapp import serve

            if not 0 <= args.port <= 65535:
                raise ValueError("port must be between 0 and 65535")
            serve(port=args.port, open_browser=not args.no_browser)
            return 0
        elif args.command == "init":
            result = initialize_workspace(args.workspace, overwrite=args.overwrite).to_dict()
        elif args.command == "doctor":
            result = doctor(args.workspace).to_dict()
        elif args.command == "zotero" and args.zotero_command == "collections":
            result = {"status": "ok", "collections": list_collections()}
        elif args.command == "zotero" and args.zotero_command == "inventory":
            result = inventory(args.workspace, args.run_id, args.scope, args.collection, args.limit)
        elif args.command == "local-discovery":
            from .local_tools import scan_local_collection

            result = scan_local_collection(args.workspace, instance_id=args.instance_id, item_keys=args.item_keys,
                                           collection_key=args.collection, start=args.start, limit=args.limit)
        elif args.command in {"candidates", "recall"}:
            from .local_tools import load_local_retrieval

            if args.command == "recall" and not args.profile_only:
                result = recall(args.workspace, args.query, max_notes=args.max_notes, max_chars=args.max_chars,
                                model_path=args.model_path, instance_id=args.instance_id, library_id=args.library_id)
            elif args.command == "candidates":
                loaded = load_local_retrieval(args.workspace, instance_id=args.instance_id, library_id=args.library_id)
                result = {"status": "prepared", "candidates": loaded["index"].candidate_pairs(
                    per_source_cap=args.per_source_cap, max_pairs=args.max_pairs), "coverage": loaded["status"], "provider_calls": 0}
            else:
                loaded = load_local_retrieval(args.workspace, instance_id=args.instance_id, library_id=args.library_id or "0")
                result = {"status": "retrieved", **loaded["index"].recall(args.query, relationships=loaded["relationships"],
                          max_notes=args.max_notes, max_chars=args.max_chars), "coverage": loaded["status"], "provider_calls": 0}
        elif args.command == "index-recall":
            result = prepare_recall(args.workspace, model_path=args.model_path)
        elif args.command == "jev-review":
            from .jev_cli import prepare_review

            result = prepare_review(args.workspace, args.pair_input)
        elif args.command == "calibrate-map":
            from .mapping import calibrate_local_map

            result = calibrate_local_map(args.workspace, args.reviews)
        elif args.command == "refresh-discovery":
            from .discovery_refresh import refresh_cached_discovery

            result = refresh_cached_discovery(args.workspace, options={"neighbors": args.neighbors},
                                              preview_decisions=args.preview_decisions,
                                              max_pairs=args.max_pairs, per_source_cap=args.per_source_cap)
        elif args.command == "community-name":
            from .mapping import community_name_manifest, _bounded_json

            result = community_name_manifest(
                args.workspace, args.community_id, scale=args.scale, execute=args.execute,
                allow_cloud=args.allow_cloud,
                approval=_bounded_json(args.approval, max_bytes=65536) if args.approval else None,
                input_price_per_million=args.input_price_per_million,
                output_price_per_million=args.output_price_per_million,
                pricing_identity=args.pricing_identity,
            )
        elif args.command == "community-names":
            from .community_naming_workspace import community_names_manifest
            from .mapping import _bounded_json

            result = community_names_manifest(
                args.workspace, scale=args.scale, apply=args.apply, cache_path=args.cache,
                override_root=args.override_root, provider=args.provider, model=args.model,
                enabled=args.execute, allow_cloud=args.allow_cloud,
                approval=_bounded_json(args.approval, max_bytes=65536) if args.approval else None,
                input_price_per_million=args.input_price_per_million,
                output_price_per_million=args.output_price_per_million,
                pricing_identity=args.pricing_identity,
            )
        elif args.command == "map":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = str(config.get("model") or "") if provider == configured_provider else ""
            model = args.model or configured_model or DEFAULT_MODELS.get(provider, "")
            literature_model = args.literature_model or (
                str(config.get("literature_model") or "")
                if provider == configured_provider
                else ""
            )
            reasoning_effort = args.reasoning_effort or (
                config.get("reasoning_effort")
                if provider == configured_provider
                else None
            )
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
                literature_model=literature_model or None,
                reasoning_effort=reasoning_effort,
                allow_cloud=args.allow_cloud is True,
                parallel=args.parallel if args.parallel is not None else int(config.get("parallel", 4)),
                provider_concurrency=_provider_concurrency(
                    args.provider_concurrency,
                    config.get("provider_concurrency"),
                ),
                max_provider_spend_usd=(
                    args.max_provider_spend_usd
                    if args.max_provider_spend_usd is not None
                    else config.get("max_provider_spend_usd")
                ),
                limit=args.limit if args.limit is not None else 0,
                extraction_version=str(extraction_config.get("version") or "2"),
                prompt_version=CURRENT_ATOMIC_PROMPT_VERSION,
                extraction_policy=_extraction_policy(args, config),
                processing=_processing_policy(args, config),
                literature_policy=_literature_policy(args, config),
                navigation_policy=_navigation_policy(args, config),
                mapping_backend=_mapping_backend(args, config),
                local_mapping=_local_mapping_options(args, config),
                relationship_decisions=_relationship_mapping_options(args, config),
            )
            result = run_map(request, run_id=args.run_id or None).to_dict()
        elif args.command == "sync":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = (
                str(config.get("model") or "")
                if provider == configured_provider
                else ""
            )
            model = (
                args.model
                or configured_model
                or DEFAULT_MODELS.get(provider, "")
            )
            literature_model = args.literature_model or (
                str(config.get("literature_model") or "")
                if provider == configured_provider
                else ""
            )
            reasoning_effort = args.reasoning_effort or (
                config.get("reasoning_effort")
                if provider == configured_provider
                else None
            )
            extraction_config = (
                config.get("extraction", {})
                if isinstance(config.get("extraction", {}), dict)
                else {}
            )
            request = MapRequest(
                workspace=args.workspace,
                scope=args.scope or str(config.get("scope") or "library"),
                collection_key=args.collection or None,
                provider=provider,
                model=model,
                literature_model=literature_model or None,
                reasoning_effort=reasoning_effort,
                allow_cloud=args.allow_cloud is True,
                parallel=(
                    args.parallel
                    if args.parallel is not None
                    else int(config.get("parallel", 4))
                ),
                provider_concurrency=_provider_concurrency(
                    args.provider_concurrency,
                    config.get("provider_concurrency"),
                ),
                max_provider_spend_usd=(
                    args.max_provider_spend_usd
                    if args.max_provider_spend_usd is not None
                    else config.get("max_provider_spend_usd")
                ),
                extraction_version=str(extraction_config.get("version") or "2"),
                extraction_policy=_extraction_policy(args, config),
                literature_policy=_literature_policy(args, config),
                navigation_policy=_navigation_policy(args, config),
                mapping_backend=_mapping_backend(args, config),
                local_mapping=_local_mapping_options(args, config),
                relationship_decisions=_relationship_mapping_options(args, config),
            )
            result = sync_zotero(
                request,
                run_id=args.run_id or None,
            )
        elif args.command == "resume":
            result = resume_map(
                args.workspace,
                args.run_id,
                retry_terminal_failures=args.retry_terminal_literature,
                max_provider_spend_usd=args.max_provider_spend_usd,
            ).to_dict()
        elif args.command == "status":
            report = get_status(args.workspace, args.run_id or None)
            result = report.to_dict()
            if not args.as_json:
                print(_status_text(result))
                return _exit_code(result)
        elif args.command == "estimate":
            config = load_config(args.workspace)
            provider = args.provider or str(config.get("provider") or "deepseek")
            model = args.model or str(config.get("model") or DEFAULT_MODELS.get(provider, ""))
            result = estimate_cost(
                args.workspace,
                scope=args.scope,
                collection_key=args.collection,
                provider=provider,
                model=model,
                graph_only=args.graph_only,
                literature_policy=_literature_policy(args, config),
                mapping_backend=_mapping_backend(args, config),
                local_mapping=_local_mapping_options(args, config),
            )
        elif args.command == "build-map":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = str(config.get("model") or "") if provider == configured_provider else ""
            configured_literature_model = (
                str(config.get("literature_model") or "")
                if provider == configured_provider
                else ""
            )
            if args.model and args.literature_model and args.model != args.literature_model:
                raise ValueError(
                    "build-map --model and --literature-model must match when both are set"
                )
            model = (
                args.literature_model
                or args.model
                or configured_literature_model
                or configured_model
                or DEFAULT_MODELS.get(provider, "")
            )
            reasoning_effort = args.reasoning_effort or (
                config.get("reasoning_effort")
                if provider == configured_provider
                else None
            )
            manifest = build_map(
                args.workspace,
                run_id=args.run_id or None,
                source_set=args.source_set or None,
                question=args.question or None,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                allow_cloud=args.allow_cloud is True,
                provider_concurrency=_provider_concurrency(
                    args.provider_concurrency,
                    config.get("provider_concurrency"),
                ),
                max_provider_spend_usd=args.max_provider_spend_usd,
                comparison_collection_keys=args.comparison_collection_keys,
                literature_policy=_literature_policy(args, config),
                navigation_policy=_navigation_policy(args, config),
                mapping_backend=_mapping_backend(args, config),
                local_mapping=_local_mapping_options(args, config),
                resume=args.resume,
                retry_terminal_failures=args.retry_terminal_literature,
                relationship_decisions=_relationship_mapping_options(args, config),
                **({"ranked_candidates": _read_local_candidates(args.local_candidates)} if args.local_candidates else {}),
            )
            result = manifest.to_dict()
            result["artifact_count"] = len(result.pop("artifacts", []) or [])
            metadata = result.get("metadata", {})
            literature = (
                metadata.get("literature_map", {})
                if isinstance(metadata, dict)
                else {}
            )
            result["metadata"] = {
                "literature_map": {
                    key: value
                    for key, value in literature.items()
                    if isinstance(value, (str, int, float, bool))
                    or value is None
                },
                "migration": (
                    metadata.get("migration", {})
                    if isinstance(metadata, dict)
                    else {}
                ),
            }
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


def _read_local_candidates(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("local shortlist exceeds the 4 MiB input limit")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("local shortlist must be bounded UTF-8 JSON") from exc
    rows = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > 256 or not all(isinstance(row, dict) for row in rows):
        raise ValueError("local shortlist requires at most 256 candidate records")
    return rows


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
        f"Parked for review: {counts.get('parked_for_review_count', 0)}\n"
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
        "connect_timeout_seconds": args.connect_timeout_seconds,
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


def _provider_concurrency(
    command_value: str | None, configured_value: Any
) -> int | str | None:
    value = command_value if command_value is not None else configured_value
    if value is None or value == "":
        return None
    if str(value).strip().casefold() == "auto":
        return "auto"
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "provider_concurrency must be auto or a positive integer"
        ) from exc
    if parsed < 1:
        raise ValueError("provider_concurrency must be auto or a positive integer")
    return parsed


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
        ocr=(
            args.ocr
            if getattr(args, "ocr", None) is not None
            else defaults.ocr
        ),
        languages=tuple(getattr(args, "ocr_language", None) or defaults.languages),
        pdf_fallback=(
            args.pdf_fallback
            if getattr(args, "pdf_fallback", None) is not None
            else defaults.pdf_fallback
        ),
    )


def _add_local_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--discovery-scope", choices=("library", "collection_and_library"), default=None,
                        help="Use library neighbors, or balance collection-local neighbors with library-wide bridges.")
    parser.add_argument("--local-tuning", choices=("auto", "manual"), default=None,
                        help="Use an admitted offline calibration, or preserve explicit mapping settings.")
    parser.add_argument("--relationship-decisions", choices=("off", "jev"), default=None,
                        help="Optional relationship decisions, separate from local clustering; defaults off.")
    parser.add_argument("--max-relationship-spend-usd", default=None,
                        help="Separate aggregate Jev spending ceiling; defaults zero.")
    parser.add_argument("--relationship-approval", type=Path, default=None,
                        help="Local manifest approving exact request identities and billing policy.")
    parser.add_argument("--execute-relationship-decisions", action="store_true", default=None,
                        help="Execute approved decisions; otherwise prepare a dry-run preview.")
    parser.add_argument("--apply-relationships", action="store_true", default=None,
                        help="Apply eligible results only in an explicitly marked private copy.")
    parser.add_argument("--relationship-batch-size", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--relationship-packet-format", choices=("full", "compact"), default=None)
    parser.add_argument("--relationship-max-pairs", type=int, default=None)
    parser.add_argument("--relationship-per-source-cap", type=int, default=None)
    parser.add_argument(
        "--mapping-backend",
        choices=("local", "llm"),
        default=None,
        help="Local MiniLM mapping (default), or explicitly selected LLM mapping.",
    )
    parser.add_argument(
        "--local-encoder",
        choices=("minilm",),
        default=None,
        help="Local sentence-transformers/all-MiniLM-L6-v2 encoder.",
    )
    parser.add_argument(
        "--local-clustering",
        choices=("leiden", "hdbscan"),
        default=None,
        help="Leiden communities (default), or optional HDBSCAN density/noise view.",
    )
    parser.add_argument(
        "--embedding-model-path",
        default=None,
        help="Existing local MiniLM model directory; model downloads are disabled.",
    )


def _mapping_backend(args: argparse.Namespace, config: dict[str, Any]) -> str:
    return getattr(args, "mapping_backend", None) or str(
        config.get("mapping_backend", "local")
    )


def _relationship_mapping_options(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    from .relationship_mapping import RelationshipMappingOptions
    value = dict(config.get("relationship_decisions", {}))
    for field, option in (("backend", "relationship_decisions"),
                          ("max_spend_usd", "max_relationship_spend_usd"),
                          ("batch_size", "relationship_batch_size"),
                          ("packet_format", "relationship_packet_format"),
                          ("max_pairs", "relationship_max_pairs"),
                          ("per_source_cap", "relationship_per_source_cap")):
        supplied = getattr(args, option, None)
        if supplied is not None:
            value[field] = supplied
    if getattr(args, "relationship_approval", None) is not None:
        value["approval_manifest"] = str(args.relationship_approval)
    if getattr(args, "execute_relationship_decisions", None) is True:
        value["dry_run"] = False
    if getattr(args, "apply_relationships", None) is True:
        value["apply"] = True
    return RelationshipMappingOptions.from_dict(value).to_dict()


def _local_mapping_options(
    args: argparse.Namespace, config: dict[str, Any]
) -> dict[str, Any]:
    from .local_mapping import LocalMappingOptions

    configured = config.get("local_mapping", {})
    defaults = LocalMappingOptions.from_dict(configured)
    payload = defaults.to_dict()
    overrides = {
        "discovery_scope": getattr(args, "discovery_scope", None),
        "tuning_mode": getattr(args, "local_tuning", None),
        "encoder": getattr(args, "local_encoder", None),
        "clustering": getattr(args, "local_clustering", None),
        "model_path": getattr(args, "embedding_model_path", None),
    }
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return LocalMappingOptions.from_dict(payload).to_dict()


def _add_literature_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--synthesis", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--clusters", action=argparse.BooleanOptionalAction, default=None)
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
        "cluster_generation_enabled": getattr(args, "clusters", None),
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
