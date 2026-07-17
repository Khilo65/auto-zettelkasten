from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

CURRENT_ENGINE_VERSION = "0.5.0"
CURRENT_ARTIFACT_SCHEMA_VERSION = "1.4"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _model_payload(model: type[Any], payload: Mapping[str, Any], *, label: str) -> dict[str, Any]:
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
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{field} must be a list of mappings")
    return [dict(item) for item in value]


@dataclass(frozen=True, slots=True)
class LiteratureMappingPolicy:
    """Serializable limits and promotion rules for literature synthesis."""

    synthesis_enabled: bool = True
    require_question: bool = False
    auto_promote_clusters: bool = True
    auto_promote_debates: bool = True
    auto_promote_gaps: bool = True
    source_backed_threshold: int = 3
    max_memberships: int = 3
    external_discovery: Literal["disabled", "per_run", "always"] = "disabled"
    max_profile_calls: int = 100
    max_synthesis_calls: int = 24
    profile_workers: int = 4
    literature_deadline_seconds: float = 1_800.0
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
            _require_bool(getattr(self, field_name), field=f"literature_mapping.{field_name}")
        for field_name in (
            "source_backed_threshold",
            "max_memberships",
            "max_profile_calls",
            "max_synthesis_calls",
            "profile_workers",
        ):
            _require_positive_int(getattr(self, field_name), field=f"literature_mapping.{field_name}")
        if self.external_discovery not in {"disabled", "per_run", "always"}:
            raise ValueError("literature_mapping.external_discovery must be disabled, per_run, or always")
        if self.weak_gap_handling != "audit_only":
            raise ValueError("literature_mapping.weak_gap_handling must be audit_only")
        if self.cluster_gap_projection != "inline":
            raise ValueError("literature_mapping.cluster_gap_projection must be inline")
        if (
            isinstance(self.literature_deadline_seconds, bool)
            or not isinstance(self.literature_deadline_seconds, (int, float))
            or self.literature_deadline_seconds <= 0
        ):
            raise ValueError("literature_mapping.literature_deadline_seconds must be a positive number")
        if (
            isinstance(self.deepseek_packet_context_fraction, bool)
            or not isinstance(self.deepseek_packet_context_fraction, (int, float))
            or not 0 < self.deepseek_packet_context_fraction < 1
        ):
            raise ValueError("literature_mapping.deepseek_packet_context_fraction must be between 0 and 1")
        # Normalize equivalent API, config, and CLI values before they enter
        # dependency fingerprints. JSON distinguishes 1800 from 1800.0 even
        # though both express the same deadline, which would otherwise bypass
        # valid paid-call checkpoints.
        object.__setattr__(self, "literature_deadline_seconds", float(self.literature_deadline_seconds))
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
            max_memberships=values.get("max_memberships", 3),
            external_discovery=values.get("external_discovery", "disabled"),
            max_profile_calls=values.get("max_profile_calls", 100),
            max_synthesis_calls=values.get("max_synthesis_calls", 24),
            profile_workers=values.get("profile_workers", 4),
            literature_deadline_seconds=values.get("literature_deadline_seconds", 1_800.0),
            deepseek_packet_context_fraction=values.get("deepseek_packet_context_fraction", 0.8),
            weak_gap_handling=values.get("weak_gap_handling", "audit_only"),
            cluster_gap_projection=values.get("cluster_gap_projection", "inline"),
            require_executable_gap_design=values.get("require_executable_gap_design", True),
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    """Serializable safety and cost limits for one document-processing invocation."""

    direct_read_char_limit: int = 120_000
    chunk_char_limit: int = 60_000
    max_total_chunks: int = 64
    max_calls_per_document_run: int = 24
    request_deadline_seconds: float = 120.0
    document_deadline_seconds: float = 900.0
    chunk_output_tokens: int = 900
    synthesis_output_tokens: int = 3_000
    context_window_fraction: float = 0.8
    estimated_chars_per_token: float = 3.5

    def __post_init__(self) -> None:
        integer_fields = (
            "direct_read_char_limit",
            "chunk_char_limit",
            "max_total_chunks",
            "max_calls_per_document_run",
            "chunk_output_tokens",
            "synthesis_output_tokens",
        )
        for field_name in integer_fields:
            if int(getattr(self, field_name)) < 1:
                raise ValueError(f"processing.{field_name} must be at least 1")
        if self.request_deadline_seconds <= 0 or self.document_deadline_seconds <= 0:
            raise ValueError("processing deadlines must be positive")
        if self.document_deadline_seconds < self.request_deadline_seconds:
            raise ValueError("processing.document_deadline_seconds cannot be shorter than request_deadline_seconds")
        if not 0 < self.context_window_fraction < 1:
            raise ValueError("processing.context_window_fraction must be between 0 and 1")
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
            max_total_chunks=int(values.get("max_total_chunks", 64)),
            max_calls_per_document_run=int(values.get("max_calls_per_document_run", 24)),
            request_deadline_seconds=float(values.get("request_deadline_seconds", 120.0)),
            document_deadline_seconds=float(values.get("document_deadline_seconds", 900.0)),
            chunk_output_tokens=int(values.get("chunk_output_tokens", 900)),
            synthesis_output_tokens=int(values.get("synthesis_output_tokens", 3_000)),
            context_window_fraction=float(values.get("context_window_fraction", 0.8)),
            estimated_chars_per_token=float(values.get("estimated_chars_per_token", 3.5)),
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
    limit: int = 0
    extraction_version: str = "1"
    prompt_version: str = "2"
    processing: ProcessingPolicy = field(default_factory=ProcessingPolicy)
    literature_policy: LiteratureMappingPolicy = field(default_factory=LiteratureMappingPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        _require_bool(self.allow_cloud, field="allow_cloud")
        if self.model == "deepseek-v4-flash" and self.provider in {"ollama", "gemini"}:
            object.__setattr__(self, "model", {"ollama": "llama3.2", "gemini": "gemini-2.5-flash"}[self.provider])
        if self.provider == "openrouter" and self.model == "deepseek-v4-flash":
            raise ValueError("openrouter requires an explicit routed model id")
        if self.scope not in {"library", "collection", "selected"}:
            raise ValueError("scope must be library, collection, or selected")
        if self.scope == "collection" and not self.collection_key:
            raise ValueError("collection scope requires collection_key")
        if self.parallel < 1:
            raise ValueError("parallel must be at least 1")
        if self.limit < 0:
            raise ValueError("limit cannot be negative")
        if not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if not isinstance(self.processing, ProcessingPolicy):
            if isinstance(self.processing, Mapping):
                object.__setattr__(self, "processing", ProcessingPolicy.from_dict(self.processing))
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
                raise ValueError("literature_policy must be a LiteratureMappingPolicy or mapping")
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
            allow_cloud=_strict_bool(payload.get("allow_cloud", False), field="allow_cloud"),
            parallel=int(payload.get("parallel", 4)),
            limit=int(payload.get("limit", 0)),
            extraction_version=str(payload.get("extraction_version", "1")),
            prompt_version=str(payload.get("prompt_version", "2")),
            processing=ProcessingPolicy.from_dict(payload.get("processing") if isinstance(payload.get("processing"), Mapping) else None),
            literature_policy=payload.get("literature_policy", LiteratureMappingPolicy()),
        )


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
        raw_locators = payload.get("locators") or []
        locators = [raw_locators] if isinstance(raw_locators, str) else list(raw_locators)
        return cls(
            finding_id=str(payload.get("finding_id") or ""),
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


@dataclass(slots=True)
class EvidenceProfile:
    """Source-level features and findings consumed by literature synthesis."""

    profile_schema: str = "evidence_profile"
    profile_schema_version: str = "1.0"
    profile_id: str = ""
    note_id: str = ""
    source_id: str = ""
    note_hash: str = ""
    source_hash: str = ""
    source_role: str = ""
    coverage: dict[str, Any] = field(default_factory=dict)
    validity: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
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
    findings: list[EvidenceFinding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    future_research: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    dependency_hash: str = ""

    def __post_init__(self) -> None:
        _require_bool(self.excluded_from_synthesis, field="excluded_from_synthesis")
        normalized_findings: list[EvidenceFinding] = []
        for finding in self.findings:
            if isinstance(finding, EvidenceFinding):
                normalized_findings.append(finding)
            elif isinstance(finding, Mapping):
                normalized_findings.append(EvidenceFinding.from_dict(finding))
            else:
                raise ValueError("findings must contain EvidenceFinding values or mappings")
        self.findings = normalized_findings

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(slots=True)
class ClusterProposal:
    """A reasoner proposal whose membership must still pass deterministic gates."""

    proposal_id: str = ""
    label: str = ""
    semantic_identity: str = ""
    shared_question: str = ""
    coherence_rationale: str = ""
    source_ids: list[str] = field(default_factory=list)
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterProposal:
        values = _model_payload(cls, payload, label="cluster proposal")
        return cls(
            proposal_id=str(values.get("proposal_id") or ""),
            label=str(values.get("label") or ""),
            semantic_identity=str(values.get("semantic_identity") or values.get("label") or ""),
            shared_question=str(values.get("shared_question") or ""),
            coherence_rationale=str(values.get("coherence_rationale") or ""),
            source_ids=_string_list(values.get("source_ids"), field="cluster proposal.source_ids"),
            supporting_evidence=_mapping_list(
                values.get("supporting_evidence"), field="cluster proposal.supporting_evidence"
            ),
        )


@dataclass(slots=True)
class ClusterSynthesis:
    """Evidence-referenced narrative material for one admitted cluster."""

    cluster_id: str = ""
    scope: str = ""
    boundaries: list[str] = field(default_factory=list)
    coherence_rationale: str = ""
    synthesis: str = ""
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

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClusterSynthesis:
        values = _model_payload(cls, payload, label="cluster synthesis")

        def mappings(key: str) -> list[dict[str, Any]]:
            return _mapping_list(values.get(key), field=f"cluster synthesis.{key}")

        return cls(
            cluster_id=str(values.get("cluster_id") or ""),
            scope=str(values.get("scope") or ""),
            boundaries=_string_list(values.get("boundaries"), field="cluster synthesis.boundaries"),
            coherence_rationale=str(values.get("coherence_rationale") or ""),
            synthesis=str(values.get("synthesis") or ""),
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
        _require_bool(values.get("non_obviousness_passed", False), field="gap value assessment.non_obviousness_passed")
        _require_bool(values.get("importance_passed", False), field="gap value assessment.importance_passed")
        information_gain = str(values.get("information_gain") or "")
        if information_gain not in {"", "high", "moderate", "low"}:
            raise ValueError("gap value assessment.information_gain must be high, moderate, or low")
        return cls(
            puzzle_type=str(values.get("puzzle_type") or ""),
            puzzle=str(values.get("puzzle") or ""),
            strongest_obvious_answer=str(values.get("strongest_obvious_answer") or ""),
            why_obvious_answer_is_inadequate=str(values.get("why_obvious_answer_is_inadequate") or ""),
            competing_explanations=_string_list(
                values.get("competing_explanations"), field="gap value assessment.competing_explanations"
            ),
            decision_or_inference_changed=str(values.get("decision_or_inference_changed") or ""),
            information_gain=information_gain,  # type: ignore[arg-type]
            non_obviousness_passed=values.get("non_obviousness_passed", False),
            importance_passed=values.get("importance_passed", False),
            rejection_reasons=_string_list(
                values.get("rejection_reasons"), field="gap value assessment.rejection_reasons"
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
            outcomes=_string_list(values.get("outcomes"), field="gap study design.outcomes"),
            mechanism_measures=_string_list(
                values.get("mechanism_measures"), field="gap study design.mechanism_measures"
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
            validity_risks=_string_list(values.get("validity_risks"), field="gap study design.validity_risks"),
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
            "central_findings",
            "agreements",
            "positions",
            "contradictions",
            "boundary_conditions",
            "methodological_fault_lines",
            "related_clusters",
            "source_roles",
        }:
            raise ValueError("gap anchor.section is not a supported cluster synthesis section")
        return anchor


@dataclass(slots=True)
class GapRationale:
    """A collection-scoped explanation for one deterministically checked gap."""

    gap_id: str = ""
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
    anchors: list[GapAnchor] = field(default_factory=list)
    merged_from_gap_ids: list[str] = field(default_factory=list)
    reframed_from_gap_id: str = ""
    priority_tier: Literal["high", "moderate", "low", ""] = ""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GapRationale:
        values = _model_payload(cls, payload, label="gap rationale")
        for field_name in ("value_assessment", "study_design"):
            nested = values.get(field_name)
            if nested is not None and not isinstance(nested, Mapping):
                raise ValueError(f"gap rationale.{field_name} must be a mapping")

        def evidence(key: str) -> list[dict[str, Any]]:
            return _mapping_list(values.get(key), field=f"gap rationale.{key}")

        priority_tier = str(values.get("priority_tier") or "")
        if priority_tier not in {"", "high", "moderate", "low"}:
            raise ValueError("gap rationale.priority_tier must be high, moderate, or low")
        anchor_rows = _mapping_list(values.get("anchors"), field="gap rationale.anchors")
        return cls(
            gap_id=str(values.get("gap_id") or ""),
            title=str(values.get("title") or ""),
            gap_statement=str(values.get("gap_statement") or ""),
            rule=str(values.get("rule") or ""),
            related_cluster_ids=_string_list(values.get("related_cluster_ids"), field="gap rationale.related_cluster_ids"),
            generation_explanation=str(values.get("generation_explanation") or ""),
            observed_pattern=str(values.get("observed_pattern") or ""),
            precise_missing_evidence=str(values.get("precise_missing_evidence") or ""),
            supporting_evidence=evidence("supporting_evidence"),
            countervailing_evidence=evidence("countervailing_evidence"),
            internal_search_summary=str(values.get("internal_search_summary") or ""),
            closest_prior_explanation=str(values.get("closest_prior_explanation") or ""),
            decision_reasoning=str(values.get("decision_reasoning") or ""),
            evidence_needed=str(values.get("evidence_needed") or ""),
            why_matters=str(values.get("why_matters") or ""),
            contribution=str(values.get("contribution") or ""),
            confidence=str(values.get("confidence") or ""),
            value_assessment=GapValueAssessment.from_dict(
                values.get("value_assessment") if isinstance(values.get("value_assessment"), Mapping) else {}
            ),
            study_design=GapStudyDesign.from_dict(
                values.get("study_design") if isinstance(values.get("study_design"), Mapping) else {}
            ),
            anchors=[GapAnchor.from_dict(row) for row in anchor_rows],
            merged_from_gap_ids=_string_list(
                values.get("merged_from_gap_ids"), field="gap rationale.merged_from_gap_ids"
            ),
            reframed_from_gap_id=str(values.get("reframed_from_gap_id") or ""),
            priority_tier=priority_tier,  # type: ignore[arg-type]
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
    literature_policy: LiteratureMappingPolicy = field(default_factory=LiteratureMappingPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        _require_bool(self.allow_cloud, field="allow_cloud")
        if not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if not isinstance(self.literature_policy, LiteratureMappingPolicy):
            if isinstance(self.literature_policy, Mapping):
                object.__setattr__(
                    self,
                    "literature_policy",
                    LiteratureMappingPolicy.from_dict(self.literature_policy),
                )
            else:
                raise ValueError("literature_policy must be a LiteratureMappingPolicy or mapping")
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
            allow_cloud=_strict_bool(payload.get("allow_cloud", False), field="allow_cloud"),
            literature_policy=payload.get("literature_policy", LiteratureMappingPolicy()),
        )


@dataclass(slots=True)
class LiteratureMapReport:
    status: str
    map_id: str = ""
    run_id: str = ""
    source_set_id: str = ""
    stage: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    artifact_paths: dict[str, Path | str] = field(default_factory=dict)
    partial_reason: str = ""
    engine_version: str = CURRENT_ENGINE_VERSION
    artifact_schema_version: str = CURRENT_ARTIFACT_SCHEMA_VERSION

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

    @property
    def terminal_count(self) -> int:
        return self.validated_note_count + self.limited_note_count + self.exhausted_count

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
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


def _require_bool(value: Any, *, field: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")


def _require_positive_int(value: Any, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be an integer of at least 1")
