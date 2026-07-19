from __future__ import annotations

import json
import inspect
import re
import time
import csv
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .files import atomic_write_text, now_iso, read_yaml, safe_filename, sha256_text, slugify, write_yaml
from .navigation import build_navigation_graph, build_typed_source_relations, rank_topic_neighborhoods


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
LITERATURE_ALGORITHM_VERSION = "10"
CLUSTER_PROPOSAL_PROMPT_VERSION = "12"
CLUSTER_SYNTHESIS_PROMPT_VERSION = "6"
GAP_REASONING_PROMPT_VERSION = "6"
ANCHOR_ALGORITHM_VERSION = "3"
SUPPORT_ENVELOPE_VERSION = "1"
PROPOSITION_ALGORITHM_VERSION = "10"
PROPOSITION_MATRIX_VERSION = "2"
GAP_RULE_VERSION = "2"
STUDY_LINEAGE_VERSION = "1"
INDEPENDENCE_ALGORITHM_VERSION = "1"
QUANTITATIVE_VALIDATION_VERSION = "1"
LOCATOR_AUDIT_VERSION = "1"

DEBATE_STATES = {
    "mapped_debate",
    "mapped_consensus",
    "emerging_convergence",
    "aligned_institutional_guidance",
    "within_program_consistency",
    "mixed_evidence",
    "conditional_relationship",
    "complementary_positions",
    "parallel_literatures",
    "single_position",
    "no_debate",
}

CAUSAL_SUPPORT_ROLES = {"causal", "mechanism_evidence"}
EMPIRICAL_SUPPORT_ROLES = {"descriptive", "associational", *CAUSAL_SUPPORT_ROLES}
ARGUMENT_SUPPORT_ROLES = {
    "conceptual",
    "interpretive",
    "normative",
    "methodological",
    "practitioner_guidance",
}

CAUSAL_LANGUAGE = re.compile(
    r"\b(?:caus(?:e|es|ed|al)|effect|leads? to|produces?|drives?|results? in|"
    r"improv(?:e|es|ed)|enhanc(?:e|es|ed)|increas(?:e|es|ed)|reduc(?:e|es|ed)|"
    r"undermin(?:e|es|ed)|hinder(?:s|ed)?|prevent(?:s|ed)?)\b",
    re.I,
)
ATTRIBUTED_RELATIONSHIP = re.compile(
    r"\b(?:assert(?:s|ed)?|claim(?:s|ed)?|argu(?:e|es|ed)|recommend(?:s|ed)?|"
    r"advocat(?:e|es|ed)|propos(?:e|es|ed)|guidance|reported?|describ(?:e|es|ed)|"
    r"find(?:s|ing)?|observ(?:e|es|ed|ation)|identif(?:y|ies|ied)|converg(?:e|es|ed|ing))\b",
    re.I,
)
NONCAUSAL_RELATIONSHIP = re.compile(
    r"\b(?:associat(?:e|es|ed|ion)|correlat(?:e|es|ed|ion)|link(?:s|ed)?|"
    r"predict(?:s|ed|or)?|relationship|odds?|probabilit(?:y|ies)|marginal effect)\b",
    re.I,
)
CAUSAL_NEGATION = re.compile(
    r"\b(?:no|none|not|cannot|can't|does not|do not|lack(?:s|ed|ing)?|absence|"
    r"unsupported|unsubstantiated|insufficient|without)\b"
    r"[^.!?;]{0,80}\bcaus(?:e|es|ed|al|ality|ation)\b",
    re.I,
)
INTERNAL_PROJECTION_ID = re.compile(r"\b(?:anchor|proposition|assertion)-[a-z0-9][a-z0-9-]*\b", re.I)
MIN_CLUSTER_VERDICT_WORDS = 50

