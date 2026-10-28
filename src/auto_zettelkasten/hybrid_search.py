"""Bounded full-note lexical and semantic retrieval without provider calls.

Callers supply cleaned substantive note text and any compatible local vectors.
This module neither reads files nor accepts research relationships. Similarity
and fusion scores rank evidence; they do not estimate relationship confidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from numbers import Real
import re
from typing import Any, Mapping, Sequence

from .retrieval import _STOP_WORDS


MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_CORPUS_BYTES = 128 * 1024 * 1024
MAX_CORPUS_SOURCES = 20_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _string(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def _positive(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def note_words(text: str) -> set[str]:
    """Tokenize all supplied text; there is no profile-sized token cutoff."""
    return {m.group() for m in re.finditer(r"[^\W_]+", text.casefold())
            if len(m.group()) > 1 and m.group() not in _STOP_WORDS}


def lexical_ranking(source_ids: Sequence[str], words: Mapping[str, set[str]],
                    frequencies: Mapping[str, int], query_words: set[str]) -> list[str]:
    scores = {sid: sum(1 + math.log((1 + len(source_ids)) / (1 + frequencies[word]))
                       for word in sorted(query_words & words[sid])) for sid in source_ids}
    return sorted((sid for sid in source_ids if scores[sid] > 0), key=lambda sid: (-scores[sid], sid))


def dense_ranking(vectors: Mapping[str, Sequence[float]], query_vector: Sequence[float]) -> list[str]:
    scores = {sid: sum(a * b for a, b in zip(query_vector, vector)) for sid, vector in vectors.items()}
    return sorted(vectors, key=lambda sid: (-scores[sid], sid))


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], *, constant: int = 60) -> list[tuple[str, float]]:
    _positive(constant, "RRF constant", 10_000)
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        if len(set(ranking)) != len(ranking):
            raise ValueError("ranking contains duplicate source identities")
        for rank, sid in enumerate(ranking, 1):
            scores[sid] += 1 / (constant + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def _vector(value: Sequence[float]) -> tuple[float, ...]:
    try:
        count = len(value)
    except TypeError as exc:
        raise ValueError("invalid vector dimension") from exc
    if isinstance(value, (str, bytes, Mapping)) or not 1 <= count <= 4096:
        raise ValueError("invalid vector dimension")
    if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in value):
        raise ValueError("vectors must contain finite real values")
    result = tuple(float(v) for v in value)
    if not math.isclose(math.sqrt(sum(v * v for v in result)), 1.0, rel_tol=1e-4, abs_tol=1e-4):
        raise ValueError("vectors must be l2-normalized")
    return result


class HybridSearchIndex:
    """Search full cleaned notes with top-32 lexical/dense routes and RRF 60.

    Vector identity and freshness must be checked by the workspace loader before
    supplying vectors. A subset is permitted so notes without current vectors
    remain searchable lexically. Source and query vectors share that checked
    space; dimensions and finite unit norms are also verified here.
    """

    def __init__(self, corpus: Sequence[Mapping[str, Any]],
                 vectors: Mapping[str, Sequence[float]] | None = None) -> None:
        if isinstance(corpus, (str, bytes)) or not isinstance(corpus, Sequence) or len(corpus) > MAX_CORPUS_SOURCES:
            raise ValueError("note corpus exceeds its source limit")
        self.rows: dict[str, dict[str, Any]] = {}
        self.words: dict[str, set[str]] = {}
        frequencies: Counter[str] = Counter()
        seen: set[str] = set()
        excluded: set[str] = set()
        total_bytes = 0
        for raw in corpus:
            if not isinstance(raw, Mapping):
                raise ValueError("corpus rows must be mappings")
            sid = _string(raw.get("source_id"), "source identity", 256)
            if sid in seen:
                raise ValueError("duplicate corpus source identity")
            seen.add(sid)
            coverage = json.loads(_json(raw.get("coverage", {"status": "unknown"})))
            if not isinstance(coverage, Mapping) or len(_json(coverage)) > 8000:
                raise ValueError("invalid source coverage")
            scope = str(coverage.get("source_scope", coverage.get("status", ""))).casefold().replace("-", "_")
            if scope in {"metadata", "metadata_only", "bibliographic_metadata"}:
                excluded.add(sid)
                continue
            text = _string(raw.get("text"), "source text", MAX_NOTE_BYTES)
            text_bytes = len(text.encode("utf-8"))
            if text_bytes > MAX_NOTE_BYTES:
                raise ValueError("note text byte limit exceeded")
            title = _string(raw.get("title", sid), "title", 1000)
            if raw.get("content_sha256") != _sha(text):
                raise ValueError("stale or mismatched source text hash")
            total_bytes += text_bytes
            if total_bytes > MAX_CORPUS_BYTES:
                raise ValueError("note corpus text byte limit exceeded")
            row = {"source_id": sid, "title": title, "text": text,
                   "content_sha256": raw["content_sha256"], "coverage": coverage}
            for name, limit in (("note_path", 4096), ("note_id", 256)):
                if raw.get(name) is not None:
                    row[name] = _string(raw[name], name, limit)
            self.rows[sid] = row
            self.words[sid] = note_words(title + " " + text)
            frequencies.update(self.words[sid])
        self.source_ids = sorted(self.rows)
        self.frequencies = frequencies
        self.corpus_sha256 = _sha(_json({sid: self.rows[sid]["content_sha256"] for sid in self.source_ids}))
        self.excluded_metadata_count = len(excluded)
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.dimension = 0
        if vectors is not None:
            if not isinstance(vectors, Mapping) or set(vectors) - seen:
                raise ValueError("vectors must bind supplied corpus sources")
            for sid in sorted(set(vectors) - excluded):
                vector = _vector(vectors[sid])
                if self.dimension and len(vector) != self.dimension:
                    raise ValueError("source vector dimensions differ")
                self.dimension = len(vector)
                self.vectors[sid] = vector

    def recall(self, query: str, *, query_vector: Sequence[float] | None = None,
               max_notes: int = 8, max_chars: int = 12_000) -> dict[str, Any]:
        _string(query, "query", 2000)
        _positive(max_notes, "max_notes", 32)
        _positive(max_chars, "max_chars", 64_000)
        query_words = note_words(query)
        lexical = lexical_ranking(self.source_ids, self.words, self.frequencies, query_words)
        dense: list[str] = []
        if query_vector is not None:
            q = _vector(query_vector)
            if self.vectors and len(q) != self.dimension:
                raise ValueError("query vector dimension differs from source vectors")
            if self.vectors:
                dense = dense_ranking(self.vectors, q)
        lexical_candidates, dense_candidates = lexical[:32], dense[:32]
        ranked = [sid for sid, _ in reciprocal_rank_fusion((lexical_candidates, dense_candidates))]
        selected = ranked[:max_notes]
        starts = {}
        for sid in selected:
            text = self.rows[sid]["text"]
            hits = [m.start() for m in re.finditer(r"[^\W_]+", text) if m.group().casefold() in query_words]
            starts[sid] = max(0, min(hits) - 160) if hits else 0
        original_count = len(selected)
        lexical_set, dense_set = set(lexical_candidates), set(dense_candidates)

        def rows_for(cap: int) -> list[dict[str, Any]]:
            result = []
            for sid in selected:
                raw, start = self.rows[sid], starts[sid]
                text = raw["text"][start:start + cap]
                route = "hybrid" if sid in lexical_set and sid in dense_set else "semantic" if sid in dense_set else "lexical"
                row = {"source_id": sid, "title": raw["title"], "text": text,
                       "content_sha256": raw["content_sha256"], "coverage": json.loads(_json(raw["coverage"])),
                       "excerpt_start": start, "excerpt_end": start + len(text), "recall_route": route}
                row.update({name: raw[name] for name in ("note_path", "note_id") if name in raw})
                result.append(row)
            return result

        def size(notes: list[dict[str, Any]]) -> int:
            return len(_json({"notes": notes, "relationships": []}))

        if size([]) > max_chars:
            raise ValueError("max_chars cannot fit an empty evidence packet")
        while selected and size(rows_for(1)) > max_chars:
            selected.pop()
        low, high = (1 if selected else 0), 2000
        while low < high:
            middle = (low + high + 1) // 2
            if size(rows_for(middle)) <= max_chars:
                low = middle
            else:
                high = middle - 1
        notes = rows_for(low)
        semantic = bool(dense)
        return {"notes": notes, "relationships": [], "chars": size(notes),
                "truncated": len(ranked) > original_count or len(lexical) > 32 or len(dense) > 32
                or len(selected) < original_count or any(row["excerpt_end"] < len(self.rows[row["source_id"]]["text"])
                                                       or row["excerpt_start"] > 0 for row in notes),
                "provenance": {"route": "full_note_hybrid" if semantic else "full_note_lexical",
                    "corpus_sha256": self.corpus_sha256, "candidate_limit_per_route": 32, "rrf_constant": 60,
                    "semantic_status": "available" if semantic else "query_vector_unavailable" if self.vectors else "no_compatible_vectors",
                    "source_count": len(self.rows), "vector_source_count": len(self.vectors),
                    "excluded_metadata_count": self.excluded_metadata_count,
                    "scores_are_probabilities": False}}
