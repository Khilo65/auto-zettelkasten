"""Bounded local discovery and recall over supplied compact records only.

Scores order a shortlist; they are not relationship probabilities. This module
does no file, provider, or Zotero I/O and never creates accepted relationships.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from itertools import islice, zip_longest
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


_STOP_WORDS = frozenset("a an and are as at be been by for from has have in into is it its of on or that the their this to was were with".split())
_ROLE_FIELDS = (
    "variable_roles", "independent_variables", "dependent_variables", "treatments",
    "controls", "control_variables", "mediators", "moderators", "covariates",
)
_MATCHED = {"matched", "matched_identifier", "matched_metadata", "exact_identifier", "identifier_match", "matched_title_author_year"}
_PAIR_PROVENANCE_MAX_BYTES = 2048
_PAIR_PROVENANCE_MAX_ROWS = 6


class CandidateResourceError(ValueError):
    """Discovery stopped at an explicit bound without discarding saved work."""


def _limit(value: int, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


def _text(value: Any, limit: int = 1000) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return re.sub(r"\s+", " ", str(value)[:limit]).strip()


def _compact(value: Any, limit: int = 1000, depth: int = 0) -> Any:
    if depth >= 4:
        return _text(value, limit)
    if isinstance(value, Mapping):
        if len(value) > 32:
            raise ValueError("compact input mapping exceeds 32 fields")
        keys = sorted(value, key=str)
        return {str(k): _compact(value[k], max(1, limit // max(1, len(keys))), depth + 1) for k in keys}
    if isinstance(value, (list, tuple)):
        return [_compact(v, max(1, limit // max(1, min(16, len(value)))), depth + 1) for v in value[:16]]
    return _text(value, limit)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _words(value: Any, max_chars: int, max_tokens: int) -> set[str]:
    text = value if isinstance(value, str) else _json(value)
    return set(islice((m.group() for m in re.finditer(r"[^\W_]+", text[:max_chars].casefold())
                      if len(m.group()) > 1 and m.group() not in _STOP_WORDS), max_tokens))


def _identity(value: Any) -> tuple[str, str, str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("discovery identity must include instance, library and item")
    fields = tuple(_text(value.get(k), 256) for k in ("instance_id", "library_type", "library_id", "item_key"))
    if not all(fields):
        raise ValueError("discovery identity must include instance, library and item")
    return (fields[0], fields[1].removesuffix("s"), fields[2], fields[3])


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else []


def _values(value: Any, depth: int = 0) -> str:
    if depth >= 4:
        return _text(value)
    if isinstance(value, Mapping):
        return " ".join(_values(v, depth + 1) for v in islice(value.values(), 32))
    if isinstance(value, (list, tuple)):
        return " ".join(_values(v, depth + 1) for v in value[:16])
    return _text(value)


def _pair_provenance(evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep auditable samples; hash all material without repeating bibliographies."""
    if not evidence:
        return []
    digest = hashlib.sha256()
    counts = Counter(row["signal"] for row in evidence)
    for row in evidence:
        encoded = _json(row).encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii") + b":" + encoded)
    summary = {"signal": "material_evidence_summary", "material_sha256": digest.hexdigest(), "signal_counts": dict(sorted(counts.items())), "sampled_signal_count": 0, "omitted_signal_count": len(evidence)}
    samples: list[dict[str, Any]] = []
    priority = {"direct_reference": 0, "semantic_similarity": 0, "source_coverage": 1, "shared_reference": 2, "zotero_related": 3, "shared_terms": 4, "shared_tag": 5}
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in evidence:
        groups[row["signal"]].append(row)
    kinds = sorted(groups, key=lambda kind: (priority.get(kind, 6), kind))
    ordered = [groups[kind][0] for kind in kinds] + [row for kind in kinds for row in groups[kind][1:]]
    for raw in ordered:
        signal = raw["signal"]
        if signal == "direct_reference":
            row = {key: raw[key] for key in ("signal", "citing_source_id", "cited_source_id", "work_id", "match_status", "locator", "source_identity", "target_identity", "literature_position_id", "freshness", "reference_origin", "match_basis") if key in raw}
        elif signal == "shared_reference":
            row = {"signal": signal, "work_id": raw["work_id"]}
            for side in ("left", "right"):
                reference = raw[f"{side}_reference"]
                row[f"{side}_reference"] = {key: reference[key] for key in ("locator", "source_identity") if key in reference}
        elif signal == "source_coverage":
            row = {"signal": signal, "sources": [{**{key: source[key] for key in ("source_id", "source_scope", "evidence_eligibility")}, "reference_statuses": sorted({coverage.get("status", "unknown") for coverage in source["reference_coverage"]})} for source in raw["sources"]]}
        elif signal == "shared_terms":
            row = {"signal": signal, "terms": raw["terms"][:8]}
        else:
            row = dict(raw)
        proposed = [*samples, row]
        summary.update(sampled_signal_count=len(proposed), omitted_signal_count=len(evidence) - len(proposed))
        if len(_json([summary, *proposed]).encode("utf-8")) <= _PAIR_PROVENANCE_MAX_BYTES:
            samples.append(row)
        if len(samples) >= _PAIR_PROVENANCE_MAX_ROWS:
            break
    summary.update(sampled_signal_count=len(samples), omitted_signal_count=len(evidence) - len(samples))
    return [summary, *samples]