CLUSTER_SYNTHESIS_SECTIONS = (
    "source_contributions",
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


_PROVIDER_INPUT_DEPENDENCY_COMPONENTS = {
    "stage",
    "key",
    "method",
    "provider",
    "model",
    "source_set_id",
    "profile_dependencies",
    "context",
    "policy",
    "prompt_version",
}


def _has_unqualified_causal_language(value: Any) -> bool:
    text = str(value or "")
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not CAUSAL_LANGUAGE.search(sentence):
            continue
        if (
            ATTRIBUTED_RELATIONSHIP.search(sentence)
            or NONCAUSAL_RELATIONSHIP.search(sentence)
            or CAUSAL_NEGATION.search(sentence)
        ):
            continue
        return True
    return False


def _human_projection_text(value: Any) -> str:
    """Remove machine-local IDs from prose while preserving readable citations."""

    text = str(value or "")
    text = re.sub(r"\bproposition-[a-z0-9][a-z0-9-]*\b\s*;?\s*", "", text, flags=re.I)
    text = re.sub(r",?\s*\b(?:anchor|assertion)-[a-z0-9][a-z0-9-]*\b", "", text, flags=re.I)
    text = re.sub(r"\(\s*[;,]\s*", "(", text)
    text = re.sub(r"\s*[;,]\s*\)", ")", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


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
        self._synthesized_cluster_ids: set[str] = set()

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
                    "profile_schema_version": str(_as_mapping(profile).get("profile_schema_version") or ""),
                    # Note and anchor hashes alone do not capture synthesis
                    # eligibility, support-envelope downgrades, typed
                    # quantitative records, or other profile-level repairs.
                    # Hash the stable semantic profile projection so a repaired
                    # profile cannot reuse a proposal produced from its older
                    # excluded or weaker state.
                    "profile_content_hash": _stable_hash(
                        _checkpoint_dependency_context(_as_mapping(profile))
                    ),
                    "anchor_revisions": sorted(
                        str(_as_mapping(anchor).get("revision_hash") or "")
                        for anchor in _as_mapping(profile).get("evidence_anchors", []) or []
                        if _as_mapping(anchor).get("revision_hash")
                    ),
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
            "anchor_algorithm_version": ANCHOR_ALGORITHM_VERSION,
            "support_envelope_version": SUPPORT_ENVELOPE_VERSION,
            "proposition_algorithm_version": PROPOSITION_ALGORITHM_VERSION,
            "proposition_matrix_version": PROPOSITION_MATRIX_VERSION,
            "gap_rule_version": GAP_RULE_VERSION,
            "study_lineage_version": STUDY_LINEAGE_VERSION,
            "independence_algorithm_version": INDEPENDENCE_ALGORITHM_VERSION,
            "quantitative_validation_version": QUANTITATIVE_VALIDATION_VERSION,
            "locator_audit_version": LOCATOR_AUDIT_VERSION,
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
        # A checkpoint is reusable only when the complete provenance contract
        # still matches. In particular, v0.5 dimension matrices and document-
        # level cluster prompts must never be upgraded into proposition-backed
        # outputs without a fresh synthesis call.
        compatible_fingerprints = {fingerprint}
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
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return dict(matching_checkpoint["response"])
        # A deterministic admission/validation repair must not repeat a paid
        # provider call when the provider-visible prompt and inputs are
        # unchanged. Revalidate the preserved response under the current local
        # contract and give it the new complete fingerprint.
        provider_input_candidates = [existing]
        if history_root.is_dir():
            provider_input_candidates.extend(
                read_yaml(candidate, {}) or {}
                for candidate in sorted(history_root.glob("*.yml"))
            )
        for prior in provider_input_candidates:
            if not _same_provider_inputs(
                prior,
                dependency_component_hashes,
                stage=stage,
                current_context_hashes=dependency_context_hashes,
            ):
                continue
            prior_response = prior.get("response")
            if not isinstance(prior_response, Mapping):
                continue
            recovered_response = _revalidate_raw_synthesis_response(stage, prior_response)
            if recovered_response is None:
                continue
            self._archive_successful_checkpoint(existing, history_root)
            write_yaml(
                path,
                {
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
                    "response": recovered_response,
                    "revalidated_from_provider_input_fingerprint": str(prior.get("fingerprint") or ""),
                    "updated_at": now_iso(),
                },
            )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return recovered_response
        failed_checkpoints = [
            checkpoint
            for checkpoint in (
                existing,
                read_yaml(failure_path, {}) or {},
            )
            if isinstance(checkpoint, Mapping)
            and checkpoint.get("fingerprint") == fingerprint
            and isinstance(checkpoint.get("raw_response"), Mapping)
        ]
        for failed_checkpoint in failed_checkpoints:
            recovered_response = _revalidate_raw_synthesis_response(
                stage,
                failed_checkpoint["raw_response"],
            )
            if recovered_response is None:
                continue
            write_yaml(
                path,
                {
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
                    "response": recovered_response,
                    "recovered_from_failed_raw_response": True,
                    "updated_at": now_iso(),
                },
            )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return recovered_response
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
                self._mark_synthesized_cluster(key)
            return dict(response)
        except Exception as exc:
            self.failures += 1
            raw_response = getattr(self.reasoner, "last_literature_response", None)
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
            if isinstance(raw_response, Mapping):
                failure_payload["raw_response"] = dict(raw_response)
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

    def _mark_synthesized_cluster(self, key: str) -> None:
        cluster_id = str(key).split("--repair", 1)[0]
        self._synthesized_cluster_ids.add(cluster_id)
        self.synthesized_clusters = len(self._synthesized_cluster_ids)

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


def _revalidate_raw_synthesis_response(
    stage: str,
    raw_response: Mapping[str, Any],
) -> dict[str, Any] | None:
    kind = {
        "cluster_proposal": "cluster_proposal",
        "cluster_synthesis": "cluster_synthesis",
        "gap_adjudication": "gap_adjudication",
    }.get(stage)
    if kind is None:
        return None
    # Keep provider transport independent from the collection mapper while
    # allowing a paid response to survive a local schema-adapter repair.
    from .readers import _validate_literature_response

    try:
        return _validate_literature_response(raw_response, kind=kind)
    except (TypeError, ValueError, RuntimeError):
        return None


def _same_provider_inputs(
    checkpoint: Any,
    current_component_hashes: Mapping[str, str],
    *,
    stage: str = "",
    current_context_hashes: Mapping[str, str] | None = None,
) -> bool:
    if not isinstance(checkpoint, Mapping):
        return False
    prior_hashes = checkpoint.get("dependency_component_hashes")
    if not isinstance(prior_hashes, Mapping):
        return False
    exact_match = all(
        str(prior_hashes.get(component) or "") == str(current_component_hashes.get(component) or "")
        for component in _PROVIDER_INPUT_DEPENDENCY_COMPONENTS
    )
    if exact_match:
        return True
    # Provider adapters use compact stage-specific packets. A successful call
    # remains reusable when every provider-visible input is unchanged even if a
    # richer local context gained projection-only records.
    if stage not in {"cluster_proposal", "gap_adjudication"} or current_context_hashes is None:
        return False
    if not all(
        str(prior_hashes.get(component) or "") == str(current_component_hashes.get(component) or "")
        for component in _PROVIDER_INPUT_DEPENDENCY_COMPONENTS - {"context"}
    ):
        return False
    prior_context_hashes = checkpoint.get("dependency_context_hashes")
    if not isinstance(prior_context_hashes, Mapping):
        return False
    visible_context_components = (
        ("propositions",)
        if stage == "cluster_proposal"
        else ("candidates", "internal_search_log")
    )
    return all(
        str(prior_context_hashes.get(component) or "")
        == str(current_context_hashes.get(component) or "")
        for component in visible_context_components
    )


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
            "source_contributions",
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
_GENERIC_COMPARABILITY_TERMS = {
    "case",
    "civil",
    "conflict",
    "conflicts",
    "international",
    "internationalized",
    "intrastate",
    "mediation",
    "mediator",
    "number",
    "numbers",
    "outcome",
    "peace",
    "probability",
    "process",
    "rate",
    "share",
    "study",
    "war",
    "wars",
}
_OUTCOME_SIGNAL_TERMS = {
    "agreement",
    "casualty",
    "consent",
    "duration",
    "durability",
    "effectiveness",
    "implementation",
    "legitimacy",
    "onset",
    "participation",
    "relapse",
    "settlement",
    "stability",
    "success",
    "trust",
    "withdrawal",
}
_NON_DISCRIMINATING_RELATIONSHIP_TERMS = {
    "associated",
    "association",
    "correlated",
    "correlation",
    "create",
    "effect",
    "fewer",
    "greater",
    "higher",
    "lower",
    "longer",
    "negative",
    "negatively",
    "positive",
    "positively",
    "predict",
    "predicted",
    "relationship",
    "shorter",
    "significant",
    "significantly",
}
_WEAK_LOCATOR_MARKERS = {"", "unknown", "unavailable", "not reported", "n/a", "none", "not supplied"}
_TRACEABLE_LOCATOR = re.compile(
    r"(?:\b(?:p{1,2}\.?|pages?|paragraphs?|paras?)\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\b|"
    r"\b(?:chapter|section|appendix)\s+[a-z0-9ivx.-]+\b|"
    r"\b(?:abstract|introduction|background|literature review|methods?|methodology|data|results?|findings?|"
    r"discussion|conclusions?|limitations?)\s*(?:section)?\b|\b(?:table|figure)\s*\d+[a-z]?\b)",
    flags=re.IGNORECASE,
)
_GENERATED_NOTE_LOCATOR = re.compile(
    r"\b(?:atomic note|detailed findings?|technical findings?|plain[- ]english findings?|"
    r"key findings?|automated validation|source lineage)\b(?:\s*\(\d+\))?",
    flags=re.IGNORECASE,
)
_PAGE_LOCATOR = re.compile(
    r"\b(?:p{1,2}\.?|pages?|paragraphs?|paras?)\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\b",
    flags=re.IGNORECASE,
)
_OBJECT_LOCATOR = re.compile(r"\b(?:table|figure|appendix)\s+[a-z0-9ivx.-]+\b", flags=re.IGNORECASE)
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
    if value is None:
        return {}
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
        and not _GENERATED_NOTE_LOCATOR.search(text)
    )


def _source_locator(value: Any) -> dict[str, Any]:
    """Normalize a locator and distinguish source-native from generated note headings."""

    text = _locator_text(value).strip()
    normalized = _normalized_locator(text)
    if not text or text.casefold() in _WEAK_LOCATOR_MARKERS:
        kind = "missing"
    elif _GENERATED_NOTE_LOCATOR.search(text):
        kind = "generated_note_heading"
    elif _PAGE_LOCATOR.search(text):
        kind = "page_or_paragraph"
    elif _OBJECT_LOCATOR.search(text):
        kind = "source_object"
    elif _TRACEABLE_LOCATOR.search(text):
        kind = "source_heading"
    else:
        kind = "untyped_text"
    return {
        "raw": text,
        "normalized": normalized,
        "kind": kind,
        "traceable": kind in {"page_or_paragraph", "source_object", "source_heading"},
        "strong_synthesis_support": kind in {"page_or_paragraph", "source_object", "source_heading"},
        "rejection_reason": "generated_note_heading" if kind == "generated_note_heading" else "",
    }


def _normalized_locator(value: Any) -> str:
    text = _locator_text(value).casefold().replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\b(pp?)\.\s+(?=\d)", r"\1.", text)
    return re.sub(r"\s+", " ", text).strip(" .;,:")


def _reference_matches_profile(reference: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    """Require a reasoner reference to resolve to an existing located anchor."""
    if str(reference.get("source_id") or "") != str(profile.get("source_id") or ""):
        return False
    anchor_id = str(
        reference.get("evidence_anchor_id")
        or reference.get("claim_id")
        or reference.get("finding_id")
        or ""
    )
    if not anchor_id:
        return False
    claim = next(
        (
            row
            for row in profile.get("claims", []) or []
            if str(row.get("evidence_anchor_id") or row.get("claim_id") or "") == anchor_id
        ),
        None,
    )
    if claim is None or not claim.get("locator_complete"):
        return False
    locator = reference.get("locator") or claim.get("locator")
    return _complete_locator(locator) and _normalized_locator(locator) == _normalized_locator(claim.get("locator"))


def _reference_is_synthesis_eligible(
    reference: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> bool:
    if not _reference_matches_profile(reference, profile):
        return False
    anchor_id = str(
        reference.get("evidence_anchor_id")
        or reference.get("claim_id")
        or reference.get("finding_id")
        or ""
    )
    return any(
        str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or "") == anchor_id
        and _anchor_is_synthesis_eligible(anchor)
        for anchor in profile.get("claims", []) or []
    )


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
    # Statistical nulls take precedence over the sign of an imprecise point
    # estimate. "Negative (not significant)" is null evidence, not a mapped
    # negative position in a debate.
    if any(marker in text for marker in ("null", "no effect", "no association", "not significant")):
        return "null"
    if any(marker in text for marker in ("positive", "increase", "higher", "supports", "improves")):
        return "positive"
    if any(marker in text for marker in ("negative", "decrease", "lower", "undermines", "reduces")):
        return "negative"
    if any(marker in text for marker in ("mixed", "conditional", "heterogeneous", "varies")):
        return "mixed"
    return slugify(text, "not-reported").replace("-", "_")


def _normalized_support_envelope(item: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    supplied = item.get("support_envelope")
    envelope = dict(supplied) if isinstance(supplied, Mapping) else {}
    empirical_role = str(envelope.get("empirical_role") or item.get("empirical_role") or "none").casefold()
    argument_role = str(envelope.get("argument_role") or item.get("argument_role") or "none").casefold()
    finding_type = str(item.get("finding_type") or "").casefold()
    claim_text = str(item.get("claim") or item.get("finding") or item.get("text") or "").casefold()
    if empirical_role not in {*EMPIRICAL_SUPPORT_ROLES, "none"}:
        empirical_role = "none"
    if argument_role not in {*ARGUMENT_SUPPORT_ROLES, "none"}:
        argument_role = "none"
    if empirical_role == "none" and argument_role == "none":
        if any(token in finding_type for token in ("causal", "experiment", "quasi")):
            empirical_role = "causal"
        elif "mechanism" in finding_type or "process tracing" in claim_text:
            empirical_role = "mechanism_evidence"
        elif any(token in finding_type for token in ("association", "correlation", "regression", "statistical")):
            empirical_role = "associational"
        elif any(token in finding_type for token in ("descriptive", "qualitative", "empirical")):
            empirical_role = "descriptive"
        elif any(token in finding_type for token in ARGUMENT_SUPPORT_ROLES):
            argument_role = next(token for token in ARGUMENT_SUPPORT_ROLES if token in finding_type)
        elif claim_text and _complete_locator(item.get("locator") or item.get("locators")):
            # Legacy analytical rows did not declare evidence roles. Treat the
            # located statement as descriptive support, never causal support.
            empirical_role = "descriptive"

    coverage = str(envelope.get("coverage") or item.get("coverage") or "").casefold()
    if not coverage:
        source_coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
        if source_coverage.get("full_document") is True:
            coverage = "full_text"
        elif str(raw.get("note_status") or raw.get("status") or "").casefold() in ANALYTICAL_STATUSES:
            coverage = "full_text"
        else:
            coverage = "unknown"
    if coverage not in {"full_text", "limited_text", "abstract", "metadata", "unknown"}:
        coverage = "unknown"
    scope = envelope.get("scope") if isinstance(envelope.get("scope"), Mapping) else {}
    restrictions = [
        str(value)
        for value in envelope.get("restrictions", item.get("restrictions", [])) or []
        if str(value).strip()
    ]
    support_status = str(envelope.get("support_status") or item.get("support_status") or "").casefold()
    if support_status not in {"supported", "support_unknown", "limited", "unsupported"}:
        support_status = (
            "supported"
            if coverage == "full_text" and (empirical_role != "none" or argument_role != "none")
            else "support_unknown"
        )
    return {
        "empirical_role": empirical_role,
        "argument_role": argument_role,
        "coverage": coverage,
        "scope": {
            str(key): [str(value) for value in values or [] if str(value).strip()]
            for key, values in scope.items()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray))
        },
        "restrictions": restrictions,
        "support_status": support_status,
    }


def _anchor_evidence_role(item: Mapping[str, Any], envelope: Mapping[str, Any]) -> str:
    supplied = str(item.get("evidence_role") or "").strip().casefold()
    if supplied:
        return supplied
    if str(envelope.get("empirical_role") or "none") != "none":
        return str(envelope["empirical_role"])
    if str(envelope.get("argument_role") or "none") != "none":
        return str(envelope["argument_role"])
    return "support_unknown"


def _stable_evidence_anchor_id(
    source_id: str,
    locator: str,
    evidence_role: str,
    item: Mapping[str, Any],
) -> str:
    supplied = str(
        item.get("evidence_anchor_id")
        or item.get("claim_id")
        or item.get("finding_id")
        or ""
    )
    if supplied:
        return supplied
    source_span = str(item.get("source_span") or item.get("span") or item.get("quote") or "")
    identity = {
        "source_id": source_id,
        "locator": _normalized_locator(locator),
        "evidence_role": evidence_role,
        "source_span_hash": _stable_hash(source_span)[:12] if source_span else "",
    }
    return f"anchor-{_stable_hash(identity)[:16]}"


def _normalize_claims(raw: Mapping[str, Any], source_id: str, family_id: str) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    candidate_fields = (
        # Profiles may already contain a selected anchor set while retaining
        # more precise structured findings. Keep both available to map-local
        # proposition admission: otherwise a broad table-summary anchor can
        # hide the exact row that tests the proposed relationship.
        ("evidence_anchors", "findings")
        if raw.get("evidence_anchors")
        else ("claims", "structured_findings", "findings", "evidence_records")
    )
    for key in candidate_fields:
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
    for candidate in candidates[:24]:
        item = _as_mapping(candidate) if not isinstance(candidate, str) else {"finding": candidate}
        locator = _locator_text(item.get("locator") or item.get("locators") or raw.get("locator") or raw.get("locators"))
        # Source-level dimensions are retrieval metadata only. They must not be
        # copied into every anchor; doing so creates false evidence cells.
        dimensions = {dimension: _dimension_values(item, dimension) for dimension in EVIDENCE_DIMENSIONS}
        direction = _normalize_direction(dimensions["finding direction"] or item.get("direction") or item.get("finding_direction"))
        dimensions["finding direction"] = [] if direction == "not_reported" else [direction]
        text = str(item.get("claim") or item.get("finding") or item.get("text") or item.get("description") or "").strip()
        envelope = _normalized_support_envelope(item, raw)
        evidence_role = _anchor_evidence_role(item, envelope)
        anchor_id = _stable_evidence_anchor_id(source_id, locator, evidence_role, item)
        source_locator = _source_locator(locator)
        quantitative_raw = (
            item.get("quantitative_results")
            or ([item.get("quantitative_result")] if isinstance(item.get("quantitative_result"), Mapping) else [])
            or item.get("statistics")
            or item.get("estimates")
            or []
        )
        quantitative_results = [
            _as_mapping(row)
            for row in quantitative_raw
            if isinstance(row, Mapping)
        ]
        claims.append(
            {
                "evidence_anchor_id": anchor_id,
                # Internal compatibility alias. New persisted references also
                # include evidence_anchor_id and readers prefer it.
                "claim_id": anchor_id,
                "source_id": source_id,
                "study_family_id": family_id,
                "text": text,
                "locator": locator,
                "locator_complete": bool(source_locator["traceable"]),
                "source_locator": source_locator,
                "dimensions": dimensions,
                "direction": direction,
                "topic": str(item.get("topic") or raw.get("topic") or ""),
                "plain_english_meaning": str(item.get("plain_english_meaning") or item.get("plain_english") or ""),
                "magnitude": str(item.get("magnitude") or item.get("estimate") or ""),
                "comparison": str(item.get("comparison") or ""),
                "uncertainty": str(item.get("uncertainty") or ""),
                "quantitative_results": quantitative_results,
                "evidence_role": evidence_role,
                "support_envelope": envelope,
                "support_status": str(envelope.get("support_status") or "support_unknown"),
                "boundary_condition": str(item.get("boundary_condition") or item.get("boundary") or "; ".join(_flatten_values(item.get("conditions")))),
                "mechanism_tested": item.get("mechanism_tested"),
                "addresses_gap": item.get("addresses_gap", False),
                "gap_rule": str(item.get("gap_rule") or ""),
                "answer_status": str(item.get("answer_status") or ""),
            }
        )
    return sorted(
        {_stable_hash([row["source_id"], row["evidence_anchor_id"]]): row for row in claims}.values(),
        key=lambda row: row["evidence_anchor_id"],
    )


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


def _lineage_values(value: Any) -> list[str]:
    return sorted({_canonical_phrase(row) or str(row).casefold() for row in _flatten_values(value) if str(row).strip()})


def _normalized_study_lineage(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    family_id: str,
    doi: str,
) -> dict[str, Any]:
    supplied = _as_mapping(raw.get("study_lineage"))
    source_role = str(raw.get("source_role") or "").casefold()
    institution = str(
        supplied.get("institution")
        or next(iter(_flatten_values(supplied.get("institutions"))), "")
        or supplied.get("institutional_series")
        or raw.get("institution")
        or raw.get("issuing_institution")
        or raw.get("publisher")
        or ""
    ).strip()
    program_ids = _lineage_values(
        supplied.get("program_ids")
        or supplied.get("program_id")
        or supplied.get("institutional_series")
        or raw.get("research_program")
        or raw.get("program_id")
    )
    dataset_ids = _lineage_values(
        supplied.get("dataset_ids")
        or supplied.get("dataset_id")
        or supplied.get("datasets")
        or supplied.get("data_sources")
        or raw.get("datasets")
        or raw.get("dataset")
    )
    sample_ids = _lineage_values(
        supplied.get("sample_ids")
        or supplied.get("sample_id")
        or supplied.get("sampling_frame")
        or raw.get("sample_id")
        or raw.get("fieldwork_id")
    )
    publication_id = str(
        supplied.get("publication_id")
        or (f"doi:{doi}" if doi else source_id)
    )
    explicit_group = str(
        supplied.get("evidence_base_group_id")
        or raw.get("evidence_base_group_id")
        or ""
    ).strip()
    supplied_family = str(
        supplied.get("study_family_id")
        or raw.get("study_family_id")
        or raw.get("study_id")
        or ""
    ).strip()
    substantive_family = (
        supplied_family
        if supplied_family
        and not supplied_family.casefold().startswith(("doi:", "title:"))
        and supplied_family != source_id
        else ""
    )
    explicit_group_basis = str(supplied.get("group_basis") or "").strip()
    if explicit_group and explicit_group_basis in {
        "shared_sample_or_fieldwork",
        "shared_dataset",
        "shared_research_program",
        "study_family",
        "institutional_guidance_program",
    }:
        group_basis = "explicit_evidence_base_group"
        group_identity: Any = explicit_group
    elif sample_ids:
        group_basis = "shared_sample_or_fieldwork"
        group_identity = sample_ids
    elif dataset_ids:
        group_basis = "shared_dataset"
        group_identity = dataset_ids
    elif program_ids:
        group_basis = "shared_research_program"
        group_identity = program_ids
    elif substantive_family:
        group_basis = "study_family"
        group_identity = substantive_family
    elif institution and any(marker in source_role for marker in ("practitioner", "guidance", "institutional")):
        group_basis = "institutional_guidance_program"
        group_identity = _canonical_phrase(institution)
    else:
        # A publication identity proves that a record exists; it does not
        # prove that its sample, dataset, fieldwork, or institutional evidence
        # is independent from another publication.
        group_basis = "independence_uncertain"
        group_identity = publication_id
    group_id = explicit_group or f"evidence-base-{_stable_hash([group_basis, group_identity])[:16]}"
    counted_as_independent = group_basis != "independence_uncertain"
    return {
        "lineage_id": str(
            supplied.get("lineage_id")
            or supplied.get("study_lineage_id")
            or f"lineage-{_stable_hash(source_id)[:16]}"
        ),
        "source_id": source_id,
        "publication_id": publication_id,
        "study_family_id": family_id,
        "evidence_base_group_id": group_id,
        "group_basis": group_basis,
        "program_ids": program_ids,
        "dataset_ids": dataset_ids,
        "sample_ids": sample_ids,
        "institution": institution,
        "authors": [str(value) for value in supplied.get("authors", []) or [] if str(value).strip()],
        "populations": [str(value) for value in supplied.get("populations", []) or [] if str(value).strip()],
        "periods": [str(value) for value in supplied.get("periods", []) or [] if str(value).strip()],
        "overlap_signals": [str(value) for value in supplied.get("overlap_signals", []) or [] if str(value).strip()],
        "publication_relationships": [
            _as_mapping(value)
            for value in supplied.get("publication_relationships", []) or []
            if isinstance(value, Mapping)
        ],
        "independence_status": (
            "independent_evidence_base" if counted_as_independent else "independence_uncertain"
        ),
        "counted_as_independent": counted_as_independent,
        "confidence": str(supplied.get("confidence") or ("moderate" if counted_as_independent else "unknown")),
        "version": STUDY_LINEAGE_VERSION,
    }


def build_independence_records(profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return strict public lineage records and conservative effective-base accounting."""

    rows = [dict(row) for row in profiles]
    strict_lineages: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for profile in rows:
        lineage = _as_mapping(profile.get("study_lineage"))
        source_id = str(profile.get("source_id") or "")
        lineage_id = str(lineage.get("lineage_id") or f"lineage-{_stable_hash(source_id)[:16]}")
        confidence = str(lineage.get("confidence") or "unknown").casefold()
        if confidence == "medium":
            confidence = "moderate"
        if confidence not in {"high", "moderate", "low", "unknown"}:
            confidence = "unknown"
        strict_lineages.append(
            {
                "study_lineage_id": lineage_id,
                "source_ids": [source_id] if source_id else [],
                "authors": [str(value) for value in lineage.get("authors", []) or [] if str(value)],
                "institutions": [str(lineage.get("institution"))] if lineage.get("institution") else [],
                "datasets": [str(value) for value in lineage.get("dataset_ids", []) or [] if str(value)],
                "data_sources": [str(value) for value in lineage.get("dataset_ids", []) or [] if str(value)],
                "sampling_frame": "; ".join(
                    str(value) for value in lineage.get("sample_ids", []) or [] if str(value)
                ),
                "unit_of_analysis": str(lineage.get("unit_of_analysis") or ""),
                "populations": [str(value) for value in lineage.get("populations", []) or [] if str(value)],
                "periods": [str(value) for value in lineage.get("periods", []) or [] if str(value)],
                "publication_relationships": [
                    _as_mapping(value)
                    for value in lineage.get("publication_relationships", []) or []
                    if isinstance(value, Mapping)
                ],
                "institutional_series": "; ".join(
                    str(value) for value in lineage.get("program_ids", []) or [] if str(value)
                ),
                "overlap_signals": sorted(
                    {
                        *[str(value) for value in lineage.get("overlap_signals", []) or [] if str(value)],
                        *(
                            [str(lineage.get("group_basis"))]
                            if lineage.get("group_basis")
                            and lineage.get("group_basis") != "independence_uncertain"
                            else []
                        ),
                    }
                ),
                "confidence": confidence,
            }
        )
        group_id = str(lineage.get("evidence_base_group_id") or "")
        if group_id:
            groups[group_id].append(profile)

    lineage_id_by_source = {
        str(source_id): str(lineage["study_lineage_id"])
        for lineage in strict_lineages
        for source_id in lineage["source_ids"]
    }
    group_rows: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    for group_id, members in sorted(groups.items()):
        lineages = [_as_mapping(row.get("study_lineage")) for row in members]
        source_ids = sorted(str(row.get("source_id") or "") for row in members if row.get("source_id"))
        bases = sorted({str(row.get("group_basis") or "") for row in lineages if row.get("group_basis")})
        overlap_signals = sorted(
            {
                *bases,
                *[
                    str(signal)
                    for lineage in lineages
                    for signal in lineage.get("overlap_signals", []) or []
                    if str(signal)
                ],
            }
        )
        counted = all(lineage.get("counted_as_independent") is True for lineage in lineages)
        if not counted:
            relationship = "independence_uncertain"
        elif any("institutional_guidance" in basis for basis in bases):
            relationship = "institutional_series"
        elif len(source_ids) > 1 and any("study_family" in basis for basis in bases):
            relationship = "same_study"
        elif len(source_ids) > 1:
            relationship = "overlapping_evidence_base"
        else:
            relationship = "independent_evidence_base"
        rationale = (
            "Independence is unresolved because the profile contains no usable sample, dataset, program, or study-family lineage."
            if relationship == "independence_uncertain"
            else "Publications are counted once for this shared evidence base."
            if len(source_ids) > 1
            else "The available lineage identifies one countable evidence base."
        )
        group_rows.append(
            {
                "evidence_base_group_id": group_id,
                "proposition_id": "",
                "source_ids": source_ids,
                "study_lineage_ids": [lineage_id_by_source[source_id] for source_id in source_ids],
                "relationship": relationship,
                "counted_as_independent": counted,
                "rationale": rationale,
                "overlap_signals": overlap_signals,
            }
        )
        assessments.append(
            {
                "assessment_id": f"independence-{_stable_hash([group_id, source_ids])[:16]}",
                "proposition_id": "",
                "source_ids": source_ids,
                "evidence_base_group_ids": [group_id],
                "status": relationship,
                "effective_evidence_base_count": 1 if counted else 0,
                "rationale": rationale,
                "overlap_signals": overlap_signals,
                "confidence": "unknown" if not counted else "moderate",
                "evidence": [],
            }
        )
    return {
        "study_lineages": sorted(strict_lineages, key=lambda row: row["source_ids"]),
        "independence_assessments": assessments,
        "evidence_base_groups": group_rows,
    }


def _reconcile_evidence_base_groups(rows: list[dict[str, Any]]) -> None:
    """Union publications only on explicit, study-level overlap signals."""

    parents = list(range(len(rows)))
    reasons_by_root: dict[int, set[str]] = defaultdict(set)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int, reasons: Sequence[str]) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            reasons_by_root[left_root].update(reasons)
            return
        keep, merge = sorted((left_root, right_root))
        parents[merge] = keep
        reasons_by_root[keep].update(reasons_by_root.pop(merge, set()))
        reasons_by_root[keep].update(reasons)

    def values(lineage: Mapping[str, Any], key: str) -> set[str]:
        return {_canonical_phrase(value) or str(value).casefold() for value in lineage.get(key, []) or [] if str(value)}

    generic_lineage_values = {
        "archive",
        "case study",
        "document analysis",
        "interview",
        "observation",
        "secondary data",
        "survey",
    }
    for left_index, right_index in combinations(range(len(rows)), 2):
        left, right = rows[left_index], rows[right_index]
        left_lineage = _as_mapping(left.get("study_lineage"))
        right_lineage = _as_mapping(right.get("study_lineage"))
        reasons: list[str] = []
        left_family = str(left.get("study_family_id") or "")
        right_family = str(right.get("study_family_id") or "")
        family_is_substantive = (
            left_family
            and not left_family.casefold().startswith(("doi:", "title:"))
            and left_family
            not in {
                str(left.get("source_id") or ""),
                str(right.get("source_id") or ""),
            }
        )
        if family_is_substantive and left_family == right_family:
            reasons.append(f"study_family:{left_family}")
        for key, label in (
            ("sample_ids", "shared_sample"),
            ("dataset_ids", "shared_dataset"),
            ("program_ids", "shared_program"),
        ):
            shared = values(left_lineage, key) & values(right_lineage, key)
            shared -= generic_lineage_values
            reasons.extend(f"{label}:{value}" for value in sorted(shared))
        left_group = str(left_lineage.get("evidence_base_group_id") or "")
        right_group = str(right_lineage.get("evidence_base_group_id") or "")
        if (
            left_group
            and left_group == right_group
            and bool(left_lineage.get("counted_as_independent"))
            and bool(right_lineage.get("counted_as_independent"))
        ):
            reasons.append(f"declared_group:{left_group}")
        left_role = str(left.get("source_role") or "").casefold()
        right_role = str(right.get("source_role") or "").casefold()
        if any(marker in left_role for marker in ("practitioner", "guidance", "institutional")) and any(
            marker in right_role for marker in ("practitioner", "guidance", "institutional")
        ):
            left_institution = _canonical_phrase(left_lineage.get("institution"))
            right_institution = _canonical_phrase(right_lineage.get("institution"))
            if left_institution and left_institution == right_institution:
                reasons.append(f"institutional_guidance:{left_institution}")
        if reasons:
            union(left_index, right_index, reasons)

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        members_by_root[find(index)].append(index)
    for root, member_indexes in sorted(members_by_root.items()):
        if len(member_indexes) == 1:
            continue
        reasons = sorted(reasons_by_root.get(find(root), set()))
        group_id = f"evidence-base-{_stable_hash(reasons or [rows[index]['source_id'] for index in member_indexes])[:16]}"
        for index in member_indexes:
            row = rows[index]
            row["evidence_base_group_id"] = group_id
            lineage = _as_mapping(row.get("study_lineage"))
            lineage.update(
                evidence_base_group_id=group_id,
                group_basis=";".join(reasons),
                independence_status=(
                    "institutional_series"
                    if any(reason.startswith("institutional_guidance:") for reason in reasons)
                    else "overlapping_evidence_base"
                ),
                counted_as_independent=True,
            )
            row["study_lineage"] = lineage
            for claim in row.get("claims", []) or []:
                claim["evidence_base_group_id"] = group_id
                claim["evidence_base_counted"] = True


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
        study_lineage = _normalized_study_lineage(
            raw,
            source_id=source_id,
            family_id=family_id,
            doi=doi,
        )
        for claim in claims:
            claim["evidence_base_group_id"] = study_lineage["evidence_base_group_id"]
            claim["evidence_base_counted"] = bool(study_lineage.get("counted_as_independent"))
            claim["independence_status"] = str(study_lineage.get("independence_status") or "")
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
                "evidence_base_group_id": study_lineage["evidence_base_group_id"],
                "evidence_base_counted": bool(study_lineage.get("counted_as_independent")),
                "study_lineage": study_lineage,
                "source_role": str(raw.get("source_role") or "analytical_source"),
                "research_questions": list(raw.get("research_questions") or []),
                "zotero_item_key": str(raw.get("zotero_item_key") or context.get("zotero_item_key") or ""),
                "note_status": status,
                "analytical": analytical,
                "limited": not analytical,
                "exclusion_reason": str(raw.get("exclusion_reason") or context.get("exclusion_reason") or ""),
                "semantic_topic_scores": topic_scores,
                "semantic_topic_labels": topic_labels,
                "dimensions": dimensions,
                "claims": claims,
                "evidence_anchors": claims,
                "quantitative_results": [
                    _as_mapping(row)
                    for row in raw.get("quantitative_results", []) or []
                    if isinstance(row, Mapping)
                ],
                "source_locators": [
                    _as_mapping(row)
                    for row in raw.get("source_locators", []) or []
                    if isinstance(row, Mapping)
                ],
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
    _reconcile_evidence_base_groups(normalized)
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


def map_topic_neighborhoods(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build unlimited navigation neighborhoods without analytical force.

    Neighborhoods expose broad topic, case, method, citation, and tag
    relationships for retrieval and the Obsidian graph. They never satisfy a
    cluster, debate, or gap threshold.
    """

    rows = _ensure_profiles(profiles)
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, label: str, profile: Mapping[str, Any], strength: str) -> None:
        identity = _canonical_phrase(label)
        if not identity or set(identity.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
            return
        key = (kind, identity)
        neighborhood = by_identity.setdefault(
            key,
            {
                "topic_neighborhood_id": f"neighborhood-{slugify(kind)}-{_stable_hash([kind, identity])[:12]}",
                "kind": kind,
                "semantic_identity": identity,
                "label": re.sub(r"\s+", " ", str(label)).strip(),
                "source_ids": [],
                "note_ids": [],
                "signals": [],
                "analytical_support": False,
            },
        )
        source_id = str(profile.get("source_id") or "")
        note_id = str(profile.get("note_id") or "")
        if source_id and source_id not in neighborhood["source_ids"]:
            neighborhood["source_ids"].append(source_id)
        if note_id and note_id not in neighborhood["note_ids"]:
            neighborhood["note_ids"].append(note_id)
        signal = {"source_id": source_id, "kind": kind, "strength": strength}
        if signal not in neighborhood["signals"]:
            neighborhood["signals"].append(signal)

    for profile in rows:
        for identity, score in profile.get("semantic_topic_scores", {}).items():
            add(
                "semantic",
                str(profile.get("semantic_topic_labels", {}).get(identity) or identity),
                profile,
                "strong" if float(score) >= 0.8 else "moderate",
            )
        for value in profile.get("dimensions", {}).get("case", []) or []:
            add("case", str(value), profile, "moderate")
        for value in profile.get("dimensions", {}).get("method", []) or []:
            add("method", str(value), profile, "moderate")
        for value in profile.get("normalized_tags", []) or []:
            add("tag", str(value), profile, "weak")

    profile_by_source = {str(row.get("source_id") or ""): row for row in rows}
    for relation in relations or []:
        relation_sources = [
            profile_by_source[str(source_id)]
            for source_id in relation.get("source_ids", []) or []
            if str(source_id) in profile_by_source
        ]
        if len(relation_sources) != 2:
            continue
        label = " / ".join(
            str(row.get("title") or row.get("source_id") or "") for row in relation_sources
        )
        for profile in relation_sources:
            add("citation_or_relation", label, profile, "strong")

    for neighborhood in by_identity.values():
        neighborhood["source_ids"] = sorted(set(neighborhood["source_ids"]))
        neighborhood["note_ids"] = sorted(set(neighborhood["note_ids"]))
        neighborhood["signals"] = sorted(
            neighborhood["signals"], key=lambda row: (row["source_id"], row["kind"], row["strength"])
        )
        neighborhood["source_count"] = len(neighborhood["source_ids"])
    return sorted(by_identity.values(), key=lambda row: row["topic_neighborhood_id"])


def _anchor_scope_values(anchor: Mapping[str, Any], key: str) -> list[str]:
    envelope = _as_mapping(anchor.get("support_envelope"))
    scope = _as_mapping(envelope.get("scope"))
    scoped = scope.get(key) or scope.get(f"{key}s") or []
    values = _flatten_values(scoped)
    if not values:
        values = list(_as_mapping(anchor.get("dimensions")).get(key, []) or [])
    return [str(value) for value in values if str(value).strip()]


def _anchor_is_synthesis_eligible(anchor: Mapping[str, Any]) -> bool:
    envelope = _as_mapping(anchor.get("support_envelope"))
    return bool(
        anchor.get("locator_complete")
        and str(envelope.get("coverage") or "unknown") == "full_text"
        and str(envelope.get("support_status") or anchor.get("support_status") or "support_unknown") == "supported"
        and (
            str(envelope.get("empirical_role") or "none") in EMPIRICAL_SUPPORT_ROLES
            or str(envelope.get("argument_role") or "none") in ARGUMENT_SUPPORT_ROLES
        )
    )


def _proposition_signature(anchor: Mapping[str, Any]) -> dict[str, Any]:
    topic, outcome, relationship = _claim_proposition_parts(anchor)
    if not relationship and topic and str(anchor.get("direction") or "not_reported") != "not_reported":
        # Compatibility rows sometimes state only that a topic has a reported
        # directional result. That is weak but still an asserted relationship,
        # unlike tags or source-level topic metadata alone.
        relationship = {"reported_relationship"}
    envelope = _as_mapping(anchor.get("support_envelope"))
    empirical_role = str(envelope.get("empirical_role") or "none")
    argument_role = str(envelope.get("argument_role") or "none")
    evidence_family = "empirical" if empirical_role != "none" else argument_role
    return {
        "topic": sorted(topic),
        "outcome": sorted(outcome),
        "relationship": sorted(relationship),
        "evidence_family": evidence_family,
    }


def _proposition_statement(anchors: Sequence[Mapping[str, Any]], signature: Mapping[str, Any]) -> str:
    texts = [str(row.get("text") or "").strip() for row in anchors if str(row.get("text") or "").strip()]
    if texts:
        return sorted(texts, key=lambda value: (len(value), value.casefold()))[0]
    terms = [
        *[str(value) for value in signature.get("topic", []) or []],
        *[str(value) for value in signature.get("relationship", []) or []],
        *[str(value) for value in signature.get("outcome", []) or []],
    ]
    return " ".join(dict.fromkeys(terms)).strip()


def build_literature_propositions(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Group located source anchors into comparable map-local propositions."""

    rows = _ensure_profiles(profiles)
    eligible = [
        anchor
        for profile in rows
        if profile.get("analytical")
        for anchor in profile.get("claims", []) or []
        if _anchor_is_synthesis_eligible(anchor)
    ]
    by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for anchor in eligible:
        signature = _proposition_signature(anchor)
        if not signature["relationship"] and not signature["outcome"]:
            # Shared subject vocabulary without an asserted relationship is a
            # topic neighborhood, not an analytical proposition.
            continue
        identity = _stable_hash(signature)
        by_signature[identity].append(dict(anchor))

    propositions: list[dict[str, Any]] = []
    for identity, anchors in sorted(by_signature.items()):
        sources = sorted({str(row.get("source_id") or "") for row in anchors})
        if len(sources) < 2:
            continue
        families = sorted({str(row.get("study_family_id") or row.get("source_id") or "") for row in anchors})
        evidence_bases = sorted(
            {
                evidence_base_id
                for row in anchors
                if (evidence_base_id := _anchor_evidence_base_id(row))
            }
        )
        signature = _proposition_signature(anchors[0])
        cells: list[dict[str, Any]] = []
        for source_id in sources:
            source_anchors = [row for row in anchors if str(row.get("source_id") or "") == source_id]
            evidence = [_evidence_ref(row) for row in source_anchors]
            cells.append(
                {
                    "source_id": source_id,
                    "study_family_id": str(source_anchors[0].get("study_family_id") or source_id),
                    "evidence_base_group_id": str(
                        source_anchors[0].get("evidence_base_group_id") or ""
                    ),
                    "counted_as_independent": bool(source_anchors[0].get("evidence_base_counted")),
                    "stance_or_finding": "; ".join(
                        dict.fromkeys(str(row.get("text") or "").strip() for row in source_anchors if row.get("text"))
                    ),
                    "evidence_type": sorted(
                        {
                            str(row.get("evidence_role") or "support_unknown")
                            for row in source_anchors
                        }
                    ),
                    "scope": {
                        key: sorted({value for row in source_anchors for value in _anchor_scope_values(row, key)})
                        for key in ("population", "case", "geography", "period", "outcome")
                    },
                    "boundary_conditions": sorted(
                        {str(row.get("boundary_condition") or "") for row in source_anchors if row.get("boundary_condition")}
                    ),
                    "direction_or_interpretation": sorted(
                        {str(row.get("direction") or "not_reported") for row in source_anchors}
                    ),
                    "uncertainty": sorted(
                        {str(row.get("uncertainty") or "") for row in source_anchors if row.get("uncertainty")}
                    ),
                    "evidence": evidence,
                }
            )
        proposition_id = f"proposition-{_stable_hash(signature)[:16]}"
        propositions.append(
            {
                "proposition_id": proposition_id,
                "semantic_identity": identity,
                "statement": _proposition_statement(anchors, signature),
                "question": f"What does the collection establish about {_proposition_statement(anchors, signature)}?",
                "proposition_type": str(signature.get("evidence_family") or "unknown"),
                "signature": signature,
                "source_ids": sources,
                "study_family_ids": families,
                "evidence_base_group_ids": evidence_bases,
                "independent_study_family_count": len(families),
                "effective_evidence_base_count": len(evidence_bases),
                "publication_count": len(sources),
                "cells": cells,
                "evidence": [_evidence_ref(row) for row in anchors],
                "comparability": {
                    "passed": True,
                    "basis": "shared_source_local_relationship_signature",
                    "independence_passed": len(evidence_bases) >= 2,
                    "outcomes": list(signature.get("outcome", []) or []),
                    "evidence_family": str(signature.get("evidence_family") or "unknown"),
                },
            }
        )
    return sorted(propositions, key=lambda row: row["proposition_id"])


def _profile_evidence_base_id(profile: Mapping[str, Any]) -> str:
    lineage = _as_mapping(profile.get("study_lineage"))
    counted = profile.get("evidence_base_counted")
    if counted is None:
        counted = lineage.get("counted_as_independent")
    if counted is not True:
        return ""
    return str(
        profile.get("evidence_base_group_id")
        or lineage.get("evidence_base_group_id")
        or ""
    )


def _anchor_evidence_base_id(anchor: Mapping[str, Any]) -> str:
    if anchor.get("evidence_base_counted") is not True:
        return ""
    return str(anchor.get("evidence_base_group_id") or "")


def _reference_evidence_base_id(reference: Mapping[str, Any]) -> str:
    if reference.get("counted_as_independent") is not True:
        return ""
    return str(reference.get("evidence_base_group_id") or "")


def _map_topic_clusters_legacy(
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


def _proposal_source_roles(proposal: Mapping[str, Any]) -> dict[str, str]:
    supplied = proposal.get("source_roles")
    result: dict[str, str] = {}
    if isinstance(supplied, Mapping):
        result = {str(source_id): str(role).casefold() for source_id, role in supplied.items()}
    elif isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes, bytearray)):
        for row in supplied:
            if not isinstance(row, Mapping):
                continue
            source_id = str(row.get("source_id") or "")
            role = str(row.get("role") or "").casefold()
            if source_id:
                result[source_id] = role
    return {source_id: role for source_id, role in result.items() if role in {"core", "context", "bridge"}}


def _qualify_provider_proposition(
    proposition: Mapping[str, Any],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Narrow unsupported causal wording to a traceable source-attribution claim."""

    result = dict(proposition)
    statement = str(result.get("statement") or "").strip()
    if not _has_unqualified_causal_language(statement):
        return result
    empirical_roles: set[str] = set()
    for reference in result.get("evidence", []) or []:
        source_id = str(reference.get("source_id") or "")
        anchor_id = str(
            reference.get("evidence_anchor_id")
            or reference.get("claim_id")
            or reference.get("finding_id")
            or ""
        )
        profile = profile_by_source.get(source_id, {})
        anchor = next(
            (
                row
                for row in profile.get("claims", []) or []
                if str(row.get("evidence_anchor_id") or row.get("claim_id") or "") == anchor_id
            ),
            {},
        )
        empirical_roles.add(
            str(_as_mapping(anchor.get("support_envelope")).get("empirical_role") or "none")
        )
    if empirical_roles & CAUSAL_SUPPORT_ROLES:
        return result
    proposition_type = str(result.get("proposition_type") or "").casefold()
    if proposition_type in {"practice_guidance", "practitioner", "guidance", "recommendation"}:
        prefix = "Practice-guidance sources advance the claim that "
    elif proposition_type in {"normative", "interpretive", "theoretical", "conceptual"}:
        prefix = "Normative or interpretive sources argue that "
    else:
        prefix = "The cited sources report or argue that "
    result["original_statement"] = statement
    result["statement"] = prefix + statement[:1].lower() + statement[1:]
    result["support_qualification"] = "causal_relationship_not_established"
    result["proposition_id"] = (
        "proposition-"
        + _stable_hash(
            {
                "statement": _canonical_phrase(result["statement"]),
                "source_ids": sorted(str(value) for value in result.get("source_ids", []) or []),
            }
        )[:16]
    )
    return result


def _comparability_tokens(value: Any) -> set[str]:
    """Return relationship-bearing terms rather than broad field vocabulary."""

    aliases = {
        "intens": "intensity",
        "succeed": "success",
        "successful": "success",
        "succeeded": "success",
        "succeeds": "success",
    }
    return {
        aliases.get(token, token)
        for token in _tokens(value)
        if token not in _GENERIC_COMPARABILITY_TERMS and not token.isdigit()
    }


def _anchor_supports_proposition_statement(anchor: Mapping[str, Any], statement: str) -> bool:
    """Require the cited anchor to address the proposition's actual relationship."""

    statement_tokens = _comparability_tokens(statement)
    # Topic/dimension/scope fields can be source-level retrieval metadata in
    # legacy profiles. They must never make an unrelated statistic support a
    # proposition; only the source-local claim text can do that.
    anchor_tokens = _comparability_tokens(anchor.get("text"))
    shared_tokens = statement_tokens & anchor_tokens
    if len(shared_tokens) < 2:
        return False
    statement_outcomes = statement_tokens & _OUTCOME_SIGNAL_TERMS
    if statement_outcomes and not (statement_outcomes & anchor_tokens):
        return False
    # Sharing only an outcome and generic relationship wording is not enough.
    # For example, "mediation duration is associated with success" must not
    # populate a row about conflict intensity merely because both findings say
    # "associated with mediation success". At least one proposition-specific
    # subject or mechanism must occur in the source-local anchor text.
    discriminating_tokens = (
        statement_tokens
        - _OUTCOME_SIGNAL_TERMS
        - _NON_DISCRIMINATING_RELATIONSHIP_TERMS
    )
    return bool(discriminating_tokens & anchor_tokens)


def _neutralize_directional_proposition(statement: str) -> str:
    neutral = re.sub(
        r"\b(?:fewer|greater|higher|lower|more|less|negative(?:ly)?|positive(?:ly)?)\b",
        "",
        statement,
        flags=re.I,
    )
    neutral = re.sub(r"\s+", " ", neutral)
    neutral = re.sub(r"\s+([,.;:])", r"\1", neutral)
    neutral = neutral.strip()
    return neutral[:1].upper() + neutral[1:] if neutral else neutral


def _precise_proposition_references(
    statement: str,
    source_ids: set[str],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one exact, jointly comparable anchor per participating source.

    Provider references are hypotheses, not authority. A broad table-summary
    anchor can contain every word in a compound proposition while obscuring
    which source-local finding actually supplies the comparison. This selector
    finds a proposition-specific subject shared by independent studies, then
    chooses the shortest eligible anchor that states that relationship for each
    source. Sources without the shared relationship fall out of the row.
    """

    statement_tokens = _comparability_tokens(statement)
    discriminating_tokens = (
        statement_tokens
        - _OUTCOME_SIGNAL_TERMS
        - _NON_DISCRIMINATING_RELATIONSHIP_TERMS
    )
    candidates_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for source_id in sorted(source_ids):
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        candidates = [
            anchor
            for anchor in profile.get("claims", []) or []
            if _anchor_is_synthesis_eligible(anchor)
            and _anchor_supports_proposition_statement(anchor, statement)
        ]
        if candidates:
            candidates_by_source[source_id] = candidates

    token_families: dict[str, set[str]] = defaultdict(set)
    for source_id, anchors in candidates_by_source.items():
        family_id = str(profile_by_source[source_id].get("study_family_id") or source_id)
        for anchor in anchors:
            for token in discriminating_tokens & _comparability_tokens(anchor.get("text")):
                token_families[token].add(family_id)
    shared_tokens = {
        token: families for token, families in token_families.items() if len(families) >= 2
    }
    if not shared_tokens:
        return [], {"passed": False, "reason": "no_shared_proposition_subject"}

    selections: list[tuple[tuple[int, int, str], str, list[Mapping[str, Any]]]] = []
    for token, families in shared_tokens.items():
        selected: list[Mapping[str, Any]] = []
        for source_id, anchors in candidates_by_source.items():
            matching = [
                anchor
                for anchor in anchors
                if token in _comparability_tokens(anchor.get("text"))
            ]
            if not matching:
                continue
            selected.append(
                min(
                    matching,
                    key=lambda anchor: (
                        len(_comparability_tokens(anchor.get("text"))),
                        -len(statement_tokens & _comparability_tokens(anchor.get("text"))),
                        str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""),
                    ),
                )
            )
        selected_families = {
            str(anchor.get("study_family_id") or anchor.get("source_id") or "")
            for anchor in selected
        }
        if len(selected_families) < 2:
            continue
        selections.append(
            (
                (
                    -len(selected_families),
                    sum(len(_comparability_tokens(anchor.get("text"))) for anchor in selected),
                    token,
                ),
                token,
                selected,
            )
        )
    if not selections:
        return [], {"passed": False, "reason": "no_independent_shared_proposition_subject"}

    _, shared_token, selected = min(selections, key=lambda row: row[0])
    references = [_evidence_ref(anchor) for anchor in selected]
    common_anchor_tokens = set.intersection(
        *(_comparability_tokens(anchor.get("text")) for anchor in selected)
    )
    unshared_statement_tokens = discriminating_tokens - common_anchor_tokens
    narrowed_statement = ""
    if unshared_statement_tokens:
        narrowed_statement = min(
            (str(anchor.get("text") or "").strip() for anchor in selected),
            key=lambda value: (len(_comparability_tokens(value)), len(value), value.casefold()),
        )
    return references, {
        "passed": True,
        "shared_proposition_subject": shared_token,
        "narrowed_statement": narrowed_statement,
        "selected_directions": sorted(
            {str(anchor.get("direction") or "not_reported") for anchor in selected}
        ),
        "unshared_provider_terms": sorted(unshared_statement_tokens),
    }


def _provider_comparable_cells(
    cells: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep only cells that actually address one shared bounded proposition.

    Provider proposals are useful hypotheses, but a model can accidentally put
    adjacent claims into one row.  Outcome-bearing empirical cells therefore
    need a shared non-generic outcome.  Conceptual rows without outcomes need
    at least two shared relationship-bearing terms.  This deliberately favors
    false negatives over false analytical clusters.
    """

    rows = [dict(cell) for cell in cells if isinstance(cell, Mapping)]
    if len(rows) < 2:
        return [], {"passed": False, "reason": "fewer_than_two_source_cells"}

    outcome_tokens: dict[str, set[str]] = {}
    for cell in rows:
        source_id = str(cell.get("source_id") or "")
        tokens = _comparability_tokens(_as_mapping(cell.get("scope")).get("outcome", []))
        if not tokens:
            tokens = _comparability_tokens(cell.get("stance_or_finding", "")) & _OUTCOME_SIGNAL_TERMS
        outcome_tokens[source_id] = tokens
    cells_with_outcomes = {
        source_id: tokens for source_id, tokens in outcome_tokens.items() if tokens
    }
    if cells_with_outcomes:
        token_families: dict[str, set[str]] = defaultdict(set)
        for cell in rows:
            source_id = str(cell.get("source_id") or "")
            family_id = str(cell.get("study_family_id") or source_id)
            for token in outcome_tokens.get(source_id, set()):
                token_families[token].add(family_id)
        shared = {
            token: families for token, families in token_families.items() if len(families) >= 2
        }
        if not shared:
            return [], {"passed": False, "reason": "no_shared_bounded_outcome"}
        strongest = max(len(families) for families in shared.values())
        admitted_terms = sorted(
            token for token, families in shared.items() if len(families) == strongest
        )
        selected = [
            cell
            for cell in rows
            if outcome_tokens.get(str(cell.get("source_id") or ""), set()) & set(admitted_terms)
        ]
        if len({str(cell.get("study_family_id") or cell.get("source_id")) for cell in selected}) < 2:
            return [], {"passed": False, "reason": "no_independent_shared_outcome"}
        return selected, {
            "passed": True,
            "basis": "shared_bounded_outcome",
            "shared_outcome_terms": admitted_terms,
        }

    stance_tokens = {
        str(cell.get("source_id") or ""): _comparability_tokens(cell.get("stance_or_finding", ""))
        for cell in rows
    }
    pair_families: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cell in rows:
        source_id = str(cell.get("source_id") or "")
        family_id = str(cell.get("study_family_id") or source_id)
        for token_pair in combinations(sorted(stance_tokens.get(source_id, set())), 2):
            pair_families[token_pair].add(family_id)
    shared_pairs = {
        pair: families for pair, families in pair_families.items() if len(families) >= 2
    }
    if not shared_pairs:
        return [], {"passed": False, "reason": "no_shared_conceptual_relationship"}
    strongest = max(len(families) for families in shared_pairs.values())
    admitted_pair = min(pair for pair, families in shared_pairs.items() if len(families) == strongest)
    selected = [
        cell
        for cell in rows
        if set(admitted_pair) <= stance_tokens.get(str(cell.get("source_id") or ""), set())
    ]
    return selected, {
        "passed": True,
        "basis": "shared_conceptual_relationship",
        "shared_relationship_terms": list(admitted_pair),
    }


def _proposal_propositions(
    proposal: Mapping[str, Any],
    propositions: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    source_ids = {str(value) for value in proposal.get("source_ids", []) or [] if str(value)}
    proposal_evidence = [
        dict(reference)
        for reference in proposal.get("supporting_evidence", []) or []
        if isinstance(reference, Mapping)
    ]
    invalid_proposal_evidence = bool(proposal_evidence) and any(
        (profile := profile_by_source.get(str(reference.get("source_id") or ""))) is None
        or not _reference_matches_profile(reference, profile)
        for reference in proposal_evidence
    )
    supplied = [row for row in proposal.get("propositions", []) or [] if isinstance(row, Mapping)]
    proposition_by_id = {str(row.get("proposition_id") or ""): row for row in propositions}
    matched: list[dict[str, Any]] = []
    for raw in supplied:
        known = proposition_by_id.get(str(raw.get("proposition_id") or ""))
        if known is not None:
            # Preserve deterministic/legacy proposition identities. Provider-authored
            # proposition text is narrowed below before it receives a map-local ID.
            matched.append(dict(known))
            continue
        statement = str(raw.get("statement") or raw.get("proposition") or "").strip()
        if not statement:
            continue
        references: list[dict[str, Any]] = []
        for reference in raw.get("evidence", raw.get("supporting_evidence", [])) or []:
            if not isinstance(reference, Mapping):
                continue
            profile = profile_by_source.get(str(reference.get("source_id") or ""))
            if profile is None or not _reference_matches_profile(reference, profile):
                continue
            claim_id = str(
                reference.get("evidence_anchor_id")
                or reference.get("claim_id")
                or reference.get("finding_id")
                or ""
            )
            claim = next(
                (
                    item
                    for item in profile.get("claims", []) or []
                    if str(item.get("evidence_anchor_id") or item.get("claim_id") or "") == claim_id
                ),
                None,
            )
            if (
                claim is None
                or not _anchor_is_synthesis_eligible(claim)
                or not _anchor_supports_proposition_statement(claim, statement)
            ):
                continue
            references.append(dict(reference))
        expanded_source_ids: list[str] = []
        declared_source_ids = {
            str(value)
            for value in raw.get("source_ids", []) or []
            if str(value) in profile_by_source
        }
        referenced_source_ids = {str(reference.get("source_id") or "") for reference in references}
        for source_id in sorted(declared_source_ids - referenced_source_ids):
            candidates = [
                anchor
                for anchor in profile_by_source[source_id].get("claims", []) or []
                if _anchor_is_synthesis_eligible(anchor)
                and _anchor_supports_proposition_statement(anchor, statement)
            ]
            candidates.sort(
                key=lambda anchor: (
                    -len(
                        _comparability_tokens(statement)
                        & _comparability_tokens(anchor.get("text"))
                    ),
                    str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""),
                )
            )
            if not candidates:
                continue
            references.extend(_evidence_ref(anchor) for anchor in candidates[:3])
            expanded_source_ids.append(source_id)
        provider_reference_ids = {
            str(reference.get("source_id") or ""): str(
                reference.get("evidence_anchor_id")
                or reference.get("claim_id")
                or reference.get("finding_id")
                or ""
            )
            for reference in references
        }
        references, precision = _precise_proposition_references(
            statement,
            declared_source_ids or set(provider_reference_ids),
            profile_by_source,
        )
        if not references:
            continue
        repaired_source_ids = {
            str(reference.get("source_id") or "")
            for reference in references
            if provider_reference_ids.get(str(reference.get("source_id") or ""))
            != str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
        }
        expanded_source_ids = sorted(set(expanded_source_ids) | repaired_source_ids)
        provider_statement = statement
        if str(precision.get("narrowed_statement") or "").strip():
            statement = str(precision["narrowed_statement"]).strip()
        selected_directions = {
            str(value) for value in precision.get("selected_directions", []) or []
        }
        if "null" in selected_directions or len(selected_directions - {"not_reported"}) > 1:
            statement = _neutralize_directional_proposition(statement)
        supplied_comparability = _as_mapping(raw.get("comparability"))
        if supplied_comparability.get("passed") is False:
            continue
        cells, deterministic_comparability = _provider_comparable_cells(
            _proposition_cells_from_references(references, profile_by_source)
        )
        admitted_source_ids = {str(cell.get("source_id") or "") for cell in cells}
        references = [
            reference
            for reference in references
            if str(reference.get("source_id") or "") in admitted_source_ids
        ]
        families = {
            str(profile_by_source[str(reference["source_id"])].get("study_family_id") or reference["source_id"])
            for reference in references
        }
        if len(families) < 2:
            continue
        signature = {
            "statement": _canonical_phrase(statement),
            "source_ids": sorted({str(reference["source_id"]) for reference in references}),
        }
        candidate = _qualify_provider_proposition(
            {
                "proposition_id": f"proposition-{_stable_hash(signature)[:16]}",
                "statement": statement,
                "question": str(raw.get("question") or ""),
                "proposition_type": str(raw.get("proposition_type") or "unknown"),
                "source_ids": signature["source_ids"],
                "study_family_ids": sorted(families),
                "independent_study_family_count": len(families),
                "evidence": references,
                "cells": cells,
                "comparability": {
                    **supplied_comparability,
                    **deterministic_comparability,
                    **precision,
                    "provider_assessment": supplied_comparability,
                    "provider_statement": provider_statement,
                    "deterministic_anchor_expansion_source_ids": expanded_source_ids,
                    # Provider-authored direction labels are not assumed to
                    # share an orientation. The synthesis prose must make an
                    # actual positive/negative disagreement explicit.
                    "direction_orientation_aligned": False,
                },
            },
            profile_by_source,
        )
        if candidate.get("original_statement") and provider_statement != statement:
            candidate["original_statement"] = provider_statement
        matched.append(candidate)
    if matched:
        return sorted({_stable_hash(row): row for row in matched}.values(), key=lambda row: row["proposition_id"])

    if supplied:
        # An explicit provider proposition that fails support or comparability
        # cannot be rescued as a broader deterministic topic grouping.
        return []

    if invalid_proposal_evidence:
        # A proposal with no valid proposition row cannot be rescued by broad
        # source membership when its representative evidence is invented or
        # untraceable. Valid proposition rows above remain authoritative and
        # may survive an unrelated bad representative reference.
        return []

    # Legacy/custom reasoners may still submit only source IDs. They can use a
    # single deterministic proposition already proved from those sources. If
    # the source set spans multiple propositions, the proposal is only a broad
    # topic bin and must not fuse them into one analytical cluster.
    for proposition in propositions:
        participants = set(str(value) for value in proposition.get("source_ids", []) or [])
        if len(participants & source_ids) >= 2:
            matched.append(dict(proposition))
    if len(matched) != 1:
        return []
    return matched


def _proposition_cells_from_references(
    references: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    anchors_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for reference in references:
        source_id = str(reference.get("source_id") or "")
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        anchor_id = str(
            reference.get("evidence_anchor_id")
            or reference.get("claim_id")
            or reference.get("finding_id")
            or ""
        )
        anchor = next(
            (
                item
                for item in profile.get("claims", []) or []
                if str(item.get("evidence_anchor_id") or item.get("claim_id") or "") == anchor_id
                and _anchor_is_synthesis_eligible(item)
            ),
            None,
        )
        if anchor is not None:
            anchors_by_source[source_id].append(anchor)

    cells: list[dict[str, Any]] = []
    for source_id, anchors in sorted(anchors_by_source.items()):
        cells.append(
            {
                "source_id": source_id,
                "study_family_id": str(anchors[0].get("study_family_id") or source_id),
                "stance_or_finding": "; ".join(
                    dict.fromkeys(
                        str(anchor.get("text") or "").strip()
                        for anchor in anchors
                        if str(anchor.get("text") or "").strip()
                    )
                ),
                "evidence_type": sorted(
                    {str(anchor.get("evidence_role") or "support_unknown") for anchor in anchors}
                ),
                "scope": {
                    key: sorted(
                        {
                            value
                            for anchor in anchors
                            for value in _anchor_scope_values(anchor, key)
                        }
                    )
                    for key in ("population", "case", "geography", "period", "outcome")
                },
                "boundary_conditions": sorted(
                    {
                        str(anchor.get("boundary_condition") or "")
                        for anchor in anchors
                        if str(anchor.get("boundary_condition") or "").strip()
                    }
                ),
                "direction_or_interpretation": sorted(
                    {str(anchor.get("direction") or "not_reported") for anchor in anchors}
                ),
                "uncertainty": sorted(
                    {
                        str(anchor.get("uncertainty") or "")
                        for anchor in anchors
                        if str(anchor.get("uncertainty") or "").strip()
                    }
                ),
                "evidence": [_evidence_ref(anchor) for anchor in anchors],
            }
        )
    return cells


def map_overlapping_clusters(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
    propositions: Sequence[Mapping[str, Any]] | None = None,
    topic_neighborhoods: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Admit analytical clusters only through comparable proposition rows."""

    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row.get("analytical")]
    profile_by_source = {str(row["source_id"]): row for row in analytical}
    proposition_rows = [dict(row) for row in (propositions or build_literature_propositions(rows))]
    min_backed = max(3, int(_policy_value(policy, "source_backed_threshold", 3)))
    min_emerging = 2
    max_memberships = max(1, min(3, int(_policy_value(policy, "max_memberships", 3))))
    auto_promote = bool(_policy_value(policy, "auto_promote_clusters", True))
    rejected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    proposal_rows = [dict(row) for row in proposals or [] if isinstance(row, Mapping)]
    if not proposal_rows:
        proposal_rows = [
            {
                "proposal_id": f"proposal-{row['proposition_id']}",
                "label": row.get("statement") or row["proposition_id"],
                "semantic_identity": " ".join(
                    row.get("signature", {}).get("topic", [])
                    or row.get("signature", {}).get("outcome", [])
                    or row.get("signature", {}).get("relationship", [])
                ),
                "shared_question": row.get("question") or "",
                "coherence_rationale": "Independent sources address the same located proposition.",
                "source_ids": row.get("source_ids", []),
                "source_roles": {source_id: "core" for source_id in row.get("source_ids", []) or []},
                "propositions": [row],
                "formation_route": "deterministic_proposition",
            }
            for row in proposition_rows
        ]

    for proposal in proposal_rows:
        admitted = _proposal_propositions(proposal, proposition_rows, profile_by_source)
        roles = _proposal_source_roles(proposal)
        proposal_sources = {
            str(value) for value in proposal.get("source_ids", []) or [] if str(value) in profile_by_source
        }
        proposition_sources = {
            str(value)
            for proposition in admitted
            for value in proposition.get("source_ids", []) or []
            if str(value) in profile_by_source
        }
        core_sources = {
            source_id
            for source_id in proposition_sources
            if roles.get(source_id, "core") == "core"
        }

        # Practitioner guidance cannot become core evidence for an empirical
        # effectiveness proposition.
        empirical_effectiveness = any(
            str(row.get("proposition_type") or "") == "empirical"
            for row in admitted
        )
        if empirical_effectiveness:
            for source_id in list(core_sources):
                source_role = str(profile_by_source[source_id].get("source_role") or "").casefold()
                anchor_roles = {
                    str(_as_mapping(anchor.get("support_envelope")).get("argument_role") or "none")
                    for anchor in profile_by_source[source_id].get("claims", []) or []
                }
                if "practitioner" in source_role or anchor_roles == {"practitioner_guidance"}:
                    core_sources.remove(source_id)
                    roles[source_id] = "context"

        core_families = {
            str(profile_by_source[source_id].get("study_family_id") or source_id)
            for source_id in core_sources
        }
        core_evidence_bases = {
            evidence_base_id
            for source_id in core_sources
            if (evidence_base_id := _profile_evidence_base_id(profile_by_source[source_id]))
        }
        valid_multi_source = [
            row
            for row in admitted
            if len(
                {
                    evidence_base_id
                    for source_id in row.get("source_ids", []) or []
                    if str(source_id) in core_sources
                    and (evidence_base_id := _profile_evidence_base_id(profile_by_source[str(source_id)]))
                }
            )
            >= min_emerging
            and bool(_as_mapping(row.get("comparability")).get("passed", True))
        ]
        if not valid_multi_source or len(core_evidence_bases) < min_emerging:
            rejected.append(
                {
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                    "semantic_identity": str(proposal.get("semantic_identity") or proposal.get("label") or ""),
                    "source_ids": sorted(proposal_sources),
                    "action": "reject",
                    "reason": "no_valid_multi_source_proposition_row",
                }
            )
            continue
        context_sources = {
            source_id for source_id in proposal_sources if roles.get(source_id) == "context"
        }
        bridge_sources = {
            source_id for source_id in proposal_sources if roles.get(source_id) == "bridge"
        }
        all_sources = sorted(core_sources | context_sources | bridge_sources)
        semantic_identity = _canonical_phrase(
            proposal.get("semantic_identity")
            or [row.get("proposition_id") for row in valid_multi_source]
        ) or _stable_hash([row["proposition_id"] for row in valid_multi_source])
        candidates.append(
            {
                "proposal": proposal,
                "semantic_identity": semantic_identity,
                "propositions": valid_multi_source,
                "core_source_ids": sorted(core_sources),
                "context_source_ids": sorted(context_sources),
                "bridge_source_ids": sorted(bridge_sources),
                "source_ids": all_sources,
                "core_family_count": len(core_families),
                "core_evidence_base_count": len(core_evidence_bases),
            }
        )

    # Cap analytical memberships using core-family strength and proposition
    # count. Topic-neighborhood membership remains unlimited.
    selected_by_source: dict[str, set[str]] = defaultdict(set)
    for source_id in profile_by_source:
        available = [row for row in candidates if source_id in row["source_ids"]]
        available.sort(
            key=lambda row: (
                -row["core_evidence_base_count"],
                -row["core_family_count"],
                -len(row["propositions"]),
                row["semantic_identity"],
            )
        )
        selected_by_source[source_id] = {row["semantic_identity"] for row in available[:max_memberships]}

    relation_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for relation in relations or []:
        for source_id in relation.get("source_ids", []) or []:
            relation_ids_by_source[str(source_id)].append(str(relation.get("relation_id") or ""))
    neighborhood_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for neighborhood in topic_neighborhoods or []:
        for source_id in neighborhood.get("source_ids", []) or []:
            neighborhood_ids_by_source[str(source_id)].append(str(neighborhood.get("topic_neighborhood_id") or ""))

    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        included = [
            source_id
            for source_id in candidate["source_ids"]
            if candidate["semantic_identity"] in selected_by_source[source_id]
        ]
        core_sources = [source_id for source_id in candidate["core_source_ids"] if source_id in included]
        core_families = sorted(
            {str(profile_by_source[source_id].get("study_family_id") or source_id) for source_id in core_sources}
        )
        core_evidence_bases = sorted(
            {
                evidence_base_id
                for source_id in core_sources
                if (evidence_base_id := _profile_evidence_base_id(profile_by_source[source_id]))
            }
        )
        if len(core_evidence_bases) < min_emerging:
            rejected.append(
                {
                    "proposal_id": str(candidate["proposal"].get("proposal_id") or ""),
                    "semantic_identity": candidate["semantic_identity"],
                    "source_ids": included,
                    "action": "reject",
                    "reason": "overlap_policy_removed_proposition_support",
                }
            )
            continue
        cluster_propositions: list[dict[str, Any]] = []
        proposition_family_counts: list[int] = []
        for proposition in candidate["propositions"]:
            proposition_sources = {
                str(value) for value in proposition.get("source_ids", []) or [] if str(value) in core_sources
            }
            proposition_families = sorted(
                {
                    str(profile_by_source[source_id].get("study_family_id") or source_id)
                    for source_id in proposition_sources
                }
            )
            proposition_evidence_bases = sorted(
                {
                    evidence_base_id
                    for source_id in proposition_sources
                    if (evidence_base_id := _profile_evidence_base_id(profile_by_source[source_id]))
                }
            )
            if len(proposition_evidence_bases) < min_emerging:
                continue
            projected = dict(proposition)
            projected["source_ids"] = sorted(proposition_sources)
            projected["study_family_ids"] = proposition_families
            projected["independent_study_family_count"] = len(proposition_families)
            projected["evidence_base_group_ids"] = proposition_evidence_bases
            projected["effective_evidence_base_count"] = len(proposition_evidence_bases)
            projected["cells"] = [
                dict(cell)
                for cell in proposition.get("cells", []) or []
                if isinstance(cell, Mapping) and str(cell.get("source_id") or "") in proposition_sources
            ]
            projected["evidence"] = [
                dict(reference)
                for reference in proposition.get("evidence", []) or []
                if isinstance(reference, Mapping)
                and str(reference.get("source_id") or "") in proposition_sources
            ]
            cluster_propositions.append(projected)
            proposition_family_counts.append(len(proposition_evidence_bases))
        if not cluster_propositions:
            rejected.append(
                {
                    "proposal_id": str(candidate["proposal"].get("proposal_id") or ""),
                    "semantic_identity": candidate["semantic_identity"],
                    "source_ids": included,
                    "action": "reject",
                    "reason": "overlap_policy_removed_proposition_support",
                }
            )
            continue
        proposition_ids = sorted(row["proposition_id"] for row in cluster_propositions)
        cluster_identity = {
            "semantic_identity": candidate["semantic_identity"],
            "proposition_ids": proposition_ids,
        }
        cluster_id = f"cluster-{slugify(candidate['semantic_identity'])}-{_stable_hash(cluster_identity)[:10]}"
        strongest_proposition_family_count = max(proposition_family_counts)
        qualification = (
            "source_backed_cluster"
            if strongest_proposition_family_count >= min_backed
            else "emerging_cluster"
        )
        role_by_source = {
            source_id: (
                "core" if source_id in core_sources
                else "bridge" if source_id in candidate["bridge_source_ids"]
                else "context"
            )
            for source_id in included
        }
        revision_hash = _stable_hash(
            {
                "cluster_id": cluster_id,
                "proposition_ids": proposition_ids,
                "source_roles": role_by_source,
                "core_study_families": core_families,
                "core_evidence_bases": core_evidence_bases,
            }
        )
        proposal = candidate["proposal"]
        label = str(proposal.get("label") or cluster_propositions[0].get("statement") or "Analytical Cluster")
        clusters.append(
            {
                "cluster_id": cluster_id,
                "semantic_identity": candidate["semantic_identity"],
                "label": label,
                "shared_question": str(proposal.get("shared_question") or cluster_propositions[0].get("question") or ""),
                "coherence_rationale": str(
                    proposal.get("coherence_rationale")
                    or "Independent core studies address at least one comparable proposition."
                ),
                "proposal_id": str(proposal.get("proposal_id") or ""),
                "proposal_supporting_evidence": [
                    reference for row in cluster_propositions for reference in row.get("evidence", []) or []
                ],
                "formation_route": str(proposal.get("formation_route") or "reasoner_proposition_proposal"),
                "proposition_ids": proposition_ids,
                "propositions": cluster_propositions,
                "source_roles": [
                    {"source_id": source_id, "role": role_by_source[source_id]} for source_id in sorted(role_by_source)
                ],
                "core_source_ids": sorted(core_sources),
                "context_source_ids": sorted(source_id for source_id in included if role_by_source[source_id] == "context"),
                "bridge_source_ids": sorted(source_id for source_id in included if role_by_source[source_id] == "bridge"),
                "topic_neighborhood_ids": sorted(
                    {value for source_id in included for value in neighborhood_ids_by_source[source_id] if value}
                ),
                "shared_concepts": [],
                "shared_normalized_tags": [],
                "shared_methods": [],
                "note_ids": sorted(str(profile_by_source[source_id]["note_id"]) for source_id in included),
                "source_ids": sorted(included),
                "study_family_ids": sorted(
                    {str(profile_by_source[source_id].get("study_family_id") or source_id) for source_id in included}
                ),
                "core_study_family_ids": core_families,
                "independent_study_family_count": len(core_families),
                "core_evidence_base_group_ids": core_evidence_bases,
                "effective_evidence_base_count": len(core_evidence_bases),
                "qualifying_proposition_family_count": strongest_proposition_family_count,
                "source_count": len(included),
                "status": qualification if auto_promote else "cluster_candidate",
                "qualification_status": qualification,
                "promoted": auto_promote,
                "automation_status": "promoted" if auto_promote else "candidate",
                "source_backed": strongest_proposition_family_count >= min_backed,
                "revision_hash": revision_hash,
                "relation_ids": sorted(
                    {value for source_id in included for value in relation_ids_by_source[source_id] if value}
                ),
                "representative_sources": [
                    {
                        "note_id": profile_by_source[source_id]["note_id"],
                        "source_id": source_id,
                        "study_family_id": profile_by_source[source_id]["study_family_id"],
                        "title": profile_by_source[source_id]["title"],
                        "note_path": profile_by_source[source_id]["note_path"],
                        "note_hash": profile_by_source[source_id]["note_hash"],
                        "cluster_role": role_by_source[source_id],
                    }
                    for source_id in sorted(included)
                ],
            }
        )

    clustered_sources = {source_id for cluster in clusters for source_id in cluster["source_ids"]}
    unclustered = []
    for profile in rows:
        source_id = str(profile["source_id"])
        if source_id in clustered_sources:
            continue
        if profile.get("limited"):
            reason = profile.get("exclusion_reason") or "limited_profile_excluded_from_analytical_clustering"
        elif not any(source_id in row.get("source_ids", []) for row in proposition_rows):
            reason = "no_comparable_multi_source_proposition"
        elif not any(
            source_id in row.get("source_ids", [])
            and int(row.get("effective_evidence_base_count", 0) or 0) >= min_emerging
            for row in proposition_rows
        ):
            reason = "no_comparable_independent_evidence_base_proposition"
        else:
            reason = "analytical_membership_limit_or_proposal_rejection"
        unclustered.append({"source_id": source_id, "note_id": profile["note_id"], "reason": reason})
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
    anchor_id = str(claim.get("evidence_anchor_id") or claim.get("claim_id") or "")
    return {
        "evidence_anchor_id": anchor_id,
        "claim_id": anchor_id,
        "source_id": str(claim.get("source_id", "")),
        "study_family_id": str(claim.get("study_family_id", "")),
        "evidence_base_group_id": str(claim.get("evidence_base_group_id") or ""),
        "counted_as_independent": bool(claim.get("evidence_base_counted")),
        "independence_status": str(claim.get("independence_status") or "independence_uncertain"),
        "locator": str(claim.get("locator", "")),
        "source_locator": dict(_as_mapping(claim.get("source_locator")) or _source_locator(claim.get("locator"))),
        "support_status": str(claim.get("support_status") or "support_unknown"),
        "empirical_role": str(_as_mapping(claim.get("support_envelope")).get("empirical_role") or "none"),
        "argument_role": str(_as_mapping(claim.get("support_envelope")).get("argument_role") or "none"),
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
        claim_id = str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
        claim = next(
            row
            for row in profile.get("claims", []) or []
            if str(row.get("evidence_anchor_id") or row.get("claim_id") or "") == claim_id
        )
        resolved.append(_evidence_ref(claim))
    return sorted(
        {_stable_hash(row): row for row in resolved}.values(),
        key=lambda row: (row["source_id"], row["evidence_anchor_id"], row["locator"]),
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


def _quantitative_text_errors(item: Mapping[str, Any]) -> list[str]:
    """Catch arithmetic mismatches before numerical prose reaches Markdown."""

    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "assertion",
            "finding",
            "text",
            "summary",
            "position",
            "agreement",
            "contradiction",
            "technical_result",
            "technical_detail",
            "technical_context",
            "statistics",
            "plain_english_meaning",
            "plain_english",
        )
    )
    errors: list[str] = []
    percentages = [float(value) for value in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", text)]
    point_changes = [
        float(value)
        for value in re.findall(
            r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:percentage\s*points?|pp)\b",
            text,
            flags=re.I,
        )
    ]
    if len(percentages) >= 2 and point_changes:
        percentage_differences = {
            abs(right - left)
            for index, left in enumerate(percentages)
            for right in percentages[index + 1 :]
        }
        if any(
            all(abs(value - expected) > 0.11 for expected in percentage_differences)
            for value in point_changes
        ):
            errors.append("percentage_point_arithmetic_mismatch")
    decimal_effects = {
        float(value)
        for value in re.findall(r"(?<![\d.])([+-]0\.\d{2,})(?!\d)", text)
    }
    decimal_effects.update(
        float(value)
        for value in re.findall(
            r"\bmarginal\s+effects?\s*(?:is|was|=|of|:)?\s*([+-]?0\.\d{2,})(?!\d)",
            text,
            flags=re.I,
        )
    )
    if decimal_effects and point_changes and not any(
        abs(abs(effect) * 100 - points) <= 0.11
        for effect in decimal_effects
        for points in point_changes
    ):
        errors.append("decimal_effect_to_percentage_point_mismatch")
    return sorted(set(errors))


def _has_numerical_claim(text: Any) -> bool:
    return bool(
        re.search(
            r"(?:"
            r"(?<!\w)[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:%|percentage\s*points?|pp\b|odds?\s*ratios?|coefficients?|CI\b)"
            r"|\b(?:odds?\s*ratios?|coefficients?|marginal\s+effects?|treatment\s+effects?|effect\s+sizes?|estimates?)"
            r"\b(?:\s+\w+){0,3}\s*(?:=|was|were|of|:)?\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
            r"|\bp\s*[<=>]\s*(?:\d+(?:\.\d+)?|\.\d+)"
            r"|\b[nN]\s*=\s*\d+"
            r"|\b\d+\s+(?:cases?|observations?|respondents?|participants?)\b"
            r"|\b\d+\s+(?:of|out\s+of)\s+\d+\b"
            r")",
            str(text or ""),
            flags=re.I,
        )
    )


def _quantitative_item_errors(item: Mapping[str, Any], *, require_comparable: bool = True) -> list[str]:
    errors = list(_quantitative_text_errors(item))
    quantitative_text = " ".join(
        str(item.get(key) or "")
        for key in (
            "assertion",
            "finding",
            "text",
            "summary",
            "position",
            "agreement",
            "contradiction",
            "technical_result",
            "technical_detail",
            "technical_context",
            "statistics",
        )
    )
    has_numerical_claim = _has_numerical_claim(quantitative_text)
    comparisons = [
        _as_mapping(row)
        for row in item.get("quantitative_comparisons", []) or []
        if isinstance(row, Mapping)
    ]
    for comparison in comparisons if require_comparable else []:
        for field, error in (
            ("estimands_comparable", "quantitative_estimands_not_comparable"),
            ("outcomes_comparable", "quantitative_outcomes_not_comparable"),
            ("populations_comparable", "quantitative_populations_not_comparable"),
            ("arithmetic_reproducible", "quantitative_arithmetic_not_reproducible"),
        ):
            if comparison.get(field) is not True:
                errors.append(error)
    results = [
        _as_mapping(row)
        for row in item.get("quantitative_results", []) or []
        if isinstance(row, Mapping)
    ]
    if require_comparable and has_numerical_claim and not results and not comparisons:
        errors.append("quantitative_claim_missing_typed_results")
    if require_comparable and has_numerical_claim and len(results) < 2 and not comparisons:
        errors.append("quantitative_comparison_requires_two_typed_results")
    if require_comparable and len(results) >= 2:
        for field, error in (
            ("estimand_type", "quantitative_estimands_not_comparable"),
            ("outcome_definition", "quantitative_outcomes_not_comparable"),
            ("population", "quantitative_populations_not_comparable"),
        ):
            values = {str(row.get(field) or "").strip().casefold() for row in results}
            if "" in values or len(values) != 1:
                errors.append(error)
    return sorted(set(errors))


def _evidence_quantitative_results(
    evidence: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for reference in evidence:
        source_id = str(reference.get("source_id") or "")
        anchor_id = str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
        profile = profile_by_source.get(source_id, {})
        anchor = next(
            (
                row
                for row in profile.get("claims", []) or []
                if str(row.get("evidence_anchor_id") or row.get("claim_id") or "") == anchor_id
            ),
            {},
        )
        for result in anchor.get("quantitative_results", []) or []:
            if not isinstance(result, Mapping):
                continue
            result_row = _as_mapping(result)
            result_row.setdefault("source_id", source_id)
            result_row.setdefault("evidence_anchor_id", anchor_id)
            results.append(result_row)
    return sorted(
        {_stable_hash(row): row for row in results}.values(),
        key=lambda row: (str(row.get("source_id") or ""), str(row.get("quantitative_result_id") or "")),
    )


def _quantitative_comparison_records(
    syntheses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cluster_id, synthesis in sorted(syntheses.items()):
        persisted = [
            _as_mapping(row)
            for row in synthesis.get("quantitative_comparisons", []) or []
            if isinstance(row, Mapping)
        ]
        if persisted:
            rows.extend(persisted)
            continue
        for section in CLUSTER_SYNTHESIS_SECTIONS:
            for item in synthesis.get(section, []) or []:
                item_values = _as_mapping(item)
                errors = _quantitative_item_errors(item_values)
                if not errors and not any(
                    item_values.get(key)
                    for key in ("statistics", "technical_context", "quantitative_results", "quantitative_comparisons")
                ):
                    continue
                comparisons = [
                    _as_mapping(row)
                    for row in item_values.get("quantitative_comparisons", []) or []
                    if isinstance(row, Mapping)
                ]
                results = [
                    _as_mapping(row)
                    for row in item_values.get("quantitative_results", []) or []
                    if isinstance(row, Mapping)
                ]
                proposition_ids = [str(value) for value in item_values.get("proposition_ids", []) or [] if str(value)]
                source_ids = sorted(
                    {
                        str(reference.get("source_id") or "")
                        for reference in item_values.get("evidence", []) or []
                        if reference.get("source_id")
                    }
                )
                rows.append(
                    {
                        "comparison_id": f"quantitative-comparison-{_stable_hash([cluster_id, section, item])[:16]}",
                        "proposition_id": proposition_ids[0] if len(proposition_ids) == 1 else "",
                        "source_ids": source_ids,
                        "quantitative_result_ids": sorted(
                            str(row.get("quantitative_result_id") or "") for row in results if row.get("quantitative_result_id")
                        ),
                        "status": "rejected" if errors else ("valid" if comparisons or len(results) >= 2 else "qualified"),
                        "estimands_comparable": not any("estimand" in error for error in errors),
                        "outcomes_comparable": not any("outcome" in error for error in errors),
                        "populations_comparable": not any("population" in error for error in errors),
                        "arithmetic_reproducible": not any("arithmetic" in error or "percentage_point" in error for error in errors),
                        "reason": ";".join(errors) if errors else "deterministic_quantitative_checks_passed",
                        "qualifications": errors,
                    }
                )
    return rows


def _cluster_anchor_propositions(cluster: Mapping[str, Any]) -> dict[tuple[str, str], list[str]]:
    by_anchor: dict[tuple[str, str], list[str]] = defaultdict(list)
    for proposition in cluster.get("propositions", []) or []:
        proposition_id = str(proposition.get("proposition_id") or "")
        for reference in proposition.get("evidence", []) or []:
            key = (
                str(reference.get("source_id") or ""),
                str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
            )
            if proposition_id and proposition_id not in by_anchor[key]:
                by_anchor[key].append(proposition_id)
    return by_anchor


def _fallback_source_contributions(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep each study's important cluster-relevant finding visible without calling it agreement."""

    profile_by_source = {str(row.get("source_id") or ""): row for row in profiles}
    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping)
    }
    proposition_ids_by_anchor = _cluster_anchor_propositions(cluster)
    cluster_terms = _tokens(
        [
            cluster.get("label"),
            cluster.get("semantic_identity"),
            cluster.get("shared_question"),
            *[row.get("statement") for row in cluster.get("propositions", []) or []],
        ]
    )
    contributions: list[dict[str, Any]] = []
    for source_id in cluster.get("source_ids", []) or []:
        source_id = str(source_id)
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        role = role_by_source.get(source_id, "context")
        anchors = [
            row
            for row in profile.get("claims", []) or []
            if _anchor_is_synthesis_eligible(row)
        ]
        anchors.sort(
            key=lambda row: (
                not bool(
                    proposition_ids_by_anchor.get(
                        (source_id, str(row.get("evidence_anchor_id") or row.get("claim_id") or ""))
                    )
                ),
                -len(cluster_terms & _tokens([row.get("text"), row.get("topic"), row.get("dimensions")])),
                str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
            )
        )
        selected = anchors[:3] if role == "core" else anchors[:1]
        for anchor in selected:
            anchor_id = str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or "")
            proposition_ids = proposition_ids_by_anchor.get((source_id, anchor_id), [])
            if role == "context":
                comparison_status = "context_only"
            elif role == "bridge":
                comparison_status = "context_only"
            else:
                comparison_status = "single_source"
            contribution_kind = (
                "direct_proposition_finding"
                if proposition_ids
                else "bridge_evidence"
                if role == "bridge"
                else "conceptual_context"
                if role == "context"
                else "unique_cluster_relevant_finding"
            )
            contribution_id = f"contribution-{_stable_hash([cluster.get('cluster_id'), source_id, anchor_id])[:16]}"
            contributions.append(
                {
                    "contribution_id": contribution_id,
                    "source_id": source_id,
                    "cluster_role": role,
                    "contribution_kind": contribution_kind,
                    "finding": str(anchor.get("text") or ""),
                    "plain_english_meaning": str(anchor.get("plain_english_meaning") or ""),
                    "technical_result": "; ".join(
                        value
                        for value in (
                            str(anchor.get("magnitude") or ""),
                            str(anchor.get("comparison") or ""),
                            str(anchor.get("uncertainty") or ""),
                        )
                        if value
                    ),
                    "comparison_status": comparison_status,
                    "related_proposition_ids": proposition_ids,
                    "relation_to_cluster_question": (
                        "Direct evidence for an admitted proposition."
                        if proposition_ids
                        else "A cluster-relevant source finding retained without a cross-source comparison."
                    ),
                    "evidence": [_evidence_ref(anchor)],
                }
            )
    return contributions


def _cluster_synthesis_quality_errors(
    synthesis: Mapping[str, Any],
    cluster: Mapping[str, Any],
) -> list[str]:
    """Require a real verdict with complete proposition and core-source coverage."""

    errors: list[str] = []
    verdict = _human_projection_text(synthesis.get("synthesis") or "")
    verdict_words = re.findall(r"\b[\w'-]+\b", verdict)
    verdict_sentences = [
        value.strip()
        for value in re.split(r"(?<=[.!?])\s+", verdict)
        if value.strip()
    ]
    if not verdict:
        errors.append("missing_substantive_verdict")
    elif len(verdict_words) < MIN_CLUSTER_VERDICT_WORDS or len(verdict_sentences) < 2:
        errors.append("verdict_too_thin")

    if not synthesis.get("central_findings"):
        errors.append("missing_central_findings")

    admitted_proposition_ids = {
        str(value)
        for value in (
            cluster.get("proposition_ids")
            or [row.get("proposition_id") for row in cluster.get("propositions", []) or []]
        )
        if str(value)
    }
    covered_proposition_ids = {
        str(value)
        for row in synthesis.get("synthesis_assertions", []) or []
        for value in row.get("proposition_ids", []) or []
        if str(value)
    }
    for proposition_id in sorted(admitted_proposition_ids - covered_proposition_ids):
        errors.append(f"uncovered_admitted_proposition:{proposition_id}")

    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    core_source_ids = {
        source_id for source_id, role in role_by_source.items() if role == "core"
    }
    if not core_source_ids:
        core_source_ids = {
            str(reference.get("source_id") or "")
            for proposition in cluster.get("propositions", []) or []
            for reference in proposition.get("evidence", []) or []
            if reference.get("source_id")
        }
    covered_source_ids = {
        str(reference.get("source_id") or "")
        for reference in synthesis.get("supporting_evidence", []) or []
        if reference.get("source_id")
    }
    for source_id in sorted(core_source_ids - covered_source_ids):
        errors.append(f"uncovered_core_source:{source_id}")

    contribution_source_ids = {
        str(row.get("source_id") or "")
        for row in synthesis.get("source_contributions", []) or []
        if row.get("source_id")
    }
    for source_id in sorted(core_source_ids - contribution_source_ids):
        errors.append(f"missing_core_source_contribution:{source_id}")

    if str(synthesis.get("debate_state") or "") not in DEBATE_STATES:
        errors.append("missing_relationship_assessment")

    limits_are_explicit = bool(
        synthesis.get("boundaries")
        or synthesis.get("boundary_conditions")
        or synthesis.get("methodological_fault_lines")
        or re.search(
            r"\b(?:limit(?:ation|ations|ed)?|uncertain|cannot|can't|does not|do not|"
            r"no causal|associational|descriptive|unsubstantiated|unsupported|scope)\b",
            verdict,
            flags=re.I,
        )
    )
    if not limits_are_explicit:
        errors.append("missing_evidentiary_limits")
    return errors


def validate_cluster_synthesis(
    value: Any,
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Admit only proposition-linked assertions supported by located anchors."""
    raw = _as_mapping(value) if value else {}
    cluster_id = str(cluster.get("cluster_id") or "")
    if raw.get("cluster_id") and str(raw.get("cluster_id")) != cluster_id:
        raw = {}
    profile_by_source = {str(row["source_id"]): row for row in profiles}
    allowed_source_ids = {str(value) for value in cluster.get("source_ids", []) or []}
    cluster_role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    raw_sections = {
        key: _sanitize_reasoned_items(raw.get(key, []), profile_by_source, allowed_source_ids=allowed_source_ids)
        for key in CLUSTER_SYNTHESIS_SECTIONS
    }
    proposition_by_id = {
        str(row.get("proposition_id") or ""): row
        for row in cluster.get("propositions", []) or []
        if row.get("proposition_id")
    }
    proposition_ids_by_anchor: dict[tuple[str, str], set[str]] = defaultdict(set)
    for proposition_id, proposition in proposition_by_id.items():
        for reference in proposition.get("evidence", []) or []:
            proposition_ids_by_anchor[
                (
                    str(reference.get("source_id") or ""),
                    str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
                )
            ].add(proposition_id)
    sections: dict[str, list[dict[str, Any]]] = {key: [] for key in CLUSTER_SYNTHESIS_SECTIONS}
    rejected_assertions: list[dict[str, Any]] = []
    quantitative_comparisons: list[dict[str, Any]] = []
    for section, items in raw_sections.items():
        for item in items:
            evidence = list(item.get("evidence", []) or [])
            inferred_propositions = sorted(
                {
                    proposition_id
                    for reference in evidence
                    for proposition_id in proposition_ids_by_anchor.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
                        ),
                        set(),
                    )
                }
            )
            supplied = [
                str(value)
                for value in (
                    item.get("proposition_ids")
                    or ([item.get("proposition_id")] if item.get("proposition_id") else [])
                )
                if str(value) in proposition_by_id
            ]
            proposition_ids = sorted(set(supplied or inferred_propositions))
            statement = str(
                item.get("assertion")
                or item.get("finding")
                or item.get("position")
                or item.get("agreement")
                or item.get("contradiction")
                or item.get("text")
                or item.get("summary")
                or ""
            ).strip()
            rejection_reason = ""
            effective_evidence_bases = {
                evidence_base_id
                for reference in evidence
                if (evidence_base_id := _reference_evidence_base_id(reference))
            }
            if not item.get("quantitative_results"):
                item["quantitative_results"] = _evidence_quantitative_results(evidence, profile_by_source)
            quantitative_errors = _quantitative_item_errors(
                item,
                require_comparable=section
                in {"central_findings", "agreements", "positions", "contradictions"},
            )
            has_quantitative_content = bool(
                item.get("quantitative_results")
                or item.get("quantitative_comparisons")
                or item.get("statistics")
                or item.get("technical_context")
                or item.get("technical_result")
                or item.get("technical_detail")
            )
            if has_quantitative_content:
                quantitative_result_ids = sorted(
                    str(row.get("quantitative_result_id") or "")
                    for row in item.get("quantitative_results", []) or []
                    if isinstance(row, Mapping) and row.get("quantitative_result_id")
                )
                quantitative_comparisons.append(
                    {
                        "comparison_id": f"quantitative-comparison-{_stable_hash([cluster_id, section, item])[:16]}",
                        "proposition_id": proposition_ids[0] if len(proposition_ids) == 1 else "",
                        "source_ids": sorted(
                            {str(row.get("source_id") or "") for row in evidence if row.get("source_id")}
                        ),
                        "quantitative_result_ids": quantitative_result_ids,
                        "status": (
                            "rejected"
                            if quantitative_errors
                            else "valid"
                            if len(quantitative_result_ids) >= 2 or item.get("quantitative_comparisons")
                            else "qualified"
                        ),
                        "estimands_comparable": not any("estimand" in error for error in quantitative_errors),
                        "outcomes_comparable": not any("outcome" in error for error in quantitative_errors),
                        "populations_comparable": not any("population" in error for error in quantitative_errors),
                        "arithmetic_reproducible": not any(
                            "arithmetic" in error or "percentage_point" in error
                            for error in quantitative_errors
                        ),
                        "reason": (
                            ";".join(quantitative_errors)
                            if quantitative_errors
                            else "deterministic_quantitative_checks_passed"
                        ),
                        "qualifications": quantitative_errors,
                    }
                )
            if (
                section != "source_contributions"
                and quantitative_errors
                and not _has_numerical_claim(statement)
            ):
                # A bad optional numerical gloss must not erase a supported
                # qualitative proposition. Preserve the rejected comparison in
                # the machine audit, drop only the unverified arithmetic, and
                # retain source-specific figures in the contribution section.
                for field_name in (
                    "technical_result",
                    "technical_detail",
                    "technical_context",
                    "statistics",
                    "quantitative_results",
                    "quantitative_comparisons",
                ):
                    item.pop(field_name, None)
                item["plain_english_meaning"] = (
                    "In plain English, the collection supports the direction of this relationship. "
                    "The source-specific figures remain separate below because their quantitative "
                    "comparability was not verified."
                )
                item["quantitative_detail_status"] = "omitted_unvalidated_comparison"
                quantitative_errors = []
            if section == "source_contributions":
                contribution_source_id = str(item.get("source_id") or (evidence[0].get("source_id") if evidence else ""))
                if not contribution_source_id or any(
                    str(reference.get("source_id") or "") != contribution_source_id
                    for reference in evidence
                ):
                    rejection_reason = "source_contribution_mixes_sources"
                item["source_id"] = contribution_source_id
                item["cluster_role"] = cluster_role_by_source.get(contribution_source_id, "context")
                supplied_comparison = str(item.get("comparison_status") or "")
                item["comparison_status"] = (
                    "context_only"
                    if item["cluster_role"] in {"context", "bridge"}
                    else supplied_comparison
                    if supplied_comparison
                    in {"single_source", "supports_shared_pattern", "contrasts_with_shared_pattern"}
                    else "single_source"
                )
                item["related_proposition_ids"] = proposition_ids
                supplied_kind = str(item.get("contribution_kind") or "")
                allowed_kinds = {
                    "direct_proposition_finding",
                    "unique_cluster_relevant_finding",
                    "boundary_evidence",
                    "methodological_context",
                    "conceptual_context",
                    "bridge_evidence",
                }
                item["contribution_kind"] = (
                    supplied_kind
                    if supplied_kind in allowed_kinds
                    else "bridge_evidence"
                    if item["cluster_role"] == "bridge"
                    else "conceptual_context"
                    if item["cluster_role"] == "context"
                    else "direct_proposition_finding"
                    if proposition_ids
                    else "unique_cluster_relevant_finding"
                )
                item["technical_result"] = str(
                    item.get("technical_result")
                    or item.get("technical_detail")
                    or item.get("technical_context")
                    or item.get("statistics")
                    or ""
                )
                item["relation_to_cluster_question"] = str(
                    item.get("relation_to_cluster_question")
                    or (
                        "Direct evidence for an admitted proposition."
                        if proposition_ids
                        else "A cluster-relevant source finding retained without a cross-source comparison."
                    )
                )
                if quantitative_errors:
                    rejection_reason = ";".join(quantitative_errors)
            elif not proposition_ids:
                rejection_reason = "assertion_not_linked_to_proposition_cell"
            elif _has_unqualified_causal_language(statement) and not any(
                str(reference.get("empirical_role") or "none") in CAUSAL_SUPPORT_ROLES
                for reference in evidence
            ):
                rejection_reason = "causal_wording_without_causal_or_mechanism_anchor"
            elif not effective_evidence_bases:
                rejection_reason = "assertion_has_no_independent_source_support"
            elif section in {"central_findings", "agreements", "contradictions"} and len(
                effective_evidence_bases
            ) < 2:
                rejection_reason = "comparative_assertion_requires_two_effective_evidence_bases"
            elif section in {"central_findings", "agreements", "positions", "contradictions"} and any(
                cluster_role_by_source.get(str(reference.get("source_id") or ""), "context") != "core"
                for reference in evidence
            ):
                rejection_reason = "context_or_bridge_source_cannot_support_comparative_verdict"
            elif section == "agreements" and str(raw.get("debate_state") or "") == "mapped_consensus" and len(
                effective_evidence_bases
            ) < 3:
                rejection_reason = "mapped_consensus_requires_three_effective_evidence_bases"
            elif section == "contradictions" and re.match(
                r"^\s*no\s+(?:direct\s+)?contradictions?\b",
                statement,
                flags=re.IGNORECASE,
            ):
                rejection_reason = "absence_of_contradiction_is_not_a_contradiction"
            elif quantitative_errors:
                rejection_reason = ";".join(quantitative_errors)
            if rejection_reason:
                rejected_assertions.append(
                    {"section": section, "reason": rejection_reason, "statement": statement, "evidence": evidence}
                )
                continue
            if section == "source_contributions":
                contribution_id = str(
                    item.get("contribution_id")
                    or f"contribution-{_stable_hash([cluster_id, item['source_id'], evidence, statement])[:16]}"
                )
                sections[section].append(
                    {
                        "contribution_id": contribution_id,
                        "source_id": item["source_id"],
                        "cluster_role": item["cluster_role"],
                        "contribution_kind": item["contribution_kind"],
                        "related_proposition_ids": item["related_proposition_ids"],
                        "finding": statement,
                        "technical_result": item["technical_result"],
                        "plain_english_meaning": str(item.get("plain_english_meaning") or ""),
                        "relation_to_cluster_question": item["relation_to_cluster_question"],
                        "comparison_status": item["comparison_status"],
                        "evidence": evidence,
                    }
                )
                continue
            semantic_item = {
                key: value
                for key, value in item.items()
                if key not in {"item_id", "assertion_id", "updated_at", "evidence", "supporting_evidence"}
            }
            assertion_id = (
                f"assertion-{slugify(section)}-"
                f"{_stable_hash([cluster_id, section, proposition_ids, semantic_item])[:12]}"
            )
            item["assertion_id"] = assertion_id
            item["item_id"] = assertion_id
            item["proposition_ids"] = proposition_ids
            sections[section].append(item)
    deduplicated_contributions: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for contribution in sections["source_contributions"]:
        anchor_ids = tuple(
            sorted(
                {
                    str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
                    for reference in contribution.get("evidence", []) or []
                    if str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
                }
            )
        )
        finding_identity = _canonical_phrase(contribution.get("finding") or "")
        key = (
            str(contribution.get("source_id") or ""),
            (f"finding:{finding_identity}",) if finding_identity else anchor_ids,
        )
        prior = deduplicated_contributions.get(key)
        if prior is None or sum(
            len(str(contribution.get(field_name) or ""))
            for field_name in ("finding", "plain_english_meaning", "technical_result")
        ) > sum(
            len(str(prior.get(field_name) or ""))
            for field_name in ("finding", "plain_english_meaning", "technical_result")
        ):
            deduplicated_contributions[key] = contribution
    sections["source_contributions"] = list(deduplicated_contributions.values())

    fallback_contributions = _fallback_source_contributions(cluster, profiles)
    existing_contribution_keys = {
        (
            str(row.get("source_id") or ""),
            str((_as_mapping(row.get("evidence", [{}])[0])).get("evidence_anchor_id") or "")
            if row.get("evidence")
            else "",
        )
        for row in sections["source_contributions"]
    }
    existing_contribution_findings = {
        (
            str(row.get("source_id") or ""),
            _canonical_phrase(row.get("finding") or ""),
        )
        for row in sections["source_contributions"]
        if _canonical_phrase(row.get("finding") or "")
    }
    for contribution in fallback_contributions:
        contribution = dict(contribution)
        fallback_quantitative_errors = _quantitative_item_errors(
            contribution,
            require_comparable=False,
        )
        if fallback_quantitative_errors:
            evidence = [
                _as_mapping(reference)
                for reference in contribution.get("evidence", []) or []
                if isinstance(reference, Mapping)
            ]
            source_ids = sorted(
                {
                    str(reference.get("source_id") or "")
                    for reference in evidence
                    if reference.get("source_id")
                }
            )
            quantitative_results = _evidence_quantitative_results(evidence, profile_by_source)
            quantitative_comparisons.append(
                {
                    "comparison_id": (
                        f"quantitative-comparison-"
                        f"{_stable_hash([cluster_id, 'source_contributions', contribution])[:16]}"
                    ),
                    "proposition_id": str((contribution.get("related_proposition_ids") or [""])[0]),
                    "source_ids": source_ids,
                    "quantitative_result_ids": sorted(
                        str(row.get("quantitative_result_id") or "")
                        for row in quantitative_results
                        if row.get("quantitative_result_id")
                    ),
                    "status": "rejected",
                    "estimands_comparable": not any(
                        "estimand" in error for error in fallback_quantitative_errors
                    ),
                    "outcomes_comparable": not any(
                        "outcome" in error for error in fallback_quantitative_errors
                    ),
                    "populations_comparable": not any(
                        "population" in error for error in fallback_quantitative_errors
                    ),
                    "arithmetic_reproducible": not any(
                        "arithmetic" in error or "percentage_point" in error
                        for error in fallback_quantitative_errors
                    ),
                    "reason": ";".join(fallback_quantitative_errors),
                    "qualifications": fallback_quantitative_errors,
                }
            )
            rejected_assertions.append(
                {
                    "section": "source_contributions",
                    "reason": ";".join(fallback_quantitative_errors),
                    "statement": str(contribution.get("finding") or ""),
                    "evidence": evidence,
                }
            )
            if _has_numerical_claim(contribution.get("finding")):
                contribution["finding"] = (
                    "The source reports a cluster-relevant quantitative finding, but its exact figures "
                    "are omitted here because the stored numerical summaries did not pass arithmetic validation."
                )
            contribution["technical_result"] = ""
            contribution["plain_english_meaning"] = (
                "The source supports the stated relationship, but its stored numerical summaries could not be "
                "reconciled as one comparison. Consult the linked source locator for the separate estimates."
            )
        reference = _as_mapping((contribution.get("evidence") or [{}])[0])
        key = (str(contribution.get("source_id") or ""), str(reference.get("evidence_anchor_id") or ""))
        finding_key = (
            str(contribution.get("source_id") or ""),
            _canonical_phrase(contribution.get("finding") or ""),
        )
        if key in existing_contribution_keys or finding_key in existing_contribution_findings:
            continue
        sections["source_contributions"].append(contribution)
        existing_contribution_keys.add(key)
        if finding_key[1]:
            existing_contribution_findings.add(finding_key)
    capped_contributions: list[dict[str, Any]] = []
    contribution_counts: Counter[str] = Counter()
    for contribution in sections["source_contributions"]:
        source_id = str(contribution.get("source_id") or "")
        limit = 3 if cluster_role_by_source.get(source_id) == "core" else 1
        if contribution_counts[source_id] >= limit:
            continue
        contribution_counts[source_id] += 1
        capped_contributions.append(contribution)
    sections["source_contributions"] = capped_contributions
    top_evidence = _resolve_reasoner_evidence(
        raw.get("supporting_evidence", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    # Top-level evidence is a summary convenience, not a stricter provenance
    # boundary than the assertions themselves. Count every admitted section
    # reference when deciding whether the synthesis is genuinely multi-source.
    top_evidence = sorted(
        {
            _stable_hash(reference): reference
            for reference in [
                *top_evidence,
                *(
                    reference
                    for rows in sections.values()
                    for row in rows
                    for reference in row.get("evidence", []) or []
                ),
            ]
        }.values(),
        key=lambda row: (row["source_id"], row["claim_id"]),
    )
    comparative_evidence = [
        reference
        for section, rows in sections.items()
        if section != "source_contributions"
        for row in rows
        for reference in row.get("evidence", []) or []
    ]
    supporting_evidence_bases = {
        evidence_base_id
        for row in comparative_evidence
        if (evidence_base_id := _reference_evidence_base_id(row))
    }
    substantive = len(supporting_evidence_bases) >= 2 and any(
        rows for section, rows in sections.items() if section != "source_contributions"
    )
    gap_hypotheses = _sanitize_reasoned_items(
        raw.get("gap_hypotheses", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    for hypothesis in gap_hypotheses:
        evidence = list(hypothesis.get("evidence", []) or [])
        proposition_ids = sorted(
            {
                proposition_id
                for reference in evidence
                for proposition_id in proposition_ids_by_anchor.get(
                    (
                        str(reference.get("source_id") or ""),
                        str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
                    ),
                    set(),
                )
            }
        )
        supplied = str(hypothesis.get("proposition_id") or "")
        if supplied in proposition_by_id:
            proposition_ids = sorted({*proposition_ids, supplied})
        if not proposition_ids:
            hypothesis["_rejected"] = True
            hypothesis["_rejection_reason"] = "gap_hypothesis_missing_proposition_lineage"
            continue
        hypothesis["proposition_id"] = proposition_ids[0]
        hypothesis.setdefault("related_cluster_ids", [cluster_id])
        hypothesis["related_cluster_ids"] = sorted(
            {cluster_id, *(str(value) for value in hypothesis.get("related_cluster_ids", []) or [] if str(value))}
        )
        hypothesis["supporting_evidence"] = list(hypothesis.pop("evidence", []))
    gap_hypotheses = [row for row in gap_hypotheses if not row.pop("_rejected", False)]
    synthesis_assertions = sorted(
        [
            dict(item)
            for section, items in sections.items()
            if section != "source_contributions"
            for item in items
        ],
        key=lambda row: str(row.get("assertion_id") or ""),
    )
    debate_state = str(raw.get("debate_state") or "")
    if debate_state not in DEBATE_STATES:
        debate_state = ""
    verdict_paragraphs: list[dict[str, Any]] = []
    seen_verdict_statements: set[str] = set()
    if substantive:
        for section in ("central_findings", "agreements", "positions", "contradictions"):
            for item in sections[section]:
                statement = _cluster_item_text(item)
                if not statement or statement.casefold() in seen_verdict_statements:
                    continue
                seen_verdict_statements.add(statement.casefold())
                plain = str(item.get("plain_english_meaning") or item.get("plain_english") or "").strip()
                technical = str(
                    item.get("technical_result")
                    or item.get("technical_detail")
                    or item.get("technical_context")
                    or item.get("statistics")
                    or ""
                ).strip()
                paragraph = " ".join(value for value in (statement, plain, technical) if value)
                verdict_paragraphs.append(
                    {
                        "verdict_id": f"verdict-{_stable_hash([cluster_id, item.get('assertion_id'), paragraph])[:16]}",
                        "section": section,
                        "text": paragraph,
                        "assertion_ids": [str(item.get("assertion_id") or "")],
                        "proposition_ids": [str(value) for value in item.get("proposition_ids", []) or []],
                        "evidence": list(item.get("evidence", []) or []),
                    }
                )
    synthesis_text = "\n\n".join(row["text"] for row in verdict_paragraphs)
    result = {
        "cluster_id": cluster_id,
        "status": "deterministic_fallback",
        "scope": str(raw.get("scope") or cluster.get("shared_question") or ""),
        "boundaries": [str(value) for value in raw.get("boundaries", []) or [] if str(value)],
        "coherence_rationale": str(
            raw.get("coherence_rationale") if substantive else cluster.get("coherence_rationale") or ""
        ),
        "synthesis": synthesis_text,
        "verdict_paragraphs": verdict_paragraphs,
        "synthesis_assertions": synthesis_assertions,
        "debate_state": debate_state,
        "rejected_assertions": rejected_assertions,
        "supporting_evidence": top_evidence,
        "effective_evidence_base_count": len(supporting_evidence_bases),
        "gap_hypotheses": gap_hypotheses,
        "quantitative_comparisons": quantitative_comparisons,
        **sections,
    }
    if substantive:
        quality_errors = _cluster_synthesis_quality_errors(result, cluster)
        result["quality_errors"] = quality_errors
        result["quality_status"] = "complete" if not quality_errors else "incomplete"
        result["status"] = "reasoned" if not quality_errors else "partial"
    else:
        result["quality_errors"] = []
        result["quality_status"] = "not_applicable"
    return result


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
        if evidence_classification == "debate":
            evidence_classification = "mapped_debate"
        if evidence_classification not in DEBATE_STATES:
            evidence_classification = "no_debate"
        detected_debate = evidence_classification == "mapped_debate"
        promoted = detected_debate and auto_promote
        classification = evidence_classification
        assessment.update(
            {
                "classification": classification,
                "evidence_classification": evidence_classification,
                "status": classification,
                "promoted": promoted,
                "automation_status": "promoted" if promoted else "mapped",
                "positions": positions if classification in {"mapped_debate", "complementary_positions", "parallel_literatures"} and positions else assessment.get("positions", []),
                "agreements": (
                    agreements
                    if evidence_classification
                    in {
                        "mapped_consensus",
                        "emerging_convergence",
                        "aligned_institutional_guidance",
                        "within_program_consistency",
                    }
                    and agreements
                    else assessment.get("agreements", [])
                ),
                "contradictions": contradictions if classification in {"mapped_debate", "mixed_evidence"} and contradictions else assessment.get("contradictions", []),
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
    candidates: list[dict[str, Any]] = []
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


def _build_dimensional_evidence_matrices_legacy(
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


def build_evidence_matrices(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build proposition rows by core source; never cross-product metadata."""

    rows = _ensure_profiles(profiles)
    profile_by_source = {str(row["source_id"]): row for row in rows}
    matrices: list[dict[str, Any]] = []
    for cluster in clusters:
        core_source_ids = [str(value) for value in cluster.get("core_source_ids", []) or []]
        proposition_rows: list[dict[str, Any]] = []
        for proposition in cluster.get("propositions", []) or []:
            cells: dict[str, dict[str, Any]] = {}
            for raw_cell in proposition.get("cells", []) or []:
                source_id = str(raw_cell.get("source_id") or "")
                if source_id not in core_source_ids:
                    continue
                valid_evidence = [
                    dict(reference)
                    for reference in raw_cell.get("evidence", []) or []
                    if isinstance(reference, Mapping)
                    and (
                        (profile := profile_by_source.get(source_id)) is not None
                        and _reference_is_synthesis_eligible(reference, profile)
                    )
                ]
                if not valid_evidence:
                    continue
                cells[source_id] = {
                    "source_id": source_id,
                    "study_family_id": str(raw_cell.get("study_family_id") or source_id),
                    "evidence_base_group_id": str(
                        raw_cell.get("evidence_base_group_id")
                        or profile_by_source[source_id].get("evidence_base_group_id")
                        or ""
                    ),
                    "counted_as_independent": bool(
                        raw_cell.get("counted_as_independent")
                        if "counted_as_independent" in raw_cell
                        else profile_by_source[source_id].get("evidence_base_counted")
                    ),
                    "stance_or_finding": str(raw_cell.get("stance_or_finding") or ""),
                    "evidence_type": list(raw_cell.get("evidence_type", []) or []),
                    "scope": dict(raw_cell.get("scope") or {}),
                    "boundary_conditions": list(raw_cell.get("boundary_conditions", []) or []),
                    "direction_or_interpretation": list(raw_cell.get("direction_or_interpretation", []) or []),
                    "uncertainty": list(raw_cell.get("uncertainty", []) or []),
                    "evidence": valid_evidence,
                }
            family_count = len(
                {
                    str(cell.get("study_family_id") or source_id)
                    for source_id, cell in cells.items()
                }
            )
            evidence_base_count = len(
                {
                    str(cell.get("evidence_base_group_id") or "")
                    for source_id, cell in cells.items()
                    if cell.get("counted_as_independent") is True
                    and cell.get("evidence_base_group_id")
                }
            )
            if len(cells) < 2:
                continue
            proposition_rows.append(
                {
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": str(proposition.get("statement") or ""),
                    "question": str(proposition.get("question") or ""),
                    "proposition_type": str(proposition.get("proposition_type") or "unknown"),
                    "comparability": dict(proposition.get("comparability") or {}),
                    "independent_core_study_family_count": family_count,
                    "effective_evidence_base_count": evidence_base_count,
                    "publication_count": len(cells),
                    "admission_eligible": evidence_base_count >= 2,
                    "cells": {source_id: cells[source_id] for source_id in sorted(cells)},
                }
            )
        matrices.append(
            {
                "matrix_id": f"matrix-{_stable_hash([cluster['cluster_id'], PROPOSITION_MATRIX_VERSION])[:12]}",
                "matrix_version": PROPOSITION_MATRIX_VERSION,
                "cluster_id": str(cluster["cluster_id"]),
                "core_source_ids": core_source_ids,
                "propositions": proposition_rows,
                "proposition_count": len(proposition_rows),
                "locator_backed_only": True,
                "source_level_metadata_inherited": False,
                "admission_passed": any(row.get("admission_eligible") for row in proposition_rows),
            }
        )
    return sorted(matrices, key=lambda row: row["cluster_id"])


def _build_debate_registry_legacy(
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


def _cell_direction(cell: Mapping[str, Any]) -> str:
    values = [str(value) for value in cell.get("direction_or_interpretation", []) or [] if str(value)]
    normalized = {
        direction
        for value in values
        if (direction := _normalize_direction(value)) in {"positive", "negative", "null", "mixed"}
    }
    normalized.discard("not_reported")
    return next(iter(normalized)) if len(normalized) == 1 else ("mixed" if normalized else "not_reported")


def _proposition_debate_state(proposition: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    cells = [dict(cell) for cell in _as_mapping(proposition.get("cells")).values() if isinstance(cell, Mapping)]
    if not cells:
        return "no_debate", {}
    if len(cells) == 1:
        return "single_position", {}
    evidence_roles = {
        str(role)
        for cell in cells
        for role in cell.get("evidence_type", []) or []
        if str(role)
    }
    empirical = evidence_roles & EMPIRICAL_SUPPORT_ROLES
    argumentative = evidence_roles & ARGUMENT_SUPPORT_ROLES
    evidence_base_count = int(
        proposition.get("effective_evidence_base_count")
        or len(
            {
                str(cell.get("evidence_base_group_id") or "")
                for cell in cells
                if cell.get("counted_as_independent") is True
                and cell.get("evidence_base_group_id")
            }
        )
    )
    publication_count = int(proposition.get("publication_count") or len(cells))
    if publication_count >= 2 and evidence_base_count == 1:
        return "within_program_consistency", {"publication_count": publication_count}
    if empirical and argumentative:
        return "complementary_positions", {}
    directions = [_cell_direction(cell) for cell in cells]
    reported = {value for value in directions if value not in {"not_reported", "mixed"}}
    boundaries = {
        _canonical_phrase(cell.get("boundary_conditions", []))
        for cell in cells
        if _canonical_phrase(cell.get("boundary_conditions", []))
    }
    if len(reported) >= 2:
        comparability = _as_mapping(proposition.get("comparability"))
        if bool(comparability.get("direction_orientation_aligned", True)):
            return "mapped_debate", {"directions": sorted(reported)}
        stance_text = " ".join(str(cell.get("stance_or_finding") or "") for cell in cells)
        explicit_positive = bool(
            re.search(r"\b(?:positive(?:ly)?|increas(?:e|es|ed)|higher|more likely)\b", stance_text, re.I)
        )
        explicit_negative = bool(
            re.search(r"\b(?:negative(?:ly)?|decreas(?:e|es|ed)|lower|less likely|reduc(?:e|es|ed))\b", stance_text, re.I)
        )
        if explicit_positive and explicit_negative:
            return "mapped_debate", {"directions": sorted(reported)}
        return "mixed_evidence", {"directions": sorted(reported), "reason": "direction_orientation_unresolved"}
    if "mixed" in directions:
        return "mixed_evidence", {"directions": sorted(set(directions))}
    if len(boundaries) >= 2:
        return "conditional_relationship", {"boundary_sets": sorted(boundaries)}
    if len(reported) == 1:
        if argumentative == {"practitioner_guidance"}:
            return "aligned_institutional_guidance", {"direction": next(iter(reported))}
        return (
            "mapped_consensus" if evidence_base_count >= 3 else "emerging_convergence",
            {"direction": next(iter(reported)), "effective_evidence_base_count": evidence_base_count},
        )

    stance_tokens = [_tokens(cell.get("stance_or_finding", "")) for cell in cells]
    common = set.intersection(*stance_tokens) if stance_tokens and all(stance_tokens) else set()
    if common and len(common) >= 2:
        if argumentative == {"practitioner_guidance"}:
            return "aligned_institutional_guidance", {"shared_terms": sorted(common)}
        return (
            "mapped_consensus" if evidence_base_count >= 3 else "emerging_convergence",
            {"shared_terms": sorted(common), "effective_evidence_base_count": evidence_base_count},
        )
    if argumentative:
        opposition = any(
            re.search(r"\b(?:reject|oppose|cannot|should not|incompatible|contrary|fails?)\b", str(cell.get("stance_or_finding") or ""), re.I)
            for cell in cells
        )
        return ("mapped_debate" if opposition else "complementary_positions"), {}
    return "mixed_evidence", {}


def build_debate_registry(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Classify only relationships established by proposition matrix cells."""

    matrices = {row["cluster_id"]: row for row in build_evidence_matrices(profiles, clusters)}
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    assessments: list[dict[str, Any]] = []
    precedence = {
        "mapped_debate": 11,
        "conditional_relationship": 10,
        "mixed_evidence": 9,
        "complementary_positions": 8,
        "mapped_consensus": 7,
        "emerging_convergence": 6,
        "aligned_institutional_guidance": 5,
        "within_program_consistency": 4,
        "parallel_literatures": 3,
        "single_position": 2,
        "no_debate": 1,
    }
    for cluster in clusters:
        matrix = matrices.get(str(cluster["cluster_id"]), {})
        proposition_assessments: list[dict[str, Any]] = []
        for proposition in matrix.get("propositions", []) or []:
            state, explanation = _proposition_debate_state(proposition)
            proposition_assessments.append(
                {
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": str(proposition.get("statement") or ""),
                    "state": state,
                    "explanation": explanation,
                    "cells": proposition.get("cells", {}),
                }
            )
        states = [row["state"] for row in proposition_assessments]
        state = max(states, key=lambda value: precedence[value]) if states else "no_debate"
        proposition_source_sets = [
            {
                str(source_id)
                for source_id in _as_mapping(proposition.get("cells"))
            }
            for proposition in matrix.get("propositions", []) or []
        ]
        propositions_are_parallel = bool(proposition_source_sets) and all(
            not left_sources & right_sources
            for left_sources, right_sources in combinations(proposition_source_sets, 2)
        )
        if propositions_are_parallel and len(proposition_assessments) > 1 and all(
            value in {
                "mapped_consensus",
                "emerging_convergence",
                "aligned_institutional_guidance",
                "within_program_consistency",
                "single_position",
                "no_debate",
            }
            for value in states
        ):
            state = "parallel_literatures"
        promoted = auto_promote and state == "mapped_debate"
        supporting = [
            reference
            for proposition in matrix.get("propositions", []) or []
            for cell in _as_mapping(proposition.get("cells")).values()
            for reference in cell.get("evidence", []) or []
        ]
        assessment = {
            "debate_id": f"debate-{_stable_hash([cluster['cluster_id'], state])[:12]}",
            "cluster_id": str(cluster["cluster_id"]),
            "classification": state,
            "evidence_classification": state,
            "status": state,
            "promoted": promoted,
            "automation_status": "promoted" if promoted else "mapped",
            "proposition_assessments": proposition_assessments,
            "supporting_evidence": supporting,
            "effective_evidence_base_count": len(
                {
                    evidence_base_id
                    for row in supporting
                    if (evidence_base_id := _reference_evidence_base_id(row))
                }
            ),
            "positions": [],
            "agreements": [],
            "contradictions": [],
            "contradiction_groups": [],
            "boundaries": [],
            "method_fault_lines": [],
        }
        assessments.append(assessment)
    debates = [row for row in assessments if row["status"] == "mapped_debate"]
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": [],
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": 0,
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
        claim = resolve_claim(
            str(item.get("evidence_anchor_id") or item.get("claim_id") or ""),
            str(item.get("source_id", "")),
        )
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
        if synthesis.get("status") != "reasoned":
            continue
        for signal in synthesis.get("gap_hypotheses", []) or []:
            item = _as_mapping(signal)
            item.setdefault("related_cluster_ids", [cluster_id])
            proposed.append((item, None, "cluster_synthesis"))
    for signal in _reasoner_proposals(reasoner, rows, clusters, request):
        proposed.append((_as_mapping(signal), None, "reasoner_proposal"))

    cluster_by_id = {str(row["cluster_id"]): row for row in clusters}
    for debate in debate_registry.get("debates", []) or []:
        cluster = cluster_by_id.get(str(debate.get("cluster_id")), {})
        groups = [
            {
                "proposition_id": assessment.get("proposition_id"),
                "proposition": assessment.get("statement"),
                "supporting_evidence": [
                    reference
                    for cell in _as_mapping(assessment.get("cells")).values()
                    for reference in cell.get("evidence", []) or []
                ],
            }
            for assessment in debate.get("proposition_assessments", []) or []
            if assessment.get("state") == "mapped_debate"
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
                        "proposition_id": str(group.get("proposition_id") or ""),
                        "originating_cluster_revision": str(cluster.get("revision_hash") or ""),
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

    profile_by_source = {str(row["source_id"]): row for row in rows}
    for matrix in evidence_matrices or ():
        cluster = cluster_by_id.get(str(matrix.get("cluster_id")), {})
        for proposition in matrix.get("propositions", []) or []:
            methods_by_source = {
                source_id: tuple(profile_by_source.get(source_id, {}).get("dimensions", {}).get("method", []) or [])
                for source_id in _as_mapping(proposition.get("cells"))
            }
            nonempty = [methods for methods in methods_by_source.values() if methods]
            shared_methods = set.intersection(*(set(methods) for methods in nonempty)) if nonempty else set()
            evidence = [
                reference
                for cell in _as_mapping(proposition.get("cells")).values()
                for reference in cell.get("evidence", []) or []
            ]
            families = {str(row.get("study_family_id")) for row in evidence}
            if len(shared_methods) != 1 or len(nonempty) != len(methods_by_source) or len(families) < 2:
                continue
            method = sorted(shared_methods)[0]
            proposed.append(
                (
                    {
                        "rule": "methodological_concentration",
                        "topic": proposition.get("statement") or cluster.get("semantic_identity", ""),
                        "missing_evidence": f"A comparable test of this proposition using a method other than {method}.",
                        "related_cluster_ids": [matrix.get("cluster_id")],
                        "proposition_id": proposition.get("proposition_id"),
                        "originating_cluster_revision": cluster.get("revision_hash", ""),
                        "supporting_evidence": evidence,
                        "why_matters": "The collection cannot distinguish a robust relationship from a method-dependent result.",
                        "contribution": "A methodologically distinct comparison would test whether the proposition survives a changed evidence strategy.",
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
                "proposition_ids": [],
                "originating_cluster_revisions": [],
                "missing_cell": dict(signal.get("missing_cell") or {}),
            },
        )
        candidate["related_cluster_ids"].extend(_flatten_values(signal.get("related_cluster_ids") or signal.get("related_clusters")))
        candidate["supporting_evidence"].extend(_signal_evidence(signal, profile, claim_lookup))
        candidate["proposal_origins"].append(origin)
        if signal.get("proposition_id"):
            candidate["proposition_ids"].append(str(signal["proposition_id"]))
        if signal.get("originating_cluster_revision"):
            candidate["originating_cluster_revisions"].append(str(signal["originating_cluster_revision"]))
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
        evidence_keys = {
            (str(row.get("source_id") or ""), str(row.get("evidence_anchor_id") or row.get("claim_id") or ""))
            for row in candidate["supporting_evidence"]
        }
        for cluster_id in candidate["related_cluster_ids"]:
            cluster = cluster_by_id.get(cluster_id, {})
            for proposition in cluster.get("propositions", []) or []:
                proposition_keys = {
                    (str(row.get("source_id") or ""), str(row.get("evidence_anchor_id") or row.get("claim_id") or ""))
                    for row in proposition.get("evidence", []) or []
                }
                if evidence_keys & proposition_keys:
                    candidate["proposition_ids"].append(str(proposition.get("proposition_id") or ""))
                    candidate["originating_cluster_revisions"].append(str(cluster.get("revision_hash") or ""))
        candidate["proposition_ids"] = sorted({value for value in candidate["proposition_ids"] if value})
        candidate["proposition_id"] = candidate["proposition_ids"][0] if len(candidate["proposition_ids"]) == 1 else ""
        lineage_keys = {
            (
                str(reference.get("source_id") or ""),
                str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
            )
            for cluster_id in candidate["related_cluster_ids"]
            for proposition in cluster_by_id.get(cluster_id, {}).get("propositions", []) or []
            if str(proposition.get("proposition_id") or "") in set(candidate["proposition_ids"])
            for reference in proposition.get("evidence", []) or []
            if isinstance(reference, Mapping)
        }
        candidate["proposition_evidence_keys"] = [
            {"source_id": source_id, "evidence_anchor_id": anchor_id}
            for source_id, anchor_id in sorted(lineage_keys)
        ]
        candidate["originating_cluster_revisions"] = sorted(
            {value for value in candidate["originating_cluster_revisions"] if value}
        )
        candidate["originating_cluster_revision"] = (
            candidate["originating_cluster_revisions"][0]
            if len(candidate["originating_cluster_revisions"]) == 1
            else ""
        )
        if not candidate["missing_cell"]:
            missing_key = {
                "untested_mechanism": "mechanism",
                "empirical_coverage": "population_or_case",
                "methodological_concentration": "method",
                "measurement_or_data": "measure_or_data",
                "boundary_condition": "boundary_condition",
                "replication": "replication",
                "contradictory_findings": "adjudicating_comparison",
                "cross_cluster_integration": "cross_cluster_relationship",
                "author_stated_gap": "author_specified_relationship",
            }.get(candidate["rule"], "missing_relationship")
            candidate["missing_cell"] = {"kind": missing_key, "description": candidate["precise_missing_evidence"]}
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
    if not candidate.get("proposition_ids"):
        errors.append("missing_originating_proposition")
    if candidate.get("related_cluster_ids") and not candidate.get("originating_cluster_revisions"):
        errors.append("missing_originating_cluster_revision")
    if not _as_mapping(candidate.get("missing_cell")).get("description"):
        errors.append("missing_precise_matrix_cell")
    evidence = list(candidate.get("supporting_evidence", []) or [])
    if not evidence or not all(
        row.get("source_id")
        and (row.get("evidence_anchor_id") or row.get("claim_id"))
        and _complete_locator(row.get("locator"))
        for row in evidence
    ):
        errors.append("missing_locator_backed_generation_evidence")
    rule = str(candidate.get("rule") or "")
    if rule == "cross_cluster_integration" and len(set(candidate.get("related_cluster_ids", []) or [])) < 2:
        errors.append("cross_cluster_gap_requires_two_clusters")
    if rule == "contradictory_findings" and len(
        {
            evidence_base_id
            for row in evidence
            if (evidence_base_id := _reference_evidence_base_id(row))
        }
    ) < 2:
        errors.append("contradiction_requires_two_effective_evidence_bases")
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
    lineage_keys = {
        (str(row.get("source_id") or ""), str(row.get("evidence_anchor_id") or row.get("claim_id") or ""))
        for row in candidate.get("proposition_evidence_keys", []) or []
        if isinstance(row, Mapping)
    }
    named_proposition = bool(candidate.get("proposition_id")) and bool(lineage_keys)
    opposing_comparable_pair = any(
        bool(_anchor_evidence_base_id(left))
        and bool(_anchor_evidence_base_id(right))
        and _anchor_evidence_base_id(left) != _anchor_evidence_base_id(right)
        and str(left.get("direction")) != str(right.get("direction"))
        and (
            _same_semantic_proposition(left, right)
            or (
                named_proposition
                and (
                    str(left.get("source_id") or ""),
                    str(left.get("evidence_anchor_id") or left.get("claim_id") or ""),
                )
                in lineage_keys
                and (
                    str(right.get("source_id") or ""),
                    str(right.get("evidence_anchor_id") or right.get("claim_id") or ""),
                )
                in lineage_keys
            )
        )
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
    if status in {"narrows", "narrowed"}:
        match = "narrows"
    elif status in {"counter", "counters", "contradicts"}:
        match = "counters"
    elif status in {"partial", "partially_answered"}:
        match = "partial"
    else:
        match = "answered"
    reference = {
        "evidence_anchor_id": str(item.get("evidence_anchor_id") or item.get("claim_id") or ""),
        "claim_id": str(item.get("evidence_anchor_id") or item.get("claim_id") or ""),
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
                result_status = "answers" if _complete_locator((answer_reference or {}).get("locator")) else "full_text_required"
            elif answer_status == "partial":
                result_status = "partially_answers" if _complete_locator((answer_reference or {}).get("locator")) else "full_text_required"
            elif answer_status == "narrows":
                result_status = "narrows" if _complete_locator((answer_reference or {}).get("locator")) else "full_text_required"
            elif answer_status == "counters":
                result_status = "counters" if _complete_locator((answer_reference or {}).get("locator")) else "full_text_required"
            elif profile["source_id"] in supporting_source_ids:
                result_status = "originating_support"
            elif overlap:
                result_status = "related_but_nonresponsive"
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
            results.append(
                {
                    "source_id": profile["source_id"],
                    "study_family_id": profile["study_family_id"],
                    "status": "full_text_required",
                    "semantic_overlap": overlap,
                }
            )

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
        support_families = {
            evidence_base_id
            for row in complete_support
            if (evidence_base_id := _reference_evidence_base_id(row))
        }
        locator_completeness = len(complete_support) / len(support) if support else 0.0
        rule_admission_errors = _gap_rule_admission_errors(candidate, complete_support, claim_lookup)
        if rule_admission_errors:
            status = "underspecified_gap"
            promoted = False
            decision = "reject_rule_admission"
        elif full_answers:
            status = "answered_within_collection"
            promoted = False
            decision = "reject"
        elif partial_answers:
            status = "narrowed_by_collection"
            promoted = False
            decision = "narrow"
        elif (
            bool(_policy_value(policy, "auto_promote_gaps", True))
            and len(support_families) >= min_families
            and len(complete_support) >= min_families
            and locator_completeness == 1.0
            and not rule_admission_errors
        ):
            status = "collection_surviving_gap"
            promoted = True
            decision = "promote"
        else:
            status = "collection_gap_lead"
            promoted = False
            decision = "retain_lead"
        rule_result = {
            "rule": candidate["rule"],
            "candidate_valid": candidate["rule"] in GAP_RULES and not rule_admission_errors,
            "rule_specific_admission_passed": not rule_admission_errors,
            "rule_admission_errors": rule_admission_errors,
            "independent_supporting_sources": len({row.get("source_id") for row in complete_support}),
            "independent_study_families": len(support_families),
            "effective_evidence_base_count": len(support_families),
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
    status_order = {
        "collection_surviving_gap": 0,
        "collection_gap_lead": 1,
        "narrowed_by_collection": 2,
        "answered_within_collection": 3,
        "underspecified_gap": 4,
    }
    rows: list[dict[str, Any]] = []
    for gap in gaps:
        row = dict(gap)
        result = (row.get("rule_results") or [{}])[0]
        assessment = _as_mapping(row.get("value_assessment"))
        resolution = _as_mapping(row.get("resolution_path"))
        requirements = _as_mapping(resolution.get("requirements"))
        information_gain = str(assessment.get("information_gain") or "low")
        path_type = str(resolution.get("path_type") or "")
        required_fields = _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ())
        required_complete = sum(bool(_flatten_values(requirements.get(field))) for field in required_fields)
        resolution_completeness = (
            (required_complete + bool(resolution.get("question")) + bool(resolution.get("evidence_needed")))
            / max(1, len(required_fields) + 2)
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
            "resolution_path_completeness": round(resolution_completeness, 3),
            "design_completeness": round(resolution_completeness, 3),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            status_order.get(str(row.get("status")), 9),
            {"high": 0, "moderate": 1, "low": 2}.get(row["ranking"]["information_gain"], 3),
            -row["ranking"]["resolution_path_completeness"],
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
        "assertion",
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


_RESOLUTION_PATH_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "quantitative": ("estimand", "comparison", "identification", "measurement"),
    "qualitative": ("case_selection", "mechanism_evidence", "negative_cases", "process_observations"),
    "historical_interpretive": ("archives", "periodization", "source_criticism", "competing_interpretations"),
    "theoretical": ("premises", "derivation", "scope", "model_comparison"),
    "normative": ("principles", "objections", "application_tests"),
    "methodological": ("assumptions", "diagnostics", "benchmarks", "robustness"),
    "practitioner": ("implementation_evidence", "institutional_context", "bias_checks"),
}


def _legacy_design_resolution_path(design: Mapping[str, Any]) -> dict[str, Any]:
    if not design:
        return {}
    design_type = str(design.get("design_type") or "").casefold()
    if any(token in design_type for token in ("qualitative", "case", "process", "interview", "ethnograph")):
        path_type = "qualitative"
        requirements = {
            "case_selection": design.get("target_population") or design.get("unit_of_analysis"),
            "mechanism_evidence": design.get("mechanism_measures"),
            "negative_cases": design.get("comparator"),
            "process_observations": design.get("falsification_or_process_tests"),
        }
    elif any(token in design_type for token in ("histor", "archive", "interpret")):
        path_type = "historical_interpretive"
        requirements = {
            "archives": design.get("data_route"),
            "periodization": design.get("target_population"),
            "source_criticism": design.get("validity_risks"),
            "competing_interpretations": design.get("confounders_or_rival_explanations"),
        }
    elif "normative" in design_type:
        path_type = "normative"
        requirements = {
            "principles": design.get("exposure_or_treatment"),
            "objections": design.get("confounders_or_rival_explanations"),
            "application_tests": design.get("falsification_or_process_tests"),
        }
    elif any(token in design_type for token in ("theor", "model")):
        path_type = "theoretical"
        requirements = {
            "premises": design.get("exposure_or_treatment"),
            "derivation": design.get("identification_or_inference_strategy"),
            "scope": design.get("target_population"),
            "model_comparison": design.get("comparator"),
        }
    elif any(token in design_type for token in ("method", "benchmark", "robust")):
        path_type = "methodological"
        requirements = {
            "assumptions": design.get("confounders_or_rival_explanations"),
            "diagnostics": design.get("falsification_or_process_tests"),
            "benchmarks": design.get("comparator"),
            "robustness": design.get("validity_risks"),
        }
    elif any(token in design_type for token in ("pract", "implement")):
        path_type = "practitioner"
        requirements = {
            "implementation_evidence": design.get("data_route"),
            "institutional_context": design.get("target_population"),
            "bias_checks": design.get("validity_risks"),
        }
    else:
        path_type = "quantitative"
        requirements = {
            "estimand": design.get("estimand"),
            "comparison": design.get("comparator"),
            "identification": design.get("identification_or_inference_strategy"),
            "measurement": [*(design.get("outcomes", []) or []), *(design.get("mechanism_measures", []) or [])],
        }
    return {
        "path_type": path_type,
        "question": str(design.get("research_question") or ""),
        "evidence_needed": str(design.get("data_route") or ""),
        "requirements": requirements,
        "feasibility": str(design.get("feasibility") or ""),
        "limitations": list(design.get("validity_risks", []) or []),
    }


def _gap_quality_errors(gap: Mapping[str, Any], *, require_design: bool) -> list[str]:
    assessment = _as_mapping(gap.get("value_assessment"))
    resolution_path = _as_mapping(gap.get("resolution_path"))
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
        path_type = str(resolution_path.get("path_type") or "")
        if path_type not in _RESOLUTION_PATH_REQUIREMENTS:
            errors.append("missing_or_invalid_resolution_path_type")
        if len(_tokens(resolution_path.get("question", ""))) < 3:
            errors.append("missing_resolution_path_question")
        if len(_tokens(resolution_path.get("evidence_needed", ""))) < 3:
            errors.append("missing_resolution_path_evidence_needed")
        requirements = _as_mapping(resolution_path.get("requirements"))
        for field_name in _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ()):
            if not _flatten_values(requirements.get(field_name)):
                errors.append(f"missing_resolution_path_{field_name}")
        if len(_tokens(resolution_path.get("feasibility", ""))) < 2:
            errors.append("missing_resolution_path_feasibility")
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
    resolution = _as_mapping(gap.get("resolution_path"))
    requirements = _as_mapping(resolution.get("requirements"))
    dimensions = {
        "rule": str(gap.get("rule") or ""),
        "proposition_ids": sorted(str(value) for value in gap.get("proposition_ids", []) or []),
        "missing_cell": _as_mapping(gap.get("missing_cell")),
        "path_type": str(resolution.get("path_type") or ""),
        "requirements": requirements,
        "topic": _canonical_phrase(gap.get("topic", "")),
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
            proposed_resolution = _as_mapping(rationale.get("resolution_path"))
            if not proposed_resolution:
                proposed_resolution = _legacy_design_resolution_path(_as_mapping(rationale.get("study_design")))
            gap["resolution_path"] = proposed_resolution
            # Retain a supplied legacy design only in machine audit data; the
            # canonical decision and Markdown use the type-sensitive path.
            if rationale.get("study_design"):
                gap["legacy_study_design"] = _as_mapping(rationale.get("study_design"))
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
        if not gap.get("resolution_path") and gap.get("study_design"):
            gap["resolution_path"] = _legacy_design_resolution_path(_as_mapping(gap.get("study_design")))
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
        rejected_by_id[gap_id] = {
            **gap_by_id[gap_id],
            "status": "underspecified_gap",
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
                "status": "underspecified_gap",
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
                "status": "underspecified_gap",
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


def _navigation_profile_rows(
    profiles: Sequence[Mapping[str, Any]],
    source_notes: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Hydrate graph-only metadata without changing analytical profiles."""

    notes_by_source = {
        str(row.get("source_id") or ""): row
        for row in source_notes
        if isinstance(row, Mapping) and row.get("source_id")
    }
    notes_by_id = {
        str(row.get("note_id") or ""): row
        for row in source_notes
        if isinstance(row, Mapping) and row.get("note_id")
    }
    hydrated: list[dict[str, Any]] = []
    for profile in profiles:
        row = dict(profile)
        note = notes_by_source.get(str(row.get("source_id") or "")) or notes_by_id.get(
            str(row.get("note_id") or "")
        )
        context = _as_mapping(row.get("context"))
        for field in (
            "original_zotero_tags",
            "normalized_tags",
            "zotero_relations",
            "citation_relations",
            "custody_relations",
        ):
            value = (note or {}).get(field)
            if value in (None, "", [], {}):
                value = context.get(field)
            if value not in (None, "", [], {}):
                row[field] = value
        if note:
            row.setdefault("zotero_item_key", note.get("zotero_item_key", ""))
            row.setdefault("note_path", note.get("note_path", ""))
            row.setdefault("title", note.get("title", ""))
        hydrated.append(row)
    return hydrated


def _source_notes_with_custody_relations(
    workspace: Path,
    source_notes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge exact local custody relations into graph-only source metadata."""

    rows = [dict(row) for row in source_notes]
    by_source = {str(row.get("source_id") or ""): row for row in rows if row.get("source_id")}
    registry = workspace / "01_custody" / "source_relation_registry.csv"
    if not registry.is_file():
        return rows
    with registry.open("r", encoding="utf-8", newline="") as handle:
        relation_rows = list(csv.DictReader(handle))
    for relation in relation_rows:
        source = by_source.get(str(relation.get("source_id") or ""))
        target_id = str(relation.get("related_source_id") or "")
        if source is None or target_id not in by_source or target_id == str(source.get("source_id") or ""):
            continue
        relation_type = str(relation.get("relation_type") or "zotero_related")
        predicate = relation_type if relation_type in {"cites", "cited_by"} else "zotero_related"
        values = source.get("custody_relations")
        relations = dict(values) if isinstance(values, Mapping) else {}
        targets = relations.get(predicate, [])
        target_list = list(targets) if isinstance(targets, list) else [targets] if targets else []
        if target_id not in target_list:
            target_list.append(target_id)
        relations[predicate] = target_list
        source["custody_relations"] = relations
    return rows


def _project_navigation_onto_map(
    navigation: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    clusters: Sequence[dict[str, Any]],
    gaps: Sequence[dict[str, Any]],
    *,
    policy: Any = None,
) -> None:
    """Attach bounded graph projections after analytical identities are frozen."""

    assignments = [dict(row) for row in navigation.get("assignments", []) or []]
    tags_by_id = {
        str(row.get("subject_tag_id") or ""): row
        for row in navigation.get("subject_tags", []) or []
        if row.get("subject_tag_id")
    }
    assignments_by_source: dict[str, set[str]] = defaultdict(set)
    for assignment in assignments:
        if assignment.get("promotion_status", "promoted") != "promoted":
            continue
        assignments_by_source[str(assignment.get("source_id") or "")].add(
            str(assignment.get("subject_tag_id") or "")
        )
    family_by_source = {
        str(row.get("source_id") or ""): str(row.get("study_family_id") or row.get("source_id") or "")
        for row in profiles
    }
    max_tags = int(_policy_value(policy, "max_visible_tags_per_cluster_or_gap", 6))
    max_neighborhoods = int(_policy_value(policy, "max_visible_neighborhoods", 8))
    facet_priority = {
        "mechanism": 0,
        "outcome": 1,
        "concept": 2,
        "theory": 3,
        "case": 4,
        "population": 5,
        "geography": 6,
        "method": 7,
        "measure": 8,
        "data": 9,
        "period": 10,
    }

    cluster_tags: dict[str, set[str]] = {}
    for cluster in clusters:
        core = set(str(value) for value in cluster.get("core_source_ids", []) or [])
        qualifying_counts: Counter[str] = Counter()
        for proposition in cluster.get("propositions", []) or []:
            sources = core & {str(value) for value in proposition.get("source_ids", []) or []}
            tag_sources: dict[str, set[str]] = defaultdict(set)
            for source_id in sources:
                for tag_id in assignments_by_source.get(source_id, set()):
                    tag_sources[tag_id].add(source_id)
            for tag_id, supporting_sources in tag_sources.items():
                families = {family_by_source.get(source_id, source_id) for source_id in supporting_sources}
                if len(families) >= 2:
                    qualifying_counts[tag_id] = max(qualifying_counts[tag_id], len(families))
        ranked_tag_ids = sorted(
            qualifying_counts,
            key=lambda tag_id: (
                -qualifying_counts[tag_id],
                facet_priority.get(str(tags_by_id.get(tag_id, {}).get("facet_type") or ""), 99),
                str(tags_by_id.get(tag_id, {}).get("canonical_tag") or tag_id),
            ),
        )[:max_tags]
        visible_neighborhoods = rank_topic_neighborhoods(
            navigation.get("topic_neighborhoods", []) or [],
            list(core),
            proposition_tag_ids=ranked_tag_ids,
            max_visible=max_neighborhoods,
        )
        cluster["subject_tag_ids"] = ranked_tag_ids
        cluster["subject_tags"] = [
            str(tags_by_id[tag_id].get("canonical_tag") or "")
            for tag_id in ranked_tag_ids
            if tag_id in tags_by_id
        ]
        cluster["topic_neighborhood_ids"] = [
            str(row.get("topic_neighborhood_id") or "") for row in visible_neighborhoods
        ]
        cluster["topic_neighborhoods"] = [
            {
                "topic_neighborhood_id": str(row.get("topic_neighborhood_id") or ""),
                "label": str(row.get("label") or ""),
                "facet_type": str(row.get("facet_type") or ""),
                "canonical_tag": str(
                    tags_by_id.get(str(row.get("canonical_tag_id") or ""), {}).get("canonical_tag") or ""
                ),
                "cluster_member_count": int(row.get("cluster_member_count", 0) or 0),
            }
            for row in visible_neighborhoods
        ]
        cluster["navigation_projection_hash"] = _stable_hash(
            {
                "cluster_id": cluster.get("cluster_id"),
                "subject_tag_ids": ranked_tag_ids,
                "topic_neighborhood_ids": cluster["topic_neighborhood_ids"],
            }
        )
        cluster_tags[str(cluster.get("cluster_id") or "")] = set(ranked_tag_ids)

    cluster_ids_by_neighborhood: dict[str, set[str]] = defaultdict(set)
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id:
            continue
        for neighborhood_id in cluster.get("topic_neighborhood_ids", []) or []:
            if neighborhood_id:
                cluster_ids_by_neighborhood[str(neighborhood_id)].add(cluster_id)
    for summary in navigation.get("human_neighborhood_summaries", []) or []:
        if not isinstance(summary, dict):
            continue
        neighborhood_id = str(summary.get("neighborhood_id") or "")
        summary["related_cluster_ids"] = sorted(cluster_ids_by_neighborhood.get(neighborhood_id, set()))

    propositions_by_id = {
        str(proposition.get("proposition_id") or ""): proposition
        for cluster in clusters
        for proposition in cluster.get("propositions", []) or []
        if proposition.get("proposition_id")
    }
    for gap in gaps:
        eligible_tag_ids: set[str] = set()
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            eligible_tag_ids.update(cluster_tags.get(str(cluster_id), set()))
        for proposition_id in gap.get("proposition_ids", []) or []:
            proposition = propositions_by_id.get(str(proposition_id), {})
            source_ids = [str(value) for value in proposition.get("source_ids", []) or []]
            if not source_ids:
                continue
            shared = set.intersection(
                *(assignments_by_source.get(source_id, set()) for source_id in source_ids)
            ) if source_ids else set()
            eligible_tag_ids.update(shared)
        ranked_gap_tags = sorted(
            (tag_id for tag_id in eligible_tag_ids if tag_id in tags_by_id),
            key=lambda tag_id: (
                facet_priority.get(str(tags_by_id[tag_id].get("facet_type") or ""), 99),
                str(tags_by_id[tag_id].get("canonical_tag") or tag_id),
            ),
        )[:max_tags]
        gap["subject_tag_ids"] = ranked_gap_tags
        gap["subject_tags"] = [str(tags_by_id[tag_id].get("canonical_tag") or "") for tag_id in ranked_gap_tags]
        gap["navigation_projection_hash"] = _stable_hash(
            {"gap_id": gap.get("gap_id"), "subject_tag_ids": ranked_gap_tags}
        )


def build_locator_audit(profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        for anchor in profile.get("claims", []) or []:
            locator = _as_mapping(anchor.get("source_locator")) or _source_locator(anchor.get("locator"))
            rows.append(
                {
                    "source_id": str(profile.get("source_id") or ""),
                    "evidence_anchor_id": str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""),
                    "locator": str(anchor.get("locator") or ""),
                    "locator_kind": str(locator.get("kind") or "missing"),
                    "traceable": bool(locator.get("traceable")),
                    "strong_synthesis_support": bool(locator.get("strong_synthesis_support")),
                    "rejection_reason": str(locator.get("rejection_reason") or ""),
                }
            )
    counts = Counter(str(row["locator_kind"]) for row in rows)
    return {
        "version": LOCATOR_AUDIT_VERSION,
        "anchor_count": len(rows),
        "strong_locator_count": sum(1 for row in rows if row["strong_synthesis_support"]),
        "generated_note_heading_count": counts.get("generated_note_heading", 0),
        "locator_kind_counts": dict(sorted(counts.items())),
        "rows": sorted(rows, key=lambda row: (row["source_id"], row["evidence_anchor_id"])),
    }


def build_coverage_register(
    profiles: Sequence[Mapping[str, Any]],
    *,
    source_set: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the strict public CoverageRegister for every frozen inventory row."""

    source_set = source_set or {}
    profile_by_source = {str(row.get("source_id") or ""): row for row in profiles}
    profile_by_note = {str(row.get("note_id") or ""): row for row in profiles}
    records: list[dict[str, Any]] = []
    inventory_rows = [dict(row) for row in source_set.get("rows", []) or [] if isinstance(row, Mapping)]
    if inventory_rows:
        for item in inventory_rows:
            source_id = str(item.get("source_id") or "")
            note_id = str(item.get("note_id") or "")
            profile = profile_by_source.get(source_id) or profile_by_note.get(note_id)
            terminal_status = str(item.get("terminal_status") or "pending")
            exclusion_reason = str(
                (profile or {}).get("exclusion_reason")
                or ("source_processing_exhausted" if terminal_status == "exhausted" else "")
            )
            records.append(
                {
                    "source_id": source_id,
                    "title": str((profile or {}).get("title") or item.get("title") or source_id or note_id),
                    "zotero_key": str(item.get("zotero_item_key") or ""),
                    "terminal_state": terminal_status,
                    "exclusion_reason": exclusion_reason,
                    "attempted_route": [
                        str(value)
                        for value in item.get("attempted_route", []) or []
                        if str(value)
                    ],
                    "could_affect_existing_cluster": terminal_status in {"limited_note", "exhausted"},
                }
            )
    else:
        for profile in profiles:
            terminal_status = "validated_note" if profile.get("analytical") else "limited_note"
            records.append(
                {
                    "source_id": str(profile.get("source_id") or ""),
                    "title": str(profile.get("title") or profile.get("source_id") or ""),
                    "zotero_key": str(profile.get("zotero_item_key") or ""),
                    "terminal_state": terminal_status,
                    "exclusion_reason": str(profile.get("exclusion_reason") or ""),
                    "attempted_route": [],
                    "could_affect_existing_cluster": terminal_status == "limited_note",
                }
            )
    status_counts = Counter(str(row.get("terminal_state") or "pending") for row in records)
    counts = {
        "validated_note": status_counts.get("validated_note", 0),
        "limited_note": status_counts.get("limited_note", 0),
        "exhausted": status_counts.get("exhausted", 0),
        "partial": status_counts.get("partial", 0),
        "pending": status_counts.get("pending", 0),
    }
    status = (
        "partial"
        if counts["partial"] or counts["pending"]
        else "complete_with_exclusions"
        if counts["exhausted"]
        else "complete"
    )
    return {
        "source_set_id": str(source_set.get("source_set_id") or ""),
        "inventory_count": len(records),
        "counts": counts,
        "records": records,
        "status": status,
    }


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
    source_notes: Sequence[Mapping[str, Any]] = (),
    navigation_policy: Any = None,
    source_set: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure end-to-end mapper over already-built evidence profiles."""
    normalized = normalize_evidence_profiles(profiles)
    independence = build_independence_records(normalized)
    locator_audit = build_locator_audit(normalized)
    coverage_register = build_coverage_register(normalized, source_set=source_set)
    _notify_stage(stage_callback, "evidence_anchors")
    _notify_stage(stage_callback, "relation_mapping")
    relations = map_profile_relations(normalized)
    _notify_stage(stage_callback, "topic_neighborhoods")
    topic_neighborhoods = map_topic_neighborhoods(normalized, relations)
    _notify_stage(stage_callback, "proposition_mapping")
    propositions = build_literature_propositions(normalized)
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
            context={
                "propositions": propositions,
                "study_lineages": independence["study_lineages"],
                "evidence_base_groups": independence["evidence_base_groups"],
                "independence_assessments": independence["independence_assessments"],
            },
        )
        if len(analytical_families) >= 2
        else {}
    )
    clustered = map_overlapping_clusters(
        normalized,
        relations,
        policy=policy,
        proposals=list(proposal_response.get("clusters", []) or []),
        propositions=propositions,
        topic_neighborhoods=topic_neighborhoods,
    )
    _notify_stage(stage_callback, "evidence_matrices")
    admission_matrices = build_evidence_matrices(normalized, clustered["clusters"])
    admitted_cluster_ids = {
        str(row["cluster_id"]) for row in admission_matrices if row.get("admission_passed")
    }
    rejected_matrix_clusters = [
        {
            "proposal_id": str(cluster.get("proposal_id") or ""),
            "semantic_identity": str(cluster.get("semantic_identity") or ""),
            "source_ids": list(cluster.get("source_ids", []) or []),
            "action": "reject",
            "reason": "proposition_matrix_has_no_valid_multi_source_row",
        }
        for cluster in clustered["clusters"]
        if str(cluster["cluster_id"]) not in admitted_cluster_ids
    ]
    if rejected_matrix_clusters:
        clustered["rejected_proposals"] = [
            *clustered["rejected_proposals"],
            *rejected_matrix_clusters,
        ]
    registry = reconcile_cluster_registry(
        [cluster for cluster in clustered["clusters"] if str(cluster["cluster_id"]) in admitted_cluster_ids],
        previous_registry,
    )
    mapped_propositions = sorted(
        {
            str(proposition["proposition_id"]): dict(proposition)
            for cluster in registry["clusters"]
            for proposition in cluster.get("propositions", []) or []
            if str(proposition.get("proposition_id") or "")
        }.values(),
        key=lambda row: str(row["proposition_id"]),
    )
    _notify_stage(stage_callback, "support_validation")
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
                "study_lineages": [
                    row for row in independence["study_lineages"]
                    if set(str(value) for value in row.get("source_ids", []) or [])
                    & set(cluster.get("source_ids", []) or [])
                ],
                "evidence_base_groups": [
                    row for row in independence["evidence_base_groups"]
                    if set(row.get("source_ids", []) or []) & set(cluster.get("source_ids", []) or [])
                ],
                "required_source_contributions": _fallback_source_contributions(cluster, normalized),
            },
        )
        validated_synthesis = validate_cluster_synthesis(
            synthesis_response,
            cluster,
            normalized,
        )
        if validated_synthesis.get("status") == "partial" and reasoner_call is not None:
            repair_response = _reasoner_stage(
                reasoner,
                reasoner_call,
                stage="cluster_synthesis",
                key=f"{cluster_id}--repair",
                method_name="synthesize_cluster",
                profiles=member_profiles,
                request=request,
                context={
                    "cluster": cluster,
                    "evidence_matrix": matrix_by_cluster.get(cluster_id, {}),
                    "deterministic_debate": deterministic_debate_by_cluster.get(cluster_id, {}),
                    "all_cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
                    "repair_requirements": list(validated_synthesis.get("quality_errors", []) or []),
                    "previous_response": synthesis_response,
                    "required_source_contributions": _fallback_source_contributions(cluster, normalized),
                },
            )
            repaired_synthesis = validate_cluster_synthesis(
                repair_response,
                cluster,
                normalized,
            )
            repaired_synthesis["repair_attempted"] = True
            if (
                repaired_synthesis.get("status") == "reasoned"
                or len(repaired_synthesis.get("quality_errors", []) or [])
                < len(validated_synthesis.get("quality_errors", []) or [])
            ):
                validated_synthesis = repaired_synthesis
            else:
                validated_synthesis["repair_attempted"] = True
        cluster_syntheses[cluster_id] = validated_synthesis
    quantitative_comparisons = _quantitative_comparison_records(cluster_syntheses)
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
        if row.get("status") in {"answered_within_collection", "underspecified_gap"}
    ]
    visible_validated = [
        row
        for row in validated
        if row.get("status") not in {"answered_within_collection", "underspecified_gap"}
    ]
    _notify_stage(stage_callback, "resolution_paths")
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
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            cluster_id = str(cluster_id)
            if cluster_id in valid_cluster_ids:
                gap_ids_by_cluster[cluster_id].append(str(gap["gap_id"]))
    for cluster in registry["clusters"]:
        cluster["related_gap_ids"] = sorted(set(gap_ids_by_cluster.get(str(cluster["cluster_id"]), [])))
    navigation_profiles = _navigation_profile_rows([_as_mapping(profile) for profile in profiles], source_notes)
    if bool(_policy_value(navigation_policy, "subject_tags_enabled", True)):
        navigation = build_navigation_graph(
            navigation_profiles,
            propositions=mapped_propositions,
            max_candidates_per_source=int(
                _policy_value(navigation_policy, "max_candidate_tags_per_source", 24)
            ),
            max_visible_tags_per_source=int(
                _policy_value(navigation_policy, "max_visible_tags_per_source", 8)
            ),
            max_inferred_links_per_source=int(
                _policy_value(navigation_policy, "max_inferred_related_note_links", 8)
            ),
            minimum_neighborhood_sources=int(
                _policy_value(navigation_policy, "min_sources_per_neighborhood", 2)
            ),
        )
    else:
        typed_relations = build_typed_source_relations(
            navigation_profiles,
            propositions=mapped_propositions,
            max_inferred_links_per_source=int(
                _policy_value(navigation_policy, "max_inferred_related_note_links", 8)
            ),
        )
        navigation = {
            "tag_reconciliation_version": "1",
            "navigation_relation_version": "1",
            "neighborhood_promotion_version": "1",
            "subject_tags": [],
            "assignments": [],
            "candidates": [],
            "rejected_candidates": [],
            "candidate_count": 0,
            "promoted_subject_tag_count": 0,
            "rejected_generic_tag_count": 0,
            "unconfirmed_zotero_tag_count": 0,
            "topic_neighborhoods": [],
            "singleton_facets": [],
            "promoted_neighborhood_count": 0,
            "singleton_facet_count": 0,
            "typed_relations": typed_relations,
            "typed_relation_counts": dict(Counter(str(row.get("relation_type") or "") for row in typed_relations)),
            "graph_projection_hash": _stable_hash({"typed_relations": typed_relations, "subject_tags_enabled": False}),
        }
    _project_navigation_onto_map(
        navigation,
        navigation_profiles,
        registry["clusters"],
        gaps,
        policy=navigation_policy,
    )
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
                    "resolution_path": gap.get("resolution_path", {}),
                    "proposition_ids": gap.get("proposition_ids", []),
                    "originating_cluster_revisions": gap.get("originating_cluster_revisions", []),
                    "missing_cell": gap.get("missing_cell", {}),
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
        "mapper_version": "0.8.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "profile_count": len(normalized),
        "analytical_profile_count": sum(1 for row in normalized if row["analytical"]),
        "limited_profile_count": sum(1 for row in normalized if row["limited"]),
        "relation_count": len(navigation["typed_relations"]),
        "typed_relation_counts": dict(navigation.get("typed_relation_counts", {})),
        "subject_tag_count": int(navigation.get("promoted_subject_tag_count", 0) or 0),
        "subject_tag_assignment_count": len(navigation.get("assignments", []) or []),
        "topic_neighborhood_count": len(navigation["topic_neighborhoods"]),
        "singleton_facet_count": int(navigation.get("singleton_facet_count", 0) or 0),
        "graph_projection_hash": str(navigation.get("graph_projection_hash") or ""),
        "proposition_count": len(mapped_propositions),
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
        "gap_lead_count": sum(1 for row in gaps if row["status"] == "collection_gap_lead"),
        "synthesized_cluster_count": sum(
            1 for row in cluster_syntheses.values() if row.get("status") == "reasoned"
        ),
        "partial_cluster_synthesis_count": sum(
            1 for row in cluster_syntheses.values() if row.get("status") == "partial"
        ),
        "rejected_underspecified_gap_count": sum(
            1 for row in rejected_gaps if row.get("status") == "underspecified_gap"
        ),
        "rejected_gap_quality_count": sum(
            1 for row in rejected_gaps if row.get("quality_rejection_reasons")
        ),
        "merged_gap_count": len(gap_merge_ledger),
        "study_lineage_count": len(independence["study_lineages"]),
        "evidence_base_group_count": len(independence["evidence_base_groups"]),
        "independence_assessment_count": len(independence["independence_assessments"]),
        "cluster_source_contribution_count": sum(
            len(row.get("source_contributions", []) or []) for row in cluster_syntheses.values()
        ),
        "quantitative_comparison_count": len(quantitative_comparisons),
        "rejected_quantitative_comparison_count": sum(
            1 for row in quantitative_comparisons if row.get("status") == "rejected"
        ),
        "strong_locator_count": int(locator_audit.get("strong_locator_count", 0) or 0),
        "rejected_generated_locator_count": int(locator_audit.get("generated_note_heading_count", 0) or 0),
        "coverage_inventory_count": int(coverage_register.get("inventory_count", 0) or 0),
        "coverage_exhausted_count": int(_as_mapping(coverage_register.get("counts")).get("exhausted", 0) or 0),
        "coverage_accounting_valid": sum(
            int(_as_mapping(coverage_register.get("counts")).get(key, 0) or 0)
            for key in ("validated_note", "limited_note", "exhausted", "partial", "pending")
        )
        == int(coverage_register.get("inventory_count", 0) or 0),
    }
    packet = {
        "packet_kind": "literature_map",
        "mapper_version": "0.8.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
        "gap_ids": [row["gap_id"] for row in gaps],
        "counts": manifest,
        "not_method_ready_bundle": True,
        "not_manuscript_text": True,
    }
    partial_cluster_ids = sorted(
        cluster_id
        for cluster_id, synthesis in cluster_syntheses.items()
        if synthesis.get("status") == "partial"
    )
    if partial_cluster_ids:
        packet.update(
            {
                "status": "partial",
                "partial_reason": "incomplete_cluster_synthesis:" + ",".join(partial_cluster_ids),
                "partial_cluster_ids": partial_cluster_ids,
            }
        )
    else:
        packet["status"] = "complete"
    return {
        "manifest": manifest,
        "profiles": normalized,
        "relations": navigation["typed_relations"],
        "topic_neighborhoods": navigation["topic_neighborhoods"],
        "navigation": navigation,
        "propositions": mapped_propositions,
        "study_lineages": independence["study_lineages"],
        "independence_assessments": independence["independence_assessments"],
        "evidence_base_groups": independence["evidence_base_groups"],
        "cluster_registry": {
            **registry,
            "rejected_proposals": clustered["rejected_proposals"],
            "unclustered_sources": clustered["unclustered_sources"],
            "max_cluster_memberships": clustered["max_cluster_memberships"],
        },
        "evidence_matrices": matrices,
        "cluster_syntheses": cluster_syntheses,
        "cluster_source_contributions": {
            cluster_id: list(synthesis.get("source_contributions", []) or [])
            for cluster_id, synthesis in cluster_syntheses.items()
        },
        "quantitative_comparisons": quantitative_comparisons,
        "locator_audit": locator_audit,
        "coverage_register": coverage_register,
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
    return sorted({str(tag) for tag in cluster.get("subject_tags", []) or [] if str(tag)})


def _gap_obsidian_tags(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    cluster_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    del profile_by_source, cluster_by_id
    return sorted({str(tag) for tag in gap.get("subject_tags", []) or [] if str(tag)})


def _cluster_markdown_legacy(
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
            technical = str(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            ).strip()
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


def _cluster_markdown(
    cluster: Mapping[str, Any],
    matrix: Mapping[str, Any] | None,
    debate: Mapping[str, Any] | None,
    related_gaps: Sequence[Mapping[str, Any]] = (),
    *,
    synthesis: Mapping[str, Any] | None = None,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    neighborhood_index_note: str = "Literature Neighborhoods",
) -> str:
    """Render the human projection; full validation traces stay in YAML."""

    synthesis = synthesis or {}
    profile_by_source = profile_by_source or {}
    source_links = [_obsidian_note_link(row) for row in cluster.get("representative_sources", []) or []]
    frontmatter = {
        "type": "literature_cluster",
        "title": cluster_display_title(cluster),
        "aliases": [str(cluster["cluster_id"]), str(cluster.get("label") or "")],
        "cluster_id": cluster["cluster_id"],
        "revision_hash": cluster.get("revision_hash", ""),
        "status": cluster.get("status", ""),
        "qualification_status": cluster.get("qualification_status", ""),
        "debate_state": str((debate or {}).get("classification") or "no_debate"),
        "tags": _cluster_obsidian_tags(cluster),
        "sources": source_links,
        "related_gaps": [_gap_wikilink(gap) for gap in related_gaps],
    }
    sections: list[str] = [f"# {cluster_display_title(cluster)}"]

    def citation_text(values: Sequence[Mapping[str, Any]], *, limit: int = 6) -> str:
        citations: list[str] = []
        for reference in values:
            profile = profile_by_source.get(str(reference.get("source_id") or ""), reference)
            locator = str(reference.get("locator") or "").strip()
            citation = _obsidian_note_link(profile) + (f" — {locator}" if locator else "")
            if citation not in citations:
                citations.append(citation)
        return "; ".join(citations[:limit])

    question = _human_projection_text(cluster.get("shared_question") or "")
    verdict_rows = [
        _as_mapping(row)
        for row in synthesis.get("verdict_paragraphs", []) or []
        if isinstance(row, Mapping) and row.get("text")
    ]
    if not verdict_rows:
        verdict_rows = [
            {"text": assertion_text, "evidence": row.get("evidence", [])}
            for row in synthesis.get("central_findings", []) or []
            if isinstance(row, Mapping)
            and (assertion_text := _cluster_item_text(row))
        ]
    if verdict_rows and synthesis.get("status", "reasoned") == "reasoned":
        content: list[str] = []
        if question:
            content.append(f"**Question:** {question}")
        for row in verdict_rows:
            paragraph = re.sub(r"(?m)^#{1,6}\s+", "", _human_projection_text(row.get("text") or "")).strip()
            if not paragraph:
                continue
            paragraph_citations = citation_text(row.get("evidence", []) or [])
            content.append(paragraph + (f" — Sources: {paragraph_citations}" if paragraph_citations else ""))
        content.append(
            "**Evidence basis:** "
            f"{int(cluster.get('source_count', 0) or 0)} publications; "
            f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases."
        )
        sections.append("## Question and verdict\n\n" + "\n\n".join(content))
    elif question:
        sections.append("## Cluster question\n\n" + question)

    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
    }
    role_lines: list[str] = []
    for role in ("core", "context", "bridge"):
        members = [
            row
            for row in cluster.get("representative_sources", []) or []
            if role_by_source.get(str(row.get("source_id") or ""), str(row.get("cluster_role") or "context")) == role
        ]
        if not members:
            continue
        role_lines.append(f"**{role.title()} sources**")
        role_lines.extend(f"- {_obsidian_note_link(row)}" for row in members)
    if role_lines:
        sections.append("## Source roles\n\n" + "\n".join(role_lines))

    proposition_lines: list[str] = []
    for proposition in cluster.get("propositions", []) or []:
        statement = _human_projection_text(proposition.get("statement") or "")
        if not statement:
            continue
        proposition_lines.append(f"### {statement}")
        question_text = _human_projection_text(proposition.get("question") or "")
        if question_text and question_text != statement:
            proposition_lines.append(question_text)
        evidence = list(proposition.get("evidence", []) or [])
        citations = []
        for reference in evidence:
            profile = profile_by_source.get(str(reference.get("source_id") or ""), reference)
            citation = f"{_obsidian_note_link(profile)} — {reference.get('locator', '')}"
            if citation not in citations:
                citations.append(citation)
        if citations:
            proposition_lines.append("Evidence: " + "; ".join(citations[:5]))
    if proposition_lines:
        sections.append("## Comparable propositions\n\n" + "\n\n".join(proposition_lines))

    def assertion_text(row: Mapping[str, Any]) -> str:
        return _human_projection_text(
            row.get("assertion")
            or row.get("finding")
            or row.get("position")
            or row.get("agreement")
            or row.get("contradiction")
            or row.get("text")
            or row.get("summary")
            or ""
        )

    def render_assertions(values: Sequence[Mapping[str, Any]], *, plain_english: bool = False) -> str:
        lines: list[str] = []
        for row in values:
            text = assertion_text(row)
            if not text:
                continue
            citations = citation_text(row.get("evidence", []) or [])
            lines.append(f"- {text}" + (f" — Sources: {citations}" if citations else ""))
            plain = _human_projection_text(row.get("plain_english_meaning") or row.get("plain_english") or "")
            if plain_english and plain:
                lines.append(f"  - In plain English: {plain}")
            technical = _human_projection_text(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            )
            if technical:
                lines.append(f"  - Technical detail: {technical}")
        return "\n".join(lines)

    contribution_parts: list[str] = []
    contributions_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for contribution in synthesis.get("source_contributions", []) or []:
        if isinstance(contribution, Mapping) and contribution.get("source_id"):
            contributions_by_source[str(contribution["source_id"])].append(contribution)
    for source_id in cluster.get("source_ids", []) or []:
        source_id = str(source_id)
        rows = contributions_by_source.get(source_id, [])
        if not rows:
            continue
        profile = profile_by_source.get(source_id, {})
        role = role_by_source.get(source_id, "context")
        contribution_parts.append(f"### {_obsidian_note_link(profile)} — {role.title()}")
        for row in rows:
            text = assertion_text(row)
            if not text:
                continue
            comparison = str(row.get("comparison_status") or "source_specific_not_compared").replace("_", " ")
            citations = citation_text(row.get("evidence", []) or [])
            line = f"- {text}"
            if citations:
                line += f" — Source: {citations}"
            contribution_parts.append(line)
            plain = _human_projection_text(row.get("plain_english_meaning") or row.get("plain_english") or "")
            if plain:
                contribution_parts.append(f"  - In plain English: {plain}")
            technical = _human_projection_text(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            )
            if technical:
                contribution_parts.append(f"  - Technical detail: {technical}")
            contribution_parts.append(f"  - Comparative status: {comparison}.")
    if contribution_parts:
        sections.append(
            "## What each source contributes\n\n"
            "These findings are retained because they matter to the cluster. A source-specific contribution is not "
            "treated as cross-study agreement unless it also passes the proposition and independence gates.\n\n"
            + "\n".join(contribution_parts)
        )

    central = render_assertions(synthesis.get("central_findings", []) or [], plain_english=True)
    if central:
        sections.append("## Findings and interpretation\n\n" + central)

    debate_state = str((debate or {}).get("classification") or synthesis.get("debate_state") or "no_debate")
    relationship_explanations = {
        "mapped_consensus": "At least three effective evidence bases support a comparable conclusion.",
        "emerging_convergence": "Two effective evidence bases point in the same direction; this is convergence, not mature consensus.",
        "aligned_institutional_guidance": "Institutional guidance aligns, but recommendations are not independent effectiveness evidence.",
        "within_program_consistency": "Multiple publications from one evidence program are consistent; they count as one effective evidence base.",
        "conditional_relationship": "The relationship changes across identified cases, populations, periods, measures, or conditions.",
        "complementary_positions": "The sources answer different but compatible parts of the cluster question.",
        "parallel_literatures": "The propositions sit near each other but are not directly comparable.",
    }
    relationship_parts: list[str] = [
        f"**Mapped relationship:** {debate_state.replace('_', ' ')}."
        + (f" {relationship_explanations[debate_state]}" if debate_state in relationship_explanations else "")
    ]
    for title, key in (
        ("Agreements", "agreements"),
        ("Positions", "positions"),
        ("Contradictions", "contradictions"),
    ):
        rendered = render_assertions(synthesis.get(key, []) or [])
        if rendered:
            relationship_parts.append(f"### {title}\n\n{rendered}")
    if len(relationship_parts) > 1 or debate_state != "no_debate":
        sections.append("## Relationship among the findings\n\n" + "\n\n".join(relationship_parts))

    boundary_parts: list[str] = []
    boundaries = render_assertions(synthesis.get("boundary_conditions", []) or [])
    methods = render_assertions(synthesis.get("methodological_fault_lines", []) or [])
    if boundaries:
        boundary_parts.append("### Boundaries\n\n" + boundaries)
    if methods:
        boundary_parts.append("### Method and measurement differences\n\n" + methods)
    if boundary_parts:
        sections.append("## Why findings differ\n\n" + "\n\n".join(boundary_parts))

    neighboring = render_assertions(synthesis.get("related_clusters", []) or [])
    if neighboring:
        sections.append("## Neighboring clusters\n\n" + neighboring)

    neighborhood_lines: list[str] = []
    for neighborhood in cluster.get("topic_neighborhoods", []) or []:
        label = _human_projection_text(neighborhood.get("label") or "")
        facet = str(neighborhood.get("facet_type") or "subject").replace("_", " ")
        canonical_tag = str(neighborhood.get("canonical_tag") or "")
        member_count = int(neighborhood.get("cluster_member_count", 0) or 0)
        if not label:
            continue
        tag_link = f" #{canonical_tag}" if canonical_tag else ""
        heading = f"{facet.title()} neighborhoods"
        neighborhood_link = (
            f"[[{neighborhood_index_note}#{heading}|{facet.title()}: {label}]]"
        )
        neighborhood_lines.append(
            f"- **{neighborhood_link}** — shared by {member_count} core sources in this cluster.{tag_link}"
        )
    if neighborhood_lines:
        sections.append(
            "## Related literature\n\n"
            "These are retrieval paths shared by multiple core sources. They help you browse the vault; "
            "they did not determine admission to this analytical cluster.\n\n"
            + "\n".join(neighborhood_lines)
        )

    gap_lines = []
    for gap in related_gaps:
        statement = _human_projection_text(gap.get("gap_statement") or gap.get("precise_missing_evidence") or "")
        gap_lines.append(f"- {_gap_wikilink(gap)}" + (f" — {statement}" if statement else ""))
    if gap_lines:
        sections.append("## Collection gaps\n\n" + "\n".join(gap_lines))

    matrix_rows = list((matrix or {}).get("propositions", []) or [])
    if matrix_rows:
        table = ["| Proposition | Core-source findings |", "|---|---|"]
        for proposition in matrix_rows:
            statement = re.sub(r"\s+", " ", _human_projection_text(proposition.get("statement") or "")).strip()
            cells = []
            for source_id, cell in _as_mapping(proposition.get("cells")).items():
                profile = profile_by_source.get(str(source_id), {})
                source_label = str(profile.get("title") or source_id)
                finding = re.sub(r"\s+", " ", _human_projection_text(cell.get("stance_or_finding") or "")).strip()
                cells.append(f"{source_label}: {finding}")
            table.append(
                "| "
                + statement.replace("|", "\\|")
                + " | "
                + "<br>".join(cells).replace("|", "\\|")
                + " |"
            )
        sections.append("## Proposition matrix\n\n" + "\n".join(table))

    source_index = [f"- {_obsidian_note_link(row)}" for row in cluster.get("representative_sources", []) or []]
    if source_index:
        sections.append("## Source index\n\n" + "\n".join(source_index))
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def _gap_markdown_legacy(
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


def _gap_markdown(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    cluster_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a collection-scoped rationale without exposing audit machinery."""

    profile_by_source = profile_by_source or {}
    cluster_by_id = cluster_by_id or {}
    related_clusters = [
        cluster_by_id[str(cluster_id)]
        for cluster_id in gap.get("related_cluster_ids", []) or []
        if str(cluster_id) in cluster_by_id
    ]
    source_ids = {
        str(reference.get("source_id") or "")
        for field in ("supporting_evidence", "countervailing_evidence")
        for reference in gap.get(field, []) or []
        if reference.get("source_id")
    }
    source_links = [
        _obsidian_note_link(profile_by_source[source_id])
        for source_id in sorted(source_ids)
        if source_id in profile_by_source
    ]
    frontmatter = {
        "type": "literature_gap",
        "title": gap_display_title(gap),
        "aliases": [str(gap["gap_id"]), gap_display_title(gap).removeprefix("Gap: ")],
        "gap_id": gap["gap_id"],
        "rule": gap.get("rule", ""),
        "status": gap.get("status", ""),
        "scope": "collection_only",
        "promoted": bool(gap.get("promoted", False)),
        "novelty_claimed": False,
        "related_clusters": [_cluster_wikilink(cluster) for cluster in related_clusters],
        "sources": source_links,
        "tags": _gap_obsidian_tags(gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id),
    }
    sections: list[str] = [f"# {gap_display_title(gap)}"]

    statement = _human_projection_text(gap.get("gap_statement") or gap.get("precise_missing_evidence") or "")
    if statement:
        sections.append("## Gap statement\n\n" + statement)
    sections.append(
        "## Collection status\n\n"
        f"This is a **{str(gap.get('status') or '').replace('_', ' ')}** inside the frozen collection. "
        "It is not a claim that the wider literature contains no answer."
    )

    lineage_lines = [f"- Related cluster: {_cluster_wikilink(cluster)}" for cluster in related_clusters]
    proposition_ids = {str(value) for value in gap.get("proposition_ids", []) or []}
    for cluster in related_clusters:
        for proposition in cluster.get("propositions", []) or []:
            if str(proposition.get("proposition_id") or "") in proposition_ids:
                lineage_lines.append(
                    f"- Originating proposition: {_human_projection_text(proposition.get('statement', ''))}"
                )
    missing_cell = _as_mapping(gap.get("missing_cell"))
    if missing_cell.get("description"):
        lineage_lines.append(
            f"- Missing evidence-matrix cell: {_human_projection_text(missing_cell.get('description'))}"
        )
    if lineage_lines:
        sections.append("## Where the gap came from\n\n" + "\n".join(lineage_lines))

    generation = _human_projection_text(gap.get("generation_explanation") or "")
    observed = _human_projection_text(gap.get("observed_pattern") or "")
    if generation or observed:
        parts = []
        if generation:
            parts.append(generation)
        if observed:
            parts.append("**Observed collection pattern:** " + observed)
        sections.append("## Why the mapper raised it\n\n" + "\n\n".join(parts))

    def evidence_lines(values: Sequence[Mapping[str, Any]]) -> list[str]:
        lines: list[str] = []
        for reference in values:
            source_id = str(reference.get("source_id") or "")
            profile = profile_by_source.get(source_id, reference)
            anchor_id = str(reference.get("evidence_anchor_id") or reference.get("claim_id") or "")
            anchor = next(
                (
                    row
                    for row in profile.get("claims", []) or []
                    if str(row.get("evidence_anchor_id") or row.get("claim_id") or "") == anchor_id
                ),
                {},
            )
            claim_text = _human_projection_text(anchor.get("text") or "")
            line = f"- {_obsidian_note_link(profile)} — {reference.get('locator', '')}"
            if claim_text:
                line += f": {claim_text}"
            lines.append(line)
        return lines

    support = evidence_lines(gap.get("supporting_evidence", []) or [])
    if support:
        sections.append("## Evidence that reveals the gap\n\n" + "\n".join(support))

    search_parts: list[str] = []
    search_summary = _human_projection_text(gap.get("internal_search_summary") or "")
    if search_summary:
        search_parts.append(search_summary)
    closest = list(gap.get("closest_prior_work", []) or [])
    if closest:
        search_parts.append(
            "**Closest collection evidence**\n\n"
            + "\n".join(
                f"- {_obsidian_note_link(row)} — {_human_projection_text(row.get('overlap_explanation', ''))}"
                for row in closest[:5]
            )
        )
    closest_explanation = _human_projection_text(gap.get("closest_prior_explanation") or "")
    if closest_explanation:
        search_parts.append("**Why it does not fully answer the gap:** " + closest_explanation)
    if search_parts:
        sections.append("## Collection-wide falsification\n\n" + "\n\n".join(search_parts))

    counter = evidence_lines(gap.get("countervailing_evidence", []) or [])
    limited = [
        row
        for row in gap.get("warnings", []) or []
        if row.get("warning") == "possible_counterevidence_requires_full_text"
    ]
    counter_parts: list[str] = []
    if counter:
        counter_parts.append("**Counterevidence**\n\n" + "\n".join(counter))
    if limited:
        counter_parts.append(
            "**Possible counterevidence requiring full text**\n\n"
            + "\n".join(
                f"- {_obsidian_note_link(profile_by_source.get(str(row.get('source_id') or ''), row))}"
                for row in limited
            )
        )
    if counter_parts:
        sections.append("## Counterevidence and limits\n\n" + "\n\n".join(counter_parts))

    decision = _human_projection_text(gap.get("decision_reasoning") or "")
    if decision:
        sections.append("## Why it survived or was narrowed\n\n" + decision)

    resolution = _as_mapping(gap.get("resolution_path"))
    if resolution:
        path_lines = [
            f"**Approach:** {str(resolution.get('path_type') or '').replace('_', ' ')}",
            f"**Question:** {resolution.get('question', '')}",
            f"**Evidence needed:** {resolution.get('evidence_needed', '')}",
        ]
        requirements = _as_mapping(resolution.get("requirements"))
        if requirements:
            path_lines.append("**Requirements:**")
            path_lines.extend(
                f"- {str(key).replace('_', ' ').title()}: {'; '.join(_flatten_values(value))}"
                for key, value in requirements.items()
                if _flatten_values(value)
            )
        if resolution.get("feasibility"):
            path_lines.append(f"**Feasibility:** {resolution.get('feasibility')}")
        sections.append("## A path to resolving it\n\n" + "\n\n".join(path_lines))

    final_parts = []
    if gap.get("why_matters"):
        final_parts.append("**Why it matters:** " + str(gap["why_matters"]))
    if gap.get("contribution"):
        final_parts.append("**Possible contribution:** " + str(gap["contribution"]))
    ranking = _as_mapping(gap.get("ranking"))
    if ranking.get("confidence_tier"):
        final_parts.append("**Collection-level confidence:** " + str(ranking["confidence_tier"]))
    if final_parts:
        sections.append("## Significance\n\n" + "\n\n".join(final_parts))
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def _map_verdict_excerpt(value: Any, *, sentence_limit: int = 3, character_limit: int = 900) -> str:
    human_text = _human_projection_text(value)
    human_text = re.sub(
        r"(?im)^\s{0,3}#{1,6}\s+(?:cluster\s+)?(?:synthesis|verdict)\s*$",
        "",
        human_text,
    )
    text = re.sub(r"\s+", " ", human_text).strip()
    if not text:
        return ""
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]
    excerpt = " ".join(sentences[:sentence_limit])
    if len(excerpt) <= character_limit:
        return excerpt
    shortened = excerpt[:character_limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened + "."


def _map_unclustered_reason_label(value: Any) -> str:
    reason = str(value or "").strip()
    normalized = reason.casefold()
    if reason == "no_comparable_multi_source_proposition":
        return "No comparable proposition shared with another core study"
    if reason == "no_comparable_independent_evidence_base_proposition":
        return "Comparable publications do not provide two effective evidence bases"
    if normalized.startswith("coverage classification:"):
        return "Limited source coverage"
    if "membership" in normalized and "limit" in normalized:
        return "Analytical-cluster membership limit"
    return reason.replace("_", " ").strip().capitalize() or "No admission reason recorded"


def _literature_neighborhoods_markdown(
    report: Mapping[str, Any],
    source_set: Mapping[str, Any],
) -> str:
    navigation = _as_mapping(report.get("navigation"))
    summaries = [
        _as_mapping(row)
        for row in navigation.get("human_neighborhood_summaries", []) or []
        if isinstance(row, Mapping)
    ]
    neighborhood_by_id = {
        str(row.get("topic_neighborhood_id") or ""): _as_mapping(row)
        for row in navigation.get("topic_neighborhoods", []) or []
        if isinstance(row, Mapping)
    }
    profile_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if isinstance(row, Mapping)
    }
    cluster_by_id = {
        str(row.get("cluster_id") or ""): row
        for row in _as_mapping(report.get("cluster_registry")).get("clusters", []) or []
        if isinstance(row, Mapping) and row.get("cluster_id")
    }
    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    title = f"Literature Neighborhoods - {collection_name}"
    sections = [
        f"# {title}",
        (
            "## How to use this index\n\n"
            "Neighborhoods are collection-native browsing routes. They group sources that share a discriminative "
            "concept, mechanism, outcome, method, case, or explicit relation. They do not establish agreement, a "
            "debate, or a gap; those judgments belong to analytical cluster notes."
        ),
    ]
    if summaries:
        by_facet: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in summaries:
            detail = neighborhood_by_id.get(str(row.get("neighborhood_id") or ""), {})
            row = {**detail, **row}
            by_facet[str(row.get("facet_type") or row.get("kind") or "subject")].append(row)
        for facet, rows in sorted(by_facet.items()):
            lines: list[str] = []
            for row in sorted(rows, key=lambda value: str(value.get("label") or "").casefold()):
                label = _human_projection_text(row.get("label") or row.get("canonical_tag") or "Neighborhood")
                explanation = _human_projection_text(
                    row.get("explanation") or row.get("summary") or row.get("why_useful") or ""
                )
                source_ids = [str(value) for value in row.get("source_ids", []) or []]
                links = [
                    _obsidian_note_link(profile_by_source[source_id])
                    for source_id in source_ids
                    if source_id in profile_by_source
                ]
                lines.append(f"### {label}")
                if explanation:
                    lines.append(explanation)
                if links:
                    lines.append("Sources: " + "; ".join(links))
                cluster_links = [
                    _cluster_wikilink(cluster_by_id[cluster_id])
                    for cluster_id in row.get("related_cluster_ids", []) or []
                    if cluster_id in cluster_by_id
                ]
                if cluster_links:
                    lines.append("Analytical clusters: " + "; ".join(cluster_links))
            if lines:
                sections.append(f"## {facet.replace('_', ' ').title()} neighborhoods\n\n" + "\n\n".join(lines))
    else:
        sections.append(
            "## Active neighborhoods\n\n"
            "No repeated, discriminative collection-native neighborhood met the promotion threshold. "
            "Source-local facets remain searchable in the machine audit without crowding the Obsidian graph."
        )
    metrics = _as_mapping(navigation.get("navigation_metrics"))
    if metrics:
        sections.append(
            "## Vocabulary health\n\n"
            f"- Active vocabulary: {int(metrics.get('active_tag_concept_count', 0) or 0)}\n"
            f"- Source-local singleton facets: {int(metrics.get('source_local_singleton_tag_count', 0) or 0)}\n"
            f"- Unresolved reconciliation proposals: {int(metrics.get('unresolved_reconciliation_count', 0) or 0)}"
        )
    return _markdown_with_frontmatter(
        {"type": "literature_neighborhood_index", "title": title, "scope": "collection_only", "tags": []},
        "\n\n".join(sections),
    )


def _literature_map_markdown(
    report: Mapping[str, Any],
    source_set: Mapping[str, Any],
    *,
    map_id: str,
) -> str:
    """Render a collection-level intellectual overview without another model call."""

    manifest = _as_mapping(report.get("manifest"))
    cluster_registry = _as_mapping(report.get("cluster_registry"))
    clusters = list(cluster_registry.get("clusters", []) or [])
    unclustered = list(cluster_registry.get("unclustered_sources", []) or [])
    syntheses = _as_mapping(report.get("cluster_syntheses"))
    debates = {
        str(row.get("cluster_id") or ""): row
        for row in _as_mapping(report.get("debate_registry")).get("assessments", []) or []
        if isinstance(row, Mapping)
    }
    gaps = list(_as_mapping(report.get("gap_registry")).get("gaps", []) or [])
    coverage_register = _as_mapping(report.get("coverage_register"))
    coverage_counts = _as_mapping(coverage_register.get("counts"))
    profile_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    analytical_source_ids = {
        source_id for source_id, profile in profile_by_source.items() if profile.get("analytical")
    }
    clustered_analytical_source_ids = {
        str(source_id)
        for cluster in clusters
        for source_id in cluster.get("source_ids", []) or []
        if str(source_id) in analytical_source_ids
    }
    partial_cluster_ids = [
        str(cluster.get("cluster_id") or "")
        for cluster in clusters
        if _as_mapping(syntheses.get(str(cluster.get("cluster_id") or ""))).get("status") == "partial"
    ]
    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    map_title = f"Literature Map - {collection_name}"
    frontmatter = {
        "type": "literature_map_index",
        "title": map_title,
        "map_id": map_id,
        "source_set_id": str(source_set.get("source_set_id") or ""),
        "scope": "collection_only",
        "status": "partial" if partial_cluster_ids else "complete",
        "tags": [],
    }
    sections = [
        f"# {map_title}",
        (
            "## What this map does\n\n"
            "This is the collection-level overview of the frozen Zotero source set. Atomic notes analyze individual "
            "sources; cluster notes compare sources that address the same propositions; this map shows how those "
            "clusters, relationships, and collection-relative gaps fit together. It does not make a claim about the "
            "complete published literature."
        ),
    ]

    analytical_count = int(manifest.get("analytical_profile_count", 0) or 0)
    limited_count = int(manifest.get("limited_profile_count", 0) or 0)
    inventory_count = int(coverage_register.get("inventory_count", analytical_count + limited_count) or 0)
    exhausted_count = int(coverage_counts.get("exhausted", 0) or 0)
    partial_count = int(coverage_counts.get("partial", 0) or 0)
    pending_count = int(coverage_counts.get("pending", 0) or 0)
    cluster_count = len(clusters)
    clustered_count = len(clustered_analytical_source_ids)
    collection_verdict = (
        f"The frozen collection contains {inventory_count} inventoried items: {analytical_count} analytical profiles, "
        f"{limited_count} limited profiles, and {exhausted_count} exhausted sources. "
        f"The mapper admitted {cluster_count} analytical cluster{'s' if cluster_count != 1 else ''}; "
        f"{clustered_count} analytical source{'s' if clustered_count != 1 else ''} contribute to at least one admitted cluster. "
    )
    if cluster_count:
        collection_verdict += (
            "The remaining analytical sources stay searchable but are not treated as evidence for a shared debate "
            "unless they address a comparable multi-source proposition."
        )
    else:
        collection_verdict += (
            "No multi-source proposition met the admission threshold, so the collection currently has no defensible "
            "analytical cluster."
        )
    sections.append("## Collection verdict\n\n" + collection_verdict)

    cluster_cards: list[str] = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        synthesis = _as_mapping(syntheses.get(cluster_id))
        debate = _as_mapping(debates.get(cluster_id))
        question = _human_projection_text(cluster.get("shared_question") or "")
        verdict = _map_verdict_excerpt(synthesis.get("synthesis")) if synthesis.get("status") == "reasoned" else ""
        role_by_source = {
            str(row.get("source_id") or ""): str(row.get("role") or "")
            for row in cluster.get("source_roles", []) or []
            if isinstance(row, Mapping)
        }
        core_count = sum(1 for role in role_by_source.values() if role == "core")
        cluster_cards.append(f"### {_cluster_wikilink(cluster)}")
        if question:
            cluster_cards.append(f"**Question:** {question}")
        cluster_cards.append(
            f"**Verdict:** {verdict}"
            if verdict
            else "**Verdict:** The cluster synthesis is incomplete and remains resumable."
        )
        cluster_cards.append(
            f"- Evidence base: {core_count} core sources; "
            f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases"
        )
        cluster_cards.append(
            "- Relationship among findings: "
            + str(debate.get("classification") or synthesis.get("debate_state") or "no_debate").replace("_", " ")
        )
        cluster_cards.append(
            f"- Source-specific contributions retained: {len(synthesis.get('source_contributions', []) or [])}"
        )
        related_gap_count = sum(
            1 for gap in gaps if cluster_id in {str(value) for value in gap.get("related_cluster_ids", []) or []}
        )
        cluster_cards.append(f"- Linked collection gaps: {related_gap_count}")
    sections.append(
        "## Clusters at a glance\n\n"
        + ("\n\n".join(cluster_cards) if cluster_cards else "No analytical clusters were admitted in this map.")
    )

    relationship_lines: list[str] = []
    cluster_by_id = {str(cluster.get("cluster_id") or ""): cluster for cluster in clusters}
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        synthesis = _as_mapping(syntheses.get(cluster_id))
        for relationship in synthesis.get("related_clusters", []) or []:
            if not isinstance(relationship, Mapping):
                continue
            text = _human_projection_text(
                relationship.get("relationship")
                or relationship.get("assertion")
                or relationship.get("summary")
                or relationship.get("text")
                or ""
            )
            target_id = str(relationship.get("cluster_id") or relationship.get("related_cluster_id") or "")
            target = cluster_by_id.get(target_id)
            if text:
                target_text = f" and {_cluster_wikilink(target)}" if target is not None else ""
                relationship_lines.append(f"- {_cluster_wikilink(cluster)}{target_text}: {text}")
    for left, right in combinations(clusters, 2):
        shared = {str(value) for value in left.get("source_ids", []) or []} & {
            str(value) for value in right.get("source_ids", []) or []
        }
        if shared:
            relationship_lines.append(
                f"- {_cluster_wikilink(left)} and {_cluster_wikilink(right)} share "
                f"{len(shared)} source{'s' if len(shared) != 1 else ''}."
            )
    for gap in gaps:
        related_ids = [
            str(value) for value in gap.get("related_cluster_ids", []) or [] if str(value) in cluster_by_id
        ]
        if len(set(related_ids)) > 1:
            relationship_lines.append(
                f"- {_gap_wikilink(gap)} connects "
                + ", ".join(_cluster_wikilink(cluster_by_id[cluster_id]) for cluster_id in sorted(set(related_ids)))
                + "."
            )
    relationship_lines = list(dict.fromkeys(relationship_lines))
    if not relationship_lines:
        relationship_lines.append(
            "No evidence-backed cross-cluster relationship was established. The admitted clusters should therefore "
            "be read as separate evidence domains rather than as one combined debate."
        )
    sections.append("## How the clusters relate\n\n" + "\n".join(relationship_lines))

    gap_lines: list[str] = []
    for gap in gaps:
        statement = _human_projection_text(gap.get("gap_statement") or gap.get("precise_missing_evidence") or "")
        status = str(gap.get("status") or "collection_gap_lead").replace("_", " ")
        gap_lines.append(f"- {_gap_wikilink(gap)} — {status}" + (f": {statement}" if statement else ""))
    if not gap_lines:
        gap_lines.append(
            "No gap survived the collection-wide specificity, non-obviousness, worth, and internal-falsification gates. "
            "This means no defensible collection-relative gap was established, not that the wider literature has no gaps."
        )
    sections.append("## Collection gaps\n\n" + "\n".join(gap_lines))

    reason_counts = Counter(
        _map_unclustered_reason_label(row.get("reason"))
        for row in unclustered
        if isinstance(row, Mapping)
    )
    coverage_lines = [
        f"- Frozen inventory: {inventory_count}",
        f"- Analytical profiles: {analytical_count}",
        f"- Limited profiles: {limited_count}",
        f"- Exhausted sources: {exhausted_count}",
        f"- Partial sources: {partial_count}",
        f"- Pending sources: {pending_count}",
        f"- Analytical sources represented in admitted clusters: {clustered_count}",
        f"- Unclustered sources: {len(unclustered)}",
        f"- Accounting valid: {'yes' if sum(int(coverage_counts.get(key, 0) or 0) for key in ('validated_note', 'limited_note', 'exhausted', 'partial', 'pending')) == inventory_count else 'no'}",
    ]
    exhausted_rows = [
        row
        for row in coverage_register.get("records", []) or []
        if isinstance(row, Mapping) and row.get("terminal_state") == "exhausted"
    ]
    if exhausted_rows:
        coverage_lines.extend(["", "**Exhausted-source register**"])
        coverage_lines.extend(
            f"- `{row.get('zotero_key') or row.get('source_id') or 'unknown item'}` — "
            f"{row.get('exclusion_reason') or 'source processing exhausted'}"
            for row in exhausted_rows
        )
    if reason_counts:
        coverage_lines.extend(["", "**Why sources remain outside analytical clusters**"])
        coverage_lines.extend(
            f"- {reason}: {count}" for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        )
    coverage_lines.extend(
        [
            "",
            "Unclustered does not mean irrelevant. It means a source did not provide comparable evidence for an admitted "
            "multi-source proposition in this frozen collection. Limited sources remain searchable but cannot establish "
            "substantive findings, debates, or gaps without adequate full text.",
        ]
    )
    sections.append("## Coverage and limits\n\n" + "\n".join(coverage_lines))
    sections.append(
        "## Tags and literature neighborhoods\n\n"
        "Subject tags are collection-native retrieval labels derived from the existing source profiles, such as "
        "`mechanism/mediator-legitimacy`, `outcome/mediation-success`, or `case/syria`. They let Obsidian connect "
        "notes through a shared concept, mechanism, outcome, method, or case.\n\n"
        "A literature neighborhood is promoted only when at least two independent analytical sources share the same "
        "typed subject tag. A neighborhood is a browsing aid, not an analytical cluster: it cannot establish a debate, "
        "admit a cluster, or answer a gap. Single-source facets remain source-local search metadata and do not become "
        "native graph tags by default. Open [[" + literature_neighborhoods_note_stem(source_set) + "|the neighborhood index]] "
        "to browse the promoted routes."
    )
    sections.append(
        "## Navigate\n\n"
        "- [[clusters/INDEX|Cluster Index]] — concise navigation to the admitted clusters\n"
        "- [[gaps/INDEX|Gap Registry Index]] — collection-relative gaps and leads\n"
        f"- [[{literature_neighborhoods_note_stem(source_set)}|Literature Neighborhoods]] — discriminative retrieval routes\n"
        "- [[02_source_memory/indexes/INDEX|Source Index]] — every generated source note"
    )
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def stable_literature_map_id(source_set: Mapping[str, Any], question: str | None = None) -> str:
    """Identify a map by its stable source-set alias, never by a mutable snapshot."""
    del question  # A question is a projection lens, not part of collection-map identity.
    source_set_alias = str(source_set.get("source_set_alias") or source_set.get("source_set_id") or "source-set")
    identity = {"source_set_alias": source_set_alias}
    return f"literature-map-{slugify(source_set_alias)}-{_stable_hash(identity)[:12]}"


def literature_map_note_stem(source_set: Mapping[str, Any], map_id: str) -> str:
    """Human-first filename with the stable map identity retained."""

    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    label = safe_filename(collection_name, fallback="Collection")[:100].rstrip(" .-")
    return f"Literature Map - {label} [{map_id}]"


def literature_neighborhoods_note_stem(source_set: Mapping[str, Any]) -> str:
    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    label = safe_filename(collection_name, fallback="Collection")[:100].rstrip(" .-")
    return f"Literature Neighborhoods - {label}"


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


def _projection_without_volatile_timestamps(value: Any) -> Any:
    """Return projection content without recursively generated timestamps."""

    if isinstance(value, Mapping):
        return {
            str(key): _projection_without_volatile_timestamps(child)
            for key, child in value.items()
            if str(key) not in {"created_at", "updated_at"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_projection_without_volatile_timestamps(child) for child in value]
    return value


def _write_projection_yaml(path: Path, value: Any) -> None:
    """Avoid rewriting a generated projection when only timestamps changed."""

    existing = read_yaml(path, None)
    if existing is not None:
        existing_projection = yaml.safe_dump(
            _projection_without_volatile_timestamps(existing),
            sort_keys=False,
            allow_unicode=True,
        )
        requested_projection = yaml.safe_dump(
            _projection_without_volatile_timestamps(value),
            sort_keys=False,
            allow_unicode=True,
        )
        if existing_projection == requested_projection:
            return
        # Some live payloads contain string-like model values that compare
        # differently in memory but serialize to the same YAML scalar. Compare
        # the exact emitted form as a final guard for top-level run timestamps.
        existing_text = path.read_text(encoding="utf-8")
        requested_text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
        timestamp_line = re.compile(r"^(created_at|updated_at):[^\n]*(?:\n|$)", re.MULTILINE)
        if timestamp_line.sub(r"\1: <volatile-timestamp>\n", existing_text) == timestamp_line.sub(
            r"\1: <volatile-timestamp>\n", requested_text
        ):
            return
    write_yaml(path, value)


def _preserve_existing_projection_fields(
    path: Path,
    value: Mapping[str, Any],
    fields: Sequence[str],
) -> dict[str, Any]:
    """Carry forward post-render lineage until its owning stage refreshes it."""

    projected = dict(value)
    existing = read_yaml(path, {}) or {}
    if not isinstance(existing, Mapping):
        return projected
    for field in fields:
        if field not in projected and field in existing:
            projected[field] = existing[field]
    return projected


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
    # Generated literature artifacts are immutable for unchanged semantic
    # inputs. Route all YAML writes in this function through the projection
    # writer so a replay cannot churn hashes solely by refreshing timestamps.
    write_yaml = _write_projection_yaml
    root = workspace / "03_literature_synthesis"
    cluster_root = root / "clusters"
    gap_root = root / "gaps"
    gap_candidates_root = gap_root / "candidates"
    prior_root = root / "closest_prior_work"
    packet_root = root / "packets"
    map_id = map_id or stable_literature_map_id(source_set, question)
    neighborhoods_note_stem = literature_neighborhoods_note_stem(source_set)
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
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            cluster_id = str(cluster_id)
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
    neighborhood_path = root / "topic_neighborhoods.yml"
    subject_tag_registry_path = root / "subject_tag_registry.yml"
    subject_tag_assignments_path = root / "subject_tag_assignments.yml"
    typed_relations_path = root / "typed_source_relations.yml"
    navigation_audit_path = root / "navigation_audit.yml"
    source_index_root = workspace / "02_source_memory" / "indexes"
    compatibility_subject_tag_registry_path = source_index_root / "subject_tag_registry.yml"
    compatibility_subject_tag_assignments_path = source_index_root / "subject_tag_assignments.yml"
    compatibility_typed_links_path = source_index_root / "typed_links.yml"
    compatibility_typed_note_links_path = source_index_root / "typed_note_links.yml"
    proposition_path = root / "propositions.yml"
    debate_path = root / "debate_registry.yml"
    synthesis_path = root / "cluster_syntheses.yml"
    study_lineage_path = root / "study_lineage_registry.yml"
    independence_path = root / "independence_assessments.yml"
    source_contributions_path = root / "cluster_source_contributions.yml"
    quantitative_path = root / "quantitative_comparisons.yml"
    locator_audit_path = root / "locator_audit.yml"
    coverage_register_path = root / "coverage_register.yml"
    tag_concept_registry_path = root / "tag_concept_registry.yml"
    write_yaml(matrix_path, {"updated_at": generated_at, "matrices": report["evidence_matrices"]})
    write_yaml(
        neighborhood_path,
        {"updated_at": generated_at, "topic_neighborhoods": report.get("topic_neighborhoods", [])},
    )
    navigation = _as_mapping(report.get("navigation"))
    tag_registry_payload = {
        "updated_at": generated_at,
        "tag_reconciliation_version": navigation.get("tag_reconciliation_version", "1"),
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "subject_tags": navigation.get("subject_tags", []),
    }
    tag_assignments_payload = {
        "updated_at": generated_at,
        "tag_reconciliation_version": navigation.get("tag_reconciliation_version", "1"),
        "assignments": navigation.get("assignments", []),
    }
    typed_relations_payload = {
        "updated_at": generated_at,
        "navigation_relation_version": navigation.get("navigation_relation_version", "1"),
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "relation_counts": navigation.get("typed_relation_counts", {}),
        "relations": navigation.get("typed_relations", []),
        "links": navigation.get("typed_relations", []),
    }
    navigation_audit_payload = {
        "updated_at": generated_at,
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "candidate_count": navigation.get("candidate_count", 0),
        "rejected_generic_tag_count": navigation.get("rejected_generic_tag_count", 0),
        "unconfirmed_zotero_tag_count": navigation.get("unconfirmed_zotero_tag_count", 0),
        "singleton_facet_count": navigation.get("singleton_facet_count", 0),
        "candidates": navigation.get("candidates", []),
        "rejected_candidates": navigation.get("rejected_candidates", []),
        "singleton_facets": navigation.get("singleton_facets", []),
    }
    for path, payload in (
        (subject_tag_registry_path, tag_registry_payload),
        (compatibility_subject_tag_registry_path, tag_registry_payload),
        (subject_tag_assignments_path, tag_assignments_payload),
        (compatibility_subject_tag_assignments_path, tag_assignments_payload),
        (typed_relations_path, typed_relations_payload),
        (compatibility_typed_links_path, typed_relations_payload),
        (compatibility_typed_note_links_path, typed_relations_payload),
        (navigation_audit_path, navigation_audit_payload),
    ):
        write_yaml(path, payload)
    write_yaml(proposition_path, {"updated_at": generated_at, "propositions": report.get("propositions", [])})
    write_yaml(debate_path, {"updated_at": generated_at, **dict(report["debate_registry"])})
    write_yaml(synthesis_path, {"updated_at": generated_at, "syntheses": synthesis_by_cluster})
    write_yaml(
        study_lineage_path,
        {
            "updated_at": generated_at,
            "version": STUDY_LINEAGE_VERSION,
            "study_lineages": report.get("study_lineages", []),
            "evidence_base_groups": report.get("evidence_base_groups", []),
        },
    )
    write_yaml(
        independence_path,
        {
            "updated_at": generated_at,
            "version": INDEPENDENCE_ALGORITHM_VERSION,
            "assessments": report.get("independence_assessments", []),
        },
    )
    write_yaml(
        source_contributions_path,
        {"updated_at": generated_at, "clusters": report.get("cluster_source_contributions", {})},
    )
    write_yaml(
        quantitative_path,
        {"updated_at": generated_at, "comparisons": report.get("quantitative_comparisons", [])},
    )
    write_yaml(locator_audit_path, {"updated_at": generated_at, **dict(report.get("locator_audit", {}))})
    write_yaml(
        coverage_register_path,
        dict(report.get("coverage_register", {})),
    )
    write_yaml(
        tag_concept_registry_path,
        {
            "updated_at": generated_at,
            "version": navigation.get("tag_reconciliation_version", "2"),
            "concepts": navigation.get("tag_concept_registry", []),
            "reconciliation_proposals": navigation.get("tag_reconciliation_proposals", []),
        },
    )
    paths.extend(
        (
            matrix_path,
            neighborhood_path,
            subject_tag_registry_path,
            subject_tag_assignments_path,
            typed_relations_path,
            navigation_audit_path,
            compatibility_subject_tag_registry_path,
            compatibility_subject_tag_assignments_path,
            compatibility_typed_links_path,
            compatibility_typed_note_links_path,
            proposition_path,
            debate_path,
            synthesis_path,
            study_lineage_path,
            independence_path,
            source_contributions_path,
            quantitative_path,
            locator_audit_path,
            coverage_register_path,
            tag_concept_registry_path,
        )
    )

    gap_registry_path = root / "gap_registry.yml"
    compatibility_gaps = gap_root / "gaps.yml"
    compatibility_gap_index = workspace / "02_source_memory" / "indexes" / "gap_candidates.yml"
    gap_memory_path = root / "gap_memory.yml"
    gap_merge_ledger_path = root / "gap_merge_ledger.yml"
    search_path = root / "internal_search_log.yml"
    gap_status = (
        "complete_no_qualifying_gaps"
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
        gap_index.append(
            f"- {_gap_wikilink(gap)} — {str(gap['rule']).replace('_', ' ')}; "
            f"{str(gap['status']).replace('_', ' ')}; rank {gap['rank']}"
        )
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
            {"type": "literature_gap_index", "tags": []},
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
                neighborhood_index_note=neighborhoods_note_stem,
            ),
        )
        synthesis = _as_mapping(synthesis_by_cluster.get(str(cluster["cluster_id"])))
        debate = _as_mapping(debate_by_cluster.get(str(cluster["cluster_id"])))
        question_text = _human_projection_text(cluster.get("shared_question") or "")
        verdict_text = _map_verdict_excerpt(synthesis.get("synthesis"), sentence_limit=2, character_limit=550)
        cluster_index.extend(
            [
                f"## {_cluster_wikilink(cluster)}",
                *( [f"**Question:** {question_text}"] if question_text else [] ),
                f"**Verdict:** {verdict_text or 'Synthesis remains incomplete and resumable.'}",
                f"- Evidence: {len(cluster.get('core_source_ids', []) or [])} core publications; "
                f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases",
                "- Relationship: "
                + str(debate.get("classification") or synthesis.get("debate_state") or "no_debate").replace("_", " "),
                f"- Source-specific contributions retained: {len(synthesis.get('source_contributions', []) or [])}",
                f"- Linked collection gaps: {len(gaps_by_cluster.get(str(cluster['cluster_id']), []) or [])}",
                "",
            ]
        )
        paths.append(path)
    cluster_index_path = cluster_root / "INDEX.md"
    atomic_write_text(
        cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": []},
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
        "graph_projection_hash": str(report.get("manifest", {}).get("graph_projection_hash") or ""),
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
    packet = _preserve_existing_projection_fields(
        packet_path,
        packet,
        ("profile_packet_count", "profile_packet_paths"),
    )
    write_yaml(packet_path, packet)
    paths.append(packet_path)

    map_note_stem = literature_map_note_stem(source_set, map_id)
    map_markdown = _literature_map_markdown(report, source_set, map_id=map_id)
    neighborhoods_markdown = _literature_neighborhoods_markdown(report, source_set)
    literature_map_path = root / f"{map_note_stem}.md"
    literature_neighborhoods_path = root / f"{neighborhoods_note_stem}.md"
    index_path = root / "INDEX.md"
    manifest = report["manifest"]
    atomic_write_text(literature_map_path, map_markdown)
    atomic_write_text(literature_neighborhoods_path, neighborhoods_markdown)
    atomic_write_text(
        index_path,
        _markdown_with_frontmatter(
            {"type": "literature_map_pointer", "primary_map": f"[[{map_note_stem}]]", "tags": []},
            f"# Literature Map\n\nOpen [[{map_note_stem}|the collection literature map]].",
        ),
    )
    paths.extend((literature_map_path, literature_neighborhoods_path, index_path))

    canonical_registry_path = map_root / "cluster_registry.yml"
    canonical_ledger_path = map_root / "cluster_ledger.yml"
    canonical_matrix_path = map_root / "evidence_matrices.yml"
    canonical_neighborhood_path = map_root / "topic_neighborhoods.yml"
    canonical_subject_tag_registry_path = map_root / "subject_tag_registry.yml"
    canonical_subject_tag_assignments_path = map_root / "subject_tag_assignments.yml"
    canonical_typed_relations_path = map_root / "typed_source_relations.yml"
    canonical_navigation_audit_path = map_root / "navigation_audit.yml"
    canonical_proposition_path = map_root / "propositions.yml"
    canonical_debate_path = map_root / "debate_registry.yml"
    canonical_synthesis_path = map_root / "cluster_syntheses.yml"
    canonical_study_lineage_path = map_root / "study_lineage_registry.yml"
    canonical_independence_path = map_root / "independence_assessments.yml"
    canonical_source_contributions_path = map_root / "cluster_source_contributions.yml"
    canonical_quantitative_path = map_root / "quantitative_comparisons.yml"
    canonical_locator_audit_path = map_root / "locator_audit.yml"
    canonical_coverage_register_path = map_root / "coverage_register.yml"
    canonical_tag_concept_registry_path = map_root / "tag_concept_registry.yml"
    canonical_gap_registry_path = map_root / "gap_registry.yml"
    canonical_gap_memory_path = map_root / "gap_memory.yml"
    canonical_gap_merge_ledger_path = map_root / "gap_merge_ledger.yml"
    canonical_search_path = map_root / "internal_search_log.yml"
    canonical_packet_path = map_root / "packet.yml"
    write_yaml(canonical_registry_path, {"updated_at": generated_at, **dict(report["cluster_registry"])})
    write_yaml(canonical_ledger_path, {"updated_at": generated_at, "events": report["cluster_registry"]["ledger"]})
    write_yaml(canonical_matrix_path, {"updated_at": generated_at, "matrices": report["evidence_matrices"]})
    write_yaml(
        canonical_neighborhood_path,
        {"updated_at": generated_at, "topic_neighborhoods": report.get("topic_neighborhoods", [])},
    )
    write_yaml(canonical_subject_tag_registry_path, tag_registry_payload)
    write_yaml(canonical_subject_tag_assignments_path, tag_assignments_payload)
    write_yaml(canonical_typed_relations_path, typed_relations_payload)
    write_yaml(canonical_navigation_audit_path, navigation_audit_payload)
    write_yaml(
        canonical_proposition_path,
        {"updated_at": generated_at, "propositions": report.get("propositions", [])},
    )
    write_yaml(canonical_debate_path, {"updated_at": generated_at, **dict(report["debate_registry"])})
    write_yaml(canonical_synthesis_path, {"updated_at": generated_at, "syntheses": synthesis_by_cluster})
    write_yaml(
        canonical_study_lineage_path,
        {
            "updated_at": generated_at,
            "version": STUDY_LINEAGE_VERSION,
            "study_lineages": report.get("study_lineages", []),
            "evidence_base_groups": report.get("evidence_base_groups", []),
        },
    )
    write_yaml(
        canonical_independence_path,
        {
            "updated_at": generated_at,
            "version": INDEPENDENCE_ALGORITHM_VERSION,
            "assessments": report.get("independence_assessments", []),
        },
    )
    write_yaml(
        canonical_source_contributions_path,
        {"updated_at": generated_at, "clusters": report.get("cluster_source_contributions", {})},
    )
    write_yaml(
        canonical_quantitative_path,
        {"updated_at": generated_at, "comparisons": report.get("quantitative_comparisons", [])},
    )
    write_yaml(
        canonical_locator_audit_path,
        {"updated_at": generated_at, **dict(report.get("locator_audit", {}))},
    )
    write_yaml(
        canonical_coverage_register_path,
        dict(report.get("coverage_register", {})),
    )
    write_yaml(
        canonical_tag_concept_registry_path,
        {
            "updated_at": generated_at,
            "version": navigation.get("tag_reconciliation_version", "2"),
            "concepts": navigation.get("tag_concept_registry", []),
            "reconciliation_proposals": navigation.get("tag_reconciliation_proposals", []),
        },
    )
    write_yaml(canonical_gap_registry_path, {"updated_at": generated_at, **dict(report["gap_registry"])})
    write_yaml(canonical_gap_memory_path, {"updated_at": generated_at, "entries": report["gap_memory"]})
    write_yaml(
        canonical_gap_merge_ledger_path,
        {"updated_at": generated_at, "events": merge_events},
    )
    write_yaml(canonical_search_path, {"updated_at": generated_at, "searches": report["internal_search_log"]})
    packet = _preserve_existing_projection_fields(
        canonical_packet_path,
        packet,
        ("profile_packet_count", "profile_packet_paths"),
    )
    write_yaml(canonical_packet_path, packet)
    paths.extend(
        (
            canonical_registry_path,
            canonical_ledger_path,
            canonical_matrix_path,
            canonical_neighborhood_path,
            canonical_subject_tag_registry_path,
            canonical_subject_tag_assignments_path,
            canonical_typed_relations_path,
            canonical_navigation_audit_path,
            canonical_proposition_path,
            canonical_debate_path,
            canonical_synthesis_path,
            canonical_study_lineage_path,
            canonical_independence_path,
            canonical_source_contributions_path,
            canonical_quantitative_path,
            canonical_locator_audit_path,
            canonical_coverage_register_path,
            canonical_tag_concept_registry_path,
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
        canonical_gap_index.append(
            f"- {_gap_wikilink(gap)} — {str(gap['rule']).replace('_', ' ')}; "
            f"{str(gap['status']).replace('_', ' ')}; rank {gap['rank']}"
        )
        paths.append(canonical_gap_path)
    canonical_gap_index_path = canonical_gap_root / "INDEX.md"
    atomic_write_text(
        canonical_gap_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_gap_index", "tags": []},
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
                neighborhood_index_note=neighborhoods_note_stem,
            ),
        )
        synthesis = _as_mapping(synthesis_by_cluster.get(str(cluster["cluster_id"])))
        debate = _as_mapping(debate_by_cluster.get(str(cluster["cluster_id"])))
        question_text = _human_projection_text(cluster.get("shared_question") or "")
        verdict_text = _map_verdict_excerpt(synthesis.get("synthesis"), sentence_limit=2, character_limit=550)
        canonical_cluster_index.extend(
            [
                f"## {_cluster_wikilink(cluster)}",
                *( [f"**Question:** {question_text}"] if question_text else [] ),
                f"**Verdict:** {verdict_text or 'Synthesis remains incomplete and resumable.'}",
                f"- Evidence: {len(cluster.get('core_source_ids', []) or [])} core publications; "
                f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases",
                "- Relationship: "
                + str(debate.get("classification") or synthesis.get("debate_state") or "no_debate").replace("_", " "),
                f"- Source-specific contributions retained: {len(synthesis.get('source_contributions', []) or [])}",
                f"- Linked collection gaps: {len(gaps_by_cluster.get(str(cluster['cluster_id']), []) or [])}",
                "",
            ]
        )
        paths.append(canonical_cluster_path)
    canonical_cluster_index_path = canonical_cluster_root / "INDEX.md"
    atomic_write_text(
        canonical_cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": []},
            "\n".join(canonical_cluster_index),
        ),
    )
    paths.append(canonical_cluster_index_path)

    canonical_literature_map_path = map_root / f"{map_note_stem}.md"
    canonical_literature_neighborhoods_path = map_root / f"{neighborhoods_note_stem}.md"
    canonical_index_path = map_root / "INDEX.md"
    atomic_write_text(canonical_literature_map_path, map_markdown)
    atomic_write_text(canonical_literature_neighborhoods_path, neighborhoods_markdown)
    atomic_write_text(
        canonical_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_map_pointer", "primary_map": f"[[{map_note_stem}]]", "tags": []},
            f"# Literature Map\n\nOpen [[{map_note_stem}|the collection literature map]].",
        ),
    )
    paths.extend((canonical_literature_map_path, canonical_literature_neighborhoods_path, canonical_index_path))

    canonical_manifest_path = map_root / "manifest.yml"
    canonical_artifacts = {
        "manifest": str(canonical_manifest_path),
        "cluster_registry": str(canonical_registry_path),
        "cluster_ledger": str(canonical_ledger_path),
        "evidence_matrices": str(canonical_matrix_path),
        "topic_neighborhoods": str(canonical_neighborhood_path),
        "subject_tag_registry": str(canonical_subject_tag_registry_path),
        "subject_tag_assignments": str(canonical_subject_tag_assignments_path),
        "typed_source_relations": str(canonical_typed_relations_path),
        "navigation_audit": str(canonical_navigation_audit_path),
        "propositions": str(canonical_proposition_path),
        "debate_registry": str(canonical_debate_path),
        "cluster_syntheses": str(canonical_synthesis_path),
        "study_lineage_registry": str(canonical_study_lineage_path),
        "independence_assessments": str(canonical_independence_path),
        "cluster_source_contributions": str(canonical_source_contributions_path),
        "quantitative_comparisons": str(canonical_quantitative_path),
        "locator_audit": str(canonical_locator_audit_path),
        "coverage_register": str(canonical_coverage_register_path),
        "tag_concept_registry": str(canonical_tag_concept_registry_path),
        "gap_registry": str(canonical_gap_registry_path),
        "gap_memory": str(canonical_gap_memory_path),
        "gap_merge_ledger": str(canonical_gap_merge_ledger_path),
        "internal_search_log": str(canonical_search_path),
        "packet": str(canonical_packet_path),
        "literature_map_markdown": str(canonical_literature_map_path),
        "literature_neighborhoods_markdown": str(canonical_literature_neighborhoods_path),
        "index": str(canonical_index_path),
        "cluster_index": str(canonical_cluster_index_path),
        "gap_index": str(canonical_gap_index_path),
    }
    manifest_lineage_fields = (
        "note_projection_hashes",
        "semantic_note_hashes",
        "profile_dependency_hashes",
        "source_set_id",
        "source_set_dependency_hash",
        "provider",
        "model",
        "literature_policy",
        "algorithm_versions",
        "validation_mode",
    )
    canonical_manifest_payload = _preserve_existing_projection_fields(
        canonical_manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "run_id": run_id,
            "source_set_id": source_set.get("source_set_id", ""),
            "source_set_dependency_hash": source_set.get("dependency_hash", ""),
            "engine_version": "0.8.0",
            "artifact_schema_version": "1.7",
            **dict(manifest),
            "artifacts": canonical_artifacts,
        },
        manifest_lineage_fields,
    )
    write_yaml(canonical_manifest_path, canonical_manifest_payload)
    paths.append(canonical_manifest_path)

    manifest_path = root / "manifest.yml"
    artifact_names = {
        "manifest": str(manifest_path),
        "cluster_registry": str(registry_path),
        "cluster_ledger": str(ledger_path),
        "evidence_matrices": str(matrix_path),
        "topic_neighborhoods": str(neighborhood_path),
        "subject_tag_registry": str(subject_tag_registry_path),
        "subject_tag_assignments": str(subject_tag_assignments_path),
        "typed_source_relations": str(typed_relations_path),
        "navigation_audit": str(navigation_audit_path),
        "propositions": str(proposition_path),
        "debate_registry": str(debate_path),
        "cluster_syntheses": str(synthesis_path),
        "study_lineage_registry": str(study_lineage_path),
        "independence_assessments": str(independence_path),
        "cluster_source_contributions": str(source_contributions_path),
        "quantitative_comparisons": str(quantitative_path),
        "locator_audit": str(locator_audit_path),
        "coverage_register": str(coverage_register_path),
        "tag_concept_registry": str(tag_concept_registry_path),
        "gap_registry": str(gap_registry_path),
        "gap_memory": str(gap_memory_path),
        "gap_merge_ledger": str(gap_merge_ledger_path),
        "internal_search_log": str(search_path),
        "packet": str(packet_path),
        "literature_map_markdown": str(literature_map_path),
        "literature_neighborhoods_markdown": str(literature_neighborhoods_path),
        "index": str(index_path),
        "canonical_map": str(canonical_manifest_path),
    }
    manifest_payload = _preserve_existing_projection_fields(
        manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "engine_version": "0.8.0",
            "artifact_schema_version": "1.7",
            **dict(manifest),
            "artifacts": artifact_names,
        },
        manifest_lineage_fields,
    )
    write_yaml(manifest_path, manifest_payload)
    paths.insert(0, manifest_path)
    packet = {
        **packet,
        "path": str(canonical_packet_path),
        "compatibility_path": str(packet_path),
        "map_path": str(map_root),
        "literature_map_markdown": str(canonical_literature_map_path),
        "compatibility_literature_map_markdown": str(literature_map_path),
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
    navigation_policy: Any = None,
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
    navigation_source_notes = _source_notes_with_custody_relations(workspace, notes)
    report = build_literature_report(
        profiles if profiles is not None else notes,
        previous_registry=previous_registry,
        policy=effective_policy,
        question=effective_question,
        reasoner=reasoner,
        request=request,
        stage_callback=stage_callback,
        reasoner_call=reasoner_calls,
        source_notes=navigation_source_notes,
        navigation_policy=navigation_policy,
        source_set=source_set,
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
    partial_cluster_ids = list(packet.get("partial_cluster_ids", []) or [])
    cluster_map = {
        "status": (
            "partial"
            if partial_cluster_ids
            else (
                "built"
                if promoted_clusters
                else ("cluster_candidates" if clusters else "complete_no_analytical_clusters")
            )
        ),
        "automation_status": (
            "promoted" if promoted_clusters else ("candidate" if clusters else "not_applicable")
        ),
        "clusters": clusters,
        "relations": report["relations"],
        "topic_neighborhoods": report.get("topic_neighborhoods", []),
        "navigation": report.get("navigation", {}),
        "propositions": report.get("propositions", []),
        "topic_neighborhood_count": report["manifest"].get("topic_neighborhood_count", 0),
        "proposition_count": report["manifest"].get("proposition_count", 0),
        "rejected_proposals": report["cluster_registry"]["rejected_proposals"],
        "unclustered_sources": report["cluster_registry"]["unclustered_sources"],
        "cluster_syntheses": report["cluster_syntheses"],
        "synthesized_cluster_count": report["manifest"]["synthesized_cluster_count"],
        "partial_cluster_synthesis_count": report["manifest"]["partial_cluster_synthesis_count"],
        "partial_cluster_ids": partial_cluster_ids,
        "partial_reason": str(packet.get("partial_reason") or ""),
        "evidence_base_group_count": report["manifest"].get("evidence_base_group_count", 0),
        "cluster_source_contribution_count": report["manifest"].get("cluster_source_contribution_count", 0),
        "quantitative_comparison_count": report["manifest"].get("quantitative_comparison_count", 0),
        "rejected_quantitative_comparison_count": report["manifest"].get(
            "rejected_quantitative_comparison_count", 0
        ),
        "rejected_generated_locator_count": report["manifest"].get("rejected_generated_locator_count", 0),
        "coverage_inventory_count": report["manifest"].get("coverage_inventory_count", 0),
        "coverage_exhausted_count": report["manifest"].get("coverage_exhausted_count", 0),
        "coverage_accounting_valid": report["manifest"].get("coverage_accounting_valid", False),
        "minimum_analytical_notes": 2,
        "path": str(workspace / "03_literature_synthesis" / "clusters" / "clusters.yml"),
        "registry_path": str(workspace / "03_literature_synthesis" / "cluster_registry.yml"),
    }
    gap_status = (
        "complete_no_qualifying_gaps"
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
    """Public entry point for proposition-anchored mapping over existing profiles."""
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
        source_set=source_set,
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
