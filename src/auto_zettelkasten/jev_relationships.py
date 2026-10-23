"""Deterministic, private-copy-only acceptance of supplied Jev pair decisions.

This module performs no inference or Zotero I/O and never changes source notes.
Provider request caching remains separate from the canonical pair decision state.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from .files import atomic_write_text, read_yaml
from .jev_billing import BATCH_TEMPLATE_VERSION
from .jev_decisions import MODEL_ID, TEMPLATE_VERSION, DecisionRequest, Endpoint, EvidencePassage, _relationship_options, build_payload
from .notes import parse_atomic_note
from .profiles import profile_sidecar_path
from .relationships import (
    JEV_RELATIONSHIP_CONTRACT, RECIPROCAL_RELATION_TYPES, canonical_pair,
    persist_relationship_registry, profile_content_hash, profile_row, stable_hash,
)


CURRENT_POLICY_VERSION = "jev-private-winner-only-v1"
POLICY_VERSION = CURRENT_POLICY_VERSION
LEGACY_POLICY_VERSION = "jev-private-policy-v1"
READABLE_POLICY_VERSIONS = frozenset({CURRENT_POLICY_VERSION, LEGACY_POLICY_VERSION})
READABLE_SOURCE_TEMPLATE_VERSIONS = frozenset({TEMPLATE_VERSION, "relationship-decision-v2"})
READABLE_BATCH_TEMPLATE_VERSIONS = frozenset({BATCH_TEMPLATE_VERSION, "relationship-batch-v1"})
MARKER_RELATIVE_PATH = ".auto-zettelkasten/jev-private-copy.json"
MAX_NOTE_BYTES = 2 * 1024 * 1024
_RELATIONS = {"supports", "undermines", "qualifies", "extends", "complements", "contrasts"}


@dataclass(frozen=True)
class JevAcceptancePolicy:
    confidence_floor: float = 0.0
    probability_margin: float = 0.0
    identity: str = POLICY_VERSION

    def validate(self) -> None:
        if self.identity not in READABLE_POLICY_VERSIONS:
            raise ValueError("unsupported Jev acceptance policy")
        if not _probability(self.confidence_floor) or not _probability(self.probability_margin):
            raise ValueError("Jev acceptance cutoffs must be probabilities")
        if self.identity == CURRENT_POLICY_VERSION:
            if self.confidence_floor != 0 or self.probability_margin != 0:
                raise ValueError("winner-only policy cannot include numeric acceptance cutoffs")
        elif self.confidence_floor < 0.80 or self.probability_margin < 0.15:
            raise ValueError("historical Jev thresholds cannot be weakened under their recorded identity")


def _copy(value: Any) -> Any:
    # Independent objects avoid YAML aliases in public graph evidence metadata.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _probability(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1


def _replayable_input(value: Any) -> Any:
    if type(value) is float and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: _replayable_input(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_replayable_input(item) for item in value]
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _wire_hash(request: DecisionRequest) -> str:
    return _hash(json.dumps(build_payload(request), ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _inside(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    if not candidate.resolve().is_relative_to(root) or any(part.is_symlink() for part in [candidate, *candidate.parents] if part.is_relative_to(root)):
        raise ValueError("Jev dependency must be a nonsymlink inside the workspace")
    return candidate


def _read_note(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(MAX_NOTE_BYTES + 1)
    if len(raw) > MAX_NOTE_BYTES:
        raise ValueError("Jev note dependency exceeds the byte limit")
    return raw.decode("utf-8")


def _profile_rows(profiles: Sequence[Any], *, source_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    rows = {}
    for profile in profiles:
        row = profile_row(profile)
        sid = str(row.get("source_id") or "")
        if source_ids is not None and sid not in source_ids:
            continue
        if not sid or sid in rows:
            raise ValueError("Jev profiles require unique source identities")
        rows[sid] = row
    return rows


def bind_jev_request(
    workspace: Path | str, request: DecisionRequest, *, profiles: Sequence[Any] = (),
    note_paths: Mapping[str, str | Path] | None = None,
) -> dict[str, dict[str, Any]]:
    """Bind a prepared pair to actual unchanged local notes and current profiles."""
    request.validate()
    root = Path(workspace).expanduser().resolve(strict=True)
    rows = _profile_rows(profiles, source_ids={request.endpoint_a.source_id, request.endpoint_b.source_id})
    result = {}
    for endpoint in (request.endpoint_a, request.endpoint_b):
        row = rows.get(endpoint.source_id, {})
        if row and row.get("note_id") != endpoint.note_id:
            raise ValueError("Jev profile note identity differs from the pair")
        context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
        metadata_path = _inside(root, Path("11_state/note_metadata") / f"{endpoint.note_id}.yml")
        metadata = read_yaml(metadata_path, {})
        if not isinstance(metadata, Mapping):
            raise ValueError("invalid Jev note metadata")
        location = (note_paths or {}).get(endpoint.source_id) or context.get("note_path") or row.get("note_path") or metadata.get("note_path")
        if not location:
            locations = {str(p.locator.get("note_relative_path") or "") for p in endpoint.passages}
            locations.discard("")
            location = next(iter(locations)) if len(locations) == 1 else ""
        if not location:
            raise ValueError("Jev pair has no canonical note path")
        path = _inside(root, location)
        text = _read_note(path)
        front, _ = parse_atomic_note(text)
        stored = metadata.get("frontmatter") or {}
        if not isinstance(stored, Mapping):
            raise ValueError("invalid Jev note identity metadata")
        for field, expected in (("source_id", endpoint.source_id), ("note_id", endpoint.note_id)):
            actual = front.get(field, stored.get(field))
            if actual != expected:
                raise ValueError("Jev canonical note identity mismatch")
        if any(p.text not in text for p in endpoint.passages):
            raise ValueError("Jev evidence text does not occur in its canonical note")
        if not row:
            raise ValueError("Jev binding requires a current canonical profile")
        binding = {"source_id": endpoint.source_id, "note_id": endpoint.note_id,
                   "note_path": str(path.relative_to(root)), "note_sha256": _hash(text),
                   "profile_hash": profile_content_hash(row)}
        profile_path = _inside(root, profile_sidecar_path(Path("02_source_memory/profiles"), endpoint.note_id))
        if profile_path.is_file():
            binding.update(profile_path=str(profile_path.relative_to(root)), profile_file_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest())
        result[endpoint.source_id] = binding
    return result


def _attribution_problem(endpoint: Any) -> bool:
    for field in ("attribution_status", "ownership_status", "note_source_identity"):
        value = str(endpoint.ownership.get(field) or "").casefold()
        if any(term in value for term in ("unresolved", "mismatch", "ambiguous", "conflict")):
            return True
    return any(bool(endpoint.ownership.get(field)) for field in ("attribution_warning", "ownership_warning", "author_attribution_unresolved"))


def evaluate_jev_decision(
    artifact: Mapping[str, Any], request: DecisionRequest, *, profiles: Sequence[Any] = (),
    policy: JevAcceptancePolicy | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate one saved pair without model calls or filesystem mutation."""
    policy = policy or JevAcceptancePolicy()
    policy.validate()
    request.validate()
    if isinstance(artifact.get("decision"), Mapping):
        artifact = artifact["decision"]
    endpoints = {"A": request.endpoint_a, "B": request.endpoint_b}
    pair = canonical_pair(request.endpoint_a.source_id, request.endpoint_b.source_id)
    provenance = artifact.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    rows = _profile_rows(profiles, source_ids=set(pair))
    current = _copy({sid: value for sid, value in (bindings or {}).items() if sid in pair})
    record = {
        "source_id": pair[0], "target_source_id": pair[1], "model": MODEL_ID, "provider": "typesafe",
        "output_contract": JEV_RELATIONSHIP_CONTRACT, "decision_schema_version": "1",
        "policy_version": policy.identity, "relationship_policy_identity": stable_hash(asdict(policy)),
        "template_version": str(provenance.get("template_version") or ""),
        "source_template_version": str(provenance.get("source_template_version") or request.template_version),
        "prompt_version": str(provenance.get("template_version") or ""),
        "pair_request_sha256": _wire_hash(request), "bindings": current,
        "batch_context_hash": str(artifact.get("batch_context_hash") or provenance.get("batch_context_sha256") or provenance.get("batch_context_hash") or provenance.get("request_payload_sha256") or ""),
        "cache_key": str(artifact.get("cache_key") or provenance.get("cache_key") or ""),
    }
    record["decision_key"] = stable_hash({key: record[key] for key in (
        "pair_request_sha256", "model", "template_version", "relationship_policy_identity", "bindings")})

    def finish(status: str, *reasons: str) -> dict[str, Any]:
        record.update(decision_status=status, status=status, policy_reasons=list(reasons),
                      verification_status="policy_accepted" if status in {"accepted", "no_relationship"} else "not_accepted")
        return {"status": status, "reasons": list(reasons), "decision_key": record["decision_key"], "record": _copy(record),
                "evaluation_input": {"request": _copy(asdict(request)), "artifact": _copy(_replayable_input(artifact)),
                                     "profiles": _copy(list(rows.values())), "policy": asdict(policy)}}

    if provenance.get("model") != MODEL_ID or provenance.get("provider") != "typesafe":
        return finish("rejected", "unpinned_provider_or_model")
    if (record["template_version"] not in {TEMPLATE_VERSION, BATCH_TEMPLATE_VERSION}
            or record["source_template_version"] != TEMPLATE_VERSION
            or (record["template_version"] == BATCH_TEMPLATE_VERSION
                and provenance.get("source_template_version") != TEMPLATE_VERSION)):
        return finish("rejected", "unsupported_decision_template")
    expected_hash = record["pair_request_sha256"]
    supplied_hash = provenance.get("pair_request_sha256") or artifact.get("pair_request_sha256")
    if supplied_hash is None:
        supplied_hash = provenance.get("request_payload_sha256")
    if supplied_hash != expected_hash:
        return finish("rejected", "pair_request_changed")
    declared = artifact.get("endpoints")
    if not isinstance(declared, Mapping):
        return finish("rejected", "missing_endpoint_bindings")
    for side, endpoint in endpoints.items():
        value = declared.get(side)
        if not isinstance(value, Mapping) or any(value.get(key) != expected for key, expected in (
            ("source_id", endpoint.source_id), ("note_id", endpoint.note_id),
            ("coverage", endpoint.coverage), ("ownership", endpoint.ownership),
        )):
            return finish("rejected", "endpoint_binding_mismatch")
    answers = artifact.get("answers")
    answer = answers.get("relationship") if isinstance(answers, Mapping) else None
    if not isinstance(answer, Mapping):
        return finish("rejected", "missing_relationship_answer")
    options = _relationship_options(request)
    probabilities = answer.get("probabilities")
    if (not isinstance(probabilities, Mapping) or set(probabilities) != set(options)
            or any(not _probability(value) for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > 0.02 or not _probability(answer.get("confidence"))):
        return finish("rejected", "invalid_probabilities")
    ordered = sorted(probabilities, key=lambda key: (-probabilities[key], key))
    top, second = float(probabilities[ordered[0]]), float(probabilities[ordered[1]])
    calculated = max(0.0, (top - 1 / len(options)) / (1 - 1 / len(options)))
    record.update(confidence=float(answer["confidence"]), calculated_confidence=calculated,
                  top_probability=top, probability_margin=top - second, probabilities=dict(probabilities))
    if abs(float(answer["confidence"]) - calculated) > 0.02:
        return finish("rejected", "confidence_distribution_mismatch")
    if abs(top - second) <= 1e-12:
        return finish("deferred", "tied_winning_options")
    choice = answer.get("choice")
    if choice != ordered[0]:
        return finish("rejected", "provider_choice_not_local_winner")
    option = options[choice]
    selected = artifact.get("selected_evidence")
    expected_evidence = []
    if option["indices"] is not None:
        for side, index in zip(("A", "B"), option["indices"]):
            endpoint = endpoints[side]
            passage = endpoint.passages[index]
            expected_evidence.append({"endpoint": side, "source_id": endpoint.source_id, "note_id": endpoint.note_id,
                                      "passage_id": passage.passage_id, "text": passage.text, "locator": dict(passage.locator),
                                      "attribution": passage.attribution, "content_sha256": passage.content_sha256})
    if not isinstance(selected, list) or len(selected) != len(expected_evidence):
        return finish("rejected", "invalid_selected_evidence")
    for actual, expected in zip(selected, expected_evidence):
        if not isinstance(actual, Mapping) or any(actual.get(key) != value for key, value in expected.items() if key not in {"source_id", "note_id"}):
            return finish("rejected", "selected_evidence_not_supplied")
    relation = artifact.get("relationship") or {}
    if not isinstance(relation, Mapping) or relation.get("relation_type") != option["relation_type"] or relation.get("direction") != option["direction"]:
        return finish("rejected", "relationship_binding_mismatch")
    record.update(selected_evidence=expected_evidence, direction=option["direction"], relation_type=option["relation_type"])
    record["choice"] = choice
    for endpoint in endpoints.values():
        row = rows.get(endpoint.source_id)
        binding = current.get(endpoint.source_id)
        if row and row.get("note_id") != endpoint.note_id:
            return finish("rejected", "profile_identity_mismatch")
        if not isinstance(binding, Mapping) or binding.get("note_id") != endpoint.note_id or not binding.get("note_sha256") or not binding.get("profile_hash"):
            return finish("deferred", "missing_current_source_binding")
        if row and profile_content_hash(row) != binding["profile_hash"]:
            return finish("deferred", "stale_profile_binding")
        if _attribution_problem(endpoint):
            return finish("deferred", "unresolved_attribution")
    source, target = endpoints["A"], endpoints["B"]
    if option["direction"] == "B_to_A":
        source, target = target, source
    record.update(source_id=source.source_id, target_source_id=target.source_id,
                  source_note_id=source.note_id, target_note_id=target.note_id,
                  source_profile_hash=current[source.source_id]["profile_hash"],
                  target_profile_hash=current[target.source_id]["profile_hash"],
                  source_note_sha256=current[source.source_id]["note_sha256"],
                  target_note_sha256=current[target.source_id]["note_sha256"])
    if choice == "insufficient_evidence":
        return finish("deferred", "insufficient_evidence")
    if choice == "topical_only_or_none":
        record["reason"] = "The supplied notes establish no substantive relationship."
        return finish("no_relationship")
    if option["relation_type"] not in _RELATIONS:
        return finish("rejected", "unsupported_relationship")
    if policy.identity == LEGACY_POLICY_VERSION:
        if min(float(answer["confidence"]), calculated) + 1e-12 < policy.confidence_floor:
            return finish("deferred", "below_confidence_floor")
        if top - second + 1e-12 < policy.probability_margin:
            return finish("deferred", "below_probability_margin")
    for endpoint in endpoints.values():
        scope = str(endpoint.coverage.get("source_scope") or endpoint.coverage.get("status") or "").casefold()
        if "metadata" in scope or endpoint.coverage.get("evidence_eligibility") in {"context_only", "metadata_only"}:
            return finish("deferred", "insufficient_source_coverage")
    by_side = {row["source_id"]: row for row in expected_evidence}
    relation_id = "jev-" + stable_hash([record["decision_key"], option["relation_type"], source.source_id, target.source_id])[:24]
    source_title = str(rows.get(source.source_id, {}).get("title") or source.note_id)
    target_title = str(rows.get(target.source_id, {}).get("title") or target.note_id)
    record.update(relation_id=relation_id, connection_id=relation_id, active=True,
                  provenance="probabilistic_relationship_jev", inferred=True, strength=110,
                  reciprocal_type=RECIPROCAL_RELATION_TYPES[option["relation_type"]],
                  reason=f"{source_title} {option['relation_type']} {target_title} using the supplied note evidence.",
                  source_evidence=_copy(by_side[source.source_id]), target_evidence=_copy(by_side[target.source_id]),
                  evidence_basis="note_summary" if all(value["locator"].get("kind") == "note_lines" for value in expected_evidence) else "supplied_passages")
    return finish("accepted")


def initialize_private_relationship_workspace(workspace: Path | str, *, original_workspace: Path | str) -> dict[str, Any]:
    """Explicitly mark a copied workspace after verifying its copied source notes."""
    root = Path(workspace).expanduser().resolve(strict=True)
    original = Path(original_workspace).expanduser().resolve(strict=True)
    if root == original or root.is_relative_to(original) or original.is_relative_to(root):
        raise ValueError("private relationship workspace must be separate from the original")
    if any((parent / ".git").exists() for parent in [root, *root.parents]):
        raise ValueError("private relationship workspace cannot be inside a Git checkout")
    notes = root / "02_source_memory/notes"
    copies = list(notes.glob("*.md")) if notes.is_dir() else []
    if not copies or len(copies) > 100_000:
        raise ValueError("private copy must contain a bounded set of existing source notes")
    hashes = {}
    for path in copies:
        relative = path.relative_to(root)
        source = _inside(original, relative)
        text = _read_note(_inside(root, relative))
        if text != _read_note(source):
            raise ValueError("private copy source notes differ from the original")
        hashes[str(relative)] = _hash(text)
    marker = {"schema": "jev-private-copy-v1", "workspace": str(root), "original_workspace": str(original),
              "original_note_hashes": hashes}
    target = _inside(root, MARKER_RELATIVE_PATH)
    if target.exists() and json.loads(target.read_text()) != marker:
        raise ValueError("private-copy marker already binds a different source snapshot")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(marker, indent=2, sort_keys=True) + "\n")
    return marker


