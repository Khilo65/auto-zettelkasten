from __future__ import annotations

import json
import re
from dataclasses import MISSING, asdict, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, get_args, get_origin, get_type_hints

import yaml

from .files import atomic_write_text, sha256_text, slugify
from .notes import (
    ANALYTICAL_NOTE_STATUSES,
    LIMITED_NOTE_STATUSES,
    canonical_source_note_text,
    parse_atomic_note,
    semantic_note_hash,
)


PROFILE_SCHEMA_VERSION = "1.0"
PROFILE_SIDECAR_VERSION = "1"
PROFILE_CHECKPOINT_VERSION = "1"
PROFILE_PROMPT_VERSION = "1"
PROFILE_CLASSIFIER_VERSION = "1"
PROFILE_ALGORITHM_VERSION = "1"

# Public lower-case aliases match the names persisted in dependency records.
profile_prompt_version = PROFILE_PROMPT_VERSION
profile_classifier_version = PROFILE_CLASSIFIER_VERSION
profile_algorithm_version = PROFILE_ALGORITHM_VERSION

GENERATED_GRAPH_HEADING = "## Graph Links"
LEGACY_AUTOMATED_VALIDATION_HEADING = "## Automated Validation"
LEGACY_SOURCE_FAITHFULNESS_HEADING = "## Source-Faithfulness Review"
GENERATED_NOTE_SECTION_MARKERS = (
    GENERATED_GRAPH_HEADING,
    LEGACY_AUTOMATED_VALIDATION_HEADING,
    LEGACY_SOURCE_FAITHFULNESS_HEADING,
)

_SIDECAR_FIELDS = frozenset({"profile_schema_version", "profile"})
_CHECKPOINT_FIELDS = frozenset({"checkpoint_schema_version", "fingerprint", "profile"})
_TRACEABLE_LOCATOR = re.compile(
    r"(?:\b(?:p{1,2}\.?|pages?|paragraphs?)\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\b|"
    r"\b(?:abstract|introduction|background|literature review|methods?|methodology|data|results?|findings?|"
    r"discussion|conclusions?|limitations?|appendix)\b|\b(?:table|figure)\s*\d+[a-z]?\b)",
    flags=re.IGNORECASE,
)
_STATISTICAL_FIGURE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:%|percent(?:age)?\b)|\bp\s*[<=>]\s*0?\.\d+|\bn\s*=\s*\d+|"
    r"\b(?:confidence|credible) intervals?\b|\b(?:odds|hazard) ratios?\b|"
    r"\b(?:coefficient|effect size|correlation|standard error|standard deviation)s?\b)",
    flags=re.IGNORECASE,
)
_NUMBERED_MAGNITUDE = re.compile(
    r"(?:-?\d+(?:\.\d+)?\s*(?:%|percent(?:age)?\b)|"
    r"(?:odds ratio|hazard ratio|coefficient|effect size|mean|median)\s*(?:of|=|:)\s*-?\d+(?:\.\d+)?)",
    flags=re.IGNORECASE,
)
_UNCERTAINTY = re.compile(
    r"(?:p\s*[<=>]\s*0?\.\d+|(?:95\s*%\s*)?(?:confidence|credible) interval[^.;]*|"
    r"standard (?:error|deviation)[^.;]*|statistically (?:significant|insignificant)|uncertain(?:ty)?)",
    flags=re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"(?:compared (?:with|to)[^;(]*|relative to[^;(]*|versus[^;(]*|than (?:the )?[^;(]*|"
    r"(?:control|reference|baseline) (?:group|category|period|value)?[^;(]*)",
    flags=re.IGNORECASE,
)
_CONDITION = re.compile(
    r"(?:\b(?:when|where|among|conditional on|only if|only when|for respondents|for cases)\b[^,.;]*)",
    flags=re.IGNORECASE,
)
_DIRECTION_TERMS = (
    ("negative", ("decrease", "decreased", "decline", "declined", "lower", "negative", "reduce", "reduced")),
    ("positive", ("increase", "increased", "higher", "positive", "raise", "raised", "grow", "grew")),
    ("no_clear_difference", ("no difference", "null effect", "not statistically significant", "unchanged")),
    ("mixed", ("mixed", "heterogeneous", "varied", "inconsistent")),
)
_LABEL_ALIASES: Mapping[str, tuple[str, ...]] = {
    "research_questions": ("research questions", "research question", "questions", "question"),
    "concepts": ("concepts", "concept", "constructs", "construct", "keywords", "keyword"),
    "theories": ("theories", "theory", "frameworks", "framework"),
    "mechanisms": ("mechanisms", "mechanism", "processes", "process"),
    "outcomes": ("outcomes", "outcome", "dependent variables", "dependent variable"),
    "cases": ("cases", "case", "units of analysis", "unit of analysis"),
    "populations": ("populations", "population", "sample", "participants"),
    "geographies": ("geographies", "geography", "locations", "location", "countries", "country"),
    "periods": ("periods", "period", "timeframe", "time frame", "study period"),
    "methods": ("methods", "method", "research design", "design"),
    "data_sources": ("data sources", "data source", "data", "dataset", "datasets"),
    "measures": ("measures", "measure", "measurement", "operationalization", "indicators", "indicator"),
    "author_stated_gaps": ("author stated gaps", "author-stated gaps", "research gaps", "gap", "gaps"),
    "future_research": ("future research", "further research", "research agenda"),
}
_PROFILE_ALIASES: Mapping[str, tuple[str, ...]] = {
    "schema_version": ("profile_version", "version", "profile_schema_version"),
    "profile_status": ("status",),
    "source_role": ("source_roles",),
    "research_questions": ("questions",),
    "geographies": ("geography",),
    "periods": ("time_periods",),
    "data_sources": ("data", "datasets"),
    "study_family": ("study_family_identity", "study_family_id"),
    "support_boundaries": ("support_boundary", "boundaries"),
    "author_stated_gaps": ("gaps",),
    "exclusion_reason": ("context_reason", "exclusion_context_reason"),
    "context_metadata": ("metadata",),
    "classifier_version": ("profile_classifier_version",),
    "algorithm_version": ("profile_algorithm_version",),
}
_FINDING_ALIASES: Mapping[str, tuple[str, ...]] = {
    "claim": ("statement", "finding", "text"),
    "plain_english_meaning": ("plain_english", "meaning"),
    "is_statistical": ("statistical",),
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate YAML field: {key}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class ProfileError(RuntimeError):
    """Base error for evidence-profile construction and persistence."""


class ProfileContractError(ProfileError):
    """The model dataclasses are absent or incompatible with the profile contract."""


class ProfileParseError(ProfileError):
    """A model response cannot be parsed as a strict evidence profile."""


class ProfilePersistenceError(ProfileError):
    """A profile sidecar is malformed, incompatible, or cannot be trusted."""


class ProfileCheckpointError(ProfileError):
    """A profile-call checkpoint is malformed or incompatible."""


@dataclass(frozen=True, slots=True)
class ProfileValidation:
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    substantive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "substantive": self.substantive,
        }


