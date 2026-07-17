from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import ClusterSynthesis, EvidenceProfile, LiteratureMapRequest


@runtime_checkable
class ZoteroClient(Protocol):
    """Read-only Zotero boundary. Implementations must not mutate Zotero."""

    def status(self) -> Mapping[str, Any]: ...

    def collections(self) -> list[dict[str, Any]]: ...

    def selected_collection(self) -> Mapping[str, Any]: ...

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]: ...

    def children(self, item_key: str) -> list[dict[str, Any]]: ...

    def fulltext(self, item_key: str) -> Mapping[str, Any] | None: ...

    def file(self, item_key: str) -> tuple[bytes, str] | None: ...


@runtime_checkable
class ReaderProvider(Protocol):
    name: str
    model: str
    is_cloud: bool

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class HierarchicalReaderProvider(ReaderProvider, Protocol):
    """Optional reader extension for documents that exceed a model context budget."""

    def summarize_chunk(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        chunk_id: str = "",
        locator: str = "",
        max_output_tokens: int | None = None,
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]: ...

    def synthesize_document(
        self,
        chunk_memos: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        question: str | None = None,
        *,
        max_output_tokens: int | None = None,
        deadline_seconds: float | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class LiteratureReasoner(Protocol):
    """Interpret source profiles and synthesize a bounded literature map."""

    name: str
    model: str
    is_cloud: bool

    def profile_source(
        self,
        note: Mapping[str, Any],
        *,
        question: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> EvidenceProfile | Mapping[str, Any]: ...

    def propose_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def map_debates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def detect_gaps(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ClusterSynthesisReasoner(Protocol):
    """Optional cluster-first narrative extension for literature reasoners."""

    def synthesize_cluster(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> ClusterSynthesis | Mapping[str, Any]: ...


@runtime_checkable
class ExternalDiscoveryProvider(Protocol):
    """Optional read-only source discovery boundary."""

    name: str
    is_cloud: bool

    def discover(
        self,
        query: str,
        *,
        context: Mapping[str, Any] | None = None,
        limit: int = 0,
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class VisionProvider(Protocol):
    name: str
    model: str
    is_cloud: bool

    def inspect_document(
        self,
        document: bytes,
        media_type: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ControllerPort(Protocol):
    """Controller gate for proposals that may alter canonical source memory."""

    def review_tag_proposals(
        self,
        proposals: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]: ...


class WorkspacePath(Protocol):
    def __fspath__(self) -> str: ...


PathLike = str | Path | WorkspacePath
