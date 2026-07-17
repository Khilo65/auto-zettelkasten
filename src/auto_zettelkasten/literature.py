from __future__ import annotations

import json
import inspect
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .files import atomic_write_text, now_iso, read_yaml, safe_filename, sha256_text, slugify, write_yaml


ANALYTICAL_STATUSES = {"analytical_atomic_note", "verified_atomic_note", "analytical"}
LIMITED_STATUSES = {"abstract_only_atomic_note", "metadata_only_source_note", "fulltext_available", "limited"}
EVIDENCE_DIMENSIONS = (
    "theory",
    "mechanism",
    "method",
    "data",
    "case",
    "period",
    "outcome",
    "finding direction",
    "uncertainty",
    "limitations",
)
GAP_RULES = (
    "contradictory_findings",
    "untested_mechanism",
    "empirical_coverage",
    "methodological_concentration",
    "measurement_or_data",
    "boundary_condition",
    "replication",
    "cross_cluster_integration",
    "author_stated_gap",
)
LITERATURE_ALGORITHM_VERSION = "8"
CLUSTER_PROPOSAL_PROMPT_VERSION = "9"
CLUSTER_SYNTHESIS_PROMPT_VERSION = "3"
GAP_REASONING_PROMPT_VERSION = "5"

CLUSTER_SYNTHESIS_SECTIONS = (
    "central_findings",
    "agreements",
    "positions",
    "contradictions",
    "boundary_conditions",
    "methodological_fault_lines",
    "related_clusters",
    "source_roles",
)


class LiteratureSynthesisPartialError(RuntimeError):
    """A resumable literature-synthesis budget or deadline stop."""


class _CheckpointedReasonerCalls:
    """Budgeted, resumable provider calls for collection-level reasoning."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        reasoner: Any,
        request: Any,
        *,
        stage_callback: Callable[..., Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.reasoner = reasoner
        self.request = request
        request_values = _as_mapping(request) if request is not None else {}
        policy = request_values.get("literature_policy")
        self.max_calls = int(_policy_value(policy, "max_synthesis_calls", 24))
        self.deadline_seconds = float(_policy_value(policy, "literature_deadline_seconds", 1_800.0))
        self.root = workspace / "11_state" / "runs" / run_id / "literature" / "synthesis"
        self.stage_callback = stage_callback
        self.started = time.monotonic()
        self.provider_calls = 0
        self.checkpoint_hits = 0
        self.failures = 0
        self.synthesized_clusters = 0

    def __call__(
        self,
        stage: str,
        key: str,
        method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        method = getattr(self.reasoner, method_name, None)
        if not callable(method):
            return {}
        if time.monotonic() - self.started >= self.deadline_seconds:
            raise LiteratureSynthesisPartialError("literature_stage_deadline_reached")

        enriched_context = dict(context)
        if stage == "cluster_synthesis":
            enriched_context["atomic_notes"] = self._atomic_notes(profiles)
        dependency = {
            "stage": stage,
            "key": key,
            "method": method_name,
            "provider": str(getattr(self.reasoner, "name", "")),
            "model": str(getattr(self.reasoner, "model", "")),
            "source_set_id": str(_as_mapping(self.request).get("source_set_id") or ""),
            "profile_dependencies": [
                {
                    "source_id": str(_as_mapping(profile).get("source_id") or ""),
                    "note_hash": str(_as_mapping(profile).get("note_hash") or ""),
                    "dependency_hash": str(_as_mapping(profile).get("dependency_hash") or ""),
                }
                for profile in profiles
            ],
            "context": _checkpoint_dependency_context(
                enriched_context,
                sort_sequences=stage == "gap_adjudication",
            ),
            "policy": _as_mapping(_as_mapping(self.request).get("literature_policy")) if self.request is not None else {},
            "prompt_version": _synthesis_stage_prompt_version(stage),
            "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        }
        fingerprint = _stable_hash(dependency)
        dependency_component_hashes = {
            str(component): _stable_hash(value)
            for component, value in dependency.items()
        }
        dependency_context_hashes = {
            str(component): _stable_hash(value)
            for component, value in _as_mapping(dependency.get("context")).items()
        }
        dependency_context_item_hashes: dict[str, dict[str, str]] = {}
        for component, values in _as_mapping(dependency.get("context")).items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                continue
            item_hashes: dict[str, str] = {}
            for index, value in enumerate(values):
                if not isinstance(value, Mapping):
                    continue
                item = value
                identifier = str(
                    item.get("gap_id")
                    or item.get("cluster_id")
                    or item.get("source_id")
                    or index
                )
                item_hashes[identifier] = _stable_hash(value)
            if item_hashes:
                dependency_context_item_hashes[str(component)] = item_hashes
        compatible_fingerprints = {fingerprint}
        prior_algorithm_dependency = dict(dependency)
        prior_algorithm_dependency["algorithm_version"] = "6"
        compatible_fingerprints.add(_stable_hash(prior_algorithm_dependency))
        if stage in {"cluster_proposal", "cluster_synthesis"}:
            v04_dependency = dict(dependency)
            v04_dependency["algorithm_version"] = "7"
            v04_policy = dict(_as_mapping(v04_dependency.get("policy")))
            for field_name in (
                "weak_gap_handling",
                "cluster_gap_projection",
                "require_executable_gap_design",
            ):
                v04_policy.pop(field_name, None)
            v04_dependency["policy"] = v04_policy
            compatible_fingerprints.add(_stable_hash(v04_dependency))
        legacy_dependency = dict(dependency)
        legacy_dependency.pop("prompt_version", None)
        legacy_dependency["prompt_versions"] = {
            "cluster_proposal": CLUSTER_PROPOSAL_PROMPT_VERSION,
            "cluster_synthesis": CLUSTER_SYNTHESIS_PROMPT_VERSION,
            # v0.4.0 initially coupled every stage to gap prompt v1. Accept
            # that completed fingerprint once and upgrade it mechanically.
            "gap_reasoning": "1",
        }
        if stage in {"cluster_proposal", "cluster_synthesis"}:
            compatible_fingerprints.add(_stable_hash(legacy_dependency))
        path = self.root / safe_filename(stage) / f"{safe_filename(key, fallback='packet')}.yml"
        failure_path = self.root / "failures" / safe_filename(stage) / f"{safe_filename(key, fallback='packet')}.yml"
        history_root = self.root / "history" / safe_filename(stage) / safe_filename(key, fallback="packet")
        existing = read_yaml(path, {}) or {}
        matching_checkpoint: Mapping[str, Any] | None = None
        if isinstance(existing, Mapping) and existing.get("fingerprint") in compatible_fingerprints:
            matching_checkpoint = existing
        if matching_checkpoint is None:
            for compatible_fingerprint in sorted(compatible_fingerprints):
                historical = read_yaml(history_root / f"{compatible_fingerprint}.yml", {}) or {}
                if (
                    isinstance(historical, Mapping)
                    and historical.get("fingerprint") == compatible_fingerprint
                    and isinstance(historical.get("response"), Mapping)
                ):
                    matching_checkpoint = historical
                    break
        if matching_checkpoint is not None and isinstance(matching_checkpoint.get("response"), Mapping):
            if existing.get("fingerprint") != fingerprint or not isinstance(existing.get("response"), Mapping):
                self._archive_successful_checkpoint(existing, history_root)
                write_yaml(
                    path,
                    {
                        **dict(matching_checkpoint),
                        "fingerprint": fingerprint,
                        "upgraded_from_fingerprint": str(matching_checkpoint.get("fingerprint") or ""),
                        "updated_at": now_iso(),
                    },
                )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            self._progress(stage, path, active=False)
            return dict(matching_checkpoint["response"])
        if self.provider_calls >= self.max_calls:
            raise LiteratureSynthesisPartialError("literature_synthesis_call_budget_reached")

        self._archive_successful_checkpoint(existing, history_root)
        self.provider_calls += 1
        self._progress(stage, path, active=True)
        try:
            response = method(profiles, self.request, context=enriched_context)
            if is_dataclass(response):
                response = asdict(response)
            if not isinstance(response, Mapping):
                raise ValueError(f"{method_name} must return a mapping")
            payload = {
                "checkpoint_schema_version": "1",
                "fingerprint": fingerprint,
                "stage": stage,
                "key": key,
                "status": "completed",
                "provider": str(getattr(self.reasoner, "name", "")),
                "model": str(getattr(self.reasoner, "model", "")),
                "dependency_component_hashes": dependency_component_hashes,
                "dependency_context_hashes": dependency_context_hashes,
                "dependency_context_item_hashes": dependency_context_item_hashes,
                "response": dict(response),
                "updated_at": now_iso(),
            }
            write_yaml(path, payload)
            if failure_path.exists():
                failure_path.unlink()
            if stage == "cluster_synthesis":
                self.synthesized_clusters += 1
            return dict(response)
        except Exception as exc:
            self.failures += 1
            failure_payload = {
                "checkpoint_schema_version": "1",
                "fingerprint": fingerprint,
                "stage": stage,
                "key": key,
                "status": "failed",
                "provider": str(getattr(self.reasoner, "name", "")),
                "model": str(getattr(self.reasoner, "model", "")),
                "dependency_component_hashes": dependency_component_hashes,
                "dependency_context_hashes": dependency_context_hashes,
                "dependency_context_item_hashes": dependency_context_item_hashes,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "updated_at": now_iso(),
            }
            target = failure_path if isinstance(existing.get("response"), Mapping) else path
            write_yaml(target, failure_payload)
            raise
        finally:
            self._progress(stage, path, active=False)

    @staticmethod
    def _archive_successful_checkpoint(existing: Any, history_root: Path) -> None:
        if not isinstance(existing, Mapping) or not isinstance(existing.get("response"), Mapping):
            return
        existing_fingerprint = str(existing.get("fingerprint") or "")
        if not existing_fingerprint:
            return
        history_path = history_root / f"{existing_fingerprint}.yml"
        if not history_path.exists():
            write_yaml(history_path, dict(existing))

    def _atomic_notes(self, profiles: Sequence[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        workspace = self.workspace.resolve()
        for profile in profiles:
            raw = _as_mapping(profile)
            context = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
            note_path = str(raw.get("note_path") or context.get("note_path") or "")
            if not note_path:
                continue
            path = (self.workspace / note_path).resolve()
            if path != workspace and workspace not in path.parents:
                continue
            if not path.is_file():
                continue
            rows.append(
                {
                    "source_id": str(raw.get("source_id") or ""),
                    "note_id": str(raw.get("note_id") or ""),
                    "note_path": note_path,
                    "markdown": path.read_text(encoding="utf-8"),
                }
            )
        return rows

    def _progress(self, stage: str, path: Path, *, active: bool) -> None:
        if self.stage_callback is None:
            return
        values = {
            "active_synthesis_packet": str(path) if active else "",
            "synthesis_call_count": self.provider_calls,
            "synthesis_checkpoint_hit_count": self.checkpoint_hits,
            "synthesized_cluster_count": self.synthesized_clusters,
            "synthesis_failure_count": self.failures,
        }
        if stage == "cluster_synthesis":
            values["active_cluster"] = path.stem if active else ""
        elif stage == "gap_adjudication":
            values["active_gap_packet"] = path.stem if active else ""
        _notify_stage(self.stage_callback, stage, **values)


def _notify_stage(callback: Callable[..., Any] | None, stage: str, **values: Any) -> None:
    """Keep the legacy one-argument stage callback compatible with live progress callbacks."""

    if callback is None:
        return
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        callback(stage, **values)
        return
    accepts_values = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    if accepts_values:
        callback(stage, **values)
    else:
        callback(stage)


def _synthesis_stage_prompt_version(stage: str) -> str:
    return {
        "cluster_proposal": CLUSTER_PROPOSAL_PROMPT_VERSION,
        "cluster_synthesis": CLUSTER_SYNTHESIS_PROMPT_VERSION,
        "gap_adjudication": GAP_REASONING_PROMPT_VERSION,
    }.get(stage, LITERATURE_ALGORITHM_VERSION)


def _checkpoint_dependency_context(value: Any, *, sort_sequences: bool = False) -> Any:
    """Remove projection-only churn while retaining semantic synthesis inputs."""

    if isinstance(value, Mapping):
        transient = {
            "active_cluster",
            "atomic_notes",
            "compatibility_path",
            "map_path",
            "path",
            "registry_status",
            "related_gap_ids",
            "updated_at",
        }
        return {
            str(key): _checkpoint_dependency_context(child, sort_sequences=sort_sequences)
            for key, child in value.items()
            if str(key) not in transient
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = [
            _checkpoint_dependency_context(child, sort_sequences=sort_sequences)
            for child in value
        ]
        if sort_sequences:
            normalized.sort(
                key=lambda child: json.dumps(
                    child,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return normalized
    return value

_DIMENSION_ALIASES = {
    "theory": ("theory", "theories", "theoretical_framework", "theoretical_frameworks"),
    "mechanism": ("mechanism", "mechanisms"),
    "method": ("method", "methods", "methodology", "research_design"),
    "data": ("data", "dataset", "datasets", "data_source", "data_sources"),
    "case": ("case", "cases", "setting", "settings", "country", "countries"),
    "period": ("period", "periods", "time_period", "time_periods", "year", "years"),
    "outcome": ("outcome", "outcomes", "dependent_variable", "dependent_variables"),
    "finding direction": ("finding_direction", "direction", "effect_direction"),
    "uncertainty": ("uncertainty", "confidence", "precision", "qualification", "qualifications"),
    "limitations": ("limitations", "limitation", "caveats", "caveat"),
}
_SEMANTIC_FIELDS = (
    "semantic_topics",
    "topics",
    "topic",
    "concepts",
    "key_concepts",
    "themes",
    "theme",
    "mechanisms",
    "mechanism",
    "theories",
    "theory",
    "outcomes",
    "outcome",
)
_STOPWORDS = {
    "a", "also", "an", "and", "are", "as", "at", "be", "been", "being", "between", "by", "can", "could", "decrease",
    "decreased", "decreases", "did", "do", "does", "effect", "finding", "for", "four", "from", "grounded", "had", "has", "have",
    "how", "in", "include", "included", "includes", "including", "increase", "increased", "increases", "into", "is", "it", "made",
    "make", "makes", "may", "might", "more", "must", "negative", "not", "of", "on", "only", "or", "our", "page", "paper", "positive",
    "report", "reported", "research", "result", "see", "should", "source", "study", "studies", "than", "that", "the", "their", "then",
    "these", "this", "those", "to", "using", "via", "we", "what", "when", "where", "whether", "which", "with", "would",
}
_GENERIC_TOPIC_IDENTITIES = {"analytical", "document", "full", "none", "unknown", "unspecified"}
_WEAK_LOCATOR_MARKERS = {"", "unknown", "unavailable", "not reported", "n/a", "none", "not supplied"}
_TRACEABLE_LOCATOR = re.compile(
    r"(?:\b(?:p{1,2}\.?|pages?|paragraphs?|paras?)\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\b|"
    r"\b(?:chapter|section|appendix)\s+[a-z0-9ivx.-]+\b|"
    r"\b(?:abstract|introduction|background|literature review|methods?|methodology|data|results?|findings?|"
    r"discussion|conclusions?|limitations?)\s*(?:section)?\b|\b(?:table|figure)\s*\d+[a-z]?\b)",
    flags=re.IGNORECASE,
)
_AUTHOR_GAP_MARKER = re.compile(
    r"\b(?:future|further|unknown|unresolved|understudied|unexplored|"
    r"remain(?:s|ed)?\s+(?:unclear|unknown|untested|unresolved)|"
    r"not\s+(?:examined|tested|assessed|explored|known|identified)|"
    r"lack(?:s|ing)?\s+(?:of\s+)?(?:evidence|research|data|studies|testing)|"
    r"no\s+(?:empirical\s+)?(?:evidence|research|study|studies|test|testing|replication)|"
    r"need(?:s|ed)?\s+(?:for|to)|should|"
    r"require(?:s|d)?\s+(?:further|additional|testing|research|evidence)|"
    r"replicat(?:e|es|ed|ion)|research\s+gap)\b",
    flags=re.IGNORECASE,
)


def _as_mapping(value: Any) -> dict[str, Any]:
    """Accept mappings and the incoming dataclass models without importing them."""
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return dict(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    result: dict[str, Any] = {}
    for name in getattr(value, "__slots__", ()):
        if hasattr(value, name):
            result[str(name)] = getattr(value, name)
    if result:
        return result
    try:
        return dict(vars(value))
    except (TypeError, AttributeError):
        raise TypeError(f"expected a mapping or serializable model, got {type(value).__name__}") from None


def _policy_value(policy: Any, names: str | Sequence[str], default: Any) -> Any:
    values = _as_mapping(policy) if policy is not None else {}
    for name in (names,) if isinstance(names, str) else names:
        if name in values and values[name] is not None:
            return values[name]
    return default


def _stable_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def _flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        for key in ("value", "name", "label", "text", "title", "description"):
            if value.get(key):
                return _flatten_values(value[key])
        rows: list[str] = []
        for item in value.values():
            rows.extend(_flatten_values(item))
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        rows = []
        for item in value:
            rows.extend(_flatten_values(item))
        return rows
    return [str(value)]


def _stem_token(value: str) -> str:
    if len(value) > 5 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 5 and value.endswith("sses"):
        return value[:-2]
    if len(value) > 4 and value.endswith("s") and not value.endswith(("ss", "is", "us")):
        return value[:-1]
    return value


def _tokens(value: Any) -> set[str]:
    text = " ".join(_flatten_values(value)).casefold()
    return {
        _stem_token(token)
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in _STOPWORDS and len(token) > 2
    }


def _canonical_phrase(value: Any) -> str:
    return " ".join(sorted(_tokens(value)))


def _author_gap_record(value: Any, *, origin: str) -> dict[str, Any] | None:
    item = _as_mapping(value) if not isinstance(value, str) else {"missing_evidence": value}
    text = str(
        item.get("precise_missing_evidence")
        or item.get("missing_evidence")
        or item.get("gap_text")
        or item.get("text")
        or ""
    ).strip()
    if len(_tokens(text)) < 2:
        return None
    if origin != "future_research" and not _AUTHOR_GAP_MARKER.search(text):
        return None
    return {**item, "missing_evidence": text, "_author_gap_origin": origin}


def _dimension_values(row: Mapping[str, Any], dimension: str) -> list[str]:
    values: list[str] = []
    for alias in _DIMENSION_ALIASES[dimension]:
        if alias in row:
            values.extend(_flatten_values(row.get(alias)))
    normalized = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip()
        if clean and clean.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(clean)
    return normalized


def _locator_text(value: Any) -> str:
    if isinstance(value, Mapping):
        ordered = [value.get(key) for key in ("page", "pages", "section", "paragraph", "table", "figure", "quote")]
        text = "; ".join(item for item in _flatten_values(ordered) if item)
        return text or "; ".join(_flatten_values(value))
    values = _flatten_values(value)
    return "; ".join(values[:3])


def _complete_locator(value: Any) -> bool:
    text = _locator_text(value).strip().casefold()
    return bool(
        text
        and text not in _WEAK_LOCATOR_MARKERS
        and not any(marker in text for marker in ("not supplied", "not available"))
        and _TRACEABLE_LOCATOR.search(text)
    )


def _normalized_locator(value: Any) -> str:
    text = _locator_text(value).casefold().replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip(" .;,:")


def _reference_matches_profile(reference: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    """Require a reasoner reference to resolve to an existing located claim."""
    if str(reference.get("source_id") or "") != str(profile.get("source_id") or ""):
        return False
    claim_id = str(reference.get("claim_id") or "")
    if not claim_id:
        return False
    claim = next(
        (row for row in profile.get("claims", []) or [] if str(row.get("claim_id") or "") == claim_id),
        None,
    )
    if claim is None or not claim.get("locator_complete"):
        return False
    locator = reference.get("locator") or claim.get("locator")
    return _complete_locator(locator) and _normalized_locator(locator) == _normalized_locator(claim.get("locator"))


def _proposal_membership_evidence(
    profile: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one exact located claim when a concise proposal omits member evidence."""

    located_claims = [claim for claim in profile.get("claims", []) or [] if claim.get("locator_complete")]
    if not located_claims:
        return {}
    proposal_terms = _tokens(
        [
            proposal.get("semantic_identity"),
            proposal.get("label"),
            proposal.get("shared_question"),
            proposal.get("coherence_rationale"),
        ]
    )

    def claim_score(claim: Mapping[str, Any]) -> tuple[int, str]:
        claim_terms = _tokens(
            [
                claim.get("text"),
                claim.get("topic"),
                claim.get("dimensions"),
            ]
        )
        return (len(proposal_terms & claim_terms), str(claim.get("claim_id") or ""))

    selected = max(located_claims, key=claim_score)
    return _evidence_ref(selected)


def _normalize_direction(value: Any) -> str:
    text = " ".join(_flatten_values(value)).casefold().strip()
    if not text:
        return "not_reported"
    if any(marker in text for marker in ("positive", "increase", "higher", "supports", "improves")):
        return "positive"
    if any(marker in text for marker in ("negative", "decrease", "lower", "undermines", "reduces")):
        return "negative"
    if any(marker in text for marker in ("null", "no effect", "no association", "not significant")):
        return "null"
    if any(marker in text for marker in ("mixed", "conditional", "heterogeneous", "varies")):
        return "mixed"
    return slugify(text, "not-reported").replace("-", "_")