def strip_generated_note_sections(note_text: str) -> str:
    """Remove generated graph projections while preserving committed note content."""

    return _committed_note_text(note_text)


note_semantic_hash = semantic_note_hash


def profile_dependency_payload(
    note_text: str,
    *,
    source_set_id: str,
    provider: str,
    model: str,
    policy: Any,
    prompt_version: str = PROFILE_PROMPT_VERSION,
    classifier_version: str = PROFILE_CLASSIFIER_VERSION,
    algorithm_version: str = PROFILE_ALGORITHM_VERSION,
    profile_prompt_version: str | None = None,
    profile_classifier_version: str | None = None,
    profile_algorithm_version: str | None = None,
) -> dict[str, Any]:
    effective_prompt_version = profile_prompt_version or prompt_version
    effective_classifier_version = profile_classifier_version or classifier_version
    effective_algorithm_version = profile_algorithm_version or algorithm_version
    return {
        "note_semantic_hash": semantic_note_hash(note_text),
        "source_set_id": str(source_set_id),
        "provider": str(provider),
        "model": str(model),
        "policy": _canonical_value(policy),
        "profile_prompt_version": str(effective_prompt_version),
        "classifier_version": str(effective_classifier_version),
        "algorithm_version": str(effective_algorithm_version),
    }


def profile_dependency_fingerprint(
    note_text: str,
    *,
    source_set_id: str,
    provider: str,
    model: str,
    policy: Any,
    prompt_version: str = PROFILE_PROMPT_VERSION,
    classifier_version: str = PROFILE_CLASSIFIER_VERSION,
    algorithm_version: str = PROFILE_ALGORITHM_VERSION,
    profile_prompt_version: str | None = None,
    profile_classifier_version: str | None = None,
    profile_algorithm_version: str | None = None,
) -> str:
    payload = profile_dependency_payload(
        note_text,
        source_set_id=source_set_id,
        provider=provider,
        model=model,
        policy=policy,
        prompt_version=prompt_version,
        classifier_version=classifier_version,
        algorithm_version=algorithm_version,
        profile_prompt_version=profile_prompt_version,
        profile_classifier_version=profile_classifier_version,
        profile_algorithm_version=profile_algorithm_version,
    )
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(encoded)


profile_fingerprint = profile_dependency_fingerprint


def build_profile_prompt(note_text: str) -> str:
    """Build a compact prompt containing only committed Markdown note content."""

    profile_class, finding_class = _model_classes()
    profile_shape = _dataclass_shape(profile_class)
    finding_shape = _dataclass_shape(finding_class)
    committed_note = _committed_note_text(note_text)
    return (
        "Create one source-faithful evidence profile from the committed Markdown note below. "
        "Use only this note; do not infer from or request source full text. Return exactly one JSON object with no fences or commentary. "
        f"Profile keys and value kinds: {json.dumps(profile_shape, sort_keys=True, separators=(',', ':'))}. "
        f"Each findings item must use: {json.dumps(finding_shape, sort_keys=True, separators=(',', ':'))}. "
        "Keep substantive findings only for analytical full-document notes. Every substantive finding needs a traceable locator; "
        "statistical findings also need a plain-English meaning. Use empty strings, lists, or objects when the note does not supply a field.\n\n"
        f"COMMITTED MARKDOWN NOTE:\n{committed_note}"
    )


