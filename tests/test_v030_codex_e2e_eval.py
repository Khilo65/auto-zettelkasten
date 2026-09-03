from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from copy import deepcopy
from collections.abc import Mapping
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.codex_attempt_guard import (
    _ACTIVE_GUARD,
    CodexAttemptDeny,
    CodexAttemptStateError,
    reserve_codex_attempt,
)
from auto_zettelkasten.files import (
    append_jsonl,
    read_yaml,
    sha256_file,
    sha256_text,
    write_yaml,
)
from auto_zettelkasten.notes import semantic_note_hash
from conftest import fake_codex_preflight


TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "v030_codex_e2e_eval", TOOLS / "v030_codex_e2e_eval.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)
CODE_COMMIT = "a" * 40


class _FakeGuard:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.run_id = "fake-campaign-run"
        self.reservations: list[tuple[str, str]] = []

    @contextmanager
    def activate(self):
        token = _ACTIVE_GUARD.set(self)
        try:
            yield self
        finally:
            _ACTIVE_GUARD.reset(token)

    def reserve(self, contract_id: str, job_id: str | None = None) -> str:
        assert job_id is not None
        self.reservations.append((contract_id, job_id))
        return job_id

    def finish(self, state: str, *, reason: str = "") -> None:
        self.events.append((state, reason))


def _manifest(root: Path) -> Path:
    files = root / "frozen"
    files.mkdir(parents=True)
    initialize_workspace(root)
    cases: list[dict[str, Any]] = []
    definitions = (
        ("pdf", "application/pdf", "pypdf_text", "validated_note"),
        ("html", "text/html", "html_text", "validated_note"),
        ("metadata", "application/json", "zotero_metadata", "limited_note"),
    )
    for index, (name, media_type, route, terminal_status) in enumerate(definitions, 1):
        parent_key = f"P{index}"
        row: dict[str, Any] = {
            "case_id": name,
            "media_type": media_type,
            "expected": {
                "content_route": route,
                "selected_pages": [],
                "terminal_status": terminal_status,
            },
            "zotero_parent": {
                "key": parent_key,
                "data": {
                    "key": parent_key,
                    "itemType": "journalArticle",
                    "title": f"Synthetic {name}",
                    "date": "2026",
                    "creators": [],
                },
            },
        }
        if terminal_status == "validated_note":
            suffix = ".pdf" if media_type == "application/pdf" else ".html"
            path = files / f"{name}{suffix}"
            path.write_bytes(
                b"%PDF-1.4\nsynthetic\n%%EOF\n"
                if media_type == "application/pdf"
                else b"<html><body>Synthetic full article text.</body></html>"
            )
            row.update(
                file=str(path.relative_to(root)),
                sha256=sha256_file(path),
                zotero_attachment={
                    "key": f"A{index}",
                    "data": {
                        "key": f"A{index}",
                        "parentItem": parent_key,
                        "itemType": "attachment",
                        "contentType": media_type,
                        "filename": path.name,
                    },
                },
            )
            if media_type == "text/html":
                row["zotero_fulltext"] = True
        cases.append(row)
    path = root / "PRIVATE_MANIFEST.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "code_commit": CODE_COMMIT,
                "evaluation_id": "synthetic-raw-e2e",
                "run_id": "synthetic-raw-e2e-run",
                "workspace": str(root),
                "question": "How do the synthetic works relate?",
                "gate": {
                    "schema_version": "1",
                    "kind": "raw_e2e",
                    "stage": "strategic-test",
                    "case_count": 3,
                    "source_attempt_limit": 5,
                    "relationship_attempt_limit": 4,
                    "total_attempt_limit": 9,
                    "document_attempt_limit": 4,
                    "stage_deadline_seconds": 60,
                    "cluster_generation_enabled": True,
                },
                "cases": cases,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _strategic_manifests(root: Path) -> tuple[Path, Path, Path, Path]:
    template_sha256 = runner._STRATEGIC_TEMPLATE_MANIFEST_SHA256
    strategic_keys = [
        "SYN00008",
        "SYN00001",
        "SYN00006",
        "SYN00003",
        "SYN00007",
        "SYN00002",
        "SYN00005",
        "SYN00004",
    ]
    other_pdf = [f"PDF{index:05d}" for index in range(7)]
    other_html = [f"HTM{index:05d}" for index in range(19)]
    metadata = [f"MET{index:05d}" for index in range(6)]
    ordered = [*strategic_keys, *other_pdf, *other_html, *metadata]
    pdf_keys = set(strategic_keys[:2] + other_pdf)
    origin = root / "sealed-origin"
    custody40 = root / "strategic40-custody"
    inventory = origin / "01_custody" / "zotero" / "inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("[]\n", encoding="utf-8")
    rows40: list[dict[str, Any]] = []
    for ordinal, key in enumerate(ordered, 1):
        parent = {
            "key": key,
            "data": {
                "key": key,
                "itemType": "journalArticle",
                "title": f"Frozen source {ordinal}",
                "creators": [],
            },
        }
        source_id = runner.base.source_id_for_item(parent)
        if key in metadata:
            rows40.append(
                {
                    "ordinal": ordinal,
                    "parent_key": key,
                    "parent_record": parent,
                    "phase": "baseline",
                    "source_id": source_id,
                    "disposition": "metadata_only",
                    "raw": None,
                    "zotero_fulltext": None,
                    "selected": {
                        "content_sha256": sha256_text(key),
                        "text_sha256": sha256_text(""),
                        "media_type": "application/json",
                        "reason": "metadata_only",
                        "route": "zotero_metadata",
                        "scope": "metadata_only",
                        "terminal_status": "limited_note",
                    },
                }
            )
            continue
        media_type = "application/pdf" if key in pdf_keys else "text/html"
        suffix = ".pdf" if media_type == "application/pdf" else ".html"
        attachment_key = f"A{ordinal:07d}"
        relative = Path("01_custody") / "files" / f"{attachment_key}{suffix}"
        content = (
            b"%PDF-1.4\nsynthetic frozen source\n%%EOF\n"
            if media_type == "application/pdf"
            else b"<html><body>Synthetic frozen source.</body></html>"
        )
        for workspace in (origin, custody40):
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        digest = sha256_file(custody40 / relative)
        fulltext = None
        if media_type == "text/html" and key == strategic_keys[2]:
            fulltext_relative = (
                Path("01_custody") / "zotero_fulltext" / f"{attachment_key}.html"
            )
            fulltext_path = custody40 / fulltext_relative
            fulltext_path.parent.mkdir(parents=True, exist_ok=True)
            fulltext_path.write_bytes(content)
            fulltext = {
                "path": fulltext_relative.as_posix(),
                "sha256": sha256_file(fulltext_path),
                "size": fulltext_path.stat().st_size,
            }
        rows40.append(
            {
                "ordinal": ordinal,
                "parent_key": key,
                "parent_record": parent,
                "phase": "baseline",
                "source_id": source_id,
                "disposition": "substantive_raw_source",
                "raw": {
                    "attachment_key": attachment_key,
                    "media_type": media_type,
                    "origin_relative_path": relative.as_posix(),
                    "path": relative.as_posix(),
                    "sha256": digest,
                    "size": len(content),
                },
                "zotero_fulltext": fulltext,
                "selected": {
                    "content_sha256": digest,
                    "text_sha256": digest,
                    "media_type": media_type,
                    "reason": "",
                    "route": (
                        "pypdf_text"
                        if key in strategic_keys[:2]
                        else "pypdf_poppler_tesseract"
                        if media_type == "application/pdf"
                        else "zotero_fulltext"
                        if fulltext is not None
                        else "html_text"
                    ),
                    "scope": "full_document",
                    "terminal_status": "validated_note",
                },
            }
        )
    manifest40_path = custody40 / "PRIVATE_CUSTODY_MANIFEST.json"
    manifest40_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "frozen_private_raw_custody",
                "custody_only": True,
                "private": True,
                "never_production_prompt_input": True,
                "origin": {
                    "workspace": origin.name,
                    "inventory_path": str(inventory.relative_to(origin)),
                    "inventory_sha256": sha256_file(inventory),
                    "inventory_size": inventory.stat().st_size,
                },
                "selection": {"template_manifest_sha256": template_sha256},
                "sources": rows40,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    by_key = {row["parent_key"]: row for row in rows40}
    custody8 = root / "strategic8-custody"
    rows8: list[dict[str, Any]] = []
    for ordinal, key in enumerate(strategic_keys, 1):
        source = deepcopy(by_key[key])
        source.update(
            ordinal=ordinal,
            strategic40_ordinal=by_key[key]["ordinal"],
            cluster_expectation=(
                "related_candidate" if ordinal <= 4 else "control"
            ),
        )
        raw = source["raw"]
        target = custody8 / raw["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(custody40 / raw["path"], target)
        if source["zotero_fulltext"] is not None:
            fulltext_path = source["zotero_fulltext"]["path"]
            target = custody8 / fulltext_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(custody40 / fulltext_path, target)
        rows8.append(source)
    manifest8_path = custody8 / "PRIVATE_CUSTODY_MANIFEST.json"
    manifest8_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "frozen_private_raw_custody",
                "custody_only": True,
                "private": True,
                "never_production_prompt_input": True,
                "derived_from": {
                    "workspace": custody40.name,
                    "manifest_path": manifest40_path.name,
                    "manifest_sha256": sha256_file(manifest40_path),
                    "template_manifest_sha256": template_sha256,
                },
                "selection": {"template_manifest_sha256": template_sha256},
                "sources": rows8,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    semantic_oracle = root / "strategic8-oracle" / "PRIVATE_SEMANTIC_ORACLE.json"
    semantic_oracle.parent.mkdir()
    semantic_oracle.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "kind": "v030_strategic8_semantic_oracle",
                "source_custody_manifest_sha256": sha256_file(manifest8_path),
                "source_template_manifest_sha256": template_sha256,
                "core_parent_keys": strategic_keys[:3],
                "context_parent_key": strategic_keys[3],
                "control_parent_keys": strategic_keys[4:],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def live_manifest(name: str, custody_path: Path, sources: list[dict[str, Any]]) -> Path:
        live = root / f"{name}-live"
        initialize_workspace(live)
        cases: list[dict[str, Any]] = []
        live_sources = (
            sorted(sources, key=lambda row: str(row["parent_key"]).casefold())
            if len(sources) == 8
            else sources
        )
        for source in live_sources:
            raw = source["raw"]
            frozen_route = source["selected"]["route"]
            content_route = (
                "html_text"
                if source["selected"]["media_type"] == "text/html"
                else runner.base.PDF_INPUT_ROUTE
                if frozen_route == "pypdf_poppler_tesseract"
                else frozen_route
            )
            case: dict[str, Any] = {
                "case_id": source["parent_key"].casefold(),
                "media_type": source["selected"]["media_type"],
                "zotero_parent": source["parent_record"],
                "expected": {
                    "terminal_status": source["selected"]["terminal_status"],
                    "content_route": content_route,
                    "selected_pages": [],
                },
            }
            if source.get("cluster_expectation"):
                case["cluster_expectation"] = source["cluster_expectation"]
            if raw is not None:
                source_path = custody_path.parent / raw["path"]
                live_path = live / raw["path"]
                live_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, live_path)
                case.update(
                    file=raw["path"],
                    sha256=raw["sha256"],
                    zotero_attachment={
                        "key": raw["attachment_key"],
                        "data": {
                            "key": raw["attachment_key"],
                            "parentItem": source["parent_key"],
                            "itemType": "attachment",
                            "contentType": raw["media_type"],
                            "filename": Path(raw["path"]).name,
                        },
                    },
                )
                if source["zotero_fulltext"] is not None:
                    fulltext_path = custody_path.parent / source["zotero_fulltext"][
                        "path"
                    ]
                    case["zotero_fulltext"] = {
                        "content": fulltext_path.read_text(encoding="utf-8"),
                        "contentType": "text/html",
                    }
            cases.append(case)
        count = len(sources)
        controls = runner._STRATEGIC_CONTROLS[count]
        path = live / "PRIVATE_MANIFEST.json"
        payload = {
                    "schema_version": "1",
                    "code_commit": CODE_COMMIT,
                    "evaluation_id": f"synthetic-{name}",
                    "run_id": f"synthetic-{name}-run",
                    "workspace": str(live),
                    "question": "How do the frozen sources relate?",
                    "source_custody_manifest": str(custody_path),
                    "source_custody_manifest_sha256": sha256_file(custody_path),
                    "source_template_manifest_sha256": template_sha256,
                    "gate": {
                        "schema_version": "1",
                        "kind": "raw_e2e",
                        "case_count": count,
                        **controls,
                    },
                    "cases": cases,
                    "collections": [],
                }
        if count == 8:
            payload.update(
                strategic8_semantic_oracle=str(semantic_oracle),
                strategic8_semantic_oracle_sha256=sha256_file(semantic_oracle),
            )
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        return path

    live8 = live_manifest("strategic8", manifest8_path, rows8)
    live40 = live_manifest("strategic40", manifest40_path, rows40)
    return live8, live40, manifest8_path, manifest40_path


