#!/usr/bin/env python3
"""Run a hash-locked raw-custody Codex atomic/relationship/cluster gate."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from typing import Any

import v030_codex_pdf_eval as base
from auto_zettelkasten.api import build_map
from auto_zettelkasten.extraction import (
    _ocr_pdf_page,
    _page_text_is_suspicious,
    _short_ocr_text_is_readable,
    probe_pdf_bytes,
)
from auto_zettelkasten.models import LiteratureMappingPolicy, NavigationPolicy
from auto_zettelkasten.notes import read_note, semantic_note_hash
from auto_zettelkasten.pipeline import _custodied_pdf_candidate
from auto_zettelkasten.readers import CodexReader
from v030_codex_campaign_guard import CodexCampaignGuard


_GATE_FIELDS = {
    "schema_version",
    "kind",
    "stage",
    "case_count",
    "source_attempt_limit",
    "relationship_attempt_limit",
    "total_attempt_limit",
    "document_attempt_limit",
    "stage_deadline_seconds",
    "cluster_generation_enabled",
}
_GRAPH500_MANIFEST_SHA256 = (
    "c8049f3c213b1c9512c2bb00c234e09e4263ed249c6e88fa41e5c392e7419930"
)
_GRAPH500_CASE_COUNT = 500
_GRAPH500_RELATIONSHIP_LIMIT = 233
_GRAPH500_DEADLINE_SECONDS = 14_400
_GRAPH500_STAGE = "graph500"
_GRAPH500_QUESTION = "Which relationships and clusters organize this sample?"
_GRAPH500_CONFIG_SHA256 = (
    "62cb726a98c75db3cdef5d5b721d5a762d411e52e6a1ff9c4559720e3c913d75"
)
_GRAPH500_WORKSPACE_MANIFEST_SHA256 = (
    "4ff1806546b456d7f4fa2728d16ebeabaade68fb564105931535e4aec87ef6d9"
)
_GRAPH500_BASELINE_FILE_COUNT = 1_990
_GRAPH500_BASELINE_SHA256 = (
    "a54da2024064714c2c7b33301c74debba268e410598c321927a5db67844c97d2"
)
_STRATEGIC_TEMPLATE_MANIFEST_SHA256 = (
    "d03588ec13153d233e6555cdb96cab58dbfa27c25edc63c9c51848a33d282545"
)
_STRATEGIC_QUESTION = "How do the frozen sources relate?"
_RAW_CONFIG_SHA256 = (
    "30c031ef07a78a8f0c3f0d63cfd1bca1ad4fee8d786bf560884718f039162d4d"
)
_STRATEGIC_CUSTODY_MANIFEST_SHA256 = {
    8: "9cdd578f41529ecbedb57978493bb1169d6432fda7bee3cf2cf94a55593ee3cd",
    40: "134984d8431baf397bc5d23b3c067d7e0569ef019fd9a1b91bc7dbb16621abe4",
}
_STRATEGIC_CONTROLS = {
    8: {
        "stage": "strategic8",
        "source_attempt_limit": 8,
        "relationship_attempt_limit": 12,
        "total_attempt_limit": 20,
        "document_attempt_limit": 4,
        "stage_deadline_seconds": 8_460,
        "cluster_generation_enabled": True,
    },
    40: {
        "stage": "strategic40",
        "source_attempt_limit": 52,
        "relationship_attempt_limit": 28,
        "total_attempt_limit": 80,
        "document_attempt_limit": 8,
        "stage_deadline_seconds": 14_400,
        "cluster_generation_enabled": True,
    },
}
_ROUTE_ORACLE_FIELDS = {
    "schema_version",
    "kind",
    "code_commit",
    "custody_manifest_sha256",
    "source_template_manifest_sha256",
    "request_identity",
    "routes",
}
_ROUTE_ORACLE_ROW_FIELDS = {
    "case_id",
    "custody_sha256",
    "content_route",
    "selected_pages",
}
_STRATEGIC8_ORACLE_FIELDS = {
    "schema_version",
    "kind",
    "source_custody_manifest_sha256",
    "source_template_manifest_sha256",
    "core_parent_keys",
    "context_parent_key",
    "control_parent_keys",
}

def _safe_relative_file(root: Path, value: Any, *, label: str) -> Path:
    relative = Path(str(value or ""))
    path = (root / relative).resolve()
    if (
        not str(value or "")
        or relative.is_absolute()
        or not base._inside(path, root)
        or not path.is_file()
    ):
        raise ValueError(f"{label} is missing or outside its custody workspace")
    return path


def _workspace_manifest_is_bound(workspace: Path) -> bool:
    payload = base.read_yaml(workspace / "11_state" / "workspace_manifest.yml", {})
    return (
        isinstance(payload, Mapping)
        and set(payload)
        == {"engine_version", "artifact_schema_version", "created_at", "workspace"}
        and payload.get("engine_version") == "0.30.0"
        and payload.get("artifact_schema_version") == "1.20"
        and bool(str(payload.get("created_at") or ""))
        and Path(str(payload.get("workspace") or "")).resolve() == workspace
    )


def _campaign_allowlist(
    workspace: Path,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    authorization_path: Path | None,
) -> set[str]:
    paths = {
        manifest_path.resolve(),
        workspace
        / "11_state"
        / "evaluations"
        / "codex-e2e"
        / f"{manifest.get('evaluation_id')}-prepare.yml",
        workspace / ".v030-codex-e2e-attempt-ledger.json",
        workspace / ".v030-codex-e2e-attempt-ledger.lock",
    }
    if authorization_path is not None:
        paths.add(authorization_path.resolve())
    return {
        str(path.relative_to(workspace))
        for path in paths
        if base._inside(path, workspace)
    }


def _assert_exact_fresh_inventory(
    workspace: Path,
    expected: set[str],
    allowed_campaign: set[str],
) -> None:
    symlinks = [
        str(path.relative_to(workspace))
        for path in workspace.rglob("*")
        if path.is_symlink()
    ]
    if symlinks:
        raise ValueError(f"fresh disposable workspace contains a symlink: {symlinks[0]}")
    actual = {
        str(path.relative_to(workspace))
        for path in workspace.rglob("*")
        if path.is_file()
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected - allowed_campaign)
    if missing or extra:
        detail = (missing or extra)[0]
        kind = "missing" if missing else "unexpected"
        raise ValueError(
            f"fresh disposable workspace has {kind} baseline state: {detail}"
        )


def _inventory_sha256(workspace: Path, relative_paths: set[str]) -> str:
    rows = [
        {
            "path": relative,
            "sha256": base.sha256_file(workspace / relative),
            "size": (workspace / relative).stat().st_size,
        }
        for relative in sorted(relative_paths)
    ]
    return base.sha256_text(
        json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def _assert_raw_fresh_workspace(
    workspace: Path,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    authorization_path: Path | None,
) -> None:
    expected = {
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
    }
    for row in manifest.get("cases", []) or []:
        if not isinstance(row, Mapping) or not row.get("file"):
            continue
        path = (workspace / str(row["file"])).resolve()
        if not base._inside(path, workspace):
            raise ValueError("raw E2E custody path is outside the live workspace")
        expected.add(str(path.relative_to(workspace)))
    _assert_exact_fresh_inventory(
        workspace,
        expected,
        _campaign_allowlist(workspace, manifest, manifest_path, authorization_path),
    )


def _private_json(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
    filename: str = "PRIVATE_CUSTODY_MANIFEST.json",
) -> dict[str, Any]:
    resolved = base._private(path, label=label)
    if resolved.name != filename or not resolved.is_file():
        raise ValueError(f"{label} must be {filename}")
    if not base._SHA256.fullmatch(expected_sha256) or base.sha256_file(resolved) != expected_sha256:
        raise ValueError(f"{label} SHA-256 mismatch")
    try:
        return base._mapping(json.loads(resolved.read_text(encoding="utf-8")), label=label)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def _provider_free_pdf_route(
    case: Mapping[str, Any],
    request: Any,
    *,
    reader: CodexReader,
    frozen_fallback_route: str,
) -> tuple[str, list[int]]:
    path = case.get("path")
    attachment = case.get("attachment")
    parent = case.get("parent")
    if not isinstance(path, Path) or not isinstance(attachment, Mapping):
        raise ValueError("PDF route validation requires bound custody evidence")
    if not isinstance(parent, Mapping):
        raise ValueError("PDF route validation requires a bound parent record")
    status = reader.pdf_input_file_status()
    if (
        status.get("version") != base.DIRECT_PDF_CLI_VERSION
        or status.get("helper_version") != base.DIRECT_PDF_CLI_VERSION
        or status.get("helper_manifest_valid") is not True
        or status.get("pdf_input_file_capability") is not True
        or not base._direct_pdf_helper_identity_valid(
            status.get("_helper_manifest_identity")
        )
    ):
        raise ValueError("PDF route validation requires the verified Codex PDF helper")
    fallback = request.extraction_policy.pdf_fallback
    probe_request = (
        replace(
            request,
            extraction_policy=replace(request.extraction_policy, pdf_fallback="none"),
        )
        if fallback == "ocr"
        else request
    )
    document = path.read_bytes()
    candidate, extracted = _custodied_pdf_candidate(
        document,
        path,
        attachment,
        parent,
        {"source_id": base.source_id_for_item(parent)},
        probe_request,
        actual_primary_pdf=True,
        cancelled=None,
        reader=reader,
    )
    if (
        candidate is None
        and extracted.route == "codex_pdf_unsupported"
        and extracted.reason == "pdf_token_ceiling_exceeded"
        and fallback == "ocr"
    ):
        if frozen_fallback_route not in {
            "pypdf_pdfium_tesseract",
            "pypdf_poppler_tesseract",
        }:
            raise ValueError(f"{case['case_id']} frozen OCR fallback route is invalid")
        probe = probe_pdf_bytes(document)
        if probe.status == "failed" or not probe.suspicious_pages:
            raise ValueError(f"{case['case_id']} OCR renderer probe is unavailable")
        # ponytail: one accepted PDFium page pins the aggregate route; otherwise stop.
        recovered = _ocr_pdf_page(
            document,
            probe.suspicious_pages[0] - 1,
            request.extraction_policy.languages,
        )
        if (
            not recovered.available
            or recovered.route != "pdfium_tesseract"
            or (
                _page_text_is_suspicious(recovered.text)
                and not _short_ocr_text_is_readable(recovered.text)
            )
        ):
            raise ValueError(f"{case['case_id']} OCR renderer probe failed")
        return f"pypdf_{recovered.route}", []
    if candidate is None or extracted.status != "succeeded":
        raise ValueError(f"{case['case_id']} PDF route probe did not succeed")
    selected_pages: list[int] = []
    if extracted.route == base.IMAGE_ROUTE:
        document_route = base._mapping(
            candidate.get("document_route"), label="PDF document route"
        )
        identity = base._mapping(
            document_route.get("identity_payload"), label="PDF route identity"
        )
        selected_pages = [int(value) for value in identity.get("selected_pages", [])]
    return str(extracted.route), selected_pages


def _pdf_route_request_identity(request: Any) -> dict[str, Any]:
    if isinstance(request, Mapping):
        identity = dict(request)
    else:
        payload = base._mapping(request.to_dict(), label="PDF route request")
        identity = {
            key: payload[key]
            for key in (
                "question",
                "provider",
                "model",
                "reasoning_effort",
                "allow_cloud",
                "extraction_version",
                "prompt_version",
                "extraction_policy",
                "processing",
            )
        } | {
            "attachment_capability": base.codex_source_bundle_attachment_identity(
                base.DIRECT_PDF_CLI_VERSION
            )
        }
    processing = base._mapping(identity.get("processing"), label="PDF route processing")
    # Attempt ceilings remain manifest/authorization-bound, not PDF routing inputs.
    processing.pop("max_calls_per_document_run", None)
    return {**identity, "processing": processing}


def _validated_route_oracle(
    manifest: Mapping[str, Any],
    workspace: Path,
    request: Any,
    all_sources: Sequence[Mapping[str, Any]],
    protected_roots: Sequence[Path],
) -> dict[str, dict[str, Any]]:
    value = manifest.get("pdf_route_oracle")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("strategic pdf_route_oracle must be an absolute path")
    path = base._private(Path(value), label="PDF route oracle")
    if path.name != "PRIVATE_PDF_ROUTE_ORACLE.json" or any(
        base._inside(path, root) for root in (workspace, *protected_roots)
    ):
        raise ValueError(
            "PDF route oracle must be outside live and protected custody workspaces"
        )
    digest = str(manifest.get("pdf_route_oracle_sha256") or "")
    if not base._SHA256.fullmatch(digest) or base.sha256_file(path) != digest:
        raise ValueError("PDF route oracle SHA-256 mismatch")
    try:
        oracle = base._mapping(
            json.loads(path.read_text(encoding="utf-8")), label="PDF route oracle"
        )
    except json.JSONDecodeError as exc:
        raise ValueError("PDF route oracle must be valid JSON") from exc
    custody_hashes = base._mapping(
        oracle.get("custody_manifest_sha256"), label="PDF route custody hashes"
    )
    if (
        set(oracle) != _ROUTE_ORACLE_FIELDS
        or oracle.get("schema_version") != "1"
        or oracle.get("kind") != "v030_private_pdf_route_oracle"
        or oracle.get("code_commit") != manifest.get("code_commit")
        or custody_hashes
        != {
            "strategic8": _STRATEGIC_CUSTODY_MANIFEST_SHA256[8],
            "strategic40": _STRATEGIC_CUSTODY_MANIFEST_SHA256[40],
        }
        or oracle.get("source_template_manifest_sha256")
        != _STRATEGIC_TEMPLATE_MANIFEST_SHA256
        or _pdf_route_request_identity(base._mapping(
            oracle.get("request_identity"), label="PDF route request identity"
        )) != _pdf_route_request_identity(request)
    ):
        raise ValueError("PDF route oracle identity is invalid")
    rows = [
        base._mapping(row, label="PDF route oracle row")
        for row in oracle.get("routes", []) or []
    ]
    expected_pdf = {
        str(row["parent_key"]).casefold(): str(
            base._mapping(row.get("raw"), label="PDF custody raw file")["sha256"]
        )
        for row in all_sources
        if base._mapping(row.get("selected"), label="PDF custody selection").get(
            "media_type"
        )
        == "application/pdf"
    }
    routes: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        selected_pages = row.get("selected_pages")
        route = str(row.get("content_route") or "")
        if (
            set(row) != _ROUTE_ORACLE_ROW_FIELDS
            or case_id in routes
            or expected_pdf.get(case_id) != row.get("custody_sha256")
            or route
            not in {
                base.IMAGE_ROUTE,
                base.PDF_INPUT_ROUTE,
                "pypdf_text",
                "pypdf_pdfium_tesseract",
                "pypdf_poppler_tesseract",
            }
            or not isinstance(selected_pages, list)
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page < 1
                for page in selected_pages
            )
            or selected_pages != sorted(set(selected_pages))
            or (route == base.IMAGE_ROUTE) != bool(selected_pages)
            or len(selected_pages) > 16
        ):
            raise ValueError("PDF route oracle row is invalid")
        routes[case_id] = row
    if (
        len(rows) != 9
        or [str(row["case_id"]) for row in rows] != sorted(expected_pdf)
        or set(routes) != set(expected_pdf)
    ):
        raise ValueError("PDF route oracle inventory is invalid")
    return routes


def _validated_custody_sources(
    path: Path,
    manifest: Mapping[str, Any],
    *,
    expected_count: int,
    verify_origin: bool,
) -> list[dict[str, Any]]:
    if (
        str(manifest.get("schema_version") or "") != "1"
        or manifest.get("status") != "frozen_private_raw_custody"
        or manifest.get("custody_only") is not True
        or manifest.get("private") is not True
        or manifest.get("never_production_prompt_input") is not True
    ):
        raise ValueError("source custody manifest identity is invalid")
    sources = [
        base._mapping(row, label="source custody row")
        for row in manifest.get("sources", []) or []
    ]
    if len(sources) != expected_count or [row.get("ordinal") for row in sources] != list(
        range(1, expected_count + 1)
    ):
        raise ValueError("source custody manifest count or order is invalid")

    origin_root: Path | None = None
    if verify_origin:
        origin = base._mapping(manifest.get("origin"), label="source custody origin")
        origin_name = str(origin.get("workspace") or "")
        if not origin_name or Path(origin_name).name != origin_name:
            raise ValueError("source custody origin workspace is invalid")
        origin_root = base._private(path.parent.parent / origin_name, label="source custody origin")
        inventory = _safe_relative_file(
            origin_root, origin.get("inventory_path"), label="source custody origin inventory"
        )
        if (
            base.sha256_file(inventory) != str(origin.get("inventory_sha256") or "")
            or inventory.stat().st_size != origin.get("inventory_size")
        ):
            raise ValueError("source custody origin inventory changed")

    source_ids: set[str] = set()
    media_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    for row in sources:
        parent = base._mapping(row.get("parent_record"), label="source custody parent")
        parent_data = base._mapping(parent.get("data", parent), label="source custody parent data")
        parent_key = str(row.get("parent_key") or "")
        source_id = str(row.get("source_id") or "")
        if (
            not parent_key
            or str(parent.get("key") or parent_data.get("key") or "") != parent_key
            or base.source_id_for_item(parent) != source_id
            or source_id in source_ids
        ):
            raise ValueError("source custody identity is invalid")
        source_ids.add(source_id)
        selected = base._mapping(row.get("selected"), label="source custody selection")
        disposition = str(row.get("disposition") or "")
        disposition_counts[disposition] += 1
        raw = row.get("raw")
        fulltext = row.get("zotero_fulltext")
        if disposition == "metadata_only":
            if (
                raw is not None
                or fulltext is not None
                or selected.get("media_type") != "application/json"
                or selected.get("route") != "zotero_metadata"
                or selected.get("scope") != "metadata_only"
                or selected.get("terminal_status") != "limited_note"
            ):
                raise ValueError("metadata-only custody selection is invalid")
            media_counts["application/json"] += 1
            continue
        if disposition != "substantive_raw_source":
            raise ValueError("source custody disposition is invalid")
        raw_row = base._mapping(raw, label="source custody raw file")
        media_type = str(raw_row.get("media_type") or "")
        if media_type not in {"application/pdf", "text/html"}:
            raise ValueError("source custody media type is invalid")
        target_file = _safe_relative_file(
            path.parent, raw_row.get("path"), label="source custody raw file"
        )
        expected_digest = str(raw_row.get("sha256") or "")
        if (
            not base._SHA256.fullmatch(expected_digest)
            or base.sha256_file(target_file) != expected_digest
            or target_file.stat().st_size != raw_row.get("size")
        ):
            raise ValueError("source custody raw file changed")
        if origin_root is not None:
            origin_file = _safe_relative_file(
                origin_root,
                raw_row.get("origin_relative_path"),
                label="source custody origin raw file",
            )
            if (
                base.sha256_file(origin_file) != expected_digest
                or origin_file.stat().st_size != raw_row.get("size")
            ):
                raise ValueError("source custody origin raw file changed")
        allowed_routes = (
            {
                "pypdf_text",
                "pypdf_poppler_tesseract",
                base.IMAGE_ROUTE,
                base.PDF_INPUT_ROUTE,
            }
            if media_type == "application/pdf"
            else {"html_text", "zotero_fulltext"}
        )
        if (
            selected.get("media_type") != media_type
            or selected.get("route") not in allowed_routes
            or selected.get("scope") not in {"full_document", "partial_document"}
            or selected.get("terminal_status") != "validated_note"
        ):
            raise ValueError("substantive custody selection is invalid")
        if fulltext is not None:
            fulltext_row = base._mapping(fulltext, label="source custody full text")
            fulltext_file = _safe_relative_file(
                path.parent,
                fulltext_row.get("path"),
                label="source custody full text",
            )
            fulltext_digest = str(fulltext_row.get("sha256") or "")
            if (
                media_type != "text/html"
                or not base._SHA256.fullmatch(fulltext_digest)
                or base.sha256_file(fulltext_file) != fulltext_digest
                or fulltext_file.stat().st_size != fulltext_row.get("size")
            ):
                raise ValueError("source custody full text changed")
        media_counts[media_type] += 1

    expected_media = (
        Counter({"text/html": 6, "application/pdf": 2})
        if expected_count == 8
        else Counter({"text/html": 25, "application/pdf": 9, "application/json": 6})
    )
    expected_dispositions = (
        Counter({"substantive_raw_source": 8})
        if expected_count == 8
        else Counter({"substantive_raw_source": 34, "metadata_only": 6})
    )
    if media_counts != expected_media or disposition_counts != expected_dispositions:
        raise ValueError("source custody media or disposition counts are invalid")
    return sources


def _custody_origin_root(path: Path, manifest: Mapping[str, Any]) -> Path:
    origin = base._mapping(manifest.get("origin"), label="source custody origin")
    name = str(origin.get("workspace") or "")
    if not name or Path(name).name != name:
        raise ValueError("source custody origin workspace is invalid")
    return base._private(path.parent.parent / name, label="source custody origin")


def _validate_strategic_custody(
    manifest_path: Path,
    manifest_sha256: str,
    manifest: dict[str, Any],
    settings: base.GateSettings,
    *,
    compute_pdf_routes: bool = False,
) -> list[dict[str, Any]]:
    expected = _STRATEGIC_CONTROLS.get(settings.case_count)
    if expected is None:
        return []
    if manifest.get("question") != _STRATEGIC_QUESTION:
        raise ValueError("strategic question must remain neutral and canonical")
    if manifest.get("collections") != []:
        raise ValueError("strategic canaries require an empty collection snapshot")
    controls = {key: manifest.get("gate", {}).get(key) for key in expected}
    legacy40 = {
        **expected, "source_attempt_limit": 40, "relationship_attempt_limit": 40,
        "document_attempt_limit": 4,
    }
    if controls != expected and not (settings.case_count == 40 and controls == legacy40):
        raise ValueError(f"strategic{settings.case_count} gate controls are invalid")
    custody_value = manifest.get("source_custody_manifest")
    if not isinstance(custody_value, str) or not Path(custody_value).is_absolute():
        raise ValueError("strategic source_custody_manifest must be an absolute path")
    custody_path = base._private(Path(custody_value), label="source custody manifest")
    workspace = base._private(Path(str(manifest.get("workspace") or "")), label="workspace")
    if base._inside(custody_path, workspace) or base._inside(workspace, custody_path.parent):
        raise ValueError("source custody manifest must be outside the live workspace")
    custody_sha256 = str(manifest.get("source_custody_manifest_sha256") or "")
    if custody_sha256 != _STRATEGIC_CUSTODY_MANIFEST_SHA256[settings.case_count]:
        raise ValueError("strategic source custody manifest binding is invalid")
    custody = _private_json(custody_path, custody_sha256, label="source custody manifest")
    template_sha256 = str(manifest.get("source_template_manifest_sha256") or "")
    selection = base._mapping(custody.get("selection"), label="source custody selection")
    if (
        template_sha256 != _STRATEGIC_TEMPLATE_MANIFEST_SHA256
        or selection.get("template_manifest_sha256") != template_sha256
    ):
        raise ValueError("strategic source template manifest binding is invalid")

    sources = _validated_custody_sources(
        custody_path,
        custody,
        expected_count=settings.case_count,
        verify_origin=settings.case_count == 40,
    )
    all_sources = sources
    protected_roots = [custody_path.parent]
    if settings.case_count == 40:
        protected_roots.append(_custody_origin_root(custody_path, custody))
    if settings.case_count == 8:
        role_counts = Counter(str(row.get("cluster_expectation") or "") for row in sources)
        if role_counts != Counter({"related_candidate": 4, "control": 4}):
            raise ValueError("strategic8 custody labels are invalid")
        derived = base._mapping(custody.get("derived_from"), label="strategic8 derivation")
        parent_name = str(derived.get("workspace") or "")
        parent_relative = str(derived.get("manifest_path") or "")
        if not parent_name or Path(parent_name).name != parent_name or Path(parent_relative).name != parent_relative:
            raise ValueError("strategic8 derivation path is invalid")
        parent_path = base._private(
            custody_path.parent.parent / parent_name / parent_relative,
            label="strategic40 custody manifest",
        )
        parent_sha256 = str(derived.get("manifest_sha256") or "")
        if parent_sha256 != _STRATEGIC_CUSTODY_MANIFEST_SHA256[40]:
            raise ValueError("strategic8 parent custody manifest binding is invalid")
        parent = _private_json(parent_path, parent_sha256, label="strategic40 custody manifest")
        parent_sources = _validated_custody_sources(
            parent_path, parent, expected_count=40, verify_origin=True
        )
        all_sources = parent_sources
        protected_roots.extend(
            [parent_path.parent, _custody_origin_root(parent_path, parent)]
        )
        if derived.get("template_manifest_sha256") != template_sha256:
            raise ValueError("strategic8 derivation template binding is invalid")
        parent_by_id = {str(row["source_id"]): row for row in parent_sources}
        for row in sources:
            parent_row = parent_by_id.get(str(row["source_id"]))
            if parent_row is None or any(
                row.get(key) != parent_row.get(key)
                for key in (
                    "source_id",
                    "parent_key",
                    "parent_record",
                    "phase",
                    "disposition",
                    "raw",
                    "selected",
                    "zotero_fulltext",
                )
            ):
                raise ValueError("strategic8 source differs from strategic40 custody")
        manifest["_strategic8_semantic_oracle"] = _validated_strategic8_oracle(
            manifest,
            workspace,
            protected_roots,
            sources,
            custody_sha256,
            template_sha256,
        )

    _, cases, live_workspace = base._validated_manifest(
        manifest_path, manifest_sha256, settings
    )
    expected_sources = (
        sorted(
            sources,
            key=lambda row: (
                str(row["parent_key"]).casefold(),
                str(row["source_id"]),
            ),
        )
        if settings.case_count == 8
        else sources
    )
    if [case["case_id"] for case in cases] != [
        str(row["parent_key"]).casefold() for row in expected_sources
    ]:
        raise ValueError("live case order differs from the canonical custody order")
    request = base._request(manifest, live_workspace, settings)
    route_reader = (
        CodexReader(
            base.SOURCE_MODEL,
            allow_cloud=True,
            reasoning_effort=base.REASONING_EFFORT,
            credential_forbidden_roots=tuple(
                dict.fromkeys((live_workspace, *protected_roots))
            ),
        )
        if compute_pdf_routes
        else None
    )
    oracle_routes = (
        {}
        if compute_pdf_routes
        else _validated_route_oracle(
            manifest,
            live_workspace,
            request,
            all_sources,
            protected_roots,
        )
    )
    route_oracle: list[dict[str, Any]] = []
    for source, case in zip(expected_sources, cases, strict=True):
        raw = source.get("raw")
        selected = base._mapping(source.get("selected"), label="source custody selection")
        parent_key = str(source["parent_key"])
        if (
            case["case_id"] != parent_key.casefold()
            or case["parent"] != source["parent_record"]
            or case["media_type"] != selected["media_type"]
            or case["expected_terminal_status"] != selected["terminal_status"]
            or (
                settings.case_count == 8
                and case["cluster_expectation"]
                != str(source.get("cluster_expectation") or "")
            )
            or (
                settings.case_count == 40
                and case["cluster_expectation"] != ""
            )
        ):
            raise ValueError("live case identity or selection differs from source custody")
        if raw is None:
            if case["path"] is not None or case["attachment"] is not None or case["expected_route"] != "zotero_metadata":
                raise ValueError("live metadata-only case differs from source custody")
            continue
        raw_row = base._mapping(raw, label="source custody raw file")
        expected_attachment = {
            "key": raw_row["attachment_key"],
            "data": {
                "key": raw_row["attachment_key"],
                "parentItem": parent_key,
                "itemType": "attachment",
                "contentType": raw_row["media_type"],
                "filename": Path(str(raw_row["path"])).name,
            },
        }
        if (
            case["sha256"] != raw_row["sha256"]
            or case["path"].relative_to(live_workspace).as_posix() != raw_row["path"]
            or case["attachment"] != expected_attachment
        ):
            raise ValueError("live raw case differs from source custody")
        if case["media_type"] == "application/pdf":
            if compute_pdf_routes:
                assert route_reader is not None
                expected_live_route, expected_pages = _provider_free_pdf_route(
                    case,
                    request,
                    reader=route_reader,
                    frozen_fallback_route=str(selected["route"]),
                )
            else:
                route_row = oracle_routes.get(case["case_id"])
                if route_row is None:
                    raise ValueError("PDF route oracle omits a live PDF case")
                expected_live_route = str(route_row["content_route"])
                expected_pages = list(route_row["selected_pages"])
            route_oracle.append(
                {
                    "case_id": case["case_id"],
                    "custody_sha256": case["sha256"],
                    "content_route": expected_live_route,
                    "selected_pages": expected_pages,
                }
            )
        else:
            expected_live_route, expected_pages = "html_text", []
        if (
            not compute_pdf_routes
            and (
                case["expected_route"] != expected_live_route
                or case["expected_selected_pages"] != expected_pages
            )
        ):
            raise ValueError("live case route differs from frozen custody policy")
        frozen_fulltext = source.get("zotero_fulltext")
        live_fulltext = case.get("zotero_fulltext")
        if frozen_fulltext is None:
            if live_fulltext is not None:
                raise ValueError("live case adds unbound Zotero full text")
        else:
            frozen_fulltext = base._mapping(frozen_fulltext, label="source custody full text")
            frozen_fulltext_file = _safe_relative_file(
                custody_path.parent,
                frozen_fulltext.get("path"),
                label="source custody full text",
            )
            expected_fulltext = {
                "content": frozen_fulltext_file.read_text(encoding="utf-8"),
                "contentType": "text/html",
            }
            if (
                live_fulltext != expected_fulltext
                or base.sha256_text(expected_fulltext["content"])
                != frozen_fulltext.get("sha256")
            ):
                raise ValueError("live Zotero full text differs from source custody")
    return route_oracle


def _validated_strategic8_oracle(
    manifest: Mapping[str, Any],
    workspace: Path,
    protected_roots: Sequence[Path],
    sources: Sequence[Mapping[str, Any]],
    custody_sha256: str,
    template_sha256: str,
) -> dict[str, Any]:
    value = manifest.get("strategic8_semantic_oracle")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("strategic8 semantic oracle must be an absolute path")
    path = base._private(Path(value), label="strategic8 semantic oracle")
    if not path.is_file() or any(
        base._inside(path, root) for root in (workspace, *protected_roots)
    ):
        raise ValueError(
            "strategic8 semantic oracle must be outside live and custody workspaces"
        )
    expected_sha256 = str(manifest.get("strategic8_semantic_oracle_sha256") or "")
    if not base._SHA256.fullmatch(expected_sha256) or base.sha256_file(path) != expected_sha256:
        raise ValueError("strategic8 semantic oracle SHA-256 mismatch")
    oracle = _private_json(
        path,
        expected_sha256,
        label="strategic8 semantic oracle",
        filename="PRIVATE_SEMANTIC_ORACLE.json",
    )
    if (
        set(oracle) != _STRATEGIC8_ORACLE_FIELDS
        or oracle.get("schema_version") != "1"
        or oracle.get("kind") != "v030_strategic8_semantic_oracle"
        or oracle.get("source_custody_manifest_sha256") != custody_sha256
        or oracle.get("source_template_manifest_sha256") != template_sha256
    ):
        raise ValueError("strategic8 semantic oracle identity is invalid")

    def keys(name: str, count: int) -> list[str]:
        values = oracle.get(name)
        if (
            not isinstance(values, list)
            or len(values) != count
            or any(not isinstance(item, str) or not item.strip() for item in values)
        ):
            raise ValueError(f"strategic8 semantic oracle {name} is invalid")
        normalized = [item.casefold() for item in values]
        if len(set(normalized)) != count:
            raise ValueError(f"strategic8 semantic oracle {name} is invalid")
        return normalized

    core = keys("core_parent_keys", 3)
    controls = keys("control_parent_keys", 4)
    context = str(oracle.get("context_parent_key") or "").strip().casefold()
    known = {str(row.get("parent_key") or "").casefold() for row in sources}
    if (
        not context
        or len({*core, context, *controls}) != 8
        or {*core, context, *controls} != known
    ):
        raise ValueError("strategic8 semantic oracle source partition is invalid")
    return {
        "sha256": expected_sha256,
        "core_parent_keys": core,
        "context_parent_key": context,
        "control_parent_keys": controls,
    }


def _strategic8_oracle_acceptance(
    workspace: Path,
    cases: Sequence[Mapping[str, Any]],
    report: Mapping[str, Any],
    oracle: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    source_by_parent = {
        str(row["parent"]["key"]).casefold(): base.source_id_for_item(row["parent"])
        for row in cases
    }
    expected_members = {source_by_parent[str(key)] for key in oracle["core_parent_keys"]}
    context = source_by_parent[str(oracle["context_parent_key"])]
    expected_members.add(context)
    clusters = (
        report.get("cluster_map", {}).get("clusters", [])
        if isinstance(report.get("cluster_map"), Mapping)
        else []
    )
    cluster_rows = [
        (row, {str(value) for value in row.get("source_ids", []) or []})
        for row in clusters
        if isinstance(row, Mapping)
    ]
    expected_group_clusters = [
        (row, members)
        for row, members in cluster_rows
        if len(members & expected_members) >= 2
    ]
    covered_members = set().union(
        *(members & expected_members for _row, members in expected_group_clusters)
    )
    if not expected_group_clusters:
        errors.append("strategic8_expected_cluster_missing_or_duplicated")

    cluster_roles: list[tuple[set[str], set[str], bool]] = []
    for row, members in expected_group_clusters:
        supplied_roles = row.get("source_roles", [])
        roles = (
            {str(key): str(value).casefold() for key, value in supplied_roles.items()}
            if isinstance(supplied_roles, Mapping)
            else {
                str(row.get("source_id") or ""): str(
                    row.get("role") or row.get("proposed_role") or ""
                ).casefold()
                for row in supplied_roles or []
                if isinstance(row, Mapping) and row.get("source_id")
            }
        )
        core = {source_id for source_id in members if roles.get(source_id) == "core"}
        roles_valid = not (
            set(roles) != members
            or len(core) < 2
            or any(role not in {"core", "context", "bridge"} for role in roles.values())
        )
        if not roles_valid:
            errors.append("strategic8_final_cluster_roles_incorrect")
        cluster_roles.append((members, core, roles_valid))

    registry = base.read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    accepted_pairs = {
        pair
        for row in registry.get("relations", []) or []
        if isinstance(row, Mapping)
        and row.get("active", True)
        and str(row.get("decision_status") or row.get("status") or "")
        == "accepted"
        and (pair := _pair(row)) is not None
    }
    evaluated_pairs = {
        pair
        for row in registry.get("current_pair_decisions", []) or []
        if isinstance(row, Mapping) and (pair := _pair(row)) is not None
    }
    required_pairs = set(combinations(sorted(expected_members), 2))
    if not required_pairs.issubset(evaluated_pairs):
        errors.append("strategic8_required_pairs_not_all_evaluated")

    def connected(nodes: set[str]) -> bool:
        if len(nodes) < 2:
            return False
        reached = {next(iter(nodes))}
        while True:
            expanded = reached | {
                right if left in reached else left
                for left, right in accepted_pairs
                if left in nodes and right in nodes and (left in reached or right in reached)
            }
            if expanded == reached:
                return reached == nodes
            reached = expanded

    # Required atomic-note connectivity is independent of optional cluster boundaries.
    if not connected(expected_members):
        errors.append("strategic8_core_not_connected_by_accepted_edges")
    for members, core, roles_valid in cluster_roles:
        if not roles_valid:
            continue
        if not connected(core):
            errors.append("strategic8_core_not_connected_by_accepted_edges")
        if any(
            not any(
                tuple(sorted((context_source, source_id))) in accepted_pairs
                for source_id in core
            )
            for context_source in members - core
        ):
            errors.append("strategic8_contextual_relationship_missing")
    core_counts = [len(core) for _members, core, valid in cluster_roles if valid]
    return sorted(set(errors)), {
        "strategic8_semantic_oracle_sha256": str(oracle["sha256"]),
        "strategic8_role_policy": "probabilistic_cluster_boundaries_connected_notes_v6",
        "strategic8_actual_core_count": (
            core_counts[0] if len(core_counts) == 1 else None
        ),
        "strategic8_expected_group_cluster_count": len(expected_group_clusters),
        "strategic8_expected_group_core_counts": core_counts,
        "strategic8_expected_member_coverage_count": len(covered_members),
        "strategic8_required_pair_count": len(required_pairs),
        "strategic8_evaluated_required_pair_count": len(
            required_pairs & evaluated_pairs
        ),
        "strategic8_expected_member_count": len(expected_members),
    }


def freeze_routes(
    *,
    strategic40_manifest_path: Path,
    strategic40_manifest_sha256: str,
    strategic8_manifest_path: Path,
    strategic8_manifest_sha256: str,
    output_path: Path,
    repository_probe: Any = base._repository_state,
) -> dict[str, Any]:
    """Freeze the final-head PDF routing policy without starting a provider."""
    base._verify_runtime_import_root()
    output = base._private(output_path, label="PDF route oracle")
    if output.name != "PRIVATE_PDF_ROUTE_ORACLE.json":
        raise ValueError("route oracle output must be PRIVATE_PDF_ROUTE_ORACLE.json")
    if output.exists():
        raise ValueError("route oracle output already exists")
    preflight: list[dict[str, Any]] = []
    for path, digest in (
        (strategic40_manifest_path, strategic40_manifest_sha256),
        (strategic8_manifest_path, strategic8_manifest_sha256),
    ):
        candidate = base._private(path, label="manifest")
        if base.sha256_file(candidate) != digest:
            raise ValueError("manifest SHA-256 mismatch")
        try:
            preflight.append(
                base._mapping(
                    json.loads(candidate.read_text(encoding="utf-8")),
                    label="manifest",
                )
            )
        except json.JSONDecodeError as exc:
            raise ValueError("manifest must be valid JSON") from exc
    code_commit = str(preflight[0].get("code_commit") or "")
    if code_commit != preflight[1].get("code_commit"):
        raise ValueError("route-freeze manifests use different code commits")
    protected_roots = [
        base._private(Path(str(manifest.get("workspace") or "")), label="workspace")
        for manifest in preflight
    ]
    custody_manifests: list[tuple[Path, dict[str, Any]]] = []
    for manifest, count in zip(preflight, (40, 8), strict=True):
        custody_path = base._private(
            Path(str(manifest.get("source_custody_manifest") or "")),
            label="source custody manifest",
        )
        custody = _private_json(
            custody_path,
            _STRATEGIC_CUSTODY_MANIFEST_SHA256[count],
            label="source custody manifest",
        )
        custody_manifests.append((custody_path, custody))
        protected_roots.append(custody_path.parent)
    protected_roots.append(
        _custody_origin_root(*custody_manifests[0])
    )
    if any(base._inside(output, root) for root in protected_roots):
        raise ValueError(
            "route oracle output must be outside live and protected custody workspaces"
        )
    base._verify_repository(code_commit, repository_probe)
    routes40: list[dict[str, Any]] = []
    routes8: list[dict[str, Any]] = []
    with base.deny_codex_attempts():
        manifest40, settings40 = _manifest_settings(
            strategic40_manifest_path,
            strategic40_manifest_sha256,
            provider_free_pdf_routes=routes40,
            compute_pdf_routes=True,
        )
        manifest8, settings8 = _manifest_settings(
            strategic8_manifest_path,
            strategic8_manifest_sha256,
            provider_free_pdf_routes=routes8,
            compute_pdf_routes=True,
        )
    if settings40.case_count != 40 or settings8.case_count != 8:
        raise ValueError("route freezing requires strategic40 and strategic8 manifests")
    if code_commit != manifest8.get("code_commit"):
        raise ValueError("route-freeze manifests use different code commits")
    sorted40 = sorted(routes40, key=lambda row: str(row["case_id"]))
    sorted8 = sorted(routes8, key=lambda row: str(row["case_id"]))
    by_case40 = {str(row["case_id"]): row for row in sorted40}
    if (
        len(sorted40) != 9
        or len(sorted8) != 2
        or sorted8 != [by_case40.get(str(row["case_id"])) for row in sorted8]
    ):
        raise ValueError("strategic8 PDF routes differ from the strategic40 subset")
    workspace40 = base._private(
        Path(str(manifest40.get("workspace") or "")), label="workspace"
    )
    oracle = {
        "schema_version": "1",
        "kind": "v030_private_pdf_route_oracle",
        "code_commit": code_commit,
        "custody_manifest_sha256": {
            "strategic8": _STRATEGIC_CUSTODY_MANIFEST_SHA256[8],
            "strategic40": _STRATEGIC_CUSTODY_MANIFEST_SHA256[40],
        },
        "source_template_manifest_sha256": _STRATEGIC_TEMPLATE_MANIFEST_SHA256,
        "request_identity": _pdf_route_request_identity(
            base._request(manifest40, workspace40, settings40)
        ),
        "routes": sorted40,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    base._write_attempt_ledger(output, oracle)
    return {
        "status": "frozen",
        "provider_calls": 0,
        "route_count": len(sorted40),
        "strategic8_route_count": len(sorted8),
        "code_commit": code_commit,
        "route_oracle": str(output),
        "route_oracle_sha256": base.sha256_file(output),
    }


def _manifest_settings(
    manifest_path: Path,
    manifest_sha256: str,
    *,
    provider_free_pdf_routes: list[dict[str, Any]] | None = None,
    compute_pdf_routes: bool = False,
) -> tuple[dict[str, Any], base.GateSettings]:
    path = base._private(manifest_path, label="manifest")
    if base.sha256_file(path) != manifest_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("manifest must be valid JSON") from exc
    manifest = base._mapping(value, label="manifest")
    gate = base._mapping(manifest.get("gate"), label="manifest gate")
    if set(gate) != _GATE_FIELDS:
        raise ValueError("E2E manifest gate fields are incomplete or unknown")
    kind = str(gate.get("kind") or "")
    if gate.get("schema_version") != "1" or kind not in {
        "raw_e2e",
        "graph_e2e",
    }:
        raise ValueError("E2E manifest gate identity is invalid")
    settings = base.GateSettings(
        kind=kind,
        stage=str(gate.get("stage") or ""),
        case_count=gate.get("case_count"),
        source_attempt_limit=gate.get("source_attempt_limit"),
        relationship_attempt_limit=gate.get("relationship_attempt_limit"),
        total_attempt_limit=gate.get("total_attempt_limit"),
        document_attempt_limit=gate.get("document_attempt_limit"),
        stage_deadline_seconds=gate.get("stage_deadline_seconds"),
        clusters_enabled=gate.get("cluster_generation_enabled"),
        allow_html=kind == "raw_e2e",
        allow_metadata_only=kind == "raw_e2e",
        require_private_expectations=False,
        require_direct_image_route=False,
        require_direct_pdf_route=False,
        report_directory="codex-e2e",
        attempt_ledger_name=".v030-codex-e2e-attempt-ledger.json",
        attempt_lock_name=".v030-codex-e2e-attempt-ledger.lock",
    )
    if kind == "raw_e2e" and settings.case_count > 40:
        raise ValueError("raw E2E gate supports at most 40 cases")
    if kind == "graph_e2e" and (
        settings.case_count != _GRAPH500_CASE_COUNT
        or settings.stage != _GRAPH500_STAGE
        or settings.source_attempt_limit != 0
        or settings.relationship_attempt_limit != _GRAPH500_RELATIONSHIP_LIMIT
        or settings.total_attempt_limit != _GRAPH500_RELATIONSHIP_LIMIT
        or settings.document_attempt_limit != 1
        or settings.stage_deadline_seconds != _GRAPH500_DEADLINE_SECONDS
        or not settings.clusters_enabled
        or manifest.get("selection_manifest_sha256")
        != _GRAPH500_MANIFEST_SHA256
    ):
        raise ValueError("graph E2E controls do not match the frozen 500-work gate")
    if gate != settings.manifest_binding():
        raise ValueError("E2E manifest gate controls are not canonical")
    if kind == "raw_e2e":
        workspace = base._private(
            Path(str(manifest.get("workspace") or "")), label="workspace"
        )
        if (
            base.sha256_file(workspace / "auto-zettelkasten.yml")
            != _RAW_CONFIG_SHA256
            or not _workspace_manifest_is_bound(workspace)
        ):
            raise ValueError("raw E2E workspace identity is not frozen")
        routes = _validate_strategic_custody(
            path,
            manifest_sha256,
            manifest,
            settings,
            compute_pdf_routes=compute_pdf_routes,
        )
        if provider_free_pdf_routes is not None:
            provider_free_pdf_routes.extend(routes)
    return manifest, settings


def _workspace_file(workspace: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is required")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{label} path must be relative")
    path = (workspace / relative).resolve()
    if not base._inside(path, workspace) or not path.is_file():
        raise ValueError(f"{label} file is missing or outside the workspace")
    return path


def _graph_inputs(
    manifest: Mapping[str, Any],
    settings: base.GateSettings,
    *,
    require_frozen_notes: bool,
    manifest_path: Path,
    authorization_path: Path | None = None,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    workspace = base._private(Path(str(manifest.get("workspace") or "")), label="workspace")
    if not workspace.is_dir():
        raise ValueError("graph E2E workspace does not exist")
    config_path = _workspace_file(
        workspace, "auto-zettelkasten.yml", label="workspace configuration"
    )
    if base.sha256_file(config_path) != _GRAPH500_CONFIG_SHA256:
        raise ValueError("graph E2E workspace configuration is not frozen")
    workspace_manifest = _workspace_file(
        workspace,
        "11_state/workspace_manifest.yml",
        label="workspace manifest",
    )
    if base.sha256_file(workspace_manifest) != _GRAPH500_WORKSPACE_MANIFEST_SHA256:
        raise ValueError("graph E2E workspace manifest is not frozen")
    selection_value = manifest.get("selection_manifest")
    if selection_value != "11_state/harness_bakeoff_manifest.yml":
        raise ValueError("graph E2E selection manifest path is not canonical")
    selection_path = _workspace_file(
        workspace, selection_value, label="selection manifest"
    )
    expected_file_hash = str(manifest.get("selection_manifest_file_sha256") or "")
    if (
        not base._SHA256.fullmatch(expected_file_hash)
        or base.sha256_file(selection_path) != expected_file_hash
    ):
        raise ValueError("graph E2E selection manifest file SHA-256 mismatch")
    selection = base._mapping(
        base.read_yaml(selection_path, {}) or {}, label="selection manifest"
    )
    selection_identity = dict(selection)
    claimed_identity = str(selection_identity.pop("manifest_sha256", ""))
    calculated_identity = base.sha256_text(
        json.dumps(selection_identity, sort_keys=True, ensure_ascii=False)
    )
    if claimed_identity != _GRAPH500_MANIFEST_SHA256 or calculated_identity != claimed_identity:
        raise ValueError("graph E2E frozen selection identity mismatch")
    if (
        selection.get("schema_version") != "1"
        or selection.get("status") != "frozen_provider_neutral_slice"
        or selection.get("never_production_prompt_input") is not True
        or int(selection.get("source_count", -1)) != settings.case_count
    ):
        raise ValueError("graph E2E selection manifest is not the frozen sample")
    sources = [
        base._mapping(row, label="selection source")
        for row in selection.get("sources", []) or []
    ]
    if len(sources) != settings.case_count:
        raise ValueError("graph E2E selection source count mismatch")
    source_ids: set[str] = set()
    note_ids: set[str] = set()
    packets: set[str] = set()
    strata: set[str] = set()
    baseline_paths = {
        "auto-zettelkasten.yml",
        "11_state/workspace_manifest.yml",
        selection_value,
        "01_custody/zotero/collection_snapshot.yml",
        "02_source_memory/indexes/literature_positions.yml",
        "02_source_memory/indexes/missing_sources.yml",
    }
    for row in sources:
        source_id = str(row.get("source_id") or "")
        note_id = str(row.get("note_id") or "")
        if not source_id or source_id in source_ids or not note_id or note_id in note_ids:
            raise ValueError("graph E2E source and note IDs must be unique")
        source_ids.add(source_id)
        note_ids.add(note_id)
        packets.add(str(row.get("deepest_leaf_packet_key") or ""))
        strata.add(str(row.get("primary_stratum_id") or ""))
        note = _workspace_file(workspace, row.get("note_path"), label="source note")
        profile = _workspace_file(
            workspace, row.get("profile_path"), label="source profile"
        )
        baseline_paths.update(
            {
                str(note.relative_to(workspace)),
                str(profile.relative_to(workspace)),
                f"11_state/note_metadata/{note_id}.yml",
            }
        )
        if require_frozen_notes and base.sha256_file(note) != str(
            row.get("origin_note_sha256") or ""
        ):
            raise ValueError("graph E2E frozen note SHA-256 mismatch")
        expected_semantic_hash = str(row.get("semantic_note_sha256") or "")
        if (
            not base._SHA256.fullmatch(expected_semantic_hash)
            or semantic_note_hash(note.read_text(encoding="utf-8"))
            != expected_semantic_hash
        ):
            raise ValueError("graph E2E semantic note SHA-256 mismatch")
        if base.sha256_file(profile) != str(row.get("profile_sha256") or ""):
            raise ValueError("graph E2E profile SHA-256 mismatch")
        if row.get("bundle_path"):
            bundle = _workspace_file(
                workspace, row.get("bundle_path"), label="source bundle"
            )
            baseline_paths.add(str(bundle.relative_to(workspace)))
            if base.sha256_file(bundle) != str(row.get("bundle_sha256") or ""):
                raise ValueError("graph E2E bundle SHA-256 mismatch")
        elif row.get("bundle_sha256"):
            raise ValueError("graph E2E bundle path/hash binding is incomplete")
        frontmatter = read_note(note)["frontmatter"]
        if (
            str(frontmatter.get("source_id") or "") != source_id
            or str(frontmatter.get("note_id") or "") != note_id
        ):
            raise ValueError("graph E2E note identity mismatch")
    sampling = base._mapping(selection.get("sampling"), label="selection sampling")
    selected_packets = {
        str(value) for value in sampling.get("selected_packet_keys", []) or []
    }
    if (
        len(strata) != 20
        or "" in strata
        or int(sampling.get("selected_packet_count", -1)) != 51
        or packets != selected_packets
        or "" in packets
    ):
        raise ValueError("graph E2E stratification or packet coverage mismatch")
    if require_frozen_notes:
        for relative in baseline_paths:
            _workspace_file(workspace, relative, label="graph baseline")
        if (
            len(baseline_paths) != _GRAPH500_BASELINE_FILE_COUNT
            or _inventory_sha256(workspace, baseline_paths)
            != _GRAPH500_BASELINE_SHA256
        ):
            raise ValueError("graph E2E frozen baseline identity mismatch")
        _assert_exact_fresh_inventory(
            workspace,
            baseline_paths,
            _campaign_allowlist(
                workspace, manifest, manifest_path, authorization_path
            ),
        )
    source_set = {
        "source_set_id": f"source-set-{_GRAPH500_MANIFEST_SHA256[:20]}",
        "source_ids": sorted(source_ids),
        "note_ids": sorted(note_ids),
    }
    return workspace, sources, source_set


def _graph_policy(settings: base.GateSettings) -> LiteratureMappingPolicy:
    return LiteratureMappingPolicy(
        synthesis_enabled=True,
        cluster_generation_enabled=True,
        external_discovery="disabled",
        max_profile_calls=0,
        max_synthesis_calls=settings.relationship_attempt_limit,
        literature_deadline_seconds=float(settings.stage_deadline_seconds),
    )


def _pair(row: Mapping[str, Any]) -> tuple[str, str] | None:
    nested = row.get("relationship")
    value = nested if isinstance(nested, Mapping) else row
    source_ids = [str(item) for item in value.get("source_ids", []) or [] if str(item)]
    if len(source_ids) == 2:
        return tuple(sorted(source_ids))
    left = str(value.get("source_id") or value.get("left_source_id") or "")
    right = str(
        value.get("target_source_id") or value.get("right_source_id") or ""
    )
    if not left or not right or left == right:
        return None
    return tuple(sorted((left, right)))


def _graph_acceptance(
    workspace: Path,
    run_id: str,
    sources: Sequence[Mapping[str, Any]],
    settings: base.GateSettings,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    source_ids = {str(row["source_id"]) for row in sources}
    packet_by_source = {
        str(row["source_id"]): str(row["deepest_leaf_packet_key"])
        for row in sources
    }
    receipt = base.read_yaml(
        workspace / "11_state" / "runs" / run_id / "semantic_build_receipt.yml",
        {},
    ) or {}
    if not isinstance(receipt, Mapping):
        receipt = {}
    summary = (
        dict(receipt.get("summary", {}) or {})
        if isinstance(receipt.get("summary"), Mapping)
        else {}
    )
    literature = (
        dict(summary.get("literature_map", {}) or {})
        if isinstance(summary.get("literature_map"), Mapping)
        else {}
    )
    if (
        receipt.get("receipt_schema_version") != "2"
        or receipt.get("status") != "built"
        or receipt.get("semantic_replayable") is not True
        or int(summary.get("source_count", -1)) != settings.case_count
        or literature.get("status") != "completed"
        or str(literature.get("partial_reason") or "")
        or int(literature.get("profile_count", -1)) != settings.case_count
        or int(literature.get("profile_valid_count", -1))
        + int(literature.get("profile_excluded_count", -1))
        != settings.case_count
        or int(literature.get("synthesis_failure_count", -1)) != 0
    ):
        errors.append("graph_receipt_incomplete")

    profiles = base._profile_source_ids(workspace)
    if profiles != source_ids:
        errors.append("graph_profile_source_set_mismatch")

    registry = base.read_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml", {}
    ) or {}
    if not isinstance(registry, Mapping):
        registry = {}
        errors.append("typed_relationship_registry_missing")
    relationship_rows = [
        dict(row)
        for field in ("relations", "links", "pair_decisions")
        for row in registry.get(field, []) or []
        if isinstance(row, Mapping)
        and str(row.get("source_kind") or "source") == "source"
        and str(row.get("target_kind") or "source") == "source"
    ]
    accepted_pairs = {
        pair
        for row in relationship_rows
        if row.get("active", True)
        and str(row.get("decision_status") or row.get("status") or "") == "accepted"
        and (pair := _pair(row)) is not None
    }
    decision_rows = [
        dict(row)
        for row in registry.get("current_pair_decisions", []) or []
        if isinstance(row, Mapping)
    ]
    if not decision_rows:
        decision_rows = [
            row
            for row in relationship_rows
            if str(row.get("decision_status") or row.get("status") or "")
            == "no_relationship"
        ]
    negative_pairs = {
        pair
        for row in decision_rows
        if str(row.get("decision_status") or row.get("status") or "")
        == "no_relationship"
        and (pair := _pair(row)) is not None
    }
    all_pairs = accepted_pairs | negative_pairs
    if not all_pairs or any(not set(pair).issubset(source_ids) for pair in all_pairs):
        errors.append("graph_relationship_endpoint_accounting_failed")
    if not accepted_pairs:
        errors.append("graph_accepted_relationships_missing")
    if not negative_pairs:
        errors.append("graph_negative_memory_missing")
    if not any(
        packet_by_source[left] == packet_by_source[right]
        for left, right in accepted_pairs
    ):
        errors.append("graph_within_packet_relationship_missing")
    if not any(
        packet_by_source[left] != packet_by_source[right]
        for left, right in accepted_pairs
    ):
        errors.append("graph_cross_packet_relationship_missing")

    note_ids = {str(row["source_id"]): str(row["note_id"]) for row in sources}
    projections: dict[str, set[str]] = {}
    for row in sources:
        note = _workspace_file(workspace, row.get("note_path"), label="source note")
        related = read_note(note)["frontmatter"].get("related_notes", []) or []
        projections[str(row["source_id"])] = {
            str(value.get("note_id") or "")
            for value in related
            if isinstance(value, Mapping) and value.get("note_id")
        }
    if any(
        note_ids[right] not in projections[left]
        or note_ids[left] not in projections[right]
        for left, right in accepted_pairs
    ):
        errors.append("graph_relationship_projection_incomplete")
    relationship_errors, _ = base._relationship_errors(workspace, source_ids)
    errors.extend(relationship_errors)

    cluster_registry = base.read_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml", {}
    ) or {}
    if not isinstance(cluster_registry, Mapping):
        cluster_registry = {}
    clusters = [
        dict(row)
        for row in cluster_registry.get("clusters", []) or []
        if isinstance(row, Mapping)
    ]
    member_ids = {
        str(source_id)
        for cluster in clusters
        for source_id in cluster.get("source_ids", []) or []
        if str(source_id)
    }
    unclustered_ids = {
        str(row.get("source_id") if isinstance(row, Mapping) else row)
        for row in cluster_registry.get("unclustered_sources", []) or []
        if str(row.get("source_id") if isinstance(row, Mapping) else row)
    }
    valid_count = int(literature.get("profile_valid_count", -1))
    excluded_count = int(literature.get("profile_excluded_count", -1))
    if (
        not clusters
        or int(summary.get("cluster_count", -1)) != len(clusters)
        or not (member_ids | unclustered_ids).issubset(source_ids)
        or member_ids & unclustered_ids
        or len(member_ids | unclustered_ids) != valid_count
        or valid_count + excluded_count != settings.case_count
    ):
        errors.append("graph_cluster_disposition_accounting_failed")
    if (
        cluster_registry.get("pending_revisions")
        or cluster_registry.get("refresh_pending")
        or any(
        cluster.get("refresh_pending") is True for cluster in clusters
        )
    ):
        errors.append("graph_cluster_refresh_pending")
    if (
        cluster_registry.get("missing_member_ids")
        or cluster_registry.get("quality_errors")
        or cluster_registry.get("parked_for_review")
        or any(
            cluster.get("missing_member_ids")
            or cluster.get("quality_errors")
            or cluster.get("parked_for_review")
            for cluster in clusters
        )
    ):
        errors.append("graph_cluster_membership_quality_incomplete")
    synthesis_payload = base.read_yaml(
        workspace / "03_literature_synthesis" / "cluster_syntheses.yml", {}
    ) or {}
    synthesis_values = (
        synthesis_payload.get("syntheses", {})
        if isinstance(synthesis_payload, Mapping)
        else {}
    )
    if isinstance(synthesis_values, Mapping):
        syntheses = [
            {"cluster_id": str(cluster_id), **dict(value)}
            for cluster_id, value in synthesis_values.items()
            if isinstance(value, Mapping)
        ]
    elif isinstance(synthesis_values, list):
        syntheses = [
            dict(value) for value in synthesis_values if isinstance(value, Mapping)
        ]
    else:
        syntheses = []
    cluster_ids = {str(cluster.get("cluster_id") or "") for cluster in clusters}
    synthesis_ids = {
        str(synthesis.get("cluster_id") or "") for synthesis in syntheses
    }
    if (
        cluster_ids != synthesis_ids
        or (
            isinstance(synthesis_payload, Mapping)
            and any(
                synthesis_payload.get(field)
                for field in (
                    "refresh_pending",
                    "missing_member_ids",
                    "quality_errors",
                    "parked_for_review",
                )
            )
        )
        or any(
            synthesis.get("refresh_pending")
            or synthesis.get("missing_member_ids")
            or synthesis.get("quality_errors")
            or synthesis.get("parked_for_review")
            for synthesis in syntheses
        )
    ):
        errors.append("graph_cluster_synthesis_quality_incomplete")
    synthesized_count = int(literature.get("synthesized_cluster_count", -1))
    if synthesized_count != len(clusters):
        errors.append("graph_cluster_synthesis_incomplete")

    source, relationship = base._attempts(workspace, run_id)
    if source["count"] or source["reported_count"] or source["reservation_count"]:
        errors.append("graph_source_provider_call_detected")
    if not (
        0 < relationship["count"] <= settings.relationship_attempt_limit
        and relationship["count"] == relationship["reported_count"]
        and relationship["count"]
        == int(literature.get("synthesis_call_count", -1))
    ):
        errors.append("graph_relationship_attempt_ledger_disagreement")
    contracts: set[str] = set()
    for row in relationship["rows"]:
        if str(row.get("status") or "") in {"failed", "interrupted"} and not (
            base._attempt_pause_reason(row)
        ):
            errors.append("graph_unfinished_relationship_attempt")
    for row in base._latest_attempt_rows(relationship["rows"]):
        if base._attempt_pause_reason(row):
            errors.append("graph_unfinished_relationship_attempt")
            continue
        if str(row.get("status") or "") in {"failed", "interrupted"}:
            continue
        if str(row.get("status") or "") != "completed":
            errors.append("graph_unfinished_relationship_attempt")
            continue
        completion_error = base._completion_error(
            row, source=False, settings=settings
        )
        if completion_error:
            errors.append(completion_error)
        completion = row.get("provider_completion")
        if isinstance(completion, Mapping):
            contracts.add(str(completion.get("contract_id") or ""))
    if not contracts & base._RELATIONSHIP_CONTRACTS or not contracts & base._CLUSTER_CONTRACTS:
        errors.append("graph_required_contract_roles_missing")
    relationship_state = base.read_yaml(
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml",
        {},
    ) or {}
    if (
        not isinstance(relationship_state, Mapping)
        or relationship_state.get("relationship_stage_complete") is not True
        or relationship_state.get("relationship_discovery_status") != "complete"
        or relationship_state.get("relationship_discovery_incomplete_jobs")
    ):
        errors.append("graph_relationship_completeness_accounting_failed")
    return sorted(set(errors)), {
        "source_attempt_count": source["count"],
        "relationship_attempt_count": relationship["count"],
        "total_attempt_count": source["count"] + relationship["count"],
        "source_count": len(source_ids),
        "profile_valid_count": valid_count,
        "profile_excluded_count": excluded_count,
        "accepted_relationship_count": len(accepted_pairs),
        "negative_relationship_count": len(negative_pairs),
        "cluster_count": len(clusters),
        "clustered_source_count": len(member_ids),
        "unclustered_source_count": len(unclustered_ids),
    }


def _graph_gate(
    *,
    mode: str,
    manifest: Mapping[str, Any],
    settings: base.GateSettings,
    manifest_path: Path,
    manifest_sha256: str,
    authorization_path: Path | None,
    authorization_sha256: str,
    execute: bool,
    graph_runner: Any,
    repository_probe: Any,
    attempt_guard_factory: Any,
) -> tuple[Path, dict[str, Any]]:
    base._verify_runtime_import_root()
    workspace, sources, source_set = _graph_inputs(
        manifest,
        settings,
        require_frozen_notes=mode in {"prepare", "run"},
        manifest_path=manifest_path,
        authorization_path=authorization_path,
    )
    if manifest_path.resolve().parent != workspace:
        raise ValueError("graph E2E manifest must be stored at the workspace root")
    evaluation_id = str(manifest.get("evaluation_id") or "")
    run_id = str(manifest.get("run_id") or "")
    code_commit = str(manifest.get("code_commit") or "")
    question = str(manifest.get("question") or "").strip()
    if (
        manifest.get("schema_version") != "1"
        or not base._SAFE_ID.fullmatch(evaluation_id)
        or not base._SAFE_ID.fullmatch(run_id)
        or not base._GIT_COMMIT.fullmatch(code_commit)
        or question != _GRAPH500_QUESTION
    ):
        raise ValueError("graph E2E manifest identity or question is invalid")
    report_base = {
        "report_schema_version": "1",
        "evaluation_id": evaluation_id,
        "mode": mode,
        "manifest_sha256": manifest_sha256,
        "selection_manifest_sha256": _GRAPH500_MANIFEST_SHA256,
        "code_commit": code_commit,
        "run_id": run_id,
        "source_model": "none",
        "relationship_model": base.RELATIONSHIP_MODEL,
        "reasoning_effort": base.REASONING_EFFORT,
        "source_attempt_limit": settings.source_attempt_limit,
        "relationship_attempt_limit": settings.relationship_attempt_limit,
        "total_attempt_limit": settings.total_attempt_limit,
        "stage_deadline_seconds": settings.stage_deadline_seconds,
        "cluster_generation_enabled": True,
        "case_count": settings.case_count,
    }
    if mode == "prepare":
        report = {**report_base, "status": "prepared", "created_at": base.now_iso()}
        return base._write_report(
            workspace, evaluation_id, mode, report, settings
        ), report
    if mode not in {"run", "resume", "replay", "revalidate"}:
        raise ValueError("graph E2E mode is invalid")
    if not execute:
        raise PermissionError("live and replay modes require execute=True")
    base._verify_repository(code_commit, repository_probe)
    run_root = workspace / "11_state" / "runs" / run_id
    if mode == "run":
        base._assert_run_reservation_available(workspace, settings)
        forbidden_outputs = (
            workspace / "02_source_memory" / "indexes" / "typed_links.yml",
            workspace / "02_source_memory" / "indexes" / "relationship_selection_state.yml",
            workspace / "03_literature_synthesis" / "cluster_registry.yml",
        )
        if any(path.exists() for path in forbidden_outputs) or (
            run_root.exists() and any(run_root.iterdir())
        ):
            raise ValueError("graph E2E run requires a fresh disposable sample clone")
    elif not run_root.is_dir():
        raise ValueError("graph E2E run state is missing")

    ledger_identity = base._ledger_identity(manifest, manifest_sha256, settings)
    initial_source, initial_relationship = base._attempts(workspace, run_id)
    resume_reason = (
        base._resume_reservation_reason(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )
        if mode == "resume"
        else None
    )
    live_guard: Any | None = None
    if mode in {"run", "resume"}:
        supplied_path = authorization_path is not None
        supplied_sha = bool(authorization_sha256)
        if supplied_path != supplied_sha:
            raise ValueError("authorization path and SHA-256 must be supplied together")
        if not supplied_path and repository_probe is base._repository_state:
            raise ValueError("live graph E2E requires campaign authorization")
        if supplied_path:
            assert authorization_path is not None
            live_guard = attempt_guard_factory(
                authorization_path,
                authorization_sha256,
                repository_root=base._REPOSITORY_ROOT,
                stage=settings.stage,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                evaluation_id=evaluation_id,
                run_id=run_id,
                source_attempt_limit=settings.source_attempt_limit,
                relationship_attempt_limit=settings.relationship_attempt_limit,
                total_attempt_limit=settings.total_attempt_limit,
                resume_reason=resume_reason,
            )
        try:
            base._begin_attempt_reservation(
                workspace,
                ledger_identity,
                mode=mode,
                source_count=initial_source["count"],
                relationship_count=initial_relationship["count"],
                resume_reason=resume_reason,
                settings=settings,
            )
        except Exception:
            if live_guard is not None:
                live_guard.finish("failed", reason="local_reservation_failed")
            raise
    elif mode == "replay":
        base._verify_accepted_reservation(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )
    else:
        base._verify_failed_reservation(
            workspace,
            ledger_identity,
            source_count=initial_source["count"],
            relationship_count=initial_relationship["count"],
            settings=settings,
        )

    before: dict[str, tuple[str, int, int]] | None = None
    prior_acceptance: dict[str, Any] | None = None
    if mode in base._PROVIDER_FREE_MODES:
        prior_errors, prior_acceptance = _graph_acceptance(
            workspace, run_id, sources, settings
        )
        if mode == "replay" and prior_errors:
            raise ValueError("graph replay requires a previously accepted gate")
        before = base._gate_snapshot(workspace)

    provider_free = mode in base._PROVIDER_FREE_MODES
    reasoner = (
        CodexReader(
            base.RELATIONSHIP_MODEL,
            allow_cloud=True,
            reasoning_effort=base.REASONING_EFFORT,
            attempt_guard=live_guard,
        )
        if live_guard is not None
        else None
    )
    context = (
        base.deny_codex_attempts()
        if provider_free
        else live_guard.activate()
        if live_guard is not None
        else nullcontext()
    )
    try:
        with context, base._stage_deadline(settings):
            if provider_free:
                reasoner = base._ReplayCodexReader(
                    base.RELATIONSHIP_MODEL,
                    allow_cloud=True,
                    reasoning_effort=base.REASONING_EFFORT,
                )
            graph_runner(
                workspace,
                run_id=run_id,
                source_set=source_set,
                question=question,
                provider="codex",
                model=base.RELATIONSHIP_MODEL,
                reasoning_effort=base.REASONING_EFFORT,
                allow_cloud=True,
                provider_concurrency="auto",
                literature_policy=_graph_policy(settings),
                navigation_policy=NavigationPolicy(),
                reasoner=reasoner,
                resume=mode != "run",
                retry_terminal_failures=False,
            )
    except (
        base.ProviderQuotaExhausted,
        base.ProviderTimeout,
        base.ProviderInterrupted,
        KeyboardInterrupt,
    ) as exc:
        pause_reason = (
            "quota"
            if isinstance(exc, base.ProviderQuotaExhausted)
            else "timeout"
            if isinstance(exc, base.ProviderTimeout)
            else "interruption"
        )
        source, relationship = base._attempts(workspace, run_id)
        ceiling_errors = base._count_ceiling_errors(
            source["count"], relationship["count"], settings
        )
        status = "failed" if provider_free or ceiling_errors else "paused"
        if mode in {"run", "resume"}:
            base._finish_attempt_reservation(
                workspace,
                ledger_identity,
                state="paused" if status == "paused" else "failed",
                source_count=source["count"],
                relationship_count=relationship["count"],
                reason=pause_reason if status == "paused" else "",
                settings=settings,
            )
            if live_guard is not None:
                if status == "paused":
                    live_guard.finish("paused", reason=pause_reason)
                else:
                    live_guard.finish("failed", reason="attempt_ceiling_exceeded")
        report = {
            **report_base,
            **base._count_payload(source["count"], relationship["count"]),
            "status": status,
            "paused_by": pause_reason if status == "paused" else "",
            "validation_errors": ceiling_errors,
            "created_at": base.now_iso(),
        }
        return base._write_report(
            workspace, evaluation_id, mode, report, settings
        ), report
    except Exception as exc:
        try:
            source, relationship = base._attempts(workspace, run_id)
            source_count = source["count"]
            relationship_count = relationship["count"]
        except Exception:
            source_count = relationship_count = None
        if mode in {"run", "resume"}:
            base._finish_attempt_reservation(
                workspace,
                ledger_identity,
                state="failed",
                source_count=source_count,
                relationship_count=relationship_count,
                settings=settings,
            )
            if live_guard is not None:
                live_guard.finish("failed", reason="gate_execution_failed")
        counts = (
            base._count_payload(source_count, relationship_count)
            if source_count is not None and relationship_count is not None
            else {}
        )
        report = {
            **report_base,
            **counts,
            "status": "failed",
            "error_type": type(exc).__name__,
            "validation_errors": ["gate_execution_failed"],
            "created_at": base.now_iso(),
        }
        return base._write_report(
            workspace, evaluation_id, mode, report, settings
        ), report

    try:
        errors, acceptance = _graph_acceptance(
            workspace, run_id, sources, settings
        )
        source, relationship = base._attempts(workspace, run_id)
    except Exception as exc:
        source, relationship = base._attempts(workspace, run_id)
        if mode in {"run", "resume"}:
            base._finish_attempt_reservation(
                workspace,
                ledger_identity,
                state="failed",
                source_count=source["count"],
                relationship_count=relationship["count"],
                settings=settings,
            )
            if live_guard is not None:
                live_guard.finish("failed", reason="gate_validation_failed")
        report = {
            **report_base,
            **base._count_payload(source["count"], relationship["count"]),
            "status": "failed",
            "error_type": type(exc).__name__,
            "validation_errors": ["gate_validation_failed"],
            "created_at": base.now_iso(),
        }
        return base._write_report(
            workspace, evaluation_id, mode, report, settings
        ), report
    paused_by = base._pause_reason({}, [*source["rows"], *relationship["rows"]])
    status = "passed" if not errors else "paused" if paused_by else "failed"
    if provider_free and status == "paused":
        status = "failed"
    report = {
        **report_base,
        **acceptance,
        "status": status,
        "paused_by": paused_by if status == "paused" else "",
        "validation_errors": errors,
        "created_at": base.now_iso(),
    }
    if provider_free:
        assert before is not None and prior_acceptance is not None
        after = base._gate_snapshot(workspace)
        changed = sorted(set(before) ^ set(after)) + sorted(
            path for path in set(before) & set(after) if before[path] != after[path]
        )
        if changed or acceptance != prior_acceptance:
            report["status"] = "failed"
            report["validation_errors"] = sorted(
                {
                    *report["validation_errors"],
                    "semantic_replay_changed"
                    if changed
                    else "acceptance_replay_changed",
                }
            )
        report.update(
            semantic_file_count=len(after),
            semantic_snapshot_sha256=base._snapshot_digest(after),
            semantic_changed_paths=changed,
            exact_zero_call_replay=(
                report["status"] == "passed"
                and not changed
                and acceptance == prior_acceptance
            ),
        )
    else:
        reservation_state = {"passed": "accepted", "paused": "paused"}.get(
            str(status), "failed"
        )
        base._finish_attempt_reservation(
            workspace,
            ledger_identity,
            state=reservation_state,
            source_count=acceptance["source_attempt_count"],
            relationship_count=acceptance["relationship_attempt_count"],
            reason=paused_by if reservation_state == "paused" else "",
            settings=settings,
        )
        if live_guard is not None:
            if status == "passed":
                live_guard.finish("passed")
            elif status == "paused":
                live_guard.finish("paused", reason=paused_by)
            else:
                live_guard.finish("failed", reason="acceptance_failed")
    return base._write_report(workspace, evaluation_id, mode, report, settings), report


def run_gate(
    *,
    mode: str,
    manifest_path: Path,
    manifest_sha256: str,
    authorization_path: Path | None = None,
    authorization_sha256: str = "",
    execute: bool = False,
    map_runner: Any = base.run_map,
    graph_runner: Any = build_map,
    repository_probe: Any = base._repository_state,
    attempt_guard_factory: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    base._verify_runtime_import_root()
    provider_free_pdf_routes: list[dict[str, Any]] = []
    manifest, settings = _manifest_settings(
        manifest_path,
        manifest_sha256,
        provider_free_pdf_routes=provider_free_pdf_routes,
    )
    if (
        settings.kind == "raw_e2e"
        and settings.case_count not in _STRATEGIC_CONTROLS
        and mode in {"run", "resume"}
        and map_runner is base.run_map
    ):
        raise ValueError("live raw E2E supports only frozen strategic8/40 gates")
    if settings.kind == "raw_e2e" and mode == "run":
        if settings.case_count == 40 and any(
            manifest["gate"][key] != value
            for key, value in _STRATEGIC_CONTROLS[40].items()
        ):
            raise ValueError("fresh strategic40 requires the current immutable gate controls")
        workspace = base._private(
            Path(str(manifest.get("workspace") or "")), label="workspace"
        )
        _assert_raw_fresh_workspace(
            workspace, manifest, manifest_path, authorization_path
        )
    guard_factory = attempt_guard_factory or CodexCampaignGuard.start
    if settings.kind == "graph_e2e":
        return _graph_gate(
            mode=mode,
            manifest=manifest,
            settings=settings,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            authorization_path=authorization_path,
            authorization_sha256=authorization_sha256,
            execute=execute,
            graph_runner=graph_runner,
            repository_probe=repository_probe,
            attempt_guard_factory=guard_factory,
        )
    strategic8_oracle = manifest.get("_strategic8_semantic_oracle")

    def strategic8_acceptance(
        workspace: Path,
        _run_id: str,
        cases: Sequence[Mapping[str, Any]],
        report: Mapping[str, Any],
    ) -> tuple[list[str], dict[str, Any]]:
        assert isinstance(strategic8_oracle, Mapping)
        return _strategic8_oracle_acceptance(
            workspace, cases, report, strategic8_oracle
        )

    path, report = base.run_gate(
        mode=mode,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        authorization_path=authorization_path,
        authorization_sha256=authorization_sha256,
        execute=execute,
        map_runner=map_runner,
        repository_probe=repository_probe,
        attempt_guard_factory=guard_factory,
        settings=settings,
        acceptance_hook=(
            strategic8_acceptance
            if isinstance(strategic8_oracle, Mapping)
            else None
        ),
    )
    if mode == "prepare" and provider_free_pdf_routes:
        report = {**report, "provider_free_pdf_routes": provider_free_pdf_routes}
        path = base._write_report(
            Path(str(manifest["workspace"])),
            str(manifest["evaluation_id"]),
            mode,
            report,
            settings,
        )
    return path, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "freeze-routes",
            "prepare",
            "run",
            "resume",
            "replay",
            "revalidate",
        ),
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--strategic8-manifest", type=Path)
    parser.add_argument("--strategic8-manifest-sha256", default="")
    parser.add_argument("--route-oracle-output", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--authorization-sha256", default="")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "freeze-routes":
        if (
            args.strategic8_manifest is None
            or not args.strategic8_manifest_sha256
            or args.route_oracle_output is None
        ):
            raise ValueError(
                "freeze-routes requires the strategic8 manifest, its SHA-256, "
                "and --route-oracle-output"
            )
        result = freeze_routes(
            strategic40_manifest_path=args.manifest,
            strategic40_manifest_sha256=args.manifest_sha256,
            strategic8_manifest_path=args.strategic8_manifest,
            strategic8_manifest_sha256=args.strategic8_manifest_sha256,
            output_path=args.route_oracle_output,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    path, report = run_gate(
        mode=args.mode,
        manifest_path=args.manifest,
        manifest_sha256=args.manifest_sha256,
        authorization_path=args.authorization,
        authorization_sha256=args.authorization_sha256,
        execute=args.execute,
    )
    print(json.dumps({"report": str(path), **report}, sort_keys=True, default=str))
    return 0 if report["status"] in {"prepared", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
