from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping

CURRENT_ENGINE_VERSION = "0.28.0"
CURRENT_ARTIFACT_SCHEMA_VERSION = "1.19"
CURRENT_PROFILE_SCHEMA_VERSION = "1.3"


FAMILY_RELATION_TYPES = frozenset(
    {
        "same_proposition",
        "shared_research_problem",
        "rival_explanation",
        "complementary_mechanism",
        "boundary_contrast",
        "methodological_fault_line",
        "sequential_relationship",
        "interpretive_or_normative_disagreement",
    }
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _model_payload(
    model: type[Any], payload: Mapping[str, Any], *, label: str
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a mapping")
    values = dict(payload)
    unknown = sorted(set(values) - set(model.__dataclass_fields__))
    if unknown:
        raise ValueError(f"unknown {label} fields: {', '.join(unknown)}")
    return values


def _string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(value)


def _mapping_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"{field} must be a list of mappings")
    return [dict(item) for item in value]


def _require_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_positive_decimal(value: Any, *, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive decimal amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a positive decimal amount") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be a positive decimal amount")
    return result


def _readable_bundle_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return "\n".join(
            f"{str(key).replace('_', ' ').strip().capitalize()}: "
            f"{_readable_bundle_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    if isinstance(value, list):
        return "\n".join(
            f"- {_readable_bundle_text(item)}"
            for item in value
            if item not in (None, "", [], {})
        )
    return str(value)


def _normalized_bundle_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = dict(value)
    facets = profile.pop("bounded_facets", {})
    if isinstance(facets, Mapping):
        for key, item in facets.items():
            profile.setdefault(str(key), item)
    for field_name in (
        "concepts",
        "theories",
        "mechanisms",
        "methods",
        "cases",
        "datasets",
        "data",
        "geography",
        "periods",
        "populations",
        "outcomes",
        "measures",
        "limitations",
        "boundaries",
        "gaps",
        "future_research",
    ):
        item = profile.get(field_name)
        if item not in (None, "") and not isinstance(item, list):
            profile[field_name] = [str(item)]
    return profile


def _scope_mapping(value: Any, *, field: str) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{field} must be a mapping of string keys to lists of strings"
        )
    scope: dict[str, list[str]] = {}
    for key, items in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field} keys must be strings")
        scope[key] = _string_list(items, field=f"{field}.{key}")
    return scope


def _any_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    return dict(value)


def _family_relation_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    rows = _mapping_list(value, field=field)
    allowed = {"relation_type", "source_ids", "rationale", "evidence", "comparability"}
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise ValueError(f"unknown {field}[{index}] fields: {', '.join(unknown)}")
        relation_type = _require_string(
            row.get("relation_type", ""), field=f"{field}[{index}].relation_type"
        )
        if relation_type not in FAMILY_RELATION_TYPES:
            raise ValueError(f"{field}[{index}].relation_type is invalid")
        source_ids = _string_list(
            row.get("source_ids"), field=f"{field}[{index}].source_ids"
        )
        if len(set(source_ids)) < 2:
            raise ValueError(
                f"{field}[{index}].source_ids must contain at least two sources"
            )
        normalized.append(
            {
                "relation_type": relation_type,
                "source_ids": source_ids,
                "rationale": _require_string(
                    row.get("rationale", ""), field=f"{field}[{index}].rationale"
                ),
                "evidence": _mapping_list(
                    row.get("evidence"), field=f"{field}[{index}].evidence"
                ),
                "comparability": _any_mapping(
                    row.get("comparability", {}),
                    field=f"{field}[{index}].comparability",
                ),
            }
        )
    return normalized


def _strict_adjudication(value: Any, *, field: str) -> dict[str, Any]:
    row = _any_mapping(value, field=field)
    if not row:
        return {}
    allowed = {
        "kind",
        "candidate",
        "decision",
        "checks",
        "explanation",
        "what_would_change",
        "proposition_ids",
        "related_cluster_ids",
        "evidence",
    }
    unknown = sorted(set(row) - allowed)
    if unknown:
        raise ValueError(f"unknown {field} fields: {', '.join(unknown)}")
    kind = _require_string(row.get("kind", ""), field=f"{field}.kind")
    if kind not in {"consensus", "contradiction", "strong_gap"}:
        raise ValueError(f"{field}.kind is invalid")
    decision = _require_string(row.get("decision", ""), field=f"{field}.decision")
    if decision not in {"established", "not_established"}:
        raise ValueError(f"{field}.decision is invalid")
    checks = _mapping_list(row.get("checks"), field=f"{field}.checks")
    normalized_checks: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if set(check) - {"requirement", "passed", "explanation"}:
            raise ValueError(f"unknown {field}.checks[{index}] fields")
        normalized_checks.append(
            {
                "requirement": _require_string(
                    check.get("requirement", ""),
                    field=f"{field}.checks[{index}].requirement",
                ),
                "passed": _require_bool(
                    check.get("passed", False), field=f"{field}.checks[{index}].passed"
                ),
                "explanation": _require_string(
                    check.get("explanation", ""),
                    field=f"{field}.checks[{index}].explanation",
                ),
            }
        )
    return {
        "kind": kind,
        "candidate": _require_string(
            row.get("candidate", ""), field=f"{field}.candidate"
        ),
        "decision": decision,
        "checks": normalized_checks,
        "explanation": _require_string(
            row.get("explanation", ""), field=f"{field}.explanation"
        ),
        "what_would_change": _require_string(
            row.get("what_would_change", ""), field=f"{field}.what_would_change"
        ),
        "proposition_ids": _string_list(
            row.get("proposition_ids"), field=f"{field}.proposition_ids"
        ),
        "related_cluster_ids": _string_list(
            row.get("related_cluster_ids"), field=f"{field}.related_cluster_ids"
        ),
        "evidence": _mapping_list(row.get("evidence"), field=f"{field}.evidence"),
    }


def _strict_adjudication_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    return [
        _strict_adjudication(row, field=f"{field}[{index}]")
        for index, row in enumerate(_mapping_list(value, field=field))
    ]


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _stable_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_locator(value: str) -> str:
    normalized = value.casefold().replace("\u2013", "-").replace("\u2014", "-")
    normalized = re.sub(r"\b(?:pages?|pp?\.?)\s*", "p ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .;,:")
    return normalized


def _locator_identity(locator: str, locators: list[str]) -> list[str]:
    values = list(locators)
    if locator and locator not in values:
        values.append(locator)
    return sorted(
        {_normalized_locator(value) for value in values if _normalized_locator(value)}
    )