def _private_marker(root: Path) -> Mapping[str, Any]:
    path = _inside(root, MARKER_RELATIVE_PATH)
    marker = json.loads(path.read_text())
    if (not isinstance(marker, Mapping) or marker.get("schema") != "jev-private-copy-v1"
            or marker.get("workspace") != str(root) or not marker.get("original_workspace")):
        raise ValueError("Jev application requires a path-bound private-copy marker")
    original = Path(marker["original_workspace"]).resolve()
    if root == original or root.is_relative_to(original) or original.is_relative_to(root):
        raise ValueError("private-copy marker points at an original workspace")
    if any((parent / ".git").exists() for parent in [root, *root.parents]):
        raise ValueError("private-copy marker cannot authorize a Git checkout")
    return marker


def validate_private_relationship_workspace(workspace: Path | str) -> Mapping[str, Any]:
    """Validate opt-in private-copy scope before any provider dispatch."""
    root = Path(workspace).expanduser().resolve(strict=True)
    try:
        return _private_marker(root)
    except FileNotFoundError as exc:
        raise ValueError("Jev application requires an explicitly initialized private copy") from exc


def _binding_fresh(root: Path, binding: Mapping[str, Any], *, evidence: Sequence[Mapping[str, Any]] = ()) -> bool:
    try:
        text = _read_note(_inside(root, binding["note_path"]))
        if _hash(text) != binding.get("note_sha256"):
            return False
        if any(item.get("text") not in text for item in evidence):
            return False
        if binding.get("profile_path"):
            path = _inside(root, binding["profile_path"])
            if _hash(_read_note(path)) != binding.get("profile_file_sha256"):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False