def parse_profile_json(value: str | bytes | bytearray) -> Any:
    """Parse a bare JSON object and reject malformed, duplicate, or unknown fields."""

    if not isinstance(value, (str, bytes, bytearray)):
        raise ProfileParseError("profile response must be JSON text")
    try:
        text = bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value
    except UnicodeDecodeError as exc:
        raise ProfileParseError("profile response must be UTF-8 JSON text") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda token: (_raise_invalid_json_constant(token)),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProfileParseError(f"profile response is not strict JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ProfileParseError("profile response must be one JSON object")
    try:
        return profile_from_dict(payload)
    except (ProfileContractError, TypeError, ValueError) as exc:
        raise ProfileParseError(f"invalid profile response: {exc}") from exc


def build_evidence_profile(
    note_text: str,
    *,
    source_set_id: str = "",
    provider: str = "deterministic",
    model: str = "deterministic-v1",
    policy: Any = None,
    reasoner_method: Callable[[str], Any] | None = None,
) -> Any:
    """Profile a committed note deterministically, or call a supplied reasoner for analytical notes."""

    frontmatter, _ = _parse_note(note_text)
    analytical_full_document = _is_analytical_full_document(frontmatter)
    if not analytical_full_document or reasoner_method is None:
        return deterministic_profile(
            note_text,
            source_set_id=source_set_id,
            provider=provider,
            model=model,
            policy=policy,
        )
    response = reasoner_method(build_profile_prompt(note_text))
    profile = parse_profile_json(response) if isinstance(response, (str, bytes, bytearray)) else profile_from_dict(response)
    _require_matching_lineage(profile, frontmatter)
    profile = _apply_controlled_profile_metadata(
        profile,
        note_text,
        frontmatter,
        source_set_id=source_set_id,
        provider=provider,
        model=model,
        policy=policy,
    )
    validation = validate_profile(profile)
    if not validation.passed:
        raise ProfileParseError(f"reasoner profile failed validation: {', '.join(validation.errors)}")
    return profile


profile_note = build_evidence_profile


def deterministic_profile(
    note_text: str,
    *,
    source_set_id: str = "",
    provider: str = "deterministic",
    model: str = "deterministic-v1",
    policy: Any = None,
) -> Any:
    frontmatter, body = _parse_note(note_text)
    sections = _markdown_sections(_strip_generated_body(body))
    note_status = str(frontmatter.get("note_status") or "")
    source_scope = str(frontmatter.get("source_scope") or "")
    coverage_gate = _coverage_gate(frontmatter.get("source_coverage"))
    limited = not _is_analytical_full_document(frontmatter)
    context_metadata = _context_metadata(frontmatter)
    zotero_tag_context = [str(value) for value in frontmatter.get("normalized_tags", []) if str(value).strip()]
    stated_concepts = _labeled_values(body, "concepts")
    # Zotero tags are context for limited notes and a weak relation signal for
    # analytical notes; they are not substantive semantic evidence by themselves.
    concepts = _dedupe(stated_concepts + (zotero_tag_context if limited else []))
    exclusion_reason = ""
    if limited:
        exclusion_reason = _limited_reason(note_status, source_scope, sections)
    source_role = _source_role(frontmatter, sections) if not limited else "context_only"
    questions = [] if limited else _research_questions(sections)
    detailed_findings = sections.get("Detailed Findings", "")
    plain_english = sections.get("Plain-English Interpretation", "")
    locator_text = sections.get("Locators", "")
    populations = [] if limited else _populations(sections)
    outcomes = [] if limited else _labeled_values(body, "outcomes")
    finding_payloads = [] if limited else _extract_findings(
        detailed_findings,
        plain_english,
        locator_text,
        note_id=str(frontmatter.get("note_id") or ""),
        populations=populations,
        outcomes=outcomes,
    )
    findings = [_construct_finding(payload) for payload in finding_payloads]
    support_boundaries = _support_boundaries(sections, exclusion_reason)
    semantic_hash = semantic_note_hash(note_text)
    dependency_hash = profile_dependency_fingerprint(
        note_text,
        source_set_id=source_set_id,
        provider=provider,
        model=model,
        policy=policy,
    )
    geographies = [] if limited else _geographies(frontmatter, body)
    periods = [] if limited else _periods(sections)
    data_sources = [] if limited else _data_sources(sections)
    study_family = _study_family(frontmatter)
    full_document = note_status in ANALYTICAL_NOTE_STATUSES and source_scope == "full_document" and coverage_gate == "passed"
    coverage = {
        "note_status": note_status,
        "source_scope": source_scope,
        "coverage_gate": coverage_gate,
        "full_document": full_document,
    }
    validity = {
        "status": "valid" if full_document else "excluded_context_only",
        "analytical": note_status in ANALYTICAL_NOTE_STATUSES,
        "full_document": full_document,
        "profile_prompt_version": PROFILE_PROMPT_VERSION,
        "classifier_version": PROFILE_CLASSIFIER_VERSION,
        "algorithm_version": PROFILE_ALGORITHM_VERSION,
    }
    features = {
        "source_role": [source_role] if source_role else [],
        "geography": geographies,
        "periods": periods,
        "data_sources": data_sources,
        "zotero_tag_context": zotero_tag_context,
    }
    canonical = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": f"profile-{semantic_hash[:16]}",
        "note_id": str(frontmatter.get("note_id") or ""),
        "source_id": str(frontmatter.get("source_id") or ""),
        "note_hash": semantic_hash,
        "source_hash": str(frontmatter.get("inspected_content_hash") or ""),
        "note_status": note_status,
        "source_scope": source_scope,
        "coverage_gate": coverage_gate,
        "profile_status": "limited_context_only" if limited else "analytical",
        "source_role": source_role,
        "coverage": coverage,
        "validity": validity,
        "context": {
            "metadata": context_metadata,
            "note_status": note_status,
            "source_scope": source_scope,
            "source_set_id": source_set_id,
            "exclusion_reason": exclusion_reason,
        },
        "excluded_from_synthesis": limited,
        "features": features,
        "research_questions": questions,
        "concepts": concepts,
        "theories": [] if limited else _labeled_values(body, "theories"),
        "mechanisms": [] if limited else _labeled_values(body, "mechanisms"),
        "outcomes": outcomes,
        "cases": [] if limited else _labeled_values(body, "cases"),
        "populations": populations,
        "geographies": geographies,
        "periods": periods,
        "methods": [] if limited else _methods(sections),
        "data_sources": data_sources,
        "measures": [] if limited else _measures(sections),
        "study_family": study_family["identity"],
        "findings": findings,
        "limitations": [] if limited else _content_items(sections.get("Limitations", "")),
        "support_boundaries": support_boundaries,
        "author_stated_gaps": [] if limited else _labeled_values(body, "author_stated_gaps"),
        "future_research": [] if limited else _labeled_values(body, "future_research"),
        "exclusion_reason": exclusion_reason,
        "context_metadata": context_metadata,
        "profile_prompt_version": PROFILE_PROMPT_VERSION,
        "classifier_version": PROFILE_CLASSIFIER_VERSION,
        "algorithm_version": PROFILE_ALGORITHM_VERSION,
        "provider": provider,
        "model": model,
        "dependency_hash": dependency_hash,
    }
    return _construct_profile(canonical)


