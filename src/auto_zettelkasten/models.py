from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


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
    prompt_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        if not isinstance(self.allow_cloud, bool):
            raise ValueError("allow_cloud must be a boolean")
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
            prompt_version=str(payload.get("prompt_version", "1")),
        )


@dataclass(slots=True)
class ArtifactManifest:
    status: str
    workspace: Path | str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    created_at: str = ""
    engine_version: str = "0.2.0"
    artifact_schema_version: str = "1.1"
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
    exhausted_count: int = 0
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
    engine_version: str = "0.2.0"
    artifact_schema_version: str = "1.1"

    @property
    def terminal_count(self) -> int:
        return self.validated_note_count + self.exhausted_count

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
    engine_version: str = "0.2.0"
    artifact_schema_version: str = "1.1"

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


ExpansionScope = Literal["source_set", "cluster", "gap", "source"]
ExpansionState = Literal["proposed", "accepted", "parked", "rejected"]
ExpansionFulfillment = Literal["not_started", "mapped", "exhausted", "exported", "routed", "blocked"]


@dataclass(frozen=True, slots=True)
class ExpansionRequest:
    """Serializable request for one bounded literature-graph expansion run."""

    workspace: Path | str
    scope: ExpansionScope
    target_ids: tuple[str, ...] | Sequence[str]
    provider: Literal["internal", "semantic-scholar"] = "internal"
    depth: int = 1
    budget: int = 100
    allow_network: bool = False
    run_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser())
        if not isinstance(self.allow_network, bool):
            raise ValueError("allow_network must be a boolean")
        normalized_targets = tuple(sorted({str(value).strip() for value in self.target_ids if str(value).strip()}))
        object.__setattr__(self, "target_ids", normalized_targets)
        if self.scope not in {"source_set", "cluster", "gap", "source"}:
            raise ValueError("scope must be source_set, cluster, gap, or source")
        if not self.target_ids:
            raise ValueError("target_ids must contain at least one identifier")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) for value in self.target_ids):
            raise ValueError("target_ids must contain only opaque 1-128 character identifiers")
        if self.provider not in {"internal", "semantic-scholar"}:
            raise ValueError("provider must be internal or semantic-scholar")
        if self.depth not in {1, 2}:
            raise ValueError("depth must be 1 or 2")
        if not 1 <= self.budget <= 500:
            raise ValueError("budget must be between 1 and 500")
        if self.provider == "semantic-scholar" and not self.allow_network:
            raise ValueError("semantic-scholar requires explicit allow_network consent")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, allow_network: bool | None = None) -> ExpansionRequest:
        targets = payload.get("target_ids", ())
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            raise ValueError("target_ids must be a sequence")
        return cls(
            workspace=payload["workspace"],
            scope=str(payload["scope"]),  # type: ignore[arg-type]
            target_ids=tuple(str(value) for value in targets),
            provider=str(payload.get("provider", "internal")),  # type: ignore[arg-type]
            depth=int(payload.get("depth", 1)),
            budget=int(payload.get("budget", 100)),
            # Network consent is invocation-scoped and is never restored from a
            # persisted request. Callers must pass the current consent override.
            allow_network=False if allow_network is None else allow_network,
            run_id=str(payload.get("run_id") or "") or None,
        )


@dataclass(slots=True)
class ExpansionCandidate:
    work_id: str
    suggestion_id: str
    title: str = ""
    year: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    url: str = ""
    isbn: str = ""
    provider_ids: dict[str, str] = field(default_factory=dict)
    zotero_item_key: str = ""
    local_zotero_item: dict[str, Any] = field(default_factory=dict)
    target_scope: str = ""
    target_id: str = ""
    target_ids: list[str] = field(default_factory=list)
    primary_relation: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)
    related_source_ids: list[str] = field(default_factory=list)
    related_cluster_ids: list[str] = field(default_factory=list)
    related_gap_ids: list[str] = field(default_factory=list)
    ranking: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    depth: int = 1
    provider: str = "internal"
    actionability: str = "ready"
    state: ExpansionState = "proposed"
    fulfillment: ExpansionFulfillment = "not_started"
    decision_version: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"work-[A-Za-z0-9._-]{1,127}", self.work_id):
            raise ValueError("invalid expansion work_id")
        if not re.fullmatch(r"suggestion-[A-Za-z0-9._-]{1,127}", self.suggestion_id):
            raise ValueError("invalid expansion suggestion_id")
        if self.target_scope not in {"source_set", "cluster", "gap", "source"}:
            raise ValueError("invalid expansion target_scope")
        if not self.target_id or self.target_ids != [self.target_id]:
            raise ValueError("each expansion suggestion must belong to exactly one target_id")
        if self.state not in {"proposed", "accepted", "parked", "rejected"}:
            raise ValueError("invalid expansion state")
        if self.fulfillment not in {"not_started", "mapped", "exhausted", "exported", "routed", "blocked"}:
            raise ValueError("invalid expansion fulfillment")
        if self.decision_version < 0:
            raise ValueError("decision_version cannot be negative")
        if self.depth not in {1, 2}:
            raise ValueError("expansion candidate depth must be 1 or 2")
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("expansion candidate score must be between 0 and 1")
        if not self.primary_relation:
            raise ValueError("primary_relation cannot be empty")
        if self.actionability not in {"ready", "resolve_identity"}:
            raise ValueError("invalid expansion actionability")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExpansionCandidate:
        field_names = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in field_names})


@dataclass(frozen=True, slots=True)
class ExpansionDecision:
    suggestion_id: str
    expected_version: int
    decision: Literal["accepted", "parked", "rejected"]
    reason: str
    actor: str = "user"
    decided_at: str = ""

    def __post_init__(self) -> None:
        if not self.suggestion_id.strip():
            raise ValueError("suggestion_id cannot be empty")
        if self.expected_version < 0:
            raise ValueError("expected_version cannot be negative")
        if self.decision not in {"accepted", "parked", "rejected"}:
            raise ValueError("decision must be accepted, parked, or rejected")
        if not self.reason.strip():
            raise ValueError("decision reason cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(slots=True)
class ExpansionReport:
    status: str
    workspace: Path | str
    run_id: str
    scope: str = ""
    target_ids: list[str] = field(default_factory=list)
    provider: str = "internal"
    seed_count: int = 0
    candidate_count: int = 0
    proposed_count: int = 0
    accepted_count: int = 0
    parked_count: int = 0
    rejected_count: int = 0
    unresolved_count: int = 0
    existing_work_count: int = 0
    truncated: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    artifact_manifest: ArtifactManifest | None = None
    engine_version: str = "0.2.0"
    artifact_schema_version: str = "1.1"

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
