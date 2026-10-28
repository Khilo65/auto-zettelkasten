"""Opt-in, in-memory retrieval ablations over an explicitly supplied corpus.

This module performs no I/O, model inference, or relationship acceptance. Search
scores rank candidates; they are not probabilities or claims of corroboration.
The production recall route is deliberately not changed by this adapter.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from numbers import Real
import re
from typing import Any, Mapping, Sequence

from .hybrid_search import dense_ranking, lexical_ranking, note_words as _words, reciprocal_rank_fusion


VARIANTS = ("legacy", "lexical", "dense", "hybrid", "graph", "untyped", "typed")
_IDENTITY_FIELDS = (
    "model_assets", "tokenizer", "libraries", "cleaner_version",
    "chunking_version", "aggregation", "normalization", "dimension",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _string(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"invalid {name}")
    return value


def _positive(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def validate_vector_compatibility(actual: Mapping[str, Any], expected: Mapping[str, Any],
                                  source_hashes: Mapping[str, str]) -> None:
    """Require full encoder/processing identity, not merely equal dimensions.

    Callers normalize their frozen manifest into these fields. ``model_assets``,
    ``tokenizer`` and ``libraries`` may themselves be identity objects or hashes.
    A manifest supplied for a subset must bind exactly that subset's clean text.
    """
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        raise ValueError("vector identities must be mappings")
    for name in _IDENTITY_FIELDS:
        if name not in actual or name not in expected or actual[name] in (None, "", {}):
            raise ValueError(f"missing vector identity: {name}")
        if _json(actual[name]) != _json(expected[name]):
            raise ValueError(f"incompatible vector identity: {name}")
    _positive(actual["dimension"], "vector dimension", 4096)
    if actual["normalization"] != "l2":
        raise ValueError("comparison requires l2-normalized source vectors")
    if actual.get("source_hashes") != dict(source_hashes) or expected.get("source_hashes") != dict(source_hashes):
        raise ValueError("vector source hashes do not bind the supplied corpus")


class ComparisonIndex:
    """A frozen common-text index; query embeddings are supplied by the caller.

    Corpus rows require source_id, text, title and the SHA-256 of exact clean
    text. Optional edges use source_id/target_source_id (or source/target).
    Accepted edge labels are projected only for the explicit ``typed`` variant.
    """

    def __init__(self, corpus: Sequence[Mapping[str, Any]],
                 vectors: Mapping[str, Sequence[float]] | None = None, *,
                 vector_manifest: Mapping[str, Any] | None = None,
                 expected_vector_manifest: Mapping[str, Any] | None = None,
                 semantic_edges: Sequence[Mapping[str, Any]] = (),
                 reference_edges: Sequence[Mapping[str, Any]] = (),
                 accepted_edges: Sequence[Mapping[str, Any]] = ()) -> None:
        if len(corpus) > 100_000:
            raise ValueError("comparison corpus exceeds 100000 sources")
        self.rows: dict[str, dict[str, Any]] = {}
        self.words: dict[str, set[str]] = {}
        frequencies: Counter[str] = Counter()
        total_chars = 0
        for raw in corpus:
            if not isinstance(raw, Mapping):
                raise ValueError("corpus rows must be mappings")
            sid = _string(raw.get("source_id"), "source identity", 256)
            if sid in self.rows:
                raise ValueError("duplicate corpus source identity")
            text = _string(raw.get("text"), "source text", 2_000_000)
            title = _string(raw.get("title", sid), "title", 1000)
            if raw.get("content_sha256") != _sha(text):
                raise ValueError("stale or mismatched source text hash")
            coverage = json.loads(_json(raw.get("coverage", {"status": "unknown"})))
            if not isinstance(coverage, Mapping) or len(_json(coverage)) > 8000:
                raise ValueError("invalid source coverage")
            scope = str(coverage.get("source_scope", coverage.get("status", ""))).casefold().replace("-", "_")
            if scope in {"metadata", "metadata_only", "bibliographic_metadata"}:
                raise ValueError("metadata-only entries are not comparison evidence")
            total_chars += len(text)
            if total_chars > 100_000_000:
                raise ValueError("comparison text limit exceeded")
            self.rows[sid] = {"source_id": sid, "title": title, "text": text,
                              "content_sha256": raw["content_sha256"], "coverage": coverage}
            self.words[sid] = _words(title + " " + text)
            frequencies.update(self.words[sid])
        self.frequencies = frequencies
        self.source_ids = sorted(self.rows)
        self.corpus_sha256 = _sha(_json({sid: self.rows[sid]["content_sha256"] for sid in self.source_ids}))
        self.vector_identity_verified = False
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.dimension = 0
        if vectors is not None:
            if set(vectors) != set(self.rows):
                raise ValueError("vectors must cover exactly the comparison corpus")
            for sid in self.source_ids:
                vector = self._vector(vectors[sid])
                if self.dimension and len(vector) != self.dimension:
                    raise ValueError("source vector dimensions differ")
                self.dimension = len(vector)
                self.vectors[sid] = vector
        if (vector_manifest is None) != (expected_vector_manifest is None):
            raise ValueError("both actual and expected vector identities are required")
        if vector_manifest is not None:
            validate_vector_compatibility(vector_manifest, expected_vector_manifest,
                                          {sid: self.rows[sid]["content_sha256"] for sid in self.source_ids})
            if not self.vectors or vector_manifest["dimension"] != self.dimension:
                raise ValueError("vector manifest dimension does not match vectors")
            self.vector_identity_verified = True
        self.edges = {
            "semantic": self._edges(semantic_edges, "semantic"),
            "reference": self._edges(reference_edges, "reference"),
            "accepted": self._edges(accepted_edges, "accepted"),
        }

    @staticmethod
    def _vector(value: Sequence[float]) -> tuple[float, ...]:
        if isinstance(value, (str, bytes)) or not 1 <= len(value) <= 4096:
            raise ValueError("invalid vector dimension")
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in value):
            raise ValueError("vectors must contain finite real values")
        result = tuple(float(v) for v in value)
        if not math.isclose(math.sqrt(sum(v * v for v in result)), 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise ValueError("vectors must be l2-normalized")
        return result

    def _edges(self, rows: Sequence[Mapping[str, Any]], layer: str) -> dict[str, list[dict[str, Any]]]:
        if len(rows) > (500_000 if layer == "semantic" else 100_000):
            raise ValueError(f"{layer} comparison edge limit exceeded")
        adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ValueError("edge rows must be mappings")
            if raw.get("active", True) is not True or any(raw.get(key) for key in
                    ("refresh_pending", "reconciliation_pending", "legacy_review_pending")):
                continue
            if layer == "accepted" and (raw.get("decision_status") or raw.get("status")) != "accepted":
                continue
            if raw.get("freshness") in ("stale", "mismatched", "invalid"):
                continue
            left = raw.get("source_id", raw.get("source"))
            right = raw.get("target_source_id", raw.get("target"))
            if left not in self.rows or right not in self.rows or left == right:
                raise ValueError("edge endpoints must bind distinct supplied sources")
            for side, sid in (("source", left), ("target", right)):
                bound = raw.get(f"{side}_content_sha256")
                if bound is not None and bound != self.rows[sid]["content_sha256"]:
                    raise ValueError("stale edge source content hash")
            score = raw.get("score", raw.get("similarity", 0.0))
            if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score):
                raise ValueError("invalid comparison edge score")
            evidence = []
            for passage in raw.get("evidence", ()):
                if not isinstance(passage, Mapping) or passage.get("source_id") not in (left, right):
                    raise ValueError("edge evidence has an unrequested endpoint")
                sid, quote = passage["source_id"], passage.get("text")
                if not isinstance(quote, str) or not quote or len(quote) > 2000 or quote not in self.rows[sid]["text"]:
                    raise ValueError("edge evidence is not a source excerpt")
                if passage.get("content_sha256", self.rows[sid]["content_sha256"]) != self.rows[sid]["content_sha256"]:
                    raise ValueError("stale edge evidence hash")
                start = self.rows[sid]["text"].find(quote)
                evidence.append({"source_id": sid, "text": quote, "start": start,
                                 "end": start + len(quote), "content_sha256": self.rows[sid]["content_sha256"]})
            if len(evidence) > 4:
                raise ValueError("too many edge evidence passages")
            row = {"source_ids": sorted((left, right)), "layer": layer,
                   "evidence": sorted(evidence, key=_json), "score": float(score)}
            if layer == "accepted":
                relation_type = _string(raw.get("relation_type"), "accepted relation type", 100)
                row["label"] = {"relation_type": relation_type, "source_id": left, "target_source_id": right}
                row["freshness"] = "content_bound" if all(raw.get(f"{side}_content_sha256") for side in ("source", "target")) else "recorded_unverified"
            elif layer == "reference":
                row["reference_origin"] = _string(raw.get("reference_origin", "unspecified_reference_record"), "reference origin", 200)
            identity = _sha(_json(row))
            if identity in seen:
                continue
            seen.add(identity)
            row["edge_id"] = identity
            for sid in (left, right):
                adjacency[sid].append(row)
        return dict(adjacency)

    def recall(self, query: str, *, query_vector: Sequence[float] | None = None,
               variant: str = "hybrid", max_notes: int = 8, max_chars: int = 12_000,
               legacy_index: Any = None, legacy_relationships: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        _string(query, "query", 2000)
        _positive(max_notes, "max_notes", 128)
        _positive(max_chars, "max_chars", 1_000_000)
        if variant not in VARIANTS:
            raise ValueError("unknown comparison variant")
        if variant == "legacy":
            if legacy_index is None:
                raise ValueError("legacy variant requires the unchanged production index")
            result = legacy_index.recall(query, relationships=legacy_relationships,
                                         max_notes=max_notes, max_chars=max_chars)
            if any(note.get("source_id") not in self.rows for note in result.get("notes", ())):
                raise ValueError("legacy index returned a source outside the comparison corpus")
            return {**result, "variant": variant, "provenance": {"route": "unchanged_production_recall"}}
        query_words = _words(query)
        lexical = lexical_ranking(self.source_ids, self.words, self.frequencies, query_words)
        dense: list[str] = []
        if variant != "lexical":
            if not self.vectors or query_vector is None:
                raise ValueError("semantic comparison requires source and query vectors")
            q = self._vector(query_vector)
            if len(q) != self.dimension:
                raise ValueError("query vector dimension differs from source vectors")
            dense = dense_ranking(self.vectors, q)
        if variant == "lexical":
            ranked = lexical[:32]
        elif variant == "dense":
            ranked = dense[:32]
        else:
            ranked = [sid for sid, _ in reciprocal_rank_fusion((lexical[:32], dense[:32]))]
        route = {sid: "query" for sid in ranked}
        selected = ranked[:max_notes]
        used_edges: dict[str, dict[str, Any]] = {}
        if variant in {"graph", "untyped", "typed"}:
            seed_limit = max(1, max_notes - min(2, max_notes // 2))
            selected = ranked[:seed_limit]
            query_order = {sid: rank for rank, (sid, _) in enumerate(reciprocal_rank_fusion((lexical, dense)))}
            candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for seed in selected:
                for layer, limit in (("semantic", 4), ("reference", 2), ("accepted", 2)):
                    if layer == "accepted" and variant == "graph":
                        continue
                    def edge_key(edge):
                        other = next(sid for sid in edge["source_ids"] if sid != seed)
                        return (-edge["score"], other, edge["edge_id"]) if layer == "semantic" else (query_order.get(other, len(self.rows)), other, edge["edge_id"])
                    for edge in sorted(self.edges[layer].get(seed, ()), key=edge_key)[:limit]:
                        other = next(sid for sid in edge["source_ids"] if sid != seed)
                        if other not in selected:
                            candidates[other].append(edge)
            for sid in sorted(candidates, key=lambda sid: (query_order.get(sid, len(self.rows)), sid))[:max_notes - len(selected)]:
                selected.append(sid)
                route[sid] = "graph_neighbor"
                for edge in candidates[sid]:
                    used_edges[edge["edge_id"]] = edge
            selected.extend(sid for sid in ranked if sid not in selected and len(selected) < max_notes)
            # Also expose relevant edges between direct query seeds, without
            # adding topology or changing order in the label-only ablation.
            for sid in selected:
                for layer in (("semantic", "reference", "accepted") if variant != "graph" else ("semantic", "reference")):
                    for edge in self.edges[layer].get(sid, ()):
                        if set(edge["source_ids"]) <= set(selected):
                            used_edges[edge["edge_id"]] = edge
        bounded = len(ranked) > len(selected) or (variant != "dense" and len(lexical) > 32) or (variant != "lexical" and len(dense) > 32)
        return self._packet(query_words, selected, route, list(used_edges.values()), variant, max_chars, bounded=bounded)

    def _packet(self, query_words: set[str], selected: list[str], routes: Mapping[str, str],
                edges: list[dict[str, Any]], variant: str, max_chars: int, *, bounded: bool) -> dict[str, Any]:
        def project(edge, typed):
            row = json.loads(_json({key: edge[key] for key in ("edge_id", "source_ids", "layer", "evidence")}))
            if edge["layer"] == "accepted":
                row.update(label=dict(edge["label"]) if typed else None, freshness=edge["freshness"])
            elif edge["layer"] == "reference":
                row["reference_origin"] = edge["reference_origin"]
            return row
        starts: dict[str, int] = {}
        for sid in selected:
            text = self.rows[sid]["text"]
            hits = [m.start() for m in re.finditer(r"[^\W_]+", text) if m.group().casefold() in query_words]
            starts[sid] = max(0, min(hits) - 160) if hits else 0
        original_count = len(selected)
        def rows_for(cap):
            result = []
            for sid in selected:
                raw = self.rows[sid]
                start = starts[sid]
                text = raw["text"][start:start + cap]
                result.append({"source_id": sid, "title": raw["title"], "text": text,
                               "content_sha256": raw["content_sha256"], "coverage": json.loads(_json(raw["coverage"])),
                               "excerpt_start": start, "excerpt_end": start + len(text),
                               "recall_route": routes.get(sid, "query")})
            return result
        def projected(typed):
            return [project(edge, typed) for edge in sorted(edges, key=lambda edge: edge["edge_id"])
                    if set(edge["source_ids"]) <= set(selected)]
        def size(notes):
            # Reserve the more informative projection for BOTH paired variants.
            return len(_json({"notes": notes, "relationships": projected(True)}))
        while selected and size(rows_for(0)) > max_chars:
            selected.pop()
        if not selected and size([]) > max_chars:
            raise ValueError("max_chars cannot fit an empty evidence packet")
        low, high = 0, 2000
        while low < high:
            middle = (low + high + 1) // 2
            if size(rows_for(middle)) <= max_chars:
                low = middle
            else:
                high = middle - 1
        notes = rows_for(low)
        relationships = projected(variant == "typed")
        chars = len(_json({"notes": notes, "relationships": relationships}))
        return {"notes": notes, "relationships": relationships, "chars": chars,
                "truncated": bounded or len(selected) < original_count or any(row["excerpt_end"] < len(self.rows[row["source_id"]]["text"]) or row["excerpt_start"] > 0 for row in notes),
                "variant": variant, "provenance": {"route": "experimental_common_text",
                    "corpus_sha256": self.corpus_sha256, "vector_identity_verified": self.vector_identity_verified,
                    "candidate_limit_per_route": 32, "rrf_constant": 60,
                    "reserved_chars": size(notes), "scores_are_probabilities": False}}
