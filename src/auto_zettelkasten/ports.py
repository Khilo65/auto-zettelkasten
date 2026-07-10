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


class WorkspacePath(Protocol):
    def __fspath__(self) -> str: ...


PathLike = str | Path | WorkspacePath