def _normalize_claims(raw: Mapping[str, Any], source_id: str, family_id: str) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("claims", "structured_findings", "findings", "evidence_records"):
        value = raw.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            candidates.extend(value)
        elif value:
            candidates.append(value)
    if not candidates:
        has_dimensions = any(_dimension_values(raw, dimension) for dimension in EVIDENCE_DIMENSIONS)
        if has_dimensions or raw.get("locator") or raw.get("locators"):
            candidates.append(raw)
    claims: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        item = _as_mapping(candidate) if not isinstance(candidate, str) else {"finding": candidate}
        locator = _locator_text(item.get("locator") or item.get("locators") or raw.get("locator") or raw.get("locators"))
        dimensions = {
            dimension: _dimension_values(item, dimension) or _dimension_values(raw, dimension)
            for dimension in EVIDENCE_DIMENSIONS
        }
        direction = _normalize_direction(dimensions["finding direction"] or item.get("direction") or item.get("finding_direction"))
        dimensions["finding direction"] = [] if direction == "not_reported" else [direction]
        text = str(item.get("claim") or item.get("finding") or item.get("text") or item.get("description") or "").strip()
        identity = {
            "source_id": source_id,
            "text": text,
            "locator": locator,
            "dimensions": dimensions,
            "index": index,
        }
        claims.append(
            {
                "claim_id": str(item.get("claim_id") or item.get("finding_id") or f"claim-{_stable_hash(identity)[:12]}"),
                "source_id": source_id,
                "study_family_id": family_id,
                "text": text,
                "locator": locator,
                "locator_complete": _complete_locator(locator),
                "dimensions": dimensions,
                "direction": direction,
                "topic": str(item.get("topic") or raw.get("topic") or ""),
                "boundary_condition": str(item.get("boundary_condition") or item.get("boundary") or "; ".join(_flatten_values(item.get("conditions")))),
                "mechanism_tested": item.get("mechanism_tested"),
                "addresses_gap": item.get("addresses_gap", False),
                "gap_rule": str(item.get("gap_rule") or ""),
                "answer_status": str(item.get("answer_status") or ""),
            }
        )
    return sorted(claims, key=lambda row: row["claim_id"])


