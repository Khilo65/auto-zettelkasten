"""Read-only Graphiti episode hydration for private retrieval comparisons.

An episode can retrieve an original note without verifying the generated fact
that led to it. No model/provider client is constructed or called by this module.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import re
from typing import Any

from .framework_comparison import ComparisonError, FRAMEWORKS, validate_corpus
from .retrieval import _STOP_WORDS


GRAPHITI_COMMIT = FRAMEWORKS["graphiti"].commit
EPISODE_API_SOURCE = f"https://github.com/getzep/graphiti/blob/{GRAPHITI_COMMIT}/graphiti_core/nodes.py"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ComparisonError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class EpisodeBinding:
    episode_uuid: str
    source_id: str
    content_sha256: str
    corpus_sha256: str
    group_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class EpisodeBindingRegistry:
    """Validate the corpus once, then bind actual successful ingestion results.

    Passing a new UUID to the pinned add_episode API requests an EXISTING episode,
    so callers capture result.episode.uuid instead of assuming a created UUID.
    """
    def __init__(self, corpus: Sequence[Mapping[str, Any]], *, corpus_sha256: str, group_id: str):
        rows, actual = validate_corpus(corpus)
        if actual != corpus_sha256:
            raise ComparisonError("episode registry corpus identity mismatch")
        self._rows = json.loads(_json(rows))
        self.corpus_sha256 = actual
        self.group_id = _id(group_id, "episode group")
        self._bindings: dict[str, EpisodeBinding] = {}

    @property
    def bindings(self) -> dict[str, EpisodeBinding]:
        return dict(self._bindings)

    def record_ingestion(self, result: Any, source_id: str) -> EpisodeBinding:
        _id(source_id, "ingestion source identity")
        if source_id not in self._rows:
            raise ComparisonError("ingestion source is outside the frozen corpus")
        episode = _get(result, "episode")
        uid = _id(_get(episode, "uuid"), "ingested episode identity")
        row = self._rows[source_id]
        source = _get(episode, "source")
        if getattr(source, "value", source) != "text" or _get(episode, "group_id") != self.group_id:
            raise ComparisonError("ingested episode type/group mismatch")
        content = _get(episode, "content")
        if content != row["text"] or _sha(content) != row["content_sha256"]:
            raise ComparisonError("ingested episode differs from the original note")
        metadata = _get(episode, "episode_metadata")
        if metadata is not None and (not isinstance(metadata, Mapping) or
             any(metadata.get(key, expected) != expected for key, expected in
                 (("source_id", source_id), ("content_sha256", row["content_sha256"]),
                  ("corpus_sha256", self.corpus_sha256)))):
            raise ComparisonError("ingested episode metadata mismatch")
        bound = EpisodeBinding(uid, source_id, row["content_sha256"], self.corpus_sha256, self.group_id)
        if uid in self._bindings and self._bindings[uid] != bound:
            raise ComparisonError("episode identity was rebound to another source")
        self._bindings[uid] = bound
        return bound


def bind_ingested_episodes(results: Sequence[Any], *, source_ids: Sequence[str],
                           corpus: Sequence[Mapping[str, Any]], corpus_sha256: str,
                           group_id: str) -> dict[str, EpisodeBinding]:
    """Commit a complete ingestion mapping only after every result validates.

    ``source_ids`` is the explicit input order used for the native add_episode
    calls. No mapping is returned on a missing, duplicate or mismatched result.
    """
    if not isinstance(results, (list, tuple)) or not isinstance(source_ids, (list, tuple)):
        raise ComparisonError("ingestion results and source order must be sequences")
    if any(not isinstance(row, Mapping) for row in corpus) or any(not isinstance(sid, str) for sid in source_ids):
        raise ComparisonError("invalid ingestion corpus/source identities")
    expected = {row.get("source_id") for row in corpus}
    if (len(results) != len(source_ids) or len(source_ids) != len(expected) or
            len(set(source_ids)) != len(source_ids) or set(source_ids) != expected):
        raise ComparisonError("ingestion results do not cover the exact corpus source order")
    registry = EpisodeBindingRegistry(corpus, corpus_sha256=corpus_sha256, group_id=group_id)
    for source_id, result in zip(source_ids, results):
        registry.record_ingestion(result, source_id)
    return registry.bindings


def research_search_kwargs(group_id: str, *, num_results: int = 32,
                           filters_factory: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Request no explicit temporal filters; this does not undo invalidation."""
    _id(group_id, "episode group")
    if type(num_results) is not int or not 1 <= num_results <= 128:
        raise ComparisonError("invalid Graphiti result limit")
    if filters_factory is None:
        from graphiti_core.search.search_filters import SearchFilters
        filters_factory = SearchFilters
    filters = filters_factory(valid_at=None, invalid_at=None, created_at=None, expired_at=None)
    if any(_get(filters, name) is not None for name in ("valid_at", "invalid_at", "created_at", "expired_at")):
        raise ComparisonError("research search must not silently add temporal filters")
    return {"group_ids": [group_id], "num_results": num_results, "search_filter": filters}


