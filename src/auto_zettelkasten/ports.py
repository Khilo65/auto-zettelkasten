from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class ZoteroClient(Protocol):
    """Read-only Zotero boundary. Implementations must not mutate Zotero."""

    def status(self) -> Mapping[str, Any]: ...

    def collections(self) -> list[dict[str, Any]]: ...

    def selected_collection(self) -> Mapping[str, Any]: ...

    def inventory(self, scope: str, collection_key: str | None = None) -> list[dict[str, Any]]: ...

    def item(self, item_key: str) -> Mapping[str, Any] | None: ...

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


@runtime_checkable
class ScholarlyGraphProvider(Protocol):
    """Identifier/metadata-only graph boundary. Source text must never cross it."""

    name: str
    is_network: bool

    def resolve_work(self, work: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def citation_neighbors(
        self,
        work: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...

    def citation_neighbors_page(
        self,
        work: Mapping[str, Any],
        *,
        relation: str,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any]: ...

    def recommendations(
        self,
        positive_paper_ids: Sequence[str],
        *,
        negative_paper_ids: Sequence[str] = (),
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...

    def drain_attempts(self) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class ExpansionControllerPort(Protocol):
    """Controller gate for graph suggestions, separate from tag review."""

    def review_expansion_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]: ...


class WorkspacePath(Protocol):
    def __fspath__(self) -> str: ...


PathLike = str | Path | WorkspacePath
