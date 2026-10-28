"""Pure, experimental semantic reranking of grounded community phrases.

This is KeyBERT-inspired ranking of a caller's lexical shortlist, not a new
clustering method or a complete KeyBERT implementation. The caller supplies
frozen compatible vectors. No models, network clients or filesystem are used.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import math
from numbers import Real
import unicodedata

from .community_labels import _digest, propose_community_labels


VERSION = "community-phrase-embedding-rerank-v1"
MAX_DIMENSIONS = 4096
MAX_VECTOR_ROWS = 40_000


def _unit(value):
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or not 1 <= len(value) <= MAX_DIMENSIONS
            or any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in value)):
        raise ValueError("vectors must contain bounded finite real values")
    values = [float(v) for v in value]
    # hypot avoids overflow for otherwise valid finite components.
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("vector norm must be finite and nonzero")
    return [v / norm for v in values]


def _cosine(left, right):
    return max(-1., min(1., math.fsum(a * b for a, b in zip(left, right))))


def _rows(values, key, expected_hashes, identity):
    if (not isinstance(values, Sequence) or isinstance(values, (str, bytes))
            or len(values) > MAX_VECTOR_ROWS):
        raise ValueError("vector row limit exceeded")
    rows, dimension, hashes = {}, None, {}
    for row in values:
        if not isinstance(row, Mapping):
            raise ValueError("vector rows must be objects")
        name = row.get(key)
        if not isinstance(name, str) or not name or len(name) > 256 or name in rows:
            raise ValueError("duplicate or invalid vector identity")
        if row.get("embedding_identity") != identity:
            raise ValueError("incompatible embedding identity")
        if name not in expected_hashes or row.get("content_sha256") != expected_hashes[name]:
            raise ValueError("unknown or stale vector evidence")
        vector = _unit(row.get("vector"))
        if dimension is not None and len(vector) != dimension:
            raise ValueError("incompatible vector dimensions")
        dimension = len(vector)
        rows[name] = vector
        hashes[name] = _digest(vector)
    return rows, dimension, hashes


def rerank_community_labels(
    communities, corpus, proposals, note_vectors, phrase_vectors, *,
    embedding_identity, contrast_weight=0., diversity=.3, top_n=5,
):
    """Compare semantic-only and sibling-contrast phrase rankings locally.

    Notes bind to exact cleaned ``content_sha256``; phrases bind to their exact
    UTF-8 text hash. All vectors must claim one embedding identity and dimension.
    That contract cannot independently establish which model created a vector.
    ``contrast_weight=0`` uses centroid fit alone. Positive weights penalize fit
    to the closest sibling centroid. MMR removes redundant ranked alternatives.
    No numerical similarity is interpreted as confidence or proof of MECE.
    """
    if (not isinstance(embedding_identity, str) or not embedding_identity.strip()
            or len(embedding_identity) > 512 or any(ord(c) < 32 for c in embedding_identity)):
        raise ValueError("a pinned embedding identity is required")
    for value in (contrast_weight, diversity):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("contrast weight and diversity must lie between zero and one")
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 8:
        raise ValueError("top_n must be an integer between one and eight")
    # Reuse the existing membership, body-hash and grounding contract. A copied
    # prepared artifact is not permission to introduce arbitrary title phrases.
    expected = propose_community_labels(communities, corpus)
    if proposals != expected:
        raise ValueError("phrase proposals do not match current grounded evidence")
    notes = {row["source_id"]: row for row in corpus}
    required_notes = {sid for row in expected["labels"].values() for sid in row["source_ids"]}
    required_phrases = {candidate["phrase"] for row in expected["labels"].values() for candidate in row["candidates"]}
    note_hashes = {sid: row["content_sha256"] for sid, row in notes.items()}
    phrase_hashes = {phrase: hashlib.sha256(phrase.encode("utf-8")).hexdigest() for phrase in required_phrases}
    note_units, note_dimension, note_vector_hashes = _rows(note_vectors, "source_id", note_hashes, embedding_identity)
    phrase_units, phrase_dimension, phrase_vector_hashes = _rows(phrase_vectors, "phrase", phrase_hashes, embedding_identity)
    if required_notes - note_units.keys() or required_phrases - phrase_units.keys():
        raise ValueError("required note or phrase vectors are missing")
    if phrase_dimension is not None and note_dimension != phrase_dimension:
        raise ValueError("note and phrase vector dimensions differ")
    centroids = {}
    for cid, row in expected["labels"].items():
        ids = row["source_ids"]
        centroids[cid] = _unit([math.fsum(note_units[sid][at] for sid in ids) / len(ids)
                                for at in range(note_dimension)])
    labels = {}
    for cid, row in sorted(expected["labels"].items()):
        scored = []
        for candidate in row["candidates"]:
            vector = phrase_units[candidate["phrase"]]
            own_fit = _cosine(vector, centroids[cid])
            competitors = sorted(((_cosine(vector, center), other)
                                  for other, center in centroids.items() if other != cid),
                                 key=lambda pair: (-pair[0], pair[1]))
            other_fit, neighbor = competitors[0] if competitors else (0., None)
            relevance = own_fit - float(contrast_weight) * max(0., other_fit)
            scored.append({**deepcopy(candidate), "semantic_fit": own_fit,
                           "nearest_sibling_fit": other_fit, "nearest_sibling_id": neighbor,
                           "relevance_score": relevance})
        ranked = []
        while scored and len(ranked) < top_n:
            for candidate in scored:
                redundancy = max((max(0., _cosine(phrase_units[candidate["phrase"]],
                                                  phrase_units[previous["phrase"]]))
                                  for previous in ranked), default=0.)
                candidate["mmr_score"] = ((1 - float(diversity)) * candidate["relevance_score"]
                                           - float(diversity) * redundancy) if ranked else candidate["relevance_score"]
            selected = min(scored, key=lambda candidate: (-candidate["mmr_score"], candidate["key"], candidate["phrase"]))
            ranked.append(selected)
            scored.remove(selected)
        chosen = ranked[0] if ranked else None
        low_coverage = bool(chosen and chosen["coverage"] < .5)
        mixed = bool(low_coverage and len(row["source_ids"]) >= 3)
        label = chosen["label"] if chosen else row["display_label"]
        labels[cid] = {"community_id": cid, "source_ids": list(row["source_ids"]),
                       "display_label": label, "status": "mixed_review_required" if mixed else "proposed" if chosen else "unresolved",
                       "selected_candidate": chosen, "candidates": ranked,
                       "diagnostics": {"low_literal_member_coverage": low_coverage,
                                       "mixed_content_warning": mixed,
                                       "mixed_content_is_lexical_heuristic": True,
                                       "semantic_exclusivity_certified": False}}
    duplicate_labels = {}
    for cid, row in labels.items():
        key = " ".join(unicodedata.normalize("NFKC", row["display_label"]).casefold().split())
        duplicate_labels.setdefault(key, []).append(cid)
    parameters = {"contrast_weight": float(contrast_weight), "diversity": float(diversity), "top_n": top_n}
    return {"version": VERSION, "labels": labels, "parameters": parameters,
            "identity": _digest({"version": VERSION, "proposal_identity": expected["input_identity"],
                                  "embedding_identity": embedding_identity, "parameters": parameters,
                                  "note_vectors": {sid: note_vector_hashes[sid] for sid in sorted(required_notes)},
                                  "phrase_vectors": phrase_vector_hashes}),
            "diagnostics": {"duplicate_label_groups": [ids for _, ids in sorted(duplicate_labels.items()) if len(ids) > 1],
                            "model_calls": 0, "membership_changed": False, "links_changed": False,
                            "candidate_pool": "existing_lexical_shortlist", "embedding_identity": embedding_identity,
                            "limitations": ["Centroids can obscure minority themes in mixed communities.",
                                            "Phrase selection is not a grammatical title generator or a proof of MECE.",
                                            "The caller must verify model and representation compatibility."]}}