class LocalRetrievalIndex:
    """Index lean_discovery_projection rows, optionally enriched by scanner rows.

    Source IDs must already be bound to the catalogue. Scanner identities are
    checked as full instance/library/item tuples, never as global item keys.
    Only catalogue-declared item aliases can share a source within that scope.
    """

    def __init__(
        self,
        projection_rows: Sequence[Mapping[str, Any]],
        discovery_records: Sequence[Mapping[str, Any]] = (),
        *,
        citation_records: Sequence[Mapping[str, Any]] = (),
        ignored_tags: Sequence[str] = (),
        max_posting_size: int = 64,
        max_source_chars: int = 16_000,
        max_tokens_per_source: int = 512,
        max_signals_per_source: int = 64,
        max_discovery_signals_per_source: int = 256,
        max_discovery_records_per_source: int = 4,
        max_sources: int = 100_000,
    ) -> None:
        for name, value in locals().copy().items():
            if name.startswith("max_"):
                _limit(value, name)
        if len(projection_rows) > max_sources or len(discovery_records) > max_sources * max_discovery_records_per_source:
            raise ValueError("local index input record limit exceeded")
        self.max_source_chars = max_source_chars
        self.max_tokens_per_source = max_tokens_per_source
        self.max_signals_per_source = max_signals_per_source
        self.max_discovery_signals_per_source = max_discovery_signals_per_source
        self.rows: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, tuple[str, str, str, str]] = {}
        self.references: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.tags: dict[str, set[str]] = defaultdict(set)
        self.direct: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.direct_targets: dict[str, set[str]] = defaultdict(set)
        self.coverage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.last_candidate_stats: dict[str, int] = {}
        self._ignored_tags = {_text(tag).casefold() for tag in ignored_tags}
        for raw in projection_rows:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("source_id"), str) or not raw["source_id"].strip():
                raise ValueError("projection source_id is required")
            sid = raw["source_id"].strip()
            if len(sid) > 256:
                raise ValueError("source_id exceeds 256 characters")
            row = {key: _text(raw.get(key), max_source_chars if key == "thesis" else 1000)
                   for key in ("title", "author", "year", "thesis", "method", "source_scope", "evidence_eligibility", "note_id", "note_hash", "profile_hash", "note_path", "zotero_key")}
            row["source_scope"] = row["source_scope"] or "unknown"
            row["evidence_eligibility"] = row["evidence_eligibility"] or "unavailable"
            row["source_id"] = sid
            aliases = raw.get("zotero_item_keys") or []
            if not isinstance(aliases, (list, tuple)) or len(aliases) > 64 or any(not isinstance(key, str) or len(key) > 256 for key in aliases):
                raise ValueError("catalogue aliases must contain at most 64 item keys")
            row["zotero_item_keys"] = sorted({key.strip() for key in aliases if key.strip()} | ({row["zotero_key"]} if row["zotero_key"] else set()))
            row["collection_keys"] = sorted({_text(v, 256) for v in _items(raw.get("collection_keys"))[:64] if _text(v, 256)})
            row["facets"] = _compact(raw.get("facets") or {}, max_source_chars)
            tags = raw.get("tags") or []
            if not isinstance(tags, (list, tuple)) or len(tags) > max_discovery_signals_per_source or any(not isinstance(value, str) for value in tags):
                raise ValueError("source tags must be a bounded text sequence")
            row["tags"] = sorted({_text(value).casefold() for value in tags if _text(value) and _text(value).casefold() not in self._ignored_tags})
            for key in (*_ROLE_FIELDS, "source_references", "evidence_references", "coverage"):
                if raw.get(key):
                    row[key] = _compact(raw[key], max_source_chars)
            if sid in self.rows and self.rows[sid] != row:
                raise ValueError(f"conflicting projection rows for source_id {sid}")
            self.rows[sid] = row
            self.tags[sid].update(row["tags"])
            if raw.get("identity"):
                identity = _identity(raw["identity"])
                if sid in self.identities and self.identities[sid] != identity:
                    raise ValueError(f"conflicting projection identity for {sid}")
                self.identities[sid] = identity
        by_identity: dict[tuple[str, str, str, str], str] = {}
        for sid, identity in list(self.identities.items()):
            self._bind_identity(sid, identity, by_identity)
        record_counts: Counter[str] = Counter()
        for record in discovery_records:
            if not isinstance(record, Mapping) or not isinstance(record.get("source_id"), str) or record["source_id"] not in self.rows:
                raise ValueError("discovery record requires an exact catalogue source_id")
            sid = record["source_id"]
            record_counts[sid] += 1
            if record_counts[sid] > max_discovery_records_per_source:
                raise ValueError(f"discovery record limit exceeded for {sid}")
            identity = _identity(record.get("identity"))
            self._bind_identity(sid, identity, by_identity)
            if record.get("note_id") and self.rows[sid]["note_id"] and _text(record["note_id"], 256) != self.rows[sid]["note_id"]:
                raise ValueError(f"discovery note identity conflicts with projection for {sid}")
        for record in sorted(discovery_records, key=lambda r: (r["source_id"], _text(r.get("input_fingerprint")), _json(_compact(r, max_source_chars)))):
            self._add_discovery(record, by_identity)
        if (not isinstance(citation_records, (list, tuple))
                or len(citation_records) > max(1, len(self.rows)) * max_discovery_signals_per_source):
            raise ValueError("citation memory records must be a bounded sequence")
        citation_material = []
        for record in citation_records:
            if not isinstance(record, Mapping):
                raise ValueError("citation memory record must be a mapping")
            source, target = record.get("source_id"), record.get("target_source_id")
            if (not isinstance(source, str) or not isinstance(target, str)
                    or source not in self.rows or target not in self.rows or source == target):
                raise ValueError("citation memory requires distinct current catalogue source IDs")
            material = {key: _text(record.get(key), 1000) for key in ("literature_position_id", "match_status", "locator", "freshness", "reference_origin", "match_basis")}
            material.update(signal="direct_reference", citing_source_id=source, cited_source_id=target,
                            work_id="source:" + target)
            citation_material.append(material)
        for material in sorted(citation_material, key=_json):
            source, target = material["citing_source_id"], material["cited_source_id"]
            self._add_direct(source, target, material)
            if len(self.references[source]) < max_discovery_signals_per_source:
                self.references[source].setdefault(material["work_id"], {key: value for key, value in material.items() if key != "signal"})
        self.words: dict[str, set[str]] = {}
        postings: dict[tuple[str, str], list[str]] = defaultdict(list)
        for sid, row in sorted(self.rows.items()):
            role_text = " ".join(key.replace("_", " ") + " " + _json(row[key]) for key in _ROLE_FIELDS if row.get(key))
            fields = [role_text, row["title"], _values(row["facets"]), row["thesis"], row["method"], row["author"]]
            searchable = " ".join(value[:max(1, max_source_chars // len(fields))] for value in fields)
            self.words[sid] = _words(searchable, max_source_chars, max_tokens_per_source)
            for kind, signals in (("term", self.words[sid]), ("tag", self.tags[sid]), ("reference", self.references[sid])):
                for signal in sorted(signals):
                    postings[(kind, signal)].append(sid)
        self.frequencies = {signal: len(ids) for signal, ids in postings.items()}
        self.postings = {signal: self._diverse(ids, max_posting_size) for signal, ids in postings.items()}
        self.collection_sizes = Counter(key for row in self.rows.values() for key in row["collection_keys"])

    def _bind_identity(self, sid: str, identity: tuple[str, str, str, str], by_identity: dict[tuple[str, str, str, str], str]) -> None:
        row = self.rows[sid]
        allowed = row["zotero_item_keys"]
        if allowed and identity[-1] not in allowed:
            raise ValueError(f"discovery item key conflicts with projection for {sid}")
        prior = self.identities.get(sid)
        if prior and (prior[:3] != identity[:3] or (prior[-1] != identity[-1] and identity[-1] not in allowed)):
            raise ValueError(f"conflicting discovery identity for source_id {sid}")
        canonical_key = row["zotero_key"] or (prior[-1] if prior else identity[-1])
        self.identities[sid] = (*identity[:3], canonical_key)
        if not allowed:
            row["zotero_item_keys"] = allowed = [canonical_key]
        for key in allowed:
            qualified = (*identity[:3], key)
            if qualified in by_identity and by_identity[qualified] != sid:
                raise ValueError("one discovery identity is bound to multiple source IDs")
            by_identity[qualified] = sid

    def _add_discovery(self, record: Mapping[str, Any], by_identity: Mapping[tuple[str, str, str, str], str]) -> None:
        sid = record["source_id"]
        identity = _identity(record["identity"])
        identity_fields = ("instance_id", "library_type", "library_id", "item_key")
        source_identity = dict(zip(identity_fields, identity))
        if record.get("coverage") is not None and not isinstance(record["coverage"], Mapping):
            raise ValueError("discovery coverage must be a mapping")
        coverage = _compact(record.get("coverage") or {"status": "not_examined"}, 2000)
        coverage["identity"] = source_identity
        if coverage not in self.coverage[sid]:
            self.coverage[sid].append(coverage)
        if record.get("note_id"):
            note_id = _text(record["note_id"], 256)
            if self.rows[sid]["note_id"] and self.rows[sid]["note_id"] != note_id:
                raise ValueError(f"discovery note identity conflicts for {sid}")
            self.rows[sid]["note_id"] = note_id
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("discovery metadata must be a mapping")
        for value in _items(metadata.get("tags"))[:self.max_discovery_signals_per_source]:
            tag = _text(value.get("tag") if isinstance(value, Mapping) else value).casefold()
            if tag and tag not in self._ignored_tags and len(self.tags[sid]) < self.max_discovery_signals_per_source:
                self.tags[sid].add(tag)
        matched_target_count = 0
        for ref in _items(record.get("references"))[:self.max_discovery_signals_per_source]:
            if not isinstance(ref, Mapping) or not isinstance(ref.get("work_id"), str) or not ref["work_id"].strip():
                continue
            work_id = _text(ref["work_id"], 512)
            compact = {key: _compact(ref[key], 1000) for key in ("work_id", "identifiers", "title", "author", "year", "match_status", "locator") if ref.get(key)}
            compact["work_id"] = work_id
            compact["source_identity"] = source_identity
            if work_id not in self.references[sid] and len(self.references[sid]) < self.max_discovery_signals_per_source:
                self.references[sid][work_id] = compact
            if ref.get("match_status") in _MATCHED:
                for key in _items(ref.get("matched_item_keys"))[:self.max_discovery_signals_per_source]:
                    if matched_target_count >= self.max_discovery_signals_per_source:
                        break
                    matched_target_count += 1
                    target_identity = (*identity[:3], _text(key, 256))
                    target = by_identity.get(target_identity)
                    if target and target != sid:
                        self._add_direct(sid, target, {"signal": "direct_reference", "citing_source_id": sid, "cited_source_id": target, "target_identity": dict(zip(identity_fields, target_identity)), **compact})
        relations = metadata.get("relations") or {}
        relation_count = 0
        if isinstance(relations, Mapping):
            for predicate in islice(sorted(relations), self.max_discovery_signals_per_source):
                if predicate != "dc:relation":
                    continue
                values = relations[predicate] if isinstance(relations[predicate], list) else [relations[predicate]]
                for value in values[:self.max_discovery_signals_per_source]:
                    relation_count += 1
                    if relation_count > self.max_discovery_signals_per_source:
                        break
                    target_identity = (*identity[:3], _text(value, 256))
                    if isinstance(value, str) and "://" in value:
                        parsed = urlparse(value)
                        parts = parsed.path.strip("/").split("/")
                        if parsed.hostname not in {"zotero.org", "www.zotero.org"} or len(parts) != 4 or parts[0] not in {"users", "groups"} or parts[2] != "items":
                            continue
                        target_identity = (identity[0], parts[0].removesuffix("s"), parts[1], parts[3])
                    target = by_identity.get(target_identity)
                    if target and target != sid:
                        self._add_direct(sid, target, {"signal": "zotero_related", "source_id": sid, "target_source_id": target, "predicate": _text(predicate, 256), "source_identity": source_identity, "target_identity": dict(zip(identity_fields, target_identity))})
                if relation_count >= self.max_discovery_signals_per_source:
                    break

    def _add_direct(self, source: str, target: str, evidence: dict[str, Any]) -> None:
        key = tuple(sorted((source, target)))
        # Bound relation hubs while ingesting, before any candidate expansion.
        if target not in self.direct_targets[source] and len(self.direct_targets[source]) >= self.max_discovery_signals_per_source:
            return
        if len(self.direct[key]) < 8 and evidence not in self.direct[key]:
            self.direct[key].append(evidence)
        for left, right in ((source, target), (target, source)):
            if len(self.direct_targets[left]) < self.max_discovery_signals_per_source:
                self.direct_targets[left].add(right)

    def _diverse(self, ids: Sequence[str], limit: int) -> list[str]:
        groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for sid in sorted(ids):
            groups[tuple(self.rows[sid]["collection_keys"])].append(sid)
        streams = deque(iter(groups[key]) for key in sorted(groups))
        result = []
        while streams and len(result) < limit:
            stream = streams.popleft()
            sid = next(stream, None)
            if sid is not None:
                result.append(sid)
                streams.append(stream)
        return result

    def _signals(self, sid: str) -> list[tuple[str, str]]:
        signals = [(kind, value) for kind, values in (("term", self.words[sid]), ("reference", self.references[sid]), ("tag", self.tags[sid])) for value in values]
        return sorted(signals, key=lambda s: (self.frequencies[s], s))[:self.max_signals_per_source]

    def _targets(self, signals: Sequence[tuple[str, str]], fanout: int, sid: str = "", *, excluded_targets: set[str] | frozenset[str] = frozenset()) -> list[str]:
        streams = [self.postings.get(signal, []) for signal in signals]
        if sid:
            collections = set(self.rows[sid]["collection_keys"])
            streams = [sorted(ids, key=lambda target: (bool(collections & set(self.rows[target]["collection_keys"])), target)) for ids in streams]
            direct = sorted(self.direct_targets[sid] - excluded_targets)[:fanout]
            streams.insert(0, direct)
        targets: dict[str, None] = {}
        for row in zip_longest(*streams):
            for target in row:
                if target is not None and target != sid and target not in excluded_targets:
                    targets[target] = None
                    if len(targets) >= fanout:
                        return list(targets)
        return list(targets)

    def _shared(self, left: str, right: str) -> list[tuple[str, list[str]]]:
        return [(kind, sorted(shared)) for kind, shared in (("term", self.words[left] & self.words[right]), ("reference", self.references[left].keys() & self.references[right].keys()), ("tag", self.tags[left] & self.tags[right]))]

    def _score(self, left: str, right: str) -> float:
        return round(8.0 * len(self.direct.get((left, right), [])) + sum(
            {"term": 1.0, "reference": 4.0, "tag": 2.0}[kind] * (1 + math.log((1 + len(self.rows)) / (1 + self.frequencies[(kind, value)])))
            for kind, shared in self._shared(left, right) for value in shared
        ), 6)

    def _pair_material(self, left: str, right: str) -> list[dict[str, Any]]:
        evidence = sorted(self.direct.get((left, right), []), key=_json)
        for kind, ordered in self._shared(left, right):
            if kind == "term" and ordered:
                evidence.append({"signal": "shared_terms", "terms": sorted(ordered)})
            elif kind == "tag":
                evidence.extend({"signal": "shared_tag", "tag": value} for value in ordered)
            elif kind == "reference":
                evidence.extend({"signal": "shared_reference", "work_id": value, "left_reference": self.references[left][value], "right_reference": self.references[right][value]} for value in ordered)
        if evidence:
            evidence.append({"signal": "source_coverage", "sources": [{"source_id": sid, "source_scope": self.rows[sid]["source_scope"], "evidence_eligibility": self.rows[sid]["evidence_eligibility"], "reference_coverage": sorted(self.coverage[sid], key=_json) or [{"status": "not_examined"}]} for sid in (left, right)]})
        return evidence

    def _pair(self, left: str, right: str) -> dict[str, Any]:
        evidence = self._pair_material(left, right)
        kinds = sorted({row["signal"] for row in evidence if row["signal"] != "source_coverage"})
        row = {"left_source_id": left, "right_source_id": right, "left_source_title": self.rows[left]["title"], "right_source_title": self.rows[right]["title"], "score": self._score(left, right), "why_compare": "; ".join(kinds), "discovery_route": "deterministic_local_ranked", "discovery_provenance": _pair_provenance(evidence)}
        if self.rows[left]["profile_hash"] and self.rows[right]["profile_hash"]:
            row["input_profile_hashes"] = {sid: self.rows[sid]["profile_hash"] for sid in (left, right)}
        return row

    def candidate_pairs(self, *, per_source_cap: int = 8, max_pairs: int = 256, fanout: int = 32, source_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Return globally deduplicated pairs with hard caps on both endpoints."""
        _limit(per_source_cap, "per_source_cap", zero=True)
        _limit(max_pairs, "max_pairs", zero=True)
        _limit(fanout, "fanout")
        sources = sorted(self.rows if source_ids is None else set(source_ids))
        if any(sid not in self.rows for sid in sources):
            raise ValueError("candidate source_ids must belong to the index")
        self.last_candidate_stats = {"scored_pairs": 0, "max_source_fanout": 0, "returned_pairs": 0}
        if not per_source_cap or not max_pairs:
            return []
        proposals: dict[str, list[tuple[str, str]]] = {}
        pairs: dict[tuple[str, str], float] = {}
        for sid in sources:
            targets = self._targets(self._signals(sid), fanout, sid)
            self.last_candidate_stats["max_source_fanout"] = max(self.last_candidate_stats["max_source_fanout"], len(targets))
            keys = [tuple(sorted((sid, target))) for target in targets]
            for key in keys:
                if key not in pairs:
                    pairs[key] = self._score(*key)
            proposals[sid] = sorted((key for key in keys if pairs[key] > 0), key=lambda key: (bool(set(self.rows[key[0]]["collection_keys"]) & set(self.rows[key[1]]["collection_keys"])), -pairs[key], key))
        counts: Counter[str] = Counter()
        selected: set[tuple[str, str]] = set()
        # Round-robin gives sparse sources a route independent of map families.
        sources.sort(key=lambda sid: (min((self.collection_sizes[k] for k in self.rows[sid]["collection_keys"]), default=0), sid))
        for _ in range(min(per_source_cap, len(self.rows))):
            before = len(selected)
            for sid in sources:
                if counts[sid] >= per_source_cap:
                    continue
                for key in proposals[sid]:
                    if key not in selected and all(counts[endpoint] < per_source_cap for endpoint in key):
                        selected.add(key)
                        counts.update(key)
                        break
                if len(selected) >= max_pairs:
                    break
            if len(selected) >= max_pairs or len(selected) == before:
                break
        result = sorted((self._pair(*key) for key in selected), key=lambda row: (-row["score"], row["left_source_id"], row["right_source_id"]))
        for rank, row in enumerate(result, 1):
            row["rank"] = rank
        self.last_candidate_stats.update(scored_pairs=len(pairs), returned_pairs=len(result))
        return result

    def fused_candidate_pairs(
        self, semantic_neighbors: Sequence[Mapping[str, Any]], *, per_source_cap: int = 8,
        max_pairs: int = 256, fanout: int = 32, source_ids: Sequence[str] | None = None,
        resolved_pairs: Sequence[tuple[str, str]] = (), retain_pool: bool = False,
    ) -> list[dict[str, Any]]:
        """Alternate semantic and local rankings, then select bounded fair pairs.

        Communities are deliberately absent: a bridge across communities remains
        eligible. Caller-supplied resolved pairs must already be freshness-checked.
        Neither these scores nor selection establishes a substantive relationship.
        """
        for name, value in (("per_source_cap", per_source_cap), ("max_pairs", max_pairs), ("fanout", fanout)):
            _limit(value, name, zero=name != "fanout")
        if fanout > 256 or per_source_cap > 256 or max_pairs > 100_000:
            raise ValueError("fused candidate limits exceed their supported bounds")
        if (not isinstance(semantic_neighbors, (list, tuple))
                or len(semantic_neighbors) > max(1, len(self.rows)) * 256):
            raise ValueError("semantic neighbors must be a bounded sequence")
        if source_ids is not None and (not isinstance(source_ids, (list, tuple, set, frozenset))
                                       or any(not isinstance(sid, str) for sid in source_ids)):
            raise ValueError("candidate source_ids must be a sequence")
        sources = sorted(self.rows if source_ids is None else set(source_ids))
        if any(sid not in self.rows for sid in sources):
            raise ValueError("candidate source_ids must belong to the index")
        if (not isinstance(resolved_pairs, (list, tuple, set, frozenset))
                or len(resolved_pairs) > max(1, len(self.rows)) * 256):
            raise ValueError("resolved pairs must be a bounded sequence")
        resolved: set[tuple[str, str]] = set()
        excluded: dict[str, set[str]] = defaultdict(set)
        for pair in resolved_pairs:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(not isinstance(sid, str) or not sid or len(sid) > 256 for sid in pair)
                    or pair[0] == pair[1]):
                raise ValueError("resolved pairs require two distinct source IDs")
            key = tuple(sorted(pair))
            resolved.add(key)
            excluded[key[0]].add(key[1])
            excluded[key[1]].add(key[0])
        stats = {"source_count": len(self.rows), "seed_source_count": len(sources), "resolved_pair_count": len(resolved),
                 "semantic_input_count": len(semantic_neighbors), "unknown_semantic_endpoints": 0,
                 "scored_pairs": 0, "max_source_fanout": 0, "returned_pairs": 0,
                 "bounded_candidate_count": 0, "unexamined_candidate_pairs": 0, "selection_truncated": 0}
        self.last_candidate_stats = stats
        self.last_candidate_pool = []
        if not per_source_cap or not max_pairs:
            stats["selection_truncated"] = int(bool(semantic_neighbors or self.rows))
            return []
        semantic: dict[str, dict[str, tuple[float, dict[str, Any]]]] = defaultdict(dict)

        def order(sid: str, target: str, score: float) -> tuple[float, bool, str]:
            return (-score, bool(set(self.rows[sid]["collection_keys"]) & set(self.rows[target]["collection_keys"])), target)

        for raw in semantic_neighbors:
            if not isinstance(raw, Mapping):
                raise ValueError("semantic neighbor must be a mapping")
            left, right, score = raw.get("source_id"), raw.get("target_source_id"), raw.get("score")
            if (not isinstance(left, str) or not isinstance(right, str) or not left or not right
                    or len(left) > 256 or len(right) > 256 or left == right):
                raise ValueError("semantic neighbor requires distinct bounded source IDs")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("semantic similarity must be finite and between zero and one")
            if left not in self.rows or right not in self.rows:
                stats["unknown_semantic_endpoints"] += 1
                continue
            pair = tuple(sorted((left, right)))
            if not score or pair in resolved:
                continue
            provenance = raw.get("provenance") or {}
            if not isinstance(provenance, Mapping):
                raise ValueError("semantic provenance must be a mapping")
            identity = provenance.get("embedding_identity", "")
            if not isinstance(identity, str) or len(identity) > 256:
                raise ValueError("semantic embedding identity must be bounded text")
            material: dict[str, Any] = {"signal": "semantic_similarity", "source_id": pair[0], "target_source_id": pair[1],
                                        "embedding_identity": identity, "evidence_backed": False, "accepted": False}
            for side, sid in (("source", left), ("target", right)):
                chunks = provenance.get(f"{side}_chunk_ids")
                if chunks is not None:
                    if (not isinstance(chunks, (list, tuple)) or len(chunks) > 100_000
                            or any(not isinstance(value, str) or not value or len(value) > 256 for value in chunks)):
                        raise ValueError("semantic chunk identities must be bounded text sequences")
                    label = "source" if sid == pair[0] else "target"
                    material[f"{label}_chunk_ids_sha256"] = hashlib.sha256(_json(sorted(chunks)).encode("utf-8")).hexdigest()
                    material[f"{label}_chunk_count"] = len(chunks)
            for sid, target in ((left, right), (right, left)):
                bucket = semantic[sid]
                prior = bucket.get(target)
                if prior is not None and (score < prior[0] or (score == prior[0] and _json(material) >= _json(prior[1]))):
                    continue
                bucket[target] = (float(score), material)
                if len(bucket) > fanout:
                    worst = max(bucket, key=lambda other: order(sid, other, bucket[other][0]))
                    del bucket[worst]
                    stats["selection_truncated"] = 1
        proposals: dict[str, list[tuple[str, str]]] = {}
        local_pairs: set[tuple[str, str]] = set()
        semantic_pairs: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        candidate_keys: set[tuple[str, str]] = set()
        for sid in sources:
            semantic_targets = sorted(semantic[sid], key=lambda target: order(sid, target, semantic[sid][target][0]))
            local_targets = self._targets(self._signals(sid), fanout, sid, excluded_targets=excluded[sid])
            local_ranked = []
            for target in local_targets:
                key = tuple(sorted((sid, target)))
                if self._score(*key) > 0:
                    local_ranked.append(key)
                    local_pairs.add(key)
            local_ranked.sort(key=lambda key: (bool(set(self.rows[key[0]]["collection_keys"]) & set(self.rows[key[1]]["collection_keys"])), -self._score(*key), key))
            semantic_ranked = []
            for target in semantic_targets:
                key = tuple(sorted((sid, target)))
                semantic_ranked.append(key)
                value = semantic[sid][target]
                if key not in semantic_pairs or value[0] > semantic_pairs[key][0]:
                    semantic_pairs[key] = value
            merged: dict[tuple[str, str], None] = {}
            for semantic_key, local_key in zip_longest(semantic_ranked, local_ranked):
                for key in (semantic_key, local_key):
                    if key is not None and key not in resolved:
                        merged[key] = None
                    if len(merged) >= fanout:
                        break
                if len(merged) >= fanout:
                    break
            proposals[sid] = list(merged)
            candidate_keys.update(merged)
            stats["max_source_fanout"] = max(stats["max_source_fanout"], len(merged))
            if len(local_targets) >= fanout:
                stats["selection_truncated"] = 1
        counts: Counter[str] = Counter()
        selected: dict[tuple[str, str], None] = {}
        sources.sort(key=lambda sid: (min((self.collection_sizes[key] for key in self.rows[sid]["collection_keys"]), default=0), sid))
        for _ in range(min(per_source_cap, len(self.rows))):
            before = len(selected)
            for sid in sources:
                if counts[sid] >= per_source_cap:
                    continue
                for key in proposals[sid]:
                    if key not in selected and all(counts[endpoint] < per_source_cap for endpoint in key):
                        selected[key] = None
                        counts.update(key)
                        break
                if len(selected) >= max_pairs:
                    break
            if len(selected) >= max_pairs or len(selected) == before:
                break
        # Retain the complete bounded discovery pool independently of this pass's
        # endpoint/payment allocation. Round-robin proposal order stays stable.
        ordered_pool: dict[tuple[str, str], None] = {}
        for rank in range(fanout):
            for sid in sources:
                if rank < len(proposals[sid]):
                    ordered_pool[proposals[sid][rank]] = None
        if retain_pool and len(ordered_pool) > 100_000:
            raise CandidateResourceError("candidate pool exceeds the bounded queue; reduce source scope")
        materialized = {}
        for key in ordered_pool if retain_pool else selected:
            row = self._pair(*key)
            row.update(rank=len(materialized) + 1, local_score=row["score"], discovery_route="deterministic_local_fused",
                       ranking_method="alternating_semantic_local", decision_status="unexamined")
            if key in semantic_pairs:
                score, material = semantic_pairs[key]
                row["semantic_score"] = score
                row["discovery_provenance"] = _pair_provenance([*self._pair_material(*key), material])
                row["why_compare"] = "; ".join(filter(None, ("semantic_similarity", row["why_compare"])))
            row["candidate_routes"] = (["semantic_similarity"] if key in semantic_pairs else []) + (["local_reference_tag_profile"] if key in local_pairs else [])
            materialized[key] = row
        self.last_candidate_pool = list(materialized.values()) if retain_pool else []
        result = [{**materialized[key], "rank": rank} for rank, key in enumerate(selected, 1)]
        stats.update(scored_pairs=len(candidate_keys), bounded_candidate_count=len(candidate_keys), returned_pairs=len(result),
                     unexamined_candidate_pairs=len(candidate_keys) - len(result),
                     selection_truncated=int(bool(stats["selection_truncated"] or len(candidate_keys) > len(result))))
        return result

    def recall(self, query: str, *, relationships: Sequence[Mapping[str, Any]] = (), max_notes: int = 8, max_chars: int = 12_000, max_query_chars: int = 2000, max_query_tokens: int = 64, fanout: int = 32, neighbor_cap: int = 2) -> dict[str, Any]:
        """Return bounded compact notes and one hop of explicitly accepted links.

        ``chars`` counts serialized note and relationship records, excluding this
        small response envelope. Missing locators and coverage remain unknown.
        """
        for name, value in (("max_notes", max_notes), ("max_chars", max_chars), ("max_query_chars", max_query_chars), ("max_query_tokens", max_query_tokens), ("fanout", fanout), ("neighbor_cap", neighbor_cap)):
            _limit(value, name, zero=name == "neighbor_cap")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        terms = _words(query, max_query_chars, max_query_tokens)
        query_literal = _text(query[:max_query_chars])
        query_signals = {("term", term) for term in terms}
        query_signals.update(("tag", value) for value in terms | {query_literal.casefold()} if ("tag", value) in self.postings)
        if ("reference", query_literal) in self.postings:
            query_signals.add(("reference", query_literal))
        signals = sorted(query_signals, key=lambda s: (self.frequencies.get(s, 0), s))
        targets = self._targets(signals, fanout)
        def matches(sid: str) -> list[tuple[str, str]]:
            return [(kind, value) for kind, value in signals if value in {"term": self.words[sid], "tag": self.tags[sid], "reference": self.references[sid]}[kind]]
        ranked = sorted(targets, key=lambda sid: (-sum(1 + math.log((1 + len(self.rows)) / (1 + self.frequencies[signal])) for signal in matches(sid)), sid))
        seed_limit = max(1, max_notes - min(neighbor_cap, max_notes // 2)) if relationships else max_notes
        selected = ranked[:seed_limit]
        links: dict[str, dict[str, Any]] = {}
        adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        if len(relationships) > 100_000:
            raise ValueError("relationship input limit exceeded")
        for raw in relationships:
            if not isinstance(raw, Mapping) or raw.get("active", True) is not True or raw.get("reconciliation_pending") or raw.get("legacy_review_pending") or raw.get("refresh_pending"):
                continue
            if (raw.get("decision_status") or raw.get("status")) != "accepted":
                continue
            left, right = _text(raw.get("source_id"), 256), _text(raw.get("target_source_id") or raw.get("related_source_id"), 256)
            if left not in self.rows or right not in self.rows or left == right:
                continue
            row = {key: _compact(raw[key], 2000) for key in ("relation_id", "relation_type", "reason", "source_evidence", "target_evidence", "provenance", "freshness", "selected_evidence", "decision_policy", "model", "template_version", "evidence_basis") if raw.get(key)}
            for key in ("confidence", "winning_probability", "runner_up_probability", "probability_margin"):
                value = raw.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
                    row[key] = value
            row.update(source_id=left, target_source_id=right, decision_status="accepted")
            for source, target in ((left, right), (right, left)):
                if source in selected:
                    entry = (target, row)
                    if entry not in adjacency[source]:
                        adjacency[source].append(entry)
                        adjacency[source] = sorted(adjacency[source], key=lambda pair: (pair[0], _json(pair[1])))[:neighbor_cap]
        for sid in list(selected):
            for target, row in sorted(adjacency[sid], key=lambda pair: (pair[0], _json(pair[1])))[:neighbor_cap]:
                if target not in selected and len(selected) < max_notes:
                    selected.append(target)
                if target in selected:
                    links[_json(row)] = row
        selected.extend(sid for sid in ranked if sid not in selected and len(selected) < max_notes)
        note_rows: dict[str, dict[str, Any]] = {}
        for sid in selected:
            source = self.rows[sid]
            row = {key: source[key] for key in ("source_id", "note_id", "note_path", "note_hash", "profile_hash", "title", "author", "year", "source_scope", "evidence_eligibility")}
            row.update(coverage=self.coverage[sid] or source.get("coverage") or {"status": "not_examined"}, matched_terms=sorted(terms & self.words[sid]), recall_route="query" if sid in ranked else "accepted_neighbor")
            row["matched_signals"] = [{"signal": kind, "value": value} for kind, value in matches(sid)]
            if source.get("coverage"):
                row["profile_coverage"] = source["coverage"]
            for key in (*_ROLE_FIELDS, "source_references", "evidence_references"):
                if source.get(key):
                    row[key] = source[key]
            if self.references[sid]:
                row["discovery_references"] = [self.references[sid][key] for key in sorted(self.references[sid])[:4]]
            row["provenance"] = {"route": "supplied_compact_profile", "identity": dict(zip(("instance_id", "library_type", "library_id", "item_key"), self.identities[sid])) if sid in self.identities else {}}
            note_rows[sid] = {**row, "thesis": "", "method": ""}
        # Reserve bounded accepted-edge records before allocating excerpts. A
        # full-text-rich query must not crowd out the memory layer's typed links.
        # Their endpoints need room too, otherwise the edge cannot be inspected.
        reserved_links, required, edge_chars = [], set(), 0
        base_sizes = {sid: len(_json(row)) for sid, row in note_rows.items()}
        for row in sorted(links.values(), key=_json):
            endpoints = {row["source_id"], row["target_source_id"]}
            if not endpoints <= note_rows.keys():
                continue
            size = len(_json(row))
            needed = required | endpoints
            if edge_chars + size + sum(base_sizes[sid] for sid in needed) <= max_chars:
                reserved_links.append(row)
                edge_chars += size
                required = needed
        note_budget = max_chars - edge_chars
        ordered = [sid for sid in selected if sid in required] + [sid for sid in selected if sid not in required]
        notes: list[dict[str, Any]] = []
        chars = 0
        pending_required = set(required)
        for sid in ordered:
            row = note_rows[sid]
            source = self.rows[sid]
            pending_required.discard(sid)
            remaining = note_budget - chars - base_sizes[sid] - sum(base_sizes[other] for other in pending_required)
            if remaining < 0:
                continue
            row["thesis"] = source["thesis"][:min(2000, remaining // 2)]
            row["method"] = source["method"][:min(1000, remaining // 4)]
            while len(_json(row)) > base_sizes[sid] + remaining and (row["thesis"] or row["method"]):
                field = "thesis" if len(row["thesis"]) >= len(row["method"]) else "method"
                row[field] = row[field][:len(row[field]) // 2]
            if chars + len(_json(row)) <= note_budget:
                notes.append(row)
                chars += len(_json(row))
        included = {row["source_id"] for row in notes}
        kept_links = []
        for row in reserved_links:
            if {row["source_id"], row["target_source_id"]} <= included and chars + len(_json(row)) <= max_chars:
                kept_links.append(row)
                chars += len(_json(row))
        query_tokens = list(islice((m for m in re.finditer(r"[^\W_]+", query[:max_query_chars].casefold()) if len(m.group()) > 1 and m.group() not in _STOP_WORDS), max_query_tokens + 1))
        truncated = len(query) > max_query_chars or len(query_tokens) > max_query_tokens or len(notes) < len(ranked) or len(kept_links) < len(links) or len(targets) >= fanout or any(self.frequencies.get(s, 0) > len(self.postings.get(s, [])) for s in signals)
        return {"notes": notes, "relationships": kept_links, "chars": chars, "truncated": truncated, "query_terms": sorted(terms), "discovery_route": "deterministic_local_ranked"}
