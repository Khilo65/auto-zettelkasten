from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .api import (
    build_map,
    decide_expansion,
    doctor,
    export_expansion_candidates,
    export_to_obsidian,
    get_status,
    initialize_workspace,
    inventory,
    list_collections,
    list_expansion_candidates,
    map_accepted_candidates,
    migrate_workspace,
    resume_expansion,
    resume_map,
    run_expansion,
    run_map,
)
from .models import ExpansionDecision, ExpansionRequest, MapRequest
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

    migrate_parser = commands.add_parser("migrate", help="Migrate additive workspace artifacts to a supported schema.")
    migrate_parser.add_argument("--workspace", type=Path, required=True)
    migrate_parser.add_argument("--to", required=True, dest="target")
    migrate_parser.add_argument("--dry-run", action="store_true")

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

    resume_parser = commands.add_parser("resume", help="Resume an interrupted or partially terminal run.")
    resume_parser.add_argument("--workspace", type=Path, required=True)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--allow-cloud", action="store_true")

    status_parser = commands.add_parser("status", help="Show workspace or run status.")
    status_parser.add_argument("--workspace", type=Path, required=True)
    status_parser.add_argument("--run-id", default="")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    build_parser_command = commands.add_parser("build-map", help="Rebuild typed links, clusters, gaps, and indexes from validated notes.")
    build_parser_command.add_argument("--workspace", type=Path, required=True)
    build_parser_command.add_argument("--run-id", default="")

    expansion_parser = commands.add_parser("expansion", help="Discover and review bounded literature-graph suggestions.")
    expansion_commands = expansion_parser.add_subparsers(dest="expansion_command", required=True)

    expansion_run = expansion_commands.add_parser("run", help="Run graph expansion for one or more canonical targets.")
    expansion_run.add_argument("--workspace", type=Path, required=True)
    expansion_run.add_argument("--scope", choices=("source-set", "cluster", "gap", "source"), required=True)
    expansion_run.add_argument("--id", action="append", required=True, dest="target_ids")
    expansion_run.add_argument("--provider", choices=("internal", "semantic-scholar"), default="internal")
    expansion_run.add_argument("--depth", type=int, default=1)
    expansion_run.add_argument("--budget", type=int, default=100)
    expansion_run.add_argument("--allow-network", action="store_true")
    expansion_run.add_argument("--run-id", default="")

    expansion_resume = expansion_commands.add_parser("resume", help="Resume a graph expansion run.")
    expansion_resume.add_argument("--workspace", type=Path, required=True)
    expansion_resume.add_argument("--run-id", required=True)
    expansion_resume.add_argument("--allow-network", action="store_true")

    expansion_list = expansion_commands.add_parser("list", help="List materialized expansion suggestions.")
    expansion_list.add_argument("--workspace", type=Path, required=True)
    expansion_list.add_argument("--state", choices=("proposed", "accepted", "parked", "rejected"))
    expansion_list.add_argument("--json", action="store_true", dest="as_json")

    expansion_decide = expansion_commands.add_parser("decide", help="Accept, park, or reject an expansion suggestion.")
    expansion_decide.add_argument("--workspace", type=Path, required=True)
    expansion_decide.add_argument("--suggestion", required=True)
    expansion_decide.add_argument("--decision", choices=("accept", "park", "reject"), required=True)
    expansion_decide.add_argument("--reason", required=True)
    expansion_decide.add_argument("--expected-version", type=int, required=True)
    expansion_decide.add_argument("--actor", default="user")

    expansion_map = expansion_commands.add_parser("map-accepted", help="Map accepted suggestions that already exist in Zotero.")
    expansion_map.add_argument("--workspace", type=Path, required=True)
    expansion_map.add_argument("--suggestion", action="append", default=[], dest="suggestion_ids")
    expansion_map.add_argument("--provider", choices=("deepseek", "openrouter", "gemini", "ollama"), default=None)
    expansion_map.add_argument("--model", default=None)
    expansion_map.add_argument("--allow-cloud", action="store_true")
    expansion_map.add_argument("--parallel", type=int, default=None)
    expansion_map.add_argument("--question", default="")
    expansion_map.add_argument("--run-id", default="")

    expansion_export = expansion_commands.add_parser("export", help="Export reviewed expansion suggestions.")
    expansion_export.add_argument("--workspace", type=Path, required=True)
    expansion_export.add_argument("--state", choices=("accepted",), default="accepted")
    expansion_export.add_argument("--format", choices=("bibtex", "ris"), required=True, dest="output_format")
    expansion_export.add_argument("--output", type=Path, required=True)

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
        elif args.command == "migrate":
            result = _result_payload(migrate_workspace(args.workspace, target=args.target, dry_run=args.dry_run))
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
            )
            result = run_map(request, run_id=args.run_id or None).to_dict()
        elif args.command == "resume":
            result = resume_map(args.workspace, args.run_id, allow_cloud=args.allow_cloud).to_dict()
        elif args.command == "status":
            report = get_status(args.workspace, args.run_id or None)
            result = report.to_dict()
            if not args.as_json:
                print(_status_text(result))
                return 0 if result.get("status") != "blocked" else 2
        elif args.command == "build-map":
            result = build_map(args.workspace, run_id=args.run_id or None).to_dict()
        elif args.command == "expansion" and args.expansion_command == "run":
            request = ExpansionRequest(
                workspace=args.workspace,
                scope=args.scope.replace("-", "_"),
                target_ids=tuple(args.target_ids),
                provider=args.provider,
                depth=args.depth,
                budget=args.budget,
                allow_network=args.allow_network,
                run_id=args.run_id or None,
            )
            result = _result_payload(run_expansion(request))
        elif args.command == "expansion" and args.expansion_command == "resume":
            result = _result_payload(
                resume_expansion(args.workspace, args.run_id, allow_network=args.allow_network)
            )
        elif args.command == "expansion" and args.expansion_command == "list":
            candidates = list_expansion_candidates(args.workspace, state=args.state)
            rows = [_result_payload(candidate) for candidate in candidates]
            result = {"status": "ok", "candidate_count": len(rows), "candidates": rows}
            if not args.as_json:
                print(_expansion_list_text(rows))
                return 0
        elif args.command == "expansion" and args.expansion_command == "decide":
            decision_names = {"accept": "accepted", "park": "parked", "reject": "rejected"}
            decision = ExpansionDecision(
                suggestion_id=args.suggestion,
                expected_version=args.expected_version,
                decision=decision_names[args.decision],
                reason=args.reason,
                actor=args.actor,
            )
            result = _result_payload(decide_expansion(args.workspace, decision))
        elif args.command == "expansion" and args.expansion_command == "map-accepted":
            config = load_config(args.workspace)
            configured_provider = str(config.get("provider") or "deepseek")
            provider = args.provider or configured_provider
            configured_model = str(config.get("model") or "") if provider == configured_provider else ""
            model = args.model or configured_model or DEFAULT_MODELS.get(provider, "")
            result = _result_payload(
                map_accepted_candidates(
                    args.workspace,
                    suggestion_ids=tuple(args.suggestion_ids),
                    provider=provider,
                    model=model,
                    allow_cloud=args.allow_cloud,
                    parallel=args.parallel if args.parallel is not None else int(config.get("parallel", 4)),
                    question=args.question or None,
                    run_id=args.run_id or None,
                )
            )
        elif args.command == "expansion" and args.expansion_command == "export":
            result = _result_payload(
                export_expansion_candidates(
                    args.workspace,
                    args.output,
                    state=args.state,
                    format=args.output_format,
                )
            )
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
    return 0 if result.get("status") not in {"blocked", "failed"} else 2


def _status_text(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    return (
        f"Status: {payload.get('status', 'unknown')}\n"
        f"Run: {payload.get('run_id') or 'none'}\n"
        f"Inventory: {counts.get('inventory_count', 0)}\n"
        f"Validated notes: {counts.get('validated_note_count', 0)}\n"
        f"Exhausted: {counts.get('exhausted_count', 0)}\n"
        f"Clusters: {counts.get('cluster_count', 0)}\n"
        f"Candidate gaps: {counts.get('gap_candidate_count', 0)}"
        f"\nExpansion candidates: {counts.get('expansion_candidate_count', 0)}"
    )


def _result_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        if isinstance(payload, dict):
            return payload
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"unsupported command result: {type(value).__name__}")


def _expansion_list_text(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "No expansion suggestions."
    return "\n".join(
        f"{row.get('suggestion_id', '')}\t{row.get('state', 'proposed')}\t{row.get('score', 0):.3f}\t{row.get('title', '')}"
        for row in rows
    )


if __name__ == "__main__":
    raise SystemExit(main())