def _topic_scores(raw: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    weights = {
        "semantic_topics": 1.0, "topics": 1.0, "topic": 1.0, "concepts": 0.9, "key_concepts": 0.9,
        "themes": 0.85, "theme": 0.85, "mechanisms": 0.8, "mechanism": 0.8,
        "theories": 0.7, "theory": 0.7, "outcomes": 0.65, "outcome": 0.65,
    }
    for field in _SEMANTIC_FIELDS:
        for value in _flatten_values(raw.get(field)):
            phrase = _canonical_phrase(value)
            if phrase and not set(phrase.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
                scores[phrase] = max(scores.get(phrase, 0.0), weights[field])
    for claim in claims:
        phrase = _canonical_phrase(claim.get("topic"))
        if phrase and not set(phrase.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
            scores[phrase] = max(scores.get(phrase, 0.0), 0.95)
        for dimension in ("mechanism", "theory", "outcome"):
            for value in claim.get("dimensions", {}).get(dimension, []) or []:
                phrase = _canonical_phrase(value)
                if phrase and not set(phrase.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
                    scores[phrase] = max(scores.get(phrase, 0.0), 0.7)
    if not scores:
        title_tokens = sorted(_tokens(raw.get("title", "")))
        for token in title_tokens:
            scores[token] = 0.45
    return dict(sorted(scores.items()))


def _topic_labels(
    raw: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
) -> dict[str, str]:
    candidates: dict[str, Counter[str]] = defaultdict(Counter)

    def add(value: Any) -> None:
        for flattened in _flatten_values(value):
            label = re.sub(r"\s+", " ", flattened).strip(" .;:,-")
            identity = _canonical_phrase(label)
            if identity in scores and label:
                candidates[identity][label] += 1

    for field in _SEMANTIC_FIELDS:
        add(raw.get(field))
    for claim in claims:
        add(claim.get("topic"))
        dimensions = claim.get("dimensions", {}) if isinstance(claim.get("dimensions"), Mapping) else {}
        for dimension in ("mechanism", "theory", "outcome"):
            add(dimensions.get(dimension))
    return {
        identity: sorted(labels, key=lambda label: (-labels[label], len(label), label.casefold()))[0]
        for identity, labels in sorted(candidates.items())
        if labels
    }


def normalize_evidence_profiles(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Pure compatibility boundary for EvidenceProfile models and current note rows."""
    normalized: list[dict[str, Any]] = []
    for position, value in enumerate(profiles):
        raw = _as_mapping(value)
        context = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
        context_metadata = context.get("metadata") if isinstance(context.get("metadata"), Mapping) else {}
        features = raw.get("features") if isinstance(raw.get("features"), Mapping) else {}
        title = str(raw.get("title") or context.get("title") or context_metadata.get("title") or "").strip()
        source_id = str(raw.get("source_id") or raw.get("id") or f"source-{_stable_hash([title, position])[:12]}")
        note_id = str(raw.get("note_id") or f"note-{_stable_hash(source_id)[:12]}")
        doi = str(raw.get("doi") or raw.get("DOI") or "").strip().casefold()
        family_id = str(raw.get("study_family_id") or raw.get("study_id") or (f"doi:{doi}" if doi else source_id))
        coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
        status = str(
            raw.get("note_status")
            or raw.get("profile_status")
            or raw.get("status")
            or coverage.get("note_status")
            or context.get("note_status")
            or "analytical"
        ).casefold()
        coverage_status = str(coverage.get("status", "")).casefold()
        validity_status = str((raw.get("validity") or {}).get("status", "") if isinstance(raw.get("validity"), Mapping) else "").casefold()
        excluded = bool(raw.get("excluded_from_synthesis", False))
        invalid = validity_status in {"invalid", "failed", "excluded"}
        limited_coverage = coverage_status in {"abstract", "abstract_only", "metadata_only", "limited", "failed"}
        analytical = (
            bool(raw.get("analytical", status in ANALYTICAL_STATUSES))
            and status not in LIMITED_STATUSES
            and not excluded
            and not invalid
            and not limited_coverage
        )
        claims = _normalize_claims(raw, source_id, family_id)
        dimensions = {dimension: _dimension_values(raw, dimension) for dimension in EVIDENCE_DIMENSIONS}
        normalized_tag_values = _flatten_values(
            raw.get("normalized_tags")
            or context_metadata.get("normalized_tags")
            or context.get("normalized_tags")
            or features.get("zotero_tag_context")
        )
        normalized_tags = sorted({slugify(tag) for tag in normalized_tag_values if slugify(tag)})
        tag_values = _flatten_values(
            raw.get("normalized_tags")
            or raw.get("tags")
            or context_metadata.get("normalized_tags")
            or context.get("normalized_tags")
            or features.get("zotero_tag_context")
        )
        tags = sorted({_canonical_phrase(tag) for tag in tag_values if _canonical_phrase(tag)})
        semantic_raw = dict(raw)
        semantic_raw["title"] = title
        if tags:
            tag_set = set(tags)
            for field in _SEMANTIC_FIELDS:
                semantic_raw[field] = [
                    value
                    for value in _flatten_values(raw.get(field))
                    if _canonical_phrase(value) not in tag_set
                ]
            semantic_claims = []
            for claim in claims:
                cleaned_claim = dict(claim)
                if _canonical_phrase(cleaned_claim.get("topic")) in tag_set:
                    cleaned_claim["topic"] = ""
                cleaned_dimensions = {
                    dimension: [
                        value
                        for value in values
                        if _canonical_phrase(value) not in tag_set
                    ]
                    for dimension, values in cleaned_claim.get("dimensions", {}).items()
                }
                cleaned_claim["dimensions"] = cleaned_dimensions
                semantic_claims.append(cleaned_claim)
        else:
            semantic_claims = claims
        topic_scores = _topic_scores(semantic_raw, semantic_claims)
        topic_labels = _topic_labels(semantic_raw, semantic_claims, topic_scores)
        author_gap_records = [
            record
            for origin, values in (
                ("author_stated_gap", raw.get("author_stated_gaps") or raw.get("gaps") or []),
                ("future_research", raw.get("future_research") or []),
            )
            for value in values
            if (record := _author_gap_record(value, origin=origin)) is not None
        ]
        search_values = [title, *topic_scores, *[claim.get("text", "") for claim in claims]]
        for dimension in EVIDENCE_DIMENSIONS:
            search_values.extend(dimensions[dimension])
        normalized.append(
            {
                "_normalized": True,
                "source_id": source_id,
                "note_id": note_id,
                "title": title,
                "study_family_id": family_id,
                "zotero_item_key": str(raw.get("zotero_item_key") or context.get("zotero_item_key") or ""),
                "note_status": status,
                "analytical": analytical,
                "limited": not analytical,
                "exclusion_reason": str(raw.get("exclusion_reason") or context.get("exclusion_reason") or ""),
                "semantic_topic_scores": topic_scores,
                "semantic_topic_labels": topic_labels,
                "dimensions": dimensions,
                "claims": claims,
                "tags": tags,
                "normalized_tags": normalized_tags,
                "relations": raw.get("citation_relations")
                or raw.get("relations")
                or raw.get("zotero_relations")
                or context.get("zotero_relations")
                or {},
                "gap_signals": list(raw.get("gap_signals") or raw.get("gap_candidates") or []),
                "author_stated_gaps": author_gap_records,
                "gap_answers": list(raw.get("gap_answers") or raw.get("answered_gaps") or []),
                "note_path": str(raw.get("note_path") or context.get("note_path") or ""),
                "note_hash": str(raw.get("note_hash") or ""),
                "date": str(raw.get("date") or context.get("date") or ""),
                "search_tokens": sorted(_tokens(search_values)),
            }
        )
    return sorted(normalized, key=lambda row: (row["source_id"], row["note_id"]))


def _ensure_profiles(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    if all(isinstance(row, Mapping) and row.get("_normalized") for row in profiles):
        return [dict(row) for row in profiles]  # defensive copies keep stages pure for callers
    return normalize_evidence_profiles(profiles)


def _relation_strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        rows: list[str] = []
        for key, item in value.items():
            rows.append(str(key))
            rows.extend(_relation_strings(item))
        return rows
    return _flatten_values(value)


def _has_explicit_relation(source: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    haystack = " ".join(_relation_strings(source.get("relations"))).casefold()
    aliases = {
        str(target.get("source_id", "")).casefold(),
        str(target.get("note_id", "")).casefold(),
        str(target.get("zotero_item_key", "")).casefold(),
    } - {""}
    return any(alias in haystack for alias in aliases)


def map_profile_relations(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Map evidence relations; tag overlap is recorded only after a substantive relation exists."""
    rows = [row for row in _ensure_profiles(profiles) if row["analytical"]]
    relations: list[dict[str, Any]] = []
    for left, right in combinations(rows, 2):
        shared_topics = sorted(set(left["semantic_topic_scores"]) & set(right["semantic_topic_scores"]))
        left_findings = set().union(*(_tokens(claim.get("text", "")) for claim in left["claims"])) if left["claims"] else set()
        right_findings = set().union(*(_tokens(claim.get("text", "")) for claim in right["claims"])) if right["claims"] else set()
        shared_findings = sorted(left_findings & right_findings)
        finding_overlap = len(shared_findings) / max(1, min(len(left_findings), len(right_findings)))
        structured_finding_match = len(shared_findings) >= 2 and finding_overlap >= 0.4
        explicit_lr = _has_explicit_relation(left, right)
        explicit_rl = _has_explicit_relation(right, left)
        if not (shared_topics or structured_finding_match or explicit_lr or explicit_rl):
            continue
        evidence: list[dict[str, Any]] = []
        if shared_topics:
            evidence.append({"kind": "semantic_profile", "values": shared_topics})
        if structured_finding_match:
            evidence.append(
                {
                    "kind": "structured_findings",
                    "values": shared_findings,
                    "overlap_coefficient": round(finding_overlap, 3),
                }
            )
        if explicit_lr or explicit_rl:
            evidence.append(
                {
                    "kind": "explicit_zotero_or_citation_relation",
                    "directions": [
                        direction
                        for flag, direction in ((explicit_lr, "left_to_right"), (explicit_rl, "right_to_left"))
                        if flag
                    ],
                }
            )
        shared_tags = sorted(set(left["tags"]) & set(right["tags"]))
        if shared_tags:
            evidence.append({"kind": "tag_tiebreaker", "values": shared_tags, "weight": "weak"})
        confidence = min(
            0.99,
            0.45
            + 0.12 * len(shared_topics)
            + (0.04 * min(len(shared_findings), 4) if structured_finding_match else 0)
            + (0.25 if explicit_lr or explicit_rl else 0),
        )
        source_ids = sorted((left["source_id"], right["source_id"]))
        relations.append(
            {
                "relation_id": f"relation-{_stable_hash(source_ids)[:12]}",
                "source_ids": source_ids,
                "confidence": round(confidence, 3),
                "evidence": evidence,
            }
        )
    return sorted(relations, key=lambda row: row["relation_id"])


def map_overlapping_clusters(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build deterministic semantic clusters with at most three memberships per source."""
    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row["analytical"]]
    max_memberships = max(1, min(3, int(_policy_value(policy, ("max_memberships", "max_cluster_memberships", "max_overlapping_clusters"), 3))))
    min_emerging = max(2, int(_policy_value(policy, ("min_emerging_families", "emerging_cluster_min_sources"), 2)))
    min_backed = max(3, int(_policy_value(policy, ("source_backed_threshold", "min_source_backed_families", "source_backed_cluster_min_sources"), 3)))
    auto_promote = bool(_policy_value(policy, "auto_promote_clusters", True))
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    profile_by_source = {row["source_id"]: row for row in analytical}
    reasoned_metadata: dict[str, dict[str, Any]] = {}
    reasoner_clustered_sources: set[str] = set()
    proposal_rejections: list[dict[str, Any]] = []
    for raw_proposal in proposals or ():
        proposal = _as_mapping(raw_proposal)
        identity = _canonical_phrase(proposal.get("semantic_identity") or proposal.get("label"))
        source_ids = sorted({str(value) for value in proposal.get("source_ids", []) or [] if str(value)})
        evidence = [
            _as_mapping(value)
            for value in proposal.get("supporting_evidence", []) or []
            if isinstance(value, Mapping) or is_dataclass(value)
        ]
        valid_evidence: list[dict[str, Any]] = []
        valid_sources: set[str] = set()
        for source_id in source_ids:
            profile = profile_by_source.get(source_id)
            if profile is None:
                continue
            supplied = [
                reference for reference in evidence if str(reference.get("source_id") or "") == source_id
            ]
            if supplied:
                matched = [reference for reference in supplied if _reference_matches_profile(reference, profile)]
                if not matched:
                    # An explicitly invented or paraphrased reference is never
                    # repaired silently.
                    continue
                reference = dict(matched[0])
            else:
                reference = _proposal_membership_evidence(profile, proposal)
                if not reference:
                    continue
            valid_sources.add(source_id)
            valid_evidence.append(reference)
        families = {profile_by_source[source_id]["study_family_id"] for source_id in valid_sources}
        if not identity or not source_ids or len(families) < min_emerging:
            proposal_rejections.append(
                {
                    "semantic_identity": identity or str(proposal.get("label") or ""),
                    "source_ids": source_ids,
                    "action": "reject",
                    "reason": "reasoner_proposal_missing_independent_locator_backed_membership",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                }
            )
            continue
        dropped_sources = sorted(set(source_ids) - valid_sources)
        if dropped_sources:
            proposal_rejections.append(
                {
                    "semantic_identity": identity,
                    "source_ids": dropped_sources,
                    "action": "narrow",
                    "reason": "reasoner_proposal_membership_missing_exact_claim_locator",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                }
            )
        reasoned_metadata[identity] = {
            "proposal_id": str(proposal.get("proposal_id") or ""),
            "label": str(proposal.get("label") or proposal.get("semantic_identity") or identity),
            "shared_question": str(proposal.get("shared_question") or ""),
            "coherence_rationale": str(proposal.get("coherence_rationale") or ""),
            "supporting_evidence": valid_evidence,
        }
        reasoner_clustered_sources.update(valid_sources)
        for source_id in sorted(valid_sources):
            by_topic[identity].append({"profile": profile_by_source[source_id], "score": 1.0})

    # Valid reasoner proposals enrich the map; they never suppress coherent
    # deterministic clusters when the proposal packet is partial.
    for profile in analytical:
        if profile["source_id"] in reasoner_clustered_sources:
            continue
        for identity, score in profile["semantic_topic_scores"].items():
            by_topic[identity].append({"profile": profile, "score": score})
    for relation in relations or ():
        relation_profiles = [
            profile_by_source[str(source_id)]
            for source_id in relation.get("source_ids", []) or []
            if str(source_id) in profile_by_source
        ]
        if len(relation_profiles) < 2:
            continue
        shared_topics = set(relation_profiles[0]["semantic_topic_scores"])
        for profile in relation_profiles[1:]:
            shared_topics &= set(profile["semantic_topic_scores"])
        if shared_topics:
            continue
        structured_values = sorted(
            {
                str(value)
                for evidence in relation.get("evidence", []) or []
                if evidence.get("kind") == "structured_findings"
                for value in evidence.get("values", []) or []
            }
        )
        relation_identity = _canonical_phrase(structured_values)
        if len(relation_identity.split()) < 2:
            continue
        for profile in relation_profiles:
            by_topic[relation_identity].append({"profile": profile, "score": 0.6})

    candidates: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = list(proposal_rejections)
    for identity, members in sorted(by_topic.items()):
        unique = {row["profile"]["source_id"]: row for row in members}
        families = {row["profile"]["study_family_id"] for row in unique.values()}
        if len(families) < min_emerging:
            rejected.append(
                {
                    "semantic_identity": identity,
                    "source_ids": sorted(unique),
                    "action": "reject",
                    "reason": "singleton_cluster" if len(unique) == 1 else "insufficient_independent_study_families",
                }
            )
            continue
        candidates[identity] = {
            "identity": identity,
            "members": unique,
            "family_count": len(families),
            "mean_score": sum(float(row["score"]) for row in unique.values()) / len(unique),
        }

    kept_candidates: dict[str, dict[str, Any]] = {}
    ordered_candidates = sorted(
        candidates.values(),
        key=lambda row: (-len(row["members"]), len(str(row["identity"]).split()), str(row["identity"])),
    )
    for candidate in ordered_candidates:
        candidate_tokens = set(str(candidate["identity"]).split())
        candidate_sources = set(candidate["members"])
        superseded_by = ""
        for kept in kept_candidates.values():
            kept_tokens = set(str(kept["identity"]).split())
            if not (candidate_tokens <= kept_tokens or kept_tokens <= candidate_tokens):
                continue
            kept_sources = set(kept["members"])
            membership_jaccard = len(candidate_sources & kept_sources) / max(1, len(candidate_sources | kept_sources))
            if membership_jaccard >= 0.8:
                superseded_by = str(kept["identity"])
                break
        if superseded_by:
            rejected.append(
                {
                    "semantic_identity": candidate["identity"],
                    "source_ids": sorted(candidate_sources),
                    "action": "reject",
                    "reason": "redundant_semantic_cluster",
                    "superseded_by": superseded_by,
                }
            )
        else:
            kept_candidates[str(candidate["identity"])] = candidate
    candidates = kept_candidates

    selected_by_source: dict[str, set[str]] = defaultdict(set)
    for profile in analytical:
        available = [candidate for candidate in candidates.values() if profile["source_id"] in candidate["members"]]
        available.sort(key=lambda row: (-row["family_count"], -row["mean_score"], row["identity"]))
        selected_by_source[profile["source_id"]].update(row["identity"] for row in available[:max_memberships])

    relation_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for relation in relations or ():
        for source_id in relation.get("source_ids", []) or []:
            relation_ids_by_source[str(source_id)].append(str(relation.get("relation_id", "")))

    clusters: list[dict[str, Any]] = []
    for identity, candidate in sorted(candidates.items()):
        member_rows = [
            row["profile"]
            for source_id, row in candidate["members"].items()
            if identity in selected_by_source[source_id]
        ]
        families = sorted({row["study_family_id"] for row in member_rows})
        if len(families) < min_emerging:
            rejected.append(
                {
                    "semantic_identity": identity,
                    "source_ids": sorted(row["source_id"] for row in member_rows),
                    "action": "reject",
                    "reason": "overlap_policy_removed_coherence",
                }
            )
            continue
        source_ids = sorted(row["source_id"] for row in member_rows)
        note_ids = sorted(row["note_id"] for row in member_rows)
        reasoned = reasoned_metadata.get(identity, {})
        label_counts = Counter(
            str(row.get("semantic_topic_labels", {}).get(identity) or identity)
            for row in member_rows
        )
        label = str(reasoned.get("label") or sorted(label_counts, key=lambda value: (-label_counts[value], len(value), value.casefold()))[0])
        cluster_id = f"cluster-{slugify(identity)}-{_stable_hash({'semantic_identity': identity})[:10]}"
        revision_hash = _stable_hash({"cluster_id": cluster_id, "source_ids": source_ids, "study_family_ids": families})
        qualification_status = "source_backed_cluster" if len(families) >= min_backed else "emerging_cluster"
        tag_families: dict[str, set[str]] = defaultdict(set)
        for row in member_rows:
            for tag in row.get("normalized_tags", []) or []:
                tag_families[str(tag)].add(str(row["study_family_id"]))
        shared_normalized_tags = sorted(
            tag for tag, tag_study_families in tag_families.items() if len(tag_study_families) >= min_emerging
        )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "semantic_identity": identity,
                "label": label,
                "shared_question": str(reasoned.get("shared_question") or f"What does the mapped evidence establish about {label}?"),
                "coherence_rationale": str(
                    reasoned.get("coherence_rationale")
                    or f"Independent profiles share the mapped semantic identity: {label}."
                ),
                "proposal_id": str(reasoned.get("proposal_id") or ""),
                "proposal_supporting_evidence": list(reasoned.get("supporting_evidence", []) or []),
                "formation_route": "reasoner_proposal" if reasoned else "deterministic_fallback",
                "shared_concepts": [identity],
                "shared_normalized_tags": shared_normalized_tags,
                "shared_methods": sorted({value for row in member_rows for value in row["dimensions"]["method"]}),
                "note_ids": note_ids,
                "source_ids": source_ids,
                "study_family_ids": families,
                "independent_study_family_count": len(families),
                "source_count": len(source_ids),
                "status": qualification_status if auto_promote else "cluster_candidate",
                "qualification_status": qualification_status,
                "promoted": auto_promote,
                "automation_status": "promoted" if auto_promote else "candidate",
                "source_backed": len(families) >= min_backed,
                "revision_hash": revision_hash,
                "relation_ids": sorted({relation_id for source_id in source_ids for relation_id in relation_ids_by_source[source_id] if relation_id}),
                "representative_sources": [
                    {
                        "note_id": row["note_id"],
                        "source_id": row["source_id"],
                        "study_family_id": row["study_family_id"],
                        "title": row["title"],
                        "note_path": row["note_path"],
                        "note_hash": row["note_hash"],
                    }
                    for row in sorted(member_rows, key=lambda item: item["source_id"])
                ],
            }
        )

    clustered_sources = {source_id for cluster in clusters for source_id in cluster["source_ids"]}
    unclustered = []
    for profile in rows:
        if profile["source_id"] in clustered_sources:
            continue
        if profile["limited"]:
            reason = profile.get("exclusion_reason") or "limited_profile_excluded_from_analytical_clustering"
        elif not profile["semantic_topic_scores"]:
            reason = "no_semantic_topic_identity"
        else:
            reason = "no_coherent_multi_family_cluster"
        unclustered.append({"source_id": profile["source_id"], "note_id": profile["note_id"], "reason": reason})
    return {
        "clusters": sorted(clusters, key=lambda row: row["cluster_id"]),
        "rejected_proposals": sorted(rejected, key=lambda row: (row["reason"], row["semantic_identity"])),
        "unclustered_sources": sorted(unclustered, key=lambda row: row["source_id"]),
        "max_cluster_memberships": max_memberships,
    }


def reconcile_cluster_registry(
    clusters: Sequence[Mapping[str, Any]],
    previous_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer stable lifecycle events without changing semantic cluster IDs."""
    previous_payload = dict(previous_registry or {})
    previous = [dict(row) for row in previous_payload.get("clusters", []) or []]
    old_by_id = {str(row.get("cluster_id")): row for row in previous if row.get("cluster_id")}
    current = [dict(row) for row in clusters]
    current_by_id = {str(row["cluster_id"]): row for row in current}
    ledger: list[dict[str, Any]] = [dict(row) for row in previous_payload.get("ledger", []) or []]

    for cluster in current:
        old = old_by_id.get(str(cluster["cluster_id"]))
        if old is None:
            cluster["registry_status"] = "new"
            ledger.append({"event": "new", "cluster_id": cluster["cluster_id"], "revision_hash": cluster["revision_hash"]})
        elif str(old.get("revision_hash")) == str(cluster.get("revision_hash")):
            cluster["registry_status"] = "unchanged"
            ledger.append({"event": "unchanged", "cluster_id": cluster["cluster_id"], "revision_hash": cluster["revision_hash"]})
        else:
            cluster["registry_status"] = "revision"
            ledger.append(
                {
                    "event": "revision",
                    "cluster_id": cluster["cluster_id"],
                    "prior_revision_hash": str(old.get("revision_hash", "")),
                    "revision_hash": cluster["revision_hash"],
                    "added_source_ids": sorted(set(cluster.get("source_ids", [])) - set(old.get("source_ids", []))),
                    "removed_source_ids": sorted(set(old.get("source_ids", [])) - set(cluster.get("source_ids", []))),
                }
            )

    unmatched_old = [row for row in previous if str(row.get("cluster_id")) not in current_by_id]
    unmatched_current = [row for row in current if str(row.get("cluster_id")) not in old_by_id]
    old_to_new: dict[str, list[str]] = defaultdict(list)
    new_to_old: dict[str, list[str]] = defaultdict(list)
    for old in unmatched_old:
        old_sources = set(old.get("source_ids", []) or [])
        old_identity = _canonical_phrase(old.get("semantic_identity") or old.get("label"))
        for new in unmatched_current:
            membership_overlap = bool(old_sources & set(new.get("source_ids", []) or []))
            semantic_overlap = bool(old_identity and old_identity == _canonical_phrase(new.get("semantic_identity")))
            if membership_overlap or semantic_overlap:
                old_to_new[str(old.get("cluster_id"))].append(str(new["cluster_id"]))
                new_to_old[str(new["cluster_id"])].append(str(old.get("cluster_id")))

    retired: list[dict[str, Any]] = [dict(row) for row in previous_payload.get("retired_clusters", []) or []]
    for old in unmatched_old:
        old_id = str(old.get("cluster_id"))
        successors = sorted(set(old_to_new.get(old_id, [])))
        if len(successors) > 1:
            ledger.append({"event": "split", "prior_cluster_ids": [old_id], "cluster_ids": successors})
        elif len(successors) == 1 and len(set(new_to_old.get(successors[0], []))) == 1:
            ledger.append({"event": "supersede", "prior_cluster_ids": [old_id], "cluster_ids": successors})
        elif not successors:
            retired.append({**old, "registry_status": "retired"})
            ledger.append({"event": "retire", "prior_cluster_ids": [old_id], "cluster_ids": []})
    for new_id, predecessors in sorted(new_to_old.items()):
        unique = sorted(set(predecessors))
        if len(unique) > 1:
            ledger.append({"event": "merge", "prior_cluster_ids": unique, "cluster_ids": [new_id]})

    def event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(row.get("event")),
            repr(row.get("cluster_ids", [])),
            repr(row.get("prior_cluster_ids", [])),
            str(row.get("cluster_id", "")),
            str(row.get("revision_hash", "")),
        )

    unique_ledger = {_stable_hash(row): row for row in ledger}
    unique_retired = {str(row.get("cluster_id")): row for row in retired if row.get("cluster_id")}
    return {
        "clusters": sorted(current, key=lambda row: row["cluster_id"]),
        "ledger": sorted(unique_ledger.values(), key=event_key),
        "retired_clusters": sorted(unique_retired.values(), key=lambda row: str(row.get("cluster_id"))),
    }


def _evidence_ref(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": str(claim.get("claim_id", "")),
        "source_id": str(claim.get("source_id", "")),
        "study_family_id": str(claim.get("study_family_id", "")),
        "locator": str(claim.get("locator", "")),
    }


def _resolve_reasoner_evidence(
    values: Any,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    *,
    allowed_source_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for value in values or []:
        if not isinstance(value, Mapping):
            continue
        reference = _as_mapping(value)
        source_id = str(reference.get("source_id") or "")
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            continue
        profile = profile_by_source.get(source_id)
        if profile is None or not _reference_matches_profile(reference, profile):
            continue
        claim_id = str(reference.get("claim_id") or "")
        claim = next(
            row for row in profile.get("claims", []) or [] if str(row.get("claim_id") or "") == claim_id
        )
        resolved.append(_evidence_ref(claim))
    return sorted(
        {_stable_hash(row): row for row in resolved}.values(),
        key=lambda row: (row["source_id"], row["claim_id"], row["locator"]),
    )


def _sanitize_reasoned_items(
    values: Any,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    *,
    allowed_source_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in values or []:
        if not isinstance(value, Mapping):
            continue
        item = _as_mapping(value)
        evidence = _resolve_reasoner_evidence(
            item.get("evidence") or item.get("supporting_evidence") or [],
            profile_by_source,
            allowed_source_ids=allowed_source_ids,
        )
        if not evidence:
            continue
        cleaned = {
            str(key): val
            for key, val in item.items()
            if key not in {"evidence", "supporting_evidence"}
        }
        cleaned["evidence"] = evidence
        rows.append(cleaned)
    return rows


def validate_cluster_synthesis(
    value: Any,
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Admit only cluster prose and comparisons backed by real located claims."""
    raw = _as_mapping(value) if value else {}
    cluster_id = str(cluster.get("cluster_id") or "")
    if raw.get("cluster_id") and str(raw.get("cluster_id")) != cluster_id:
        raw = {}
    profile_by_source = {str(row["source_id"]): row for row in profiles}
    allowed_source_ids = {str(value) for value in cluster.get("source_ids", []) or []}
    sections = {
        key: _sanitize_reasoned_items(raw.get(key, []), profile_by_source, allowed_source_ids=allowed_source_ids)
        for key in CLUSTER_SYNTHESIS_SECTIONS
    }
    for section, items in sections.items():
        for item in items:
            semantic_item = {
                key: value
                for key, value in item.items()
                if key not in {"item_id", "updated_at", "evidence", "supporting_evidence"}
            }
            item["item_id"] = (
                f"cluster-item-{slugify(section)}-"
                f"{_stable_hash([cluster_id, section, semantic_item])[:12]}"
            )
    top_evidence = _resolve_reasoner_evidence(
        raw.get("supporting_evidence", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    if not top_evidence:
        top_evidence = sorted(
            {
                _stable_hash(reference): reference
                for rows in sections.values()
                for row in rows
                for reference in row.get("evidence", []) or []
            }.values(),
            key=lambda row: (row["source_id"], row["claim_id"]),
        )
    supporting_families = {str(row.get("study_family_id") or row.get("source_id")) for row in top_evidence}
    substantive = len(supporting_families) >= 2
    gap_hypotheses = _sanitize_reasoned_items(
        raw.get("gap_hypotheses", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    for hypothesis in gap_hypotheses:
        hypothesis.setdefault("related_cluster_ids", [cluster_id])
        hypothesis["related_cluster_ids"] = sorted(
            {cluster_id, *(str(value) for value in hypothesis.get("related_cluster_ids", []) or [] if str(value))}
        )
        hypothesis["supporting_evidence"] = list(hypothesis.pop("evidence", []))
    return {
        "cluster_id": cluster_id,
        "status": "reasoned" if substantive else "deterministic_fallback",
        "scope": str(raw.get("scope") or cluster.get("shared_question") or ""),
        "boundaries": [str(value) for value in raw.get("boundaries", []) or [] if str(value)],
        "coherence_rationale": str(
            raw.get("coherence_rationale") if substantive else cluster.get("coherence_rationale") or ""
        ),
        "synthesis": str(raw.get("synthesis") or "") if substantive else "",
        "supporting_evidence": top_evidence,
        "gap_hypotheses": gap_hypotheses,
        **sections,
    }


def apply_cluster_syntheses_to_debates(
    debate_registry: Mapping[str, Any],
    syntheses: Mapping[str, Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Use validated reasoned proposition groups without weakening promotion gates."""
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    assessments: list[dict[str, Any]] = []
    for original in debate_registry.get("assessments", []) or []:
        assessment = dict(original)
        synthesis = syntheses.get(str(assessment.get("cluster_id") or ""), {})
        contradictions = list(synthesis.get("contradictions", []) or [])
        positions = list(synthesis.get("positions", []) or [])
        agreements = list(synthesis.get("agreements", []) or [])
        # Deterministic comparable-proposition gates remain authoritative.
        # Reasoner prose can explain an admitted classification but cannot
        # create or erase a debate by itself.
        evidence_classification = str(
            assessment.get("evidence_classification") or assessment.get("classification") or "no_debate"
        )
        detected_debate = evidence_classification == "debate"
        promoted = detected_debate and auto_promote
        classification = "debate_candidate" if detected_debate and not promoted else evidence_classification
        assessment.update(
            {
                "classification": classification,
                "evidence_classification": evidence_classification,
                "status": "mapped_debate" if promoted else classification,
                "promoted": promoted,
                "automation_status": "promoted" if promoted else ("candidate" if detected_debate else "not_applicable"),
                "positions": positions if detected_debate and positions else assessment.get("positions", []),
                "agreements": agreements if evidence_classification == "mapped_consensus" and agreements else assessment.get("agreements", []),
                "contradictions": contradictions if detected_debate and contradictions else assessment.get("contradictions", []),
                "contradiction_groups": (
                    [
                        {
                            "proposition": str(row.get("proposition") or row.get("contradiction") or row.get("text") or ""),
                            "positions": [],
                            "supporting_evidence": list(row.get("evidence", []) or []),
                        }
                        for row in contradictions
                    ]
                    if detected_debate and contradictions
                    else assessment.get("contradiction_groups", [])
                ),
                "boundaries": list(synthesis.get("boundary_conditions", []) or assessment.get("boundaries", [])),
                "method_fault_lines": list(
                    synthesis.get("methodological_fault_lines", []) or assessment.get("method_fault_lines", [])
                ),
                "synthesis_status": str(synthesis.get("status") or "deterministic_fallback"),
            }
        )
        assessments.append(assessment)
    debates = [row for row in assessments if row.get("promoted")]
    candidates = [row for row in assessments if row.get("classification") == "debate_candidate"]
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": sorted(candidates, key=lambda row: row["cluster_id"]),
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": len(candidates),
    }


_DIRECTIONAL_PROPOSITION_TOKENS = {
    "association",
    "conditional",
    "decrease",
    "decreased",
    "heterogeneous",
    "higher",
    "improve",
    "improved",
    "increase",
    "increased",
    "lower",
    "null",
    "reduce",
    "reduced",
    "significant",
    "support",
    "undermine",
    "vary",
}


def _claim_proposition_parts(claim: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    dimensions = claim.get("dimensions", {}) if isinstance(claim.get("dimensions"), Mapping) else {}
    topic_terms = _tokens(claim.get("topic", ""))
    outcome_terms = _tokens(dimensions.get("outcome", []))
    relationship_terms = _tokens(
        [
            claim.get("text", ""),
            dimensions.get("mechanism", []),
            dimensions.get("theory", []),
        ]
    )
    relationship_terms -= topic_terms | outcome_terms | _DIRECTIONAL_PROPOSITION_TOKENS
    return topic_terms, outcome_terms, relationship_terms


def _same_semantic_proposition(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_topic, left_outcome, left_relationship = _claim_proposition_parts(left)
    right_topic, right_outcome, right_relationship = _claim_proposition_parts(right)
    if left_topic and right_topic and not (left_topic & right_topic):
        return False
    if left_outcome and right_outcome and not (left_outcome & right_outcome):
        return False
    if left_relationship or right_relationship:
        if not left_relationship or not right_relationship:
            return False
        shared_relationship = left_relationship & right_relationship
        relationship_jaccard = len(shared_relationship) / max(1, len(left_relationship | right_relationship))
        if len(shared_relationship) < 2 or relationship_jaccard < 0.65:
            return False
    return bool(
        (left_topic & right_topic)
        or (left_outcome & right_outcome)
        or (left_relationship & right_relationship)
    )


def _shared_proposition_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    left_topic, left_outcome, left_relationship = _claim_proposition_parts(left)
    right_topic, right_outcome, right_relationship = _claim_proposition_parts(right)
    shared_topic = left_topic & right_topic
    if shared_topic:
        return " ".join(sorted(shared_topic))
    shared = (left_outcome & right_outcome) | (left_relationship & right_relationship)
    if not shared:
        shared = _tokens(left.get("text", "")) & _tokens(right.get("text", ""))
        shared -= _DIRECTIONAL_PROPOSITION_TOKENS
    return " ".join(sorted(shared))


def build_evidence_matrices(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build matrices whose cells contain only claim/source/locator-backed records."""
    rows = _ensure_profiles(profiles)
    by_source = {row["source_id"]: row for row in rows}
    matrices: list[dict[str, Any]] = []
    for cluster in clusters:
        entries_by_dimension: dict[str, list[dict[str, Any]]] = {}
        omitted_unlocated = 0
        for dimension in EVIDENCE_DIMENSIONS:
            by_value: dict[str, dict[str, Any]] = {}
            for source_id in cluster.get("source_ids", []) or []:
                for claim in by_source.get(str(source_id), {}).get("claims", []) or []:
                    values = claim.get("dimensions", {}).get(dimension, []) or []
                    if values and not claim.get("locator_complete"):
                        omitted_unlocated += 1
                        continue
                    for value in values:
                        identity = _canonical_phrase(value) or str(value).casefold()
                        cell = by_value.setdefault(identity, {"value": str(value), "evidence": []})
                        reference = _evidence_ref(claim)
                        if reference not in cell["evidence"]:
                            cell["evidence"].append(reference)
            entries = []
            for identity, cell in sorted(by_value.items()):
                evidence = sorted(cell["evidence"], key=lambda row: (row["source_id"], row["claim_id"], row["locator"]))
                entries.append(
                    {
                        "value_id": f"matrix-value-{_stable_hash([cluster['cluster_id'], dimension, identity])[:12]}",
                        "value": cell["value"],
                        "source_count": len({row["source_id"] for row in evidence}),
                        "study_family_count": len({row["study_family_id"] for row in evidence}),
                        "evidence": evidence,
                    }
                )
            entries_by_dimension[dimension] = entries
        matrices.append(
            {
                "matrix_id": f"matrix-{_stable_hash(cluster['cluster_id'])[:12]}",
                "cluster_id": cluster["cluster_id"],
                "dimensions": entries_by_dimension,
                "dimension_names": list(EVIDENCE_DIMENSIONS),
                "locator_backed_only": True,
                "omitted_unlocated_cell_count": omitted_unlocated,
            }
        )
    return sorted(matrices, key=lambda row: row["cluster_id"])


def build_debate_registry(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Classify debates only when two independently located positions exist."""
    rows = _ensure_profiles(profiles)
    by_source = {row["source_id"]: row for row in rows}
    assessments: list[dict[str, Any]] = []
    debates: list[dict[str, Any]] = []
    debate_candidates: list[dict[str, Any]] = []
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    for cluster in clusters:
        claims = [
            claim
            for source_id in cluster.get("source_ids", []) or []
            for claim in by_source.get(str(source_id), {}).get("claims", []) or []
            if claim.get("locator_complete")
        ]
        substantive = [
            claim
            for claim in claims
            if str(claim.get("direction") or "not_reported") not in {"not_reported", "mixed"}
        ]
        comparable_pairs = [
            (left, right)
            for left, right in combinations(substantive, 2)
            if _same_semantic_proposition(left, right)
            and str(left.get("study_family_id")) != str(right.get("study_family_id"))
        ]
        opposing_pairs = [
            (left, right)
            for left, right in comparable_pairs
            if str(left.get("direction")) != str(right.get("direction"))
        ]
        agreement_pairs = [
            (left, right)
            for left, right in comparable_pairs
            if str(left.get("direction")) == str(right.get("direction"))
        ]
        if opposing_pairs:
            classification = "debate"
        elif agreement_pairs:
            classification = "mapped_consensus"
        elif len(claims) >= 2:
            classification = "mixed_evidence"
        else:
            classification = "no_debate"

        debate_claims = {
            (str(claim.get("source_id")), str(claim.get("claim_id"))): claim
            for pair in opposing_pairs
            for claim in pair
        }
        agreement_claims = {
            (str(claim.get("source_id")), str(claim.get("claim_id"))): claim
            for pair in agreement_pairs
            for claim in pair
        }
        by_position: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for claim in debate_claims.values():
            by_position[str(claim.get("direction"))].append(claim)
        positions = []
        for direction, evidence in sorted(by_position.items()):
            refs = sorted((_evidence_ref(claim) for claim in evidence), key=lambda row: (row["source_id"], row["claim_id"]))
            positions.append({"position": direction, "evidence": refs})
        agreements = []
        by_agreement: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for claim in agreement_claims.values():
            by_agreement[str(claim.get("direction"))].append(claim)
        for direction, evidence in sorted(by_agreement.items()):
            agreements.append(
                {
                    "finding_direction": direction,
                    "evidence": sorted((_evidence_ref(claim) for claim in evidence), key=lambda row: (row["source_id"], row["claim_id"])),
                }
            )
        contradictions = []
        grouped_contradictions: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = defaultdict(dict)
        for left, right in sorted(
            opposing_pairs,
            key=lambda pair: (
                str(pair[0].get("study_family_id")),
                str(pair[0].get("source_id")),
                str(pair[1].get("study_family_id")),
                str(pair[1].get("source_id")),
            ),
        ):
            proposition = _shared_proposition_identity(left, right) or str(cluster.get("semantic_identity") or "")
            contradictions.append(
                {
                    "proposition": proposition,
                    "position_a": str(left.get("direction")),
                    "position_b": str(right.get("direction")),
                    "evidence": [_evidence_ref(left), _evidence_ref(right)],
                }
            )
            for claim in (left, right):
                grouped_contradictions[proposition][
                    (str(claim.get("source_id")), str(claim.get("claim_id")))
                ] = claim
        contradiction_groups = []
        for proposition, grouped_claims in sorted(grouped_contradictions.items()):
            grouped_positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for claim in grouped_claims.values():
                grouped_positions[str(claim.get("direction"))].append(_evidence_ref(claim))
            contradiction_groups.append(
                {
                    "proposition": proposition,
                    "positions": [
                        {
                            "position": direction,
                            "evidence": sorted(evidence, key=lambda row: (row["source_id"], row["claim_id"])),
                        }
                        for direction, evidence in sorted(grouped_positions.items())
                    ],
                    "supporting_evidence": sorted(
                        (_evidence_ref(claim) for claim in grouped_claims.values()),
                        key=lambda row: (row["source_id"], row["claim_id"]),
                    ),
                }
            )
        boundaries = []
        for claim in claims:
            boundary_values = [
                claim.get("boundary_condition"),
                *(claim.get("dimensions", {}).get("case", []) or []),
                *(claim.get("dimensions", {}).get("period", []) or []),
            ]
            for value in _flatten_values(boundary_values):
                boundaries.append({"boundary": value, "evidence": [_evidence_ref(claim)]})
        method_fault_lines = []
        if classification == "debate":
            seen_faults: set[tuple[str, str]] = set()
            for claim in debate_claims.values():
                for method in claim.get("dimensions", {}).get("method", []) or []:
                    key = (_canonical_phrase(method), str(claim.get("direction")))
                    if key in seen_faults:
                        continue
                    seen_faults.add(key)
                    method_fault_lines.append(
                        {"method": method, "finding_direction": claim.get("direction"), "evidence": [_evidence_ref(claim)]}
                    )
        detected_debate = classification == "debate"
        promoted = detected_debate and auto_promote
        visible_classification = "debate_candidate" if detected_debate and not promoted else classification
        assessment = {
            "debate_id": f"debate-{_stable_hash(cluster['cluster_id'])[:12]}",
            "cluster_id": cluster["cluster_id"],
            "classification": visible_classification,
            "evidence_classification": classification,
            "status": "mapped_debate" if promoted else ("debate_candidate" if detected_debate else classification),
            "promoted": promoted,
            "automation_status": "promoted" if promoted else ("candidate" if detected_debate else "not_applicable"),
            "positions": positions if detected_debate else [],
            "agreements": agreements,
            "contradictions": contradictions,
            "contradiction_groups": contradiction_groups,
            "boundaries": sorted(boundaries, key=lambda row: (row["boundary"], row["evidence"][0]["source_id"])),
            "method_fault_lines": sorted(method_fault_lines, key=lambda row: (str(row["method"]), str(row["finding_direction"]))),
            "evidence_claim_count": len(claims),
        }
        assessments.append(assessment)
        if promoted:
            debates.append(assessment)
        elif detected_debate:
            debate_candidates.append(assessment)
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": sorted(debate_candidates, key=lambda row: row["cluster_id"]),
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": len(debate_candidates),
    }


def _reasoner_proposals(
    reasoner: Any,
    profiles: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    request: Any = None,
) -> list[Any]:
    if reasoner is None:
        return []
    if isinstance(reasoner, Mapping):
        return list(reasoner.get("gap_candidates") or reasoner.get("gaps") or [])
    method = getattr(reasoner, "propose_gap_candidates", None) or getattr(reasoner, "propose_gaps", None)
    if callable(method):
        proposed = method(profiles=profiles, clusters=clusters)
        return list(proposed or [])
    detect = getattr(reasoner, "detect_gaps", None)
    if callable(detect) and request is not None:
        proposed = detect(profiles, request, context={"clusters": clusters})
        if isinstance(proposed, Mapping):
            return list(proposed.get("gap_candidates") or proposed.get("gaps") or [])
        return list(proposed or [])
    if callable(reasoner):
        proposed = reasoner(profiles=profiles, clusters=clusters)
        return list(proposed or [])
    return []


def _signal_evidence(
    signal: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    claim_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    def resolve_claim(claim_id: str, source_id: str = "") -> Mapping[str, Any] | None:
        if source_id and (source_id, claim_id) in claim_lookup:
            return claim_lookup[(source_id, claim_id)]
        profile_source_id = str((profile or {}).get("source_id") or "")
        if profile_source_id and (profile_source_id, claim_id) in claim_lookup:
            return claim_lookup[(profile_source_id, claim_id)]
        matches = [claim for (candidate_source, candidate_claim), claim in claim_lookup.items() if candidate_claim == claim_id]
        return matches[0] if len(matches) == 1 else None

    evidence: list[dict[str, Any]] = []
    for claim_id in _flatten_values(
        signal.get("supporting_claim_ids") or signal.get("claim_ids") or signal.get("claim_id")
    ):
        claim = resolve_claim(claim_id)
        if claim is not None:
            evidence.append(_evidence_ref(claim))
    for raw in signal.get("supporting_evidence", []) or signal.get("evidence", []) or []:
        item = _as_mapping(raw)
        claim = resolve_claim(str(item.get("claim_id", "")), str(item.get("source_id", "")))
        if claim:
            evidence.append(_evidence_ref(claim))
    if not evidence and profile is not None:
        signal_topic = _tokens(signal.get("topic") or signal.get("semantic_identity") or "")
        signal_subject = _tokens(
            [
                signal.get("topic", ""),
                signal.get("semantic_identity", ""),
                signal.get("precise_missing_evidence", ""),
                signal.get("missing_evidence", ""),
                signal.get("gap_text", ""),
            ]
        )
        for claim in profile.get("claims", []) or []:
            claim_topic = _tokens(claim.get("topic", ""))
            claim_subject = _tokens(
                [
                    claim.get("topic", ""),
                    claim.get("text", ""),
                    claim.get("dimensions", {}),
                ]
            )
            shared_subject = signal_subject & claim_subject
            topic_match = bool(signal_topic & claim_topic)
            semantic_match = len(shared_subject) >= 2 or (
                len(shared_subject) == 1 and min(len(signal_subject), len(claim_subject)) == 1
            )
            if topic_match or semantic_match:
                evidence.append(_evidence_ref(claim))
    unique = {(_stable_hash(row)): row for row in evidence}
    return sorted(unique.values(), key=lambda row: (row["source_id"], row["claim_id"], row["locator"]))


_GENERIC_GAP_SUPPORT_TOKENS = {
    "causal",
    "conflict",
    "effectiveness",
    "evidence",
    "mediation",
    "mediator",
    "outcome",
    "peace",
    "success",
    "systematic",
}


def _gap_support_is_relevant(candidate: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    primary_terms = _tokens(candidate.get("topic") or candidate.get("gap_statement") or "")
    subject_terms = _tokens(
        [
            candidate.get("topic", ""),
            candidate.get("gap_statement", ""),
            candidate.get("precise_missing_evidence", ""),
            candidate.get("observed_pattern", ""),
        ]
    )
    claim_terms = _tokens(
        [
            claim.get("topic", ""),
            claim.get("text", ""),
            claim.get("dimensions", {}),
        ]
    )
    primary_overlap = primary_terms & claim_terms
    subject_overlap = subject_terms & claim_terms
    return bool(
        len(primary_overlap) >= 2
        or (primary_overlap - _GENERIC_GAP_SUPPORT_TOKENS)
        or len(subject_overlap) >= 3
    )


def generate_gap_candidates(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    debate_registry: Mapping[str, Any],
    evidence_matrices: Sequence[Mapping[str, Any]] | None = None,
    *,
    reasoner: Any = None,
    request: Any = None,
    cluster_syntheses: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate only candidates allowed by GAP_RULES; no per-cluster fallback exists."""
    rows = _ensure_profiles(profiles)
    claim_lookup = {
        (str(claim["source_id"]), str(claim["claim_id"])): claim
        for row in rows
        for claim in row["claims"]
    }
    clusters_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cluster in clusters:
        for source_id in cluster.get("source_ids", []) or []:
            clusters_by_source[str(source_id)].append(cluster)
    proposed: list[tuple[dict[str, Any], Mapping[str, Any] | None, str]] = []
    for profile in rows:
        if not profile["analytical"]:
            continue
        for signal in profile.get("gap_signals", []) or []:
            item = _as_mapping(signal)
            item.setdefault("related_cluster_ids", _related_clusters_for_gap(item, profile, clusters_by_source))
            if not item.get("related_cluster_ids"):
                item.setdefault(
                    "collection_level_rationale",
                    "A specific structured profile signal comes from an analytical source outside an admitted cluster.",
                )
            proposed.append((item, profile, "structured_profile_signal"))
        for signal in profile.get("author_stated_gaps", []) or []:
            item = _as_mapping(signal) if not isinstance(signal, str) else {"missing_evidence": signal}
            item["rule"] = "author_stated_gap"
            item.setdefault("topic", next(iter(profile.get("semantic_topic_scores", {})), ""))
            item.setdefault("related_cluster_ids", _related_clusters_for_gap(item, profile, clusters_by_source))
            if not item.get("related_cluster_ids"):
                item.setdefault(
                    "collection_level_rationale",
                    "An author-stated research need comes from an analytical source outside an admitted cluster.",
                )
            origin = str(item.pop("_author_gap_origin", "author_stated_gap") or "author_stated_gap")
            proposed.append((item, profile, origin))
    for cluster_id, synthesis in sorted((cluster_syntheses or {}).items()):
        for signal in synthesis.get("gap_hypotheses", []) or []:
            item = _as_mapping(signal)
            item.setdefault("related_cluster_ids", [cluster_id])
            proposed.append((item, None, "cluster_synthesis"))
    for signal in _reasoner_proposals(reasoner, rows, clusters, request):
        proposed.append((_as_mapping(signal), None, "reasoner_proposal"))

    cluster_by_id = {str(row["cluster_id"]): row for row in clusters}
    for debate in debate_registry.get("debates", []) or []:
        cluster = cluster_by_id.get(str(debate.get("cluster_id")), {})
        groups = list(debate.get("contradiction_groups", []) or [])
        if not groups:
            groups = [
                {
                    "proposition": cluster.get("semantic_identity", debate.get("cluster_id", "")),
                    "supporting_evidence": [
                        reference
                        for position in debate.get("positions", []) or []
                        for reference in position.get("evidence", []) or []
                    ],
                }
            ]
        for group in groups:
            proposition = str(
                group.get("proposition")
                or cluster.get("semantic_identity")
                or debate.get("cluster_id", "")
            )
            proposed.append(
                (
                    {
                        "rule": "contradictory_findings",
                        "topic": proposition,
                        "missing_evidence": (
                            f"Comparable evidence that adjudicates the opposing findings about {proposition} "
                            "under matched cases, measures, and periods."
                        ),
                        "related_cluster_ids": [debate.get("cluster_id")],
                        "supporting_evidence": list(group.get("supporting_evidence", []) or []),
                        "why_matters": (
                            f"The collection reports opposing directions for the same proposition about {proposition}."
                        ),
                        "contribution": (
                            f"A matched design could determine when the mapped relationship involving {proposition} changes direction."
                        ),
                    },
                    None,
                    "debate_rule",
                )
            )

    for matrix in evidence_matrices or ():
        methods = matrix.get("dimensions", {}).get("method", []) or []
        evidence = [reference for entry in methods for reference in entry.get("evidence", []) or []]
        families = {str(row.get("study_family_id")) for row in evidence}
        if len(methods) == 1 and len(families) >= 2:
            cluster = cluster_by_id.get(str(matrix.get("cluster_id")), {})
            proposed.append(
                (
                    {
                        "rule": "methodological_concentration",
                        "topic": cluster.get("semantic_identity", matrix.get("cluster_id", "")),
                        "missing_evidence": f"Evidence using methods other than {methods[0]['value']}.",
                        "related_cluster_ids": [matrix.get("cluster_id")],
                        "supporting_evidence": evidence,
                        "why_matters": "The mapped result may depend on one methodological family.",
                        "contribution": "A methodologically distinct test of the same mapped relationship.",
                    },
                    None,
                    "methodological_concentration_rule",
                )
            )

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for signal, profile, origin in proposed:
        rule = str(signal.get("rule") or signal.get("gap_type") or "")
        if rule not in GAP_RULES:
            continue
        topic = str(signal.get("topic") or signal.get("semantic_identity") or "").strip()
        missing = str(signal.get("precise_missing_evidence") or signal.get("missing_evidence") or signal.get("gap_text") or "").strip()
        if not topic or not missing:
            continue
        key = (rule, _canonical_phrase(topic), _canonical_phrase(missing))
        candidate = grouped.setdefault(
            key,
            {
                "gap_id": f"gap-{slugify(rule)}-{_stable_hash(key)[:12]}",
                "rule": rule,
                "topic": topic,
                "precise_missing_evidence": missing,
                "related_cluster_ids": [],
                "supporting_evidence": [],
                "countervailing_evidence": [],
                "observed_pattern": str(signal.get("observed_pattern") or signal.get("observed_evidence") or ""),
                "generation_explanation": str(signal.get("generation_explanation") or ""),
                "evidence_needed": str(signal.get("evidence_needed") or signal.get("study_needed") or missing),
                "why_matters": str(signal.get("why_matters") or ""),
                "contribution": str(signal.get("contribution") or ""),
                "proposal_origins": [],
                "collection_level_rationale": str(signal.get("collection_level_rationale") or ""),
            },
        )
        candidate["related_cluster_ids"].extend(_flatten_values(signal.get("related_cluster_ids") or signal.get("related_clusters")))
        candidate["supporting_evidence"].extend(_signal_evidence(signal, profile, claim_lookup))
        candidate["proposal_origins"].append(origin)
        if not candidate["collection_level_rationale"] and signal.get("collection_level_rationale"):
            candidate["collection_level_rationale"] = str(signal["collection_level_rationale"])
    result = []
    for candidate in grouped.values():
        candidate["related_cluster_ids"] = sorted(
            {str(value) for value in candidate["related_cluster_ids"] if str(value) in cluster_by_id}
        )
        candidate["supporting_evidence"] = sorted(
            {_stable_hash(row): row for row in candidate["supporting_evidence"]}.values(),
            key=lambda row: (row["source_id"], row["claim_id"], row["locator"]),
        )
        candidate["supporting_evidence"] = [
            reference
            for reference in candidate["supporting_evidence"]
            if (
                (claim := claim_lookup.get((str(reference.get("source_id") or ""), str(reference.get("claim_id") or ""))))
                is not None
                and _gap_support_is_relevant(candidate, claim)
            )
        ]
        candidate["proposal_origins"] = sorted(set(candidate["proposal_origins"]))
        candidate["generation_explanation"] = candidate["generation_explanation"] or (
            f"Generated by the {candidate['rule'].replace('_', ' ')} rule from "
            f"{', '.join(candidate['proposal_origins'])}."
        )
        if not candidate["observed_pattern"] and candidate["supporting_evidence"]:
            supporting_claims = [
                claim_lookup[(str(reference["source_id"]), str(reference["claim_id"]))]
                for reference in candidate["supporting_evidence"]
                if (str(reference["source_id"]), str(reference["claim_id"])) in claim_lookup
            ]
            supporting_claims.sort(key=lambda claim: (str(claim["source_id"]), str(claim["claim_id"])))
            claim_texts = [str(claim.get("text") or "") for claim in supporting_claims]
            candidate["observed_pattern"] = " ".join(text for text in claim_texts if text)[:1_200]
        candidate["why_matters"] = candidate["why_matters"] or (
            f"Without {candidate['precise_missing_evidence'].rstrip('.')}, the collection cannot resolve "
            f"the mapped question about {candidate['topic']}."
        )
        candidate["contribution"] = candidate["contribution"] or (
            f"Evidence that supplies {candidate['evidence_needed'].rstrip('.')} would fill the identified collection-level omission."
        )
        candidate["specificity_errors"] = _gap_specificity_errors(candidate)
        candidate["specificity_status"] = "qualified" if not candidate["specificity_errors"] else "underspecified_gap"
        result.append(candidate)
    return sorted(result, key=lambda row: row["gap_id"])


def _related_clusters_for_gap(
    signal: Mapping[str, Any],
    profile: Mapping[str, Any],
    clusters_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    candidates = list(clusters_by_source.get(str(profile.get("source_id") or ""), []) or [])
    if not candidates:
        return []
    terms = _tokens(
        signal.get("topic")
        or signal.get("precise_missing_evidence")
        or signal.get("missing_evidence")
        or signal.get("gap_text")
        or ""
    )
    ranked = [
        (
            len(terms & _tokens([cluster.get("semantic_identity", ""), cluster.get("label", "")])),
            str(cluster.get("cluster_id") or ""),
        )
        for cluster in candidates
    ]
    best = max((score for score, _ in ranked), default=0)
    if best > 0:
        return sorted(cluster_id for score, cluster_id in ranked if score == best and cluster_id)
    return [str(candidates[0].get("cluster_id") or "")] if len(candidates) == 1 else []


_VAGUE_GAP = re.compile(
    r"^(?:more|further|additional) research(?: is needed)?$|"
    r"^unobserved factors?(?: affecting .*)?$|"
    r"^(?:limited|insufficient|more) data$|"
    r"^need for (?:comparative )?analysis$|"
    r"^future studies$",
    flags=re.IGNORECASE,
)


def _gap_specificity_errors(candidate: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    topic = str(candidate.get("topic") or "").strip()
    missing = str(candidate.get("precise_missing_evidence") or "").strip()
    if len(_tokens(topic)) < 2 and not candidate.get("related_cluster_ids"):
        errors.append("topic_not_specific")
    if len(_tokens(missing)) < 4:
        errors.append("missing_evidence_not_specific")
    if _VAGUE_GAP.fullmatch(missing.strip(" .")):
        errors.append("generic_gap_language")
    if not candidate.get("related_cluster_ids") and not candidate.get("collection_level_rationale"):
        errors.append("missing_cluster_or_collection_rationale")
    evidence = list(candidate.get("supporting_evidence", []) or [])
    if not evidence or not all(
        row.get("source_id") and row.get("claim_id") and _complete_locator(row.get("locator"))
        for row in evidence
    ):
        errors.append("missing_locator_backed_generation_evidence")
    rule = str(candidate.get("rule") or "")
    if rule == "cross_cluster_integration" and len(set(candidate.get("related_cluster_ids", []) or [])) < 2:
        errors.append("cross_cluster_gap_requires_two_clusters")
    if rule == "contradictory_findings" and len({
        str(row.get("study_family_id") or row.get("source_id")) for row in evidence
    }) < 2:
        errors.append("contradiction_requires_two_study_families")
    return sorted(set(errors))


def _gap_rule_admission_errors(
    candidate: Mapping[str, Any],
    complete_support: Sequence[Mapping[str, Any]],
    claim_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    """Enforce rule-specific evidence relationships before promotion.

    Generic source and locator thresholds cannot establish a contradiction:
    the cited claims must describe the same proposition, come from distinct
    study families, and point in different substantive directions.
    """

    rule = str(candidate.get("rule") or "")
    if rule != "contradictory_findings":
        return []
    claims = [
        claim
        for reference in complete_support
        if (
            claim := claim_lookup.get(
                (str(reference.get("source_id") or ""), str(reference.get("claim_id") or ""))
            )
        )
        is not None
        and str(claim.get("direction") or "not_reported") not in {"not_reported", "mixed"}
    ]
    opposing_comparable_pair = any(
        str(left.get("study_family_id") or left.get("source_id"))
        != str(right.get("study_family_id") or right.get("source_id"))
        and str(left.get("direction")) != str(right.get("direction"))
        and _same_semantic_proposition(left, right)
        for left, right in combinations(claims, 2)
    )
    return [] if opposing_comparable_pair else ["contradiction_requires_opposing_comparable_claims"]


def _gap_search_terms(candidate: Mapping[str, Any]) -> list[str]:
    terms = _tokens(
        [
            candidate.get("rule", ""),
            candidate.get("topic", ""),
            candidate.get("precise_missing_evidence", ""),
        ]
    )
    return sorted(terms)


def _gap_subject_terms(candidate: Mapping[str, Any]) -> set[str]:
    return _tokens([candidate.get("topic", ""), candidate.get("precise_missing_evidence", "")])


def _answer_matches(candidate: Mapping[str, Any], answer: Any) -> tuple[str, dict[str, Any] | None]:
    item = _as_mapping(answer) if not isinstance(answer, str) else {"text": answer}
    rule = str(item.get("rule") or item.get("gap_rule") or "")
    answer_tokens = _tokens([item.get("topic", ""), item.get("text", ""), item.get("answer", "")])
    candidate_tokens = _gap_subject_terms(candidate)
    if item.get("gap_id") and str(item.get("gap_id")) == str(candidate.get("gap_id")):
        subject_match = True
    else:
        subject_match = bool(candidate_tokens & answer_tokens)
    if rule and rule != candidate.get("rule"):
        return "none", None
    if not subject_match:
        return "none", None
    status = str(item.get("status") or item.get("answer_status") or "answered").casefold()
    if status in {"partial", "partially_answered", "narrows"}:
        match = "partial"
    else:
        match = "answered"
    reference = {
        "claim_id": str(item.get("claim_id") or ""),
        "source_id": str(item.get("source_id") or ""),
        "study_family_id": str(item.get("study_family_id") or item.get("source_id") or ""),
        "locator": _locator_text(item.get("locator")),
    }
    return match, reference


def _profile_locator_completeness(profile: Mapping[str, Any]) -> float:
    claims = profile.get("claims", []) or []
    if not claims:
        return 0.0
    return sum(1 for claim in claims if claim.get("locator_complete")) / len(claims)


def semantic_closest_prior(
    candidate: Mapping[str, Any],
    profiles: Sequence[Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank closest prior sources by deterministic semantic overlap and locator quality."""
    rows = [row for row in _ensure_profiles(profiles) if row["analytical"]]
    terms = set(_gap_search_terms(candidate))
    family_counts = Counter(row["study_family_id"] for row in rows)
    ranked: list[dict[str, Any]] = []
    for profile in rows:
        profile_terms = set(profile["search_tokens"])
        overlap = sorted(terms & profile_terms)
        if not overlap:
            continue
        union = terms | profile_terms
        semantic_score = len(overlap) / max(1, len(union))
        locator_completeness = _profile_locator_completeness(profile)
        confidence = min(0.99, 0.2 + semantic_score * 0.65 + locator_completeness * 0.15)
        ranked.append(
            {
                "prior_id": f"prior-{_stable_hash(profile['source_id'])[:12]}",
                "source_id": profile["source_id"],
                "note_id": profile["note_id"],
                "title": profile["title"],
                "note_path": profile["note_path"],
                "study_family_id": profile["study_family_id"],
                "source_count": family_counts[profile["study_family_id"]],
                "locator_completeness": round(locator_completeness, 3),
                "confidence": round(confidence, 3),
                "semantic_overlap": overlap,
                "overlap_explanation": f"Matched collection terms: {', '.join(overlap)}.",
            }
        )
    ranked.sort(key=lambda row: (-row["confidence"], -row["locator_completeness"], -row["source_count"], row["prior_id"]))
    return ranked[: max(0, limit)]


def search_and_validate_gaps(
    candidates: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
    *,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Search every analytical profile before deterministic promotion or rejection."""
    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row["analytical"]]
    limited = [row for row in rows if row["limited"]]
    claim_lookup = {
        (str(claim["source_id"]), str(claim["claim_id"])): claim
        for row in rows
        for claim in row.get("claims", []) or []
    }
    min_families = max(2, int(_policy_value(policy, ("min_gap_support_families", "gap_promotion_min_sources"), 2)))
    prior_limit = int(_policy_value(policy, "closest_prior_limit", 5))
    validated: list[dict[str, Any]] = []
    search_log: list[dict[str, Any]] = []
    for raw_candidate in sorted(candidates, key=lambda row: str(row.get("gap_id"))):
        candidate = dict(raw_candidate)
        terms = set(_gap_search_terms(candidate))
        supporting_source_ids = {str(row.get("source_id")) for row in candidate.get("supporting_evidence", []) or []}
        results: list[dict[str, Any]] = []
        full_answers: list[dict[str, Any]] = []
        partial_answers: list[dict[str, Any]] = []
        countervailing: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        for profile in analytical:
            overlap = sorted(terms & set(profile["search_tokens"]))
            answer_status = "none"
            answer_reference: dict[str, Any] | None = None
            for answer in profile.get("gap_answers", []) or []:
                status, reference = _answer_matches(candidate, answer)
                if status != "none":
                    answer_status, answer_reference = status, reference
                    break
            if answer_status == "none":
                for claim in profile.get("claims", []) or []:
                    if not claim.get("addresses_gap"):
                        continue
                    if claim.get("gap_rule") and claim.get("gap_rule") != candidate.get("rule"):
                        continue
                    claim_terms = _tokens(
                        [
                            claim.get("topic", ""),
                            claim.get("text", ""),
                            claim.get("dimensions", {}),
                        ]
                    )
                    if not (_gap_subject_terms(candidate) & claim_terms):
                        continue
                    answer_status = "partial" if claim.get("answer_status") in {"partial", "partially_answered"} else "answered"
                    answer_reference = _evidence_ref(claim)
                    break
            if answer_reference is not None:
                answer_reference = {**answer_reference, "source_id": profile["source_id"], "study_family_id": profile["study_family_id"]}
                countervailing.append(answer_reference)
                if _complete_locator(answer_reference.get("locator")):
                    (full_answers if answer_status == "answered" else partial_answers).append(answer_reference)
                else:
                    warnings.append({"warning": "possible_answer_requires_locator", "source_id": profile["source_id"]})
            if answer_status == "answered":
                result_status = "answers_gap" if _complete_locator((answer_reference or {}).get("locator")) else "possible_answer_unlocated"
            elif answer_status == "partial":
                result_status = "partially_answers_gap" if _complete_locator((answer_reference or {}).get("locator")) else "possible_answer_unlocated"
            elif profile["source_id"] in supporting_source_ids:
                result_status = "supports_gap_rule"
            elif overlap:
                result_status = "relevant_not_answering"
            else:
                result_status = "no_match"
            results.append(
                {
                    "source_id": profile["source_id"],
                    "study_family_id": profile["study_family_id"],
                    "status": result_status,
                    "semantic_overlap": overlap,
                }
            )

        for profile in limited:
            overlap = sorted(terms & set(profile["search_tokens"]))
            if not overlap:
                continue
            warning = {
                "warning": "possible_counterevidence_requires_full_text",
                "source_id": profile["source_id"],
                "note_status": profile["note_status"],
                "semantic_overlap": overlap,
            }
            warnings.append(warning)

        analytical_source_ids = {row["source_id"] for row in analytical}
        raw_support = list(candidate.get("supporting_evidence", []) or [])
        support = [row for row in raw_support if str(row.get("source_id")) in analytical_source_ids]
        for row in raw_support:
            if str(row.get("source_id")) not in analytical_source_ids:
                warnings.append(
                    {
                        "warning": "possible_counterevidence_requires_full_text",
                        "source_id": str(row.get("source_id") or ""),
                    }
                )
        complete_support = [row for row in support if _complete_locator(row.get("locator")) and row.get("claim_id") and row.get("source_id")]
        support_families = {str(row.get("study_family_id") or row.get("source_id")) for row in complete_support}
        locator_completeness = len(complete_support) / len(support) if support else 0.0
        rule_admission_errors = _gap_rule_admission_errors(candidate, complete_support, claim_lookup)
        if rule_admission_errors:
            status = "rejected_rule_admission"
            promoted = False
            decision = "reject_rule_admission"
        elif full_answers:
            status = "rejected_answered_elsewhere"
            promoted = False
            decision = "reject"
        elif partial_answers:
            status = "narrowed_gap_lead"
            promoted = False
            decision = "narrow"
        elif (
            bool(_policy_value(policy, "auto_promote_gaps", True))
            and len(support_families) >= min_families
            and len(complete_support) >= min_families
            and locator_completeness == 1.0
            and not rule_admission_errors
        ):
            status = "mapped_collection_gap"
            promoted = True
            decision = "promote"
        else:
            status = "gap_lead"
            promoted = False
            decision = "retain_lead"
        rule_result = {
            "rule": candidate["rule"],
            "candidate_valid": candidate["rule"] in GAP_RULES and not rule_admission_errors,
            "rule_specific_admission_passed": not rule_admission_errors,
            "rule_admission_errors": rule_admission_errors,
            "independent_supporting_sources": len({row.get("source_id") for row in complete_support}),
            "independent_study_families": len(support_families),
            "complete_locator_count": len(complete_support),
            "locator_completeness": round(locator_completeness, 3),
            "collection_search_complete": True,
            "analytical_profile_count_searched": len(analytical),
            "answered_elsewhere_count": len(full_answers),
            "partially_answered_elsewhere_count": len(partial_answers),
            "decision": decision,
        }
        closest = semantic_closest_prior(candidate, analytical, limit=prior_limit)
        candidate.update(
            {
                "status": status,
                "scope": "collection_only",
                "promoted": promoted,
                "automation_status": "promoted" if promoted else ("rejected" if decision == "reject" else "lead"),
                "novelty_claimed": False,
                "rule_results": [rule_result],
                "supporting_evidence": support,
                "observed_evidence": support,
                "countervailing_evidence": sorted(countervailing, key=lambda row: (row["source_id"], row.get("claim_id", ""))),
                "internal_search_terms": sorted(terms),
                "internal_search_results": results,
                "closest_prior_work": closest,
                "warnings": sorted(warnings, key=lambda row: (row["warning"], row["source_id"])),
                "promotion_metadata": {
                    "scope": "collection_only",
                    "promoted": promoted,
                    "novelty_claimed": False,
                    "rule_results": [rule_result],
                    "precise_missing_evidence": candidate["precise_missing_evidence"],
                    "supporting_locators": [row for row in support if row.get("locator")],
                    "countervailing_locators": [row for row in countervailing if row.get("locator")],
                    "internal_search": {"terms": sorted(terms), "results": results},
                    "why_matters": candidate["why_matters"],
                    "contribution": candidate["contribution"],
                },
            }
        )
        validated.append(candidate)
        search_log.append(
            {
                "search_id": f"search-{_stable_hash(candidate['gap_id'])[:12]}",
                "gap_id": candidate["gap_id"],
                "terms": sorted(terms),
                "analytical_profile_count_searched": len(analytical),
                "results": results,
                "limited_profile_warnings": [row for row in warnings if row["warning"] == "possible_counterevidence_requires_full_text"],
                "complete": True,
            }
        )
    return validated, search_log


def rank_gap_registry(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering by decision, confidence, source count, locators, and stable ID."""
    status_order = {"mapped_collection_gap": 0, "narrowed_gap_lead": 1, "gap_lead": 2, "rejected_answered_elsewhere": 3}
    rows: list[dict[str, Any]] = []
    for gap in gaps:
        row = dict(gap)
        result = (row.get("rule_results") or [{}])[0]
        assessment = _as_mapping(row.get("value_assessment"))
        design = _as_mapping(row.get("study_design"))
        information_gain = str(assessment.get("information_gain") or "low")
        design_fields = (
            "research_question",
            "estimand",
            "unit_of_analysis",
            "target_population",
            "exposure_or_treatment",
            "comparator",
            "identification_or_inference_strategy",
            "data_route",
            "feasibility",
        )
        design_completeness = sum(bool(str(design.get(field) or "").strip()) for field in design_fields) / len(
            design_fields
        )
        closest_confidence = max((float(item.get("confidence", 0)) for item in row.get("closest_prior_work", []) or []), default=0.0)
        source_count = int(result.get("independent_study_families", 0))
        locator_completeness = float(result.get("locator_completeness", 0))
        confidence = min(0.99, 0.35 + 0.12 * min(source_count, 4) + 0.25 * locator_completeness + 0.1 * closest_confidence)
        confidence_tier = "high" if confidence >= 0.8 else ("moderate" if confidence >= 0.6 else "low")
        row["ranking"] = {
            "confidence": round(confidence, 3),
            "confidence_tier": confidence_tier,
            "source_count": source_count,
            "locator_completeness": round(locator_completeness, 3),
            "stable_id": row["gap_id"],
            "information_gain": information_gain,
            "design_completeness": round(design_completeness, 3),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            status_order.get(str(row.get("status")), 9),
            {"high": 0, "moderate": 1, "low": 2}.get(row["ranking"]["information_gain"], 3),
            -row["ranking"]["design_completeness"],
            -row["ranking"]["source_count"],
            -row["ranking"]["locator_completeness"],
            row["gap_id"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _reasoner_stage(
    reasoner: Any,
    reasoner_call: Any,
    *,
    stage: str,
    key: str,
    method_name: str,
    profiles: Sequence[Any],
    request: Any,
    context: Mapping[str, Any],
) -> Mapping[str, Any]:
    if reasoner is None:
        return {}
    if reasoner_call is not None:
        try:
            return reasoner_call(stage, key, method_name, profiles, context)
        except LiteratureSynthesisPartialError:
            raise
        except Exception:
            # A paid, checkpointed synthesis failure is resumable work, not
            # evidence that the deterministic fallback is an equivalent
            # completed result. Let the pipeline mark the invocation partial.
            raise
    if isinstance(reasoner, Mapping):
        if stage == "cluster_proposal":
            return {"clusters": list(reasoner.get("cluster_proposals") or reasoner.get("clusters") or [])}
        if stage == "cluster_synthesis":
            syntheses = reasoner.get("cluster_syntheses", {})
            return dict(syntheses.get(key, {})) if isinstance(syntheses, Mapping) else {}
        if stage == "gap_adjudication":
            return {
                "gaps": list(reasoner.get("gap_rationales") or reasoner.get("gaps") or []),
                "rejected": list(reasoner.get("rejected_gap_rationales") or reasoner.get("rejected") or []),
            }
        return {}
    method = getattr(reasoner, method_name, None)
    if not callable(method) or request is None:
        return {}
    try:
        value = method(profiles, request, context=context)
    except LiteratureSynthesisPartialError:
        raise
    except Exception:
        return {}
    return _as_mapping(value) if value else {}


def _cluster_item_text(row: Mapping[str, Any]) -> str:
    for key in (
        "technical_finding",
        "finding",
        "claim",
        "proposition",
        "position",
        "agreement",
        "contradiction",
        "boundary",
        "fault_line",
        "methodological_fault_line",
        "relationship",
        "role",
        "text",
        "summary",
    ):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _gap_anchor_catalog(
    cluster_syntheses: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    catalog: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cluster_id, synthesis in cluster_syntheses.items():
        for section in CLUSTER_SYNTHESIS_SECTIONS:
            for row in synthesis.get(section, []) or []:
                if not isinstance(row, Mapping) or not row.get("item_id"):
                    continue
                catalog[(str(cluster_id), section, str(row["item_id"]))] = dict(row)
    return catalog


def _resolve_gap_anchors(
    gap: Mapping[str, Any],
    proposed_anchors: Sequence[Mapping[str, Any]],
    cluster_syntheses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Validate supplied anchors and deterministically derive missing cluster anchors."""

    catalog = _gap_anchor_catalog(cluster_syntheses)
    related = {str(value) for value in gap.get("related_cluster_ids", []) or []}
    support_keys = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in gap.get("supporting_evidence", []) or []
    }
    accepted: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw in proposed_anchors:
        anchor = _as_mapping(raw)
        key = (
            str(anchor.get("cluster_id") or ""),
            str(anchor.get("section") or ""),
            str(anchor.get("item_id") or ""),
        )
        row = catalog.get(key)
        if row is None or key[0] not in related:
            continue
        row_evidence = {
            (str(ref.get("source_id") or ""), str(ref.get("claim_id") or ""))
            for ref in row.get("evidence", []) or []
        }
        if support_keys and not (row_evidence & support_keys):
            continue
        accepted[key] = {"cluster_id": key[0], "section": key[1], "item_id": key[2]}

    gap_terms = _tokens(
        [
            gap.get("gap_statement", ""),
            gap.get("precise_missing_evidence", ""),
            gap.get("observed_pattern", ""),
        ]
    )
    preferred_sections = {
        "contradictory_findings": ("contradictions", "positions"),
        "untested_mechanism": ("central_findings", "positions"),
        "empirical_coverage": ("boundary_conditions", "central_findings"),
        "methodological_concentration": ("methodological_fault_lines",),
        "measurement_or_data": ("methodological_fault_lines",),
        "boundary_condition": ("boundary_conditions", "contradictions"),
        "replication": ("central_findings", "agreements"),
        "cross_cluster_integration": ("related_clusters",),
        "author_stated_gap": ("central_findings", "source_roles"),
    }.get(str(gap.get("rule") or ""), ())
    anchored_clusters = {row["cluster_id"] for row in accepted.values()}
    for cluster_id in sorted(related - anchored_clusters):
        candidates: list[tuple[int, str, str, Mapping[str, Any]]] = []
        for (candidate_cluster, section, item_id), row in catalog.items():
            if candidate_cluster != cluster_id:
                continue
            evidence_keys = {
                (str(ref.get("source_id") or ""), str(ref.get("claim_id") or ""))
                for ref in row.get("evidence", []) or []
            }
            evidence_overlap = len(evidence_keys & support_keys)
            if support_keys and evidence_overlap == 0:
                continue
            semantic_overlap = len(gap_terms & _tokens(_cluster_item_text(row)))
            preference = len(preferred_sections) - preferred_sections.index(section) if section in preferred_sections else 0
            candidates.append((100 * evidence_overlap + 20 * preference + semantic_overlap, section, item_id, row))
        if candidates:
            _, section, item_id, _ = max(candidates, key=lambda value: (value[0], value[1], value[2]))
            key = (cluster_id, section, item_id)
            accepted[key] = {"cluster_id": cluster_id, "section": section, "item_id": item_id}
    return sorted(accepted.values(), key=lambda row: (row["cluster_id"], row["section"], row["item_id"]))


def _gap_quality_errors(gap: Mapping[str, Any], *, require_design: bool) -> list[str]:
    assessment = _as_mapping(gap.get("value_assessment"))
    design = _as_mapping(gap.get("study_design"))
    errors: list[str] = []
    assessment_text_fields = (
        "puzzle_type",
        "puzzle",
        "strongest_obvious_answer",
        "why_obvious_answer_is_inadequate",
        "decision_or_inference_changed",
    )
    for field_name in assessment_text_fields:
        if len(_tokens(assessment.get(field_name, ""))) < 2:
            errors.append(f"missing_value_assessment_{field_name}")
    if not assessment.get("competing_explanations"):
        errors.append("missing_competing_explanations")
    if assessment.get("information_gain") not in {"high", "moderate"}:
        errors.append("insufficient_information_gain")
    if assessment.get("non_obviousness_passed") is not True:
        errors.append("obvious_answer_not_falsified")
    if assessment.get("importance_passed") is not True:
        errors.append("importance_not_established")
    errors.extend(str(value) for value in assessment.get("rejection_reasons", []) or [] if str(value))

    if require_design:
        required_text = (
            "design_type",
            "research_question",
            "estimand",
            "unit_of_analysis",
            "target_population",
            "exposure_or_treatment",
            "comparator",
            "identification_or_inference_strategy",
            "data_route",
            "feasibility",
            "ethical_constraints",
        )
        for field_name in required_text:
            if len(_tokens(design.get(field_name, ""))) < 2:
                errors.append(f"missing_study_design_{field_name}")
        for field_name in (
            "outcomes",
            "mechanism_measures",
            "confounders_or_rival_explanations",
            "falsification_or_process_tests",
            "validity_risks",
        ):
            if not design.get(field_name):
                errors.append(f"missing_study_design_{field_name}")
        strategy = str(design.get("identification_or_inference_strategy") or "").strip()
        if re.fullmatch(
            r"(?:mixed methods?|process tracing|controlled comparison|case study|regression|experiment)",
            strategy,
            flags=re.I,
        ):
            errors.append("nonexecutable_identification_strategy_label_only")
    return sorted(set(errors))


def _normalize_checkpoint_scalar(value: Any) -> str:
    """Clean one-item arrays stringified by older provider adapters."""

    text = str(value or "").strip()
    if len(text) >= 4 and text[:2] in {"['", '["'} and text[-2:] in {"']", '"]'}:
        return text[2:-2].strip()
    return text


def _normalize_gap_nested_scalars(values: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    normalized = dict(values)
    for field_name in fields:
        normalized[field_name] = _normalize_checkpoint_scalar(normalized.get(field_name))
    return normalized


def _gap_structured_signature(gap: Mapping[str, Any]) -> str:
    design = _as_mapping(gap.get("study_design"))
    dimensions = {
        "rule": str(gap.get("rule") or ""),
        "exposure": _canonical_phrase(design.get("exposure_or_treatment", "")),
        "mechanisms": sorted(_canonical_phrase(value) for value in design.get("mechanism_measures", []) or []),
        "outcomes": sorted(_canonical_phrase(value) for value in design.get("outcomes", []) or []),
        "population": _canonical_phrase(design.get("target_population", "")),
        "setting": _canonical_phrase([design.get("unit_of_analysis", ""), gap.get("topic", "")]),
    }
    return _stable_hash(dimensions)


def _merge_candidates_are_compatible(canonical: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    if str(canonical.get("rule") or "") != str(candidate.get("rule") or ""):
        return False
    canonical_topic = _tokens(canonical.get("topic", ""))
    candidate_topic = _tokens(candidate.get("topic", ""))
    if not canonical_topic or not candidate_topic:
        return False
    topic_overlap = len(canonical_topic & candidate_topic) / max(1, min(len(canonical_topic), len(candidate_topic)))
    canonical_missing = _tokens(
        [canonical.get("gap_statement", ""), canonical.get("precise_missing_evidence", "")]
    )
    candidate_missing = _tokens(candidate.get("precise_missing_evidence", ""))
    missing_overlap = len(canonical_missing & candidate_missing)
    missing_ratio = missing_overlap / max(1, min(len(canonical_missing), len(candidate_missing)))
    return topic_overlap >= 0.5 and (missing_overlap >= 3 or missing_ratio >= 0.4)


def _reframing_is_evidence_constrained(reframed: Mapping[str, Any], original: Mapping[str, Any]) -> bool:
    """Allow a rule change only when both candidates concern the same evidence-backed puzzle."""

    reframed_evidence = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in reframed.get("supporting_evidence", []) or []
        if isinstance(row, Mapping)
    }
    original_evidence = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in original.get("supporting_evidence", []) or []
        if isinstance(row, Mapping)
    }
    if not (reframed_evidence & original_evidence):
        return False
    reframed_terms = _tokens(
        [reframed.get("topic", ""), reframed.get("gap_statement", ""), reframed.get("precise_missing_evidence", "")]
    )
    original_terms = _tokens([original.get("topic", ""), original.get("precise_missing_evidence", "")])
    overlap = len(reframed_terms & original_terms)
    overlap_ratio = overlap / max(1, min(len(reframed_terms), len(original_terms)))
    return overlap >= 3 or overlap_ratio >= 0.35


def _apply_gap_rationales(
    gaps: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    profile_by_source = {str(row["source_id"]): row for row in profiles}
    rationale_by_id = {
        str(row.get("gap_id") or ""): _as_mapping(row)
        for row in response.get("gaps", []) or []
        if isinstance(row, Mapping) and row.get("gap_id")
    }
    result: list[dict[str, Any]] = []
    for raw_gap in gaps:
        gap = dict(raw_gap)
        rationale = rationale_by_id.get(str(gap.get("gap_id") or ""), {})
        reasoner_evidence = _resolve_reasoner_evidence(
            rationale.get("supporting_evidence", []),
            profile_by_source,
        )
        reasoner_evidence = [
            reference
            for reference in reasoner_evidence
            if (
                (profile := profile_by_source.get(str(reference.get("source_id") or ""))) is not None
                and (
                    claim := next(
                        (
                            item
                            for item in profile.get("claims", []) or []
                            if str(item.get("claim_id") or "") == str(reference.get("claim_id") or "")
                        ),
                        None,
                    )
                )
                is not None
                and _gap_support_is_relevant(gap, claim)
            )
        ]
        if rationale and reasoner_evidence:
            for field in (
                "title",
                "gap_statement",
                "precise_missing_evidence",
                "generation_explanation",
                "observed_pattern",
                "internal_search_summary",
                "closest_prior_explanation",
                "decision_reasoning",
                "evidence_needed",
                "why_matters",
                "contribution",
                "confidence",
                "priority_tier",
            ):
                proposed_text = str(rationale.get(field) or "").strip()
                if not proposed_text:
                    continue
                if field in {"title", "gap_statement"} and (
                    len(_tokens(proposed_text)) < 2 or _VAGUE_GAP.fullmatch(proposed_text.strip(" ."))
                ):
                    continue
                gap[field] = proposed_text
            proposed_clusters = {
                str(value)
                for value in rationale.get("related_cluster_ids", []) or []
                if str(value) in set(gap.get("related_cluster_ids", []) or [])
            }
            if proposed_clusters:
                gap["related_cluster_ids"] = sorted(proposed_clusters)
            gap["value_assessment"] = _normalize_gap_nested_scalars(
                _as_mapping(rationale.get("value_assessment")),
                (
                    "puzzle_type",
                    "puzzle",
                    "strongest_obvious_answer",
                    "why_obvious_answer_is_inadequate",
                    "decision_or_inference_changed",
                ),
            )
            gap["study_design"] = _normalize_gap_nested_scalars(
                _as_mapping(rationale.get("study_design")),
                (
                    "design_type",
                    "research_question",
                    "estimand",
                    "unit_of_analysis",
                    "target_population",
                    "exposure_or_treatment",
                    "comparator",
                    "identification_or_inference_strategy",
                    "data_route",
                    "feasibility",
                    "ethical_constraints",
                ),
            )
            gap["proposed_anchors"] = [
                dict(row) for row in rationale.get("anchors", []) or [] if isinstance(row, Mapping)
            ]
            gap["merged_from_gap_ids"] = sorted(
                {str(value) for value in rationale.get("merged_from_gap_ids", []) or [] if str(value)}
            )
            gap["reframed_from_gap_id"] = str(rationale.get("reframed_from_gap_id") or "")
            reasoner_counter = _resolve_reasoner_evidence(
                rationale.get("countervailing_evidence", []),
                profile_by_source,
            )
            if reasoner_counter:
                gap["countervailing_evidence"] = sorted(
                    {
                        _stable_hash(row): row
                        for row in [*(gap.get("countervailing_evidence", []) or []), *reasoner_counter]
                    }.values(),
                    key=lambda row: (row["source_id"], row["claim_id"]),
                )
            gap["rationale_status"] = "reasoned"
        else:
            gap["rationale_status"] = "deterministic_fallback"
        rule_result = (gap.get("rule_results") or [{}])[0]
        searched = int(rule_result.get("analytical_profile_count_searched", 0) or 0)
        gap.setdefault("title", str(gap.get("precise_missing_evidence") or ""))
        gap.setdefault("gap_statement", str(gap.get("precise_missing_evidence") or ""))
        gap.setdefault(
            "internal_search_summary",
            f"The mapper searched {searched} analytical profiles in the frozen collection; "
            f"{rule_result.get('answered_elsewhere_count', 0)} fully answered and "
            f"{rule_result.get('partially_answered_elsewhere_count', 0)} partially answered the candidate.",
        )
        closest = list(gap.get("closest_prior_work", []) or [])
        gap.setdefault(
            "closest_prior_explanation",
            (
                f"The closest mapped source was {closest[0].get('title', closest[0].get('source_id', ''))}: "
                f"{closest[0].get('overlap_explanation', '')}"
                if closest
                else "No analytical profile supplied close prior evidence for this candidate."
            ),
        )
        gap.setdefault(
            "decision_reasoning",
            f"The deterministic rule decision was {rule_result.get('decision', 'retain_lead')} with "
            f"{rule_result.get('independent_study_families', 0)} independent study families and "
            f"locator completeness {rule_result.get('locator_completeness', 0)}.",
        )
        gap.setdefault("evidence_needed", str(gap.get("precise_missing_evidence") or ""))
        result.append(gap)
    return result


def _apply_gap_adjudication(
    gaps: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    cluster_syntheses: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cluster_syntheses = cluster_syntheses or {}
    reasoned = _apply_gap_rationales(gaps, response, profiles)
    retained_ids = {
        str(row.get("gap_id") or "")
        for row in reasoned
        if row.get("rationale_status") == "reasoned"
    }
    gap_by_id = {str(row.get("gap_id") or ""): dict(row) for row in gaps}
    rejected_by_id: dict[str, dict[str, Any]] = {}
    for raw_rejection in response.get("rejected", []) or []:
        if not isinstance(raw_rejection, Mapping):
            continue
        gap_id = str(raw_rejection.get("gap_id") or "")
        reason = str(raw_rejection.get("reason") or "").strip()
        if gap_id not in gap_by_id or gap_id in retained_ids or len(_tokens(reason)) < 2:
            continue
        underspecified = bool(
            re.search(r"\b(?:vague|generic|underspecified|not specific|missing scope|missing relationship)\b", reason, re.I)
        )
        rejected_by_id[gap_id] = {
            **gap_by_id[gap_id],
            "status": "underspecified_gap" if underspecified else "rejected_by_gap_adjudication",
            "promoted": False,
            "automation_status": "rejected",
            "adjudication_reason": reason,
            "adjudication_record": dict(raw_rejection),
        }
    visible: list[dict[str, Any]] = []
    require_design = bool(_policy_value(policy, "require_executable_gap_design", True))
    seen_signatures: dict[str, str] = {}
    for gap in reasoned:
        gap_id = str(gap.get("gap_id") or "")
        if gap_id in rejected_by_id:
            continue
        if gap.get("rationale_status") != "reasoned":
            errors = ["gap_adjudication_did_not_retain_candidate"]
        else:
            errors = _gap_quality_errors(gap, require_design=require_design)
            reframed_from = str(gap.get("reframed_from_gap_id") or "")
            if reframed_from:
                original = gap_by_id.get(reframed_from)
                if original is None:
                    errors.append("reframing_source_candidate_not_found")
                elif reframed_from != gap_id and not _reframing_is_evidence_constrained(gap, original):
                    errors.append("reframing_not_evidence_constrained")
            gap["anchors"] = _resolve_gap_anchors(
                gap,
                gap.get("proposed_anchors", []) or [],
                cluster_syntheses,
            )
            gap.pop("proposed_anchors", None)
            gap["structured_signature"] = _gap_structured_signature(gap)
            prior_gap_id = seen_signatures.get(gap["structured_signature"])
            if prior_gap_id:
                errors.append(f"duplicate_structured_gap:{prior_gap_id}")
        if errors:
            rejected_by_id[gap_id] = {
                **gap,
                "status": "rejected_gap_quality",
                "promoted": False,
                "automation_status": "rejected",
                "quality_gate_passed": False,
                "quality_rejection_reasons": sorted(set(errors)),
            }
            continue
        seen_signatures[gap["structured_signature"]] = gap_id
        gap["quality_gate_passed"] = True
        visible.append(gap)

    merge_ledger: list[dict[str, Any]] = []
    merged_owner: dict[str, str] = {}
    for gap in visible:
        gap_id = str(gap.get("gap_id") or "")
        valid_merged_ids: list[str] = []
        for merged_id in gap.get("merged_from_gap_ids", []) or []:
            merged_id = str(merged_id)
            merged_candidate = gap_by_id.get(merged_id)
            if (
                merged_id == gap_id
                or merged_candidate is None
                or merged_id in retained_ids
                or merged_id in merged_owner
                or not _merge_candidates_are_compatible(gap, merged_candidate)
            ):
                continue
            merged_owner[merged_id] = gap_id
            valid_merged_ids.append(merged_id)
            rejected_by_id[merged_id] = {
                **merged_candidate,
                "status": "merged_gap",
                "promoted": False,
                "automation_status": "rejected",
                "adjudication_reason": f"Merged into {gap_id} as a semantic duplicate.",
            }
            merge_ledger.append(
                {
                    "event": "merge",
                    "canonical_gap_id": gap_id,
                    "merged_gap_id": merged_id,
                    "reason": "same structured exposure-mechanism-outcome-population-setting puzzle",
                }
            )
        gap["merged_from_gap_ids"] = sorted(valid_merged_ids)
        gap["merge_events"] = [
            row for row in merge_ledger if row["canonical_gap_id"] == str(gap.get("gap_id") or "")
        ]
    return visible, sorted(rejected_by_id.values(), key=lambda row: str(row.get("gap_id") or ""))


def build_literature_report(
    profiles: Sequence[Any],
    *,
    previous_registry: Mapping[str, Any] | None = None,
    policy: Any = None,
    question: str | None = None,
    reasoner: Any = None,
    request: Any = None,
    stage_callback: Any = None,
    reasoner_call: Any = None,
) -> dict[str, Any]:
    """Pure end-to-end mapper over already-built evidence profiles."""
    normalized = normalize_evidence_profiles(profiles)
    _notify_stage(stage_callback, "relation_mapping")
    relations = map_profile_relations(normalized)
    _notify_stage(stage_callback, "clustering")
    analytical_families = {
        str(row.get("study_family_id") or row.get("source_id") or "")
        for row in normalized
        if row.get("analytical")
    }
    proposal_response = (
        _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="cluster_proposal",
            key="collection",
            method_name="propose_clusters",
            profiles=normalized,
            request=request,
            context={"relations": relations},
        )
        if len(analytical_families) >= 2
        else {}
    )
    clustered = map_overlapping_clusters(
        normalized,
        relations,
        policy=policy,
        proposals=list(proposal_response.get("clusters", []) or []),
    )
    registry = reconcile_cluster_registry(clustered["clusters"], previous_registry)
    _notify_stage(stage_callback, "evidence_matrices")
    matrices = build_evidence_matrices(normalized, registry["clusters"])
    deterministic_debates = build_debate_registry(normalized, registry["clusters"], policy=policy)
    matrix_by_cluster = {str(row["cluster_id"]): row for row in matrices}
    deterministic_debate_by_cluster = {
        str(row["cluster_id"]): row for row in deterministic_debates["assessments"]
    }
    cluster_syntheses: dict[str, dict[str, Any]] = {}
    synthesis_order = sorted(
        registry["clusters"],
        key=lambda row: (
            str(row.get("formation_route") or "") != "reasoner_proposal",
            str(row.get("status") or "") == "emerging_cluster",
            -int(row.get("source_count", 0) or 0),
            str(row.get("cluster_id") or ""),
        ),
    )
    for cluster in synthesis_order:
        cluster_id = str(cluster["cluster_id"])
        _notify_stage(stage_callback, "cluster_synthesis", active_cluster=cluster_id)
        member_profiles = [
            row for row in normalized if str(row.get("source_id") or "") in set(cluster.get("source_ids", []) or [])
        ]
        synthesis_response = _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="cluster_synthesis",
            key=cluster_id,
            method_name="synthesize_cluster",
            profiles=member_profiles,
            request=request,
            context={
                "cluster": cluster,
                "evidence_matrix": matrix_by_cluster.get(cluster_id, {}),
                "deterministic_debate": deterministic_debate_by_cluster.get(cluster_id, {}),
                "all_cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
            },
        )
        cluster_syntheses[cluster_id] = validate_cluster_synthesis(
            synthesis_response,
            cluster,
            normalized,
        )
    _notify_stage(stage_callback, "debate_mapping", active_cluster="")
    debates = apply_cluster_syntheses_to_debates(
        deterministic_debates,
        cluster_syntheses,
        policy=policy,
    )
    _notify_stage(stage_callback, "gap_detection")
    generated_candidates = generate_gap_candidates(
        normalized,
        registry["clusters"],
        debates,
        matrices,
        cluster_syntheses=cluster_syntheses,
    )
    specificity_rejections = [
        {**row, "status": "underspecified_gap", "promoted": False, "automation_status": "rejected"}
        for row in generated_candidates
        if row.get("specificity_errors")
    ]
    candidates = [row for row in generated_candidates if not row.get("specificity_errors")]
    _notify_stage(stage_callback, "internal_falsification")
    validated, search_log = search_and_validate_gaps(candidates, normalized, policy=policy)
    deterministic_rejections = [
        row
        for row in validated
        if row.get("status") in {"rejected_answered_elsewhere", "rejected_rule_admission"}
    ]
    visible_validated = [
        row
        for row in validated
        if row.get("status") not in {"rejected_answered_elsewhere", "rejected_rule_admission"}
    ]
    adjudication_response = (
        _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="gap_adjudication",
            key="collection",
            method_name="detect_gaps",
            profiles=normalized,
            request=request,
            context={
                "clusters": registry["clusters"],
                "cluster_syntheses": cluster_syntheses,
                "candidates": visible_validated,
                "internal_search_log": search_log,
            },
        )
        if visible_validated
        else {}
    )
    adjudicated_gaps, reasoner_rejections = _apply_gap_adjudication(
        visible_validated,
        adjudication_response,
        normalized,
        cluster_syntheses,
        policy=policy,
    )
    gap_merge_ledger = sorted(
        [
            dict(event)
            for gap in adjudicated_gaps
            for event in gap.pop("merge_events", []) or []
        ],
        key=lambda row: (row["canonical_gap_id"], row["merged_gap_id"]),
    )
    gaps = rank_gap_registry(adjudicated_gaps)
    rejected_gaps = sorted(
        [*specificity_rejections, *deterministic_rejections, *reasoner_rejections],
        key=lambda row: str(row.get("gap_id") or ""),
    )
    # Materialize the reciprocal graph relationship only after final
    # collection-wide adjudication. It is a projection and does not alter the
    # cluster membership revision hash.
    valid_cluster_ids = {str(row["cluster_id"]) for row in registry["clusters"]}
    for gap in gaps:
        gap["related_cluster_ids"] = sorted(
            {str(value) for value in gap.get("related_cluster_ids", []) or [] if str(value) in valid_cluster_ids}
        )
    gap_ids_by_cluster: dict[str, list[str]] = defaultdict(list)
    for gap in gaps:
        for anchor in gap.get("anchors", []) or []:
            cluster_id = str(anchor.get("cluster_id") or "")
            if cluster_id in valid_cluster_ids:
                gap_ids_by_cluster[cluster_id].append(str(gap["gap_id"]))
    for cluster in registry["clusters"]:
        cluster["related_gap_ids"] = sorted(set(gap_ids_by_cluster.get(str(cluster["cluster_id"]), [])))
    _notify_stage(stage_callback, "projection")
    gap_memory = [
        {
            "gap_id": gap["gap_id"],
            "rule": gap["rule"],
            "status": gap["status"],
            "revision_hash": _stable_hash(
                {
                    "gap_id": gap["gap_id"],
                    "supporting_evidence": gap.get("supporting_evidence", []),
                    "countervailing_evidence": gap.get("countervailing_evidence", []),
                    "status": gap["status"],
                    "value_assessment": gap.get("value_assessment", {}),
                    "study_design": gap.get("study_design", {}),
                    "anchors": gap.get("anchors", []),
                    "merged_from_gap_ids": gap.get("merged_from_gap_ids", []),
                    "reframed_from_gap_id": gap.get("reframed_from_gap_id", ""),
                    "priority_tier": gap.get("priority_tier", ""),
                    "structured_signature": gap.get("structured_signature", ""),
                }
            ),
        }
        for gap in gaps
    ]
    manifest = {
        "mapper_version": "0.5.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "profile_count": len(normalized),
        "analytical_profile_count": sum(1 for row in normalized if row["analytical"]),
        "limited_profile_count": sum(1 for row in normalized if row["limited"]),
        "relation_count": len(relations),
        "cluster_count": len(registry["clusters"]),
        "source_backed_cluster_count": sum(
            1 for row in registry["clusters"] if row["qualification_status"] == "source_backed_cluster"
        ),
        "emerging_cluster_count": sum(
            1 for row in registry["clusters"] if row["qualification_status"] == "emerging_cluster"
        ),
        "promoted_cluster_count": sum(1 for row in registry["clusters"] if row["promoted"]),
        "cluster_candidate_count": sum(1 for row in registry["clusters"] if not row["promoted"]),
        "unclustered_source_count": len(clustered["unclustered_sources"]),
        "debate_count": debates["debate_count"],
        "debate_candidate_count": debates["debate_candidate_count"],
        "gap_count": len(gaps),
        "promoted_gap_count": sum(1 for row in gaps if row["promoted"]),
        "gap_lead_count": sum(1 for row in gaps if row["status"] in {"gap_lead", "narrowed_gap_lead"}),
        "synthesized_cluster_count": sum(
            1 for row in cluster_syntheses.values() if row.get("status") == "reasoned"
        ),
        "rejected_underspecified_gap_count": sum(
            1 for row in rejected_gaps if row.get("status") == "underspecified_gap"
        ),
        "rejected_gap_quality_count": sum(
            1 for row in rejected_gaps if row.get("status") == "rejected_gap_quality"
        ),
        "merged_gap_count": len(gap_merge_ledger),
    }
    packet = {
        "packet_kind": "literature_map",
        "mapper_version": "0.5.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
        "gap_ids": [row["gap_id"] for row in gaps],
        "counts": manifest,
        "not_method_ready_bundle": True,
        "not_manuscript_text": True,
    }
    return {
        "manifest": manifest,
        "profiles": normalized,
        "relations": relations,
        "cluster_registry": {
            **registry,
            "rejected_proposals": clustered["rejected_proposals"],
            "unclustered_sources": clustered["unclustered_sources"],
            "max_cluster_memberships": clustered["max_cluster_memberships"],
        },
        "evidence_matrices": matrices,
        "cluster_syntheses": cluster_syntheses,
        "debate_registry": debates,
        "gap_registry": {
            "allowed_rules": list(GAP_RULES),
            "gaps": gaps,
            "rejected_candidates": rejected_gaps,
            "merge_ledger": gap_merge_ledger,
        },
        "gap_memory": gap_memory,
        "internal_search_log": search_log,
        "packet": packet,
    }


def _markdown_with_frontmatter(frontmatter: Mapping[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        dict(frontmatter),
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    ).strip()
    return f"---\n{yaml_text}\n---\n\n{body.strip()}\n"


def _bounded_display_label(value: Any, *, fallback: str, limit: int = 56) -> str:
    label = safe_filename(str(value or "").replace("_", " "), fallback=fallback)
    label = label[:1].upper() + label[1:]
    if len(label) <= limit:
        return label
    shortened = label[:limit].rsplit(" ", 1)[0].rstrip(" .-:;")
    return f"{shortened or label[:limit].rstrip(' .-:;')}…"


def cluster_display_title(cluster: Mapping[str, Any]) -> str:
    label = _bounded_display_label(
        cluster.get("label") or cluster.get("semantic_identity"),
        fallback="Unnamed Cluster",
        limit=100,
    )
    if label == label.casefold():
        label = label.title()
    return f"Cluster: {label}"


def gap_display_title(gap: Mapping[str, Any]) -> str:
    label = _bounded_display_label(
        gap.get("title") or gap.get("gap_statement") or gap.get("precise_missing_evidence") or gap.get("topic"),
        fallback=str(gap.get("rule") or "Collection Gap").replace("_", " ").title(),
        limit=110,
    )
    return f"Gap: {label}"


def cluster_note_stem(cluster: Mapping[str, Any]) -> str:
    label = cluster_display_title(cluster).removeprefix("Cluster: ")
    return f"Cluster - {label} [{cluster['cluster_id']}]"


def gap_note_stem(gap: Mapping[str, Any]) -> str:
    label = gap_display_title(gap).removeprefix("Gap: ")
    return f"Gap - {label} [{gap['gap_id']}]"


def _cluster_wikilink(cluster: Mapping[str, Any], *, label: str | None = None) -> str:
    display = (label or cluster_display_title(cluster)).replace("|", "-").replace("]", "")
    return f"[[{cluster_note_stem(cluster)}|{display}]]"


def _gap_wikilink(gap: Mapping[str, Any], *, label: str | None = None) -> str:
    display = (label or gap_display_title(gap)).replace("|", "-").replace("]", "")
    return f"[[{gap_note_stem(gap)}|{display}]]"


def _clear_generated_markdown(directory: Path) -> None:
    for path in directory.glob("*.md"):
        if path.name != "INDEX.md":
            path.unlink()


def _obsidian_note_link(row: Mapping[str, Any]) -> str:
    note_path = str(row.get("note_path") or "")
    target = Path(note_path).stem if note_path else str(row.get("note_id") or row.get("source_id") or "")
    title = str(row.get("title") or "").replace("|", "-").replace("]", "")
    return f"[[{target}|{title}]]" if title and title != target else f"[[{target}]]"


def _cluster_obsidian_tags(cluster: Mapping[str, Any]) -> list[str]:
    qualification = slugify(str(cluster.get("qualification_status") or cluster.get("status") or "cluster"))
    return sorted(
        {
            "auto-zettelkasten/cluster",
            f"auto-zettelkasten/cluster/{qualification}",
            *(str(tag) for tag in cluster.get("shared_normalized_tags", []) or [] if str(tag)),
        }
    )


def _gap_obsidian_tags(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    cluster_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    evidence_source_ids = {
        str(row.get("source_id") or "")
        for field in ("supporting_evidence", "countervailing_evidence")
        for row in gap.get(field, []) or []
        if row.get("source_id")
    }
    evidence_source_ids.update(
        str(row.get("source_id") or "")
        for row in gap.get("warnings", []) or []
        if row.get("warning") == "possible_counterevidence_requires_full_text" and row.get("source_id")
    )
    tag_families: dict[str, set[str]] = defaultdict(set)
    for source_id in evidence_source_ids:
        profile = profile_by_source.get(source_id, {})
        family_id = str(profile.get("study_family_id") or source_id)
        for tag in profile.get("normalized_tags", []) or []:
            tag_families[str(tag)].add(family_id)
    shared_tags = {tag for tag, families in tag_families.items() if len(families) >= 2}
    for cluster_id in gap.get("related_cluster_ids", []) or []:
        shared_tags.update(cluster_by_id.get(str(cluster_id), {}).get("shared_normalized_tags", []) or [])
    rule = slugify(str(gap.get("rule") or "gap"))
    status = slugify(str(gap.get("status") or "lead"))
    return sorted(
        {
            "auto-zettelkasten/gap",
            f"auto-zettelkasten/gap/{rule}",
            f"auto-zettelkasten/gap-status/{status}",
            *(str(tag) for tag in shared_tags if str(tag)),
        }
    )


def _cluster_markdown(
    cluster: Mapping[str, Any],
    matrix: Mapping[str, Any] | None,
    debate: Mapping[str, Any] | None,
    related_gaps: Sequence[Mapping[str, Any]] = (),
    *,
    synthesis: Mapping[str, Any] | None = None,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    synthesis = synthesis or {}
    profile_by_source = profile_by_source or {}

    def item_text(row: Mapping[str, Any]) -> str:
        return _cluster_item_text(row) or "Evidence-backed mapped statement."

    def evidence_text(reference: Mapping[str, Any]) -> str:
        profile = profile_by_source.get(str(reference.get("source_id") or ""), reference)
        return (
            f"{_obsidian_note_link(profile)} — `{reference.get('claim_id', '')}` — "
            f"{reference.get('locator', '')}"
        )

    gaps_by_anchor: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for gap in related_gaps:
        for anchor in gap.get("anchors", []) or []:
            if str(anchor.get("cluster_id") or "") != str(cluster.get("cluster_id") or ""):
                continue
            gaps_by_anchor[(str(anchor.get("section") or ""), str(anchor.get("item_id") or ""))].append(gap)

    def gap_callout(gap: Mapping[str, Any]) -> list[str]:
        assessment = _as_mapping(gap.get("value_assessment"))
        design = _as_mapping(gap.get("study_design"))
        label = gap_display_title(gap).removeprefix("Gap: ")
        puzzle = str(assessment.get("puzzle") or gap.get("gap_statement") or "").strip()
        payoff = str(
            assessment.get("decision_or_inference_changed") or gap.get("why_matters") or ""
        ).strip()
        test = str(design.get("research_question") or "").strip()
        strategy = str(design.get("identification_or_inference_strategy") or "").strip()
        if strategy:
            test = f"{test} {strategy}".strip()
        return [
            "",
            f"> [!question] {_gap_wikilink(gap, label=f'Research opportunity: {label}')}",
            f"> **Puzzle:** {puzzle}",
            f"> **Payoff:** {payoff}",
            f"> **Test:** {test}",
        ]

    def render_items(
        values: Sequence[Mapping[str, Any]],
        *,
        section: str,
        include_plain_english: bool = False,
    ) -> str:
        lines: list[str] = []
        for row in values:
            lines.append(f"- {item_text(row)}")
            plain = str(row.get("plain_english_meaning") or row.get("plain_english") or "").strip()
            if include_plain_english and plain:
                lines.append(f"  - Plain English: {plain}")
            technical = str(row.get("technical_context") or row.get("statistics") or "").strip()
            if technical:
                lines.append(f"  - Technical context: {technical}")
            references = list(row.get("evidence", []) or [])
            for reference in references[:3]:
                lines.append(f"  - Evidence: {evidence_text(reference)}")
            if len(references) > 3:
                lines.append(f"  - Additional located evidence: {len(references) - 3} references in the canonical matrix.")
            for gap in gaps_by_anchor.get((section, str(row.get("item_id") or "")), []):
                lines.extend(gap_callout(gap))
        return "\n".join(lines) or "- No locator-backed statements were admitted."

    def fallback_synthesis() -> str:
        lines = [
            str(cluster.get("coherence_rationale") or cluster.get("shared_question") or ""),
            "",
            "The locator-backed source claims are kept side by side because the collection does not support a stronger cross-source consensus or debate:",
        ]
        for source_id in cluster.get("source_ids", []) or []:
            profile = profile_by_source.get(str(source_id), {})
            for claim in list(profile.get("claims", []) or [])[:2]:
                text = str(claim.get("text") or claim.get("claim") or "").strip()
                if not text or not claim.get("locator"):
                    continue
                lines.append(
                    f"- {_obsidian_note_link(profile)}: {text} — `{claim.get('claim_id', '')}` — {claim.get('locator', '')}"
                )
        return "\n".join(lines).strip()

    sources = "\n".join(
        f"- {_obsidian_note_link(row)} — `{row.get('source_id', '')}`"
        for row in cluster.get("representative_sources", []) or []
    ) or "- None"
    source_links = [_obsidian_note_link(row) for row in cluster.get("representative_sources", []) or []]
    anchored_gap_ids = {
        str(gap.get("gap_id") or "")
        for gap in related_gaps
        for anchor in gap.get("anchors", []) or []
        if str(anchor.get("cluster_id") or "") == str(cluster.get("cluster_id") or "")
    }
    gap_links = [
        _gap_wikilink(row)
        for row in related_gaps
        if str(row.get("gap_id") or "") in anchored_gap_ids
    ]
    matrix_lines = ["| Dimension | Mapped values | Representative evidence |", "|---|---|---|"]
    matrix_dimensions = (matrix or {}).get("dimensions", {})
    for dimension in EVIDENCE_DIMENSIONS:
        entries = list(matrix_dimensions.get(dimension, []) or [])
        values = [re.sub(r"\s+", " ", str(entry.get("value") or "")).strip() for entry in entries]
        values = [value for value in values if value]
        displayed_values = values[:3]
        values_text = "; ".join(displayed_values) or "No locator-backed values"
        if len(values) > len(displayed_values):
            values_text += f"; +{len(values) - len(displayed_values)} more"
        representative = next(
            (
                reference
                for entry in entries
                for reference in entry.get("evidence", []) or []
                if reference.get("source_id") and reference.get("claim_id") and reference.get("locator")
            ),
            None,
        )
        if representative is not None:
            representative_profile = profile_by_source.get(str(representative.get("source_id") or ""), {})
            evidence = (
                f"{representative_profile.get('title') or representative.get('source_id', '')} — "
                f"`{representative.get('claim_id', '')}` — {representative.get('locator', '')}"
            )
        else:
            evidence = "No representative locator"
        escaped_values = values_text.replace("|", "\\|")
        escaped_evidence = evidence.replace("|", "\\|")
        matrix_lines.append(f"| {str(dimension).title()} | {escaped_values} | {escaped_evidence} |")
    matrix_lines.extend(
        [
            "",
            "The complete locator-level matrix is preserved in [evidence_matrices.yml](../evidence_matrices.yml) for agent use.",
        ]
    )
    matrix_text = "\n".join(matrix_lines)
    classification = str((debate or {}).get("classification") or "no_debate")
    synthesis_evidence = "\n".join(
        f"- {evidence_text(reference)}" for reference in synthesis.get("supporting_evidence", []) or []
    ) or "- No reasoned narrative was admitted; use the evidence-backed sections below."
    frontmatter = {
        "type": "literature_cluster",
        "title": cluster_display_title(cluster),
        "aliases": [str(cluster["cluster_id"]), str(cluster["label"])],
        "cluster_id": cluster["cluster_id"],
        "semantic_identity": cluster["semantic_identity"],
        "status": cluster["status"],
        "qualification_status": cluster.get("qualification_status", cluster["status"]),
        "promoted": bool(cluster.get("promoted", True)),
        "automation_status": cluster.get("automation_status", "promoted"),
        "revision_hash": cluster["revision_hash"],
        "synthesis_status": synthesis.get("status", "deterministic_fallback"),
        "debate_classification": classification,
        "tags": _cluster_obsidian_tags(cluster),
        "sources": source_links,
        "related_gaps": gap_links,
    }
    boundaries = "\n".join(f"- {value}" for value in synthesis.get("boundaries", []) or []) or "- No additional boundary was established."
    synthesis_narrative = str(synthesis.get("synthesis") or "").strip() or fallback_synthesis()
    synthesis_narrative = re.sub(r"(?m)^#{1,6}\s+", "", synthesis_narrative).strip()
    body = (
        f"# {cluster_display_title(cluster)}\n\n"
        f"## Scope and Boundaries\n\n{str(synthesis.get('scope') or cluster['shared_question'])}\n\n{boundaries}\n\n"
        f"## Why These Sources Form a Cluster\n\n{synthesis.get('coherence_rationale') or cluster.get('coherence_rationale', '')}\n\n"
        f"## Synthesis\n\n{synthesis_narrative}\n\n"
        f"### Evidence for the Synthesis\n\n{synthesis_evidence}\n\n"
        f"## Cluster Status\n\n- Sources: {cluster['source_count']}\n"
        f"- Independent study families: {cluster['independent_study_family_count']}\n"
        f"- Debate classification: {classification}\n"
        f"- Formation route: {cluster.get('formation_route', 'deterministic_fallback')}\n"
        f"- Synthesis status: {synthesis.get('status', 'deterministic_fallback')}\n\n"
        f"## Central Findings\n\n{render_items(synthesis.get('central_findings', []) or [], section='central_findings', include_plain_english=True)}\n\n"
        f"## Agreements and Mapped Consensus\n\n{render_items(synthesis.get('agreements', []) or [], section='agreements')}\n\n"
        f"## Debate Positions\n\n{render_items(synthesis.get('positions', []) or [], section='positions')}\n\n"
        f"## Contradictions\n\n{render_items(synthesis.get('contradictions', []) or [], section='contradictions')}\n\n"
        f"## Boundary Conditions\n\n{render_items(synthesis.get('boundary_conditions', []) or [], section='boundary_conditions')}\n\n"
        f"## Methodological and Measurement Fault Lines\n\n{render_items(synthesis.get('methodological_fault_lines', []) or [], section='methodological_fault_lines')}\n\n"
        f"## Relationships to Neighboring Clusters\n\n{render_items(synthesis.get('related_clusters', []) or [], section='related_clusters')}\n\n"
        f"## Evidence Matrix\n\n{matrix_text}\n\n"
        f"## Source Roles\n\n{render_items(synthesis.get('source_roles', []) or [], section='source_roles')}\n\n"
        f"## Source Index\n\n{sources}"
    )
    return _markdown_with_frontmatter(frontmatter, body)


def _gap_markdown(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    cluster_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    profile_by_source = profile_by_source or {}
    cluster_by_id = cluster_by_id or {}

    def evidence_line(row: Mapping[str, Any]) -> str:
        profile = profile_by_source.get(str(row.get("source_id") or ""), row)
        claim = next(
            (
                value
                for value in profile.get("claims", []) or []
                if str(value.get("claim_id") or "") == str(row.get("claim_id") or "")
            ),
            {},
        )
        claim_text = str(claim.get("text") or "").strip()
        return (
            f"- {_obsidian_note_link(profile)} — `{row.get('claim_id', '')}` — "
            f"{row.get('locator', '')}"
            f"{f' — {claim_text}' if claim_text else ''}"
        )

    support = "\n".join(
        evidence_line(row) for row in gap.get("supporting_evidence", []) or []
    ) or "- None"
    counter = "\n".join(
        evidence_line(row) for row in gap.get("countervailing_evidence", []) or []
    ) or "- None"
    limited_warnings = [
        row
        for row in gap.get("warnings", []) or []
        if row.get("warning") == "possible_counterevidence_requires_full_text"
    ]
    warning_lines = "\n".join(
        f"- {_obsidian_note_link(profile_by_source.get(str(row.get('source_id') or ''), row))} — full text required"
        for row in limited_warnings
    ) or "- None"
    source_ids = {
        str(row.get("source_id") or "")
        for field in ("supporting_evidence", "countervailing_evidence")
        for row in gap.get(field, []) or []
        if row.get("source_id")
    }
    source_ids.update(str(row.get("source_id") or "") for row in limited_warnings if row.get("source_id"))
    source_links = [
        _obsidian_note_link(profile_by_source[source_id])
        for source_id in sorted(source_ids)
        if source_id in profile_by_source
    ]
    related_clusters = [
        cluster_by_id[str(cluster_id)]
        for cluster_id in gap.get("related_cluster_ids", []) or []
        if str(cluster_id) in cluster_by_id
    ]
    cluster_links = [_cluster_wikilink(cluster) for cluster in related_clusters]
    cluster_lines = "\n".join(
        f"- {_cluster_wikilink(cluster)}" for cluster in related_clusters
    ) or "- No canonical cluster relation recorded."
    result = (gap.get("rule_results") or [{}])[0]
    search_counts = Counter(str(row.get("status") or "") for row in gap.get("internal_search_results", []) or [])
    search_terms = ", ".join(f"`{value}`" for value in gap.get("internal_search_terms", []) or []) or "None"
    search_lines = "\n".join(
        f"- {status.replace('_', ' ')}: {count}" for status, count in sorted(search_counts.items())
    ) or "- No search results were recorded."
    closest_lines = "\n".join(
        f"- {_obsidian_note_link(row)} — confidence {row.get('confidence', 0)} — {row.get('overlap_explanation', '')}"
        for row in gap.get("closest_prior_work", []) or []
    ) or "- No semantically close analytical source was identified."
    assessment = _as_mapping(gap.get("value_assessment"))
    design = _as_mapping(gap.get("study_design"))

    def list_text(values: Any) -> str:
        rows = [str(value).strip() for value in values or [] if str(value).strip()]
        return "\n".join(f"- {value}" for value in rows) or "- None specified."

    frontmatter = {
        "type": "literature_gap",
        "title": gap_display_title(gap),
        "aliases": [str(gap["gap_id"]), gap_display_title(gap).removeprefix("Gap: ")],
        "gap_id": gap["gap_id"],
        "rule": gap["rule"],
        "status": gap["status"],
        "scope": "collection_only",
        "promoted": bool(gap["promoted"]),
        "automation_status": gap.get("automation_status", "lead"),
        "novelty_claimed": False,
        "rationale_status": gap.get("rationale_status", "deterministic_fallback"),
        "quality_gate_passed": bool(gap.get("quality_gate_passed", False)),
        "priority_tier": gap.get("priority_tier", ""),
        "structured_signature": gap.get("structured_signature", ""),
        "merged_from_gap_ids": list(gap.get("merged_from_gap_ids", []) or []),
        "tags": _gap_obsidian_tags(gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id),
        "sources": source_links,
        "related_clusters": cluster_links,
    }
    body = (
        f"# {gap_display_title(gap)}\n\n"
        f"## Gap Statement\n\n{gap.get('gap_statement') or gap['precise_missing_evidence']}\n\n"
        f"## Status and Scope\n\n- Rule: {gap['rule'].replace('_', ' ').title()}\n"
        f"- Status: {str(gap.get('status') or '').replace('_', ' ')}\n"
        f"- Scope: frozen collection only\n- Novelty beyond the collection: not claimed\n\n"
        f"## How the System Identified This Gap\n\n{gap.get('generation_explanation', '')}\n\n"
        f"## Observed Evidence Pattern\n\n{gap.get('observed_pattern', '')}\n\n"
        f"## Precise Missing Evidence\n\n{gap['precise_missing_evidence']}\n\n"
        f"## Why This Is Not an Obvious Gap\n\n"
        f"**Puzzle:** {assessment.get('puzzle', '')}\n\n"
        f"**Strongest obvious answer:** {assessment.get('strongest_obvious_answer', '')}\n\n"
        f"**Why that answer is inadequate:** {assessment.get('why_obvious_answer_is_inadequate', '')}\n\n"
        f"**Competing explanations:**\n\n{list_text(assessment.get('competing_explanations', []))}\n\n"
        f"**What resolving it changes:** {assessment.get('decision_or_inference_changed', '')}\n\n"
        f"**Expected information gain:** {assessment.get('information_gain', '')}\n\n"
        f"## Automated Rule Result\n\n- Decision: {result.get('decision', '')}\n"
        f"- Independent study families: {result.get('independent_study_families', 0)}\n"
        f"- Locator completeness: {result.get('locator_completeness', 0)}\n"
        f"- Analytical profiles searched: {result.get('analytical_profile_count_searched', 0)}\n\n"
        f"## Related Clusters\n\n{cluster_lines}\n\n"
        f"## Supporting Sources and Locators\n\n{support}\n\n"
        f"## Collection-Wide Internal Search\n\nSearch terms: {search_terms}\n\n{search_lines}\n\n"
        f"{gap.get('internal_search_summary', '')}\n\n"
        f"## Closest Prior Work in the Collection\n\n{closest_lines}\n\n{gap.get('closest_prior_explanation', '')}\n\n"
        f"## Countervailing Sources and Locators\n\n{counter}\n\n"
        f"## Possible Counterevidence Requiring Full Text\n\n{warning_lines}\n\n"
        f"## Why the Candidate Survived or Was Narrowed\n\n{gap.get('decision_reasoning', '')}\n\n"
        f"## Executable Study Design\n\n"
        f"- Design: {design.get('design_type', '')}\n"
        f"- Research question: {design.get('research_question', '')}\n"
        f"- Estimand: {design.get('estimand', '')}\n"
        f"- Unit of analysis: {design.get('unit_of_analysis', '')}\n"
        f"- Target population: {design.get('target_population', '')}\n"
        f"- Exposure or treatment: {design.get('exposure_or_treatment', '')}\n"
        f"- Comparator: {design.get('comparator', '')}\n"
        f"- Identification or inference strategy: {design.get('identification_or_inference_strategy', '')}\n"
        f"- Data route: {design.get('data_route', '')}\n"
        f"- Feasibility: {design.get('feasibility', '')}\n"
        f"- Ethical constraints: {design.get('ethical_constraints', '')}\n\n"
        f"### Outcomes\n\n{list_text(design.get('outcomes', []))}\n\n"
        f"### Mechanism Measures\n\n{list_text(design.get('mechanism_measures', []))}\n\n"
        f"### Confounders and Rival Explanations\n\n{list_text(design.get('confounders_or_rival_explanations', []))}\n\n"
        f"### Falsification and Process Tests\n\n{list_text(design.get('falsification_or_process_tests', []))}\n\n"
        f"### Validity Risks\n\n{list_text(design.get('validity_risks', []))}\n\n"
        f"## Evidence Needed to Resolve It\n\n{gap.get('evidence_needed', '')}\n\n"
        f"## Why It Matters\n\n{gap['why_matters']}\n\n"
        f"## Contribution\n\n{gap['contribution']}"
    )
    return _markdown_with_frontmatter(frontmatter, body)


def stable_literature_map_id(source_set: Mapping[str, Any], question: str | None = None) -> str:
    """Identify a map by its stable source-set alias, never by a mutable snapshot."""
    del question  # A question is a projection lens, not part of collection-map identity.
    source_set_alias = str(source_set.get("source_set_alias") or source_set.get("source_set_id") or "source-set")
    identity = {"source_set_alias": source_set_alias}
    return f"literature-map-{slugify(source_set_alias)}-{_stable_hash(identity)[:12]}"


def _load_map_cluster_registry(workspace: Path, map_id: str) -> Mapping[str, Any]:
    canonical = workspace / "03_literature_synthesis" / "maps" / map_id / "cluster_registry.yml"
    payload = read_yaml(canonical, None)
    if isinstance(payload, Mapping):
        return payload
    maps_root = workspace / "03_literature_synthesis" / "maps"
    if maps_root.exists() and any(path.is_dir() for path in maps_root.iterdir()):
        return {}
    for legacy in (
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        workspace / "03_literature_synthesis" / "clusters" / "clusters.yml",
    ):
        payload = read_yaml(legacy, None)
        if isinstance(payload, Mapping):
            return payload
    return {}


def persist_literature_report(
    workspace: Path,
    report: Mapping[str, Any],
    *,
    source_set: Mapping[str, Any],
    run_id: str,
    question: str | None,
    map_id: str | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    """Persist compatibility projections after all pure stages have completed."""
    root = workspace / "03_literature_synthesis"
    cluster_root = root / "clusters"
    gap_root = root / "gaps"
    gap_candidates_root = gap_root / "candidates"
    prior_root = root / "closest_prior_work"
    packet_root = root / "packets"
    map_id = map_id or stable_literature_map_id(source_set, question)
    map_root = root / "maps" / map_id
    canonical_cluster_root = map_root / "clusters"
    canonical_gap_root = map_root / "gaps"
    for directory in (
        cluster_root,
        gap_candidates_root,
        prior_root,
        packet_root,
        canonical_cluster_root,
        canonical_gap_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    generated_at = now_iso()
    clusters = list(report["cluster_registry"]["clusters"])
    gaps = list(report["gap_registry"]["gaps"])
    profile_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if row.get("source_id")
    }
    cluster_by_id = {str(row["cluster_id"]): row for row in clusters}
    gaps_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gap in gaps:
        for anchor in gap.get("anchors", []) or []:
            cluster_id = str(anchor.get("cluster_id") or "")
            if cluster_id in cluster_by_id and gap not in gaps_by_cluster[cluster_id]:
                gaps_by_cluster[cluster_id].append(gap)
    matrix_by_cluster = {row["cluster_id"]: row for row in report["evidence_matrices"]}
    debate_by_cluster = {row["cluster_id"]: row for row in report["debate_registry"]["assessments"]}
    synthesis_by_cluster = {
        str(cluster_id): synthesis
        for cluster_id, synthesis in (report.get("cluster_syntheses", {}) or {}).items()
    }
    prior_merge_payload = read_yaml(map_root / "gap_merge_ledger.yml", {}) or {}
    prior_merge_events = (
        list(prior_merge_payload.get("events", []) or [])
        if isinstance(prior_merge_payload, Mapping)
        else []
    )
    merge_events = sorted(
        {
            _stable_hash(event): dict(event)
            for event in [
                *prior_merge_events,
                *(report["gap_registry"].get("merge_ledger", []) or []),
            ]
            if isinstance(event, Mapping)
        }.values(),
        key=lambda row: (
            str(row.get("canonical_gap_id") or ""),
            str(row.get("merged_gap_id") or ""),
            str(row.get("event") or ""),
        ),
    )
    paths: list[Path] = []

    registry_path = root / "cluster_registry.yml"
    ledger_path = root / "cluster_ledger.yml"
    compatibility_clusters = cluster_root / "clusters.yml"
    cluster_updates = cluster_root / "cluster_updates.yml"
    write_yaml(registry_path, {"updated_at": generated_at, **dict(report["cluster_registry"])})
    write_yaml(ledger_path, {"updated_at": generated_at, "events": report["cluster_registry"]["ledger"]})
    write_yaml(
        compatibility_clusters,
        {
            "updated_at": generated_at,
            "minimum_independent_study_families": 2,
            "clusters": clusters,
            "unclustered_sources": report["cluster_registry"]["unclustered_sources"],
        },
    )
    write_yaml(cluster_updates, {"updated_at": generated_at, "updates": report["cluster_registry"]["rejected_proposals"]})
    paths.extend((registry_path, ledger_path, compatibility_clusters, cluster_updates))

    matrix_path = root / "evidence_matrices.yml"
    debate_path = root / "debate_registry.yml"
    synthesis_path = root / "cluster_syntheses.yml"
    write_yaml(matrix_path, {"updated_at": generated_at, "matrices": report["evidence_matrices"]})
    write_yaml(debate_path, {"updated_at": generated_at, **dict(report["debate_registry"])})
    write_yaml(synthesis_path, {"updated_at": generated_at, "syntheses": synthesis_by_cluster})
    paths.extend((matrix_path, debate_path, synthesis_path))

    gap_registry_path = root / "gap_registry.yml"
    compatibility_gaps = gap_root / "gaps.yml"
    compatibility_gap_index = workspace / "02_source_memory" / "indexes" / "gap_candidates.yml"
    gap_memory_path = root / "gap_memory.yml"
    gap_merge_ledger_path = root / "gap_merge_ledger.yml"
    search_path = root / "internal_search_log.yml"
    gap_status = (
        "blocked_no_source_backed_clusters"
        if not clusters
        else (
            "mapped_collection_gaps"
            if any(row.get("promoted") for row in gaps)
            else ("gap_leads" if gaps else "complete_no_qualifying_gaps")
        )
    )
    gap_payload = {
        "updated_at": generated_at,
        "status": gap_status,
        "novelty_claimed": False,
        "allowed_rules": list(GAP_RULES),
        "gap_candidates": gaps,
    }
    write_yaml(gap_registry_path, {"updated_at": generated_at, **dict(report["gap_registry"])})
    write_yaml(compatibility_gaps, gap_payload)
    write_yaml(compatibility_gap_index, gap_payload)
    write_yaml(gap_memory_path, {"updated_at": generated_at, "entries": report["gap_memory"]})
    write_yaml(
        gap_merge_ledger_path,
        {"updated_at": generated_at, "events": merge_events},
    )
    write_yaml(search_path, {"updated_at": generated_at, "searches": report["internal_search_log"]})
    paths.extend(
        (
            gap_registry_path,
            compatibility_gaps,
            compatibility_gap_index,
            gap_memory_path,
            gap_merge_ledger_path,
            search_path,
        )
    )

    _clear_generated_markdown(gap_candidates_root)
    _clear_generated_markdown(prior_root)
    gap_index = ["# Gap Registry Index", "", f"Gap record count: {len(gaps)}", f"Promoted collection gaps: {sum(1 for row in gaps if row['promoted'])}", ""]
    for gap in gaps:
        path = gap_candidates_root / f"{gap_note_stem(gap)}.md"
        atomic_write_text(path, _gap_markdown(gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id))
        gap_index.append(f"- {_gap_wikilink(gap)} — {gap['rule']}; {gap['status']}; rank {gap['rank']}")
        paths.append(path)
        prior_path = prior_root / f"Closest Prior - {gap_note_stem(gap).removeprefix('Gap - ')}.md"
        prior_lines = [f"# Closest Prior: {gap_display_title(gap).removeprefix('Gap: ')}", "", f"Gap ID: `{gap['gap_id']}`", ""]
        prior_lines.extend(
            f"- `{row['prior_id']}` — {row['title']} — confidence {row['confidence']} — {row['overlap_explanation']}"
            for row in gap.get("closest_prior_work", []) or []
        )
        if not gap.get("closest_prior_work"):
            prior_lines.append("- No semantic overlap in the mapped analytical profiles.")
        atomic_write_text(prior_path, "\n".join(prior_lines) + "\n")
        paths.append(prior_path)
    gap_index_path = gap_root / "INDEX.md"
    atomic_write_text(
        gap_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_gap_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/gap"]},
            "\n".join(gap_index),
        ),
    )
    paths.append(gap_index_path)

    _clear_generated_markdown(cluster_root)
    cluster_index = ["# Cluster Index", "", f"Cluster count: {len(clusters)}", ""]
    for cluster in clusters:
        path = cluster_root / f"{cluster_note_stem(cluster)}.md"
        atomic_write_text(
            path,
            _cluster_markdown(
                cluster,
                matrix_by_cluster.get(cluster["cluster_id"]),
                debate_by_cluster.get(cluster["cluster_id"]),
                gaps_by_cluster.get(str(cluster["cluster_id"]), []),
                synthesis=synthesis_by_cluster.get(str(cluster["cluster_id"]), {}),
                profile_by_source=profile_by_source,
            ),
        )
        cluster_index.append(
            f"- {_cluster_wikilink(cluster)} — {cluster['status']}; "
            f"{cluster['source_count']} sources; {cluster['independent_study_family_count']} study families"
        )
        paths.append(path)
    cluster_index_path = cluster_root / "INDEX.md"
    atomic_write_text(
        cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/cluster"]},
            "\n".join(cluster_index),
        ),
    )
    paths.append(cluster_index_path)

    packet_id = f"literature-packet-{slugify(map_id)}"
    packet = {
        **dict(report["packet"]),
        "packet_id": packet_id,
        "map_id": map_id,
        "run_id": run_id,
        "source_set_id": source_set.get("source_set_id", ""),
        "question": question or "",
        "dependency_hash": _stable_hash(
            {
                "algorithm_version": LITERATURE_ALGORITHM_VERSION,
                "source_set_dependency_hash": source_set.get("dependency_hash", ""),
                "cluster_revisions": [row["revision_hash"] for row in clusters],
                "gap_memory": report["gap_memory"],
            }
        ),
        "created_at": generated_at,
    }
    packet_path = packet_root / f"{packet_id}.yml"
    write_yaml(packet_path, packet)
    paths.append(packet_path)

    index_path = root / "INDEX.md"
    manifest = report["manifest"]
    index_lines = [
        "# Literature Mapping Index",
        "",
        f"- Mapper version: {manifest['mapper_version']}",
        f"- Profiles: {manifest['profile_count']}",
        f"- Analytical profiles: {manifest['analytical_profile_count']}",
        f"- Limited profiles: {manifest['limited_profile_count']}",
        f"- Clusters: {manifest['cluster_count']}",
        f"- Unclustered sources: {manifest['unclustered_source_count']}",
        f"- Debates: {manifest['debate_count']}",
        f"- Gap records: {manifest['gap_count']}",
        f"- Promoted collection gaps: {manifest['promoted_gap_count']}",
        "",
        "- [[clusters/INDEX|Cluster Index]]",
        "- [[gaps/INDEX|Gap Registry Index]]",
    ]
    atomic_write_text(
        index_path,
        _markdown_with_frontmatter(
            {"type": "literature_map_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/map"]},
            "\n".join(index_lines),
        ),
    )
    paths.append(index_path)

    canonical_registry_path = map_root / "cluster_registry.yml"
    canonical_ledger_path = map_root / "cluster_ledger.yml"
    canonical_matrix_path = map_root / "evidence_matrices.yml"
    canonical_debate_path = map_root / "debate_registry.yml"
    canonical_synthesis_path = map_root / "cluster_syntheses.yml"
    canonical_gap_registry_path = map_root / "gap_registry.yml"
    canonical_gap_memory_path = map_root / "gap_memory.yml"
    canonical_gap_merge_ledger_path = map_root / "gap_merge_ledger.yml"
    canonical_search_path = map_root / "internal_search_log.yml"
    canonical_packet_path = map_root / "packet.yml"
    write_yaml(canonical_registry_path, {"updated_at": generated_at, **dict(report["cluster_registry"])})
    write_yaml(canonical_ledger_path, {"updated_at": generated_at, "events": report["cluster_registry"]["ledger"]})
    write_yaml(canonical_matrix_path, {"updated_at": generated_at, "matrices": report["evidence_matrices"]})
    write_yaml(canonical_debate_path, {"updated_at": generated_at, **dict(report["debate_registry"])})
    write_yaml(canonical_synthesis_path, {"updated_at": generated_at, "syntheses": synthesis_by_cluster})
    write_yaml(canonical_gap_registry_path, {"updated_at": generated_at, **dict(report["gap_registry"])})
    write_yaml(canonical_gap_memory_path, {"updated_at": generated_at, "entries": report["gap_memory"]})
    write_yaml(
        canonical_gap_merge_ledger_path,
        {"updated_at": generated_at, "events": merge_events},
    )
    write_yaml(canonical_search_path, {"updated_at": generated_at, "searches": report["internal_search_log"]})
    write_yaml(canonical_packet_path, packet)
    paths.extend(
        (
            canonical_registry_path,
            canonical_ledger_path,
            canonical_matrix_path,
            canonical_debate_path,
            canonical_synthesis_path,
            canonical_gap_registry_path,
            canonical_gap_memory_path,
            canonical_gap_merge_ledger_path,
            canonical_search_path,
            canonical_packet_path,
        )
    )

    canonical_gap_index = ["# Canonical Gap Index", "", f"Map ID: `{map_id}`", f"Gap record count: {len(gaps)}", ""]
    _clear_generated_markdown(canonical_gap_root)
    for gap in gaps:
        canonical_gap_path = canonical_gap_root / f"{gap_note_stem(gap)}.md"
        atomic_write_text(
            canonical_gap_path,
            _gap_markdown(gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id),
        )
        canonical_gap_index.append(f"- {_gap_wikilink(gap)} — {gap['rule']}; {gap['status']}; rank {gap['rank']}")
        paths.append(canonical_gap_path)
    canonical_gap_index_path = canonical_gap_root / "INDEX.md"
    atomic_write_text(
        canonical_gap_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_gap_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/gap"]},
            "\n".join(canonical_gap_index),
        ),
    )
    paths.append(canonical_gap_index_path)

    canonical_cluster_index = ["# Canonical Cluster Index", "", f"Map ID: `{map_id}`", f"Cluster count: {len(clusters)}", ""]
    _clear_generated_markdown(canonical_cluster_root)
    for cluster in clusters:
        canonical_cluster_path = canonical_cluster_root / f"{cluster_note_stem(cluster)}.md"
        atomic_write_text(
            canonical_cluster_path,
            _cluster_markdown(
                cluster,
                matrix_by_cluster.get(cluster["cluster_id"]),
                debate_by_cluster.get(cluster["cluster_id"]),
                gaps_by_cluster.get(str(cluster["cluster_id"]), []),
                synthesis=synthesis_by_cluster.get(str(cluster["cluster_id"]), {}),
                profile_by_source=profile_by_source,
            ),
        )
        canonical_cluster_index.append(f"- {_cluster_wikilink(cluster)} — {cluster['status']}")
        paths.append(canonical_cluster_path)
    canonical_cluster_index_path = canonical_cluster_root / "INDEX.md"
    atomic_write_text(
        canonical_cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/cluster"]},
            "\n".join(canonical_cluster_index),
        ),
    )
    paths.append(canonical_cluster_index_path)

    canonical_index_path = map_root / "INDEX.md"
    canonical_index = [
        "# Canonical Literature Map",
        "",
        f"- Map ID: `{map_id}`",
        f"- Source set: `{source_set.get('source_set_id', '')}`",
        f"- Dependency hash: `{source_set.get('dependency_hash', '')}`",
        f"- Profiles: {manifest['profile_count']}",
        f"- Clusters: {manifest['cluster_count']}",
        f"- Debates: {manifest['debate_count']}",
        f"- Gap records: {manifest['gap_count']}",
        "",
        "- [[clusters/INDEX|Cluster Index]]",
        "- [[gaps/INDEX|Gap Index]]",
    ]
    atomic_write_text(
        canonical_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_map_index", "tags": ["auto-zettelkasten/index", "auto-zettelkasten/map"]},
            "\n".join(canonical_index),
        ),
    )
    paths.append(canonical_index_path)

    canonical_manifest_path = map_root / "manifest.yml"
    canonical_artifacts = {
        "manifest": str(canonical_manifest_path),
        "cluster_registry": str(canonical_registry_path),
        "cluster_ledger": str(canonical_ledger_path),
        "evidence_matrices": str(canonical_matrix_path),
        "debate_registry": str(canonical_debate_path),
        "cluster_syntheses": str(canonical_synthesis_path),
        "gap_registry": str(canonical_gap_registry_path),
        "gap_memory": str(canonical_gap_memory_path),
        "gap_merge_ledger": str(canonical_gap_merge_ledger_path),
        "internal_search_log": str(canonical_search_path),
        "packet": str(canonical_packet_path),
        "index": str(canonical_index_path),
        "cluster_index": str(canonical_cluster_index_path),
        "gap_index": str(canonical_gap_index_path),
    }
    write_yaml(
        canonical_manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "run_id": run_id,
            "source_set_id": source_set.get("source_set_id", ""),
            "source_set_dependency_hash": source_set.get("dependency_hash", ""),
            "engine_version": "0.5.0",
            "artifact_schema_version": "1.4",
            **dict(manifest),
            "artifacts": canonical_artifacts,
        },
    )
    paths.append(canonical_manifest_path)

    manifest_path = root / "manifest.yml"
    artifact_names = {
        "manifest": str(manifest_path),
        "cluster_registry": str(registry_path),
        "cluster_ledger": str(ledger_path),
        "evidence_matrices": str(matrix_path),
        "debate_registry": str(debate_path),
        "cluster_syntheses": str(synthesis_path),
        "gap_registry": str(gap_registry_path),
        "gap_memory": str(gap_memory_path),
        "gap_merge_ledger": str(gap_merge_ledger_path),
        "internal_search_log": str(search_path),
        "packet": str(packet_path),
        "index": str(index_path),
        "canonical_map": str(canonical_manifest_path),
    }
    write_yaml(
        manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "engine_version": "0.5.0",
            "artifact_schema_version": "1.4",
            **dict(manifest),
            "artifacts": artifact_names,
        },
    )
    paths.insert(0, manifest_path)
    packet = {
        **packet,
        "path": str(canonical_packet_path),
        "compatibility_path": str(packet_path),
        "map_path": str(map_root),
    }
    return packet, paths


def build_literature_map(
    workspace: Path,
    *,
    source_set: Mapping[str, Any],
    notes: Sequence[Mapping[str, Any]],
    question: str | None,
    run_id: str,
    profiles: Sequence[Any] | None = None,
    request: Any = None,
    policy: Any = None,
    reasoner: Any = None,
    stage_callback: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Path]]:
    """Compatibility entry point for current pipeline callers."""
    request_values = _as_mapping(request) if request is not None else {}
    effective_policy = policy or request_values.get("literature_policy")
    effective_question = question if question is not None else request_values.get("question")
    effective_run_id = run_id or str(request_values.get("run_id") or request_values.get("map_id") or "literature-map")
    map_id = stable_literature_map_id(source_set, effective_question)
    previous_registry = _load_map_cluster_registry(workspace, map_id)
    reasoner_calls = (
        _CheckpointedReasonerCalls(
            workspace,
            effective_run_id,
            reasoner,
            request,
            stage_callback=stage_callback,
        )
        if reasoner is not None and not isinstance(reasoner, Mapping) and request is not None
        else None
    )
    report = build_literature_report(
        profiles if profiles is not None else notes,
        previous_registry=previous_registry,
        policy=effective_policy,
        question=effective_question,
        reasoner=reasoner,
        request=request,
        stage_callback=stage_callback,
        reasoner_call=reasoner_calls,
    )
    if reasoner_calls is not None:
        report["manifest"].update(
            {
                "synthesis_call_count": reasoner_calls.provider_calls,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits,
                "synthesis_failure_count": reasoner_calls.failures,
            }
        )
        report["packet"].update(
            {
                "synthesis_call_count": reasoner_calls.provider_calls,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits,
                "synthesis_failure_count": reasoner_calls.failures,
            }
        )
    packet, paths = persist_literature_report(
        workspace,
        report,
        source_set=source_set,
        run_id=effective_run_id,
        question=effective_question,
        map_id=map_id,
    )
    clusters = report["cluster_registry"]["clusters"]
    gaps = report["gap_registry"]["gaps"]
    promoted_clusters = [row for row in clusters if row.get("promoted")]
    cluster_map = {
        "status": (
            "built"
            if promoted_clusters
            else ("cluster_candidates" if clusters else "blocked_insufficient_source_memory")
        ),
        "automation_status": (
            "promoted" if promoted_clusters else ("candidate" if clusters else "not_applicable")
        ),
        "clusters": clusters,
        "relations": report["relations"],
        "rejected_proposals": report["cluster_registry"]["rejected_proposals"],
        "unclustered_sources": report["cluster_registry"]["unclustered_sources"],
        "cluster_syntheses": report["cluster_syntheses"],
        "synthesized_cluster_count": report["manifest"]["synthesized_cluster_count"],
        "minimum_analytical_notes": 2,
        "path": str(workspace / "03_literature_synthesis" / "clusters" / "clusters.yml"),
        "registry_path": str(workspace / "03_literature_synthesis" / "cluster_registry.yml"),
    }
    gap_status = (
        "blocked_no_source_backed_clusters"
        if not clusters
        else ("mapped_collection_gaps" if any(row["promoted"] for row in gaps) else ("gap_leads" if gaps else "complete_no_qualifying_gaps"))
    )
    gap_map = {
        "status": gap_status,
        "gap_candidates": gaps,
        "rejected_candidates": report["gap_registry"].get("rejected_candidates", []),
        "rejected_underspecified_gap_count": report["manifest"]["rejected_underspecified_gap_count"],
        "rejected_gap_quality_count": report["manifest"]["rejected_gap_quality_count"],
        "merged_gap_count": report["manifest"]["merged_gap_count"],
        "gap_merge_ledger": report["gap_registry"].get("merge_ledger", []),
        "gap_merge_ledger_path": str(workspace / "03_literature_synthesis" / "gap_merge_ledger.yml"),
        "novelty_claimed": False,
        "path": str(workspace / "03_literature_synthesis" / "gaps" / "gaps.yml"),
        "registry_path": str(workspace / "03_literature_synthesis" / "gap_registry.yml"),
    }
    return cluster_map, gap_map, packet, paths


def run_literature_map(
    request: Any,
    *,
    profiles: Sequence[Any],
    source_set: Mapping[str, Any],
    reasoner: Any = None,
    stage_callback: Any = None,
) -> Any:
    """Public v0.5 entry point for systematic mapping over existing profiles."""
    from .models import LiteratureMapReport

    values = _as_mapping(request)
    workspace = Path(values.get("workspace") or ".").expanduser()
    question = values.get("question") or None
    policy = values.get("literature_policy")
    run_id = str(values.get("run_id") or "")
    map_id = stable_literature_map_id(source_set, question)
    if not bool(_policy_value(policy, "synthesis_enabled", True)):
        return LiteratureMapReport(
            status="disabled",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(source_set.get("source_set_id") or values.get("source_set_id") or ""),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="synthesis_disabled",
        )
    if bool(_policy_value(policy, "require_question", False)) and not question:
        return LiteratureMapReport(
            status="blocked",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(source_set.get("source_set_id") or values.get("source_set_id") or ""),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="question_required",
        )
    if reasoner is not None and bool(getattr(reasoner, "is_cloud", False)) and not bool(values.get("allow_cloud", False)):
        return LiteratureMapReport(
            status="blocked",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(source_set.get("source_set_id") or values.get("source_set_id") or ""),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="cloud_reasoner_not_allowed",
        )

    previous_registry = _load_map_cluster_registry(workspace, map_id)
    report = build_literature_report(
        profiles,
        previous_registry=previous_registry if isinstance(previous_registry, Mapping) else {},
        policy=policy,
        question=question,
        reasoner=reasoner,
        request=request,
        stage_callback=stage_callback,
    )
    _, paths = persist_literature_report(
        workspace,
        report,
        source_set=source_set,
        run_id=run_id,
        question=question,
        map_id=map_id,
    )
    map_root = workspace / "03_literature_synthesis" / "maps" / map_id
    canonical_manifest = read_yaml(map_root / "manifest.yml", {}) or {}
    artifacts = canonical_manifest.get("artifacts", {}) if isinstance(canonical_manifest, Mapping) else {}
    artifact_paths = {
        str(key): Path(str(value)).relative_to(workspace) if Path(str(value)).is_absolute() else Path(str(value))
        for key, value in artifacts.items()
    }
    return LiteratureMapReport(
        status="completed",
        map_id=map_id,
        run_id=run_id,
        source_set_id=str(source_set.get("source_set_id") or values.get("source_set_id") or ""),
        stage="completed",
        counts={**dict(report["manifest"]), "written_artifact_count": len(paths)},
        artifact_paths=artifact_paths,
    )


# Small aliases keep stage discovery straightforward for callers using generic names.
map_relations = map_profile_relations
cluster_profiles = map_overlapping_clusters