@dataclass(frozen=True, slots=True)
class LiteratureMappingPolicy:
    """Serializable limits and promotion rules for literature synthesis."""

    synthesis_enabled: bool = True
    require_question: bool = False
    auto_promote_clusters: bool = True
    auto_promote_debates: bool = True
    auto_promote_gaps: bool = True
    source_backed_threshold: int = 3
    max_memberships: int = 0
    external_discovery: Literal["disabled", "per_run", "always"] = "disabled"
    max_profile_calls: int = 0
    max_synthesis_calls: int = 0
    profile_workers: int = 4
    literature_deadline_seconds: float = 0.0
    deepseek_packet_context_fraction: float = 0.8
    weak_gap_handling: Literal["audit_only"] = "audit_only"
    cluster_gap_projection: Literal["inline"] = "inline"
    require_executable_gap_design: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "synthesis_enabled",
            "require_question",
            "auto_promote_clusters",
            "auto_promote_debates",
            "auto_promote_gaps",
            "require_executable_gap_design",
        ):
            _require_bool(
                getattr(self, field_name), field=f"literature_mapping.{field_name}"
            )
        for field_name in ("source_backed_threshold", "profile_workers"):
            _require_positive_int(
                getattr(self, field_name), field=f"literature_mapping.{field_name}"
            )
        for field_name in ("max_memberships", "max_profile_calls", "max_synthesis_calls"):
            _nonnegative_int(
                getattr(self, field_name), field=f"literature_mapping.{field_name}"
            )
        if self.external_discovery not in {"disabled", "per_run", "always"}:
            raise ValueError(
                "literature_mapping.external_discovery must be disabled, per_run, or always"
            )
        if self.weak_gap_handling != "audit_only":
            raise ValueError("literature_mapping.weak_gap_handling must be audit_only")
        if self.cluster_gap_projection != "inline":
            raise ValueError("literature_mapping.cluster_gap_projection must be inline")
        if (
            isinstance(self.literature_deadline_seconds, bool)
            or not isinstance(self.literature_deadline_seconds, (int, float))
            or self.literature_deadline_seconds < 0
        ):
            raise ValueError(
                "literature_mapping.literature_deadline_seconds cannot be negative"
            )
        if (
            isinstance(self.deepseek_packet_context_fraction, bool)
            or not isinstance(self.deepseek_packet_context_fraction, (int, float))
            or not 0 < self.deepseek_packet_context_fraction < 1
        ):
            raise ValueError(
                "literature_mapping.deepseek_packet_context_fraction must be between 0 and 1"
            )
        # Normalize equivalent API, config, and CLI values before they enter
        # dependency fingerprints. JSON distinguishes 1800 from 1800.0 even
        # though both express the same deadline, which would otherwise bypass
        # valid paid-call checkpoints.
        object.__setattr__(
            self, "literature_deadline_seconds", float(self.literature_deadline_seconds)
        )
        object.__setattr__(
            self,
            "deepseek_packet_context_fraction",
            float(self.deepseek_packet_context_fraction),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> LiteratureMappingPolicy:
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("literature_mapping must be a mapping")
        values = dict(payload or {})
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown literature_mapping fields: {', '.join(unknown)}")
        return cls(
            synthesis_enabled=values.get("synthesis_enabled", True),
            require_question=values.get("require_question", False),
            auto_promote_clusters=values.get("auto_promote_clusters", True),
            auto_promote_debates=values.get("auto_promote_debates", True),
            auto_promote_gaps=values.get("auto_promote_gaps", True),
            source_backed_threshold=values.get("source_backed_threshold", 3),
            max_memberships=values.get("max_memberships", 0),
            external_discovery=values.get("external_discovery", "disabled"),
            max_profile_calls=values.get("max_profile_calls", 0),
            max_synthesis_calls=values.get("max_synthesis_calls", 0),
            profile_workers=values.get("profile_workers", 4),
            literature_deadline_seconds=values.get(
                "literature_deadline_seconds", 0.0
            ),
            deepseek_packet_context_fraction=values.get(
                "deepseek_packet_context_fraction", 0.8
            ),
            weak_gap_handling=values.get("weak_gap_handling", "audit_only"),
            cluster_gap_projection=values.get("cluster_gap_projection", "inline"),
            require_executable_gap_design=values.get(
                "require_executable_gap_design", True
            ),
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class NavigationPolicy:
    """Serializable limits for collection-native tags and navigation links."""

    subject_tags_enabled: bool = True
    max_candidate_tags_per_source: int = 24
    max_visible_tags_per_source: int = 6
    max_visible_tags_per_cluster_or_gap: int = 6
    min_sources_per_neighborhood: int = 2
    max_visible_neighborhoods: int = 8
    max_collection_neighborhoods: int = 20
    max_inferred_related_note_links: int = 8
    external_ontology: Literal["disabled"] = "disabled"
    automatic_semantic_synonym_merging: bool = False

    def __post_init__(self) -> None:
        _require_bool(
            self.subject_tags_enabled, field="navigation.subject_tags_enabled"
        )
        _require_bool(
            self.automatic_semantic_synonym_merging,
            field="navigation.automatic_semantic_synonym_merging",
        )
        for field_name in (
            "max_candidate_tags_per_source",
            "max_visible_tags_per_source",
            "max_visible_tags_per_cluster_or_gap",
            "min_sources_per_neighborhood",
            "max_visible_neighborhoods",
            "max_collection_neighborhoods",
            "max_inferred_related_note_links",
        ):
            _require_positive_int(
                getattr(self, field_name), field=f"navigation.{field_name}"
            )
        if self.external_ontology != "disabled":
            raise ValueError("navigation.external_ontology must be disabled")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> NavigationPolicy:
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("navigation must be a mapping")
        values = dict(payload or {})
        unknown = sorted(set(values) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown navigation fields: {', '.join(unknown)}")
        return cls(
            subject_tags_enabled=values.get("subject_tags_enabled", True),
            max_candidate_tags_per_source=values.get(
                "max_candidate_tags_per_source", 24
            ),
            max_visible_tags_per_source=values.get("max_visible_tags_per_source", 6),
            max_visible_tags_per_cluster_or_gap=values.get(
                "max_visible_tags_per_cluster_or_gap", 6
            ),
            min_sources_per_neighborhood=values.get("min_sources_per_neighborhood", 2),
            max_visible_neighborhoods=values.get("max_visible_neighborhoods", 8),
            max_collection_neighborhoods=values.get("max_collection_neighborhoods", 20),
            max_inferred_related_note_links=values.get(
                "max_inferred_related_note_links", 8
            ),
            external_ontology=values.get("external_ontology", "disabled"),
            automatic_semantic_synonym_merging=values.get(
                "automatic_semantic_synonym_merging", False
            ),
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    """Serializable PDF extraction and OCR routing policy."""

    ocr: Literal["auto", "off", "required"] = "auto"
    languages: tuple[str, ...] = ("eng",)

    def __post_init__(self) -> None:
        if self.ocr not in {"auto", "off", "required"}:
            raise ValueError("extraction.ocr must be auto, off, or required")
        languages = self.languages
        if isinstance(languages, str):
            languages = (languages,)
        elif not isinstance(languages, tuple):
            languages = tuple(languages)
        normalized = tuple(
            dict.fromkeys(str(language).strip() for language in languages if str(language).strip())
        )
        if not normalized:
            raise ValueError("extraction.languages must contain at least one OCR language")
        if any(not re.fullmatch(r"[A-Za-z0-9_+-]+", language) for language in normalized):
            raise ValueError("extraction.languages contains an invalid OCR language code")
        object.__setattr__(self, "languages", normalized)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> ExtractionPolicy:
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("extraction must be a mapping")
        values = dict(payload or {})
        aliases = {"version", "vision"}
        unknown = sorted(set(values) - set(cls.__dataclass_fields__) - aliases)
        if unknown:
            raise ValueError(f"unknown extraction fields: {', '.join(unknown)}")
        languages = values.get("languages", ("eng",))
        if isinstance(languages, str):
            languages = (languages,)
        elif not isinstance(languages, (list, tuple)):
            raise ValueError("extraction.languages must be a string or sequence")
        return cls(
            ocr=str(values.get("ocr", "auto")),  # type: ignore[arg-type]
            languages=tuple(str(value) for value in languages),
        )


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    """Serializable safety and cost limits for one document-processing invocation."""

    direct_read_char_limit: int = 120_000
    chunk_char_limit: int = 60_000
    max_total_chunks: int = 0
    max_calls_per_document_run: int = 0
    connect_timeout_seconds: float = 60.0
    request_deadline_seconds: float = 600.0
    document_deadline_seconds: float = 0.0
    chunk_output_tokens: int = 900
    synthesis_output_tokens: int = 3_000
    context_window_fraction: float = 0.5
    estimated_chars_per_token: float = 3.5

    def __post_init__(self) -> None:
        positive_integer_fields = (
            "direct_read_char_limit",
            "chunk_char_limit",
            "chunk_output_tokens",
            "synthesis_output_tokens",
        )
        for field_name in positive_integer_fields:
            if int(getattr(self, field_name)) < 1:
                raise ValueError(f"processing.{field_name} must be at least 1")
        for field_name in ("max_total_chunks", "max_calls_per_document_run"):
            if int(getattr(self, field_name)) < 0:
                raise ValueError(f"processing.{field_name} cannot be negative")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("processing.connect_timeout_seconds must be positive")
        if self.request_deadline_seconds <= 0:
            raise ValueError("processing.request_deadline_seconds must be positive")
        if self.document_deadline_seconds < 0:
            raise ValueError("processing.document_deadline_seconds cannot be negative")
        if not 0 < self.context_window_fraction < 1:
            raise ValueError(
                "processing.context_window_fraction must be between 0 and 1"
            )
        if self.estimated_chars_per_token <= 0:
            raise ValueError("processing.estimated_chars_per_token must be positive")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> ProcessingPolicy:
        values = dict(payload or {})
        unknown = sorted(set(values) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown processing fields: {', '.join(unknown)}")
        return cls(
            direct_read_char_limit=int(values.get("direct_read_char_limit", 120_000)),
            chunk_char_limit=int(values.get("chunk_char_limit", 60_000)),
            max_total_chunks=int(values.get("max_total_chunks", 0)),
            max_calls_per_document_run=int(
                values.get("max_calls_per_document_run", 0)
            ),
            connect_timeout_seconds=float(values.get("connect_timeout_seconds", 60.0)),
            request_deadline_seconds=float(
                values.get("request_deadline_seconds", 600.0)
            ),
            document_deadline_seconds=float(
                values.get("document_deadline_seconds", 0.0)
            ),
            chunk_output_tokens=int(values.get("chunk_output_tokens", 900)),
            synthesis_output_tokens=int(values.get("synthesis_output_tokens", 3_000)),
            context_window_fraction=float(values.get("context_window_fraction", 0.5)),
            estimated_chars_per_token=float(
                values.get("estimated_chars_per_token", 3.5)
            ),
        )


@dataclass(frozen=True, slots=True)
class MapRequest:
    """A complete, serializable request for one mapping run."""

    workspace: Path | str
    scope: Literal["library", "collection", "selected"] = "library"
    collection_key: str | None = None
    question: str | None = None
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    allow_cloud: bool = False
    parallel: int = 4
    provider_concurrency: int | Literal["auto"] | None = None
    max_provider_spend_usd: Decimal | None = None
    limit: int = 0
    extraction_version: str = "2"
    prompt_version: str = "11"
    retry_terminal_failures: bool = False
    extraction_policy: ExtractionPolicy = field(default_factory=ExtractionPolicy)
    processing: ProcessingPolicy = field(default_factory=ProcessingPolicy)
    literature_policy: LiteratureMappingPolicy = field(
        default_factory=LiteratureMappingPolicy
    )
    navigation_policy: NavigationPolicy = field(default_factory=NavigationPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        _require_bool(self.allow_cloud, field="allow_cloud")
        _require_bool(
            self.retry_terminal_failures, field="retry_terminal_failures"
        )
        if self.model == "deepseek-v4-flash" and self.provider in {"ollama", "gemini"}:
            object.__setattr__(
                self,
                "model",
                {"ollama": "llama3.2", "gemini": "gemini-2.5-flash"}[self.provider],
            )
        if self.provider == "openrouter" and self.model == "deepseek-v4-flash":
            raise ValueError("openrouter requires an explicit routed model id")
        if self.scope not in {"library", "collection", "selected"}:
            raise ValueError("scope must be library, collection, or selected")
        if self.scope == "collection" and not self.collection_key:
            raise ValueError("collection scope requires collection_key")
        if self.parallel < 1:
            raise ValueError("parallel must be at least 1")
        if self.provider_concurrency is not None:
            if self.provider_concurrency != "auto" and (
                isinstance(self.provider_concurrency, bool)
                or not isinstance(self.provider_concurrency, int)
                or self.provider_concurrency < 1
            ):
                raise ValueError(
                    "provider_concurrency must be auto or a positive integer"
                )
            if (
                isinstance(self.provider_concurrency, int)
                and self.provider == "deepseek"
                and self.model == "deepseek-v4-flash"
                and self.provider_concurrency > 2_500
            ):
                raise ValueError(
                    "provider_concurrency exceeds DeepSeek V4 Flash account limit 2500"
                )
        object.__setattr__(
            self,
            "max_provider_spend_usd",
            _optional_positive_decimal(
                self.max_provider_spend_usd,
                field="max_provider_spend_usd",
            ),
        )
        if self.limit < 0:
            raise ValueError("limit cannot be negative")
        if not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if not isinstance(self.extraction_policy, ExtractionPolicy):
            if isinstance(self.extraction_policy, Mapping):
                object.__setattr__(
                    self,
                    "extraction_policy",
                    ExtractionPolicy.from_dict(self.extraction_policy),
                )
            else:
                raise ValueError(
                    "extraction_policy must be an ExtractionPolicy or mapping"
                )
        if not isinstance(self.processing, ProcessingPolicy):
            if isinstance(self.processing, Mapping):
                object.__setattr__(
                    self, "processing", ProcessingPolicy.from_dict(self.processing)
                )
            else:
                raise ValueError("processing must be a ProcessingPolicy or mapping")
        if not isinstance(self.literature_policy, LiteratureMappingPolicy):
            if isinstance(self.literature_policy, Mapping):
                object.__setattr__(
                    self,
                    "literature_policy",
                    LiteratureMappingPolicy.from_dict(self.literature_policy),
                )
            else:
                raise ValueError(
                    "literature_policy must be a LiteratureMappingPolicy or mapping"
                )
        if not isinstance(self.navigation_policy, NavigationPolicy):
            if isinstance(self.navigation_policy, Mapping):
                object.__setattr__(
                    self,
                    "navigation_policy",
                    NavigationPolicy.from_dict(self.navigation_policy),
                )
            else:
                raise ValueError(
                    "navigation_policy must be a NavigationPolicy or mapping"
                )
        if self.literature_policy.require_question and not self.question:
            raise ValueError("literature_policy requires a question")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MapRequest:
        return cls(
            workspace=payload["workspace"],
            scope=str(payload.get("scope", "library")),  # type: ignore[arg-type]
            collection_key=payload.get("collection_key") or None,
            question=payload.get("question") or None,
            provider=str(payload.get("provider", "deepseek")),
            model=str(payload.get("model", "deepseek-v4-flash")),
            allow_cloud=_strict_bool(
                payload.get("allow_cloud", False), field="allow_cloud"
            ),
            parallel=int(payload.get("parallel", 4)),
            provider_concurrency=(
                "auto"
                if payload.get("provider_concurrency") == "auto"
                else int(payload["provider_concurrency"])
                if payload.get("provider_concurrency") is not None
                else None
            ),
            max_provider_spend_usd=payload.get("max_provider_spend_usd"),
            limit=int(payload.get("limit", 0)),
            extraction_version=str(payload.get("extraction_version", "2")),
            prompt_version=str(payload.get("prompt_version", "11")),
            retry_terminal_failures=_strict_bool(
                payload.get("retry_terminal_failures", False),
                field="retry_terminal_failures",
            ),
            extraction_policy=ExtractionPolicy.from_dict(
                payload.get("extraction_policy")
                if isinstance(payload.get("extraction_policy"), Mapping)
                else None
            ),
            processing=ProcessingPolicy.from_dict(
                payload.get("processing")
                if isinstance(payload.get("processing"), Mapping)
                else None
            ),
            literature_policy=payload.get(
                "literature_policy", LiteratureMappingPolicy()
            ),
            navigation_policy=payload.get("navigation_policy", NavigationPolicy()),
        )


@dataclass(frozen=True, slots=True)
class SupportEnvelope:
    """What kind of support an anchor can provide and within which bounds."""

    empirical_role: Literal[
        "descriptive", "associational", "causal", "mechanism_evidence", "none"
    ] = "none"
    argument_role: Literal[
        "conceptual",
        "interpretive",
        "normative",
        "methodological",
        "practitioner_guidance",
        "none",
    ] = "none"
    coverage: Literal[
        "full_text", "limited_text", "abstract", "metadata", "unknown"
    ] = "unknown"
    scope: dict[str, list[str]] = field(default_factory=dict)
    restrictions: list[str] = field(default_factory=list)
    support_status: Literal[
        "supported", "support_unknown", "limited", "unsupported"
    ] = "support_unknown"

    def __post_init__(self) -> None:
        empirical_roles = {
            "descriptive",
            "associational",
            "causal",
            "mechanism_evidence",
            "none",
        }
        argument_roles = {
            "conceptual",
            "interpretive",
            "normative",
            "methodological",
            "practitioner_guidance",
            "none",
        }
        coverages = {"full_text", "limited_text", "abstract", "metadata", "unknown"}
        statuses = {"supported", "support_unknown", "limited", "unsupported"}
        _require_string(self.empirical_role, field="support_envelope.empirical_role")
        _require_string(self.argument_role, field="support_envelope.argument_role")
        _require_string(self.coverage, field="support_envelope.coverage")
        _require_string(self.support_status, field="support_envelope.support_status")
        if self.empirical_role not in empirical_roles:
            raise ValueError("support_envelope.empirical_role is invalid")
        if self.argument_role not in argument_roles:
            raise ValueError("support_envelope.argument_role is invalid")
        if self.coverage not in coverages:
            raise ValueError("support_envelope.coverage is invalid")
        if self.support_status not in statuses:
            raise ValueError("support_envelope.support_status is invalid")
        object.__setattr__(
            self, "scope", _scope_mapping(self.scope, field="support_envelope.scope")
        )
        object.__setattr__(
            self,
            "restrictions",
            _string_list(self.restrictions, field="support_envelope.restrictions"),
        )

    def to_dict(self) -> dict[str, Any]:
        scope = _scope_mapping(self.scope, field="support_envelope.scope")
        restrictions = _string_list(
            self.restrictions, field="support_envelope.restrictions"
        )
        return {
            "empirical_role": self.empirical_role,
            "argument_role": self.argument_role,
            "coverage": self.coverage,
            "scope": {key: list(values) for key, values in scope.items()},
            "restrictions": restrictions,
            "support_status": self.support_status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SupportEnvelope:
        values = _model_payload(cls, payload, label="support envelope")
        return cls(
            empirical_role=values.get("empirical_role", "none"),
            argument_role=values.get("argument_role", "none"),
            coverage=values.get("coverage", "unknown"),
            scope=_scope_mapping(
                values.get("scope", {}), field="support_envelope.scope"
            ),
            restrictions=_string_list(
                values.get("restrictions", []),
                field="support_envelope.restrictions",
            ),
            support_status=values.get("support_status", "support_unknown"),
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class EvidenceFinding:
    """One source-bounded finding retained as structured evidence."""

    finding_id: str = ""
    claim: str = ""
    finding_type: str = ""
    direction: str = ""
    magnitude: str = ""
    comparison: str = ""
    conditions: list[str] = field(default_factory=list)
    plain_english_meaning: str = ""
    is_statistical: bool = False
    population: str = ""
    outcome: str = ""
    estimate: str = ""
    uncertainty: str = ""
    evidence: str = ""
    locator: str = ""
    locators: list[str] = field(default_factory=list)
    qualifiers: list[str] = field(default_factory=list)
    confidence: str = ""

    def __post_init__(self) -> None:
        _require_bool(self.is_statistical, field="is_statistical")
        locators = list(self.locators)
        if self.locator and self.locator not in locators:
            locators.insert(0, self.locator)
        locator = self.locator or (locators[0] if locators else "")
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "locators", locators)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceFinding:
        finding_id = payload.get("finding_id")
        claim_id = payload.get("claim_id")
        if finding_id and claim_id and finding_id != claim_id:
            raise ValueError("conflicting finding_id and claim_id")
        raw_locators = payload.get("locators") or []
        locators = (
            [raw_locators] if isinstance(raw_locators, str) else list(raw_locators)
        )
        return cls(
            finding_id=str(finding_id or claim_id or ""),
            claim=str(payload.get("claim") or ""),
            finding_type=str(payload.get("finding_type") or ""),
            direction=str(payload.get("direction") or ""),
            magnitude=str(payload.get("magnitude") or ""),
            comparison=str(payload.get("comparison") or ""),
            conditions=list(payload.get("conditions") or []),
            plain_english_meaning=str(payload.get("plain_english_meaning") or ""),
            is_statistical=payload.get("is_statistical", False),
            population=str(payload.get("population") or ""),
            outcome=str(payload.get("outcome") or ""),
            estimate=str(payload.get("estimate") or ""),
            uncertainty=str(payload.get("uncertainty") or ""),
            evidence=str(payload.get("evidence") or ""),
            locator=str(payload.get("locator") or ""),
            locators=locators,
            qualifiers=list(payload.get("qualifiers") or []),
            confidence=str(payload.get("confidence") or ""),
        )


@dataclass(frozen=True, slots=True)
class SourceLocator:
    """A typed source-native or generated locator for one evidence anchor."""

    locator_id: str = ""
    source_id: str = ""
    evidence_anchor_id: str = ""
    locator_type: Literal[
        "page",
        "page_range",
        "table",
        "figure",
        "chapter",
        "source_heading",
        "paragraph",
        "quote_span",
        "generated_heading",
        "unknown",
    ] = "unknown"
    value: str = ""
    page_start: int | None = None
    page_end: int | None = None
    source_native: bool = False
    supports_strong_assertion: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "locator_id",
            "source_id",
            "evidence_anchor_id",
            "locator_type",
            "value",
        ):
            _require_string(
                getattr(self, field_name), field=f"source locator.{field_name}"
            )
        allowed_types = {
            "page",
            "page_range",
            "table",
            "figure",
            "chapter",
            "source_heading",
            "paragraph",
            "quote_span",
            "generated_heading",
            "unknown",
        }
        if self.locator_type not in allowed_types:
            raise ValueError("source locator.locator_type is invalid")
        for field_name in ("page_start", "page_end"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(
                    f"source locator.{field_name} must be a positive integer or null"
                )
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("source locator.page_end cannot precede page_start")
        _require_bool(self.source_native, field="source locator.source_native")
        _require_bool(
            self.supports_strong_assertion,
            field="source locator.supports_strong_assertion",
        )
        if self.locator_type == "generated_heading" and self.source_native:
            raise ValueError("generated headings cannot be source-native locators")
        if self.supports_strong_assertion and (
            not self.source_native
            or self.locator_type in {"generated_heading", "unknown"}
        ):
            raise ValueError("strong assertions require a typed source-native locator")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceLocator:
        values = _model_payload(cls, payload, label="source locator")
        page_start = values.get("page_start")
        page_end = values.get("page_end")
        if (
            isinstance(page_start, bool)
            or not isinstance(page_start, int)
            or page_start < 1
        ):
            page_start = None
        if (
            isinstance(page_end, bool)
            or not isinstance(page_end, int)
            or page_end < 1
        ):
            page_end = None
        if page_start is not None and page_end is not None and page_end < page_start:
            page_start = page_end = None
        return cls(
            locator_id=_require_string(
                values.get("locator_id", ""), field="source locator.locator_id"
            ),
            source_id=_require_string(
                values.get("source_id", ""), field="source locator.source_id"
            ),
            evidence_anchor_id=_require_string(
                values.get("evidence_anchor_id", ""),
                field="source locator.evidence_anchor_id",
            ),
            locator_type=_require_string(
                values.get("locator_type", "unknown"),
                field="source locator.locator_type",
            ),  # type: ignore[arg-type]
            value=_require_string(
                values.get("value", ""), field="source locator.value"
            ),
            page_start=page_start,
            page_end=page_end,
            source_native=values.get("source_native", False),
            supports_strong_assertion=values.get("supports_strong_assertion", False),
        )


@dataclass(frozen=True, slots=True)
class QuantitativeResult:
    """A typed numerical result retained without conflating estimands or scales."""

    quantitative_result_id: str = ""
    source_id: str = ""
    evidence_anchor_id: str = ""
    statistic: str = ""
    estimand_type: str = ""
    outcome_definition: str = ""
    estimate: str = ""
    unit: str = ""
    scale: str = ""
    baseline: str = ""
    reference_group: str = ""
    comparison_group: str = ""
    denominator: str = ""
    sample: str = ""
    uncertainty: str = ""
    population: str = ""
    period: str = ""
    model: str = ""
    provenance: Literal["source_reported", "system_derived", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            _require_string(
                getattr(self, field_name), field=f"quantitative result.{field_name}"
            )
        if self.provenance not in {"source_reported", "system_derived", "unknown"}:
            raise ValueError("quantitative result.provenance is invalid")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> QuantitativeResult:
        values = _model_payload(cls, payload, label="quantitative result")
        normalized = {
            field_name: _require_string(
                values.get(field_name, ""), field=f"quantitative result.{field_name}"
            )
            for field_name in cls.__dataclass_fields__
        }
        return cls(**normalized)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class StudyLineage:
    """Evidence-base lineage signals used to avoid publication-count inflation."""

    study_lineage_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    institutions: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    sampling_frame: str = ""
    unit_of_analysis: str = ""
    populations: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    publication_relationships: list[dict[str, Any]] = field(default_factory=list)
    institutional_series: str = ""
    overlap_signals: list[str] = field(default_factory=list)
    confidence: Literal["high", "moderate", "low", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        for field_name in (
            "study_lineage_id",
            "sampling_frame",
            "unit_of_analysis",
            "institutional_series",
            "confidence",
        ):
            _require_string(
                getattr(self, field_name), field=f"study lineage.{field_name}"
            )
        if self.confidence not in {"high", "moderate", "low", "unknown"}:
            raise ValueError("study lineage.confidence is invalid")
        for field_name in (
            "source_ids",
            "authors",
            "institutions",
            "datasets",
            "data_sources",
            "populations",
            "periods",
            "overlap_signals",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name), field=f"study lineage.{field_name}"
                ),
            )
        object.__setattr__(
            self,
            "publication_relationships",
            _mapping_list(
                self.publication_relationships,
                field="study lineage.publication_relationships",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StudyLineage:
        values = _model_payload(cls, payload, label="study lineage")
        return cls(
            study_lineage_id=_require_string(
                values.get("study_lineage_id", ""),
                field="study lineage.study_lineage_id",
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="study lineage.source_ids"
            ),
            authors=_string_list(
                values.get("authors", []), field="study lineage.authors"
            ),
            institutions=_string_list(
                values.get("institutions", []), field="study lineage.institutions"
            ),
            datasets=_string_list(
                values.get("datasets", []), field="study lineage.datasets"
            ),
            data_sources=_string_list(
                values.get("data_sources", []), field="study lineage.data_sources"
            ),
            sampling_frame=_require_string(
                values.get("sampling_frame", ""), field="study lineage.sampling_frame"
            ),
            unit_of_analysis=_require_string(
                values.get("unit_of_analysis", ""),
                field="study lineage.unit_of_analysis",
            ),
            populations=_string_list(
                values.get("populations", []), field="study lineage.populations"
            ),
            periods=_string_list(
                values.get("periods", []), field="study lineage.periods"
            ),
            publication_relationships=_mapping_list(
                values.get("publication_relationships", []),
                field="study lineage.publication_relationships",
            ),
            institutional_series=_require_string(
                values.get("institutional_series", ""),
                field="study lineage.institutional_series",
            ),
            overlap_signals=_string_list(
                values.get("overlap_signals", []), field="study lineage.overlap_signals"
            ),
            confidence=_require_string(
                values.get("confidence", "unknown"), field="study lineage.confidence"
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    """A revision-aware, source-located unit of evidence used by synthesis."""

    evidence_anchor_id: str = ""
    revision_hash: str = ""
    source_id: str = ""
    study_family_id: str = ""
    evidence_role: str = "support_unknown"
    claim: str = ""
    finding_type: str = ""
    direction: str = ""
    magnitude: str = ""
    comparison: str = ""
    conditions: list[str] = field(default_factory=list)
    plain_english_meaning: str = ""
    uncertainty: str = ""
    locator: str = ""
    locators: list[str] = field(default_factory=list)
    source_locators: list[SourceLocator] = field(default_factory=list)
    qualifiers: list[str] = field(default_factory=list)
    planning_roles: list[str] = field(default_factory=list)
    salience_priority: int = 0
    support_boundary: str = ""
    support_envelope: SupportEnvelope = field(default_factory=SupportEnvelope)
    quantitative_result: QuantitativeResult | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_anchor_id",
            "revision_hash",
            "source_id",
            "study_family_id",
            "evidence_role",
            "claim",
            "finding_type",
            "direction",
            "magnitude",
            "comparison",
            "plain_english_meaning",
            "uncertainty",
            "locator",
            "support_boundary",
        ):
            _require_string(
                getattr(self, field_name), field=f"evidence_anchor.{field_name}"
            )
        conditions = _string_list(self.conditions, field="evidence_anchor.conditions")
        locators = _string_list(self.locators, field="evidence_anchor.locators")
        qualifiers = _string_list(self.qualifiers, field="evidence_anchor.qualifiers")
        planning_roles = _string_list(
            self.planning_roles, field="evidence_anchor.planning_roles"
        )
        salience_priority = _nonnegative_int(
            self.salience_priority, field="evidence_anchor.salience_priority"
        )
        if self.locator and self.locator not in locators:
            locators.insert(0, self.locator)
        locator = self.locator or (locators[0] if locators else "")
        envelope = self.support_envelope
        if isinstance(envelope, Mapping):
            envelope = SupportEnvelope.from_dict(envelope)
        elif not isinstance(envelope, SupportEnvelope):
            raise ValueError(
                "evidence_anchor.support_envelope must be a SupportEnvelope or mapping"
            )
        source_locators: list[SourceLocator] = []
        for source_locator in self.source_locators:
            if isinstance(source_locator, SourceLocator):
                source_locators.append(source_locator)
            elif isinstance(source_locator, Mapping):
                source_locators.append(SourceLocator.from_dict(source_locator))
            else:
                raise ValueError(
                    "evidence_anchor.source_locators must contain SourceLocator values or mappings"
                )
        quantitative_result = self.quantitative_result
        if isinstance(quantitative_result, Mapping):
            quantitative_result = QuantitativeResult.from_dict(quantitative_result)
        elif quantitative_result is not None and not isinstance(
            quantitative_result, QuantitativeResult
        ):
            raise ValueError(
                "evidence_anchor.quantitative_result must be a QuantitativeResult, mapping, or null"
            )
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "locators", locators)
        object.__setattr__(self, "source_locators", source_locators)
        object.__setattr__(self, "qualifiers", qualifiers)
        object.__setattr__(self, "planning_roles", planning_roles)
        object.__setattr__(self, "salience_priority", salience_priority)
        object.__setattr__(self, "support_envelope", envelope)
        object.__setattr__(self, "quantitative_result", quantitative_result)
        if not self.evidence_anchor_id:
            object.__setattr__(self, "evidence_anchor_id", _base_anchor_id(self))
        object.__setattr__(self, "revision_hash", _anchor_revision_hash(self))

    def to_dict(self) -> dict[str, Any]:
        conditions = _string_list(self.conditions, field="evidence_anchor.conditions")
        locators = _string_list(self.locators, field="evidence_anchor.locators")
        qualifiers = _string_list(self.qualifiers, field="evidence_anchor.qualifiers")
        return {
            "evidence_anchor_id": self.evidence_anchor_id,
            "revision_hash": _anchor_revision_hash(self),
            "source_id": self.source_id,
            "study_family_id": self.study_family_id,
            "evidence_role": self.evidence_role,
            "claim": self.claim,
            "finding_type": self.finding_type,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "comparison": self.comparison,
            "conditions": conditions,
            "plain_english_meaning": self.plain_english_meaning,
            "uncertainty": self.uncertainty,
            "locator": self.locator,
            "locators": locators,
            "source_locators": [
                source_locator.to_dict() for source_locator in self.source_locators
            ],
            "qualifiers": qualifiers,
            "planning_roles": list(self.planning_roles),
            "salience_priority": self.salience_priority,
            "support_boundary": self.support_boundary,
            "support_envelope": self.support_envelope.to_dict(),
            "quantitative_result": (
                self.quantitative_result.to_dict()
                if self.quantitative_result is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceAnchor:
        if not isinstance(payload, Mapping):
            raise ValueError("evidence anchor must be a mapping")
        values = dict(payload)
        canonical_present = "evidence_anchor_id" in values
        aliases = {
            key: values[key] for key in ("finding_id", "claim_id") if key in values
        }
        alias_values = list(aliases.values())
        if any(not isinstance(value, str) for value in alias_values):
            raise ValueError("evidence anchor aliases must be strings")
        if len(set(alias_values)) > 1:
            raise ValueError("conflicting evidence anchor aliases")
        if (
            canonical_present
            and aliases
            and any(value != values["evidence_anchor_id"] for value in alias_values)
        ):
            raise ValueError("conflicting evidence_anchor_id and legacy alias")
        if not canonical_present and aliases:
            values["evidence_anchor_id"] = next(iter(aliases.values()))
        values.pop("finding_id", None)
        values.pop("claim_id", None)
        if isinstance(values.get("support_envelope"), str):
            boundary = str(values["support_envelope"]).strip()
            values["support_envelope"] = {
                "restrictions": [boundary] if boundary else [],
                "support_status": "supported",
            }
        values = _model_payload(cls, values, label="evidence anchor")
        envelope = values.get("support_envelope", {})
        if isinstance(envelope, SupportEnvelope):
            support_envelope = envelope
        elif isinstance(envelope, Mapping):
            support_envelope = SupportEnvelope.from_dict(envelope)
        else:
            raise ValueError("evidence_anchor.support_envelope must be a mapping")
        source_locators = values.get("source_locators", [])
        if not isinstance(source_locators, list):
            raise ValueError("evidence_anchor.source_locators must be a list")
        quantitative_result = values.get("quantitative_result")
        if isinstance(quantitative_result, QuantitativeResult):
            normalized_quantitative_result = quantitative_result
        elif isinstance(quantitative_result, Mapping):
            normalized_quantitative_result = QuantitativeResult.from_dict(
                quantitative_result
            )
        elif quantitative_result is None:
            normalized_quantitative_result = None
        else:
            raise ValueError(
                "evidence_anchor.quantitative_result must be a mapping or null"
            )
        return cls(
            evidence_anchor_id=values.get("evidence_anchor_id", ""),
            revision_hash=values.get("revision_hash", ""),
            source_id=values.get("source_id", ""),
            study_family_id=values.get("study_family_id", ""),
            evidence_role=values.get("evidence_role", "support_unknown"),
            claim=values.get("claim", ""),
            finding_type=values.get("finding_type", ""),
            direction=values.get("direction", ""),
            magnitude=values.get("magnitude", ""),
            comparison=values.get("comparison", ""),
            conditions=_string_list(
                values.get("conditions", []), field="evidence_anchor.conditions"
            ),
            plain_english_meaning=values.get("plain_english_meaning", ""),
            uncertainty=values.get("uncertainty", ""),
            locator=values.get("locator", ""),
            locators=_string_list(
                values.get("locators", []), field="evidence_anchor.locators"
            ),
            source_locators=[
                SourceLocator.from_dict(row)
                for row in _mapping_list(
                    source_locators, field="evidence_anchor.source_locators"
                )
            ],
            qualifiers=_string_list(
                values.get("qualifiers", []), field="evidence_anchor.qualifiers"
            ),
            planning_roles=_string_list(
                values.get("planning_roles", []),
                field="evidence_anchor.planning_roles",
            ),
            salience_priority=_nonnegative_int(
                values.get("salience_priority", 0),
                field="evidence_anchor.salience_priority",
            ),
            support_boundary=_require_string(
                values.get("support_boundary", ""),
                field="evidence_anchor.support_boundary",
            ),
            support_envelope=support_envelope,
            quantitative_result=normalized_quantitative_result,
        )


def _base_anchor_id(anchor: EvidenceAnchor) -> str:
    identity = {
        "source_id": anchor.source_id,
        "locators": _locator_identity(anchor.locator, anchor.locators),
        "evidence_role": anchor.evidence_role.casefold().strip(),
    }
    return f"anchor-{_stable_json_hash(identity)[:20]}"


def _anchor_revision_hash(anchor: EvidenceAnchor) -> str:
    content = {
        "source_id": anchor.source_id,
        "study_family_id": anchor.study_family_id,
        "evidence_role": anchor.evidence_role,
        "claim": anchor.claim,
        "finding_type": anchor.finding_type,
        "direction": anchor.direction,
        "magnitude": anchor.magnitude,
        "comparison": anchor.comparison,
        "conditions": anchor.conditions,
        "plain_english_meaning": anchor.plain_english_meaning,
        "uncertainty": anchor.uncertainty,
        "locator": anchor.locator,
        "locators": anchor.locators,
        "qualifiers": anchor.qualifiers,
        "support_envelope": anchor.support_envelope.to_dict(),
    }
    if anchor.source_locators:
        content["source_locators"] = [
            locator.to_dict() for locator in anchor.source_locators
        ]
    if anchor.quantitative_result is not None:
        content["quantitative_result"] = anchor.quantitative_result.to_dict()
    return _stable_json_hash(content)


def _evidence_roles(finding: EvidenceFinding) -> tuple[str, str, str]:
    finding_type = (
        finding.finding_type.casefold().strip().replace("-", "_").replace(" ", "_")
    )
    empirical_role = "none"
    argument_role = "none"
    if any(
        token in finding_type for token in ("causal", "experiment", "quasi_experiment")
    ):
        empirical_role = "causal"
    elif "mechanism" in finding_type or "process_tracing" in finding_type:
        empirical_role = "mechanism_evidence"
    elif (
        any(
            token in finding_type
            for token in ("association", "correlation", "regression", "statistical")
        )
        or finding.is_statistical
    ):
        empirical_role = "associational"
    elif any(
        token in finding_type for token in ("descriptive", "qualitative", "empirical")
    ):
        empirical_role = "descriptive"
    else:
        argument_role = next(
            (
                role
                for role in (
                    "conceptual",
                    "interpretive",
                    "normative",
                    "methodological",
                    "practitioner_guidance",
                )
                if role in finding_type
            ),
            "none",
        )
    evidence_role = empirical_role if empirical_role != "none" else argument_role
    return (
        empirical_role,
        argument_role,
        evidence_role if evidence_role != "none" else "support_unknown",
    )


def _profile_coverage(coverage: Mapping[str, Any]) -> str:
    explicit = (
        str(coverage.get("status") or coverage.get("coverage") or "").casefold().strip()
    )
    if explicit in {"full_text", "limited_text", "abstract", "metadata", "unknown"}:
        return explicit
    source_scope = str(coverage.get("source_scope") or "").casefold().strip()
    if source_scope == "full_document" and coverage.get("coverage_gate") == "passed":
        return "full_text"
    if "abstract" in source_scope:
        return "abstract"
    if "metadata" in source_scope:
        return "metadata"
    if source_scope:
        return "limited_text"
    return "unknown"


def _anchors_from_findings(
    findings: list[EvidenceFinding],
    *,
    source_id: str,
    study_family_id: str,
    coverage: Mapping[str, Any],
    scope: Mapping[str, list[str]],
    restrictions: list[str],
) -> list[EvidenceAnchor]:
    del scope  # Source-level retrieval metadata must not be inherited by every anchor.
    anchor_coverage = _profile_coverage(coverage)
    candidates: list[tuple[EvidenceFinding, EvidenceAnchor, str]] = []
    for finding in findings:
        empirical_role, argument_role, evidence_role = _evidence_roles(finding)
        support_status = "support_unknown"
        if (
            evidence_role != "support_unknown"
            and anchor_coverage == "full_text"
            and finding.locator
        ):
            support_status = "supported"
        elif evidence_role != "support_unknown" and anchor_coverage in {
            "limited_text",
            "abstract",
            "metadata",
        }:
            support_status = "limited"
        finding_scope = {
            key: values
            for key, values in {
                "populations": [finding.population] if finding.population else [],
                "outcomes": [finding.outcome] if finding.outcome else [],
            }.items()
            if values
        }
        envelope = SupportEnvelope(
            empirical_role=empirical_role,  # type: ignore[arg-type]
            argument_role=argument_role,  # type: ignore[arg-type]
            coverage=anchor_coverage,  # type: ignore[arg-type]
            scope=finding_scope,
            restrictions=list(dict.fromkeys([*restrictions, *finding.qualifiers])),
            support_status=support_status,  # type: ignore[arg-type]
        )
        anchor = EvidenceAnchor(
            source_id=source_id,
            study_family_id=study_family_id,
            evidence_role=evidence_role,
            claim=finding.claim,
            finding_type=finding.finding_type,
            direction=finding.direction,
            magnitude=finding.magnitude,
            comparison=finding.comparison,
            conditions=list(finding.conditions),
            plain_english_meaning=finding.plain_english_meaning,
            uncertainty=finding.uncertainty,
            locator=finding.locator,
            locators=list(finding.locators),
            qualifiers=list(finding.qualifiers),
            support_envelope=envelope,
        )
        source_span = finding.evidence.strip() or finding.finding_id.strip()
        if not source_span:
            span_identity = {
                "magnitude": finding.magnitude,
                "comparison": finding.comparison,
                "conditions": finding.conditions,
                "uncertainty": finding.uncertainty,
                "qualifiers": finding.qualifiers,
            }
            source_span = json.dumps(
                span_identity
                if any(span_identity.values())
                else {"claim": finding.claim},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        candidates.append(
            (finding, anchor, _stable_json_hash({"source_span": source_span}))
        )

    collisions: dict[str, int] = {}
    for _, anchor, _ in candidates:
        collisions[anchor.evidence_anchor_id] = (
            collisions.get(anchor.evidence_anchor_id, 0) + 1
        )

    anchors: list[EvidenceAnchor] = []
    for _, anchor, span_hash in candidates:
        anchor_id = anchor.evidence_anchor_id
        if collisions[anchor_id] > 1:
            anchor_id = f"{anchor_id}-{span_hash[:12]}"
        anchors.append(
            EvidenceAnchor.from_dict(
                {
                    **anchor.to_dict(),
                    "evidence_anchor_id": anchor_id,
                }
            )
        )
    return _dedupe_anchors(anchors)


def _dedupe_anchors(anchors: list[EvidenceAnchor]) -> list[EvidenceAnchor]:
    by_id: dict[str, EvidenceAnchor] = {}
    for anchor in anchors:
        existing = by_id.get(anchor.evidence_anchor_id)
        if existing is None or (anchor.revision_hash, anchor.claim) < (
            existing.revision_hash,
            existing.claim,
        ):
            by_id[anchor.evidence_anchor_id] = anchor
    return [by_id[anchor_id] for anchor_id in sorted(by_id)]


def _anchor_payload_with_bound_nested_ids(
    anchor: EvidenceAnchor, anchor_id: str
) -> dict[str, Any]:
    payload = anchor.to_dict()
    source_id = anchor.source_id
    payload["evidence_anchor_id"] = anchor_id
    payload["source_locators"] = [
        {
            **dict(row),
            "source_id": source_id,
            "evidence_anchor_id": anchor_id,
        }
        for row in payload.get("source_locators", []) or []
        if isinstance(row, Mapping)
    ]
    quantitative = payload.get("quantitative_result")
    if isinstance(quantitative, Mapping):
        payload["quantitative_result"] = {
            **dict(quantitative),
            "source_id": source_id,
            "evidence_anchor_id": anchor_id,
        }
    return payload


def _canonicalize_anchor_ids(anchors: list[EvidenceAnchor]) -> list[EvidenceAnchor]:
    canonical: list[tuple[EvidenceAnchor, str]] = []
    for anchor in anchors:
        base = EvidenceAnchor.from_dict(
            _anchor_payload_with_bound_nested_ids(anchor, "")
        )
        span_identity = {
            "locators": _locator_identity(base.locator, base.locators),
            "magnitude": base.magnitude,
            "comparison": base.comparison,
            "conditions": base.conditions,
            "uncertainty": base.uncertainty,
            "qualifiers": base.qualifiers,
        }
        span_hash = _stable_json_hash(span_identity)
        canonical.append((base, span_hash))
    collisions: dict[str, int] = {}
    for anchor, _ in canonical:
        collisions[anchor.evidence_anchor_id] = (
            collisions.get(anchor.evidence_anchor_id, 0) + 1
        )
    result: list[EvidenceAnchor] = []
    for anchor, span_hash in canonical:
        anchor_id = anchor.evidence_anchor_id
        if collisions[anchor_id] > 1:
            anchor_id = f"{anchor_id}-{span_hash[:12]}"
        result.append(
            EvidenceAnchor.from_dict(
                _anchor_payload_with_bound_nested_ids(anchor, anchor_id)
            )
        )
    return _dedupe_anchors(result)


EVIDENCE_ELIGIBILITY_VALUES = frozenset(
    {"substantive_bounded", "context_only", "unavailable"}
)


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """Measured provider limits that participate in call checkpoint identity."""

    provider: str = ""
    model: str = ""
    context_window_tokens: int = 0
    max_output_tokens: int = 0
    request_timeout_seconds: float = 0
    endpoint_restrictions: dict[str, Any] = field(default_factory=dict)
    capability_revision: str = "1"

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "capability_revision"):
            _require_string(
                getattr(self, field_name), field=f"provider capability.{field_name}"
            )
        if self.context_window_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("provider capability token limits must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("provider capability timeout must be positive")
        object.__setattr__(
            self,
            "endpoint_restrictions",
            _any_mapping(
                self.endpoint_restrictions,
                field="provider capability.endpoint_restrictions",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProviderCapability:
        values = _model_payload(cls, payload, label="provider capability")
        return cls(
            provider=_require_string(
                values.get("provider", ""), field="provider capability.provider"
            ),
            model=_require_string(
                values.get("model", ""), field="provider capability.model"
            ),
            context_window_tokens=int(values.get("context_window_tokens", 0)),
            max_output_tokens=int(values.get("max_output_tokens", 0)),
            request_timeout_seconds=float(
                values.get("request_timeout_seconds", 0)
            ),
            endpoint_restrictions=_any_mapping(
                values.get("endpoint_restrictions", {}),
                field="provider capability.endpoint_restrictions",
            ),
            capability_revision=_require_string(
                values.get("capability_revision", "1"),
                field="provider capability.capability_revision",
            ),
        )


@dataclass(frozen=True, slots=True)
class LiteraturePosition:
    """One source-local account of an important cited work."""

    literature_position_id: str = ""
    current_source_id: str = ""
    raw_citation: str = ""
    author: str = ""
    year: str = ""
    title: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)
    engagement: str = ""
    relation_label: str = ""
    locator: str = ""
    matched_source_id: str = ""
    provenance: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "literature_position_id",
            "current_source_id",
            "raw_citation",
            "author",
            "year",
            "title",
            "engagement",
            "relation_label",
            "locator",
            "matched_source_id",
            "provenance",
        ):
            _require_string(
                getattr(self, field_name), field=f"literature position.{field_name}"
            )
        if not self.current_source_id or not self.raw_citation or not self.engagement:
            raise ValueError(
                "literature position requires current_source_id, raw_citation, and engagement"
            )
        if not isinstance(self.identifiers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.identifiers.items()
        ):
            raise ValueError("literature position.identifiers must map strings to strings")
        if not self.literature_position_id:
            object.__setattr__(
                self,
                "literature_position_id",
                "literature-position-"
                + _stable_json_hash(
                    {
                        "source_id": self.current_source_id,
                        "citation": self.raw_citation,
                        "engagement": self.engagement,
                    }
                )[:16],
            )

    def semantic_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("matched_source_id", None)
        return payload

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LiteraturePosition:
        values = dict(payload)
        normalized = values.pop("normalized", {})
        if isinstance(normalized, Mapping):
            for field_name in ("author", "year", "title"):
                if not values.get(field_name) and normalized.get(field_name):
                    values[field_name] = str(normalized[field_name])
        if not values.get("engagement"):
            values["engagement"] = str(
                values.pop("merged_engagement_account", "")
                or values.pop("engagement_account", "")
                or values.pop("merged_engagement", "")
            )
        else:
            values.pop("merged_engagement_account", None)
            values.pop("engagement_account", None)
            values.pop("merged_engagement", None)
        values = _model_payload(cls, values, label="literature position")
        identifiers = values.get("identifiers", {})
        if not isinstance(identifiers, Mapping):
            raise ValueError("literature position.identifiers must be a mapping")
        return cls(
            literature_position_id=_require_string(
                values.get("literature_position_id", ""),
                field="literature position.literature_position_id",
            ),
            current_source_id=_require_string(
                values.get("current_source_id", ""),
                field="literature position.current_source_id",
            ),
            raw_citation=_require_string(
                values.get("raw_citation", ""),
                field="literature position.raw_citation",
            ),
            author=_require_string(
                values.get("author", ""), field="literature position.author"
            ),
            year=_require_string(
                values.get("year", ""), field="literature position.year"
            ),
            title=_require_string(
                values.get("title", ""), field="literature position.title"
            ),
            identifiers={
                str(key): str(value) for key, value in identifiers.items()
            },
            engagement=_require_string(
                values.get("engagement", ""),
                field="literature position.engagement",
            ),
            relation_label=_require_string(
                values.get("relation_label", ""),
                field="literature position.relation_label",
            ),
            locator=_require_string(
                values.get("locator", ""), field="literature position.locator"
            ),
            matched_source_id=_require_string(
                values.get("matched_source_id", ""),
                field="literature position.matched_source_id",
            ),
            provenance=_require_string(
                values.get("provenance", ""),
                field="literature position.provenance",
            ),
        )


@dataclass(frozen=True, slots=True)
class MissingSourceRecommendation:
    """A cited work worth acquiring without inventing a source note."""

    external_source_id: str = ""
    raw_citation: str = ""
    normalized_citation: dict[str, str] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    discussed_by_source_ids: list[str] = field(default_factory=list)
    importance: str = ""
    relevant_collections: list[str] = field(default_factory=list)
    relevant_topics: list[str] = field(default_factory=list)
    relevant_clusters: list[str] = field(default_factory=list)
    acquisition_priority: str = "normal"
    match_status: str = "unresolved"
    retrieval_status: str = "not_requested"
    ambiguity_notes: str = ""
    zotero_key: str = ""
    source_id: str = ""
    note_id: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "external_source_id",
            "raw_citation",
            "importance",
            "acquisition_priority",
            "match_status",
            "retrieval_status",
            "ambiguity_notes",
            "zotero_key",
            "source_id",
            "note_id",
        ):
            _require_string(
                getattr(self, field_name),
                field=f"missing source recommendation.{field_name}",
            )
        if not self.raw_citation:
            raise ValueError("missing source recommendation.raw_citation is required")
        object.__setattr__(
            self,
            "discussed_by_source_ids",
            _string_list(
                self.discussed_by_source_ids,
                field="missing source recommendation.discussed_by_source_ids",
            ),
        )
        for field_name in (
            "relevant_collections",
            "relevant_topics",
            "relevant_clusters",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name),
                    field=f"missing source recommendation.{field_name}",
                ),
            )
        for field_name in ("normalized_citation", "identifiers"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping) or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in value.items()
            ):
                raise ValueError(
                    f"missing source recommendation.{field_name} must map strings to strings"
                )
        if not self.external_source_id:
            object.__setattr__(
                self,
                "external_source_id",
                "external-source-"
                + _stable_json_hash(
                    {
                        "citation": self.raw_citation,
                        "identifiers": dict(self.identifiers),
                    }
                )[:16],
            )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> MissingSourceRecommendation:
        values = _model_payload(cls, payload, label="missing source recommendation")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class SourceAnalysisBundle:
    """Canonical source-owned output of one source-reading call."""

    bundle_schema_version: str = "1"
    source_identity: dict[str, str] = field(default_factory=dict)
    observed_bibliographic_identity: dict[str, Any] = field(default_factory=dict)
    scope_assessment: dict[str, Any] = field(default_factory=dict)
    analysis_sections: dict[str, str] = field(default_factory=dict)
    compact_profile: dict[str, Any] = field(default_factory=dict)
    evidence_anchors: list[EvidenceAnchor] = field(default_factory=list)
    literature_positions: list[LiteraturePosition] = field(default_factory=list)
    missing_source_recommendations: list[MissingSourceRecommendation] = field(
        default_factory=list
    )
    self_review: dict[str, Any] = field(default_factory=dict)
    component_diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.bundle_schema_version != "1":
            raise ValueError("source analysis bundle schema must be 1")
        source_identity = self.source_identity
        if not isinstance(source_identity, Mapping) or not any(
            str(source_identity.get(key) or "")
            for key in ("source_id", "zotero_key", "attachment_key")
        ):
            raise ValueError("source analysis bundle requires a stable source identity")
        if not isinstance(self.analysis_sections, Mapping) or not any(
            str(value).strip() for value in self.analysis_sections.values()
        ):
            raise ValueError("source analysis bundle requires usable analysis sections")
        for field_name in (
            "observed_bibliographic_identity",
            "scope_assessment",
            "compact_profile",
            "self_review",
        ):
            object.__setattr__(
                self,
                field_name,
                _any_mapping(
                    getattr(self, field_name),
                    field=f"source analysis bundle.{field_name}",
                ),
            )
        object.__setattr__(
            self,
            "source_identity",
            {str(key): str(value) for key, value in source_identity.items()},
        )
        object.__setattr__(
            self,
            "analysis_sections",
            {
                str(key): str(value)
                for key, value in self.analysis_sections.items()
                if str(value).strip()
            },
        )
        object.__setattr__(
            self,
            "component_diagnostics",
            _mapping_list(
                self.component_diagnostics,
                field="source analysis bundle.component_diagnostics",
            ),
        )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            **self.to_dict(),
            "literature_positions": [
                row.semantic_dict() for row in self.literature_positions
            ],
            "component_diagnostics": [],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_schema_version": self.bundle_schema_version,
            "source_identity": dict(self.source_identity),
            "observed_bibliographic_identity": dict(
                self.observed_bibliographic_identity
            ),
            "scope_assessment": dict(self.scope_assessment),
            "analysis_sections": dict(self.analysis_sections),
            "compact_profile": dict(self.compact_profile),
            "evidence_anchors": [
                anchor.to_dict() for anchor in self.evidence_anchors
            ],
            "literature_positions": [
                position.to_dict() for position in self.literature_positions
            ],
            "missing_source_recommendations": [
                recommendation.to_dict()
                for recommendation in self.missing_source_recommendations
            ],
            "self_review": dict(self.self_review),
            "component_diagnostics": list(self.component_diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceAnalysisBundle:
        values = _model_payload(cls, payload, label="source analysis bundle")
        diagnostics = _mapping_list(
            values.get("component_diagnostics", []),
            field="source analysis bundle.component_diagnostics",
        )

        def optional_mapping(field_name: str) -> dict[str, Any]:
            value = values.get(field_name, {})
            if isinstance(value, Mapping):
                return dict(value)
            diagnostics.append(
                {"component": field_name, "reason": "component_not_mapping"}
            )
            return {}

        def optional_rows(
            field_name: str, model: type[Any]
        ) -> list[Any]:
            rows = values.get(field_name, [])
            if not isinstance(rows, list):
                diagnostics.append(
                    {"component": field_name, "reason": "component_not_list"}
                )
                return []
            accepted = []
            for index, row in enumerate(rows):
                try:
                    if not isinstance(row, Mapping):
                        raise ValueError("row is not a mapping")
                    accepted.append(model.from_dict(row))
                except (TypeError, ValueError) as exc:
                    diagnostics.append(
                        {
                            "component": field_name,
                            "row_index": index,
                            "reason": f"{type(exc).__name__}:{exc}",
                            "raw": dict(row) if isinstance(row, Mapping) else row,
                        }
                    )
            return accepted

        schema_value = values.get("bundle_schema_version", "1")
        if (
            isinstance(schema_value, (int, float))
            and not isinstance(schema_value, bool)
            and float(schema_value) == 1.0
        ) or (
            isinstance(schema_value, str)
            and schema_value.strip().casefold() in {"1", "1.0", "v1"}
        ):
            schema_value = "1"
        return cls(
            bundle_schema_version=_require_string(
                schema_value,
                field="source analysis bundle.bundle_schema_version",
            ),
            source_identity=_any_mapping(
                values.get("source_identity", {}),
                field="source analysis bundle.source_identity",
            ),
            observed_bibliographic_identity=optional_mapping(
                "observed_bibliographic_identity"
            ),
            scope_assessment=optional_mapping("scope_assessment"),
            analysis_sections={
                str(key): _readable_bundle_text(value)
                for key, value in _any_mapping(
                    values.get("analysis_sections", {}),
                    field="source analysis bundle.analysis_sections",
                ).items()
            },
            compact_profile=_normalized_bundle_profile(
                optional_mapping("compact_profile")
            ),
            evidence_anchors=optional_rows("evidence_anchors", EvidenceAnchor),
            literature_positions=optional_rows(
                "literature_positions", LiteraturePosition
            ),
            missing_source_recommendations=optional_rows(
                "missing_source_recommendations", MissingSourceRecommendation
            ),
            self_review=optional_mapping("self_review"),
            component_diagnostics=diagnostics,
        )


@dataclass(frozen=True, slots=True)
class RelationshipPairJob:
    """Immutable semantic unit for one cross-source adjudication."""

    pair_job_id: str = ""
    catalogue_revision: str = ""
    left_source_id: str = ""
    right_source_id: str = ""
    profiles: dict[str, Any] = field(default_factory=dict)
    atomic_notes: dict[str, Any] = field(default_factory=dict)
    literature_positions: list[dict[str, Any]] = field(default_factory=list)
    selected_evidence: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    graph_context: dict[str, Any] = field(default_factory=dict)
    candidate_basis: list[dict[str, Any]] = field(default_factory=list)
    prior_pair_memory: dict[str, Any] = field(default_factory=dict)
    output_contract: str = "relationship-decision-v8"

    def __post_init__(self) -> None:
        if (
            not self.left_source_id
            or not self.right_source_id
            or self.left_source_id == self.right_source_id
        ):
            raise ValueError("relationship pair job requires two distinct source IDs")
        if self.output_contract not in {
            "relationship-decision-v4",
            "relationship-decision-v5",
            "relationship-decision-v6",
            "relationship-decision-v7",
            "relationship-decision-v8",
        }:
            raise ValueError("relationship pair job output contract is invalid")
        left, right = sorted((self.left_source_id, self.right_source_id))
        object.__setattr__(self, "left_source_id", left)
        object.__setattr__(self, "right_source_id", right)
        for field_name in (
            "profiles",
            "atomic_notes",
            "selected_evidence",
            "graph_context",
            "prior_pair_memory",
        ):
            object.__setattr__(
                self,
                field_name,
                _any_mapping(
                    getattr(self, field_name),
                    field=f"relationship pair job.{field_name}",
                ),
            )
        object.__setattr__(
            self,
            "literature_positions",
            _mapping_list(
                self.literature_positions,
                field="relationship pair job.literature_positions",
            ),
        )
        object.__setattr__(
            self,
            "candidate_basis",
            _mapping_list(
                self.candidate_basis, field="relationship pair job.candidate_basis"
            ),
        )
        if not self.pair_job_id:
            object.__setattr__(
                self,
                "pair_job_id",
                "relationship-job-"
                + _stable_json_hash(
                    {
                        "pair": [left, right],
                        "endpoint_semantic_hashes": {
                            side: str(
                                _any_mapping(
                                    self.atomic_notes.get(side, {}),
                                    field=(
                                        "relationship pair job.atomic_notes."
                                        + side
                                    ),
                                ).get("semantic_hash")
                                or _any_mapping(
                                    self.profiles.get(side, {}),
                                    field=(
                                        "relationship pair job.profiles."
                                        + side
                                    ),
                                ).get("dependency_hash")
                                or _any_mapping(
                                    self.profiles.get(side, {}),
                                    field=(
                                        "relationship pair job.profiles."
                                        + side
                                    ),
                                ).get("note_hash")
                                or ""
                            )
                            for side in ("left", "right")
                        },
                        "adjudication_evidence": {
                            "literature_positions": self.literature_positions,
                            "selected_evidence": self.selected_evidence,
                            "candidate_basis": self.candidate_basis,
                            "pair_context": self.graph_context.get(
                                "pair_context", {}
                            ),
                        },
                        "output_contract": self.output_contract,
                    }
                )[:20],
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_job_id": self.pair_job_id,
            "catalogue_revision": self.catalogue_revision,
            "pair": {
                "left_source_id": self.left_source_id,
                "right_source_id": self.right_source_id,
            },
            "profiles": dict(self.profiles),
            "atomic_notes": dict(self.atomic_notes),
            "literature_positions": list(self.literature_positions),
            "selected_evidence": dict(self.selected_evidence),
            "graph_context": dict(self.graph_context),
            "candidate_basis": list(self.candidate_basis),
            "prior_pair_memory": dict(self.prior_pair_memory),
            "output_contract": self.output_contract,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RelationshipPairJob:
        values = dict(payload)
        pair = values.pop("pair", {})
        if not isinstance(pair, Mapping):
            raise ValueError("relationship pair job.pair must be a mapping")
        values["left_source_id"] = pair.get("left_source_id", "")
        values["right_source_id"] = pair.get("right_source_id", "")
        values = _model_payload(cls, values, label="relationship pair job")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RelationshipProviderBatch:
    """Transport-only grouping of immutable relationship pair jobs."""

    batch_id: str = ""
    pair_job_ids: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    capability_identity: str = ""
    serialized_context_fingerprint: str = ""

    def __post_init__(self) -> None:
        pair_job_ids = _string_list(
            self.pair_job_ids, field="relationship provider batch.pair_job_ids"
        )
        if not 1 <= len(pair_job_ids) <= 30 or len(pair_job_ids) != len(
            set(pair_job_ids)
        ):
            raise ValueError(
                "relationship provider batch requires one to thirty unique pair jobs"
            )
        object.__setattr__(self, "pair_job_ids", pair_job_ids)
        if not self.batch_id:
            object.__setattr__(
                self,
                "batch_id",
                "relationship-batch-"
                + _stable_json_hash(
                    {
                        "pair_job_ids": pair_job_ids,
                        "provider": self.provider,
                        "model": self.model,
                        "capability_identity": self.capability_identity,
                        "serialized_context_fingerprint": self.serialized_context_fingerprint,
                    }
                )[:20],
            )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> RelationshipProviderBatch:
        return cls(**_model_payload(cls, payload, label="relationship provider batch"))


@dataclass(frozen=True, slots=True)
class RelationshipDecision:
    """One complete final relationship judgment; partial semantic edits are impossible."""

    pair_job_id: str = ""
    decision: Literal["relationship", "no_relationship", "needs_more_context"] = (
        "no_relationship"
    )
    left_source_id: str = ""
    right_source_id: str = ""
    relation_type: str = ""
    secondary_relation_types: list[str] = field(default_factory=list)
    relationship_tier: str = ""
    actor_source_id: str = ""
    reference_source_id: str = ""
    forward_label: str = ""
    inverse_label: str = ""
    comparison_proposition: str = ""
    reason: str = ""
    left_endpoint_claim: str = ""
    right_endpoint_claim: str = ""
    left_evidence_anchor_ids: list[str] = field(default_factory=list)
    right_evidence_anchor_ids: list[str] = field(default_factory=list)
    boundary_or_qualification: str = ""
    confidence: str = ""
    connection_id: str = ""
    output_contract: str = "relationship-decision-v8"

    def __post_init__(self) -> None:
        if self.decision not in {
            "relationship",
            "no_relationship",
            "needs_more_context",
        }:
            raise ValueError("relationship decision is invalid")
        if self.output_contract not in {
            "relationship-decision-v4",
            "relationship-decision-v5",
            "relationship-decision-v6",
            "relationship-decision-v7",
            "relationship-decision-v8",
        }:
            raise ValueError("relationship decision output contract is invalid")
        pair = {self.left_source_id, self.right_source_id}
        if len(pair) != 2 or "" in pair:
            raise ValueError("relationship decision requires two distinct source IDs")
        object.__setattr__(
            self,
            "left_evidence_anchor_ids",
            _string_list(
                self.left_evidence_anchor_ids,
                field="relationship decision.left_evidence_anchor_ids",
            ),
        )
        object.__setattr__(
            self,
            "right_evidence_anchor_ids",
            _string_list(
                self.right_evidence_anchor_ids,
                field="relationship decision.right_evidence_anchor_ids",
            ),
        )
        object.__setattr__(
            self,
            "secondary_relation_types",
            _string_list(
                self.secondary_relation_types,
                field="relationship decision.secondary_relation_types",
            ),
        )
        if self.decision == "relationship":
            if any(
                relation_type == self.relation_type
                for relation_type in self.secondary_relation_types
            ):
                raise ValueError(
                    "secondary relationship types cannot repeat the primary type"
                )
            expected_tier = (
                "contextual"
                if self.relation_type == "contextual_connection"
                else "direct"
            )
            if not self.relationship_tier:
                object.__setattr__(self, "relationship_tier", expected_tier)
            elif self.relationship_tier != expected_tier:
                raise ValueError(
                    "relationship decision tier does not match relation type"
                )
            if (
                {self.actor_source_id, self.reference_source_id} != pair
                or self.actor_source_id == self.reference_source_id
            ):
                raise ValueError(
                    "relationship decision direction must use both pair endpoints"
                )
            if not all(
                (
                    self.relation_type,
                    self.forward_label,
                    self.inverse_label,
                    self.comparison_proposition,
                    self.reason,
                    (
                        self.left_endpoint_claim
                        if self.output_contract
                        in {"relationship-decision-v7", "relationship-decision-v8"}
                        else "legacy"
                    ),
                    (
                        self.right_endpoint_claim
                        if self.output_contract
                        in {"relationship-decision-v7", "relationship-decision-v8"}
                        else "legacy"
                    ),
                    (
                        self.left_evidence_anchor_ids
                        if self.output_contract != "relationship-decision-v8"
                        else ["optional"]
                    ),
                    (
                        self.right_evidence_anchor_ids
                        if self.output_contract != "relationship-decision-v8"
                        else ["optional"]
                    ),
                )
            ):
                raise ValueError(
                    "relationship decision requires a complete semantic record"
                )
        elif any(
            (
                self.relation_type,
                self.secondary_relation_types,
                self.relationship_tier,
                self.actor_source_id,
                self.reference_source_id,
                self.forward_label,
                self.inverse_label,
                self.left_evidence_anchor_ids,
                self.right_evidence_anchor_ids,
            )
        ):
            raise ValueError(
                "non-relationship decisions cannot contain an active relationship"
            )
        if not self.reason:
            raise ValueError("relationship decision.reason is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_job_id": self.pair_job_id,
            "decision": self.decision,
            "pair": {
                "left_source_id": self.left_source_id,
                "right_source_id": self.right_source_id,
            },
            "relation_type": self.relation_type,
            "secondary_relation_types": list(self.secondary_relation_types),
            "relationship_tier": self.relationship_tier,
            "actor_source_id": self.actor_source_id,
            "reference_source_id": self.reference_source_id,
            "forward_label": self.forward_label,
            "inverse_label": self.inverse_label,
            "comparison_proposition": self.comparison_proposition,
            "reason": self.reason,
            "left_endpoint_claim": self.left_endpoint_claim,
            "right_endpoint_claim": self.right_endpoint_claim,
            "left_evidence_anchor_ids": list(self.left_evidence_anchor_ids),
            "right_evidence_anchor_ids": list(self.right_evidence_anchor_ids),
            "boundary_or_qualification": self.boundary_or_qualification,
            "confidence": self.confidence,
            "connection_id": self.connection_id,
            "output_contract": self.output_contract,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RelationshipDecision:
        values = dict(payload)
        pair = values.pop("pair", {})
        if not isinstance(pair, Mapping):
            raise ValueError("relationship decision.pair must be a mapping")
        values["left_source_id"] = pair.get("left_source_id", "")
        values["right_source_id"] = pair.get("right_source_id", "")
        return cls(**_model_payload(cls, values, label="relationship decision"))


@dataclass(frozen=True, slots=True)
class ClusterPlanningCard:
    """Compact source card for a global cluster-planning call."""

    source_id: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    evidence_references: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("cluster planning card.source_id is required")
        object.__setattr__(
            self,
            "profile",
            _any_mapping(self.profile, field="cluster planning card.profile"),
        )
        references = _mapping_list(
            self.evidence_references,
            field="cluster planning card.evidence_references",
        )
        if not 1 <= len(references) <= 5:
            raise ValueError(
                "cluster planning card requires one to five evidence references"
            )
        seen: set[str] = set()
        for reference in references:
            anchor_id = str(reference.get("evidence_anchor_id") or "")
            if (
                not anchor_id
                or anchor_id in seen
                or not str(reference.get("proposition") or "")
            ):
                raise ValueError("cluster planning card contains invalid evidence")
            seen.add(anchor_id)
        object.__setattr__(self, "evidence_references", references)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterPlanningCard:
        return cls(**_model_payload(cls, payload, label="cluster planning card"))


@dataclass(slots=True)
class EvidenceProfile:
    """Source-level features and findings consumed by literature synthesis."""

    profile_schema: str = "evidence_profile"
    profile_schema_version: str = CURRENT_PROFILE_SCHEMA_VERSION
    profile_id: str = ""
    note_id: str = ""
    source_id: str = ""
    note_hash: str = ""
    source_hash: str = ""
    source_role: str = ""
    coverage: dict[str, Any] = field(default_factory=dict)
    validity: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    evidence_eligibility: Literal[
        "substantive_bounded", "context_only", "unavailable"
    ] = "substantive_bounded"
    # Legacy in-memory mirror. v1.3 serialization uses evidence_eligibility only.
    excluded_from_synthesis: bool = False
    exclusion_reason: str = ""
    features: dict[str, list[str]] = field(default_factory=dict)
    research_questions: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    theories: list[str] = field(default_factory=list)
    mechanisms: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    populations: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    study_family_id: str = ""
    study_lineage: StudyLineage | None = None
    findings: list[EvidenceFinding] = field(default_factory=list)
    evidence_anchors: list[EvidenceAnchor] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    future_research: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    dependency_hash: str = ""

    def __post_init__(self) -> None:
        _require_bool(self.excluded_from_synthesis, field="excluded_from_synthesis")
        if self.evidence_eligibility not in EVIDENCE_ELIGIBILITY_VALUES:
            raise ValueError("evidence_eligibility is invalid")
        if (
            self.excluded_from_synthesis
            and self.evidence_eligibility == "substantive_bounded"
        ):
            self.evidence_eligibility = "context_only"
        self.excluded_from_synthesis = (
            self.evidence_eligibility != "substantive_bounded"
        )
        if self.profile_schema != "evidence_profile":
            raise ValueError("profile_schema must be evidence_profile")
        if self.profile_schema_version not in {
            "1.0",
            "1.1",
            "1.2",
            CURRENT_PROFILE_SCHEMA_VERSION,
        }:
            raise ValueError("profile_schema_version must be 1.0, 1.1, 1.2, or 1.3")
        lineage = self.study_lineage
        if isinstance(lineage, Mapping):
            lineage = StudyLineage.from_dict(lineage)
        elif lineage is not None and not isinstance(lineage, StudyLineage):
            raise ValueError("study_lineage must be a StudyLineage, mapping, or null")
        self.study_lineage = lineage
        normalized_findings: list[EvidenceFinding] = []
        for finding in self.findings:
            if isinstance(finding, EvidenceFinding):
                normalized_findings.append(finding)
            elif isinstance(finding, Mapping):
                normalized_findings.append(EvidenceFinding.from_dict(finding))
            else:
                raise ValueError(
                    "findings must contain EvidenceFinding values or mappings"
                )
        self.findings = normalized_findings
        normalized_anchors: list[EvidenceAnchor] = []
        for anchor in self.evidence_anchors:
            if isinstance(anchor, EvidenceAnchor):
                normalized_anchors.append(anchor)
            elif isinstance(anchor, Mapping):
                normalized_anchors.append(EvidenceAnchor.from_dict(anchor))
            else:
                raise ValueError(
                    "evidence_anchors must contain EvidenceAnchor values or mappings"
                )
        if len(normalized_anchors) > 24:
            raise ValueError("evidence_anchors cannot contain more than 24 items")
        if not normalized_anchors and normalized_findings:
            scope = {
                "populations": list(self.populations),
                "outcomes": list(self.outcomes),
                "geography": list(self.geography),
                "periods": list(self.periods),
                "cases": list(self.cases),
            }
            normalized_anchors = _anchors_from_findings(
                normalized_findings,
                source_id=self.source_id,
                study_family_id=self.study_family_id,
                coverage=self.coverage,
                scope=scope,
                restrictions=[*self.boundaries, *self.limitations],
            )
        elif normalized_anchors:
            normalized_anchors = _canonicalize_anchor_ids(normalized_anchors)
        if len(normalized_anchors) > 24:
            raise ValueError("evidence_anchors cannot contain more than 24 items")
        self.evidence_anchors = normalized_anchors
        self.profile_schema_version = CURRENT_PROFILE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["profile_schema_version"] = CURRENT_PROFILE_SCHEMA_VERSION
        payload.pop("excluded_from_synthesis", None)
        payload["study_lineage"] = (
            self.study_lineage.to_dict() if self.study_lineage is not None else None
        )
        payload["evidence_anchors"] = [
            anchor.to_dict() for anchor in self.evidence_anchors
        ]
        return payload


@dataclass(frozen=True, slots=True)
class LiteratureProposition:
    """A map-local proposition backed by comparable source-level anchors."""

    proposition_id: str = ""
    semantic_identity: str = ""
    statement: str = ""
    question: str = ""
    proposition_type: str = ""
    signature: dict[str, Any] = field(default_factory=dict)
    source_ids: list[str] = field(default_factory=list)
    study_family_ids: list[str] = field(default_factory=list)
    independent_study_family_count: int = 0
    cells: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    comparability: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "proposition_id",
            "semantic_identity",
            "statement",
            "question",
            "proposition_type",
        ):
            _require_string(
                getattr(self, field_name), field=f"literature proposition.{field_name}"
            )
        object.__setattr__(
            self,
            "signature",
            _any_mapping(self.signature, field="literature proposition.signature"),
        )
        object.__setattr__(
            self,
            "source_ids",
            _string_list(self.source_ids, field="literature proposition.source_ids"),
        )
        object.__setattr__(
            self,
            "study_family_ids",
            _string_list(
                self.study_family_ids, field="literature proposition.study_family_ids"
            ),
        )
        object.__setattr__(
            self,
            "independent_study_family_count",
            _nonnegative_int(
                self.independent_study_family_count,
                field="literature proposition.independent_study_family_count",
            ),
        )
        if self.independent_study_family_count != len(set(self.study_family_ids)):
            raise ValueError(
                "literature proposition.independent_study_family_count must match study_family_ids"
            )
        object.__setattr__(
            self,
            "cells",
            _mapping_list(self.cells, field="literature proposition.cells"),
        )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="literature proposition.evidence"),
        )
        object.__setattr__(
            self,
            "comparability",
            _any_mapping(
                self.comparability, field="literature proposition.comparability"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LiteratureProposition:
        normalized = dict(payload)
        if "participating_core_sources" in normalized:
            participating_sources = _string_list(
                normalized.pop("participating_core_sources"),
                field="literature proposition.participating_core_sources",
            )
            if "source_ids" in normalized:
                source_ids = _string_list(
                    normalized["source_ids"],
                    field="literature proposition.source_ids",
                )
                if source_ids != participating_sources:
                    raise ValueError(
                        "conflicting literature proposition source aliases"
                    )
            else:
                normalized["source_ids"] = participating_sources
        if "supporting_evidence" in normalized:
            if (
                "evidence" in normalized
                and normalized["evidence"] != normalized["supporting_evidence"]
            ):
                raise ValueError("conflicting literature proposition evidence aliases")
            normalized["evidence"] = normalized.pop("supporting_evidence")
        flattened_evidence_fields = ("source_id", "evidence_anchor_id", "locator")
        if any(field_name in normalized for field_name in flattened_evidence_fields):
            missing = [
                field_name
                for field_name in flattened_evidence_fields
                if not isinstance(normalized.get(field_name), str)
                or not str(normalized.get(field_name)).strip()
            ]
            if missing:
                raise ValueError(
                    "flattened literature proposition evidence requires non-empty "
                    "source_id, evidence_anchor_id, and locator strings"
                )
            flattened_evidence = {
                field_name: str(normalized.pop(field_name)).strip()
                for field_name in flattened_evidence_fields
            }
            evidence = _mapping_list(
                normalized.get("evidence", []),
                field="literature proposition.evidence",
            )
            matching_rows = [
                row
                for row in evidence
                if str(row.get("source_id") or "") == flattened_evidence["source_id"]
                and str(
                    row.get("evidence_anchor_id")
                    or row.get("finding_id")
                    or row.get("claim_id")
                    or ""
                )
                == flattened_evidence["evidence_anchor_id"]
            ]
            if matching_rows and any(
                str(row.get("locator") or "") != flattened_evidence["locator"]
                for row in matching_rows
            ):
                raise ValueError(
                    "conflicting flattened literature proposition evidence"
                )
            if not matching_rows:
                evidence.append(flattened_evidence)
            normalized["evidence"] = evidence
        if isinstance(normalized.get("comparability"), str):
            comparability_summary = str(normalized["comparability"]).strip()
            normalized["comparability"] = (
                {"summary": comparability_summary} if comparability_summary else {}
            )
        values = _model_payload(cls, normalized, label="literature proposition")
        return cls(
            proposition_id=_require_string(
                values.get("proposition_id", ""),
                field="literature proposition.proposition_id",
            ),
            semantic_identity=_require_string(
                values.get("semantic_identity", ""),
                field="literature proposition.semantic_identity",
            ),
            statement=_require_string(
                values.get("statement", ""), field="literature proposition.statement"
            ),
            question=_require_string(
                values.get("question", ""), field="literature proposition.question"
            ),
            proposition_type=_require_string(
                values.get("proposition_type", ""),
                field="literature proposition.proposition_type",
            ),
            signature=_any_mapping(
                values.get("signature", {}), field="literature proposition.signature"
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="literature proposition.source_ids"
            ),
            study_family_ids=_string_list(
                values.get("study_family_ids", []),
                field="literature proposition.study_family_ids",
            ),
            independent_study_family_count=_nonnegative_int(
                values.get(
                    "independent_study_family_count",
                    len(set(values.get("study_family_ids", []))),
                ),
                field="literature proposition.independent_study_family_count",
            ),
            cells=_mapping_list(
                values.get("cells", []), field="literature proposition.cells"
            ),
            evidence=_mapping_list(
                values.get("evidence", []), field="literature proposition.evidence"
            ),
            comparability=_any_mapping(
                values.get("comparability", {}),
                field="literature proposition.comparability",
            ),
        )


@dataclass(frozen=True, slots=True)
class SynthesisAssertion:
    """A proposition-linked assertion admitted into a cluster synthesis."""

    assertion_id: str = ""
    item_id: str = ""
    cluster_id: str = ""
    section: str = ""
    statement: str = ""
    proposition_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    support_status: str = ""
    qualifiers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "assertion_id",
            "item_id",
            "cluster_id",
            "section",
            "statement",
            "support_status",
        ):
            _require_string(
                getattr(self, field_name), field=f"synthesis assertion.{field_name}"
            )
        if self.assertion_id and self.item_id and self.assertion_id != self.item_id:
            raise ValueError("synthesis assertion.assertion_id and item_id must match")
        if self.support_status not in {
            "",
            "supported",
            "support_unknown",
            "limited",
            "unsupported",
        }:
            raise ValueError("synthesis assertion.support_status is invalid")
        object.__setattr__(
            self,
            "proposition_ids",
            _string_list(
                self.proposition_ids, field="synthesis assertion.proposition_ids"
            ),
        )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="synthesis assertion.evidence"),
        )
        object.__setattr__(
            self,
            "qualifiers",
            _string_list(self.qualifiers, field="synthesis assertion.qualifiers"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SynthesisAssertion:
        normalized = dict(payload)
        if "proposition_id" in normalized:
            proposition_id = _require_string(
                normalized.pop("proposition_id"),
                field="synthesis assertion.proposition_id",
            )
            if "proposition_ids" in normalized:
                proposition_ids = _string_list(
                    normalized["proposition_ids"],
                    field="synthesis assertion.proposition_ids",
                )
                if proposition_ids != [proposition_id]:
                    raise ValueError(
                        "conflicting synthesis assertion proposition aliases"
                    )
            else:
                normalized["proposition_ids"] = [proposition_id]
        statement_aliases = [
            normalized.pop(key)
            for key in (
                "assertion",
                "finding",
                "position",
                "agreement",
                "contradiction",
                "text",
                "summary",
            )
            if key in normalized
        ]
        supplied_statements = [
            value for value in statement_aliases if value not in (None, "")
        ]
        if "statement" in normalized:
            supplied_statements.append(normalized["statement"])
        if any(not isinstance(value, str) for value in supplied_statements):
            raise ValueError("synthesis assertion statement aliases must be strings")
        if len(set(supplied_statements)) > 1:
            raise ValueError("conflicting synthesis assertion statement aliases")
        if "statement" not in normalized and supplied_statements:
            normalized["statement"] = supplied_statements[0]
        evidence_aliases = [
            normalized.pop(key)
            for key in ("supporting_evidence", "evidence_anchors")
            if key in normalized
        ]
        supplied_evidence = [
            value for value in evidence_aliases if value not in (None, [])
        ]
        if "evidence" in normalized and normalized["evidence"] not in (None, []):
            supplied_evidence.append(normalized["evidence"])
        if len({json.dumps(value, sort_keys=True) for value in supplied_evidence}) > 1:
            raise ValueError("conflicting synthesis assertion evidence aliases")
        if "evidence" not in normalized and supplied_evidence:
            normalized["evidence"] = supplied_evidence[0]
        evidence = normalized.get("evidence")
        if (
            isinstance(evidence, list)
            and evidence
            and all(isinstance(value, str) for value in evidence)
        ):
            parsed_evidence: list[dict[str, str]] = []
            for value in evidence:
                parts = str(value).strip().split(maxsplit=2)
                if (
                    len(parts) != 3
                    or not parts[0].startswith("source-")
                    or not parts[1].startswith("anchor-")
                    or not parts[2].strip()
                ):
                    raise ValueError(
                        "synthesis assertion string evidence must contain source_id, evidence_anchor_id, and locator"
                    )
                parsed_evidence.append(
                    {
                        "source_id": parts[0],
                        "evidence_anchor_id": parts[1],
                        "locator": parts[2].strip(),
                    }
                )
            normalized["evidence"] = parsed_evidence
        values = _model_payload(cls, normalized, label="synthesis assertion")
        return cls(
            assertion_id=_require_string(
                values.get("assertion_id", ""), field="synthesis assertion.assertion_id"
            ),
            item_id=_require_string(
                values.get("item_id", ""), field="synthesis assertion.item_id"
            ),
            cluster_id=_require_string(
                values.get("cluster_id", ""), field="synthesis assertion.cluster_id"
            ),
            section=_require_string(
                values.get("section", ""), field="synthesis assertion.section"
            ),
            statement=_require_string(
                values.get("statement", ""), field="synthesis assertion.statement"
            ),
            proposition_ids=_string_list(
                values.get("proposition_ids", []),
                field="synthesis assertion.proposition_ids",
            ),
            evidence=_mapping_list(
                values.get("evidence", []), field="synthesis assertion.evidence"
            ),
            support_status=_require_string(
                values.get("support_status", ""),
                field="synthesis assertion.support_status",
            ),
            qualifiers=_string_list(
                values.get("qualifiers", []), field="synthesis assertion.qualifiers"
            ),
        )


@dataclass(frozen=True, slots=True)
class ClusterSourceContribution:
    """One cluster-relevant contribution without implying cross-source agreement."""

    contribution_id: str = ""
    source_id: str = ""
    cluster_role: Literal["core", "context", "bridge"] = "context"
    contribution_kind: Literal[
        "direct_proposition_finding",
        "unique_cluster_relevant_finding",
        "boundary_evidence",
        "methodological_context",
        "conceptual_context",
        "bridge_evidence",
    ] = "conceptual_context"
    related_proposition_ids: list[str] = field(default_factory=list)
    evidence_thread_id: str = ""
    finding: str = ""
    technical_result: str = ""
    plain_english_meaning: str = ""
    relation_to_cluster_question: str = ""
    comparison_status: Literal[
        "single_source",
        "supports_shared_pattern",
        "contrasts_with_shared_pattern",
        "context_only",
    ] = "context_only"
    origin: Literal["reasoner", "deterministic_profile_fallback"] = "reasoner"
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "contribution_id",
            "source_id",
            "cluster_role",
            "contribution_kind",
            "evidence_thread_id",
            "finding",
            "technical_result",
            "plain_english_meaning",
            "relation_to_cluster_question",
            "comparison_status",
            "origin",
        ):
            _require_string(
                getattr(self, field_name),
                field=f"cluster source contribution.{field_name}",
            )
        if self.cluster_role not in {"core", "context", "bridge"}:
            raise ValueError("cluster source contribution.cluster_role is invalid")
        allowed_kinds = {
            "direct_proposition_finding",
            "unique_cluster_relevant_finding",
            "boundary_evidence",
            "methodological_context",
            "conceptual_context",
            "bridge_evidence",
        }
        if self.contribution_kind not in allowed_kinds:
            raise ValueError("cluster source contribution.contribution_kind is invalid")
        if self.comparison_status not in {
            "single_source",
            "supports_shared_pattern",
            "contrasts_with_shared_pattern",
            "context_only",
        }:
            raise ValueError("cluster source contribution.comparison_status is invalid")
        if self.origin not in {"reasoner", "deterministic_profile_fallback"}:
            raise ValueError("cluster source contribution.origin is invalid")
        object.__setattr__(
            self,
            "related_proposition_ids",
            _string_list(
                self.related_proposition_ids,
                field="cluster source contribution.related_proposition_ids",
            ),
        )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="cluster source contribution.evidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterSourceContribution:
        values = _model_payload(cls, payload, label="cluster source contribution")
        return cls(
            contribution_id=_require_string(
                values.get("contribution_id", ""),
                field="cluster source contribution.contribution_id",
            ),
            source_id=_require_string(
                values.get("source_id", ""),
                field="cluster source contribution.source_id",
            ),
            cluster_role=_require_string(
                values.get("cluster_role", "context"),
                field="cluster source contribution.cluster_role",
            ),  # type: ignore[arg-type]
            contribution_kind=_require_string(
                values.get("contribution_kind", "conceptual_context"),
                field="cluster source contribution.contribution_kind",
            ),  # type: ignore[arg-type]
            related_proposition_ids=_string_list(
                values.get("related_proposition_ids", []),
                field="cluster source contribution.related_proposition_ids",
            ),
            evidence_thread_id=_require_string(
                values.get("evidence_thread_id") or "",
                field="cluster source contribution.evidence_thread_id",
            ),
            finding=_require_string(
                values.get("finding", ""), field="cluster source contribution.finding"
            ),
            technical_result=_require_string(
                values.get("technical_result", ""),
                field="cluster source contribution.technical_result",
            ),
            plain_english_meaning=_require_string(
                values.get("plain_english_meaning", ""),
                field="cluster source contribution.plain_english_meaning",
            ),
            relation_to_cluster_question=_require_string(
                values.get("relation_to_cluster_question", ""),
                field="cluster source contribution.relation_to_cluster_question",
            ),
            comparison_status=_require_string(
                values.get("comparison_status", "context_only"),
                field="cluster source contribution.comparison_status",
            ),  # type: ignore[arg-type]
            origin=_require_string(
                values.get("origin", "reasoner"),
                field="cluster source contribution.origin",
            ),  # type: ignore[arg-type]
            evidence=_mapping_list(
                values.get("evidence", []), field="cluster source contribution.evidence"
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceThread:
    """A human-readable line of inquiry inside a broader literature cluster."""

    thread_id: str = ""
    title: str = ""
    question: str = ""
    summary: str = ""
    plain_english_meaning: str = ""
    relationship: str = ""
    source_ids: list[str] = field(default_factory=list)
    proposition_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "thread_id",
            "title",
            "question",
            "summary",
            "plain_english_meaning",
            "relationship",
        ):
            _require_string(
                getattr(self, field_name), field=f"evidence thread.{field_name}"
            )
        object.__setattr__(
            self,
            "source_ids",
            _string_list(self.source_ids, field="evidence thread.source_ids"),
        )
        object.__setattr__(
            self,
            "proposition_ids",
            _string_list(self.proposition_ids, field="evidence thread.proposition_ids"),
        )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="evidence thread.evidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceThread:
        values = _model_payload(cls, payload, label="evidence thread")
        return cls(
            thread_id=_require_string(
                values.get("thread_id", ""), field="evidence thread.thread_id"
            ),
            title=_require_string(
                values.get("title", ""), field="evidence thread.title"
            ),
            question=_require_string(
                values.get("question", ""), field="evidence thread.question"
            ),
            summary=_require_string(
                values.get("summary", ""), field="evidence thread.summary"
            ),
            plain_english_meaning=_require_string(
                values.get("plain_english_meaning", ""),
                field="evidence thread.plain_english_meaning",
            ),
            relationship=_require_string(
                values.get("relationship", ""), field="evidence thread.relationship"
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="evidence thread.source_ids"
            ),
            proposition_ids=_string_list(
                values.get("proposition_ids", []),
                field="evidence thread.proposition_ids",
            ),
            evidence=_mapping_list(
                values.get("evidence", []), field="evidence thread.evidence"
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceBaseGroup:
    """Publications treated as one proposition-specific evidence base."""

    evidence_base_group_id: str = ""
    proposition_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    study_lineage_ids: list[str] = field(default_factory=list)
    relationship: Literal[
        "independent_evidence_base",
        "overlapping_evidence_base",
        "same_study",
        "institutional_series",
        "independence_uncertain",
    ] = "independence_uncertain"
    counted_as_independent: bool = False
    rationale: str = ""
    overlap_signals: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_base_group_id",
            "proposition_id",
            "relationship",
            "rationale",
        ):
            _require_string(
                getattr(self, field_name), field=f"evidence base group.{field_name}"
            )
        if self.relationship not in {
            "independent_evidence_base",
            "overlapping_evidence_base",
            "same_study",
            "institutional_series",
            "independence_uncertain",
        }:
            raise ValueError("evidence base group.relationship is invalid")
        _require_bool(
            self.counted_as_independent,
            field="evidence base group.counted_as_independent",
        )
        if (
            self.relationship == "independence_uncertain"
            and self.counted_as_independent
        ):
            raise ValueError(
                "uncertain evidence bases cannot increase the independent count"
            )
        for field_name in ("source_ids", "study_lineage_ids", "overlap_signals"):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name), field=f"evidence base group.{field_name}"
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceBaseGroup:
        values = _model_payload(cls, payload, label="evidence base group")
        return cls(
            evidence_base_group_id=_require_string(
                values.get("evidence_base_group_id", ""),
                field="evidence base group.evidence_base_group_id",
            ),
            proposition_id=_require_string(
                values.get("proposition_id", ""),
                field="evidence base group.proposition_id",
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="evidence base group.source_ids"
            ),
            study_lineage_ids=_string_list(
                values.get("study_lineage_ids", []),
                field="evidence base group.study_lineage_ids",
            ),
            relationship=_require_string(
                values.get("relationship", "independence_uncertain"),
                field="evidence base group.relationship",
            ),  # type: ignore[arg-type]
            counted_as_independent=values.get("counted_as_independent", False),
            rationale=_require_string(
                values.get("rationale", ""), field="evidence base group.rationale"
            ),
            overlap_signals=_string_list(
                values.get("overlap_signals", []),
                field="evidence base group.overlap_signals",
            ),
        )


@dataclass(frozen=True, slots=True)
class IndependenceAssessment:
    """Proposition-specific effective evidence-base accounting."""

    assessment_id: str = ""
    proposition_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    evidence_base_group_ids: list[str] = field(default_factory=list)
    status: Literal[
        "independent_evidence_base",
        "overlapping_evidence_base",
        "same_study",
        "institutional_series",
        "independence_uncertain",
    ] = "independence_uncertain"
    effective_evidence_base_count: int = 0
    rationale: str = ""
    overlap_signals: list[str] = field(default_factory=list)
    confidence: Literal["high", "moderate", "low", "unknown"] = "unknown"
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "assessment_id",
            "proposition_id",
            "status",
            "rationale",
            "confidence",
        ):
            _require_string(
                getattr(self, field_name), field=f"independence assessment.{field_name}"
            )
        if self.status not in {
            "independent_evidence_base",
            "overlapping_evidence_base",
            "same_study",
            "institutional_series",
            "independence_uncertain",
        }:
            raise ValueError("independence assessment.status is invalid")
        if self.confidence not in {"high", "moderate", "low", "unknown"}:
            raise ValueError("independence assessment.confidence is invalid")
        object.__setattr__(
            self,
            "effective_evidence_base_count",
            _nonnegative_int(
                self.effective_evidence_base_count,
                field="independence assessment.effective_evidence_base_count",
            ),
        )
        if (
            self.status == "independence_uncertain"
            and self.effective_evidence_base_count > 0
        ):
            raise ValueError(
                "uncertain assessments cannot increase the independent count"
            )
        for field_name in ("source_ids", "evidence_base_group_ids", "overlap_signals"):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name),
                    field=f"independence assessment.{field_name}",
                ),
            )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="independence assessment.evidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> IndependenceAssessment:
        values = _model_payload(cls, payload, label="independence assessment")
        return cls(
            assessment_id=_require_string(
                values.get("assessment_id", ""),
                field="independence assessment.assessment_id",
            ),
            proposition_id=_require_string(
                values.get("proposition_id", ""),
                field="independence assessment.proposition_id",
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="independence assessment.source_ids"
            ),
            evidence_base_group_ids=_string_list(
                values.get("evidence_base_group_ids", []),
                field="independence assessment.evidence_base_group_ids",
            ),
            status=_require_string(
                values.get("status", "independence_uncertain"),
                field="independence assessment.status",
            ),  # type: ignore[arg-type]
            effective_evidence_base_count=_nonnegative_int(
                values.get("effective_evidence_base_count", 0),
                field="independence assessment.effective_evidence_base_count",
            ),
            rationale=_require_string(
                values.get("rationale", ""), field="independence assessment.rationale"
            ),
            overlap_signals=_string_list(
                values.get("overlap_signals", []),
                field="independence assessment.overlap_signals",
            ),
            confidence=_require_string(
                values.get("confidence", "unknown"),
                field="independence assessment.confidence",
            ),  # type: ignore[arg-type]
            evidence=_mapping_list(
                values.get("evidence", []), field="independence assessment.evidence"
            ),
        )


@dataclass(frozen=True, slots=True)
class QuantitativeComparisonValidation:
    """Deterministic comparability and arithmetic result for numerical evidence."""

    comparison_id: str = ""
    proposition_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    quantitative_result_ids: list[str] = field(default_factory=list)
    status: Literal["valid", "qualified", "rejected", "not_comparable"] = (
        "not_comparable"
    )
    estimands_comparable: bool = False
    outcomes_comparable: bool = False
    populations_comparable: bool = False
    arithmetic_reproducible: bool = False
    reason: str = ""
    qualifications: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in ("comparison_id", "proposition_id", "status", "reason"):
            _require_string(
                getattr(self, field_name),
                field=f"quantitative comparison validation.{field_name}",
            )
        if self.status not in {"valid", "qualified", "rejected", "not_comparable"}:
            raise ValueError("quantitative comparison validation.status is invalid")
        for field_name in (
            "estimands_comparable",
            "outcomes_comparable",
            "populations_comparable",
            "arithmetic_reproducible",
        ):
            _require_bool(
                getattr(self, field_name),
                field=f"quantitative comparison validation.{field_name}",
            )
        for field_name in ("source_ids", "quantitative_result_ids", "qualifications"):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name),
                    field=f"quantitative comparison validation.{field_name}",
                ),
            )
        if self.status == "valid" and not all(
            (
                self.estimands_comparable,
                self.outcomes_comparable,
                self.populations_comparable,
                self.arithmetic_reproducible,
            )
        ):
            raise ValueError(
                "valid quantitative comparisons require all checks to pass"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> QuantitativeComparisonValidation:
        values = _model_payload(
            cls, payload, label="quantitative comparison validation"
        )
        return cls(
            comparison_id=_require_string(
                values.get("comparison_id", ""),
                field="quantitative comparison validation.comparison_id",
            ),
            proposition_id=_require_string(
                values.get("proposition_id", ""),
                field="quantitative comparison validation.proposition_id",
            ),
            source_ids=_string_list(
                values.get("source_ids", []),
                field="quantitative comparison validation.source_ids",
            ),
            quantitative_result_ids=_string_list(
                values.get("quantitative_result_ids", []),
                field="quantitative comparison validation.quantitative_result_ids",
            ),
            status=_require_string(
                values.get("status", "not_comparable"),
                field="quantitative comparison validation.status",
            ),  # type: ignore[arg-type]
            estimands_comparable=values.get("estimands_comparable", False),
            outcomes_comparable=values.get("outcomes_comparable", False),
            populations_comparable=values.get("populations_comparable", False),
            arithmetic_reproducible=values.get("arithmetic_reproducible", False),
            reason=_require_string(
                values.get("reason", ""),
                field="quantitative comparison validation.reason",
            ),
            qualifications=_string_list(
                values.get("qualifications", []),
                field="quantitative comparison validation.qualifications",
            ),
        )


@dataclass(frozen=True, slots=True)
class SubjectTag:
    """A canonical, collection-native subject tag used only for retrieval."""

    subject_tag_id: str = ""
    label: str = ""
    slug: str = ""
    facet_type: str = ""
    original_variants: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    study_family_ids: list[str] = field(default_factory=list)
    assignment_provenance: list[str] = field(default_factory=list)
    relationship_proposals: list[dict[str, Any]] = field(default_factory=list)
    source_count: int = 0
    independent_source_count: int = 0
    revision_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "subject_tag_id",
            "label",
            "slug",
            "facet_type",
            "revision_hash",
        ):
            _require_string(
                getattr(self, field_name), field=f"subject tag.{field_name}"
            )
        allowed_facets = {
            "",
            "concept",
            "theory",
            "mechanism",
            "outcome",
            "case",
            "population",
            "geography",
            "period",
            "method",
            "data",
            "measure",
        }
        if self.facet_type not in allowed_facets:
            raise ValueError("subject tag.facet_type is invalid")
        object.__setattr__(
            self,
            "original_variants",
            _string_list(self.original_variants, field="subject tag.original_variants"),
        )
        object.__setattr__(
            self,
            "source_ids",
            _string_list(self.source_ids, field="subject tag.source_ids"),
        )
        object.__setattr__(
            self,
            "study_family_ids",
            _string_list(self.study_family_ids, field="subject tag.study_family_ids"),
        )
        object.__setattr__(
            self,
            "assignment_provenance",
            _string_list(
                self.assignment_provenance, field="subject tag.assignment_provenance"
            ),
        )
        object.__setattr__(
            self,
            "relationship_proposals",
            _mapping_list(
                self.relationship_proposals, field="subject tag.relationship_proposals"
            ),
        )
        object.__setattr__(
            self,
            "source_count",
            _nonnegative_int(self.source_count, field="subject tag.source_count"),
        )
        object.__setattr__(
            self,
            "independent_source_count",
            _nonnegative_int(
                self.independent_source_count,
                field="subject tag.independent_source_count",
            ),
        )
        if self.source_count != len(set(self.source_ids)):
            raise ValueError("subject tag.source_count must match source_ids")
        if self.independent_source_count != len(set(self.study_family_ids)):
            raise ValueError(
                "subject tag.independent_source_count must match study_family_ids"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SubjectTag:
        values = _model_payload(cls, payload, label="subject tag")
        source_ids = _string_list(
            values.get("source_ids", []), field="subject tag.source_ids"
        )
        study_family_ids = _string_list(
            values.get("study_family_ids", []), field="subject tag.study_family_ids"
        )
        return cls(
            subject_tag_id=_require_string(
                values.get("subject_tag_id", ""), field="subject tag.subject_tag_id"
            ),
            label=_require_string(values.get("label", ""), field="subject tag.label"),
            slug=_require_string(values.get("slug", ""), field="subject tag.slug"),
            facet_type=_require_string(
                values.get("facet_type", ""), field="subject tag.facet_type"
            ),
            original_variants=_string_list(
                values.get("original_variants", []),
                field="subject tag.original_variants",
            ),
            source_ids=source_ids,
            study_family_ids=study_family_ids,
            assignment_provenance=_string_list(
                values.get("assignment_provenance", []),
                field="subject tag.assignment_provenance",
            ),
            relationship_proposals=_mapping_list(
                values.get("relationship_proposals", []),
                field="subject tag.relationship_proposals",
            ),
            source_count=_nonnegative_int(
                values.get("source_count", len(set(source_ids))),
                field="subject tag.source_count",
            ),
            independent_source_count=_nonnegative_int(
                values.get("independent_source_count", len(set(study_family_ids))),
                field="subject tag.independent_source_count",
            ),
            revision_hash=_require_string(
                values.get("revision_hash", ""), field="subject tag.revision_hash"
            ),
        )


@dataclass(frozen=True, slots=True)
class SubjectTagAssignment:
    """One provenance-preserving assignment of a subject tag to a source."""

    assignment_id: str = ""
    subject_tag_id: str = ""
    source_id: str = ""
    note_id: str = ""
    facet_type: str = ""
    original_value: str = ""
    provenance: str = ""
    reason: str = ""
    confirmed_by_profile: bool = False
    visible: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "assignment_id",
            "subject_tag_id",
            "source_id",
            "note_id",
            "facet_type",
            "original_value",
            "provenance",
            "reason",
        ):
            _require_string(
                getattr(self, field_name), field=f"subject tag assignment.{field_name}"
            )
        _require_bool(
            self.confirmed_by_profile,
            field="subject tag assignment.confirmed_by_profile",
        )
        _require_bool(self.visible, field="subject tag assignment.visible")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SubjectTagAssignment:
        values = _model_payload(cls, payload, label="subject tag assignment")
        confirmed_by_profile = values.get("confirmed_by_profile", False)
        visible = values.get("visible", True)
        _require_bool(
            confirmed_by_profile,
            field="subject tag assignment.confirmed_by_profile",
        )
        _require_bool(visible, field="subject tag assignment.visible")
        return cls(
            assignment_id=_require_string(
                values.get("assignment_id", ""),
                field="subject tag assignment.assignment_id",
            ),
            subject_tag_id=_require_string(
                values.get("subject_tag_id", ""),
                field="subject tag assignment.subject_tag_id",
            ),
            source_id=_require_string(
                values.get("source_id", ""), field="subject tag assignment.source_id"
            ),
            note_id=_require_string(
                values.get("note_id", ""), field="subject tag assignment.note_id"
            ),
            facet_type=_require_string(
                values.get("facet_type", ""), field="subject tag assignment.facet_type"
            ),
            original_value=_require_string(
                values.get("original_value", ""),
                field="subject tag assignment.original_value",
            ),
            provenance=_require_string(
                values.get("provenance", ""), field="subject tag assignment.provenance"
            ),
            reason=_require_string(
                values.get("reason", ""), field="subject tag assignment.reason"
            ),
            confirmed_by_profile=confirmed_by_profile,
            visible=visible,
        )


@dataclass(frozen=True, slots=True)
class TypedSourceRelation:
    """A typed navigation relation that carries no analytical force."""

    relation_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    note_ids: list[str] = field(default_factory=list)
    relation_type: str = ""
    reasons: list[str] = field(default_factory=list)
    subject_tag_ids: list[str] = field(default_factory=list)
    proposition_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provenance: str = ""
    inferred: bool = False

    def __post_init__(self) -> None:
        for field_name in ("relation_id", "relation_type", "provenance"):
            _require_string(
                getattr(self, field_name), field=f"typed source relation.{field_name}"
            )
        allowed_types = {
            "",
            "cites",
            "cited_by",
            "zotero_related",
            "same_proposition",
            "shared_concept",
            "same_case",
            "same_method",
            "same_outcome",
            "semantic_similarity",
            "legacy_untyped_relation",
        }
        if self.relation_type not in allowed_types:
            raise ValueError("typed source relation.relation_type is invalid")
        object.__setattr__(
            self,
            "source_ids",
            _string_list(self.source_ids, field="typed source relation.source_ids"),
        )
        object.__setattr__(
            self,
            "note_ids",
            _string_list(self.note_ids, field="typed source relation.note_ids"),
        )
        object.__setattr__(
            self,
            "reasons",
            _string_list(self.reasons, field="typed source relation.reasons"),
        )
        object.__setattr__(
            self,
            "subject_tag_ids",
            _string_list(
                self.subject_tag_ids, field="typed source relation.subject_tag_ids"
            ),
        )
        object.__setattr__(
            self,
            "proposition_ids",
            _string_list(
                self.proposition_ids, field="typed source relation.proposition_ids"
            ),
        )
        object.__setattr__(
            self,
            "evidence",
            _mapping_list(self.evidence, field="typed source relation.evidence"),
        )
        _require_bool(self.inferred, field="typed source relation.inferred")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TypedSourceRelation:
        values = _model_payload(cls, payload, label="typed source relation")
        inferred = values.get("inferred", False)
        _require_bool(inferred, field="typed source relation.inferred")
        return cls(
            relation_id=_require_string(
                values.get("relation_id", ""), field="typed source relation.relation_id"
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="typed source relation.source_ids"
            ),
            note_ids=_string_list(
                values.get("note_ids", []), field="typed source relation.note_ids"
            ),
            relation_type=_require_string(
                values.get("relation_type", ""),
                field="typed source relation.relation_type",
            ),
            reasons=_string_list(
                values.get("reasons", []), field="typed source relation.reasons"
            ),
            subject_tag_ids=_string_list(
                values.get("subject_tag_ids", []),
                field="typed source relation.subject_tag_ids",
            ),
            proposition_ids=_string_list(
                values.get("proposition_ids", []),
                field="typed source relation.proposition_ids",
            ),
            evidence=_mapping_list(
                values.get("evidence", []), field="typed source relation.evidence"
            ),
            provenance=_require_string(
                values.get("provenance", ""), field="typed source relation.provenance"
            ),
            inferred=inferred,
        )


@dataclass(frozen=True, slots=True)
class TopicNeighborhood:
    """A non-analytical navigation grouping for related literature sources."""

    topic_neighborhood_id: str = ""
    kind: str = ""
    semantic_identity: str = ""
    label: str = ""
    source_ids: list[str] = field(default_factory=list)
    note_ids: list[str] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    analytical_support: bool = False
    source_count: int = 0
    facet_type: str = ""
    canonical_tag_id: str = ""
    promotion_status: str = ""
    independent_source_count: int = 0
    member_relationship_reasons: list[dict[str, Any]] = field(default_factory=list)
    visibility_status: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "topic_neighborhood_id",
            "kind",
            "semantic_identity",
            "label",
            "facet_type",
            "canonical_tag_id",
            "promotion_status",
            "visibility_status",
        ):
            _require_string(
                getattr(self, field_name), field=f"topic neighborhood.{field_name}"
            )
        if self.kind not in {
            "",
            "semantic",
            "case",
            "method",
            "tag",
            "citation_or_relation",
            "concept",
            "theory",
            "mechanism",
            "outcome",
            "population",
            "geography",
            "period",
            "data",
            "measure",
            "typed_relation",
        }:
            raise ValueError("topic neighborhood.kind is invalid")
        _require_bool(
            self.analytical_support, field="topic neighborhood.analytical_support"
        )
        if self.analytical_support:
            raise ValueError("topic neighborhood.analytical_support must be false")
        object.__setattr__(
            self,
            "source_ids",
            _string_list(self.source_ids, field="topic neighborhood.source_ids"),
        )
        object.__setattr__(
            self,
            "note_ids",
            _string_list(self.note_ids, field="topic neighborhood.note_ids"),
        )
        object.__setattr__(
            self,
            "signals",
            _mapping_list(self.signals, field="topic neighborhood.signals"),
        )
        object.__setattr__(
            self,
            "member_relationship_reasons",
            _mapping_list(
                self.member_relationship_reasons,
                field="topic neighborhood.member_relationship_reasons",
            ),
        )
        object.__setattr__(
            self,
            "source_count",
            _nonnegative_int(
                self.source_count, field="topic neighborhood.source_count"
            ),
        )
        if self.source_count != len(set(self.source_ids)):
            raise ValueError("topic neighborhood.source_count must match source_ids")
        object.__setattr__(
            self,
            "independent_source_count",
            _nonnegative_int(
                self.independent_source_count,
                field="topic neighborhood.independent_source_count",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TopicNeighborhood:
        values = _model_payload(cls, payload, label="topic neighborhood")
        analytical_support = values.get("analytical_support", False)
        _require_bool(analytical_support, field="topic neighborhood.analytical_support")
        return cls(
            topic_neighborhood_id=_require_string(
                values.get("topic_neighborhood_id", ""),
                field="topic neighborhood.topic_neighborhood_id",
            ),
            kind=_require_string(
                values.get("kind", ""), field="topic neighborhood.kind"
            ),
            semantic_identity=_require_string(
                values.get("semantic_identity", ""),
                field="topic neighborhood.semantic_identity",
            ),
            label=_require_string(
                values.get("label", ""), field="topic neighborhood.label"
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="topic neighborhood.source_ids"
            ),
            note_ids=_string_list(
                values.get("note_ids", []), field="topic neighborhood.note_ids"
            ),
            signals=_mapping_list(
                values.get("signals", []), field="topic neighborhood.signals"
            ),
            analytical_support=analytical_support,
            source_count=_nonnegative_int(
                values.get("source_count", len(set(values.get("source_ids", [])))),
                field="topic neighborhood.source_count",
            ),
            facet_type=_require_string(
                values.get("facet_type", ""), field="topic neighborhood.facet_type"
            ),
            canonical_tag_id=_require_string(
                values.get("canonical_tag_id", ""),
                field="topic neighborhood.canonical_tag_id",
            ),
            promotion_status=_require_string(
                values.get("promotion_status", ""),
                field="topic neighborhood.promotion_status",
            ),
            independent_source_count=_nonnegative_int(
                values.get("independent_source_count", 0),
                field="topic neighborhood.independent_source_count",
            ),
            member_relationship_reasons=_mapping_list(
                values.get("member_relationship_reasons", []),
                field="topic neighborhood.member_relationship_reasons",
            ),
            visibility_status=_require_string(
                values.get("visibility_status", ""),
                field="topic neighborhood.visibility_status",
            ),
        )


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """One frozen-inventory item and its visible inclusion or exclusion reason."""

    source_id: str = ""
    title: str = ""
    zotero_key: str = ""
    terminal_state: Literal[
        "validated_note",
        "limited_note",
        "duplicate_alias",
        "parked_for_review",
        "exhausted",
        "partial",
        "pending",
    ] = "pending"
    exclusion_reason: str = ""
    attempted_route: list[str] = field(default_factory=list)
    could_affect_existing_cluster: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "source_id",
            "title",
            "zotero_key",
            "terminal_state",
            "exclusion_reason",
        ):
            _require_string(
                getattr(self, field_name), field=f"coverage record.{field_name}"
            )
        if self.terminal_state not in {
            "validated_note",
            "limited_note",
            "duplicate_alias",
            "parked_for_review",
            "exhausted",
            "partial",
            "pending",
        }:
            raise ValueError("coverage record.terminal_state is invalid")
        if self.terminal_state == "exhausted":
            object.__setattr__(self, "terminal_state", "parked_for_review")
        object.__setattr__(
            self,
            "attempted_route",
            _string_list(self.attempted_route, field="coverage record.attempted_route"),
        )
        _require_bool(
            self.could_affect_existing_cluster,
            field="coverage record.could_affect_existing_cluster",
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CoverageRecord:
        values = _model_payload(cls, payload, label="coverage record")
        return cls(
            source_id=_require_string(
                values.get("source_id", ""), field="coverage record.source_id"
            ),
            title=_require_string(
                values.get("title", ""), field="coverage record.title"
            ),
            zotero_key=_require_string(
                values.get("zotero_key", ""), field="coverage record.zotero_key"
            ),
            terminal_state=_require_string(
                values.get("terminal_state", "pending"),
                field="coverage record.terminal_state",
            ),  # type: ignore[arg-type]
            exclusion_reason=_require_string(
                values.get("exclusion_reason", ""),
                field="coverage record.exclusion_reason",
            ),
            attempted_route=_string_list(
                values.get("attempted_route", []),
                field="coverage record.attempted_route",
            ),
            could_affect_existing_cluster=values.get(
                "could_affect_existing_cluster", False
            ),
        )


@dataclass(frozen=True, slots=True)
class CoverageRegister:
    """Complete accounting for the frozen source inventory."""

    source_set_id: str = ""
    inventory_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    records: list[CoverageRecord] = field(default_factory=list)
    status: Literal["complete", "complete_with_exclusions", "partial"] = "complete"

    def __post_init__(self) -> None:
        _require_string(self.source_set_id, field="coverage register.source_set_id")
        _require_string(self.status, field="coverage register.status")
        if self.status not in {"complete", "complete_with_exclusions", "partial"}:
            raise ValueError("coverage register.status is invalid")
        inventory_count = _nonnegative_int(
            self.inventory_count, field="coverage register.inventory_count"
        )
        if not isinstance(self.counts, Mapping) or any(
            not isinstance(key, str) for key in self.counts
        ):
            raise ValueError("coverage register.counts must be a mapping")
        counts = {
            key: _nonnegative_int(value, field=f"coverage register.counts.{key}")
            for key, value in self.counts.items()
        }
        records: list[CoverageRecord] = []
        for record in self.records:
            if isinstance(record, CoverageRecord):
                records.append(record)
            elif isinstance(record, Mapping):
                records.append(CoverageRecord.from_dict(record))
            else:
                raise ValueError(
                    "coverage register.records must contain CoverageRecord values or mappings"
                )
        if records and inventory_count != len(records):
            raise ValueError("coverage register.inventory_count must match records")
        accounting_fields = (
            "validated_note",
            "limited_note",
            "parked_for_review",
            "partial",
            "pending",
        )
        if "exhausted" in counts and "parked_for_review" not in counts:
            counts["parked_for_review"] = counts.pop("exhausted")
        if (
            any(field_name in counts for field_name in accounting_fields)
            and sum(counts.get(field_name, 0) for field_name in accounting_fields)
            != inventory_count
        ):
            raise ValueError(
                "coverage register counts must account for the complete inventory"
            )
        if self.status == "complete" and (
            counts.get("parked_for_review", 0)
            or counts.get("partial", 0)
            or counts.get("pending", 0)
        ):
            raise ValueError(
                "complete coverage cannot contain parked, partial, or pending items"
            )
        if self.status == "complete_with_exclusions" and not counts.get("parked_for_review", 0):
            raise ValueError(
                "complete_with_exclusions requires at least one parked item"
            )
        object.__setattr__(self, "inventory_count", inventory_count)
        object.__setattr__(self, "counts", counts)
        object.__setattr__(self, "records", records)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CoverageRegister:
        values = _model_payload(cls, payload, label="coverage register")
        counts = values.get("counts", {})
        if not isinstance(counts, Mapping):
            raise ValueError("coverage register.counts must be a mapping")
        return cls(
            source_set_id=_require_string(
                values.get("source_set_id", ""), field="coverage register.source_set_id"
            ),
            inventory_count=_nonnegative_int(
                values.get("inventory_count", 0),
                field="coverage register.inventory_count",
            ),
            counts=dict(counts),
            records=[
                CoverageRecord.from_dict(row)
                for row in _mapping_list(
                    values.get("records", []), field="coverage register.records"
                )
            ],
            status=_require_string(
                values.get("status", "complete"), field="coverage register.status"
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class TagConcept:
    """A persisted collection-level tag identity and its reconciliation relations."""

    tag_concept_id: str = ""
    label: str = ""
    slug: str = ""
    original_variants: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    graph_active: bool = False
    activation_reason: str = ""
    revision_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "tag_concept_id",
            "label",
            "slug",
            "activation_reason",
            "revision_hash",
        ):
            _require_string(
                getattr(self, field_name), field=f"tag concept.{field_name}"
            )
        for field_name in ("original_variants", "source_ids"):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name), field=f"tag concept.{field_name}"
                ),
            )
        relations = _mapping_list(self.relations, field="tag concept.relations")
        for index, relation in enumerate(relations):
            relation_type = relation.get("relation_type")
            target_id = relation.get("target_tag_concept_id")
            if relation_type not in {
                "alias_of",
                "broader_than",
                "narrower_than",
                "related_to",
                "superseded_by",
            }:
                raise ValueError(
                    f"tag concept.relations[{index}].relation_type is invalid"
                )
            _require_string(
                target_id, field=f"tag concept.relations[{index}].target_tag_concept_id"
            )
        object.__setattr__(self, "relations", relations)
        _require_bool(self.graph_active, field="tag concept.graph_active")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TagConcept:
        values = _model_payload(cls, payload, label="tag concept")
        return cls(
            tag_concept_id=_require_string(
                values.get("tag_concept_id", ""), field="tag concept.tag_concept_id"
            ),
            label=_require_string(values.get("label", ""), field="tag concept.label"),
            slug=_require_string(values.get("slug", ""), field="tag concept.slug"),
            original_variants=_string_list(
                values.get("original_variants", []),
                field="tag concept.original_variants",
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="tag concept.source_ids"
            ),
            relations=_mapping_list(
                values.get("relations", []), field="tag concept.relations"
            ),
            graph_active=values.get("graph_active", False),
            activation_reason=_require_string(
                values.get("activation_reason", ""),
                field="tag concept.activation_reason",
            ),
            revision_hash=_require_string(
                values.get("revision_hash", ""), field="tag concept.revision_hash"
            ),
        )


@dataclass(frozen=True, slots=True)
class NeighborhoodSummary:
    """Human-readable retrieval summary for a visible literature neighborhood."""

    neighborhood_id: str = ""
    label: str = ""
    why_useful: str = ""
    source_ids: list[str] = field(default_factory=list)
    effective_evidence_base_count: int = 0
    related_cluster_ids: list[str] = field(default_factory=list)
    representative_source_ids: list[str] = field(default_factory=list)
    relationship_reasons: list[str] = field(default_factory=list)
    visible: bool = True

    def __post_init__(self) -> None:
        for field_name in ("neighborhood_id", "label", "why_useful"):
            _require_string(
                getattr(self, field_name), field=f"neighborhood summary.{field_name}"
            )
        for field_name in (
            "source_ids",
            "related_cluster_ids",
            "representative_source_ids",
            "relationship_reasons",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_list(
                    getattr(self, field_name),
                    field=f"neighborhood summary.{field_name}",
                ),
            )
        object.__setattr__(
            self,
            "effective_evidence_base_count",
            _nonnegative_int(
                self.effective_evidence_base_count,
                field="neighborhood summary.effective_evidence_base_count",
            ),
        )
        _require_bool(self.visible, field="neighborhood summary.visible")

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NeighborhoodSummary:
        values = _model_payload(cls, payload, label="neighborhood summary")
        return cls(
            neighborhood_id=_require_string(
                values.get("neighborhood_id", ""),
                field="neighborhood summary.neighborhood_id",
            ),
            label=_require_string(
                values.get("label", ""), field="neighborhood summary.label"
            ),
            why_useful=_require_string(
                values.get("why_useful", ""), field="neighborhood summary.why_useful"
            ),
            source_ids=_string_list(
                values.get("source_ids", []), field="neighborhood summary.source_ids"
            ),
            effective_evidence_base_count=_nonnegative_int(
                values.get("effective_evidence_base_count", 0),
                field="neighborhood summary.effective_evidence_base_count",
            ),
            related_cluster_ids=_string_list(
                values.get("related_cluster_ids", []),
                field="neighborhood summary.related_cluster_ids",
            ),
            representative_source_ids=_string_list(
                values.get("representative_source_ids", []),
                field="neighborhood summary.representative_source_ids",
            ),
            relationship_reasons=_string_list(
                values.get("relationship_reasons", []),
                field="neighborhood summary.relationship_reasons",
            ),
            visible=values.get("visible", True),
        )


@dataclass(frozen=True, slots=True)
class ResolutionPath:
    """A type-sensitive route for resolving a collection-scoped gap."""

    path_type: Literal[
        "quantitative",
        "qualitative",
        "historical_interpretive",
        "theoretical",
        "normative",
        "methodological",
        "practitioner",
    ]
    question: str
    evidence_needed: str
    requirements: dict[str, Any]
    feasibility: str
    limitations: list[str]

    def __post_init__(self) -> None:
        allowed = {
            "quantitative",
            "qualitative",
            "historical_interpretive",
            "theoretical",
            "normative",
            "methodological",
            "practitioner",
        }
        for field_name in ("path_type", "question", "evidence_needed", "feasibility"):
            _require_string(
                getattr(self, field_name), field=f"resolution path.{field_name}"
            )
        if self.path_type not in allowed:
            raise ValueError("resolution path.path_type is invalid")
        object.__setattr__(
            self,
            "requirements",
            _any_mapping(self.requirements, field="resolution path.requirements"),
        )
        object.__setattr__(
            self,
            "limitations",
            _string_list(self.limitations, field="resolution path.limitations"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        type(self).from_dict(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ResolutionPath:
        values = _model_payload(cls, payload, label="resolution path")
        missing = [
            field_name
            for field_name in cls.__dataclass_fields__
            if field_name not in values
        ]
        if missing:
            raise ValueError(f"missing resolution path fields: {', '.join(missing)}")
        path_type = _require_string(
            values["path_type"], field="resolution path.path_type"
        )
        if path_type.strip().casefold() in {
            "mixed method",
            "mixed methods",
            "mixed-method",
            "mixed-methods",
        }:
            # The public ontology deliberately has no catch-all mixed-methods
            # bucket. Routes whose defining work is combining/coding methods
            # belong to the methodological resolution family.
            path_type = "methodological"
        return cls(
            path_type=path_type,  # type: ignore[arg-type]
            question=_require_string(
                values["question"], field="resolution path.question"
            ),
            evidence_needed=_require_string(
                values["evidence_needed"], field="resolution path.evidence_needed"
            ),
            requirements=_any_mapping(
                values["requirements"], field="resolution path.requirements"
            ),
            feasibility=_require_string(
                values["feasibility"], field="resolution path.feasibility"
            ),
            limitations=_string_list(
                values["limitations"], field="resolution path.limitations"
            ),
        )


@dataclass(slots=True)
class ClusterProposal:
    """A reasoner proposal whose membership must still pass deterministic gates."""

    proposal_id: str = ""
    label: str = ""
    semantic_identity: str = ""
    shared_question: str = ""
    bounded_object: str = ""
    coherence_rationale: str = ""
    source_ids: list[str] = field(default_factory=list)
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    propositions: list[LiteratureProposition] = field(default_factory=list)
    family_relations: list[dict[str, Any]] = field(default_factory=list)
    source_roles: list[dict[str, Any]] = field(default_factory=list)
    study_lineages: list[StudyLineage] = field(default_factory=list)
    evidence_base_groups: list[EvidenceBaseGroup] = field(default_factory=list)
    independence_assessments: list[IndependenceAssessment] = field(default_factory=list)
    effective_evidence_base_count: int = 0

    def __post_init__(self) -> None:
        normalized_lineages: list[StudyLineage] = []
        for lineage in self.study_lineages:
            normalized_lineages.append(
                lineage
                if isinstance(lineage, StudyLineage)
                else StudyLineage.from_dict(lineage)
            )
        normalized_groups: list[EvidenceBaseGroup] = []
        for group in self.evidence_base_groups:
            normalized_groups.append(
                group
                if isinstance(group, EvidenceBaseGroup)
                else EvidenceBaseGroup.from_dict(group)
            )
        normalized_assessments: list[IndependenceAssessment] = []
        for assessment in self.independence_assessments:
            normalized_assessments.append(
                assessment
                if isinstance(assessment, IndependenceAssessment)
                else IndependenceAssessment.from_dict(assessment)
            )
        self.study_lineages = normalized_lineages
        self.evidence_base_groups = normalized_groups
        self.independence_assessments = normalized_assessments
        self.effective_evidence_base_count = _nonnegative_int(
            self.effective_evidence_base_count,
            field="cluster proposal.effective_evidence_base_count",
        )
        self.family_relations = _family_relation_list(
            self.family_relations,
            field="cluster proposal.family_relations",
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterProposal:
        values = _model_payload(cls, payload, label="cluster proposal")
        source_ids = _string_list(
            values.get("source_ids"), field="cluster proposal.source_ids"
        )
        propositions = [
            LiteratureProposition.from_dict(row)
            for row in _mapping_list(
                values.get("propositions"), field="cluster proposal.propositions"
            )
        ]
        source_roles = values.get("source_roles")
        if isinstance(source_roles, Mapping):
            if set(str(key) for key in source_roles) <= {"core", "context", "bridge"}:
                source_role_rows = [
                    {"source_id": str(source_id), "role": str(role)}
                    for role, role_sources in sorted(
                        source_roles.items(), key=lambda row: str(row[0])
                    )
                    for source_id in _string_list(
                        role_sources,
                        field=f"cluster proposal.source_roles.{role}",
                    )
                ]
            else:
                source_role_rows = [
                    {
                        "source_id": str(source_id),
                        "role": str(
                            role.get("role") if isinstance(role, Mapping) else role
                        ),
                    }
                    for source_id, role in sorted(
                        source_roles.items(), key=lambda row: str(row[0])
                    )
                ]
        elif source_roles == []:
            source_role_rows = []
        elif isinstance(source_roles, list) and all(
            isinstance(role, str) for role in source_roles
        ):
            normalized_roles = [str(role).strip().casefold() for role in source_roles]
            if len(normalized_roles) == len(source_ids) and all(
                role in {"core", "context", "bridge"} for role in normalized_roles
            ):
                source_role_rows = [
                    {"source_id": source_id, "role": role}
                    for source_id, role in zip(
                        source_ids, normalized_roles, strict=True
                    )
                ]
            elif propositions:
                proposition_source_ids = {
                    source_id
                    for proposition in propositions
                    for source_id in proposition.source_ids
                }
                source_role_rows = [
                    {
                        "source_id": source_id,
                        "role": "core"
                        if source_id in proposition_source_ids
                        else "context",
                    }
                    for source_id in source_ids
                ]
            else:
                raise ValueError(
                    "cluster proposal.source_roles string lists must align with source_ids and contain only roles"
                )
        else:
            source_role_rows = _mapping_list(
                source_roles, field="cluster proposal.source_roles"
            )
        return cls(
            proposal_id=str(values.get("proposal_id") or ""),
            label=str(values.get("label") or ""),
            semantic_identity=str(
                values.get("semantic_identity") or values.get("label") or ""
            ),
            shared_question=str(values.get("shared_question") or ""),
            bounded_object=str(values.get("bounded_object") or ""),
            coherence_rationale=str(values.get("coherence_rationale") or ""),
            source_ids=source_ids,
            supporting_evidence=_mapping_list(
                values.get("supporting_evidence"),
                field="cluster proposal.supporting_evidence",
            ),
            propositions=propositions,
            family_relations=_family_relation_list(
                values.get("family_relations"),
                field="cluster proposal.family_relations",
            ),
            source_roles=source_role_rows,
            study_lineages=[
                StudyLineage.from_dict(row)
                for row in _mapping_list(
                    values.get("study_lineages", []),
                    field="cluster proposal.study_lineages",
                )
            ],
            evidence_base_groups=[
                EvidenceBaseGroup.from_dict(row)
                for row in _mapping_list(
                    values.get("evidence_base_groups", []),
                    field="cluster proposal.evidence_base_groups",
                )
            ],
            independence_assessments=[
                IndependenceAssessment.from_dict(row)
                for row in _mapping_list(
                    values.get("independence_assessments", []),
                    field="cluster proposal.independence_assessments",
                )
            ],
            effective_evidence_base_count=_nonnegative_int(
                values.get("effective_evidence_base_count", 0),
                field="cluster proposal.effective_evidence_base_count",
            ),
        )


@dataclass(slots=True)
class DebateFamily:
    """An admitted analytical family that reuses the public cluster identity."""

    cluster_id: str = ""
    label: str = ""
    semantic_identity: str = ""
    shared_question: str = ""
    bounded_object: str = ""
    coherence_rationale: str = ""
    source_ids: list[str] = field(default_factory=list)
    core_source_ids: list[str] = field(default_factory=list)
    context_source_ids: list[str] = field(default_factory=list)
    bridge_source_ids: list[str] = field(default_factory=list)
    source_roles: list[dict[str, Any]] = field(default_factory=list)
    family_relations: list[dict[str, Any]] = field(default_factory=list)
    proposition_ids: list[str] = field(default_factory=list)
    qualification_status: str = ""
    admission_status: str = ""
    effective_evidence_base_count: int = 0
    revision_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "cluster_id",
            "label",
            "semantic_identity",
            "shared_question",
            "bounded_object",
            "coherence_rationale",
            "qualification_status",
            "admission_status",
            "revision_hash",
        ):
            _require_string(
                getattr(self, field_name), field=f"debate family.{field_name}"
            )
        if self.qualification_status not in {
            "",
            "source_backed_cluster",
            "emerging_cluster",
            "evidence_concentrated_cluster",
            "cluster_candidate",
        }:
            raise ValueError("debate family.qualification_status is invalid")
        self.source_ids = _string_list(
            self.source_ids, field="debate family.source_ids"
        )
        self.core_source_ids = _string_list(
            self.core_source_ids, field="debate family.core_source_ids"
        )
        self.context_source_ids = _string_list(
            self.context_source_ids, field="debate family.context_source_ids"
        )
        self.bridge_source_ids = _string_list(
            self.bridge_source_ids, field="debate family.bridge_source_ids"
        )
        self.source_roles = _mapping_list(
            self.source_roles, field="debate family.source_roles"
        )
        self.family_relations = _family_relation_list(
            self.family_relations, field="debate family.family_relations"
        )
        self.proposition_ids = _string_list(
            self.proposition_ids, field="debate family.proposition_ids"
        )
        self.effective_evidence_base_count = _nonnegative_int(
            self.effective_evidence_base_count,
            field="debate family.effective_evidence_base_count",
        )
        source_set = set(self.source_ids)
        if any(source_id not in source_set for source_id in self.core_source_ids):
            raise ValueError("debate family.core_source_ids must belong to source_ids")
        if any(source_id not in source_set for source_id in self.context_source_ids):
            raise ValueError(
                "debate family.context_source_ids must belong to source_ids"
            )
        if any(source_id not in source_set for source_id in self.bridge_source_ids):
            raise ValueError(
                "debate family.bridge_source_ids must belong to source_ids"
            )
        if any(
            source_id not in source_set
            for relation in self.family_relations
            for source_id in relation["source_ids"]
        ):
            raise ValueError("debate family relations must reference family source_ids")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DebateFamily:
        values = _model_payload(cls, payload, label="debate family")
        return cls(**values)


@dataclass(slots=True)
class ClusterSynthesis:
    """Evidence-referenced narrative material for one admitted cluster."""

    cluster_id: str = ""
    scope: str = ""
    boundaries: list[str] = field(default_factory=list)
    coherence_rationale: str = ""
    synthesis: str = ""
    evidence_threads: list[EvidenceThread] = field(default_factory=list)
    central_findings: list[dict[str, Any]] = field(default_factory=list)
    agreements: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    boundary_conditions: list[dict[str, Any]] = field(default_factory=list)
    methodological_fault_lines: list[dict[str, Any]] = field(default_factory=list)
    related_clusters: list[dict[str, Any]] = field(default_factory=list)
    source_roles: list[dict[str, Any]] = field(default_factory=list)
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    gap_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    synthesis_assertions: list[SynthesisAssertion] = field(default_factory=list)
    source_contributions: list[ClusterSourceContribution] = field(default_factory=list)
    evidence_base_groups: list[EvidenceBaseGroup] = field(default_factory=list)
    independence_assessments: list[IndependenceAssessment] = field(default_factory=list)
    quantitative_comparisons: list[QuantitativeComparisonValidation] = field(
        default_factory=list
    )
    strict_adjudications: list[dict[str, Any]] = field(default_factory=list)
    effective_evidence_base_count: int = 0
    debate_state: str = ""

    def __post_init__(self) -> None:
        self.evidence_threads = [
            thread
            if isinstance(thread, EvidenceThread)
            else EvidenceThread.from_dict(thread)
            for thread in self.evidence_threads
        ]
        self.source_contributions = [
            contribution
            if isinstance(contribution, ClusterSourceContribution)
            else ClusterSourceContribution.from_dict(contribution)
            for contribution in self.source_contributions
        ]
        self.evidence_base_groups = [
            group
            if isinstance(group, EvidenceBaseGroup)
            else EvidenceBaseGroup.from_dict(group)
            for group in self.evidence_base_groups
        ]
        self.independence_assessments = [
            assessment
            if isinstance(assessment, IndependenceAssessment)
            else IndependenceAssessment.from_dict(assessment)
            for assessment in self.independence_assessments
        ]
        self.quantitative_comparisons = [
            comparison
            if isinstance(comparison, QuantitativeComparisonValidation)
            else QuantitativeComparisonValidation.from_dict(comparison)
            for comparison in self.quantitative_comparisons
        ]
        self.strict_adjudications = _strict_adjudication_list(
            self.strict_adjudications,
            field="cluster synthesis.strict_adjudications",
        )
        self.effective_evidence_base_count = _nonnegative_int(
            self.effective_evidence_base_count,
            field="cluster synthesis.effective_evidence_base_count",
        )
        _require_string(self.debate_state, field="cluster synthesis.debate_state")
        if self.debate_state not in {
            "",
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
        }:
            raise ValueError("cluster synthesis.debate_state is invalid")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterSynthesis:
        values = _model_payload(cls, payload, label="cluster synthesis")
        debate_state_value = values.get("debate_state", "")
        if isinstance(debate_state_value, Mapping):
            classification = debate_state_value.get(
                "classification"
            ) or debate_state_value.get("state")
            if not isinstance(classification, str) or not classification.strip():
                raise ValueError(
                    "cluster synthesis.debate_state.classification must be a string"
                )
            for section in (
                "agreements",
                "positions",
                "contradictions",
                "boundary_conditions",
                "methodological_fault_lines",
            ):
                if (
                    not values.get(section)
                    and debate_state_value.get(section) is not None
                ):
                    values[section] = debate_state_value[section]
            values["debate_state"] = classification

        def mappings(key: str) -> list[dict[str, Any]]:
            return _mapping_list(values.get(key), field=f"cluster synthesis.{key}")

        return cls(
            cluster_id=str(values.get("cluster_id") or ""),
            scope=str(values.get("scope") or ""),
            boundaries=_string_list(
                values.get("boundaries"), field="cluster synthesis.boundaries"
            ),
            coherence_rationale=str(values.get("coherence_rationale") or ""),
            synthesis=str(values.get("synthesis") or ""),
            evidence_threads=[
                EvidenceThread.from_dict(row) for row in mappings("evidence_threads")
            ],
            central_findings=mappings("central_findings"),
            agreements=mappings("agreements"),
            positions=mappings("positions"),
            contradictions=mappings("contradictions"),
            boundary_conditions=mappings("boundary_conditions"),
            methodological_fault_lines=mappings("methodological_fault_lines"),
            related_clusters=mappings("related_clusters"),
            source_roles=mappings("source_roles"),
            supporting_evidence=mappings("supporting_evidence"),
            gap_hypotheses=mappings("gap_hypotheses"),
            synthesis_assertions=[
                SynthesisAssertion.from_dict(row)
                for row in mappings("synthesis_assertions")
            ],
            source_contributions=[
                ClusterSourceContribution.from_dict(row)
                for row in mappings("source_contributions")
            ],
            evidence_base_groups=[
                EvidenceBaseGroup.from_dict(row)
                for row in mappings("evidence_base_groups")
            ],
            independence_assessments=[
                IndependenceAssessment.from_dict(row)
                for row in mappings("independence_assessments")
            ],
            quantitative_comparisons=[
                QuantitativeComparisonValidation.from_dict(row)
                for row in mappings("quantitative_comparisons")
            ],
            strict_adjudications=mappings("strict_adjudications"),
            effective_evidence_base_count=_nonnegative_int(
                values.get("effective_evidence_base_count", 0),
                field="cluster synthesis.effective_evidence_base_count",
            ),
            debate_state=_require_string(
                values.get("debate_state", ""), field="cluster synthesis.debate_state"
            ),
        )


@dataclass(frozen=True, slots=True)
class GapValueAssessment:
    """Why a collection-relative puzzle is non-obvious and worth resolving."""

    puzzle_type: str = ""
    puzzle: str = ""
    strongest_obvious_answer: str = ""
    why_obvious_answer_is_inadequate: str = ""
    competing_explanations: list[str] = field(default_factory=list)
    decision_or_inference_changed: str = ""
    information_gain: Literal["high", "moderate", "low", ""] = ""
    non_obviousness_passed: bool = False
    importance_passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> GapValueAssessment:
        values = _model_payload(cls, payload or {}, label="gap value assessment")
        _require_bool(
            values.get("non_obviousness_passed", False),
            field="gap value assessment.non_obviousness_passed",
        )
        _require_bool(
            values.get("importance_passed", False),
            field="gap value assessment.importance_passed",
        )
        information_gain = str(values.get("information_gain") or "")
        if information_gain not in {"", "high", "moderate", "low"}:
            raise ValueError(
                "gap value assessment.information_gain must be high, moderate, or low"
            )
        return cls(
            puzzle_type=str(values.get("puzzle_type") or ""),
            puzzle=str(values.get("puzzle") or ""),
            strongest_obvious_answer=str(values.get("strongest_obvious_answer") or ""),
            why_obvious_answer_is_inadequate=str(
                values.get("why_obvious_answer_is_inadequate") or ""
            ),
            competing_explanations=_string_list(
                values.get("competing_explanations"),
                field="gap value assessment.competing_explanations",
            ),
            decision_or_inference_changed=str(
                values.get("decision_or_inference_changed") or ""
            ),
            information_gain=information_gain,  # type: ignore[arg-type]
            non_obviousness_passed=values.get("non_obviousness_passed", False),
            importance_passed=values.get("importance_passed", False),
            rejection_reasons=_string_list(
                values.get("rejection_reasons"),
                field="gap value assessment.rejection_reasons",
            ),
        )


@dataclass(frozen=True, slots=True)
class GapStudyDesign:
    """Minimum executable design capable of resolving a promoted gap."""

    design_type: str = ""
    research_question: str = ""
    estimand: str = ""
    unit_of_analysis: str = ""
    target_population: str = ""
    exposure_or_treatment: str = ""
    comparator: str = ""
    outcomes: list[str] = field(default_factory=list)
    mechanism_measures: list[str] = field(default_factory=list)
    identification_or_inference_strategy: str = ""
    data_route: str = ""
    confounders_or_rival_explanations: list[str] = field(default_factory=list)
    falsification_or_process_tests: list[str] = field(default_factory=list)
    feasibility: str = ""
    ethical_constraints: str = ""
    validity_risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> GapStudyDesign:
        values = _model_payload(cls, payload or {}, label="gap study design")
        return cls(
            design_type=str(values.get("design_type") or ""),
            research_question=str(values.get("research_question") or ""),
            estimand=str(values.get("estimand") or ""),
            unit_of_analysis=str(values.get("unit_of_analysis") or ""),
            target_population=str(values.get("target_population") or ""),
            exposure_or_treatment=str(values.get("exposure_or_treatment") or ""),
            comparator=str(values.get("comparator") or ""),
            outcomes=_string_list(
                values.get("outcomes"), field="gap study design.outcomes"
            ),
            mechanism_measures=_string_list(
                values.get("mechanism_measures"),
                field="gap study design.mechanism_measures",
            ),
            identification_or_inference_strategy=str(
                values.get("identification_or_inference_strategy") or ""
            ),
            data_route=str(values.get("data_route") or ""),
            confounders_or_rival_explanations=_string_list(
                values.get("confounders_or_rival_explanations"),
                field="gap study design.confounders_or_rival_explanations",
            ),
            falsification_or_process_tests=_string_list(
                values.get("falsification_or_process_tests"),
                field="gap study design.falsification_or_process_tests",
            ),
            feasibility=str(values.get("feasibility") or ""),
            ethical_constraints=str(values.get("ethical_constraints") or ""),
            validity_risks=_string_list(
                values.get("validity_risks"), field="gap study design.validity_risks"
            ),
        )


@dataclass(frozen=True, slots=True)
class GapAnchor:
    """A precise location in a cluster synthesis where a gap emerged."""

    cluster_id: str = ""
    section: str = ""
    item_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GapAnchor:
        values = _model_payload(cls, payload, label="gap anchor")
        anchor = cls(
            cluster_id=str(values.get("cluster_id") or ""),
            section=str(values.get("section") or ""),
            item_id=str(values.get("item_id") or ""),
        )
        if not anchor.cluster_id or not anchor.item_id:
            raise ValueError("gap anchor.cluster_id and item_id cannot be empty")
        if anchor.section not in {
            "evidence_threads",
            "central_findings",
            "agreements",
            "positions",
            "contradictions",
            "boundary_conditions",
            "methodological_fault_lines",
            "related_clusters",
            "source_roles",
        }:
            raise ValueError(
                "gap anchor.section is not a supported cluster synthesis section"
            )
        return anchor


@dataclass(slots=True)
class GapRationale:
    """A collection-scoped explanation for one deterministically checked gap."""

    gap_id: str = ""
    proposition_id: str = ""
    originating_proposition_id: str = ""
    originating_cluster_ids: list[str] = field(default_factory=list)
    title: str = ""
    gap_statement: str = ""
    rule: str = ""
    related_cluster_ids: list[str] = field(default_factory=list)
    generation_explanation: str = ""
    observed_pattern: str = ""
    precise_missing_evidence: str = ""
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    countervailing_evidence: list[dict[str, Any]] = field(default_factory=list)
    internal_search_summary: str = ""
    closest_prior_explanation: str = ""
    decision_reasoning: str = ""
    evidence_needed: str = ""
    why_matters: str = ""
    contribution: str = ""
    confidence: str = ""
    value_assessment: GapValueAssessment = field(default_factory=GapValueAssessment)
    study_design: GapStudyDesign = field(default_factory=GapStudyDesign)
    resolution_path: ResolutionPath | None = None
    anchors: list[GapAnchor] = field(default_factory=list)
    merged_from_gap_ids: list[str] = field(default_factory=list)
    reframed_from_gap_id: str = ""
    priority_tier: Literal["high", "moderate", "low", ""] = ""
    strict_adjudication: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GapRationale:
        values = _model_payload(cls, payload, label="gap rationale")
        for field_name in ("value_assessment", "study_design", "resolution_path"):
            nested = values.get(field_name)
            if nested is not None and not isinstance(nested, Mapping):
                raise ValueError(f"gap rationale.{field_name} must be a mapping")

        def evidence(key: str) -> list[dict[str, Any]]:
            return _mapping_list(values.get(key), field=f"gap rationale.{key}")

        priority_tier = str(values.get("priority_tier") or "")
        if priority_tier not in {"", "high", "moderate", "low"}:
            raise ValueError(
                "gap rationale.priority_tier must be high, moderate, or low"
            )
        anchor_rows = _mapping_list(
            values.get("anchors"), field="gap rationale.anchors"
        )
        if (
            values.get("originating_proposition_id")
            and values.get("proposition_id")
            and values["originating_proposition_id"] != values["proposition_id"]
        ):
            raise ValueError(
                "conflicting proposition_id and originating_proposition_id"
            )
        proposition_id = str(
            values.get("originating_proposition_id")
            or values.get("proposition_id")
            or ""
        )
        related_cluster_ids = _string_list(
            values.get("related_cluster_ids"), field="gap rationale.related_cluster_ids"
        )
        originating_cluster_ids = _string_list(
            values.get("originating_cluster_ids"),
            field="gap rationale.originating_cluster_ids",
        )
        if not originating_cluster_ids:
            originating_cluster_ids = list(related_cluster_ids)
        return cls(
            gap_id=str(values.get("gap_id") or ""),
            proposition_id=proposition_id,
            originating_proposition_id=proposition_id,
            originating_cluster_ids=originating_cluster_ids,
            title=str(values.get("title") or ""),
            gap_statement=str(values.get("gap_statement") or ""),
            rule=str(values.get("rule") or ""),
            related_cluster_ids=related_cluster_ids,
            generation_explanation=str(values.get("generation_explanation") or ""),
            observed_pattern=str(values.get("observed_pattern") or ""),
            precise_missing_evidence=str(values.get("precise_missing_evidence") or ""),
            supporting_evidence=evidence("supporting_evidence"),
            countervailing_evidence=evidence("countervailing_evidence"),
            internal_search_summary=str(values.get("internal_search_summary") or ""),
            closest_prior_explanation=str(
                values.get("closest_prior_explanation") or ""
            ),
            decision_reasoning=str(values.get("decision_reasoning") or ""),
            evidence_needed=str(values.get("evidence_needed") or ""),
            why_matters=str(values.get("why_matters") or ""),
            contribution=str(values.get("contribution") or ""),
            confidence=str(values.get("confidence") or ""),
            value_assessment=GapValueAssessment.from_dict(
                values.get("value_assessment")
                if isinstance(values.get("value_assessment"), Mapping)
                else {}
            ),
            study_design=GapStudyDesign.from_dict(
                values.get("study_design")
                if isinstance(values.get("study_design"), Mapping)
                else {}
            ),
            resolution_path=(
                ResolutionPath.from_dict(values["resolution_path"])
                if isinstance(values.get("resolution_path"), Mapping)
                else None
            ),
            anchors=[GapAnchor.from_dict(row) for row in anchor_rows],
            merged_from_gap_ids=_string_list(
                values.get("merged_from_gap_ids"),
                field="gap rationale.merged_from_gap_ids",
            ),
            reframed_from_gap_id=str(values.get("reframed_from_gap_id") or ""),
            priority_tier=priority_tier,  # type: ignore[arg-type]
            strict_adjudication=_strict_adjudication(
                values.get("strict_adjudication", {}),
                field="gap rationale.strict_adjudication",
            ),
        )


@dataclass(frozen=True, slots=True)
class LiteratureMapRequest:
    """Serializable request for synthesis over an existing source set."""

    workspace: Path | str
    source_set_id: str = ""
    run_id: str = ""
    map_id: str = ""
    question: str | None = None
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    allow_cloud: bool = False
    provider_concurrency: int | Literal["auto"] = "auto"
    max_provider_spend_usd: Decimal | None = None
    comparison_collection_keys: list[str] = field(default_factory=list)
    literature_policy: LiteratureMappingPolicy = field(
        default_factory=LiteratureMappingPolicy
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        _require_bool(self.allow_cloud, field="allow_cloud")
        if not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.provider_concurrency != "auto" and (
            isinstance(self.provider_concurrency, bool)
            or not isinstance(self.provider_concurrency, int)
            or self.provider_concurrency < 1
        ):
            raise ValueError(
                "provider_concurrency must be auto or a positive integer"
            )
        if (
            isinstance(self.provider_concurrency, int)
            and self.provider == "deepseek"
            and self.model == "deepseek-v4-flash"
            and self.provider_concurrency > 2_500
        ):
            raise ValueError(
                "provider_concurrency exceeds DeepSeek V4 Flash account limit 2500"
            )
        object.__setattr__(
            self,
            "max_provider_spend_usd",
            _optional_positive_decimal(
                self.max_provider_spend_usd,
                field="max_provider_spend_usd",
            ),
        )
        object.__setattr__(
            self,
            "comparison_collection_keys",
            sorted(
                set(
                    _string_list(
                        self.comparison_collection_keys,
                        field="literature map request.comparison_collection_keys",
                    )
                )
            ),
        )
        if not isinstance(self.literature_policy, LiteratureMappingPolicy):
            if isinstance(self.literature_policy, Mapping):
                object.__setattr__(
                    self,
                    "literature_policy",
                    LiteratureMappingPolicy.from_dict(self.literature_policy),
                )
            else:
                raise ValueError(
                    "literature_policy must be a LiteratureMappingPolicy or mapping"
                )
        if self.literature_policy.require_question and not self.question:
            raise ValueError("literature_policy requires a question")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LiteratureMapRequest:
        return cls(
            workspace=payload["workspace"],
            source_set_id=str(payload.get("source_set_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            map_id=str(payload.get("map_id") or ""),
            question=payload.get("question") or None,
            provider=str(payload.get("provider", "deepseek")),
            model=str(payload.get("model", "deepseek-v4-flash")),
            allow_cloud=_strict_bool(
                payload.get("allow_cloud", False), field="allow_cloud"
            ),
            provider_concurrency=(
                "auto"
                if payload.get("provider_concurrency", "auto") == "auto"
                else int(payload["provider_concurrency"])
            ),
            max_provider_spend_usd=payload.get("max_provider_spend_usd"),
            comparison_collection_keys=_string_list(
                payload.get("comparison_collection_keys"),
                field="literature map request.comparison_collection_keys",
            ),
            literature_policy=payload.get(
                "literature_policy", LiteratureMappingPolicy()
            ),
        )


@dataclass(slots=True)
class LiteratureMapReport:
    status: str
    map_id: str = ""
    run_id: str = ""
    source_set_id: str = ""
    stage: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    proposition_count: int = 0
    topic_neighborhood_count: int = 0
    subject_tag_count: int = 0
    subject_tag_assignment_count: int = 0
    typed_relation_count: int = 0
    singleton_facet_count: int = 0
    source_contribution_count: int = 0
    evidence_base_group_count: int = 0
    independence_assessment_count: int = 0
    quantitative_comparison_count: int = 0
    coverage_record_count: int = 0
    tag_concept_count: int = 0
    neighborhood_summary_count: int = 0
    artifact_paths: dict[str, Path | str] = field(default_factory=dict)
    partial_reason: str = ""
    engine_version: str = CURRENT_ENGINE_VERSION
    artifact_schema_version: str = CURRENT_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.proposition_count == 0 and "proposition_count" in self.counts:
            self.proposition_count = _nonnegative_int(
                self.counts["proposition_count"],
                field="literature map report.proposition_count",
            )
        else:
            self.proposition_count = _nonnegative_int(
                self.proposition_count, field="literature map report.proposition_count"
            )
        if (
            self.topic_neighborhood_count == 0
            and "topic_neighborhood_count" in self.counts
        ):
            self.topic_neighborhood_count = _nonnegative_int(
                self.counts["topic_neighborhood_count"],
                field="literature map report.topic_neighborhood_count",
            )
        else:
            self.topic_neighborhood_count = _nonnegative_int(
                self.topic_neighborhood_count,
                field="literature map report.topic_neighborhood_count",
            )
        for field_name in (
            "subject_tag_count",
            "subject_tag_assignment_count",
            "typed_relation_count",
            "singleton_facet_count",
            "source_contribution_count",
            "evidence_base_group_count",
            "independence_assessment_count",
            "quantitative_comparison_count",
            "coverage_record_count",
            "tag_concept_count",
            "neighborhood_summary_count",
        ):
            value = getattr(self, field_name)
            if value == 0 and field_name in self.counts:
                value = self.counts[field_name]
            setattr(
                self,
                field_name,
                _nonnegative_int(value, field=f"literature map report.{field_name}"),
            )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(slots=True)
class ArtifactManifest:
    status: str
    workspace: Path | str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    created_at: str = ""
    engine_version: str = CURRENT_ENGINE_VERSION
    artifact_schema_version: str = CURRENT_ARTIFACT_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(slots=True)
class RunReport:
    status: str
    workspace: Path | str
    run_id: str
    inventory_count: int = 0
    validated_note_count: int = 0
    limited_note_count: int = 0
    duplicate_alias_count: int = 0
    parked_for_review_count: int = 0
    # Constructor compatibility for v0.12 callers; omitted from v0.13 output.
    exhausted_count: int = 0
    partial_count: int = 0
    pending_count: int = 0
    reused_count: int = 0
    source_set_id: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    source_set: dict[str, Any] = field(default_factory=dict)
    cluster_map: dict[str, Any] = field(default_factory=dict)
    gap_map: dict[str, Any] = field(default_factory=dict)
    literature_packet: dict[str, Any] = field(default_factory=dict)
    obsidian: dict[str, Any] = field(default_factory=dict)
    artifact_manifest: ArtifactManifest | None = None
    engine_version: str = CURRENT_ENGINE_VERSION
    artifact_schema_version: str = CURRENT_ARTIFACT_SCHEMA_VERSION
    literature_map: dict[str, Any] = field(default_factory=dict)
    literature_report: dict[str, Any] = field(default_factory=dict)
    profile_count: int = 0
    profile_valid_count: int = 0
    profile_excluded_count: int = 0
    proposition_count: int = 0
    topic_neighborhood_count: int = 0
    subject_tag_count: int = 0
    subject_tag_assignment_count: int = 0
    typed_relation_count: int = 0
    singleton_facet_count: int = 0
    unclustered_count: int = 0
    cluster_count: int = 0
    debate_count: int = 0
    consensus_count: int = 0
    mixed_evidence_count: int = 0
    mapped_gap_count: int = 0
    gap_lead_count: int = 0
    synthesized_cluster_count: int = 0
    rejected_underspecified_gap_count: int = 0
    rejected_gap_quality_count: int = 0
    merged_gap_count: int = 0
    synthesis_call_count: int = 0
    synthesis_checkpoint_hit_count: int = 0
    synthesis_failure_count: int = 0
    checkpoint_hit_count: int = 0
    source_provider_call_count: int = 0
    literature_provider_call_count: int = 0
    provider_call_count: int = 0
    literature_failure_count: int = 0
    internal_falsification_count: int = 0
    source_peak_concurrency: int = 0
    source_worker_peak_concurrency: int = 0
    local_peak_concurrency: int = 0
    provider_peak_concurrency: int = 0
    source_queue_depth: int = 0
    source_completions_per_minute: float = 0.0
    provider_latency_p50_seconds: float = 0.0
    provider_latency_p95_seconds: float = 0.0
    provider_failure_count: int = 0
    source_stage_wall_seconds: float = 0.0
    relationship_stage_wall_seconds: float = 0.0
    cluster_peak_concurrency: int = 0
    cluster_stage_wall_seconds: float = 0.0

    @property
    def terminal_count(self) -> int:
        return (
            self.validated_note_count
            + self.limited_note_count
            + self.duplicate_alias_count
            + max(self.parked_for_review_count, self.exhausted_count)
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["parked_for_review_count"] = max(
            self.parked_for_review_count, self.exhausted_count
        )
        payload.pop("exhausted_count", None)
        payload["terminal_count"] = self.terminal_count
        return payload


@dataclass(slots=True)
class StatusReport:
    status: str
    workspace: Path | str
    run_id: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    message: str = ""
    engine_version: str = CURRENT_ENGINE_VERSION
    artifact_schema_version: str = CURRENT_ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _strict_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off", ""}:
            return False
    raise ValueError(f"{field} must be a boolean")


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_positive_int(value: Any, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be an integer of at least 1")