def make_episode_binding(episode_uuid: str, source_id: str, *, corpus: Sequence[Mapping[str, Any]],
                         corpus_sha256: str, group_id: str) -> EpisodeBinding:
    """Record an ingestion result for an episode containing EXACT note text.

    The caller persists this binding beside its experimental native index. An
    ingestion wrapper that changes episode content must not use this contract.
    """
    rows, actual = validate_corpus(corpus)
    _id(episode_uuid, "episode identity")
    _id(source_id, "source identity")
    _id(group_id, "episode group")
    if actual != corpus_sha256 or source_id not in rows:
        raise ComparisonError("episode binding does not match the frozen corpus")
    return EpisodeBinding(episode_uuid, source_id, rows[source_id]["content_sha256"], actual, group_id)


async def fetch_graphiti_episodes(driver: Any, uuids: list[str]) -> Sequence[Any]:
    """Use the pinned SDK's read-only node API, with no inference fallback.

    Upstream: EpisodicNode.get_by_uuids(driver, uuids), not EpisodeNode. Package
    version/commit verification is the caller's SDK-admission responsibility.
    """
    from graphiti_core.nodes import EpisodicNode
    return await EpisodicNode.get_by_uuids(driver, uuids)


def _binding(value: Any) -> EpisodeBinding:
    if isinstance(value, EpisodeBinding):
        result = value
    elif isinstance(value, Mapping):
        try:
            result = EpisodeBinding(**value)
        except TypeError as exc:
            raise ComparisonError("invalid episode binding fields") from exc
    else:
        raise ComparisonError("episode binding must be a frozen binding or mapping")
    for name in ("episode_uuid", "source_id", "group_id"):
        _id(getattr(result, name), name)
    for name in ("content_sha256", "corpus_sha256"):
        if not isinstance(getattr(result, name), str) or not re.fullmatch(r"[a-f0-9]{64}", getattr(result, name)):
            raise ComparisonError("invalid episode binding hash")
    return result