def validate_profile(profile: Any, *, require_substantive: bool = True) -> ProfileValidation:
    payload = profile_to_dict(profile)
    errors: list[str] = []
    warnings: list[str] = []
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), Mapping) else {}
    context = payload.get("context") if isinstance(payload.get("context"), Mapping) else {}
    status = str(_value(payload, "note_status") or coverage.get("note_status") or context.get("note_status") or "")
    scope = str(_value(payload, "source_scope") or coverage.get("source_scope") or context.get("source_scope") or "")
    gate = str(_value(payload, "coverage_gate") or coverage.get("coverage_gate") or "")
    findings = _value(payload, "findings") or []
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes, bytearray)):
        errors.append("findings_must_be_a_list")
        findings = []
    limited = (
        status in LIMITED_NOTE_STATUSES
        or bool(payload.get("excluded_from_synthesis"))
        or str(_value(payload, "profile_status") or "").startswith("limited")
    )
    substantive = status in ANALYTICAL_NOTE_STATUSES and scope == "full_document" and gate == "passed" and not limited
    if limited and findings:
        errors.append("limited_profile_contains_substantive_findings")
    if require_substantive and not substantive:
        errors.append("analytical_full_document_profile_required")
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            errors.append(f"finding_{index}:must_be_an_object")
            continue
        claim = str(_value(finding, "claim", aliases=_FINDING_ALIASES) or "").strip()
        locator = str(_value(finding, "locator", aliases=_FINDING_ALIASES) or "").strip()
        meaning = str(_value(finding, "plain_english_meaning", aliases=_FINDING_ALIASES) or "").strip()
        statistical = (
            bool(_value(finding, "is_statistical", aliases=_FINDING_ALIASES))
            or str(finding.get("finding_type") or "").casefold() == "statistical"
            or bool(_STATISTICAL_FIGURE.search(claim))
        )
        if not claim:
            errors.append(f"finding_{index}:missing_claim")
        if not locator or not _TRACEABLE_LOCATOR.search(locator):
            errors.append(f"finding_{index}:traceable_locator_required")
        if statistical and not meaning:
            errors.append(f"finding_{index}:plain_english_meaning_required_for_statistical_finding")
    if substantive and not findings:
        warnings.append("analytical_profile_has_no_substantive_findings")
    return ProfileValidation(passed=not errors, errors=tuple(errors), warnings=tuple(warnings), substantive=substantive)


validate_evidence_profile = validate_profile


def profile_to_dict(profile: Any) -> dict[str, Any]:
    profile_class, _ = _model_classes()
    if not isinstance(profile, profile_class) or not is_dataclass(profile):
        raise ProfileContractError("profile must be an EvidenceProfile dataclass")
    return _canonical_value(asdict(profile))


def profile_from_dict(payload: Mapping[str, Any]) -> Any:
    if not isinstance(payload, Mapping):
        raise ProfileContractError("profile must be an object")
    profile_class, _ = _model_classes()
    allowed = {field.name for field in fields(profile_class)}
    unknown = set(payload) - allowed
    if unknown:
        raise ProfileContractError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    values = dict(payload)
    _validate_dataclass_value_types(profile_class, values, "profile")
    findings_field = _actual_field_name(profile_class, "findings", _PROFILE_ALIASES)
    if findings_field and findings_field in values:
        raw_findings = values[findings_field]
        if not isinstance(raw_findings, list):
            raise ProfileContractError("profile.findings must be a list")
        values[findings_field] = [_finding_from_dict(row, index=index) for index, row in enumerate(raw_findings)]
    _require_dataclass_fields(profile_class, values, "profile")
    try:
        return profile_class(**values)
    except (TypeError, ValueError) as exc:
        raise ProfileContractError(f"profile does not match EvidenceProfile: {exc}") from exc


def profile_sidecar_path(profiles_dir: Path | str, note_id: str) -> Path:
    identifier = slugify(str(note_id), fallback="profile")
    return Path(profiles_dir) / f"{identifier}.yml"


def save_profile(profiles_dir: Path | str, profile: Any) -> Path:
    note_id = str(_value(profile_to_dict(profile), "note_id") or "")
    if not note_id:
        raise ProfilePersistenceError("profile.note_id is required for sidecar persistence")
    path = profile_sidecar_path(profiles_dir, note_id)
    write_profile_sidecar(path, profile)
    return path


