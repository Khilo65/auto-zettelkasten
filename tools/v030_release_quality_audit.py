#!/usr/bin/env python3
"""Prepare and score private, provider-free v0.30 release review packets."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import re
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from email.parser import BytesParser
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.notes import read_note, source_id_for_item


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SEED = "v030-autonomous-provisional-release-audit-v1"
_MODES = {"exhaustive40": 40, "stratified500": 500}
_RELATION_LIMIT = 200
_MEMBERSHIP_LIMIT = 200
_DECISION_LIMIT = 100
_Z_95 = 1.959963984540054
_NOTE_CRITERIA = (
    "identity_correct",
    "custody_link_correct",
    "status_correct",
    "metadata_only_non_pretense",
    "locators_supported",
    "unsupported_claims_absent",
    "false_quotations_absent",
    "source_grounded",
    "equal_or_better",
    "materially_worse",
    "material_error",
)
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_PRIVATE_DENYLIST_BYTES = 1_000_000
_MAX_PRIVATE_LITERALS = 10_000
_CREDENTIAL_NAMES = re.compile(
    r"(?:^|/)(?:\.codex(?:/.*)?|\.env(?:\..*)?|auth\.json|credentials[^/]*\.json|"
    r"\.pypirc|\.npmrc|\.netrc|[^/]+\.(?:pem|key|p12))$",
    re.IGNORECASE,
)
_PRIVATE_WORKSPACE_MARKER = b"Auto-Zettelkasten" + b"-test"
_SECRET_PATTERNS = {
    "private_root": re.compile(
        rb"(?:\\?/Users\\?/[A-Za-z0-9._-]+|"
        rb"\\?/home\\?/[A-Za-z0-9._-]+|"
        rb"[A-Za-z]:(?:\\{1,2})Users(?:\\{1,2})[A-Za-z0-9._-]+|"
        + re.escape(_PRIVATE_WORKSPACE_MARKER)
        + rb")"
    ),
    "private_key": re.compile(rb"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
    "openai_token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "github_fine_grained_token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "pypi_token": re.compile(rb"\bpypi-[A-Za-z0-9_-]{20,}"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "bearer_token": re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._=-]{20,}"),
    "jwt": re.compile(
        rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        rb"[A-Za-z0-9_-]{8,}\b"
    ),
}
_HISTORICAL_TEST_SENTINELS = {
    "tests/test_codex_provider.py": (
        b"Bearer " + b"synthetic-authorization-value",
        b"eyJsyntheticA." + b"eyJsyntheticB.syntheticC",
        b"sk-" + b"SYNTHETICINVALID0000",
    ),
    "tests/test_v030_codex_provider_eval.py": (
        b"Bearer " + b"synthetic-authorization-value",
        b"Bearer " + b"synthetic-isolation-secret",
    ),
    "tests/test_v030_codex_throughput_eval.py": (
        b"Bearer " + b"synthetic-isolation-secret",
    ),
}
_PRIVATE_LITERAL_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REVIEWER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_REVIEWER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}


class _ArchiveAuditError(ValueError):
    pass


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _diagnostic_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _private_yaml_path(path: Path, workspace: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix not in {".yml", ".yaml"}:
        raise ValueError(f"{label} must be a YAML path")
    if (
        _inside(resolved, workspace)
        or _inside(resolved, _REPOSITORY_ROOT)
        or _git_root(resolved) is not None
    ):
        raise ValueError(f"{label} must be outside the workspace and Git")
    return resolved


def _private_input(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve()
    if (
        expanded.is_symlink()
        or not resolved.is_file()
        or _inside(resolved, _REPOSITORY_ROOT)
        or _git_root(resolved)
    ):
        raise ValueError(f"{label} must be an existing private file outside Git")
    return resolved


def _private_literal_denylist(
    path: Path | None,
    expected_sha256: str,
) -> tuple[str, tuple[bytes, ...]]:
    if (path is None) != (not expected_sha256):
        raise ValueError("private denylist path and SHA-256 must be supplied together")
    if path is None:
        return "", ()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("private denylist SHA-256 must be lowercase hexadecimal")
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate != candidate.resolve():
        raise ValueError("private literal denylist path must be absolute and canonical")
    source = _private_input(path, label="private literal denylist")
    if source.stat().st_size > _MAX_PRIVATE_DENYLIST_BYTES:
        raise ValueError("private literal denylist exceeds its byte ceiling")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("private literal denylist SHA-256 mismatch")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("private literal denylist must be valid JSON") from exc
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    if (
        not isinstance(value, Mapping)
        or set(value) != {"private_literal_denylist_schema_version", "literals"}
        or value.get("private_literal_denylist_schema_version") != "1"
        or not isinstance(value.get("literals"), list)
        or not value["literals"]
        or len(value["literals"]) > _MAX_PRIVATE_LITERALS
        or data != canonical
    ):
        raise ValueError("private literal denylist schema or encoding is invalid")
    labels: set[str] = set()
    literals: list[bytes] = []
    normalized: set[bytes] = set()
    for raw in value["literals"]:
        if not isinstance(raw, Mapping) or set(raw) != {"label", "literal"}:
            raise ValueError("private literal denylist entry is invalid")
        label = raw.get("label")
        literal = raw.get("literal")
        if (
            not isinstance(label, str)
            or _PRIVATE_LITERAL_LABEL.fullmatch(label) is None
            or label in labels
            or not isinstance(literal, str)
            or not 8 <= len(literal) <= 4096
            or not literal.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in literal)
        ):
            raise ValueError("private literal denylist entry is invalid")
        encoded = literal.encode("ascii")
        folded = encoded.lower()
        if folded in normalized:
            raise ValueError("private literal denylist contains duplicate literals")
        labels.add(label)
        normalized.add(folded)
        literals.append(encoded)
    return expected_sha256, tuple(literals)


def _redact_private_literals(value: Any, literals: Sequence[bytes]) -> Any:
    if isinstance(value, str):
        for literal in literals:
            value = re.sub(
                re.escape(literal.decode("ascii")),
                "[REDACTED_PRIVATE_LITERAL]",
                value,
                flags=re.IGNORECASE,
            )
        return value
    if isinstance(value, Mapping):
        return {
            key: _redact_private_literals(child, literals)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_private_literals(child, literals) for child in value]
    return value


def _private_workspace(path: Path, workspace: Path) -> Path:
    resolved = path.expanduser().resolve()
    if (
        not resolved.is_dir()
        or resolved == workspace
        or _inside(resolved, workspace)
        or _inside(workspace, resolved)
        or _inside(resolved, _REPOSITORY_ROOT)
        or _git_root(resolved)
    ):
        raise ValueError(
            "baseline workspace must be a distinct private non-Git directory"
        )
    return resolved


def _custody_root(path: Path, workspace: Path) -> Path:
    resolved = path.expanduser().resolve()
    return (
        workspace if resolved == workspace else _private_workspace(resolved, workspace)
    )


def _require_outside_evidence_roots(
    paths: Sequence[Path], roots: Sequence[Path]
) -> None:
    if any(_inside(path, root) for path in paths for root in roots):
        raise ValueError(
            "packet, bindings, review, and report must be outside every evidence "
            "workspace/root"
        )


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _workspace_file(workspace: Path, relative: str, *, label: str) -> Path:
    value = Path(relative)
    path = (value if value.is_absolute() else workspace / value).resolve()
    if not _inside(path, workspace) or not path.is_file():
        raise ValueError(f"{label} must be an existing file inside the workspace")
    return path


def _stable_yaml(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    before = sha256_file(path)
    payload = _mapping(read_yaml(path, None), label=label)
    if sha256_file(path) != before:
        raise ValueError(f"{label} changed while the audit packet was prepared")
    return payload, before


def _stable_text(path: Path, *, label: str) -> tuple[str, str]:
    before = sha256_file(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if sha256_file(path) != before:
        raise ValueError(f"{label} changed while the audit packet was prepared")
    return text, before


def _artifact(path: Path, workspace: Path, digest: str) -> dict[str, str]:
    resolved = path.resolve()
    if _inside(resolved, workspace):
        return {
            "path": str(resolved.relative_to(workspace)),
            "path_scope": "workspace",
            "sha256": digest,
        }
    if _inside(resolved, _REPOSITORY_ROOT) or _git_root(resolved):
        raise ValueError("private evidence artifacts cannot come from Git")
    return {"path": str(resolved), "path_scope": "private", "sha256": digest}


def _artifact_path(workspace: Path, artifact: Mapping[str, Any]) -> Path:
    scope = str(artifact.get("path_scope") or "")
    value = str(artifact.get("path") or "")
    if scope == "workspace":
        return _workspace_file(workspace, value, label="packet artifact")
    if scope == "private":
        return _private_input(Path(value), label="packet artifact")
    raise ValueError("packet artifact path scope is invalid")


def _source_ids(row: Mapping[str, Any]) -> list[str]:
    values = (
        row.get("left_source_id"),
        row.get("right_source_id"),
        row.get("source_id"),
        row.get("target_source_id"),
    )
    result = [str(value) for value in values if str(value or "")]
    result.extend(str(value) for value in row.get("source_ids", []) or [] if str(value))
    for key in ("relationship", "decision"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            result.extend(_source_ids(nested))
    return list(dict.fromkeys(result))


def _source_roles(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(role) for key, role in value.items()}
    if isinstance(value, list):
        rows = [_mapping(row, label="cluster source role") for row in value]
        roles = {
            str(row.get("source_id") or ""): str(row.get("role") or "") for row in rows
        }
        if "" not in roles:
            return roles
    raise ValueError("cluster source roles must identify every role by source ID")


def _stable_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {_digest(row): row for row in rows}
    return [unique[key] for key in sorted(unique)]


def _balanced_limit(
    rows: Sequence[dict[str, Any]],
    limit: int,
    *,
    group_for: Any,
) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return _stable_rows(rows)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _stable_rows(rows):
        groups[str(group_for(row))].append(row)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for key in sorted(groups):
            if groups[key] and len(selected) < limit:
                selected.append(groups[key].pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def _review_row(
    kind: str,
    artifact: Mapping[str, str],
    payload: Mapping[str, Any],
    required: Sequence[str],
) -> dict[str, Any]:
    base = {
        "kind": kind,
        "artifact_path": str(artifact["path"]),
        "artifact_path_scope": str(artifact["path_scope"]),
        "artifact_sha256": str(artifact["sha256"]),
        "payload": dict(payload),
        "required_judgments": list(required),
    }
    row_sha256 = _digest(base)
    return {
        "review_id": f"{kind}-{row_sha256[:20]}",
        **base,
        "row_sha256": row_sha256,
    }


def _row_group(row: Mapping[str, Any], strata: Mapping[str, str]) -> str:
    ids = _source_ids(_mapping(row.get("payload", {}), label="review payload"))
    return "|".join(sorted({strata.get(source_id, "unknown") for source_id in ids}))


def _load_sources(
    workspace: Path,
    mode: str,
    manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], list[dict[str, str]]]:
    if manifest_path is None and mode == "exhaustive40":
        candidate = workspace / "PRIVATE_MANIFEST.json"
        manifest_path = candidate if candidate.is_file() else None
    if manifest_path is not None:
        path = _workspace_file(workspace, str(manifest_path), label="Strategic40 private manifest")
        try:
            manifest = _mapping(
                json.loads(path.read_text(encoding="utf-8")),
                label="Strategic40 private manifest",
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Strategic40 private manifest must be valid JSON") from exc
        expected = _MODES[mode]
        cases = [
            _mapping(row, label="Strategic40 manifest case")
            for row in manifest.get("cases", []) or []
        ]
        if len(cases) != expected:
            raise ValueError(f"{mode} requires exactly {expected} manifest cases")
        strata: dict[str, str] = {}
        for case in cases:
            parent = _mapping(case.get("zotero_parent", {}), label="Strategic40 Zotero parent")
            source_id = str(case.get("source_id") or source_id_for_item(parent))
            if not source_id or source_id in strata:
                raise ValueError("Strategic40 source IDs must be non-empty and unique")
            strata[source_id] = str(case.get("primary_stratum_id") or "unstratified")
        notes: dict[str, tuple[Path, dict[str, Any]]] = {}
        for note_path in sorted((workspace / "02_source_memory" / "notes").glob("*.md")):
            note = read_note(note_path)
            frontmatter = _mapping(
                note.get("frontmatter", {}), label="Strategic40 note frontmatter"
            )
            source_id = str(frontmatter.get("source_id") or "")
            if source_id in notes:
                raise ValueError("Strategic40 notes repeat a source ID")
            if source_id in strata:
                notes[source_id] = (note_path, frontmatter)
        if set(notes) != set(strata):
            raise ValueError("Strategic40 notes must account for every manifest source")
        contexts = []
        for source_id in sorted(strata):
            note_path, frontmatter = notes[source_id]
            text, note_sha256 = _stable_text(note_path, label=f"note for {source_id}")
            contexts.append({
                "source_id": source_id,
                "note_id": str(frontmatter.get("note_id") or note_path.stem),
                "primary_stratum_id": strata[source_id],
                "note_artifact": _artifact(note_path, workspace, note_sha256),
                "note_text": text,
            })
        return contexts, strata, [_artifact(path, workspace, sha256_file(path))]

    manifest_path = workspace / "11_state" / "harness_bakeoff_manifest.yml"
    manifest, manifest_sha256 = _stable_yaml(
        manifest_path, label="harness bakeoff manifest"
    )
    expected = _MODES[mode]
    sources = [
        _mapping(row, label="manifest source")
        for row in manifest.get("sources", []) or []
    ]
    if int(manifest.get("source_count", -1)) != expected or len(sources) != expected:
        raise ValueError(f"{mode} requires exactly {expected} manifest sources")
    source_ids = [str(row.get("source_id") or "") for row in sources]
    if not all(source_ids) or len(set(source_ids)) != expected:
        raise ValueError("manifest source IDs must be non-empty and unique")

    contexts: list[dict[str, Any]] = []
    artifacts = [_artifact(manifest_path, workspace, manifest_sha256)]
    strata: dict[str, str] = {}
    for row in sorted(sources, key=lambda value: str(value["source_id"])):
        source_id = str(row["source_id"])
        stratum_id = str(row.get("primary_stratum_id") or "unstratified")
        note_path = _workspace_file(
            workspace, str(row.get("note_path") or ""), label=f"note for {source_id}"
        )
        text, note_sha256 = _stable_text(note_path, label=f"note for {source_id}")
        note_artifact = _artifact(note_path, workspace, note_sha256)
        strata[source_id] = stratum_id
        contexts.append(
            {
                "source_id": source_id,
                "note_id": str(row.get("note_id") or ""),
                "primary_stratum_id": stratum_id,
                "note_artifact": note_artifact,
                "note_text": text,
            }
        )
    return contexts, strata, artifacts


def _load_baseline(
    baseline_workspace: Path,
    baseline_manifest_path: Path,
    source_ids: set[str],
    current_workspace: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if not _inside(baseline_manifest_path, baseline_workspace):
        raise ValueError("baseline manifest must be inside the baseline workspace")
    manifest, manifest_sha256 = _stable_yaml(
        baseline_manifest_path, label="baseline manifest"
    )
    rows = [
        _mapping(row, label="baseline manifest source")
        for row in manifest.get("sources", []) or []
    ]
    if (
        len(rows) != 40
        or {str(row.get("source_id") or "") for row in rows} != source_ids
    ):
        raise ValueError("baseline manifest must contain the same forty source IDs")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = str(row["source_id"])
        note_path = _workspace_file(
            baseline_workspace,
            str(row.get("note_path") or ""),
            label=f"baseline note for {source_id}",
        )
        text, digest = _stable_text(note_path, label=f"baseline note for {source_id}")
        result[source_id] = {
            "note_text": text,
            "note_artifact": _artifact(note_path, current_workspace, digest),
        }
    return result, _artifact(baseline_manifest_path, current_workspace, manifest_sha256)


def _custody_records(
    manifest_path: Path,
    workspace: Path,
    source_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    manifest, manifest_sha256 = _stable_yaml(manifest_path, label="custody manifest")
    manifest_artifact = _artifact(manifest_path, workspace, manifest_sha256)
    if manifest.get("sources") is not None:
        cases = []
        for raw_source in manifest.get("sources", []) or []:
            source = _mapping(raw_source, label="custody source")
            selected = _mapping(source.get("selected", {}), label="custody source selection")
            raw = source.get("raw")
            raw_file = _mapping(raw, label="custody raw source") if raw is not None else None
            terminal_status = str(selected.get("terminal_status") or "")
            metadata_only = terminal_status == "limited_note"
            case: dict[str, Any] = {
                "case_id": str(source.get("parent_key") or ""),
                "source_id": str(source.get("source_id") or ""),
                "media_type": str(selected.get("media_type") or ""),
                "expected": {
                    "content_route": str(selected.get("route") or ""),
                    "terminal_status": terminal_status,
                    "source_scope": str(selected.get("scope") or ""),
                    "note_status": (
                        "metadata_only_source_note"
                        if metadata_only
                        else "analytical_atomic_note"
                    ),
                },
                "zotero_parent": _mapping(
                    source.get("parent_record", {}), label="custody source parent"
                ),
            }
            if raw_file is not None:
                attachment_key = str(raw_file.get("attachment_key") or "")
                raw_path = str(raw_file.get("path") or "")
                case.update({
                    "file": raw_path,
                    "sha256": str(raw_file.get("sha256") or ""),
                    "zotero_attachment": {
                        "key": attachment_key,
                        "data": {
                            "key": attachment_key,
                            "parentItem": str(source.get("parent_key") or ""),
                            "itemType": "attachment",
                            "contentType": str(raw_file.get("media_type") or ""),
                            "filename": Path(raw_path).name,
                        },
                    },
                })
            cases.append(case)
    else:
        cases = [
            _mapping(row, label="custody case")
            for row in manifest.get("cases", []) or []
        ]
    records: dict[str, dict[str, Any]] = {}
    artifacts = [manifest_artifact]
    for case in cases:
        parent = _mapping(case.get("zotero_parent", {}), label="custody Zotero parent")
        source_id = str(case.get("source_id") or source_id_for_item(parent))
        if not source_id or source_id in records:
            raise ValueError("custody cases must map uniquely to source IDs")
        file_value = case.get("file") or case.get("pdf")
        candidates: list[Path] = []
        if isinstance(file_value, str) and file_value:
            value = Path(file_value).expanduser()
            candidates.extend(
                [value]
                if value.is_absolute()
                else [manifest_path.parent / value, workspace / value]
            )
        elif isinstance(case.get("destination_name"), str):
            candidates.append(
                workspace / "01_custody" / "files" / str(case["destination_name"])
            )
        source_path = next(
            (path.resolve() for path in candidates if path.is_file()), None
        )
        if candidates and source_path is None:
            raise ValueError(
                f"raw custody evidence is missing for source-{_diagnostic_identity(source_id)}"
            )
        if source_path is None:
            evidence_artifact = manifest_artifact
        else:
            digest = sha256_file(source_path)
            expected = str(case.get("sha256") or "")
            if expected and expected != digest:
                raise ValueError(
                    "custody SHA-256 mismatch for "
                    f"source-{_diagnostic_identity(source_id)}"
                )
            evidence_artifact = _artifact(source_path, workspace, digest)
            artifacts.append(evidence_artifact)
        expected = _mapping(case.get("expected", {}) or {}, label="custody expectation")
        content_route = str(
            expected.get("content_route") or case.get("expected_route") or ""
        )
        terminal_status = str(
            expected.get("terminal_status")
            or case.get("expected_terminal_status")
            or (
                "limited_note"
                if source_path is None or content_route == "zotero_metadata"
                else "validated_note"
            )
        )
        if terminal_status not in {"validated_note", "limited_note"}:
            raise ValueError(
                "custody terminal status is invalid for "
                f"source-{_diagnostic_identity(source_id)}"
            )
        metadata_only = terminal_status == "limited_note"
        source_scope = str(
            expected.get("source_scope")
            or case.get("expected_source_scope")
            or ("metadata_only" if metadata_only else "full_document")
        )
        note_status = str(
            expected.get("note_status")
            or case.get("expected_note_status")
            or (
                "metadata_only_source_note"
                if metadata_only
                else "analytical_atomic_note"
            )
        )
        metadata_status_is_consistent = (
            source_scope == "metadata_only"
            and note_status == "metadata_only_source_note"
            and content_route == "zotero_metadata"
            and source_path is None
        )
        substantive_status_is_consistent = (
            source_path is not None
            and source_scope in {"full_document", "partial_document"}
            and note_status
            in {
                "analytical_atomic_note",
                "verified_atomic_note",
                "partial_document_atomic_note",
            }
            and content_route != "zotero_metadata"
        )
        if not (
            (metadata_only and metadata_status_is_consistent)
            or (not metadata_only and substantive_status_is_consistent)
        ):
            raise ValueError(
                "custody terminal/source status is inconsistent for "
                f"source-{_diagnostic_identity(source_id)}"
            )
        records[source_id] = {
            "artifact": evidence_artifact,
            "status_expectation": {
                "terminal_status": terminal_status,
                "note_status": note_status,
                "source_scope": source_scope,
                "metadata_only": metadata_only,
            },
            "case": {
                key: case[key]
                for key in (
                    "case_id",
                    "media_type",
                    "expected",
                    "zotero_parent",
                    "zotero_attachment",
                    "sha256",
                )
                if key in case
            },
        }
    if set(records) != source_ids:
        raise ValueError("custody manifest must account for the same forty source IDs")
    status_counts = {
        status: sum(
            record["status_expectation"]["terminal_status"] == status
            for record in records.values()
        )
        for status in ("validated_note", "limited_note")
    }
    if status_counts != {"validated_note": 34, "limited_note": 6}:
        raise ValueError(
            "exhaustive40 custody must contain 34 substantive and 6 limited sources"
        )
    return records, artifacts


def _run_map_artifacts(workspace: Path) -> list[dict[str, str]]:
    paths = {
        workspace / "03_literature_synthesis" / "manifest.yml",
        *(workspace / "03_literature_synthesis" / "maps").glob("*/manifest.yml"),
        *(workspace / "11_state" / "runs").glob("*/manifest.yml"),
    }
    artifacts = []
    for path in sorted((path for path in paths if path.is_file()), key=str):
        artifacts.append(_artifact(path, workspace, sha256_file(path)))
    return artifacts


def _blinded_note_row(
    source: Mapping[str, Any],
    baseline: Mapping[str, Any],
    custody: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_variant = (
        "A" if int(_digest([_SEED, source["source_id"]])[:2], 16) % 2 == 0 else "B"
    )
    baseline_variant = "B" if current_variant == "A" else "A"
    versions = {
        current_variant: {
            "artifact_sha256": source["note_artifact"]["sha256"],
            "note_text": source["note_text"],
        },
        baseline_variant: {
            "artifact_sha256": baseline["note_artifact"]["sha256"],
            "note_text": baseline["note_text"],
        },
    }
    row = _review_row(
        "note",
        custody["artifact"],
        {
            "source_id": source["source_id"],
            "primary_stratum_id": source["primary_stratum_id"],
            "variants": {key: versions[key] for key in ("A", "B")},
            "source_evidence": {
                "artifact_path": custody["artifact"]["path"],
                "artifact_path_scope": custody["artifact"]["path_scope"],
                "artifact_sha256": custody["artifact"]["sha256"],
                "custody_case": custody["case"],
                "status_expectation": custody["status_expectation"],
            },
        },
        tuple(
            f"variant_{variant.casefold()}_{criterion}"
            for variant in ("A", "B")
            for criterion in _NOTE_CRITERIA
        ),
    )
    return row, {
        "review_id": row["review_id"],
        "current_variant": current_variant,
        "baseline_variant": baseline_variant,
        "current_note_artifact": source["note_artifact"],
        "baseline_note_artifact": baseline["note_artifact"],
    }


def _select_clusters(
    clusters: Sequence[dict[str, Any]],
    syntheses: Mapping[str, Any],
    strata: Mapping[str, str],
    *,
    exhaustive: bool,
) -> list[dict[str, Any]]:
    clusters = _stable_rows(clusters)
    if exhaustive:
        return clusters
    mandatory: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        source_ids = [str(value) for value in cluster.get("source_ids", []) or []]
        cluster_strata = {strata.get(source_id, "unknown") for source_id in source_ids}
        synthesis = syntheses.get(cluster_id, {})
        ambiguous = (
            bool(cluster.get("refresh_pending"))
            or bool(
                _mapping(synthesis, label="cluster synthesis").get("parked_for_review")
            )
            or bool(
                _mapping(synthesis, label="cluster synthesis").get("quality_errors")
            )
            or str(_mapping(synthesis, label="cluster synthesis").get("status") or "")
            != "reasoned"
        )
        if len(cluster_strata) > 1 or len(source_ids) >= 10 or ambiguous:
            mandatory.append(cluster)
        else:
            remaining.append(cluster)
    sample_size = math.ceil(len(remaining) * 0.25)
    sampled = sorted(remaining, key=lambda row: _digest([_SEED, row]))[:sample_size]
    return _stable_rows([*mandatory, *sampled])


def _archive_path_is_safe(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _read_archive_files(path: Path, *, wheel: bool) -> dict[str, bytes]:
    if path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise _ArchiveAuditError("release archive exceeds the inspection byte ceiling")
    rows: list[tuple[str, bytes]] = []
    names: set[str] = set()
    total = 0
    if wheel:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            declared_total = sum(info.file_size for info in members if not info.is_dir())
            if declared_total > _MAX_ARCHIVE_BYTES:
                raise _ArchiveAuditError(
                    "release archive exceeds the inspection byte ceiling"
                )
            for info in members:
                identity = _diagnostic_identity(info.filename)
                if info.filename in names or not _archive_path_is_safe(info.filename):
                    raise _ArchiveAuditError(
                        f"unsafe or duplicate archive member: {identity}"
                    )
                names.add(info.filename)
                if info.is_dir():
                    raise _ArchiveAuditError(
                        f"explicit archive directory: {identity}"
                    )
                file_type = stat.S_IFMT(info.external_attr >> 16)
                if file_type not in {0, stat.S_IFREG}:
                    raise _ArchiveAuditError(
                        f"non-regular archive member: {identity}"
                    )
                data = archive.read(info)
                total += len(data)
                rows.append((info.filename, data))
    else:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            declared_total = sum(member.size for member in members if member.isfile())
            if declared_total > _MAX_ARCHIVE_BYTES:
                raise _ArchiveAuditError(
                    "release archive exceeds the inspection byte ceiling"
                )
            for member in members:
                identity = _diagnostic_identity(member.name)
                if member.name in names or not _archive_path_is_safe(member.name):
                    raise _ArchiveAuditError(
                        f"unsafe or duplicate archive member: {identity}"
                    )
                names.add(member.name)
                if member.isdir():
                    raise _ArchiveAuditError(
                        f"explicit archive directory: {identity}"
                    )
                if not member.isfile():
                    raise _ArchiveAuditError(
                        f"non-regular archive member: {identity}"
                    )
                handle = archive.extractfile(member)
                if handle is None:
                    raise _ArchiveAuditError(
                        f"unreadable archive member: {identity}"
                    )
                data = handle.read()
                total += len(data)
                rows.append((member.name, data))
    if total > _MAX_ARCHIVE_BYTES:
        raise _ArchiveAuditError("release archive exceeds the inspection byte ceiling")
    return dict(rows)


def _archive_files(path: Path, *, wheel: bool) -> dict[str, bytes]:
    try:
        return _read_archive_files(path, wheel=wheel)
    except _ArchiveAuditError:
        raise
    except Exception:
        raise ValueError("release archive is unreadable") from None


def _private_literal_findings(
    scope: str,
    name: str,
    data: bytes,
    private_literals: Sequence[bytes],
) -> list[str]:
    folded = data.lower()
    target = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return [
        f"{scope}:target-{target}:private_literal:{hashlib.sha256(literal).hexdigest()}"
        for literal in private_literals
        if literal.lower() in folded
    ]


def _content_findings(
    scope: str,
    name: str,
    data: bytes,
    private_literals: Sequence[bytes] = (),
) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(data.splitlines(), 1):
        for label, pattern in _SECRET_PATTERNS.items():
            if pattern.search(line):
                findings.append(f"{scope}:{name}:{line_number}:{label}")
    findings.extend(_private_literal_findings(scope, name, data, private_literals))
    return findings


def _path_findings(
    scope: str,
    value: str,
    private_literals: Sequence[bytes] = (),
) -> list[str]:
    encoded = value.encode("utf-8")
    identity = hashlib.sha256(encoded).hexdigest()[:16]
    findings = [
        f"{scope}:path-{identity}:filename_{label}"
        for label, pattern in _SECRET_PATTERNS.items()
        if pattern.search(encoded)
    ]
    findings.extend(
        _private_literal_findings(scope, f"path-{identity}", encoded, private_literals)
    )
    return findings


def _historical_content_findings(
    object_id: str,
    data: bytes,
    allowed: set[bytes],
    private_literals: Sequence[bytes] = (),
) -> tuple[list[str], list[dict[str, Any]]]:
    findings: list[str] = []
    counts: Counter[bytes] = Counter()
    for line_number, line in enumerate(data.splitlines(), 1):
        for label, pattern in _SECRET_PATTERNS.items():
            unrecognized = False
            for match in pattern.finditer(line):
                value = match.group(0)
                if value in allowed:
                    counts[value] += 1
                else:
                    unrecognized = True
            if unrecognized:
                findings.append(
                    f"git_history_blob:{object_id}:{line_number}:{label}"
                )
    findings.extend(
        _private_literal_findings(
            "git_history_blob", object_id, data, private_literals
        )
    )
    exemptions = [
        {
            "object_id": object_id,
            "sentinel_sha256": hashlib.sha256(value).hexdigest(),
            "occurrences": count,
        }
        for value, count in sorted(counts.items())
    ]
    return findings, exemptions


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _normalized_requirement(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(.*)", value.strip())
    if match is None:
        raise ValueError("pyproject dependency is invalid")
    return _normalized_distribution(match.group(1)) + match.group(2).strip()


def _pkg_info_matches_project(metadata: Any, project: Mapping[str, Any]) -> bool:
    dependencies = project.get("dependencies", []) or []
    optional = project.get("optional-dependencies", {}) or {}
    if not isinstance(dependencies, list) or not isinstance(optional, Mapping):
        raise ValueError("pyproject dependencies are invalid")
    expected_requirements = [
        _normalized_requirement(str(requirement)) for requirement in dependencies
    ]
    expected_extras: list[str] = []
    for raw_extra, raw_requirements in optional.items():
        extra = _normalized_distribution(str(raw_extra))
        if not isinstance(raw_requirements, list):
            raise ValueError("pyproject optional dependencies are invalid")
        expected_extras.append(extra)
        for raw_requirement in raw_requirements:
            requirement = _normalized_requirement(str(raw_requirement))
            if ";" in requirement:
                requirement, marker = requirement.split(";", 1)
                expected_requirements.append(
                    f"{requirement.strip()}; ({marker.strip()}) and extra == '{extra}'"
                )
            else:
                expected_requirements.append(f"{requirement}; extra == '{extra}'")
    return (
        metadata.get("Name") == str(project.get("name") or "")
        and metadata.get("Version") == str(project.get("version") or "")
        and metadata.get("Requires-Python")
        == (str(project["requires-python"]) if project.get("requires-python") else None)
        and sorted(metadata.get_all("Requires-Dist") or [])
        == sorted(expected_requirements)
        and sorted(metadata.get_all("Provides-Extra") or [])
        == sorted(expected_extras)
    )


def _git_output(repository: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    return result.stdout


def _head_blob(repository: Path, relative: str) -> bytes:
    output = _git_output(repository, "show", f"HEAD:{relative}", binary=True)
    assert isinstance(output, bytes)
    return output


def _head_package_sources(repository: Path) -> dict[str, bytes]:
    output = _git_output(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "HEAD",
        "--",
        "src/auto_zettelkasten",
        binary=True,
    )
    assert isinstance(output, bytes)
    result: dict[str, bytes] = {}
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, separator, path_bytes = raw.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ValueError("committed package tree is invalid")
        mode, kind, _object_id = (value.decode("ascii") for value in fields)
        relative = path_bytes.decode("utf-8")
        path = PurePosixPath(relative)
        if (
            kind != "blob"
            or mode not in {"100644", "100755"}
            or path.suffix != ".py"
            or not path.is_relative_to("src/auto_zettelkasten")
        ):
            raise ValueError(
                "unexpected committed package member: "
                f"{_diagnostic_identity(relative)}"
            )
        result[relative] = _head_blob(repository, relative)
    if not result:
        raise ValueError("committed package contains no Python modules")
    return result


def _historical_sentinel_object_ids(
    repository: Path, range_spec: str
) -> dict[str, set[bytes]]:
    result: dict[str, set[bytes]] = defaultdict(set)
    for path, sentinels in _HISTORICAL_TEST_SENTINELS.items():
        commits = str(
            _git_output(repository, "rev-list", range_spec, "--", path)
        ).splitlines()
        for commit in commits:
            tree = _git_output(
                repository, "ls-tree", "-z", commit, "--", path, binary=True
            )
            assert isinstance(tree, bytes)
            for raw in tree.split(b"\0"):
                metadata, separator, _path = raw.partition(b"\t")
                fields = metadata.split()
                if separator and len(fields) == 3 and fields[1] == b"blob":
                    result[fields[2].decode("ascii")].update(sentinels)
    return result


def _history_findings(
    repository: Path,
    range_spec: str,
    private_literals: Sequence[bytes] = (),
) -> tuple[list[str], list[dict[str, Any]]]:
    findings: list[str] = []
    exemptions: list[dict[str, Any]] = []
    messages = _git_output(
        repository, "log", "--format=%B%x00", range_spec, binary=True
    )
    assert isinstance(messages, bytes)
    findings.extend(
        _content_findings(
            "git_history_commit", "messages", messages, private_literals
        )
    )

    names = _git_output(
        repository,
        "log",
        "--format=",
        "--name-only",
        "-z",
        range_spec,
        "--",
        binary=True,
    )
    assert isinstance(names, bytes)
    for raw in names.split(b"\0"):
        relative = raw.strip(b"\n").decode("utf-8")
        if relative and _CREDENTIAL_NAMES.search(relative):
            findings.append(f"git_history_path:{relative}:credential_filename")
        if relative:
            findings.extend(
                _path_findings("git_history_path", relative, private_literals)
            )

    objects = _git_output(
        repository,
        "rev-list",
        "--objects",
        "--no-object-names",
        range_spec,
        binary=True,
    )
    assert isinstance(objects, bytes)
    object_ids = list(dict.fromkeys(objects.decode("ascii").splitlines()))
    if not object_ids:
        return findings, exemptions
    checked = subprocess.run(
        ("git", "cat-file", "--batch-check"),
        cwd=repository,
        check=True,
        input="".join(f"{object_id}\n" for object_id in object_ids),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()
    blobs: list[tuple[str, int]] = []
    for row in checked:
        fields = row.split()
        if len(fields) == 3 and fields[1] == "blob":
            blobs.append((fields[0], int(fields[2])))
    allowed = _historical_sentinel_object_ids(repository, range_spec)
    process = subprocess.Popen(
        ("git", "cat-file", "--batch"),
        cwd=repository,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    try:
        for object_id, size in blobs:
            if size > _MAX_ARCHIVE_BYTES:
                findings.append(f"git_history_blob:{object_id}:inspection_byte_ceiling")
                continue
            process.stdin.write(f"{object_id}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").split()
            if len(header) != 3 or header[0] != object_id or header[1] != "blob":
                raise ValueError("Git history blob stream is invalid")
            data = process.stdout.read(int(header[2]))
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise ValueError("Git history blob stream is truncated")
            blob_findings, blob_exemptions = _historical_content_findings(
                object_id,
                data,
                allowed.get(object_id, set()),
                private_literals,
            )
            findings.extend(blob_findings)
            exemptions.extend(blob_exemptions)
        process.stdin.close()
        process.stderr.read()
        if process.wait() != 0:
            raise ValueError("Git history blob scan failed")
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return findings, sorted(
        exemptions,
        key=lambda row: (str(row["object_id"]), str(row["sentinel_sha256"])),
    )


def _added_diff_findings(
    repository: Path,
    scope: str,
    *diff_args: str,
    private_literals: Sequence[bytes] = (),
) -> list[str]:
    output = _git_output(
        repository,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        *diff_args,
        "--",
        binary=True,
    )
    assert isinstance(output, bytes)
    added = b"\n".join(
        line[1:]
        for line in output.splitlines()
        if line.startswith(b"+") and not line.startswith(b"+++")
    )
    return _content_findings(scope, "added-lines", added, private_literals)


def _release_archive_findings(
    repository: Path,
    sdist_files: Mapping[str, bytes],
    wheel_files: Mapping[str, bytes],
    wheel_filename: str,
    private_literals: Sequence[bytes] = (),
) -> tuple[list[str], dict[str, Any]]:
    findings: list[str] = []
    sdist_roots = {PurePosixPath(name).parts[0] for name in sdist_files}
    if len(sdist_roots) != 1:
        raise ValueError("sdist must contain exactly one package root")
    sdist_root = sdist_roots.pop()
    pyproject_name = f"{sdist_root}/pyproject.toml"
    if pyproject_name not in sdist_files:
        raise ValueError("sdist pyproject.toml is missing")
    project = _mapping(
        tomllib.loads(sdist_files[pyproject_name].decode("utf-8")).get("project"),
        label="sdist project metadata",
    )
    distribution = str(project.get("name") or "")
    version = str(project.get("version") or "")
    normalized = distribution.replace("-", "_").replace(".", "_")
    expected_root = f"{normalized}-{version}"
    if sdist_root != expected_root or not distribution or not version:
        raise ValueError("sdist root disagrees with project name/version")
    package_metadata = BytesParser().parsebytes(
        sdist_files.get(f"{sdist_root}/PKG-INFO", b"")
    )
    if not _pkg_info_matches_project(package_metadata, project):
        findings.append("sdist:pkg_info_pyproject_mismatch")

    package_sources = _head_package_sources(repository)
    static = {"CHANGELOG.md", "LICENSE", "README.md", "pyproject.toml", ".gitignore"}
    expected_sdist = {
        *(f"{sdist_root}/{name}" for name in static),
        f"{sdist_root}/PKG-INFO",
        *(f"{sdist_root}/{name}" for name in package_sources),
    }
    if set(sdist_files) != expected_sdist:
        findings.append("sdist:member_allowlist")
    for relative, expected in package_sources.items():
        if sdist_files.get(f"{sdist_root}/{relative}") != expected:
            findings.append(f"sdist:{relative}:committed_head_mismatch")
    for relative in static:
        if sdist_files.get(f"{sdist_root}/{relative}") != _head_blob(
            repository, relative
        ):
            findings.append(f"sdist:{relative}:committed_head_mismatch")

    dist_info = f"{normalized}-{version}.dist-info"
    expected_wheel = {
        *(relative.removeprefix("src/") for relative in package_sources),
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/RECORD",
    }
    if set(wheel_files) != expected_wheel:
        findings.append("wheel:member_allowlist")
    for relative in package_sources:
        sdist_name = f"{sdist_root}/{relative}"
        wheel_name = relative.removeprefix("src/")
        if wheel_files.get(wheel_name) != sdist_files.get(sdist_name):
            findings.append(f"wheel:{wheel_name}:sdist_source_mismatch")
    metadata_name = f"{dist_info}/METADATA"
    metadata_bytes = wheel_files.get(metadata_name, b"")
    if metadata_bytes != sdist_files.get(f"{sdist_root}/PKG-INFO"):
        findings.append("wheel:metadata_sdist_mismatch")
    metadata = BytesParser().parsebytes(metadata_bytes)
    if metadata.get("Name") != distribution or metadata.get("Version") != version:
        findings.append("wheel:metadata_name_or_version")

    scripts = _mapping(project.get("scripts", {}), label="project scripts")
    expected_entry_points = (
        "[console_scripts]\n"
        + "".join(f"{name} = {scripts[name]}\n" for name in sorted(scripts))
    ).encode()
    if wheel_files.get(f"{dist_info}/entry_points.txt") != expected_entry_points:
        findings.append("wheel:entry_points_mismatch")
    if wheel_files.get(f"{dist_info}/licenses/LICENSE") != sdist_files.get(
        f"{sdist_root}/LICENSE"
    ):
        findings.append("wheel:license_sdist_mismatch")

    wheel_metadata = BytesParser().parsebytes(
        wheel_files.get(f"{dist_info}/WHEEL", b"")
    )
    wheel_tags = wheel_metadata.get_all("Tag") or []
    wheel_generators = wheel_metadata.get_all("Generator") or []
    if (
        set(wheel_metadata.keys())
        != {"Wheel-Version", "Generator", "Root-Is-Purelib", "Tag"}
        or wheel_metadata.get_all("Wheel-Version") != ["1.0"]
        or wheel_metadata.get_all("Root-Is-Purelib") != ["true"]
        or wheel_tags != ["py3-none-any"]
        or len(wheel_generators) != 1
        or re.fullmatch(r"hatchling \d+(?:\.\d+)+", wheel_generators[0]) is None
    ):
        findings.append("wheel:wheel_metadata")
    if wheel_filename != f"{normalized}-{version}-py3-none-any.whl":
        findings.append("wheel:filename_tag")

    record_name = f"{dist_info}/RECORD"
    try:
        record_rows = list(
            csv.reader(
                io.StringIO(wheel_files.get(record_name, b"").decode("utf-8"))
            )
        )
    except UnicodeDecodeError:
        record_rows = []
    record_by_name: dict[str, list[str]] = {}
    record_shape_valid = True
    for row in record_rows:
        if len(row) != 3 or row[0] in record_by_name:
            record_shape_valid = False
            continue
        record_by_name[row[0]] = row
    record_inventory_valid = record_shape_valid and set(record_by_name) == set(
        wheel_files
    )
    record_integrity_valid = True
    for name, data in wheel_files.items():
        row = record_by_name.get(name, [])
        if name == record_name:
            record_integrity_valid = record_integrity_valid and row == [name, "", ""]
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record_integrity_valid = record_integrity_valid and row == [
            name,
            "sha256=" + digest.decode("ascii"),
            str(len(data)),
        ]
    if not record_inventory_valid:
        findings.append("wheel:record_inventory")
    if not record_integrity_valid:
        findings.append("wheel:record_hash_or_size")

    for scope, files in (("sdist", sdist_files), ("wheel", wheel_files)):
        for name, data in files.items():
            relative = name.removeprefix(f"{sdist_root}/") if scope == "sdist" else name
            if _CREDENTIAL_NAMES.search(relative):
                findings.append(f"{scope}:{relative}:credential_filename")
            findings.extend(
                _path_findings(f"{scope}_path", relative, private_literals)
            )
            findings.extend(
                _content_findings(scope, relative, data, private_literals)
            )
    return findings, {
        "distribution": distribution,
        "version": version,
        "sdist_member_count": len(sdist_files),
        "wheel_member_count": len(wheel_files),
    }


def package_audit(
    repository: Path,
    sdist_path: Path,
    wheel_path: Path,
    report_path: Path,
    *,
    base_ref: str = "origin/main",
    artifacts: Sequence[Path] = (),
    private_denylist_path: Path | None = None,
    private_denylist_sha256: str = "",
) -> dict[str, Any]:
    repository = repository.expanduser().resolve()
    if not repository.is_dir() or _git_root(repository) != repository:
        raise ValueError("repository must be a Git worktree root")
    report_path = _private_yaml_path(report_path, repository, label="package audit report")
    sdist_path = _private_input(sdist_path, label="sdist")
    wheel_path = _private_input(wheel_path, label="wheel")
    if not sdist_path.name.endswith(".tar.gz") or wheel_path.suffix != ".whl":
        raise ValueError("package audit requires one .tar.gz sdist and one .whl wheel")
    artifact_paths = [_private_input(path, label="release artifact") for path in artifacts]
    denylist_sha256, private_literals = _private_literal_denylist(
        private_denylist_path, private_denylist_sha256
    )

    sdist_files = _archive_files(sdist_path, wheel=False)
    wheel_files = _archive_files(wheel_path, wheel=True)
    findings, package = _release_archive_findings(
        repository,
        sdist_files,
        wheel_files,
        wheel_path.name,
        private_literals,
    )
    findings.extend(
        _content_findings(
            "sdist_container",
            sdist_path.name,
            sdist_path.read_bytes(),
            private_literals,
        )
    )
    findings.extend(
        _content_findings(
            "wheel_container",
            wheel_path.name,
            wheel_path.read_bytes(),
            private_literals,
        )
    )
    findings.extend(
        _path_findings("sdist_container_path", sdist_path.name, private_literals)
    )
    findings.extend(
        _path_findings("wheel_container_path", wheel_path.name, private_literals)
    )
    normalized = str(package["distribution"]).replace("-", "_").replace(".", "_")
    if sdist_path.name != f"{normalized}-{package['version']}.tar.gz":
        findings.append("sdist:filename")
    status = _git_output(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        binary=True,
    )
    assert isinstance(status, bytes)
    repository_dirty = bool(status)
    if repository_dirty:
        findings.append("git_tree:dirty_candidate_state")
    tracked = _git_output(repository, "ls-files", "-z", binary=True)
    assert isinstance(tracked, bytes)
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        path = (repository / relative).resolve()
        if not _inside(path, repository) or not path.is_file():
            findings.append(f"git_tree:{relative}:missing_or_unsafe")
            continue
        if _CREDENTIAL_NAMES.search(relative):
            findings.append(f"git_tree:{relative}:credential_filename")
        findings.extend(_path_findings("git_tree_path", relative, private_literals))
        findings.extend(
            _content_findings(
                "git_tree", relative, path.read_bytes(), private_literals
            )
        )
    range_spec = f"{base_ref}..HEAD"
    findings.extend(
        _added_diff_findings(
            repository,
            "git_diff",
            range_spec,
            private_literals=private_literals,
        )
    )
    findings.extend(
        _added_diff_findings(
            repository,
            "staged_diff",
            "--cached",
            private_literals=private_literals,
        )
    )
    history_findings, history_exemptions = _history_findings(
        repository, range_spec, private_literals
    )
    findings.extend(history_findings)
    for path in artifact_paths:
        if _CREDENTIAL_NAMES.search(path.name):
            findings.append(f"artifact:{path.name}:credential_filename")
        findings.extend(_path_findings("artifact_path", path.name, private_literals))
        findings.extend(
            _content_findings(
                "artifact", path.name, path.read_bytes(), private_literals
            )
        )

    findings = sorted(set(findings))
    matched_private_literals = sorted({
        finding.rsplit(":private_literal:", 1)[1]
        for finding in findings
        if ":private_literal:" in finding
    })
    report = {
        "package_audit_schema_version": "1",
        "status": "passed" if not findings else "failed",
        "provider_calls": 0,
        "repository_head": str(_git_output(repository, "rev-parse", "HEAD")).strip(),
        "repository_dirty": repository_dirty,
        "base_ref": base_ref,
        "private_literal_denylist_sha256": denylist_sha256,
        "matched_private_literal_sha256": matched_private_literals,
        "sdist": {"path": str(sdist_path), "sha256": sha256_file(sdist_path)},
        "wheel": {"path": str(wheel_path), "sha256": sha256_file(wheel_path)},
        "release_artifacts": [
            {"path": str(path), "sha256": sha256_file(path)} for path in artifact_paths
        ],
        "history_test_sentinel_policy": {
            "scope": "history_only",
            "paths": sorted(_HISTORICAL_TEST_SENTINELS),
            "exemptions": history_exemptions,
        },
        **package,
        "findings": findings,
    }
    report = _redact_private_literals(report, private_literals)
    write_yaml(report_path, report)
    return report


def prepare(
    workspace: Path,
    mode: str,
    packet_path: Path,
    *,
    baseline_workspace: Path | None = None,
    baseline_manifest_path: Path | None = None,
    custody_manifest_path: Path | None = None,
    bindings_path: Path | None = None,
    source_manifest_path: Path | None = None,
) -> dict[str, Any]:
    if mode not in _MODES:
        raise ValueError(f"unsupported review mode: {mode}")
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("workspace does not exist")
    packet_path = _private_yaml_path(packet_path, workspace, label="review packet")
    sources, strata, artifacts = _load_sources(workspace, mode, source_manifest_path)
    exhaustive = mode == "exhaustive40"
    note_bindings: list[dict[str, Any]] = []
    baseline_manifest_artifact: dict[str, str] | None = None
    custody_manifest_artifact: dict[str, str] | None = None
    custody: dict[str, dict[str, Any]] = {}
    if exhaustive:
        if None in {
            baseline_workspace,
            baseline_manifest_path,
            custody_manifest_path,
            bindings_path,
        }:
            raise ValueError(
                "exhaustive40 requires baseline workspace/manifest, custody manifest, and bindings"
            )
        baseline_root = _private_workspace(baseline_workspace, workspace)  # type: ignore[arg-type]
        baseline_manifest = _private_input(
            baseline_manifest_path,
            label="baseline manifest",  # type: ignore[arg-type]
        )
        custody_manifest = _private_input(
            custody_manifest_path,
            label="custody manifest",  # type: ignore[arg-type]
        )
        custody_root = _custody_root(custody_manifest.parent, workspace)
        bindings_path = _private_yaml_path(
            bindings_path,
            workspace,
            label="review bindings",  # type: ignore[arg-type]
        )
        if packet_path == bindings_path:
            raise ValueError("review packet and bindings must be distinct paths")
        _require_outside_evidence_roots(
            (packet_path, bindings_path),
            tuple(dict.fromkeys((workspace, baseline_root, custody_root))),
        )
        baseline, baseline_manifest_artifact = _load_baseline(
            baseline_root, baseline_manifest, set(strata), workspace
        )
        custody, custody_artifacts = _custody_records(
            custody_manifest, workspace, set(strata)
        )
        custody_manifest_artifact = custody_artifacts[0]
        artifacts.extend(custody_artifacts)
    else:
        artifacts.extend(source["note_artifact"] for source in sources)
        baseline = {}
    artifacts.extend(_run_map_artifacts(workspace))

    typed_path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    cluster_path = workspace / "03_literature_synthesis" / "cluster_registry.yml"
    synthesis_path = workspace / "03_literature_synthesis" / "cluster_syntheses.yml"
    typed, typed_sha256 = _stable_yaml(typed_path, label="typed link registry")
    cluster_registry, cluster_sha256 = _stable_yaml(
        cluster_path, label="cluster registry"
    )
    synthesis_registry, synthesis_sha256 = _stable_yaml(
        synthesis_path, label="cluster synthesis registry"
    )
    typed_artifact = _artifact(typed_path, workspace, typed_sha256)
    cluster_artifact = _artifact(cluster_path, workspace, cluster_sha256)
    synthesis_artifact = _artifact(synthesis_path, workspace, synthesis_sha256)
    artifacts.extend((typed_artifact, cluster_artifact, synthesis_artifact))

    pair_decisions = [
        _mapping(row, label="pair decision")
        for row in typed.get("pair_decisions", []) or []
        if isinstance(row, Mapping) and row.get("active") is True
    ]
    accepted = [
        row
        for row in pair_decisions
        if str(row.get("decision_status") or row.get("status") or "") == "accepted"
    ]
    negative = [
        row
        for row in pair_decisions
        if str(row.get("decision_status") or row.get("status") or "")
        == "no_relationship"
    ]
    clusters = [
        _mapping(row, label="cluster")
        for row in cluster_registry.get("clusters", []) or []
    ]
    syntheses = _mapping(
        synthesis_registry.get("syntheses", {}), label="cluster syntheses"
    )
    if not accepted or not clusters or not syntheses:
        raise ValueError(
            "review requires accepted relationships, clusters, and syntheses"
        )
    known_sources = set(strata)
    for row in [*accepted, *negative]:
        if not set(_source_ids(row)) <= known_sources:
            raise ValueError(
                "relationship endpoint is outside the frozen source manifest"
            )

    relation_rows = [
        _review_row(
            "relationship",
            typed_artifact,
            {"relationship": row},
            (
                "pass",
                "material_error",
                "relation_correct",
                "type_direction_correct",
                "source_grounded",
            ),
        )
        for row in accepted
    ]
    if not exhaustive and len(relation_rows) > _RELATION_LIMIT:
        cross = [
            row
            for row in relation_rows
            if len(set(_row_group(row, strata).split("|"))) > 1
        ]
        if len(cross) > _RELATION_LIMIT:
            raise ValueError(
                "cross-stratum relationships exceed the 200-row audit limit"
            )
        cross_ids = {row["review_id"] for row in cross}
        fill = _balanced_limit(
            [row for row in relation_rows if row["review_id"] not in cross_ids],
            _RELATION_LIMIT - len(cross),
            group_for=lambda row: _row_group(row, strata),
        )
        relation_rows = _stable_rows([*cross, *fill])

    selected_clusters = _select_clusters(
        clusters, syntheses, strata, exhaustive=exhaustive
    )
    membership_rows: list[dict[str, Any]] = []
    cluster_rows: list[dict[str, Any]] = []
    synthesis_rows: list[dict[str, Any]] = []
    for cluster in selected_clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id or cluster_id not in syntheses:
            raise ValueError("every audited cluster must have a synthesis")
        roles = _source_roles(cluster.get("source_roles", {}))
        source_ids = [str(value) for value in cluster.get("source_ids", []) or []]
        for source_id in source_ids:
            if source_id not in known_sources:
                raise ValueError("cluster member is outside the frozen source manifest")
            membership_rows.append(
                _review_row(
                    "membership",
                    cluster_artifact,
                    {
                        "cluster": cluster,
                        "member_source_id": source_id,
                        "member_role": str(roles.get(source_id) or ""),
                    },
                    (
                        "pass",
                        "material_error",
                        "membership_correct",
                        "role_correct",
                        "source_grounded",
                    ),
                )
            )
        cluster_rows.append(
            _review_row(
                "cluster",
                cluster_artifact,
                {"cluster": cluster},
                (
                    "pass",
                    "material_error",
                    "cluster_coherent",
                    "severe_overmerge",
                    "source_grounded",
                ),
            )
        )
        synthesis_rows.append(
            _review_row(
                "synthesis",
                synthesis_artifact,
                {
                    "cluster": cluster,
                    "synthesis": _mapping(syntheses[cluster_id], label="synthesis"),
                },
                (
                    "pass",
                    "material_error",
                    "central_claims_supported",
                    "source_grounded",
                ),
            )
        )
    if not membership_rows or not synthesis_rows:
        raise ValueError("review requires cluster memberships and syntheses")
    if not exhaustive and len(membership_rows) > _MEMBERSHIP_LIMIT:
        first_by_cluster: dict[str, dict[str, Any]] = {}
        for row in _stable_rows(membership_rows):
            cluster_id = str(row["payload"]["cluster"].get("cluster_id") or "")
            first_by_cluster.setdefault(cluster_id, row)
        if len(first_by_cluster) > _MEMBERSHIP_LIMIT:
            raise ValueError("audited clusters exceed the 200-row membership limit")
        mandatory_ids = {row["review_id"] for row in first_by_cluster.values()}
        fill = _balanced_limit(
            [row for row in membership_rows if row["review_id"] not in mandatory_ids],
            _MEMBERSHIP_LIMIT - len(mandatory_ids),
            group_for=lambda row: strata.get(
                str(row["payload"].get("member_source_id") or ""), "unknown"
            ),
        )
        membership_rows = _stable_rows([*first_by_cluster.values(), *fill])

    decisions: list[dict[str, Any]] = [
        _review_row(
            "rejected_or_unclustered",
            typed_artifact,
            {"decision_kind": "no_relationship", "decision": row},
            ("pass", "material_error", "decision_correct"),
        )
        for row in negative
    ]
    decisions.extend(
        _review_row(
            "rejected_or_unclustered",
            cluster_artifact,
            {
                "decision_kind": "unclustered",
                "decision": _mapping(row, label="unclustered row"),
            },
            ("pass", "material_error", "decision_correct"),
        )
        for row in cluster_registry.get("unclustered_sources", []) or []
    )
    decisions.extend(
        _review_row(
            "rejected_or_unclustered",
            cluster_artifact,
            {
                "decision_kind": "rejected_cluster",
                "decision": _mapping(row, label="rejected proposal"),
            },
            ("pass", "material_error", "decision_correct"),
        )
        for row in cluster_registry.get("rejected_proposals", []) or []
    )
    if not decisions:
        raise ValueError("review requires rejected or unclustered decisions")
    if not exhaustive:
        decisions = _balanced_limit(
            decisions,
            _DECISION_LIMIT,
            group_for=lambda row: _row_group(row, strata),
        )

    note_rows = []
    if exhaustive:
        for source in sources:
            row, binding = _blinded_note_row(
                source,
                baseline[str(source["source_id"])],
                custody[str(source["source_id"])],
            )
            note_rows.append(row)
            note_bindings.append(binding)

    rows = sorted(
        [
            *note_rows,
            *relation_rows,
            *membership_rows,
            *cluster_rows,
            *decisions,
            *synthesis_rows,
        ],
        key=lambda row: (str(row["kind"]), str(row["review_id"])),
    )
    artifact_rows = sorted(
        {_canonical(row): row for row in artifacts}.values(),
        key=lambda row: str(row["path"]),
    )
    packet = {
        "review_packet_schema_version": "1",
        "evidence_status": "autonomous_provisional",
        "never_production_input": True,
        "provider_calls": 0,
        "mode": mode,
        "source_count": len(sources),
        "selection_seed": _SEED,
        "selection_policy": {
            "exhaustive40": "all notes, relationships, memberships, decisions, and syntheses",
            "stratified500": {
                "relationships": "all cross-stratum, then deterministic strata balance; maximum 200",
                "clusters": "all cross-stratum, 10+ member, or ambiguous clusters, plus 25% of the remainder",
                "memberships": "at least one per audited cluster, then deterministic strata balance; maximum 200",
                "decisions": "deterministic strata balance; maximum 100",
            },
        },
        "workspace_binding_sha256": _digest(str(workspace)),
        "artifacts": artifact_rows,
        "source_context": [] if exhaustive else sources,
        "selection_counts": {
            "notes": len(note_rows),
            "relationships": len(relation_rows),
            "memberships": len(membership_rows),
            "clusters": len(cluster_rows),
            "rejected_or_unclustered": len(decisions),
            "syntheses": len(synthesis_rows),
            "total": len(rows),
        },
        "rows": rows,
    }
    packet["packet_identity"] = _digest(packet)
    write_yaml(packet_path, packet)
    if exhaustive:
        assert (
            bindings_path is not None
            and baseline_manifest_artifact is not None
            and custody_manifest_artifact is not None
        )
        bindings = {
            "binding_schema_version": "1",
            "evidence_status": "autonomous_provisional",
            "never_production_input": True,
            "packet_sha256": sha256_file(packet_path),
            "workspace": str(workspace),
            "baseline_workspace": str(baseline_root),
            "protected_evidence_roots": {
                "baseline": str(baseline_root),
                "custody": str(custody_root),
            },
            "baseline_manifest_artifact": baseline_manifest_artifact,
            "custody_manifest_artifact": custody_manifest_artifact,
            "bindings": sorted(note_bindings, key=lambda row: str(row["review_id"])),
        }
        bindings["binding_identity"] = _digest(bindings)
        write_yaml(bindings_path, bindings)
    return packet


def _verify_packet(workspace: Path, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (
        packet.get("review_packet_schema_version") != "1"
        or packet.get("evidence_status") != "autonomous_provisional"
        or packet.get("never_production_input") is not True
        or packet.get("provider_calls") != 0
        or packet.get("mode") not in _MODES
        or packet.get("workspace_binding_sha256") != _digest(str(workspace))
    ):
        raise ValueError("review packet identity is invalid")
    without_identity = dict(packet)
    identity = str(without_identity.pop("packet_identity", ""))
    if identity != _digest(without_identity):
        raise ValueError("review packet identity hash is invalid")

    artifact_hashes: dict[tuple[str, str], str] = {}
    for raw in packet.get("artifacts", []) or []:
        artifact = _mapping(raw, label="packet artifact")
        relative = str(artifact.get("path") or "")
        scope = str(artifact.get("path_scope") or "")
        expected = str(artifact.get("sha256") or "")
        key = (scope, relative)
        if key in artifact_hashes:
            raise ValueError("review packet repeats an artifact")
        path = _artifact_path(workspace, artifact)
        if len(expected) != 64 or sha256_file(path) != expected:
            raise ValueError(
                f"stale review artifact: {_diagnostic_identity(relative)}"
            )
        artifact_hashes[key] = expected

    rows = [_mapping(row, label="review row") for row in packet.get("rows", []) or []]
    if not rows:
        raise ValueError("review packet contains no rows")
    seen: set[str] = set()
    for row in rows:
        review_id = str(row.get("review_id") or "")
        if not review_id or review_id in seen:
            raise ValueError("review row IDs must be non-empty and unique")
        seen.add(review_id)
        base = {
            key: value
            for key, value in row.items()
            if key not in {"review_id", "row_sha256"}
        }
        row_sha256 = _digest(base)
        if (
            row_sha256 != row.get("row_sha256")
            or review_id != f"{row.get('kind')}-{row_sha256[:20]}"
        ):
            raise ValueError("review row identity is invalid")
        path = str(row.get("artifact_path") or "")
        scope = str(row.get("artifact_path_scope") or "")
        if artifact_hashes.get((scope, path)) != row.get("artifact_sha256"):
            raise ValueError("review row is not bound to its artifact")
    return rows


def _verify_note_bindings(
    workspace: Path,
    packet_path: Path,
    bindings_path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], tuple[Path, ...]]:
    bindings = _mapping(read_yaml(bindings_path, None), label="review bindings")
    without_identity = dict(bindings)
    identity = str(without_identity.pop("binding_identity", ""))
    if (
        bindings.get("binding_schema_version") != "1"
        or bindings.get("evidence_status") != "autonomous_provisional"
        or bindings.get("never_production_input") is not True
        or bindings.get("packet_sha256") != sha256_file(packet_path)
        or bindings.get("workspace") != str(workspace)
        or identity != _digest(without_identity)
    ):
        raise ValueError("review bindings are invalid or stale")
    protected = _mapping(
        bindings.get("protected_evidence_roots"),
        label="protected evidence roots",
    )
    if set(protected) != {"baseline", "custody"}:
        raise ValueError("review bindings must identify both protected evidence roots")
    baseline_root = _private_workspace(Path(str(protected["baseline"])), workspace)
    custody_root = _custody_root(Path(str(protected["custody"])), workspace)
    if bindings.get("baseline_workspace") != str(baseline_root):
        raise ValueError("review bindings baseline root is inconsistent")
    manifest_artifact = _mapping(
        bindings.get("baseline_manifest_artifact"), label="baseline manifest artifact"
    )
    manifest_path = _artifact_path(workspace, manifest_artifact)
    if not _inside(manifest_path, baseline_root) or sha256_file(manifest_path) != str(
        manifest_artifact.get("sha256") or ""
    ):
        raise ValueError("baseline manifest binding is stale")
    custody_artifact = _mapping(
        bindings.get("custody_manifest_artifact"), label="custody manifest artifact"
    )
    custody_path = _artifact_path(workspace, custody_artifact)
    if not _inside(custody_path, custody_root) or sha256_file(custody_path) != str(
        custody_artifact.get("sha256") or ""
    ):
        raise ValueError("custody manifest binding is stale")

    note_rows = {
        str(row["review_id"]): row for row in rows if row.get("kind") == "note"
    }
    raw_bindings = [
        _mapping(row, label="note binding")
        for row in bindings.get("bindings", []) or []
    ]
    by_id = {str(row.get("review_id") or ""): row for row in raw_bindings}
    if len(by_id) != len(raw_bindings) or set(by_id) != set(note_rows):
        raise ValueError("review bindings must cover every blinded note exactly once")
    current_variants: dict[str, str] = {}
    for review_id, binding in by_id.items():
        current_variant = str(binding.get("current_variant") or "")
        baseline_variant = str(binding.get("baseline_variant") or "")
        if {current_variant, baseline_variant} != {"A", "B"}:
            raise ValueError("note variant binding is invalid")
        packet_versions = _mapping(
            note_rows[review_id].get("payload", {}).get("variants", {}),
            label="packet note variants",
        )
        for label, key in (
            (current_variant, "current_note_artifact"),
            (baseline_variant, "baseline_note_artifact"),
        ):
            artifact = _mapping(binding.get(key), label=key)
            path = _artifact_path(workspace, artifact)
            expected = str(artifact.get("sha256") or "")
            if (
                sha256_file(path) != expected
                or _mapping(
                    packet_versions.get(label), label="packet note variant"
                ).get("artifact_sha256")
                != expected
            ):
                raise ValueError("blinded note artifact binding is stale")
        current_variants[review_id] = current_variant
    return current_variants, tuple(dict.fromkeys((baseline_root, custody_root)))


def _wilson_lower(successes: int, total: int) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    z2 = _Z_95**2
    denominator = 1 + z2 / total
    center = proportion + z2 / (2 * total)
    margin = _Z_95 * math.sqrt(
        proportion * (1 - proportion) / total + z2 / (4 * total**2)
    )
    return (center - margin) / denominator


def _rate(values: Sequence[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def score(
    workspace: Path,
    packet_path: Path,
    review_path: Path,
    report_path: Path,
    *,
    bindings_path: Path | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("workspace does not exist")
    packet_path = _private_yaml_path(packet_path, workspace, label="review packet")
    review_path = _private_yaml_path(review_path, workspace, label="review")
    report_path = _private_yaml_path(report_path, workspace, label="score report")
    if len({packet_path, review_path, report_path}) != 3:
        raise ValueError("packet, review, and report must be distinct private paths")
    packet = _mapping(read_yaml(packet_path, None), label="review packet")
    rows = _verify_packet(workspace, packet)
    current_variants: dict[str, str] = {}
    protected_roots: tuple[Path, ...] = ()
    if packet.get("mode") == "exhaustive40":
        if bindings_path is None:
            raise ValueError("exhaustive40 scoring requires private note bindings")
        bindings_path = _private_yaml_path(
            bindings_path, workspace, label="review bindings"
        )
        if bindings_path in {packet_path, review_path, report_path}:
            raise ValueError("review bindings must use a distinct private path")
        current_variants, protected_roots = _verify_note_bindings(
            workspace, packet_path, bindings_path, rows
        )
        _require_outside_evidence_roots(
            (packet_path, bindings_path, review_path, report_path),
            tuple(dict.fromkeys((workspace, *protected_roots))),
        )
    review = _mapping(read_yaml(review_path, None), label="review")
    if (
        review.get("review_schema_version") != "2"
        or review.get("evidence_status") != "autonomous_provisional"
        or review.get("never_production_input") is not True
        or review.get("packet_sha256") != sha256_file(packet_path)
    ):
        raise ValueError("review is not bound to this autonomous provisional packet")

    judgments = [
        _mapping(row, label="review judgment")
        for row in review.get("judgments", []) or []
    ]
    by_id = {str(row.get("review_id") or ""): row for row in judgments}
    if len(by_id) != len(judgments) or set(by_id) != {
        str(row["review_id"]) for row in rows
    }:
        raise ValueError("review judgments must cover every packet row exactly once")
    for row in rows:
        judgment = by_id[str(row["review_id"])]
        judgment_payload = dict(judgment)
        judgment_sha256 = str(judgment_payload.pop("judgment_sha256", ""))
        if (
            judgment.get("artifact_sha256") != row["artifact_sha256"]
            or judgment.get("row_sha256") != row["row_sha256"]
            or judgment.get("packet_sha256") != sha256_file(packet_path)
            or not _REVIEWER_ID.fullmatch(str(judgment.get("reviewer_task_id") or ""))
            or not _REVIEWER_MODEL.fullmatch(str(judgment.get("model") or ""))
            or judgment.get("reasoning_effort") not in _REASONING_EFFORTS
            or judgment_sha256 != _digest(judgment_payload)
        ):
            raise ValueError("review judgment is stale or bound to another row")
        for field in row.get("required_judgments", []) or []:
            if type(judgment.get(str(field))) is not bool:
                raise ValueError(f"review judgment {field} must be boolean")

    def values(kind: str, field: str) -> list[bool]:
        return [
            bool(by_id[str(row["review_id"])][field])
            for row in rows
            if row.get("kind") == kind
        ]

    note_metrics: dict[str, list[bool]] = {
        criterion: [] for criterion in _NOTE_CRITERIA
    }
    passes: list[bool] = []
    material_errors = 0
    source_grounded: list[bool] = []
    metadata_only_non_pretense: list[bool] = []
    for row in rows:
        judgment = by_id[str(row["review_id"])]
        if row.get("kind") == "note":
            prefix = f"variant_{current_variants[str(row['review_id'])].casefold()}_"
            current = {
                criterion: bool(judgment[prefix + criterion])
                for criterion in _NOTE_CRITERIA
            }
            for criterion, value in current.items():
                note_metrics[criterion].append(value)
            source_evidence = _mapping(
                _mapping(row.get("payload", {}), label="note payload").get(
                    "source_evidence"
                ),
                label="note source evidence",
            )
            status_expectation = _mapping(
                source_evidence.get("status_expectation"),
                label="note status expectation",
            )
            if type(status_expectation.get("metadata_only")) is not bool:
                raise ValueError("note metadata-only expectation must be boolean")
            is_metadata_only = bool(status_expectation["metadata_only"])
            if is_metadata_only:
                metadata_only_non_pretense.append(current["metadata_only_non_pretense"])
            passes.append(
                all(
                    current[field]
                    for field in (
                        "identity_correct",
                        "custody_link_correct",
                        "status_correct",
                        "locators_supported",
                        "unsupported_claims_absent",
                        "false_quotations_absent",
                        "source_grounded",
                    )
                )
                and (not is_metadata_only or current["metadata_only_non_pretense"])
                and not current["material_error"]
            )
            material_errors += current["material_error"]
            source_grounded.append(current["source_grounded"])
        else:
            passes.append(bool(judgment["pass"]))
            material_errors += bool(judgment["material_error"])
            if "source_grounded" in row.get("required_judgments", []):
                source_grounded.append(bool(judgment["source_grounded"]))
    relation_correct = values("relationship", "relation_correct")
    membership_correct = values("membership", "membership_correct")
    type_direction = values("relationship", "type_direction_correct")
    roles = values("membership", "role_correct")
    decisions = values("rejected_or_unclustered", "decision_correct")
    supported = values("synthesis", "central_claims_supported")
    cluster_coherence = values("cluster", "cluster_coherent")
    severe_overmerges = values("cluster", "severe_overmerge")
    metrics = {
        "material_error_count": material_errors,
        "overall_pass_rate": _rate(passes),
        "relationship_correctness": {
            "reviewed": len(relation_correct),
            "point_accuracy": _rate(relation_correct),
            "wilson_95_lower": _wilson_lower(
                sum(relation_correct), len(relation_correct)
            ),
        },
        "membership_correctness": {
            "reviewed": len(membership_correct),
            "point_accuracy": _rate(membership_correct),
            "wilson_95_lower": _wilson_lower(
                sum(membership_correct), len(membership_correct)
            ),
        },
        "type_direction_accuracy": _rate(type_direction),
        "role_accuracy": _rate(roles),
        "rejected_unclustered_accuracy": _rate(decisions),
        "cluster_coherence_accuracy": _rate(cluster_coherence),
        "severe_overmerge_count": sum(severe_overmerges),
        "source_grounded_rate": _rate(source_grounded),
        "syntheses_supported": sum(supported),
        "syntheses_reviewed": len(supported),
        "note_current": {
            criterion: {
                "passed": sum(results),
                "reviewed": len(results),
                "rate": _rate(results),
            }
            for criterion, results in note_metrics.items()
        },
        "metadata_only_non_pretense": {
            "passed": sum(metadata_only_non_pretense),
            "reviewed": len(metadata_only_non_pretense),
            "rate": _rate(metadata_only_non_pretense),
        },
    }
    checks = {
        "zero_material_errors": material_errors == 0,
        "overall_pass_rate_at_least_0_90": metrics["overall_pass_rate"] >= 0.90,
        "type_direction_accuracy_at_least_0_90": metrics["type_direction_accuracy"]
        >= 0.90,
        "role_accuracy_at_least_0_90": metrics["role_accuracy"] >= 0.90,
        "rejected_unclustered_accuracy_at_least_0_90": metrics[
            "rejected_unclustered_accuracy"
        ]
        >= 0.90,
        "cluster_coherence_at_least_0_90": metrics["cluster_coherence_accuracy"]
        >= 0.90,
        "no_severe_overmerges": metrics["severe_overmerge_count"] == 0,
        "source_grounded_rate_at_least_0_90": metrics["source_grounded_rate"] >= 0.90,
        "all_audited_syntheses_supported": bool(supported) and all(supported),
    }
    if packet["mode"] == "exhaustive40":
        checks.update(
            {
                "relationship_point_accuracy_at_least_0_90": _rate(relation_correct)
                >= 0.90,
                "membership_point_accuracy_at_least_0_90": _rate(membership_correct)
                >= 0.90,
                "note_equal_or_better_rate_at_least_0_90": bool(
                    note_metrics["equal_or_better"]
                )
                and _rate(note_metrics["equal_or_better"]) >= 0.90,
                "no_materially_worse_notes": not any(note_metrics["materially_worse"]),
                "all_note_identities_correct": all(note_metrics["identity_correct"]),
                "all_note_custody_links_correct": all(
                    note_metrics["custody_link_correct"]
                ),
                "all_current_note_statuses_correct": bool(
                    note_metrics["status_correct"]
                )
                and all(note_metrics["status_correct"]),
                "all_metadata_only_notes_non_pretending": bool(
                    metadata_only_non_pretense
                )
                and all(metadata_only_non_pretense),
                "all_note_locators_supported": all(note_metrics["locators_supported"]),
                "no_unsupported_note_claims": all(
                    note_metrics["unsupported_claims_absent"]
                ),
                "no_false_note_quotations": all(
                    note_metrics["false_quotations_absent"]
                ),
            }
        )
    else:
        checks.update(
            {
                "relationship_wilson_lower_at_least_0_80": metrics[
                    "relationship_correctness"
                ]["wilson_95_lower"]
                >= 0.80,
                "membership_wilson_lower_at_least_0_80": metrics[
                    "membership_correctness"
                ]["wilson_95_lower"]
                >= 0.80,
            }
        )
    report = {
        "score_schema_version": "1",
        "status": "passed" if all(checks.values()) else "failed",
        "evidence_status": "autonomous_provisional",
        "never_production_input": True,
        "provider_calls": 0,
        "mode": packet["mode"],
        "packet_sha256": sha256_file(packet_path),
        "review_sha256": sha256_file(review_path),
        "reviewers": [
            {
                "reviewer_task_id": task_id,
                "model": model,
                "reasoning_effort": effort,
            }
            for task_id, model, effort in sorted({
                (
                    str(judgment["reviewer_task_id"]),
                    str(judgment["model"]),
                    str(judgment["reasoning_effort"]),
                )
                for judgment in judgments
            })
        ],
        "judgment_sha256": {
            str(judgment["review_id"]): str(judgment["judgment_sha256"])
            for judgment in sorted(judgments, key=lambda value: str(value["review_id"]))
        },
        "bindings_sha256": (
            sha256_file(bindings_path) if bindings_path is not None else None
        ),
        "thresholds": {
            "material_errors": 0,
            "overall_pass_rate": 0.90,
            "exhaustive40_relationship_membership_point_accuracy": 0.90,
            "stratified500_relationship_membership_wilson_95_lower": 0.80,
            "type_direction_accuracy": 0.90,
            "role_accuracy": 0.90,
            "rejected_unclustered_accuracy": 0.90,
            "cluster_coherence_accuracy": 0.90,
            "source_grounded_rate": 0.90,
            "severe_overmerges": 0,
            "all_audited_syntheses_supported": True,
        },
        "metrics": metrics,
        "checks": checks,
    }
    write_yaml(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--workspace", type=Path, required=True)
    prepare_parser.add_argument("--mode", choices=sorted(_MODES), required=True)
    prepare_parser.add_argument("--packet", type=Path, required=True)
    prepare_parser.add_argument("--baseline-workspace", type=Path)
    prepare_parser.add_argument("--baseline-manifest", type=Path)
    prepare_parser.add_argument("--custody-manifest", type=Path)
    prepare_parser.add_argument("--bindings", type=Path)
    prepare_parser.add_argument("--source-manifest", type=Path)
    score_parser = commands.add_parser("score")
    score_parser.add_argument("--workspace", type=Path, required=True)
    score_parser.add_argument("--packet", type=Path, required=True)
    score_parser.add_argument("--review", type=Path, required=True)
    score_parser.add_argument("--report", type=Path, required=True)
    score_parser.add_argument("--bindings", type=Path)
    package_parser = commands.add_parser("package")
    package_parser.add_argument("--repository", type=Path, default=_REPOSITORY_ROOT)
    package_parser.add_argument("--sdist", type=Path, required=True)
    package_parser.add_argument("--wheel", type=Path, required=True)
    package_parser.add_argument("--report", type=Path, required=True)
    package_parser.add_argument("--base-ref", default="origin/main")
    package_parser.add_argument("--artifact", action="append", type=Path, default=[])
    package_parser.add_argument("--private-denylist", type=Path)
    package_parser.add_argument("--private-denylist-sha256", default="")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(
            args.workspace,
            args.mode,
            args.packet,
            baseline_workspace=args.baseline_workspace,
            baseline_manifest_path=args.baseline_manifest,
            custody_manifest_path=args.custody_manifest,
            bindings_path=args.bindings,
            source_manifest_path=args.source_manifest,
        )
        summary = {
            "status": result["evidence_status"],
            "mode": result["mode"],
            "review_rows": result["selection_counts"]["total"],
            "provider_calls": 0,
        }
    elif args.command == "score":
        result = score(
            args.workspace,
            args.packet,
            args.review,
            args.report,
            bindings_path=args.bindings,
        )
        summary = {
            "status": result["status"],
            "mode": result["mode"],
            "provider_calls": 0,
        }
    else:
        result = package_audit(
            args.repository,
            args.sdist,
            args.wheel,
            args.report,
            base_ref=args.base_ref,
            artifacts=args.artifact,
            private_denylist_path=args.private_denylist,
            private_denylist_sha256=args.private_denylist_sha256,
        )
        summary = {
            "status": result["status"],
            "distribution": result["distribution"],
            "version": result["version"],
            "provider_calls": 0,
        }
    print(json.dumps(summary, sort_keys=True))
    return 2 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