def _trusted_decision_record(record: Mapping[str, Any]) -> bool:
    template = record.get("template_version")
    source_template = record.get("source_template_version")
    if source_template is None and template in READABLE_SOURCE_TEMPLATE_VERSIONS:
        source_template = template
    expected_source_template = {
        BATCH_TEMPLATE_VERSION: TEMPLATE_VERSION,
        "relationship-batch-v1": "relationship-decision-v2",
    }.get(template, template)
    return bool(
        record.get("output_contract") == JEV_RELATIONSHIP_CONTRACT
        and record.get("decision_schema_version") == "1"
        and record.get("provider") == "typesafe" and record.get("model") == MODEL_ID
        and record.get("policy_version") in READABLE_POLICY_VERSIONS
        and template in READABLE_SOURCE_TEMPLATE_VERSIONS | READABLE_BATCH_TEMPLATE_VERSIONS
        and record.get("prompt_version") == template
        and source_template in READABLE_SOURCE_TEMPLATE_VERSIONS
        and source_template == expected_source_template
    )


def jev_relationship_is_fresh(
    workspace: Path | str, row: Mapping[str, Any], *, memo: dict[str, bool] | None = None,
) -> bool:
    """Read-only freshness for a canonical accepted Jev edge.

    Reads at most two source notes and two optional profile sidecars, each
    bounded to MAX_NOTE_BYTES. Memoization is scoped to one graph/recall read;
    callers must discard the memo before a later refresh.
    """
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
        if (not isinstance(row, Mapping) or not _trusted_decision_record(row)
                or row.get("decision_status") != "accepted" or row.get("active", True) is not True
                or row.get("verification_status") != "policy_accepted"
                or row.get("refresh_pending") or row.get("reconciliation_pending")):
            return False
        pair = (row.get("source_id"), row.get("target_source_id"))
        bindings = row.get("bindings")
        selected = row.get("selected_evidence")
        if (not all(isinstance(sid, str) and sid for sid in pair) or pair[0] == pair[1]
                or not isinstance(bindings, Mapping) or set(bindings) != set(pair)
                or not isinstance(selected, list) or len(selected) != 2):
            return False
        evidence_by_source = {}
        for item in selected:
            if (not isinstance(item, Mapping) or item.get("source_id") not in bindings
                    or item["source_id"] in evidence_by_source or not isinstance(item.get("text"), str)
                    or not item["text"] or _hash(item["text"]) != item.get("content_sha256")):
                return False
            evidence_by_source[item["source_id"]] = item
        for sid, note_field, profile_field in zip(pair, ("source_note_id", "target_note_id"), ("source_profile_hash", "target_profile_hash")):
            binding = bindings[sid]
            evidence = evidence_by_source[sid]
            if (not isinstance(binding, Mapping) or binding.get("source_id") != sid
                    or binding.get("note_id") != row.get(note_field)
                    or evidence.get("note_id") != binding.get("note_id")
                    or binding.get("profile_hash") != row.get(profile_field)):
                return False
            key = stable_hash([str(root), binding, evidence])
            if memo is not None and key in memo:
                valid = memo[key]
            else:
                valid = _binding_fresh(root, binding, evidence=[evidence])
                if memo is not None:
                    memo[key] = valid
            if not valid:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False