def write_profile_sidecar(path: Path | str, profile: Any) -> bool:
    target = Path(path)
    payload = {"profile_schema_version": PROFILE_SIDECAR_VERSION, "profile": profile_to_dict(profile)}
    if target.exists():
        existing = _read_yaml_mapping(target, error_type=ProfilePersistenceError, label="profile sidecar")
        _validate_wrapper(existing, _SIDECAR_FIELDS, "profile_schema_version", PROFILE_SIDECAR_VERSION, ProfilePersistenceError)
        try:
            existing_profile = profile_from_dict(_required_mapping(existing, "profile", ProfilePersistenceError))
        except ProfileContractError as exc:
            raise ProfilePersistenceError(f"malformed profile sidecar {target}: {exc}") from exc
        if profile_to_dict(existing_profile) == payload["profile"]:
            return False
    atomic_write_text(target, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return True


def load_profile(profiles_dir: Path | str, note_id: str) -> Any:
    return load_profile_sidecar(profile_sidecar_path(profiles_dir, note_id))


def load_profile_sidecar(path: Path | str) -> Any:
    target = Path(path)
    payload = _read_yaml_mapping(target, error_type=ProfilePersistenceError, label="profile sidecar")
    _validate_wrapper(payload, _SIDECAR_FIELDS, "profile_schema_version", PROFILE_SIDECAR_VERSION, ProfilePersistenceError)
    try:
        return profile_from_dict(_required_mapping(payload, "profile", ProfilePersistenceError))
    except ProfileContractError as exc:
        raise ProfilePersistenceError(f"malformed profile sidecar {target}: {exc}") from exc


def profile_checkpoint_path(literature_state_dir: Path | str, note_id: str) -> Path:
    identifier = slugify(str(note_id), fallback="profile")
    return Path(literature_state_dir) / "profile_calls" / f"{identifier}.yml"


def write_profile_checkpoint(
    literature_state_dir: Path | str,
    note_id: str,
    fingerprint: str,
    profile: Any,
) -> Path:
    if not fingerprint:
        raise ProfileCheckpointError("profile checkpoint fingerprint cannot be empty")
    path = profile_checkpoint_path(literature_state_dir, note_id)
    serialized_profile = profile_to_dict(profile)
    profile_note_id = str(_value(serialized_profile, "note_id") or "")
    if profile_note_id != str(note_id):
        raise ProfileCheckpointError("profile checkpoint note_id does not match the profile")
    payload = {
        "checkpoint_schema_version": PROFILE_CHECKPOINT_VERSION,
        "fingerprint": str(fingerprint),
        "profile": serialized_profile,
    }
    if path.exists():
        existing = _read_yaml_mapping(path, error_type=ProfileCheckpointError, label="profile checkpoint")
        _validate_wrapper(
            existing,
            _CHECKPOINT_FIELDS,
            "checkpoint_schema_version",
            PROFILE_CHECKPOINT_VERSION,
            ProfileCheckpointError,
        )
        try:
            profile_from_dict(_required_mapping(existing, "profile", ProfileCheckpointError))
        except ProfileContractError as exc:
            raise ProfileCheckpointError(f"malformed profile checkpoint {path}: {exc}") from exc
        if _canonical_value(existing) == _canonical_value(payload):
            return path
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return path


save_profile_checkpoint = write_profile_checkpoint


def load_profile_checkpoint(
    literature_state_dir: Path | str,
    note_id: str,
    expected_fingerprint: str,
) -> Any | None:
    path = profile_checkpoint_path(literature_state_dir, note_id)
    if not path.exists():
        return None
    payload = _read_yaml_mapping(path, error_type=ProfileCheckpointError, label="profile checkpoint")
    _validate_wrapper(
        payload,
        _CHECKPOINT_FIELDS,
        "checkpoint_schema_version",
        PROFILE_CHECKPOINT_VERSION,
        ProfileCheckpointError,
    )
    try:
        profile = profile_from_dict(_required_mapping(payload, "profile", ProfileCheckpointError))
    except ProfileContractError as exc:
        raise ProfileCheckpointError(f"malformed profile checkpoint {path}: {exc}") from exc
    if str(payload.get("fingerprint") or "") != str(expected_fingerprint):
        return None
    return profile


def _model_classes() -> tuple[type[Any], type[Any]]:
    from . import models

    profile_class = getattr(models, "EvidenceProfile", None)
    finding_class = getattr(models, "EvidenceFinding", None)
    if not isinstance(profile_class, type) or not is_dataclass(profile_class):
        raise ProfileContractError("models.EvidenceProfile dataclass is required")
    if not isinstance(finding_class, type) or not is_dataclass(finding_class):
        raise ProfileContractError("models.EvidenceFinding dataclass is required")
    return profile_class, finding_class


def _construct_profile(canonical: Mapping[str, Any]) -> Any:
    profile_class, _ = _model_classes()
    values = _adapt_canonical_values(profile_class, canonical, _PROFILE_ALIASES)
    _require_dataclass_fields(profile_class, values, "profile")
    try:
        return profile_class(**values)
    except (TypeError, ValueError) as exc:
        raise ProfileContractError(f"EvidenceProfile integration contract mismatch: {exc}") from exc


def _construct_finding(canonical: Mapping[str, Any]) -> Any:
    _, finding_class = _model_classes()
    values = _adapt_canonical_values(finding_class, canonical, _FINDING_ALIASES)
    _require_dataclass_fields(finding_class, values, "finding")
    try:
        return finding_class(**values)
    except (TypeError, ValueError) as exc:
        raise ProfileContractError(f"EvidenceFinding integration contract mismatch: {exc}") from exc


def _finding_from_dict(payload: Any, *, index: int) -> Any:
    if not isinstance(payload, Mapping):
        raise ProfileContractError(f"profile.findings[{index}] must be an object")
    _, finding_class = _model_classes()
    allowed = {field.name for field in fields(finding_class)}
    unknown = set(payload) - allowed
    if unknown:
        raise ProfileContractError(f"unknown finding fields at index {index}: {', '.join(sorted(unknown))}")
    values = dict(payload)
    _validate_dataclass_value_types(finding_class, values, f"profile.findings[{index}]")
    _require_dataclass_fields(finding_class, values, f"profile.findings[{index}]")
    try:
        return finding_class(**values)
    except (TypeError, ValueError) as exc:
        raise ProfileContractError(f"invalid finding at index {index}: {exc}") from exc


def _adapt_canonical_values(
    dataclass_type: type[Any],
    canonical: Mapping[str, Any],
    aliases: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    hints = get_type_hints(dataclass_type)
    values: dict[str, Any] = {}
    for field in fields(dataclass_type):
        canonical_name = next(
            (
                name
                for name, alternatives in aliases.items()
                if field.name == name or field.name in alternatives
            ),
            field.name,
        )
        if canonical_name not in canonical:
            continue
        values[field.name] = _coerce_for_annotation(canonical[canonical_name], hints.get(field.name, field.type))
    return values


def _coerce_for_annotation(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {list, Sequence}:
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [] if value in (None, "") else [value]
    if origin is tuple:
        return tuple(value) if isinstance(value, (list, tuple)) else (() if value in (None, "") else (value,))
    if origin is not None and type(None) in args:
        non_none = next((candidate for candidate in args if candidate is not type(None)), Any)
        return None if value is None else _coerce_for_annotation(value, non_none)
    if annotation is str and isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    if annotation is str and isinstance(value, Mapping):
        return json.dumps(_canonical_value(value), sort_keys=True, ensure_ascii=False)
    return value


def _require_dataclass_fields(dataclass_type: type[Any], values: Mapping[str, Any], label: str) -> None:
    missing = [
        field.name
        for field in fields(dataclass_type)
        if field.init and field.default is MISSING and field.default_factory is MISSING and field.name not in values
    ]
    if missing:
        raise ProfileContractError(f"missing required {label} fields: {', '.join(missing)}")


def _validate_dataclass_value_types(
    dataclass_type: type[Any],
    values: Mapping[str, Any],
    label: str,
) -> None:
    hints = get_type_hints(dataclass_type)
    for field in fields(dataclass_type):
        if field.name in values:
            _require_value_type(values[field.name], hints.get(field.name, field.type), f"{label}.{field.name}")


def _require_value_type(value: Any, annotation: Any, label: str) -> None:
    if annotation is Any:
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is not None and type(None) in args:
        if value is None:
            return
        non_none = next(candidate for candidate in args if candidate is not type(None))
        _require_value_type(value, non_none, label)
        return
    if origin is list:
        if not isinstance(value, list):
            raise ProfileContractError(f"{label} must be a list")
        return
    if origin is tuple:
        if not isinstance(value, list):
            raise ProfileContractError(f"{label} must be a JSON array")
        return
    if origin is dict:
        if not isinstance(value, Mapping):
            raise ProfileContractError(f"{label} must be an object")
        return
    if annotation is str and not isinstance(value, str):
        raise ProfileContractError(f"{label} must be a string")
    if annotation is bool and not isinstance(value, bool):
        raise ProfileContractError(f"{label} must be a boolean")
    if annotation is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise ProfileContractError(f"{label} must be an integer")
    if annotation is float and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ProfileContractError(f"{label} must be a number")


def _actual_field_name(
    dataclass_type: type[Any],
    canonical_name: str,
    aliases: Mapping[str, tuple[str, ...]],
) -> str | None:
    names = {field.name for field in fields(dataclass_type)}
    return next((name for name in (canonical_name, *aliases.get(canonical_name, ())) if name in names), None)


def _parse_note(note_text: str) -> tuple[dict[str, Any], str]:
    try:
        frontmatter, body = parse_atomic_note(note_text)
    except yaml.YAMLError as exc:
        raise ProfileParseError(f"committed note has malformed YAML frontmatter: {exc}") from exc
    return frontmatter, body


def _committed_note_text(note_text: str) -> str:
    return canonical_source_note_text(note_text)


def _strip_generated_body(body: str) -> str:
    marker_pattern = "|".join(re.escape(marker) for marker in GENERATED_NOTE_SECTION_MARKERS)
    return re.sub(rf"\n*(?:{marker_pattern})\s*\n.*\Z", "", body, flags=re.DOTALL).rstrip()


def _normalize_markdown(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.strip().splitlines()) + "\n"


def _markdown_sections(body: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip()] = body[match.end() : end].strip()
    return sections


def _context_metadata(frontmatter: Mapping[str, Any]) -> dict[str, Any]:
    fields_to_keep = (
        "title",
        "creators",
        "date",
        "publicationTitle",
        "publisher",
        "DOI",
        "doi",
        "url",
        "itemType",
        "zotero_item_key",
        "original_zotero_tags",
        "normalized_tags",
    )
    return {key: _canonical_value(frontmatter[key]) for key in fields_to_keep if frontmatter.get(key) not in (None, "", [], {})}


def _limited_reason(note_status: str, source_scope: str, sections: Mapping[str, str]) -> str:
    supplied = sections.get("Scope Limitation") or sections.get("Processing Status")
    if supplied:
        return supplied
    defaults = {
        "abstract_only_atomic_note": "Abstract-only context; the full publication was not profiled and no substantive finding is supported.",
        "metadata_only_source_note": "Metadata-only context; no source text supports substantive findings.",
        "fulltext_available": "Full text is available but no committed analytical note supports substantive findings yet.",
    }
    return defaults.get(
        note_status,
        f"Unsupported note status or source scope ({note_status or 'missing status'}, {source_scope or 'missing scope'}); context only.",
    )


def _is_analytical_full_document(frontmatter: Mapping[str, Any]) -> bool:
    return (
        str(frontmatter.get("note_status") or "") in ANALYTICAL_NOTE_STATUSES
        and str(frontmatter.get("source_scope") or "") == "full_document"
        and _coverage_gate(frontmatter.get("source_coverage")) == "passed"
    )


def _source_role(frontmatter: Mapping[str, Any], sections: Mapping[str, str]) -> str:
    text = " ".join(
        [
            str(frontmatter.get("title") or ""),
            sections.get("Thesis", ""),
            sections.get("Method and Research Design", ""),
        ]
    ).casefold()
    if "meta-analysis" in text or "meta analysis" in text:
        return "meta_analysis"
    if "systematic review" in text or "literature review" in text:
        return "literature_review"
    if "conceptual" in text or "theoretical" in text or "theory-building" in text:
        return "theoretical"
    if "methodological" in text or "measurement" in text:
        return "methodological"
    if any(term in text for term in ("experiment", "randomized", "regression", "survey", "interview", "case study")):
        return "empirical"
    return "analytical_source"


def _research_questions(sections: Mapping[str, str]) -> list[str]:
    thesis = sections.get("Thesis", "")
    explicit = _labeled_values(thesis, "research_questions")
    questions = re.findall(r"(?:^|(?<=[.!]))\s*([^\n?]{8,}\?)", thesis)
    return _dedupe([*explicit, *(question.strip() for question in questions)])


def _labeled_values(text: str, key: str) -> list[str]:
    aliases = _LABEL_ALIASES[key]
    label_pattern = "|".join(re.escape(alias) for alias in aliases)
    values: list[str] = []
    for match in re.finditer(rf"^(?:[-*]\s*)?(?:\*\*)?(?:{label_pattern})(?:\*\*)?\s*:\s*(.+)$", text, flags=re.MULTILINE | re.IGNORECASE):
        values.extend(_split_values(match.group(1)))
    return _dedupe(values)


def _split_values(value: str) -> list[str]:
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    if not value:
        return []
    pieces = re.split(r"\s*(?:;|\||,(?=\s*(?:and\s+)?[^,]{1,80}$))\s*", value)
    return [piece.strip(" -") for piece in pieces if piece.strip(" -")]


def _methods(sections: Mapping[str, str]) -> list[str]:
    text = sections.get("Method and Research Design", "")
    explicit = _labeled_values(text, "methods")
    terms = (
        "randomized controlled trial",
        "difference-in-differences",
        "regression discontinuity",
        "instrumental variables",
        "process tracing",
        "case study",
        "comparative case study",
        "survey",
        "interviews",
        "ethnography",
        "content analysis",
        "panel regression",
        "logistic regression",
        "linear regression",
        "meta-analysis",
    )
    lowered = text.casefold()
    return _dedupe([*explicit, *(term for term in terms if term in lowered)])


def _data_sources(sections: Mapping[str, str]) -> list[str]:
    text = "\n".join((sections.get("Method and Research Design", ""), sections.get("Evidence and Data", "")))
    explicit = _labeled_values(text, "data_sources")
    terms = ("survey data", "administrative data", "census data", "interview data", "archival data", "panel data", "registry data")
    lowered = text.casefold()
    return _dedupe([*explicit, *(term for term in terms if term in lowered)])


def _measures(sections: Mapping[str, str]) -> list[str]:
    text = "\n".join((sections.get("Method and Research Design", ""), sections.get("Evidence and Data", "")))
    return _labeled_values(text, "measures")


def _populations(sections: Mapping[str, str]) -> list[str]:
    text = "\n".join((sections.get("Method and Research Design", ""), sections.get("Evidence and Data", "")))
    return _labeled_values(text, "populations")


def _geographies(frontmatter: Mapping[str, Any], body: str) -> list[str]:
    values = _labeled_values(body, "geographies")
    for key in ("place", "country", "geography"):
        if frontmatter.get(key):
            values.extend(_split_values(str(frontmatter[key])))
    return _dedupe(values)


def _periods(sections: Mapping[str, str]) -> list[str]:
    text = "\n".join((sections.get("Method and Research Design", ""), sections.get("Evidence and Data", "")))
    explicit = _labeled_values(text, "periods")
    year_ranges = re.findall(r"\b(?:19|20)\d{2}\s*[-\u2013\u2014]\s*(?:19|20)\d{2}\b", text)
    return _dedupe([*explicit, *year_ranges])


def _study_family(frontmatter: Mapping[str, Any]) -> dict[str, str]:
    doi = str(frontmatter.get("DOI") or frontmatter.get("doi") or "").strip().casefold()
    title = re.sub(r"\W+", " ", str(frontmatter.get("title") or "")).casefold().strip()
    identity = f"doi:{doi}" if doi else (f"title:{title}" if title else "")
    return {"identity": identity, "basis": "doi" if doi else ("normalized_title" if title else "unavailable")}


def _support_boundaries(sections: Mapping[str, str], exclusion_reason: str) -> list[str]:
    boundaries: list[str] = []
    supported = sections.get("What This Source Can Support", "")
    unsupported = sections.get("What This Source Cannot Support", "")
    if supported:
        boundaries.append(f"Can support: {supported}")
    if unsupported:
        boundaries.append(f"Cannot support: {unsupported}")
    if exclusion_reason:
        boundaries.append(f"Context only: {exclusion_reason}")
    return boundaries


def _extract_findings(
    detailed: str,
    plain_english: str,
    locator_text: str,
    *,
    note_id: str,
    populations: Sequence[str],
    outcomes: Sequence[str],
) -> list[dict[str, Any]]:
    claims = _content_items(detailed)
    meanings = _content_items(plain_english)
    fallback_locator = _first_locator(locator_text)
    findings: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        if _non_finding(claim):
            continue
        locator = _first_locator(claim) or fallback_locator
        statistical = bool(_STATISTICAL_FIGURE.search(claim))
        meaning = meanings[min(index, len(meanings) - 1)] if meanings else ""
        condition = _match_text(_CONDITION, claim)
        magnitude = _match_text(_NUMBERED_MAGNITUDE, claim)
        uncertainty = _match_text(_UNCERTAINTY, claim)
        identity = json.dumps(
            {"note_id": note_id, "claim": claim, "locator": locator, "index": index},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        findings.append(
            {
                "finding_id": f"finding-{sha256_text(identity)[:16]}",
                "claim": claim,
                "finding_type": "statistical" if statistical else "qualitative",
                "direction": _direction(claim),
                "magnitude": magnitude,
                "comparison": _match_text(_COMPARISON, claim),
                "conditions": [] if condition == "not_reported" else [condition],
                "plain_english_meaning": meaning,
                "population": populations[0] if populations else "not_reported",
                "outcome": outcomes[0] if outcomes else "not_reported",
                "estimate": magnitude,
                "uncertainty": uncertainty,
                "evidence": claim,
                "locator": locator,
                "locators": [locator] if locator else [],
                "qualifiers": [] if condition == "not_reported" else [condition],
                "confidence": "uncertainty_reported" if uncertainty != "not_reported" else "not_reported",
                "is_statistical": statistical,
            }
        )
    return findings


def _content_items(text: str) -> list[str]:
    if not text.strip():
        return []
    bullets = [
        re.sub(r"^[-*]\s+", "", line).strip()
        for line in text.splitlines()
        if re.match(r"^[-*]\s+\S", line.strip())
    ]
    if bullets:
        return _dedupe(bullets)
    paragraphs = [re.sub(r"\s+", " ", paragraph).strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    return _dedupe(paragraphs)


def _non_finding(claim: str) -> bool:
    lowered = claim.casefold()
    return any(
        phrase in lowered
        for phrase in (
            "no substantive finding",
            "no quantitative estimates",
            "not reported in the note",
            "requires the full text",
            "full publication was not",
        )
    )


def _direction(text: str) -> str:
    lowered = text.casefold()
    for label, terms in _DIRECTION_TERMS:
        if any(term in lowered for term in terms):
            return label
    return "not_reported"


def _first_locator(text: str) -> str:
    return "; ".join(_dedupe([match.group(0) for match in _TRACEABLE_LOCATOR.finditer(text)]))


def _match_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0).strip() if match else "not_reported"


def _coverage_gate(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("gate", "coverage_gate", "status"):
            if key in value:
                return str(value[key]).strip().casefold()
        if value.get("passed") is True:
            return "passed"
        if value.get("passed") is False:
            return "failed"
        return ""
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return str(value or "").strip().casefold()


def _require_matching_lineage(profile: Any, frontmatter: Mapping[str, Any]) -> None:
    payload = profile_to_dict(profile)
    for field in ("note_id", "source_id"):
        expected = str(frontmatter.get(field) or "")
        actual = str(_value(payload, field) or "")
        if actual and expected != actual:
            raise ProfileParseError(f"profile response changed committed {field}")


def _apply_controlled_profile_metadata(
    profile: Any,
    note_text: str,
    frontmatter: Mapping[str, Any],
    *,
    source_set_id: str,
    provider: str,
    model: str,
    policy: Any,
) -> Any:
    payload = profile_to_dict(profile)
    filtered_findings: list[dict[str, Any]] = []
    omitted_findings = 0
    for finding in payload.get("findings", []) or []:
        candidate = dict(finding)
        locator = str(candidate.get("locator") or "").strip()
        if not _TRACEABLE_LOCATOR.search(locator):
            locator = next(
                (
                    str(value).strip()
                    for value in candidate.get("locators", []) or []
                    if _TRACEABLE_LOCATOR.search(str(value))
                ),
                "",
            )
            if locator:
                candidate["locator"] = locator
        statistical = bool(candidate.get("is_statistical")) or bool(
            _STATISTICAL_FIGURE.search(str(candidate.get("claim") or ""))
        )
        if not locator or (statistical and not str(candidate.get("plain_english_meaning") or "").strip()):
            omitted_findings += 1
            continue
        filtered_findings.append(candidate)
    payload["findings"] = filtered_findings
    note_hash = semantic_note_hash(note_text)
    payload.update(
        profile_schema="evidence_profile",
        profile_schema_version=PROFILE_SCHEMA_VERSION,
        profile_id=f"profile-{note_hash[:16]}",
        note_id=str(frontmatter.get("note_id") or ""),
        source_id=str(frontmatter.get("source_id") or ""),
        note_hash=note_hash,
        source_hash=str(frontmatter.get("inspected_content_hash") or ""),
        provider=provider,
        model=model,
        dependency_hash=profile_dependency_fingerprint(
            note_text,
            source_set_id=source_set_id,
            provider=provider,
            model=model,
            policy=policy,
        ),
    )
    validity = dict(payload.get("validity") or {})
    validity.update(
        profile_prompt_version=PROFILE_PROMPT_VERSION,
        classifier_version=PROFILE_CLASSIFIER_VERSION,
        algorithm_version=PROFILE_ALGORITHM_VERSION,
        omitted_untraceable_or_uninterpreted_finding_count=omitted_findings,
    )
    payload["validity"] = validity
    coverage = dict(payload.get("coverage") or {})
    coverage.update(
        note_status=str(frontmatter.get("note_status") or ""),
        source_scope=str(frontmatter.get("source_scope") or ""),
        coverage_gate=_coverage_gate(frontmatter.get("source_coverage")),
        full_document=_is_analytical_full_document(frontmatter),
    )
    payload["coverage"] = coverage
    context = dict(payload.get("context") or {})
    context.update(
        source_set_id=source_set_id,
        note_status=str(frontmatter.get("note_status") or ""),
        source_scope=str(frontmatter.get("source_scope") or ""),
    )
    payload["context"] = context
    return profile_from_dict(payload)


def _value(
    payload: Any,
    canonical_name: str,
    *,
    aliases: Mapping[str, tuple[str, ...]] = _PROFILE_ALIASES,
) -> Any:
    if not isinstance(payload, Mapping):
        return None
    for key in (canonical_name, *aliases.get(canonical_name, ())):
        if key in payload:
            return payload[key]
    return None


def _read_yaml_mapping(path: Path, *, error_type: type[ProfileError], label: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_type(f"cannot read {label} {path}: {exc}") from exc
    try:
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        raise error_type(f"malformed {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise error_type(f"malformed {label} {path}: root must be a mapping")
    return dict(value)


def _validate_wrapper(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
    version_field: str,
    expected_version: str,
    error_type: type[ProfileError],
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise error_type(f"unknown persisted fields: {', '.join(sorted(unknown))}")
    if str(payload.get(version_field) or "") != expected_version:
        raise error_type(
            f"unsupported {version_field}: {payload.get(version_field)!r}; expected {expected_version!r}"
        )
    missing = allowed - set(payload)
    if missing:
        raise error_type(f"missing persisted fields: {', '.join(sorted(missing))}")


def _required_mapping(payload: Mapping[str, Any], key: str, error_type: type[ProfileError]) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise error_type(f"persisted {key} must be a mapping")
    return value


def _dataclass_shape(dataclass_type: type[Any]) -> dict[str, str]:
    hints = get_type_hints(dataclass_type)
    return {field.name: _annotation_kind(hints.get(field.name, field.type)) for field in fields(dataclass_type)}


def _annotation_kind(annotation: Any) -> str:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {list, tuple, Sequence}:
        return "array"
    if origin in {dict, Mapping}:
        return "object"
    if origin is not None and type(None) in args:
        return _annotation_kind(next(candidate for candidate in args if candidate is not type(None)))
    if annotation is bool:
        return "boolean"
    if annotation in {int, float}:
        return "number"
    return "string"


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value is not deterministically serializable: {type(value).__name__}")


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value)).strip().rstrip(".")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _raise_invalid_json_constant(token: str) -> Any:
    raise ValueError(f"invalid JSON constant: {token}")
