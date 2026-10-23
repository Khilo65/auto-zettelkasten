"""Offline CLI support for preparing reviewable Jev relationship requests.

This module deliberately has no provider dispatch path. It validates a bounded
pair packet, renders the pinned typed request through :mod:`jev_decisions`, and
stores a local review-required artifact.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .files import now_iso, write_json
from .workspace import resolve_workspace

MAX_INPUT_BYTES = 1 * 1024 * 1024
MAX_ENDPOINTS_TEXT_CHARS = 20_000
MAX_PASSAGES_PER_ENDPOINT = 4
MAX_PASSAGE_CHARS = 8_000
MAX_TOTAL_EVIDENCE_CHARS = 24_000
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _required_text(value: Any, label: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters")
    return value


def _read_packet(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read pair input: {exc}") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("pair input exceeds the 1 MiB limit")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("pair input must be bounded UTF-8 JSON") from exc
    packet = _object(value, "pair input")
    allowed = {"request_id", "workspace_id", "library_id", "left", "right", "options"}
    unknown = set(packet) - allowed
    if unknown:
        raise ValueError(f"unknown pair input fields: {', '.join(sorted(unknown))}")
    return packet


def _validate_endpoint(value: Any, side: str, *, source_ids: set[str]) -> dict[str, Any]:
    endpoint = _object(value, f"{side} endpoint")
    allowed = {"source_id", "note_id", "coverage", "ownership", "note_text", "evidence"}
    unknown = set(endpoint) - allowed
    if unknown:
        raise ValueError(f"unknown {side} endpoint fields: {', '.join(sorted(unknown))}")
    source_id = _required_text(endpoint.get("source_id"), f"{side}.source_id", limit=256)
    note_id = _required_text(endpoint.get("note_id"), f"{side}.note_id", limit=256)
    if source_id in source_ids:
        raise ValueError("left and right endpoints must have distinct source IDs")
    source_ids.add(source_id)
    coverage = _object(endpoint.get("coverage"), f"{side}.coverage")
    ownership = _object(endpoint.get("ownership"), f"{side}.ownership")
    note_text = _required_text(endpoint.get("note_text"), f"{side}.note_text", limit=MAX_ENDPOINTS_TEXT_CHARS)
    raw_passages = endpoint.get("evidence")
    if not isinstance(raw_passages, list) or not 1 <= len(raw_passages) <= MAX_PASSAGES_PER_ENDPOINT:
        raise ValueError(f"{side}.evidence must contain 1 to {MAX_PASSAGES_PER_ENDPOINT} passages")
    passages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_passages):
        passage = _object(raw, f"{side}.evidence[{index}]")
        allowed_passage = {"passage_id", "text", "locator", "attribution", "content_sha256"}
        unknown = set(passage) - allowed_passage
        if unknown:
            raise ValueError(f"unknown {side}.evidence[{index}] fields: {', '.join(sorted(unknown))}")
        passage_id = _required_text(passage.get("passage_id"), f"{side}.evidence[{index}].passage_id", limit=128)
        if passage_id == "none":
            raise ValueError("passage_id 'none' is reserved and cannot identify evidence")
        if passage_id in seen_ids:
            raise ValueError(f"duplicate {side} passage_id: {passage_id}")
        seen_ids.add(passage_id)
        text = _required_text(passage.get("text"), f"{side}.evidence[{index}].text", limit=MAX_PASSAGE_CHARS)
        locator = _object(passage.get("locator"), f"{side}.evidence[{index}].locator")
        attribution = _required_text(passage.get("attribution"), f"{side}.evidence[{index}].attribution", limit=256)
        digest = passage.get("content_sha256")
        actual_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != actual_digest or not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
            raise ValueError(f"{side}.evidence[{index}].content_sha256 does not match its exact text")
        passages.append({"passage_id": passage_id, "text": text,
                         "locator": locator, "attribution": attribution, "content_sha256": actual_digest})
    return {"source_id": source_id, "note_id": note_id, "coverage": coverage,
            "ownership": ownership, "note_text": note_text, "passages": passages}


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported rendered payload value: {type(value).__name__}")


def _construct(module: Any, name: str, payload: Mapping[str, Any]) -> Any:
    """Adapt JSON packet mappings to the transport module's frozen typed values."""
    kind = getattr(module, name)
    try:
        return kind(**dict(payload))
    except TypeError as exc:
        raise ValueError(f"transport {name} contract does not accept validated packet fields: {exc}") from exc