def _reevaluate(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    """Do not let a caller promote or edit a previously evaluated record."""
    inputs = evaluation.get("evaluation_input")
    if not isinstance(inputs, Mapping):
        raise ValueError("Jev application requires the original frozen evaluation input")
    raw = dict(inputs.get("request") or {})
    try:
        for name in ("endpoint_a", "endpoint_b"):
            endpoint = dict(raw[name])
            endpoint["passages"] = tuple(EvidencePassage(**value) for value in endpoint["passages"])
            raw[name] = Endpoint(**endpoint)
        request = DecisionRequest(**raw)
        original_record = evaluation["record"]
        checked = evaluate_jev_decision(inputs["artifact"], request, profiles=inputs.get("profiles", []),
                                       policy=JevAcceptancePolicy(**inputs["policy"]), bindings=original_record.get("bindings"))
    except (TypeError, KeyError) as exc:
        raise ValueError("invalid frozen Jev evaluation input") from exc
    if checked["record"] != original_record or checked["status"] != evaluation.get("status"):
        raise ValueError("Jev evaluation was modified after deterministic validation")
    return checked


@contextmanager
def _registry_lock(root: Path):
    path = _inside(root, ".auto-zettelkasten/jev-registry.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        yield
    finally:
        os.close(descriptor)
        path.unlink()


def apply_jev_decisions(workspace: Path | str, evaluations: Sequence[Mapping[str, Any]], *, apply: bool = False) -> dict[str, Any]:
    """Preview or append validated decisions to the existing canonical registry."""
    if type(apply) is not bool or isinstance(evaluations, (str, bytes)) or len(evaluations) > 256:
        raise ValueError("Jev application requires an explicit boolean and bounded decisions")
    root = Path(workspace).expanduser().resolve(strict=True)
    values = []
    for evaluation in evaluations:
        if not isinstance(evaluation, Mapping) or not isinstance(evaluation.get("record"), Mapping):
            raise ValueError("Jev evaluation is missing its policy record")
        value = _copy(_reevaluate(evaluation)["record"])
        if value.get("output_contract") != JEV_RELATIONSHIP_CONTRACT or value.get("policy_version") != POLICY_VERSION:
            raise ValueError("unsupported Jev evaluation contract")
        if value.get("status") != evaluation.get("status"):
            raise ValueError("Jev evaluation status mismatch")
        if not value.get("bindings") or not all(_binding_fresh(root, item) for item in value["bindings"].values()):
            value.update(status="deferred", decision_status="deferred", verification_status="not_accepted", policy_reasons=["stale_note_or_profile"])
        for evidence in value.get("selected_evidence", []):
            binding = value.get("bindings", {}).get(evidence["source_id"], {})
            try:
                actual = _read_note(_inside(root, binding["note_path"]))
                valid = evidence["text"] in actual and _hash(evidence["text"]) == evidence["content_sha256"]
            except (OSError, ValueError, KeyError, UnicodeError):
                valid = False
            if not valid:
                value.update(status="deferred", decision_status="deferred", verification_status="not_accepted", policy_reasons=["stale_evidence"])
        values.append(value)
    counts = {status: sum(row["status"] == status for row in values) for status in ("accepted", "no_relationship", "deferred", "rejected")}
    result = {"status": "preview", "applied": False, "counts": counts, "records": values, "provider_calls": 0}
    if not apply:
        return result
    try:
        _private_marker(root)
    except FileNotFoundError as exc:
        raise ValueError("Jev application requires an explicitly initialized private copy") from exc
    registry_path = _inside(root, "02_source_memory/indexes/typed_links.yml")
    _inside(root, "02_source_memory/indexes/typed_note_links.yml")
    with _registry_lock(root):
        prior = read_yaml(registry_path, {}) or {}
        prior_relations = prior.get("relations", prior.get("links", []))
        protected_pairs = {canonical_pair(str(row.get("source_id") or ""), str(row.get("target_source_id") or ""))
                           for row in prior_relations if isinstance(row, Mapping) and row.get("active", True)
                           and row.get("decision_status") == "accepted" and row.get("output_contract") != JEV_RELATIONSHIP_CONTRACT}
        accepted, negative, retired = [], [], []
        for row in values:
            pair = canonical_pair(row["source_id"], row["target_source_id"])
            if pair in protected_pairs:
                row.update(status="deferred", decision_status="deferred", verification_status="not_accepted", policy_reasons=["existing_relationship_preserved"])
            if row["status"] == "accepted":
                accepted.append(row)
            elif row["status"] == "no_relationship":
                negative.append(row)
            for existing in prior_relations:
                if (isinstance(existing, Mapping) and existing.get("output_contract") == JEV_RELATIONSHIP_CONTRACT
                        and canonical_pair(str(existing.get("source_id") or ""), str(existing.get("target_source_id") or "")) == pair
                        and (existing.get("bindings") != row.get("bindings")
                             or not all(_binding_fresh(root, item) for item in (existing.get("bindings") or {}).values()))):
                    retired.append(str(existing.get("relation_id") or ""))
        registry = persist_relationship_registry(root, structural_relations=[], accepted_relations=_copy(accepted),
                                                no_relationship_decisions=_copy(negative), preserve_unmentioned_structural=True,
                                                jev_pair_decisions=_copy(values), jev_retired_relation_ids=retired,
                                                preserve_existing_semantic_rows=True)
    result.update(status="applied_private_copy", applied=True, registry=registry,
                  counts={status: sum(row["status"] == status for row in values) for status in counts})
    return result


def reusable_jev_pairs(
    workspace: Path | str, *, profiles: Sequence[Any] = (),
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    requests: Sequence[DecisionRequest] = (),
    policy: JevAcceptancePolicy | None = None,
) -> set[tuple[str, str]]:
    """Return pairs with a current local accepted, negative or deferred decision."""
    root = Path(workspace).expanduser().resolve(strict=True)
    registry = read_yaml(_inside(root, "02_source_memory/indexes/typed_links.yml"), {}) or {}
    rows = _profile_rows(profiles)
    current = bindings or {}
    policy = policy or JevAcceptancePolicy()
    policy.validate()
    policy_identity = stable_hash(asdict(policy))
    request_hashes = {canonical_pair(request.endpoint_a.source_id, request.endpoint_b.source_id): _wire_hash(request) for request in requests}
    pairs = set()
    freshness: dict[str, bool] = {}
    for record in registry.get("jev_pair_decisions", []) or []:
        if (not isinstance(record, Mapping) or not _trusted_decision_record(record)
                or record.get("relationship_policy_identity") != policy_identity
                or record.get("source_template_version", record.get("template_version")) != TEMPLATE_VERSION
                or record.get("template_version") not in {TEMPLATE_VERSION, BATCH_TEMPLATE_VERSION}
                or record.get("model") != MODEL_ID or record.get("status") not in {"accepted", "no_relationship", "deferred"}):
            continue
        pair = canonical_pair(str(record["source_id"]), str(record["target_source_id"]))
        all_saved = record.get("bindings") or {}
        if not isinstance(all_saved, Mapping):
            continue
        saved = {sid: all_saved[sid] for sid in pair if isinstance(all_saved.get(sid), Mapping)}
        if len(saved) != 2:
            continue
        if pair in request_hashes and request_hashes[pair] != record.get("pair_request_sha256"):
            continue
        fresh = True
        for sid, binding in saved.items():
            supplied = current.get(sid)
            if supplied:
                # Lean catalogue rows already carry current canonical profile
                # hashes. The raw note hash is optional; validate saved note
                # bytes locally rather than preloading every candidate note.
                keys = ("note_id", "profile_hash", *(key for key in ("note_sha256", "note_path") if key in supplied))
                if any(supplied.get(key) != binding.get(key) for key in keys):
                    fresh = False
            elif sid in rows and profile_content_hash(rows[sid]) != binding.get("profile_hash"):
                fresh = False
            if not fresh:
                break
            identity = stable_hash(binding)
            if identity not in freshness:
                freshness[identity] = _binding_fresh(root, binding)
            if not freshness[identity]:
                fresh = False
        if fresh:
            pairs.add(pair)
    return pairs