def _bind_synthetic_strategic_fixture(
    monkeypatch: pytest.MonkeyPatch,
    custody8: Path,
    custody40: Path,
) -> None:
    monkeypatch.setattr(
        runner,
        "_STRATEGIC_CUSTODY_MANIFEST_SHA256",
        {8: sha256_file(custody8), 40: sha256_file(custody40)},
    )
    text_pdf_ids = {
        str(row["parent_key"]).casefold()
        for row in json.loads(custody40.read_text(encoding="utf-8"))["sources"]
        if row["selected"]["route"] == "pypdf_text"
    }

    def route(
        case: dict[str, Any], _request: Any, *, reader: Any
    ) -> tuple[str, list[int]]:
        assert isinstance(reader, runner.CodexReader)
        if case["case_id"] in text_pdf_ids:
            return "pypdf_text", []
        return runner.base.PDF_INPUT_ROUTE, []

    monkeypatch.setattr(runner, "_provider_free_pdf_route", route)
    root = custody8.parent.parent
    live8 = root / "strategic8-live" / "PRIVATE_MANIFEST.json"
    live40 = root / "strategic40-live" / "PRIVATE_MANIFEST.json"
    oracle = root / "route-oracle" / "PRIVATE_PDF_ROUTE_ORACLE.json"
    result = runner.freeze_routes(
        strategic40_manifest_path=live40,
        strategic40_manifest_sha256=sha256_file(live40),
        strategic8_manifest_path=live8,
        strategic8_manifest_sha256=sha256_file(live8),
        output_path=oracle,
        repository_probe=lambda: (CODE_COMMIT, False),
    )
    assert result["provider_calls"] == 0
    for path in (live8, live40):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            pdf_route_oracle=str(oracle),
            pdf_route_oracle_sha256=sha256_file(oracle),
        )
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


