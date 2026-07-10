from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Mapping


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
    engine_version: str = "0.1.0"
    artifact_schema_version: str = "1.0"
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
    engine_version: str = "0.1.0"
    artifact_schema_version: str = "1.0"

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
    engine_version: str = "0.1.0"
    artifact_schema_version: str = "1.0"

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