def _excerpt(text: str, query: str, length: int) -> tuple[int, str]:
    terms = {m.group().casefold() for m in re.finditer(r"[^\W_]+", query)
             if len(m.group()) > 1 and m.group().casefold() not in _STOP_WORDS}
    starts = [0]
    if terms:
        for match in re.finditer(r"[^\W_]+", text):
            if match.group().casefold() in terms:
                starts.append(max(0, match.start() - min(160, length // 4)))
                if len(starts) >= 129:
                    break
    def score(start):
        words = {m.group().casefold() for m in re.finditer(r"[^\W_]+", text[start:start + length])}
        return (-len(terms & words), start)
    start = min(set(starts), key=score)
    return start, text[start:start + length]


async def hydrate_graphiti_evidence(
    edges: Sequence[Any], *, driver: Any, bindings: Mapping[str, EpisodeBinding | Mapping[str, Any]],
    corpus: Sequence[Mapping[str, Any]], corpus_sha256: str, query: str = "",
    fetch_episodes: Callable[[Any, list[str]], Any] | None = None,
    max_notes: int = 8, max_chars: int = 12_000,
) -> dict[str, Any]:
    """Read episode content and return original-note evidence plus a fact audit.

    Every episode named by a hit must validate before ANY of that hit's evidence
    is admitted. Unrelated valid hits can survive a separately rejected hit.
    Audit text is outside the bounded agent evidence packet and must not be
    silently inserted into an agent's context by a consuming adapter.
    """
    rows, actual = validate_corpus(corpus)
    if corpus_sha256 != actual:
        raise ComparisonError("Graphiti corpus identity mismatch")
    if not isinstance(query, str) or len(query) > 2000 or "\x00" in query:
        raise ComparisonError("invalid grounding query")
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)) or len(edges) > 128:
        raise ComparisonError("Graphiti edges must be a bounded sequence")
    if not isinstance(bindings, Mapping) or len(bindings) > 100_000:
        raise ComparisonError("episode bindings must be a bounded mapping")
    if type(max_notes) is not int or not 1 <= max_notes <= 128 or type(max_chars) is not int or not 1 <= max_chars <= 1_000_000:
        raise ComparisonError("invalid grounding evidence budget")
    valid, audit, request_ids, edge_ids = [], [], set(), set()
    for edge in edges:
        identity = _id(_get(edge, "uuid"), "fact identity")
        if identity in edge_ids:
            raise ComparisonError("duplicate Graphiti fact identity")
        edge_ids.add(identity)
        fact = _get(edge, "fact", "")
        if not isinstance(fact, str) or len(fact) > 8192:
            raise ComparisonError("fact audit text exceeds its bound")
        episode_ids = _get(edge, "episodes", ())
        if not isinstance(episode_ids, (list, tuple)) or len(episode_ids) > 64 or any(not isinstance(v, str) for v in episode_ids):
            raise ComparisonError("invalid fact episode references")
        episode_ids = list(dict.fromkeys(episode_ids))
        record = {"native_id": identity, "generated_fact": fact, "episode_ids": episode_ids,
                  "fact_verification": "unknown", "original_publication_verified": False,
                  "included_source_ids": []}
        # Preserve temporal metadata for the audit. Never discard an older or
        # invalidated scholarly claim merely because a newer one was supplied.
        record["native_temporal_flags"] = {
            key: str(_get(edge, key))[:128] for key in ("valid_at", "invalid_at", "expired_at")
            if _get(edge, key) is not None}
        errors = []
        bound_rows = []
        if not episode_ids:
            errors.append("missing_episode_references")
        for episode_id in episode_ids:
            try:
                _id(episode_id, "episode identity")
                bound = _binding(bindings.get(episode_id))
                if bound.episode_uuid != episode_id or bound.corpus_sha256 != actual or bound.source_id not in rows:
                    raise ComparisonError("episode/source/corpus binding mismatch")
                if bound.content_sha256 != rows[bound.source_id]["content_sha256"]:
                    raise ComparisonError("stale episode source content")
                bound_rows.append(bound)
            except ComparisonError as exc:
                errors.append(str(exc))
        if errors:
            record.update(grounding_status="invalid_episode_binding", failures=errors)
        else:
            request_ids.update(episode_ids)
            valid.append((record, bound_rows))
        audit.append(record)
    if len(request_ids) > 512:
        raise ComparisonError("Graphiti episode hydration exceeds the read bound")
    hydrated = []
    if request_ids:
        value = (fetch_episodes or fetch_graphiti_episodes)(driver, sorted(request_ids))
        hydrated = await value if inspect.isawaitable(value) else value
    if not isinstance(hydrated, (list, tuple)) or len(hydrated) > 512:
        raise ComparisonError("episode lookup must return a bounded list")
    by_uuid = {}
    for episode in hydrated:
        uid = _id(_get(episode, "uuid"), "hydrated episode identity")
        if uid not in request_ids or uid in by_uuid:
            raise ComparisonError("episode lookup returned an unrequested or duplicate identity")
        by_uuid[uid] = episode
    candidates: dict[str, dict[str, Any]] = {}
    for record, bound_rows in valid:
        errors = []
        for bound in bound_rows:
            episode = by_uuid.get(bound.episode_uuid)
            content = _get(episode, "content")
            source = _get(episode, "source")
            source = getattr(source, "value", source)
            metadata = _get(episode, "episode_metadata")
            if episode is None:
                errors.append("missing_hydrated_episode")
            elif _get(episode, "group_id") != bound.group_id:
                errors.append("episode_group_mismatch")
            elif source != "text":
                errors.append("episode_source_is_not_plain_note_text")
            elif not isinstance(content, str) or _sha(content) != bound.content_sha256 or content != rows[bound.source_id]["text"]:
                errors.append("hydrated_content_mismatch")
            elif metadata is not None and (not isinstance(metadata, Mapping) or
                 any(metadata.get(key, expected) != expected for key, expected in
                     (("source_id", bound.source_id), ("content_sha256", bound.content_sha256), ("corpus_sha256", actual)))):
                errors.append("episode_metadata_mismatch")
        if errors:
            record.update(grounding_status="invalid_hydrated_episode", failures=errors)
            continue
        record["grounding_status"] = "original_note_available_fact_unverified"
        for bound in bound_rows:
            item = candidates.setdefault(bound.source_id, {"source_id": bound.source_id, "episode_uuids": [], "native_fact_ids": []})
            if bound.episode_uuid not in item["episode_uuids"]:
                item["episode_uuids"].append(bound.episode_uuid)
            item["native_fact_ids"].append(record["native_id"])
    def note_rows(length):
        result = []
        for sid, candidate in candidates.items():
            if len(result) >= max_notes:
                break
            row = rows[sid]
            start, excerpt = _excerpt(row["text"], query, length)
            result.append({**candidate, "title": row["title"], "excerpt": excerpt,
                           "excerpt_start": start, "excerpt_end": start + len(excerpt),
                           "content_sha256": row["content_sha256"], "coverage": row["coverage"],
                           "retrieval_route": "graphiti_episode", "grounding_status": "literal_note_evidence",
                           "source_retrieval_status": "original_note_retrieved", "fact_verification": "unknown",
                           "original_publication_verified": False})
        return result
    # Reserve metadata equally rather than clipping complete records into an
    # invalid or misleading JSON fragment.
    while candidates and len(_json(note_rows(1))) > max_chars:
        candidates.pop(next(reversed(candidates)))
    low, high = 1, 2000
    if not candidates:
        notes = []
    else:
        while low < high:
            middle = (low + high + 1) // 2
            if len(_json(note_rows(middle))) <= max_chars:
                low = middle
            else:
                high = middle - 1
        notes = note_rows(low)
    if len(_json(notes)) > max_chars:
        raise ComparisonError("evidence budget cannot fit an empty packet")
    included = {row["source_id"] for row in notes}
    for record, bound_rows in valid:
        if record["grounding_status"] == "original_note_available_fact_unverified":
            record["included_source_ids"] = sorted({bound.source_id for bound in bound_rows} & included)
    return {"framework": "graphiti", "corpus_sha256": actual, "notes": notes, "chars": len(_json(notes)),
            "native_records": audit,
            "grounding_failures": [record for record in audit if record["grounding_status"].startswith("invalid")],
            "truncated": len(included) < len({bound.source_id for record, bounds in valid
                                            if record["grounding_status"] == "original_note_available_fact_unverified"
                                            for bound in bounds}) or any(row["excerpt_start"] or row["excerpt_end"] < len(rows[row["source_id"]]["text"]) for row in notes),
            "provider_calls": 0, "database_read_batches": int(bool(request_ids)),
            "fact_verification": "unknown", "original_publication_verified": False,
            "native_temporal_claims_filtered": False,
            "native_query_temporal_filtering": "not_evaluated",
            "native_ingestion_invalidation": "not_undone_by_hydration",
            "audit_is_agent_evidence": False}