def _graph_manifest(root: Path) -> tuple[Path, list[dict[str, Any]], str]:
    write_yaml(
        root / "auto-zettelkasten.yml",
        {
            "engine_version": "0.30.0",
            "artifact_schema_version": "1.20",
            "literature_mapping": {"synthesis_enabled": True},
        },
    )
    write_yaml(
        root / "11_state" / "workspace_manifest.yml",
        {"engine_version": "0.30.0", "artifact_schema_version": "1.20"},
    )
    write_yaml(root / "01_custody" / "zotero" / "collection_snapshot.yml", {})
    write_yaml(
        root / "02_source_memory" / "indexes" / "literature_positions.yml", {}
    )
    write_yaml(root / "02_source_memory" / "indexes" / "missing_sources.yml", {})
    sources: list[dict[str, Any]] = []
    packets = [f"packet-{index:02d}" for index in range(51)]
    strata = [f"stratum-{index:02d}" for index in range(20)]
    for index in range(500):
        source_id = f"source-{index:03d}"
        note_id = f"note-{index:03d}"
        note_path = root / "02_source_memory" / "notes" / f"{note_id}.md"
        profile_path = root / "02_source_memory" / "profiles" / f"{note_id}.yml"
        bundle_path = root / "02_source_memory" / "bundles" / f"{source_id}.yml"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\n"
            f"source_id: {source_id}\n"
            f"note_id: {note_id}\n"
            "related_notes: []\n"
            "---\n"
            f"# Synthetic graph note {index}\n",
            encoding="utf-8",
        )
        note_semantic_sha256 = semantic_note_hash(
            note_path.read_text(encoding="utf-8")
        )
        write_yaml(profile_path, {"profile": {"source_id": source_id}})
        write_yaml(
            root / "11_state" / "note_metadata" / f"{note_id}.yml",
            {"source_id": source_id, "note_id": note_id},
        )
        source = {
            "source_id": source_id,
            "note_id": note_id,
            "phase": "baseline",
            "primary_stratum_id": strata[index % len(strata)],
            "note_path": str(note_path.relative_to(root)),
            "semantic_note_sha256": note_semantic_sha256,
            "origin_note_sha256": sha256_file(note_path),
            "profile_path": str(profile_path.relative_to(root)),
            "profile_sha256": sha256_file(profile_path),
            "deepest_leaf_packet_key": packets[(index // 2) % len(packets)],
        }
        if index < 484:
            write_yaml(bundle_path, {"source_id": source_id, "status": "validated"})
            source.update(
                bundle_path=str(bundle_path.relative_to(root)),
                bundle_sha256=sha256_file(bundle_path),
            )
        else:
            source.update(bundle_path="", bundle_sha256="")
        sources.append(source)
    selection = root / "11_state" / "harness_bakeoff_manifest.yml"
    selection_payload = {
        "schema_version": "1",
        "status": "frozen_provider_neutral_slice",
        "never_production_prompt_input": True,
        "source_count": 500,
        "primary_stratum_ids": strata,
        "sources": sources,
        "sampling": {
            "selected_packet_count": 51,
            "selected_packet_keys": packets,
        },
    }
    selection_identity = sha256_text(
        json.dumps(selection_payload, sort_keys=True, ensure_ascii=False)
    )
    write_yaml(selection, {**selection_payload, "manifest_sha256": selection_identity})
    manifest = root / "PRIVATE_GRAPH_MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "code_commit": CODE_COMMIT,
                "evaluation_id": "synthetic-graph500-e2e",
                "run_id": "synthetic-graph500-e2e-run",
                "workspace": str(root),
                "question": "Which relationships and clusters organize this sample?",
                "selection_manifest": "11_state/harness_bakeoff_manifest.yml",
                "selection_manifest_sha256": selection_identity,
                "selection_manifest_file_sha256": sha256_file(selection),
                "gate": {
                    "schema_version": "1",
                    "kind": "graph_e2e",
                    "stage": "graph500",
                    "case_count": 500,
                    "source_attempt_limit": 0,
                    "relationship_attempt_limit": 233,
                    "total_attempt_limit": 233,
                    "document_attempt_limit": 1,
                    "stage_deadline_seconds": 14_400,
                    "cluster_generation_enabled": True,
                },
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, sources, selection_identity


def _bind_synthetic_graph_fixture(
    monkeypatch: pytest.MonkeyPatch,
    manifest: Path,
    sources: list[dict[str, Any]],
    selection_identity: str,
) -> None:
    workspace = manifest.parent
    baseline_paths = {
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
        "11_state/harness_bakeoff_manifest.yml",
        "01_custody/zotero/collection_snapshot.yml",
        "02_source_memory/indexes/literature_positions.yml",
        "02_source_memory/indexes/missing_sources.yml",
    }
    for source in sources:
        baseline_paths.update(
            {
                str(source["note_path"]),
                str(source["profile_path"]),
                f"11_state/note_metadata/{source['note_id']}.yml",
            }
        )
        if source.get("bundle_path"):
            baseline_paths.add(str(source["bundle_path"]))
    assert len(baseline_paths) == 1_990
    monkeypatch.setattr(runner, "_GRAPH500_MANIFEST_SHA256", selection_identity)
    monkeypatch.setattr(
        runner,
        "_GRAPH500_CONFIG_SHA256",
        sha256_file(workspace / "auto-zettelkasten.yml"),
    )
    monkeypatch.setattr(
        runner,
        "_GRAPH500_WORKSPACE_MANIFEST_SHA256",
        sha256_file(workspace / "11_state" / "workspace_manifest.yml"),
    )
    monkeypatch.setattr(
        runner,
        "_GRAPH500_BASELINE_SHA256",
        runner._inventory_sha256(workspace, baseline_paths),
    )


def _fake_codex(path: Path, calls_path: Path) -> None:
    body = f'''#!{sys.executable}
import json, sys
from pathlib import Path
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
sections = schema.get("properties", {{}}).get("analysis_sections", {{}}).get("properties")
payload = {{}} if sections is None else {{
    "analysis_sections": {{key: "Synthetic source-grounded analysis; see p. 1." for key in sections}},
    "compact_profile": {{
        "thesis": "Synthetic thesis.",
        "method_or_knowledge_basis": "Synthetic document analysis.",
        "source_genre": "journal article",
        "inferential_design": "descriptive",
        "mechanisms": [], "outcomes": [], "cases": [], "populations": [],
        "periods": [], "datasets": [],
    }},
    "evidence_anchors": [{{
        "claim": "The synthetic source supports one bounded claim.",
        "locator": "p. 1", "planning_roles": ["finding"],
        "salience_priority": 10, "evidence_role": "descriptive",
        "support_boundary": "Synthetic fixture only.",
        "plain_english_meaning": "One bounded claim is supported.",
        "uncertainty": "Synthetic fixture only.", "quantitative_result": None,
    }}],
    "literature_positions": [],
    "observed_bibliographic_identity": {{"title": "", "creators": [], "date": ""}},
}}
with Path({str(calls_path)!r}).open("a", encoding="utf-8") as handle:
    handle.write("source_bundle\\n")
print(json.dumps({{"type": "thread.started"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps(payload)}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}), flush=True)
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _completion(contract_id: str, *, source: bool) -> dict[str, Any]:
    model = runner.base.SOURCE_MODEL if source else runner.base.RELATIONSHIP_MODEL
    identity = runner.base.codex_contract_identity(
        contract_id,
        model,
        runner.base.REASONING_EFFORT,
        runner.base.DIRECT_PDF_CLI_VERSION,
    )
    return {
        **identity,
        "codex_cli_version": runner.base.DIRECT_PDF_CLI_VERSION,
        "finish_reason": "turn.completed",
        "max_output_tokens": identity["output_reservation"],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _attempt(
    number: int,
    contract_id: str,
    *,
    source: bool,
    key: str | None = None,
) -> dict[str, Any]:
    return {
        "attempt_id": f"attempt-{number}",
        "stage": contract_id,
        "key": key or f"key-{number}",
        "fingerprint": f"fingerprint-{number}",
        "attempt": 1,
        "status": "completed",
        "provider_completion": _completion(contract_id, source=source),
    }


def _write_run(workspace: Path, request: Any, client: Any, run_id: str) -> dict[str, Any]:
    assert request.parallel == 3
    assert request.literature_policy.cluster_generation_enabled is True
    assert request.literature_policy.max_profile_calls == 5
    assert request.literature_policy.max_synthesis_calls == 4
    items = client.inventory("library")
    assert len(items) == 3
    assert client.children("P3") == []
    html_attachment = client.children("P2")[0]
    assert client.file(html_attachment["key"])[1] == "text/html"
    assert "Synthetic full article" in client.fulltext(html_attachment["key"])["content"]

    run_root = workspace / "11_state" / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "inventory.json").write_text(
        json.dumps(items, sort_keys=True), encoding="utf-8"
    )
    note_root = workspace / "02_source_memory" / "notes"
    profile_root = workspace / "02_source_memory" / "profiles"
    source_ids = [runner.base.source_id_for_item(item) for item in items]
    report_items = []
    for index, source_id in enumerate(source_ids, 1):
        case = client._by_parent[f"P{index}"]
        terminal_status = str(case["expected_terminal_status"])
        item_root = run_root / "items" / f"P{index}"
        item_root.mkdir(parents=True, exist_ok=True)
        if terminal_status == "validated_note":
            custody = workspace / "01_custody" / "files" / Path(case["path"]).name
            custody.parent.mkdir(parents=True, exist_ok=True)
            custody.write_bytes(Path(case["path"]).read_bytes())
            frozen = {
                "content_hash": case["sha256"],
                "source_file": str(custody),
                "content_route": case["expected_route"],
                "media_type": case["media_type"],
                "source_scope": "full_document",
            }
        else:
            frozen = {
                "content_hash": "f" * 64,
                "source_file": "zotero://select/library/items/P3",
                "content_route": "zotero_metadata",
                "media_type": "application/json",
                "source_scope": "metadata_only",
            }
        write_yaml(item_root / "frozen_content.yml", frozen)
        related = (
            [{"note_id": "note-2"}]
            if index == 1
            else [{"note_id": "note-1"}]
            if index == 2
            else []
        )
        note_path = note_root / f"note-{index}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\n"
            f"source_id: {source_id}\n"
            f"note_id: note-{index}\n"
            f"related_notes: {json.dumps(related)}\n"
            "---\n# Synthetic note\n",
            encoding="utf-8",
        )
        write_yaml(
            profile_root / f"note-{index}.yml",
            {"profile": {"source_id": source_id, "note_id": f"note-{index}"}},
        )
        report_items.append(
            {
                "source_id": source_id,
                "note_id": f"note-{index}",
                "note_path": str(note_path.relative_to(workspace)),
                "terminal_status": terminal_status,
            }
        )

    relation = {
        "relation_id": "synthetic-relation",
        "source_id": source_ids[0],
        "target_source_id": source_ids[1],
        "relation_type": "complements",
        "decision_status": "accepted",
        "active": True,
    }
    registry = {
        "relations": [relation],
        "links": [relation],
        "pair_decisions": [],
        "current_pair_decisions": [
            {"source_ids": source_ids[:2], "status": "accepted"}
        ],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(
            workspace / "02_source_memory" / "indexes" / name,
            registry,
        )
    write_yaml(
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "relationship_stage_complete": True,
            "relationship_discovery_status": "complete",
            "relationship_discovery_incomplete_jobs": [],
        },
    )
    cluster = {
        "cluster_id": "cluster-synthetic",
        "source_ids": source_ids[:2],
        "refresh_pending": False,
    }
    cluster_map = {
        "status": "complete",
        "clusters": [cluster],
        "unclustered_sources": [],
        "synthesized_cluster_count": 1,
    }
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        {"clusters": [cluster], "pending_revisions": []},
    )

    source_rows = [
        *[
            _attempt(index, "chunk_evidence", source=True, key="pdf-source")
            for index in range(1, 4)
        ],
        _attempt(4, "source_bundle", source=True, key="pdf-source"),
        _attempt(5, "source_bundle", source=True, key="html-source"),
    ]
    source_usage = run_root / "literature" / "profiles" / "provider_usage.yml"
    write_yaml(
        source_usage,
        {"provider_call_count": 5, "attempts": source_rows},
    )
    for row in source_rows:
        append_jsonl(
            source_usage.with_name("provider_events.jsonl"),
            {"event_type": "reserved", "attempt_id": row["attempt_id"]},
        )
    relationship_rows = [
        _attempt(3, "relationship_candidate_selection", source=False),
        _attempt(4, "relationship_adjudication", source=False),
        _attempt(5, "cluster_plan", source=False),
        _attempt(6, "cluster_synthesis", source=False),
    ]
    write_yaml(
        run_root / "literature" / "synthesis" / "provider_usage.yml",
        {"provider_call_count": 4, "attempts": relationship_rows},
    )
    report = {
        "status": "completed",
        "inventory_count": 3,
        "validated_note_count": 2,
        "limited_note_count": 1,
        "profile_count": 3,
        "profile_valid_count": 2,
        "profile_excluded_count": 1,
        "items": report_items,
        "cluster_map": cluster_map,
        "gap_map": {"status": "complete_no_qualifying_gaps", "gap_candidates": []},
        "cluster_count": 1,
        "synthesized_cluster_count": 1,
        "mapped_gap_count": 0,
        "source_provider_call_count": 5,
        "literature_provider_call_count": 4,
        "synthesis_call_count": 4,
        "provider_call_count": 9,
    }
    write_yaml(run_root / "run_report.yml", report)
    return report


def _write_graph_run(workspace: Path, kwargs: Mapping[str, Any]) -> None:
    source_ids = list(kwargs["source_set"]["source_ids"])
    assert len(source_ids) == 500
    assert kwargs["provider"] == "codex"
    assert kwargs["model"] == runner.base.RELATIONSHIP_MODEL
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["provider_concurrency"] == "auto"
    policy = kwargs["literature_policy"]
    assert policy == runner.LiteratureMappingPolicy(
        synthesis_enabled=True,
        cluster_generation_enabled=True,
        external_discovery="disabled",
        max_profile_calls=0,
        max_synthesis_calls=233,
        literature_deadline_seconds=14_400.0,
    )
    assert kwargs["navigation_policy"] == runner.NavigationPolicy()
    reasoner = kwargs["reasoner"]
    contracts = (
        "relationship_candidate_selection",
        "relationship_adjudication",
        "cluster_plan",
        "cluster_synthesis",
    )
    for index, contract in enumerate(contracts):
        reserve_codex_attempt(
            reasoner.attempt_guard,
            contract_id=contract,
            job_id=f"graph-job-{index}",
        )

    note_root = workspace / "02_source_memory" / "notes"
    related = {
        0: ["note-001", "note-002"],
        1: ["note-000"],
        2: ["note-000"],
    }
    for index, targets in related.items():
        (note_root / f"note-{index:03d}.md").write_text(
            "---\n"
            f"source_id: source-{index:03d}\n"
            f"note_id: note-{index:03d}\n"
            "related_notes:\n"
            + "".join(f"- note_id: {target}\n" for target in targets)
            + "---\n"
            f"# Synthetic graph note {index}\n",
            encoding="utf-8",
        )
    relations = [
        {
            "relation_id": "within-packet",
            "source_id": source_ids[0],
            "target_source_id": source_ids[1],
            "decision_status": "accepted",
            "active": True,
        },
        {
            "relation_id": "cross-packet",
            "source_id": source_ids[0],
            "target_source_id": source_ids[2],
            "decision_status": "accepted",
            "active": True,
        },
    ]
    registry = {
        "relations": relations,
        "links": relations,
        "pair_decisions": [],
        "current_pair_decisions": [
            {
                "source_ids": source_ids[:2],
                "status": "accepted",
            },
            {
                "source_ids": [source_ids[0], source_ids[2]],
                "status": "accepted",
            },
            {
                "source_ids": source_ids[3:5],
                "status": "no_relationship",
            }
        ],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(
            workspace / "02_source_memory" / "indexes" / name,
            registry,
        )
    write_yaml(
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "relationship_stage_complete": True,
            "relationship_discovery_status": "complete",
            "relationship_discovery_incomplete_jobs": [],
        },
    )
    cluster = {
        "cluster_id": "cluster-all-synthetic",
        "source_ids": source_ids,
        "refresh_pending": False,
    }
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        {
            "clusters": [cluster],
            "unclustered_sources": [],
            "pending_revisions": [],
        },
    )
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_syntheses.yml",
        {
            "syntheses": {
                "cluster-all-synthetic": {
                    "cluster_id": "cluster-all-synthetic",
                    "status": "reasoned",
                }
            }
        },
    )
    run_root = workspace / "11_state" / "runs" / kwargs["run_id"]
    relationship_rows = [
        _attempt(index, contract, source=False)
        for index, contract in enumerate(contracts, 1)
    ]
    write_yaml(
        run_root / "literature" / "synthesis" / "provider_usage.yml",
        {"provider_call_count": len(relationship_rows), "attempts": relationship_rows},
    )
    write_yaml(
        run_root / "semantic_build_receipt.yml",
        {
            "receipt_schema_version": "2",
            "status": "built",
            "semantic_replayable": True,
            "summary": {
                "source_count": 500,
                "relationship_count": len(relations),
                "cluster_count": 1,
                "gap_count": 0,
                "literature_map": {
                    "status": "completed",
                    "profile_count": 500,
                    "profile_valid_count": 500,
                    "profile_excluded_count": 0,
                    "synthesized_cluster_count": 1,
                    "synthesis_call_count": len(relationship_rows),
                    "synthesis_failure_count": 0,
                    "partial_reason": "",
                },
            },
        },
    )


def test_raw_workspace_config_hash_matches_current_initialized_config(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)

    assert sha256_file(tmp_path / "auto-zettelkasten.yml") == (
        runner._RAW_CONFIG_SHA256
    )


def test_provider_free_pdf_route_uses_verified_reader_without_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = {
        "manifest_version": 1,
        "upstream_tag": "rust-v0.152.1",
        "upstream_commit": "5adb68a49933ae446bf11935662c83dba55a0804",
        "platform": "macos-arm64",
        "license": "Apache-2.0",
        "notice": "NOTICE",
        "input_file_protocol_revision": "input_file-v1",
        "patch_sha256": "1" * 64,
        "binary_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
    }
    reader = runner.CodexReader(
        runner.base.SOURCE_MODEL,
        allow_cloud=True,
        reasoning_effort=runner.base.REASONING_EFFORT,
    )
    reader._preflight = {
        "version": runner.base.DIRECT_PDF_CLI_VERSION,
        "helper_version": runner.base.DIRECT_PDF_CLI_VERSION,
        "helper_manifest_valid": True,
        "pdf_input_file_capability": True,
        "_helper_manifest_identity": helper,
    }
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic\n%%EOF\n")
    parent = {"key": "P1", "data": {"key": "P1"}}
    case = {
        "case_id": "p1",
        "path": path,
        "parent": parent,
        "attachment": {"key": "A1", "data": {"key": "A1"}},
    }
    request = SimpleNamespace()
    calls: list[Any] = []

    def candidate(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], Any]:
        calls.append(kwargs.get("reader"))
        assert args[0] == path.read_bytes()
        return {}, SimpleNamespace(
            status="succeeded", route=runner.base.PDF_INPUT_ROUTE
        )

    monkeypatch.setattr(runner, "_custodied_pdf_candidate", candidate)

    with runner.base.deny_codex_attempts():
        route, pages = runner._provider_free_pdf_route(
            case, request, reader=reader
        )

    assert (route, pages) == (runner.base.PDF_INPUT_ROUTE, [])
    assert calls == [reader]


def test_mixed_raw_fresh_run_and_exact_replay(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "private")
    digest = sha256_file(manifest)
    authorization = manifest.parent / "authorization.json"
    authorization.write_text("{}\n", encoding="utf-8")
    provider = tmp_path / "provider"
    provider.mkdir()
    executable = provider / "codex"
    calls_path = provider / "calls.txt"
    _fake_codex(executable, calls_path)
    events: list[tuple[str, str]] = []
    guard = _FakeGuard(events)
    calls: list[bool] = []

    def fake_map(request: Any, **kwargs: Any) -> Any:
        resume = bool(kwargs["resume"])
        calls.append(resume)
        if not resume:
            reader = kwargs["reader"]
            assert reader.attempt_guard is guard
            assert kwargs["literature_reasoner"].attempt_guard is guard
            reader._preflight = fake_codex_preflight(
                tmp_path,
                executable,
                {"PATH": os.environ["PATH"]},
            )
            responses = [
                json.loads(
                    reader._generate_with_reasoning(
                        "system",
                        f"user-{index}",
                        2_048,
                        10,
                        reasoning_effort="medium",
                        output_contract=contract,
                    )
                )
                for index, contract in enumerate(
                    (
                        "chunk_evidence",
                        "chunk_evidence",
                        "chunk_evidence",
                        "source_bundle",
                        "source_bundle",
                    )
                )
            ]
            response = responses[-1]
            assert response["compact_profile"]["thesis"] == "Synthetic thesis."
            return _write_run(
                Path(request.workspace), request, kwargs["client"], kwargs["run_id"]
            )
        assert isinstance(kwargs["reader"].attempt_guard, CodexAttemptDeny)
        with pytest.raises(CodexAttemptStateError, match="forbidden"):
            reserve_codex_attempt(
                kwargs["reader"].attempt_guard,
                contract_id="source_bundle",
                job_id="request:replay",
            )
        return read_yaml(
            Path(request.workspace)
            / "11_state"
            / "runs"
            / kwargs["run_id"]
            / "run_report.yml"
        )

    def guard_factory(
        path: Path, digest: str, **kwargs: Any
    ) -> _FakeGuard:
        assert path == authorization
        assert digest == sha256_file(authorization)
        assert kwargs["evaluation_id"] == "synthetic-raw-e2e"
        assert kwargs["run_id"] == "synthetic-raw-e2e-run"
        assert kwargs["source_attempt_limit"] == 5
        assert kwargs["relationship_attempt_limit"] == 4
        assert kwargs["total_attempt_limit"] == 9
        return guard

    _, first = runner.run_gate(
        mode="run",
        manifest_path=manifest,
        manifest_sha256=digest,
        authorization_path=authorization,
        authorization_sha256=sha256_file(authorization),
        execute=True,
        map_runner=fake_map,
        repository_probe=lambda: (CODE_COMMIT, False),
        attempt_guard_factory=guard_factory,
    )
    assert first["status"] == "passed"
    assert first["source_attempt_count"] == 5
    assert first["relationship_attempt_count"] == 4
    assert first["cluster_generation_enabled"] is True
    assert events == [("passed", "")]
    assert [contract for contract, _ in guard.reservations] == [
        "chunk_evidence",
        "chunk_evidence",
        "chunk_evidence",
        "source_bundle",
        "source_bundle",
    ]
    provider_calls = calls_path.read_bytes()

    _, replay = runner.run_gate(
        mode="replay",
        manifest_path=manifest,
        manifest_sha256=digest,
        execute=True,
        map_runner=fake_map,
        repository_probe=lambda: (CODE_COMMIT, False),
    )
    assert calls == [False, True]
    assert replay["status"] == "passed"
    assert replay["exact_zero_call_replay"] is True
    assert replay["semantic_changed_paths"] == []
    assert calls_path.read_bytes() == provider_calls


def test_raw_e2e_binds_exact_html_route_and_requires_a_relationship(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private"
    source_file = workspace / "01_custody" / "files" / "source.html"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("<html>source</html>", encoding="utf-8")
    write_yaml(
        workspace
        / "11_state"
        / "runs"
        / "raw-run"
        / "items"
        / "P1"
        / "frozen_content.yml",
        {
            "content_hash": sha256_file(source_file),
            "source_file": str(source_file),
            "content_route": runner.base.TEXT_ROUTE,
            "media_type": "text/html",
            "source_scope": "full_document",
        },
    )
    settings = runner.base.GateSettings(
        kind="raw_e2e",
        allow_html=True,
        require_direct_image_route=False,
    )
    route_errors, _ = runner.base._route_errors(
        workspace,
        "raw-run",
        [
            {
                "case_id": "html",
                "parent": {"key": "P1"},
                "sha256": sha256_file(source_file),
                "media_type": "text/html",
                "expected_route": "html_text",
                "expected_terminal_status": "validated_note",
            }
        ],
        settings,
    )
    assert route_errors == ["html:route_mismatch"]

    empty_registry = {
        "relations": [],
        "links": [],
        "pair_decisions": [],
        "current_pair_decisions": [],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(
            workspace / "02_source_memory" / "indexes" / name,
            empty_registry,
        )
    write_yaml(
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {
            "relationship_stage_complete": True,
            "relationship_discovery_status": "complete",
            "relationship_discovery_incomplete_jobs": [],
        },
    )
    relationship_errors, _ = runner.base._relationship_errors(
        workspace, {"source-1"}, require_accepted=True
    )
    assert relationship_errors == ["accepted_relationship_missing"]


def test_raw_e2e_binds_zotero_fulltext_and_raw_attachment_custody(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private"
    source_file = workspace / "01_custody" / "files" / "source.html"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("<html>raw attachment</html>", encoding="utf-8")
    fulltext = "Selected Zotero full text."
    write_yaml(
        workspace
        / "11_state"
        / "runs"
        / "raw-run"
        / "items"
        / "P1"
        / "frozen_content.yml",
        {
            "content_hash": sha256_text(fulltext),
            "source_file": "zotero://select/library/items/A1",
            "content_route": "zotero_fulltext",
            "media_type": "text/html",
            "source_scope": "full_document",
        },
    )
    settings = runner.base.GateSettings(
        kind="raw_e2e",
        allow_html=True,
        require_direct_image_route=False,
    )
    route_errors, _ = runner.base._route_errors(
        workspace,
        "raw-run",
        [
            {
                "case_id": "html",
                "parent": {"key": "P1"},
                "attachment": {"key": "A1"},
                "path": source_file,
                "sha256": sha256_file(source_file),
                "media_type": "text/html",
                "zotero_fulltext": {
                    "content": fulltext,
                    "contentType": "text/html",
                },
                "expected_route": "zotero_fulltext",
                "expected_terminal_status": "validated_note",
            }
        ],
        settings,
    )

    assert route_errors == []


def test_gate_binding_case_count_and_media_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "private")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["gate"]["total_attempt_limit"] = 10
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must equal its role limits"):
        runner._manifest_settings(manifest, sha256_file(manifest))

    payload["gate"]["total_attempt_limit"] = 9
    payload["gate"]["case_count"] = 4
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly 4 cases"):
        runner.run_gate(
            mode="prepare",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
        )

    payload["gate"]["case_count"] = 3
    payload["cases"][1]["zotero_attachment"]["data"]["contentType"] = "text/plain"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="contentType mismatch"):
        runner.run_gate(
            mode="prepare",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
        )


def test_strategic_manifests_bind_exact_private_custody_and_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live8, live40, custody8, custody40 = _strategic_manifests(tmp_path)
    with pytest.raises(ValueError, match="custody manifest binding"):
        runner._manifest_settings(live8, sha256_file(live8))
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    monkeypatch.setattr(
        runner,
        "_provider_free_pdf_route",
        lambda *_args, **_kwargs: pytest.fail(
            "validated gates must reuse the frozen route oracle"
        ),
    )

    manifest8, settings8 = runner._manifest_settings(live8, sha256_file(live8))
    manifest40, settings40 = runner._manifest_settings(live40, sha256_file(live40))

    assert settings8.case_count == 8
    assert settings40.case_count == 40
    assert manifest8["source_template_manifest_sha256"] == (
        runner._STRATEGIC_TEMPLATE_MANIFEST_SHA256
    )
    assert manifest40["source_template_manifest_sha256"] == (
        runner._STRATEGIC_TEMPLATE_MANIFEST_SHA256
    )
    assert manifest8["pdf_route_oracle"] == manifest40["pdf_route_oracle"]
    oracle = json.loads(
        Path(manifest8["pdf_route_oracle"]).read_text(encoding="utf-8")
    )
    assert set(oracle) == runner._ROUTE_ORACLE_FIELDS
    assert len(oracle["routes"]) == 9
    assert [row["case_id"] for row in oracle["routes"]] == sorted(
        row["case_id"] for row in oracle["routes"]
    )
    assert [row["content_route"] for row in oracle["routes"]].count(
        runner.base.TEXT_ROUTE
    ) == 2
    assert [row["content_route"] for row in oracle["routes"]].count(
        runner.base.PDF_INPUT_ROUTE
    ) == 7
    assert oracle["request_identity"]["attachment_capability"] == (
        runner.base.codex_source_bundle_attachment_identity(
            runner.base.DIRECT_PDF_CLI_VERSION
        )
    )
    assert "helper_manifest" not in oracle["request_identity"][
        "attachment_capability"
    ]
    custody40_payload = json.loads(custody40.read_text(encoding="utf-8"))
    legacy_ocr_index = next(
        index
        for index, row in enumerate(custody40_payload["sources"])
        if row["selected"]["route"] == "pypdf_poppler_tesseract"
    )
    live40_payload = json.loads(live40.read_text(encoding="utf-8"))
    assert live40_payload["cases"][legacy_ocr_index]["expected"] == {
        "content_route": runner.base.PDF_INPUT_ROUTE,
        "selected_pages": [],
        "terminal_status": "validated_note",
    }
    report_path, report = runner.run_gate(
        mode="prepare",
        manifest_path=live8,
        manifest_sha256=sha256_file(live8),
    )
    assert read_yaml(report_path) == report
    assert report["status"] == "prepared"
    assert report["provider_free_pdf_routes"] == [
        {
            "case_id": str(source["parent_key"]).casefold(),
            "content_route": "pypdf_text",
            "custody_sha256": source["raw"]["sha256"],
            "selected_pages": [],
        }
        for source in sorted(
            (
                row
                for row in json.loads(custody8.read_text(encoding="utf-8"))[
                    "sources"
                ]
                if row["selected"]["route"] == "pypdf_text"
            ),
            key=lambda row: str(row["parent_key"]).casefold(),
        )
    ]
    assert "cluster_expectation" not in json.dumps(
        report["provider_free_pdf_routes"]
    )
    live8_payload = json.loads(live8.read_text(encoding="utf-8"))
    case_ids = [str(case["case_id"]) for case in live8_payload["cases"]]
    assert case_ids == sorted(case_ids)
    _, cases, _workspace = runner.base._validated_manifest(
        live8, sha256_file(live8), settings8
    )
    inventory = runner.base.ManifestZoteroClient(cases).inventory("library")
    assert [str(row["key"]).casefold() for row in inventory] == case_ids
    assert "cluster_expectation" not in json.dumps(inventory)


def test_strategic_route_oracle_is_strict_and_hash_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live8, _live40, custody8, custody40 = _strategic_manifests(tmp_path)
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    manifest = json.loads(live8.read_text(encoding="utf-8"))
    oracle_path = Path(manifest["pdf_route_oracle"])

    manifest["pdf_route_oracle_sha256"] = "0" * 64
    live8.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="oracle SHA-256 mismatch"):
        runner._manifest_settings(live8, sha256_file(live8))

    manifest = json.loads(live8.read_text(encoding="utf-8"))
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    oracle["request_identity"]["question"] = "Prefer a hidden group."
    oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
    manifest["pdf_route_oracle_sha256"] = sha256_file(oracle_path)
    live8.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="oracle identity is invalid"):
        runner._manifest_settings(live8, sha256_file(live8))


def test_strategic8_semantic_oracle_is_hash_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live8, _live40, custody8, custody40 = _strategic_manifests(tmp_path)
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    manifest = json.loads(live8.read_text(encoding="utf-8"))
    manifest["strategic8_semantic_oracle_sha256"] = "0" * 64
    live8.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="semantic oracle SHA-256 mismatch"):
        runner._manifest_settings(live8, sha256_file(live8))


def test_strategic_route_oracle_keeps_legacy_image_routes_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _live8, live40, custody8, custody40 = _strategic_manifests(tmp_path)
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    manifest = json.loads(live40.read_text(encoding="utf-8"))
    oracle_path = Path(manifest["pdf_route_oracle"])
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    legacy = next(
        row
        for row in oracle["routes"]
        if row["content_route"] == runner.base.PDF_INPUT_ROUTE
    )
    legacy["content_route"] = runner.base.IMAGE_ROUTE
    legacy["selected_pages"] = [1]
    oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
    case = next(
        row for row in manifest["cases"] if row["case_id"] == legacy["case_id"]
    )
    case["expected"]["content_route"] = runner.base.IMAGE_ROUTE
    case["expected"]["selected_pages"] = [1]
    manifest["pdf_route_oracle_sha256"] = sha256_file(oracle_path)
    live40.write_text(json.dumps(manifest), encoding="utf-8")

    validated, settings = runner._manifest_settings(live40, sha256_file(live40))

    assert settings.case_count == 40
    assert validated["pdf_route_oracle_sha256"] == sha256_file(oracle_path)


def test_route_oracle_freeze_rejects_protected_custody_output_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live8, live40, custody8, custody40 = _strategic_manifests(tmp_path)
    monkeypatch.setattr(
        runner,
        "_STRATEGIC_CUSTODY_MANIFEST_SHA256",
        {8: sha256_file(custody8), 40: sha256_file(custody40)},
    )
    monkeypatch.setattr(
        runner,
        "_provider_free_pdf_route",
        lambda *_args, **_kwargs: pytest.fail("PDF probe must not run"),
    )

    with pytest.raises(ValueError, match="protected custody workspaces"):
        runner.freeze_routes(
            strategic40_manifest_path=live40,
            strategic40_manifest_sha256=sha256_file(live40),
            strategic8_manifest_path=live8,
            strategic8_manifest_sha256=sha256_file(live8),
            output_path=custody40.parent / "PRIVATE_PDF_ROUTE_ORACLE.json",
            repository_probe=lambda: (CODE_COMMIT, False),
        )


def test_strategic_custody_rejects_corruption_and_wrong_live_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live8, _live40, custody8, custody40 = _strategic_manifests(tmp_path)
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    custody = json.loads(custody8.read_text(encoding="utf-8"))
    raw_path = custody8.parent / custody["sources"][0]["raw"]["path"]
    original = raw_path.read_bytes()
    raw_path.write_bytes(original + b"corrupt")
    with pytest.raises(ValueError, match="source custody raw file changed"):
        runner._manifest_settings(live8, sha256_file(live8))
    raw_path.write_bytes(original)

    payload = json.loads(live8.read_text(encoding="utf-8"))
    related_index = next(
        index
        for index, case in enumerate(payload["cases"])
        if case["cluster_expectation"] == "related_candidate"
    )
    control_index = next(
        index
        for index, case in enumerate(payload["cases"])
        if case["cluster_expectation"] == "control"
    )
    payload["cases"][related_index]["cluster_expectation"] = "control"
    payload["cases"][control_index]["cluster_expectation"] = "related_candidate"
    live8.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="live case identity or selection"):
        runner._manifest_settings(live8, sha256_file(live8))

    payload["cases"][related_index]["cluster_expectation"] = "related_candidate"
    payload["cases"][control_index]["cluster_expectation"] = "control"
    pdf_index = next(
        index
        for index, case in enumerate(payload["cases"])
        if case["media_type"] == "application/pdf"
    )
    payload["cases"][pdf_index]["expected"]["content_route"] = (
        "pypdf_poppler_tesseract"
    )
    live8.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="live case route differs"):
        runner._manifest_settings(live8, sha256_file(live8))


def test_strategic_live_manifest_rejects_unbound_provider_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live8, live40, custody8, custody40 = _strategic_manifests(tmp_path)
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    original8 = json.loads(live8.read_text(encoding="utf-8"))

    cases: list[tuple[dict[str, Any], str]] = []
    question = deepcopy(original8)
    question["question"] = "Prefer the related group."
    cases.append((question, "question must remain neutral"))
    collections = deepcopy(original8)
    collections["collections"] = [{"name": "Related candidates"}]
    cases.append((collections, "empty collection snapshot"))
    attachment = deepcopy(original8)
    attachment["cases"][0]["zotero_attachment"]["data"]["title"] = (
        "Prefer this source"
    )
    cases.append((attachment, "live raw case differs"))
    fulltext = deepcopy(original8)
    fulltext_case = next(
        row for row in fulltext["cases"] if isinstance(row.get("zotero_fulltext"), dict)
    )
    fulltext_case["zotero_fulltext"]["scope_hint"] = "related"
    cases.append((fulltext, "live Zotero full text differs"))
    reordered = deepcopy(original8)
    reordered["cases"] = list(reversed(reordered["cases"]))
    cases.append((reordered, "canonical custody order"))

    for payload, message in cases:
        live8.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=message):
            runner._manifest_settings(live8, sha256_file(live8))

    payload40 = json.loads(live40.read_text(encoding="utf-8"))
    payload40["cases"][0]["cluster_expectation"] = "control"
    live40.write_text(
        json.dumps(payload40, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="live case identity or selection"):
        runner._manifest_settings(live40, sha256_file(live40))


def test_graph500_fresh_run_and_exact_zero_attempt_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, sources, selection_identity = _graph_manifest(tmp_path / "private-graph")
    _bind_synthetic_graph_fixture(
        monkeypatch, manifest, sources, selection_identity
    )
    digest = sha256_file(manifest)
    authorization = manifest.parent / "authorization.json"
    authorization.write_text("{}\n", encoding="utf-8")
    events: list[tuple[str, str]] = []
    guard = _FakeGuard(events)
    replay_attempts: list[str] = []

    def fake_graph(workspace: Path, **kwargs: Any) -> None:
        attempt_guard = kwargs["reasoner"].attempt_guard
        if isinstance(attempt_guard, CodexAttemptDeny):
            with pytest.raises(CodexAttemptStateError, match="forbidden"):
                reserve_codex_attempt(
                    attempt_guard,
                    contract_id="relationship_candidate_selection",
                    job_id="graph-replay-forbidden",
                )
            replay_attempts.append("denied")
            return
        assert attempt_guard is guard
        _write_graph_run(workspace, kwargs)

    _, first = runner.run_gate(
        mode="run",
        manifest_path=manifest,
        manifest_sha256=digest,
        authorization_path=authorization,
        authorization_sha256=sha256_file(authorization),
        execute=True,
        graph_runner=fake_graph,
        repository_probe=lambda: (CODE_COMMIT, False),
        attempt_guard_factory=lambda *_args, **_kwargs: guard,
    )
    assert first["status"] == "passed"
    assert first["source_attempt_count"] == 0
    assert first["relationship_attempt_count"] == 4
    assert first["accepted_relationship_count"] == 2
    assert first["negative_relationship_count"] == 1
    assert first["cluster_count"] == 1
    assert [contract for contract, _ in guard.reservations] == [
        "relationship_candidate_selection",
        "relationship_adjudication",
        "cluster_plan",
        "cluster_synthesis",
    ]
    assert events == [("passed", "")]

    _, replay = runner.run_gate(
        mode="replay",
        manifest_path=manifest,
        manifest_sha256=digest,
        execute=True,
        graph_runner=fake_graph,
        repository_probe=lambda: (CODE_COMMIT, False),
    )
    assert replay["status"] == "passed"
    assert replay["exact_zero_call_replay"] is True
    assert replay["semantic_changed_paths"] == []
    assert replay_attempts == ["denied"]

    registry_path = (
        manifest.parent / "03_literature_synthesis" / "cluster_registry.yml"
    )
    registry = read_yaml(registry_path)
    registry["pending_revisions"] = ["revision-pending"]
    registry["clusters"][0]["missing_member_ids"] = ["source-499"]
    write_yaml(registry_path, registry)
    syntheses_path = (
        manifest.parent / "03_literature_synthesis" / "cluster_syntheses.yml"
    )
    syntheses = read_yaml(syntheses_path)
    synthesis = syntheses["syntheses"]["cluster-all-synthetic"]
    synthesis.update(
        refresh_pending=True,
        missing_member_ids=["source-499"],
        quality_errors=["unsupported_central_claim"],
        parked_for_review=True,
    )
    write_yaml(syntheses_path, syntheses)
    _, settings = runner._manifest_settings(manifest, digest)
    errors, _ = runner._graph_acceptance(
        manifest.parent,
        "synthetic-graph500-e2e-run",
        sources,
        settings,
    )
    assert "graph_cluster_refresh_pending" in errors
    assert "graph_cluster_membership_quality_incomplete" in errors
    assert "graph_cluster_synthesis_quality_incomplete" in errors


def test_graph500_cumulative_attempt_history_uses_latest_logical_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, sources, selection_identity = _graph_manifest(
        tmp_path / "private-graph-attempt-history"
    )
    _bind_synthetic_graph_fixture(
        monkeypatch, manifest_path, sources, selection_identity
    )
    manifest, settings = runner._manifest_settings(
        manifest_path, sha256_file(manifest_path)
    )
    workspace = manifest_path.parent
    run_id = str(manifest["run_id"])
    _, _, source_set = runner._graph_inputs(
        manifest,
        settings,
        require_frozen_notes=True,
        manifest_path=manifest_path,
    )
    _write_graph_run(
        workspace,
        {
            "run_id": run_id,
            "source_set": source_set,
            "provider": "codex",
            "model": runner.base.RELATIONSHIP_MODEL,
            "reasoning_effort": "medium",
            "provider_concurrency": "auto",
            "literature_policy": runner._graph_policy(settings),
            "navigation_policy": runner.NavigationPolicy(),
            "reasoner": type("Reasoner", (), {"attempt_guard": _FakeGuard([])})(),
        },
    )
    usage_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "literature"
        / "synthesis"
        / "provider_usage.yml"
    )
    receipt_path = workspace / "11_state" / "runs" / run_id / "semantic_build_receipt.yml"
    usage = read_yaml(usage_path)
    completed = {**usage["attempts"][0], "attempt_id": "candidate-2", "attempt": 2}
    failed = {
        "attempt_id": "candidate-1",
        "stage": completed["stage"],
        "key": completed["key"],
        "fingerprint": completed["fingerprint"],
        "attempt": 1,
        "status": "failed",
        "failure_class": "timeout",
        "error_type": "ProviderTimeout",
        "provider_completion": {},
    }
    rows = [failed, completed, *usage["attempts"][1:]]

    def persist() -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
        usage["attempts"] = rows
        usage["provider_call_count"] = len(rows)
        write_yaml(usage_path, usage)
        receipt = read_yaml(receipt_path)
        receipt["summary"]["literature_map"]["synthesis_call_count"] = len(rows)
        write_yaml(receipt_path, receipt)
        errors, acceptance = runner._graph_acceptance(
            workspace, run_id, sources, settings
        )
        source, relationship = runner.base._attempts(workspace, run_id)
        return errors, acceptance, [*source["rows"], *relationship["rows"]]

    errors, acceptance, attempts = persist()
    assert errors == []
    assert acceptance["relationship_attempt_count"] == 5
    assert runner.base._pause_reason({}, attempts) == ""

    failed.update(
        failure_class="isolation",
        error_type="ProviderIsolationFailure",
    )
    errors, acceptance, attempts = persist()
    assert "graph_unfinished_relationship_attempt" in errors
    assert acceptance["relationship_attempt_count"] == 5
    assert runner.base._pause_reason({}, attempts) == ""

    failed.update(failure_class="timeout", error_type="ProviderTimeout")
    rows.append(
        {
            **failed,
            "attempt_id": "candidate-3",
            "attempt": 3,
            "status": "interrupted",
            "failure_class": "transport",
            "error_type": "InterruptedProviderAttempt",
            "transport_kind": "interrupted_process",
            "retry_on_resume": True,
        }
    )
    errors, acceptance, attempts = persist()
    assert "graph_unfinished_relationship_attempt" in errors
    assert acceptance["relationship_attempt_count"] == 6
    assert runner.base._pause_reason({}, attempts) == "interruption"

    rows.append({**completed, "attempt_id": "candidate-4", "attempt": 4})
    errors, acceptance, attempts = persist()
    assert errors == []
    assert acceptance["relationship_attempt_count"] == 7
    assert runner.base._pause_reason({}, attempts) == ""


def test_graph500_recomputes_frozen_manifest_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, sources, selection_identity = _graph_manifest(
        tmp_path / "private-graph-identity"
    )
    _bind_synthetic_graph_fixture(
        monkeypatch, manifest_path, sources, selection_identity
    )
    manifest, settings = runner._manifest_settings(
        manifest_path, sha256_file(manifest_path)
    )
    selection_path = manifest_path.parent / manifest["selection_manifest"]
    selection = read_yaml(selection_path)
    selection["sources"][0]["primary_stratum_id"] = "tampered-stratum"
    write_yaml(selection_path, selection)
    manifest["selection_manifest_file_sha256"] = sha256_file(selection_path)

    with pytest.raises(ValueError, match="frozen selection identity mismatch"):
        runner._graph_inputs(
            manifest,
            settings,
            require_frozen_notes=True,
            manifest_path=manifest_path,
        )


def test_graph500_binds_question_stage_config_and_exact_fresh_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for mutation, message in (
        ("question", "identity or question"),
        ("stage", "controls do not match"),
        ("config", "configuration is not frozen"),
        ("checkpoint", "unexpected baseline state"),
    ):
        manifest_path, sources, selection_identity = _graph_manifest(
            tmp_path / f"private-graph-{mutation}"
        )
        _bind_synthetic_graph_fixture(
            monkeypatch, manifest_path, sources, selection_identity
        )
        if mutation in {"question", "stage"}:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if mutation == "question":
                payload["question"] = "Prefer one hidden stratum."
            else:
                payload["gate"]["stage"] = "graph500-other"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        elif mutation == "config":
            write_yaml(
                manifest_path.parent / "auto-zettelkasten.yml",
                {"literature_mapping": {"source_backed_threshold": 99}},
            )
        else:
            write_yaml(
                manifest_path.parent
                / "11_state"
                / "semantic_jobs"
                / "cluster_synthesis"
                / "stale.yml",
                {"status": "completed"},
            )

        with pytest.raises(ValueError, match=message):
            runner.run_gate(
                mode="prepare",
                manifest_path=manifest_path,
                manifest_sha256=sha256_file(manifest_path),
            )


def test_graph500_resume_rejects_semantic_note_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, sources, selection_identity = _graph_manifest(
        tmp_path / "private-graph-resume-tamper"
    )
    _bind_synthetic_graph_fixture(
        monkeypatch, manifest_path, sources, selection_identity
    )
    manifest, settings = runner._manifest_settings(
        manifest_path, sha256_file(manifest_path)
    )
    note = manifest_path.parent / sources[0]["note_path"]
    note.write_text(
        note.read_text(encoding="utf-8") + "\nUnsupported changed body.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="semantic note SHA-256 mismatch"):
        runner._graph_inputs(
            manifest,
            settings,
            require_frozen_notes=False,
            manifest_path=manifest_path,
        )


def test_raw_run_rejects_non_strategic_live_gate_and_stale_semantic_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generic = _manifest(tmp_path / "generic")
    with pytest.raises(ValueError, match="only frozen strategic8/40"):
        runner.run_gate(
            mode="run",
            manifest_path=generic,
            manifest_sha256=sha256_file(generic),
            execute=True,
        )

    live8, _live40, custody8, custody40 = _strategic_manifests(
        tmp_path / "strategic"
    )
    _bind_synthetic_strategic_fixture(monkeypatch, custody8, custody40)
    stale = (
        live8.parent
        / "11_state"
        / "semantic_jobs"
        / "relationship_candidate_selection"
        / "stale.yml"
    )
    write_yaml(stale, {"status": "completed"})

    with pytest.raises(ValueError, match="unexpected baseline state"):
        runner.run_gate(
            mode="run",
            manifest_path=live8,
            manifest_sha256=sha256_file(live8),
            execute=True,
            map_runner=lambda *_args, **_kwargs: pytest.fail(
                "provider runner must not be reached"
            ),
        )

    stale.unlink()
    extra_custody = live8.parent / "01_custody" / "files" / "extra.html"
    extra_custody.write_text("unbound", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected baseline state"):
        runner.run_gate(
            mode="run",
            manifest_path=live8,
            manifest_sha256=sha256_file(live8),
            execute=True,
            map_runner=lambda *_args, **_kwargs: pytest.fail(
                "provider runner must not be reached"
            ),
        )

    extra_custody.unlink()
    config_path = live8.parent / "auto-zettelkasten.yml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\nparallel: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workspace identity is not frozen"):
        runner._manifest_settings(live8, sha256_file(live8))


def test_runner_rejects_auto_zettelkasten_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        runner.base.auto_zettelkasten,
        "__file__",
        str(tmp_path / "another-checkout" / "auto_zettelkasten" / "__init__.py"),
    )
    with pytest.raises(RuntimeError, match="outside this repository's src"):
        runner.run_gate(
            mode="prepare",
            manifest_path=tmp_path / "not-read.json",
            manifest_sha256="0" * 64,
        )


def test_private_strategic8_cluster_labels_never_enter_provider_inputs(
    tmp_path: Path,
) -> None:
    cases = []
    for index in range(8):
        parent = {
            "key": f"P{index}",
            "data": {"key": f"P{index}", "title": f"Source {index}"},
        }
        cases.append(
            {
                "parent": parent,
                "cluster_expectation": (
                    "related_candidate" if index < 4 else "control"
                ),
            }
        )
    source_ids = [runner.base.source_id_for_item(row["parent"]) for row in cases]
    client = runner.base.ManifestZoteroClient(cases)
    assert "cluster_expectation" not in json.dumps(client.inventory("library"))
    report = {"cluster_map": {"clusters": [{"source_ids": source_ids[:3]}]}}
    assert runner.base._private_cluster_expectation_errors(cases, report) == []

    report["cluster_map"]["clusters"][0]["source_ids"].append(source_ids[4])
    assert runner.base._private_cluster_expectation_errors(cases, report) == [
        "private_related_cluster_or_control_separation_failed"
    ]

    expected = source_ids[:4]
    accepted = [
        (source_ids[0], source_ids[1]),
        (source_ids[1], source_ids[2]),
        (source_ids[2], source_ids[3]),
    ]
    registry = {
        "relations": [
            {
                "source_id": left,
                "target_source_id": right,
                "decision_status": "accepted",
                "active": True,
            }
            for left, right in accepted
        ],
        "current_pair_decisions": [
            {
                "source_ids": [left, right],
                "status": "accepted" if (left, right) in accepted else "no_relationship",
            }
            for left, right in combinations(expected, 2)
        ],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(tmp_path / "02_source_memory" / "indexes" / name, registry)
    report = {
        "cluster_map": {
            "clusters": [
                {
                    "source_ids": expected,
                    "source_roles": [
                        {"source_id": source_id, "role": "core"}
                        for source_id in source_ids[:3]
                    ]
                    + [{"source_id": source_ids[3], "role": "context"}],
                }
            ]
        }
    }
    oracle = {
        "sha256": "1" * 64,
        "core_parent_keys": [f"p{index}" for index in range(3)],
        "context_parent_key": "p3",
        "control_parent_keys": [f"p{index}" for index in range(4, 8)],
    }
    errors, acceptance = runner._strategic8_oracle_acceptance(
        tmp_path, cases, report, oracle
    )
    assert errors == []
    assert acceptance["strategic8_evaluated_required_pair_count"] == 6

    registry["current_pair_decisions"].pop()
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "typed_links.yml", registry
    )
    errors, _acceptance = runner._strategic8_oracle_acceptance(
        tmp_path, cases, report, oracle
    )
    assert errors == ["strategic8_required_pairs_not_all_evaluated"]