def prepare_review(workspace: Path | str, input_path: Path | str) -> dict[str, Any]:
    """Validate and render a packet, then persist a review-required artifact."""
    root = resolve_workspace(workspace)
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    packet = _read_packet(Path(input_path).expanduser())
    request_id = packet.get("request_id") or ""
    if request_id and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", request_id)):
        raise ValueError("request_id must contain 1 to 96 safe filename characters")
    workspace_id = _required_text(packet.get("workspace_id") or str(root), "workspace_id", limit=512)
    library_id = _required_text(packet.get("library_id") or "unspecified", "library_id", limit=256)
    source_ids: set[str] = set()
    left = _validate_endpoint(packet.get("left"), "left", source_ids=source_ids)
    right = _validate_endpoint(packet.get("right"), "right", source_ids=source_ids)
    evidence_size = sum(len(row["text"]) for endpoint in (left, right) for row in endpoint["passages"])
    if evidence_size > MAX_TOTAL_EVIDENCE_CHARS:
        raise ValueError("combined evidence exceeds the 24,000 character limit")
    options = packet.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("options must be a JSON object")

    # Delayed import keeps every non-Jev CLI command independent of this optional adapter.
    from . import jev_decisions

    endpoint_left = _construct(jev_decisions, "Endpoint", {
        **{key: left[key] for key in ("source_id", "note_id", "note_text", "coverage", "ownership")},
        "passages": tuple(_construct(jev_decisions, "EvidencePassage", row) for row in left["passages"]),
    })
    endpoint_right = _construct(jev_decisions, "Endpoint", {
        **{key: right[key] for key in ("source_id", "note_id", "note_text", "coverage", "ownership")},
        "passages": tuple(_construct(jev_decisions, "EvidencePassage", row) for row in right["passages"]),
    })
    request_fields = {
        "workspace_id": workspace_id,
        "library_id": library_id,
        "endpoint_a": endpoint_left,
        "endpoint_b": endpoint_right,
        "template_version": jev_decisions.TEMPLATE_VERSION,
        "request_options": dict(options),
    }
    request = _construct(jev_decisions, "DecisionRequest", request_fields)
    rendered = _jsonable(jev_decisions.build_payload(request))
    cache_key = jev_decisions.decision_cache_key(request)
    if not isinstance(cache_key, str) or not cache_key:
        raise ValueError("transport returned an invalid decision cache key")

    safe_id = request_id or hashlib.sha256(
        f"{workspace_id}\0{library_id}\0{left['source_id']}\0{right['source_id']}\0{cache_key}".encode("utf-8")
    ).hexdigest()[:24]
    output_dir = root / ".auto-zettelkasten" / "jev" / "reviews"
    if any(path.is_symlink() for path in (root / ".auto-zettelkasten", root / ".auto-zettelkasten" / "jev", output_dir)):
        raise ValueError("review output path cannot contain a symbolic link")
    if output_dir.exists() and not output_dir.resolve().is_relative_to(root):
        raise ValueError("review output directory must remain inside the workspace")
    output_path = output_dir / f"{safe_id}.json"
    if output_path.is_symlink():
        raise ValueError("review artifact path cannot be a symlink")
    artifact = {
        "schema": "jev-review-artifact-v1",
        "created_at": now_iso(),
        "status": "review_required",
        "mode": "dry_run",
        "provider_called": False,
        "decision": None,
        "workspace_id": workspace_id,
        "library_id": library_id,
        "request_id": safe_id,
        "cache_key": cache_key,
        "model": jev_decisions.MODEL_ID,
        "template_version": jev_decisions.TEMPLATE_VERSION,
        "endpoints": [left, right],
        "evidence": [*left["passages"], *right["passages"]],
        "rendered_payload": rendered,
    }
    if output_path.exists():
        raise ValueError("review artifact already exists; choose a new request_id to avoid overwriting it")
    private_dirs = (root / ".auto-zettelkasten", root / ".auto-zettelkasten" / "jev", output_dir)
    for directory in private_dirs:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError("review output directory must remain inside the workspace")
        os.chmod(directory, 0o700)
    write_json(output_path, artifact)
    os.chmod(output_path, 0o600)
    return {"status": "review_required", "mode": "dry_run", "provider_called": False,
            "artifact": str(output_path), "cache_key": cache_key,
            "model": artifact["model"], "template_version": artifact["template_version"]}
